"""Tests for content_history — output_file tracking + delete by output_file.

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)
"""
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.content_history import (
    record_entry,
    delete_entry_by_output_file,
    update_last_entry_output_file,
    load_history,
)


def _tmp_root():
    """สร้าง temp dir สำหรับ test."""
    return Path(tempfile.mkdtemp())


def test_record_entry_with_output_file():
    """record_entry เก็บ output_file ใน entry."""
    root = _tmp_root()
    try:
        record_entry(
            root,
            product_ids="TestProduct",
            concept="test concept",
            platform="TikTok",
            caption_summary="test caption",
            output_file="/tmp/test.md",
        )
        h = load_history(root)
        assert len(h["entries"]) == 1
        assert h["entries"][0]["output_file"] == "/tmp/test.md"
    finally:
        shutil.rmtree(root)


def test_record_entry_backward_compat_no_output_file():
    """record_entry ไม่ส่ง output_file → ไม่มี field นั้นใน entry (backward compat)."""
    root = _tmp_root()
    try:
        record_entry(
            root,
            product_ids="TestProduct",
            concept="test concept",
            platform="TikTok",
            caption_summary="test caption",
        )
        h = load_history(root)
        assert len(h["entries"]) == 1
        assert "output_file" not in h["entries"][0]
    finally:
        shutil.rmtree(root)


def test_delete_entry_by_output_file():
    """delete_entry_by_output_file ลบ entry ที่มี output_file ตรงกัน."""
    root = _tmp_root()
    try:
        record_entry(root, product_ids="A", concept="c1", platform="TikTok",
                     caption_summary="cap1", output_file="/tmp/a.md")
        record_entry(root, product_ids="B", concept="c2", platform="Instagram",
                     caption_summary="cap2", output_file="/tmp/b.md")
        removed = delete_entry_by_output_file(root, "/tmp/a.md")
        assert removed == 1
        h = load_history(root)
        assert len(h["entries"]) == 1
        assert h["entries"][0]["concept"] == "c2"
    finally:
        shutil.rmtree(root)


def test_delete_entry_no_match():
    """delete_entry_by_output_file ไม่มี match → ลบ 0."""
    root = _tmp_root()
    try:
        record_entry(root, product_ids="A", concept="c1", platform="TikTok",
                     caption_summary="cap1", output_file="/tmp/a.md")
        removed = delete_entry_by_output_file(root, "/nonexistent.md")
        assert removed == 0
        h = load_history(root)
        assert len(h["entries"]) == 1
    finally:
        shutil.rmtree(root)


def test_update_last_entry_output_file():
    """update_last_entry_output_file อัปเดต entry ล่าสุด."""
    root = _tmp_root()
    try:
        record_entry(root, product_ids="A", concept="c1", platform="TikTok",
                     caption_summary="cap1")
        ok = update_last_entry_output_file(root, "/tmp/updated.md")
        assert ok
        h = load_history(root)
        assert h["entries"][0]["output_file"] == "/tmp/updated.md"
    finally:
        shutil.rmtree(root)


def test_update_last_entry_empty_history():
    """update_last_entry_output_file กับ history ว่าง → return False."""
    root = _tmp_root()
    try:
        ok = update_last_entry_output_file(root, "/tmp/test.md")
        assert not ok
    finally:
        shutil.rmtree(root)


def test_delete_then_dedup_unblocked():
    """หลังลบ output_file → entry หาย → dedup ไม่บล็อกมุมมองเดิม."""
    root = _tmp_root()
    try:
        record_entry(root, product_ids="A", concept="c1", platform="TikTok",
                     caption_summary="cap1", output_file="/tmp/a.md")
        # ลบ
        delete_entry_by_output_file(root, "/tmp/a.md")
        # บันทึกซ้ำ — ควรผ่านเพราะ history ว่างแล้ว
        record_entry(root, product_ids="A", concept="c1", platform="TikTok",
                     caption_summary="cap1", output_file="/tmp/a2.md")
        h = load_history(root)
        assert len(h["entries"]) == 1
        assert h["entries"][0]["output_file"] == "/tmp/a2.md"
    finally:
        shutil.rmtree(root)
