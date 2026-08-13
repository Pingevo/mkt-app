"""Auto-detect data files from data/ and product DB.

หน้าที่หลักตอนนี้:
- scan ไฟล์ดิบใน data/{product_id}/ (สำหรับ UI โชว์รายการไฟล์)
- อ่านสถานะสินค้าจาก product DB (data/{product_id}/product.json)
- ดึงข้อมูลสินค้าสำหรับ agent การตลาด จาก product DB (ไม่ใช่จาก cache/product_spec.txt อีกต่อไป)

การเปลี่ยนแปลงจากเดิม:
- เดิม: detect_data_files() คืน raw/images/competitor/product_spec จาก cache/
- ใหม่: detect_data_files() คืน raw/images จาก data/ + status จาก product DB
- ข้อมูลสำหรับ agent การตลาด → ใช้ product_db.get_agent_context_text() แทน

โครงสร้าง:
  data/{product_id}/          — ข้อมูลดิบจาก user (โยนไฟล์อะไรก็ได้) + product.json (DB)
  cache/{product_id}/         — deliverables ของ product_spec agent (เอกสารสเปคให้ user)
"""

from __future__ import annotations

from pathlib import Path

from . import product_db

TEXT_EXTS = {".txt", ".md", ".pdf", ".xlsx", ".xls", ".docx", ".csv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a"}


def _scan_files(directory: Path) -> dict[str, str | list[str] | None]:
    """Scan a directory for raw data files and images by file type."""
    result: dict[str, str | list[str] | None] = {"raw": None, "images": None, "videos": None, "audios": None}

    if not directory.exists() or not directory.is_dir():
        return result

    raw_files: list[str] = []
    image_files: list[str] = []
    video_files: list[str] = []
    audio_files: list[str] = []

    for item in sorted(directory.iterdir()):
        if not item.is_file() or item.name.startswith(".") or item.name == ".DS_Store":
            continue
        if item.name == "product.json":
            continue  # ไฟล์ DB ไม่นับเป็น raw data
        name_lower = item.name.lower()
        ext = item.suffix.lower()
        # ไม่นับ competitors.* เป็นข้อมูลดิบ (เป็นข้อมูลคู่แข่ง)
        if "competitor" in name_lower:
            continue
        if ext in TEXT_EXTS:
            raw_files.append(str(item))
        elif ext in IMAGE_EXTS:
            image_files.append(str(item))
        elif ext in VIDEO_EXTS:
            video_files.append(str(item))
        elif ext in AUDIO_EXTS:
            audio_files.append(str(item))

    if raw_files:
        result["raw"] = raw_files[0]
    if image_files:
        result["images"] = image_files
    if video_files:
        result["videos"] = video_files
    if audio_files:
        result["audios"] = audio_files

    return result


def detect_data_files(data_dir: str | Path | None = None, product_id: str | None = None) -> dict[str, str | list[str] | None]:
    """Auto-detect data files from data/ directory + status from product DB.

    Args:
        data_dir: Base data directory (default: project_root/data)
        product_id: Product ID for subdirectory (e.g., "product1" for data/product1/)

    Returns:
        dict with keys:
          - "raw" (str): ไฟล์ text แรกที่เจอ
          - "images" (list[str]): รูปภาพทั้งหมด
          - "videos" (list[str]): วิดีโอทั้งหมด
          - "audios" (list[str]): เสียงทั้งหมด
          - "status" (str): สถานะสินค้าจาก DB (empty/no_usable_data/processing/ready/stale)
          - "product_spec" (str|None): path ของ deliverable จาก product_spec agent (ถ้ามี)
          - "competitor" (str|None): path ของ deliverable จาก competitor_analysis agent (ถ้ามี)
    """
    project_root = Path(__file__).resolve().parent.parent
    if data_dir is None:
        data_dir = project_root / "data"

    data_dir = Path(data_dir)

    if product_id:
        data_dir = data_dir / product_id

    if not data_dir.exists() or not data_dir.is_dir():
        return {
            "raw": None, "images": None, "videos": None, "audios": None,
            "status": product_db.STATUS_EMPTY,
            "product_spec": None, "competitor": None,
        }

    # ข้อมูลดิบ: อ่านจาก data/{product_id}/ (หรือ data/{product_id}/raw/ ถ้ามี)
    raw_dir = data_dir / "raw"
    if raw_dir.exists() and raw_dir.is_dir():
        scanned = _scan_files(raw_dir)
    else:
        scanned = _scan_files(data_dir)

    # สถานะสินค้า: อ่านจาก product DB
    if product_id:
        status = product_db.get_status(product_id)
    else:
        status = product_db.STATUS_EMPTY

    # Deliverables จาก product_spec/competitor_analysis agent (เก็บใน cache/)
    product_spec_path = None
    competitor_path = None
    if product_id:
        cache_dir = project_root / "cache" / product_id
        if cache_dir.exists():
            for item in sorted(cache_dir.iterdir()):
                if not item.is_file() or item.name.startswith(".") or item.name == ".DS_Store":
                    continue
                name_lower = item.name.lower()
                ext = item.suffix.lower()
                if "competitor" in name_lower and ext in {".txt", ".md"}:
                    competitor_path = str(item)
                elif "product_spec" in name_lower and ext in {".txt", ".md"}:
                    product_spec_path = str(item)

    return {
        "raw": scanned["raw"],
        "images": scanned["images"],
        "videos": scanned["videos"],
        "audios": scanned["audios"],
        "status": status,
        "product_spec": product_spec_path,  # deliverable สำหรับ user ไม่ใช่ data source
        "competitor": competitor_path,      # deliverable สำหรับ user ไม่ใช่ data source
    }


def get_agent_data(product_id: str) -> str:
    """ดึงข้อมูลสินค้าสำหรับ agent การตลาด จาก product DB.

    นี่คือสิ่งที่แทนการอ่าน cache/product_spec.txt เดิม
    ข้อมูลมาจาก ingestion pipeline ไม่ใช่จาก product_spec agent
    """
    return product_db.get_agent_context_text(product_id)
