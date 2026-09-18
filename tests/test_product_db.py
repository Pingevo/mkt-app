"""Tests for product_db helpers ที่ใช้ตอนอัปโหลดซ้ำ (idempotent re-import).

ทดสอบผ่าน public interface ของ product_db — ไม่ mock internals.
"""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def _pdb(monkeypatch, tmp_path):
    """Setup tmp project root + reload product_db."""
    import src.product_db as product_db

    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    importlib.reload(product_db)
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    return product_db


# ------------------------------------------------------------------
#  get_existing_file_hashes — ใช้ตรวจ duplicate ตอนอัปโหลดไฟล์เดิมซ้ำ
# ------------------------------------------------------------------

def test_get_existing_file_hashes_returns_hashes_from_db_record(_pdb, tmp_path):
    """สินค้าที่ ingest แล้ว → คืน set ของ hash ของไฟล์ทั้งหมดใน record."""
    product_db = _pdb
    product_id = "CACGO K67"

    # สร้าง DB record ที่มีไฟล์ 2 ไฟล์พร้อม hash
    record = product_db.load(product_id)  # empty record
    record["files"] = [
        {"name": "a.txt", "path": "x", "hash": "hash_a", "status": "ingested"},
        {"name": "b.txt", "path": "y", "hash": "hash_b", "status": "ingested"},
    ]
    product_db.save(product_id, record)

    hashes = product_db.get_existing_file_hashes(product_id)

    assert hashes == {"hash_a", "hash_b"}


def test_get_existing_file_hashes_empty_for_new_product(_pdb, tmp_path):
    """สินค้าใหม่ที่ยังไม่มี record → คืน set ว่าง (ไม่ error)."""
    product_db = _pdb
    hashes = product_db.get_existing_file_hashes("สินค้าใหม่ที่ไม่มี")

    assert hashes == set()


def test_get_existing_file_hashes_skips_files_without_hash(_pdb, tmp_path):
    """ไฟล์ที่ยังไม่มี hash (ยังไม่ ingest) ไม่ถูกนับ — ป้องกันข้ามไฟล์จริง."""
    product_db = _pdb
    product_id = "mix"

    record = product_db.load(product_id)
    record["files"] = [
        {"name": "a.txt", "path": "x", "hash": "hash_a", "status": "ingested"},
        {"name": "b.txt", "path": "y", "hash": None, "status": "pending"},
        {"name": "c.txt", "path": "z", "status": "pending"},  # ไม่มี hash key เลย
    ]
    product_db.save(product_id, record)

    hashes = product_db.get_existing_file_hashes(product_id)

    assert hashes == {"hash_a"}


# ------------------------------------------------------------------
#  save_uploaded_files — เซฟไฟล์อัปโหลด ข้ามเนื้อซ้ำ (idempotent file layer)
# ------------------------------------------------------------------

def test_save_uploaded_files_new_product_saves_all(_pdb, tmp_path):
    """สินค้าใหม่ (ยังไม่มี DB record) → เซฟไฟล์ทั้งหมด คืนชื่อที่เซฟ."""
    product_db = _pdb
    product_id = "สินค้าใหม่"

    saved = product_db.save_uploaded_files(product_id, [
        ("a.txt", b"hello"),
        ("b.txt", b"world"),
    ])

    assert sorted(saved) == ["a.txt", "b.txt"]
    data_dir = tmp_path / "data" / product_id
    assert (data_dir / "a.txt").read_bytes() == b"hello"
    assert (data_dir / "b.txt").read_bytes() == b"world"


def test_save_uploaded_files_skips_duplicate_by_hash(_pdb, tmp_path):
    """อัปโหลดไฟล์เดิมซ้ำ (เนื้อเดิม) → ข้าม ไม่สร้างก้อนซ้ำ."""
    product_db = _pdb
    product_id = "มีอยู่"

    # สินค้ามีอยู่แล้ว: มี a.txt ในโฟลเดอร์ + DB record มี hash
    data_dir = tmp_path / "data" / product_id
    data_dir.mkdir(parents=True)
    (data_dir / "a.txt").write_bytes(b"hello")
    import hashlib
    existing_hash = hashlib.sha256(b"hello").hexdigest()
    record = product_db.load(product_id)
    record["files"] = [{"name": "a.txt", "path": str(data_dir / "a.txt"),
                        "hash": existing_hash, "status": "ingested"}]
    product_db.save(product_id, record)

    # อัปโหลด a.txt เนื้อเดิมซ้ำ + b.txt ใหม่
    saved = product_db.save_uploaded_files(product_id, [
        ("a.txt", b"hello"),   # เนื้อซ้ำ → ข้าม
        ("b.txt", b"world"),   # ใหม่ → เซฟ
    ])

    assert saved == ["b.txt"]
    # ไม่มีก้อนซ้ำ a_1.txt
    assert not (data_dir / "a_1.txt").exists()
    assert (data_dir / "b.txt").read_bytes() == b"world"


def test_save_uploaded_files_renames_on_name_clash_different_content(_pdb, tmp_path):
    """ชื่อซ้ำแต่เนื้อต่าง → เปลี่ยนชื่อเป็น _1 (เป็นไฟล์ใหม่จริง ไม่ใช่ก้อนซ้ำ)."""
    product_db = _pdb
    product_id = "clash"

    data_dir = tmp_path / "data" / product_id
    data_dir.mkdir(parents=True)
    (data_dir / "a.txt").write_bytes(b"old")
    import hashlib
    record = product_db.load(product_id)
    record["files"] = [{"name": "a.txt", "path": str(data_dir / "a.txt"),
                        "hash": hashlib.sha256(b"old").hexdigest(), "status": "ingested"}]
    product_db.save(product_id, record)

    saved = product_db.save_uploaded_files(product_id, [
        ("a.txt", b"new content"),  # ชื่อซ้ำ เนื้อต่าง → a_1.txt
    ])

    assert saved == ["a_1.txt"]
    assert (data_dir / "a.txt").read_bytes() == b"old"  # ของเดิมไม่ถูกเขียนทับ
    assert (data_dir / "a_1.txt").read_bytes() == b"new content"


def test_save_uploaded_files_skips_dotfiles(_pdb, tmp_path):
    """ไฟล์ที่ขึ้นต้นด้วย . (เช่น .DS_Store) → ข้าม."""
    product_db = _pdb
    saved = product_db.save_uploaded_files("prod", [
        (".DS_Store", b"junk"),
        (".hidden", b"junk"),
        ("real.txt", b"data"),
    ])

    assert saved == ["real.txt"]


# ------------------------------------------------------------------
#  is_ready / get_ready_products — รองรับทั้ง STATUS_READY และ STATUS_STALE
# ------------------------------------------------------------------

def test_is_ready_and_get_ready_products_accept_stale_product(_pdb, tmp_path):
    """สินค้าสถานะ stale (มีข้อมูลเดิม รอ re-ingest) ต้องถือว่าพร้อมใช้สำหรับ Agents."""
    product_db = _pdb

    # 1. Product ready
    p1 = "ProdReady"
    (tmp_path / "data" / p1).mkdir(parents=True)
    rec1 = product_db.load(p1)
    rec1["status"] = product_db.STATUS_READY
    rec1["raw_text"] = "Ready specs"
    product_db.save(p1, rec1)

    # 2. Product stale
    p2 = "ProdStale"
    (tmp_path / "data" / p2).mkdir(parents=True)
    rec2 = product_db.load(p2)
    rec2["status"] = product_db.STATUS_STALE
    rec2["raw_text"] = "Stale specs but usable"
    product_db.save(p2, rec2)

    # 3. Product pending / empty
    p3 = "ProdPending"
    (tmp_path / "data" / p3).mkdir(parents=True)
    rec3 = product_db.load(p3)
    rec3["status"] = product_db.STATUS_PENDING
    product_db.save(p3, rec3)

    assert product_db.is_ready(p1) is True
    assert product_db.is_ready(p2) is True
    assert product_db.is_ready(p3) is False

    ready_list = product_db.get_ready_products()
    assert p1 in ready_list
    assert p2 in ready_list
    assert p3 not in ready_list


# ------------------------------------------------------------------
#  get_product_source_urls — URL provenance for grounding validation
# ------------------------------------------------------------------

def test_get_product_source_urls_reads_source_import(_pdb, tmp_path):
    """URL-imported product → its source_import URLs are returned."""
    product_db = _pdb
    pid = "source_page"
    rec = product_db.load(pid)
    rec["source_import"] = {
        "original_url": "https://shop.example.com/product/1",
        "final_url": "https://shop.example.com/product/1-slug",
        "canonical_url": "https://shop.example.com/product/1-slug",
        "fetched_via": "static",
    }
    product_db.save(pid, rec)

    urls = product_db.get_product_source_urls(pid)
    assert "https://shop.example.com/product/1" in urls
    assert "https://shop.example.com/product/1-slug" in urls


def test_get_product_source_urls_empty_for_file_import(_pdb, tmp_path):
    """File-imported product (no source_import) → empty set, no error."""
    product_db = _pdb
    product_db.load("file_product")
    assert product_db.get_product_source_urls("file_product") == set()


def test_get_product_source_urls_isolation(_pdb, tmp_path):
    """Product A's source URL must never appear in product B's set —
    provenance is per-product, no global fallback."""
    product_db = _pdb
    rec_a = product_db.load("prod_a")
    rec_a["source_import"] = {"original_url": "https://a.example/p/1"}
    product_db.save("prod_a", rec_a)
    product_db.load("prod_b")

    assert "https://a.example/p/1" not in product_db.get_product_source_urls("prod_b")
