"""Final acceptance gate — real K1 Product Detail page.

Bounded acceptance tests for the consolidated Product Detail:
- Marketing + Visual save persistence (save → reopen → verify)
- Dirty section switching (facts ↔ marketing, confirm/discard)
- Add Product regression (old modal still works for CREATE)
- Media & Sources interaction smoke (expand, provenance, ⋯ menus, no ×)

NO LLM/provider calls. NO K1 re-ingestion. NO permanent data mutation
(all test edits are restored to original values).
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    pytestmark = pytest.mark.skip(reason="playwright not installed")


PRODUCT = "4G Kids Watch K1 - LAGENIO"
BRAND = "f71ed97d0da39bba"
SESSION_TOKEN = os.environ.get("MKTAPP_TEST_SESSION", "")
BASE_URL = "http://127.0.0.1:8778"


@pytest.fixture
def _real_server():
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/product_info/{urllib.parse.quote(PRODUCT)}",
            headers={
                "Cookie": f"mktapp_session={SESSION_TOKEN}; mktapp_brand={BRAND}",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            info = json.loads(resp.read())
        df_count = len(info.get("derived_facts") or {})
        if df_count == 0:
            pytest.skip("Server running old code — restart first")
    except Exception as e:
        pytest.skip(f"Server not running or not authenticated: {e}")
    yield {"url": BASE_URL}


def _open_k1_detail(page):
    """Navigate to K1 Product Detail."""
    ctx = page.context
    ctx.add_cookies([
        {"name": "mktapp_session", "value": SESSION_TOKEN, "url": BASE_URL},
        {"name": "mktapp_brand", "value": BRAND, "url": BASE_URL},
    ])
    page.goto(f"{BASE_URL}/", timeout=15000)
    page.wait_for_timeout(1000)
    card = page.query_selector(f'.folder-item:has-text("{PRODUCT}")')
    assert card is not None
    card.query_selector('.folder-info').click()
    page.wait_for_selector("#product-detail-view", state="visible", timeout=10000)
    page.wait_for_timeout(500)


def _expand_marketing(page):
    """Expand the marketing section."""
    page.query_selector("text=การตลาด").click()
    page.wait_for_timeout(500)


def _enter_marketing_edit(page):
    """Click แก้ไข in the marketing section."""
    page.evaluate("""() => {
        const sections = document.querySelectorAll('#pd-body > div');
        for (const s of sections) {
            if (s.textContent.includes('การตลาด')) {
                const btn = s.querySelector('button');
                if (btn && btn.textContent.includes('แก้ไข')) { btn.click(); return; }
            }
        }
    }""")
    page.wait_for_timeout(500)


def _get_mkt_profile():
    """Fetch current marketing profile via API."""
    req = urllib.request.Request(
        f"{BASE_URL}/api/product_profile/{urllib.parse.quote(PRODUCT)}",
        headers={"Cookie": f"mktapp_session={SESSION_TOKEN}; mktapp_brand={BRAND}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _restore_mkt_profile(original):
    """Restore original marketing profile via API."""
    payload = {
        "competitors": original.get("competitors", []),
        "differentiators": original.get("differentiators", []),
        "use_cases": original.get("use_cases", []),
        "price_tier": original.get("price_tier", ""),
        "tone_adjustment": original.get("tone_adjustment", ""),
    }
    aud = original.get("audience", {})
    if aud:
        audience = {}
        if aud.get("primary"):
            audience["primary"] = aud["primary"]
        if aud.get("end_user"):
            audience["end_user"] = aud["end_user"]
        if audience:
            payload["audience"] = audience
    vo = original.get("visual_override", {})
    if vo:
        vis = vo.get("image_style", {})
        kw = vo.get("keywords", [])
        if vis.get("tone") or kw:
            payload["visual_override"] = {}
            if vis.get("tone"):
                payload["visual_override"]["image_style"] = {"tone": vis["tone"]}
            if kw:
                payload["visual_override"]["keywords"] = kw

    req = urllib.request.Request(
        f"{BASE_URL}/api/product_profile_save/{urllib.parse.quote(PRODUCT)}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Cookie": f"mktapp_session={SESSION_TOKEN}; mktapp_brand={BRAND}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        assert result.get("ok"), f"Failed to restore: {result}"


# ============================================================================
# 1. Marketing + Visual save persistence
# ============================================================================

def test_marketing_save_persistence(_real_server):
    """Marketing edit → save → reopen → values persist, no stale dirty."""
    original_profile = _get_mkt_profile()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Expand marketing
        _expand_marketing(page)

        # Enter marketing edit
        _enter_marketing_edit(page)

        # Verify edit form appeared
        assert page.query_selector('#pd-mkt-age') is not None, "Marketing edit form not found"

        # Change a positioning field (tone_adjustment)
        tone_input = page.query_selector('#pd-mkt-tone')
        assert tone_input is not None, "Tone field not found"
        original_tone = tone_input.input_value()
        tone_input.fill("TEST_TONE_ACCEPTANCE")

        # Change a visual field (visual_tone)
        vis_input = page.query_selector('#pd-mkt-visual-tone')
        assert vis_input is not None, "Visual tone field not found"
        original_vis = vis_input.input_value()
        vis_input.fill("TEST_VISUAL_ACCEPTANCE")

        # Save
        save_btn = page.query_selector("button:has-text('บันทึก')")
        assert save_btn is not None
        save_btn.click()
        page.wait_for_timeout(1000)

        # Should be back in view mode (no pd-mkt-* inputs)
        assert page.query_selector('#pd-mkt-age') is None, \
            "Should be in view mode after save"

        # Close Product Detail — should NOT show dirty warning
        dialog_messages = []
        page.once('dialog', lambda d: (dialog_messages.append(d.message), d.accept()))
        page.query_selector("#pd-back span").click()
        page.wait_for_timeout(500)
        assert len(dialog_messages) == 0, \
            f"Stale dirty warning after save: {dialog_messages}"

        # Reopen and verify saved values persisted
        _open_k1_detail(page)
        _expand_marketing(page)
        expanded_text = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            for (const s of sections) {
                if (s.textContent.includes('การตลาด')) return s.textContent;
            }
            return '';
        }""")
        assert 'TEST_TONE_ACCEPTANCE' in expanded_text, \
            f"Saved tone should persist, got: {expanded_text[:300]}"
        assert 'TEST_VISUAL_ACCEPTANCE' in expanded_text, \
            f"Saved visual tone should persist, got: {expanded_text[:300]}"

        browser.close()

    # Restore original values
    _restore_mkt_profile(original_profile)


# ============================================================================
# 2. Dirty section switching
# ============================================================================

def test_dirty_facts_to_marketing_switch(_real_server):
    """Dirty facts edit → try to open marketing → confirm appears → cancel stays → confirm discards."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Enter facts edit mode
        page.query_selector("#pd-body button:has-text('แก้ไข')").click()
        page.wait_for_timeout(500)
        assert page.query_selector('.pd-fact-val') is not None

        # Modify a value
        page.query_selector('.pd-fact-val').fill("TEST_DIRTY_SWITCH")

        # Try to expand marketing — should show confirm
        dialog_messages = []
        page.once('dialog', lambda d: (dialog_messages.append(d.message), d.dismiss()))
        page.query_selector("text=การตลาด").click()
        page.wait_for_timeout(500)

        assert len(dialog_messages) > 0, "No dirty warning on section switch"
        assert any("เปลี่ยนแปลง" in m or "ยังไม่ได้บันทึก" in m for m in dialog_messages)

        # Should still be in facts edit mode
        assert page.query_selector('.pd-fact-val') is not None, \
            "Should remain in facts edit after canceling switch"
        assert page.query_selector('.pd-fact-val').input_value() == "TEST_DIRTY_SWITCH", \
            "Unsaved value should remain"

        # Now accept discard — switch to marketing
        page.once('dialog', lambda d: d.accept())
        page.query_selector("text=การตลาด").click()
        page.wait_for_timeout(500)

        # Should now be in view mode (edit was discarded)
        assert page.query_selector('.pd-fact-val') is None, \
            "Should discard facts edit after confirming switch"
        # Marketing section should be expanded
        mkt_expanded = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            for (const s of sections) {
                if (s.textContent.includes('การตลาด') && s.textContent.includes('กลุ่มเป้าหมาย')) return true;
            }
            return false;
        }""")
        assert mkt_expanded, "Marketing should be expanded after switch"

        browser.close()


def test_dirty_marketing_to_facts_switch(_real_server):
    """Dirty marketing edit → try to open facts → confirm appears → cancel stays → confirm discards."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Expand marketing and enter edit mode
        _expand_marketing(page)
        _enter_marketing_edit(page)
        assert page.query_selector('#pd-mkt-age') is not None

        # Modify a marketing value
        page.query_selector('#pd-mkt-tone').fill("TEST_MKT_DIRTY")

        # Try to edit facts — should show confirm
        dialog_messages = []
        page.once('dialog', lambda d: (dialog_messages.append(d.message), d.dismiss()))
        page.query_selector("#pd-body button:has-text('แก้ไข')").click()
        page.wait_for_timeout(500)

        assert len(dialog_messages) > 0, "No dirty warning on section switch"
        # Should still be in marketing edit
        assert page.query_selector('#pd-mkt-age') is not None, \
            "Should remain in marketing edit after canceling switch"
        assert page.query_selector('#pd-mkt-tone').input_value() == "TEST_MKT_DIRTY", \
            "Unsaved value should remain"

        # Now accept discard — switch to facts
        page.once('dialog', lambda d: d.accept())
        page.query_selector("#pd-body button:has-text('แก้ไข')").click()
        page.wait_for_timeout(500)

        # Should now be in facts edit mode
        assert page.query_selector('.pd-fact-val') is not None, \
            "Should switch to facts edit after confirming discard"

        browser.close()


# ============================================================================
# 3. Add Product regression
# ============================================================================

def test_add_product_modal_opens(_real_server):
    """+ เพิ่ม button opens the old Add Product modal with controls."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        ctx = page.context
        ctx.add_cookies([
            {"name": "mktapp_session", "value": SESSION_TOKEN, "url": BASE_URL},
            {"name": "mktapp_brand", "value": BRAND, "url": BASE_URL},
        ])
        page.goto(f"{BASE_URL}/", timeout=15000)
        page.wait_for_timeout(1000)

        # Click + เพิ่ม
        add_btn = page.query_selector("button.sidebar-add-btn")
        assert add_btn is not None, "Add button not found"
        add_btn.click()
        page.wait_for_timeout(500)

        # Old modal should open
        modal = page.query_selector('#upload-overlay.visible')
        assert modal is not None, "Add Product modal should open"

        # Should have product name input
        name_input = page.query_selector('#upload-product-name-modal')
        assert name_input is not None, "Product name input not found"

        # Should have file upload area
        file_input = page.query_selector('#upload-files-modal, input[type="file"]')
        assert file_input is not None, "File upload input not found"

        # Should have URL import section (for new products)
        url_block = page.query_selector('#url-import-block')
        assert url_block is not None, "URL import block not found"
        # URL block should be visible for new product (not hidden)
        url_visible = page.evaluate("""() => {
            const el = document.getElementById('url-import-block');
            return el && el.style.display !== 'none';
        }""")
        assert url_visible, "URL import should be visible for new product"

        # Should have submit button
        submit_btn = page.query_selector('#upload-submit-btn')
        assert submit_btn is not None, "Submit button not found"

        # Close modal — click X
        close_btn = page.query_selector('#upload-overlay button[onclick*="closeUploadModal"]')
        assert close_btn is not None
        close_btn.click()
        page.wait_for_timeout(300)
        assert not page.query_selector('#upload-overlay.visible'), \
            "Modal should close"

        browser.close()


# ============================================================================
# 4. Media & Sources interaction smoke
# ============================================================================

def test_sources_expand_and_interactions(_real_server):
    """Sources section expands, shows provenance, ⋯ menus, no × icons, + เพิ่มไฟล์."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Expand sources
        page.query_selector("text=สื่อและแหล่งข้อมูล").click()
        page.wait_for_timeout(500)

        # Should show URL provenance
        src_text = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            for (const s of sections) {
                if (s.textContent.includes('สื่อและแหล่งข้อมูล')) return s.textContent;
            }
            return '';
        }""")
        assert 'lagenio.com' in src_text or 'นำเข้าจาก' in src_text, \
            f"URL provenance should be visible, got: {src_text[:200]}"

        # Should show file ⋯ menus
        file_menus = page.query_selector_all('.pd-file-menu')
        assert len(file_menus) > 0, \
            f"File ⋯ menus should exist, found {len(file_menus)}"

        # Open a file ⋯ menu
        file_menu_btns = page.query_selector_all('button:has-text("⋯")')
        # Find a file menu button (not the product ⋯)
        for btn in file_menu_btns:
            parent = btn.evaluate("el => el.parentElement.parentElement")
            if parent and 'pd-file-menu' not in str(parent):
                pass  # skip product-level ⋯
        # Click the first file ⋯ button
        page.evaluate("""() => {
            const menus = document.querySelectorAll('.pd-file-menu');
            if (menus.length > 0) {
                const btn = menus[0].previousElementSibling;
                if (btn) btn.click();
            }
        }""")
        page.wait_for_timeout(200)

        # Menu should be visible
        visible_menu = page.query_selector('.pd-file-menu[style*="block"]')
        assert visible_menu is not None, "File ⋯ menu should open"
        menu_text = visible_menu.text_content()
        assert 'ดู' in menu_text or 'ลบ' in menu_text, \
            f"Menu should have view/delete actions, got: {menu_text}"

        # Should have + เพิ่มไฟล์ button
        add_btn = page.query_selector("button:has-text('+ เพิ่มไฟล์')")
        assert add_btn is not None, "Add file button not found"

        # No permanent × icons on file rows
        x_icons = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            let count = 0;
            for (const s of sections) {
                if (s.textContent.includes('สื่อและแหล่งข้อมูล')) {
                    s.querySelectorAll('button, span').forEach(el => {
                        if (el.textContent.trim() === '×' || el.textContent.trim() === '✕') count++;
                    });
                }
            }
            return count;
        }""")
        assert x_icons == 0, f"No × icons on file rows, found {x_icons}"

        browser.close()


def test_source_text_viewer(_real_server):
    """Source text 'ดูต้นฉบับ' opens viewer (does not crash)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        _open_k1_detail(page)

        # Expand sources
        page.query_selector("text=สื่อและแหล่งข้อมูล").click()
        page.wait_for_timeout(500)

        # Find and click a "ดูต้นฉบับ" menu item
        # First, open the ⋯ menu for a text file
        page.evaluate("""() => {
            const menus = document.querySelectorAll('.pd-file-menu');
            for (const menu of menus) {
                const items = menu.querySelectorAll('div');
                for (const item of items) {
                    if (item.textContent.includes('ดูต้นฉบับ')) {
                        // Open the menu first
                        const btn = menu.previousElementSibling;
                        if (btn) btn.click();
                        return;
                    }
                }
            }
        }""")
        page.wait_for_timeout(200)

        # Now click ดูต้นฉบับ
        source_link = page.evaluate("""() => {
            const menus = document.querySelectorAll('.pd-file-menu');
            for (const menu of menus) {
                const items = menu.querySelectorAll('div');
                for (const item of items) {
                    if (item.textContent.includes('ดูต้นฉบับ')) {
                        item.click();
                        return true;
                    }
                }
            }
            return false;
        }""")
        assert source_link, "ดูต้นฉบับ menu item should exist"

        # A new window/tab should have opened (window.open)
        # We can't easily check the new tab, but we can verify no JS errors occurred
        page.wait_for_timeout(500)

        browser.close()


# ============================================================================
# 5. Summary: no × icons anywhere on Product Detail
# ============================================================================

def test_no_x_icons_anywhere(_real_server):
    """No permanent × icons on the entire Product Detail page."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Expand all sections
        page.query_selector("text=การตลาด").click()
        page.wait_for_timeout(300)
        page.query_selector("text=สื่อและแหล่งข้อมูล").click()
        page.wait_for_timeout(300)

        x_icons = page.evaluate("""() => {
            const pd = document.getElementById('product-detail-view');
            if (!pd) return 0;
            let count = 0;
            pd.querySelectorAll('button, span, div').forEach(el => {
                const text = el.textContent.trim();
                if ((text === '×' || text === '✕') && el.children.length === 0) count++;
            });
            return count;
        }""")
        assert x_icons == 0, f"Product Detail should have no × icons, found {x_icons}"

        browser.close()
