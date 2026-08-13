"""Product DB — จัดการข้อมูลสินค้าแบบ structured (JSON ต่อสินค้า).

ทำหน้าที่เป็น "DB สินค้าของบริษัท" ที่ agent การตลาดดึงไปใช้
แยกออกจาก product_spec agent (ที่ทำ deliverable ให้ user)

โครงสร้างไฟล์:
  data/{product_id}/                — raw files ที่ user upload (เท่านั้น ไม่มีไฟล์ระบบปน)
  cache/{product_id}/product.json   — DB ของระบบ (status, raw_text, image_descriptions, ฯลฯ)
  cache/{product_id}/               — deliverables ของ product_spec agent (เอกสารสเปค)

data/ มีแค่ไฟล์ user เท่านั้น — ไฟล์ระบบทั้งหมดอยู่ใน cache/
อนาคต: เปลี่ยนเป็น MongoDB ได้โดยแก้แค่ไฟล์นี้
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

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
        "raw_text": "",               # text ที่ parse ได้จาก text files (รวมกัน)
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
            if not item.is_file() or item.name.startswith(".") or item.name == ".DS_Store":
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
    }


def get_agent_context_text(product_id: str) -> str:
    """สร้าง text สำหรับยัดเป็น context ของ agent การตลาด.

    วิธีสากล: ส่ง raw text ทั้งหมดให้ agent โดยไม่สกัด fields ล่วงหน้า
    แต่ละ agent จะแยกเองว่าต้องการข้อมูลอะไร
    """
    data = get_fields_for_agent(product_id)
    parts = []

    if data["raw_text"]:
        parts.append("--- ข้อมูลดิบ (text) ---")
        parts.append(data["raw_text"][:12000])  # จำกัดป้องกัน token เกิน
        parts.append("--- สิ้นสุดข้อมูลดิบ ---\n")

    if data["image_descriptions"]:
        parts.append("--- คำบรรยายรูปภาพ ---")
        for desc in data["image_descriptions"]:
            parts.append(f"[{desc.get('file', '?')}]: {desc.get('description', '')}")
        parts.append("--- สิ้นสุดคำบรรยายรูป ---\n")

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
