"""FREE-IMPORT-AI-PROPOSAL-01 — real-browser qualification of the
free-import + explicit-AI-proposal product lifecycle.

This harness first ran as PRODUCT-LIFECYCLE-E2E-01 (frozen FAIL /
NO-COMMIT — artifacts preserved under
tests/_qual_artifacts/PRODUCT-LIFECYCLE-E2E-01/) and then as E2E-02.
This phase supersedes the auto-enrichment lifecycle: import/upload/
staging/manual-edit must invoke ZERO model/provider calls; the first
allowed invocation happens only after the user presses เริ่มใช้ AI.

Verification-only: this file changes no production behavior.  It runs the
real FastAPI app under a real uvicorn server against an isolated tmp
project root, with real Chromium, and records:

  - a FakeLLM call ledger — must stay EMPTY for every free action; the
    only entries allowed are the explicit AI-proposal calls
    (metadata_summary / analyze_product_positioning);
  - an outbound-network attempt ledger (must stay empty);
  - per-journey screenshots + Playwright traces;
  - browser console errors, page errors, and HTTP responses.

Safety contract honored:
  - $0 budget — no OpenRouter/LLM/provider/System81/live-web calls.
  - `gateway.get_api_key` returns "" so any real provider path fails closed
    instead of dialing out; `_make_llm` returns a deterministic FakeLLM.
  - `src.url_import.fetch_product_page` is stubbed (deterministic local
    page) — the stub seam matches what the endpoint calls.
  - httpx-level network guard: any outbound httpx call raises + is
    recorded; browser `context.route` aborts non-localhost requests.
  - Only `web_viewer.PROJECT_ROOT` + auth singletons are patched, so the
    REAL middleware → BrandRegistry → brand_state_root chain resolves
    brand-scoped state under tmp/users/<uid>/brands/<bid>/ naturally.
    staging/product_db/ingestion roots are NOT patched (unlike the older
    staging-review suite, whose direct `_project_root` patch bypassed
    brand scoping).
  - Login UI is System81-only; the supported dev boundary is
    MKTAPP_DEV_AUTH=1 + /api/auth/login via the browser's own request
    stack (context.request), so cookies land in the real browser jar.

Artifacts: tests/_qual_artifacts/FREE-IMPORT-AI-PROPOSAL-01/
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import urllib.parse

import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

ARTIFACTS = (
    Path(__file__).resolve().parent
    / "_qual_artifacts"
    / "FREE-IMPORT-AI-PROPOSAL-01"
)


# ---------------------------------------------------------------------------
# Deterministic FakeLLM
# ---------------------------------------------------------------------------

class FakeLLM:
    """Deterministic stand-in for LLMClient — records every call in a shared
    ledger and answers by `source` label through a per-test responder.

    Ledger entry: {seq, source, markers, thread, prompt_chars}.
    `markers` = which unique product evidence markers appear in the prompt —
    this is what proves selected-only enrichment and no cross-product bleed.
    """

    # Unique evidence markers planted in fixtures; searched in every prompt.
    MARKERS = ("ALPHA-UNIQ", "BRAVO-UNIQ", "CHARLIE-UNIQ",
               "URL-ALPHA", "URL-BRAVO", "PD-UNIQ", "G-SEED")

    def __init__(self, ledger: list, responder):
        self._ledger = ledger
        self._responder = responder
        self.closed = False

    def chat(self, messages, *, source="unknown", **kwargs):
        prompt_text = "\n".join(str(m.get("content", "")) for m in messages)
        markers = [mk for mk in self.MARKERS if mk in prompt_text]
        entry = {
            "seq": len(self._ledger),
            "source": source,
            "markers": markers,
            "thread": threading.current_thread().name,
            "prompt_chars": len(prompt_text),
            "response_format": bool(kwargs.get("response_format")),
        }
        self._ledger.append(entry)
        return self._responder(source, messages, entry)

    def close(self):
        self.closed = True
        self._ledger.append({"seq": len(self._ledger), "source": "__close__"})


def _default_responder(server):
    """Default FakeLLM responder — deterministic per source."""
    def respond(source, messages, entry):
        if source == "product_segmentation.segment_products":
            plan = server.seg_plan
            if callable(plan):
                return plan(messages)
            if plan is None:
                return json.dumps({"products": []})
            return json.dumps({"products": plan})

        if source == "ingestion.metadata_summary":
            if callable(server.summary_responder):
                return server.summary_responder(messages, entry)
            mk = entry["markers"][0] if entry["markers"] else "UNKNOWN"
            return json.dumps({
                "summary": f"สรุปสินค้า {mk} — deterministic test summary",
                "category": "Qualification Category",
                "derived_facts": {
                    "qual_marker": {"label": "ตัวระบุคุณสมบัติ", "value": mk},
                },
            })

        if source == "voice_learner.analyze_product_positioning":
            if callable(server.profile_responder):
                return server.profile_responder(messages, entry)
            return json.dumps({
                "price_tier": "mid",
                "differentiators": ["qual-diff"],
                "use_cases": ["qual-use"],
                "tone_adjustment": "qual-tone",
            })

        return "{}"

    return respond


# ---------------------------------------------------------------------------
# Outbound network guard
# ---------------------------------------------------------------------------

class _NetGuard:
    """Fail-closed outbound HTTP guard — any httpx call raises + records.

    All external paths in this app go through httpx (openrouter gateway,
    system81 userinfo, url_import static fetch, voice_learner url fetch).
    In-process patching covers the app AND the uvicorn thread.
    """

    def __init__(self):
        self.attempts: list = []
        self._orig = {}

    def install(self):
        import httpx

        def _blocked(fn_name):
            def _raise(*a, **kw):
                self.attempts.append({"via": fn_name, "args": str(a)[:200]})
                raise RuntimeError(
                    f"network blocked by qualification guard ({fn_name})"
                )
            return _raise

        for mod, names in (
            (httpx, ["get", "post", "put", "delete", "request", "stream"]),
            (httpx.Client, ["send", "get", "post", "request", "stream"]),
            (httpx.AsyncClient, ["send", "get", "post", "request", "stream"]),
        ):
            for n in names:
                self._orig[(mod, n)] = getattr(mod, n)
                setattr(mod, n, _blocked(f"{mod.__name__}.{n}"))

    def uninstall(self):
        for (mod, n), fn in self._orig.items():
            setattr(mod, n, fn)
        self._orig.clear()


# ---------------------------------------------------------------------------
# Server fixture — real uvicorn against an isolated tmp project root
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_up(url: str, tries: int = 50) -> None:
    for _ in range(tries):
        try:
            urllib.request.urlopen(f"{url}/api/auth/me", timeout=1)
            return
        except urllib.error.HTTPError:
            return  # any HTTP response (incl. 401) means the server is up
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


class _QualServer:
    """Owns a tmp project root + uvicorn + all test seams.

    `restart()` mirrors a real process restart: stops uvicorn (and the lazy
    scheduler), keeps every patch installed, and starts a fresh uvicorn on
    a new port against the same tmp root.  Auth files on disk persist, so
    previously issued sessions stay valid — that IS the persistence test.
    """

    def __init__(self, tmp: Path):
        import web_viewer
        import src.auth as auth_mod
        import src.ingestion as ingestion
        import src.openrouter_gateway as gateway
        import src.url_import as url_import

        self.tmp = tmp
        self.llm_ledger: list = []
        self.net_guard = _NetGuard()
        self.ingest_calls: list = []          # ingest_product invocations
        self.ingest_delay = 0.0               # widen upload→ingest race
        self.seg_plan = None                  # segmentation responder plan
        self.summary_responder = None
        self.profile_responder = None
        self.url_fetch_result = None          # fetch_product_page stub
        self.users: dict = {}

        self._wv = web_viewer
        self._restore: list = []

        def save(obj, name):
            self._restore.append((obj, name, getattr(obj, name)))

        # --- project root + auth singletons under tmp --------------------
        save(web_viewer, "PROJECT_ROOT")
        save(web_viewer, "_scheduler_instance")
        web_viewer.PROJECT_ROOT = tmp
        web_viewer._scheduler_instance = None

        users_path = tmp / "data" / "auth" / "users.json"
        sessions_path = tmp / "data" / "auth" / "sessions.json"
        users_path.parent.mkdir(parents=True, exist_ok=True)
        from src.auth import UserStore, SessionManager
        self.user_store = UserStore(users_path)
        self.session_mgr = SessionManager(sessions_path)
        save(auth_mod, "_user_store")
        save(auth_mod, "_session_manager")
        auth_mod._user_store = self.user_store
        auth_mod._session_manager = self.session_mgr

        # --- provider gate: no key → every real provider path fails closed
        save(gateway, "get_api_key")
        gateway.get_api_key = lambda: ""

        # --- FakeLLM seam: ingestion._make_llm is THE single construction
        #     point for segmentation + enrichment across upload_stage,
        #     product_from_url, commit, and ingest_product. -------------
        save(ingestion, "_make_llm")
        responder = _default_responder(self)
        ingestion._make_llm = lambda: FakeLLM(self.llm_ledger, responder)

        # --- ingest_product invocation counter (double-ingest evidence) --
        save(ingestion, "ingest_product")
        self._real_ingest = ingestion.ingest_product

        def _counting_ingest(product_id, *a, **kw):
            self.ingest_calls.append({
                "product_id": product_id,
                "thread": threading.current_thread().name,
                "t": time.time(),
            })
            if self.ingest_delay:
                time.sleep(self.ingest_delay)
            return self._real_ingest(product_id, *a, **kw)

        ingestion.ingest_product = _counting_ingest

        # --- URL acquisition stub (deterministic local page) ------------
        save(url_import, "fetch_product_page")

        def _stub_fetch(url):
            self.llm_ledger.append({
                "seq": -1,
                "source": "__url_fetch__",
                "url": url,
            })
            if callable(self.url_fetch_result):
                return self.url_fetch_result(url)
            if self.url_fetch_result is None:
                raise url_import.UrlImportError("stub: no fixture configured")
            return self.url_fetch_result

        url_import.fetch_product_page = _stub_fetch

        # --- outbound network guard --------------------------------------
        self.net_guard.install()
        self._start_uvicorn()

    def _start_uvicorn(self):
        import uvicorn

        self.port = _find_free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(
            self._wv.app, host="127.0.0.1", port=self.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        _wait_up(self.url)

    def restart(self):
        """Stop uvicorn + scheduler, start a fresh uvicorn on the same root."""
        self.stop()
        self._start_uvicorn()

    # --- user helpers ---------------------------------------------------
    def register(self, username: str, password: str) -> dict:
        user = self.user_store.register(username, password)
        token = self.session_mgr.create_session(user.user_id)
        return {"user_id": user.user_id, "username": username,
                "password": password, "token": token}

    # --- brand workspace helpers (filesystem-side truth) ----------------
    def brand_root(self, user_id: str, brand_id: str) -> Path:
        return self.tmp / "users" / user_id / "brands" / brand_id

    def brand_data(self, user_id: str, brand_id: str) -> Path:
        return self.brand_root(user_id, brand_id) / "data"

    def brand_cache(self, user_id: str, brand_id: str) -> Path:
        return self.brand_root(user_id, brand_id) / "cache"

    def product_record(self, user_id: str, brand_id: str, name: str) -> dict:
        p = self.brand_cache(user_id, brand_id) / name / "product.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def product_profile(self, user_id: str, brand_id: str, name: str) -> dict:
        p = self.brand_cache(user_id, brand_id) / name / "product_profile.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def data_products(self, user_id: str, brand_id: str) -> list[str]:
        d = self.brand_data(user_id, brand_id)
        if not d.exists():
            return []
        return sorted(
            x.name for x in d.iterdir()
            if x.is_dir() and not x.name.startswith(".")
        )

    def stop(self):
        try:
            self._server.should_exit = True
            self._thread.join(timeout=5)
        finally:
            sched = getattr(self._wv, "_scheduler_instance", None)
            if sched is not None:
                try:
                    sched.stop()
                except Exception:
                    pass
                self._wv._scheduler_instance = None

    def teardown(self):
        self.net_guard.uninstall()
        for obj, name, val in self._restore:
            setattr(obj, name, val)
        self._restore.clear()


@pytest.fixture(scope="module")
def qual(tmp_path_factory):
    """Module-scoped isolated server + users A and B (registered via the
    real dev-auth API boundary, sessions created in tmp auth storage)."""
    tmp = tmp_path_factory.mktemp("lifecycle_qual")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    server = _QualServer(tmp)
    try:
        ua = server.register("qual_user_a", "qual_pass_a")
        ub = server.register("qual_user_b", "qual_pass_b")
        server.users = {"A": ua, "B": ub}
        yield server
    finally:
        server.stop()
        server.teardown()


@pytest.fixture(scope="module", autouse=True)
def _qual_summary(qual):
    yield
    (ARTIFACTS / "llm_ledger.json").write_text(
        json.dumps(qual.llm_ledger, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (ARTIFACTS / "network_attempts.json").write_text(
        json.dumps(qual.net_guard.attempts, indent=2),
        encoding="utf-8")


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def _launch(pw, server: _QualServer, session_token: str | None = None,
            name: str = "ctx"):
    """Fresh Chromium context: network route guard + tracing + dialog
    capture. Only the session cookie is injected when given — never the
    brand cookie. `env["dialog_dismiss"]` flips dialogs to dismiss."""
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
    except Exception:
        pass

    env = {
        "browser": browser, "context": context, "name": name,
        "console_errors": [], "http_responses": [], "blocked": [],
        "dialogs": [], "dialog_dismiss": False,
    }

    def _route(route):
        u = route.request.url
        if u.startswith(("http://127.0.0.1:", "http://localhost:")):
            route.continue_()
        elif not u.startswith(("http://", "https://")):
            route.continue_()
        else:
            env["blocked"].append(u)
            route.abort()

    context.route("**/*", _route)

    if session_token:
        context.add_cookies([{
            "name": "mktapp_session", "value": session_token,
            "url": server.url,
        }])

    page = context.new_page()
    env["page"] = page
    page.on("console", lambda m: env["console_errors"].append(m.text)
            if m.type == "error" else None)
    page.on("pageerror", lambda e: env["console_errors"].append(str(e)))

    def _dialog(d):
        env["dialogs"].append(d.message)
        if env["dialog_dismiss"]:
            d.dismiss()
        else:
            d.accept()

    page.on("dialog", _dialog)

    def _record(resp):
        try:
            env["http_responses"].append(
                {"url": resp.url, "status": resp.status})
        except Exception:
            pass

    page.on("response", _record)
    return env


def _close(env: dict, tag: str = "") -> None:
    try:
        trace_path = ARTIFACTS / f"trace_{env['name']}{tag}.zip"
        env["context"].tracing.stop(path=str(trace_path))
    except Exception:
        pass
    for obj in (env["context"], env["browser"]):
        try:
            obj.close()
        except Exception:
            pass


def _shot(env: dict, name: str) -> str:
    p = ARTIFACTS / f"{env['name']}_{name}.png"
    try:
        env["page"].screenshot(path=str(p), full_page=False)
        return str(p)
    except Exception:
        return ""


def _create_brand_ui(page, name: str) -> None:
    page.fill("#brand-gate-name", name)
    with page.expect_navigation(wait_until="load", timeout=15000):
        page.click("#brand-gate-create")
    page.wait_for_load_state("networkidle")


def _select_brand_ui(page, brand_id: str) -> None:
    row = page.wait_for_selector(
        f".brand-gate-item[data-brand-id='{brand_id}']", timeout=10000)
    btn = row.query_selector(".brand-gate-select-btn")
    with page.expect_navigation(wait_until="load", timeout=15000):
        btn.click()
    page.wait_for_load_state("networkidle")


def _open_picker(page) -> None:
    page.click("#brand-switcher-btn")
    page.wait_for_selector("#brand-gate-overlay.visible", timeout=10000)


def _wait_workspace(page, timeout: int = 15000) -> None:
    page.wait_for_selector("#brand-switcher-btn", state="visible",
                           timeout=timeout)


def _active_brand(env: dict) -> str:
    cookies = env["context"].cookies()
    bid = next((c["value"] for c in cookies
                if c["name"] == "mktapp_brand"), None)
    assert bid, "mktapp_brand cookie must be present"
    return bid


def _wait_folder(page, name: str, timeout: int = 20000) -> None:
    page.wait_for_selector(
        f".folder-item[data-folder='{name}']", timeout=timeout)


def _wait_ready(server: _QualServer, uid: str, bid: str, name: str,
                timeout: float = 30, prev_updated: str | None = None) -> dict:
    """Poll product.json until an ingest cycle settles.

    prev_updated: when re-ingesting an already-ready product, first wait for
    the record's ``updated_at`` to change — proves the new cycle actually
    started before reading a stale "ready"."""
    deadline = time.time() + timeout
    if prev_updated is not None:
        started = time.time() + 15
        while time.time() < started:
            rec = server.product_record(uid, bid, name)
            if rec.get("updated_at") and rec["updated_at"] != prev_updated:
                break
            time.sleep(0.2)
    while time.time() < deadline:
        rec = server.product_record(uid, bid, name)
        if rec.get("status") in ("ready", "no_usable_data", "stale"):
            return rec
        time.sleep(0.4)
    return server.product_record(uid, bid, name)


def _api(server: _QualServer, method: str, path: str,
         session: str | None = None, brand: str | None = None,
         json_body=None, raw: bool = False):
    """Direct API call for adversarial checks (urllib → (status, body)).
    `path` must already be URL-encoded where it embeds names."""
    url = f"{server.url}{path}"
    headers = {"Content-Type": "application/json"}
    cookie = []
    if session:
        cookie.append(f"mktapp_session={session}")
    if brand is not None:
        cookie.append(f"mktapp_brand={brand}")
    if cookie:
        headers["Cookie"] = "; ".join(cookie)
    data = json.dumps(json_body).encode() if json_body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            return resp.status, body if raw else _json_or(body)
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, body if raw else _json_or(body)


def _q(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def _json_or(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return body.decode("utf-8", "replace")


def _upload_multipart(server: _QualServer, path: str,
                      session: str, brand: str,
                      files: list[tuple[str, bytes]],
                      extra_fields: dict | None = None):
    """Minimal multipart POST via urllib (stdlib-only)."""
    boundary = "----qualboundary"
    parts = []
    for k, v in (extra_fields or {}).items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="{k}"\r\n\r\n{v}\r\n'.encode())
    for fname, content in files:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="files"; filename="{fname}"\r\n'
            f"Content-Type: text/plain\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{server.url}{path}", data=b"".join(parts), method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Cookie": f"mktapp_session={session}; mktapp_brand={brand}",
        })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, _json_or(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _json_or(e.read())


def _stage_via_ui(page, server: _QualServer, fixture_path: Path,
                  product_name: str = "") -> None:
    """Real DOM path: open Add Product → attach file → in-section Upload →
    deterministic staging preview visible (FREE-IMPORT: one single-product
    segment, zero model calls)."""
    page.wait_for_selector("button.sidebar-add-btn", timeout=10000)
    page.click("button.sidebar-add-btn")
    page.wait_for_selector("#upload-overlay.visible", timeout=10000)
    page.set_input_files("#upload-files-modal", str(fixture_path))
    page.click("#add-upload-btn")
    page.wait_for_selector("#staging-preview-modal >> visible=true",
                           timeout=20000)
    if product_name:
        page.fill("input[data-seg-name='0']", product_name)


# ---------------------------------------------------------------------------
# Deterministic fixture catalog — 3 products, unique markers, shared header,
# one blank-line page boundary.
# ---------------------------------------------------------------------------

def _catalog_text() -> str:
    """Line layout (1-based):
      1-3   shared header (common_refs)
      4-9   ALPHA block      (page 1)
      10    blank line       (page separator — splits on blank line)
      11-16 BRAVO block      (page 2)
      17-22 CHARLIE block    (page 2)
    """
    lines = [
        "QUALCO Product Catalog 2026",                       # 1
        "MOQ: 100 units — all products",                     # 2
        "Payment: 30/70 TT",                                 # 3
        "Product: QualWatch ALPHA-UNIQ Model A",             # 4
        "SKU: QW-A1",                                        # 5
        "Battery: ALPHA-UNIQ-CELL 900mAh",                   # 6
        "Display: 1.4 inch round",                           # 7
        "Price: 1,990 THB",                                  # 8
        "Color: blue only",                                  # 9
        "",                                                  # 10 (page split)
        "Product: QualWatch BRAVO-UNIQ Model B",             # 11
        "SKU: QW-B2",                                        # 12
        "Battery: BRAVO-UNIQ-CELL 1200mAh",                  # 13
        "Display: 1.7 inch square",                          # 14
        "Price: 2,990 THB",                                  # 15
        "Color: black only",                                 # 16
        "Product: QualWatch CHARLIE-UNIQ Model C",           # 17
        "SKU: QW-C3",                                        # 18
        "Battery: CHARLIE-UNIQ-CELL 1500mAh",                # 19
        "Display: 2.0 inch wide",                            # 20
        "Price: 3,990 THB",                                  # 21
        "Color: red only",                                   # 22
    ]
    return "\n".join(lines)


def _llm_calls(server: _QualServer, start: int = 0) -> list[dict]:
    """Model calls since `start`, excluding harness bookkeeping entries
    (local URL-fetch stub, client-close marker)."""
    return [e for e in server.llm_ledger[start:]
            if isinstance(e, dict) and e.get("source") not in
            (None, "__url_fetch__", "__close__")]


# ---------------------------------------------------------------------------
# Journey A — auth, user, and brand isolation through the real UI
# ---------------------------------------------------------------------------

class TestJourneyA:
    """Login UI is System81-only (no dev form); the supported dev/test
    boundary is MKTAPP_DEV_AUTH=1 + /api/auth/{register,login} over the
    real HTTP stack. Brand create/select/switch/logout are all real DOM."""

    def test_a_isolation(self, qual):
        server = qual
        ua, ub = server.users["A"], server.users["B"]
        with sync_playwright() as p:
            env_a = _launch(p, server, ua["token"], name="A_userA")
            env_b = _launch(p, server, ub["token"], name="A_userB")
            pa, pb = env_a["page"], env_b["page"]
            a1 = a2 = b1 = None
            try:
                # --- unauthenticated context → redirected to /login --------
                env_n = _launch(p, server, None, name="A_anon")
                pn = env_n["page"]
                pn.goto(f"{server.url}/", wait_until="networkidle",
                        timeout=15000)
                pn.wait_for_url("**/login", timeout=15000)
                assert "/login" in pn.url
                _shot(env_n, "anon_login_redirect")
                st, body = _api(server, "GET", "/api/data_folders")
                assert st == 401, f"expected 401, got {st}: {body}"
                _close(env_n, "_login")

                # --- user A: brand gate → create A1 → create A2 -----------
                pa.goto(f"{server.url}/", wait_until="networkidle",
                        timeout=15000)
                pa.wait_for_selector("#brand-gate-overlay.visible",
                                     timeout=10000)
                _create_brand_ui(pa, "QualBrandA1")
                _wait_workspace(pa)
                a1 = _active_brand(env_a)
                _open_picker(pa)
                _create_brand_ui(pa, "QualBrandA2")
                _wait_workspace(pa)
                a2 = _active_brand(env_a)
                assert a1 != a2
                _shot(env_a, "two_brands")

                # --- user B: create B1 ------------------------------------
                pb.goto(f"{server.url}/", wait_until="networkidle",
                        timeout=15000)
                pb.wait_for_selector("#brand-gate-overlay.visible",
                                     timeout=10000)
                _create_brand_ui(pb, "QualBrandB1")
                _wait_workspace(pb)
                b1 = _active_brand(env_b)
                assert b1 not in (a1, a2)
                _shot(env_b, "brand")

                # --- picker lists only own brands --------------------------
                _open_picker(pa)
                pa.wait_for_selector(".brand-gate-item", timeout=10000)
                ids_a = {i.get_attribute("data-brand-id")
                         for i in pa.query_selector_all(".brand-gate-item")}
                assert ids_a == {a1, a2}, f"A saw foreign brands: {ids_a}"
                pa.click("#brand-gate-close")
                pa.wait_for_selector("#brand-gate-overlay", state="hidden")

                _open_picker(pb)
                pb.wait_for_selector(".brand-gate-item", timeout=10000)
                ids_b = {i.get_attribute("data-brand-id")
                         for i in pb.query_selector_all(".brand-gate-item")}
                assert ids_b == {b1}, f"B saw foreign brands: {ids_b}"
                pb.click("#brand-gate-close")
                pb.wait_for_selector("#brand-gate-overlay", state="hidden")

                # --- API: forged foreign brand → 403 ------------------------
                st, _ = _api(server, "GET", "/api/data_folders",
                             session=ua["token"], brand=b1)
                assert st == 403, f"forged foreign brand → {st}"
                st, body = _api(server, "GET", "/api/folder_files/x",
                                session=ub["token"], brand=b1)
                # missing folder → 200 + error dict (not a list)
                assert not (isinstance(body, list) and body), (st, body)

                # --- forged brand cookie in the BROWSER fails closed -------
                env_a["context"].add_cookies([{
                    "name": "mktapp_brand", "value": b1, "url": server.url}])
                pa.reload()
                pa.wait_for_load_state("networkidle")
                pa.wait_for_selector("#brand-gate-overlay.visible",
                                     timeout=10000)
                assert "QualBrandB1" not in pa.inner_text("body")
                _shot(env_a, "forged_brand_gate")

                # --- brand persistence across refresh ----------------------
                _select_brand_ui(pa, a1)
                _wait_workspace(pa)
                pa.reload()
                pa.wait_for_load_state("networkidle")
                _wait_workspace(pa)
                assert "QualBrandA1" in \
                    pa.inner_text("#brand-switcher-btn")
                assert _active_brand(env_a) == a1

                # --- logout → /login; session dies --------------------------
                pa.click("#logout-btn")
                pa.wait_for_url("**/login", timeout=15000)
                _shot(env_a, "logged_out")
                st, _ = _api(server, "GET", "/api/data_folders",
                             session=ua["token"])
                assert st == 401, f"post-logout session must die: {st}"
                # The revoked token is dead for good — issue a fresh session
                # for the remaining journeys (still A's identity).
                ua["token"] = server.session_mgr.create_session(ua["user_id"])

                # --- fresh login restores only A's brands ------------------
                env_a2 = _launch(p, server, None, name="A_relogin")
                resp = env_a2["context"].request.post(
                    f"{server.url}/api/auth/login",
                    data={"username": ua["username"],
                          "password": ua["password"]})
                assert resp.status == 200
                pa2 = env_a2["page"]
                pa2.goto(f"{server.url}/", wait_until="networkidle",
                         timeout=15000)
                pa2.wait_for_selector("#brand-gate-overlay.visible",
                                      timeout=10000)
                pa2.wait_for_selector(".brand-gate-item", timeout=10000)
                ids = {i.get_attribute("data-brand-id")
                       for i in pa2.query_selector_all(".brand-gate-item")}
                assert ids == {a1, a2}, f"relogin brands wrong: {ids}"
                _select_brand_ui(pa2, a2)
                _wait_workspace(pa2)
                assert _active_brand(env_a2) == a2
                _shot(env_a2, "relogged_in")
                _close(env_a2, "_done")
            finally:
                ua["brand_ids"] = {"A1": a1, "A2": a2}
                ub["brand_ids"] = {"B1": b1}
                _close(env_a)
                _close(env_b)

        assert a1 and a2 and b1
        assert not env_a["blocked"], f"browser outbound: {env_a['blocked']}"
        assert not env_b["blocked"], f"browser outbound: {env_b['blocked']}"


# ---------------------------------------------------------------------------
# Journey B — free deterministic file import: Add Product modal geometry →
# staging preview → commit → ready, ZERO model calls; cancel leaves nothing.
# ---------------------------------------------------------------------------

class TestJourneyB:

    def test_b_free_import_and_cancel(self, qual):
        """FREE-IMPORT contract: catalog file → deterministic ONE-product
        staging preview → commit → ready, with ZERO model calls the whole
        way.  A multi-product file is imported as one editable product —
        automatic splitting is out of scope for this phase."""
        server = qual
        ua = server.users["A"]
        a1 = ua["brand_ids"]["A1"]
        uid = ua["user_id"]
        fname = "qualco_catalog.txt"
        catalog = _catalog_text()

        with sync_playwright() as p:
            env = _launch(p, server, ua["token"], name="B_upload")
            page = env["page"]
            try:
                page.goto(f"{server.url}/", wait_until="networkidle",
                          timeout=15000)
                if page.query_selector("#brand-gate-overlay.visible"):
                    _select_brand_ui(page, a1)
                _wait_workspace(page)

                # --- Add Product modal geometry (Product Owner contract) --
                # header เพิ่มสินค้า + ×, segmented file/URL toggle, exactly
                # one mode visible, actions inside their section, NO bottom
                # cancel footer in add mode.
                page.wait_for_selector("button.sidebar-add-btn",
                                       timeout=10000)
                page.click("button.sidebar-add-btn")
                page.wait_for_selector("#upload-overlay.visible",
                                       timeout=10000)
                assert "เพิ่มสินค้า" in page.inner_text(
                    "#upload-modal-title")
                footer_disp = page.evaluate(
                    "getComputedStyle("
                    "document.getElementById('upload-modal-footer')).display")
                assert footer_disp == "none", \
                    f"add mode must hide shared footer: {footer_disp}"
                assert page.locator("#upload-mode-toggle").is_visible()
                # default: file mode only
                assert page.locator("#upload-file-mode").is_visible()
                assert not page.locator("#url-import-block").is_visible()
                # toggle → url mode only; import action inside URL section
                page.click("#upload-mode-url")
                assert page.locator("#url-import-block").is_visible()
                assert not page.locator("#upload-file-mode").is_visible()
                assert page.locator(
                    "#url-import-block #import-url-btn").is_visible()
                # back to file; upload action inside file section
                page.click("#upload-mode-file")
                assert page.locator("#upload-file-mode").is_visible()
                assert not page.locator("#url-import-block").is_visible()
                assert page.locator(
                    "#upload-file-mode #add-upload-btn").is_visible()
                _shot(env, "modal_geometry")
                # × closes cleanly (no dirty state yet)
                page.click("#upload-overlay button[title='ปิด']")
                page.wait_for_selector("#upload-overlay.visible",
                                       state="hidden", timeout=5000)

                # --- open Add Product via real DOM; upload catalog --------
                cat_path = server.tmp / "_fixtures" / fname
                cat_path.parent.mkdir(parents=True, exist_ok=True)
                cat_path.write_text(catalog, encoding="utf-8")
                ledger_before = len(server.llm_ledger)
                _stage_via_ui(page, server, cat_path,
                              product_name="QualWatch Alpha")

                # --- staging preview BEFORE any product materialization ---
                _shot(env, "staging_preview")
                assert server.data_products(uid, a1) == [], \
                    "product materialized before human confirmation"
                # deterministic single-product import — exactly one choice
                seg_count = page.locator(".seg-toggle[data-seg-action]").count()
                assert seg_count == 1, \
                    f"expected 1 deterministic segment, got {seg_count}"

                page.locator(".seg-toggle[data-seg-action='0'] label",
                             has_text="เพิ่ม").click()
                page.click("#staging-confirm-btn")
                page.wait_for_function(
                    "document.querySelector('#upload-modal-status')"
                    ".textContent.includes('บันทึกเรียบร้อย')",
                    timeout=30000)
                _shot(env, "committed")

                # --- exactly one product materialized, zero model calls ---
                prods = server.data_products(uid, a1)
                assert "QualWatch Alpha" in prods, prods
                new_calls = server.llm_ledger[ledger_before:]
                llm_calls = [c for c in new_calls
                             if c.get("source") != "__url_fetch__"]
                assert llm_calls == [], \
                    f"free import invoked the model: {llm_calls}"

                rec = server.product_record(uid, a1, "QualWatch Alpha")
                assert rec.get("status") == "ready", \
                    f"not ready: {rec.get('status')}"
                raw = rec.get("raw_text", "")
                assert "ALPHA-UNIQ" in raw
                # the whole catalog is this one product's source — no split
                assert "BRAVO-UNIQ" in raw and "CHARLIE-UNIQ" in raw
                # free import: no AI-derived facts
                assert not (rec.get("derived_facts") or {})

                _wait_folder(page, "QualWatch Alpha")
                _shot(env, "sidebar_after_commit")

                # ================= cancel path ============================
                page.wait_for_selector("button.sidebar-add-btn",
                                       timeout=10000)
                page.click("button.sidebar-add-btn")
                page.wait_for_selector("#upload-overlay.visible",
                                       timeout=10000)
                cat2 = server.tmp / "_fixtures" / "catalog2.txt"
                cat2.write_text(catalog.replace("QUALCO", "QUALCO2"),
                                encoding="utf-8")
                page.set_input_files("#upload-files-modal", str(cat2))
                page.click("#add-upload-btn")
                page.wait_for_selector(
                    "#staging-preview-modal >> visible=true", timeout=20000)
                before = set(server.data_products(uid, a1))
                staging_root = server.brand_data(uid, a1) / ".staging"
                staging_before = set(staging_root.iterdir()) \
                    if staging_root.exists() else set()
                lb = len(server.llm_ledger)
                page.click("#staging-preview-modal >> text=ยกเลิก")
                time.sleep(0.6)
                after = set(server.data_products(uid, a1))
                assert after == before, \
                    f"cancel materialized products: {after - before}"
                staging_after = set(staging_root.iterdir()) \
                    if staging_root.exists() else set()
                assert not (staging_after - staging_before), \
                    f"cancel left a batch dir: {staging_after - staging_before}"
                llm_cancel = [c for c in server.llm_ledger[lb:]
                              if c.get("source") != "__url_fetch__"]
                assert not llm_cancel, f"cancel invoked the model: {llm_cancel}"
                _shot(env, "cancelled")
            finally:
                _close(env)

        (ARTIFACTS / "journeyB_llm_ledger.json").write_text(
            json.dumps(server.llm_ledger, ensure_ascii=False, indent=2),
            encoding="utf-8")
        assert not env["blocked"]


# ---------------------------------------------------------------------------
# Journey C — URL import parity through the same staging/review surface.
# ---------------------------------------------------------------------------

class TestJourneyC:

    def test_c_url_import_staging(self, qual):
        server = qual
        ua = server.users["A"]
        a1 = ua["brand_ids"]["A1"]
        uid = ua["user_id"]

        # FREE-IMPORT URL contract: deterministic structured-page
        # classification only — exactly-one-product evidence → stage for
        # review; multi/ambiguous → reject with zero products and zero
        # model calls.  This journey proves the accepted single-product
        # path end-to-end (review → commit → Media & Sources thumbnails →
        # persistence) AND both rejection paths.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42m"
            "P8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")  # valid 1×1 PNG

        single_text = (
            "ACME Shop — Product Page\n"          # 1
            "Shipping: nationwide\n"              # 2
            "\n"                                  # 3
            "Product: URL-ALPHA Gadget One\n"     # 4
            "Price: 500 THB\n"                    # 5
            "Spec: URL-ALPHA-SPEC titanium\n"     # 6
        )
        multi_text = (
            "ACME Shop — Catalog Page\n"
            "Shipping: nationwide\n"
            "\n"
            "Product: URL-ALPHA Gadget One\n"
            "Price: 500 THB\n"
            "Spec: URL-ALPHA-SPEC titanium\n"
            "\n"
            "Product: URL-BRAVO Gadget Two\n"
            "Price: 900 THB\n"
            "Spec: URL-BRAVO-SPEC carbon\n"
        )
        cur = {"text": single_text, "page_class": "single"}
        server.url_fetch_result = lambda url: {
            "original_url": url, "final_url": url,
            "canonical_url": url,
            "fetched_via": "static",
            "page_title": "ACME Shop", "og_title": "ACME Shop",
            "text": cur["text"],
            # deterministic structured-page classification (JSON-LD) —
            # never a model call
            "page_class": cur["page_class"],
            "page_signals": {"product_count": 1, "multi_signals": []},
            # Two real PNG source images — for a single-product page they
            # are legitimately owned media (no cross-product ambiguity)
            # and must render as PD Media & Sources thumbnails.
            "images": [
                {"name": "page_img_001.png", "content": png,
                 "source_url": "https://qual-example.invalid/img1.png"},
                {"name": "page_img_002.png", "content": png,
                 "source_url": "https://qual-example.invalid/img2.png"},
            ],
            "fetched_at": "2026-09-15T00:00:00",
        }

        with sync_playwright() as p:
            env = _launch(p, server, ua["token"], name="C_url")
            page = env["page"]
            try:
                page.goto(f"{server.url}/", wait_until="networkidle",
                          timeout=15000)
                if page.query_selector("#brand-gate-overlay.visible"):
                    _select_brand_ui(page, a1)
                _wait_workspace(page)

                page.wait_for_selector("button.sidebar-add-btn",
                                       timeout=10000)
                page.click("button.sidebar-add-btn")
                page.wait_for_selector("#upload-overlay.visible",
                                       timeout=10000)
                # URL mode — segmented toggle shows one mode at a time
                page.click("#upload-mode-url")
                page.fill("#import-url-input",
                          "https://qual-example.invalid/catalog")
                lb = len(server.llm_ledger)
                page.click("#import-url-btn")
                page.wait_for_selector(
                    "#staging-preview-modal >> visible=true", timeout=20000)
                _shot(env, "url_staged")

                existing = set(server.data_products(uid, a1))
                assert "URL Alpha Gadget" not in existing

                # FREE-IMPORT: classification + staging were deterministic —
                # zero model calls since the journey began
                llm_calls = [c for c in server.llm_ledger[lb:]
                             if c.get("source") != "__url_fetch__"]
                assert llm_calls == [], \
                    f"free URL import invoked the model: {llm_calls}"

                # Exactly one segment is offered for review — the contract
                # allows review of the single detected product, never a
                # multi-product picker.
                toggles = page.locator(
                    "#staging-preview-modal .seg-toggle[data-seg-action]")
                assert toggles.count() == 1, \
                    f"single-product URL must stage exactly 1 choice: {toggles.count()}"
                page.fill("input[data-seg-name='0']", "URL Alpha Gadget")
                toggles.locator("label", has_text="เพิ่ม").click()
                page.click("#staging-confirm-btn")
                page.wait_for_function(
                    "document.querySelector('#upload-modal-status')"
                    ".textContent.includes('บันทึกเรียบร้อย')",
                    timeout=30000)

                prods = server.data_products(uid, a1)
                assert "URL Alpha Gadget" in prods, prods
                assert "URL Bravo Gadget" not in prods

                # --- API-level explicit-name check: POST /api/product_from_url
                #     with product_name — after remediation the validated name
                #     becomes the staged segment's suggested_name (the preview
                #     default the human reviews); commit without an override
                #     name must materialize under it.
                st, body = _api(
                    server, "POST", "/api/product_from_url",
                    session=ua["token"], brand=a1,
                    json_body={"url": "https://qual-example.invalid/x",
                               "product_name": "My Explicit Name"})
                explicit_result = {
                    "stage_status": st,
                    "explicit_name_in_products": False,
                }
                if st == 200 and body.get("batch_id"):
                    explicit_result["staged_suggested_name"] = (
                        body.get("segments") or [{}])[0].get("suggested_name")
                    st2, cbody = _api(
                        server, "POST",
                        f"/api/stage/{body['batch_id']}/commit",
                        session=ua["token"], brand=a1,
                        json_body={"choices": [
                            {"segment_index": 0, "action": "create"}]})
                    explicit_result["commit_status"] = st2
                    explicit_result["commit_body"] = str(cbody)[:200]
                    explicit_result["explicit_name_in_products"] = \
                        "My Explicit Name" in server.data_products(uid, a1)
                    # clean up the extra product so later journeys are stable
                    for pname in ("My Explicit Name", "URL Alpha Gadget (1)"):
                        if pname in server.data_products(uid, a1):
                            import shutil as _sh
                            _sh.rmtree(server.brand_data(uid, a1) / pname,
                                       ignore_errors=True)
                            _sh.rmtree(server.brand_cache(uid, a1) / pname,
                                       ignore_errors=True)
                (ARTIFACTS / "journeyC_explicit_name.json").write_text(
                    json.dumps({"explicit_name_honored_via_api":
                                explicit_result["explicit_name_in_products"],
                                "explicit_result": explicit_result},
                               indent=2), encoding="utf-8")
                assert explicit_result["explicit_name_in_products"], \
                    f"product_name not honored: {explicit_result}"

                # FREE-IMPORT: still zero model calls after commit
                llm_calls = [c for c in server.llm_ledger[lb:]
                             if c.get("source") != "__url_fetch__"]
                assert llm_calls == [], \
                    f"URL commit invoked the model: {llm_calls}"

                rec = server.product_record(uid, a1, "URL Alpha Gadget")
                assert rec.get("status") == "ready"
                assert "URL-ALPHA" in rec.get("raw_text", "")
                si = rec.get("source_import") or {}
                assert si.get("original_url") == \
                    "https://qual-example.invalid/catalog", si

                # ---- single-product owned media -------------------------
                # The whole accepted page represents this one product, so
                # its page images are legitimately OWNED media (not
                # unassigned_source_media) — persisted inside the product
                # dir and returned by the product-facing accessor.
                # Free import: no AI-derived facts exist yet.
                assert not (rec.get("derived_facts") or {})
                imgs = rec.get("image_descriptions", [])
                assert len(imgs) == 2, f"images: {imgs}"
                for img in imgs:
                    assert not img.get("unassigned_source_media"), \
                        f"single-product URL image wrongly unassigned: {img}"
                    ipath = img.get("path")
                    assert ipath and Path(ipath).exists(), \
                        f"dead path: {ipath}"
                    assert str(server.brand_data(uid, a1)
                               / "URL Alpha Gadget") in ipath, ipath
                assert (rec.get("metadata") or {}).get("image_count") == 2, \
                    f"image_count: {rec.get('metadata')}"
                # Same filter rule as product_db.get_product_image_paths
                # (that accessor needs an in-process brand context, which
                # only the server thread has): not unassigned + live path.
                owned = [d["path"] for d in imgs
                         if not d.get("unassigned_source_media")
                         and Path(d.get("path") or "").exists()]
                assert len(owned) == 2, f"accessor must return owned media: {owned}"
                _shot(env, "url_committed")

                # ---- PD browser: PI facts + Media & Sources thumbnails ---
                # Free import has no AI facts — the product renders its
                # deterministic source content and image sources as real
                # <img> thumbnails through the secure route, surviving
                # reload.  Product Detail itself must be AI-cost-free.
                lb_pd = len(server.llm_ledger)
                for attempt in range(2):  # 0 = fresh open, 1 = post-reload
                    page.click(
                        ".folder-item[data-folder='URL Alpha Gadget'] "
                        ".folder-info")
                    page.wait_for_selector(
                        "#product-detail-view >> visible=true", timeout=10000)
                    page.wait_for_selector("#pd-body .pd-section",
                                           timeout=10000)
                    body_text = page.inner_text("#pd-body")
                    assert "URL Alpha Gadget" in page.inner_text("#pd-name")
                    # deterministic source evidence renders without AI
                    assert "URL-ALPHA" in body_text
                    assert "URL-BRAVO" not in body_text

                    # expand สื่อและแหล่งข้อมูล → real <img> thumbnails
                    page.click(
                        "#pd-body div[onclick=\"_pdToggleSection('sources')\"]")
                    thumbs = page.locator(
                        "#pd-body img[src*='product_source_file']")
                    page.wait_for_function(
                        "document.querySelectorAll("
                        "\"#pd-body img[src*='product_source_file']\")"
                        ".length >= 2", timeout=10000)
                    assert thumbs.count() == 2, \
                        f"expected 2 image thumbnails: {thumbs.count()}"
                    loaded = page.eval_on_selector_all(
                        "#pd-body img[src*='product_source_file']",
                        "els => els.every(e => e.complete && e.naturalWidth > 0)")
                    assert loaded, "image thumbnails did not actually render"
                    # secure source route serves the bytes behind the thumb
                    st_src, src_body = _api(
                        server, "GET",
                        "/api/product_source_file/URL%20Alpha%20Gadget/"
                        "page_img_001.png",
                        session=ua["token"], brand=a1, raw=True)
                    assert st_src == 200 and src_body == png, \
                        f"source route: {st_src}"
                    if attempt == 0:
                        page.reload()
                        page.wait_for_load_state("networkidle")
                        _wait_workspace(page)
                    else:
                        page.click("#pd-back span")
                        page.wait_for_selector(
                            "#product-detail-view", state="hidden",
                            timeout=10000)
                llm_pd = [c for c in server.llm_ledger[lb_pd:]
                          if c.get("source") != "__url_fetch__"]
                assert llm_pd == [], \
                    f"opening Product Detail invoked the model: {llm_pd}"

                media_evidence = {
                    "URL Alpha Gadget": {
                        "image_descriptions": imgs,
                        "image_count":
                            (rec.get("metadata") or {}).get("image_count"),
                        "owned_image_paths_returned": len(owned),
                        "thumbnails_rendered": 2,
                        "status": rec.get("status"),
                    }
                }
                (ARTIFACTS / "journeyC_media_isolation.json").write_text(
                    json.dumps(media_evidence, ensure_ascii=False,
                               indent=2, default=str), encoding="utf-8")

                # ---- Scenario B: multi-product URL → reject before commit --
                cur["text"] = multi_text
                cur["page_class"] = "multi"
                products_before = set(server.data_products(uid, a1))
                staging_root = server.brand_data(uid, a1) / ".staging"
                staging_before = (sorted(p.name for p in staging_root.iterdir())
                                  if staging_root.exists() else [])
                lb_rej = len(server.llm_ledger)

                page.click("button.sidebar-add-btn")
                page.wait_for_selector("#upload-overlay.visible",
                                       timeout=10000)
                page.click("#upload-mode-url")
                page.fill("#import-url-input",
                          "https://qual-example.invalid/catalog")
                page.click("#import-url-btn")
                page.wait_for_function(
                    "document.querySelector('#upload-modal-status')"
                    ".textContent.includes('หลายสินค้า')", timeout=20000)
                assert "err" in (page.get_attribute(
                    "#upload-modal-status", "class") or "")
                # no product-selection UI is ever presented
                assert not page.locator(
                    "#staging-preview-modal").is_visible()
                _shot(env, "url_multi_rejected")

                # zero materialization / enrichment / leftover staging
                assert set(server.data_products(uid, a1)) == products_before
                staging_after = (sorted(p.name for p in staging_root.iterdir())
                                 if staging_root.exists() else [])
                assert staging_after == staging_before, \
                    f"rejected import left staging state: {staging_after}"
                llm_rej = [c for c in server.llm_ledger[lb_rej:]
                           if c.get("source") != "__url_fetch__"]
                assert llm_rej == [], \
                    f"rejected multi URL invoked the model: {llm_rej}"
                # API-level: same contract for non-UI callers
                st_rej, body_rej = _api(
                    server, "POST", "/api/product_from_url",
                    session=ua["token"], brand=a1,
                    json_body={"url": "https://qual-example.invalid/multi"})
                assert st_rej == 400 and body_rej.get("multi_product") is True

                # ---- Scenario C: undetectable page → fail safe -----------
                cur["page_class"] = "ambiguous"
                lb_amb = len(server.llm_ledger)
                page.fill("#import-url-input",
                          "https://qual-example.invalid/ambiguous")
                page.click("#import-url-btn")
                page.wait_for_function(
                    "document.querySelector('#upload-modal-status')"
                    ".textContent.includes('ไม่สามารถยืนยัน')", timeout=20000)
                assert not page.locator(
                    "#staging-preview-modal").is_visible()
                assert set(server.data_products(uid, a1)) == products_before
                llm_amb = [c for c in server.llm_ledger[lb_amb:]
                           if c.get("source") != "__url_fetch__"]
                assert llm_amb == [], \
                    f"ambiguous URL invoked the model: {llm_amb}"
                _shot(env, "url_ambiguous_rejected")

                # restore the accepted single-product fixture for cancel
                cur["text"] = single_text
                cur["page_class"] = "single"
                page.evaluate("closeUploadModal(true)")
                page.wait_for_selector("#upload-overlay.visible",
                                       state="hidden", timeout=5000)

                # ---- cancel path ------------------------------------------
                page.wait_for_selector("button.sidebar-add-btn",
                                       timeout=10000)
                page.click("button.sidebar-add-btn")
                page.wait_for_selector("#upload-overlay.visible",
                                       timeout=10000)
                page.click("#upload-mode-url")
                page.fill("#import-url-input",
                          "https://qual-example.invalid/other")
                page.click("#import-url-btn")
                page.wait_for_selector(
                    "#staging-preview-modal >> visible=true", timeout=20000)
                before = set(server.data_products(uid, a1))
                lb2 = len(server.llm_ledger)
                page.click("#staging-preview-modal >> text=ยกเลิก")
                time.sleep(0.6)
                assert set(server.data_products(uid, a1)) == before
                llm_cancel = [c for c in server.llm_ledger[lb2:]
                              if c.get("source") != "__url_fetch__"]
                assert llm_cancel == [], \
                    f"URL cancel invoked the model: {llm_cancel}"
            finally:
                server.url_fetch_result = None
                _close(env)
        assert not env["blocked"]


# ---------------------------------------------------------------------------
# Journey D — Product Detail is the single management surface.
# ---------------------------------------------------------------------------

class TestJourneyD:

    def test_d_product_detail_management(self, qual):
        server = qual
        ua = server.users["A"]
        a1 = ua["brand_ids"]["A1"]
        uid = ua["user_id"]
        failures: list = []

        fname = "pd_seed.txt"
        seed = ("Product: PD-UNIQ Detail Widget\n"
                "Price: 111 THB\n"
                "Spec: PD-UNIQ-SPEC alloy body\n")
        seed_path = server.tmp / "_fixtures" / fname
        seed_path.parent.mkdir(parents=True, exist_ok=True)
        seed_path.write_text(seed, encoding="utf-8")

        with sync_playwright() as p:
            env = _launch(p, server, ua["token"], name="D_pd")
            page = env["page"]
            try:
                page.goto(f"{server.url}/", wait_until="networkidle",
                          timeout=15000)
                if page.query_selector("#brand-gate-overlay.visible"):
                    _select_brand_ui(page, a1)
                _wait_workspace(page)

                # --- seed the product through the REAL staging UI ----------
                _stage_via_ui(page, server, seed_path,
                              product_name="PD Detail Widget")
                page.locator(".seg-toggle[data-seg-action='0'] label",
                             has_text="เพิ่ม").click()
                page.click("#staging-confirm-btn")
                page.wait_for_function(
                    "document.querySelector('#upload-modal-status')"
                    ".textContent.includes('บันทึกเรียบร้อย')",
                    timeout=30000)
                _wait_folder(page, "PD Detail Widget")

                # --- path 1: card click → Product Detail -------------------
                page.click(".folder-item[data-folder='PD Detail Widget'] "
                           ".folder-info")
                page.wait_for_selector(
                    "#product-detail-view >> visible=true", timeout=10000)
                # _pdLoad() renders async — wait for the name to populate
                page.wait_for_function(
                    "document.getElementById('pd-name')"
                    ".textContent.length > 0", timeout=10000)
                assert "PD Detail Widget" in page.inner_text("#pd-name")
                _shot(env, "pd_open_card")
                page.click("#pd-back span")
                page.wait_for_selector("#product-detail-view",
                                       state="hidden", timeout=10000)

                # --- path 2: gear ⚙ → Product Detail ------------------------
                page.click(".folder-item[data-folder='PD Detail Widget'] "
                           ".folder-manage-btn")
                page.wait_for_selector(
                    "#product-detail-view >> visible=true", timeout=10000)

                # --- legacy manage modal must be unreachable ---------------
                legacy_refs = page.evaluate(
                    "document.querySelectorAll("
                    "'[onclick*=\"openUploadModalForFolder\"],"
                    "[onclick*=\"openFolderManage\"]').length")
                if legacy_refs != 0:
                    failures.append(
                        f"legacy manage modal wired: {legacy_refs}")

                # --- facts: manual only (free import has no AI facts) -------
                page.locator(".pd-section").first.locator(
                    "text=แก้ไข").click()
                add_input = page.locator("#pd-body .pillar-keyword-add input")
                add_input.wait_for(state="visible", timeout=10000)
                rows_before = page.evaluate(
                    "document.querySelectorAll("
                    "'#pd-facts-container .pd-fact-row').length")
                add_input.fill("color")
                add_input.press("Enter")
                page.wait_for_function(
                    "document.querySelectorAll("
                    "'#pd-facts-container .pd-fact-row').length"
                    f" === {rows_before + 1}", timeout=10000)
                last = page.locator(
                    "#pd-facts-container .pd-fact-row").last
                if last.locator(".pd-fact-label").input_value() != "color":
                    failures.append("chip add did not prefill label 'color'")
                last.locator(".pd-fact-val").fill("purple")
                page.click("#pd-body >> button:text-is('บันทึก')")
                page.wait_for_selector("#pd-facts-container", state="detached",
                                       timeout=10000)
                _shot(env, "pd_facts_saved")

                st, info = _api(server, "GET",
                                "/api/product_info/PD%20Detail%20Widget",
                                session=ua["token"], brand=a1)
                if not (st == 200
                        and info["manual_facts"].get("color") == "purple"):
                    failures.append(f"manual facts not persisted: {info}")
                else:
                    if info["effective_facts"].get(
                            "color", {}).get("source") != "manual":
                        failures.append("manual source label missing")

                # --- reopen after refresh → persists -------------------------
                page.reload()
                page.wait_for_load_state("networkidle")
                _wait_workspace(page)
                page.click(".folder-item[data-folder='PD Detail Widget'] "
                           ".folder-info")
                page.wait_for_selector(
                    "#product-detail-view >> visible=true", timeout=10000)
                # _pdLoad() renders async — wait for the facts section.
                page.wait_for_selector("#pd-body .pd-section",
                                       timeout=10000)
                if "purple" not in page.inner_text("#pd-body"):
                    failures.append("manual fact lost after refresh")

                # --- marketing: set, save, verify --------------------------
                page.click("div[onclick=\"_pdToggleSection('marketing')\"]")
                page.wait_for_selector(
                    ".pd-section:has-text('การตลาด') >> text=แก้ไข",
                    timeout=10000)
                page.locator(".pd-section",
                             has_text="การตลาด").locator(
                    "text=แก้ไข").click()
                page.wait_for_selector("#pd-mkt-age", timeout=10000)
                page.fill("#pd-mkt-age", "30-40")
                page.fill("#pd-mkt-visual-tone", "neon cyber")
                page.click("#pd-body >> button:text-is('บันทึก')")
                page.wait_for_selector("#pd-mkt-age", state="detached",
                                       timeout=10000)
                prof = server.product_profile(uid, a1, "PD Detail Widget")
                if prof.get("audience", {}).get("primary", {}).get(
                        "age") != "30-40":
                    failures.append(f"mkt audience not saved: {prof}")
                if prof.get("visual_override", {}).get(
                        "image_style", {}).get("tone") != "neon cyber":
                    failures.append(f"mkt visual not saved: {prof}")
                _shot(env, "pd_mkt_saved")

                # --- clear audience + visual → persist? (suspect defect) ---
                page.locator(".pd-section",
                             has_text="การตลาด").locator(
                    "text=แก้ไข").click()
                page.wait_for_selector("#pd-mkt-age", timeout=10000)
                page.fill("#pd-mkt-age", "")
                page.fill("#pd-mkt-visual-tone", "")
                page.click("#pd-body >> button:text-is('บันทึก')")
                page.wait_for_selector("#pd-mkt-age", state="detached",
                                       timeout=10000)
                prof2 = server.product_profile(uid, a1, "PD Detail Widget")
                cleared = {
                    "audience_after_clear": prof2.get("audience"),
                    "visual_after_clear": prof2.get("visual_override"),
                    "audience_cleared": not (prof2.get("audience") or {}),
                    "visual_cleared": not (
                        prof2.get("visual_override") or {}),
                }
                (ARTIFACTS / "journeyD_clear_result.json").write_text(
                    json.dumps(cleared, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                if not cleared["audience_cleared"]:
                    failures.append(
                        "audience clear not persisted "
                        f"(leftover: {cleared['audience_after_clear']})")
                if not cleared["visual_cleared"]:
                    failures.append(
                        "visual_override clear not persisted "
                        f"(leftover: {cleared['visual_after_clear']})")

                # --- dirty-change warning: dismiss stays, accept leaves ----
                page.locator(".pd-section",
                             has_text="การตลาด").locator(
                    "text=แก้ไข").click()
                page.wait_for_selector("#pd-mkt-age", timeout=10000)
                page.fill("#pd-mkt-age", "99")
                env["dialogs"].clear()
                env["dialog_dismiss"] = True          # cancel → stay editing
                page.click("div[onclick=\"_pdToggleSection('sources')\"]")
                time.sleep(0.4)
                stayed = page.evaluate(
                    "document.getElementById('pd-mkt-age') !== null")
                env["dialog_dismiss"] = False         # accept → discard+leave
                page.click("div[onclick=\"_pdToggleSection('sources')\"]")
                time.sleep(0.4)
                left = page.evaluate(
                    "document.getElementById('pd-mkt-age') === null")
                dirty = {"dialogs": env["dialogs"], "stay_worked": stayed,
                         "leave_worked": left}
                (ARTIFACTS / "journeyD_dirty_dialog.json").write_text(
                    json.dumps(dirty, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                if not any("ทิ้ง" in m for m in env["dialogs"]):
                    failures.append("dirty-change warning never shown")
                if not stayed:
                    failures.append("dismiss did not stay on section")
                if not left:
                    failures.append("accept did not leave section")

                # --- add file via PD → ingestion run count ------------------
                # Existing-product source upload is free: exactly one
                # deterministic ingest run, zero model calls.
                server.ingest_calls.clear()
                server.ingest_delay = 0.6   # expose upload→ingest race
                lb_add = len(server.llm_ledger)
                extra = server.tmp / "_fixtures" / "pd_extra.txt"
                extra.write_text(
                    "Extra PD-UNIQ accessory info: includes strap\n",
                    encoding="utf-8")
                page.set_input_files("#pd-file-input", str(extra))
                deadline = time.time() + 40
                while time.time() < deadline:
                    rec = server.product_record(uid, a1, "PD Detail Widget")
                    names = {f.get("name") for f in rec.get("files", [])}
                    if rec.get("status") == "ready" \
                            and "pd_extra.txt" in names:
                        break
                    time.sleep(0.5)
                server.ingest_delay = 0.0
                n_ingest = len(server.ingest_calls)
                llm_add = [c for c in server.llm_ledger[lb_add:]
                           if c.get("source") != "__url_fetch__"]
                if llm_add:
                    failures.append(
                        f"existing-product file add invoked the model: "
                        f"{llm_add}")
                (ARTIFACTS / "journeyD_ingest_count.json").write_text(
                    json.dumps({"ingest_calls": server.ingest_calls,
                                "count": n_ingest}, indent=2),
                    encoding="utf-8")
                if n_ingest != 1:
                    failures.append(
                        f"file add triggered {n_ingest} ingest runs "
                        "(expected exactly 1)")

                # --- fast-fake case: same guarantee with instant ingest ----
                # If the JS still issued a second /api/ingest, a completed
                # (non-processing) first ingest would let it launch run #2.
                server.ingest_calls.clear()
                extra2 = server.tmp / "_fixtures" / "pd_extra2.txt"
                extra2.write_text("Second accessory: PD-UNIQ case\n",
                                  encoding="utf-8")
                page.set_input_files("#pd-file-input", str(extra2))
                deadline = time.time() + 40
                while time.time() < deadline:
                    rec = server.product_record(uid, a1, "PD Detail Widget")
                    names = {f.get("name") for f in rec.get("files", [])}
                    if rec.get("status") == "ready" \
                            and "pd_extra2.txt" in names:
                        break
                    time.sleep(0.3)
                n_ingest_fast = len(server.ingest_calls)
                (ARTIFACTS / "journeyD_ingest_count_fast.json").write_text(
                    json.dumps({"count": n_ingest_fast}, indent=2),
                    encoding="utf-8")
                if n_ingest_fast != 1:
                    failures.append(
                        f"fast-fake file add triggered {n_ingest_fast} "
                        "ingest runs (expected exactly 1)")

                # --- source file view through PD (popup window) -------------
                src_header = page.locator(
                    "div[onclick=\"_pdToggleSection('sources')\"]")
                if src_header.count() and not page.query_selector(
                        "button[onclick*='_pdToggleFileMenu']"):
                    src_header.click()
                    time.sleep(0.4)
                # locator (not ElementHandle) — survives _pdRefresh re-render
                menu_btn = page.locator(
                    "button[onclick*='_pdToggleFileMenu']").first
                if menu_btn.count():
                    menu_btn.click()
                    time.sleep(0.2)
                    with page.expect_popup(timeout=8000) as pop:
                        page.locator(".pd-file-menu:visible") \
                            .locator("text=ดูต้นฉบับ").click()
                    pw = pop.value
                    pw.wait_for_load_state()
                    content = pw.content()
                    if "PD-UNIQ" not in content:
                        failures.append("source popup missing content")
                    _shot(env, "pd_source_popup")
                else:
                    failures.append("no source file ⋯ menu in PD")

                # --- delete product through PD ⋯ menu ------------------------
                # Sentinel sibling proves delete removes only the chosen
                # product — seeded here so D stands alone (QualWatch Alpha
                # is created by Journey B and is absent in subset runs).
                sentinel = server.brand_data(uid, a1) / "D Sibling Sentinel"
                sentinel.mkdir(parents=True, exist_ok=True)
                page.click("button[onclick='_pdToggleMenu()']")
                page.click("#pd-menu >> text=ลบสินค้า")
                page.wait_for_selector("#product-detail-view",
                                       state="hidden", timeout=10000)
                prods = server.data_products(uid, a1)
                if "PD Detail Widget" in prods:
                    failures.append("deleted product still on disk")
                if "D Sibling Sentinel" not in prods:
                    failures.append("delete removed wrong product")
                _shot(env, "pd_deleted")
            finally:
                server.ingest_delay = 0.0
                _close(env)

        assert not env["blocked"]
        assert not failures, f"Journey D defects: {failures}"


# ---------------------------------------------------------------------------
# Journey E — product source viewing security matrix (API-adversarial).
# ---------------------------------------------------------------------------

class TestJourneyE:

    def test_e_source_security_matrix(self, qual):
        server = qual
        ua = server.users["A"]
        ub = server.users["B"]
        a1 = ua["brand_ids"]["A1"]
        a2 = ua["brand_ids"]["A2"]
        b1 = ub["brand_ids"]["B1"]
        product = "QualWatch Alpha"

        results = {}

        def check(name, cond, detail=""):
            results[name] = {"pass": bool(cond),
                             "detail": str(detail)[:300]}

        qprod = _q(product)

        # --- owner + owning brand → list + read ------------------------------
        st, files = _api(server, "GET", f"/api/folder_files/{qprod}",
                         session=ua["token"], brand=a1)
        check("owner_list",
              st == 200 and isinstance(files, list) and files,
              (st, files))
        st, body = _api(server, "GET",
                        f"/api/product_source_file/{qprod}/"
                        "qualco_catalog.txt",
                        session=ua["token"], brand=a1)
        check("owner_read",
              st == 200 and "ALPHA-UNIQ" in str(body), (st, str(body)[:120]))

        # --- no auth → 401 ----------------------------------------------------
        st, body = _api(server, "GET",
                        f"/api/product_source_file/{qprod}/"
                        "qualco_catalog.txt")
        check("no_auth_denied", st == 401, (st, body))
        st, body = _api(server, "GET", f"/api/folder_files/{qprod}")
        check("no_auth_list_denied", st == 401, (st, body))

        # --- no active brand → fail closed ------------------------------------
        st, body = _api(server, "GET", f"/api/folder_files/{qprod}",
                        session=ua["token"])
        check("no_brand_denied", st == 403, (st, body))
        st, body = _api(server, "GET",
                        f"/api/product_source_file/{qprod}/"
                        "qualco_catalog.txt",
                        session=ua["token"])
        check("no_brand_read_denied", st == 403, (st, body))

        # --- other user → no list, no read ------------------------------------
        st, body = _api(server, "GET", f"/api/folder_files/{qprod}",
                        session=ub["token"], brand=b1)
        check("other_user_list",
              st in (200, 404) and
              (not isinstance(body, list) or len(body) == 0),
              (st, body))
        st, body = _api(server, "GET",
                        f"/api/product_source_file/{qprod}/"
                        "qualco_catalog.txt",
                        session=ub["token"], brand=b1)
        check("other_user_read_denied",
              "ALPHA-UNIQ" not in str(body), (st, str(body)[:200]))

        # --- other brand of same user → no read -------------------------------
        st, body = _api(server, "GET",
                        f"/api/product_source_file/{qprod}/"
                        "qualco_catalog.txt",
                        session=ua["token"], brand=a2)
        check("other_brand_read_denied",
              "ALPHA-UNIQ" not in str(body), (st, str(body)[:200]))
        st, body = _api(server, "GET", f"/api/folder_files/{qprod}",
                        session=ua["token"], brand=a2)
        check("other_brand_list",
              st in (200, 404) and
              (not isinstance(body, list) or len(body) == 0),
              (st, body))

        # --- traversal variants on the file param -----------------------------
        escapes = [
            "..%2F..%2F..%2F..%2Fbatch.json",
            "..%2Fqualco_catalog.txt",
            "%2e%2e%2f%2e%2e%2fbatch.json",
            "%252e%252e%252fqualco_catalog.txt",
            "..%5C..%5Cqualco_catalog.txt",
            "%2Fetc%2Fpasswd",
            "..%2F..%2F..%2Fusers",
            "sub%2F..%2F..%2Fqualco_catalog.txt",
            ".%2E%2F.%2E%2Fbatch.json",
        ]
        for esc in escapes:
            st, body = _api(server, "GET",
                            f"/api/product_source_file/{qprod}/{esc}",
                            session=ua["token"], brand=a1)
            content = str(body)
            check(f"traversal_{esc[:26]}",
                  "ALPHA-UNIQ" not in content
                  and "root:" not in content
                  and "batch_id" not in content
                  and str(server.tmp) not in content,
                  (st, content[:150]))

        # --- sibling-product escape -------------------------------------------
        st, body = _api(server, "GET",
                        f"/api/product_source_file/{qprod}/"
                        "..%2FQualWatch%20Charlie%2Fqualco_catalog.txt",
                        session=ua["token"], brand=a1)
        check("sibling_product_escape",
              "CHARLIE-UNIQ" not in str(body), (st, str(body)[:150]))

        # --- folder param traversal --------------------------------------------
        st, body = _api(server, "GET", "/api/folder_files/..%2F..%2F",
                        session=ua["token"], brand=a1)
        check("folder_param_traversal",
              not (isinstance(body, list) and body)
              and "ALPHA" not in str(body), (st, str(body)[:150]))

        (ARTIFACTS / "journeyE_matrix.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8")
        failed = [k for k, v in results.items() if not v["pass"]]
        assert not failed, f"source matrix failures: {failed}"


# ---------------------------------------------------------------------------
# Journey F — persistence, authority, failure truthfulness (server restart).
# ---------------------------------------------------------------------------

class TestJourneyF:

    def test_f_persistence_and_failure(self, qual):
        server = qual
        ua = server.users["A"]
        a1 = ua["brand_ids"]["A1"]
        uid = ua["user_id"]
        product = "QualWatch Alpha"
        findings: dict = {}

        rec0 = server.product_record(uid, a1, product)
        assert rec0.get("status") == "ready"
        # FREE-IMPORT: deterministic import produced NO AI-derived facts —
        # the product is fully usable without ever calling a model.
        assert not (rec0.get("derived_facts") or {})

        qprod = _q(product)

        # --- manual correction is the highest authority ----------------------
        st, _ = _api(server, "POST",
                     f"/api/product_profile_save/{qprod}",
                     session=ua["token"], brand=a1,
                     json_body={"facts": {"qual_marker": "MANUAL-OVERRIDE"}})
        assert st == 200
        st, info = _api(server, "GET", f"/api/product_info/{qprod}",
                        session=ua["token"], brand=a1)
        assert info["effective_facts"]["qual_marker"]["value"] == \
            "MANUAL-OVERRIDE"
        assert info["effective_facts"]["qual_marker"]["source"] == "manual"
        findings["manual_facts_canonical"] = True

        # --- no AI-derived facts exist to mislabel ---------------------------
        findings["derived_facts_present"] = bool(info.get("derived_facts"))

        # --- restart server → login → reselect → state persists --------------
        server.restart()

        with sync_playwright() as p:
            env = _launch(p, server, None, name="F_restart")
            page = env["page"]
            try:
                resp = env["context"].request.post(
                    f"{server.url}/api/auth/login",
                    data={"username": ua["username"],
                          "password": ua["password"]})
                assert resp.status == 200
                page.goto(f"{server.url}/", wait_until="networkidle",
                          timeout=15000)
                page.wait_for_selector("#brand-gate-overlay.visible",
                                       timeout=10000)
                _select_brand_ui(page, a1)
                _wait_workspace(page)
                _wait_folder(page, product)
                page.click(f".folder-item[data-folder='{product}'] "
                           ".folder-info")
                page.wait_for_selector(
                    "#product-detail-view >> visible=true", timeout=10000)
                page.wait_for_selector("#pd-body .pd-section",
                                       timeout=10000)
                body_text = page.inner_text("#pd-body")
                findings["manual_persist_after_restart"] = \
                    "MANUAL-OVERRIDE" in body_text
                _shot(env, "post_restart_pd")
            finally:
                _close(env)

        # --- manual re-ingest is FREE: deterministic re-parse, zero calls ----
        lb = len(server.llm_ledger)
        prev = server.product_record(uid, a1, product).get("updated_at")
        st, _ = _api(server, "POST", f"/api/ingest/{qprod}",
                     session=ua["token"], brand=a1,
                     json_body={"force": True})
        assert st == 200
        rec1 = _wait_ready(server, uid, a1, product, prev_updated=prev)
        llm_reingest = [c for c in server.llm_ledger[lb:]
                        if c.get("source") != "__url_fetch__"]
        findings["reingest_model_calls"] = llm_reingest
        findings["status_after_reingest"] = rec1.get("status")
        findings["reingest_ingest_error"] = rec1.get("ingest_error")

        # --- AI failure → product stays usable, canonical untouched ---------
        # summary_responder raising = deterministic provider failure on the
        # facts scope of the EXPLICIT enrich boundary.
        def failing_summary(messages, entry):
            raise RuntimeError("deterministic qual failure")

        server.summary_responder = failing_summary
        st, body = _api(server, "POST",
                        f"/api/product_ai/{qprod}/enrich",
                        session=ua["token"], brand=a1,
                        json_body={"scopes": ["facts"]})
        server.summary_responder = None
        findings["enrich_failure_status"] = st
        findings["enrich_failure_body"] = str(body)[:200]
        rec2 = server.product_record(uid, a1, product)
        findings["status_after_ai_failure"] = rec2.get("status")
        findings["ai_enrichment_after_failure"] = \
            rec2.get("ai_enrichment")
        findings["ai_proposal_after_failure"] = rec2.get("ai_proposal")
        # canonical facts still exactly what the user set
        st, info2 = _api(server, "GET", f"/api/product_info/{qprod}",
                         session=ua["token"], brand=a1)
        findings["facts_after_ai_failure"] = (
            (info2.get("effective_facts") or {})
            .get("qual_marker", {}).get("value"))

        (ARTIFACTS / "journeyF_findings.json").write_text(
            json.dumps(findings, ensure_ascii=False, indent=2),
            encoding="utf-8")

        # FREE-IMPORT gates
        assert findings["reingest_model_calls"] == [], \
            f"manual re-ingest invoked the model: {findings['reingest_model_calls']}"
        assert findings["status_after_reingest"] == "ready", \
            f"free re-ingest broke readiness: {findings['status_after_reingest']}"
        assert not findings["reingest_ingest_error"], \
            f"free re-ingest recorded ingest_error: {findings['reingest_ingest_error']}"
        assert findings["enrich_failure_status"] == 500, \
            f"AI failure must surface as endpoint error: {findings['enrich_failure_status']}"
        assert findings["status_after_ai_failure"] == "ready", \
            ("AI failure must NEVER degrade a usable product — got "
             f"{findings['status_after_ai_failure']!r}")
        assert (findings["ai_enrichment_after_failure"] or {}).get(
            "status") == "failed", \
            f"AI failure must record failed state: {findings['ai_enrichment_after_failure']}"
        assert not findings["ai_proposal_after_failure"], \
            "failed generation must not leave a pending proposal"
        assert findings["facts_after_ai_failure"] == "MANUAL-OVERRIDE", \
            "AI failure must not touch canonical facts"


# ---------------------------------------------------------------------------
# Journey G — staging commit-boundary security checks (adversarial API).
# ---------------------------------------------------------------------------

class TestJourneyG:

    def _stage_one(self, server, session, brand,
                   fname="g_seed.txt") -> str:
        seed = "Product: G-SEED batch item\nPrice: 1 THB\nSpec: g\n"
        st, body = _upload_multipart(server, "/api/upload_stage",
                                     session, brand,
                                     [(fname, seed.encode())])
        assert st == 200, body
        return body["batch_id"]

    def test_g_commit_boundary(self, qual):
        server = qual
        ua = server.users["A"]
        a1 = ua["brand_ids"]["A1"]
        uid = ua["user_id"]
        results = {}

        def commit(choices, fname="g_seed.txt"):
            bid = self._stage_one(server, ua["token"], a1, fname)
            st, body = _api(server, "POST", f"/api/stage/{bid}/commit",
                            session=ua["token"], brand=a1,
                            json_body={"choices": choices})
            return st, body

        # 1. absolute-path product name
        st, body = commit([{"segment_index": 0, "action": "create",
                            "name": "/tmp/qual_escape"}])
        escaped = Path("/tmp/qual_escape").exists()
        results["absolute_name"] = {
            "status": st, "escaped_fs": escaped, "body": str(body)[:200]}
        if escaped:
            shutil.rmtree("/tmp/qual_escape", ignore_errors=True)

        # 2. ../ traversal name
        st, body = commit([{"segment_index": 0, "action": "create",
                            "name": "../escape_dir"}])
        trav = (server.brand_data(uid, a1).parent / "escape_dir").exists()
        results["traversal_name"] = {
            "status": st, "escaped_fs": trav, "body": str(body)[:200]}

        # 3. negative segment index — Python negative indexing resolves it
        st, body = commit([{"segment_index": -1, "action": "create",
                            "name": "NegIndex"}])
        results["negative_index"] = {
            "status": st,
            "created": "NegIndex" in server.data_products(uid, a1),
            "body": str(body)[:200]}

        # 4. out-of-range index → IndexError (uncaught → 500)
        st, body = commit([{"segment_index": 99, "action": "create",
                            "name": "OOB"}])
        results["oob_index"] = {"status": st, "body": str(body)[:200]}

        # 5. duplicate selection of the same segment
        st, body = commit([
            {"segment_index": 0, "action": "create", "name": "Dup"},
            {"segment_index": 0, "action": "create", "name": "Dup"}])
        dups = [d for d in server.data_products(uid, a1)
                if d.startswith("Dup")]
        results["duplicate_segment"] = {"status": st,
                                        "created_dirs": dups,
                                        "body": str(body)[:200]}

        # 6. unknown action silently ignored?
        st, body = commit([{"segment_index": 0, "action": "bogus",
                            "name": "Bogus"}])
        results["unknown_action"] = {
            "status": st, "body": str(body)[:200],
            "created": "Bogus" in server.data_products(uid, a1)}

        # 7. update action with traversal target — ".." resolves to the
        #    brand root itself, so copied files land outside data/.
        st, body = commit([{"segment_index": 0, "action": "update",
                            "target": ".."}])
        escaped_file = server.brand_root(uid, a1) / "g_seed.txt"
        results["update_traversal_target"] = {
            "status": st,
            "escaped_fs": escaped_file.exists(),
            "body": str(body)[:200]}
        if escaped_file.exists():
            escaped_file.unlink()

        # 8. partial commit: valid first, then out-of-range → non-atomic?
        before = set(server.data_products(uid, a1))
        st, body = commit([
            {"segment_index": 0, "action": "create", "name": "PartialOK"},
            {"segment_index": 99, "action": "create", "name": "Boom"}])
        results["partial_commit"] = {
            "status": st,
            "partial_created": sorted(
                set(server.data_products(uid, a1)) - before),
            "body": str(body)[:200]}

        # 9. /api/upload bypasses staging for a NEW folder — direct API only
        before = set(server.data_products(uid, a1))
        lb_g = len(server.llm_ledger)
        st, body = _upload_multipart(
            server, "/api/upload", ua["token"], a1,
            [("direct.txt", _catalog_text().encode())],
            extra_fields={"product_name": "DirectUpload"})
        results["direct_upload_new"] = {"status": st,
                                        "body": str(body)[:200]}
        deadline = time.time() + 25
        while time.time() < deadline:
            new = set(server.data_products(uid, a1)) - before
            if new:
                time.sleep(1.5)   # let background ingest settle
                break
            time.sleep(0.5)
        results["direct_upload_new"]["auto_created"] = sorted(
            set(server.data_products(uid, a1)) - before)
        results["direct_upload_new"]["model_calls"] = [
            c for c in server.llm_ledger[lb_g:]
            if c.get("source") != "__url_fetch__"]

        (ARTIFACTS / "journeyG_boundary.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8")

        # E2E-02: every malformed payload must be rejected BEFORE any side
        # effect — 400 + zero filesystem/product/enrichment effects.
        failures = []
        for key in ("absolute_name", "traversal_name", "negative_index",
                    "oob_index", "duplicate_segment", "unknown_action",
                    "update_traversal_target", "partial_commit"):
            if results[key]["status"] != 400:
                failures.append(
                    f"{key}: expected 400 rejection, got "
                    f"{results[key]['status']} ({results[key].get('body')})")
        if results["absolute_name"]["escaped_fs"]:
            failures.append("absolute name still wrote outside brand root")
        if results["traversal_name"]["escaped_fs"]:
            failures.append("traversal name still wrote outside data/")
        if results["negative_index"]["created"]:
            failures.append("negative index still materialized a product")
        if len(results["duplicate_segment"]["created_dirs"]) > 1:
            failures.append("duplicate selection still materialized twice")
        if results["update_traversal_target"]["escaped_fs"]:
            failures.append("update traversal still wrote outside data/")
        if results["partial_commit"]["partial_created"]:
            failures.append(
                "non-atomic commit: "
                f"{results['partial_commit']['partial_created']}")
        # /api/upload must refuse to create a new product entirely
        if results["direct_upload_new"]["status"] == 200:
            failures.append("/api/upload still accepts new-product creation")
        if results["direct_upload_new"]["auto_created"]:
            failures.append(
                "/api/upload materialized product(s) without review "
                f"{results['direct_upload_new']['auto_created']}")
        if results["direct_upload_new"]["model_calls"]:
            failures.append(
                "/api/upload invoked the model "
                f"{results['direct_upload_new']['model_calls']}")
        assert not failures, f"Journey G boundary defects: {failures}"


# ---------------------------------------------------------------------------
# Journey H — explicit AI proposal lifecycle (the ONLY paid boundary).
#
# Contract 17 authority matrix, all through real Chromium + the FakeLLM
# ledger:  open dialog = 0 calls; เริ่มใช้ AI = first allowed calls;
# generation writes ONLY ai_proposal; keep/accept/edit per field; bulk
# accept = empty fields only; stale proposal can never silently overwrite.
# ---------------------------------------------------------------------------

class TestJourneyH:

    def _seed(self, server, session, brand, name="AI Widget") -> None:
        seed = ("Product: AI-UNIQ Authority Widget\n"
                "Price: 777 THB\n"
                "Spec: AI-UNIQ-SPEC ceramic body\n").encode()
        st, body = _upload_multipart(server, "/api/upload_stage",
                                     session, brand,
                                     [("ai_widget.txt", seed)])
        assert st == 200, body
        st, body = _api(server, "POST",
                        f"/api/stage/{body['batch_id']}/commit",
                        session=session, brand=brand,
                        json_body={"choices": [{
                            "segment_index": 0, "action": "create",
                            "name": name}]})
        assert st == 200, body

    def _facts(self, server, session, brand, qprod) -> dict:
        st, info = _api(server, "GET", f"/api/product_info/{qprod}",
                        session=session, brand=brand)
        assert st == 200, info
        return info.get("effective_facts") or {}

    def test_h_ai_proposal_authority(self, qual):
        server = qual
        ua = server.users["A"]
        a1 = ua["brand_ids"]["A1"]
        uid = ua["user_id"]
        product = "AI Widget"
        qprod = _q(product)
        failures = []
        evidence = {"cost_boundary": {}, "authority": {}}

        lb_seed = len(server.llm_ledger)
        self._seed(server, ua["token"], a1, product)
        seed_calls = _llm_calls(server, lb_seed)
        assert seed_calls == [], \
            f"seed import invoked the model: {seed_calls}"

        # (B) manual canonical fact — the user authority AI must respect
        st, _ = _api(server, "POST", f"/api/product_profile_save/{qprod}",
                     session=ua["token"], brand=a1,
                     json_body={"facts": {"Battery": "770mAh"}})
        assert st == 200

        # FakeLLM proposal content — deterministic, includes a conflict
        # field (Battery 750 vs canonical 770) and two new empty fields.
        def rich_summary(messages, entry):
            return json.dumps({
                "summary": "AI-suggested summary",
                "category": "AI Category",
                "derived_facts": {
                    "battery": {"label": "Battery", "value": "750mAh"},
                    "chip": {"label": "Chip", "value": "Q9"},
                    "warranty": {"label": "Warranty", "value": "2y"},
                }})
        server.summary_responder = rich_summary

        def rich_profile(messages, entry):
            return json.dumps({
                "price_tier": "pro",
                "differentiators": ["ai-diff"],
                "use_cases": ["ai-use"],
                "tone_adjustment": "ai-tone",
            })
        server.profile_responder = rich_profile

        with sync_playwright() as p:
            env = _launch(p, server, ua["token"], name="H_ai")
            page = env["page"]
            try:
                page.goto(f"{server.url}/", wait_until="networkidle",
                          timeout=15000)
                if page.query_selector("#brand-gate-overlay.visible"):
                    _select_brand_ui(page, a1)
                _wait_workspace(page)
                _wait_folder(page, product)
                page.click(f".folder-item[data-folder='{product}'] "
                           ".folder-info")
                page.wait_for_selector(
                    "#product-detail-view >> visible=true", timeout=10000)
                page.wait_for_selector("#pd-body .pd-section",
                                       timeout=10000)

                # --- (C) open AI dialog — itself must be FREE -------------
                lb = len(server.llm_ledger)
                page.click("text=เติมข้อมูลด้วย AI")
                page.wait_for_selector("#pd-ai-overlay.visible",
                                       timeout=10000)
                page.wait_for_selector("#pd-ai-scope-facts", timeout=10000)
                assert page.locator("#pd-ai-scope-marketing").is_visible()
                assert "เครดิต" in page.inner_text("#pd-ai-body")
                _shot(env, "ai_setup_dialog")
                open_calls = _llm_calls(server, lb)
                evidence["cost_boundary"]["dialog_open_calls"] = len(
                    open_calls)
                assert open_calls == [], \
                    f"opening the AI dialog invoked the model: {open_calls}"

                # --- (C→D) เริ่มใช้ AI = first allowed model invocation ---
                page.click("text=เริ่มใช้ AI")
                page.wait_for_selector("#pd-ai-body .pd-ai-row",
                                       timeout=20000)
                gen_calls = _llm_calls(server, lb)
                sources = sorted(c.get("source") for c in gen_calls)
                evidence["cost_boundary"]["generate_calls"] = sources
                assert sources == [
                    "ingestion.metadata_summary",
                    "voice_learner.analyze_product_positioning"], \
                    f"unexpected model calls at paid boundary: {sources}"
                _shot(env, "ai_review_rows")

                # --- (D) canonical UNCHANGED before any resolution --------
                facts = self._facts(server, ua["token"], a1, qprod)
                assert facts.get("Battery", {}).get("value") == "770mAh", \
                    f"generation mutated canonical facts: {facts}"
                assert "Chip" not in facts
                rec = server.product_record(uid, a1, product)
                assert (rec.get("ai_proposal") or {}).get("proposal_id"), \
                    "no pending proposal stored"
                prop_id = rec["ai_proposal"]["proposal_id"]
                meta = rec.get("metadata") or {}
                assert meta.get("summary") != "AI-suggested summary", \
                    "generation wrote canonical metadata"

                # CURRENT-vs-SUGGESTION rendered
                batt = page.locator(
                    "#pd-ai-body div[data-kind='fact'][data-key='Battery']")
                assert batt.count() == 1, "Battery row missing"
                row_txt = page.locator(".pd-ai-row",
                                       has_text="Battery").first.inner_text()
                assert "770mAh" in row_txt and "750mAh" in row_txt, row_txt

                # --- (E) Keep current -----------------------------------
                lb2 = len(server.llm_ledger)
                batt.locator("button", has_text="เก็บค่าเดิม").click()
                page.wait_for_function(
                    "!document.querySelector("
                    "\"#pd-ai-body div[data-key='Battery']\")",
                    timeout=10000)
                facts = self._facts(server, ua["token"], a1, qprod)
                assert facts.get("Battery", {}).get("value") == "770mAh", \
                    f"keep wrote canonical: {facts}"
                assert _llm_calls(server, lb2) == [], \
                    "resolve invoked the model"

                # --- (F) Accept AI for an empty field only ---------------
                chip = page.locator(
                    "#pd-ai-body div[data-kind='fact'][data-key='Chip']")
                chip.locator("button", has_text="รับค่า").click()
                page.wait_for_function(
                    "!document.querySelector("
                    "\"#pd-ai-body div[data-key='Chip']\")", timeout=10000)
                facts = self._facts(server, ua["token"], a1, qprod)
                assert facts.get("Chip", {}).get("value") == "Q9", \
                    f"accepted fact not canonical: {facts}"
                assert facts["Chip"]["source"] == "manual", \
                    f"accepted AI fact must enter canonical layer: {facts['Chip']}"

                # --- (G) Edit/Merge — user value wins ---------------------
                war = page.locator(
                    "#pd-ai-body div[data-kind='fact'][data-key='Warranty']")
                war.locator("button", has_text="แก้ไขก่อนรับ").click()
                war_edit = page.locator(
                    "#pd-ai-body div[data-key='Warranty'] .pd-ai-edit")
                war_edit.fill("3y user-chosen")
                page.locator("#pd-ai-body div[data-key='Warranty'] "
                             "button", has_text="บันทึกค่าของฉัน").click()
                page.wait_for_function(
                    "!document.querySelector("
                    "\"#pd-ai-body div[data-key='Warranty']\")",
                    timeout=10000)
                facts = self._facts(server, ua["token"], a1, qprod)
                assert facts.get("Warranty", {}).get("value") == \
                    "3y user-chosen", f"edit value not canonical: {facts}"

                # --- bulk: accept empty fields only -----------------------
                # Deterministic summary is POPULATED (raw_text preview) —
                # bulk must leave that row behind, never overwrite it.
                lb3 = len(server.llm_ledger)
                page.click("text=รับเฉพาะข้อมูลใหม่ในช่องว่าง")
                page.wait_for_function(
                    "document.querySelectorAll("
                    "'#pd-ai-body .pd-ai-row').length <= 1",
                    timeout=10000)
                facts = self._facts(server, ua["token"], a1, qprod)
                assert facts.get("Battery", {}).get("value") == "770mAh", \
                    "bulk accept overwrote a populated field"
                prof = server.product_profile(uid, a1, product)
                assert prof.get("price_tier") == "pro", \
                    f"empty profile fields not bulk-accepted: {prof}"
                # the populated summary row survived bulk — keep current
                # (deterministic preview) rather than the AI suggestion.
                sum_row = page.locator(
                    "#pd-ai-body div[data-kind='summary']")
                assert sum_row.count() == 1, \
                    "populated summary row should remain after bulk"
                det_summary = (server.product_record(
                    uid, a1, product).get("metadata") or {}).get("summary")
                assert det_summary and det_summary != "AI-suggested summary"
                sum_row.locator(
                    "button", has_text="เก็บค่าเดิม").click()
                page.wait_for_function(
                    "document.querySelectorAll("
                    "'#pd-ai-body .pd-ai-row').length === 0",
                    timeout=10000)
                rec = server.product_record(uid, a1, product)
                assert (rec.get("metadata") or {}).get("summary") == \
                    det_summary, "keep overwrote deterministic summary"
                # empty canonical category WAS bulk-accepted
                assert (rec.get("metadata") or {}).get("category") == \
                    "AI Category"
                assert not rec.get("ai_proposal"), \
                    "fully-resolved proposal must clear"
                assert _llm_calls(server, lb3) == [], \
                    "bulk resolve invoked the model"
                _shot(env, "ai_resolved")

                # --- (H) stale proposal conflict ---------------------------
                # New generation (replaces the consumed one — setup view is
                # shown again since no proposal is pending).
                page.wait_for_selector("#pd-ai-scope-facts", timeout=10000)
                lb4 = len(server.llm_ledger)
                page.click("text=เริ่มใช้ AI")
                page.wait_for_selector("#pd-ai-body .pd-ai-row",
                                       timeout=20000)
                rec = server.product_record(uid, a1, product)
                assert (rec.get("ai_proposal") or {}).get(
                    "proposal_id") != prop_id, \
                    "rerun did not create a NEW proposal"
                # canonical still untouched by generation itself
                facts = self._facts(server, ua["token"], a1, qprod)
                assert facts.get("Battery", {}).get("value") == "770mAh"

                # user drifts canonical AFTER generation
                st, _ = _api(server, "POST",
                             f"/api/product_profile_save/{qprod}",
                             session=ua["token"], brand=a1,
                             json_body={"facts": {"Battery": "800mAh"}})
                assert st == 200
                # attempt to accept the stale AI value → blocked
                page.locator(
                    "#pd-ai-body div[data-kind='fact'][data-key='Battery'] "
                    "button", has_text="ใช้ค่าที่ AI แนะนำ").click()
                page.wait_for_function(
                    "document.querySelector('#pd-ai-status')"
                    ".textContent.includes('ค่าปัจจุบันเปลี่ยนไป')",
                    timeout=10000)
                _shot(env, "ai_stale_conflict")
                facts = self._facts(server, ua["token"], a1, qprod)
                assert facts.get("Battery", {}).get("value") == "800mAh", \
                    ("stale AI proposal overwrote a drifted canonical "
                     f"value: {facts.get('Battery')}")
                # row persists for re-decision against refreshed CURRENT
                assert page.locator(
                    "#pd-ai-body div[data-key='Battery']").count() == 1
                # resolve by keeping current → consumes the row
                page.locator(
                    "#pd-ai-body div[data-key='Battery'] button",
                    has_text="เก็บค่าเดิม").click()
                page.wait_for_function(
                    "!document.querySelector("
                    "\"#pd-ai-body div[data-key='Battery']\")",
                    timeout=10000)
                facts = self._facts(server, ua["token"], a1, qprod)
                assert facts.get("Battery", {}).get("value") == "800mAh"

                # --- (I) discard cleans pending state --------------------
                page.click("text=ทิ้งข้อเสนอทั้งหมด")
                page.wait_for_selector("#pd-ai-scope-facts", timeout=10000)
                rec = server.product_record(uid, a1, product)
                assert not rec.get("ai_proposal"), \
                    "discard left a pending proposal"
                assert rec.get("status") == "ready"
                page.click("#pd-ai-overlay button[title='ปิด']")

                evidence["cost_boundary"]["total_model_calls"] = len(
                    _llm_calls(server, lb))
                evidence["authority"]["final_facts"] = facts
                evidence["authority"]["status"] = rec.get("status")
            finally:
                server.summary_responder = None
                server.profile_responder = None
                _close(env)

        (ARTIFACTS / "journeyH_ai_proposal.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2,
                       default=str), encoding="utf-8")
        assert not env["blocked"]
        assert not failures, f"Journey H defects: {failures}"
