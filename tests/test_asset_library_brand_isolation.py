"""ASSET-ISO-01 — Brand Asset Library catalog must be brand-scoped.

Contract under test:
  - Under an active brand workspace the catalog resolves beneath that brand
    (``users/<uid>/brands/<bid>/cache/assets/db.json``), never the user-level
    shared catalog.
  - User-only workspace → fail closed (ValueError), like ``local_brand_dir``.
  - No-workspace CLI → legacy ``workspace/local/cache/assets/db.json`` fallback.
  - Every read/mutation op is brand-isolated: list, query, get, path
    resolution, update, delete, Agent 4 auto selection, media references.
  - Legacy shared catalogs migrate once per brand: records are claimed only
    when their stored path resolves inside that brand's ``brand/assets/`` dir;
    everything else stays quarantined in the untouched legacy file.
"""
import json
from pathlib import Path

import pytest

from src import asset_library
from src.auth import UserStore
from src.brand_registry import BrandRegistry
from src.workspace_context import (
    WorkspaceContext,
    reset_workspace,
    set_workspace,
)

_CFG = {
    "supported_formats": {"image": [".png"], "text": [".txt"]},
    "max_file_size_mb": {"image": 10, "text": 10},
    "query": {"default_top_k": 8, "max_top_k": 20},
}


def _tag(*a, **k):
    return {"subject": "logo", "style": "flat", "tags": ["t"], "description": "d"}


def _emb(*a, **k):
    return None


def _make_user(tmp_path, name="u_iso"):
    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    return UserStore(users_path).register(name, f"pw_{name}").user_id


def _make_brand(uid, tmp_path, name):
    return BrandRegistry(user_id=uid, project_root=tmp_path).create(name)["brand_id"]


def _brand_ctx(uid, bid, tmp_path):
    return set_workspace(WorkspaceContext.for_brand(uid, bid, tmp_path))


def _assets_dir(tmp_path, uid, bid):
    d = tmp_path / "users" / uid / "brands" / bid / "brand" / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ingest(path, note=""):
    return asset_library.ingest_asset(
        path, llm=None, user_note=note, tagger=_tag, embedder=_emb, config=_CFG
    )


@pytest.fixture
def two_brands(tmp_path):
    uid = _make_user(tmp_path)
    a = _make_brand(uid, tmp_path, "BrandA")
    b = _make_brand(uid, tmp_path, "BrandB")
    return {"uid": uid, "a": a, "b": b, "root": tmp_path}


# ---------------------------------------------------------------------------
# Core isolation — red on the current user-scoped catalog
# ---------------------------------------------------------------------------

def test_ingest_under_a_invisible_under_b(two_brands):
    uid, a, b, root = two_brands["uid"], two_brands["a"], two_brands["b"], two_brands["root"]
    tok = _brand_ctx(uid, a, root)
    a_file = _assets_dir(root, uid, a) / "logo_a.png"
    a_file.write_bytes(b"png-a")
    rec = _ingest(a_file)
    assert rec["status"] == "ready"
    a_id = rec["id"]
    reset_workspace(tok)

    tok = _brand_ctx(uid, b, root)
    try:
        assert [x["id"] for x in asset_library.list_all()] == []
        assert asset_library.query_assets() == []
        assert asset_library.get_asset(a_id) is None
        assert asset_library.get_asset_paths([a_id]) == []
        assert asset_library.update_asset(a_id, description="x") is None
        assert asset_library.delete_asset(a_id) is False
        # Media reference construction cannot resolve the foreign asset id.
        catalog = asset_library.build_reference_catalog([], [a_id])
        assert catalog == []
    finally:
        reset_workspace(tok)
    assert a_file.exists(), "foreign brand ops must not touch A's file"


def test_brand_a_retains_access_after_switch(two_brands):
    uid, a, b, root = two_brands["uid"], two_brands["a"], two_brands["b"], two_brands["root"]
    tok = _brand_ctx(uid, a, root)
    f = _assets_dir(root, uid, a) / "logo_a.png"
    f.write_bytes(b"png-a")
    rec = _ingest(f)
    reset_workspace(tok)

    tok = _brand_ctx(uid, b, root)
    assert asset_library.list_all() == []
    reset_workspace(tok)

    tok = _brand_ctx(uid, a, root)
    try:
        ids = [x["id"] for x in asset_library.list_all()]
        assert ids == [rec["id"]]
        assert asset_library.get_asset(rec["id"])["path"] == str(f.resolve())
        assert asset_library.get_asset_paths([rec["id"]]) == [str(f.resolve())]
    finally:
        reset_workspace(tok)


def test_fail_closed_under_user_only_workspace(two_brands, tmp_path):
    uid = two_brands["uid"]
    tok = set_workspace(WorkspaceContext.for_user(uid, tmp_path))
    try:
        with pytest.raises(ValueError):
            asset_library.list_all()
        with pytest.raises(ValueError):
            asset_library.query_assets()
        with pytest.raises(ValueError):
            asset_library.get_asset("a_0001")
        with pytest.raises(ValueError):
            asset_library.get_asset_paths(["a_0001"])
        with pytest.raises(ValueError):
            asset_library.update_asset("a_0001", description="x")
        with pytest.raises(ValueError):
            asset_library.delete_asset("a_0001")
        with pytest.raises(ValueError):
            asset_library.build_reference_catalog([], ["a_0001"])
    finally:
        reset_workspace(tok)


def test_no_workspace_cli_fallback_preserved(tmp_path, monkeypatch):
    # No workspace context → legacy workspace/local catalog still works (CLI compat).
    import src.local_workspace as lw
    monkeypatch.setattr(lw, "_project_root", lambda: tmp_path)
    f = tmp_path / "workspace" / "local" / "brand" / "assets"
    f.mkdir(parents=True, exist_ok=True)
    img = f / "cli.png"
    img.write_bytes(b"cli")
    rec = _ingest(img)
    assert rec["status"] == "ready"
    assert [a["id"] for a in asset_library.list_all()] == [rec["id"]]
    assert (tmp_path / "workspace" / "local" / "cache" / "assets" / "db.json").exists()


def test_user_isolation(tmp_path):
    u1 = _make_user(tmp_path, "u_one")
    u2 = _make_user(tmp_path, "u_two")
    b1 = _make_brand(u1, tmp_path, "U1Brand")
    b2 = _make_brand(u2, tmp_path, "U2Brand")
    tok = _brand_ctx(u1, b1, tmp_path)
    f = _assets_dir(tmp_path, u1, b1) / "u1.png"
    f.write_bytes(b"u1")
    rec = _ingest(f)
    reset_workspace(tok)

    tok = _brand_ctx(u2, b2, tmp_path)
    try:
        assert asset_library.list_all() == []
        assert asset_library.get_asset(rec["id"]) is None
    finally:
        reset_workspace(tok)


def test_persistence_across_context_reset(two_brands):
    uid, a, b, root = two_brands["uid"], two_brands["a"], two_brands["b"], two_brands["root"]
    tok = _brand_ctx(uid, a, root)
    f = _assets_dir(root, uid, a) / "persist.png"
    f.write_bytes(b"p")
    rec = _ingest(f)
    reset_workspace(tok)
    # Simulate restart: fresh context, catalog re-read from disk.
    tok = _brand_ctx(uid, a, root)
    assert [x["id"] for x in asset_library.list_all()] == [rec["id"]]
    reset_workspace(tok)
    tok = _brand_ctx(uid, b, root)
    assert asset_library.list_all() == []
    reset_workspace(tok)


def test_agent4_auto_selection_cannot_see_foreign_asset(two_brands):
    uid, a, b, root = two_brands["uid"], two_brands["a"], two_brands["b"], two_brands["root"]
    tok = _brand_ctx(uid, a, root)
    f = _assets_dir(root, uid, a) / "sel.png"
    f.write_bytes(b"s")
    rec = _ingest(f)
    reset_workspace(tok)

    from src.orchestrator import Orchestrator

    tok = _brand_ctx(uid, b, root)
    try:
        orch = Orchestrator()
        # Empty catalog → early return without consuming the LLM at all.
        summary = orch._select_assets_for_content(llm=None, concept="x", product_ids=["p"])
        assert summary == ""
        assert orch._selected_asset_ids == []
        # Even a preselected foreign id cannot resolve into the catalog/paths.
        assert asset_library.get_asset_paths([rec["id"]]) == []
    finally:
        reset_workspace(tok)


# ---------------------------------------------------------------------------
# Legacy shared-catalog migration — deterministic, idempotent, non-destructive
# ---------------------------------------------------------------------------

def _legacy_db_path(tmp_path, uid):
    return tmp_path / "users" / uid / "workspace" / "local" / "cache" / "assets" / "db.json"


def _write_legacy(tmp_path, uid, records):
    p = _legacy_db_path(tmp_path, uid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"assets": records, "next_id": 99}, indent=2), encoding="utf-8")
    return p


def test_legacy_migration_claims_provable_and_quarantines_rest(two_brands):
    uid, a, b, root = two_brands["uid"], two_brands["a"], two_brands["b"], two_brands["root"]
    a_dir = _assets_dir(root, uid, a)
    inside = a_dir / "legacy_a.png"
    inside.write_bytes(b"in-a")
    missing_inside = a_dir / "gone_a.png"          # path inside A, file absent
    outside = root / "elsewhere.png"
    outside.write_bytes(b"out")
    records = [
        {"id": "a_0001", "file": "legacy_a.png", "path": str(inside.resolve()),
         "type": "image", "status": "ready", "hash": "h1", "created_at": "t"},
        {"id": "a_0002", "file": "gone_a.png", "path": str(missing_inside.resolve()),
         "type": "image", "status": "ready", "hash": "h2", "created_at": "t"},
        {"id": "a_0003", "file": "elsewhere.png", "path": str(outside.resolve()),
         "type": "image", "status": "ready", "hash": "h3", "created_at": "t"},
        {"id": "a_0004", "file": "nopath.png", "path": "",
         "type": "image", "status": "ready", "hash": "h4", "created_at": "t"},
    ]
    legacy = _write_legacy(root, uid, records)
    legacy_bytes_before = legacy.read_bytes()

    tok = _brand_ctx(uid, a, root)
    try:
        ids = sorted(x["id"] for x in asset_library.list_all())
        # Path-inside-A records claimed (missing file still proves ownership by path).
        assert ids == ["a_0001", "a_0002"]
        # get_asset_paths skips the missing file but resolves the real one.
        assert asset_library.get_asset_paths(["a_0001", "a_0002"]) == [str(inside.resolve())]
        # Stable ids + next_id continues after the highest claimed id.
        new_file = a_dir / "new.png"
        new_file.write_bytes(b"new")
        new = _ingest(new_file)
        assert new["id"] == "a_0003"  # next_id = max(claimed)+1 within this brand
    finally:
        reset_workspace(tok)

    # Legacy preserved byte-for-byte (non-destructive quarantine).
    assert legacy.read_bytes() == legacy_bytes_before
    # Migration report records claimed + quarantined sets.
    report_path = root / "users" / uid / "brands" / a / "cache" / "assets" / "migration_report.json"
    report = json.loads(report_path.read_text())
    assert sorted(report["claimed_ids"]) == ["a_0001", "a_0002"]
    assert sorted(r["id"] for r in report["quarantined"]) == ["a_0003", "a_0004"]

    # Under B: provable-for-A records are NOT claimed here; legacy still untouched.
    tok = _brand_ctx(uid, b, root)
    try:
        assert asset_library.list_all() == []
        assert asset_library.get_asset("a_0001") is None
    finally:
        reset_workspace(tok)
    assert legacy.read_bytes() == legacy_bytes_before

    # Repeated access under A is idempotent — same view, no re-migration drift.
    tok = _brand_ctx(uid, a, root)
    try:
        ids2 = sorted(x["id"] for x in asset_library.list_all())
        assert ids2 == ["a_0001", "a_0002", "a_0003"]  # includes the new ingest
    finally:
        reset_workspace(tok)


def test_legacy_migration_absent_legacy_is_noop(two_brands):
    uid, a, root = two_brands["uid"], two_brands["a"], two_brands["root"]
    tok = _brand_ctx(uid, a, root)
    try:
        assert asset_library.list_all() == []
        assert not _legacy_db_path(root, uid).exists()
    finally:
        reset_workspace(tok)
