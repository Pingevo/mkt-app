"""Regression tests for K52 source-fidelity defects found during investigation.

Defects:
  1. PDF embedded images were not extracted → K52 product image lost
  2. PDF table flattening lost "EXW Price/Unit" qualifier → "US$13.00" detached
  3. Scope/source_refs pointed to wrong product lines (K56 Pro, not K52)
  4. Re-ingest could produce empty/wrong text due to scope filename mismatch

These tests verify the actual K52 cache state after ingestion through the
public staging pipeline.  The source document is a synthetic two-row catalog
PDF built at test time — no supplier/business source data is committed.
"""
import random
from pathlib import Path

import pytest

import src.ingestion as ingestion
import src.product_db as product_db

pytestmark = pytest.mark.usefixtures("brand_ws")

K52_PRODUCT_ID = "CACGO K52"


def _noise_png(size: int, seed: int) -> bytes:
    """Deterministic noise image — incompressible, so size controls bytes."""
    import fitz

    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, size, size), False)
    rng = random.Random(seed)
    mv = pix.samples_mv
    for i in range(len(mv)):
        mv[i] = rng.getrandbits(8)
    return pix.tobytes("png")


def _catalog_pdf() -> bytes:
    """Minimal synthetic catalog PDF preserving the structure under test:
    bordered table ``No. | Model | Product Picture | Function Description |
    Accessory | EXW Price/Unit | MOQ 3000pcs`` with a sibling row above the
    K52 row and one embedded image inside each product's picture cell.
    K52's image is >50KB; the sibling's is smaller so leakage is caught by
    the >50KB product-image assertion.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=1160, height=560)
    xs = [20, 60, 140, 340, 800, 920, 1030, 1140]
    ys = [30, 70, 270, 470]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]), width=0.5)
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y), width=0.5)
    for x, h in zip(xs, ("No.", "Model", "Product Picture",
                         "Function Description", "Accessory",
                         "EXW Price/Unit", "MOQ 3000pcs")):
        page.insert_text((x + 4, 55), h, fontsize=9)
    rows = (
        ("19", "K56 Pro",
         "1. CPU: Realtek 8762DK\n2. Bluetooth: BT 5.0\n"
         "3. Battery: 300mAh\n4. App: Dafit",
         "USB Cable\nUser manual", "US$9.99"),
        ("20", "K52",
         "1. CPU: Realtek 8763EWE\n2. Bluetooth: BT 5.0\n"
         "3. Battery: 400mAh\n4. App: FitCloudPro",
         "USB Cable\nUser manual\nGift box", "US$13.00"),
    )
    for (y0, y1), (no, model, desc, acc, price) in zip(
            ((70, 270), (270, 470)), rows):
        page.insert_text((xs[0] + 4, y0 + 20), no, fontsize=9)
        page.insert_text((xs[1] + 4, y0 + 20), model, fontsize=9)
        page.insert_textbox(fitz.Rect(xs[3] + 4, y0 + 8, xs[4] - 4, y1 - 8),
                            desc, fontsize=8)
        page.insert_textbox(fitz.Rect(xs[4] + 4, y0 + 8, xs[5] - 4, y1 - 8),
                            acc, fontsize=8)
        page.insert_text((xs[5] + 4, y0 + 20), price, fontsize=9)
        page.insert_text((xs[6] + 4, y0 + 20), "3000", fontsize=9)
    page.insert_image(fitz.Rect(xs[2] + 10, 95, xs[2] + 90, 175),
                      stream=_noise_png(80, 56))
    page.insert_image(fitz.Rect(xs[2] + 10, 295, xs[2] + 150, 435),
                      stream=_noise_png(220, 52))
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(autouse=True)
def setup_k52(brand_ws):
    """Arrange CACGO K52 through the public staging pipeline using a
    synthetic catalog PDF built at test time."""
    import src.staging as staging
    batch_id = staging.create_batch([("catalog.pdf", _catalog_pdf())])
    seg_res = staging.run_segmentation(batch_id)
    k52_idx = next(i for i, s in enumerate(seg_res["segments"])
                   if s.get("product_key") == "K52")
    staging.commit_batch(batch_id, [
        {"segment_index": k52_idx, "action": "create", "name": K52_PRODUCT_ID}
    ])


def test_k52_pdf_embedded_images_extracted():
    """PDF embedded images must be extracted and stored in cache."""
    record = product_db.load(K52_PRODUCT_ID)
    descs = record.get("image_descriptions", [])
    assert len(descs) > 0, "K52 cache must have image_descriptions"

    # Verify at least one image file exists
    found = False
    for desc in descs:
        p = Path(desc.get("path", ""))
        if p.exists():
            found = True
            break
    assert found, "At least one extracted image file must exist"


def test_k52_exw_price_association():
    """EXW Price/Unit must remain associated with US$13.00 in the cache."""
    record = product_db.load(K52_PRODUCT_ID)
    raw = record.get("raw_text", "")

    # EXW must be present
    assert "EXW" in raw, "EXW qualifier must be preserved in raw_text"
    # US$13.00 must be present
    assert "US$13.00" in raw, "K52 price must be preserved in raw_text"
    # The association: EXW and US$13.00 must be in the same scoped context.
    # The header is at the top of the page; the price is at the end of K52's row.
    # On the same page they should be within ~3000 chars (page text length).
    idx_exw = raw.find("EXW")
    idx_13 = raw.find("US$13.00")
    assert idx_exw >= 0 and idx_13 >= 0
    assert abs(idx_13 - idx_exw) < 3000, (
        f"EXW (pos {idx_exw}) and US$13.00 (pos {idx_13}) too far apart — "
        f"table structure lost"
    )


def test_k52_product_key_facts_preserved():
    """Key product facts must survive ingestion."""
    record = product_db.load(K52_PRODUCT_ID)
    raw = record.get("raw_text", "")

    checks = [
        ("K52", "model"),
        ("Realtek 8763EWE", "CPU"),
        ("400mAh", "battery"),
        ("FitCloudPro", "app"),
        ("MOQ", "MOQ context"),
    ]
    for fact, desc in checks:
        assert fact in raw, f"K52 fact missing: {desc} ({fact})"


def test_k52_scope_points_to_correct_product():
    """Scope source_refs must point to K52's lines, not adjacent products."""
    record = product_db.load(K52_PRODUCT_ID)
    scope = record.get("scope", {})
    source_refs = scope.get("source_refs", [])

    assert source_refs, "K52 must have scope.source_refs"

    # Verify the referenced lines actually contain "K52"
    for te in record.get("text_extracts", []):
        if te.get("file") == source_refs[0].get("file"):
            lines = te.get("text", "").split("\n")
            for ref in source_refs:
                start = ref.get("line_start", 0)
                end = ref.get("line_end", 0)
                slice_text = "\n".join(lines[max(0, start - 1):end])
                assert "K52" in slice_text, (
                    f"source_refs lines {start}-{end} do not contain 'K52' — "
                    f"scope points to wrong product"
                )
            break


def test_k52_get_product_image_paths_returns_correct_image():
    """get_product_image_paths must return the K52 product image, not adjacent product."""
    paths = product_db.get_product_image_paths(K52_PRODUCT_ID)
    assert len(paths) > 0, "K52 must have at least one image path"

    # The K52 image is the largest image on page 6 (K52's page)
    # Verify the returned path is a real file
    for p in paths:
        pp = Path(p)
        assert pp.exists(), f"Image path must exist: {p}"
        # Check it's not a tiny logo/icon
        assert pp.stat().st_size > 50000, (
            f"Product image should be >50KB, got {pp.stat().st_size} bytes for {p}"
        )


def test_k52_media_reference_path():
    """The K52 image must be available via the media reference path."""
    from src import asset_library

    product_paths = product_db.get_product_image_paths(K52_PRODUCT_ID)
    refs = asset_library.build_input_references(product_paths, [], resource_paths=[])
    assert len(refs) > 0, "build_input_references must return at least one reference for K52"

    # Verify the reference is a real file
    for ref in refs:
        assert Path(ref).exists(), f"Media reference must be a real file: {ref}"


def test_k52_reingest_preserves_text():
    """Re-ingest must not produce empty or wrong product text."""
    # Save current state
    record_before = product_db.load(K52_PRODUCT_ID)
    raw_before = record_before.get("raw_text", "")
    scope_before = record_before.get("scope", {})

    # Re-ingest with force
    result = ingestion.ingest_product(K52_PRODUCT_ID, force=True, is_new_upload=False)
    assert result.get("status") == "ready"

    record_after = product_db.load(K52_PRODUCT_ID)
    raw_after = record_after.get("raw_text", "")

    # raw_text must not be empty and must still contain K52
    assert "K52" in raw_after, "Re-ingest must preserve K52 product text"
    assert len(raw_after) > 100, "Re-ingest must produce non-trivial raw_text"


def test_k52_image_paths_reach_all_agents(monkeypatch):
    """K52 scoped image must be passed to Agent 1–4 multimodal input."""
    import os
    from src.orchestrator import Orchestrator
    from src.agents import base_agent

    os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

    expected_img = product_db.get_product_image_paths(K52_PRODUCT_ID)
    assert expected_img, "K52 must have a scoped product image"

    captured: dict[str, list[str]] = {}
    prompts: dict[str, str] = {}

    import json

    def _capture_run(self, user_prompt, **kwargs):
        captured[self.agent_name] = list(kwargs.get("image_paths") or [])
        prompts[self.agent_name] = user_prompt
        if self.agent_name == "content_creator":
            return json.dumps({"posts": [{
                "platform": "Facebook", "concept": "test", "title": "T",
                "caption": "C", "hashtags": "#h", "asset_ids": [],
                "image_prompts": [], "video_prompts": [],
            }]})
        return f"[{self.agent_name} result]"

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator(brand_dir="brand", product_id=K52_PRODUCT_ID)

    def _fake_chat(*a, **k):
        if "grounding" in k.get("source", ""):
            return json.dumps({"grounded": True, "unsupported_claims": []})
        return json.dumps({"posts": [{
            "platform": "Facebook", "concept": "test", "title": "T",
            "caption": "C", "hashtags": "#h", "asset_ids": [],
            "image_prompts": [], "video_prompts": [],
        }]})

    fake_llm = type("F", (), {"chat": _fake_chat, "close": lambda s: None})()

    orch.run_product_spec("raw data", llm=fake_llm)
    assert expected_img[0] in captured.get("product_spec", []), "Agent 1 must receive K52 image"
    assert "ไม่มีรูปภาพสินค้าส่งมาในรอบนี้" not in prompts.get("product_spec", "")

    orch.run_competitor_analysis("spec", "", llm=fake_llm)
    assert expected_img[0] in captured.get("competitor_analysis", []), "Agent 2 must receive K52 image"

    orch.run_campaign_strategy("spec", "", llm=fake_llm)
    assert expected_img[0] in captured.get("campaign_strategy", []), "Agent 3 must receive K52 image"

    orch.run_content_creator("spec", "", "", llm=fake_llm)
    assert expected_img[0] in captured.get("content_creator", []), "Agent 4 must receive K52 image"
