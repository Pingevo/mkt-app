"""Browser E2E: Add Product → staging → commit → reload → restart → Agents.

REAL-WEB-PRODUCT-TO-AGENTS-E2E-01 regression: the existing browser fixtures
patch ``staging._project_root`` to the bare tmp root, which SPLITS the
workspace — staging writes land at ``tmp/data/`` while ``product_db`` writes
to ``tmp/users/<uid>/brands/<bid>/cache/``.  That harness artifact means the
suite can never observe a workspace-scope divergence between Add Product and
the Agents path — the class of bug behind the "agent cannot find the product"
report.

This suite deliberately does NOT patch ``staging._project_root`` —
``web_viewer.PROJECT_ROOT`` alone drives the whole brand-scoped chain via
``WorkspaceContext`` exactly as in production.  The LLM boundary is a
recording fake (the seam is product resolution + context delivery, not model
output).
"""
import json
import os
import random
import socket
import struct
import sys
import threading
import time
import urllib.request
import zlib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    pytestmark = pytest.mark.skip(reason="playwright not installed")

PRODUCT_NAME = "E2E Scoped Watch"


class _FakeLLM:
    """Recording boundary — logs which product facts/images reach the model."""
    last_truncated = False
    last_finish_reason = "stop"
    last_cost_usd = 0.0
    last_annotations_count = 0
    _m6_last_audit = {}
    _m6_last_raw = None

    def __init__(self):
        self.calls = []

    def _record(self, kind, messages, kw):
        n_img = 0
        blob = ""
        for m in (messages or []):
            c = m.get("content")
            if isinstance(c, list):
                n_img += sum(1 for x in c if isinstance(x, dict)
                             and x.get("type") == "image_url")
                blob += " ".join(x.get("text", "") for x in c
                                 if isinstance(x, dict))
            elif isinstance(c, str):
                blob += c
        self.calls.append({"kind": kind, "source": kw.get("source", ""),
                           "n_images": n_img,
                           "has_name": PRODUCT_NAME in blob,
                           "has_model": "E2E-SCOPED-7X" in blob})

    def _serve(self, source, messages):
        last = ""
        for m in reversed(messages or []):
            c = m.get("content")
            if isinstance(c, str):
                last = c
                break
            if isinstance(c, list):
                last = " ".join(x.get("text", "") for x in c
                                if isinstance(x, dict))
                break
        if "grounding" in source or "grounded" in last:
            return json.dumps({"grounded": True, "unsupported_claims": []})
        if "metadata_summary" in source:
            return json.dumps({"summary": "s", "category": "c",
                               "derived_facts": {}}, ensure_ascii=False)
        return "## spec\n- name ok\n- model ok\n- price ok\n" * 8

    def chat(self, messages, **kw):
        self._record("chat", messages, kw)
        text = self._serve(str(kw.get("source", "")), messages)
        if kw.get("return_annotations"):
            return text, []
        return text

    def chat_with_tools(self, messages, tools=None, **kw):
        self._record("tools", messages, kw)
        return self._serve(str(kw.get("source", "")), messages), []

    def close(self): pass
    def abort(self): pass


def _png_bytes(seed: int = 7, n: int = 120) -> bytes:
    rng = random.Random(seed)
    raw = b"".join(b"\x00" + bytes(rng.randrange(256) for _ in range(n * 3))
                   for _ in range(n))

    def chunk(tag, data):
        c = tag + data
        return (struct.pack(">I", len(data)) + c
                + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _make_pdf(path: Path) -> None:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    lines = [
        f"Product Name: {PRODUCT_NAME}",
        "Model: E2E-SCOPED-7X",
        "CPU: ScopedChip Q9",
        "Battery: 610mAh",
        "Display: 1.52 inch",
        "Price: 990 THB",
    ]
    y = 72
    for ln in lines:
        page.insert_text((72, y), ln, fontsize=12)
        y += 22
    page.insert_image(fitz.Rect(72, y + 10, 72 + 120, y + 130),
                      stream=_png_bytes())
    doc.save(str(path))
    doc.close()


@pytest.fixture(scope="module")
def _server(tmp_path_factory):
    """Real uvicorn server — real brand-scoped workspace resolution.

    Unlike other browser fixtures, ``staging._project_root`` is left alone:
    the workspace contextvar (built from ``web_viewer.PROJECT_ROOT`` by the
    auth middleware) resolves ``users/<uid>/brands/<bid>/`` exactly like
    production.  Config + asset_library still point at the tmp config copy.
    """
    import web_viewer
    from src import staging, ingestion, config_loader, asset_library, product_db
    from src.orchestrator import Orchestrator
    import yaml as _yaml

    tmp = tmp_path_factory.mktemp("e2e_scope")
    for d in ("data", "cache", "brand", "output"):
        (tmp / d).mkdir(exist_ok=True)
    config_dir = tmp / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False))
    _prod_agents_path = PROJECT_ROOT / "config" / "agents.yaml"
    with open(_prod_agents_path, encoding="utf-8") as f:
        _prod_agents = _yaml.safe_load(f)
    for section in ("defaults", "auto_mode", "product_spec", "competitor_analysis",
                    "campaign_strategy", "content_creator"):
        if section in _prod_agents and isinstance(_prod_agents[section], dict):
            _prod_agents[section]["model"] = "fake"
    (config_dir / "agents.yaml").write_text(
        _yaml.dump(_prod_agents, allow_unicode=True, sort_keys=False))
    (config_dir / "ingestion.yaml").write_text(
        "supported_formats:\n"
        "  text:    [.txt, .md, .pdf, .xlsx, .xls, .docx, .csv]\n"
        "  image:   [.jpg, .jpeg, .png, .webp]\n"
        "max_file_size_mb:\n  text: 50\n  image: 50\n  video: 500\n  audio: 50\n"
        "raw_text_length: 3000\nmodel: fake\n")
    (config_dir / "system.yaml").write_text("api_timeout_credits: 10\n")
    (config_dir / "media.yaml").write_text(
        "auto_generate_image: false\nauto_generate_video: false\n")
    (config_dir / "web_search.yaml").write_text("enabled: true\n")

    _orig = {
        "PROJECT_ROOT": web_viewer.PROJECT_ROOT,
        "make_client": Orchestrator.make_client,
        "ingestion_make_llm": ingestion._make_llm,
        "api_key": os.environ.get("OPENROUTER_API_KEY"),
        "current_llm": getattr(web_viewer, "_current_llm", {}),
        "session_ts": getattr(web_viewer, "_session_ts", {}),
        "cancel": getattr(web_viewer, "_cancel_requested", {}),
        "active_llms": getattr(web_viewer, "_active_llms", {}),
    }
    config_loader._project_root = lambda: tmp
    asset_library._project_root = lambda: tmp
    web_viewer.PROJECT_ROOT = tmp
    # NOTE: staging._project_root intentionally NOT patched — real chain.

    from src import brand_loader as _bl_mod
    _orig_resolve = _bl_mod._resolve_brand_dir

    def _patched_resolve(brand_dir):
        if brand_dir is None:
            return _orig_resolve(None)
        bd = Path(brand_dir)
        if not bd.is_absolute():
            bd = tmp / brand_dir
        if not bd.exists() or not bd.is_dir():
            return None
        return bd
    _bl_mod._resolve_brand_dir = _patched_resolve

    from src.auth import UserStore, SessionManager
    import src.auth as auth_mod
    (tmp / "data" / "auth").mkdir(parents=True, exist_ok=True)
    _store = UserStore(tmp / "data" / "auth" / "users.json")
    _sess = SessionManager(tmp / "data" / "auth" / "sessions.json")
    auth_mod._user_store = _store
    auth_mod._session_manager = _sess
    _user = _store.register("e2euser", "e2epass")
    _ws_root = tmp / "users" / _user.user_id
    for d in ("data", "cache", "brand", "output"):
        (_ws_root / d).mkdir(parents=True, exist_ok=True)

    fake_llm = _FakeLLM()
    ingestion._make_llm = lambda *a, **kw: fake_llm
    Orchestrator.make_client = staticmethod(lambda *a, **kw: fake_llm)
    web_viewer._current_llm = {}
    web_viewer._session_ts = {}
    web_viewer._cancel_requested = {}
    web_viewer._active_llms = {}
    os.environ["OPENROUTER_API_KEY"] = "dummy-key-for-testing"
    os.environ["MKTAPP_DEV_AUTH"] = "1"

    import uvicorn
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(web_viewer.app, host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("Server did not start")

    yield {
        "url": f"http://127.0.0.1:{port}",
        "tmp": tmp,
        "fake_llm": fake_llm,
        "user_id": _user.user_id,
        "server": server,
        "app": web_viewer.app,
    }

    server.should_exit = True
    thread.join(timeout=5)
    web_viewer.PROJECT_ROOT = _orig["PROJECT_ROOT"]
    Orchestrator.make_client = _orig["make_client"]
    ingestion._make_llm = _orig["ingestion_make_llm"]
    _bl_mod._resolve_brand_dir = _orig_resolve
    if _orig["api_key"] is not None:
        os.environ["OPENROUTER_API_KEY"] = _orig["api_key"]


@pytest.fixture(scope="module")
def _ctx(_server):
    """One browser context: dev-auth login + brand selected + PDF built."""
    url = _server["url"]
    tmp = _server["tmp"]
    pdf = tmp / "e2e_src.pdf"
    _make_pdf(pdf)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()
    page = ctx.new_page()
    console = []
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: console.append(f"PAGEERROR: {e}"))

    ctx.request.post(f"{url}/api/auth/register",
                     data={"username": "e2euser", "password": "e2epass"})
    r = ctx.request.post(f"{url}/api/auth/login",
                         data={"username": "e2euser", "password": "e2epass"})
    assert r.status == 200
    r = ctx.request.post(f"{url}/api/brands", data={"name": "E2E Brand"})
    bid = r.json()["brand_id"]
    assert ctx.request.post(f"{url}/api/brands/{bid}/select").status == 200

    yield {"page": page, "ctx": ctx, "url": url, "bid": bid,
           "pdf": pdf, "console": console,
           "server": _server}
    browser.close()
    pw.stop()


def test_ui_add_product_commits_brand_scoped_and_survives_restart(_ctx):
    """The real chain: UI upload → staging preview → เพิ่ม → commit →
    product lands under users/<uid>/brands/<bid>/ and stays `ready`
    after a page reload AND a server restart."""
    page, url, tmp = _ctx["page"], _ctx["url"], _ctx["server"]["tmp"]
    pdf = _ctx["pdf"]

    page.goto(url + "/")
    page.wait_for_selector(".sidebar-add-btn", timeout=15000)
    page.click(".sidebar-add-btn")
    page.wait_for_selector("#add-upload-btn", state="visible", timeout=8000)
    page.set_input_files("#upload-files-modal", str(pdf))
    page.wait_for_selector("#add-upload-btn", state="visible", timeout=5000)
    page.click("#add-upload-btn")

    page.wait_for_selector("#staging-preview-modal", state="visible",
                           timeout=60000)
    segs = page.eval_on_selector_all(
        "#staging-preview-modal input[data-seg-name]",
        "els => els.map(e => e.value)")
    assert segs, "staging preview produced no segments"
    page.fill("#staging-preview-modal input[data-seg-name='0']", PRODUCT_NAME)
    for t in page.query_selector_all("#staging-preview-modal .seg-toggle"):
        idx = t.get_attribute("data-seg-action")
        page.check(f"input[name='seg-act-{idx}'][value='create']")
    page.click("#staging-confirm-btn")
    page.wait_for_selector("#upload-modal-status.ok", timeout=15000)

    # Product committed under the BRAND scope — not the project root.
    bid = _ctx["bid"]
    brand_data = (tmp / "users" / _ctx["server"]["user_id"]
                  / "brands" / bid / "data" / PRODUCT_NAME)
    brand_cache = (tmp / "users" / _ctx["server"]["user_id"]
                   / "brands" / bid / "cache" / PRODUCT_NAME)
    assert brand_data.is_dir(), \
        f"source files must land under brand data/, got {list((tmp / 'data').glob('*'))}"
    assert (brand_cache / "product.json").exists()

    # Sidebar shows it ready (poll — ingestion may be async)
    deadline = time.time() + 60
    status = None
    while time.time() < deadline:
        items = page.eval_on_selector_all(
            ".folder-item",
            "els => els.map(e => ({f: e.dataset.folder, s: e.dataset.status}))")
        mine = [i for i in items if i["f"] == PRODUCT_NAME]
        if mine and mine[0]["s"] in ("ready", "stale"):
            status = mine[0]["s"]
            break
        page.wait_for_timeout(1500)
        page.reload()
        page.wait_for_selector(".sidebar-add-btn", timeout=15000)
    assert status in ("ready", "stale"), f"sidebar status: {status}"

    # Survives a real server restart — disk persistence, not process memory.
    srv = _ctx["server"]["server"]
    srv.should_exit = True
    time.sleep(1.5)
    import uvicorn
    srv2 = uvicorn.Server(uvicorn.Config(
        _ctx["server"]["app"], host="127.0.0.1",
        port=int(url.rsplit(":", 1)[1]), log_level="warning"))
    t = threading.Thread(target=srv2.run, daemon=True)
    t.start()
    for _ in range(40):
        try:
            urllib.request.urlopen(url + "/api/auth/me", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    page.goto(url + "/")
    page.wait_for_selector(
        f".folder-item[data-folder='{PRODUCT_NAME}']", timeout=15000)


def test_ui_agent_run_resolves_same_product_with_scoped_context(_ctx):
    """Wizard: product card selectable → product_spec run completes and the
    LLM boundary receives THIS product's facts + image — no 'product not
    found' at any seam."""
    page, url = _ctx["page"], _ctx["url"]
    fake = _ctx["server"]["fake_llm"]

    # Wizard flow: step1 select product card → step2 pick agent → through to run.
    if not page.query_selector("#flow-box-0"):
        page.click(".flow-box.add-new")
        page.wait_for_selector("#flow-box-0", timeout=8000)
    page.wait_for_selector(
        f"#flow-product-grid-0 .product-card[data-folder='{PRODUCT_NAME}']",
        timeout=15000)
    page.click(
        f"#flow-product-grid-0 .product-card[data-folder='{PRODUCT_NAME}']")
    page.click("#flow-next-0")
    page.wait_for_selector(
        "#flow-add-agent-chips-0 .add-agent-chip[data-agent-key='product_spec']",
        timeout=8000)
    page.click(
        "#flow-add-agent-chips-0 .add-agent-chip[data-agent-key='product_spec']")
    page.click("#flow-next-0")
    page.wait_for_timeout(500)
    page.click("#flow-next-0")   # → step 4 review
    page.wait_for_timeout(500)
    page.click("#flow-next-0")   # ✓ ยืนยัน → runSingleFlow → POST /api/run_flows

    deadline = time.time() + 120
    outcome = None
    while time.time() < deadline:
        cls = page.evaluate(
            "() => document.getElementById('flow-0-0')?.className || ''")
        if "done" in cls:
            outcome = "done"
            break
        if "error" in cls:
            badge = page.evaluate(
                "() => document.getElementById('flow-0-0-status')?.textContent || ''")
            pytest.fail(f"flow errored: {badge}")
        page.wait_for_timeout(1500)
    assert outcome == "done", "product_spec flow did not complete"

    # The model boundary saw THIS product's scoped facts + extracted image.
    gen = [c for c in fake.calls if c["source"].startswith("product_spec")]
    assert gen, "no product_spec LLM call recorded"
    assert any(c["has_name"] and c["has_model"] for c in gen), \
        "product scoped facts did not reach the model prompt"
    assert any(c["n_images"] > 0 for c in gen), \
        "product image did not reach the model"

    # Final grounding received the SAME product-scoped image set (visual
    # evidence contract) plus the product text evidence — nothing unrelated.
    ground = [c for c in fake.calls
              if c["source"].endswith("final_grounding_check")]
    assert ground, "no final_grounding_check call recorded"
    gen_imgs = max(c["n_images"] for c in gen)
    assert all(c["n_images"] == gen_imgs for c in ground), \
        "final grounding did not receive the same product image set"
    assert all(c["has_model"] for c in ground), \
        "product text evidence did not reach final grounding"


def test_other_brand_cannot_see_product(_ctx):
    """Isolation: a second brand in the same user workspace must not list
    the product — scoping is real, not a shared catalog."""
    ctx, url = _ctx["ctx"], _ctx["url"]
    r = ctx.request.post(f"{url}/api/brands", data={"name": "E2E Other"})
    other = r.json()["brand_id"]
    ctx.request.post(f"{url}/api/brands/{other}/select")
    page = _ctx["page"]
    page.goto(url + "/")
    page.wait_for_selector(".sidebar-add-btn", timeout=15000)
    page.wait_for_timeout(1200)
    folders = page.eval_on_selector_all(
        ".folder-item", "els => els.map(e => e.dataset.folder)")
    assert PRODUCT_NAME not in folders
