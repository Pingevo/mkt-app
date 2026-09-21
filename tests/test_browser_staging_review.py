"""Browser tests for the staging/review contract — FREE-IMPORT lifecycle.

Current contract under test (real uvicorn + real Chromium):
- file upload stages ONE deterministic product — no AI segmentation;
- staging/review UI renders, review → commit materializes the product;
- cancel produces zero products;
- commit performs ZERO model calls — product is usable after
  deterministic materialization;
- URL import accepts only deterministically single-product pages —
  multi/ambiguous pages are rejected before anything is materialized;
- committed products carry source_import provenance where appropriate.

The FakeLLM here is a tripwire, not a fixture provider: import paths must
never call it.  Any accidental model invocation on a free path is caught
by the ``fake_llm.calls == []`` assertions.  No paid/live calls.
"""
from __future__ import annotations

import io
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
# Fixtures
# ---------------------------------------------------------------------------

# A spec-sheet-shaped file.  Even though it contains several product-looking
# blocks, the current contract imports every file as ONE deterministic
# product — AI splitting is a separate future explicit workflow.
_CATALOG_TEXT = """Product A Spec Sheet
Model: PA-100
Price: 1990 THB
Battery: 500mAh
Screen: 1.4 inch
Color: Black
Weight: 45g
Warranty: 1 year
Material: ABS plastic
Features: Heart rate monitor

Product B Spec Sheet
Model: PB-200
Price: 2990 THB
Battery: 800mAh
Screen: 1.8 inch
Color: Blue
Weight: 55g
Warranty: 2 years
Material: Aluminum
Features: GPS tracking

Product C Spec Sheet
Model: PC-300
Price: 3990 THB
Battery: 1200mAh
Screen: 2.0 inch
Color: Red
Weight: 65g
Warranty: 3 years
Material: Titanium
Features: 4G LTE calling"""


class _FakeLLM:
    """Recording tripwire — import paths must never invoke a model.

    ``chat`` only records the call source; tests assert the ledger stays
    empty on every free path.  Returns benign JSON so an accidental call
    surfaces as an assertion failure rather than a crash.  ``responses``
    serves deterministic JSON to the explicit enrich endpoint so the
    proposal-review UI can be exercised."""

    _RESPONSES = {
        "ingestion.metadata_summary": json.dumps({
            "summary": "สรุปจาก AI",
            "category": "Smartwatch",
            "derived_facts": {
                "battery": {"label": "แบตเตอรี่", "value": "750mAh"},
            },
        }),
        "voice_learner.analyze_product_positioning": json.dumps({
            "price_tier": "mid",
        }),
    }

    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        self.calls.append({"source": source})
        return self._RESPONSES.get(source, "{}")

    def close(self):
        pass

    def abort(self):
        pass


# ---------------------------------------------------------------------------
# Server fixture — real uvicorn + tripwire LLM
# ---------------------------------------------------------------------------

@pytest.fixture
def _server(tmp_path_factory):
    import web_viewer
    from src import product_db, staging, ingestion, config_loader, asset_library
    from src.orchestrator import Orchestrator
    import yaml as _yaml

    tmp = tmp_path_factory.mktemp("staging_e2e")
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

    # staging._project_root is NOT patched: production resolves
    # brand_state_root(workspace) so staged/committed data lands under
    # the brand root — same as DATA_DIR()/product_db in requests.
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
        "user_id": _user.user_id,
    }

    server.should_exit = True
    thread.join(timeout=5)

    product_db._project_root = _orig["product_db_root"]
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
        # Create brand
        req = urllib.request.Request(
            f"{url}/api/brands",
            data=json.dumps({"name": "StagingTestBrand"}).encode(),
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

        # Ensure brand data dirs exist
        tmp = _server["tmp"]
        users_dir = tmp / "users"
        uid = _server["user_id"]
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
            "user_id": _server["user_id"],
            "brand_root": brand_root,
        }
        context.close()
        browser.close()


def _api(browser_ctx, path, method="GET", body=None, files=None,
         allow_error=False):
    """Simple API call helper. browser_ctx is the _browser dict.
    allow_error: return the parsed JSON body even on non-2xx responses
    (e.g. URL rejection contract tests)."""
    url = browser_ctx["url"] + path
    headers = {"Cookie": f"mktapp_session={browser_ctx['server']['session_token']}; mktapp_brand={browser_ctx['brand_id']}"}
    if files:
        # multipart upload
        boundary = "----TestBoundary"
        body_parts = []
        for fname, content in files:
            body_parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{fname}"\r\nContent-Type: text/plain\r\n\r\n'.encode())
            body_parts.append(content)
            body_parts.append(b"\r\n")
        body_parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(body_parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    else:
        data = None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if allow_error:
            return json.loads(e.read())
        raise


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUploadStaging:
    """Upload → deterministic single-product staging → review → commit."""

    def test_upload_single_segment_review_commit(self, _browser):
        """Upload file → staging preview shows ONE deterministic product
        (no AI segmentation) → review → commit → product created."""
        page = _browser["page"]
        tmp = _browser["tmp"]
        fake_llm = _browser["fake_llm"]

        # Upload via API (same as browser would)
        result = _api(_browser, "/api/upload_stage", method="POST",
                      files=[("catalog.txt", _CATALOG_TEXT.encode("utf-8"))])
        assert "batch_id" in result, f"Expected batch_id, got: {result}"
        # FREE-IMPORT: exactly one deterministic segment — a catalog-shaped
        # file is imported as ONE editable product, never AI-split.
        assert len(result.get("segments", [])) == 1, \
            f"Expected 1 deterministic segment, got {len(result.get('segments', []))}"
        assert len(result.get("matches", [])) == 1

        # Open the upload modal and inject the staging state the UI would
        # have after startStagingUpload (same seam the browser path uses).
        page.click("text=+ เพิ่ม")
        page.wait_for_selector("#upload-overlay.visible", timeout=5000)
        page.evaluate("""(data) => {
            _stagingBatchId = data.batch_id;
            _stagingSegments = data.segments;
            _stagingMatches = data.matches;
            _uploadQueue = [];
            renderStagingPreview();
            document.getElementById('upload-submit-btn').style.display = 'none';
        }""", {"batch_id": result["batch_id"], "segments": result["segments"], "matches": result["matches"]})

        # Preview renders the single staged product with an action toggle
        preview = page.locator("#staging-preview-modal")
        assert preview.is_visible(), "Staging preview should be visible"
        toggles = preview.locator(".seg-toggle[data-seg-action]").all()
        assert len(toggles) == 1, f"Expected 1 segment toggle, got {len(toggles)}"

        confirm_btn = preview.locator("#staging-confirm-btn")
        assert "1" in confirm_btn.text_content()
        confirm_btn.click()
        page.wait_for_timeout(3000)

        # Exactly one product materialized — and not a model call was made.
        data_dir = _browser["brand_root"] / "data"
        products = [d.name for d in data_dir.iterdir()
                    if d.is_dir() and not d.name.startswith(".") and d.name != "auth"]
        assert len(products) == 1, f"Expected 1 product, got {products}"
        assert fake_llm.calls == [], \
            f"import path must be model-free, got: {[c['source'] for c in fake_llm.calls]}"

    def test_cancel_produces_no_products(self, _browser):
        """Cancel staging → zero products created."""
        page = _browser["page"]
        brand_root = _browser["brand_root"]

        # Upload via API
        result = _api(_browser, "/api/upload_stage", method="POST",
                      files=[("catalog.txt", _CATALOG_TEXT.encode("utf-8"))])
        batch_id = result["batch_id"]

        # Open modal + show staging preview
        page.click("text=+ เพิ่ม")
        page.wait_for_selector("#upload-overlay.visible", timeout=5000)
        page.evaluate("""(data) => {
            _stagingBatchId = data.batch_id;
            _stagingSegments = data.segments;
            _stagingMatches = data.matches;
            renderStagingPreview();
        }""", {"batch_id": result["batch_id"], "segments": result["segments"], "matches": result["matches"]})
        page.wait_for_timeout(300)

        # Cancel staging
        page.locator("#staging-preview-modal").locator("text=ยกเลิก").click()
        page.wait_for_timeout(500)

        # Verify no products created
        data_dir = brand_root / "data"
        products = [d.name for d in data_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
        assert len(products) == 0, f"Cancel should produce 0 products, got {products}"


class TestStagingToggle:
    """FREE-URL-FACTS-AND-STAGING-TOGGLE-01 — the staging action control is
    an accessible binary toggle (ข้าม / เพิ่ม), never a <select>."""

    _CATALOG_CSV = ("Model,SKU,Price\n"
                    "K67,K67-A,10\n"
                    "K72,K72-A,20\n"
                    "K52,K52-A,30\n")

    def _stage(self, _browser, page, n=2):
        """Real upload_stage → real multi-segment preview rendered in the
        real upload modal."""
        result = _api(_browser, "/api/upload_stage", method="POST",
                      files=[("catalog.csv", self._CATALOG_CSV.encode("utf-8"))])
        assert len(result.get("segments", [])) >= n, result
        page.click("text=+ เพิ่ม")
        page.wait_for_selector("#upload-overlay.visible", timeout=5000)
        page.evaluate("""(data) => {
            _stagingBatchId = data.batch_id;
            _stagingSegments = data.segments;
            _stagingMatches = data.matches;
            _uploadQueue = [];
            renderStagingPreview();
        }""", result)
        page.wait_for_timeout(300)
        return result["batch_id"]

    def test_control_is_toggle_not_select(self, _browser):
        page = _browser["page"]
        self._stage(_browser, page)
        preview = page.locator("#staging-preview-modal")
        assert preview.locator("select[data-seg-action]").count() == 0, \
            "the old <select> must be gone"
        toggles = preview.locator(".seg-toggle[data-seg-action]")
        assert toggles.count() == 3
        # every toggle exposes both labels as clickable radios
        for i in range(3):
            t = toggles.nth(i)
            assert t.locator("input[type=radio][value='create']").count() == 1
            assert t.locator("input[type=radio][value='skip']").count() == 1
            labels = t.locator("label").all_inner_texts()
            assert labels == ["ข้าม", "เพิ่ม"], labels
            # default for a new candidate is เพิ่ม (create)
            assert t.locator("input:checked").get_attribute("value") == "create"

    def test_toggle_switches_and_updates_confirm(self, _browser):
        page = _browser["page"]
        self._stage(_browser, page)
        preview = page.locator("#staging-preview-modal")
        confirm = preview.locator("#staging-confirm-btn")
        assert "3" in confirm.text_content()

        # row 0 → ข้าม: count drops, others unaffected
        preview.locator(
            ".seg-toggle[data-seg-action='0'] label",
            has_text="ข้าม").click()
        assert preview.locator(
            ".seg-toggle[data-seg-action='0'] input:checked"
        ).get_attribute("value") == "skip"
        assert preview.locator(
            ".seg-toggle[data-seg-action='1'] input:checked"
        ).get_attribute("value") == "create"
        assert "2" in confirm.text_content()
        assert not confirm.is_disabled()

        # all → ข้าม: confirm disabled with the new terminology
        for i in (1, 2):
            preview.locator(
                f".seg-toggle[data-seg-action='{i}'] label",
                has_text="ข้าม").click()
        assert confirm.is_disabled()

        # row 1 back → เพิ่ม: re-enabled, count 1
        preview.locator(
            ".seg-toggle[data-seg-action='1'] label",
            has_text="เพิ่ม").click()
        assert "1" in confirm.text_content()
        assert not confirm.is_disabled()

    def test_toggle_is_keyboard_accessible(self, _browser):
        page = _browser["page"]
        self._stage(_browser, page, n=1)
        grp = page.locator(".seg-toggle[data-seg-action='0']")
        assert grp.get_attribute("role") == "radiogroup"
        # native radio group: focus + arrow key moves the selection
        grp.locator("input[value='create']").focus()
        page.keyboard.press("ArrowRight")
        assert grp.locator("input:checked").get_attribute("value") == "skip"
        page.keyboard.press("ArrowLeft")
        assert grp.locator("input:checked").get_attribute("value") == "create"

    def test_commit_creates_only_toggled_on(self, _browser):
        """Real commit: only candidates left on เพิ่ม are materialized."""
        page = _browser["page"]
        self._stage(_browser, page)
        preview = page.locator("#staging-preview-modal")
        preview.locator(
            ".seg-toggle[data-seg-action='2'] label",
            has_text="ข้าม").click()
        preview.locator("#staging-confirm-btn").click()
        page.wait_for_function(
            "document.querySelector('#upload-modal-status')"
            ".textContent.includes('บันทึกเรียบร้อย')", timeout=15000)
        data_dir = _browser["brand_root"] / "data"
        products = sorted(d.name for d in data_dir.iterdir()
                          if d.is_dir() and not d.name.startswith("."))
        assert products == ["K67", "K72"], products


class TestUrlGate:
    """Deterministic URL gate: only confidently single-product pages are
    staged — multi/ambiguous pages are rejected before anything is
    materialized.  page_class comes from classify_product_signals inside
    fetch_product_page (JSON-LD), never from a model."""

    def _fake_fetch(self, page_class):
        def _fetch(url, **kw):
            return {
                "original_url": url,
                "final_url": url,
                "canonical_url": url,
                "fetched_at": "2025-01-01T00:00:00",
                "fetched_via": "test",
                "page_title": "Test Product Page",
                "og_title": "Test OG",
                "text": _CATALOG_TEXT,
                "images": [],
                "page_class": page_class,
            }
        return _fetch

    def _products(self, browser):
        data_dir = browser["brand_root"] / "data"
        return [d.name for d in data_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".")
                and d.name != "auth"]

    def test_multi_product_url_rejected(self, _browser, monkeypatch):
        """page_class=multi → clear rejection, zero products, zero model calls."""
        from src import url_import as _url_mod

        monkeypatch.setattr(_url_mod, "fetch_product_page",
                            self._fake_fetch("multi"))
        fake_llm = _browser["fake_llm"]

        result = _api(_browser, "/api/product_from_url", method="POST",
                      body={"url": "https://example.com/test-catalog"},
                      allow_error=True)
        assert result.get("ok") is False, f"expected rejection: {result}"
        assert result.get("multi_product") is True, result
        assert "สินค้า" in (result.get("error") or "")

        assert self._products(_browser) == [], \
            f"multi URL must create nothing: {self._products(_browser)}"
        assert fake_llm.calls == [], \
            f"rejected URL must not call a model: {fake_llm.calls}"

    def test_ambiguous_url_rejected(self, _browser, monkeypatch):
        """page_class=ambiguous → fail safe, zero products."""
        from src import url_import as _url_mod

        monkeypatch.setattr(_url_mod, "fetch_product_page",
                            self._fake_fetch("ambiguous"))
        result = _api(_browser, "/api/product_from_url", method="POST",
                      body={"url": "https://example.com/ambiguous"},
                      allow_error=True)
        assert result.get("ok") is False, f"expected rejection: {result}"
        assert result.get("ambiguous_product") is True, result
        assert self._products(_browser) == [], \
            f"ambiguous URL must create nothing: {self._products(_browser)}"

    def test_single_product_url_stages_and_commits(self, _browser, monkeypatch):
        """page_class=single → staged deterministic product → commit →
        usable product carrying source_import provenance."""
        from src import url_import as _url_mod

        monkeypatch.setattr(_url_mod, "fetch_product_page",
                            self._fake_fetch("single"))
        fake_llm = _browser["fake_llm"]

        result = _api(_browser, "/api/product_from_url", method="POST",
                      body={"url": "https://example.com/integrity-test"})
        assert result.get("staged") is True, result
        assert len(result.get("segments", [])) == 1
        batch_id = result["batch_id"]

        commit_result = _api(_browser, f"/api/stage/{batch_id}/commit",
                             method="POST",
                             body={"choices": [{"segment_index": 0, "action": "create"}]})
        assert len(commit_result.get("created", [])) == 1

        cache_dir = _browser["brand_root"] / "cache"
        records = list(cache_dir.glob("*/product.json"))
        assert len(records) == 1, f"expected 1 product record, got {records}"
        record = json.loads(records[0].read_text())
        assert record.get("status") == "ready"
        si = record.get("source_import")
        assert si is not None, "committed URL product missing source_import"
        assert si.get("original_url") == "https://example.com/integrity-test"
        assert si.get("final_url") == "https://example.com/integrity-test"
        assert fake_llm.calls == [], \
            f"URL import + commit must be model-free: {fake_llm.calls}"


class TestDeterministicCommit:
    """Product integrity after deterministic (model-free) commit."""

    def test_committed_product_deterministic_state(self, _browser):
        """A committed product is usable from deterministic data alone:
        ready status, raw_text/files/metadata present, no AI-derived
        fields, no model calls."""
        result = _api(_browser, "/api/upload_stage", method="POST",
                      files=[("catalog.txt", _CATALOG_TEXT.encode("utf-8"))])
        batch_id = result["batch_id"]
        commit_result = _api(_browser, f"/api/stage/{batch_id}/commit",
                             method="POST",
                             body={"choices": [{"segment_index": 0, "action": "create"}]})
        assert len(commit_result.get("created", [])) == 1

        cache_dir = _browser["brand_root"] / "cache"
        records = list(cache_dir.glob("*/product.json"))
        assert len(records) == 1, f"expected 1 product record, got {records}"
        record = json.loads(records[0].read_text())

        # Deterministic materialization evidence
        assert record.get("status") == "ready"
        assert record.get("raw_text"), "missing raw_text evidence"
        assert record.get("text_extracts"), "missing text_extracts"
        assert len(record.get("files", [])) > 0, "missing files"
        meta = record.get("metadata") or {}
        assert meta.get("summary"), "deterministic metadata summary missing"
        assert meta.get("file_count") == 1

        # No AI artifacts — enrichment is a separate explicit action.
        assert not record.get("derived_facts"), \
            "free import must not produce derived_facts"
        assert not record.get("ai_proposal"), \
            "free import must not produce an AI proposal"

        # The product is usable: normal Product Information path serves it.
        name = record["product_id"]
        info = _api(_browser, f"/api/product_info/{urllib.parse.quote(name)}")
        assert info.get("product_id") == name
        assert info.get("source_files"), "product has no source_files"
        assert _browser["fake_llm"].calls == [], \
            f"import must be model-free: {_browser['fake_llm'].calls}"

    def test_commit_ready_with_zero_model_calls(self, _browser):
        """Status reaches ready with the model ledger empty — readiness is
        deterministic materialization, not enrichment success."""
        fake_llm = _browser["fake_llm"]
        result = _api(_browser, "/api/upload_stage", method="POST",
                      files=[("catalog.txt", _CATALOG_TEXT.encode("utf-8"))])
        batch_id = result["batch_id"]
        _api(_browser, f"/api/stage/{batch_id}/commit", method="POST",
             body={"choices": [{"segment_index": 0, "action": "create"}]})

        cache_dir = _browser["brand_root"] / "cache"
        records = list(cache_dir.glob("*/product.json"))
        assert len(records) == 1
        record = json.loads(records[0].read_text())
        assert record.get("status") == "ready"
        assert fake_llm.calls == [], \
            f"commit must be model-free, got: {[c['source'] for c in fake_llm.calls]}"


class TestStaleSourceReview:
    """SOURCE-STALE-01 — a disk mutation after proposal generation must
    surface in the review UI: stale warning shown, accept withheld."""

    def test_stale_source_review_ui(self, _browser):
        page = _browser["page"]
        brand_root = _browser["brand_root"]
        fake_llm = _browser["fake_llm"]

        # Deterministic import → committed product (free path).
        result = _api(_browser, "/api/upload_stage", method="POST",
                      files=[("spec.txt", _CATALOG_TEXT.encode("utf-8"))])
        commit = _api(_browser, f"/api/stage/{result['batch_id']}/commit",
                      method="POST",
                      body={"choices": [{"segment_index": 0,
                                         "action": "create"}]})
        name = commit["created"][0]
        assert fake_llm.calls == []  # import stayed model-free

        # Explicit paid step: generate the proposal (the only model calls).
        enc = urllib.parse.quote(name)
        enr = _api(_browser, f"/api/product_ai/{enc}/enrich", method="POST",
                   body={"scopes": ["facts"]})
        assert enr.get("ok"), f"enrich failed: {enr}"
        assert len(fake_llm.calls) == 1

        view = _api(_browser, f"/api/product_ai/{enc}/proposal")
        assert view["source_stale"] is False
        pid = view["proposal"]["proposal_id"]

        # Pending disk mutation — a new source file lands before re-ingest
        # rebuilds product.json (the real save_uploaded_files window).
        (brand_root / "data" / name / "extra_spec.txt").write_text(
            "Battery 900mAh rev B", encoding="utf-8")

        view = _api(_browser, f"/api/product_ai/{enc}/proposal")
        assert view["source_stale"] is True
        # Real pipeline keys proposal facts by label — take the actual key.
        fkey = next(iter(view["proposal"]["facts"]))

        # Review UI: warning visible, NO accept path offered, keep/edit/
        # discard preserved.
        page.evaluate(f"openProductDetail({json.dumps(name)})")
        page.evaluate("pdAiOpen()")
        page.wait_for_selector("#pd-ai-overlay.visible", timeout=10000)
        page.wait_for_selector("#pd-ai-body .pd-ai-stale", timeout=10000)
        assert "ไฟล์ต้นทาง" in page.inner_text("#pd-ai-body .pd-ai-stale")
        rows = page.locator("#pd-ai-body .pd-ai-row")
        assert rows.count() >= 1, "proposal rows should still render"
        assert page.locator(
            "#pd-ai-body button:has-text('รับค่า')").count() == 0
        assert page.locator(
            "#pd-ai-body button:has-text('ใช้ค่าที่ AI แนะนำ')").count() == 0
        assert page.locator(
            "#pd-ai-body button:has-text('รับเฉพาะข้อมูลใหม่')").count() == 0
        assert page.locator(
            "#pd-ai-body button:has-text('เก็บค่าเดิม')").count() >= 1 or \
            page.locator(
                "#pd-ai-body button:has-text('ไม่ใช้')").count() >= 1
        assert page.locator(
            "#pd-ai-body button:has-text('แก้ไข')").count() >= 1
        assert page.locator(
            "#pd-ai-body button:has-text('ทิ้งข้อเสนอทั้งหมด')").count() == 1

        # Server still guards: a direct accept resolves to a source_changed
        # conflict, nothing applied — view/resolve made no model calls.
        out = _api(_browser, f"/api/product_ai/{enc}/resolve", method="POST",
                   body={"proposal_id": pid, "resolutions": [
                       {"kind": "fact", "key": fkey,
                        "action": "accept"}]})
        assert out["conflicts"][0].get("reason") == "source_changed"
        assert out.get("applied") == []
        assert len(fake_llm.calls) == 1


class TestFileViewerIsolation:
    """File viewer must not leak cross-brand/cross-user data."""

    # 1x1 transparent PNG — deterministic binary fixture
    _PNG = __import__("base64").b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    _SECRET_B = "BRAND_B_SECRET_MARKER_7f3a"
    _SECRET_U = "USER_B_SECRET_MARKER_9c1e"

    def _req(self, _browser, path, session=None, brand_id=None):
        """GET path with explicit session/brand cookies → (status, content-type, body)."""
        import http.client
        port = int(_browser["url"].split(":")[-1])
        session = session or _browser["server"]["session_token"]
        brand_id = brand_id if brand_id is not None else _browser["brand_id"]
        conn = http.client.HTTPConnection("127.0.0.1", port)
        cookie = f"mktapp_session={session}"
        if brand_id:
            cookie += f"; mktapp_brand={brand_id}"
        conn.request("GET", path, headers={"Cookie": cookie})
        resp = conn.getresponse()
        result = (resp.status, resp.getheader("Content-Type", "") or "", resp.read())
        conn.close()
        return result

    def _make_brand(self, _browser, session, name):
        """Create a brand via API for the session's user → (brand_id, brand_root)."""
        import urllib.request as _ur
        req = _ur.Request(
            f"{_browser['url']}/api/brands",
            data=json.dumps({"name": name}).encode(),
            headers={"Content-Type": "application/json",
                     "Cookie": f"mktapp_session={session}"},
            method="POST")
        bid = json.loads(_ur.urlopen(req, timeout=5).read())["brand_id"]
        return bid

    def _brand_data(self, _browser, user_id, brand_id):
        root = _browser["tmp"] / "users" / user_id / "brands" / brand_id
        (root / "data").mkdir(parents=True, exist_ok=True)
        return root / "data"

    def test_own_brand_read_success(self, _browser):
        """Positive control — owner reads own product text + image → 200."""
        data_dir = self._brand_data(_browser, _browser["user_id"], _browser["brand_id"])
        pdir = data_dir / "OwnProduct"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "marker_a.txt").write_text("BRAND_A_MARKER_CONTENT", encoding="utf-8")
        (pdir / "img_a.png").write_bytes(self._PNG)

        status, ct, body = self._req(
            _browser, "/api/product_source_file/OwnProduct/marker_a.txt")
        assert status == 200
        assert "BRAND_A_MARKER_CONTENT" in body.decode("utf-8")

        status, ct, body = self._req(
            _browser, "/api/product_source_file/OwnProduct/img_a.png")
        assert status == 200
        assert "image" in ct, f"Expected image MIME, got {ct}"
        assert body == self._PNG

    def test_cross_brand_read_denied(self, _browser):
        """Same user, different brand — Brand B's files are invisible to Brand A context."""
        session = _browser["server"]["session_token"]
        bid_b = self._make_brand(_browser, session, "BrandB")
        data_b = self._brand_data(_browser, _browser["user_id"], bid_b)
        pdir = data_b / "SecretProductB"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "secret_b.txt").write_text(self._SECRET_B, encoding="utf-8")
        (pdir / "img_b.png").write_bytes(self._PNG)

        # Brand A context asks for Brand B's product folder — contain_path
        # resolves under Brand A's data dir → not found, secret never served.
        status, ct, body = self._req(
            _browser, "/api/product_source_file/SecretProductB/secret_b.txt")
        assert self._SECRET_B not in body.decode("utf-8", errors="replace"), \
            "Cross-brand read leaked Brand B file content"
        assert status in (400, 404) or "ไม่พบ" in body.decode("utf-8", errors="replace"), \
            f"Expected denial/not-found, got {status}"

        status, ct, body = self._req(
            _browser, "/api/product_source_file/SecretProductB/img_b.png")
        assert "image" not in ct or body != self._PNG, \
            "Cross-brand read leaked Brand B image bytes"

        # Presenting Brand B's id is fine (same owner) — sanity: it resolves.
        status, ct, body = self._req(
            _browser, "/api/product_source_file/SecretProductB/secret_b.txt",
            brand_id=bid_b)
        assert status == 200 and self._SECRET_B in body.decode("utf-8")

    def test_cross_user_read_denied(self, _browser):
        """User A session cannot reach User B's product files — even with B's brand id."""
        import src.auth as auth_mod
        session_a = _browser["server"]["session_token"]

        # Register user B + create B's brand + product file under B's workspace
        user_b = auth_mod._user_store.register("userb_iso", "passb")
        session_b = auth_mod._session_manager.create_session(user_b.user_id)
        bid_b = self._make_brand(_browser, session_b, "UserBBrand")
        data_b = self._brand_data(_browser, user_b.user_id, bid_b)
        pdir = data_b / "UserBProduct"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "secret_u.txt").write_text(self._SECRET_U, encoding="utf-8")

        # (a) User A session + A's own brand + folder name of B's product
        #     → contain_path stays inside A's brand data → not found.
        status, ct, body = self._req(
            _browser, "/api/product_source_file/UserBProduct/secret_u.txt",
            session=session_a)
        body_s = body.decode("utf-8", errors="replace")
        assert self._SECRET_U not in body_s, "Cross-user read leaked User B content"
        assert status in (400, 404) or "ไม่พบ" in body_s, \
            f"Expected denial/not-found, got {status}"

        # (b) User A session presenting User B's brand_id cookie → ownership
        #     check fails → context degrades to user-only → 403.
        status, ct, body = self._req(
            _browser, "/api/product_source_file/UserBProduct/secret_u.txt",
            session=session_a, brand_id=bid_b)
        assert status == 403, f"Expected 403 for foreign brand cookie, got {status}"
        assert self._SECRET_U not in body.decode("utf-8", errors="replace")

    def test_traversal_blocked(self, _browser):
        """../ in filename cannot escape product scope — contain_path rejects it."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", int(_browser["url"].split(":")[-1]))
        conn.request("GET",
            "/api/product_source_file/test/..%2F..%2Fstaging.py",
            headers={"Cookie": f"mktapp_session={_browser['server']['session_token']}; mktapp_brand={_browser['brand_id']}"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        assert "ไม่พบ" in body.get("content", "") or resp.status in (400, 404), \
            f"Traversal should be denied, got {resp.status}: {body}"
        conn.close()

    def test_nonexistent_product_returns_not_found(self, _browser):
        """Reading a file from a nonexistent product returns error content."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", int(_browser["url"].split(":")[-1]))
        conn.request("GET",
            "/api/product_source_file/NonexistentProduct/some.txt",
            headers={"Cookie": f"mktapp_session={_browser['server']['session_token']}; mktapp_brand={_browser['brand_id']}"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        assert "ไม่พบ" in body.get("content", ""), \
            f"Expected not-found message, got: {body}"
        conn.close()

    def test_no_auth_returns_error(self, _browser):
        """Request without session cookie is rejected."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", int(_browser["url"].split(":")[-1]))
        conn.request("GET",
            "/api/product_source_file/anything/file.txt")
        resp = conn.getresponse()
        assert resp.status in (401, 403), f"Expected 401/403, got {resp.status}"
        conn.close()
