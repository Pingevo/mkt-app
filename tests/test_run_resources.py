from __future__ import annotations

import io
from pathlib import Path

import pytest

from src.run_resources import RunResourceStore


def _minimal_config(overrides: dict | None = None) -> dict:
    cfg = {
        "enabled": True,
        "storage_dir": "",
        "ttl_hours": 24,
        "max_files_per_flow": 5,
        "max_file_size_mb": 15,
        "max_total_size_mb": 30,
        "max_extracted_chars_total": 120000,
        "max_extracted_chars_per_file": 50,
        "allowed_extensions": [
            ".txt", ".md", ".csv", ".pdf", ".xlsx", ".xls", ".docx",
            ".png", ".jpg", ".jpeg", ".webp",
        ],
    }
    if overrides:
        cfg.update(overrides)
    return cfg


@pytest.fixture
def store(tmp_path: Path):
    return RunResourceStore(
        project_root=tmp_path,
        storage_dir=tmp_path / "run_resources",
        config=_minimal_config(),
    )


def test_upload_text_then_extract(store: RunResourceStore, tmp_path: Path):
    result = store.upload(
        filename="note.txt",
        content=b"Hello from user",
        media_type="text/plain",
    )
    assert result["status"] == "ready"
    assert result["name"] == "note.txt"
    assert result["size_bytes"] == 15
    assert result["extracted_text"].strip() == "Hello from user"
    # resource is retrievable within its own scope
    found = store.get_resource(result["resource_id"], result["scope_id"])
    assert found is not None
    assert found["resource_id"] == result["resource_id"]


def test_upload_rejects_unsupported_extension(store: RunResourceStore):
    result = store.upload(filename="evil.exe", content=b"payload", media_type=None)
    assert result["status"] == "rejected_extension"
    assert result.get("resource_id") is None


def test_upload_rejects_oversized_file(store: RunResourceStore):
    cfg = _minimal_config({"max_file_size_mb": 0.00001})
    small_store = RunResourceStore(
        project_root=store.project_root,
        storage_dir=store.storage_dir,
        config=cfg,
    )
    result = small_store.upload(filename="huge.txt", content=b"x" * 1000, media_type=None)
    assert result["status"] == "rejected_size"


def test_cross_scope_resolve_is_denied(store: RunResourceStore):
    first = store.upload(filename="secret.txt", content=b"confidential", media_type=None)
    resource_id = first["resource_id"]
    other_session = store.create_upload_session()
    assert store.get_resource(resource_id, other_session) is None


def test_build_context_splits_text_and_images(store: RunResourceStore, tmp_path: Path):
    txt = store.upload(filename="data.txt", content=b"text content here", media_type=None)
    img = store.upload(filename="photo.png", content=b"\x89PNG\r\n\x1a\nfake", media_type="image/png")
    records = [txt, img]
    ctx = store.build_resource_context(records)
    assert "--- User-provided resources ---" in ctx["text"]
    assert "text content here" in ctx["text"]
    assert "photo.png" in ctx["text"]
    assert len(ctx["image_paths"]) == 1
    assert Path(ctx["image_paths"][0]).name == "photo.png"


def test_extracted_text_truncated_and_marked(store: RunResourceStore):
    cfg = _minimal_config({"max_extracted_chars_per_file": 10})
    small_store = RunResourceStore(
        project_root=store.project_root,
        storage_dir=store.storage_dir,
        config=cfg,
    )
    result = small_store.upload(
        filename="long.txt",
        content=b"This is a very long sentence",
        media_type=None,
    )
    assert result["status"] == "ready"
    assert "[truncated]" in result["extracted_text"]
    assert len(result["extracted_text"].replace("[truncated]", "")) <= 10


def test_upload_rejects_invalid_session_id(store: RunResourceStore):
    result = store.upload(
        filename="note.txt",
        content=b"hello",
        session_id="../../etc/passwd",
    )
    assert result["status"] == "invalid_session"


def test_upload_rejects_path_traversal_session(store: RunResourceStore, tmp_path: Path):
    # ถ้า session id ไม่อยู่ในรูปแบบ จะไม่สร้าง dir นอก storage
    result = store.upload(
        filename="note.txt",
        content=b"hello",
        session_id="upload_session_1234567890abcdef12",
    )
    assert result["status"] == "ready"
    assert (tmp_path / "etc").exists() is False


def test_upload_rejects_too_many_files(store: RunResourceStore):
    cfg = _minimal_config({"max_files_per_flow": 2})
    small_store = RunResourceStore(
        project_root=store.project_root,
        storage_dir=store.storage_dir,
        config=cfg,
    )
    session_id = small_store.create_upload_session()
    for i in range(3):
        result = small_store.upload(
            filename=f"note{i}.txt",
            content=b"hello",
            session_id=session_id,
        )
    assert result["status"] == "rejected_file_count"


def test_build_context_honors_total_budget(store: RunResourceStore):
    cfg = _minimal_config({
        "max_extracted_chars_per_file": 100,
        "max_extracted_chars_total": 10,
    })
    small_store = RunResourceStore(
        project_root=store.project_root,
        storage_dir=store.storage_dir,
        config=cfg,
    )
    r1 = small_store.upload(filename="a.txt", content=b"1234567890abc")
    r2 = small_store.upload(filename="b.txt", content=b"1234567890abc")
    ctx = small_store.build_resource_context([r1, r2], workflow_id="wf_1", step_id="st_1")
    assert ctx["trace"][0]["workflow_id"] == "wf_1"
    assert ctx["trace"][1]["truncated"] is True
    assert "total budget 10 chars reached" in ctx["text"]


def test_resolve_input_ref_with_invalid_session(store: RunResourceStore):
    rec = store.resolve_input_ref("resource:res_1234567890abcdef12", "../../etc")
    assert rec is None


def test_delete_and_cleanup_expired(store: RunResourceStore):
    cfg = _minimal_config({"ttl_hours": -1})
    exp_store = RunResourceStore(
        project_root=store.project_root,
        storage_dir=store.storage_dir,
        config=cfg,
    )
    rec = exp_store.upload(filename="old.txt", content=b"old")
    resource_id = rec["resource_id"]
    session_id = rec["scope_id"]
    # expired ทันที
    assert exp_store.get_resource(resource_id, session_id) is None
    removed = exp_store.cleanup_expired()
    assert removed >= 1
    # path ไม่อยู่แล้ว
    assert exp_store.get_resource(resource_id, session_id) is None
    # ลบสดได้เฉพาะ resource ใหม่
    fresh = store.upload(filename="fresh.txt", content=b"fresh")
    assert store.delete_resource(fresh["resource_id"], fresh["scope_id"]) is True
    assert store.get_resource(fresh["resource_id"], fresh["scope_id"]) is None
