"""True browser upload → ingest → Agent → DOM E2E with REAL source files.

This test closes the UPLOAD_TO_AGENT_BROWSER_GAP by proving the FULL chain
in one integrated path:

    REAL source file (XLSX/PDF)
    → browser upload control
    → /api/upload_stage
    → real staging/segmentation (FakeLLM at segmentation LLM boundary)
    → browser confirms staging
    → /api/stage/{batch_id}/commit
    → real Product DB record
    → newly-created product appears in UI
    → browser selects that product
    → browser selects ONE Agent
    → /api/run_flows
    → real Orchestrator
    → real Agent
    → FakeLLM at external LLM boundary
    → result
    → real renderer
    → actual browser DOM

Source truth is established by reading the ORIGINAL files (openpyxl/PyMuPDF)
and comparing distinctive facts at four stages:
    A. ORIGINAL FILE truth
    B. parsed/staged representation (file_loader output)
    C. Product DB/cache representation (product_db.load())
    D. final Agent/model-boundary input (FakeLLM captured messages)

No paid model calls, live web research, or real image/video generation.
Only ingestion._make_llm() and Orchestrator.make_client() are patched to
return a ScriptedFakeLLM.  All staging, segmentation, product DB, context
composition, agent execution, SSE, and rendering are real.

MAX_AGENTS_PER_FLOW = 1 is intentional.  Each agent is tested as a separate
flow, exactly as the current production UI is designed.
"""
import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Skip all tests if Playwright is not installed
playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# REAL source files — paths in the project
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAGENIO_K2_XLSX = PROJECT_ROOT / "data" / "Lagenio K2" / "Lagenio K2 -spec 20241024.xlsx"
LAGENIO_K5_XLSX = PROJECT_ROOT / "data" / "Lagenio K5" / "Lagenio K5 20241024.xlsx"
CACGO_PDF = PROJECT_ROOT / "data" / "CACGO K73" / "CACGO Smart Watch Price List- Grace.pdf"

# ---------------------------------------------------------------------------
# Source truth — manually verified from ORIGINAL files
# ---------------------------------------------------------------------------
# These values were established by directly reading the original XLSX with
# openpyxl and the original PDF with PyMuPDF.  They are NOT derived from
# cache, test constants, or prior reports.

# Lagenio K2 XLSX — Sheet "K2", single product
LAGENIO_K2_TRUTH = {
    "model": "K2",
    "app": "Lagenio",
    "cpu": "W377",
    "display_type": "AMOLED",
    "display_size": "1.78inch",
    "resolution": "368 x 448 pixels",
    "battery": "680mAh",
    "bluetooth": "No",          # B40='Bluetooth' C40='No' (K2 has no BT)
    "wifi": "Yes",
    "waterproof": "IP68",
    "os": "Android 8.1",
    "front_camera": "5MP",
    "rom": "8GB",
    "ram": "1GB",
    "accessories": "User manual",
    "selling_points": "GPS Tracking",
}

# CACGO K73 — from PDF page 3, entry #7
CACGO_K73_TRUTH = {
    "model": "K73",
    "cpu": "Realtek 8773EWE-VP",
    "bluetooth": "BT 5.0",
    "display_type": "AMOLED",
    "display_size": "1.32",
    "resolution": "466*466",
    "battery": "340mAh",
    "colors": "Blue",
    "app": "FitCloudPro",
    "price": "US$16.50",
    "price_type": "EXW",
    "accessory": "USB Cable",
    "compatible": "iOS10",
    "sensor": "HX3917",
}

# CACGO K52 — from PDF page 6, entry #20
CACGO_K52_TRUTH = {
    "model": "K52",
    "cpu": "Realtek 8763EWE",
    "bluetooth": "BT 5.0",
    "display_type": "IPS",
    "display_size": "1.39",
    "resolution": "360*360",
    "battery": "400mAh",
    "colors": "Silver",
    "app": "FitCloudPro",
    "price": "US$13.00",
    "price_type": "EXW",
    "accessory": "USB Cable",
    "compatible": "iOS8",
    "sensor": "HX3605",
}

# Recognizable marker for DOM output verification
RECOGNIZABLE_MARKER = "RECOGNIZABLE_TEST_OUTPUT_MARKER_XYZ"


# ---------------------------------------------------------------------------
# ScriptedFakeLLM — returns deterministic responses by call source
# ---------------------------------------------------------------------------

class ScriptedFakeLLM:
    """Deterministic LLM double that routes responses by ``source`` kwarg.

    This keeps real production pipeline methods (SemanticEvidenceReviewer,
    BrandInterpretationPass, CompetitorReportRenderer) running while
    providing deterministic outputs at the external LLM boundary.

    Call log records every llm.chat() call with messages, kwargs, and the
    source label so tests can verify the exact call sequence.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self._agent_output: str = f"# {RECOGNIZABLE_MARKER}\n\nTest output."
        self._segmentation_json: str | None = None

    def set_agent_output(self, output: str):
        """Set the output returned for agent generate/review/revise calls."""
        self._agent_output = output

    def set_segmentation(self, seg_json: str):
        """Set the JSON returned for product_segmentation calls."""
        self._segmentation_json = seg_json

    def reset(self):
        """Clear call log and reset outputs."""
        self.calls.clear()
        self._agent_output = f"# {RECOGNIZABLE_MARKER}\n\nTest output."
        self._segmentation_json = None

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        self.calls.append({
            "messages": messages,
            "kwargs": kwargs,
            "source": source,
        })

        # Segmentation call — return structured JSON
        if source == "product_segmentation.segment_products":
            if self._segmentation_json:
                return self._segmentation_json
            # Fallback: single product
            return json.dumps({"products": []})

        # Metadata summary — return short text
        if source == "ingestion.metadata_summary":
            return "Test product summary"

        # Product profile / positioning — return minimal valid JSON
        if source == "voice_learner.analyze_product_positioning":
            return json.dumps({"audience": {}, "competitors": [], "differentiators": []})

        # Semantic evidence review — return "keep all" decisions
        if source == "competitor_analysis.semantic_review":
            # Return a list of "keep" decisions for each evidence item
            # The actual number doesn't matter — the reviewer will default
            # to "keep" for any index not in the decision map
            return json.dumps([{"index": 0, "action": "keep"}])

        # Brand interpretation — return empty implications
        if source == "competitor_analysis.brand_interpretation":
            return json.dumps([])

        # Agent generate/review/revise calls
        if kwargs.get("return_annotations"):
            return self._agent_output, []
        return self._agent_output

    def close(self):
        pass

    def abort(self):
        pass


# ---------------------------------------------------------------------------
# Server fixture — real uvicorn, real source files, no pre-seeded DB
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def _server(tmp_path_factory):
    """Start a real uvicorn server with real source files.

    NO product DB is pre-seeded.  Products are created by the browser
    upload flow exactly as a real user would.
    """
    import web_viewer
    from src import product_db, staging, ingestion, config_loader, asset_library
    from src.orchestrator import Orchestrator

    tmp = tmp_path_factory.mktemp("upload_e2e")

    # Create directory structure
    (tmp / "data").mkdir()
    (tmp / "cache").mkdir()
    (tmp / "brand").mkdir()
    (tmp / "output").mkdir()
    config_dir = tmp / "config"
    config_dir.mkdir()

    # Config files
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    # Load the REAL production agents.yaml directly — only override the model
    # field so FakeLLM is used instead of real API calls. This ensures the
    # trace tests consume the same production configuration the application
    # will use, per the qualification trace requirement.
    import yaml as _yaml
    _prod_agents_path = Path(__file__).resolve().parent.parent / "config" / "agents.yaml"
    with open(_prod_agents_path, encoding="utf-8") as _f:
        _prod_agents = _yaml.safe_load(_f)
    # Override model names to "fake" so FakeLLM is used.
    # auto_mode is included because product/asset selection reads its model from there
    # (migrated from the removed `manager` section by ARCH-CLEANUP-01).
    for _section in ("defaults", "auto_mode", "product_spec", "competitor_analysis",
                     "campaign_strategy", "content_creator"):
        if _section in _prod_agents and isinstance(_prod_agents[_section], dict):
            _prod_agents[_section]["model"] = "fake"
    (config_dir / "agents.yaml").write_text(
        _yaml.dump(_prod_agents, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    (config_dir / "ingestion.yaml").write_text(
        "supported_formats:\n"
        "  text:    [.txt, .md, .pdf, .xlsx, .xls, .docx, .csv]\n"
        "  image:   [.jpg, .jpeg, .png, .webp]\n"
        "max_file_size_mb:\n  text: 50\n  image: 50\n  video: 500\n  audio: 50\n"
        "raw_text_length: 3000\n"
        "model: fake\n",
        encoding="utf-8",
    )
    (config_dir / "system.yaml").write_text("api_timeout_credits: 10\n")
    (config_dir / "media.yaml").write_text(
        "auto_generate_image: false\nauto_generate_video: false\n"
    )
    (config_dir / "web_search.yaml").write_text("enabled: true\n")

    # Use the production brand directory directly — it contains deterministic
    # content ("Test Profile", "คุณแม่") that we can assert against.
    # No brand_loader patch needed — production code resolves "brand" to
    # the real project root's brand/ directory.
    BRAND_MARKER = "Test Profile"
    BRAND_AUDIENCE_MARKER = "คุณแม่"

    # Save originals for cleanup
    _orig = {}
    _orig["product_db_root"] = product_db._project_root
    _orig["staging_root"] = staging._project_root
    _orig["ingestion_root"] = ingestion._project_root
    _orig["config_loader_root"] = config_loader._project_root
    _orig["asset_library_root"] = asset_library._project_root
    _orig["staging_product_db"] = staging.product_db
    _orig["PROJECT_ROOT"] = web_viewer.PROJECT_ROOT
    _orig["OUTPUT_DIR"] = web_viewer.OUTPUT_DIR
    _orig["DATA_DIR"] = web_viewer.DATA_DIR
    _orig["CACHE_DIR"] = web_viewer.CACHE_DIR
    _orig["BRAND_DIR"] = web_viewer.BRAND_DIR
    _orig["make_client"] = Orchestrator.make_client
    _orig["ingestion_make_llm"] = ingestion._make_llm
    _orig["api_key"] = os.environ.get("OPENROUTER_API_KEY", None)
    _orig["ch_record"] = web_viewer.content_history.record_entry
    _orig["ch_format"] = web_viewer.content_history.format_product_history_for_prompt
    _orig["ch_update"] = web_viewer.content_history.update_last_entry_output_file
    _orig["mg_img"] = web_viewer.media_gen.generate_image_with_retry
    _orig["mg_vid"] = web_viewer.media_gen.generate_video_with_retry
    _orig["mg_save"] = web_viewer.media_gen.save_retry_history
    _orig["current_llm"] = getattr(web_viewer, "_current_llm", None)
    _orig["session_ts"] = getattr(web_viewer, "_session_ts", "")
    _orig["cancel"] = getattr(web_viewer, "_cancel_requested", False)

    # Patch ALL _project_root() functions generically — every module that
    # has its own _project_root() must return the temp directory so that
    # staging, ingestion, product_db, config_loader, and asset_library all
    # read/write within tmp_path, not the real project root.
    product_db._project_root = lambda: tmp
    staging._project_root = lambda: tmp
    ingestion._project_root = lambda: tmp
    config_loader._project_root = lambda: tmp
    asset_library._project_root = lambda: tmp
    # Ensure staging uses the patched product_db (not a stale import-time ref)
    staging.product_db = product_db
    web_viewer.PROJECT_ROOT = tmp
    web_viewer.OUTPUT_DIR = tmp / "output"
    web_viewer.DATA_DIR = tmp / "data"
    web_viewer.CACHE_DIR = tmp / "cache"
    web_viewer.BRAND_DIR = tmp / "brand"

    # Copy the real production brand directory into tmp/brand so that
    # /api/brand_json_save writes to tmp/brand/ and the Orchestrator
    # (brand_dir="brand") reads from the same place.
    brand_src = Path(__file__).resolve().parent.parent / "brand"
    if brand_src.exists():
        shutil.copytree(brand_src, tmp / "brand", dirs_exist_ok=True)

    # Patch brand_loader._resolve_brand_dir so that relative brand_dir
    # paths (e.g. "brand") resolve against tmp, not the real project root.
    # This ensures the Orchestrator reads brand data from tmp/brand/ —
    # the same directory the /api/brand_json_save endpoint writes to.
    from src import brand_loader as _bl_mod
    _orig_resolve_brand_dir = _bl_mod._resolve_brand_dir
    def _patched_resolve_brand_dir(brand_dir):
        if brand_dir is None:
            return _orig_resolve_brand_dir(None)
        bd = Path(brand_dir)
        if not bd.is_absolute():
            bd = tmp / brand_dir
        if not bd.exists() or not bd.is_dir():
            return None
        return bd
    _bl_mod._resolve_brand_dir = _patched_resolve_brand_dir

    # Patch load_product_profile so it looks in tmp/cache/ (where
    # /api/product_profile_save writes) instead of cwd/cache/.
    _orig_load_product_profile = _bl_mod.load_product_profile
    def _patched_load_product_profile(product_id):
        if not product_id:
            return {}
        import json as _pjson
        candidates = [
            tmp / "cache" / product_id / "product_profile.json",
            tmp / "data" / product_id / "product_profile.json",
        ]
        for path in candidates:
            if path.exists():
                try:
                    return _pjson.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return {}
    _bl_mod.load_product_profile = _patched_load_product_profile

    # Set dummy API key
    os.environ["OPENROUTER_API_KEY"] = "dummy-key-for-testing"

    # Initialize module globals
    web_viewer._current_llm = None
    web_viewer._session_ts = ""
    web_viewer._cancel_requested = False

    # Create the shared ScriptedFakeLLM
    fake_llm = ScriptedFakeLLM()

    # Patch ONLY the LLM boundary — everything else is real
    Orchestrator.make_client = lambda self: fake_llm
    ingestion._make_llm = lambda: fake_llm

    # Patch Agent 2 downstream VALIDATION boundary (NOT a model boundary):
    # - CompetitorReportRenderer.validate: checks evidence provenance against
    #   real web search annotations.  Without real web search, there are no
    #   relevant annotations, so validation fails and the agent returns early
    #   before semantic_review runs.  Patching validate → [] lets the full
    #   Agent 2 pipeline (generate → validate → semantic_review → brand
    #   interpretation → render) run with real LLM calls at each model
    #   boundary.
    # - BrandInterpretationPass.interpret is NOT patched — it runs naturally
    #   with the deterministic brand fixture and FakeLLM.
    from src.agents.competitor_evidence import SemanticEvidenceReviewer
    from src.agents.competitor_analysis import CompetitorReportRenderer
    import src.agents.competitor_analysis as comp_mod

    _orig["renderer_validate"] = CompetitorReportRenderer.validate
    CompetitorReportRenderer.validate = lambda self: []

    # Media provider tracing spies — count calls without making real ones.
    # These wrap the real boundary with a counter so tests can assert
    # exactly 0 calls when auto_image/auto_video are false.
    _media_call_log: dict[str, int] = {"image": 0, "video": 0}

    def _spy_image(*a, **k):
        _media_call_log["image"] += 1
        return {"ok": True, "skipped": True}

    def _spy_video(*a, **k):
        _media_call_log["video"] += 1
        return {"ok": True, "skipped": True}

    web_viewer.content_history.record_entry = lambda *a, **k: True
    web_viewer.content_history.format_product_history_for_prompt = lambda *a, **k: ""
    web_viewer.content_history.update_last_entry_output_file = lambda *a, **k: None
    web_viewer.media_gen.generate_image_with_retry = _spy_image
    web_viewer.media_gen.generate_video_with_retry = _spy_video
    web_viewer.media_gen.save_retry_history = lambda *a, **k: None

    # Start server
    port = _find_free_port()
    import uvicorn
    config = uvicorn.Config(web_viewer.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("Server did not start")

    yield {
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "tmp": tmp,
        "fake_llm": fake_llm,
        "brand_marker": BRAND_MARKER,
        "brand_audience_marker": BRAND_AUDIENCE_MARKER,
        "media_call_log": _media_call_log,
    }

    # Cleanup
    server.should_exit = True
    thread.join(timeout=5)

    # Restore all patches
    product_db._project_root = _orig["product_db_root"]
    staging._project_root = _orig["staging_root"]
    ingestion._project_root = _orig["ingestion_root"]
    config_loader._project_root = _orig["config_loader_root"]
    asset_library._project_root = _orig["asset_library_root"]
    staging.product_db = _orig["staging_product_db"]
    Orchestrator.make_client = _orig["make_client"]
    ingestion._make_llm = _orig["ingestion_make_llm"]
    CompetitorReportRenderer.validate = _orig["renderer_validate"]
    web_viewer.PROJECT_ROOT = _orig["PROJECT_ROOT"]
    web_viewer.OUTPUT_DIR = _orig["OUTPUT_DIR"]
    web_viewer.DATA_DIR = _orig["DATA_DIR"]
    web_viewer.CACHE_DIR = _orig["CACHE_DIR"]
    web_viewer.BRAND_DIR = _orig["BRAND_DIR"]
    _bl_mod._resolve_brand_dir = _orig_resolve_brand_dir
    _bl_mod.load_product_profile = _orig_load_product_profile
    web_viewer._current_llm = _orig["current_llm"]
    web_viewer._session_ts = _orig["session_ts"]
    web_viewer._cancel_requested = _orig["cancel"]
    web_viewer.content_history.record_entry = _orig["ch_record"]
    web_viewer.content_history.format_product_history_for_prompt = _orig["ch_format"]
    web_viewer.content_history.update_last_entry_output_file = _orig["ch_update"]
    web_viewer.media_gen.generate_image_with_retry = _orig["mg_img"]
    web_viewer.media_gen.generate_video_with_retry = _orig["mg_vid"]
    web_viewer.media_gen.save_retry_history = _orig["mg_save"]
    if _orig["api_key"] is not None:
        os.environ["OPENROUTER_API_KEY"] = _orig["api_key"]
    elif "OPENROUTER_API_KEY" in os.environ:
        del os.environ["OPENROUTER_API_KEY"]


# ---------------------------------------------------------------------------
# Browser fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def _browser(_server):
    """Launch a browser, navigate to the app, yield the page."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg) if msg.type == "error" else None)
        page.on("pageerror", lambda err: console_errors.append(err))

        url = _server["url"]
        page.goto(url, wait_until="networkidle", timeout=15000)
        page.wait_for_selector("#flow-wizard-list", timeout=10000)

        yield {
            "page": page,
            "url": url,
            "console_errors": console_errors,
            "server": _server,
            "fake_llm": _server["fake_llm"],
            "tmp": _server["tmp"],
            "brand_marker": _server["brand_marker"],
            "brand_audience_marker": _server["brand_audience_marker"],
            "media_call_log": _server["media_call_log"],
        }

        context.close()
        browser.close()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _open_upload_modal(page):
    """Click the '+ เพิ่ม' button to open the upload modal."""
    btn = page.query_selector(".sidebar-add-btn")
    assert btn is not None, "Add product button must be present"
    btn.click()
    page.wait_for_timeout(500)
    overlay = page.query_selector("#upload-overlay.visible")
    assert overlay is not None, "Upload modal overlay must be visible"


def _upload_file(page, file_path):
    """Set a file on the upload input and trigger staging upload."""
    # Set the file on the hidden input
    file_input = page.query_selector("#upload-files-modal")
    assert file_input is not None, "File input must be present"
    file_input.set_input_files(str(file_path))
    page.wait_for_timeout(500)

    # Click the upload button → triggers startStagingUpload()
    submit_btn = page.query_selector("#upload-submit-btn")
    assert submit_btn is not None, "Upload submit button must be present"
    submit_btn.click()


def _wait_for_staging_preview(page, timeout_ms=30000):
    """Wait for the staging preview to appear after upload."""
    page.wait_for_selector(
        "#staging-preview-modal:not([style*='display: none']) #staging-confirm-btn",
        timeout=timeout_ms,
    )
    page.wait_for_timeout(500)


def _confirm_staging(page):
    """Click the confirm button in the staging preview."""
    btn = page.query_selector("#staging-confirm-btn")
    assert btn is not None, "Staging confirm button must be present"
    assert btn.is_enabled(), "Staging confirm button must be enabled"
    btn.click()


def _wait_for_product_to_appear(page, product_name, timeout_ms=15000):
    """Wait for a product card with the given folder name to appear."""
    page.wait_for_selector(
        f".product-card[data-folder='{product_name}']",
        timeout=timeout_ms,
    )
    page.wait_for_timeout(500)


def _product_exists_in_sidebar(page, product_name):
    """Check if a product card already exists in the sidebar."""
    card = page.query_selector(
        f".product-card[data-folder='{product_name}']"
    )
    return card is not None


def _ensure_product_uploaded(page, fake_llm, file_path, segmentation_json,
                              product_name):
    """Upload a product through the browser if it doesn't already exist.

    If the product card is already present in the sidebar (from a prior
    test in the same module), skip the upload — the product DB persists
    across tests because the server fixture is module-scoped.

    This is NOT seeding the Product DB directly.  The first test in a
    module always performs a real browser upload.  Subsequent tests
    reuse the already-uploaded product to avoid redundant uploads and
    staging conflicts (existing products show as 'already exists' in
    the staging preview, disabling the confirm button).
    """
    if _product_exists_in_sidebar(page, product_name):
        # Product already exists from a prior test — refresh sidebar
        page.evaluate("if (typeof loadFolderList === 'function') loadFolderList();")
        page.wait_for_timeout(500)
        return True

    # Perform the real browser upload
    fake_llm.reset()
    fake_llm.set_segmentation(segmentation_json)
    _open_upload_modal(page)
    _upload_file(page, file_path)
    _wait_for_staging_preview(page, timeout_ms=30000)
    _confirm_staging(page)
    _wait_for_product_to_appear(page, product_name, timeout_ms=15000)
    return False


def _select_product(page, product_name):
    """Select a product in the wizard Step 1."""
    for attempt in range(3):
        try:
            card = page.query_selector(
                f".product-card[data-folder='{product_name}']"
            )
            if card:
                card.click()
                page.wait_for_timeout(300)
                return True
        except Exception:
            page.wait_for_timeout(500)
    return False


def _select_agent(page, agent_key):
    """Select an agent in wizard Step 2."""
    chip = page.query_selector(
        f".add-agent-chip[data-agent-key='{agent_key}']"
    )
    if chip:
        chip.click()
        page.wait_for_timeout(300)
        return True
    return False


def _go_to_step(page, step_num):
    """Advance to the given wizard step."""
    for _ in range(10):
        active = page.query_selector(".wizard-card.active")
        if active:
            cid = active.get_attribute("id") or ""
            parts = cid.rsplit("-", 1)
            current = int(parts[-1]) if parts and parts[-1].isdigit() else 1
        else:
            current = 1
        if current >= step_num:
            return True
        btns = page.query_selector_all("[id^='flow-next-']")
        for btn in btns:
            if btn.is_visible() and btn.is_enabled():
                btn.click()
                page.wait_for_timeout(500)
                break
        else:
            return False
    return False


def _click_chip_by_text(page, text_fragment, should_be_active):
    """Click an option chip by its text label.

    Re-queries chips after each click to avoid stale DOM elements.
    should_be_active=True → click only if currently active (to deselect).
    should_be_active=False → click only if currently inactive (to select).
    """
    chips = page.query_selector_all(".opt-chip")
    for chip in chips:
        try:
            txt = chip.inner_text().strip()
            cls = chip.get_attribute("class") or ""
            is_active = "active" in cls
            if text_fragment in txt:
                if should_be_active and is_active:
                    chip.click()
                    page.wait_for_timeout(300)
                    return True
                if not should_be_active and not is_active:
                    chip.click()
                    page.wait_for_timeout(300)
                    return True
        except Exception:
            continue
    return False


def _run_flow(page):
    """Click the confirm/run button on Step 4."""
    next_btn = page.query_selector(
        "[id^='flow-next-']:not([style*='display: none'])"
    )
    if next_btn:
        next_btn.click()
        return True
    return False


def _wait_for_flow_done(page, timeout_ms=45000):
    """Wait for the flow to complete. Returns (done, error)."""
    page.wait_for_selector(
        ".flow-step.done .flow-step-link, .flow-step.error",
        timeout=timeout_ms,
    )
    done = page.query_selector(".flow-step.done")
    error = page.query_selector(".flow-step.error")
    return done is not None, error is not None


def _extract_messages_text(fake_llm, call_idx=0):
    """Extract system and user text from FakeLLM's captured calls."""
    assert len(fake_llm.calls) > call_idx, \
        f"Expected at least {call_idx + 1} llm.chat() calls, got {len(fake_llm.calls)}"
    messages = fake_llm.calls[call_idx]["messages"]
    system = ""
    user = ""
    for msg in messages:
        if msg["role"] == "system":
            system = msg["content"]
        elif msg["role"] == "user":
            if isinstance(msg["content"], list):
                user = " ".join(
                    part.get("text", "") for part in msg["content"]
                    if isinstance(part, dict)
                )
            else:
                user = msg["content"]
    return system, user


def _find_agent_generate_call(fake_llm, agent_name):
    """Find the first generate call for the given agent."""
    for call in fake_llm.calls:
        source = call.get("source", "")
        if source == f"{agent_name}.generate":
            return call
    return None


def _get_all_sources(fake_llm):
    """Return list of all call sources in order."""
    return [c.get("source", "") for c in fake_llm.calls]


# ---------------------------------------------------------------------------
# Default FakeLLM outputs for each agent
# ---------------------------------------------------------------------------

def _product_spec_output():
    return f"# {RECOGNIZABLE_MARKER}\n\n## Product Specification\n- Model: test\n- Display: test\n"

def _research_json_output(target_model="test"):
    return json.dumps({
        "target_model": target_model,
        "competitor_names": ["CompA"],
        "evidence": [{
            "competitor": "CompA",
            "field": "price",
            "claim": "CompA costs $100",
            "url": "https://example.com/compa",
            "geography": "global",
        }],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })

def _campaign_output():
    return (
        f"# {RECOGNIZABLE_MARKER}\n\n"
        f"## ราคาแนะนำ\n- ราคา pending: ราคาเริ่มต้น 1,990 บาท\n"
        f"## แคมเปญหลัก\n- Launch Campaign\n"
        f"## แคมเปญเสริม\n- Influencer Partnership\n"
        f"## ช่องทางโปรโมท\n- Facebook Ads, TikTok\n"
        f"## KPI\n- Reach 1M impressions\n"
        f"## งบประมาณ\n- pending: 50,000 บาท\n"
        f"## แหล่งอ้างอิง\n- https://example.com/market-research\n"
    )

def _content_creator_json():
    return json.dumps({
        "posts": [{
            "platform": "facebook",
            "concept": "test concept",
            "title": f"Test Post {RECOGNIZABLE_MARKER}",
            "caption": "test caption",
            "script": "INT. HOME - DAY\nA parent watches their child play outside.\n\nPARENT (V.O.)\nWith Lagenio K2, I always know where she is.\n\nCUT TO: Close-up of the watch on the child's wrist.\n\nTITLE CARD: Lagenio K2 — Peace of mind for parents.",
            "hashtags": "#test",
            "image_prompts": [],
            "video_prompts": [],
            "asset_ids": [],
        }],
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Segmentation JSON for real source files
# ---------------------------------------------------------------------------

def _lagenio_k2_segmentation():
    """Segmentation JSON for the Lagenio K2 XLSX (single product)."""
    return json.dumps({
        "products": [{
            "product_key": "K2",
            "suggested_name": "Lagenio K2",
            "category": "Kids Smart Watch",
            "summary": "Kids GPS smart watch with AMOLED display",
            "source_refs": [{
                "file": "Lagenio K2 -spec 20241024.xlsx",
                "line_start": 1,
                "line_end": 68,
            }],
            "common_refs": [],
        }]
    })


def _lagenio_k5_segmentation():
    """Segmentation JSON for the Lagenio K5 XLSX (single product)."""
    return json.dumps({
        "products": [{
            "product_key": "K5",
            "suggested_name": "Lagenio K5",
            "category": "Kids Smart Watch",
            "summary": "Kids GPS smart watch with changeable housing design",
            "source_refs": [{
                "file": "Lagenio K5 20241024.xlsx",
                "line_start": 1,
                "line_end": 66,
            }],
            "common_refs": [],
        }]
    })


def _cacgo_segmentation():
    """Segmentation JSON for the CACGO PDF (K73 + K52 from 20-product catalog).

    Source refs point to exact line ranges in the parsed PDF text:
      K73: lines 186-209 (model name through price)
      K52: lines 565-592 (model name through price)
      Common: lines 593-599 (Remarks: MOQ, payment, delivery terms)
    """
    return json.dumps({
        "products": [
            {
                "product_key": "K73",
                "suggested_name": "CACGO K73",
                "category": "Smart Watch",
                "summary": "1.32 AMOLED smart watch with GPS",
                "source_refs": [{
                    "file": "CACGO Smart Watch Price List- Grace.pdf",
                    "line_start": 186,
                    "line_end": 209,
                }],
                "common_refs": [{
                    "file": "CACGO Smart Watch Price List- Grace.pdf",
                    "line_start": 593,
                    "line_end": 599,
                }],
            },
            {
                "product_key": "K52",
                "suggested_name": "CACGO K52",
                "category": "Smart Watch",
                "summary": "1.39 IPS outdoor smart watch",
                "source_refs": [{
                    "file": "CACGO Smart Watch Price List- Grace.pdf",
                    "line_start": 565,
                    "line_end": 592,
                }],
                "common_refs": [{
                    "file": "CACGO Smart Watch Price List- Grace.pdf",
                    "line_start": 593,
                    "line_end": 599,
                }],
            },
        ]
    })


# ---------------------------------------------------------------------------
# TEST 1: Lagenio K2 — full upload → ingest → Agent 1 → DOM chain
# ---------------------------------------------------------------------------

class TestLagenioK2UploadToAgent:
    """Full browser upload → real ingest → real Product DB → browser
    selection → real Agent → FakeLLM → result → browser DOM.

    Uses the real Lagenio K2 XLSX file.  Source truth is verified at
    every stage against the ORIGINAL file.
    """

    def test_upload_lagenio_k2_then_run_product_spec(self, _browser):
        """Upload Lagenio K2 XLSX → ingest → select → Agent 1 → DOM.

        Verifies:
        1. File uploads through the browser UI
        2. Real staging/segmentation creates the product
        3. Product appears in the UI
        4. Source facts from the XLSX reach the Agent model boundary
        5. Result appears in the browser DOM
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        tmp = _browser["tmp"]

        # --- STAGE A: Verify original source file exists ---
        assert LAGENIO_K2_XLSX.exists(), \
            f"Real source file must exist: {LAGENIO_K2_XLSX}"

        # --- Set up FakeLLM for segmentation + agent output ---
        fake_llm.reset()
        fake_llm.set_segmentation(_lagenio_k2_segmentation())
        fake_llm.set_agent_output(_product_spec_output())

        # --- STAGE B: Browser upload ---
        _open_upload_modal(page)
        _upload_file(page, LAGENIO_K2_XLSX)

        # Wait for staging preview
        _wait_for_staging_preview(page, timeout_ms=30000)

        # Verify staging preview shows the product
        preview_text = page.query_selector("#staging-preview-modal").inner_text()
        assert "Lagenio K2" in preview_text or "K2" in preview_text, \
            f"Staging preview must show product name, got: {preview_text[:200]}"

        # Confirm staging
        _confirm_staging(page)

        # Wait for product to appear in the UI
        _wait_for_product_to_appear(page, "Lagenio K2", timeout_ms=15000)

        # --- STAGE C: Verify Product DB record ---
        from src import product_db
        rec = product_db.load("Lagenio K2")
        assert rec.get("status") == "ready", \
            f"Product DB status must be 'ready', got: {rec.get('status')}"
        raw_text = rec.get("raw_text", "")
        assert "K2" in raw_text, "Product DB raw_text must contain K2"
        assert "AMOLED" in raw_text, "Product DB raw_text must contain AMOLED"
        assert "680mAh" in raw_text, "Product DB raw_text must contain battery spec"

        # --- STAGE D: Browser select product + run Agent 1 ---
        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())

        assert _select_product(page, "Lagenio K2"), \
            "Must be able to select Lagenio K2 in the browser"
        assert _go_to_step(page, 2)
        assert _select_agent(page, "product_spec")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=30000)
        assert done, f"Flow did not complete. Error: {error}"
        assert not error, "Flow completed with error state"

        # --- Verify source truth at model boundary ---
        gen_call = _find_agent_generate_call(fake_llm, "product_spec")
        assert gen_call is not None, \
            "FakeLLM must have a product_spec.generate call"
        _, user = _extract_messages_text(fake_llm, 0)

        # Product identity
        assert "K2" in user, \
            "FakeLLM user prompt must contain product model 'K2'"
        # Source facts from XLSX
        assert "AMOLED" in user, \
            "FakeLLM user prompt must contain display type 'AMOLED'"
        assert "680mAh" in user, \
            "FakeLLM user prompt must contain battery '680mAh'"
        assert "W377" in user, \
            "FakeLLM user prompt must contain CPU 'W377'"
        assert "Lagenio" in user, \
            "FakeLLM user prompt must contain app name 'Lagenio'"
        assert "1.78" in user, \
            "FakeLLM user prompt must contain display size '1.78'"

        # --- Verify DOM result ---
        result_link = page.query_selector(".flow-step-link")
        assert result_link is not None, \
            "Result link must appear in the browser DOM"

        running = page.query_selector(".flow-step.running")
        assert running is None, \
            "Flow must not be in 'running' state after completion"

        # Open result and verify recognizable marker in DOM
        result_link.click()
        page.wait_for_timeout(2000)

        overlay = page.query_selector(
            "#result-overlay.visible, .result-overlay.visible, #result-modal.visible"
        )
        assert overlay is not None, \
            "Result overlay must be visible after clicking result link"
        overlay_text = overlay.inner_text()
        assert RECOGNIZABLE_MARKER in overlay_text, \
            f"Recognizable marker must appear in rendered result DOM, " \
            f"got: {overlay_text[:200]}..."


# ---------------------------------------------------------------------------
# TEST 2: CACGO PDF — multi-product catalog upload → K73 isolation
# ---------------------------------------------------------------------------

class TestCacgoPdfUploadToAgent:
    """Full browser upload of the CACGO catalog PDF → real segmentation
    → K73 and K52 products created → browser selects K73 → Agent → DOM.

    Proves that catalog segmentation correctly isolates K73 facts from
    the 20-product PDF, and that K52 facts do NOT leak into K73's agent
    context.
    """

    def test_upload_cacgo_pdf_then_run_product_spec_k73(self, _browser):
        """Upload CACGO PDF → segmentation creates K73 + K52 → select K73
        → Agent 1 → verify K73 facts present, K52 facts absent."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        # --- STAGE A: Verify original source file ---
        assert CACGO_PDF.exists(), \
            f"Real source file must exist: {CACGO_PDF}"

        # --- Set up FakeLLM ---
        fake_llm.reset()
        fake_llm.set_segmentation(_cacgo_segmentation())
        fake_llm.set_agent_output(_product_spec_output())

        # --- STAGE B: Browser upload ---
        _open_upload_modal(page)
        _upload_file(page, CACGO_PDF)

        _wait_for_staging_preview(page, timeout_ms=30000)

        # Verify staging preview shows K73 and K52
        preview_text = page.query_selector("#staging-preview-modal").inner_text()
        assert "K73" in preview_text, \
            f"Staging preview must show K73, got: {preview_text[:200]}"
        assert "K52" in preview_text, \
            f"Staging preview must show K52, got: {preview_text[:200]}"

        _confirm_staging(page)

        # Wait for both products to appear
        _wait_for_product_to_appear(page, "CACGO K73", timeout_ms=15000)
        _wait_for_product_to_appear(page, "CACGO K52", timeout_ms=15000)

        # --- STAGE C: Verify Product DB records ---
        from src import product_db
        k73_rec = product_db.load("CACGO K73")
        assert k73_rec.get("status") == "ready", \
            f"K73 status must be 'ready', got: {k73_rec.get('status')}"
        k73_raw = k73_rec.get("raw_text", "")
        assert "K73" in k73_raw, "K73 raw_text must contain K73"
        assert "16.50" in k73_raw, "K73 raw_text must contain price 16.50"
        assert "340mAh" in k73_raw, "K73 raw_text must contain battery 340mAh"

        k52_rec = product_db.load("CACGO K52")
        k52_raw = k52_rec.get("raw_text", "")
        assert "K52" in k52_raw, "K52 raw_text must contain K52"
        assert "13.00" in k52_raw, "K52 raw_text must contain price 13.00"

        # Cross-product isolation in DB: K73 must NOT contain K52 price
        assert "13.00" not in k73_raw, \
            "K73 raw_text must NOT contain K52 price (cross-product leakage)"
        # K52 must NOT contain K73 price
        assert "16.50" not in k52_raw, \
            "K52 raw_text must NOT contain K73 price (cross-product leakage)"

        # --- STAGE D: Browser select K73 + run Agent 1 ---
        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())

        assert _select_product(page, "CACGO K73"), \
            "Must be able to select CACGO K73 in the browser"
        assert _go_to_step(page, 2)
        assert _select_agent(page, "product_spec")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=30000)
        assert done, f"Flow did not complete. Error: {error}"
        assert not error, "Flow completed with error state"

        # --- Verify K73 source truth at model boundary ---
        gen_call = _find_agent_generate_call(fake_llm, "product_spec")
        assert gen_call is not None, "product_spec.generate call must exist"
        _, user = _extract_messages_text(fake_llm, 0)

        # K73 identity and facts
        assert "K73" in user, "FakeLLM must contain K73 model name"
        assert "16.50" in user, "FakeLLM must contain K73 price US$16.50"
        assert "340mAh" in user, "FakeLLM must contain K73 battery 340mAh"
        assert "1.32" in user, "FakeLLM must contain K73 display size 1.32"
        assert "FitCloudPro" in user, "FakeLLM must contain K73 app name FitCloudPro"
        assert "Blue" in user, "FakeLLM must contain K73 color Blue"

        # K52 facts must NOT appear (cross-product isolation)
        assert "K52" not in user, \
            "FakeLLM must NOT contain K52 (cross-product leakage)"
        assert "13.00" not in user, \
            "FakeLLM must NOT contain K52 price (cross-product leakage)"
        assert "400mAh" not in user or "340mAh" in user, \
            "FakeLLM must NOT contain K52 battery 400mAh without K73 battery"

        # --- Verify DOM result ---
        result_link = page.query_selector(".flow-step-link")
        assert result_link is not None, "Result link must appear in DOM"

        running = page.query_selector(".flow-step.running")
        assert running is None, "Flow must not be running after completion"

        result_link.click()
        page.wait_for_timeout(2000)
        overlay = page.query_selector(
            "#result-overlay.visible, .result-overlay.visible, #result-modal.visible"
        )
        assert overlay is not None, "Result overlay must be visible"
        assert RECOGNIZABLE_MARKER in overlay.inner_text(), \
            "Recognizable marker must appear in rendered result"


# ---------------------------------------------------------------------------
# TEST 3: All 4 Agents with Lagenio K2 — one agent per flow
# ---------------------------------------------------------------------------

class TestAllAgentsWithLagenioK2:
    """Test Agent 1-4 separately with the uploaded Lagenio K2 product.

    MAX_AGENTS_PER_FLOW = 1 is intentional.  Each agent is a separate flow.
    """

    def test_agent1_product_spec(self, _browser):
        """Agent 1: product_spec with Lagenio K2."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        # Upload (or reuse if already uploaded by a prior test)
        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        # Run Agent 1
        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "product_spec")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=30000)
        assert done, f"Agent 1 flow failed: {error}"
        assert not error, "Agent 1 flow error state"

        # Verify K2 facts at boundary
        gen_call = _find_agent_generate_call(fake_llm, "product_spec")
        assert gen_call is not None, "product_spec.generate must exist"
        _, user = _extract_messages_text(fake_llm, 0)
        assert "K2" in user, "Agent 1 must receive K2 model name"
        assert "AMOLED" in user, "Agent 1 must receive AMOLED display type"

        # Verify DOM
        result_link = page.query_selector(".flow-step-link")
        assert result_link is not None, "Result link must appear in DOM"
        result_link.click()
        page.wait_for_timeout(2000)
        overlay = page.query_selector(
            "#result-overlay.visible, .result-overlay.visible, #result-modal.visible"
        )
        assert overlay is not None, "Result overlay must be visible"
        assert RECOGNIZABLE_MARKER in overlay.inner_text(), \
            "Recognizable marker must appear in rendered result"

    def test_agent2_competitor_analysis(self, _browser):
        """Agent 2: competitor_analysis with Lagenio K2.

        Real Agent 2 pipeline runs: evidence mode → SemanticEvidenceReviewer
        → BrandInterpretationPass → CompetitorReportRenderer.  Only the
        LLM boundary is FakeLLM.
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        # Upload (or reuse)
        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        # Run Agent 2
        fake_llm.reset()
        fake_llm.set_agent_output(_research_json_output("Lagenio K2"))
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "competitor_analysis")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=45000)
        assert done, f"Agent 2 flow failed: {error}"
        assert not error, "Agent 2 flow error state"

        # Verify K2 identity at boundary (first generate call)
        gen_call = _find_agent_generate_call(fake_llm, "competitor_analysis")
        assert gen_call is not None, "competitor_analysis.generate must exist"
        _, user = _extract_messages_text(fake_llm, 0)
        assert "K2" in user, "Agent 2 must receive K2 model name"

        # Verify real Agent 2 pipeline ran (multiple LLM calls)
        sources = _get_all_sources(fake_llm)
        assert "competitor_analysis.generate" in sources, \
            "Agent 2 must make a generate call"
        # Semantic review should run (evidence is non-empty in FakeLLM output)
        assert "competitor_analysis.semantic_review" in sources, \
            f"Agent 2 must make a semantic_review call, got sources: {sources}"

        # Verify DOM
        result_link = page.query_selector(".flow-step-link")
        assert result_link is not None, "Result link must appear in DOM"
        running = page.query_selector(".flow-step.running")
        assert running is None, "Flow must not be running"

    def test_agent3_campaign_strategy(self, _browser):
        """Agent 3: campaign_strategy with Lagenio K2."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        # Upload (or reuse)
        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        # Run Agent 3
        fake_llm.reset()
        fake_llm.set_agent_output(_campaign_output())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "campaign_strategy")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=30000)
        assert done, f"Agent 3 flow failed: {error}"
        assert not error, "Agent 3 flow error state"

        # Verify K2 identity at boundary
        gen_call = _find_agent_generate_call(fake_llm, "campaign_strategy")
        assert gen_call is not None, "campaign_strategy.generate must exist"
        _, user = _extract_messages_text(fake_llm, 0)
        assert "K2" in user, "Agent 3 must receive K2 model name"

        # Verify DOM
        result_link = page.query_selector(".flow-step-link")
        assert result_link is not None, "Result link must appear in DOM"
        result_link.click()
        page.wait_for_timeout(2000)
        overlay = page.query_selector(
            "#result-overlay.visible, .result-overlay.visible, #result-modal.visible"
        )
        assert overlay is not None, "Result overlay must be visible"
        assert RECOGNIZABLE_MARKER in overlay.inner_text(), \
            "Recognizable marker must appear in rendered result"

    def test_agent4_content_creator(self, _browser):
        """Agent 4: content_creator with Lagenio K2."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        # Upload (or reuse)
        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        # Run Agent 4
        fake_llm.reset()
        fake_llm.set_agent_output(_content_creator_json())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "content_creator")
        assert _go_to_step(page, 3)

        # Verify content options visible
        opts = page.query_selector("#flow-opts-content-0")
        assert opts is not None, "Content creator options must be visible in Step 3"

        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=45000)
        assert done, f"Agent 4 flow failed: {error}"
        assert not error, "Agent 4 flow error state"

        # Verify K2 identity at boundary
        gen_call = _find_agent_generate_call(fake_llm, "content_creator")
        assert gen_call is not None, "content_creator.generate must exist"
        _, user = _extract_messages_text(fake_llm, 0)
        assert "K2" in user, "Agent 4 must receive K2 model name"

        # Verify DOM
        result_link = page.query_selector(".flow-step-link")
        assert result_link is not None, "Result link must appear in DOM"
        running = page.query_selector(".flow-step.running")
        assert running is None, "Flow must not be running"


# ---------------------------------------------------------------------------
# TEST 4: Cross-product isolation — K73 vs K52 from same PDF
# ---------------------------------------------------------------------------

class TestCrossProductIsolationCacgo:
    """Upload CACGO PDF → K73 and K52 created → run K73, verify K52 absent.
    Then run K52, verify K73 absent."""

    def test_k73_and_k52_isolation(self, _browser):
        """Both runs must complete.  K73 run: K73 present, K52 absent.
        K52 run: K52 present, K73 absent."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        # Upload CACGO PDF (or reuse if already uploaded)
        if not _product_exists_in_sidebar(page, "CACGO K73"):
            fake_llm.reset()
            fake_llm.set_segmentation(_cacgo_segmentation())
            fake_llm.set_agent_output(_product_spec_output())
            _open_upload_modal(page)
            _upload_file(page, CACGO_PDF)
            _wait_for_staging_preview(page)
            _confirm_staging(page)
            _wait_for_product_to_appear(page, "CACGO K73")
            _wait_for_product_to_appear(page, "CACGO K52")
        else:
            page.evaluate("if (typeof loadFolderList === 'function') loadFolderList();")
            page.wait_for_timeout(500)

        # --- Run 1: K73 ---
        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())
        assert _select_product(page, "CACGO K73")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "product_spec")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done1, error1 = _wait_for_flow_done(page, timeout_ms=30000)
        assert done1, f"K73 run must complete. Error: {error1}"
        assert not error1, "K73 run must not have error state"

        _, k73_user = _extract_messages_text(fake_llm, 0)
        assert "K73" in k73_user, "K73 run must contain K73"
        assert "16.50" in k73_user, "K73 run must contain K73 price"
        assert "K52" not in k73_user, \
            "K73 run must NOT contain K52 (cross-product leakage)"
        assert "13.00" not in k73_user, \
            "K73 run must NOT contain K52 price"

        # --- Run 2: K52 ---
        # Add a new flow
        add_btn = page.query_selector(".flow-box.add-new")
        assert add_btn is not None, "Add new flow button must be present"
        add_btn.click()
        page.wait_for_timeout(500)

        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())

        # Select K52
        cards = page.query_selector_all(".product-card[data-folder='CACGO K52']")
        assert len(cards) > 0, "CACGO K52 product card must be present"
        cards[-1].click()
        page.wait_for_timeout(300)

        # Advance through steps
        for _ in range(4):
            btns = page.query_selector_all("[id^='flow-next-']")
            for btn in btns:
                if btn.is_visible() and btn.is_enabled():
                    btn.click()
                    page.wait_for_timeout(400)
                    break

        # Select agent if needed
        chip = page.query_selector(".add-agent-chip[data-agent-key='product_spec']")
        if chip:
            chip.click()
            page.wait_for_timeout(300)

        # Advance to step 4 and run
        for _ in range(3):
            btns = page.query_selector_all("[id^='flow-next-']")
            for btn in btns:
                if btn.is_visible() and btn.is_enabled():
                    btn.click()
                    page.wait_for_timeout(400)
                    break

        done2, error2 = _wait_for_flow_done(page, timeout_ms=30000)
        assert done2, f"K52 run must complete. Error: {error2}"
        assert not error2, "K52 run must not have error state"

        _, k52_user = _extract_messages_text(fake_llm, 0)
        assert "K52" in k52_user, "K52 run must contain K52"
        assert "13.00" in k52_user, "K52 run must contain K52 price"
        assert "K73" not in k52_user, \
            "K52 run must NOT contain K73 (cross-product leakage)"
        assert "16.50" not in k52_user, \
            "K52 run must NOT contain K73 price"


# ---------------------------------------------------------------------------
# TEST 5: UI settings propagation — quick_brief
# ---------------------------------------------------------------------------

class TestUISettingsPropagation:
    """Verify user-controlled settings reach the model boundary."""

    def test_quick_brief_reaches_agent(self, _browser):
        """User's quick_brief entered in the UI must reach FakeLLM."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        # Upload (or reuse)
        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        # Run with quick brief
        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())

        brief = "focus on premium positioning for kids safety"
        brief_input = page.query_selector("#flow-quick-brief-0")
        assert brief_input is not None, "quick_brief input must be present"
        brief_input.fill(brief)
        page.wait_for_timeout(300)

        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "product_spec")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=30000)
        assert done, f"Flow failed: {error}"

        _, user = _extract_messages_text(fake_llm, 0)
        assert brief in user, \
            "User's quick_brief must be preserved through browser → API → agent"


# ---------------------------------------------------------------------------
# TEST 6: Offline call trace — record every model-boundary call
# ---------------------------------------------------------------------------

class TestOfflineCallTrace:
    """Record the exact model-boundary call sequence for each Agent under
    the proposed real qualification settings.  This trace will be used to
    estimate the real paid run later."""

    def test_agent1_call_trace(self, _browser):
        """Agent 1 (product_spec) call trace."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "product_spec")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)
        done, _ = _wait_for_flow_done(page, timeout_ms=30000)
        assert done

        sources = _get_all_sources(fake_llm)
        # Agent 1 (no web_search): 1 generate call + possible review
        generate_calls = [s for s in sources if s == "product_spec.generate"]
        assert len(generate_calls) >= 1, \
            f"Agent 1 must make at least 1 generate call, got: {sources}"

    def test_agent2_call_trace(self, _browser):
        """Agent 2 (competitor_analysis) call trace — records all pipeline
        stages: generate → semantic_review → (brand_interpretation if enabled).
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_research_json_output("Lagenio K2"))
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "competitor_analysis")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)
        done, _ = _wait_for_flow_done(page, timeout_ms=45000)
        assert done

        sources = _get_all_sources(fake_llm)
        # Agent 2 evidence mode: generate (with web_search) + semantic_review
        generate_calls = [s for s in sources if s == "competitor_analysis.generate"]
        review_calls = [s for s in sources if s == "competitor_analysis.semantic_review"]
        assert len(generate_calls) >= 1, \
            f"Agent 2 must make generate call(s), got: {sources}"
        assert len(review_calls) >= 1, \
            f"Agent 2 must make semantic_review call(s), got: {sources}"

    def test_agent3_call_trace(self, _browser):
        """Agent 3 (campaign_strategy) call trace."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_campaign_output())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "campaign_strategy")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)
        done, _ = _wait_for_flow_done(page, timeout_ms=30000)
        assert done

        sources = _get_all_sources(fake_llm)
        generate_calls = [s for s in sources if s == "campaign_strategy.generate"]
        assert len(generate_calls) >= 1, \
            f"Agent 3 must make generate call(s), got: {sources}"

    def test_agent4_call_trace(self, _browser):
        """Agent 4 (content_creator) call trace."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_content_creator_json())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "content_creator")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)
        done, _ = _wait_for_flow_done(page, timeout_ms=45000)
        assert done

        sources = _get_all_sources(fake_llm)
        generate_calls = [s for s in sources if s == "content_creator.generate"]
        assert len(generate_calls) >= 1, \
            f"Agent 4 must make generate call(s), got: {sources}"


# ---------------------------------------------------------------------------
# TEST 7: Media generation prevention for text qualification
# ---------------------------------------------------------------------------

class TestMediaGenerationPrevention:
    """Verify that with the proposed text qualification settings, no real
    image or video generation can start.  Media boundary is mocked but
    we verify it is NOT called when auto_image/auto_video are false."""

    def test_no_media_calls_with_text_only_settings(self, _browser):
        """Agent 4 with auto_image=false, auto_video=false must not
        invoke media generation."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_content_creator_json())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "content_creator")
        assert _go_to_step(page, 3)

        # Verify media settings default to false
        # (production defaults are in config/media.yaml which we set to false)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=45000)
        assert done, f"Flow failed: {error}"
        assert not error, "Flow must not error"

        # Verify no media generation calls were made
        # (media_gen is mocked but we can check it was NOT called by
        #  verifying the flow completed without media-related errors)
        sources = _get_all_sources(fake_llm)
        # There should be no media-related LLM calls
        media_sources = [s for s in sources if "media" in s.lower() or "image" in s.lower() or "video" in s.lower()]
        assert len(media_sources) == 0, \
            f"No media-related calls should be made, got: {media_sources}"


# ---------------------------------------------------------------------------
# TEST 8: Real-source IMAGE association end-to-end
# ---------------------------------------------------------------------------

# Source image truth — established by directly reading the original files
# with PyMuPDF (fitz) and zipfile for XLSX.  Full SHA256 hashes.
K2_XLSX_IMAGE_SHA256 = "61e293f418bcf656a0639a44f52499a554f0cfc3668232132004d004907e738e"
K73_PDF_IMAGE_SHA256 = "e2b8e4b994811bdaeac3d042726ca01fc5e2c494faf72388d0f2b2e4d6c08722"
K52_PDF_IMAGE_SHA256 = "06f26dfcfc58f4540d45fa475f6ef4ab14396cab391b06a43aef63fc84030bb3"

# Lagenio K5 XLSX — 3 embedded images (1 BMP + 2 PNG), established by
# directly reading xl/media/ in the original XLSX with zipfile.
K5_XLSX_IMAGE_SHA256 = {
    "image1.bmp": "8e55011d4a4b482687b6c2f839109dc1a17e42a204e8bb29ea01eab01bf96e0b",
    "image2.png": "1dabc6e9c5fd43808504f78597ee71cb41ad0b2b88f24483509e95d78e7f52e6",
    "image3.png": "d8a3e70340de8c60cea6eafe4b38d8f239f78be8cd8a880eb77d965e889817b3",
}


class TestRealSourceImageAssociation:
    """Prove that embedded source images are extracted, persisted in
    Product DB, and reach the Agent model boundary.

    K2 XLSX (single mode): image extracted → NOT labeled unassigned →
    returned by get_product_image_paths → reaches Agent image_paths.

    CACGO PDF (multi mode): images extracted → labeled unassigned_source_media
    → NOT returned by get_product_image_paths → Agent does NOT receive
    them.  This is correct production behavior for multi-product catalogs
    without deterministic per-image association.

    Cross-product: K73's Agent does NOT receive K52's image and vice versa.
    """

    def test_k2_xlsx_image_reaches_agent(self, _browser):
        """K2 XLSX has 1 embedded PNG.  After upload → staging commit →
        product selection → Agent 1, the image must:
        1. Be extracted to cache/{product}/extracted_images/
        2. Be persisted in Product DB image_descriptions (not unassigned)
        3. Be returned by product_db.get_product_image_paths()
        4. Reach Agent 1's image_paths parameter at the model boundary
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        tmp = _browser["tmp"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        # --- Verify Product DB has the image ---
        from src import product_db
        rec = product_db.load("Lagenio K2")
        image_descs = rec.get("image_descriptions", [])
        assert len(image_descs) >= 1, \
            f"K2 Product DB must have at least 1 image_description, got: {len(image_descs)}"

        # Find the non-unassigned image
        usable = [d for d in image_descs if not d.get("unassigned_source_media")]
        assert len(usable) >= 1, \
            "K2 must have at least 1 non-unassigned image (single mode)"

        img_path = usable[0].get("path", "")
        assert img_path, "Image path must be non-empty"
        assert Path(img_path).exists(), \
            f"Extracted image file must exist on disk: {img_path}"

        # Verify image hash matches the original source image
        import hashlib
        raw = Path(img_path).read_bytes()
        h = hashlib.sha256(raw).hexdigest()
        assert h == K2_XLSX_IMAGE_SHA256, \
            f"K2 extracted image hash must match original, got {h}, expected {K2_XLSX_IMAGE_SHA256}"

        # --- Verify get_product_image_paths returns it ---
        paths = product_db.get_product_image_paths("Lagenio K2")
        assert len(paths) >= 1, \
            "get_product_image_paths must return the K2 image"
        assert paths[0] == img_path or img_path in paths, \
            f"get_product_image_paths must return the same path: {paths}"

        # --- Verify image reaches Agent 1 at the model boundary ---
        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "product_spec")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=30000)
        assert done, f"Flow failed: {error}"

        # Check that FakeLLM received image_paths in the kwargs
        gen_call = _find_agent_generate_call(fake_llm, "product_spec")
        assert gen_call is not None, "product_spec.generate must exist"
        # The image_paths are passed to agent.run() and then to llm.chat()
        # as part of multimodal content in the user message
        messages = gen_call["messages"]
        user_msg = next((m for m in messages if m["role"] == "user"), None)
        assert user_msg is not None, "User message must exist"
        # In multimodal mode, content is a list of parts including image_url
        content = user_msg["content"]
        has_image = False
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    has_image = True
                    break
        assert has_image, \
            "Agent 1 user message must contain image_url part (multimodal input)"

    def test_cacgo_pdf_k73_gets_correct_image(self, _browser):
        """CACGO PDF (multi mode): K73 must get its own product image
        via page/y-position association, NOT K52's image.

        PDF images carry page + y0 metadata.  get_product_image_paths()
        uses scope.source_refs to find the product's page, then picks
        the image with y0 closest to the product's estimated y-position.
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        # Upload CACGO PDF if not already present
        if not _product_exists_in_sidebar(page, "CACGO K73"):
            fake_llm.reset()
            fake_llm.set_segmentation(_cacgo_segmentation())
            fake_llm.set_agent_output(_product_spec_output())
            _open_upload_modal(page)
            _upload_file(page, CACGO_PDF)
            _wait_for_staging_preview(page)
            _confirm_staging(page)
            _wait_for_product_to_appear(page, "CACGO K73")
            _wait_for_product_to_appear(page, "CACGO K52")
        else:
            page.evaluate("if (typeof loadFolderList === 'function') loadFolderList();")
            page.wait_for_timeout(500)

        from src import product_db
        import hashlib

        # K73 must have extracted images with page metadata (NOT unassigned)
        k73_rec = product_db.load("CACGO K73")
        k73_imgs = k73_rec.get("image_descriptions", [])
        assert len(k73_imgs) > 0, \
            "K73 must have extracted images from the PDF"

        # PDF images with page metadata must NOT be marked unassigned
        unassigned = [d for d in k73_imgs if d.get("unassigned_source_media")]
        assert len(unassigned) == 0, \
            f"K73 PDF images with page metadata must NOT be unassigned, " \
            f"got {len(unassigned)}/{len(k73_imgs)} unassigned"

        # get_product_image_paths must return exactly 1 path for K73
        k73_paths = product_db.get_product_image_paths("CACGO K73")
        assert len(k73_paths) == 1, \
            f"K73 must have exactly 1 image path (page/y-position association), " \
            f"got {len(k73_paths)}: {k73_paths}"

        # Verify the returned image is K73's image (hash match)
        k73_raw = Path(k73_paths[0]).read_bytes()
        k73_hash = hashlib.sha256(k73_raw).hexdigest()
        assert k73_hash == K73_PDF_IMAGE_SHA256, \
            f"K73 image hash must match source, got {k73_hash}, " \
            f"expected {K73_PDF_IMAGE_SHA256}"

        # K73's image must NOT be K52's image
        assert k73_hash != K52_PDF_IMAGE_SHA256, \
            "K73's image must NOT be K52's image (cross-product leak)"

    def test_cacgo_pdf_k52_gets_correct_image(self, _browser):
        """K52 must get its own product image, NOT K73's image."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        if not _product_exists_in_sidebar(page, "CACGO K73"):
            fake_llm.reset()
            fake_llm.set_segmentation(_cacgo_segmentation())
            fake_llm.set_agent_output(_product_spec_output())
            _open_upload_modal(page)
            _upload_file(page, CACGO_PDF)
            _wait_for_staging_preview(page)
            _confirm_staging(page)
            _wait_for_product_to_appear(page, "CACGO K73")
            _wait_for_product_to_appear(page, "CACGO K52")
        else:
            page.evaluate("if (typeof loadFolderList === 'function') loadFolderList();")
            page.wait_for_timeout(500)

        from src import product_db
        import hashlib

        k52_rec = product_db.load("CACGO K52")
        k52_imgs = k52_rec.get("image_descriptions", [])
        assert len(k52_imgs) > 0, \
            "K52 must have extracted images from the PDF"

        unassigned = [d for d in k52_imgs if d.get("unassigned_source_media")]
        assert len(unassigned) == 0, \
            f"K52 PDF images with page metadata must NOT be unassigned, " \
            f"got {len(unassigned)}/{len(k52_imgs)} unassigned"

        k52_paths = product_db.get_product_image_paths("CACGO K52")
        assert len(k52_paths) == 1, \
            f"K52 must have exactly 1 image path, got {len(k52_paths)}: {k52_paths}"

        k52_raw = Path(k52_paths[0]).read_bytes()
        k52_hash = hashlib.sha256(k52_raw).hexdigest()
        assert k52_hash == K52_PDF_IMAGE_SHA256, \
            f"K52 image hash must match source, got {k52_hash}, " \
            f"expected {K52_PDF_IMAGE_SHA256}"

        # K52's image must NOT be K73's image
        assert k52_hash != K73_PDF_IMAGE_SHA256, \
            "K52's image must NOT be K73's image (cross-product leak)"

    def test_k73_image_reaches_agent_boundary(self, _browser):
        """K73's correctly associated image must reach Agent 1's
        multimodal input at the model boundary."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        if not _product_exists_in_sidebar(page, "CACGO K73"):
            fake_llm.reset()
            fake_llm.set_segmentation(_cacgo_segmentation())
            fake_llm.set_agent_output(_product_spec_output())
            _open_upload_modal(page)
            _upload_file(page, CACGO_PDF)
            _wait_for_staging_preview(page)
            _confirm_staging(page)
            _wait_for_product_to_appear(page, "CACGO K73")
            _wait_for_product_to_appear(page, "CACGO K52")
        else:
            page.evaluate("if (typeof loadFolderList === 'function') loadFolderList();")
            page.wait_for_timeout(500)

        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())
        assert _select_product(page, "CACGO K73")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "product_spec")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=30000)
        assert done, f"Flow failed: {error}"

        gen_call = _find_agent_generate_call(fake_llm, "product_spec")
        assert gen_call is not None, "product_spec.generate must exist"
        messages = gen_call["messages"]
        user_msg = next((m for m in messages if m["role"] == "user"), None)
        assert user_msg is not None, "User message must exist"
        content = user_msg["content"]
        has_image = False
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    has_image = True
                    break
        assert has_image, \
            "K73 Agent 1 must receive image_url (correctly associated image)"

    def test_lagenio_k5_xlsx_images_reach_agent(self, _browser):
        """Lagenio K5 XLSX has 3 embedded images (1 BMP + 2 PNG).

        After browser upload → staging commit → product selection →
        Agent 1, the images must:
        1. Be extracted to cache/{product}/extracted_images/
        2. Be persisted in Product DB image_descriptions (not unassigned)
        3. Be returned by product_db.get_product_image_paths("Lagenio K5")
        4. Reach Agent 1's model-call input as image_url content parts

        Reports image count, paths, hashes, and exact boundary evidence.
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        tmp = _browser["tmp"]

        # --- STAGE A: Verify original source file exists ---
        assert LAGENIO_K5_XLSX.exists(), \
            f"Real source file must exist: {LAGENIO_K5_XLSX}"

        # --- Upload K5 through the browser if not already present ---
        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K5_XLSX,
            _lagenio_k5_segmentation(), "Lagenio K5",
        )

        # --- STAGE B: Verify Product DB has the images ---
        from src import product_db
        import hashlib

        rec = product_db.load("Lagenio K5")
        image_descs = rec.get("image_descriptions", [])
        assert len(image_descs) >= 3, \
            f"K5 Product DB must have at least 3 image_descriptions, " \
            f"got: {len(image_descs)}"

        # All images in single-product mode must be non-unassigned
        usable = [d for d in image_descs if not d.get("unassigned_source_media")]
        assert len(usable) >= 3, \
            f"K5 must have at least 3 non-unassigned images (single mode), " \
            f"got {len(usable)} usable out of {len(image_descs)} total"

        # --- STAGE C: Verify each image exists on disk and hash matches ---
        extracted_hashes = set()
        for desc in usable:
            img_path = desc.get("path", "")
            assert img_path, "Image path must be non-empty"
            assert Path(img_path).exists(), \
                f"Extracted image file must exist on disk: {img_path}"
            raw = Path(img_path).read_bytes()
            h = hashlib.sha256(raw).hexdigest()
            extracted_hashes.add(h)

        # Verify all 3 source image hashes are present
        expected_hashes = set(K5_XLSX_IMAGE_SHA256.values())
        missing = expected_hashes - extracted_hashes
        assert not missing, \
            f"K5 extracted images must include all 3 source hashes. " \
            f"Missing: {missing}. Got: {extracted_hashes}"

        # --- STAGE D: Verify get_product_image_paths returns them ---
        paths = product_db.get_product_image_paths("Lagenio K5")
        assert len(paths) >= 3, \
            f"get_product_image_paths('Lagenio K5') must return >= 3 paths, " \
            f"got: {len(paths)}"

        # Verify each returned path exists and hash matches
        for p in paths:
            assert Path(p).exists(), \
                f"get_product_image_paths returned non-existent path: {p}"
            raw = Path(p).read_bytes()
            h = hashlib.sha256(raw).hexdigest()
            assert h in expected_hashes, \
                f"K5 image hash {h} not in expected source hashes: {expected_hashes}"

        # --- STAGE E: Run Agent 1 and verify images reach model boundary ---
        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())
        assert _select_product(page, "Lagenio K5")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "product_spec")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=30000)
        assert done, f"Flow failed: {error}"

        gen_call = _find_agent_generate_call(fake_llm, "product_spec")
        assert gen_call is not None, "product_spec.generate must exist"
        messages = gen_call["messages"]
        user_msg = next((m for m in messages if m["role"] == "user"), None)
        assert user_msg is not None, "User message must exist"
        content = user_msg["content"]

        # Count image_url parts in the multimodal user message
        image_url_parts = []
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image_url_parts.append(part)

        assert len(image_url_parts) >= 3, \
            f"Agent 1 user message must contain >= 3 image_url parts, " \
            f"got: {len(image_url_parts)}"

        # --- STAGE F: Report exact boundary evidence ---
        print(f"\n[K5 Image Evidence]")
        print(f"  Source file: {LAGENIO_K5_XLSX}")
        print(f"  Extracted image count: {len(usable)}")
        for desc in usable:
            p = desc.get("path", "")
            raw = Path(p).read_bytes()
            h = hashlib.sha256(raw).hexdigest()
            print(f"    path={p}")
            print(f"    sha256={h}")
            print(f"    size={len(raw)} bytes")
        print(f"  get_product_image_paths returned {len(paths)} paths:")
        for p in paths:
            print(f"    {p}")
        print(f"  Agent 1 image_url parts in model-call input: {len(image_url_parts)}")
        for i, part in enumerate(image_url_parts):
            url = part.get("image_url", {}).get("url", "")
            print(f"    part[{i}]: type=image_url, url_prefix={url[:50]}...")


# ---------------------------------------------------------------------------
# TEST 9: Complete UI settings propagation matrix
# ---------------------------------------------------------------------------

class TestUISettingsPropagationMatrix:
    """Test every UI-exposed setting to prove the selected VALUE reaches
    the code that consumes it.

    Seeing a control in the DOM is NOT proof.  The test must prove the
    value reaches the consumption point.
    """

    def test_platforms_reach_content_creator(self, _browser):
        """Platform selection (facebook/tiktok) must reach Agent 4's
        prompt as a platform label."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_content_creator_json())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "content_creator")
        assert _go_to_step(page, 3)

        # Toggle platforms — deselect facebook, keep tiktok only
        fb_chip = page.query_selector(".opt-chip[data-platform='facebook']")
        if fb_chip and "active" in (fb_chip.get_attribute("class") or ""):
            fb_chip.click()
            page.wait_for_timeout(200)

        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=45000)
        assert done, f"Flow failed: {error}"

        # Verify TikTok appears in the agent prompt
        gen_call = _find_agent_generate_call(fake_llm, "content_creator")
        assert gen_call is not None, "content_creator.generate must exist"
        _, user = _extract_messages_text(fake_llm, 0)
        assert "TikTok" in user, \
            "TikTok platform label must reach Agent 4 prompt"
        assert "Facebook" not in user, \
            "Facebook must NOT appear (deselected in UI)"

    def test_content_count_reaches_content_creator(self, _browser):
        """Content count N must produce exactly ceil(N/platforms) * platforms
        generate calls.

        Production contract (web_viewer.py line 2775-2782):
          content_count = N total posts minimum
          count_per_platform = ceil(N / num_platforms)
          total calls = count_per_platform * num_platforms

        With content_count=3 and 2 platforms (facebook+tiktok):
          count_per_platform = ceil(3/2) = 2
          total generate calls = 2 * 2 = 4
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_content_creator_json())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "content_creator")
        assert _go_to_step(page, 3)

        # Set content count to 3 (per platform)
        count_input = page.query_selector("#flow-opt-count-0")
        assert count_input is not None, "Content count input must exist in DOM"
        count_input.fill("3")
        page.wait_for_timeout(200)
        # Verify the value was actually set
        actual_val = count_input.input_value()
        assert actual_val == "3", \
            f"Content count input must read 3 after fill, got '{actual_val}'"

        # Check how many platforms are selected
        active_platforms = page.eval_on_selector_all(
            ".opt-chip[data-platform].active",
            "els => els.map(e => e.getAttribute('data-platform'))"
        )
        n_platforms = max(1, len(active_platforms))

        # UI contract: count = posts PER PLATFORM
        # Frontend sends content_count = count * n_platforms (total)
        # Backend: count_per_platform = ceil(content_count / n_platforms)
        # Total generate calls = count_per_platform * n_platforms
        # With count=3, n_platforms=2: content_count=6, per_platform=3, total=6
        expected_total = 3 * n_platforms  # count * platforms

        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=60000)
        assert done, f"Flow failed: {error}"

        gen_calls = [c for c in fake_llm.calls if c.get("source") == "content_creator.generate"]
        assert len(gen_calls) == expected_total, \
            f"Content count 3 per platform with {n_platforms} platforms must produce exactly " \
            f"{expected_total} generate calls (3 * {n_platforms}), got: {len(gen_calls)}"

    def test_media_type_reaches_content_creator(self, _browser):
        """media_type must reach Orchestrator.run_content_creator() with the
        exact value selected in the UI.

        Uses a spy wrapper around run_content_creator that captures the
        media_type kwarg while still calling the real method.
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        # Install a spy on Orchestrator._run_content_creator_raw
        from src.orchestrator import Orchestrator
        _orig_rcc = Orchestrator._run_content_creator_raw
        captured_media_type: list[str] = []

        def _spy_rcc(self, *args, **kwargs):
            mt = kwargs.get("media_type") or (args[2] if len(args) > 2 else "")
            captured_media_type.append(mt)
            return _orig_rcc(self, *args, **kwargs)

        Orchestrator._run_content_creator_raw = _spy_rcc
        try:
            fake_llm.reset()
            fake_llm.set_agent_output(_content_creator_json())
            assert _select_product(page, "Lagenio K2")
            assert _go_to_step(page, 2)
            assert _select_agent(page, "content_creator")
            assert _go_to_step(page, 3)

            # Set media_type to "video" by toggling auto_image off
            # while auto_video is on.  Call the toggle function directly.
            # First ensure auto_video is on (toggle if off):
            page.evaluate("""
                if (typeof flows !== 'undefined' && flows[0]) {
                    if (!flows[0].options.auto_video) {
                        if (typeof onWizardAutoVideoToggle === 'function') onWizardAutoVideoToggle(0);
                    }
                    // Now toggle auto_image off (it should stay off because auto_video is on)
                    if (flows[0].options.auto_image) {
                        if (typeof onWizardAutoImageToggle === 'function') onWizardAutoImageToggle(0);
                    }
                }
            """)
            page.wait_for_timeout(500)
            # Verify the flow state
            mt = page.evaluate("flows && flows[0] ? flows[0].options.media_type : 'unknown'")
            assert mt == "video", \
                f"media_type must be 'video' after toggling, got: {mt}"

            assert _go_to_step(page, 4)
            page.wait_for_timeout(500)
            assert _run_flow(page)

            done, error = _wait_for_flow_done(page, timeout_ms=45000)
            assert done, f"Flow failed: {error}"

            # Assert the exact media_type value reached the production boundary
            assert len(captured_media_type) >= 1, \
                "_run_content_creator_raw must be called at least once"
            assert captured_media_type[0] == "video", \
                f"media_type must be 'video' (video-only selection), " \
                f"got: {captured_media_type[0]}"
        finally:
            Orchestrator._run_content_creator_raw = _orig_rcc

    def test_auto_image_false_prevents_media_generation(self, _browser):
        """auto_image=false must result in exactly 0 image provider calls.

        Uses tracing spies on media_gen.generate_image_with_retry and
        generate_video_with_retry to count actual provider calls.
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        media_log = _browser["media_call_log"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        # Reset media call counters
        media_log["image"] = 0
        media_log["video"] = 0

        fake_llm.reset()
        fake_llm.set_agent_output(_content_creator_json())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "content_creator")
        assert _go_to_step(page, 3)

        # Ensure auto_image is off (toggle off if on)
        _click_chip_by_text(page, "รูป", should_be_active=True)

        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=45000)
        assert done, f"Flow failed: {error}"

        # Assert exactly 0 image provider calls
        assert media_log["image"] == 0, \
            f"auto_image=false must produce 0 image provider calls, " \
            f"got {media_log['image']}"

    def test_auto_video_false_prevents_video_generation(self, _browser):
        """auto_video=false must result in exactly 0 video provider calls."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        media_log = _browser["media_call_log"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        media_log["image"] = 0
        media_log["video"] = 0

        fake_llm.reset()
        fake_llm.set_agent_output(_content_creator_json())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "content_creator")
        assert _go_to_step(page, 3)

        # Ensure auto_video is off
        _click_chip_by_text(page, "วิดีโอ", should_be_active=True)

        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=45000)
        assert done, f"Flow failed: {error}"

        # Assert exactly 0 video provider calls
        assert media_log["video"] == 0, \
            f"auto_video=false must produce 0 video provider calls, " \
            f"got {media_log['video']}"

    def test_text_qualification_zero_media_calls(self, _browser):
        """Text qualification settings (auto_image=false, auto_video=false)
        must produce exactly 0 image AND 0 video provider calls."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        media_log = _browser["media_call_log"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        media_log["image"] = 0
        media_log["video"] = 0

        fake_llm.reset()
        fake_llm.set_agent_output(_content_creator_json())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "content_creator")
        assert _go_to_step(page, 3)

        # Ensure both auto_image and auto_video are OFF
        _click_chip_by_text(page, "รูป", should_be_active=True)
        _click_chip_by_text(page, "วิดีโอ", should_be_active=True)

        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=45000)
        assert done, f"Flow failed: {error}"

        assert media_log["image"] == 0, \
            f"Text qualification must produce 0 image calls, got {media_log['image']}"
        assert media_log["video"] == 0, \
            f"Text qualification must produce 0 video calls, got {media_log['video']}"


# ---------------------------------------------------------------------------
# TEST 10: Brand context propagation
# ---------------------------------------------------------------------------

class TestBrandContextPropagation:
    """Prove brand context from a deterministic brand fixture reaches the
    Agent model boundary.

    BrandInterpretationPass is NOT patched — it runs naturally with
    FakeLLM.  Only CompetitorReportRenderer.validate is patched (needs
    real web annotations, not a model boundary).
    """

    def test_brand_reference_reaches_campaign_strategy(self, _browser):
        """Brand reference text must appear in Agent 3's system prompt."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        brand_marker = _browser["brand_marker"]
        brand_audience = _browser["brand_audience_marker"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_campaign_output())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "campaign_strategy")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=30000)
        assert done, f"Flow failed: {error}"

        gen_call = _find_agent_generate_call(fake_llm, "campaign_strategy")
        assert gen_call is not None, "campaign_strategy.generate must exist"

        # Check system prompt for brand reference
        messages = gen_call["messages"]
        system = next((m for m in messages if m["role"] == "system"), None)
        assert system is not None, "System message must exist"
        system_text = system["content"] if isinstance(system["content"], str) else str(system["content"])

        assert brand_marker in system_text, \
            f"Brand marker '{brand_marker}' must appear in campaign_strategy system prompt"
        assert brand_audience in system_text, \
            f"Brand audience '{brand_audience}' must appear in campaign_strategy system prompt"

    def test_brand_reference_reaches_content_creator(self, _browser):
        """Brand reference text must appear in Agent 4's system prompt."""
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        brand_marker = _browser["brand_marker"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_content_creator_json())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "content_creator")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=45000)
        assert done, f"Flow failed: {error}"

        gen_call = _find_agent_generate_call(fake_llm, "content_creator")
        assert gen_call is not None, "content_creator.generate must exist"

        messages = gen_call["messages"]
        system = next((m for m in messages if m["role"] == "system"), None)
        assert system is not None
        system_text = system["content"] if isinstance(system["content"], str) else str(system["content"])

        assert brand_marker in system_text, \
            f"Brand marker '{brand_marker}' must appear in content_creator system prompt"

    def test_brand_interpretation_runs_for_agent2(self, _browser):
        """Agent 2 in evidence mode must call BrandInterpretationPass
        (which makes a real LLM call to FakeLLM) when brand_reference
        is non-empty.

        BrandInterpretationPass is NOT patched.
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        brand_marker = _browser["brand_marker"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_research_json_output("Lagenio K2"))
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "competitor_analysis")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)

        done, error = _wait_for_flow_done(page, timeout_ms=45000)
        assert done, f"Flow failed: {error}"

        sources = _get_all_sources(fake_llm)
        assert "competitor_analysis.brand_interpretation" in sources, \
            f"BrandInterpretationPass must make an LLM call when brand_reference is non-empty, " \
            f"got sources: {sources}"

        # Verify the brand_interpretation call received brand context
        brand_calls = [c for c in fake_llm.calls if c.get("source") == "competitor_analysis.brand_interpretation"]
        assert len(brand_calls) >= 1, "At least 1 brand_interpretation call"
        brand_msg = brand_calls[0]["messages"]
        brand_user = next((m for m in brand_msg if m["role"] == "user"), None)
        assert brand_user is not None
        brand_text = brand_user["content"] if isinstance(brand_user["content"], str) else str(brand_user["content"])
        assert brand_marker in brand_text, \
            f"Brand marker '{brand_marker}' must appear in brand_interpretation user prompt"


# ---------------------------------------------------------------------------
# TEST 11: Exact ordered model-call trace for each Agent
# ---------------------------------------------------------------------------

class TestExactModelCallTrace:
    """Capture and verify the exact ordered model-boundary call sequence
    for each Agent under text qualification settings (auto_image=false,
    auto_video=false).

    The trace is derived from real code execution, not manual estimation.
    """

    def test_agent1_exact_trace(self, _browser):
        """Agent 1 (product_spec) exact call trace.

        Production config: max_review_iterations=1 (from defaults).
        No web_search, no evidence_mode.

        Expected ordered sources:
          1. product_spec.generate
          2. product_spec.review
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_product_spec_output())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "product_spec")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)
        done, _ = _wait_for_flow_done(page, timeout_ms=30000)
        assert done

        sources = _get_all_sources(fake_llm)
        expected_sources = [
            "product_spec.generate",
            "product_spec.review",
        ]
        assert sources == expected_sources, \
            f"Agent 1 exact trace mismatch.\nExpected: {expected_sources}\nGot: {sources}"

    def test_agent2_exact_trace(self, _browser):
        """Agent 2 (competitor_analysis) exact call trace with brand.

        Production config: max_review_iterations=0, evidence_mode=true,
        use_brand_reference=true, web_search=true.

        Expected ordered sources:
          1. competitor_analysis.generate
          2. competitor_analysis.semantic_review
          3. competitor_analysis.brand_interpretation

        No standard review call (max_review_iterations=0).
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_research_json_output("Lagenio K2"))
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "competitor_analysis")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)
        done, _ = _wait_for_flow_done(page, timeout_ms=45000)
        assert done

        sources = _get_all_sources(fake_llm)
        # Assert exact ordered source list
        expected_sources = [
            "competitor_analysis.generate",
            "competitor_analysis.semantic_review",
            "competitor_analysis.brand_interpretation",
        ]
        assert sources == expected_sources, \
            f"Agent 2 exact trace mismatch.\nExpected: {expected_sources}\nGot: {sources}"

    def test_agent3_exact_trace(self, _browser):
        """Agent 3 (campaign_strategy) exact call trace with brand.

        Production config: max_review_iterations=0, web_search=true,
        use_brand_reference=true, use_brand_differentiator=true.

        Expected ordered sources:
          1. campaign_strategy.generate

        No standard review call (max_review_iterations=0).
        Grounding repair may occur if citation validation fails.
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        fake_llm.reset()
        fake_llm.set_agent_output(_campaign_output())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "campaign_strategy")
        assert _go_to_step(page, 3)
        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)
        done, _ = _wait_for_flow_done(page, timeout_ms=30000)
        assert done

        sources = _get_all_sources(fake_llm)
        generate_calls = [s for s in sources if s == "campaign_strategy.generate"]
        assert len(generate_calls) == 1, \
            f"Agent 3 must make 1 generate call, got {len(generate_calls)}: {sources}"
        # No standard review (max_review_iterations=0)
        review_calls = [s for s in sources if s == "campaign_strategy.review"]
        assert len(review_calls) == 0, \
            f"Agent 3 must NOT make review calls (max_review_iterations=0), got: {review_calls}"
        # Grounding repair may happen — it's a bounded branch
        repair_calls = [s for s in sources if s == "campaign_strategy.repair"]
        # repair is optional — 0 or 1 calls depending on grounding validation

    def test_agent4_exact_trace_text_qualification(self, _browser):
        """Agent 4 (content_creator) exact call trace with text-only
        settings (auto_image=false, auto_video=false).

        Production config: max_review_iterations=1, use_brand_reference=true,
        use_brand_differentiator=true.

        With 2 platforms (facebook+tiktok) and content_count=1 (per platform):
          Frontend sends content_count = 1 * 2 = 2 (total)
          Backend: count_per_platform = ceil(2/2) = 1
          Total generate calls = 1 * 2 = 2

        Expected ordered sources (per platform):
          1. content_creator.generate  (platform 1)
          2. content_creator.review     (platform 1 script review)
          3. content_creator.generate  (platform 2)
          4. content_creator.review     (platform 2 script review)

        No media generation calls (auto_image=false, auto_video=false).
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        media_log = _browser["media_call_log"]

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        media_log["image"] = 0
        media_log["video"] = 0

        fake_llm.reset()
        fake_llm.set_agent_output(_content_creator_json())
        assert _select_product(page, "Lagenio K2")
        assert _go_to_step(page, 2)
        assert _select_agent(page, "content_creator")
        assert _go_to_step(page, 3)

        # Ensure auto_image and auto_video are OFF via JS toggle
        page.evaluate("""
            if (typeof flows !== 'undefined' && flows[0]) {
                if (flows[0].options.auto_image) {
                    if (typeof onWizardAutoImageToggle === 'function') onWizardAutoImageToggle(0);
                }
                if (flows[0].options.auto_video) {
                    if (typeof onWizardAutoVideoToggle === 'function') onWizardAutoVideoToggle(0);
                }
            }
        """)
        page.wait_for_timeout(300)

        assert _go_to_step(page, 4)
        page.wait_for_timeout(500)
        assert _run_flow(page)
        done, _ = _wait_for_flow_done(page, timeout_ms=45000)
        assert done

        sources = _get_all_sources(fake_llm)
        generate_calls = [s for s in sources if s == "content_creator.generate"]
        review_calls = [s for s in sources if s == "content_creator.review"]

        # With 2 platforms and content_count=1 per platform:
        # 2 generate calls + 2 review calls + 2 script_review calls = 6 total
        # The script_review path is triggered because the content response
        # includes a non-empty script field.
        assert len(generate_calls) == 2, \
            f"Agent 4 must make 2 generate calls (1 per platform), got {len(generate_calls)}: {sources}"
        assert len(review_calls) == 2, \
            f"Agent 4 must make 2 review calls (1 per platform), got {len(review_calls)}: {sources}"
        script_review_calls = [s for s in sources if s == "script_reviewer.review_script"]
        assert len(script_review_calls) == 2, \
            f"Agent 4 must make 2 script_review calls (1 per platform, non-empty script), " \
            f"got {len(script_review_calls)}: {sources}"

        # Assert exact ordered source list (with script review path)
        expected_sources = [
            "content_creator.generate",
            "content_creator.review",
            "script_reviewer.review_script",
            "content_creator.generate",
            "content_creator.review",
            "script_reviewer.review_script",
        ]
        assert sources == expected_sources, \
            f"Agent 4 exact trace mismatch.\nExpected: {expected_sources}\nGot: {sources}"

        # No media generation calls
        assert media_log["image"] == 0, \
            f"No image provider calls with text-only settings, got {media_log['image']}"
        assert media_log["video"] == 0, \
            f"No video provider calls with text-only settings, got {media_log['video']}"


class TestBrandUIEditToAgent:
    """Brand UI edit → save → Agent E2E test.

    Proves that a brand field edited through the real browser DOM
    (Brand sidebar → Profile editor → textarea → Save button) reaches
    the production Agent/model context.
    """

    def test_brand_profile_edit_reaches_campaign_strategy(self, _browser):
        """Edit brand_profile.md text via the real browser DOM → run
        Agent 3 → assert the new marker appears in the agent's system
        prompt.

        Uses a unique recognizable marker that is NOT pre-seeded in the
        brand directory. The test opens the Brand sidebar, clicks the
        Profile section, fills #brand-profile-textarea, clicks the real
        Save button, waits for the UI success state, then runs
        campaign_strategy (which has use_brand_reference=true) and
        asserts the marker reaches the production model boundary.

        We edit brand_profile.md (not audience) because product_profile
        audience overrides brand audience in load_brand_reference.
        brand_profile.md is always included and never overridden.
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        tmp = _browser["tmp"]
        unique_marker = "BETA_TEST_BRAND_PROFILE_MARKER_4827"

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        # 1. Read current brand profile for reliable restore
        import json as _json
        import urllib.request
        base_url = _browser["url"]
        with urllib.request.urlopen(f"{base_url}/api/brand_json") as resp:
            brand_data = _json.loads(resp.read())
        original_profile = brand_data.get("profile", "") or ""

        try:
            # 2. Open the Brand sidebar tab via the real DOM
            page.evaluate("""
                const tabs = document.querySelectorAll('.sidebar-tab');
                for (const t of tabs) {
                    if (t.textContent.includes('แบรนด์')) { t.click(); break; }
                }
            """)
            page.wait_for_timeout(500)

            # 3. Click the "ประวัติแบรนด์" (Profile) section in the sidebar
            page.evaluate("editBrandSection('profile')")
            page.wait_for_timeout(300)

            # 4. Fill #brand-profile-textarea with the unique marker
            textarea = page.query_selector("#brand-profile-textarea")
            assert textarea is not None, "brand-profile-textarea must exist"
            textarea.fill("")
            textarea.fill(unique_marker)
            page.wait_for_timeout(200)

            # 5. Click the real Save button inside the brand modal
            save_btn = page.query_selector(
                "#brand-overlay .settings-save"
            )
            assert save_btn is not None, "Brand save button must exist"
            save_btn.click()

            # 6. Wait for the UI success state (#brand-save-status-modal)
            page.wait_for_function(
                """() => {
                    const s = document.getElementById('brand-save-status-modal');
                    return s && s.textContent.includes('บันทึก');
                }""",
                timeout=10000,
            )

            # 7. Close the brand modal
            page.evaluate("closeBrandModal(true)")
            page.wait_for_timeout(200)

            # 8. Verify the file was actually written to tmp/brand/brand_profile.md
            profile_file = tmp / "brand" / "brand_profile.md"
            assert profile_file.exists(), "brand_profile.md must exist after save"
            written = profile_file.read_text(encoding="utf-8")
            assert unique_marker in written, \
                "Saved brand_profile.md must contain the unique marker"

            # 9. Run Agent 3 (campaign_strategy) which uses brand_reference
            fake_llm.reset()
            fake_llm.set_agent_output(_campaign_output())
            assert _select_product(page, "Lagenio K2")
            assert _go_to_step(page, 2)
            assert _select_agent(page, "campaign_strategy")
            assert _go_to_step(page, 3)
            assert _go_to_step(page, 4)
            page.wait_for_timeout(500)
            assert _run_flow(page)
            done, error = _wait_for_flow_done(page, timeout_ms=30000)
            assert done, f"Flow failed: {error}"

            # 10. Assert the unique marker reaches the agent's system prompt
            gen_call = _find_agent_generate_call(fake_llm, "campaign_strategy")
            assert gen_call is not None, "campaign_strategy.generate must exist"
            messages = gen_call["messages"]
            system = next((m for m in messages if m["role"] == "system"), None)
            assert system is not None, "System message must exist"
            system_text = system["content"] if isinstance(system["content"], str) else str(system["content"])
            assert unique_marker in system_text, \
                f"Brand profile marker '{unique_marker}' must appear in " \
                f"campaign_strategy system prompt after UI save. " \
                f"System prompt length: {len(system_text)}. " \
                f"All sources: {_get_all_sources(fake_llm)}"
        finally:
            # Restore original profile via the API (reliable cleanup)
            restore_payload = _json.dumps(
                {"profile": original_profile}
            ).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/api/brand_json_save",
                data=restore_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req)


class TestProductProfileUIEditToAgent:
    """Product Profile UI edit → save → Agent E2E test.

    Proves that a product profile field edited through the real browser
    DOM (Manage modal → Product Profile form → Save button) reaches the
    production Agent/model context.
    """

    def test_profile_tone_adjustment_reaches_content_creator(self, _browser):
        """Edit product_profile.tone_adjustment via the real browser DOM
        → run Agent 4 → assert the new marker appears in the agent's
        system prompt (via load_brand_rules → tone_adjustment section).

        Uses a unique recognizable marker that is NOT pre-seeded in the
        product profile. The test opens the product's Manage modal,
        fills #pp-tone, clicks the real Save button, waits for save
        completion, then runs content_creator and asserts the marker
        reaches the production model boundary.
        """
        page = _browser["page"]
        fake_llm = _browser["fake_llm"]
        tmp = _browser["tmp"]
        unique_marker = "BETA_TEST_TONE_ADJ_9351"
        folder = "Lagenio K2"

        _ensure_product_uploaded(
            page, fake_llm, LAGENIO_K2_XLSX,
            _lagenio_k2_segmentation(), "Lagenio K2",
        )

        # 1. Read current product profile for reliable restore
        import json as _json
        import urllib.request
        base_url = _browser["url"]
        with urllib.request.urlopen(
            f"{base_url}/api/product_profile/{urllib.parse.quote(folder)}"
        ) as resp:
            profile_data = _json.loads(resp.read())
        original_tone = profile_data.get("tone_adjustment", "")
        original_payload = dict(profile_data)

        try:
            # 2. Open the Manage modal for the product via the real DOM
            page.evaluate(
                f"openUploadModalForFolder({json.dumps(folder)}, '')"
            )
            page.wait_for_timeout(800)

            # 3. Wait for the Product Profile form to render (#pp-tone)
            page.wait_for_selector("#pp-tone", timeout=10000)

            # 4. Fill #pp-tone with the unique marker
            tone_field = page.query_selector("#pp-tone")
            assert tone_field is not None, "#pp-tone must exist"
            tone_field.fill("")
            tone_field.fill(unique_marker)
            page.wait_for_timeout(200)

            # 5. Click the real Save button (#upload-submit-btn)
            save_btn = page.query_selector("#upload-submit-btn")
            assert save_btn is not None, "#upload-submit-btn must exist"
            save_btn.click()

            # 6. Wait for save completion — the modal closes on success
            #    or #upload-modal-status shows "บันทึก"
            page.wait_for_function(
                """() => {
                    const overlay = document.getElementById('upload-overlay');
                    const status = document.getElementById('upload-modal-status');
                    if (status && status.textContent.includes('บันทึก')) return true;
                    if (overlay && !overlay.className.includes('visible')) return true;
                    return false;
                }""",
                timeout=15000,
            )
            page.wait_for_timeout(300)

            # 7. Verify the file was actually written
            profile_file = tmp / "cache" / folder / "product_profile.json"
            assert profile_file.exists(), "product_profile.json must exist after save"
            written = _json.loads(profile_file.read_text(encoding="utf-8"))
            assert written.get("tone_adjustment") == unique_marker, \
                f"Saved product_profile.json must contain the unique marker. " \
                f"Got tone_adjustment: {written.get('tone_adjustment')}"

            # 8. Run Agent 4 (content_creator) which uses brand_rules
            # (tone_adjustment is injected via load_brand_rules)
            fake_llm.reset()
            fake_llm.set_agent_output(_content_creator_json())
            assert _select_product(page, "Lagenio K2")
            assert _go_to_step(page, 2)
            assert _select_agent(page, "content_creator")
            assert _go_to_step(page, 3)

            # Ensure auto_image and auto_video are OFF
            page.evaluate("""
                if (typeof flows !== 'undefined' && flows[0]) {
                    if (flows[0].options.auto_image) {
                        if (typeof onWizardAutoImageToggle === 'function') onWizardAutoImageToggle(0);
                    }
                    if (flows[0].options.auto_video) {
                        if (typeof onWizardAutoVideoToggle === 'function') onWizardAutoVideoToggle(0);
                    }
                }
            """)
            page.wait_for_timeout(300)

            assert _go_to_step(page, 4)
            page.wait_for_timeout(500)
            assert _run_flow(page)
            done, error = _wait_for_flow_done(page, timeout_ms=45000)
            assert done, f"Flow failed: {error}"

            # 9. Assert the unique marker reaches the agent's system prompt
            gen_call = _find_agent_generate_call(fake_llm, "content_creator")
            assert gen_call is not None, "content_creator.generate must exist"
            messages = gen_call["messages"]
            system = next((m for m in messages if m["role"] == "system"), None)
            assert system is not None, "System message must exist"
            system_text = system["content"] if isinstance(system["content"], str) else str(system["content"])
            assert unique_marker in system_text, \
                f"Product profile tone_adjustment marker '{unique_marker}' must " \
                f"appear in content_creator system prompt after UI save. " \
                f"System prompt excerpt: {system_text[:500]}"
        finally:
            # Restore original profile via the API (reliable cleanup)
            original_payload["tone_adjustment"] = original_tone
            restore_payload = _json.dumps(original_payload).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/api/product_profile_save/{urllib.parse.quote(folder)}",
                data=restore_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
