"""Product DB — จัดการข้อมูลสินค้าแบบ structured (JSON ต่อสินค้า).

ทำหน้าที่เป็น "DB สินค้าของบริษัท" ที่ agent การตลาดดึงไปใช้
แยกออกจาก product_spec agent (ที่ทำ deliverable ให้ user)

โครงสร้างไฟล์:
  data/{product_id}/                — raw files ที่ user upload เท่านั้น
  cache/{product_id}/product.json   — DB ของระบบ (status, raw_text, image_descriptions, ฯลฯ)
  cache/{product_id}/product_profile.json — ข้อมูลตำแหน่งสินค้าจากระบบหรือ user

data/ มีแค่ไฟล์ดิบจาก user — ไฟล์ระบบทั้งหมดอยู่ใน cache/
อนาคต: เปลี่ยนเป็น MongoDB ได้โดยแก้แค่ไฟล์นี้
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def _get_raw_text_max_length() -> int:
    """Read raw_text max length from config/ingestion.yaml (lazy load)."""
    try:
        from .config_loader import load_config, get_section
        cfg = load_config()
        ing_cfg = get_section(cfg, "ingestion", {"raw_text_max_length": 12000})
        return int(ing_cfg.get("raw_text_max_length", 12000))
    except Exception:
        return int(os.environ.get("PRODUCT_DB_RAW_TEXT_MAX", "12000"))

# สถานะสินค้า 6 แบบ
STATUS_EMPTY = "empty"                # ไม่มีไฟล์เลย
STATUS_PENDING = "pending"            # มีไฟล์รองรับแต่ยังไม่ได้ ingest
STATUS_NO_USABLE = "no_usable_data"   # มีไฟล์แต่ไม่รองรับทั้งหมด
STATUS_PROCESSING = "processing"      # ingestion กำลังทำ
STATUS_READY = "ready"                # ingestion เสร็จ ข้อมูลครบ
STATUS_STALE = "stale"                # raw data เปลี่ยน รอ re-ingest

ALL_STATUSES = [STATUS_EMPTY, STATUS_PENDING, STATUS_NO_USABLE, STATUS_PROCESSING, STATUS_READY, STATUS_STALE]


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _product_dir(product_id: str) -> Path:
    """โฟลเดอร์ DB ของสินค้า — อยู่ใน cache/ ไม่ใช่ data/ (แยกจากไฟล์ user)."""
    return _project_root() / "cache" / product_id


def _db_path(product_id: str) -> Path:
    return _product_dir(product_id) / "product.json"


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _empty_record(product_id: str) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "status": STATUS_EMPTY,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "ingested_at": None,          # ตอน ingestion เสร็จ
        "ingest_progress": None,      # {step, total, message, eta_seconds}
        "files": [],                  # [{name, path, type, status, hash, size, ingested_at, error}]
        "fields": {},                 # deprecated — ไม่ใช้แล้ว (วิธีสากล: agent ดึง raw text เอง)
        "raw_text": "",               # computed from text_extracts (backward compat: สินค้าเดิมอาจเก็บตรงนี้)
        "text_extracts": [],          # [{file, text}] — text รายไฟล์ (เหมือน video_transcripts)
        "image_descriptions": [],     # [{file, description}] — LLM บรรยายรูป
        "video_transcripts": [],      # [{file, transcript}] — จาก frames
        "audio_transcripts": [],      # [{file, transcript}] — จาก whisper
    }


def load(product_id: str) -> dict[str, Any]:
    """โหลด product record จาก JSON ถ้าไม่มี return empty record."""
    path = _db_path(product_id)
    if not path.exists():
        return _empty_record(product_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty_record(product_id)


def save(product_id: str, record: dict[str, Any]) -> None:
    """บันทึก product record ลง JSON."""
    path = _db_path(product_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = datetime.now().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)


def set_status(product_id: str, status: str, extra: dict | None = None) -> dict[str, Any]:
    """เปลี่ยนสถานะสินค้า และบันทึก."""
    if status not in ALL_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {ALL_STATUSES}")
    record = load(product_id)
    record["status"] = status
    if status == STATUS_READY:
        record["ingested_at"] = datetime.now().isoformat()
        record["ingest_progress"] = None
    if extra:
        record.update(extra)
    save(product_id, record)
    return record


def set_progress(product_id: str, step: int, total: int, message: str, eta_seconds: int | None = None) -> None:
    """อัปเดต progress ระหว่าง ingestion (ให้ UI ดึงไปโชว์)."""
    record = load(product_id)
    record["ingest_progress"] = {
        "step": step,
        "total": total,
        "message": message,
        "eta_seconds": eta_seconds,
    }
    save(product_id, record)


def add_file(product_id: str, file_info: dict[str, Any]) -> None:
    """เพิ่ม/อัปเดตข้อมูลไฟล์ใน record."""
    record = load(product_id)
    files = record.get("files", [])
    # หาไฟล์เดิมด้วย path
    existing_idx = None
    for i, f in enumerate(files):
        if f.get("path") == file_info.get("path"):
            existing_idx = i
            break
    if existing_idx is not None:
        files[existing_idx].update(file_info)
    else:
        files.append(file_info)
    record["files"] = files
    save(product_id, record)


def update_file_status(product_id: str, file_path: str, status: str, error: str | None = None) -> None:
    """อัปเดตสถานะไฟล์เฉพาะไฟล์ (ingested / unsupported / error)."""
    record = load(product_id)
    for f in record.get("files", []):
        if f.get("path") == file_path:
            f["status"] = status
            if error:
                f["error"] = error
            elif status == "ingested":
                f["ingested_at"] = datetime.now().isoformat()
            break
    save(product_id, record)


def set_fields(product_id: str, fields: dict[str, Any]) -> None:
    """ตั้งค่า structured fields ที่ extract ได้จาก ingestion."""
    record = load(product_id)
    record["fields"] = fields
    save(product_id, record)


def append_extracted(product_id: str, category: str, item: dict[str, Any]) -> None:
    """เพิ่มข้อมูลที่ extract ได้ (image_descriptions / video_transcripts / audio_transcripts)."""
    record = load(product_id)
    key = category
    if key not in record:
        record[key] = []
    record[key].append(item)
    save(product_id, record)


def get_all_products() -> list[dict[str, Any]]:
    """ดึงรายการสินค้าทั้งหมด พร้อมสถานะ."""
    data_dir = _project_root() / "data"
    if not data_dir.exists():
        return []
    products = []
    for item in sorted(data_dir.iterdir()):
        if not item.is_dir() or item.name.startswith(".") or item.name == ".DS_Store":
            continue
        record = load(item.name)
        products.append(record)
    return products


def get_ready_products() -> list[str]:
    """ดึงเฉพาะสินค้าที่พร้อมใช้ (status=ready)."""
    return [p["product_id"] for p in get_all_products() if p.get("status") == STATUS_READY]


def is_ready(product_id: str) -> bool:
    """เช็คว่าสินค้าพร้อมใช้หรือไม่."""
    return load(product_id).get("status") == STATUS_READY


def get_status(product_id: str) -> str:
    """ดึงสถานะสินค้า."""
    return load(product_id).get("status", STATUS_EMPTY)


def get_progress(product_id: str) -> dict | None:
    """ดึง progress ปัจจุบัน (ถ้ากำลัง ingestion)."""
    return load(product_id).get("ingest_progress")


def compute_file_hash(path: Path) -> str:
    """Public wrapper สำหรับคำนวณ hash."""
    return _file_hash(path)


def get_existing_file_hashes(product_id: str) -> set[str]:
    """คืน set ของ hash ของไฟล์ที่มีอยู่ในสินค้า — สำหรับตรวจ duplicate ตอนอัปโหลด.

    ใช้ตอนอัปโหลดไฟล์ซ้ำ: ถ้า hash ของไฟล์ที่อัปโหลดตรอบไฟล์ที่มีอยู่แล้ว → ข้าม
    (ป้องกันสร้างไฟล์ก้อนซ้ำ `ชื่อ_1.ext` ในโฟลเดอร์สินค้า)

    ไฟล์ที่ยังไม่มี hash (ยังไม่ ingest) จะไม่ถูกนับ — เพื่อไม่ให้ข้ามไฟล์จริง
    ที่ยังไม่ได้ประมวลผล.
    """
    record = load(product_id)
    return {f.get("hash") for f in record.get("files", []) if f.get("hash")}


def save_uploaded_files(
    product_id: str,
    files: list[tuple[str, bytes]],
) -> list[str]:
    """เซฟไฟล์อัปโหลดลง data/{product_id}/ โดยข้ามไฟล์ที่เนื้อซ้ำ (idempotent).

    Args:
        product_id: ชื่อสินค้า (โฟลเดอร์ปลายทาง)
        files: list ของ (filename, content)

    คืน: list ของชื่อไฟล์ที่เซฟจริง (ไม่นับที่ข้าม)

    กฎ:
      - ข้าม dotfile (ชื่อขึ้นต้นด้วย "." เช่น .DS_Store)
      - ถ้า hash เนื้อตรงไฟล์ที่มีอยู่ใน DB → ข้าม (ไม่สร้างก้อนซ้ำ)
      - ถ้าชื่อซ้ำแต่เนื้อต่าง → เปลี่ยนชื่อเป็น ชื่อ_1.ext (dedup ชื่อ ไม่เขียนทับของเดิม)
    """
    product_dir = _project_root() / "data" / product_id
    product_dir.mkdir(parents=True, exist_ok=True)
    existing_hashes = get_existing_file_hashes(product_id)

    saved: list[str] = []
    for filename, content in files:
        if not filename or filename.startswith(".") or filename == ".DS_Store":
            continue
        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash in existing_hashes:
            continue  # เนื้อซ้ำ → ข้าม
        dest = product_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            i = 1
            while dest.exists():
                dest = product_dir / f"{stem}_{i}{suffix}"
                i += 1
        dest.write_bytes(content)
        saved.append(dest.name)
    return saved


def find_stale_files(product_id: str) -> list[dict[str, Any]]:
    """หาไฟล์ที่เปลี่ยน/ลบ/เพิ่ม — เพื่อ re-ingest.

    3 กรณี:
      - deleted: ไฟล์ใน DB แต่ไม่มีใน disk
      - changed: ไฟล์ใน DB แต่ hash เปลี่ยน
      - added: ไฟล์ใน disk แต่ไม่มีใน DB (ไฟล์ใหม่ที่ user เพิ่ม)
    """
    record = load(product_id)
    stale = []
    db_paths = set()
    # ตรวจไฟล์ที่มีใน DB
    for f in record.get("files", []):
        file_path = Path(f.get("path", ""))
        if file_path.name == "product_profile.json":
            continue  # system file — not part of source data
        db_paths.add(str(file_path))
        if not file_path.exists():
            stale.append({**f, "reason": "deleted"})
            continue
        if f.get("status") != "ingested":
            continue
        old_hash = f.get("hash")
        new_hash = _file_hash(file_path)
        if old_hash != new_hash:
            stale.append({**f, "reason": "changed", "new_hash": new_hash})
    # ตรวจไฟล์ใหม่ที่ไม่มีใน DB — data/ มีแค่ไฟล์ user ไม่ต้องกรอง product.json แล้ว
    pdir = _project_root() / "data" / product_id
    if pdir.exists():
        for item in sorted(pdir.iterdir()):
            if not item.is_file() or item.name.startswith(".") or item.name == ".DS_Store" or item.name == "product_profile.json":
                continue
            if str(item) not in db_paths:
                stale.append({
                    "name": item.name,
                    "path": str(item),
                    "reason": "added",
                })
    return stale


def mark_stale_if_changed(product_id: str) -> bool:
    """ตรวจว่ามีไฟล์เปลี่ยน/ลบ/เพิ่มไหม ถ้ามี เปลี่ยนสถานะเป็น stale และ return True."""
    stale = find_stale_files(product_id)
    if stale:
        record = load(product_id)
        if record.get("status") == STATUS_READY:
            set_status(product_id, STATUS_STALE)
        return True
    return False


def delete_product(product_id: str) -> bool:
    """ลบสินค้าทั้งโฟลเดอร์ (ใช้ตอน user ลบสินค้าใน UI)."""
    import shutil
    pdir = _product_dir(product_id)
    if pdir.exists():
        shutil.rmtree(pdir)
        return True
    return False


def get_fields_for_agent(product_id: str) -> dict[str, Any]:
    """ดึงข้อมูลสินค้าสำหรับ agent การตลาด — นี่คือ "DB query" ที่ agent ใช้.

    คืน dict ที่ agent ใช้เป็น context:
      - fields: structured data (ชื่อ, หมวด, ราคา, ฯลฯ)
      - raw_text: text ที่ parse ได้
      - image_descriptions: คำบรรยายรูป
      - video_transcripts: transcript วิดีโอ
      - audio_transcripts: transcript เสียง
    """
    record = load(product_id)
    return {
        "fields": record.get("fields", {}),
        "raw_text": record.get("raw_text", ""),
        "image_descriptions": record.get("image_descriptions", []),
        "video_transcripts": record.get("video_transcripts", []),
        "audio_transcripts": record.get("audio_transcripts", []),
        "scope": record.get("scope"),
    }


def get_agent_context_text(product_id: str) -> str:
    """สร้าง text สำหรับยัดเป็น context ของ agent การตลาด.

    วิธีสากล: ส่ง raw text ทั้งหมดให้ agent โดยไม่สกัด fields ล่วงหน้า
    แต่ละ agent จะแยกเองว่าต้องการข้อมูลอะไร

    ถ้าสินค้ามี scope (แยกจาก catalog) → ระบุชื่อ/รหัสสินค้าก่อน raw text
    เพื่อไม่ให้ agent สับสนเมื่อไฟล์ต้นฉบับเป็น catalog หลายรุ่น

    Note: ฟังก์ชันนี้คืน text เท่านั้น (backward compatible)
    สำหรับ multimodal (text + รูปจริง) ใช้ get_agent_context() แทน
    """
    data = get_fields_for_agent(product_id)
    parts = []

    # ถ้ามี scope (สินค้าที่แยกจาก catalog) → บอก agent ว่านี่คือสินค้าใด
    scope = data.get("scope")
    if scope and scope.get("product_key"):
        parts.append(f"--- ขอบเขตสินค้า ---")
        parts.append(f"รหัสสินค้า: {scope['product_key']}")
        if scope.get("split_from"):
            parts.append(f"แยกจาก: {scope['split_from']}")
        parts.append("--- สิ้นสุดขอบเขตสินค้า ---\n")

    if data["raw_text"]:
        parts.append("--- ข้อมูลดิบ (text) ---")
        parts.append(data["raw_text"][:_get_raw_text_max_length()])  # จำกัดป้องกัน token เกิน
        parts.append("--- สิ้นสุดข้อมูลดิบ ---\n")

    # สถาปัตยกรรมใหม่: ไม่ส่งคำบรรยายรูปแล้ว (agent เห็นรูปจริงผ่าน get_agent_context)
    # แต่ยังส่ง transcript วิดีโอ/เสียง (เป็น text อยู่แล้ว)
    # Backward compat: สินค้าเดิมที่มี description ไม่มี path → ยังส่ง text อยู่
    for desc in data["image_descriptions"]:
        p = desc.get("path")
        d = desc.get("description", "").strip()
        if d and not (p and Path(p).exists()):
            parts.append(f"[รูป {desc.get('file', '?')}]: {d}")

    if data["video_transcripts"]:
        parts.append("--- Transcript วิดีโอ ---")
        for t in data["video_transcripts"]:
            parts.append(f"[{t.get('file', '?')}]: {t.get('transcript', '')}")
        parts.append("--- สิ้นสุด Transcript วิดีโอ ---\n")

    if data["audio_transcripts"]:
        parts.append("--- Transcript เสียง ---")
        for t in data["audio_transcripts"]:
            parts.append(f"[{t.get('file', '?')}]: {t.get('transcript', '')}")
        parts.append("--- สิ้นสุด Transcript เสียง ---\n")

    return "\n".join(parts) if parts else ""


def get_scoped_context_text(folders: list[str]) -> str:
    """สร้าง scoped context สำหรับสินค้าที่เลือก — เฉพาะส่วนของรุ่นนั้น.

    ใช้ get_agent_context_text ของแต่ละสินค้า ซึ่งเคารพ scope
    (ตัดเฉพาะส่วนของรุ่นนั้นจาก catalog แทนที่จะส่งไฟล์ดิบทั้งไฟล์).
    รองรับหลายสินค้า (combined mode) — รวม scoped context ของแต่ละตัว
    โดยแต่ละตัวมี identity envelope บอก product ID เสมอ ไม่ปนกัน.

    Identity envelope (generic, source-driven):
      --- เริ่มข้อมูลสินค้า: <product_id> ---
      <existing inner context unchanged — incl. scope header if present>
      --- สิ้นสุดข้อมูลสินค้า: <product_id> ---

    ใช้ product ID จาก runtime argument (folder) เท่านั้น — ไม่ใช้ category,
    keyword, stopword หรือ domain-specific logic.  รักษา inner scope markers
    ของ get_agent_context_text ไว้ทั้งหมด เพื่อไม่ให้ข้อมูล record ที่มี
    scope.product_key ทำซ้ำ/ขัดแย้งกับ envelope.

    คืน "" ถ้าไม่มีสินค้าใดมีข้อมูล (caller จัดการกรณีนี้เอง).
    """
    parts = []
    for folder in folders:
        ctx = get_agent_context_text(folder)
        if not ctx.strip():
            continue
        # Normalize label: strip และทำให้บรรทัดเดียว เพื่อไม่ให้ทำลาย boundary
        label = (folder or "").strip().replace("\n", " ").replace("\r", " ")
        parts.append(
            f"--- เริ่มข้อมูลสินค้า: {label} ---\n"
            f"{ctx}\n"
            f"--- สิ้นสุดข้อมูลสินค้า: {label} ---"
        )
    return "\n\n".join(parts)


def get_agent_context(product_id: str) -> dict[str, Any]:
    """ดึง context สำหรับ agent การตลาด — แบบ multimodal (retrieve-then-read).

    คืน dict:
      - text: raw_text + transcripts (string) — ส่งเป็น text ให้ LLM
      - image_paths: list ของ path รูปจริง (ส่งเป็น image_url base64 ให้ LLM vision)
      - file_paths: list ของ path PDF ดิบ (ส่งเป็น file ให้ OpenRouter ถ้าจำเป็น)

    สถาปัตยกรรมใหม่:
      - text: lossless cache (ถูก)
      - รูป: ส่งรูปจริงให้ LLM (แม่น — LLM เห็นเหมือนมนุษย์)
      - PDF: ส่งดิบตอนจำเป็น (ผ่าน OpenRouter type: "file")

    Backward compat: สินค้าเดิมที่มี image_descriptions แบบเก่า (มี description ไม่มี path)
    → รวม description เข้าใน text (ไม่เสียข้อมูลจนกว่าจะ re-ingest)
    """
    record = load(product_id)

    # text context (raw_text + transcripts)
    text_parts = []
    if record.get("raw_text"):
        text_parts.append("--- ข้อมูลดิบ (text) ---")
        text_parts.append(record["raw_text"][:_get_raw_text_max_length()])
        text_parts.append("--- สิ้นสุดข้อมูลดิบ ---\n")

    for t in record.get("video_transcripts", []):
        text_parts.append(f"[วิดีโอ {t.get('file', '?')}]: {t.get('transcript', '')}")
    for t in record.get("audio_transcripts", []):
        text_parts.append(f"[เสียง {t.get('file', '?')}]: {t.get('transcript', '')}")

    # image paths (รูปจริง — ส่งให้ LLM vision)
    # + backward compat: เก่าที่มี description ไม่มี path → รวมใน text
    image_paths: list[str] = []
    legacy_descs: list[str] = []
    for desc in record.get("image_descriptions", []):
        p = desc.get("path")
        d = desc.get("description", "").strip()
        if p and Path(p).exists():
            image_paths.append(p)
        elif d:
            # สินค้าเดิมที่ยังไม่ re-ingest — มี description แต่ไม่มี path
            legacy_descs.append(f"[{desc.get('file', '?')}]: {d}")

    if legacy_descs:
        text_parts.append("--- คำบรรยายรูปภาพ (ข้อมูลเดิม — re-ingest เพื่อใช้รูปจริง) ---")
        text_parts.extend(legacy_descs)
        text_parts.append("--- สิ้นสุดคำบรรยายรูป ---\n")

    # file paths (PDF ดิบ — ส่งผ่าน OpenRouter type: "file" ตอนจำเป็น)
    file_paths: list[str] = []
    for f in record.get("files", []):
        if f.get("type") == "text" and f.get("path", "").lower().endswith(".pdf"):
            p = f.get("path")
            if p and Path(p).exists():
                file_paths.append(p)

    return {
        "text": "\n".join(text_parts) if text_parts else "",
        "image_paths": image_paths,
        "file_paths": file_paths,
    }


def get_product_metadata(product_id: str) -> dict[str, Any]:
    """ดึง metadata สั้นของสินค้า — สำหรับ automate discovery.

    ใช้ตอน agent ต้องเลือกสินค้าเอง (automate mode):
      - อ่าน metadata ของทุกสินค้า (เบา ไม่โหลดไฟล์)
      - เลือกสินค้าที่เกี่ยวข้อง
      - ค่อยดึง context เต็มผ่าน get_agent_context()

    คืน dict:
      - summary: สรุปสั้น (string)
      - category: หมวด (string)
      - file_count: จำนวนไฟล์ (int)
      - has_images: มีรูปไหม (bool)
      - image_count: จำนวนรูป (int)
      - status: สถานะ (string)
    """
    record = load(product_id)
    meta = record.get("metadata", {})
    return {
        "product_id": product_id,
        "summary": meta.get("summary", ""),
        "category": meta.get("category", ""),
        "file_count": meta.get("file_count", len(record.get("files", []))),
        "has_images": meta.get("has_images", False),
        "image_count": meta.get("image_count", 0),
        "status": record.get("status", STATUS_EMPTY),
    }


def get_product_image_paths(product_id: str) -> list[str]:
    """ดึง path ของไฟล์รูปจริงทั้งหมดของสินค้า — สำหรับส่งเป็น reference ตอน generate.

    คืน list ของ absolute path ของรูปจริงที่ user upload ไว้
    ลำดับตามที่เก็บใน image_descriptions
    """
    record = load(product_id)
    paths: list[str] = []
    for desc in record.get("image_descriptions", []):
        p = desc.get("path")
        if p and Path(p).exists():
            paths.append(p)
    return paths


def image_to_data_url(image_path: str | Path) -> str | None:
    """แปลงไฟล์รูปเป็น base64 data URL — สำหรับส่งให้ OpenRouter input_references.

    คืน None ถ้าไฟล์ไม่มีหรืออ่านไม่ได้
    """
    p = Path(image_path)
    if not p.exists():
        return None
    mime, _ = mimetypes.guess_type(str(p))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    try:
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except OSError:
        return None
