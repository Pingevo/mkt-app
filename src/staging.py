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
    seg_mode: str = "single"
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
            seg_mode = result.get("mode", "single")
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
        seg_mode = "single"

    # จับคู่กับสินค้าเดิม
    existing = product_db.get_all_products()
    matches = match_segments_to_existing(segments, existing)

    # เซฟลง batch.json — preserve seg_mode so commit_batch can decide
    # media association ambiguity from the actual segmentation result,
    # not from bool(product_key) which can be non-empty for single mode.
    batch = _load_batch(batch_id)
    batch["status"] = "segmented"
    batch["segments"] = segments
    batch["seg_mode"] = seg_mode
    batch["matches"] = matches
    _save_batch(batch_id, batch)

    return {"segments": segments, "matches": matches, "mode": seg_mode}


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

    # Backward compatibility: batches created before seg_mode was introduced
    # have no seg_mode field.  Deterministically infer it from segment count:
    #   - more than one segment → "multi" (catalog was split)
    #   - exactly one segment  → "single"
    # This prevents shared catalog media from being exposed as product-specific
    # media for legacy multi-segment batches.
    seg_mode = batch.get("seg_mode")
    if seg_mode not in ("single", "multi"):
        seg_mode = "multi" if len(segments) > 1 else "single"
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
            # copy ไฟล์จาก staging (hard link หรือ copy — ทั้งสอง survive discard)
            saved_files = _copy_source_files(source_dir, new_data_dir)

            _write_product_record(name, seg, new_data_dir, is_create=True,
                                  saved_files=saved_files, seg_mode=seg_mode)
            created.append(name)

        elif action == "update":
            name = choice.get("target")
            if not name:
                raise ValueError(f"update ต้องมี target (segment {idx})")
            data_dir = project_root / "data" / name
            # Copy newly uploaded batch source files into the target's
            # data directory so media extraction sees the NEW files,
            # not just the old ones.  Existing files are preserved.
            saved_files = _copy_source_files(source_dir, data_dir)
            _write_product_record(name, seg, data_dir, is_create=False,
                                  saved_files=saved_files, seg_mode=seg_mode)
            updated.append(name)

    # ลบ staging
    discard_batch(batch_id)

    return {"created": created, "updated": updated}


def _copy_source_files(source_dir: Path, dest_dir: Path) -> dict[str, str]:
    """Copy/hard-link source files from staging into a product data dir.

    Hash-aware deduplication (reuses the repository's existing content-hash
    contract from product_db.compute_file_hash):
      - same filename + same hash → skip (idempotent)
      - same filename + different hash → save with a deterministic
        deduplicated filename (e.g. ``catalog (1).xlsx``) so the new
        revision is preserved alongside the old one
      - new filename → save directly

    All copied files survive discard_batch() because they are hard links
    or copies, not symlinks.

    Returns a mapping of ``source_filename -> saved_relative_filename``
    so callers can persist the actual saved filename/path in provenance
    and ensure segment text, source references, and stored source file
    all refer to the same uploaded revision.
    """
    import shutil
    from .product_db import compute_file_hash

    saved: dict[str, str] = {}
    if not source_dir.exists():
        return saved
    for src_file in source_dir.iterdir():
        if not src_file.is_file() or src_file.name.startswith(".") or src_file.name == ".DS_Store":
            continue
        src_hash = compute_file_hash(src_file)
        dest = dest_dir / src_file.name
        if dest.exists():
            dest_hash = compute_file_hash(dest)
            if dest_hash == src_hash:
                # Same content — idempotent skip
                saved[src_file.name] = src_file.name
                continue
            # Same name, different content — find a deterministic deduped name
            stem = src_file.stem
            suffix_num = 1
            ext = src_file.suffix
            deduped_name = None
            existing_deduped = None
            while True:
                candidate = f"{stem} ({suffix_num}){ext}"
                candidate_dest = dest_dir / candidate
                if not candidate_dest.exists():
                    deduped_name = candidate
                    break
                if compute_file_hash(candidate_dest) == src_hash:
                    # Already saved this revision under a deduped name
                    existing_deduped = candidate
                    break
                suffix_num += 1
            if existing_deduped is not None:
                # Idempotent repeat — return the existing deduped filename
                saved[src_file.name] = existing_deduped
                continue
            if deduped_name is None:
                continue
            try:
                import os
                os.link(str(src_file), str(dest_dir / deduped_name))
            except (OSError, AttributeError):
                shutil.copy2(str(src_file), str(dest_dir / deduped_name))
            saved[src_file.name] = deduped_name
            continue
        # New file
        try:
            import os
            os.link(str(src_file), str(dest))
        except (OSError, AttributeError):
            shutil.copy2(str(src_file), str(dest))
        saved[src_file.name] = src_file.name
    return saved


def _write_product_record(name: str, seg: dict, data_dir: Path, *, is_create: bool,
                          saved_files: dict[str, str] | None = None,
                          seg_mode: str = "single") -> None:
    """สร้าง/อัปเดต product DB record จาก segment + ไฟล์ใน data_dir.

    For create: fresh record, extracted media replaces image_descriptions.
    For update: MERGE new extracted media with existing image_descriptions,
    preserving existing valid media unless overwritten by a new file with
    the same source name.  Existing video_transcripts and audio_transcripts
    are always preserved.

    saved_files: mapping of source_filename -> saved_relative_filename from
    _copy_source_files.  Used to record provenance so segment text, source
    references, and stored source file all refer to the same uploaded
    revision (especially when a same-name/different-content revision was
    saved under a deduped filename).

    seg_mode: the actual segmentation mode ("single"|"multi") from
    segment_products → run_segmentation → batch persistence.  Used to
    decide whether embedded media association is ambiguous.  product_key
    is used solely for identity/scope, not as a proxy for product count.
    """
    from datetime import datetime
    from .ingestion import _classify_file, _load_config, extract_embedded_media

    record = product_db.load(name)
    record["product_id"] = name
    record["status"] = product_db.STATUS_PROCESSING

    # Preserve existing media that must not be overwritten on update
    existing_images = list(record.get("image_descriptions", [])) if not is_create else []
    existing_video_transcripts = list(record.get("video_transcripts", [])) if not is_create else []
    existing_audio_transcripts = list(record.get("audio_transcripts", [])) if not is_create else []

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

    # Extract embedded media (PDF/DOCX/XLSX) into the product's cache.
    # Uses the same shared extraction contract as ingest_product so
    # split products retain usable extracted images with valid paths
    # after the staging batch is discarded.
    #
    # Media association policy (generic, metadata-driven):
    #   - Single mode (mode="single"): embedded media belongs to that
    #     one product → NOT labeled unassigned → remains usable through
    #     get_product_image_paths().
    #   - Multi mode (mode="multi"): segmentation split a catalog into
    #     multiple products.  Whether an image can be safely associated
    #     depends on the available deterministic metadata:
    #       - PDF images have page + y0 metadata → get_product_image_paths()
    #         can associate them to a product using scope.source_refs
    #         (page + line range → estimated y-position → nearest image).
    #         These are NOT labeled unassigned.
    #       - XLSX/DOCX images lack page/position metadata → no
    #         deterministic association is possible in multi-mode.
    #         These ARE labeled unassigned_source_media so the media is
    #         persisted with provenance but not exposed as product-specific
    #         multimodal input.
    is_multi_split = (seg_mode == "multi")
    project_root = _project_root()
    dest_cache_dir = project_root / "cache" / name / "extracted_images"
    new_extracted_images: list[dict] = []
    if data_dir.exists():
        for f in sorted(data_dir.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                continue
            suffix = f.suffix.lower()
            if suffix in {".pdf", ".docx", ".xlsx", ".xls"}:
                try:
                    for img in extract_embedded_media(f, dest_cache_dir):
                        if is_multi_split:
                            # PDF images carry page + y0 metadata that
                            # get_product_image_paths() uses for
                            # deterministic per-product association.
                            # Only mark as unassigned when no page
                            # metadata is available (XLSX/DOCX).
                            has_page_meta = img.get("page") is not None
                            if not has_page_meta:
                                img["unassigned_source_media"] = True
                        new_extracted_images.append(img)
                except Exception:
                    continue

    # Merge: keep existing images whose paths still exist, then add new ones.
    # Don't duplicate — if a new image has the same path as an existing one,
    # the new one replaces the old.
    new_paths = {img.get("path") for img in new_extracted_images}
    merged_images = [img for img in existing_images if img.get("path") not in new_paths and Path(img.get("path", "")).exists()]
    merged_images.extend(new_extracted_images)
    record["image_descriptions"] = merged_images

    # Preserve existing transcripts (don't overwrite with empty)
    record["video_transcripts"] = existing_video_transcripts
    record["audio_transcripts"] = existing_audio_transcripts

    # Build the remap dict from saved_files: only entries where the
    # saved filename differs from the original upload filename (i.e.
    # a same-name/different-content revision was saved under a deduped
    # name).  This remap applies ONLY to references originating from
    # the current batch (scope.source_refs, scope.common_refs, and
    # fallback text_extracts created from source refs).  It does NOT
    # apply to text_extracts read from actual stored files — those
    # already have the correct filename matching the file on disk.
    remap = {k: v for k, v in (saved_files or {}).items() if k != v}

    def _remap_ref(ref: dict) -> dict:
        fname = ref.get("file", "")
        if fname in remap:
            return {**ref, "file": remap[fname]}
        return ref

    # text_extracts must preserve the FULL text of each source file (not
    # just the segment slice) so that get_product_image_paths can find the
    # original file by name and determine page boundaries for image
    # association via scope.source_refs.  raw_text remains the scoped
    # segment text for this product.
    #
    # text_extracts are built by scanning actual stored files, so their
    # `file` values already contain the real filenames on disk (e.g.
    # "catalog.txt" for old content, "catalog (1).txt" for new content).
    # Do NOT remap these — they already match the stored files.
    from .ingestion import extract_text
    text_extracts: list[dict] = []
    if data_dir.exists():
        for f in sorted(data_dir.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                continue
            ftype = _classify_file(f.name, _load_config())
            if ftype == "text":
                try:
                    full_text = extract_text(f, _load_config())
                    if full_text:
                        text_extracts.append({"file": f.name, "text": full_text})
                except Exception:
                    continue
    # If we couldn't extract full text, fall back to the segment text
    # labeled with the first source_ref file (remapped to the actual
    # saved filename) so page association still has a chance to work.
    if not text_extracts:
        source_refs = seg.get("source_refs", [])
        ref_file = source_refs[0].get("file", "segmented") if source_refs else "segmented"
        # Apply remap so the fallback extract points to the actual saved file
        ref_file = remap.get(ref_file, ref_file)
        text_extracts = [{"file": ref_file, "text": seg.get("text", "")}]
    record["text_extracts"] = text_extracts
    record["raw_text"] = seg.get("text", "")

    # scope (ถ้ามี product_key) — remap source_refs and common_refs from
    # the current batch to the actual saved filenames
    if seg.get("product_key"):
        source_refs = [_remap_ref(r) for r in seg.get("source_refs", [])]
        common_refs = [_remap_ref(r) for r in seg.get("common_refs", [])]
        record["scope"] = {
            "product_key": seg.get("product_key", ""),
            "source_refs": source_refs,
            "common_refs": common_refs,
        }

    # Persist source file provenance — maps original upload filename to
    # the actual saved filename (may differ when a same-name/different-
    # content revision was saved under a deduped name).  This ensures
    # segment text, source references, and stored source file all refer
    # to the same uploaded revision.
    if saved_files:
        record["source_file_provenance"] = dict(saved_files)

    # metadata — image_count รวม merged images + source image files
    source_image_count = sum(1 for f in files_list if f.get("type") == "image")
    record["metadata"] = {
        "summary": seg.get("summary", ""),
        "category": seg.get("category", ""),
        "file_count": len(files_list),
        "has_images": bool(merged_images) or source_image_count > 0,
        "image_count": len(merged_images) + source_image_count,
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
