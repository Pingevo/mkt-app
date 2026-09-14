"""Tests for content_history — output_file tracking + delete by output_file.

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)

MB-02: content history is brand-scoped — tests run inside a brand workspace.
"""
import sys
import tempfile
import shutil
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.brand_registry import BrandRegistry
from src.content_history import (
    record_entry,
    delete_entry_by_output_file,
    update_last_entry_output_file,
    load_history,
)
from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace


@contextmanager
def _brand_root():
    """Create a tmp project + brand workspace, yield (root, token).

    Content history is brand-scoped (MB-02), so a verified brand context is
    required.  The workspace is reset on exit.
    """
    root = Path(tempfile.mkdtemp())
    reg = BrandRegistry(user_id="test_user", project_root=root)
    brand = reg.create("TestBrand")
    ws = WorkspaceContext.for_brand("test_user", brand["brand_id"], root)
    token = set_workspace(ws)
    try:
        yield root
    finally:
        reset_workspace(token)
        shutil.rmtree(root)


def test_record_entry_with_output_file():
    """record_entry เก็บ output_file ใน entry."""
    with _brand_root() as root:
        record_entry(
            root,
            product_ids="TestProduct",
            concept="test concept",
            platform="TikTok",
            caption_summary="test caption",
            output_file="/tmp/test.md",
            config={"dedup_enabled": False},
        )
        h = load_history(root)
        assert len(h["entries"]) == 1
        assert h["entries"][0]["output_file"] == "/tmp/test.md"


def test_record_entry_backward_compat_no_output_file():
    """record_entry ไม่ส่ง output_file → ไม่มี field นั้นใน entry (backward compat)."""
    with _brand_root() as root:
        record_entry(
            root,
            product_ids="TestProduct",
            concept="test concept",
            platform="TikTok",
            caption_summary="test caption",
            config={"dedup_enabled": False},
        )
        h = load_history(root)
        assert len(h["entries"]) == 1
        assert "output_file" not in h["entries"][0]


def test_delete_entry_by_output_file():
    """delete_entry_by_output_file ลบ entry ที่มี output_file ตรงกัน."""
    with _brand_root() as root:
        record_entry(root, product_ids="A", concept="c1", platform="TikTok",
                     caption_summary="cap1", output_file="/tmp/a.md",
                     config={"dedup_enabled": False})
        record_entry(root, product_ids="B", concept="c2", platform="Instagram",
                     caption_summary="cap2", output_file="/tmp/b.md",
                     config={"dedup_enabled": False})
        removed = delete_entry_by_output_file(root, "/tmp/a.md")
        assert removed == 1
        h = load_history(root)
        assert len(h["entries"]) == 1
        assert h["entries"][0]["concept"] == "c2"


def test_delete_entry_no_match():
    """delete_entry_by_output_file ไม่มี match → ลบ 0."""
    with _brand_root() as root:
        record_entry(root, product_ids="A", concept="c1", platform="TikTok",
                     caption_summary="cap1", output_file="/tmp/a.md",
                     config={"dedup_enabled": False})
        removed = delete_entry_by_output_file(root, "/nonexistent.md")
        assert removed == 0
        h = load_history(root)
        assert len(h["entries"]) == 1


def test_update_last_entry_output_file():
    """update_last_entry_output_file อัปเดต entry ล่าสุด."""
    with _brand_root() as root:
        record_entry(root, product_ids="A", concept="c1", platform="TikTok",
                     caption_summary="cap1", config={"dedup_enabled": False})
        ok = update_last_entry_output_file(root, "/tmp/updated.md")
        assert ok
        h = load_history(root)
        assert h["entries"][0]["output_file"] == "/tmp/updated.md"


def test_update_last_entry_empty_history():
    """update_last_entry_output_file กับ history ว่าง → return False."""
    with _brand_root() as root:
        ok = update_last_entry_output_file(root, "/tmp/test.md")
        assert not ok


def test_delete_then_dedup_unblocked():
    """หลังลบ output_file → entry หาย → dedup ไม่บล็อกมุมมองเดิม."""
    with _brand_root() as root:
        record_entry(root, product_ids="A", concept="c1", platform="TikTok",
                     caption_summary="cap1", output_file="/tmp/a.md",
                     config={"dedup_enabled": False})
        # ลบ
        delete_entry_by_output_file(root, "/tmp/a.md")
        # บันทึกซ้ำ — ควรผ่านเพราะ history ว่างแล้ว
        record_entry(root, product_ids="A", concept="c1", platform="TikTok",
                     caption_summary="cap1", output_file="/tmp/a2.md",
                     config={"dedup_enabled": False})
        h = load_history(root)
        assert len(h["entries"]) == 1
        assert h["entries"][0]["output_file"] == "/tmp/a2.md"
