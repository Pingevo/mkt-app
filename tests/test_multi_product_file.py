"""Deterministic multi-product detection for structured file imports.

FREE-IMPORT contract: catalog splitting is decided by deterministic table
structure — a stable identity column (Model/SKU/Product/Item) with multiple
distinct values — never by a model call.

Real-source qualification (opt-in): point ``MKTAPP_CACGO_CATALOG_PDF`` at the
supplier catalog (``CACGO Smart Watch Price List- Grace.pdf``, 20 numbered
model rows) to run the real-document check.  It is never required CI state;
the generic contract is covered hermetically by the synthetic tests below.
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_CACGO_ENV = os.environ.get("MKTAPP_CACGO_CATALOG_PDF")
CACGO_PDF = Path(_CACGO_ENV).expanduser() if _CACGO_ENV else None


@pytest.fixture
def _ws(tmp_path):
    """Brand-scoped workspace under tmp_path — staging/product_db resolve inside it."""
    from src import workspace_context
    ws = workspace_context.WorkspaceContext(
        user_id="u1", root=tmp_path / "users" / "u1", brand_id="b1")
    workspace_context.set_workspace(ws)
    yield tmp_path
    workspace_context.set_workspace(None)


def _seg(files):
    from src.product_segmentation import segment_structured_tables
    return segment_structured_tables(files)


# ------------------------------------------------------------------
#  Real CACGO PDF — authoritative reproduction
# ------------------------------------------------------------------

@pytest.mark.skipif(
    CACGO_PDF is None or not CACGO_PDF.exists(),
    reason="real-source qualification — set MKTAPP_CACGO_CATALOG_PDF")
def test_cacgo_pdf_detects_twenty_products_via_staging(_ws, tmp_path):
    """The real price-list PDF must stage as MULTIPLE product candidates —
    one per Model row — not collapse into a single product."""
    from src import staging
    assert CACGO_PDF is not None

    batch_id = staging.create_batch(
        [(CACGO_PDF.name, CACGO_PDF.read_bytes())])
    result = staging.run_segmentation(batch_id)  # llm=None — free path

    segs = result["segments"]
    names = {s.get("suggested_name") or s.get("product_key") for s in segs}
    assert len(segs) > 1, \
        f"catalog PDF must not collapse to one product, got {names}"
    for model in ("K67", "K67A", "K76A", "K72", "K77", "K52"):
        assert model in names, f"missing product candidate {model} in {names}"
    assert len(segs) == 20, f"expected 20 numbered models, got {len(segs)}"
    # Row-level evidence: every candidate cites the source file.
    for s in segs:
        refs = s.get("source_refs", [])
        assert refs and all(r.get("file") == CACGO_PDF.name for r in refs), \
            "each candidate must keep row-level source provenance"

    from src import product_db
    chosen = [next(i for i, s in enumerate(segs) if s["product_key"] == key)
              for key in ("K67", "K72")]
    staging.commit_batch(batch_id, [
        {"segment_index": i, "action": "create"} for i in chosen])
    first = product_db.load("K67")
    second = product_db.load("K72")
    first_facts = {f["value"] for f in first["derived_facts"].values()}
    second_facts = {f["value"] for f in second["derived_facts"].values()}
    assert {"K67", "US$21.50"} <= first_facts
    assert {"K72", "US$22.50"} <= second_facts
    assert "K72" not in first_facts and "US$22.50" not in first_facts
    assert "K67" not in second_facts and "US$21.50" not in second_facts
    assert all(f.get("source_file") == CACGO_PDF.name
               for f in first["derived_facts"].values())
    assert all(f.get("source_file") == CACGO_PDF.name
               for f in second["derived_facts"].values())

    def media_hashes(record):
        return {
            hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
            for item in record["image_descriptions"]
            if not item.get("unassigned_source_media")
        }

    first_media = media_hashes(first)
    second_media = media_hashes(second)
    assert len(first_media) == 4 and len(second_media) == 4
    assert first_media - second_media
    assert second_media - first_media


# ------------------------------------------------------------------
#  Synthetic table structures — generic detector contract
# ------------------------------------------------------------------

def test_multi_product_commit_keeps_product_scoped_facts(_ws):
    from src import product_db, staging

    source = ("Model,Material,Price\n"
              "Atlas A,Titanium,$101\n"
              "Beacon B,Steel,$202\n")
    batch_id = staging.create_batch([("catalog.csv", source.encode())])
    result = staging.run_segmentation(batch_id)
    assert result["mode"] == "multi"

    staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create"},
        {"segment_index": 1, "action": "create"},
    ])
    atlas = product_db.load("Atlas A").get("derived_facts") or {}
    beacon = product_db.load("Beacon B").get("derived_facts") or {}
    atlas_pairs = {(f["label"], f["value"]) for f in atlas.values()}
    beacon_pairs = {(f["label"], f["value"]) for f in beacon.values()}

    assert {("Model", "Atlas A"), ("Material", "Titanium"),
            ("Price", "$101")} <= atlas_pairs
    assert {("Model", "Beacon B"), ("Material", "Steel"),
            ("Price", "$202")} <= beacon_pairs
    assert atlas_pairs != beacon_pairs
    assert not ({"Beacon B", "Steel", "$202"}
                & {value for _, value in atlas_pairs})
    assert not ({"Atlas A", "Titanium", "$101"}
                & {value for _, value in beacon_pairs})
    assert all(f.get("source_file") == "catalog.csv" for f in atlas.values())
    assert all(f.get("source_file") == "catalog.csv" for f in beacon.values())


def _two_product_pdf() -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=600, height=360)
    xs = [20, 80, 200, 480, 580]
    ys = [40, 80, 200, 320]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]), width=0.5)
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y), width=0.5)
    for x, text in zip(xs, ("No.", "Model", "Product Picture", "Price")):
        page.insert_text((x + 4, 65), text, fontsize=9)
    for y, values in ((110, ("1", "Atlas A", "$101")),
                      (230, ("2", "Beacon B", "$202"))):
        page.insert_text((24, y), values[0], fontsize=9)
        page.insert_text((84, y), values[1], fontsize=9)
        page.insert_text((484, y), values[2], fontsize=9)
    for rect, color in ((fitz.Rect(260, 105, 325, 170), 0xFF0000),
                        (fitz.Rect(260, 225, 325, 290), 0x0000FF)):
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 65, 65), False)
        pix.clear_with(color)
        page.insert_image(rect, stream=pix.tobytes("png"))
    data = doc.tobytes()
    doc.close()
    return data


def test_multi_product_pdf_keeps_product_scoped_media(_ws):
    from src import product_db, staging

    batch_id = staging.create_batch([("catalog.pdf", _two_product_pdf())])
    result = staging.run_segmentation(batch_id)
    assert [s["product_key"] for s in result["segments"]] == ["Atlas A", "Beacon B"]
    staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create"},
        {"segment_index": 1, "action": "create"},
    ])

    def owned(name):
        return [Path(i["path"]) for i in product_db.load(name)["image_descriptions"]
                if not i.get("unassigned_source_media")]

    atlas = owned("Atlas A")
    beacon = owned("Beacon B")
    assert len(atlas) == 1
    assert len(beacon) == 1
    assert {p.read_bytes() for p in atlas}.isdisjoint(
        {p.read_bytes() for p in beacon})
    assert product_db.get_product_image_paths("Atlas A") == [str(atlas[0])]
    assert product_db.get_product_image_paths("Beacon B") == [str(beacon[0])]


def test_identity_column_rows_become_candidates():
    """A table with a Model column and 3 distinct identities → 3 segments."""
    files = [{
        "name": "catalog.csv",
        "text": "No. | Model | Price\n1 | A1 | $10\n2 | B2 | $20\n3 | C3 | $30\n",
        "tables": [{"page": None, "rows": [
            ["No.", "Model", "Price"],
            ["1", "A1", "$10"],
            ["2", "B2", "$20"],
            ["3", "C3", "$30"],
        ]}],
    }]
    out = _seg(files)
    assert out is not None and out["mode"] == "multi"
    assert [s["product_key"] for s in out["products"]] == ["A1", "B2", "C3"]
    assert [(s["source_refs"][0]["line_start"],
             s["source_refs"][0]["line_end"])
            for s in out["products"]] == [(2, 2), (3, 3), (4, 5)]
    # row text is self-describing: header labels carry into the segment text
    assert "Model | B2" in out["products"][1]["text"]


def test_same_model_color_rows_stay_single():
    """One model with 3 color rows is ONE product — distinct identities,
    not row count, decide."""
    files = [{
        "name": "k5.csv",
        "text": "Model | Color | Price\nK5 | Black | $10\nK5 | Pink | $10\nK5 | Blue | $10\n",
        "tables": [{"page": None, "rows": [
            ["Model", "Color", "Price"],
            ["K5", "Black", "$10"],
            ["K5", "Pink", "$10"],
            ["K5", "Blue", "$10"],
        ]}],
    }]
    assert _seg(files) is None  # one distinct identity → not multi


def test_same_model_storage_options_stay_single():
    """Storage variants of one model → single product."""
    files = [{
        "name": "k5.csv",
        "text": "Model | Storage | Price\nK5 | 4GB | $10\nK5 | 8GB | $12\n",
        "tables": [{"page": None, "rows": [
            ["Model", "Storage", "Price"],
            ["K5", "4GB", "$10"],
            ["K5", "8GB", "$12"],
        ]}],
    }]
    assert _seg(files) is None


def test_spec_table_without_identity_is_not_split():
    """A hierarchical spec sheet (Section|Feature|Value) has no product
    identity column — it must not be split."""
    files = [{
        "name": "spec.xlsx",
        "text": "Battery | Capacity | 680mAh\nMemory | ROM | 8GB\nMemory | RAM | 1GB\n",
        "tables": [{"page": None, "rows": [
            ["Battery", "Capacity", "680mAh"],
            ["Memory", "ROM", "8GB"],
            ["Memory", "RAM", "1GB"],
            ["Display", "Type", "AMOLED"],
        ]}],
    }]
    assert _seg(files) is None


def test_comparison_table_is_not_split():
    """Spec|ModelA|ModelB comparison — 'Model' appears only as VALUES,
    never as a column header → no split."""
    files = [{
        "name": "cmp.csv",
        "text": "Spec | A1 | B2\nBattery | 100 | 200\nRAM | 1GB | 2GB\n",
        "tables": [{"page": None, "rows": [
            ["Spec", "A1", "B2"],
            ["Battery", "100", "200"],
            ["RAM", "1GB", "2GB"],
        ]}],
    }]
    assert _seg(files) is None


def test_no_tables_no_segmentation():
    files = [{"name": "a.txt", "text": "just prose", "tables": []}]
    assert _seg(files) is None


# ------------------------------------------------------------------
#  Base vs variant identity — Codex contract
# ------------------------------------------------------------------

def test_model_sku_color_one_base_product():
    """Model | SKU | Color — one base model with variant SKUs/colors =
    ONE product.  Variant identifiers never split a shared base."""
    files = [{
        "name": "k5.csv",
        "text": "Model | SKU | Color\nK5 | K5-BLK | Black\n"
                "K5 | K5-PINK | Pink\nK5 | K5-BLUE | Blue\n",
        "tables": [{"page": None, "rows": [
            ["Model", "SKU", "Color"],
            ["K5", "K5-BLK", "Black"],
            ["K5", "K5-PINK", "Pink"],
            ["K5", "K5-BLUE", "Blue"],
        ]}],
    }]
    assert _seg(files) is None


def test_sku_first_column_still_uses_base_model():
    """SKU | Model | Color — identity priority is semantic, not positional:
    the base Model column wins even when SKU appears first."""
    files = [{
        "name": "k5.csv",
        "text": "SKU | Model | Color\nK5-BLK | K5 | Black\n"
                "K5-PINK | K5 | Pink\nK5-BLUE | K5 | Blue\n",
        "tables": [{"page": None, "rows": [
            ["SKU", "Model", "Color"],
            ["K5-BLK", "K5", "Black"],
            ["K5-PINK", "K5", "Pink"],
            ["K5-BLUE", "K5", "Blue"],
        ]}],
    }]
    assert _seg(files) is None


def test_model_sku_storage_one_base_product():
    """Model | SKU | Storage — storage variants of one model = single."""
    files = [{
        "name": "px.csv",
        "text": "Model | SKU | Storage\nPhone X | X128 | 128GB\n"
                "Phone X | X256 | 256GB\n",
        "tables": [{"page": None, "rows": [
            ["Model", "SKU", "Storage"],
            ["Phone X", "X128", "128GB"],
            ["Phone X", "X256", "256GB"],
        ]}],
    }]
    assert _seg(files) is None


def test_distinct_models_with_skus_split():
    """Model | SKU — genuinely different base models = multi."""
    files = [{
        "name": "cat.csv",
        "text": "Model | SKU\nK67 | K67-A\nK72 | K72-A\nK52 | K52-A\n",
        "tables": [{"page": None, "rows": [
            ["Model", "SKU"],
            ["K67", "K67-A"],
            ["K72", "K72-A"],
            ["K52", "K52-A"],
        ]}],
    }]
    out = _seg(files)
    assert out is not None and out["mode"] == "multi"
    assert [s["product_key"] for s in out["products"]] == ["K67", "K72", "K52"]


def test_variant_rows_keep_evidence_in_one_segment():
    """Same base model repeated with variant SKUs + row-specific values →
    ONE segment retaining every row's evidence (all source_refs + the
    variant row text), while a genuinely different model still splits."""
    files = [{
        "name": "k5.csv",
        "text": "Model | SKU | Color | Price\n"
                "K5 | K5-BLK | Black | $10\nK5 | K5-PINK | Pink | $11\n"
                "K9 | K9 | Gray | $20\n",
        "tables": [{"page": None, "rows": [
            ["Model", "SKU", "Color", "Price"],
            ["K5", "K5-BLK", "Black", "$10"],
            ["K5", "K5-PINK", "Pink", "$11"],
            ["K9", "K9", "Gray", "$20"],
        ]}],
    }]
    out = _seg(files)
    assert out is not None and out["mode"] == "multi"
    assert [s["product_key"] for s in out["products"]] == ["K5", "K9"]
    k5 = out["products"][0]
    assert len(k5["source_refs"]) == 2, \
        "both variant rows must remain as source evidence"
    assert "K5-BLK" in k5["text"] and "K5-PINK" in k5["text"], \
        "variant rows must remain in the segment text"


def test_spec_row_with_model_cell_is_not_a_catalog():
    """A spec sheet whose DATA contains a 'Model' cell (General|Model|K2)
    must not pose as a catalog header — sibling cells aren't column names."""
    files = [{
        "name": "spec.xlsx",
        "text": "General | Model | K2\nBattery | Capacity | 680mAh\n"
                "Memory | ROM | 8GB\nDisplay | Type | AMOLED\n",
        "tables": [{"page": None, "rows": [
            ["General", "Model", "K2"],
            ["Battery", "Capacity", "680mAh"],
            ["Memory", "ROM", "8GB"],
            ["Display", "Type", "AMOLED"],
        ]}],
    }]
    assert _seg(files) is None


def test_sku_only_catalog_still_splits():
    """Only distinct SKUs + supporting field columns, no base identity —
    conservative split preserved (distinct item codes = distinct items)."""
    files = [{
        "name": "cat.csv",
        "text": "SKU | Description | Price\nA-100 | widget a | $1\n"
                "B-200 | widget b | $2\n",
        "tables": [{"page": None, "rows": [
            ["SKU", "Description", "Price"],
            ["A-100", "widget a", "$1"],
            ["B-200", "widget b", "$2"],
        ]}],
    }]
    out = _seg(files)
    assert out is not None
    assert [s["product_key"] for s in out["products"]] == ["A-100", "B-200"]
