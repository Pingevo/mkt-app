"""Regression tests for K52 source-fidelity defects found during investigation.

Defects:
  1. PDF embedded images were not extracted → K52 product image lost
  2. PDF table flattening lost "EXW Price/Unit" qualifier → "US$13.00" detached
  3. Scope/source_refs pointed to wrong product lines (K56 Pro, not K52)
  4. Re-ingest could produce empty/wrong text due to scope filename mismatch

These tests verify the actual K52 cache state after ingestion.
"""
import shutil
from pathlib import Path

import pytest

import src.ingestion as ingestion
import src.product_db as product_db

K52_PRODUCT_ID = "CACGO K52"
K52_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / K52_PRODUCT_ID


def _k52_cache_path() -> Path:
    """Return path to K52's cache file."""
    return Path(__file__).resolve().parent.parent / "cache" / K52_PRODUCT_ID / "product.json"


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

    def _capture_run(self, user_prompt, **kwargs):
        captured[self.agent_name] = list(kwargs.get("image_paths") or [])
        prompts[self.agent_name] = user_prompt
        if self.agent_name == "content_creator":
            return '{"posts":[]}'
        return f"[{self.agent_name} result]"

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator(brand_dir="brand", product_id=K52_PRODUCT_ID)
    fake_llm = type("F", (), {"chat": lambda *a, **k: '{"posts":[]}', "close": lambda s: None})()

    orch.run_product_spec("raw data", llm=fake_llm)
    assert expected_img[0] in captured.get("product_spec", []), "Agent 1 must receive K52 image"
    assert "ไม่มีรูปภาพสินค้าส่งมาในรอบนี้" not in prompts.get("product_spec", "")

    orch.run_competitor_analysis("spec", "", llm=fake_llm)
    assert expected_img[0] in captured.get("competitor_analysis", []), "Agent 2 must receive K52 image"

    orch.run_campaign_strategy("spec", "", llm=fake_llm)
    assert expected_img[0] in captured.get("campaign_strategy", []), "Agent 3 must receive K52 image"

    orch.run_content_creator("spec", "", "", llm=fake_llm)
    assert expected_img[0] in captured.get("content_creator", []), "Agent 4 must receive K52 image"
