"""Browser DOM regression — Product Detail acceptance pass.

Real-server + real-browser proofs for the three Product Detail issues:
  1. AI review modal rows stay inside modal bounds (overflow regression).
  2. Extracted embedded media renders as separate thumbnails in
     Media & Sources while the source file stays listed.
  3. Fact rows are deletable end-to-end (field tombstone reaches
     effective_facts) and the add control is chip-style (inline input,
     Enter creates a prefilled row — no stacked empty forms).

Uses a FakeLLM at the model boundary — zero provider calls.
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


class _FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return json.dumps({"summary": "s", "category": "c",
                           "derived_facts": {}}, ensure_ascii=False)

    def close(self):
        pass

    def abort(self):
        pass


@pytest.fixture(scope="module")
def _server(tmp_path_factory):
    """Minimal real uvicorn server with FakeLLM — same pattern as
    test_browser_product_facts._server."""
    import web_viewer
    from src import product_db, staging, ingestion, config_loader, asset_library
    from src.orchestrator import Orchestrator
    import yaml as _yaml

    tmp = tmp_path_factory.mktemp("pd_acceptance")
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
        "staging_root": staging._project_root,
        "staging_product_db": staging.product_db,
        "make_client": Orchestrator.make_client,
        "ingestion_make_llm": ingestion._make_llm,
        "api_key": os.environ.get("OPENROUTER_API_KEY"),
        "current_llm": getattr(web_viewer, "_current_llm", {}),
        "session_ts": getattr(web_viewer, "_session_ts", {}),
        "cancel": getattr(web_viewer, "_cancel_requested", {}),
        "active_llms": getattr(web_viewer, "_active_llms", {}),
    }
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
    (tmp / "data" / "auth").mkdir(parents=True, exist_ok=True)
    _store = UserStore(tmp / "data" / "auth" / "users.json")
    _sess = SessionManager(tmp / "data" / "auth" / "sessions.json")
    auth_mod._user_store = _store
    auth_mod._session_manager = _sess
    _user = _store.register("testuser", "testpass")
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

    import uvicorn
    import socket
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
        "session_token": _sess.create_session(_user.user_id),
    }

    server.should_exit = True
    thread.join(timeout=5)
    staging._project_root = _orig["staging_root"]
    staging.product_db = _orig["staging_product_db"]
    web_viewer.PROJECT_ROOT = _orig["PROJECT_ROOT"]
    Orchestrator.make_client = _orig["make_client"]
    ingestion._make_llm = _orig["ingestion_make_llm"]
    _bl_mod._resolve_brand_dir = _orig_resolve
    if _orig["api_key"] is not None:
        os.environ["OPENROUTER_API_KEY"] = _orig["api_key"]


@pytest.fixture(scope="module")
def _ctx(_server):
    """Browser context with authed + brand-selected cookies and a seeded
    product (source file + extracted image + derived facts)."""
    url = _server["url"]
    token = _server["session_token"]

    def _req(path, method="GET", data=None):
        r = urllib.request.Request(
            f"{url}{path}", data=data, method=method,
            headers={"Cookie": f"mktapp_session={token}",
                     "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(r, timeout=5).read())

    bid = _req("/api/brands", "POST",
               json.dumps({"name": "PdAcceptBrand"}).encode())["brand_id"]
    _req(f"/api/brands/{bid}/select", "POST", b"")

    brand_root = _server["tmp"] / "users" / _server["user_id"] / "brands" / bid
    for d in ("data", "cache", "brand", "output"):
        (brand_root / d).mkdir(parents=True, exist_ok=True)

    # Seed product: source file + extracted image + derived facts
    prod = "K2Media"
    (brand_root / "data" / prod).mkdir(parents=True)
    (brand_root / "data" / prod / "spec.txt").write_text(
        "Battery | 680mAh", encoding="utf-8")
    # URL-imported products store page images as image-type source files —
    # this seed's page_img_001.png is that authoritative presentation.
    png_bytes = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
                 b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\xdac\xfc\xcf'
                 b'\xc0P\x0f\x00\x04\x85\x01\x80\x84\xa9\x8c!\x00\x00\x00\x00IEND\xaeB`\x82')
    (brand_root / "data" / prod / "page_img_001.png").write_bytes(png_bytes)
    media_dir = brand_root / "cache" / prod / "extracted_images"
    media_dir.mkdir(parents=True)
    png = media_dir / "spec_img_001.png"
    png.write_bytes(png_bytes)

    from src import product_db
    from src.workspace_context import WorkspaceContext, set_workspace
    set_workspace(WorkspaceContext.for_brand(
        _server["user_id"], bid, _server["tmp"]))
    try:
        product_db.save(prod, {
            "files": [{"name": "spec.txt", "path": "spec.txt",
                       "type": "text", "status": "ready"},
                      {"name": "page_img_001.png", "path": "page_img_001.png",
                       "type": "image", "status": "ready"}],
            "derived_facts": {
                "battery_capacity": {"label": "Battery", "value": "680mAh"},
                "bad_field": {"label": "Wrong Heading", "value": "oops"},
            },
            "image_descriptions": [
                {"file": "spec_img_001.png", "path": str(png),
                 "source": "spec.txt"},
            ],
            "metadata": {},
        })
    finally:
        set_workspace(None)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_cookies([
            {"name": "mktapp_session", "value": token, "url": url},
            {"name": "mktapp_brand", "value": bid, "url": url},
        ])
        page = context.new_page()
        page.goto(f"{url}/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1200)
        yield {"page": page, "url": url, "product": prod,
               "server": _server, "brand_root": brand_root, "bid": bid}
        context.close()
        browser.close()


def _open_pd(page, product):
    """Open the product detail view through the real UI path."""
    page.evaluate("""p => {
      // real path used by the folder list click handler
      if (typeof openProductDetail === 'function') { openProductDetail(p); return; }
      _pdFolder = p; _pdLoad().then(([profile, info, files, ai]) =>
        _renderProductDetailView(profile, info, files, ai));
      document.getElementById('product-detail-view').style.display = 'block';
    }""", product)
    page.wait_for_timeout(800)


# ---------------------------------------------------------------------------
# 1. AI review modal — rows must fit inside modal bounds
# ---------------------------------------------------------------------------

_PROPOSAL = {
    "proposal": {
        "proposal_id": "acc1",
        "facts": {
            "battery_capacity": {"label": "Battery Capacity", "value": "680mAh"},
            "display_type": {"label": "Display Type", "value": "AMOLED"},
        },
        "summary": "สรุปสินค้า " + "นาฬิกาเด็กสมาร์ทวอทช์ " * 10,
        "category": "สมาร์ทวอทช์",
        "profile": {"differentiators": ["GPS", "กันน้ำ IP68"]},
    },
    "current": {"facts": {"battery_capacity": {"value": "999mAh"}},
                "summary": "", "category": "", "profile": {}},
    "source_stale": True,
    "ai_enrichment": {"status": "idle"},
}


def test_ai_review_modal_rows_inside_bounds(_ctx):
    """Every review row + the footer button must fit inside the dialog —
    the reported 'content pushed right / clipped' regression."""
    page = _ctx["page"]
    for width in (1440, 900):
        page.set_viewport_size({"width": width, "height": 900})
        page.evaluate("v => { _pdAiView = v; _pdAiRender(); }", _PROPOSAL)
        page.evaluate(
            "() => document.getElementById('pd-ai-overlay').classList.add('visible')")
        page.wait_for_timeout(200)
        res = page.evaluate("""() => {
          const m = document.querySelector('#pd-ai-overlay .settings-modal');
          const mr = m.getBoundingClientRect();
          const rows = [...document.querySelectorAll('#pd-ai-body .pd-ai-row')];
          const outside = rows.filter(r => {
            const rr = r.getBoundingClientRect();
            return rr.left < mr.left - 0.5 || rr.right > mr.right + 0.5
                || r.scrollWidth > r.clientWidth + 1;
          }).length;
          const footer = [...document.querySelectorAll('#pd-ai-body button')];
          const clipped = footer.filter(b => {
            const br = b.getBoundingClientRect();
            return br.right > mr.right + 0.5 || br.left < mr.left - 0.5;
          }).length;
          return {mScrollW: m.scrollWidth, mClientW: m.clientWidth,
                  rows: rows.length, outside, clipped,
                  startBtn: !!document.querySelector('#pd-ai-body')};
        }""")
        assert res["rows"] >= 4, f"expected review rows, got {res}"
        assert res["outside"] == 0, (
            f"{res['outside']} rows outside modal bounds @ {width}px")
        assert res["clipped"] == 0, (
            f"{res['clipped']} buttons clipped @ {width}px")
        assert res["mScrollW"] <= res["mClientW"] + 1, (
            f"modal horizontally overflows @ {width}px")
        page.evaluate(
            "() => document.getElementById('pd-ai-overlay').classList.remove('visible')")


def test_ai_setup_view_keeps_start_button_visible(_ctx):
    """Setup phase: scope checkboxes + เริ่มใช้ AI visible inside bounds."""
    page = _ctx["page"]
    page.set_viewport_size({"width": 900, "height": 800})
    page.evaluate("v => { _pdAiView = v; _pdAiRender(); }",
                  {"ai_enrichment": {"status": "idle"}})
    page.evaluate(
        "() => document.getElementById('pd-ai-overlay').classList.add('visible')")
    page.wait_for_timeout(200)
    res = page.evaluate("""() => {
      const m = document.querySelector('#pd-ai-overlay .settings-modal');
      const mr = m.getBoundingClientRect();
      const btn = [...document.querySelectorAll('#pd-ai-body button')]
        .find(b => b.textContent.includes('เริ่มใช้ AI'));
      const br = btn ? btn.getBoundingClientRect() : null;
      const scopes = document.querySelectorAll('#pd-ai-body input[type=checkbox]').length;
      return {hasBtn: !!btn, inBounds: br ? (br.left >= mr.left && br.right <= mr.right) : false,
              scopes, mScrollW: m.scrollWidth, mClientW: m.clientWidth};
    }""")
    assert res["hasBtn"] and res["inBounds"], f"เริ่มใช้ AI must be visible: {res}"
    assert res["scopes"] == 2
    page.evaluate(
        "() => document.getElementById('pd-ai-overlay').classList.remove('visible')")
    page.set_viewport_size({"width": 1440, "height": 900})


# ---------------------------------------------------------------------------
# 2. Extracted media thumbnails in Media & Sources
# ---------------------------------------------------------------------------

def test_extracted_media_thumbnail_in_sources(_ctx):
    """Source file stays listed AND the extracted image renders as a
    separate thumbnail served by /api/product_media."""
    page = _ctx["page"]
    _open_pd(page, _ctx["product"])
    # expand sources
    page.evaluate("() => { _pdExpandedSections['sources'] = true; _pdRefresh(); }")
    page.wait_for_timeout(600)
    res = page.evaluate("""() => {
      const imgs = [...document.querySelectorAll('#pd-body img')]
        .filter(i => i.src.includes('/api/product_media/'));
      const text = document.getElementById('pd-body').textContent;
      return {mediaImgs: imgs.map(i => i.src),
              hasSource: text.includes('spec.txt'),
              hasMediaLabel: text.includes('สื่อที่สกัด')};
    }""")
    assert res["hasSource"], "source file must stay listed"
    assert len(res["mediaImgs"]) == 1, (
        f"expected one extracted-media thumbnail, got {res}")
    assert "spec_img_001.png" in res["mediaImgs"][0]
    assert res["hasMediaLabel"], "extracted media must be labelled separately"
    # thumbnail actually resolves bytes through the secure route
    r = page.evaluate(
        "u => fetch(u).then(r => r.status)", res["mediaImgs"][0])
    assert r == 200


def test_extracted_media_shares_source_row_contract(_ctx):
    """File-extracted media must render with the SAME row presentation as
    URL-imported/file media — one shared ``pd-src-row`` contract, identical
    thumbnail sizing, icon fallback, and a ⋯ menu that opens via the
    secure route."""
    page = _ctx["page"]
    _open_pd(page, _ctx["product"])
    page.evaluate("() => { _pdExpandedSections['sources'] = true; _pdRefresh(); }")
    page.wait_for_timeout(600)
    res = page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#pd-body .pd-src-row')];
      const mediaRow = rows.find(r => {
        const i = r.querySelector('img'); return i && i.src.includes('/api/product_media/');
      });
      const fileRow = rows.find(r => {
        const i = r.querySelector('img'); return i && i.src.includes('/api/product_source_file/');
      }) || rows[0];
      const dims = r => { const i = r.querySelector('img');
        return i ? {w: i.style.width, h: i.style.height, fit: i.style.objectFit} : null; };
      const menuBtn = mediaRow && mediaRow.querySelector('button[onclick*=_pdToggleFileMenu]');
      const menuItems = mediaRow && mediaRow.querySelector('.pd-file-menu');
      return {rowCount: rows.length, mediaIsRow: !!mediaRow,
              mediaDims: mediaRow && dims(mediaRow), fileDims: fileRow && dims(fileRow),
              hasMenu: !!menuBtn, menuView: menuItems ? menuItems.textContent : ''};
    }""")
    assert res["mediaIsRow"], "extracted media must use the shared pd-src-row contract"
    assert res["mediaDims"] == res["fileDims"], (
        f"thumbnail treatment must match file media, got {res}")
    assert res["mediaDims"] == {"w": "32px", "h": "32px", "fit": "cover"}
    assert res["hasMenu"], "media row must expose the same ⋯ menu affordance"
    assert "ดู" in res["menuView"]


# ---------------------------------------------------------------------------
# 3. Fact delete (whole field) + chip-style add
# ---------------------------------------------------------------------------

def test_fact_field_delete_reaches_effective(_ctx):
    """Removing a derived row tombstones the FIELD — after save the
    effective view no longer contains the wrongly-titled fact."""
    page = _ctx["page"]
    _open_pd(page, _ctx["product"])
    page.evaluate("() => pdEditFacts()")
    page.wait_for_timeout(800)
    res = page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#pd-facts-container .pd-fact-row')];
      const bad = rows.find(r => r.getAttribute('data-key') === 'bad_field');
      const btns = rows.map(r => r.querySelector('button[onclick*=_pdRemoveFact]'));
      const glyphs = btns.map(b => b && b.textContent.trim());
      if (bad) bad.querySelector('button[onclick*=_pdRemoveFact]').click();
      const state = _pdGetFactsState();
      return {removed: !!bad, allHaveRemove: btns.every(Boolean),
              glyphs: glyphs,
              deleted: state.deleted_facts, rowsAfter:
                document.querySelectorAll('#pd-facts-container .pd-fact-row').length};
    }""")
    assert res["removed"], "derived row must be removable"
    assert res["allHaveRemove"], "every row must expose a delete control"
    assert all(g == "×" for g in res["glyphs"]), (
        f"delete affordance must be × not an ellipsis, got {res['glyphs']}")
    assert res["deleted"] == ["bad_field"], (
        f"tombstone must carry the removed key, got {res}")
    # save → effective_facts drops the field
    page.evaluate("() => pdSaveFacts()")
    page.wait_for_timeout(1200)
    eff = page.evaluate(
        "() => fetch('/api/product_info/' + encodeURIComponent(_pdFolder))"
        ".then(r => r.json()).then(d => d.effective_facts)")
    assert "bad_field" not in eff, "deleted field must leave the effective view"
    assert eff["battery_capacity"]["value"] == "680mAh"


def test_add_fact_is_chip_style(_ctx):
    """เพิ่มข้อมูล is an inline chip-style input — Enter with a field name
    creates ONE prefilled row; empty Enter creates none; no stacked
    empty forms."""
    page = _ctx["page"]
    _open_pd(page, _ctx["product"])
    page.evaluate("() => pdEditFacts()")
    page.wait_for_timeout(800)
    res = page.evaluate("""() => {
      const addBtn = [...document.querySelectorAll('#pd-body button')]
        .find(b => b.textContent.includes('เพิ่มข้อมูล'));
      const input = document.querySelector('#pd-body .pillar-keyword-add input');
      const before = document.querySelectorAll('#pd-facts-container .pd-fact-row').length;
      return {hasOldButton: !!addBtn, hasInput: !!input, before};
    }""")
    assert res["hasInput"], "add control must be an inline chip-style input"
    assert not res["hasOldButton"], "no separate + เพิ่มข้อมูล button"
    # empty Enter → no row
    page.evaluate("""() => {
      const i = document.querySelector('#pd-body .pillar-keyword-add input');
      i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    }""")
    assert page.evaluate(
        "() => document.querySelectorAll('#pd-facts-container .pd-fact-row').length"
    ) == res["before"]
    # Enter with a name → one prefilled row
    page.evaluate("""() => {
      const i = document.querySelector('#pd-body .pillar-keyword-add input');
      i.value = 'จอแสดงผล';
      i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    }""")
    page.wait_for_timeout(200)
    after = page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#pd-facts-container .pd-fact-row')];
      const last = rows[rows.length - 1];
      return {count: rows.length,
              label: last.querySelector('.pd-fact-label').value,
              inputCleared: document.querySelector('#pd-body .pillar-keyword-add input').value === ''};
    }""")
    assert after["count"] == res["before"] + 1
    assert after["label"] == "จอแสดงผล"
    assert after["inputCleared"], "chip input clears after adding"


def test_fact_field_relabel_rekeys_effective(_ctx):
    """Editing a field TITLE rekeys the fact — the wrong key is tombstoned
    and the corrected label appears in the effective view (not just the
    value being editable)."""
    page = _ctx["page"]
    _open_pd(page, _ctx["product"])
    page.evaluate("() => pdEditFacts()")
    page.wait_for_timeout(800)
    res = page.evaluate("""() => {
      const row = [...document.querySelectorAll('#pd-facts-container .pd-fact-row')]
        .find(r => r.getAttribute('data-key') === 'battery_capacity');
      if (!row) return {found: false};
      const labelInput = row.querySelector('.pd-fact-label');
      if (!labelInput) return {found: true, editableLabel: false};
      labelInput.value = 'Battery Capacity (mAh)';
      const state = _pdGetFactsState();
      return {found: true, editableLabel: true,
              facts: state.facts, deleted: state.deleted_facts};
    }""")
    assert res.get("editableLabel"), "field title must be an editable input"
    assert res["deleted"] == ["battery_capacity"], (
        f"renamed field must tombstone its old key, got {res}")
    assert res["facts"].get("Battery Capacity (mAh)") == "680mAh", (
        f"value must be carried under the new label, got {res}")
    page.evaluate("() => pdSaveFacts()")
    page.wait_for_timeout(1200)
    eff = page.evaluate(
        "() => fetch('/api/product_info/' + encodeURIComponent(_pdFolder))"
        ".then(r => r.json()).then(d => d.effective_facts)")
    assert "battery_capacity" not in eff, "old key must leave the effective view"
    assert eff["Battery Capacity (mAh)"]["value"] == "680mAh"
    # Deleting `bad_field` in the earlier test must survive THIS save —
    # tombstones accumulate, they are not overwritten by the next edit.
    assert "bad_field" not in eff, (
        "a prior field deletion must persist across later saves")
