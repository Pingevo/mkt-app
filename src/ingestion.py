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

import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from . import product_db
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
                return llm.chat(
                    messages,
                    model="google/gemini-2.5-flash",
                    temperature=0.3,
                    max_tokens=1024,
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

def _make_llm() -> LLMClient | None:
    """สร้าง LLM client สำหรับ ingestion (ใช้สำหรับ image/video/field extraction)."""
    from .config_loader import get_env
    api_key = get_env("OPENROUTER_API_KEY")
    if not api_key:
        return None
    return LLMClient(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_model="google/gemini-2.5-flash",
        timeout=180,
    )


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


def ingest_product(product_id: str, force: bool = False) -> dict[str, Any]:
    """รัน ingestion pipeline สำหรับสินค้านี้.

    Args:
        product_id: ชื่อสินค้า
        force: ถ้า True → re-ingest ทุกไฟล์แม้ hash เหมือนเดิม

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

    # เคลียร์ image_descriptions เดิมก่อน re-ingest (กัน duplicate)
    record = product_db.load(product_id)
    record["image_descriptions"] = []
    record["raw_text"] = ""
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

        # ตรวจขนาด
        ok, size_msg = _check_size(file_path, ftype, config)
        if not ok:
            product_db.update_file_status(product_id, f["path"], "error", size_msg)
            errors.append(f"{f['name']}: {size_msg}")
            continue

        # เรียก preprocessor
        try:
            preprocessor = PREPROCESSORS.get(ftype)
            if preprocessor is None:
                product_db.update_file_status(product_id, f["path"], "error", f"ไม่มี preprocessor สำหรับ {ftype}")
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
                # text รวมเก็บใน raw_text
                record = product_db.load(product_id)
                record["raw_text"] = (record.get("raw_text", "") + "\n\n" + extracted_text).strip()
                product_db.save(product_id, record)

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
            product_db.update_file_status(product_id, f["path"], "error", str(e))
            errors.append(f"{f['name']}: {e}")

    # 4. สร้าง metadata summary (LLM สรุปสั้นๆ ครั้งเดียว — สำหรับ automate discovery)
    _generate_metadata_summary(product_id, llm)

    # 5. ตั้งสถานะ ready
    product_db.set_status(product_id, product_db.STATUS_READY)

    if llm is not None:
        llm.close()

    return {
        "status": "ready",
        "files_total": len(files),
        "files_ingested": len(to_ingest),
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

    # ถ้ามี LLM และมี raw_text → สรุปสั้นๆ
    if llm is not None and raw_text.strip():
        try:
            messages = [
                {
                    "role": "user",
                    "content": (
                        "สรุปสินค้านี้เป็นภาษาไทย กระชับ ไม่เกิน 100 คำ จากข้อมูลต่อไปนี้:\n"
                        f"{raw_text[:3000]}\n\n"
                        "ระบุ: ชื่อสินค้า, ประเภท, ลักษณะเด่น"
                    ),
                }
            ]
            summary = llm.chat(
                messages,
                model="google/gemini-2.5-flash",
                temperature=0.3,
                max_tokens=512,
                stream=False,
                source="ingestion.metadata_summary",
            )
            metadata["summary"] = summary.strip()
        except Exception:
            pass  # ไม่สำคัญ — metadata พื้นฐานยังเก็บได้

    # ถ้าไม่มี LLM → ใช้ text preview เป็น summary
    if not metadata["summary"] and raw_text:
        metadata["summary"] = raw_text[:200].replace("\n", " ")

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
