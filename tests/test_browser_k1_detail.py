"""Browser DOM test — real K1 Product Detail view.

Verifies the real K1 product's Product Detail page:
- Opens into view mode (not edit form)
- Product Information shows compact subset with expand option
- Marketing section is collapsed/expandable with edit mode
- Sources section is collapsed with file management
- ⋯ menu for product delete
- Per-section dirty tracking
- No permanent X icons
- Old Manage modal not used for existing products
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


PRODUCT = "4G Kids Watch K1 - LAGENIO"
BRAND = "f71ed97d0da39bba"
SESSION_TOKEN = os.environ.get("MKTAPP_TEST_SESSION", "")
BASE_URL = "http://127.0.0.1:8778"


@pytest.fixture
def _real_server():
    """Use the ALREADY-RUNNING real web_viewer server."""
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
            pytest.skip(
                "Server is running but K1 has no derived_facts — "
                "server may be running old code. Restart the server first."
            )
    except Exception as e:
        pytest.skip(f"Server not running or not authenticated: {e}")
    yield {"url": BASE_URL}


def _open_k1_detail(page):
    """Open the K1 Product Detail view via product name click."""
    ctx = page.context
    ctx.add_cookies([
        {"name": "mktapp_session", "value": SESSION_TOKEN, "url": BASE_URL},
        {"name": "mktapp_brand", "value": BRAND, "url": BASE_URL},
    ])
    page.goto(f"{BASE_URL}/", timeout=15000)
    page.wait_for_timeout(1000)
    product_card = page.query_selector(f'.folder-item:has-text("{PRODUCT}")')
    assert product_card is not None, f"Product card for '{PRODUCT}' not found"
    info_area = product_card.query_selector('.folder-info')
    assert info_area is not None, "Product info area not found"
    info_area.click()
    page.wait_for_selector("#product-detail-view", state="visible", timeout=10000)
    page.wait_for_timeout(500)


def _open_k1_detail_via_gear(page):
    """Open the K1 Product Detail view via ⚙ button."""
    ctx = page.context
    ctx.add_cookies([
        {"name": "mktapp_session", "value": SESSION_TOKEN, "url": BASE_URL},
        {"name": "mktapp_brand", "value": BRAND, "url": BASE_URL},
    ])
    page.goto(f"{BASE_URL}/", timeout=15000)
    page.wait_for_timeout(1000)
    product_card = page.query_selector(f'.folder-item:has-text("{PRODUCT}")')
    assert product_card is not None
    gear_btn = product_card.query_selector('.folder-manage-btn')
    assert gear_btn is not None, "Gear button not found"
    gear_btn.click()
    page.wait_for_selector("#product-detail-view", state="visible", timeout=10000)
    page.wait_for_timeout(500)


def test_real_k1_opens_into_view_mode(_real_server):
    """Product Detail opens into READABLE view mode, not an edit form."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # No edit inputs in view mode
        edit_inputs = page.query_selector_all('.pd-fact-val')
        assert len(edit_inputs) == 0, \
            f"View mode should not have edit inputs, found {len(edit_inputs)}"
        fact_rows = page.query_selector_all('.pd-fact-row')
        assert len(fact_rows) == 0, \
            f"View mode should not have .pd-fact-row elements, found {len(fact_rows)}"

        # Product name visible
        name_el = page.query_selector('#pd-name')
        assert name_el is not None
        assert PRODUCT in name_el.text_content()

        browser.close()


def test_real_k1_gear_opens_product_detail(_real_server):
    """⚙ button opens Product Detail, NOT the old modal."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail_via_gear(page)

        # Product Detail should be visible
        pd_view = page.query_selector('#product-detail-view')
        assert pd_view is not None
        assert pd_view.is_visible(), "Product Detail should be visible after ⚙ click"

        # Old modal should NOT be visible
        modal = page.query_selector('#upload-overlay')
        if modal:
            assert not modal.is_visible() or 'visible' not in (modal.get_attribute('class') or ''), \
                "Old modal should NOT open when clicking ⚙ on existing product"

        browser.close()


def test_real_k1_compact_facts_visible(_real_server):
    """Important K1 facts visible in compact view; not all 10 forced visible."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Count visible fact rows — exclude section header (has 'ข้อมูลสินค้า' text)
        fact_rows = page.evaluate("""() => {
            const rows = [];
            document.querySelectorAll('#pd-body > div').forEach(section => {
                const text = section.textContent || '';
                if (text.includes('ข้อมูลสินค้า')) {
                    section.querySelectorAll('div').forEach(d => {
                        const style = window.getComputedStyle(d);
                        if (style.display === 'flex' && style.justifyContent === 'space-between'
                            && !d.textContent.includes('ข้อมูลสินค้า') && !d.textContent.includes('ดูข้อมูลทั้งหมด')) {
                            rows.push(d.textContent);
                        }
                    });
                }
            });
            return rows;
        }""")

        assert len(fact_rows) <= 5, \
            f"Compact view should show ≤5 facts, got {len(fact_rows)}"
        assert len(fact_rows) > 0, "Should show at least some facts"

        # Expand link should exist for 10 facts
        expand_link = page.query_selector("text=ดูข้อมูลทั้งหมด")
        assert expand_link is not None, "Expand link should be present for 10 facts"

        browser.close()


def test_real_k1_expand_facts(_real_server):
    """Clicking 'ดูข้อมูลทั้งหมด' reveals all facts."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        expand_link = page.query_selector("text=ดูข้อมูลทั้งหมด")
        assert expand_link is not None
        expand_link.click()
        page.wait_for_timeout(500)

        fact_rows = page.evaluate("""() => {
            const rows = [];
            document.querySelectorAll('#pd-body > div').forEach(section => {
                const text = section.textContent || '';
                if (text.includes('ข้อมูลสินค้า')) {
                    section.querySelectorAll('div').forEach(d => {
                        const style = window.getComputedStyle(d);
                        if (style.display === 'flex' && style.justifyContent === 'space-between'
                            && !d.textContent.includes('ข้อมูลสินค้า') && !d.textContent.includes('ดูข้อมูลทั้งหมด')) {
                            rows.push(d.textContent);
                        }
                    });
                }
            });
            return rows;
        }""")
        assert len(fact_rows) == 10, \
            f"Expanded view should show all 10 facts, got {len(fact_rows)}"

        browser.close()


def test_real_k1_marketing_collapsed(_real_server):
    """Marketing section is collapsed by default."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        mkt_text = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            for (const s of sections) {
                if (s.textContent.includes('การตลาด')) return s.textContent;
            }
            return '';
        }""")
        assert 'กลุ่มเป้าหมาย' in mkt_text or 'จุดขาย' in mkt_text or 'คู่แข่ง' in mkt_text, \
            f"Marketing should show summary text, got: {mkt_text[:100]}"
        # No marketing input fields in collapsed mode
        mkt_inputs = page.query_selector_all('#pd-body input[id^="pd-mkt-"]')
        assert len(mkt_inputs) == 0, \
            f"Marketing should not have input fields in collapsed mode, found {len(mkt_inputs)}"

        browser.close()


def test_real_k1_sources_collapsed(_real_server):
    """Sources section is collapsed by default."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        src_text = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            for (const s of sections) {
                if (s.textContent.includes('สื่อและแหล่งข้อมูล')) return s.textContent;
            }
            return '';
        }""")
        assert 'ไฟล์' in src_text or 'URL' in src_text or 'รูป' in src_text, \
            f"Sources should show file count summary, got: {src_text[:100]}"

        browser.close()


def test_real_k1_facts_edit_mode(_real_server):
    """Clicking 'แก้ไข' on facts section enters edit mode with inputs."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Click edit button on facts section
        edit_btn = page.query_selector("#pd-body button:has-text('แก้ไข')")
        assert edit_btn is not None, "Edit button not found"
        edit_btn.click()
        page.wait_for_timeout(500)

        # Edit mode should have .pd-fact-val inputs
        edit_inputs = page.query_selector_all('.pd-fact-val')
        assert len(edit_inputs) > 0, \
            f"Edit mode should have input fields, found {len(edit_inputs)}"

        # Should have save/cancel buttons
        save_btn = page.query_selector("button:has-text('บันทึก')")
        cancel_btn = page.query_selector("button:has-text('ยกเลิก')")
        assert save_btn is not None, "Save button not found"
        assert cancel_btn is not None, "Cancel button not found"

        # Derived facts should NOT have delete buttons (⋯)
        delete_btns = page.evaluate("""() => {
            let count = 0;
            document.querySelectorAll('.pd-fact-row').forEach(row => {
                const derivedVal = row.getAttribute('data-derived-value');
                if (derivedVal && derivedVal.trim()) {
                    const delBtn = row.querySelector('button[title*="ลบ"]');
                    if (delBtn) count++;
                }
            });
            return count;
        }""")
        assert delete_btns == 0, \
            f"Derived facts should not have delete buttons, found {delete_btns}"

        browser.close()


def test_real_k1_facts_add_remove(_real_server):
    """Custom manual facts can be added and removed in edit mode."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Enter facts edit mode
        edit_btn = page.query_selector("#pd-body button:has-text('แก้ไข')")
        edit_btn.click()
        page.wait_for_timeout(500)

        # Add custom fact
        add_btn = page.query_selector("button:has-text('+ เพิ่มข้อมูล')")
        assert add_btn is not None
        add_btn.click()
        page.wait_for_timeout(200)

        new_rows = page.query_selector_all('.pd-fact-row')
        assert len(new_rows) > 10, \
            f"Expected >10 rows after adding, got {len(new_rows)}"

        # New row should have delete button
        last_row = new_rows[-1]
        del_btn = last_row.query_selector('button[title*="ลบ"]')
        assert del_btn is not None, "New custom fact should have delete button"

        # Remove it
        del_btn.click()
        page.wait_for_timeout(200)
        remaining = page.query_selector_all('.pd-fact-row')
        assert len(remaining) == 10, \
            f"Expected 10 rows after removing, got {len(remaining)}"

        browser.close()


def test_real_k1_marketing_edit_mode(_real_server):
    """Marketing section can be expanded and edited."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Expand marketing section
        mkt_header = page.query_selector("text=การตลาด")
        assert mkt_header is not None
        mkt_header.click()
        page.wait_for_timeout(500)

        # Should show readable marketing fields
        mkt_text = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            for (const s of sections) {
                if (s.textContent.includes('การตลาด')) return s.textContent;
            }
            return '';
        }""")
        assert 'กลุ่มเป้าหมาย' in mkt_text or 'คู่แข่ง' in mkt_text, \
            f"Expanded marketing should show readable fields, got: {mkt_text[:200]}"

        # Should have an "แก้ไข" button within the marketing section
        mkt_edit_btn = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            for (const s of sections) {
                if (s.textContent.includes('การตลาด')) {
                    const btn = s.querySelector('button');
                    if (btn && btn.textContent.includes('แก้ไข')) return true;
                }
            }
            return false;
        }""")
        assert mkt_edit_btn, "Marketing section should have an edit button when expanded"

        # Click the marketing edit button
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

        # Marketing edit mode should have pd-mkt-* inputs
        mkt_inputs = page.query_selector_all('input[id^="pd-mkt-"], select[id^="pd-mkt-"], textarea[id^="pd-mkt-"]')
        assert len(mkt_inputs) > 0, \
            f"Marketing edit should have input fields, found {len(mkt_inputs)}"

        # Should have save/cancel buttons
        save_btn = page.query_selector("button:has-text('บันทึก')")
        cancel_btn = page.query_selector("button:has-text('ยกเลิก')")
        assert save_btn is not None, "Save button not found in marketing edit"
        assert cancel_btn is not None, "Cancel button not found in marketing edit"

        browser.close()


def test_real_k1_sources_expanded(_real_server):
    """Sources section shows files with ⋯ menus when expanded."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Expand sources section
        src_header = page.query_selector("text=สื่อและแหล่งข้อมูล")
        assert src_header is not None
        src_header.click()
        page.wait_for_timeout(500)

        # Should show file rows with ⋯ buttons
        file_menus = page.query_selector_all('.pd-file-menu')
        assert len(file_menus) > 0, \
            f"Expanded sources should show file ⋯ menus, found {len(file_menus)}"

        # Should have + เพิ่มไฟล์ button
        add_btn = page.query_selector("button:has-text('+ เพิ่มไฟล์')")
        assert add_btn is not None, "Add file button not found"

        # Should NOT have permanent × icons on file rows
        x_icons = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            let count = 0;
            for (const s of sections) {
                if (s.textContent.includes('สื่อและแหล่งข้อมูล')) {
                    s.querySelectorAll('button').forEach(b => {
                        if (b.textContent.trim() === '×' || b.textContent.trim() === '✕') count++;
                    });
                }
            }
            return count;
        }""")
        assert x_icons == 0, f"File rows should not have × icons, found {x_icons}"

        browser.close()


def test_real_k1_product_menu(_real_server):
    """⋯ menu opens dropdown with ลบสินค้า (destructive)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Click ⋯ button
        menu_btn = page.query_selector("button:has-text('⋯')")
        assert menu_btn is not None, "⋯ button not found"
        menu_btn.click()
        page.wait_for_timeout(200)

        # Menu should be visible
        menu = page.query_selector('#pd-menu')
        assert menu is not None, "Menu not found"
        assert menu.is_visible(), "Menu should be visible"

        # Should contain ลบสินค้า
        menu_text = menu.text_content()
        assert 'ลบสินค้า' in menu_text, f"Menu should contain ลบสินค้า, got: {menu_text}"

        # Click outside to close
        page.click('#pd-name')
        page.wait_for_timeout(200)
        assert not menu.is_visible(), "Menu should close on outside click"

        browser.close()


def test_real_k1_dirty_facts_warning(_real_server):
    """Dirty facts edit + navigate back → warning appears."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Enter facts edit mode
        edit_btn = page.query_selector("#pd-body button:has-text('แก้ไข')")
        edit_btn.click()
        page.wait_for_timeout(500)

        # Make a change
        fact_val = page.query_selector('.pd-fact-val')
        fact_val.fill("TEST_DIRTY")

        # Navigate back — should show warning
        dialog_messages = []
        page.once('dialog', lambda d: (dialog_messages.append(d.message), d.dismiss()))
        page.query_selector("#pd-back span").click()
        page.wait_for_timeout(500)

        assert len(dialog_messages) > 0, \
            "No dirty warning when navigating away"
        assert any("ยังไม่ได้บันทึก" in msg or "เปลี่ยนแปลง" in msg for msg in dialog_messages), \
            f"Expected unsaved-changes warning, got: {dialog_messages}"

        # Cancel — accept to discard
        page.once('dialog', lambda d: d.accept())
        page.query_selector("button:has-text('ยกเลิก')").click()
        page.wait_for_timeout(1000)

        # Back in view mode
        assert len(page.query_selector_all('.pd-fact-val')) == 0, \
            "Should be back in view mode"

        browser.close()


def test_real_k1_dirty_marketing_warning(_real_server):
    """Dirty marketing edit + navigate back → warning appears."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Expand marketing and enter edit mode
        page.query_selector("text=การตลาด").click()
        page.wait_for_timeout(500)
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

        # Make a change
        mkt_input = page.query_selector('#pd-mkt-age')
        if mkt_input:
            mkt_input.fill("TEST_MKT_DIRTY")

        # Navigate back — should show warning
        dialog_messages = []
        page.once('dialog', lambda d: (dialog_messages.append(d.message), d.dismiss()))
        page.query_selector("#pd-back span").click()
        page.wait_for_timeout(500)

        assert len(dialog_messages) > 0, \
            "No dirty warning when navigating away from marketing edit"

        # Cancel — accept to discard
        page.once('dialog', lambda d: d.accept())
        page.query_selector("button:has-text('ยกเลิก')").click()
        page.wait_for_timeout(1000)

        browser.close()


def test_real_k1_save_facts_returns_to_view(_real_server):
    """Save facts persists and returns to clean view mode."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Enter facts edit mode
        edit_btn = page.query_selector("#pd-body button:has-text('แก้ไข')")
        edit_btn.click()
        page.wait_for_timeout(500)

        # Make a change
        fact_val = page.query_selector('.pd-fact-val')
        original_val = fact_val.input_value()
        fact_val.fill("TEST_SAVE_PERSIST")

        # Save
        save_btn = page.query_selector("button:has-text('บันทึก')")
        save_btn.click()
        page.wait_for_timeout(1000)

        # Should be back in view mode
        assert len(page.query_selector_all('.pd-fact-val')) == 0, \
            "Should be back in view mode after save"

        # Close without warning
        dialog_messages = []
        page.once('dialog', lambda d: (dialog_messages.append(d.message), d.accept()))
        page.query_selector("#pd-back span").click()
        page.wait_for_timeout(500)
        assert len(dialog_messages) == 0, \
            f"Stale dirty warning after save: {dialog_messages}"

        # Reopen and verify saved value persisted
        _open_k1_detail(page)
        saved = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            for (const s of sections) {
                if (s.textContent.includes('ข้อมูลสินค้า')) {
                    return s.textContent;
                }
            }
            return '';
        }""")
        assert 'TEST_SAVE_PERSIST' in saved, \
            f"Saved value should persist, got: {saved[:200]}"

        # Restore original value
        _open_k1_detail(page)
        page.query_selector("#pd-body button:has-text('แก้ไข')").click()
        page.wait_for_timeout(500)
        page.query_selector('.pd-fact-val').fill(original_val)
        page.query_selector("button:has-text('บันทึก')").click()
        page.wait_for_timeout(1000)

        browser.close()


def test_real_k1_no_x_icons(_real_server):
    """No permanent × icons on the Product Detail page."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

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


def test_real_k1_no_old_modal_on_gear(_real_server):
    """⚙ button on existing product does NOT open old Manage modal."""
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

        product_card = page.query_selector(f'.folder-item:has-text("{PRODUCT}")')
        gear_btn = product_card.query_selector('.folder-manage-btn')
        gear_btn.click()
        page.wait_for_timeout(500)

        # Old modal should NOT be visible
        modal = page.query_selector('#upload-overlay')
        modal_visible = modal and modal.is_visible() and 'visible' in (modal.get_attribute('class') or '')
        assert not modal_visible, "Old Manage modal should NOT open for existing product"

        # Product Detail should be visible instead
        pd_view = page.query_selector('#product-detail-view')
        assert pd_view and pd_view.is_visible(), "Product Detail should open instead"

        browser.close()
