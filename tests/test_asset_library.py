"""Tests สำหรับ src/asset_library.py — TDD vertical slices.

Seam: public interface ของ asset_library (ingest_asset, query_assets, get_asset,
get_asset_paths, update_asset, delete_asset, ingest_all, list_all)
LLM/embedding = injectable mock, filesystem = tmp_path (local-substitutable).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import asset_library


# ------------------------------------------------------------------
#  Fixtures & helpers
# ------------------------------------------------------------------

@pytest.fixture
def cfg():
    """Config สำหรับ test — ใช้ค่าจริงจาก config/assets.yaml."""
    return asset_library._load_config()


@pytest.fixture
def fake_img(tmp_path):
    """สร้างไฟล์รูปจำลอง (1x1 PNG) ใน tmp_path."""
    # PNG header ขั้นต่ำ 1x1
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63000100000005000100cfffffffffff0000000049454e44ae426082"
    )
    p = tmp_path / "test.png"
    p.write_bytes(png_bytes)
    return p


@pytest.fixture
def fake_font(tmp_path):
    """ไฟล์ other (ฟอนต์จำลอง)."""
    p = tmp_path / "brand.ttf"
    p.write_bytes(b"\x00\x01\x00" + b"\x00" * 100)
    return p


def make_mock_tagger(subject="person", style="photo", tags=None, desc="ผู้หญิงยิ้ม"):
    """tagger ที่คืนค่าคงที่ — ไม่เรียก LLM จริง."""
    def _tag(file_path, ftype, config, llm):
        return {
            "subject": subject,
            "style": style,
            "tags": tags or ["female", "lifestyle"],
            "description": desc,
        }
    return _tag


def make_mock_embedder(dim=4):
    """embedder ที่คืน vector คงที่ — ควบคุม similarity ได้ใน test."""
    def _embed(text, config):
        # hash text → vector คงที่ (ใช้สำหรับทดสอบ similarity แบบควบคุมได้)
        h = abs(hash(text)) % 1000
        return [((h >> i) & 1) * 1.0 for i in range(dim)]
    return _embed


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """ย้าย DB path ไป tmp_path — กันทับ db.json จริง."""
    db_path = tmp_path / "db.json"
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_assets_dir", lambda: tmp_path / "assets")
    return db_path


# ------------------------------------------------------------------
#  Slice 1: ingest_asset รูป → record สมบูรณ์
# ------------------------------------------------------------------

def test_ingest_image_creates_record(isolated_db, fake_img, cfg):
    """ingest รูป → record มี type=image, description จาก tagger, tags ตาม taxonomy,
    embedding จาก embedder, status=ready, ไม่มี embedding ในผลลัพธ์."""
    result = asset_library.ingest_asset(
        fake_img,
        llm=object(),  # mock — tagger ไม่ไช้ llm จริง
        tagger=make_mock_tagger(subject="person", tags=["female", "50s"], desc="ผู้หญิงวัย 50"),
        embedder=make_mock_embedder(),
        config=cfg,
    )

    assert result["status"] == "ready"
    assert result["type"] == "image"
    assert result["subject"] == "person"
    assert result["style"] == "photo"
    assert result["tags"] == ["female", "50s"]
    assert result["description"] == "ผู้หญิงวัย 50"
    assert result["id"].startswith("a_")
    assert "embedding" not in result  # ไม่มี embedding ในผลลัพธ์
    assert result["hash"]  # hash ไม่ว่าง
    assert Path(result["path"]).exists()


# ------------------------------------------------------------------
#  Slice 2: hash ไม่เปลี่ยน → ข้าม (ไม่เรียก tagger/embedder ซ้ำ)
# ------------------------------------------------------------------

def test_ingest_same_file_skips(isolated_db, fake_img, cfg):
    """ingest ไฟล์เดิมอีกครั้ง → ข้าม ไม่เรียก tagger/embedder."""
    call_count = {"tag": 0, "embed": 0}

    def counting_tagger(*args, **kwargs):
        call_count["tag"] += 1
        return {"subject": "person", "style": "photo", "tags": ["x"], "description": "d"}

    def counting_embedder(*args, **kwargs):
        call_count["embed"] += 1
        return [1.0, 0.0, 0.0, 0.0]

    # ingest ครั้งแรก
    asset_library.ingest_asset(
        fake_img, llm=object(), tagger=counting_tagger, embedder=counting_embedder, config=cfg,
    )
    assert call_count == {"tag": 1, "embed": 1}

    # ingest ซ้ำ → ข้าม
    result = asset_library.ingest_asset(
        fake_img, llm=object(), tagger=counting_tagger, embedder=counting_embedder, config=cfg,
    )
    assert call_count == {"tag": 1, "embed": 1}  # ไม่เพิ่ม
    assert result["status"] == "ready"


# ------------------------------------------------------------------
#  Slice 3: ไฟล์ใหญ่เกิน limit → error
# ------------------------------------------------------------------

def test_ingest_oversized_file_returns_error(isolated_db, tmp_path, cfg):
    """ไฟล์เกิน max_file_size_mb → record status=error + error message."""
    # สร้างไฟล์ 21MB (limit image = 20MB)
    big = tmp_path / "big.png"
    big.write_bytes(b"\x00" * (21 * 1024 * 1024))

    result = asset_library.ingest_asset(
        big, llm=None, tagger=make_mock_tagger(), embedder=make_mock_embedder(), config=cfg,
    )

    assert result["status"] == "error"
    assert "20MB" in result["error"] or "เกิน" in result["error"]


# ------------------------------------------------------------------
#  Slice 4: ไฟล์ other (ฟอนต์) → ไม่เรียก LLM, description จากชื่อไฟล์+user_note
# ------------------------------------------------------------------

def test_ingest_other_file_no_llm(isolated_db, fake_font, cfg):
    """ไฟล์ other (ฟอนต์) → tagger ไม่ถูกเรียกเลย, description จาก user_note."""
    tag_called = {"v": False}

    def spy_tagger(*args, **kwargs):
        tag_called["v"] = True
        return {"subject": "other", "style": "other", "tags": [], "description": ""}

    result = asset_library.ingest_asset(
        fake_font,
        llm=None,
        user_note="ฟอนต์หลักของแบรนด์",
        tagger=spy_tagger,
        embedder=make_mock_embedder(),
        config=cfg,
    )

    assert tag_called["v"] is False  # tagger ไม่ถูกเรียก
    assert result["status"] == "ready"
    assert result["type"] == "other"
    assert "ฟอนต์หลักของแบรนด์" in result["description"]


# ------------------------------------------------------------------
#  Slice 5: query_assets filter type/subject
# ------------------------------------------------------------------

def test_query_filter_type_subject(isolated_db, tmp_path, cfg):
    """query_assets filter type/subject → คืนเฉพาะที่ตรง, ไม่มี embedding ในผลลัพธ์."""
    # สร้าง image + text asset
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    txt = tmp_path / "b.txt"
    txt.write_text("brand guidelines")

    asset_library.ingest_asset(
        img, llm=object(), tagger=make_mock_tagger(subject="logo"), embedder=make_mock_embedder(), config=cfg,
    )
    asset_library.ingest_asset(
        txt, llm=None, embedder=make_mock_embedder(), config=cfg,
    )

    # filter type=image
    images = asset_library.query_assets(type="image", embedder=make_mock_embedder(), config=cfg)
    assert len(images) == 1
    assert images[0]["type"] == "image"
    assert "embedding" not in images[0]

    # filter subject=logo
    logos = asset_library.query_assets(subject="logo", embedder=make_mock_embedder(), config=cfg)
    assert len(logos) == 1
    assert logos[0]["subject"] == "logo"


# ------------------------------------------------------------------
#  Slice 6: query_assets semantic rank
# ------------------------------------------------------------------

def test_query_semantic_rank(isolated_db, tmp_path, cfg):
    """query_assets มี query → rank ด้วย cosine similarity, จำกัด top_k."""
    # สร้าง 2 image ที่ embedding ต่างกัน
    img1 = tmp_path / "person.png"
    img1.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    img2 = tmp_path / "logo.png"
    img2.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    # embedder ที่คืน vector ตาม keyword ใน text — ถ้ามี "ผู้หญิง" → vector A, ถ้า "โลโก้" → vector B
    def keyword_embedder(text, config):
        if "ผู้หญิง" in text:
            return [1.0, 0.0]
        if "โลโก้" in text:
            return [0.0, 1.0]
        return [0.0, 0.0]

    asset_library.ingest_asset(
        img1, llm=object(),
        tagger=make_mock_tagger(subject="person", tags=["female"], desc="ผู้หญิงยิ้ม"),
        embedder=keyword_embedder, config=cfg,
    )
    asset_library.ingest_asset(
        img2, llm=object(),
        tagger=make_mock_tagger(subject="logo", tags=["brand"], desc="โลโก้แดง"),
        embedder=keyword_embedder, config=cfg,
    )

    # query "ผู้หญิง" → person.png ต้องติดอันดับ 1
    results = asset_library.query_assets("ผู้หญิง", embedder=keyword_embedder, config=cfg)
    assert len(results) >= 1
    assert results[0]["description"] == "ผู้หญิงยิ้ม"

    # query "โลโก้" → logo.png ติดอันดับ 1
    results = asset_library.query_assets("โลโก้", embedder=keyword_embedder, config=cfg)
    assert results[0]["description"] == "โลโก้แดง"


# ------------------------------------------------------------------
#  Slice 7: DB ว่าง → query คืน []
# ------------------------------------------------------------------

def test_query_empty_db(isolated_db, cfg):
    """DB ว่าง → query_assets คืน [] ไม่ error."""
    results = asset_library.query_assets("อะไรก็ได้", embedder=make_mock_embedder(), config=cfg)
    assert results == []


# ------------------------------------------------------------------
#  Slice 8: update_asset → re-embed
# ------------------------------------------------------------------

def test_update_asset_reembeds(isolated_db, fake_img, cfg):
    """update_asset แก้ tags → record อัปเดต + embedder ถูกเรียกใหม่."""
    embed_calls = {"n": 0}

    def counting_embedder(text, config):
        embed_calls["n"] += 1
        return [1.0, 0.0, 0.0, 0.0]

    rec = asset_library.ingest_asset(
        fake_img, llm=object(), tagger=make_mock_tagger(), embedder=counting_embedder, config=cfg,
    )
    assert embed_calls["n"] == 1

    updated = asset_library.update_asset(
        rec["id"], tags=["new", "tags"], embedder=counting_embedder, config=cfg,
    )
    assert embed_calls["n"] == 2  # re-embed ถูกเรียก
    assert updated["tags"] == ["new", "tags"]


# ------------------------------------------------------------------
#  Slice 9: get_asset_paths → เฉพาะไฟล์จริง + type=image
# ------------------------------------------------------------------

def test_get_asset_paths_only_existing_images(isolated_db, tmp_path, cfg):
    """get_asset_paths → คืนเฉพาะ path ที่ไฟล์มีจริง + type=image, ข้าม id ไม่รู้จัก."""
    img = tmp_path / "real.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    txt = tmp_path / "doc.txt"
    txt.write_text("doc")

    img_rec = asset_library.ingest_asset(
        img, llm=object(), tagger=make_mock_tagger(), embedder=make_mock_embedder(), config=cfg,
    )
    txt_rec = asset_library.ingest_asset(
        txt, llm=None, embedder=make_mock_embedder(), config=cfg,
    )

    # ลบไฟล์รูปออก → path ต้องไม่คืน
    img.unlink()

    paths = asset_library.get_asset_paths([img_rec["id"], txt_rec["id"], "a_9999"])
    assert paths == []  # รูปถูกลบ + text ไม่ใช่ image + a_9999 ไม่มี


# ------------------------------------------------------------------
#  Slice 10: ingest_all → ไฟล์ใหม่ ingest ครบ, เดิมข้าม
# ------------------------------------------------------------------

def test_ingest_all_scans_folder(isolated_db, tmp_path, cfg):
    """ingest_all → สแกน brand/assets/ ทั้งโฟลเดอร์, ไฟล์ใหม่ ingest, เดิมข้าม."""
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()

    img1 = assets_dir / "a.png"
    img1.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    img2 = assets_dir / "b.png"
    img2.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    # สแกนครั้งแรก → ingest 2 ไฟล์
    result = asset_library.ingest_all(
        llm=object(), tagger=make_mock_tagger(), embedder=make_mock_embedder(), config=cfg,
    )
    assert result["ingested"] == 2
    assert result["skipped"] == 0

    # สแกนซ้ำ → ข้ามทั้ง 2
    result = asset_library.ingest_all(
        llm=object(), tagger=make_mock_tagger(), embedder=make_mock_embedder(), config=cfg,
    )
    assert result["ingested"] == 0
    assert result["skipped"] == 2

    # เพิ่มไฟล์ใหม่ → ingest 1, ข้าม 2
    img3 = assets_dir / "c.png"
    img3.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    result = asset_library.ingest_all(
        llm=object(), tagger=make_mock_tagger(), embedder=make_mock_embedder(), config=cfg,
    )
    assert result["ingested"] == 1
    assert result["skipped"] == 2
