"""Regression test: ingest_product ต้อง preserve raw_text ของไฟล์เดิม
เมื่อเพิ่มไฟล์ใหม่แล้ว re-ingest (force=False).

บั๊ก: ingest_product เคลียร์ raw_text="" ก่อนลูป แล้ววนแค่ to_ingest
(ไฟล์ใหม่/hash เปลี่ยน) — ไฟล์เดิมที่ hash เหมือนเดิมถูกข้าม
→ raw_text ของไฟล์เดิมหาย.
"""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def _ingest(monkeypatch, tmp_path):
    """Setup tmp project root + reload ingestion/product_db."""
    import src.config_loader as config_loader
    import src.product_db as product_db

    monkeypatch.setattr(config_loader, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)

    # สร้าง config/ingestion.yaml ใน tmp_path (ใช้ของจริงจาก repo)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    real_cfg = Path(__file__).resolve().parent.parent / "config" / "ingestion.yaml"
    if real_cfg.exists():
        (cfg_dir / "ingestion.yaml").write_bytes(real_cfg.read_bytes())
    else:
        (cfg_dir / "ingestion.yaml").write_text(
            "supported_formats:\n  text: ['.txt', '.md']\n  image: ['.png', '.jpg', '.jpeg']\n",
            encoding="utf-8")

    # reload ingestion หลัง patch product_db/config_loader
    import src.ingestion as ingestion
    importlib.reload(ingestion)
    monkeypatch.setattr(ingestion, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(ingestion, "_make_llm", lambda: None)
    return ingestion


def test_incremental_ingest_preserves_existing_raw_text(_ingest, tmp_path):
    """เพิ่มไฟล์ใหม่ + re-ingest → raw_text ต้องมีทั้งของเดิมและของใหม่."""
    ingestion = _ingest
    product_id = "TestProduct"
    data_dir = tmp_path / "data" / product_id
    data_dir.mkdir(parents=True)

    # ไฟล์ที่ 1
    (data_dir / "file1.txt").write_text("FILE1 CONTENT", encoding="utf-8")

    # ingest ครั้งแรก
    ingestion.ingest_product(product_id, force=False)

    import src.product_db as product_db
    rec1 = product_db.load(product_id)
    assert "FILE1 CONTENT" in rec1.get("raw_text", ""), (
        f"sanity: file1 ต้อง ingest แล้ว — got raw_text={rec1.get('raw_text')!r}")

    # เพิ่มไฟล์ที่ 2
    (data_dir / "file2.txt").write_text("FILE2 CONTENT", encoding="utf-8")

    # re-ingest (force=False) — ไฟล์1 hash เหมือนเดิม → ข้าม; ไฟล์2 ใหม่ → ingest
    ingestion.ingest_product(product_id, force=False)

    rec2 = product_db.load(product_id)
    raw = rec2.get("raw_text", "")
    assert "FILE1 CONTENT" in raw, (
        f"BUG: raw_text ของ file1 หายหลัง re-ingest — got raw_text={raw!r}")
    assert "FILE2 CONTENT" in raw, (
        f"file2 ต้องอยู่ใน raw_text — got raw_text={raw!r}")


def test_incremental_ingest_preserves_existing_images(_ingest, tmp_path):
    """เพิ่มรูปใหม่ + re-ingest → image_descriptions ต้องมีทั้งของเดิมและของใหม่."""
    ingestion = _ingest
    product_id = "TestProduct2"
    data_dir = tmp_path / "data" / product_id
    data_dir.mkdir(parents=True)

    # รูปที่ 1
    (data_dir / "img1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    ingestion.ingest_product(product_id, force=False)

    import src.product_db as product_db
    rec1 = product_db.load(product_id)
    imgs1 = rec1.get("image_descriptions", [])
    assert any(i.get("file") == "img1.png" for i in imgs1), (
        f"sanity: img1 ต้อง ingest แล้ว — got {imgs1}")

    # เพิ่มรูปที่ 2
    (data_dir / "img2.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    ingestion.ingest_product(product_id, force=False)

    rec2 = product_db.load(product_id)
    imgs2 = rec2.get("image_descriptions", [])
    files = {i.get("file") for i in imgs2}
    assert "img1.png" in files, (
        f"BUG: img1 หายหลัง re-ingest — got {files}")
    assert "img2.png" in files, (
        f"img2 ต้องอยู่ — got {files}")


def test_ingest_after_delete_removes_stale_text(_ingest, tmp_path):
    """ลบไฟล์เดิม + re-ingest → text ของไฟล์ที่ลบต้องหายจาก raw_text."""
    ingestion = _ingest
    product_id = "TestProduct3"
    data_dir = tmp_path / "data" / product_id
    data_dir.mkdir(parents=True)

    (data_dir / "keep.txt").write_text("KEEP THIS", encoding="utf-8")
    (data_dir / "delete_me.txt").write_text("DELETE THIS", encoding="utf-8")

    ingestion.ingest_product(product_id, force=False)

    import src.product_db as product_db
    rec1 = product_db.load(product_id)
    assert "KEEP THIS" in rec1.get("raw_text", "")
    assert "DELETE THIS" in rec1.get("raw_text", "")

    # ลบไฟล์
    (data_dir / "delete_me.txt").unlink()

    # re-ingest — ไฟล์ที่ลบไม่อยู่ใน disk → ต้องเอา text ของมันออก
    ingestion.ingest_product(product_id, force=False)

    rec2 = product_db.load(product_id)
    raw = rec2.get("raw_text", "")
    assert "KEEP THIS" in raw, (
        f"BUG: text ของไฟล์ที่เก็บไว้หาย — got {raw!r}")
    assert "DELETE THIS" not in raw, (
        f"BUG: text ของไฟล์ที่ลบยังอยู่ — got {raw!r}")
