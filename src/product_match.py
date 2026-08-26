"""Product match — จับคู่ segment กับสินค้าที่มีอยู่ (idempotent re-import).

ปัญหา: อัปโหลด catalog (เช่น price list 20 รุ่น) ซ้ำ → segmentation รันใหม่
→ ถ้าสร้างสินค้าใหม่หมดทุกครั้ง จะเกิดสินค้าก้อนซ้ำ (ชื่อชน → เติม (1))

แก้: ก่อน materialize ให้จับคู่แต่ละ segment กับสินค้าที่มีอยู่ด้วยตัวตนถาวร
ถ้าตรง → update สินค้านั้น (ไม่สร้างใหม่) ถ้าไม่ตรง → create ใหม่

Interface:
  match_segments_to_existing(segments, existing) -> list[dict]
    segments: จาก segment_products แต่ละตัวมี product_key, text, ...
    existing:  product_db records แต่ละตัวมี scope.product_key?, raw_text
    คืน: ตามลำดับ segments [{
        "segment", "action": "update"|"create",
        "target": <product_id ที่ตรง> | None,
        "match_by": "key"|"text_hash"|None,
    }]

กฎจับคู่ (เรียงความแม่นยำ):
  1. product_key ตรง scope.product_key → update (match_by=key)
  2. สินค้าเดิมไม่มี product_key → hash(raw_text) ตรง hash(segment.text) → update (match_by=text_hash)
  3. ไม่ตรง → create
"""
from __future__ import annotations

import hashlib
from typing import Any


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def match_segments_to_existing(
    segments: list[dict],
    existing: list[dict],
) -> list[dict]:
    """ตัดสินใจ segment แต่ละตัว: update สินค้าที่มี หรือ create ใหม่.

    ดูกฎจับคู่ใน docstring ของ module. คืน list ตามลำดับ segments.
    """
    # ดัชนีสินค้าเดิมตาม product_key — ใช้ตัวแรกที่เจอ (ปกติ key ต้อง unique)
    existing_by_key: dict[str, str] = {}
    # ดัชนีสินค้าเดิมที่ไม่มี product_key ตาม hash(raw_text) — สำหรับ migrate ของเดิม
    existing_by_text_hash: dict[str, str] = {}
    for rec in existing:
        scope = rec.get("scope") or {}
        key = scope.get("product_key")
        pid = rec.get("product_id", "")
        if key:
            if key not in existing_by_key:
                existing_by_key[key] = pid
        else:
            # สินค้าเดิมที่ไม่มี product_key → ใช้ text_hash เป็นตัวตนสำรอง
            text_hash = _text_hash(rec.get("raw_text", ""))
            if text_hash and text_hash not in existing_by_text_hash:
                existing_by_text_hash[text_hash] = pid

    results: list[dict] = []
    for seg in segments:
        key = (seg.get("product_key") or "").strip()
        if key and key in existing_by_key:
            results.append({
                "segment": seg,
                "action": "update",
                "target": existing_by_key[key],
                "match_by": "key",
            })
            continue
        # text_hash fallback — เฉพาะ segment ที่ไม่มี key จับคู่
        text_hash = _text_hash(seg.get("text", ""))
        if text_hash in existing_by_text_hash:
            results.append({
                "segment": seg,
                "action": "update",
                "target": existing_by_text_hash[text_hash],
                "match_by": "text_hash",
            })
            continue
        results.append({
            "segment": seg,
            "action": "create",
            "target": None,
            "match_by": None,
        })
    return results
