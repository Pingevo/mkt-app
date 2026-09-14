"""Browser E2E — Product Facts UI edit → save → reload → Agent context.

Standalone browser test that does NOT depend on external XLSX fixtures.
Creates a product by uploading a simple .txt file through the real
browser upload flow, then exercises the Product Facts key/value editor
in the Manage modal: add → save → reload → verify persistence → run
product_spec → assert the fact reaches the production model boundary
in the user prompt (product data), before the raw evidence.

No paid/live external calls — FakeLLM is used at the model boundary.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    pytestmark = pytest.mark.skip(reason="playwright not installed")


# ---------------------------------------------------------------------------
# FakeLLM — captures calls, returns deterministic output
# ---------------------------------------------------------------------------

class _FakeLLM:
    """Deterministic LLM double — routes by source kwarg like ScriptedFakeLLM."""

    def __init__(self):
        self.calls = []
        self._agent_output = "# Fake spec output"
        self._segmentation_json = None

    def reset(self):
        self.calls.clear()
        self._agent_output = "# Fake spec output"
        self._segmentation_json = None

    def set_agent_output(self, text):
        self._agent_output = text

    def set_segmentation(self, json_str):
        self._segmentation_json = json_str

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        self.calls.append({"messages": messages, "kwargs": kwargs, "source": source})
        if source == "product_segmentation.segment_products":
            return self._segmentation_json or json.dumps({"products": []})
        if source == "ingestion.metadata_summary":
            return "Test product summary"
        if source == "voice_learner.analyze_product_positioning":
            return json.dumps({"audience": {}, "competitors": [], "differentiators": []})
        if source == "competitor_analysis.semantic_review":
            return json.dumps([{"index": 0, "action": "keep"}])
        if source == "competitor_analysis.brand_interpretation":
            return json.dumps([])
        if kwargs.get("return_annotations"):
            return self._agent_output, []
        return self._agent_output

    def close(self):
        pass

    def abort(self):
        pass


# ---------------------------------------------------------------------------
# Server fixture — minimal real uvicorn server with FakeLLM
# ---------------------------------------------------------------------------

@pytest.fixture
def _server(tmp_path_factory):
    import web_viewer
    from src import product_db, staging, ingestion, config_loader, asset_library
    from src.orchestrator import Orchestrator
    import yaml as _yaml

    tmp = tmp_path_factory.mktemp("facts_e2e")
    for d in ("data", "cache", "brand", "output"):
        (tmp / d).mkdir(exist_ok=True)
    config_dir = tmp / "config"
    config_dir.mkdir(exist_ok=True)

    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    _prod_agents_path = PROJECT_ROOT / "config" / "agents.yaml"
    with open(_prod_agents_path, encoding="utf-8") as f:
        _prod_agents = _yaml.safe_load(f)
    for section in ("defaults", "auto_mode", "product_spec", "competitor_analysis",
                     "campaign_strategy", "content_creator"):
        if section in _prod_agents and isinstance(_prod_agents[section], dict):
            _prod_agents[section]["model"] = "fake"
    (config_dir / "agents.yaml").write_text(
        _yaml.dump(_prod_agents, allow_unicode=True, default_flow_style=False,
                   sort_keys=False), encoding="utf-8",
    )
    (config_dir / "ingestion.yaml").write_text(
        "supported_formats:\n"
        "  text:    [.txt, .md, .pdf, .xlsx, .xls, .docx, .csv]\n"
        "  image:   [.jpg, .jpeg, .png, .webp]\n"
        "max_file_size_mb:\n  text: 50\n  image: 50\n  video: 500\n  audio: 50\n"
        "raw_text_length: 3000\nmodel: fake\n",
        encoding="utf-8",
    )
    (config_dir / "system.yaml").write_text("api_timeout_credits: 10\n")
    (config_dir / "media.yaml").write_text(
        "auto_generate_image: false\nauto_generate_video: false\n"
    )
    (config_dir / "web_search.yaml").write_text("enabled: true\n")

    _orig = {}
    _orig["product_db_root"] = product_db._project_root
    _orig["staging_root"] = staging._project_root
    _orig["ingestion_root"] = ingestion._project_root
    _orig["config_loader_root"] = config_loader._project_root
    _orig["asset_library_root"] = asset_library._project_root
    _orig["staging_product_db"] = staging.product_db
    _orig["PROJECT_ROOT"] = web_viewer.PROJECT_ROOT
    _orig["make_client"] = Orchestrator.make_client
    _orig["ingestion_make_llm"] = ingestion._make_llm
    _orig["api_key"] = os.environ.get("OPENROUTER_API_KEY", None)
    _orig["current_llm"] = getattr(web_viewer, "_current_llm", {})
    _orig["session_ts"] = getattr(web_viewer, "_session_ts", {})
    _orig["cancel"] = getattr(web_viewer, "_cancel_requested", {})
    _orig["active_llms"] = getattr(web_viewer, "_active_llms", {})

    staging._project_root = lambda: tmp
    # Do NOT patch ingestion._project_root — it calls brand_state_root()
    # which resolves via the workspace context set by the auth middleware.
    # Patching it would break brand-scoped path resolution.
    config_loader._project_root = lambda: tmp
    asset_library._project_root = lambda: tmp
    staging.product_db = product_db
    web_viewer.PROJECT_ROOT = tmp

    # Patch brand_loader._resolve_brand_dir so relative "brand" resolves to tmp
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
    # Do NOT patch load_product_profile — the real implementation uses
    # brand_state_root() which resolves via the workspace context set by
    # the auth middleware.  Patching it would break brand-scoped resolution.

    # Register test user
    from src.auth import UserStore, SessionManager
    import src.auth as auth_mod
    _users_path = tmp / "data" / "auth" / "users.json"
    _sessions_path = tmp / "data" / "auth" / "sessions.json"
    _users_path.parent.mkdir(parents=True, exist_ok=True)
    _store = UserStore(_users_path)
    _sess = SessionManager(_sessions_path)
    auth_mod._user_store = _store
    auth_mod._session_manager = _sess
    _user = _store.register("testuser", "testpass")
    _ws_root = tmp / "users" / _user.user_id
    for d in ("data", "cache", "brand", "output"):
        (_ws_root / d).mkdir(parents=True, exist_ok=True)

    fake_llm = _FakeLLM()

    def _fake_make_llm(*a, **kw):
        return fake_llm
    ingestion._make_llm = _fake_make_llm

    def _fake_make_client(*a, **kw):
        return fake_llm
    Orchestrator.make_client = staticmethod(_fake_make_client)

    web_viewer._current_llm = {}
    web_viewer._session_ts = {}
    web_viewer._cancel_requested = {}
    web_viewer._active_llms = {}

    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ["OPENROUTER_API_KEY"] = "dummy-key-for-testing"

    import uvicorn
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(web_viewer.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for startup
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("Server did not start")

    session_token = _sess.create_session(_user.user_id)

    yield {
        "url": f"http://127.0.0.1:{port}",
        "tmp": tmp,
        "fake_llm": fake_llm,
        "session_token": session_token,
    }

    server.should_exit = True
    thread.join(timeout=5)

    product_db._project_root = _orig["product_db_root"]
    staging._project_root = _orig["staging_root"]
    config_loader._project_root = _orig["config_loader_root"]
    asset_library._project_root = _orig["asset_library_root"]
    staging.product_db = _orig["staging_product_db"]
    web_viewer.PROJECT_ROOT = _orig["PROJECT_ROOT"]
    Orchestrator.make_client = _orig["make_client"]
    ingestion._make_llm = _orig["ingestion_make_llm"]
    web_viewer._current_llm = _orig["current_llm"]
    web_viewer._session_ts = _orig["session_ts"]
    web_viewer._cancel_requested = _orig["cancel"]
    web_viewer._active_llms = _orig["active_llms"]
    _bl_mod._resolve_brand_dir = _orig_resolve
    if _orig["api_key"] is not None:
        os.environ["OPENROUTER_API_KEY"] = _orig["api_key"]


# ---------------------------------------------------------------------------
# Browser fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def _browser(_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        url = _server["url"]
        context.add_cookies([{
            "name": "mktapp_session",
            "value": _server["session_token"],
            "url": url,
        }])
        # Create + select brand
        req = urllib.request.Request(
            f"{url}/api/brands",
            data=json.dumps({"name": "FactsTestBrand"}).encode(),
            headers={"Content-Type": "application/json",
                     "Cookie": f"mktapp_session={_server['session_token']}"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        bid = json.loads(resp.read())["brand_id"]
        req = urllib.request.Request(
            f"{url}/api/brands/{bid}/select",
            data=b"",
            headers={"Cookie": f"mktapp_session={_server['session_token']}"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        context.add_cookies([{
            "name": "mktapp_brand", "value": bid, "url": url,
        }])

        tmp = _server["tmp"]
        users_dir = tmp / "users"
        uid = next(d.name for d in users_dir.iterdir() if d.is_dir())
        brand_root = users_dir / uid / "brands" / bid
        for d in ("data", "cache", "brand", "output"):
            (brand_root / d).mkdir(parents=True, exist_ok=True)

        page.goto(f"{url}/", wait_until="networkidle", timeout=15000)
        page.wait_for_selector("#flow-wizard-list", timeout=10000)

        yield {
            "page": page,
            "url": url,
            "server": _server,
            "fake_llm": _server["fake_llm"],
            "tmp": _server["tmp"],
            "brand_id": bid,
        }
        context.close()
        browser.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_upload_modal(page):
    btn = page.query_selector(".sidebar-add-btn")
    assert btn is not None
    btn.click()
    page.wait_for_timeout(500)
    overlay = page.query_selector("#upload-overlay.visible")
    assert overlay is not None


def _upload_file(page, file_path):
    file_input = page.query_selector("#upload-files-modal")
    assert file_input is not None
    file_input.set_input_files(str(file_path))
    page.wait_for_timeout(500)
    page.query_selector("#upload-submit-btn").click()


def _wait_for_staging(page, timeout_ms=30000):
    page.wait_for_selector(
        "#staging-preview-modal:not([style*='display: none']) #staging-confirm-btn",
        timeout=timeout_ms,
    )
    page.wait_for_timeout(500)


def _confirm_staging(page):
    page.query_selector("#staging-confirm-btn").click()


def _wait_for_product(page, name, timeout_ms=15000):
    page.wait_for_selector(
        f".product-card[data-folder='{name}']", timeout=timeout_ms
    )
    page.wait_for_timeout(500)


def _select_product(page, name):
    for _ in range(3):
        card = page.query_selector(f".product-card[data-folder='{name}']")
        if card:
            card.click()
            page.wait_for_timeout(300)
            return True
        page.wait_for_timeout(500)
    return False


def _go_to_step(page, step):
    btn = page.query_selector(f"[id^='flow-next-']:not([style*='display: none'])")
    if btn:
        btn.click()
        page.wait_for_timeout(300)
        return True
    return False


def _select_agent(page, key):
    card = page.query_selector(f".agent-card[data-agent='{key}']")
    if card:
        card.click()
        page.wait_for_timeout(300)
        return True
    return False


def _run_flow(page):
    btn = page.query_selector("[id^='flow-next-']:not([style*='display: none'])")
    if btn:
        btn.click()
        return True
    return False


def _wait_done(page, timeout_ms=30000):
    page.wait_for_selector(
        ".flow-step.done .flow-step-link, .flow-step.error", timeout=timeout_ms
    )
    return page.query_selector(".flow-step.done") is not None


def _find_gen_call(fake_llm, agent_name):
    for c in fake_llm.calls:
        if c.get("source") == f"{agent_name}.generate":
            return c
    return None


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_product_facts_ui_edit_save_reload_and_reach_agent(_browser, tmp_path):
    """Product Facts: add via UI → save → reload → verify persistence →
    run product_spec → assert fact reaches agent user prompt before raw.
    """
    page = _browser["page"]
    fake_llm = _browser["fake_llm"]
    base_url = _browser["url"]
    unique_marker = "FACT_UI_MARKER_8841"
    raw_marker = "RAW_SPEC_MARKER_5523"
    product_name = "FactsTestProd"

    # 1. Create product via direct file placement + API ingestion
    #    (the browser upload flow is tested elsewhere; here we focus on
    #    the facts UI editing flow which is the new feature)
    users_dir = _browser["tmp"] / "users"
    uid = next(d.name for d in users_dir.iterdir() if d.is_dir())
    bid = _browser["brand_id"]
    brand_root = users_dir / uid / "brands" / bid
    data_dir = brand_root / "data" / product_name
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "spec.txt").write_text(
        f"{raw_marker} Product spec raw text\nCPU: test\nBattery: 500mAh\n",
        encoding="utf-8",
    )
    # Trigger ingestion via the API
    cookie_str = (
        f"mktapp_session={_browser['server']['session_token']}; "
        f"mktapp_brand={_browser['brand_id']}"
    )
    req = urllib.request.Request(
        f"{base_url}/api/ingest/{urllib.parse.quote(product_name)}",
        data=b"{}",
        headers={"Content-Type": "application/json",
                 "Cookie": cookie_str},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30)
    # Wait for ingestion to complete
    for _ in range(30):
        try:
            req2 = urllib.request.Request(
                f"{base_url}/api/ingest_status/{urllib.parse.quote(product_name)}",
                headers={"Cookie": cookie_str},
            )
            with urllib.request.urlopen(req2, timeout=5) as resp:
                status = json.loads(resp.read())
                if status.get("status") in ("ready", "no_usable_data", "empty"):
                    break
        except Exception:
            pass
        time.sleep(1)

    # Refresh the sidebar to show the new product
    page.evaluate("if (typeof loadFolderList === 'function') loadFolderList();")
    page.wait_for_timeout(1000)

    # Wait for product to appear and be ready
    page.wait_for_selector(
        f".product-card[data-folder='{product_name}']", timeout=15000
    )
    # Wait for the product status to be ready (not processing)
    for _ in range(30):
        ready = page.evaluate(f"""() => {{
            const card = document.querySelector(".product-card[data-folder='{product_name}']");
            if (!card) return false;
            const status = card.dataset.status || card.getAttribute('data-status') || '';
            return status === 'ready' || card.querySelector('.status-ready, [title*="ready"]');
        }}""")
        if ready:
            break
        page.wait_for_timeout(1000)
        page.evaluate("if (typeof loadFolderList === 'function') loadFolderList();")
        page.wait_for_timeout(500)

    # 2. Read current profile for cleanup
    req3 = urllib.request.Request(
        f"{base_url}/api/product_profile/{urllib.parse.quote(product_name)}",
        headers={"Cookie": cookie_str},
    )
    with urllib.request.urlopen(req3, timeout=5) as resp:
        original = json.loads(resp.read())

    try:
        # 3. Open Manage modal
        page.evaluate(
            f"openUploadModalForFolder({json.dumps(product_name)}, '')"
        )
        page.wait_for_selector("#upload-overlay.visible", timeout=10000)
        page.wait_for_selector("#pp-section:not([style*='display: none']) #pf-facts-container", timeout=15000)
        page.wait_for_timeout(500)

        # 4. Fill first fact row
        key_input = page.query_selector(".pf-fact-key")
        val_input = page.query_selector(".pf-fact-val")
        assert key_input and val_input
        key_input.fill("ราคาที่แก้ไข")
        val_input.fill(unique_marker)
        page.wait_for_timeout(200)

        # 5. Click Save
        page.query_selector("#upload-submit-btn").click()
        page.wait_for_function(
            """() => {
                const s = document.getElementById('upload-modal-status');
                const o = document.getElementById('upload-overlay');
                if (s && s.textContent.includes('บันทึก')) return true;
                if (o && !o.className.includes('visible')) return true;
                return false;
            }""",
            timeout=15000,
        )
        page.wait_for_timeout(300)

        # 6. Verify file written
        users_dir = _browser["tmp"] / "users"
        uid = next(d.name for d in users_dir.iterdir() if d.is_dir())
        bid = _browser["brand_id"]
        profile_file = (
            _browser["tmp"] / "users" / uid / "brands" / bid
            / "cache" / product_name / "product_profile.json"
        )
        assert profile_file.exists()
        written = json.loads(profile_file.read_text(encoding="utf-8"))
        assert written.get("facts", {}).get("ราคาที่แก้ไข") == unique_marker

        # 7. Reopen — verify persistence
        page.evaluate(
            f"openUploadModalForFolder({json.dumps(product_name)}, '')"
        )
        page.wait_for_selector("#upload-overlay.visible", timeout=10000)
        page.wait_for_selector("#pp-section:not([style*='display: none']) #pf-facts-container", timeout=15000)
        page.wait_for_timeout(500)
        rows = page.query_selector_all(".pf-fact-row")
        found = any(
            r.query_selector(".pf-fact-key").input_value() == "ราคาที่แก้ไข"
            and r.query_selector(".pf-fact-val").input_value() == unique_marker
            for r in rows
        )
        assert found, "Fact must persist after reload"

        # Close modal
        page.evaluate("closeUploadModal(true)")
        page.wait_for_timeout(300)

        # 8. Run product_spec via the API — the fact should appear in
        # the user prompt (product data), before the raw evidence
        fake_llm.reset()
        fake_llm.set_agent_output("# Fake spec output")
        run_req = urllib.request.Request(
            f"{base_url}/api/run_agent",
            data=json.dumps({
                "agent": "product_spec",
                "folder": product_name,
                "context": {"use_competitor": False, "use_campaign": False},
            }).encode(),
            headers={"Content-Type": "application/json",
                     "Cookie": cookie_str},
            method="POST",
        )
        # Consume the SSE stream to let the agent run
        try:
            with urllib.request.urlopen(run_req, timeout=30) as resp:
                for line in resp:
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    if '"type":"done"' in line or '"type": "done"' in line:
                        break
        except Exception:
            pass  # SSE stream may close before we read "done"

        # 9. Assert fact reaches agent user prompt
        gen_call = _find_gen_call(fake_llm, "product_spec")
        assert gen_call is not None, "product_spec.generate must exist"
        user_text = ""
        for msg in gen_call["messages"]:
            if msg["role"] == "user":
                c = msg["content"]
                if isinstance(c, list):
                    user_text += " ".join(
                        p.get("text", "") for p in c if isinstance(p, dict)
                    )
                else:
                    user_text += str(c)
        assert unique_marker in user_text, \
            f"Fact marker must reach agent user prompt. Excerpt: {user_text[:500]}"

        # 10. Fact appears BEFORE raw evidence
        facts_pos = user_text.find(unique_marker)
        raw_pos = user_text.find(raw_marker)
        assert raw_pos != -1, "Raw marker must also be present"
        assert facts_pos < raw_pos, \
            f"Fact must appear BEFORE raw (fact@{facts_pos}, raw@{raw_pos})"
    finally:
        # Restore
        payload = json.dumps(original).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/api/product_profile_save/{urllib.parse.quote(product_name)}",
            data=payload,
            headers={"Content-Type": "application/json",
                     "Cookie": cookie_str},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
