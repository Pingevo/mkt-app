"""MB-02 Slice 1 — brand-scoped business-state isolation.

Proves that products, brand files, content pillars, content history, and
generated outputs are isolated per brand (A1 vs A2 for the same user, and
across users A vs B).  All brand-scoped state must resolve beneath
``brand_state_root()`` (``users/<uid>/brands/<brand_id>/``), never the
user root, and never cross into another brand.

Seams under test (agreed):
  1. product_db.save/load/get_all_products  — cache/{product_id}/ + data/{product_id}/
  2. local_workspace local_brand_dir        — voice/terms/visual/audience/profile
  3. local_workspace content_pillars        — content_pillars.yaml
  4. content_history record/load            — cache/content_history.json
  5. web_viewer OUTPUT_DIR                   — output/{session}/
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.brand_registry import BrandRegistry
from src.workspace_context import (
    WorkspaceContext,
    set_workspace,
    reset_workspace,
    brand_state_root,
)


# ---------------------------------------------------------------------------
# Fixtures — User A / Brand A1, A2 ; User B / Brand B1
# ---------------------------------------------------------------------------

@pytest.fixture
def three_brands(tmp_path: Path):
    """Returns (project_root, user_a, a1_id, a2_id, user_b, b1_id)."""
    reg_a = BrandRegistry(user_id="user_aaa", project_root=tmp_path)
    reg_b = BrandRegistry(user_id="user_bbb", project_root=tmp_path)
    a1 = reg_a.create("A1")
    a2 = reg_a.create("A2")
    b1 = reg_b.create("B1")
    return (tmp_path, "user_aaa", a1["brand_id"], a2["brand_id"],
            "user_bbb", b1["brand_id"])


class _BrandCtx:
    """Context manager that sets/resets a brand workspace around a block."""

    def __init__(self, project: Path, user_id: str, brand_id: str):
        self.ws = WorkspaceContext.for_brand(user_id, brand_id, project)
        self.token = None

    def __enter__(self):
        self.token = set_workspace(self.ws)
        return self

    def __exit__(self, *exc):
        reset_workspace(self.token)


# ---------------------------------------------------------------------------
# 1. Products — A1/A2 isolation
# ---------------------------------------------------------------------------

def test_products_isolated_between_brands_same_user(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    from src import product_db

    with _BrandCtx(project, uid_a, a1_id):
        product_db.save("P1", {"product_id": "P1", "status": "ready"})
        product_db.set_status("P1", "ready")
        # get_all_products scans data/{product_id}/ — create it so the product is listed
        (product_db._project_root() / "data" / "P1").mkdir(parents=True, exist_ok=True)

    with _BrandCtx(project, uid_a, a2_id):
        product_db.save("P2", {"product_id": "P2", "status": "ready"})
        product_db.set_status("P2", "ready")
        (product_db._project_root() / "data" / "P2").mkdir(parents=True, exist_ok=True)

    with _BrandCtx(project, uid_a, a1_id):
        ids = sorted(p["product_id"] for p in product_db.get_all_products())
    with _BrandCtx(project, uid_a, a2_id):
        ids2 = sorted(p["product_id"] for p in product_db.get_all_products())

    assert ids == ["P1"], f"A1 must see only P1, got {ids}"
    assert ids2 == ["P2"], f"A2 must see only P2, got {ids2}"


def test_product_cache_dir_under_brand_root(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    from src import product_db

    with _BrandCtx(project, uid_a, a1_id):
        product_db.save("PX", {"product_id": "PX", "status": "ready"})
        cache_dir = product_db._product_dir("PX")

    expected = (project / "users" / uid_a / "brands" / a1_id / "cache" / "PX").resolve()
    assert cache_dir == expected, f"product cache must be under brand root, got {cache_dir}"


def test_products_isolated_across_users(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    from src import product_db

    with _BrandCtx(project, uid_a, a1_id):
        product_db.save("PA", {"product_id": "PA", "status": "ready"})
        (product_db._project_root() / "data" / "PA").mkdir(parents=True, exist_ok=True)
    with _BrandCtx(project, uid_b, b1_id):
        product_db.save("PB", {"product_id": "PB", "status": "ready"})
        (product_db._project_root() / "data" / "PB").mkdir(parents=True, exist_ok=True)
        ids = sorted(p["product_id"] for p in product_db.get_all_products())

    assert ids == ["PB"], f"B1 must see only PB, got {ids}"


# ---------------------------------------------------------------------------
# 2. Brand files — voice/terms/visual isolated per brand
# ---------------------------------------------------------------------------

def test_brand_files_isolated_between_brands(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    from src.local_workspace import local_brand_dir
    from src.brand_loader import load_brand_rules

    with _BrandCtx(project, uid_a, a1_id):
        bdir = local_brand_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "voice.json").write_text(
            '{"personality": "A1 บุคลิก"}', encoding="utf-8")

    with _BrandCtx(project, uid_a, a2_id):
        bdir = local_brand_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "voice.json").write_text(
            '{"personality": "A2 บุคลิก"}', encoding="utf-8")

    with _BrandCtx(project, uid_a, a1_id):
        rules = load_brand_rules()
    with _BrandCtx(project, uid_a, a2_id):
        rules2 = load_brand_rules()

    assert "A1 บุคลิก" in rules, f"A1 voice missing: {rules}"
    assert "A2 บุคลิก" in rules2, f"A2 voice missing: {rules2}"
    assert "A2 บุคลิก" not in rules, "A1 must not see A2 voice"
    assert "A1 บุคลิก" not in rules2, "A2 must not see A1 voice"


def test_brand_dir_under_brand_root(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    from src.local_workspace import local_brand_dir

    with _BrandCtx(project, uid_a, a1_id):
        bdir = local_brand_dir()
    expected = (project / "users" / uid_a / "brands" / a1_id / "brand").resolve()
    assert bdir == expected, f"brand dir must be under brand root, got {bdir}"


# ---------------------------------------------------------------------------
# 3. Content pillars — isolated per brand
# ---------------------------------------------------------------------------

def test_content_pillars_isolated_between_brands(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    from src.local_workspace import (
        save_content_pillars_local, load_content_pillars_local,
    )

    with _BrandCtx(project, uid_a, a1_id):
        save_content_pillars_local(["A1 เสาหลัก"], {"A1 เสาหลัก": ["k1"]})
    with _BrandCtx(project, uid_a, a2_id):
        save_content_pillars_local(["A2 เสาหลัก"], {"A2 เสาหลัก": ["k2"]})

    with _BrandCtx(project, uid_a, a1_id):
        pillars = load_content_pillars_local()
    with _BrandCtx(project, uid_a, a2_id):
        pillars2 = load_content_pillars_local()

    assert pillars["pillars"] == ["A1 เสาหลัก"], f"A1 pillars: {pillars}"
    assert pillars2["pillars"] == ["A2 เสาหลัก"], f"A2 pillars: {pillars2}"


# ---------------------------------------------------------------------------
# 4. Content history — isolated per brand
# ---------------------------------------------------------------------------

def test_content_history_isolated_between_brands(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    from src.content_history import record_entry, load_history

    with _BrandCtx(project, uid_a, a1_id):
        record_entry(project, "P1", "แนวคิด A1", "TikTok", "caption A1",
                     config={"dedup_enabled": False})
    with _BrandCtx(project, uid_a, a2_id):
        record_entry(project, "P2", "แนวคิด A2", "TikTok", "caption A2",
                     config={"dedup_enabled": False})

    with _BrandCtx(project, uid_a, a1_id):
        entries = load_history(project)["entries"]
    with _BrandCtx(project, uid_a, a2_id):
        entries2 = load_history(project)["entries"]

    assert len(entries) == 1 and entries[0]["concept"] == "แนวคิด A1"
    assert len(entries2) == 1 and entries2[0]["concept"] == "แนวคิด A2"


# ---------------------------------------------------------------------------
# 5. Output — isolated per brand
# ---------------------------------------------------------------------------

def test_output_dir_isolated_between_brands(three_brands, monkeypatch):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    import web_viewer

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", project)

    with _BrandCtx(project, uid_a, a1_id):
        out1 = web_viewer.OUTPUT_DIR()
        out1.mkdir(parents=True, exist_ok=True)
        (out1 / "session_a1").mkdir(parents=True, exist_ok=True)
        (out1 / "session_a1" / "content.md").write_text("A1 output", encoding="utf-8")

    with _BrandCtx(project, uid_a, a2_id):
        out2 = web_viewer.OUTPUT_DIR()
        out2.mkdir(parents=True, exist_ok=True)
        (out2 / "session_a2").mkdir(parents=True, exist_ok=True)
        (out2 / "session_a2" / "content.md").write_text("A2 output", encoding="utf-8")

    assert out1 == (project / "users" / uid_a / "brands" / a1_id / "output").resolve()
    assert out2 == (project / "users" / uid_a / "brands" / a2_id / "output").resolve()
    assert (out1 / "session_a1" / "content.md").exists()
    assert not (out1 / "session_a2").exists(), "A1 output must not contain A2 session"
    assert not (out2 / "session_a1").exists(), "A2 output must not contain A1 session"


# ---------------------------------------------------------------------------
# 6. Run resources — isolated per brand
# ---------------------------------------------------------------------------

def test_run_resources_isolated_between_brands(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    from src.run_resources import RunResourceStore

    with _BrandCtx(project, uid_a, a1_id):
        store1 = RunResourceStore(project, config={"enabled": True})
        sid1 = store1.create_upload_session()
        store1.upload("a1.txt", b"A1 content", session_id=sid1)
        sd1 = store1.storage_dir

    with _BrandCtx(project, uid_a, a2_id):
        store2 = RunResourceStore(project, config={"enabled": True})
        sid2 = store2.create_upload_session()
        store2.upload("a2.txt", b"A2 content", session_id=sid2)
        sd2 = store2.storage_dir

    expected1 = (project / "users" / uid_a / "brands" / a1_id / "cache" / "run_resources").resolve()
    expected2 = (project / "users" / uid_a / "brands" / a2_id / "cache" / "run_resources").resolve()
    assert sd1 == expected1, f"run_resources must be under brand root, got {sd1}"
    assert sd2 == expected2, f"run_resources must be under brand root, got {sd2}"

    # A1 store must not see A2's session directory at all
    a2_session_dir = sd2 / sid2
    assert a2_session_dir.exists(), "A2 session must exist in A2 brand store"
    assert not (sd1 / sid2).exists(), "A1 brand store must not contain A2 session"


# ---------------------------------------------------------------------------
# 7. Staging — isolated per brand
# ---------------------------------------------------------------------------

def test_staging_isolated_between_brands(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    from src.staging import _staging_root

    with _BrandCtx(project, uid_a, a1_id):
        sr1 = _staging_root()
    with _BrandCtx(project, uid_a, a2_id):
        sr2 = _staging_root()

    expected1 = (project / "users" / uid_a / "brands" / a1_id / "data" / ".staging").resolve()
    expected2 = (project / "users" / uid_a / "brands" / a2_id / "data" / ".staging").resolve()
    assert sr1 == expected1, f"staging must be under brand root, got {sr1}"
    assert sr2 == expected2, f"staging must be under brand root, got {sr2}"


# ---------------------------------------------------------------------------
# 8. Fail-closed — brand-scoped ops require active brand context
# ---------------------------------------------------------------------------

def test_product_db_fails_closed_without_brand_context(tmp_path):
    """Without a brand context, brand-scoped product ops must fail closed."""
    from src import product_db
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace

    # User-only context (no brand_id) — product ops must fail
    ws = WorkspaceContext.for_user("user_solo", tmp_path)
    token = set_workspace(ws)
    try:
        with pytest.raises(ValueError, match="brand"):
            product_db.get_all_products()
    finally:
        reset_workspace(token)
