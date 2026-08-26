"""Staging — พื้นที่พักไฟล์อัปโหลดก่อน materialize เป็นสินค้าจริง (block A).

Flow:
  1. user อัปโหลด → create_batch() เซฟไป data/.staging/{batch_id}/source/
  2. run_segmentation() รัน extract + segmentation + จับคู่กับสินค้าเดิม
     → คืน segments + matches (ข้อเสนอ ไม่ใช่การกระทำ)
  3. user เห็น preview ในหน้าต่าง → เลือก update/create แต่ละตัว → กดยืนยัน
  4. commit_batch() materialize ตามที่ user เลือก
  5. discard_batch() ลบ staging (ยกเลิก หรือ ปิดหน้าต่าง)

Lifecycle: batch หายถ้าปิดหน้าต่าง หรือ commit แล้ว
โครงสร้าง:
  data/.staging/{batch_id}/
    ├── source/       ไฟล์ต้นฉบับที่อัปโหลด
    └── batch.json    {batch_id, created_at, status, files, segments?, matches?}
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from . import product_db
from .product_match import match_segments_to_existing

# Constants — implementation detail ไม่ใช่ user-facing config
_BATCH_ID_LENGTH = 12  # ความยาว batch_id แบบสั้น พอจะ unique ในเครื่อง
_DEFAULT_PRODUCT_NAME_PREFIX = "สินค้า-"  # fallback ชื่อสินค้าถ้า extract ไม่ได้ชื่อ


def _generate_product_profile(product_id: str, llm=None) -> None:
    """Lazy wrapper — หลีกเลี่ยง circular import กับ ingestion."""
    from .ingestion import _generate_product_profile as _impl
    _impl(product_id, llm)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _staging_root() -> Path:
    return _project_root() / "data" / ".staging"


def _batch_dir(batch_id: str) -> Path:
    return _staging_root() / batch_id


def _batch_json_path(batch_id: str) -> Path:
    return _batch_dir(batch_id) / "batch.json"


def _empty_batch(batch_id: str, files: list[dict]) -> dict:
    return {
        "batch_id": batch_id,
        "created_at": datetime.now().isoformat(),
        "status": "staged",
        "files": files,
    }


def create_batch(files: list[tuple[str, bytes]]) -> str:
    """สร้าง staging batch — เซฟไฟล์ไป data/.staging/{batch_id}/source/.

    Args:
        files: list ของ (filename, content)

    คืน: batch_id (unique)

    ข้าม dotfile (.DS_Store, .hidden). สร้าง batch.json เก็บ metadata.
    """
    batch_id = uuid.uuid4().hex[:_BATCH_ID_LENGTH]
    batch_dir = _batch_dir(batch_id)
    source_dir = batch_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=False)

    saved_files: list[dict] = []
    for filename, content in files:
        if not filename or filename.startswith(".") or filename == ".DS_Store":
            continue
        dest = source_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            i = 1
            while dest.exists():
                dest = source_dir / f"{stem}_{i}{suffix}"
                i += 1
        dest.write_bytes(content)
        saved_files.append({
            "name": dest.name,
            "size": len(content),
            "hash": product_db.compute_file_hash(dest),
        })

    batch = _empty_batch(batch_id, saved_files)
    _batch_json_path(batch_id).write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
    return batch_id


def discard_batch(batch_id: str) -> None:
    """ลบ staging batch ทิ้ง (ยกเลิก หรือ ปิดหน้าต่าง).

    Idempotent — ถ้า batch_id ไม่มี ไม่ error.
    """
    batch_dir = _batch_dir(batch_id)
    if batch_dir.exists():
        shutil.rmtree(batch_dir, ignore_errors=True)


def _name_from_filename(filename: str) -> str:
    """สร้างชื่อสินค้าจากชื่อไฟล์ — เหมือน _makeProductNameFromFile ใน UI."""
    base = re.sub(r"\.[^.]+$", "", filename).strip()
    clean = re.sub(r"[^\u0E00-\u0E7A\w\s-]", "", base).strip()
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or _DEFAULT_PRODUCT_NAME_PREFIX + datetime.now().strftime("%Y%m%d%H%M%S")


def _extract_text_from_source(source_dir: Path) -> list[dict]:
    """extract text จากไฟล์ใน source/ — คืน [{name, text}] (skip ที่ extract ไม่ได้)."""
    from .file_loader import load_file

    extracted: list[dict] = []
    for f in sorted(source_dir.iterdir()):
        if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
            continue
        try:
            text = load_file(f)
            if text and text.strip():
                extracted.append({"name": f.name, "text": text})
        except Exception:
            continue  # ไฟล์ที่ extract ไม่ได้ (รูป, วิดีโอ) → ข้าม
    return extracted


def run_segmentation(batch_id: str, llm=None) -> dict:
    """รัน extract + segmentation + จับคู่กับสินค้าเดิม.

    Args:
        batch_id: จาก create_batch
        llm: LLM client (optional) — ถ้า None ใช้ single-product flow

    คืน: {segments: [...], matches: [...]}
      segments: [{product_key, suggested_name, text, ...}]
      matches: [{segment, action: update/create, target, match_by}]

    เซฟผลลัพธ์ลง batch.json (status=segmented) เพื่อดึงภายหลัง.
    """
    batch_dir = _batch_dir(batch_id)
    source_dir = batch_dir / "source"
    if not source_dir.exists():
        raise FileNotFoundError(f"Batch {batch_id} ไม่มี source/")

    extracted = _extract_text_from_source(source_dir)

    # ถ้ามี LLM → ลอง segmentation (แยกหลายสินค้าจาก catalog)
    segments: list[dict] | None = None
    if llm is not None and extracted:
        from .product_segmentation import segment_products
        seg_files = [{"name": e["name"], "path": "", "text": e["text"], "type": "text"}
                     for e in extracted]
        try:
            result = segment_products(seg_files, llm)
        except Exception:
            result = None

        if result and not result.get("error") and result.get("products"):
            segments = result["products"]
        # error หรือไม่มี products → ใช้ single flow ข้างล่าง

    # single-product flow (no LLM, LLM บอก 1 สินค้า, หรือ segmentation ล้มเหลว)
    if segments is None:
        all_text = "\n\n".join(e["text"] for e in extracted if e.get("text"))
        first_file = extracted[0]["name"] if extracted else "product"
        segments = [{
            "product_key": "",
            "suggested_name": _name_from_filename(first_file),
            "category": "",
            "summary": "",
            "text": all_text,
        }]

    # จับคู่กับสินค้าเดิม
    existing = product_db.get_all_products()
    matches = match_segments_to_existing(segments, existing)

    # เซฟลง batch.json
    batch = _load_batch(batch_id)
    batch["status"] = "segmented"
    batch["segments"] = segments
    batch["matches"] = matches
    _save_batch(batch_id, batch)

    return {"segments": segments, "matches": matches}


def _load_batch(batch_id: str) -> dict:
    """โหลด batch.json — คืน {} ถ้าไม่มี."""
    path = _batch_json_path(batch_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_batch(batch_id: str, batch: dict) -> None:
    """เซฟ batch.json."""
    path = _batch_json_path(batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")


def commit_batch(batch_id: str, choices: list[dict]) -> dict:
    """materialize ตามที่ user เลือก แล้วลบ staging.

    Args:
        batch_id: จาก create_batch (ต้อง run_segmentation แล้ว)
        choices: list ของ {segment_index, action: "create"|"update",
                            target?: str (สำหรับ update), name?: str (สำหรับ create)}

    คืน: {created: [names], updated: [names]}

    หลัง commit แล้ว staging ถูกลบทิ้ง.
    """
    import shutil
    from datetime import datetime

    batch = _load_batch(batch_id)
    segments = batch.get("segments", [])
    if not segments:
        raise ValueError(f"Batch {batch_id} ยังไม่ได้ run_segmentation")

    source_dir = _batch_dir(batch_id) / "source"
    project_root = _project_root()
    created: list[str] = []
    updated: list[str] = []

    for choice in choices:
        idx = choice["segment_index"]
        action = choice["action"]
        seg = segments[idx]

        if action == "create":
            name = choice.get("name") or seg.get("suggested_name") or f"product-{idx + 1}"
            # dedup ชื่อที่มีอยู่แล้ว
            base_name = name
            suffix = 1
            while (project_root / "data" / name).exists():
                name = f"{base_name} ({suffix})"
                suffix += 1

            new_data_dir = project_root / "data" / name
            new_data_dir.mkdir(parents=True, exist_ok=False)
            # copy ไฟล์จาก staging
            if source_dir.exists():
                for src_file in source_dir.iterdir():
                    if not src_file.is_file() or src_file.name.startswith(".") or src_file.name == ".DS_Store":
                        continue
                    dest = new_data_dir / src_file.name
                    try:
                        import os
                        os.link(str(src_file), str(dest))
                    except (OSError, AttributeError):
                        shutil.copy2(str(src_file), str(dest))

            _write_product_record(name, seg, new_data_dir)
            created.append(name)

        elif action == "update":
            name = choice.get("target")
            if not name:
                raise ValueError(f"update ต้องมี target (segment {idx})")
            # อัปเดต record ของสินค้าเดิม (ไม่แตะไฟล์ — ใช้ของเดิม)
            data_dir = project_root / "data" / name
            _write_product_record(name, seg, data_dir)
            updated.append(name)

    # ลบ staging
    discard_batch(batch_id)

    return {"created": created, "updated": updated}


def _write_product_record(name: str, seg: dict, data_dir: Path) -> None:
    """สร้าง/อัปเดต product DB record จาก segment + ไฟล์ใน data_dir."""
    from datetime import datetime
    from .ingestion import _classify_file, _load_config

    record = product_db.load(name)
    record["product_id"] = name
    record["status"] = product_db.STATUS_PROCESSING

    # สแกนไฟล์ใน data_dir
    files_list: list[dict] = []
    if data_dir.exists():
        for f in sorted(data_dir.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                continue
            ftype = _classify_file(f.name, _load_config())
            files_list.append({
                "name": f.name,
                "path": str(f),
                "type": ftype,
                "status": "ingested" if ftype is not None else "unsupported",
                "hash": product_db.compute_file_hash(f),
                "size": f.stat().st_size,
                "ingested_at": datetime.now().isoformat() if ftype is not None else None,
            })
    record["files"] = files_list

    # text จาก segment
    record["text_extracts"] = [{"file": "segmented", "text": seg.get("text", "")}]
    record["raw_text"] = seg.get("text", "")

    # scope (ถ้ามี product_key)
    if seg.get("product_key"):
        record["scope"] = {
            "product_key": seg.get("product_key", ""),
            "source_refs": seg.get("source_refs", []),
            "common_refs": seg.get("common_refs", []),
        }

    # metadata
    record["metadata"] = {
        "summary": seg.get("summary", ""),
        "category": seg.get("category", ""),
        "file_count": len(files_list),
        "has_images": any(f.get("type") == "image" for f in files_list),
        "image_count": sum(1 for f in files_list if f.get("type") == "image"),
    }

    product_db.save(name, record)
    product_db.set_status(name, product_db.STATUS_READY)
    # สร้าง product profile อัตโนมัติเหมือน flow ingestion เดิม
    # (จะสร้างเฉพาะครั้งแรก — ไม่ทับของ user ที่แก้ไว้)
    try:
        _generate_product_profile(name)
    except Exception:
        # ถ้า AI สร้าง profile ไม่ได้ ไม่ขัดขวางสินค้าที่สร้างแล้ว
        pass
