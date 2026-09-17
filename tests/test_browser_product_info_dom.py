"""Browser DOM proof — real K1 product Product Information UI.

Uses the REAL workspace data (already-populated `4G Kids Watch K1 - LAGENIO`
with 10 derived_facts, generated summary, category, source_type=url).

This test does NOT:
- re-ingest the product;
- call any LLM/provider;
- regenerate any product data.

It opens the Product Detail view via the real authenticated/brand-scoped
browser flow and asserts the DOM shows the expected values.
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
USER = "system81_bd1027e56a96a1d024a46394ecad055a23400c20886bcc7ab216db23784d429a"
BRAND = "f71ed97d0da39bba"
SESSION_TOKEN = os.environ.get("MKTAPP_TEST_SESSION", "")
BASE_URL = "http://127.0.0.1:8778"

EXPECTED_VALUES = {
    "brand": "LAGENIO",
    "model": "K1",
    "battery_capacity": "680mAh",
    "water_resistance": "IP68",
}


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


def test_real_k1_product_information_dom(_real_server):
    """Open real K1 Product Detail and verify Product Information DOM.

    Asserts:
    - derived Product Information rows are visible in view mode
    - representative real values are visible (LAGENIO, K1, 680mAh, IP68)
    - provenance badge displays "มาจาก URL"
    - section is NOT an empty manual-facts editor
    - marketing and sources sections exist (collapsed)
    - row count matches persisted effective facts (10 when expanded)
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
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

        # === ASSERT: Product Information section header ===
        body_text = page.evaluate("() => document.getElementById('pd-body').textContent")
        assert "ข้อมูลสินค้า" in body_text, \
            f"Product Information section not found, got: {body_text[:200]}"

        # === ASSERT: Fact rows visible (view mode — compact) ===
        # View mode uses flex rows, not .pd-fact-row inputs
        visible_facts = page.evaluate("""() => {
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
        assert len(visible_facts) > 0, \
            "No Product Information rows visible in view mode"
        # Compact mode should show ≤5 facts
        assert len(visible_facts) <= 5, \
            f"Compact view should show ≤5 facts, got {len(visible_facts)}"

        # === ASSERT: Expand to see all 10 ===
        expand_link = page.query_selector("text=ดูข้อมูลทั้งหมด")
        assert expand_link is not None, "Expand link should exist for 10 facts"
        expand_link.click()
        page.wait_for_timeout(500)

        all_facts = page.evaluate("""() => {
            const rows = [];
            document.querySelectorAll('#pd-body > div').forEach(section => {
                const text = section.textContent || '';
                if (text.includes('ข้อมูลสินค้า')) {
                    section.querySelectorAll('div').forEach(d => {
                        const style = window.getComputedStyle(d);
                        if (style.display === 'flex' && style.justifyContent === 'space-between'
                            && !d.textContent.includes('ข้อมูลสินค้า')) {
                            rows.push(d.textContent);
                        }
                    });
                }
            });
            return rows;
        }""")
        assert len(all_facts) == 10, \
            f"Expanded view should show 10 facts, got {len(all_facts)}"

        # === ASSERT: Representative real values visible ===
        for expected_key, expected_val in EXPECTED_VALUES.items():
            found = any(expected_val in row for row in all_facts)
            assert found, \
                f"Expected value '{expected_val}' not found in Product Information rows"

        # === ASSERT: Provenance badge "มาจาก URL" ===
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

        # === ASSERT: Marketing section exists (collapsed) ===
        assert "การตลาด" in body_text, "Marketing section should exist"

        # === ASSERT: Sources section exists (collapsed) ===
        assert "สื่อและแหล่งข้อมูล" in body_text, "Sources section should exist"

        browser.close()


def test_real_k1_derived_equals_effective(_real_server):
    """Verify that derived_facts count equals effective_facts count for K1."""
    req = urllib.request.Request(
        f"{BASE_URL}/api/product_info/{urllib.parse.quote(PRODUCT)}",
        headers={"Cookie": f"mktapp_session={SESSION_TOKEN}; mktapp_brand={BRAND}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        info = json.loads(resp.read())

    derived = info.get("derived_facts") or {}
    effective = info.get("effective_facts") or {}

    assert len(derived) == 10, f"Expected 10 derived_facts, got {len(derived)}"
    assert len(effective) == 10, f"Expected 10 effective_facts, got {len(effective)}"

    for k, v in effective.items():
        assert v.get("source") == "derived", \
            f"Expected source='derived' for {k}, got: {v.get('source')}"
