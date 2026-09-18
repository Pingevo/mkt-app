"""URL import → deterministic Product Information. NO live network, NO model.

A URL import ships the page text (source_page.txt) plus gallery photos
(page_img_*).  The free ingest path builds summary / derived_facts from the
head of the segment text, so the page text must lead and gallery OCR must
not pollute it.  Proves, end to end through the real endpoint + staging:

  - summary starts with the product (Title/Description), provenance is a footer
  - derived_facts come from the page's `label | value` lines, not image OCR
  - raw_text never starts with (or contains) gallery OCR for URL imports
  - plain uploads still OCR images, but documents come first
  - suggested product name defaults to the page title
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import make_brand_client

FIXTURE = Path(__file__).parent / "fixtures" / "apify_shopee_item.json"
SHOPEE_URL = "https://shopee.co.th/--i.1191420560.43332033245"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 300
OCR_NOISE = "ส า ย ซิ ล ิ โค น\n9 | ()\nเ @ | (๓)\nFLASH SALE"


def _canned_result() -> dict:
    from src import url_import
    item = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    return {
        "original_url": SHOPEE_URL,
        "final_url": item["url"],
        "canonical_url": item["url"],
        "fetched_via": "apify",
        "page_title": item["title"],
        "og_title": item["title"],
        "page_class": "single",
        "page_signals": {"product_count": 1, "multi_signals": [], "og_type": "product",
                         "provider": "apify", "actor": url_import.APIFY_SHOPEE_ACTOR_DEFAULT},
        "text": url_import._shopee_item_text(item, SHOPEE_URL),
        "images": [
            {"name": f"page_img_{i:03d}.jpg", "content": JPEG, "source_url": f"https://cdn/{i}"}
            for i in (1, 2, 3)
        ],
        "fetched_at": "2026-09-18T06:55:40+00:00",
    }


@pytest.fixture
def app(tmp_path, monkeypatch):
    import importlib
    import web_viewer
    import src.file_loader as file_loader
    import src.ingestion as ingestion
    import src.openrouter_gateway as gateway
    from src import url_import

    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "_current_llm", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_session_ts", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_cancel_requested", {}, raising=False)
    monkeypatch.setattr(url_import, "fetch_product_page", lambda url: _canned_result())
    monkeypatch.setattr(gateway, "get_api_key", lambda: "")
    monkeypatch.setattr(ingestion, "_make_llm", lambda: None)
    # Any OCR attempt returns recognisable noise — a real tesseract is not needed.
    monkeypatch.setattr(file_loader, "_load_image", lambda path: OCR_NOISE)

    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    client, uid, bid, brand_root = make_brand_client(web_viewer.app, tmp_path, monkeypatch)
    return {"client": client, "brand_root": brand_root}


def _import(client, product_name: str | None = None) -> dict:
    body = {"url": SHOPEE_URL}
    if product_name:
        body["product_name"] = product_name
    r = client.post("/api/product_from_url", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] and data["staged"], data
    return data


def _commit(client, data: dict, name: str) -> str:
    rc = client.post(f"/api/stage/{data['batch_id']}/commit", json={
        "choices": [{"segment_index": 0, "action": "create", "name": name}]})
    assert rc.status_code == 200, rc.text
    return rc.json()["created"][0]


def _record(brand_root: Path, name: str) -> dict:
    return json.loads((brand_root / "cache" / name / "product.json").read_text(encoding="utf-8"))


def test_suggested_name_defaults_to_page_title(app):
    data = _import(app["client"])
    name = data["segments"][0]["suggested_name"]
    assert name.startswith("NewArrival BLACK SHARK RUN สมาร์ทวอทช์ออกกำลังกาย")
    assert len(name) <= 80 and "(" not in name and "/" not in name


def test_explicit_name_still_wins(app):
    data = _import(app["client"], product_name="My Watch")
    assert data["segments"][0]["suggested_name"] == "My Watch"


def test_product_info_comes_from_page_text_not_gallery_ocr(app):
    client, brand_root = app["client"], app["brand_root"]
    data = _import(client, product_name="BLACK SHARK RUN")
    name = _commit(client, data, "BLACK SHARK RUN")
    rec = _record(brand_root, name)

    assert rec["status"] == "ready"
    assert rec["source_import"]["fetched_via"] == "apify"

    # segment text / raw_text lead with the page, never with gallery OCR
    assert data["segments"][0]["text"].startswith("Title: (NewArrival) BLACK SHARK RUN")
    assert rec["raw_text"].startswith("Title: (NewArrival) BLACK SHARK RUN")
    assert "FLASH SALE" not in rec["raw_text"] and "ซิ ล ิ โค น" not in rec["raw_text"]

    # provenance is stored, and sits at the END of the source text
    src_text = (brand_root / "data" / name / "source_page.txt").read_text(encoding="utf-8")
    assert src_text.startswith("Title: ")
    assert src_text.rstrip().endswith("Fetched: 2026-09-18T06:55:40+00:00")
    assert "Source URL: " + SHOPEE_URL in src_text

    # deterministic summary = head of the page text
    summary = rec["metadata"]["summary"]
    assert summary.startswith("Title: (NewArrival) BLACK SHARK RUN")
    assert "Source URL" not in summary and "FLASH" not in summary

    # deterministic facts = the page's label | value lines, traced to the file
    facts = rec["derived_facts"]
    assert facts["brand"]["value"] == "Black Shark(แบล็ค ชาร์ค)"
    assert facts["brand"]["source_file"] == "source_page.txt"
    assert facts["item_type"]["value"] == "Smartwatch"
    assert facts["shop"]["value"].startswith("Black Shark Thailand")
    assert facts["category"]["value"].startswith("มือถือและอุปกรณ์เสริม")
    assert "เ" not in facts and not any("(๓)" in f["value"] for f in facts.values())

    # gallery photos are still the product's media
    assert rec["metadata"]["image_count"] == 3
    assert {f["name"] for f in rec["files"] if f["type"] == "image"} == {
        "page_img_001.jpg", "page_img_002.jpg", "page_img_003.jpg"}


def test_plain_upload_keeps_ocr_but_documents_lead(brand_ws, monkeypatch):
    """Non-URL batches: image OCR still contributes, after document text."""
    import src.file_loader as file_loader
    from src import staging

    monkeypatch.setattr(file_loader, "_load_image", lambda path: OCR_NOISE)
    bid = staging.create_batch([
        ("a_photo.jpg", JPEG),                       # sorts first by name
        ("spec.txt", "Title: Widget\nPower | 900W\n".encode("utf-8")),
    ])
    seg = staging.run_segmentation(bid)["segments"][0]
    text = seg["text"]
    assert text.startswith("Title: Widget")
    assert "FLASH SALE" in text and text.index("Power | 900W") < text.index("FLASH SALE")
