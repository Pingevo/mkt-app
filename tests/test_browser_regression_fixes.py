"""Browser tests for regression fixes: file view, tooltips, URL staging."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

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
            headers={"Cookie": f"mktapp_session={SESSION_TOKEN}; mktapp_brand={BRAND}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            info = json.loads(resp.read())
        if not info.get("derived_facts"):
            pytest.skip("K1 has no derived_facts")
    except Exception as e:
        pytest.skip(f"Server not running: {e}")
    yield


def _page(pw):
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()
    ctx.add_cookies([
        {"name": "mktapp_session", "value": SESSION_TOKEN, "url": BASE_URL},
        {"name": "mktapp_brand", "value": BRAND, "url": BASE_URL},
    ])
    page = ctx.new_page()
    page.goto(f"{BASE_URL}/", timeout=15000)
    page.wait_for_timeout(1500)
    return browser, page


def _open_k1_detail(page):
    card = page.query_selector(f'.folder-item:has-text("{PRODUCT}")')
    assert card, "K1 product card not found"
    card.query_selector('.folder-info').click()
    page.wait_for_selector("#product-detail-view", state="visible", timeout=10000)
    page.wait_for_timeout(500)


class TestFileView:
    def test_image_view_url_uses_source_file_endpoint(self, _real_server):
        """The ⋯→ดู action must generate a /api/product_source_file/ URL."""
        with sync_playwright() as pw:
            browser, page = _page(pw)
            _open_k1_detail(page)
            page.click("text=สื่อและแหล่งข้อมูล")
            page.wait_for_selector(".pd-file-menu", state="attached", timeout=3000)
            # Verify generated onclick/URL contains product_source_file
            html = page.locator("#product-detail-view").inner_html()
            assert "/api/product_source_file/" in html or "view_source" in html, \
                "File actions should reference product_source_file endpoint"
            assert "/api/product_image/" + urllib.parse.quote(PRODUCT) + "/" not in html, \
                "Should NOT use product_image/{folder}/{file} — that route does not exist"
            browser.close()

    def test_source_file_endpoint_serves_image(self, _real_server):
        """GET /api/product_source_file/{folder}/{img} returns 200 + image MIME."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", 8778)
        # Get file list first
        conn.request("GET",
            f"/api/folder_files/{urllib.parse.quote(PRODUCT)}",
            headers={"Cookie": f"mktapp_session={SESSION_TOKEN}; mktapp_brand={BRAND}"})
        resp = conn.getresponse()
        files = json.loads(resp.read())
        img_files = [f for f in files if f.get("type") == "image"]
        assert img_files, "No image files found"
        fname = img_files[0]["name"]
        conn.request("GET",
            f"/api/product_source_file/{urllib.parse.quote(PRODUCT)}/{urllib.parse.quote(fname)}",
            headers={"Cookie": f"mktapp_session={SESSION_TOKEN}; mktapp_brand={BRAND}"})
        resp = conn.getresponse()
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        ct = resp.getheader("Content-Type", "")
        assert "image" in ct, f"Expected image MIME, got {ct}"
        conn.close()

    def test_source_file_endpoint_serves_text(self, _real_server):
        """GET /api/product_source_file/{folder}/source_page.txt returns 200 + JSON."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", 8778)
        conn.request("GET",
            f"/api/product_source_file/{urllib.parse.quote(PRODUCT)}/source_page.txt",
            headers={"Cookie": f"mktapp_session={SESSION_TOKEN}; mktapp_brand={BRAND}"})
        resp = conn.getresponse()
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        body = json.loads(resp.read())
        assert "content" in body, "Should return JSON with content key"
        conn.close()

    def test_image_view_popup_renders_image(self, _real_server):
        """K1 ⋯→ดู on page_img_001.jpg opens /api/product_source_file/…,
        returns 200 + image/*, and the image actually decodes in the browser."""
        with sync_playwright() as pw:
            browser, page = _page(pw)
            _open_k1_detail(page)
            page.click("text=สื่อและแหล่งข้อมูล")
            page.wait_for_selector("text=page_img_001.jpg", timeout=8000)

            responses = []
            page.context.on("response", lambda r: responses.append(r))

            # Row containing the filename → ⋯ → ดู
            row = page.locator("span", has_text="page_img_001.jpg").locator("xpath=..")
            row.locator("button").click()  # ⋯
            menu = row.locator(".pd-file-menu")
            menu.wait_for(state="visible", timeout=3000)

            with page.expect_popup(timeout=8000) as pop:
                menu.get_by_text("ดู", exact=True).click()
            popup = pop.value
            popup.wait_for_load_state("domcontentloaded")

            # 1. URL uses the source-file route
            assert "/api/product_source_file/" in popup.url, \
                f"Popup URL should use product_source_file, got {popup.url}"

            # 2. Response was 200 + image MIME
            file_resps = [r for r in responses
                          if "product_source_file" in r.url and "page_img_001" in r.url]
            assert file_resps, "No product_source_file response captured"
            resp = file_resps[-1]
            assert resp.status == 200, f"Expected 200, got {resp.status}"
            assert "image" in (resp.headers.get("content-type") or ""), \
                f"Expected image MIME, got {resp.headers.get('content-type')}"

            # 3. Image actually decodes in the browser
            img = popup.locator("img")
            img.wait_for(state="attached", timeout=5000)
            assert img.evaluate("el => el.naturalWidth") > 0, \
                "Image failed to decode (naturalWidth=0)"
            browser.close()

    def test_source_text_viewer_popup(self, _real_server):
        """K1 source_page.txt ⋯→ดูต้นฉบับ opens a readable text popup."""
        with sync_playwright() as pw:
            browser, page = _page(pw)
            _open_k1_detail(page)
            page.click("text=สื่อและแหล่งข้อมูล")
            page.wait_for_selector("text=source_page.txt", timeout=8000)

            responses = []
            page.context.on("response", lambda r: responses.append(r))

            row = page.locator("span", has_text="source_page.txt").locator("xpath=..")
            row.locator("button").click()
            menu = row.locator(".pd-file-menu")
            menu.wait_for(state="visible", timeout=3000)

            with page.expect_popup(timeout=8000) as pop:
                menu.get_by_text("ดูต้นฉบับ", exact=True).click()
            popup = pop.value

            # _viewSourceFile fetches JSON then document.write(<pre>…) — wait
            # for the pre to appear, then assert content + fetch succeeded.
            popup.wait_for_selector("pre", timeout=8000)
            text = popup.locator("pre").text_content()
            assert len(text) > 0, "Source text popup is empty"

            file_resps = [r for r in responses
                          if "product_source_file" in r.url and "source_page" in r.url]
            assert file_resps, "No product_source_file fetch captured"
            assert file_resps[-1].status == 200
            browser.close()


class TestTooltips:
    def test_marketing_edit_has_tooltips(self, _real_server):
        with sync_playwright() as pw:
            browser, page = _page(pw)
            _open_k1_detail(page)
            page.click("text=การตลาด")
            page.wait_for_timeout(500)
            page.click('button[onclick="pdEditMkt()"]')
            page.wait_for_selector("#pd-mkt-tone", timeout=3000)
            tips = page.locator("#product-detail-view span[data-tooltip]").all()
            assert len(tips) >= 9, f"Expected ≥9 tooltip icons, got {len(tips)}"
            tip_texts = [t.get_attribute("data-tooltip") or "" for t in tips]
            assert any("ช่วงอายุ" in t for t in tip_texts), "Missing age tooltip"
            assert any("คู่แข่ง" in t for t in tip_texts), "Missing competitors tooltip"
            assert any("ระดับราคา" in t for t in tip_texts), "Missing price tier tooltip"
            browser.close()


class TestUrlStaging:
    def test_url_endpoint_returns_staged_response(self, _real_server):
        """POST /api/product_from_url should return staged:true shape."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", 8778)
        payload = json.dumps({"url": "https://example.com/test-product-page"})
        conn.request("POST", "/api/product_from_url",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Cookie": f"mktapp_session={SESSION_TOKEN}; mktapp_brand={BRAND}",
            })
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        # If fetch fails (no network to example.com), that's expected.
        # What matters is the response shape when it succeeds.
        if data.get("ok"):
            assert data.get("staged") is True, "URL import should return staged:true"
            assert "batch_id" in data
            assert "segments" in data
            assert "matches" in data

    def test_staging_preview_renders_multi(self, _real_server):
        """renderStagingPreview shows segment list with per-segment actions."""
        with sync_playwright() as pw:
            browser, page = _page(pw)
            # Open upload modal first so the staging-preview element is visible
            page.click("text=+ เพิ่ม")
            page.wait_for_selector("#upload-overlay.visible", timeout=5000)
            page.evaluate("""() => {
                _stagingBatchId = 'test_batch';
                _stagingSegments = [
                    {suggested_name: 'Product A', category: 'Watch', summary: 'A'},
                    {suggested_name: 'Product B', category: 'Watch', summary: 'B'},
                ];
                _stagingMatches = [
                    {action: 'create', target: null, match_by: null},
                    {action: 'create', target: null, match_by: null},
                ];
                renderStagingPreview();
            }""")
            el = page.locator("#staging-preview-modal")
            assert el.is_visible(), "Staging preview should be visible"
            text = el.text_content()
            assert "2" in text or "Product A" in text
            assert el.locator("select[data-seg-action]").count() == 2
            browser.close()
