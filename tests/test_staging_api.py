"""Integration tests for staging API endpoints (block A).

ทดสอบผ่าน FastAPI TestClient — ใช้ web_viewer app จริง
แต่ monkeypatch project_root ให้ staging/product_db ใช้ tmp_path.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def _client(monkeypatch, tmp_path):
    """Authed client + selected brand — staging APIs require brand context.

    ``make_brand_client`` patches ``web_viewer.PROJECT_ROOT`` to tmp_path, so
    every ``_state_root()``/``brand_state_root()`` resolution lands inside
    the per-user brand workspace — no ``_project_root`` patching needed.
    Returns (client, brand_root).
    """
    import web_viewer
    # monkeypatch ingestion._make_llm ให้คืน None — ไม่เรียก LLM จริงใน test
    import src.ingestion as ing
    monkeypatch.setattr(ing, "_make_llm", lambda: None)

    from tests.conftest import make_brand_client
    client, user_id, brand_id, brand_root = make_brand_client(
        web_viewer.app, tmp_path, monkeypatch)
    return client, brand_root, user_id, brand_id


# ------------------------------------------------------------------
#  POST /api/upload_stage
# ------------------------------------------------------------------

def test_upload_stage_creates_batch_and_returns_segments(_client):
    """POST /api/upload_stage → สร้าง batch + รัน segmentation + คืน segments/matches."""
    client, tmp_path, uid, bid = _client

    files = [("files", ("k2.txt", b"Lagenio K2 smartwatch", "text/plain"))]
    r = client.post("/api/upload_stage", files=files)

    assert r.status_code == 200
    data = r.json()
    assert data["batch_id"]
    assert len(data["segments"]) == 1
    assert "Lagenio K2 smartwatch" in data["segments"][0]["text"]
    assert len(data["matches"]) == 1
    assert data["matches"][0]["action"] == "create"


def test_upload_stage_skips_dotfiles(_client):
    """upload_stage ข้าม dotfile."""
    client, tmp_path, uid, bid = _client

    files = [
        ("files", (".DS_Store", b"junk", "application/octet-stream")),
        ("files", ("real.txt", b"data", "text/plain")),
    ]
    r = client.post("/api/upload_stage", files=files)

    data = r.json()
    assert [f["name"] for f in data["files"]] == ["real.txt"]


# ------------------------------------------------------------------
#  GET /api/stage/{batch_id}
# ------------------------------------------------------------------

def test_get_stage_returns_batch_info(_client):
    """GET /api/stage/{batch_id} → คืน batch info ที่เซฟไว้."""
    client, tmp_path, uid, bid = _client

    files = [("files", ("k2.txt", b"content", "text/plain"))]
    r = client.post("/api/upload_stage", files=files)
    batch_id = r.json()["batch_id"]

    r2 = client.get(f"/api/stage/{batch_id}")
    assert r2.status_code == 200
    data = r2.json()
    assert data["batch_id"] == batch_id
    assert len(data["segments"]) == 1


def test_get_stage_nonexistent_returns_404(_client):
    """GET /api/stage/{batch_id} ที่ไม่มี → 404."""
    client, tmp_path, uid, bid = _client
    r = client.get("/api/stage/nonexistent-batch")
    assert r.status_code == 404


# ------------------------------------------------------------------
#  POST /api/stage/{batch_id}/commit
# ------------------------------------------------------------------

def test_commit_stage_creates_product(_client):
    """POST /api/stage/{batch_id}/commit กับ action=create → สร้างสินค้า."""
    client, tmp_path, uid, bid = _client

    files = [("files", ("k2.txt", b"Lagenio K2 smartwatch", "text/plain"))]
    batch_id = client.post("/api/upload_stage", files=files).json()["batch_id"]

    r = client.post(f"/api/stage/{batch_id}/commit", json={
        "choices": [{"segment_index": 0, "action": "create", "name": "Lagenio K2"}]
    })
    assert r.status_code == 200
    data = r.json()
    assert data["created"] == ["Lagenio K2"]
    # สินค้าถูกสร้างจริง
    assert (tmp_path / "data" / "Lagenio K2" / "k2.txt").exists()


def test_commit_stage_update_existing(_client):
    """commit กับ action=update → อัปเดตสินค้าเดิม ไม่สร้างใหม่."""
    client, tmp_path, uid, bid = _client
    import src.product_db as product_db

    # สร้างสินค้าเดิมก่อน — direct product_db access needs brand context
    shared_text = "Lagenio K2 smartwatch"
    product_id = "Lagenio K2"
    d = tmp_path / "data" / product_id
    d.mkdir(parents=True)
    (d / "k2.txt").write_text(shared_text, encoding="utf-8")
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    # tmp_path here is brand_root = proj/users/<uid>/brands/<bid>
    token = set_workspace(WorkspaceContext.for_brand(uid, bid, tmp_path.parents[3]))
    try:
        rec = product_db.load(product_id)
        rec["product_id"] = product_id
        rec["status"] = product_db.STATUS_READY
        rec["raw_text"] = shared_text
        product_db.save(product_id, rec)
    finally:
        reset_workspace(token)

    files = [("files", ("k2.txt", shared_text.encode("utf-8"), "text/plain"))]
    batch_id = client.post("/api/upload_stage", files=files).json()["batch_id"]

    r = client.post(f"/api/stage/{batch_id}/commit", json={
        "choices": [{"segment_index": 0, "action": "update", "target": "Lagenio K2"}]
    })
    assert r.status_code == 200
    data = r.json()
    assert data["updated"] == ["Lagenio K2"]
    assert data["created"] == []


# ------------------------------------------------------------------
#  DELETE /api/stage/{batch_id}
# ------------------------------------------------------------------

def test_delete_stage_removes_batch(_client):
    """DELETE /api/stage/{batch_id} → ลบ staging."""
    client, tmp_path, uid, bid = _client

    files = [("files", ("k2.txt", b"content", "text/plain"))]
    batch_id = client.post("/api/upload_stage", files=files).json()["batch_id"]

    r = client.delete(f"/api/stage/{batch_id}")
    assert r.status_code == 200
    assert not (tmp_path / "data" / ".staging" / batch_id).exists()


def test_delete_stage_nonexistent_is_noop(_client):
    """DELETE /api/stage/{batch_id} ที่ไม่มี → ไม่ error."""
    client, tmp_path, uid, bid = _client
    r = client.delete("/api/stage/nonexistent")
    assert r.status_code == 200
