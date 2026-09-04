"""Ingestion pipeline — แปลง raw data → product DB.

วิธีสากล (เหมือน Claude, ChatGPT, Cursor, Devin):
  - parse ไฟล์ด้วยวิธีกำหนดได้ (deterministic) → เก็บ raw text
  - รูป/วิดีโอ → ใช้ LLM vision บรรยายเป็น text (ไม่มีวิธีอื่น)
  - ไม่สกัด structured fields ล่วงหน้า — ส่ง raw text ให้ agent ตอนทำงานจริง
    แต่ละ agent ต้องการข้อมูลคนละแบบ การสกัดล่วงหน้าอาจตัดทิ้งส่วนที่ agent อื่นต้องการ

Flow:
  1. scan โฟลเดอร์สินค้า → แยกไฟล์ตามประเภท (text/image/video/audio/unsupported)
  2. ตรวจ hash → ข้ามไฟล์ที่ไม่เปลี่ยน
  3. เรียก preprocessor ตามประเภท:
     - text: extract text (pandas, pdfplumber, python-docx) — ไม่ใช้ LLM
     - image: ส่งเข้า LLM เป็น image input → บรรยายเป็น text
     - video: ffmpeg ดึง key frames → ส่งเข้า LLM
     - audio: whisper transcribe
  4. บันทึกลง product DB (raw_text + image_descriptions + video_transcripts)
  5. อัปเดตสถานะ → ready

ถ้าไฟล์ไม่รองรับ → ข้าม บันทึกเป็น unsupported ไม่ทำลาย
ถ้าไฟล์ทั้งหมดไม่รองรับ → สถานะ = no_usable_data
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from . import product_db
from .brand_loader import load_product_profile
from .config_loader import _project_root
from .file_loader import load_file
from .llm_client import LLMClient


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_config() -> dict[str, Any]:
    path = _project_root() / "config" / "ingestion.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _classify_file(filename: str, config: dict) -> str | None:
    """จัดประเภทไฟล์ คืน 'text'/'image'/'video'/'audio' หรือ None ถ้าไม่รองรับ."""
    ext = Path(filename).suffix.lower()
    for ftype, exts in config.get("supported_formats", {}).items():
        if ext in exts:
            return ftype
    return None


def _file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def _check_size(file_path: Path, ftype: str, config: dict) -> tuple[bool, str]:
    """ตรวจขนาดไฟล์ คืน (ok, message)."""
    max_sizes = config.get("max_file_size_mb", {})
    max_mb = max_sizes.get(ftype, 100)
    size_mb = _file_size_mb(file_path)
    if size_mb > max_mb:
        return False, f"ไฟล์ใหญ่เกินไป ({size_mb:.1f}MB > {max_mb}MB)"
    return True, ""


# ------------------------------------------------------------------
#  Preprocessors
# ------------------------------------------------------------------

def extract_text(file_path: Path, config: dict, llm: LLMClient | None = None) -> str:
    """แปลง text/PDF/Excel/Word → text + ดึงรูปที่ฝังใน document ออกมาเก็บเป็นไฟล์.

    สถาปัตยกรรมใหม่ (retrieve-then-read):
      - text: ดึงออกมาเก็บใน DB (lossless — text คือ text)
      - รูปใน xlsx/docx: ดึงออกเก็บเป็นไฟล์ + path ใน DB (lossless)
      - PDF: เก็บ path ดิบไว้ ส่งให้ OpenRouter ตอน agent ทำงาน (ไม่ extract text ด้วย OCR)

    คืนค่า: text ที่ดึงได้ (string)
    ผลข้างเคียง: รูปที่ดึงได้เก็บใน _extracted_images (ใช้โดย ingest_product)
    """
    global _extracted_images
    _extracted_images = []

    suffix = file_path.suffix.lower()
    text = load_file(str(file_path))

    # ดึงรูปที่ฝังใน xlsx/docx ออกมาเก็บเป็นไฟล์ (lossless)
    if suffix in {".xlsx", ".xls"}:
        _extracted_images = _extract_images_from_xlsx(file_path)
    elif suffix == ".docx":
        _extracted_images = _extract_images_from_docx(file_path)

    return text


# เก็บรูปที่ดึงได้จาก document ระหว่างการเรียก extract_text
_extracted_images: list[str] = []


def _extract_images_from_xlsx(file_path: Path) -> list[str]:
    """ดึงรูปที่ฝังใน xlsx ออกมาเก็บเป็นไฟล์ — คืน list ของ path รูปที่บันทึกแล้ว.

    วิธีสากล (ตาม VOYAGER-Inc/excel-vision-mcp):
      1. สแกน xl/media/ ใน zip archive (จับได้ครบทุกรูป)
      2. บันทึกแต่ละรูปเป็นไฟล์ใน cache/{product_id}/extracted_images/
    """
    import zipfile

    saved_paths: list[str] = []
    out_dir = _extracted_images_dir(file_path)

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            media_files = [
                name for name in zf.namelist()
                if name.startswith("xl/media/")
                and Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
            ]
            for idx, media_path in enumerate(sorted(media_files)):
                try:
                    raw = zf.read(media_path)
                    ext = Path(media_path).suffix.lower() or ".png"
                    out_file = out_dir / f"xlsx_img_{idx:03d}{ext}"
                    out_file.write_bytes(raw)
                    saved_paths.append(str(out_file))
                except Exception:
                    continue
    except (zipfile.BadZipFile, OSError):
        pass

    return saved_paths


def _extract_images_from_docx(file_path: Path) -> list[str]:
    """ดึงรูปที่ฝังใน docx ออกมาเก็บเป็นไฟล์ — คืน list ของ path รูป.

    วิธี: docx = zip archive, รูปอยู่ใน word/media/
    """
    import zipfile

    saved_paths: list[str] = []
    out_dir = _extracted_images_dir(file_path)

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            media_files = [
                name for name in zf.namelist()
                if name.startswith("word/media/")
                and Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
            ]
            for idx, media_path in enumerate(sorted(media_files)):
                try:
                    raw = zf.read(media_path)
                    ext = Path(media_path).suffix.lower() or ".png"
                    out_file = out_dir / f"docx_img_{idx:03d}{ext}"
                    out_file.write_bytes(raw)
                    saved_paths.append(str(out_file))
                except Exception:
                    continue
    except (zipfile.BadZipFile, OSError):
        pass

    return saved_paths


def _extracted_images_dir(file_path: Path) -> Path:
    """โฟลเดอร์เก็บรูปที่ดึงจาก document — อยู่ใน cache/ ไม่ปนกับไฟล์ user ใน data/."""
    # หา product_id จาก path: data/{product_id}/file.xlsx
    parts = file_path.parts
    product_id = "unknown"
    if "data" in parts:
        idx = parts.index("data")
        if idx + 1 < len(parts):
            product_id = parts[idx + 1]
    out_dir = _project_root() / "cache" / product_id / "extracted_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def extract_image_info(file_path: Path, config: dict, llm: LLMClient | None = None) -> str:
    """รูปภาพ — เก็บ path จริง (lossless) ไม่บรรยายเป็น text (lossy).

    สถาปัตยกรรมใหม่ (retrieve-then-read):
      - ingest: เก็บ path รูปจริงใน DB (ไม่เสีย token LLM)
      - agent ทำงาน: ส่งรูปจริง base64 ให้ LLM vision (เห็นเหมือนมนุษย์)

    คืนค่า: path ของรูป (string) — ใช้สำหรับเก็บใน image_descriptions.path
    """
    return str(file_path)


def extract_video_frames(file_path: Path, config: dict, llm: LLMClient | None = None) -> str:
    """ดึง key frames จากวิดีโอ → ส่งเข้า LLM บรรยาย.

    ต้องมี ffmpeg ติดตั้งในระบบ
    """
    max_frames = config.get("video_max_frames", 5)

    with tempfile.TemporaryDirectory() as tmpdir:
        # ใช้ ffmpeg ดึง frames กระจายเท่าๆ กัน
        frame_pattern = str(Path(tmpdir) / "frame_%03d.jpg")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-i", str(file_path),
                    "-vf", f"fps=1/{max_frames}",  # 1 frame ทุก N วินาที
                    "-vframes", str(max_frames),
                    "-q:v", "2",
                    frame_pattern,
                    "-y",
                ],
                capture_output=True,
                timeout=120,
            )
        except FileNotFoundError:
            return "[ffmpeg not installed — ไม่สามารถประมวลผลวิดีโอได้]"
        except subprocess.TimeoutExpired:
            return "[ffmpeg timeout — ไฟล์วิดีโอใหญ่เกินไป]"

        # รวบรวม frames ที่ได้
        frames = sorted(Path(tmpdir).glob("frame_*.jpg"))
        if not frames:
            return "[ไม่สามารถดึง frame จากวิดีโอได้]"

        # ถ้ามี LLM → ส่ง frames เข้า LLM
        if llm is not None:
            import base64
            content: list[dict] = [
                {
                    "type": "text",
                    "text": (
                        f"ต่อไปนี้คือ {len(frames)} frames จากวิดีโอสินค้า "
                        "บรรยายเป็นภาษาไทยว่าวิดีโอนี้เกี่ยวกับอะไร สินค้าคืออะไร "
                        "มีการนำเสนออย่างไร กระชับ ไม่เกิน 200 คำ"
                    ),
                }
            ]
            for frame in frames:
                with open(frame, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })

            messages = [{"role": "user", "content": content}]
            try:
                ing_cfg = _ingestion_cfg()
                return llm.chat(
                    messages,
                    model=ing_cfg.get("model", "google/gemini-2.5-flash"),
                    temperature=ing_cfg.get("temperature", 0.3),
                    max_tokens=ing_cfg.get("max_tokens_description", 1024),
                    stream=False,
                    source="ingestion.describe_video_frames",
                )
            except Exception as e:
                return f"[LLM vision failed: {e}]"

        # Fallback: ไม่มี LLM → บอกแค่จำนวน frames
        return f"[วิดีโอมี {len(frames)} frames — ต้องการ LLM เพื่อบรรยาย]"


def extract_audio_text(file_path: Path, config: dict, llm: LLMClient | None = None) -> str:
    """Transcribe เสียงเป็น text.

    ตอนนี้ใช้ OpenRouter audio model ถ้ามี ไม่งั้นบอกว่าไม่รองรับ
    (ในอนาคตอาจใช้ whisper local หรือ API อื่น)
    """
    # TODO: เพิ่ม whisper หรือ audio API
    return f"[audio transcription ยังไม่พร้อม — ไฟล์: {file_path.name}]"


PREPROCESSORS = {
    "text": extract_text,
    "image": extract_image_info,
    "video": extract_video_frames,
    "audio": extract_audio_text,
}


# ------------------------------------------------------------------
#  Main pipeline
# ------------------------------------------------------------------

def _generate_product_profile(product_id: str, llm=None) -> None:
    """สร้าง/บันทึก product_profile.json อัตโนมัติจากเนื้อหาที ingest ได้."""
    from .voice_learner import analyze_product_positioning
    from .config_loader import _project_root

    spec_text = product_db.get_agent_context_text(product_id)
    if not spec_text or not spec_text.strip():
        return

    client = llm or _make_llm()
    if client is None:
        return

    try:
        suggested = analyze_product_positioning(spec_text, client)
        if suggested:
            profile_dir = _project_root() / "cache" / product_id
            profile_dir.mkdir(parents=True, exist_ok=True)
            profile_path = profile_dir / "product_profile.json"
            # สร้างเฉพาะครั้งแรก — ไม่ทับของ user ทีแก้ไว้ใน modal
            if not profile_path.exists():
                profile_path.write_text(json.dumps(suggested, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # ถ้า AI สร้าง product profile ไม่ได้ ไม่ขัดขวาง status ready
        pass
    finally:
        if client is not llm and client is not None:
            client.close()


def _make_llm() -> LLMClient | None:
    """สร้าง LLM client สำหรับ ingestion (ใช้สำหรับ image/video/field extraction)."""
    from .config_loader import get_env, load_config, get_section
    api_key = get_env("OPENROUTER_API_KEY")
    if not api_key:
        return None
    try:
        cfg = load_config()
        ing_cfg = get_section(cfg, "ingestion", {})
    except Exception:
        ing_cfg = {}
    return LLMClient(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_model=ing_cfg.get("model", "google/gemini-2.5-flash"),
        timeout=ing_cfg.get("timeout_seconds", 180),
    )


def _ingestion_cfg() -> dict:
    """Get ingestion config from config/ingestion.yaml (lazy load)."""
    return _load_config()


def _scan_product_files(product_id: str, config: dict) -> list[dict[str, Any]]:
    """Scan โฟลเดอร์สินค้า → รายการไฟล์พร้อมประเภท."""
    pdir = _project_root() / "data" / product_id
    if not pdir.exists():
        return []

    files = []
    for item in sorted(pdir.iterdir()):
        if not item.is_file() or item.name.startswith(".") or item.name == ".DS_Store":
            continue
        ftype = _classify_file(item.name, config)
        files.append({
            "name": item.name,
            "path": str(item),
            "type": ftype,  # text/image/video/audio/None
            "size": item.stat().st_size,
            "hash": product_db.compute_file_hash(item),
        })
    return files


def _try_segment_and_split(
    product_id: str,
    all_extracted: list[dict[str, Any]],
    llm,
) -> dict[str, Any] | None:
    """ตรวจและแยก catalog หลายสินค้า — คืน result dict ถ้าแยกสำเร็จ, None ถ้าไม่แยก.

    ขั้นตอน:
      1. เรียก segment_products() กับ text ที่ extract ได้
      2. ถ้า mode=single → คืน None (ใช้ flow เดิมต่อ)
      3. ถ้า mode=multi → materialize สินค้าแยก + ลบโฟลเดอร์ต้นฉบับ
      4. ถ้ามี error → คืน None (fallback สู่ single flow เดิม)
    """
    from .product_segmentation import segment_products

    # เตรียม files สำหรับ segmentation — เฉพาะ text ที่ extract ได้
    seg_files: list[dict] = []
    for ext in all_extracted:
        if ext.get("type") == "text" and ext.get("text"):
            seg_files.append({
                "name": ext["file"],
                "path": "",  # ไม่จำเป็นตอนนี้ — segmentation ใช้ text
                "text": ext["text"],
                "type": "text",
            })

    if not seg_files:
        return None

    result = segment_products(seg_files, llm)

    # error หรือ single → ใช้ flow เดิม
    if result.get("error") or result.get("mode") != "multi":
        return None

    products = result.get("products", [])
    if len(products) <= 1:
        return None

    # materialize — สร้างสินค้าแยก
    return _materialize_split_products(product_id, products, llm)


def _materialize_split_products(
    temp_product_id: str,
    segments: list[dict],
    llm,
) -> dict[str, Any]:
    """สร้าง/อัปเดตสินค้าจาก segments + ลบโฟลเดอร์ต้นฉบับ (idempotent).

    ขั้นตอน:
      1. จับคู่ segment กับสินค้าที่มีอยู่ด้วย product_key (หรือ text_hash สำรอง)
         — ตัวที่ตรง = update สินค้าเดิม, ตัวที่ไม่ตรง = create ใหม่
      2. จองชื่อเฉพาะ segment ที่ create (dedup ชนโฟลเดอร์ที่มีอยู่)
      3. สร้างโฟลเดอร์ + source files (hard link/copy) เฉพาะ create
      4. สร้าง/อัปเดต product DB records (raw_text, metadata, scope, status=ready)
         — update ที่ text เดิมไม่เปลี่ยน → ข้าม re-profile (ประหยัด LLM)
      5. ลบโฟลเดอร์ต้นฉบับ + cache ต้นฉบับ

    ถ้า error ระหว่างทำ → rollback (ลบเฉพาะที่รอบนี้สร้างใหม่) แล้ว raise.
    """
    import shutil

    from .product_match import match_segments_to_existing

    project_root = _project_root()
    temp_data_dir = project_root / "data" / temp_product_id
    temp_cache_dir = project_root / "cache" / temp_product_id

    # 1. จับคู่ segment กับสินค้าที่มีอยู่ — ไม่นับโฟลเดอร์ต้นฉบับ (temp)
    existing = [r for r in product_db.get_all_products()
                if r.get("product_id") != temp_product_id]
    matches = match_segments_to_existing(segments, existing)

    # 2. จองชื่อเฉพาะ segment ที่ create (dedup ชนโฟลเดอร์/ชื่อที่ใช้แล้ว)
    used_names: set[str] = {r.get("product_id", "") for r in existing}
    create_names: list[str] = []   # เรียงตาม create segment
    for i, m in enumerate(matches):
        if m["action"] != "create":
            continue
        seg = m["segment"]
        name = (seg.get("suggested_name") or "").strip()
        if not name:
            name = seg.get("product_key") or f"product-{i + 1}"
        base_name = name
        suffix = 1
        while (project_root / "data" / name).exists() or name in used_names:
            name = f"{base_name} ({suffix})"
            suffix += 1
        used_names.add(name)
        create_names.append(name)

    # 3. สร้างโฟลเดอร์ + source files เฉพาะ create
    created_dirs: list[Path] = []
    created_caches: list[Path] = []
    try:
        for name in create_names:
            new_data_dir = project_root / "data" / name
            new_data_dir.mkdir(parents=True, exist_ok=False)
            created_dirs.append(new_data_dir)

            # hard link หรือ copy source files จาก temp folder
            if temp_data_dir.exists():
                for src_file in temp_data_dir.iterdir():
                    if not src_file.is_file() or src_file.name.startswith(".") or src_file.name == ".DS_Store":
                        continue
                    dest = new_data_dir / src_file.name
                    try:
                        os.link(str(src_file), str(dest))  # hard link — ไม่กินพื้นที่ซ้ำ
                    except (OSError, AttributeError):
                        shutil.copy2(str(src_file), str(dest))  # fallback copy

        # 4. สร้าง/อัปเดต DB records ทุก segment (ตามลำดับเดิม)
        final_names: list[str] = []
        ci = 0  # cursor ใน create_names
        for m in matches:
            seg = m["segment"]
            if m["action"] == "update":
                name = m["target"]
            else:
                name = create_names[ci]
                ci += 1
            final_names.append(name)

            new_cache_dir = project_root / "cache" / name
            new_cache_dir.mkdir(parents=True, exist_ok=True)
            if m["action"] == "create":
                created_caches.append(new_cache_dir)

            # อัปเดตเฉพาะถ้า text เปลี่ยน — ถ้าเดิม → ข้าม re-profile (ประหยัด LLM)
            old_record = product_db.load(name)
            new_text = seg.get("text", "")
            is_update = m["action"] == "update"
            text_unchanged = is_update and old_record.get("raw_text", "") == new_text

            data_dir = project_root / "data" / name
            record = old_record
            record["product_id"] = name
            record["status"] = product_db.STATUS_PROCESSING

            # source files — สแกนโฟลเดอร์สินค้า (create: โฟลเดอร์ใหม่, update: โฟลเดอร์เดิม)
            files_list: list[dict] = []
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

            # text เฉพาะรุ่น (จาก segmentation — คัดจากต้นฉบับแล้ว)
            record["text_extracts"] = [{"file": "segmented", "text": new_text}]
            record["raw_text"] = new_text

            # scope — บอกขอบเขตสินค้า (ใช้ตอน re-ingest)
            record["scope"] = {
                "product_key": seg.get("product_key", ""),
                "source_refs": seg.get("source_refs", []),
                "common_refs": seg.get("common_refs", []),
                "split_from": temp_product_id,
            }

            # metadata จาก segmentation (ไม่เรียก LLM ซ้ำ — ประหยัด token)
            record["metadata"] = {
                "summary": seg.get("summary", ""),
                "category": seg.get("category", ""),
                "file_count": len(files_list),
                "has_images": any(f.get("type") == "image" for f in files_list),
                "image_count": sum(1 for f in files_list if f.get("type") == "image"),
            }

            product_db.save(name, record)

            if not text_unchanged:
                # สร้าง product profile ของรุ่นนี้ (ใช้ text เฉพาะรุ่น)
                _generate_product_profile(name, llm)

            # ตั้ง status ready
            product_db.set_status(name, product_db.STATUS_READY)

    except Exception as e:
        # rollback — ลบเฉพาะที่รอบนี้สร้างใหม่ (update ไม่ลบ เพราะมีอยู่ก่อน)
        for d in created_dirs:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        for c in created_caches:
            if c.exists():
                shutil.rmtree(c, ignore_errors=True)
        raise RuntimeError(f"Split products failed, rolled back: {e}") from e

    # 5. ลบโฟลเดอร์ต้นฉบับ + cache ต้นฉบับ
    if temp_data_dir.exists():
        shutil.rmtree(temp_data_dir, ignore_errors=True)
    if temp_cache_dir.exists():
        shutil.rmtree(temp_cache_dir, ignore_errors=True)

    return {
        "status": "ready",
        "files_total": len(segments),
        "files_ingested": len(segments),
        "files_skipped": 0,
        "files_unsupported": 0,
        "errors": [],
        "split_products": final_names,
    }


def ingest_product(
    product_id: str,
    force: bool = False,
    *,
    is_new_upload: bool = False,
) -> dict[str, Any]:
    """รัน ingestion pipeline สำหรับสินค้านี้.

    Args:
        product_id: ชื่อสินค้า
        force: ถ้า True → re-ingest ทุกไฟล์แม้ hash เหมือนเดิม
        is_new_upload: True เฉพาะตอนอัปโหลดสินค้าใหม่ — เปิดใช้ catalog
            segmentation (แยกหลายสินค้าจากไฟล์เดียว). re-ingest ของสินค้า
            เดิมไม่ส่ง True เพราะจะทำให้แตกโฟลเดอร์โดยไม่คาดคิด.

    Returns:
        dict สรุปผล: {status, files_total, files_ingested, files_unsupported, errors}
    """
    config = _load_config()

    # ตั้งสถานะ processing
    product_db.set_status(product_id, product_db.STATUS_PROCESSING)

    # 1. Scan files
    files = _scan_product_files(product_id, config)
    if not files:
        product_db.set_status(product_id, product_db.STATUS_EMPTY)
        return {"status": "empty", "files_total": 0, "files_ingested": 0, "errors": []}

    # แยกไฟล์รองรับ/ไม่รองรับ
    usable = [f for f in files if f["type"] is not None]
    unsupported = [f for f in files if f["type"] is None]

    # บันทึกไฟล์ไม่รองรับลง DB
    for f in unsupported:
        product_db.add_file(product_id, {
            "name": f["name"],
            "path": f["path"],
            "type": None,
            "status": "unsupported",
            "hash": f["hash"],
            "size": f["size"],
        })

    if not usable:
        product_db.set_status(product_id, product_db.STATUS_NO_USABLE)
        return {
            "status": "no_usable_data",
            "files_total": len(files),
            "files_ingested": 0,
            "files_unsupported": len(unsupported),
            "errors": [],
        }

    # 2. ตรวจ hash → ข้ามไฟล์ที่ไม่เปลี่ยน (ถ้าไม่ force)
    existing = product_db.load(product_id)
    existing_hashes = {f["path"]: f.get("hash") for f in existing.get("files", [])}
    existing_status = {f["path"]: f.get("status") for f in existing.get("files", [])}

    to_ingest = []
    skipped = []
    for f in usable:
        if not force and existing_hashes.get(f["path"]) == f["hash"] and existing_status.get(f["path"]) == "ingested":
            skipped.append(f)
        else:
            to_ingest.append(f)

    # 3. ประมวลผลไฟล์ทีละไฟล์
    # สถาปัตยกรรมใหม่: LLM ใช้แค่ video (vision) + metadata summary — ไม่ใช้กับ image แล้ว
    #   - image: เก็บ path จริง (lossless, ไม่เสีย token)
    #   - text: deterministic parser (ไม่เสีย token)
    #   - video: ดึง frames → ส่ง LLM (จำเป็น ไม่มีทางอื่น)
    #   - metadata summary: LLM สรุปสั้นๆ 1 ครั้ง (ถูกมาก — text only, 512 max tokens)
    needs_llm = any(f["type"] == "video" for f in to_ingest) or any(f["type"] == "text" for f in to_ingest)
    llm = _make_llm() if needs_llm else None
    total_steps = len(to_ingest)
    current_step = 0
    start_time = time.time()

    all_extracted: list[dict[str, Any]] = []
    errors: list[str] = []

    # เคลียร์ entries ของไฟล์ที่จะ re-ingest + ไฟล์ที่ถูกลบจาก disk
    # - to_ingest_names: ไฟล์ที่จะ re-ingest (hash เปลี่ยนหรือไฟล์ใหม่) → ลบ entry เดิมก่อนเพิ่มใหม่
    # - all_file_names: ไฟล์ทั้งหมดที่อยู่ใน disk ตอนนี้ → เก็บไว้
    # - ไฟล์ที่ไม่อยู่ใน all_file_names = ถูกลบ → ลบ entry ออก
    to_ingest_names = {f["name"] for f in to_ingest}
    all_file_names = {f["name"] for f in files}  # ไฟล์ทั้งหมดใน disk (usable + unsupported)
    record = product_db.load(product_id)
    # ลบ entries ของไฟล์ที่ re-ingest (จะเพิ่มใหม่ในลูป) และไฟล์ที่ถูกลบ (ไม่อยู่ใน disk แล้ว)
    deleted_names = {d.get("file") for d in record.get("text_extracts", []) + record.get("image_descriptions", []) + record.get("video_transcripts", []) + record.get("audio_transcripts", [])} - all_file_names
    remove_names = to_ingest_names | deleted_names

    def _filter_by_file(entries):
        """เก็บเฉพาะ entries ที่ชื่อไฟล์ไม่อยู่ใน remove_names."""
        return [d for d in entries if d.get("file") not in remove_names]

    record["image_descriptions"] = _filter_by_file(record.get("image_descriptions", []))
    record["text_extracts"] = _filter_by_file(record.get("text_extracts", []))
    record["video_transcripts"] = _filter_by_file(record.get("video_transcripts", []))
    record["audio_transcripts"] = _filter_by_file(record.get("audio_transcripts", []))
    product_db.save(product_id, record)

    for f in to_ingest:
        current_step += 1
        ftype = f["type"]
        file_path = Path(f["path"])

        # อัปเดต progress
        elapsed = time.time() - start_time
        if current_step > 1:
            eta = int(elapsed / (current_step - 1) * (total_steps - current_step))
        else:
            eta = None
        product_db.set_progress(
            product_id,
            step=current_step,
            total=total_steps,
            message=f"กำลังประมวลผล {f['name']} ({ftype})",
            eta_seconds=eta,
        )

        # ตรวจขนาด — ถ้าใหญ่เกิน บันทึกเป็น error ใน DB เลย (ไม่ใช่ update ที่อาจไม่เจอ)
        ok, size_msg = _check_size(file_path, ftype, config)
        if not ok:
            product_db.add_file(product_id, {
                "name": f["name"],
                "path": f["path"],
                "type": ftype,
                "status": "error",
                "hash": f["hash"],
                "size": f["size"],
                "error": size_msg,
            })
            errors.append(f"{f['name']}: {size_msg}")
            continue

        # เรียก preprocessor
        try:
            preprocessor = PREPROCESSORS.get(ftype)
            if preprocessor is None:
                product_db.add_file(product_id, {
                    "name": f["name"],
                    "path": f["path"],
                    "type": ftype,
                    "status": "error",
                    "hash": f["hash"],
                    "size": f["size"],
                    "error": f"ไม่มี preprocessor สำหรับ {ftype}",
                })
                errors.append(f"{f['name']}: ไม่มี preprocessor")
                continue

            extracted_text = preprocessor(file_path, config, llm)
            all_extracted.append({
                "file": f["name"],
                "type": ftype,
                "text": extracted_text,
            })

            # บันทึกลง DB ตามประเภท
            if ftype == "image":
                # สถาปัตยกรรมใหม่: เก็บ path รูปจริง (lossless) ไม่บรรยายเป็น text
                # agent จะเห็นรูปจริงตอนทำงาน (ส่ง base64 ให้ LLM vision)
                product_db.append_extracted(product_id, "image_descriptions", {
                    "file": f["name"],
                    "path": f["path"],
                    "description": "",  # ไม่บรรยายแล้ว — agent เห็นรูปจริง
                })
            elif ftype == "video":
                product_db.append_extracted(product_id, "video_transcripts", {
                    "file": f["name"],
                    "transcript": extracted_text,
                })
            elif ftype == "audio":
                product_db.append_extracted(product_id, "audio_transcripts", {
                    "file": f["name"],
                    "transcript": extracted_text,
                })
            elif ftype == "text":
                # text เก็บเป็นรายไฟล์ใน text_extracts (เหมือน video_transcripts)
                # raw_text จะถูก rebuild จาก text_extracts ทั้งหมดหลังลูป
                product_db.append_extracted(product_id, "text_extracts", {
                    "file": f["name"],
                    "text": extracted_text,
                })

                # รูปที่ดึงจาก document (xlsx/docx) → เก็บ path ใน image_descriptions
                global _extracted_images
                for img_path in _extracted_images:
                    product_db.append_extracted(product_id, "image_descriptions", {
                        "file": Path(img_path).name,
                        "path": img_path,
                        "description": "",
                        "source": f["name"],  # บอกว่ารูปนี้มาจากไฟล์ไหน
                    })

            # บันทึกสถานะไฟล์
            product_db.add_file(product_id, {
                "name": f["name"],
                "path": f["path"],
                "type": ftype,
                "status": "ingested",
                "hash": f["hash"],
                "size": f["size"],
                "ingested_at": product_db.load(product_id).get("updated_at"),
            })

        except Exception as e:
            product_db.add_file(product_id, {
                "name": f["name"],
                "path": f["path"],
                "type": ftype,
                "status": "error",
                "hash": f["hash"],
                "size": f["size"],
                "error": str(e),
            })
            errors.append(f"{f['name']}: {e}")

    # 3.5 Catalog segmentation — ถ้าเป็น upload ใหม่และมี LLM ให้ตรวจว่าไฟล์เป็น
    # catalog หลายสินค้าหรือไม่. ถ้าใช่ → สร้างสินค้าแยกและลบโฟลเดอร์ต้นฉบับ.
    if is_new_upload and llm is not None:
        split_result = _try_segment_and_split(product_id, all_extracted, llm)
        if split_result is not None:
            # แยกสินค้าเสร็จแล้ว — ไม่ทำ metadata/profile ของต้นฉบับต่อ
            if llm is not None:
                llm.close()
            return split_result

    # 4. สร้าง metadata summary (LLM สรุปสั้นๆ ครั้งเดียว — สำหรับ automate discovery)
    _generate_metadata_summary(product_id, llm)

    # 4.5 Rebuild raw_text จาก text_extracts ทั้งหมด (รวมของเดิมที่ไม่ได้ re-ingest)
    # ถ้าสินค้ามี scope (เคยแยกจาก catalog) → ใช้ scope เพื่อคัดเฉพาะส่วนของรุ่นนี้
    # ไม่กลับไปรวมทั้ง catalog อีก
    record = product_db.load(product_id)
    scope = record.get("scope")
    if scope and scope.get("source_refs"):
        # มี scope → คัด text จาก text_extracts ตาม source_refs + common_refs
        from .product_segmentation import _slice_refs
        text_by_file = {t.get("file", ""): t.get("text", "") for t in record.get("text_extracts", [])}
        own_text = _slice_refs(text_by_file, scope.get("source_refs", []))
        common_text = _slice_refs(text_by_file, scope.get("common_refs", []))
        record["raw_text"] = "\n".join(t for t in [common_text, own_text] if t).strip()
    else:
        text_parts = [t.get("text", "") for t in record.get("text_extracts", []) if t.get("text")]
        record["raw_text"] = "\n\n".join(text_parts).strip()
    product_db.save(product_id, record)

    # 5. ตั้งสถานะ — ถ้าไม่มีไฟล์ ingested สักไฟล์ → no_usable_data ไม่ใช่ ready
    record = product_db.load(product_id)
    ingested_count = sum(1 for f in record.get("files", []) if f.get("status") == "ingested")
    if ingested_count == 0:
        product_db.set_status(product_id, product_db.STATUS_NO_USABLE)
    else:
        # สร้าง product profile ก่อนเปลี่ยน status เป็น ready
        _generate_product_profile(product_id, llm)
        product_db.set_status(product_id, product_db.STATUS_READY)

    if llm is not None:
        llm.close()

    return {
        "status": "ready" if ingested_count > 0 else "no_usable_data",
        "files_total": len(files),
        "files_ingested": ingested_count,
        "files_skipped": len(skipped),
        "files_unsupported": len(unsupported),
        "errors": errors,
    }


def _generate_metadata_summary(product_id: str, llm: LLMClient | None = None) -> None:
    """สร้าง metadata สรุปสั้นๆ ของสินค้า — สำหรับ automate discovery.

    ใช้ LLM สรุปจาก raw_text ครั้งเดียว (ถูกกว่าบรรยายทุกรูปมาก)
    ถ้าไม่มี LLM หรือไม่มี raw_text → เก็บ metadata พื้นฐานจากไฟล์

    เก็บใน product DB: metadata = {summary, category, file_count, has_images}
    """
    record = product_db.load(product_id)
    raw_text = record.get("raw_text", "")
    file_count = len(record.get("files", []))
    image_count = len(record.get("image_descriptions", []))

    metadata = {
        "summary": "",
        "category": "",
        "file_count": file_count,
        "has_images": image_count > 0,
        "image_count": image_count,
    }

    # category จาก product_profile.json ที่ user/config ระบุชัดเจน (ไม่ใช่การเดา)
    try:
        profile = load_product_profile(product_id) or {}
        profile_category = (profile.get("category") or "").strip()
        if profile_category:
            metadata["category"] = profile_category
    except Exception:
        pass

    # ถ้ามี LLM และมี raw_text → สรุปสั้นๆ
    if llm is not None and raw_text.strip():
        try:
            ing_cfg = _ingestion_cfg()
            raw_len = ing_cfg.get("raw_text_length", 3000)
            messages = [
                {
                    "role": "user",
                    "content": (
                        "สรุปสินค้านี้เป็นภาษาไทย กระชับ ไม่เกิน 100 คำ จากข้อมูลต่อไปนี้:\n"
                        f"{raw_text[:raw_len]}\n\n"
                        "ระบุ: ชื่อสินค้า, ประเภท, ลักษณะเด่น"
                    ),
                }
            ]
            summary = llm.chat(
                messages,
                model=ing_cfg.get("model", "google/gemini-2.5-flash"),
                temperature=ing_cfg.get("temperature", 0.3),
                max_tokens=ing_cfg.get("max_tokens_summary", 512),
                stream=False,
                source="ingestion.metadata_summary",
            )
            metadata["summary"] = summary.strip()
        except Exception:
            pass  # ไม่สำคัญ — metadata พื้นฐานยังเก็บได้

    # ถ้าไม่มี LLM → ใช้ text preview เป็น summary
    if not metadata["summary"] and raw_text:
        ing_cfg = _ingestion_cfg()
        metadata["summary"] = raw_text[:ing_cfg.get("summary_length", 200)].replace("\n", " ")

    record["metadata"] = metadata
    product_db.save(product_id, record)


def check_and_mark_stale(product_id: str) -> bool:
    """ตรวจว่า raw data เปลี่ยนไหม ถ้าเปลี่ยน → ตั้งสถานะ stale.

    เรียกตอน user เปิดดูสินค้า หรือก่อนสั่ง agent
    """
    return product_db.mark_stale_if_changed(product_id)


def get_supported_formats() -> dict[str, list[str]]:
    """ดึงรายการรูปแบบที่รองรับ (สำหรับโชว์ใน UI)."""
    config = _load_config()
    return config.get("supported_formats", {})
