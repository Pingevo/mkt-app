"""Asset Library — วัตถุดิบกลางของแบรนด์ที่ agent ค้นและหยิบใช้เองได้.

ไฟล์ที่ใช้ซ้ำข้ามการรัน (โลโก้ รูปพรีเซนเตอร์ เพลง ฯลฯ) — ต่างจาก:
  - product data (data/{product}/) = ของเฉพาะสินค้า
  - brand rules (brand/*.json) = text กฎ

หลักการ (ตามมาตรฐาน DAM และที่ยืนยันกับตลาด 2 รอบ):
  1. AI auto-tagging ตอนอัปโหลด — LLM vision บรรยาย+ติด tag ตาม taxonomy ครั้งเดียว
  2. Hybrid search — filter type/subject ก่อน แล้ว rank ด้วย embedding similarity
  3. Taxonomy กำหนดใน config/assets.yaml (No Hardcode)
  4. Human in the Loop — user แก้ tag/description ได้ใน UI
  5. Agent เลือกเองผ่าน tools (แบบเดียวกับ select_product_auto)

โครงไฟล์:
  brand/assets/           — ไฟล์จริงที่ user อัปโหลด (user space)
  cache/assets/db.json    — asset DB (system space) — catalog + embeddings
  config/assets.yaml      — taxonomy + ค่า config ทั้งหมด
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

import yaml


# ------------------------------------------------------------------
#  Paths & config
# ------------------------------------------------------------------

def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _assets_dir() -> Path:
    return _project_root() / "brand" / "assets"


def _db_path() -> Path:
    return _project_root() / "cache" / "assets" / "db.json"


def _load_config() -> dict[str, Any]:
    path = _project_root() / "config" / "assets.yaml"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def make_llm():
    """สร้าง LLM client สำหรับ asset tagging — ดึง model จาก config (No Hardcode).

    คืน LLMClient หรือ None ถ้าไม่มี API key.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
        from src.openrouter_gateway import get_api_key as _gate_get_api_key
        if not _gate_get_api_key():
            return None
        from src.llm_client import LLMClient
        cfg = _load_config()
        tag_cfg = cfg.get("tagging", {})
        return LLMClient(
            base_url="https://openrouter.ai/api/v1",
            default_model=tag_cfg.get("model", "google/gemini-3.8-flash"),
            timeout=180,
        )
    except Exception:
        return None


# ------------------------------------------------------------------
#  Tool calling — schemas + handlers สำหรับ LLM tool calling
#  ใช้ใน orchestrator.select_product_auto + _select_assets_for_content
# ------------------------------------------------------------------

def tool_definitions() -> list[dict]:
    """Tool schemas สำหรับ list_assets + get_asset_detail (OpenAI function schema).

    ใช้ร่วมกันทุกที่ที่ให้ LLM เรียกดู asset ผ่าน tool calling.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "list_assets",
                "description": "ดูวัตถุดิบแบรนด์ที่มี (โลโก้ รูปพรีเซนเตอร์ เพลง ฯลฯ) — ค้นหาด้วยคำหรือกรองตามประเภท เพื่อเลือกใช้ประกอบคอนเทนต์",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "คำค้น (optional) — เช่น 'โลโก้', 'พรีเซนเตอร์', 'คน'",
                        },
                        "type": {
                            "type": "string",
                            "description": "กรองประเภท (optional): image, audio, video, text, other",
                        },
                        "subject": {
                            "type": "string",
                            "description": "กรอง subject (optional): person, product, logo, background, graphic, scene, other",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_asset_detail",
                "description": "ดูรายละเอียดเต็มของ asset หนึ่ง — เรียกหลังจาก list_assets แล้วเลือก asset ที่อยากดู",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "ID ของ asset (เช่น a_0001) ที่ได้จาก list_assets",
                        },
                    },
                    "required": ["asset_id"],
                },
            },
        },
    ]


def tool_handlers() -> dict:
    """Tool handlers สำหรับ list_assets + get_asset_detail.

    คืน dict ของ {tool_name: callable} พร้อมใช้ใน chat_with_tools.
    ไม่ส่ง path/hash ออกไป — LLM ไม่ต้องใช้ค่าเหล่านั้น.
    """
    def _list_assets(query: str = "", type: str = "", subject: str = "") -> list[dict]:
        results = query_assets(query, type=type or None, subject=subject or None)
        return [
            {
                "id": a.get("id"),
                "file": a.get("file"),
                "type": a.get("type"),
                "subject": a.get("subject"),
                "style": a.get("style"),
                "tags": a.get("tags", []),
                "description": a.get("description", ""),
            }
            for a in results
        ]

    def _get_asset_detail(asset_id: str) -> dict:
        rec = get_asset(asset_id)
        if not rec:
            return {"error": f"ไม่พบ asset {asset_id}"}
        return {k: v for k, v in rec.items() if k not in ("path", "hash")}

    return {
        "list_assets": _list_assets,
        "get_asset_detail": _get_asset_detail,
    }


# ------------------------------------------------------------------
#  File classification & hash
# ------------------------------------------------------------------

def _classify_file(filename: str, config: dict) -> str | None:
    """จัดประเภทไฟล์ คืน 'image'/'audio'/'video'/'text'/'other' หรือ None ถ้าไม่รู้จัก."""
    ext = Path(filename).suffix.lower()
    formats = config.get("supported_formats", {})
    for ftype, exts in formats.items():
        if ext in (exts or []):
            return ftype
    # ถ้าไม่ตรง format ใด → 'other' (รับไฟล์อะไรก็ได้ ตามขอบเขตที่ user ตกลง)
    return "other" if ext else None


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def _check_size(path: Path, ftype: str, config: dict) -> tuple[bool, str]:
    max_mb = config.get("max_file_size_mb", {}).get(ftype, 100)
    size = _file_size_mb(path)
    if size > max_mb:
        return False, f"ไฟล์ใหญ่เกินไป ({size:.1f}MB > {max_mb}MB)"
    return True, ""


# ------------------------------------------------------------------
#  DB load/save
# ------------------------------------------------------------------

def _empty_db() -> dict[str, Any]:
    return {"assets": [], "next_id": 1}


def _load_db() -> dict[str, Any]:
    path = _db_path()
    if not path.exists():
        return _empty_db()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "assets" not in data:
            return _empty_db()
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_db()


def _save_db(db: dict[str, Any]) -> None:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def _find_by_path(db: dict, abs_path: str) -> dict | None:
    for a in db.get("assets", []):
        if a.get("path") == abs_path:
            return a
    return None


def _find_by_id(db: dict, asset_id: str) -> dict | None:
    for a in db.get("assets", []):
        if a.get("id") == asset_id:
            return a
    return None


def _strip_embedding(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "embedding"}


# ------------------------------------------------------------------
#  Embedding — injectable (true external dependency)
# ------------------------------------------------------------------

# default embedder — ใช้ OpenRouter embeddings API แบบเดียวกับ content_history
# log ด้วย source ของ asset_library เพื่อแยกจาก content_history ใน logs/llm_usage.jsonl
def _default_embedder(text: str, config: dict) -> list[float] | None:
    """Generate embedding vector for text using the canonical embedding seam.

    Uses the model specified in config (embedding.model).
    Returns None if API call fails (graceful degradation).
    Accounting is owned by the canonical seam (llm_client.generate_embedding).
    """
    try:
        from .llm_client import generate_embedding
    except ImportError:
        from llm_client import generate_embedding  # type: ignore

    model = config.get("embedding", {}).get("model", "openai/text-embedding-3-small")
    return generate_embedding(text, model=model, source="asset_library.embedding")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ------------------------------------------------------------------
#  LLM tagging — injectable (true external dependency)
# ------------------------------------------------------------------

def _default_tagger(file_path: Path, ftype: str, config: dict, llm) -> dict:
    """LLM vision บรรยายรูป + ติด tag ตาม taxonomy.

    คืน: {subject, style, tags, description}
    ถ้าไม่มี llm หรือ error → คืนค่าว่าง (graceful — ยังเก็บ path ได้)
    """
    empty = {"subject": "other", "style": "other", "tags": [], "description": ""}
    if llm is None:
        return empty
    if ftype != "image":
        # ไม่ใช่รูป → LLM ไม่ช่วย tag (text มี parser ของตัวเอง, other ใช้ชื่อไฟล์)
        return empty

    import base64
    try:
        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError:
        return empty

    taxonomy = config.get("taxonomy", {})
    subjects = taxonomy.get("subject", ["other"])
    styles = taxonomy.get("style", ["other"])
    tag_cfg = config.get("tagging", {})
    max_words = tag_cfg.get("description_max_words", 60)

    # ไม่ใช้ response_format (json_schema) เพราะบาง model ตัด response ผิด
    # ใช้ prompt บอก format แล้ว parse JSON เอง — แบบเดียวกับ select_product_auto
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"บรรยายรูปนี้เป็นภาษาไทย กระชับไม่เกิน {max_words} คำ "
                        f"และติด tag ตอบเป็น JSON เท่านั้น (ไม่มี markdown block)\n"
                        f'format: {{"subject": "ค่าจาก list", "style": "ค่าจาก list", "tags": ["tag1","tag2"], "description": "คำบรรยาย"}}\n'
                        f"subject ต้องเป็นหนึ่งใน: {', '.join(subjects)}\n"
                        f"style ต้องเป็นหนึ่งใน: {', '.join(styles)}\n"
                        f"tags: คำสำคัญ 3-8 คำ"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ],
        }
    ]

    try:
        resp = llm.chat(
            messages,
            model=tag_cfg.get("model", "google/gemini-3.8-flash"),
            temperature=tag_cfg.get("temperature", 0.2),
            max_tokens=tag_cfg.get("max_tokens", 1024),
            stream=False,
            source="asset_library.tag",
        )
        # parse JSON — strip markdown code blocks ถ้ามี (แบบเดียวกับ select_product_auto)
        text = resp.strip() if isinstance(resp, str) else str(resp)
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        parsed = json.loads(text)
        return {
            "subject": parsed.get("subject", "other"),
            "style": parsed.get("style", "other"),
            "tags": parsed.get("tags", [])[:10],
            "description": parsed.get("description", "")[:max_words * 6],
        }
    except Exception:
        return empty


# ------------------------------------------------------------------
#  Public API — ingest side
# ------------------------------------------------------------------

def _summarize_text(file_path: Path, config: dict, llm) -> dict:
    """parse text + LLM summarize — สำหรับไฟล์ text (txt/md/pdf).

    ถ้าไม่มี llm → fallback เป็น truncate 300 ตัว (graceful).
    คืน: {subject, style, tags, description}
    """
    empty = {"subject": "other", "style": "other", "tags": [], "description": ""}
    try:
        from .file_loader import load_file
        text = load_file(str(file_path))
    except Exception:
        return {**empty, "description": file_path.stem}

    if not text.strip():
        return {**empty, "description": file_path.stem}

    # ถ้าไม่มี llm → truncate (graceful — เหมือนเดิม)
    if llm is None:
        return {**empty, "description": text[:300].replace("\n", " ").strip() or file_path.stem}

    tag_cfg = config.get("tagging", {})
    max_words = tag_cfg.get("description_max_words", 60)
    taxonomy = config.get("taxonomy", {})
    subjects = taxonomy.get("subject", ["other"])
    styles = taxonomy.get("style", ["other"])

    messages = [
        {
            "role": "user",
            "content": (
                f"สรุปเนื้อหา text นี้เป็นภาษาไทย กระชับไม่เกิน {max_words} คำ "
                f"และติด tag ตอบเป็น JSON เท่านั้น (ไม่มี markdown block)\n"
                f'format: {{"subject": "ค่าจาก list", "style": "ค่าจาก list", "tags": ["tag1","tag2"], "description": "สรุปเนื้อหา"}}\n'
                f"subject ต้องเป็นหนึ่งใน: {', '.join(subjects)}\n"
                f"style ต้องเป็นหนึ่งใน: {', '.join(styles)}\n"
                f"tags: คำสำคัญ 3-8 คำ\n\n"
                f"เนื้อหา:\n{text[:4000]}"
            ),
        }
    ]

    try:
        resp = llm.chat(
            messages,
            model=tag_cfg.get("model", "google/gemini-3.8-flash"),
            temperature=tag_cfg.get("temperature", 0.2),
            max_tokens=tag_cfg.get("max_tokens", 1024),
            stream=False,
            source="asset_library.tag",
        )
        text_resp = resp.strip() if isinstance(resp, str) else str(resp)
        if text_resp.startswith("```"):
            lines = text_resp.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text_resp = "\n".join(lines)
        parsed = json.loads(text_resp)
        return {
            "subject": parsed.get("subject", "other"),
            "style": parsed.get("style", "other"),
            "tags": parsed.get("tags", [])[:10],
            "description": parsed.get("description", "")[:max_words * 6],
        }
    except Exception:
        # fallback truncate ถ้า LLM fail
        return {**empty, "description": text[:300].replace("\n", " ").strip() or file_path.stem}


def ingest_asset(
    file_path: str | Path,
    llm=None,
    user_note: str = "",
    *,
    force: bool = False,
    tagger: Callable | None = None,
    embedder: Callable | None = None,
    config: dict | None = None,
) -> dict[str, Any]:
    """Ingest ไฟล์เข้า asset library — auto-tag + embed ครั้งเดียว.

    Args:
        file_path: path ของไฟล์ที่จะ ingest (ควรอยู่ใน brand/assets/ แล้ว)
        llm: LLM client (injectable — tests ส่ง mock)
        user_note: หมายเหตุจาก user (HITL — เพิ่มเข้า description)
        tagger: override tag function (tests)
        embedder: override embed function (tests)
        config: override config (tests)

    Returns:
        asset record (ไม่มี embedding ในผลลัพธ์)
    """
    cfg = config or _load_config()
    tag_fn = tagger or _default_tagger
    embed_fn = embedder or _default_embedder

    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return {"id": "", "path": str(p), "status": "error", "error": "ไฟล์ไม่มี"}

    ftype = _classify_file(p.name, cfg)
    if ftype is None:
        return {"id": "", "path": str(p), "status": "error", "error": "ไม่รู้จักประเภทไฟล์"}

    # ตรวจขนาด
    ok, size_msg = _check_size(p, ftype, cfg)
    if not ok:
        return {"id": "", "path": str(p), "type": ftype, "status": "error", "error": size_msg}

    abs_path = str(p.resolve())
    file_hash = _file_hash(p)

    db = _load_db()

    # ถ้าไฟล์เดิม hash ไม่เปลี่ยน AND user_note ไม่เปลี่ยน → ข้าม (pattern เดียวกับ ingestion.py)
    existing = _find_by_path(db, abs_path)
    if (not force and existing and existing.get("hash") == file_hash
            and existing.get("status") == "ready"
            and (existing.get("user_note", "") or "") == user_note.strip()):
        return _strip_embedding(existing)

    # tag
    if ftype == "image":
        tagged = tag_fn(p, ftype, cfg, llm)
    elif ftype == "text":
        # parse text + LLM summarize (ตาม spec §3.1)
        tagged = _summarize_text(p, cfg, llm)
    else:
        # audio/video/other — LLM ไม่ช่วย → ใช้ชื่อไฟล์ + user_note
        tagged = {
            "subject": "other",
            "style": "other",
            "tags": [],
            "description": user_note.strip() or p.stem,
        }

    # รวม user_note เข้า description ถ้ามี
    description = tagged["description"]
    if user_note.strip() and user_note.strip() not in description:
        description = f"{description} | note: {user_note.strip()}".strip(" |")

    # embedding ของ description + tags
    embed_text = f"{description} {' '.join(tagged['tags'])}".strip()
    embedding = embed_fn(embed_text, cfg) if embed_text else None

    # สร้าง/อัปเดต record
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if existing:
        asset_id = existing["id"]
        record = existing
    else:
        asset_id = f"a_{db.get('next_id', 1):04d}"
        db["next_id"] = db.get("next_id", 1) + 1
        record = {"id": asset_id, "created_at": now}

    record.update({
        "file": p.name,
        "path": abs_path,
        "hash": file_hash,
        "type": ftype,
        "subject": tagged["subject"],
        "style": tagged["style"],
        "tags": tagged["tags"],
        "description": description,
        "user_note": user_note.strip(),
        "embedding": embedding,
        "status": "ready",
        "error": "",
        "updated_at": now,
    })

    # บันทึก
    if existing:
        db["assets"] = [record if a.get("id") == asset_id else a for a in db["assets"]]
    else:
        db["assets"].append(record)
    _save_db(db)

    return _strip_embedding(record)


def ingest_all(
    llm=None,
    *,
    force: bool = False,
    tagger: Callable | None = None,
    embedder: Callable | None = None,
    config: dict | None = None,
) -> dict[str, Any]:
    """สแกน brand/assets/ ทั้งโฟลเดอร์ → ingest ไฟล์ที่ยังไม่มี/เปลี่ยน.

    รองรับ user โยนไฟล์ตรงเข้าโฟลเดอร์เอง (ไม่ผ่าน API upload) — สแกนใหม่ได้.

    Returns: {total, ingested, skipped, errors: [...]}
    """
    cfg = config or _load_config()
    assets_dir = _assets_dir()
    if not assets_dir.exists():
        return {"total": 0, "ingested": 0, "skipped": 0, "errors": []}

    db = _load_db()
    existing_hashes = {a.get("path"): a.get("hash") for a in db.get("assets", [])}

    files = [f for f in sorted(assets_dir.iterdir()) if f.is_file() and not f.name.startswith(".")]
    ingested = 0
    skipped = 0
    errors = []

    for f in files:
        abs_path = str(f.resolve())
        fhash = _file_hash(f)
        if not force and existing_hashes.get(abs_path) == fhash:
            # ข้ามถ้า hash เหมือนและ status=ready
            rec = _find_by_path(db, abs_path)
            if rec and rec.get("status") == "ready":
                skipped += 1
                continue

        result = ingest_asset(f, llm=llm, tagger=tagger, embedder=embedder, config=cfg)
        if result.get("status") == "ready":
            ingested += 1
        else:
            errors.append(f"{f.name}: {result.get('error', 'unknown')}")

    return {"total": len(files), "ingested": ingested, "skipped": skipped, "errors": errors}


# ------------------------------------------------------------------
#  Public API — query side
# ------------------------------------------------------------------

def query_assets(
    query: str = "",
    *,
    type: str | None = None,
    subject: str | None = None,
    top_k: int | None = None,
    embedder: Callable | None = None,
    config: dict | None = None,
) -> list[dict[str, Any]]:
    """Hybrid search — filter type/subject ก่อน แล้ว rank ด้วย embedding similarity.

    Args:
        query: คำค้น (natural language) — ถ้าว่าง → คืนตาม filter เรียงใหม่→เก่า
        type: กรองประเภท (image/audio/video/text/other)
        subject: กรอง subject ตาม taxonomy
        top_k: จำนวนผลลัพธ์ (default จาก config)
        embedder: override embed function (tests)
        config: override config (tests)

    Returns:
        list ของ asset record (ไม่มี embedding) — เรียงตามความเกี่ยวข้อง
    """
    cfg = config or _load_config()
    db = _load_db()
    assets = db.get("assets", [])

    # filter
    filtered = [a for a in assets if a.get("status") == "ready"]
    if type:
        filtered = [a for a in filtered if a.get("type") == type]
    if subject:
        filtered = [a for a in filtered if a.get("subject") == subject]

    # ถ้าไม่มี query → เรียงใหม่→เก่า
    if not query.strip():
        filtered.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        top = cfg.get("query", {}).get("default_top_k", 8)
        k = top_k or top
        return [_strip_embedding(a) for a in filtered[:k]]

    # semantic rank
    embed_fn = embedder or _default_embedder
    q_vec = embed_fn(query, cfg)
    if q_vec is None:
        # graceful — ถ้า embed ไม่ได้ คืนตาม filter อย่างเดียว
        filtered.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        top = cfg.get("query", {}).get("default_top_k", 8)
        k = top_k or top
        return [_strip_embedding(a) for a in filtered[:k]]

    scored = []
    for a in filtered:
        a_vec = a.get("embedding")
        sim = _cosine_similarity(q_vec, a_vec) if a_vec else 0.0
        scored.append((sim, a))
    scored.sort(key=lambda x: x[0], reverse=True)

    top = cfg.get("query", {}).get("default_top_k", 8)
    max_k = cfg.get("query", {}).get("max_top_k", 20)
    k = min(top_k or top, max_k)
    return [_strip_embedding(a) for _, a in scored[:k]]


def get_asset(asset_id: str) -> dict[str, Any] | None:
    """คืน record เต็ม (ไม่มี embedding) หรือ None ถ้าไม่มี."""
    db = _load_db()
    rec = _find_by_id(db, asset_id)
    return _strip_embedding(rec) if rec else None


def get_asset_paths(asset_ids: list[str]) -> list[str]:
    """id → absolute path ที่ไฟล์มีจริง — สำหรับส่งเป็น input_references ของ media_gen.

    ข้าม id ที่ไม่รู้จักหรือไฟล์หาย — กรองเฉพาะ type=image (media_gen ใช้ non-image ไม่ได้)
    """
    db = _load_db()
    paths = []
    for aid in asset_ids:
        rec = _find_by_id(db, aid)
        if not rec:
            continue
        if rec.get("type") != "image":
            continue
        p = Path(rec.get("path", ""))
        if p.exists():
            paths.append(str(p))
    return paths


def build_input_references(
    product_paths: list[str],
    asset_ids: list[str],
    resource_paths: list[str] | None = None,
) -> list[str]:
    """รวมรูปสินค้า + รูปแนบจาก quick brief/run context + รูป asset เป็น list[str] เรียงลำดับ.

    Compatibility helper — คืน path ของทุก reference ตามลำดับเดียวกับ build_reference_catalog.
    **ผู้เรียกต้อง preflight ก่อน** (เรียก preflight_references บน catalog ก่อน helper นี้)
    เพื่อจับ missing file / เกิน limit ก่อนยิง provider — helper นี้ไม่ preflight เอง.

    resource_paths ถูกเก็บแยกจาก asset_ids — ห้ามใช้ res_... แทน asset-library IDs.
    ลำดับ: product → user_resource → asset_library (เดียวกับ catalog ที่ Agent 4 เห็น).
    """
    catalog = build_reference_catalog(product_paths, asset_ids, resource_paths)
    return [r["path"] for r in catalog if r.get("path")]


def build_reference_catalog(
    product_paths: list[str],
    asset_ids: list[str],
    resource_paths: list[str] | None = None,
) -> list[dict]:
    """สร้าง ordered reference catalog — mechanical fields เท่านั้น ไม่ตีความความหมายของรูป.

    แต่ละ entry มีเฉพาะข้อมูลที่ runtime รู้อยู่แล้ว:
      - ordinal: ลำดับที่ (1-based) — "Reference 1", "Reference 2", ...
      - label: "Reference N" (stable identifier สำหรับ Agent 4 และ provider)
      - provenance: "product_db" | "user_resource" | "asset_library" (source เพื่อ traceability)
      - path: path จริงของไฟล์
      - asset_id: asset library ID (ว่างถ้าไม่ใช่ asset)
      - filename: ชื่อไฟล์
      - metadata: Asset Library metadata ที่มี (subject/tags/description/type/user_note) —
        pass-through ไม่ตีความ; ไม่มีก็เป็น {}

    ลำดับคงที่: product → user_resource → asset_library.
    ไม่ truncate ไม่ select — ถ้าเกิน limit ให้ preflight_references จับ.
    """
    entries: list[dict] = []

    def _add(path: str, provenance: str, asset_id: str = "", metadata: dict | None = None):
        entries.append({
            "ordinal": len(entries) + 1,
            "label": f"Reference {len(entries) + 1}",
            "provenance": provenance,
            "path": str(path),
            "asset_id": asset_id,
            "filename": Path(path).name if path else "",
            "metadata": metadata or {},
        })

    # Product images — รูปสินค้าจาก product_db
    for p in product_paths:
        _add(str(p), "product_db")

    # User resources (Quick Brief) — รูปที่ user แนบมาเอง
    if resource_paths:
        for p in resource_paths:
            _add(str(p), "user_resource")

    # Asset Library — รูปจาก asset library ที่เลือก (เรียงตาม asset_ids order)
    # ถ้าไฟล์ missing/unreadable ก็ยังใส่ entry ไว้ — preflight_references จับได้
    # ห้าม drop เงียบๆ เพราะจะทำให้เลข Reference เลื่อน (renumbering)
    if asset_ids:
        for aid in asset_ids:
            rec = get_asset(aid)
            if not rec or rec.get("type") != "image":
                continue
            p = rec.get("path", "")
            meta = {
                k: rec.get(k)
                for k in ("subject", "tags", "description", "type", "user_note")
                if rec.get(k) not in (None, "", [])
            }
            _add(str(p), "asset_library", asset_id=aid, metadata=meta)

    return entries


def preflight_references(catalog: list[dict]) -> str | None:
    """ตรวจ catalog ก่อนยิง provider — คืน error message หรือ None ถ้าผ่าน.

    ตรวจเฉพาะสิ่งที่รู้เชิงกลไกได้:
      - ไฟล์ missing/unreadable → error ระบุเลข Reference + path (ไม่ renumber ลำดับ)
      - จำนวนเกิน max_refs_per_post → error (ไม่ truncate ไม่ select)

    ไม่ตัดสินใจแทน user ว่าจะเก็บรูปไหน — ถ้าเกิน limit ให้ user แก้เอง.
    """
    if not catalog:
        return None
    for ref in catalog:
        p = ref.get("path", "")
        if not p or not Path(p).exists():
            return f"{ref.get('label', 'Reference ?')} ({p or 'no path'}) is missing or unreadable."
    cfg = _load_config()
    max_refs = cfg.get("media", {}).get("max_refs_per_post", 5)
    if len(catalog) > max_refs:
        return (f"{len(catalog)} references exceed provider limit "
                f"(max_refs_per_post={max_refs}). Remove some references or increase the limit.")
    return None


def build_reference_remap(
    full_catalog: list[dict],
    item_asset_ids: list[str],
    product_paths: list[str],
    resource_paths: list[str] | None = None,
) -> tuple[list[dict], dict[int, int]]:
    """Build per-item catalog and a mapping from full-catalog ordinals to per-item ordinals.

    Agent 4 sees the FULL catalog (all selected assets) with Reference 1..N.
    Each media item may select only a subset of assets (item_asset_ids).
    The per-item catalog has fewer entries → Reference numbers shift.

    This function mechanically maps old→new ordinals by matching file paths
    (not by interpreting what a reference means).

    Returns (item_catalog, remap) where remap[old_ordinal] = new_ordinal.
    Entries not in the per-item catalog are absent from remap (caller can detect).
    """
    item_catalog = build_reference_catalog(product_paths, item_asset_ids, resource_paths)
    remap: dict[int, int] = {}
    for item_entry in item_catalog:
        item_path = item_entry.get("path", "")
        for full_entry in full_catalog:
            if full_entry.get("path", "") == item_path:
                remap[full_entry["ordinal"]] = item_entry["ordinal"]
                break
    return item_catalog, remap


def remap_reference_numbers(prompt: str, remap: dict[int, int]) -> str:
    """Replace 'Reference N' in prompt with remapped number.

    Mechanical text replacement — does not interpret what a reference means.
    Caller must run preflight_reference_mentions first to reject dangling refs.
    """
    import re

    def _replace(match: "re.Match[str]") -> str:
        old_num = int(match.group(1))
        new_num = remap.get(old_num, old_num)
        return f"Reference {new_num}"

    return re.sub(r"Reference (\d+)", _replace, prompt)


def preflight_reference_mentions(
    prompt: str,
    full_catalog: list[dict],
    remap: dict[int, int] | None,
) -> str | None:
    """Reject prompts that reference an ordinal not available for this media item.

    For every 'Reference N' in the prompt:
      - If N is not in the full catalog at all → unknown reference.
      - If N is in the full catalog but absent from remap (the reference was
        excluded from this media item's provider input) → dangling reference.
      - Return a visible error naming the invalid Reference number(s).
      - Do not silently retain the old number.
      - Do not substitute another reference.

    Mechanical text parsing only — does not interpret what a reference means.
    Returns None if the prompt has no Reference mentions, or if every
    mentioned ordinal is present in the per-item subset (or in the full
    catalog when no subset remap is active).
    """
    import re

    mentions = re.findall(r"Reference (\d+)", prompt)
    if not mentions:
        return None
    full_ordinals = {e["ordinal"] for e in full_catalog}
    # When remap is None (no subset selection), all full-catalog refs are present.
    # When remap is active, only ordinals in remap are available to this item.
    available = full_ordinals if remap is None else set(remap.keys())
    invalid: list[int] = []
    for num_str in mentions:
        num = int(num_str)
        if num not in available:
            if num not in invalid:
                invalid.append(num)
    if invalid:
        invalid.sort()
        names = ", ".join(f"Reference {n}" for n in invalid)
        if remap is None:
            reason = "does not exist in the reference catalog"
        else:
            reason = (
                "was excluded from this media item's reference set "
                "or does not exist in the full catalog"
            )
        return (
            f"Prompt mentions {names} but {reason}. Cannot generate with "
            f"invalid reference numbers."
        )
    return None


def extract_reference_ordinals(prompt: str) -> list[int]:
    """Extract 'Reference N' ordinals from a media prompt.

    Mechanical text parsing — same regex as preflight_reference_mentions.
    Returns sorted unique list of ordinals mentioned in the prompt.
    Empty list if no references mentioned.
    """
    import re
    mentions = re.findall(r"Reference (\d+)", prompt)
    return sorted(set(int(n) for n in mentions))


def filter_catalog_by_ordinals(
    full_catalog: list[dict],
    selected_ordinals: list[int],
) -> tuple[list[dict], dict[int, int]]:
    """Filter a reference catalog to only selected ordinals.

    Mechanical: matches ordinals, re-numbers sequentially from 1.
    Returns (filtered_catalog, remap) where remap[old_ordinal] = new_ordinal.
    Entries not in selected_ordinals are excluded — no semantic interpretation.
    Does not mutate the input catalog.
    """
    selected_set = set(selected_ordinals)
    filtered: list[dict] = []
    remap: dict[int, int] = {}
    new_idx = 0
    for entry in full_catalog:
        if entry["ordinal"] in selected_set:
            new_idx += 1
            new_entry = dict(entry)
            new_entry["ordinal"] = new_idx
            new_entry["label"] = f"Reference {new_idx}"
            filtered.append(new_entry)
            remap[entry["ordinal"]] = new_idx
    return filtered, remap


# ------------------------------------------------------------------
#  Public API — HITL side
# ------------------------------------------------------------------

def update_asset(
    asset_id: str,
    *,
    tags: list[str] | None = None,
    description: str | None = None,
    subject: str | None = None,
    style: str | None = None,
    user_note: str | None = None,
    embedder: Callable | None = None,
    config: dict | None = None,
) -> dict[str, Any] | None:
    """HITL — user แก้ metadata → re-embed → save.

    คืน record ที่อัปเดตแล้ว (ไม่มี embedding) หรือ None ถ้าไม่มี id นั้น.
    """
    cfg = config or _load_config()
    embed_fn = embedder or _default_embedder
    db = _load_db()
    rec = _find_by_id(db, asset_id)
    if not rec:
        return None

    if tags is not None:
        rec["tags"] = tags[:10]
    if description is not None:
        rec["description"] = description
    if subject is not None:
        rec["subject"] = subject
    if style is not None:
        rec["style"] = style
    if user_note is not None:
        rec["user_note"] = user_note

    # re-embed
    embed_text = f"{rec.get('description', '')} {' '.join(rec.get('tags', []))}".strip()
    rec["embedding"] = embed_fn(embed_text, cfg) if embed_text else None
    rec["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    db["assets"] = [rec if a.get("id") == asset_id else a for a in db["assets"]]
    _save_db(db)
    return _strip_embedding(rec)


def delete_asset(asset_id: str, remove_file: bool = True) -> bool:
    """ลบ asset จาก DB + ลบไฟล์จริง (ถ้า remove_file=True). คืน True ถ้าลบสำเร็จ."""
    db = _load_db()
    rec = _find_by_id(db, asset_id)
    if not rec:
        return False
    if remove_file:
        p = Path(rec.get("path", ""))
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    db["assets"] = [a for a in db["assets"] if a.get("id") != asset_id]
    _save_db(db)
    return True


def list_all() -> list[dict[str, Any]]:
    """คืน asset ทั้งหมด (ไม่มี embedding) — สำหรับ UI."""
    db = _load_db()
    return [_strip_embedding(a) for a in db.get("assets", [])]
