"""Tests for product_match — จับคู่ segment กับสินค้าที่มีอยู่ (idempotent re-import).

ตัดสินใจว่า segment แต่ละตัวจาก segmentation ควร 'update' สินค้าที่มีอยู่
หรือ 'create' ใหม่ — เพื่อให้อัปโหลด catalog ซ้ำไม่สร้างสินค้าก้อนซ้ำ.

ทดสอบ pure function `match_segments_to_existing` — ไม่มี side effect.
"""
import pytest

from src.product_match import match_segments_to_existing


def _seg(key, text="text", name="name"):
    return {"product_key": key, "suggested_name": name, "text": text}


def _existing(product_id, product_key=None, raw_text=""):
    """สินค้าที่มีอยู่ — เลียนแบบ product_db record."""
    rec = {"product_id": product_id, "raw_text": raw_text}
    if product_key is not None:
        rec["scope"] = {"product_key": product_key}
    return rec


# ------------------------------------------------------------------
#  Slice 2: key match
# ------------------------------------------------------------------

def test_all_keys_match_existing_updates():
    """ทุก segment มี key ตรงสินค้าที่มี → ทุกตัว update ไม่ create."""
    segments = [_seg("K67"), _seg("K72"), _seg("K71")]
    existing = [_existing("CACGO K67", "K67"), _existing("CACGO K72", "K72"),
                _existing("CACGO K71", "K71")]

    result = match_segments_to_existing(segments, existing)

    assert [r["action"] for r in result] == ["update", "update", "update"]
    assert [r["target"] for r in result] == ["CACGO K67", "CACGO K72", "CACGO K71"]
    assert [r["match_by"] for r in result] == ["key", "key", "key"]


def test_mixed_match_and_create():
    """บาง key ตรง บาง key ไม่มี → update ตัวที่ตรง, create ตัวที่ขาด."""
    segments = [_seg("K67"), _seg("K72"), _seg("K99")]
    existing = [_existing("CACGO K67", "K67"), _existing("CACGO K72", "K72")]
    # K99 ไม่มี → create

    result = match_segments_to_existing(segments, existing)

    assert result[0]["action"] == "update"
    assert result[0]["target"] == "CACGO K67"
    assert result[1]["action"] == "update"
    assert result[1]["target"] == "CACGO K72"
    assert result[2]["action"] == "create"
    assert result[2]["target"] is None
    assert result[2]["match_by"] is None


def test_no_existing_all_create():
    """ไม่มีสินค้าเดิมเลย → ทุก segment create."""
    segments = [_seg("K67"), _seg("K72")]

    result = match_segments_to_existing(segments, [])

    assert [r["action"] for r in result] == ["create", "create"]
    assert all(r["target"] is None for r in result)


def test_empty_product_key_falls_to_create():
    """segment ที่ไม่มี product_key (ว่าง) → ไม่จับคู่ด้วย key → create (รอ text_hash ใน slice 3)."""
    segments = [_seg("", text="some text")]
    existing = [_existing("prod", "K67")]

    result = match_segments_to_existing(segments, existing)

    assert result[0]["action"] == "create"
    assert result[0]["match_by"] is None


def test_result_preserves_segment_order_and_payload():
    """result ต้องอยู่ในลำดับเดียวกับ segments และส่ง segment กลับมาด้วย."""
    s1 = _seg("K67", text="t1", name="n1")
    s2 = _seg("K99", text="t2", name="n2")
    existing = [_existing("CACGO K67", "K67")]

    result = match_segments_to_existing([s1, s2], existing)

    assert result[0]["segment"] is s1
    assert result[1]["segment"] is s2


# ------------------------------------------------------------------
#  Slice 3: text_hash fallback (สินค้าเดิมที่ยังไม่มี product_key)
# ------------------------------------------------------------------

def test_text_hash_match_when_no_product_key():
    """สินค้าเดิมไม่มี scope.product_key แต่ raw_text เท่ากับ segment.text → update (text_hash)."""
    shared_text = "CACGO K67 CPU ATS3085L Price $21.50"
    segments = [_seg("", text=shared_text)]  # segment ไม่มี key
    existing = [_existing("สินค้าเดิม", product_key=None, raw_text=shared_text)]

    result = match_segments_to_existing(segments, existing)

    assert result[0]["action"] == "update"
    assert result[0]["target"] == "สินค้าเดิม"
    assert result[0]["match_by"] == "text_hash"


def test_text_hash_no_match_creates():
    """raw_text ต่างจาก segment.text และไม่มี key ตรง → create."""
    segments = [_seg("", text="new product text")]
    existing = [_existing("old", product_key=None, raw_text="completely different")]

    result = match_segments_to_existing(segments, existing)

    assert result[0]["action"] == "create"
    assert result[0]["match_by"] is None


def test_key_takes_precedence_over_text_hash():
    """ถ้า key ตรง → ใช้ key ไม่เลื่อนไปดู text_hash."""
    segments = [_seg("K67", text="text A")]
    existing = [_existing("CACGO K67", "K67", raw_text="text B")]  # text ต่าง แต่ key ตรง

    result = match_segments_to_existing(segments, existing)

    assert result[0]["action"] == "update"
    assert result[0]["match_by"] == "key"


def test_mixed_key_and_text_hash_matches():
    """segment 1 จับด้วย key, segment 2 จับด้วย text_hash ในการเรียกครั้งเดียว."""
    text_b = "shared text B"
    segments = [_seg("K67", text="text A"), _seg("", text=text_b)]
    existing = [
        _existing("CACGO K67", "K67", raw_text="whatever"),
        _existing("legacy prod", product_key=None, raw_text=text_b),
    ]

    result = match_segments_to_existing(segments, existing)

    assert result[0]["action"] == "update"
    assert result[0]["match_by"] == "key"
    assert result[0]["target"] == "CACGO K67"
    assert result[1]["action"] == "update"
    assert result[1]["match_by"] == "text_hash"
    assert result[1]["target"] == "legacy prod"


def test_text_hash_skips_existing_with_product_key():
    """สินค้าเดิมที่มี product_key อยู่แล้ว ไม่ใช้ text_hash (จะจับด้วย key ทางเดียว).

    ถ้า segment ไม่มี key และสินค้าเดิมมี key (ไม่ตรอบ segment) → ไม่จับ text_hash → create.
    """
    segments = [_seg("", text="some text")]
    existing = [_existing("has key", "K77", raw_text="some text")]  # text ตรง แต่มี key

    result = match_segments_to_existing(segments, existing)

    assert result[0]["action"] == "create"
    assert result[0]["match_by"] is None
