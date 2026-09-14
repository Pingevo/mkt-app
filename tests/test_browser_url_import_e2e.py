"""True browser E2E — Product URL Import through the real UI.

Real uvicorn + Chromium DOM actions only:

  U1 (session cookie) → create Brand A1 via the DOM
  → open the existing "+ เพิ่ม" upload modal → paste a product URL
  → click นำเข้าจากลิงก์ → product appears in the sidebar and reaches
    the normal ready/selection state
  → imported content lands in A1's product record (MARKER in raw_text)
  → create + switch to A2 via the DOM → imported product absent
  → switch back to A1 → product returns

Rules honored:
  - Session-cookie injection only for auth; brand create/select/switch
    ALWAYS go through real DOM controls.
  - The product is created ONLY through the UI (modal → endpoint →
    existing ingestion seam) — no fixture API shortcuts.
  - External page fetch is stubbed at src.url_import's internal seams
    (_make_client → httpx.MockTransport, socket.getaddrinfo wrapper).
    No internet, no LLM, no paid calls.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request

import httpx
import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright


PUBLIC_IP = "93.184.216.34"
MARKER = "TURBOBLEND-9000-BROWSER-E2E"
PRODUCT_URL = "https://shop.example.com/products/acme-blender"
DERIVED_NAME = "ACME Turbo Blender 9000"

PRODUCT_HTML = f"""<!DOCTYPE html>
<html><head>
<title>ACME Turbo Blender</title>
<meta property="og:title" content="{DERIVED_NAME}">
<meta property="og:description" content="Powerful 900W kitchen blender">
</head><body>
<h1>{DERIVED_NAME}</h1>
<p>Price: 2,590 THB</p>
<p>Flagship kitchen blender with a 900W copper motor, BPA-free 1.5L jug,
six stainless-steel blades, three speed settings, and dishwasher-safe
parts. Ships nationwide within 2-4 business days.</p>
<ul><li>900W motor</li><li>{MARKER}</li></ul>
</body></html>""".encode()


def _mock_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) == PRODUCT_URL:
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=PRODUCT_HTML,
        )
    return httpx.Response(404, content=b"not found")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def _url_server(tmp_path_factory):
    import web_viewer
    import src.auth as auth_mod
    import src.openrouter_gateway as gateway
    import src.url_import as url_import
    from src.auth import UserStore, SessionManager

    tmp = tmp_path_factory.mktemp("url_import_e2e")

    _orig_wv = {"PROJECT_ROOT": web_viewer.PROJECT_ROOT}
    _orig_auth = {
        "_user_store": getattr(auth_mod, "_user_store", None),
        "_session_manager": getattr(auth_mod, "_session_manager", None),
    }
    _orig_key = gateway.get_api_key
    _orig_make_client = url_import._make_client
    _orig_browser_fetch = url_import._browser_fetch
    _orig_gai = socket.getaddrinfo

    users_path = tmp / "data" / "auth" / "users.json"
    sessions_path = tmp / "data" / "auth" / "sessions.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    sess = SessionManager(sessions_path)
    auth_mod._user_store = store
    auth_mod._session_manager = sess
    user = store.register("url_user", "url_pass")

    web_viewer.PROJECT_ROOT = tmp
    gateway.get_api_key = lambda: ""  # no paid calls (credits + ingest LLM)

    # Stub the external fetch at url_import's seams — in-process server picks
    # it up; DNS passes through for everything except the test host.
    url_import._make_client = lambda: httpx.Client(
        transport=httpx.MockTransport(_mock_handler)
    )
    # The e2e tests the UI→endpoint→ingest contract — the Chromium fallback
    # is covered in test_url_import_browser.py and must not launch here.
    url_import._browser_fetch = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("browser fallback must not launch in this e2e"))

    def fake_gai(host, port=0, *a, **kw):
        if host == "shop.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 443))]
        return _orig_gai(host, port, *a, **kw)

    socket.getaddrinfo = fake_gai

    port = _find_free_port()
    import uvicorn

    config = uvicorn.Config(web_viewer.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("Server did not start")

    token = sess.create_session(user.user_id)
    yield {
        "port": port, "url": f"http://127.0.0.1:{port}",
        "tmp": tmp, "session_token": token, "user_id": user.user_id,
    }

    server.should_exit = True
    thread.join(timeout=5)
    sched = getattr(web_viewer, "_scheduler_instance", None)
    if sched is not None:
        try:
            sched.stop()
        except Exception:
            pass
    web_viewer.PROJECT_ROOT = _orig_wv["PROJECT_ROOT"]
    auth_mod._user_store = _orig_auth["_user_store"]
    auth_mod._session_manager = _orig_auth["_session_manager"]
    gateway.get_api_key = _orig_key
    url_import._make_client = _orig_make_client
    url_import._browser_fetch = _orig_browser_fetch
    socket.getaddrinfo = _orig_gai


# ---------------------------------------------------------------------------
# Browser helpers (same contract as test_browser_multibrand_e2e)
# ---------------------------------------------------------------------------

def _launch(pw, server: dict) -> dict:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_cookies([{
        "name": "mktapp_session", "value": server["session_token"],
        "url": server["url"],
    }])
    page = context.new_page()
    console_errors: list = []
    page.on("console", lambda m: console_errors.append(m) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(e))
    return {"browser": browser, "context": context, "page": page,
            "console_errors": console_errors}


def _close(env: dict) -> None:
    try:
        env["context"].close()
    except Exception:
        pass
    try:
        env["browser"].close()
    except Exception:
        pass


def _create_brand_ui(page, name: str) -> None:
    page.fill("#brand-gate-name", name)
    with page.expect_navigation(wait_until="load", timeout=15000):
        page.click("#brand-gate-create")
    page.wait_for_load_state("networkidle")


def _select_brand_ui(page, brand_id: str) -> None:
    row = page.wait_for_selector(
        f".brand-gate-item[data-brand-id='{brand_id}']", timeout=10000
    )
    btn = row.query_selector(".brand-gate-select-btn")
    with page.expect_navigation(wait_until="load", timeout=15000):
        btn.click()
    page.wait_for_load_state("networkidle")


def _sidebar_folder_names(page) -> list[str]:
    return [
        el.get_attribute("data-folder")
        for el in page.query_selector_all("#sidebar-content .folder-item")
    ]


def _active_brand_id(env: dict) -> str:
    cookies = env["context"].cookies()
    bid = next((c["value"] for c in cookies if c["name"] == "mktapp_brand"), None)
    assert bid, "mktapp_brand cookie must be set by the real UI select"
    return bid


def _wait_ready_on_disk(server: dict, brand_id: str, folder: str, timeout=30.0) -> dict:
    """Poll the real product record until ingest reaches a terminal status."""
    rec = server["tmp"] / "users" / server["user_id"] / "brands" / brand_id \
        / "cache" / folder / "product.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if rec.exists():
            data = json.loads(rec.read_text(encoding="utf-8"))
            if data.get("status") in ("ready", "no_usable_data", "stale"):
                return data
        time.sleep(0.3)
    raise AssertionError(f"product record never reached terminal status: {rec}")


# ---------------------------------------------------------------------------
# The acceptance flow
# ---------------------------------------------------------------------------

def test_url_import_through_real_ui_with_brand_isolation(_url_server):
    server = _url_server
    with sync_playwright() as p:
        env = _launch(p, server)
        page = env["page"]
        try:
            page.goto(f"{server['url']}/", wait_until="networkidle", timeout=15000)

            # -- A1 via DOM --
            _create_brand_ui(page, "UrlBrandA1")
            page.wait_for_selector("#brand-switcher-btn", state="visible", timeout=15000)
            a1 = _active_brand_id(env)

            # -- open the existing upload modal, paste link, click import --
            page.click(".sidebar-add-btn")
            page.wait_for_selector("#upload-overlay.visible", timeout=10000)
            page.wait_for_selector("#url-import-block", state="visible", timeout=5000)
            page.fill("#import-url-input", PRODUCT_URL)
            page.click("#import-url-btn")

            # modal shows success then closes; product lands in the sidebar
            page.wait_for_selector(
                f".folder-item[data-folder='{DERIVED_NAME}']", timeout=15000
            )
            record = _wait_ready_on_disk(server, a1, DERIVED_NAME)
            assert record["status"] == "ready", record.get("ingest_error")
            assert MARKER in record["raw_text"]
            prov = record.get("source_import") or {}
            assert prov.get("original_url") == PRODUCT_URL

            # sidebar reflects the normal ready/selection state
            page.wait_for_selector(
                f".folder-item[data-folder='{DERIVED_NAME}'][data-status='ready']",
                timeout=15000,
            )
            # normal selection path — a ready folder-item is clickable (not disabled)
            item = page.query_selector(
                f".folder-item[data-folder='{DERIVED_NAME}']"
            )
            assert "folder-item-disabled" not in (item.get_attribute("class") or "")

            # -- create + switch to A2 via DOM → imported product absent --
            page.click("#brand-switcher-btn")
            page.wait_for_selector("#brand-gate-overlay.visible", timeout=10000)
            _create_brand_ui(page, "UrlBrandA2")
            page.wait_for_selector("#brand-switcher-btn", state="visible", timeout=15000)
            assert DERIVED_NAME not in _sidebar_folder_names(page)

            # -- switch back to A1 → product returns --
            page.click("#brand-switcher-btn")
            page.wait_for_selector("#brand-gate-overlay.visible", timeout=10000)
            _select_brand_ui(page, a1)
            page.wait_for_selector(
                f".folder-item[data-folder='{DERIVED_NAME}']", timeout=15000
            )
            assert DERIVED_NAME in _sidebar_folder_names(page)

            fatal = [str(e) for e in env["console_errors"]
                     if not any(s in str(e) for s in ("favicon", "404", "403", "500"))]
            assert not fatal, f"fatal console errors: {fatal}"
        finally:
            _close(env)
