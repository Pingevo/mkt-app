"""Browser DOM test — real K1 Product Detail view gates.

Verifies the real K1 product's Product Detail page shows:
- Product Information before Marketing in DOM order
- Correct provenance badge (มาจาก URL)
- Dirty-state confirmation on close without save
- No stale confirmation after save
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
    """Open the K1 Product Detail view and wait for it to render."""
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
    assert info_area is not None
    info_area.click()
    page.wait_for_selector("#product-detail-view", state="visible", timeout=10000)
    page.wait_for_timeout(500)


def test_real_k1_product_information_before_marketing(_real_server):
    """Product Information must appear BEFORE Marketing in DOM order."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Get section order by checking element positions
        order = page.evaluate("""() => {
            const sections = [];
            document.querySelectorAll('#pd-body > div').forEach(d => {
                const text = d.textContent || '';
                if (text.includes('ข้อมูลสินค้า')) sections.push('product_info');
                if (text.includes('สรุปสินค้า')) sections.push('summary');
                if (text.includes('การตลาด')) sections.push('marketing');
                if (text.includes('สื่อและแหล่งข้อมูล')) sections.push('sources');
            });
            return sections;
        }""")
        print(f"Section order: {order}")
        info_idx = order.index('product_info') if 'product_info' in order else 999
        mkt_idx = order.index('marketing') if 'marketing' in order else 999
        assert info_idx < mkt_idx, \
            f"Product Information (idx={info_idx}) must appear BEFORE marketing (idx={mkt_idx}). Order: {order}"

        browser.close()


def test_real_k1_dirty_confirmation_on_edit(_real_server):
    """Editing a fact then navigating back must show confirmation."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Enter facts edit mode
        edit_btn = page.query_selector("#pd-body button:has-text('แก้ไข')")
        assert edit_btn is not None, "Edit button not found"
        edit_btn.click()
        page.wait_for_timeout(500)

        # Make a change
        fact_val = page.query_selector('.pd-fact-val')
        assert fact_val is not None, "No fact value input found"
        fact_val.fill("TEST_EDITED_VALUE")

        # Try to navigate back — should show confirmation
        dialog_messages = []
        def on_dialog(dialog):
            dialog_messages.append(dialog.message)
            dialog.dismiss()
        page.once('dialog', on_dialog)

        back_link = page.query_selector("#pd-back span")
        assert back_link is not None
        back_link.click()
        page.wait_for_timeout(500)

        assert len(dialog_messages) > 0, \
            "No confirmation dialog when closing with unsaved changes"
        assert any("ยังไม่ได้บันทึก" in msg or "เปลี่ยนแปลง" in msg for msg in dialog_messages), \
            f"Expected unsaved-changes confirmation, got: {dialog_messages}"

        # Product Detail should still be open
        pd_view = page.query_selector('#product-detail-view')
        assert pd_view and pd_view.is_visible(), \
            "Product Detail should still be open after canceling"

        browser.close()


def test_real_k1_no_confirmation_when_no_changes(_real_server):
    """Opening Product Detail and closing without changes must NOT show confirmation."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        dialog_messages = []
        page.once('dialog', lambda d: (dialog_messages.append(d.message), d.accept()))

        back_link = page.query_selector("#pd-back span")
        back_link.click()
        page.wait_for_timeout(500)

        assert len(dialog_messages) == 0, \
            f"Unexpected confirmation when no changes made: {dialog_messages}"

        # Product Detail should be closed
        pd_view = page.query_selector('#product-detail-view')
        assert pd_view and not pd_view.is_visible(), \
            "Product Detail should be closed"

        browser.close()


def test_real_k1_save_clears_dirty(_real_server):
    """After saving facts, reopening and closing must NOT show stale confirmation."""
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
        fact_val.fill("TEMP_SAVE_TEST")

        # Save
        save_btn = page.query_selector("button:has-text('บันทึก')")
        assert save_btn is not None
        save_btn.click()
        page.wait_for_timeout(1000)

        # Should be back in view mode
        assert len(page.query_selector_all('.pd-fact-val')) == 0, \
            "Should be in view mode after save"

        # Close — should NOT show stale confirmation
        dialog_messages = []
        page.once('dialog', lambda d: (dialog_messages.append(d.message), d.accept()))
        page.query_selector("#pd-back span").click()
        page.wait_for_timeout(500)
        assert len(dialog_messages) == 0, \
            f"Stale confirmation after save: {dialog_messages}"

        # Reopen and verify saved value persisted
        _open_k1_detail(page)
        page.query_selector("text=ดูข้อมูลทั้งหมด").click()  # expand facts
        page.wait_for_timeout(500)
        saved_text = page.evaluate("() => document.getElementById('pd-body').textContent")
        assert 'TEMP_SAVE_TEST' in saved_text, \
            f"Saved value should persist, got: {saved_text[:200]}"

        # Restore original value
        _open_k1_detail(page)
        page.query_selector("#pd-body button:has-text('แก้ไข')").click()
        page.wait_for_timeout(500)
        page.query_selector('.pd-fact-val').fill(original_val)
        page.query_selector("button:has-text('บันทึก')").click()
        page.wait_for_timeout(1000)

        browser.close()


def test_real_k1_provenance_badge_url(_real_server):
    """Provenance badge must show 'มาจาก URL' for URL-imported product."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        _open_k1_detail(page)

        # Expand facts to see all badges
        page.query_selector("text=ดูข้อมูลทั้งหมด").click()
        page.wait_for_timeout(500)

        badge_found = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            for (const s of sections) {
                if (s.textContent.includes('ข้อมูลสินค้า') && s.textContent.includes('มาจาก URL')) return true;
            }
            return false;
        }""")
        assert badge_found, "Expected 'มาจาก URL' provenance badge"
        badge_not_file = page.evaluate("""() => {
            const sections = document.querySelectorAll('#pd-body > div');
            for (const s of sections) {
                if (s.textContent.includes('ข้อมูลสินค้า') && s.textContent.includes('มาจากไฟล์')) return true;
            }
            return false;
        }""")
        assert not badge_not_file, "Should NOT show 'มาจากไฟล์' for URL product"

        browser.close()
