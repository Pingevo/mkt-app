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
        import os
        from dotenv import load_dotenv
        load_dotenv()
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return None
        from src.llm_client import LLMClient
        cfg = _load_config()
        tag_cfg = cfg.get("tagging", {})
        return LLMClient(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_model=tag_cfg.get("model", "google/gemini-3.7-flash"),
            timeout=180,
        )
    except Exception:
        return None


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
    """Generate embedding vector for text using OpenRouter embeddings API.

    Uses the model specified in config (embedding.model).
    Returns None if API call fails (graceful degradation).
    Logs to ai_usage with source='asset_library.embedding'.
    """
    import os
    import time as _time
    try:
        from dotenv import load_dotenv
        load_dotenv()
        from .ai_usage import log_ai_usage, log_local_usage, make_entry
    except ImportError:
        try:
            from ai_usage import log_ai_usage, log_local_usage, make_entry  # type: ignore
        except ImportError:
            log_ai_usage = log_local_usage = make_entry = None

    model = config.get("embedding", {}).get("model", "openai/text-embedding-3-small")
    t0 = _time.time()
    try:
        import os
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return None

        import httpx
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "input": text[:8000],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # log usage (Hub + local) — source ของ asset_library
            if make_entry:
                entry = make_entry(
                    provider="openrouter",
                    model=model,
                    operation="embeddings.create",
                    source="asset_library.embedding",
                    duration_ms=int((_time.time() - t0) * 1000),
                )
                usage = data.get("usage")
                if usage:
                    entry["prompt_tokens"] = usage.get("prompt_tokens")
                    entry["total_tokens"] = usage.get("total_tokens")
                entry["raw_usage"] = usage
                log_ai_usage(entry)
                log_local_usage(entry)
            return data["data"][0]["embedding"]
    except Exception as e:
        if make_entry:
            entry = make_entry(
                provider="openrouter",
                model=model,
                operation="embeddings.create",
                source="asset_library.embedding",
                duration_ms=int((_time.time() - t0) * 1000),
                status="error",
                error_message=str(e)[:200],
            )
            log_ai_usage(entry)
            log_local_usage(entry)
        return None


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
            model=tag_cfg.get("model", "google/gemini-3.7-flash"),
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
            model=tag_cfg.get("model", "google/gemini-3.7-flash"),
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
    if (existing and existing.get("hash") == file_hash
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
