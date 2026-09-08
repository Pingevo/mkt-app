"""Production-contract tests for the four remaining gaps.

All tests use fakes/mocks at provider boundaries only. No paid/model/web/media calls.

Gap coverage:
  1. Unassigned catalog media must not become product media
  2. Same filename with changed content — hash-aware dedup
  3. Scoped fallback must validate each selected product independently
  4. Product-profile tests must capture actual production input per Agent
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class FakeLLM:
    """Deterministic LLM double — captures messages at the llm.chat() boundary.

    No provider calls.  Supports per-call output overrides via call_index
    so tests can return different outputs for generate vs review phases.
    """

    def __init__(self, output: str = "mock output", outputs: list[str] | None = None):
        self._output = output
        self._outputs = outputs  # optional per-call overrides
        self.calls: list[dict] = []
        self.last_truncated = False

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            return '{"grounded": true, "unsupported_claims": []}'
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self._outputs is not None and len(self._outputs) > len(self.calls) - 1:
            out = self._outputs[len(self.calls) - 1]
        else:
            out = self._output
        if kwargs.get("return_annotations"):
            return out, []
        return out

    def close(self):
        pass

    def abort(self):
        pass


def _make_product_profile(cache_dir: Path, product_id: str, profile: dict) -> None:
    p = cache_dir / product_id / "product_profile.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_brand_dir(tmp_path: Path) -> Path:
    brand_dir = tmp_path / "brand"
    brand_dir.mkdir(exist_ok=True)
    (brand_dir / "voice.json").write_text(json.dumps({
        "personality": "base personality",
        "tone_description": "base tone",
    }), encoding="utf-8")
    (brand_dir / "audience.json").write_text(json.dumps({
        "primary": {"age": "25-45", "gender": "all"},
    }), encoding="utf-8")
    return brand_dir


def _make_xlsx_with_image(label: str = "image1") -> bytes:
    """Create a minimal xlsx with one embedded image in xl/media/."""
    import zipfile
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '</Types>'
        ))
        zf.writestr("xl/workbook.xml", (
            '<?xml version="1.0"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
            '</sheets></workbook>'
        ))
        zf.writestr(f"xl/media/{label}.png", b"\x89PNG\r\n\x1a\n" + label.encode() + b"\x00" * 50)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Gap 1: Unassigned catalog media must not become product media
# ---------------------------------------------------------------------------

@pytest.fixture
def _stage(monkeypatch, tmp_path):
    import importlib
    import src.product_db as product_db

    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    importlib.reload(product_db)
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)

    import src.staging as staging
    importlib.reload(staging)
    monkeypatch.setattr(staging, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(staging, "product_db", product_db)
    monkeypatch.setattr(staging, "_generate_product_profile", lambda *a, **kw: None)
    return staging


def test_unassigned_media_not_returned_by_get_product_image_paths(tmp_path, monkeypatch):
    """get_product_image_paths must NOT return images marked unassigned_source_media.
    Unassigned media must remain persisted but not become product-specific input."""
    import src.product_db as product_db

    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)

    cache_dir = tmp_path / "cache" / "SplitProduct" / "extracted_images"
    cache_dir.mkdir(parents=True)
    img1 = cache_dir / "assigned.png"
    img1.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    img2 = cache_dir / "unassigned.png"
    img2.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    product_db.save("SplitProduct", {
        "product_id": "SplitProduct",
        "status": product_db.STATUS_READY,
        "raw_text": "product spec text",
        "text_extracts": [{"file": "catalog.pdf", "text": "product spec text"}],
        "image_descriptions": [
            {"path": str(img1), "file": "assigned.png"},
            {"path": str(img2), "file": "unassigned.png", "unassigned_source_media": True},
        ],
    })

    paths = product_db.get_product_image_paths("SplitProduct")
    assert str(img1) in paths, "assigned image must be returned"
    assert str(img2) not in paths, "unassigned image must NOT be returned"

    # Unassigned media must remain persisted in the record
    rec = product_db.load("SplitProduct")
    all_imgs = rec.get("image_descriptions", [])
    assert len(all_imgs) == 2, "unassigned media must not be discarded from record"


def test_staging_standalone_single_product_embedded_media_remains_usable(_stage, tmp_path):
    """Production-path test: a standalone/unscoped single-product ingestion
    (llm=None flow, no product_key) must keep embedded media usable through
    get_product_image_paths().  Embedded media belongs to that product."""
    staging = _stage
    import src.product_db as product_db

    xlsx_bytes = _make_xlsx_with_image("product_img")

    batch_id = staging.create_batch([("product.xlsx", xlsx_bytes)])
    staging.run_segmentation(batch_id, llm=None)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "StandaloneProduct"},
    ])

    assert result["created"] == ["StandaloneProduct"]

    rec = product_db.load("StandaloneProduct")
    imgs = rec.get("image_descriptions", [])
    assert len(imgs) > 0, "embedded media must be extracted for standalone product"

    # Standalone product media must NOT be labeled unassigned
    for img in imgs:
        assert not img.get("unassigned_source_media"), \
            "standalone single-product embedded media must NOT be labeled unassigned"

    # get_product_image_paths must return the images (they belong to this product)
    paths = product_db.get_product_image_paths("StandaloneProduct")
    assert len(paths) > 0, \
        "standalone single-product embedded media must remain usable through get_product_image_paths"


def test_staging_single_mode_with_product_key_embedded_media_remains_usable(_stage, tmp_path):
    """Production-path test: segmentation LLM returns exactly ONE product
    with a non-empty product_key (mode="single").  Its embedded image must
    remain usable — product_key is for identity/scope, not a proxy for
    product count.  Media association is only ambiguous in multi mode."""
    staging = _stage
    import src.product_db as product_db

    xlsx_bytes = _make_xlsx_with_image("single_product_img")

    batch_id = staging.create_batch([("product.xlsx", xlsx_bytes)])
    # Simulate LLM segmentation returning one product with product_key
    # and mode="single" (LLM confirmed this is one product, not a catalog)
    batch = staging._load_batch(batch_id)
    batch["status"] = "segmented"
    batch["seg_mode"] = "single"
    batch["segments"] = [{
        "product_key": "K67",
        "suggested_name": "SingleProduct K67",
        "category": "",
        "summary": "",
        "text": "K67 product spec data",
        "source_refs": [{"file": "product.xlsx", "line_start": 1, "line_end": 10}],
        "common_refs": [],
    }]
    staging._save_batch(batch_id, batch)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "SingleK67"},
    ])

    assert result["created"] == ["SingleK67"]

    rec = product_db.load("SingleK67")
    imgs = rec.get("image_descriptions", [])
    assert len(imgs) > 0, "embedded media must be extracted"

    # Single mode → media must NOT be labeled unassigned even with product_key
    for img in imgs:
        assert not img.get("unassigned_source_media"), \
            "single-mode product with product_key must NOT label media as unassigned"

    # Media must remain usable through get_product_image_paths
    paths = product_db.get_product_image_paths("SingleK67")
    assert len(paths) > 0, \
        "single-mode product with product_key must have usable embedded media"


def test_legacy_batch_no_seg_mode_multi_segments_treated_as_multi(_stage, tmp_path):
    """Backward compatibility: a legacy batch created before seg_mode was
    introduced, with multiple segments and no seg_mode field, must be
    treated as multi mode.  Extracted catalog media remains unassigned."""
    staging = _stage
    import src.product_db as product_db

    xlsx_bytes = _make_xlsx_with_image("legacy_catalog_img")

    batch_id = staging.create_batch([("catalog.xlsx", xlsx_bytes)])
    # Simulate a legacy batch: set segments but NO seg_mode field
    batch = staging._load_batch(batch_id)
    batch["status"] = "segmented"
    # Deliberately do NOT set batch["seg_mode"]
    batch["segments"] = [
        {
            "product_key": "LegacyA",
            "suggested_name": "LegacyA",
            "category": "",
            "summary": "",
            "text": "LegacyA spec data",
            "source_refs": [{"file": "catalog.xlsx", "line_start": 1, "line_end": 10}],
            "common_refs": [],
        },
        {
            "product_key": "LegacyB",
            "suggested_name": "LegacyB",
            "category": "",
            "summary": "",
            "text": "LegacyB spec data",
            "source_refs": [{"file": "catalog.xlsx", "line_start": 11, "line_end": 20}],
            "common_refs": [],
        },
    ]
    staging._save_batch(batch_id, batch)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "LegacyMultiA"},
        {"segment_index": 1, "action": "create", "name": "LegacyMultiB"},
    ])

    assert result["created"] == ["LegacyMultiA", "LegacyMultiB"]

    for pname in ["LegacyMultiA", "LegacyMultiB"]:
        rec = product_db.load(pname)
        imgs = rec.get("image_descriptions", [])
        assert len(imgs) > 0, f"{pname}: extracted media must be persisted"

        # Legacy multi-segment batch → media must be labeled unassigned
        for img in imgs:
            assert img.get("unassigned_source_media") is True, \
                f"{pname}: legacy multi-segment batch must label media as unassigned"

        # Unassigned media must not reach Agent as product-specific
        paths = product_db.get_product_image_paths(pname)
        assert len(paths) == 0, \
            f"{pname}: unassigned media must not reach Agent"


def test_legacy_batch_no_seg_mode_single_segment_treated_as_single(_stage, tmp_path):
    """Backward compatibility: a legacy batch created before seg_mode was
    introduced, with exactly one segment and no seg_mode field, must be
    treated as single mode.  Embedded media remains usable."""
    staging = _stage
    import src.product_db as product_db

    xlsx_bytes = _make_xlsx_with_image("legacy_single_img")

    batch_id = staging.create_batch([("product.xlsx", xlsx_bytes)])
    # Simulate a legacy batch: set one segment but NO seg_mode field
    batch = staging._load_batch(batch_id)
    batch["status"] = "segmented"
    # Deliberately do NOT set batch["seg_mode"]
    batch["segments"] = [{
        "product_key": "LegacySingle",
        "suggested_name": "LegacySingle",
        "category": "",
        "summary": "",
        "text": "LegacySingle spec data",
        "source_refs": [{"file": "product.xlsx", "line_start": 1, "line_end": 10}],
        "common_refs": [],
    }]
    staging._save_batch(batch_id, batch)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "LegacySingleProduct"},
    ])

    assert result["created"] == ["LegacySingleProduct"]

    rec = product_db.load("LegacySingleProduct")
    imgs = rec.get("image_descriptions", [])
    assert len(imgs) > 0, "embedded media must be extracted"

    # Legacy single-segment batch → media must NOT be labeled unassigned
    for img in imgs:
        assert not img.get("unassigned_source_media"), \
            "legacy single-segment batch must NOT label media as unassigned"

    # Media must remain usable through get_product_image_paths
    paths = product_db.get_product_image_paths("LegacySingleProduct")
    assert len(paths) > 0, \
        "legacy single-segment batch must have usable embedded media"


def test_staging_scoped_split_products_unassigned_images_dont_reach_agents(_stage, tmp_path):
    """Production-path test: two genuinely scoped/split products from one
    catalog with multiple embedded images.  Segmentation produces
    product_key + source_refs for each product.  Unassigned catalog images
    must not reach either Agent as product-specific images."""
    staging = _stage
    import src.product_db as product_db

    xlsx_bytes = _make_xlsx_with_image("catalog_img")

    batch_id = staging.create_batch([("catalog.xlsx", xlsx_bytes)])
    # Manually inject scoped segments with product_key AND seg_mode="multi"
    # to simulate a genuine split-product scenario (segmentation LLM found
    # multiple products in the catalog)
    batch = staging._load_batch(batch_id)
    batch["status"] = "segmented"
    batch["seg_mode"] = "multi"
    batch["segments"] = [
        {
            "product_key": "ProductA",
            "suggested_name": "ProductA",
            "category": "",
            "summary": "",
            "text": "ProductA spec data",
            "source_refs": [{"file": "catalog.xlsx", "line_start": 1, "line_end": 10}],
            "common_refs": [],
        },
        {
            "product_key": "ProductB",
            "suggested_name": "ProductB",
            "category": "",
            "summary": "",
            "text": "ProductB spec data",
            "source_refs": [{"file": "catalog.xlsx", "line_start": 11, "line_end": 20}],
            "common_refs": [],
        },
    ]
    staging._save_batch(batch_id, batch)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "SplitProductA"},
        {"segment_index": 1, "action": "create", "name": "SplitProductB"},
    ])

    assert result["created"] == ["SplitProductA", "SplitProductB"]

    for pname in ["SplitProductA", "SplitProductB"]:
        rec = product_db.load(pname)
        imgs = rec.get("image_descriptions", [])
        assert len(imgs) > 0, f"{pname} must have extracted images persisted"

        # Scoped/split product media must be labeled unassigned
        for img in imgs:
            assert img.get("unassigned_source_media") is True, \
                f"{pname}: catalog images without deterministic association must be labeled unassigned"

        # get_product_image_paths must return NO images for scoped/split products
        # because all are unassigned
        paths = product_db.get_product_image_paths(pname)
        assert len(paths) == 0, \
            f"{pname}: unassigned catalog images must not reach Agent as product-specific images"

        # But the images must remain persisted in the record
        assert len(imgs) > 0, \
            f"{pname}: unassigned media must remain persisted with provenance"


def test_staging_preserves_full_text_extracts_not_segmented(_stage, tmp_path):
    """Staging must preserve the original full text_extracts with source
    filename, not replace with {"file": "segmented"}, so page/source-ref
    association can work."""
    staging = _stage
    import src.product_db as product_db

    # Create a text file as source
    source_text = "line1\nline2\nline3\nproduct spec data"

    batch_id = staging.create_batch([("catalog.txt", source_text.encode("utf-8"))])
    staging.run_segmentation(batch_id, llm=None)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "TextProduct"},
    ])

    rec = product_db.load("TextProduct")
    text_extracts = rec.get("text_extracts", [])

    # text_extracts must contain the original filename, not "segmented"
    files = [te.get("file") for te in text_extracts]
    assert "catalog.txt" in files, \
        "text_extracts must preserve original source filename for page/source-ref association"
    assert "segmented" not in files, \
        "text_extracts must NOT replace source identity with 'segmented'"


# ---------------------------------------------------------------------------
# Gap 2: Same filename with changed content — hash-aware dedup
# ---------------------------------------------------------------------------

def test_copy_source_files_same_content_idempotent(_stage, tmp_path):
    """Same filename + same hash → skip (idempotent)."""
    staging = _stage

    # Create existing product with a file
    data_dir = tmp_path / "data" / "DedupProduct"
    data_dir.mkdir(parents=True)
    existing_file = data_dir / "catalog.txt"
    existing_file.write_text("original content", encoding="utf-8")

    # Create staging batch with same filename + same content
    source_dir = tmp_path / "data" / ".staging" / "test_batch" / "source"
    source_dir.mkdir(parents=True)
    new_file = source_dir / "catalog.txt"
    new_file.write_text("original content", encoding="utf-8")

    saved = staging._copy_source_files(source_dir, data_dir)

    # Same content → skip, no new file created
    assert saved.get("catalog.txt") == "catalog.txt"
    # No deduped file should exist
    assert not (data_dir / "catalog (1).txt").exists()


def test_copy_source_files_same_name_different_content(_stage, tmp_path):
    """Same filename + different hash → save with deduped filename."""
    staging = _stage

    data_dir = tmp_path / "data" / "DedupProduct"
    data_dir.mkdir(parents=True)
    existing_file = data_dir / "catalog.txt"
    existing_file.write_text("original content", encoding="utf-8")

    source_dir = tmp_path / "data" / ".staging" / "test_batch2" / "source"
    source_dir.mkdir(parents=True)
    new_file = source_dir / "catalog.txt"
    new_file.write_text("revised content — new revision", encoding="utf-8")

    saved = staging._copy_source_files(source_dir, data_dir)

    # Same name, different content → saved with deduped name
    saved_name = saved.get("catalog.txt")
    assert saved_name is not None
    assert saved_name != "catalog.txt", "different content must get deduped filename"
    assert (data_dir / saved_name).exists(), "deduped file must exist"
    # Original file must still exist (not overwritten)
    assert (data_dir / "catalog.txt").exists()
    assert (data_dir / "catalog.txt").read_text() == "original content"
    # New file must have the new content
    assert (data_dir / saved_name).read_text() == "revised content — new revision"


def test_staging_update_same_name_different_content_provenance(_stage, tmp_path):
    """When updating with a same-name/different-content text file, ALL
    persisted references from the current batch must be remapped to the
    actual saved filename.  text_extracts built from scanning actual stored
    files must NOT be blindly remapped — old and new content must each
    have their own correct filename.

    Uses valid text files (not invalid XLSX bytes) so text_extracts are
    actually built from file content.
    """
    staging = _stage
    import src.product_db as product_db

    # Create existing product with original catalog.txt (valid text)
    data_dir = tmp_path / "data" / "ProvenanceProduct"
    data_dir.mkdir(parents=True)
    old_text = "line1 original\nline2 original\nline3 original"
    (data_dir / "catalog.txt").write_text(old_text, encoding="utf-8")
    product_db.save("ProvenanceProduct", {
        "product_id": "ProvenanceProduct",
        "status": product_db.STATUS_READY,
        "raw_text": old_text,
        "text_extracts": [{"file": "catalog.txt", "text": old_text}],
        "image_descriptions": [],
        "scope": {
            "product_key": "ProvenanceProduct",
            "source_refs": [{"file": "catalog.txt", "line_start": 1, "line_end": 3}],
            "common_refs": [{"file": "catalog.txt", "line_start": 1, "line_end": 1}],
        },
    })

    # Upload a revised catalog.txt with different content (valid text)
    new_text = "line1 revised\nline2 revised\nline3 revised"
    batch_id = staging.create_batch([("catalog.txt", new_text.encode("utf-8"))])
    # Manually set scoped segment with source_refs pointing to catalog.txt
    batch = staging._load_batch(batch_id)
    batch["status"] = "segmented"
    batch["seg_mode"] = "single"
    batch["segments"] = [{
        "product_key": "ProvenanceProduct",
        "suggested_name": "ProvenanceProduct",
        "category": "",
        "summary": "",
        "text": new_text,
        "source_refs": [{"file": "catalog.txt", "line_start": 1, "line_end": 3}],
        "common_refs": [{"file": "catalog.txt", "line_start": 1, "line_end": 1}],
    }]
    staging._save_batch(batch_id, batch)

    staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "update", "target": "ProvenanceProduct"},
    ])

    rec = product_db.load("ProvenanceProduct")

    # 1. Provenance must record the actual saved filename
    provenance = rec.get("source_file_provenance", {})
    assert "catalog.txt" in provenance
    saved_name = provenance["catalog.txt"]
    assert saved_name != "catalog.txt", "different content must have deduped name"

    # 2. Both files must exist in data_dir with their respective content
    assert (data_dir / "catalog.txt").exists()
    assert (data_dir / "catalog.txt").read_text() == old_text, \
        "catalog.txt must still contain the OLD text"
    assert (data_dir / saved_name).exists()
    assert (data_dir / saved_name).read_text() == new_text, \
        f"{saved_name} must contain the NEW text"

    # 3. scope.source_refs must be remapped to the saved filename
    scope = rec.get("scope", {})
    source_refs = scope.get("source_refs", [])
    for ref in source_refs:
        assert ref.get("file") == saved_name, \
            f"scope.source_refs must be remapped to '{saved_name}', got '{ref.get('file')}'"

    # 4. scope.common_refs must be remapped to the saved filename
    common_refs = scope.get("common_refs", [])
    for ref in common_refs:
        assert ref.get("file") == saved_name, \
            f"scope.common_refs must be remapped to '{saved_name}', got '{ref.get('file')}'"

    # 5. text_extracts must have EXACT filename-to-text correspondence:
    #    catalog.txt → old text, saved_name → new text
    text_extracts = rec.get("text_extracts", [])
    text_by_file = {te.get("file"): te.get("text") for te in text_extracts}
    assert "catalog.txt" in text_by_file, \
        "text_extracts must include catalog.txt (old revision)"
    assert text_by_file["catalog.txt"] == old_text, \
        "catalog.txt text_extract must contain OLD text"
    assert saved_name in text_by_file, \
        f"text_extracts must include {saved_name} (new revision)"
    assert text_by_file[saved_name] == new_text, \
        f"{saved_name} text_extract must contain NEW text"

    # 6. No duplicate text_extracts[*].file values representing different revisions
    file_names = [te.get("file") for te in text_extracts]
    assert len(file_names) == len(set(file_names)), \
        "text_extracts must not have duplicate filenames"

    # 7. Scoped lookup must resolve the new revision
    from src.product_segmentation import _slice_refs
    text_lookup = {te.get("file"): te.get("text") for te in text_extracts}
    sliced = _slice_refs(text_lookup, scope.get("source_refs", []))
    assert "revised" in sliced, \
        f"scoped lookup via source_refs must resolve to the new revision, got: {sliced}"


def test_copy_source_files_idempotent_repeat_returns_existing_deduped(_stage, tmp_path):
    """When the same revised content is uploaded again (idempotent repeat),
    _copy_source_files must return the existing deduplicated filename,
    not an empty mapping."""
    staging = _stage

    data_dir = tmp_path / "data" / "IdempotentProduct"
    data_dir.mkdir(parents=True)
    (data_dir / "catalog.txt").write_text("original content", encoding="utf-8")

    revised_content = "revised content - new revision".encode("utf-8")

    # First upload of revised content
    source_dir1 = tmp_path / "data" / ".staging" / "batch1" / "source"
    source_dir1.mkdir(parents=True)
    (source_dir1 / "catalog.txt").write_bytes(revised_content)

    saved1 = staging._copy_source_files(source_dir1, data_dir)
    saved_name1 = saved1.get("catalog.txt")
    assert saved_name1 is not None
    assert saved_name1 != "catalog.txt", "first revision must get deduped name"

    # Second upload of the SAME revised content (idempotent repeat)
    source_dir2 = tmp_path / "data" / ".staging" / "batch2" / "source"
    source_dir2.mkdir(parents=True)
    (source_dir2 / "catalog.txt").write_bytes(revised_content)

    saved2 = staging._copy_source_files(source_dir2, data_dir)
    saved_name2 = saved2.get("catalog.txt")
    assert saved_name2 is not None, \
        "idempotent repeat must return the existing deduped filename, not empty"
    assert saved_name2 == saved_name1, \
        "idempotent repeat must return the SAME deduped filename"

    # No additional deduped file should be created
    assert not (data_dir / "catalog (2).txt").exists()


# ---------------------------------------------------------------------------
# Gap 3: Scoped fallback must validate each selected product independently
# ---------------------------------------------------------------------------

def test_scoped_valid_plus_scoped_missing_fails(tmp_path, monkeypatch):
    """A valid scoped product + a scoped product with no usable data → fail.
    One valid product must NOT make another invalid product pass."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.agents import base_agent
    import web_viewer

    monkeypatch.chdir(tmp_path)

    # ProductA: scoped with text
    cache_a = tmp_path / "cache" / "ProductA"
    cache_a.mkdir(parents=True)
    product_db.save("ProductA", {
        "product_id": "ProductA",
        "status": product_db.STATUS_READY,
        "raw_text": "ProductA spec data",
        "text_extracts": [{"file": "catalog.pdf", "text": "ProductA spec data"}],
        "image_descriptions": [],
        "scope": {"product_key": "ProductA", "source_refs": [], "common_refs": []},
    })

    # ProductB: scoped but empty
    cache_b = tmp_path / "cache" / "ProductB"
    cache_b.mkdir(parents=True)
    product_db.save("ProductB", {
        "product_id": "ProductB",
        "status": product_db.STATUS_READY,
        "raw_text": "",
        "text_extracts": [],
        "image_descriptions": [],
        "scope": {"product_key": "ProductB", "source_refs": [], "common_refs": []},
    })

    brand_dir = _make_brand_dir(tmp_path)

    agent_was_called = False

    class _GuardAgent:
        agent_name = "product_spec"
        def run(self, *args, **kwargs):
            nonlocal agent_was_called
            agent_was_called = True
            return "should never reach"

    def _make_guard_agent(agent_name, agent_cls, llm):
        return _GuardAgent()

    orch = Orchestrator(brand_dir=str(brand_dir))
    orch._make_agent = _make_guard_agent

    with pytest.raises(ValueError, match="ProductB"):
        web_viewer._run_single_agent(
            "product_spec", "ProductA", [], [], {},
            orch, FakeLLM(), tmp_path / "output", save_output=False,
            folders=["ProductA", "ProductB"],
        )

    assert not agent_was_called


def test_scoped_direct_image_only_proceeds(tmp_path, monkeypatch):
    """A scoped product with no text but a valid direct product image
    (supplied via image_paths) must proceed."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.agents import base_agent
    import web_viewer

    monkeypatch.chdir(tmp_path)

    cache_dir = tmp_path / "cache" / "DirectImageProduct"
    cache_dir.mkdir(parents=True)
    img = cache_dir / "extracted_images" / "direct.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    product_db.save("DirectImageProduct", {
        "product_id": "DirectImageProduct",
        "status": product_db.STATUS_READY,
        "raw_text": "",
        "text_extracts": [],
        "image_descriptions": [],  # No DB image
        "scope": {"product_key": "DirectImageProduct", "source_refs": [], "common_refs": []},
    })

    brand_dir = _make_brand_dir(tmp_path)

    agent_was_called = False

    def _capture_run(self, user_prompt, **kwargs):
        nonlocal agent_was_called
        agent_was_called = True
        return "[result]"

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator(brand_dir=str(brand_dir))

    web_viewer._run_single_agent(
        "product_spec", "DirectImageProduct",
        [], [str(img)], {}, orch, FakeLLM(),
        tmp_path / "output", save_output=False,
        folders=["DirectImageProduct"],
    )

    assert agent_was_called


def test_scoped_profile_only_fails(tmp_path, monkeypatch):
    """A scoped product with only profile context (no text, no images)
    must fail — profile context does NOT count as usable factual content."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.agents import base_agent
    import web_viewer

    monkeypatch.chdir(tmp_path)

    # Create a product with a profile but no text/images
    _make_product_profile(tmp_path / "cache", "ProfileOnlyProduct", {
        "audience": {"primary": {"age": "30-50"}},
        "differentiators": ["unique feature"],
    })

    product_db.save("ProfileOnlyProduct", {
        "product_id": "ProfileOnlyProduct",
        "status": product_db.STATUS_READY,
        "raw_text": "",
        "text_extracts": [],
        "image_descriptions": [],
        "scope": {"product_key": "ProfileOnlyProduct", "source_refs": [], "common_refs": []},
    })

    brand_dir = _make_brand_dir(tmp_path)

    agent_was_called = False

    class _GuardAgent:
        agent_name = "product_spec"
        def run(self, *args, **kwargs):
            nonlocal agent_was_called
            agent_was_called = True
            return "should never reach"

    def _make_guard_agent(agent_name, agent_cls, llm):
        return _GuardAgent()

    orch = Orchestrator(brand_dir=str(brand_dir))
    orch._make_agent = _make_guard_agent

    with pytest.raises(ValueError, match="ProfileOnlyProduct"):
        web_viewer._run_single_agent(
            "product_spec", "ProfileOnlyProduct",
            [], [], {}, orch, FakeLLM(),
            tmp_path / "output", save_output=False,
            folders=["ProfileOnlyProduct"],
        )

    assert not agent_was_called


def test_both_valid_scoped_products_proceed(tmp_path, monkeypatch):
    """Both scoped products with usable text → proceed."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.agents import base_agent
    import web_viewer

    monkeypatch.chdir(tmp_path)

    for pid in ["ValidA", "ValidB"]:
        product_db.save(pid, {
            "product_id": pid,
            "status": product_db.STATUS_READY,
            "raw_text": f"{pid} spec data",
            "text_extracts": [{"file": "catalog.pdf", "text": f"{pid} spec data"}],
            "image_descriptions": [],
            "scope": {"product_key": pid, "source_refs": [], "common_refs": []},
        })

    brand_dir = _make_brand_dir(tmp_path)

    agent_was_called = False

    def _capture_run(self, user_prompt, **kwargs):
        nonlocal agent_was_called
        agent_was_called = True
        return "[result]"

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator(brand_dir=str(brand_dir))

    web_viewer._run_single_agent(
        "product_spec", "ValidA", [], [], {},
        orch, FakeLLM(), tmp_path / "output", save_output=False,
        folders=["ValidA", "ValidB"],
    )

    assert agent_was_called


# ---------------------------------------------------------------------------
# Gap 4: Product-profile tests must capture actual production input per Agent
# ---------------------------------------------------------------------------

def test_agent1_profile_fields_in_actual_prompt(tmp_path, monkeypatch):
    """Agent 1 (product_spec): run through the real Orchestrator.run_product_spec
    and capture the actual messages sent to llm.chat() — the real final model
    input boundary.

    Agent 1 does NOT enable use_brand_reference (config: use_brand_reference=None
    → defaults to False).  Therefore audience/positioning from brand_reference
    must NOT appear in Agent 1's system prompt.  tone_adjustment IS appropriate
    for product-spec (it adjusts the writing tone) and must appear via brand_rules.
    """
    from src.orchestrator import Orchestrator
    from src.config_loader import get_agent_config

    monkeypatch.chdir(tmp_path)
    brand_dir = _make_brand_dir(tmp_path)

    _make_product_profile(tmp_path / "cache", "ProfiledProduct", {
        "tone_adjustment": "โทนเฉพาะสินค้านี้",
        "audience": {"primary": {"age": "30-50", "interest": "fitness"}},
        "differentiators": ["unique feature X"],
    })

    orch = Orchestrator(brand_dir=str(brand_dir))
    orch.bind_product("ProfiledProduct")

    cfg = get_agent_config(orch.config, "product_spec")
    assert not cfg.get("use_brand_reference", False), \
        "Agent 1 must not have use_brand_reference enabled"

    # Use FakeLLM.chat() as the provider boundary — no BaseAgent.run() patch
    fake_llm = FakeLLM(
        output=(
            "# ชื่อสินค้า: ProfiledProduct\n"
            "## ราคา\n- 100 บาท\n"
            "## คุณสมบัติ\n- feature A\n"
            "## กลุ่มเป้าหมาย\n- general\n"
        )
    )

    # Run through the REAL Orchestrator.run_product_spec path
    orch.run_product_spec("raw product data", llm=fake_llm)

    # Inspect the actual messages sent to llm.chat()
    assert len(fake_llm.calls) > 0, "run_product_spec must call llm.chat()"
    messages = fake_llm.calls[0]["messages"]
    assert len(messages) >= 2, "messages must have system + user"

    system = ""
    user = ""
    for msg in messages:
        if msg["role"] == "system":
            system = msg["content"]
        elif msg["role"] == "user":
            user = msg["content"]

    # The system prompt must have been composed (not empty)
    assert system, "run_product_spec must compose a system prompt"

    # tone_adjustment IS appropriate for product-spec → must appear
    assert "โทนเฉพาะสินค้านี้" in system, \
        "tone_adjustment must appear in Agent 1 system prompt via brand_rules"

    # Agent 1 does NOT enable use_brand_reference → audience must NOT appear
    assert "30-50" not in system, \
        "audience must NOT appear in Agent 1 system prompt when use_brand_reference is False"
    assert "unique feature X" not in system, \
        "positioning/differentiators must NOT appear in Agent 1 prompt when use_brand_reference is False"

    # The user prompt must contain the raw data
    assert "raw product data" in user


def test_agent2_evidence_mode_brand_reference_absent_from_stage_a(tmp_path, monkeypatch):
    """Agent 2 (competitor_analysis): run the real evidence-mode pipeline
    through Orchestrator.run_competitor_analysis.  Capture the actual messages
    sent to llm.chat() — the real final model input boundary.

    Assert raw brand_reference is absent from the Stage A system prompt.
    Patch only downstream reviewer/renderer/validation boundaries to keep
    the test offline — do NOT patch BaseAgent.run().
    """
    from src.orchestrator import Orchestrator
    from src.config_loader import load_config, get_agent_config

    monkeypatch.chdir(tmp_path)
    brand_dir = _make_brand_dir(tmp_path)

    _make_product_profile(tmp_path / "cache", "ProfiledProduct", {
        "audience": {"primary": {"age": "30-50", "interest": "fitness"}},
        "differentiators": ["unique feature X"],
    })

    cfg = get_agent_config(load_config(), "competitor_analysis")
    assert cfg.get("evidence_mode") is True, "production config must have evidence_mode=true"
    assert cfg.get("use_brand_reference") is True

    orch = Orchestrator(brand_dir=str(brand_dir))
    orch.bind_product("ProfiledProduct")

    # A valid ResearchResponse JSON that passes _validate_research_json
    valid_research_json = json.dumps({
        "target_model": "ProfiledProduct",
        "competitor_names": ["CompA"],
        "evidence": [{
            "competitor": "CompA",
            "field": "price",
            "claim": "CompA costs $100",
            "url": "https://example.com/compa-price",
            "geography": "global",
        }],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })

    # Use FakeLLM.chat() as the provider boundary — no BaseAgent.run() patch
    fake_llm = FakeLLM(output=valid_research_json)

    # Patch only downstream boundaries to keep the test offline:
    # - SemanticEvidenceReviewer: no web calls, pass through
    # - CompetitorReportRenderer.validate: no annotation matching
    from src.agents.competitor_evidence import SemanticEvidenceReviewer
    monkeypatch.setattr(SemanticEvidenceReviewer, "review", lambda self, research, relevant: research)

    from src.agents.competitor_analysis import CompetitorReportRenderer
    monkeypatch.setattr(CompetitorReportRenderer, "validate", lambda self: [])

    # Run through the REAL Orchestrator.run_competitor_analysis path
    orch.run_competitor_analysis("test spec", "", llm=fake_llm)

    # Inspect the actual messages sent to llm.chat() — the first call is Stage A
    assert len(fake_llm.calls) > 0, "run_competitor_analysis must call llm.chat()"
    messages = fake_llm.calls[0]["messages"]
    assert len(messages) >= 2, "messages must have system + user"

    stage_a_system = ""
    for msg in messages:
        if msg["role"] == "system":
            stage_a_system = msg["content"]

    assert stage_a_system, "Stage A system prompt must be captured from llm.chat() messages"

    # Evidence core must be present
    assert "Competitor Analyst" in stage_a_system
    # Brand reference must NOT be present in Stage A
    assert "30-50" not in stage_a_system, \
        "audience must NOT appear in Stage A system prompt (evidence isolation)"
    assert "unique feature X" not in stage_a_system, \
        "positioning must NOT appear in Stage A system prompt (evidence isolation)"
    assert "ข้อมูลแบรนด์อ้างอิง" not in stage_a_system, \
        "brand_reference block must NOT appear in Stage A system prompt"


def test_agent2_brand_interpretation_receives_product_audience(tmp_path, monkeypatch):
    """Agent 2: run the real evidence-mode pipeline through
    Orchestrator.run_competitor_analysis and capture the actual
    BrandInterpretationPass.interpret() invocation after finalized evidence.

    Do NOT manually instantiate the pass — let the pipeline call it.
    Use FakeLLM.chat() as the provider boundary, not BaseAgent.run() patch.
    """
    from src.orchestrator import Orchestrator
    from src.agents import competitor_analysis as comp_mod
    from src.config_loader import get_agent_config

    monkeypatch.chdir(tmp_path)
    brand_dir = _make_brand_dir(tmp_path)

    _make_product_profile(tmp_path / "cache", "ProfiledProduct", {
        "audience": {"primary": {"age": "30-50", "interest": "fitness"}},
        "differentiators": ["unique feature X"],
    })

    orch = Orchestrator(brand_dir=str(brand_dir))
    orch.bind_product("ProfiledProduct")

    # brand_reference must contain the product-specific audience
    assert "30-50" in orch.brand_reference or "fitness" in orch.brand_reference
    assert "unique feature X" in orch.brand_reference

    # A valid ResearchResponse JSON that passes _validate_research_json
    valid_research_json = json.dumps({
        "target_model": "ProfiledProduct",
        "competitor_names": ["CompA"],
        "evidence": [{
            "competitor": "CompA",
            "field": "price",
            "claim": "CompA costs $100",
            "url": "https://example.com/compa-price",
            "geography": "global",
        }],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })

    # Use FakeLLM.chat() as the provider boundary — no BaseAgent.run() patch
    fake_llm = FakeLLM(output=valid_research_json)

    # Patch only downstream boundaries to keep the test offline
    from src.agents.competitor_evidence import SemanticEvidenceReviewer
    monkeypatch.setattr(SemanticEvidenceReviewer, "review", lambda self, research, relevant: research)

    from src.agents.competitor_analysis import CompetitorReportRenderer
    monkeypatch.setattr(CompetitorReportRenderer, "validate", lambda self: [])

    # Capture the actual BrandInterpretationPass.interpret() call from the pipeline
    captured = {}

    def _capture_interpret(self, research, brand_reference="", product_spec="", quick_brief=""):
        captured["brand_reference"] = brand_reference
        captured["product_spec"] = product_spec
        captured["quick_brief"] = quick_brief
        captured["evidence_count"] = len(research.evidence)
        captured["competitor_names"] = research.competitor_names
        return []  # no implications

    monkeypatch.setattr(comp_mod.BrandInterpretationPass, "interpret", _capture_interpret)

    # Run through the REAL Orchestrator.run_competitor_analysis path
    orch.run_competitor_analysis("test spec", "", llm=fake_llm)

    # The BrandInterpretationPass.interpret must have been called by the pipeline
    assert "brand_reference" in captured, \
        "BrandInterpretationPass.interpret must be called by the evidence-mode pipeline"
    assert "30-50" in captured["brand_reference"] or "fitness" in captured["brand_reference"], \
        "interpret() must receive product-specific audience from brand_reference"
    assert "unique feature X" in captured["brand_reference"], \
        "interpret() must receive product-specific positioning from brand_reference"
    assert captured["evidence_count"] == 1, \
        "interpret() must receive finalized evidence (1 item)"
    assert "CompA" in captured["competitor_names"], \
        "interpret() must receive the competitor names from finalized evidence"


def test_agent3_audience_positioning_in_final_input(tmp_path, monkeypatch):
    """Agent 3 (campaign_strategy): run the real Orchestrator.run_campaign_strategy
    and capture the actual messages sent to llm.chat() — the real final model
    input boundary.

    Agent 3 enables use_brand_reference=True and use_brand_differentiator=True,
    so audience and positioning must appear in the system prompt.
    """
    from src.orchestrator import Orchestrator
    from src.config_loader import get_agent_config

    monkeypatch.chdir(tmp_path)
    brand_dir = _make_brand_dir(tmp_path)

    _make_product_profile(tmp_path / "cache", "ProfiledProduct", {
        "audience": {"primary": {"age": "30-50", "interest": "fitness"}},
        "differentiators": ["unique feature X"],
        "competitors": ["CompA"],
        "price_tier": "premium",
        "tone_adjustment": "โทนเฉพาะสินค้านี้",
    })

    orch = Orchestrator(brand_dir=str(brand_dir))
    orch.bind_product("ProfiledProduct")

    cfg = get_agent_config(orch.config, "campaign_strategy")
    assert cfg.get("use_brand_reference") is True
    assert cfg.get("use_brand_differentiator") is True

    # Use FakeLLM.chat() as the provider boundary — no BaseAgent.run() patch
    fake_llm = FakeLLM(
        output=(
            "## ราคาแนะนำ\n- pending: ราคาเริ่มต้น 1,990 บาท สำหรับแพ็คเกจพื้นฐาน\n"
            "## แคมเปญหลัก\n- Launch Campaign: เปิดตัวสินค้าด้วยการสร้าง awareness ผ่าน social media และ influencer marketing\n"
            "## แคมเปญเสริม\n- Influencer Partnership: ร่วมมือกับ fitness influencer เพื่อสร้างคอนเทนต์รีวิว\n"
            "## ช่องทางโปรโมท\n- Facebook Ads, TikTok, Instagram Reels, YouTube Shorts\n"
            "## KPI\n- Reach 1M impressions, 5% engagement rate, 10K clicks\n"
            "## งบประมาณ\n- pending: งบประมาณเบื้องต้น 50,000 บาทต่อเดือน\n"
            "## แหล่งอ้างอิง\n- https://example.com/market-research\n"
        )
    )

    orch.run_campaign_strategy("test spec", "", llm=fake_llm)

    # Inspect the actual messages sent to llm.chat()
    assert len(fake_llm.calls) > 0, "run_campaign_strategy must call llm.chat()"
    messages = fake_llm.calls[0]["messages"]

    system = ""
    for msg in messages:
        if msg["role"] == "system":
            system = msg["content"]

    # Audience must appear in system prompt (via brand_reference)
    assert "30-50" in system or "fitness" in system
    # Positioning/differentiators must appear
    assert "unique feature X" in system or "CompA" in system
    # Tone adjustment must appear (via brand_rules)
    assert "โทนเฉพาะสินค้านี้" in system


def test_agent4_visual_style_in_final_prompt_and_media_config(tmp_path, monkeypatch):
    """Agent 4 (content_creator): run the real Orchestrator.run_content_creator
    and capture the actual messages sent to llm.chat() — the real final model
    input boundary.

    Agent 4 receives visual_style in build_prompt (from brand_visual) and
    brand_reference in the system prompt.  The visual_style is embedded in
    the user prompt, so we inspect the user message content from llm.chat().
    """
    from src.orchestrator import Orchestrator
    from src.config_loader import get_agent_config

    monkeypatch.chdir(tmp_path)
    brand_dir = _make_brand_dir(tmp_path)

    # Create visual.json at brand level
    (brand_dir / "visual.json").write_text(json.dumps({
        "image_style": {"tone": "base tone"},
        "keywords": ["base keyword"],
    }), encoding="utf-8")

    _make_product_profile(tmp_path / "cache", "ProfiledProduct", {
        "audience": {"primary": {"age": "30-50", "interest": "fitness"}},
        "differentiators": ["unique feature X"],
        "visual_override": {
            "image_style": {"tone": "product specific visual tone"},
            "keywords": ["product specific keyword"],
        },
    })

    orch = Orchestrator(brand_dir=str(brand_dir))
    orch.bind_product("ProfiledProduct")

    cfg = get_agent_config(orch.config, "content_creator")
    assert cfg.get("use_brand_reference") is True

    # 1. Verify brand_visual reflects the product override (media-generation config)
    assert orch.brand_visual.get("image_style", {}).get("tone") == "product specific visual tone"
    assert "product specific keyword" in orch.brand_visual.get("keywords", [])

    # 2. Use FakeLLM.chat() as the provider boundary — no BaseAgent.run() patch
    fake_llm = FakeLLM(output=json.dumps({"posts": [{
        "platform": "Facebook", "concept": "test", "title": "T",
        "caption": "C", "hashtags": "#h", "asset_ids": [],
        "image_prompts": [], "video_prompts": [],
    }]}))

    orch.run_content_creator("test spec", "", "", llm=fake_llm)

    # Inspect the actual messages sent to llm.chat()
    assert len(fake_llm.calls) > 0, "run_content_creator must call llm.chat()"
    messages = fake_llm.calls[0]["messages"]

    system = ""
    user = ""
    for msg in messages:
        if msg["role"] == "system":
            system = msg["content"]
        elif msg["role"] == "user":
            # user content may be a string or a multimodal list
            if isinstance(msg["content"], list):
                user = " ".join(
                    part.get("text", "") for part in msg["content"]
                    if isinstance(part, dict)
                )
            else:
                user = msg["content"]

    # visual_style is embedded in the user prompt — must contain the override
    assert "product specific visual tone" in user, \
        "final user prompt must contain product-specific visual override"
    assert "product specific keyword" in user, \
        "final user prompt must contain product-specific keywords"

    # System prompt must contain audience (via brand_reference)
    assert "30-50" in system or "fitness" in system


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
