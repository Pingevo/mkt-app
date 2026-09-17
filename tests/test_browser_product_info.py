"""Browser E2E — Product Information: see derived → edit → save → reload → reset.

Standalone browser test that proves the Product Information UI shows
system-derived values, lets the user edit them, saves manual corrections,
and supports reset back to the derived value.

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
# FakeLLM — returns structured JSON with derived_facts
# ---------------------------------------------------------------------------

class _FakeLLM:
    """Deterministic LLM double — returns derived_facts for ingestion.metadata_summary."""

    def __init__(self):
        self.calls = []
        self._agent_output = "# Fake spec output"
        self._derived_facts = {
            "price": {"label": "ราคา", "value": "DERIVED_PRICE_3990"},
            "battery_capacity": {"label": "แบตเตอรี่", "value": "DERIVED_BATTERY_800mAh"},
        }

    def reset(self):
        self.calls.clear()

    def set_agent_output(self, text):
        self._agent_output = text

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        self.calls.append({"messages": messages, "kwargs": kwargs, "source": source})
        if source == "ingestion.metadata_summary":
            return json.dumps({
                "summary": "Test product with derived facts",
                "category": "Test Category",
                "derived_facts": self._derived_facts,
            }, ensure_ascii=False)
        if source == "voice_learner.analyze_product_positioning":
            return json.dumps({"audience": {}, "competitors": [], "differentiators": []})
        if source == "product_segmentation.segment_products":
            return json.dumps({"products": []})
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
# Server fixture — same pattern as test_browser_product_facts.py
# ---------------------------------------------------------------------------

@pytest.fixture
def _server(tmp_path_factory):
    import web_viewer
    from src import product_db, staging, ingestion, config_loader, asset_library
    from src.orchestrator import Orchestrator
    import yaml as _yaml

    tmp = tmp_path_factory.mktemp("info_e2e")
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
    config_loader._project_root = lambda: tmp
    asset_library._project_root = lambda: tmp
    staging.product_db = product_db
    web_viewer.PROJECT_ROOT = tmp

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
        req = urllib.request.Request(
            f"{url}/api/brands",
            data=json.dumps({"name": "InfoTestBrand"}).encode(),
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
# Test
# ---------------------------------------------------------------------------

def test_product_info_see_derived_edit_save_reload_reset(_browser):
    """Product Information UI: see derived value → edit → save → reload →
    see edited value → reset → see original derived value again.
    """
    page = _browser["page"]
    base_url = _browser["url"]
    product_name = "InfoTestProd"
    derived_marker = "DERIVED_PRICE_3990"
    manual_marker = "MANUAL_PRICE_3590"

    # 1. Create product via direct file + API ingestion.  Free ingest is
    # deterministic-only (llm=None), so derived_facts come from explicit
    # `label | value` source lines — not from the FakeLLM.
    users_dir = _browser["tmp"] / "users"
    uid = next(d.name for d in users_dir.iterdir() if d.is_dir())
    bid = _browser["brand_id"]
    brand_root = users_dir / uid / "brands" / bid
    data_dir = brand_root / "data" / product_name
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "spec.txt").write_text(
        "Product spec: price 3990 baht, battery 800mAh\n"
        f"price | {derived_marker}\n"
        "RAW_INFO_MARKER_7799\n",
        encoding="utf-8",
    )
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

    # Verify derived_facts were stored (via API — no workspace context needed)
    req_info = urllib.request.Request(
        f"{base_url}/api/product_info/{urllib.parse.quote(product_name)}",
        headers={"Cookie": cookie_str},
    )
    with urllib.request.urlopen(req_info, timeout=5) as resp:
        info = json.loads(resp.read())
    assert "derived_facts" in info, "derived_facts must be stored after ingestion"
    assert info["derived_facts"]["price"]["value"] == derived_marker

    # Refresh sidebar
    page.evaluate("if (typeof loadFolderList === 'function') loadFolderList();")
    page.wait_for_timeout(1000)
    page.wait_for_selector(
        f".product-card[data-folder='{product_name}']", timeout=15000
    )
    for _ in range(30):
        ready = page.evaluate(f"""() => {{
            const card = document.querySelector(".product-card[data-folder='{product_name}']");
            if (!card) return false;
            const status = card.dataset.status || card.getAttribute('data-status') || '';
            return status === 'ready';
        }}""")
        if ready:
            break
        page.wait_for_timeout(1000)
        page.evaluate("if (typeof loadFolderList === 'function') loadFolderList();")
        page.wait_for_timeout(500)

    # Read original profile for cleanup
    req3 = urllib.request.Request(
        f"{base_url}/api/product_profile/{urllib.parse.quote(product_name)}",
        headers={"Cookie": cookie_str},
    )
    with urllib.request.urlopen(req3, timeout=5) as resp:
        original = json.loads(resp.read())

    try:
        # 2. Open Manage modal — should see derived value
        page.evaluate(
            f"openUploadModalForFolder({json.dumps(product_name)}, '')"
        )
        page.wait_for_selector("#upload-overlay.visible", timeout=10000)
        page.wait_for_selector(
            "#pp-section:not([style*='display: none']) #pf-facts-container",
            timeout=15000,
        )
        page.wait_for_timeout(500)

        # 3. Verify derived value is visible
        rows = page.query_selector_all(".pf-fact-row")
        derived_row = None
        for r in rows:
            val = r.query_selector(".pf-fact-val")
            if val and derived_marker in val.input_value():
                derived_row = r
                break
        assert derived_row is not None, \
            f"Derived value '{derived_marker}' must be visible in the UI"

        # Verify provenance badge shows "มาจากไฟล์"
        badge_text = page.evaluate("""() => {
            const row = [...document.querySelectorAll('.pf-fact-row')].find(
                r => { const v = r.querySelector('.pf-fact-val'); return v && v.value.includes('DERIVED_PRICE'); }
            );
            if (!row) return '';
            const span = row.querySelector('span span');
            return span ? span.textContent : '';
        }""")
        assert "มาจากไฟล์" in badge_text, \
            f"Derived value should show 'มาจากไฟล์' badge, got: {badge_text}"

        # 4. Edit the derived value
        val_input = derived_row.query_selector(".pf-fact-val")
        val_input.fill(manual_marker)
        page.wait_for_timeout(200)

        # 5. Save
        page.evaluate("""() => {
            const btn = document.getElementById('upload-submit-btn');
            if (btn) btn.click();
        }""")
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

        # 6. Verify manual correction saved
        profile_file = (
            _browser["tmp"] / "users" / uid / "brands" / bid
            / "cache" / product_name / "product_profile.json"
        )
        written = json.loads(profile_file.read_text(encoding="utf-8"))
        assert written.get("facts", {}).get("price") == manual_marker, \
            f"Manual correction must be saved with machine key 'price', got: {written.get('facts')}"

        # Wait for modal to fully close (setTimeout from save fires after 800ms)
        page.wait_for_selector("#upload-overlay", state="hidden", timeout=5000)
        page.wait_for_timeout(500)

        # 7. Reopen — verify edited value + "แก้ไขแล้ว" badge
        page.evaluate(
            f"openUploadModalForFolder({json.dumps(product_name)}, '')"
        )
        page.wait_for_selector("#upload-overlay.visible", timeout=10000)
        page.wait_for_selector(
            "#pp-section:not([style*='display: none']) #pf-facts-container",
            timeout=15000,
        )
        page.wait_for_timeout(500)

        rows = page.query_selector_all(".pf-fact-row")
        manual_row = None
        for r in rows:
            val = r.query_selector(".pf-fact-val")
            if val and manual_marker in val.input_value():
                manual_row = r
                break
        assert manual_row is not None, \
            f"Edited value '{manual_marker}' must be visible after reload"

        # Verify badge shows "แก้ไขแล้ว"
        badge_text = page.evaluate("""() => {
            const row = [...document.querySelectorAll('.pf-fact-row')].find(
                r => { const v = r.querySelector('.pf-fact-val'); return v && v.value.includes('MANUAL_PRICE'); }
            );
            if (!row) return '';
            const span = row.querySelector('span span');
            return span ? span.textContent : '';
        }""")
        assert "แก้ไขแล้ว" in badge_text, \
            f"Edited value should show 'แก้ไขแล้ว' badge, got: {badge_text}"

        # 8. Reset — click the reset button (use evaluate to bypass visibility check)
        reset_clicked = page.evaluate("""() => {
            const row = [...document.querySelectorAll('.pf-fact-row')].find(
                r => { const v = r.querySelector('.pf-fact-val'); return v && v.value.includes('MANUAL_PRICE'); }
            );
            if (!row) return false;
            const btn = row.querySelector("button[title*='คืนค่า']") ||
                        row.querySelector("button[title*='กลับเป็นค่าเดิม']") ||
                        row.querySelector("button[onclick*='_pfResetFactRow']");
            if (!btn) return false;
            btn.click();
            return true;
        }""")
        assert reset_clicked, "Reset button must be present and clickable"
        page.wait_for_timeout(200)

        # 9. Verify value reverted to derived
        val_after_reset = manual_row.query_selector(".pf-fact-val").input_value()
        assert derived_marker in val_after_reset, \
            f"After reset, value must revert to derived '{derived_marker}', got: '{val_after_reset}'"

        # 10. Save the reset (so manual correction is removed)
        save_result = page.evaluate("""async () => {
            if (typeof saveProductProfile !== 'function') return 'no saveProductProfile';
            try {
                await saveProductProfile();
                return 'saved';
            } catch (e) {
                return 'error: ' + e.message;
            }
        }""")
        assert save_result == 'saved', f"Save must succeed, got: {save_result}"
        page.wait_for_timeout(1000)  # Wait for file write to complete

        # 11. Verify manual correction was removed (derived value restored)
        written2 = json.loads(profile_file.read_text(encoding="utf-8"))
        facts = written2.get("facts", {})
        assert "price" not in facts or facts.get("price") != manual_marker, \
            "Manual correction must be removed after reset+save"

        # 12. Reopen — verify derived value is back with "มาจากไฟล์" badge
        page.evaluate(
            f"openUploadModalForFolder({json.dumps(product_name)}, '')"
        )
        page.wait_for_selector("#upload-overlay.visible", timeout=10000)
        page.wait_for_selector(
            "#pp-section:not([style*='display: none']) #pf-facts-container",
            timeout=15000,
        )
        page.wait_for_timeout(500)

        rows = page.query_selector_all(".pf-fact-row")
        derived_back = False
        for r in rows:
            val = r.query_selector(".pf-fact-val")
            if val and derived_marker in val.input_value():
                derived_back = True
                break
        assert derived_back, \
            "After reset+save+reload, derived value must be visible again"

        # Close modal
        page.evaluate("closeUploadModal(true)")
        page.wait_for_timeout(300)

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
