# -*- coding: utf-8 -*-
"""Web Viewer — Drag & drop product folders into agent boxes.

Run:
  python3 web_viewer.py
  → http://localhost:8778
"""

from __future__ import annotations

import asyncio
import json
import os
import queue as _queue
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse, Response
import uvicorn

from src.orchestrator import Orchestrator
from src.data_loader import detect_data_files
from src.file_loader import load_file
from src.config_loader import get_env
from src.brand_loader import load_brand_visual
from src import media_gen
from src import product_db
from src import content_history
from src.flow_context import set_flow_id, clear_flow_id
from src.cost_summary import write_cost_summary, write_flow_meta, find_flow_id_for_file, find_any_flow_id, update_cost_summary


def _sys_cfg() -> dict:
    """อ่าน system section จาก config — fallback {} ถ้าโหลดไม่ได้ (lazy, กัน circular import)."""
    try:
        from src.config_loader import load_config, get_section
        return get_section(load_config(), "system", {})
    except Exception:
        return {}

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
BRAND_DIR = PROJECT_ROOT / "brand"
CACHE_DIR = PROJECT_ROOT / "cache"

app = FastAPI(title="MKTApp Viewer")

# Central conflict cache — ตรวจครั้งเดียวตอน brand/agent settings เปลี่ยน
# ทุก UI ดึงจาก GET /api/conflicts แทนการตรวจใหม่ทุกครั้ง
_conflict_cache: dict[str, list] = {}  # { agent_key: [conflict_dict, ...] }


def _refresh_conflict_cache() -> None:
    """ตรวจ conflict ทุก agent → เก็บใน _conflict_cache.

    เรียกหลัง: brand save, agent settings save, server start
    ไม่ตรวจ Quick Brief (per-run, ไม่ได้ save)
    """
    from src.brand_priority import load_brand_priority, detect_conflicts
    rules = load_brand_priority(BRAND_DIR)
    data = _load_instructions()
    cache: dict[str, list] = {}
    for agent_key in ("product_spec", "competitor_analysis", "campaign_strategy", "content_creator"):
        settings = data.get(agent_key, {})
        conflicts = detect_conflicts(rules, settings)
        cache[agent_key] = [
            {
                "field": c.field,
                "brand_value": c.brand_value,
                "user_value": c.user_value,
                "severity": c.severity,
            }
            for c in conflicts
        ]
    global _conflict_cache
    _conflict_cache = cache


def _get_brand_visual() -> dict:
    """โหลด brand/visual.json — cached ที่ module level เพื่อไม่อ่านซ้ำทุก request."""
    global _BRAND_VISUAL_CACHE
    if _BRAND_VISUAL_CACHE is None:
        try:
            _BRAND_VISUAL_CACHE = load_brand_visual(BRAND_DIR)
        except Exception:
            _BRAND_VISUAL_CACHE = {}
    return _BRAND_VISUAL_CACHE


_BRAND_VISUAL_CACHE: dict | None = None

_orch: Orchestrator | None = None  # only used by single-agent endpoint
_cancel_requested: bool = False
_active_llms: list[Any] = []  # track all active LLM clients for cancel

# Lock สำหรับ serialize content_creator เมื่อหลาย flow รันขนานกัน
# ป้องกันการสร้างคอนเทนต์ซ้ำกัน — flow ที่มาทีหลังจะเห็น history ของ flow ก่อนหน้า
_content_creator_lock = threading.Lock()

AGENT_INFO = {
    "product_spec": {
        "name": "นักวิเคราะห์สินค้า",
        "desc": "สร้างสเปคสินค้าจากข้อมูลดิบ (txt, pdf, xlsx)",
        "icon": "📋",
        "accept": "raw",
        "flow_reason": "อ่านข้อมูลดิบแล้วสรุปเป็นสเปคสินค้า ซึ่งเป็นฐานให้ agent อื่นใช้ต่อ",
    },
    "competitor_analysis": {
        "name": "นักวิเคราะห์คู่แข่ง",
        "desc": "วิเคราะห์เปรียบเทียบคู่แข่ง (ค้นหา web เอง)",
        "icon": "🔍",
        "accept": "spec",
        "flow_reason": "ใช้สเปคสินค้าค้นหาคู่แข่งบนเว็บ แล้วสรุปจุดเด่น/จุดอ่อนเทียบกับเรา",
    },
    "campaign_strategy": {
        "name": "นักวางกลยุทธ์แคมเปญ",
        "desc": "วางกลยุทธ์แคมเปญ + ราคาแนะนำ",
        "icon": "📊",
        "accept": "spec",
        "flow_reason": "ใช้สเปคสินค้า + ข้อมูลคู่แข่งเพื่อวางกลยุทธ์ขายและกำหนดราคาแนะนำ",
    },
    "content_creator": {
        "name": "นักสร้างคอนเทนต์",
        "desc": "สร้าง content + prompt รูป + hashtag",
        "icon": "✍️",
        "accept": "spec",
        "flow_reason": "ใช้สเปคสินค้า + กลยุทธ์แคมเปญเขียนคอนเทนต์พร้อม prompt รูปและ hashtag",
    },
}

AGENT_DEPENDENCIES = {
    "product_spec": [],
    "competitor_analysis": ["product_spec"],
    "campaign_strategy": ["product_spec", "competitor_analysis"],
    "content_creator": ["product_spec", "competitor_analysis", "campaign_strategy"],
}

AGENT_ORDER = ["product_spec", "competitor_analysis", "campaign_strategy", "content_creator"]


def _get_orchestrator() -> Orchestrator:
    global _orch
    if _orch is None:
        _orch = Orchestrator(brand_dir="brand")
    return _orch


def _sse(event_type: str, text: str, **extra) -> str:
    data = {"type": event_type, "text": text, **extra}
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _scan_data_folders() -> list[dict[str, Any]]:
    """List product folders in data/ with summary info.

    data/ มีแค่ไฟล์ user เท่านั้น — DB และ deliverables อยู่ใน cache/
    สถานะสินค้าดึงจาก product DB (cache/{product_id}/product.json)
    """
    from src import product_db
    from src.ingestion import check_and_mark_stale

    folders = []
    if not DATA_DIR.exists():
        return folders
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    for item in sorted(DATA_DIR.iterdir()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        # ตรวจ raw data เปลี่ยนไหม → ถ้าเปลี่ยน ตั้ง stale
        check_and_mark_stale(item.name)

        raw_count = 0
        thumbnail = None
        for f in item.rglob("*"):
            if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                continue
            raw_count += 1
            if thumbnail is None and f.suffix.lower() in image_exts:
                thumbnail = str(f.relative_to(item))
        # สถานะจาก product DB — ถ้า DB ว่างแต่มีไฟล์ → ตรวจว่ารองรับไหม
        status = product_db.get_status(item.name)
        if status == product_db.STATUS_EMPTY and raw_count > 0:
            # มีไฟล์แต่ยังไม่ได้ ingest → ตรวจว่ารองรับไหม
            from src.ingestion import _classify_file, _load_config
            cfg = _load_config()
            has_usable = False
            for f in item.rglob("*"):
                if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                    continue
                if _classify_file(f.name, cfg) is not None:
                    has_usable = True
                    break
            status = product_db.STATUS_PENDING if has_usable else product_db.STATUS_NO_USABLE
            product_db.set_status(item.name, status)
        progress = product_db.get_progress(item.name)
        # นับ deliverables ใน cache/ (product_spec/competitor — สำหรับ user ไม่ใช่ data source)
        cache_dir = CACHE_DIR / item.name
        deliverable_count = 0
        if cache_dir.exists() and cache_dir.is_dir():
            for f in cache_dir.iterdir():
                if f.is_file() and not f.name.startswith(".") and f.name != ".DS_Store":
                    deliverable_count += 1
        # ตรวจว่ามี product_profile.json ไหม — สำหรับ sidebar indicator
        has_product_profile = (item / "product_profile.json").exists()
        folders.append({
            "name": item.name,
            "path": item.name,
            "file_count": raw_count,
            "raw_count": raw_count,
            "deliverable_count": deliverable_count,
            "status": status,        # จาก DB: empty/no_usable_data/processing/ready/stale
            "progress": progress,    # {step, total, message, eta_seconds} ถ้ากำลัง ingestion
            "thumbnail": thumbnail,
            "has_product_profile": has_product_profile,
        })
    return folders


def _scan_sessions() -> list[dict[str, Any]]:
    sessions = []
    if not OUTPUT_DIR.exists():
        return sessions
    for item in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not item.is_dir() or item.name.startswith(".") or item.name == ".DS_Store":
            continue
        files = []
        # เก็บชื่อไฟล์ทั้งหมดก่อน เพื่อกรอง .json ที่มี .md คู่กัน (content_creator structured output)
        all_names: set[str] = set()
        for f in sorted(item.iterdir()):
            if f.is_file() and not f.name.startswith(".") and f.name != ".DS_Store":
                all_names.add(f.name)
        for f in sorted(item.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                continue
            # ซ่อน .json ที่มี .md คู่กัน (content_creator structured output — user เห็น .md อย่างเดียวพอ)
            if f.suffix == ".json":
                md_pair = f.stem + ".md"
                if md_pair in all_names:
                    continue
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        if files:
            sessions.append({"name": item.name, "files": files})
    return sessions


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML_PAGE


@app.get("/wizard_ui.js", response_class=Response)
def wizard_ui_js() -> Response:
    """Wizard UI JavaScript bundle."""
    path = Path(__file__).resolve().parent / "src" / "wizard_ui.js"
    if path.exists():
        return Response(path.read_text(encoding="utf-8"), media_type="application/javascript")
    return Response("", media_type="application/javascript")


@app.get("/api/credits")
def api_credits() -> JSONResponse:
    """Fetch remaining credits and usage from OpenRouter."""
    import os
    api_key = get_env("OPENROUTER_API_KEY", "")
    if not api_key:
        return JSONResponse({"error": "no API key"}, status_code=500)
    try:
        import httpx
        resp = httpx.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=int(_sys_cfg().get("api_timeout_credits", 10)),
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return JSONResponse({
            "limit": data.get("limit"),
            "limit_remaining": data.get("limit_remaining"),
            "usage": data.get("usage"),
            "usage_daily": data.get("usage_daily"),
            "is_free_tier": data.get("is_free_tier"),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Media generation — สร้างรูป/วิดีโอจริงจาก prompt ใน content_creator output
# ---------------------------------------------------------------------------

@app.get("/api/media_config")
def api_media_config() -> JSONResponse:
    """คืน media config ปัจจุบัน — frontend ใช้แสดง auto toggle + model."""
    cfg = media_gen._load_media_config()
    return JSONResponse({
        "image_model": cfg.get("image_model", media_gen.DEFAULT_IMAGE_MODEL),
        "video_model": cfg.get("video_model", media_gen.DEFAULT_VIDEO_MODEL),
        "auto_generate_image": cfg.get("auto_generate_image", False),
        "auto_generate_video": cfg.get("auto_generate_video", False),
        "image_aspect_ratio": cfg.get("image_aspect_ratio", "16:9"),
        "video_duration": cfg.get("video_duration", 5),
        "video_aspect_ratio": cfg.get("video_aspect_ratio", "16:9"),
        "video_resolution": cfg.get("video_resolution", "720p"),
    })


@app.post("/api/media_config")
async def api_media_config_save(request: Request) -> JSONResponse:
    """บันทึก media config — ใช้สำหรับ toggle auto + เปลี่ยน model."""
    body = await request.json()
    import yaml as _yaml
    cfg_path = PROJECT_ROOT / "config" / "media.yaml"
    try:
        with cfg_path.open(encoding="utf-8") as f:
            cfg = _yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    # อัปเดตเฉพาะ field ที่ส่งมา
    for k, v in body.items():
        cfg[k] = v
    with cfg_path.open("w", encoding="utf-8") as f:
        _yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return JSONResponse({"ok": True})


@app.post("/api/parse_media_prompts")
async def api_parse_media_prompts(request: Request) -> JSONResponse:
    """Parse content_creator output แยก image + video prompts — ใช้ก่อนกดสร้างจริง.

    รองรับทั้ง .json (structured output ใหม่) และ .md (เดิม)
    ถ้า filepath เป็น .md → ลองหา .json ที่ชื่อเดียวกันก่อน (มีข้อมูล structure ครบ)
    """
    body = await request.json()
    content = body.get("content", "")
    if not content:
        # อ่านจากไฟล์แทน
        filepath = body.get("file", "")
        if filepath:
            p = Path(filepath)
            # ถ้าเป็น .md → ลองหา .json ที่ชื่อเดียวกันก่อน (structured output)
            if p.suffix == ".md":
                json_p = p.with_suffix(".json")
                if json_p.exists():
                    content = json_p.read_text(encoding="utf-8")
            if not content and p.exists():
                content = p.read_text(encoding="utf-8")
            if not content:
                return JSONResponse({"error": "file not found"}, status_code=404)
        else:
            return JSONResponse({"error": "missing content or file"}, status_code=400)
    parsed = media_gen.parse_media_prompts(content)
    return JSONResponse(parsed)


@app.post("/api/generate_media")
async def api_generate_media(request: Request) -> StreamingResponse:
    """สร้าง image หรือ video จาก prompt — SSE ส่ง status ระหว่างทำ.

    body: {
      type: "image" | "video",
      prompt: "...",
      output_dir: "session/folder",  # relative to OUTPUT_DIR
      filename: "image_1.png",
      usage: "feed post"  # optional
    }
    """
    body = await request.json()
    media_type = body.get("type", "image")
    prompt = body.get("prompt", "")
    output_dir_str = body.get("output_dir", "")
    filename = body.get("filename", "")
    usage = body.get("usage", "")
    # optional parameters จาก parse_media_prompts (duration, aspect_ratio, resolution)
    duration = body.get("duration")
    aspect_ratio = body.get("aspect_ratio")
    resolution = body.get("resolution")
    product_id = body.get("product_id", "")

    if not prompt or not output_dir_str or not filename:
        return JSONResponse({"error": "missing prompt, output_dir, or filename"})
    if media_type not in ("image", "video"):
        return JSONResponse({"error": "type must be image or video"})

    output_dir = OUTPUT_DIR / output_dir_str
    output_path = output_dir / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # หา flow_id ของ session นี้ — เพื่อผูก cost สร้างสื่อภายหลังเข้า flow เดิม
    # หา content_creator file ใน output_dir ก่อน แล้วใช้ path นั้นหา flow_id ที่ตรง
    # ถ้าไม่เจอ content file ใช้ find_any_flow_id เป็น fallback
    media_flow_id = ""
    if output_dir.exists():
        for f in output_dir.iterdir():
            if f.is_file() and "content_creator" in f.name.lower() and f.suffix == ".md":
                media_flow_id = find_flow_id_for_file(output_dir, f)
                if media_flow_id:
                    break
    if not media_flow_id:
        media_flow_id = find_any_flow_id(output_dir)

    # หารูปสินค้าจริงจาก product_id (ถ้าไม่มี ลองอ่านจาก session meta)
    if not product_id:
        meta_path = output_dir / "_session_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                product_id = meta.get("product_id", "")
            except Exception:
                pass
    product_image_paths: list[str] = []
    if product_id:
        # กรณี multi-product: product_id = "K5 + K2" → ดึงจากทุกสินค้า
        if " + " in product_id:
            for pid in product_id.split(" + "):
                product_image_paths.extend(product_db.get_product_image_paths(pid.strip()))
        else:
            product_image_paths = product_db.get_product_image_paths(product_id)

    q: _queue.Queue[str | None] = _queue.Queue()

    def worker():
        # ผูก LLM call ใน thread นี้เข้า flow เดิม (ถ้าเจอ) — สร้างสื่อภายหลัง
        if media_flow_id:
            set_flow_id(media_flow_id)
        try:
            if media_type == "image":
                q.put_nowait(_sse("status", "กำลังสร้างรูป..."))
                img_kwargs: dict = {}
                if aspect_ratio:
                    img_kwargs["aspect_ratio"] = aspect_ratio
                # ส่งรูปสินค้าจริงเป็น reference — image-to-image
                if product_image_paths:
                    img_kwargs["input_references"] = product_image_paths
                # Visual brand injection — แป๊ะ keywords/colors/tone ต่อท้าย prompt
                visual = _get_brand_visual()
                if visual:
                    img_kwargs["visual"] = visual
                result = media_gen.generate_image(prompt, output_path, **img_kwargs)
            else:
                def on_status(s):
                    q.put_nowait(_sse("status", f"วิดีโอ: {s}"))
                vid_kwargs: dict = {"on_status": on_status}
                if duration:
                    vid_kwargs["duration"] = int(duration)
                if aspect_ratio:
                    vid_kwargs["aspect_ratio"] = aspect_ratio
                if resolution:
                    vid_kwargs["resolution"] = resolution
                # ส่งรูปสินค้าจริงเป็น reference — reference-to-video
                if product_image_paths:
                    vid_kwargs["input_references"] = product_image_paths
                # Visual brand injection
                visual = _get_brand_visual()
                if visual:
                    vid_kwargs["visual"] = visual
                result = media_gen.generate_video(prompt, output_path, **vid_kwargs)

            # เก็บประวัติ (ถูก reject หรือสำเร็จ ก็เก็บ)
            media_gen.save_retry_history(output_path.parent, media_type, filename, result)

            if result.get("ok"):
                q.put_nowait(_sse("done", json.dumps({
                    "path": result.get("path"),
                    "paths": result.get("paths", [result.get("path")]),
                    "model": result.get("model"),
                    "usage": usage,
                    "type": media_type,
                    "warnings": result.get("warnings", []),
                }, ensure_ascii=False)))
            else:
                q.put_nowait(_sse("error", result.get("error", "unknown")))
        except Exception as e:
            q.put_nowait(_sse("error", str(e)))
        finally:
            # อัปเดต cost summary ของ flow เดิม (รวม cost สร้างสื่อภายหลัง)
            if media_flow_id:
                try:
                    update_cost_summary(output_dir, media_flow_id)
                except Exception:
                    pass
                clear_flow_id()
            q.put_nowait(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    async def stream():
        while True:
            item = await asyncio.get_event_loop().run_in_executor(None, q.get)
            if item is None:
                break
            yield item

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/generate_all_media")
async def api_generate_all_media(request: Request) -> StreamingResponse:
    """Parse content_creator output แล้วสร้าง image + video ทั้งหมด — SSE.

    body: { file: "session/04_content_creator_*.md", auto_image: bool, auto_video: bool }
    """
    body = await request.json()
    filepath = body.get("file", "")
    auto_image = body.get("auto_image", True)
    auto_video = body.get("auto_video", True)
    product_id = body.get("product_id", "")

    if not filepath:
        return JSONResponse({"error": "missing file"})
    p = Path(filepath)
    if not p.exists():
        # ลอง relative to OUTPUT_DIR
        p = OUTPUT_DIR / filepath
    if not p.exists():
        return JSONResponse({"error": f"file not found: {filepath}"})

    # หารูปสินค้าจริงจาก product_id (ถ้าไม่มี ลองอ่านจาก session meta)
    if not product_id:
        meta_path = p.parent / "_session_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                product_id = meta.get("product_id", "")
            except Exception:
                pass
    product_image_paths: list[str] = []
    if product_id:
        # กรณี multi-product: product_id = "K5 + K2" → ดึงจากทุกสินค้า
        if " + " in product_id:
            for pid in product_id.split(" + "):
                product_image_paths.extend(product_db.get_product_image_paths(pid.strip()))
        else:
            product_image_paths = product_db.get_product_image_paths(product_id)

    # ถ้าเป็น .md → ลองหา .json ที่ชื่อเดียวกันก่อน (structured output)
    content = ""
    if p.suffix == ".md":
        json_p = p.with_suffix(".json")
        if json_p.exists():
            content = json_p.read_text(encoding="utf-8")
    if not content:
        content = p.read_text(encoding="utf-8")
    parsed = media_gen.parse_media_prompts(content)
    images = parsed.get("images", [])
    videos = parsed.get("videos", [])

    # Phase 4: รวมรูปสินค้า + รูป asset เป็น input_references (helper เดียว)
    from src import asset_library as _al

    output_dir = p.parent
    session_rel = str(output_dir.relative_to(OUTPUT_DIR)) if output_dir.is_relative_to(OUTPUT_DIR) else str(output_dir)

    # หา flow_id ของ output file นี้ — เพื่อผูก cost สร้างสื่อภายหลังเข้า flow เดิม
    media_flow_id = find_flow_id_for_file(output_dir, p)

    # Persistent status file — เก็บสถานะ media gen ให้เห็นได้หลัง refresh
    status_file = output_dir / "_media_status.json"

    def _save_status(status: dict):
        try:
            status_file.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_status() -> dict:
        try:
            if status_file.exists():
                return json.loads(status_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    # เช็คว่ามี gen ค้างอยู่ไหม — ถ้ามี ไม่ให้เริ่มใหม่
    existing = _load_status()
    if existing.get("status") == "in_progress":
        return JSONResponse({"error": "media generation already in progress", "status": existing})

    # เริ่ม gen — บันทึกสถานะ
    _save_status({
        "status": "in_progress",
        "started_at": datetime.now().isoformat(),
        "auto_image": auto_image,
        "auto_video": auto_video,
        "total": (len(images) if auto_image else 0) + (len(videos) if auto_video else 0),
        "done": 0,
        "errors": [],
        "last_update": "",
    })

    q: _queue.Queue[str | None] = _queue.Queue()

    def worker():
        errors: list[str] = []
        # ผูก LLM call ใน thread นี้เข้า flow เดิม (ถ้าเจอ) — สร้างสื่อภายหลัง
        if media_flow_id:
            set_flow_id(media_flow_id)
        try:
            total = (len(images) if auto_image else 0) + (len(videos) if auto_video else 0)
            done = 0
            q.put_nowait(_sse("status", f"เริ่มสร้าง — {total} ไฟล์"))

            # ask mode — ไม่ auto-retry ถ้าถูก reject ส่ง error ให้ user ตัดสินใจ
            if auto_image:
                for i, img in enumerate(images):
                    q.put_nowait(_sse("status", f"รูปที่ {i+1}/{len(images)}: กำลังสร้าง..."))
                    _save_status({**_load_status(), "last_update": f"รูปที่ {i+1}: กำลังสร้าง..."})
                    fname = f"image_{i+1}.png"
                    out_path = output_dir / fname
                    img_kwargs: dict = {}
                    if img.get("aspect_ratio"):
                        img_kwargs["aspect_ratio"] = img["aspect_ratio"]
                    # ส่งรูปสินค้า + รูป asset เป็น reference — image-to-image
                    _refs = _al.build_input_references(
                        product_image_paths, img.get("asset_ids", []),
                    )
                    if _refs:
                        img_kwargs["input_references"] = _refs
                    # Visual brand injection
                    visual = _get_brand_visual()
                    if visual:
                        img_kwargs["visual"] = visual
                    result = media_gen.generate_image(img["prompt"], out_path, **img_kwargs)
                    done += 1
                    # เก็บประวัติ (ถูก reject หรือสำเร็จ ก็เก็บ)
                    media_gen.save_retry_history(output_dir, "image", fname, result)
                    if result.get("ok"):
                        q.put_nowait(_sse("media_done", json.dumps({
                            "type": "image", "path": result.get("path"),
                            "usage": img.get("usage", ""),
                            "index": i + 1, "total": len(images),
                            "warnings": result.get("warnings", []),
                        }, ensure_ascii=False)))
                    else:
                        err = f"รูปที่ {i+1}: {result.get('error')}"
                        errors.append(err)
                        q.put_nowait(_sse("error", err))

            if auto_video:
                for i, vid in enumerate(videos):
                    def on_status(s, idx=i):
                        q.put_nowait(_sse("status", f"วิดีโอที่ {idx+1}/{len(videos)}: {s}"))
                        _save_status({**_load_status(), "last_update": f"วิดีโอที่ {idx+1}: {s}"})
                    fname = f"video_{i+1}.mp4"
                    out_path = output_dir / fname
                    vid_kwargs: dict = {"on_status": on_status}
                    if vid.get("duration"):
                        vid_kwargs["duration"] = int(vid["duration"])
                    if vid.get("aspect_ratio"):
                        vid_kwargs["aspect_ratio"] = vid["aspect_ratio"]
                    if vid.get("resolution"):
                        vid_kwargs["resolution"] = vid["resolution"]
                    # ส่งรูปสินค้า + รูป asset เป็น reference — reference-to-video
                    _refs = _al.build_input_references(
                        product_image_paths, vid.get("asset_ids", []),
                    )
                    if _refs:
                        vid_kwargs["input_references"] = _refs
                    # Visual brand injection
                    visual = _get_brand_visual()
                    if visual:
                        vid_kwargs["visual"] = visual
                    result = media_gen.generate_video(vid["prompt"], out_path, **vid_kwargs)
                    done += 1
                    # เก็บประวัติ (ถูก reject หรือสำเร็จ ก็เก็บ)
                    media_gen.save_retry_history(output_dir, "video", fname, result)
                    if result.get("ok"):
                        q.put_nowait(_sse("media_done", json.dumps({
                            "type": "video", "path": result.get("path"),
                            "usage": vid.get("usage", ""),
                            "index": i + 1, "total": len(videos),
                            "warnings": result.get("warnings", []),
                        }, ensure_ascii=False)))
                    else:
                        err = f"วิดีโอที่ {i+1}: {result.get('error')}"
                        errors.append(err)
                        q.put_nowait(_sse("error", err))

            # บันทึกสถานะสุดท้าย
            final_status = "completed" if not errors else ("completed_with_errors" if done > 0 else "failed")
            _save_status({
                "status": final_status,
                "started_at": _load_status().get("started_at", ""),
                "finished_at": datetime.now().isoformat(),
                "auto_image": auto_image,
                "auto_video": auto_video,
                "total": total,
                "done": done,
                "errors": errors,
                "last_update": f"เสร็จ — {done}/{total}" + (f" ({len(errors)} errors)" if errors else ""),
            })
            q.put_nowait(_sse("done", json.dumps({"total": done, "errors": errors})))
        except Exception as e:
            errors.append(str(e))
            _save_status({
                "status": "failed",
                "started_at": _load_status().get("started_at", ""),
                "finished_at": datetime.now().isoformat(),
                "errors": errors,
                "last_update": f"error: {e}",
            })
            q.put_nowait(_sse("error", str(e)))
        finally:
            # อัปเดต cost summary ของ flow เดิม (รวม cost สร้างสื่อภายหลัง)
            if media_flow_id:
                try:
                    update_cost_summary(output_dir, media_flow_id)
                except Exception:
                    pass
                clear_flow_id()
            q.put_nowait(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    async def stream():
        while True:
            item = await asyncio.get_event_loop().run_in_executor(None, q.get)
            if item is None:
                break
            yield item

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/media_status/{session}")
def api_media_status(session: str) -> JSONResponse:
    """ดึงสถานะ media generation ของ session — ใช้ตอนเปิดหน้า output ใหม่."""
    status_file = OUTPUT_DIR / session / "_media_status.json"
    if not status_file.exists():
        return JSONResponse({"status": "none"})
    try:
        return JSONResponse(json.loads(status_file.read_text(encoding="utf-8")))
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)})


@app.get("/api/media_retry_log/{session}")
def api_media_retry_log(session: str) -> JSONResponse:
    """ดึงประวัติการ retry ของ session — ดูได้ผ่านหน้าเว็บ."""
    session_dir = OUTPUT_DIR / session
    if not session_dir.exists():
        return JSONResponse({"error": "session not found", "entries": []})
    entries = media_gen.load_retry_history(session_dir)
    return JSONResponse({"entries": entries})


@app.get("/api/cost_summary/{session}")
def api_cost_summary(session: str, file: str = "") -> JSONResponse:
    """ดึง cost summary ของ flow ที่ output file สังกัด — ใช้ใน output modal.

    Query param ``file`` (optional): ชื่อไฟล์หรือ path ของ output file
    ถ้าส่งมา จะหา flow_id ที่ตรงกับไฟล์นั้น
    ถ้าไม่ส่ง จะเอา flow_id แรกที่เจอใน session

    คืน: cost summary dict หรือ {"status": "none"} ถ้าไม่มี
    """
    session_dir = OUTPUT_DIR / session
    if not session_dir.exists():
        return JSONResponse({"status": "none"})

    # หา flow_id จาก _flow_meta_*.json
    flow_id = ""
    if file:
        flow_id = find_flow_id_for_file(session_dir, file)
    if not flow_id:
        flow_id = find_any_flow_id(session_dir)
    if not flow_id:
        return JSONResponse({"status": "none"})

    # อ่าน cost summary ไฟล์
    summary_path = session_dir / f"_cost_summary_{flow_id}.json"
    if not summary_path.exists():
        return JSONResponse({"status": "none"})
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)})


@app.post("/api/media_retry/{session}")
async def api_media_retry(session: str, request: Request) -> JSONResponse:
    """ล้างสถานะ media gen เดิม เพื่อให้กดสร้างใหม่ได้."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    auto_image = body.get("auto_image", True)
    auto_video = body.get("auto_video", True)
    status_file = OUTPUT_DIR / session / "_media_status.json"
    if status_file.exists():
        status_file.unlink()
    # หาไฟล์ content_creator ใน session
    session_dir = OUTPUT_DIR / session
    if not session_dir.exists():
        return JSONResponse({"error": "session not found"})
    cc_files = [f for f in session_dir.iterdir() if f.is_file() and "content_creator" in f.name.lower() and f.suffix == ".md"]
    if not cc_files:
        return JSONResponse({"error": "no content_creator file found"})
    # ลบไฟล์ media เดิมที่ fail
    for f in session_dir.iterdir():
        if f.is_file() and f.suffix in (".mp4", ".png", ".jpg", ".jpeg", ".webp"):
            # เก็บไฟล์ที่สร้างสำเร็จไว้ — ลบเฉพาะที่อาจจะ fail
            pass
    return JSONResponse({"ok": True, "file": str(cc_files[0].relative_to(OUTPUT_DIR)), "auto_image": auto_image, "auto_video": auto_video})


@app.get("/api/data_folders")
def api_data_folders() -> JSONResponse:
    return JSONResponse(_scan_data_folders())


@app.post("/api/upload")
async def api_upload(
    product_name: str = Form(...),
    files: list[UploadFile] = File(...),
) -> JSONResponse:
    """Upload files and create a product folder."""
    if not product_name.strip():
        return JSONResponse({"error": "กรุณาตั้งชื่อสินค้า"}, status_code=400)

    folder_name = product_name.strip()
    product_dir = DATA_DIR / folder_name
    product_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for f in files:
        if not f.filename or f.filename.startswith(".") or f.filename == ".DS_Store":
            continue
        dest = product_dir / f.filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            i = 1
            while dest.exists():
                dest = product_dir / f"{stem}_{i}{suffix}"
                i += 1
        content = await f.read()
        dest.write_bytes(content)
        saved.append(dest.name)

    # Auto-trigger ingestion after upload — user ไม่ต้องกดปุ่มเอง
    if saved:
        import threading
        from src import product_db
        from src.ingestion import ingest_product

        # ถ้ายังไม่ได้ processing ให้เริ่ม ingestion
        current_status = product_db.get_status(folder_name)
        if current_status != product_db.STATUS_PROCESSING:
            def _run():
                try:
                    ingest_product(folder_name, force=False)
                except Exception as e:
                    product_db.set_status(folder_name, product_db.STATUS_NO_USABLE, extra={
                        "ingest_error": str(e),
                    })
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()

    return JSONResponse({"ok": True, "folder": folder_name, "files": saved})


@app.get("/api/supported_formats")
def api_supported_formats() -> JSONResponse:
    """รายการรูปแบบไฟล์ที่ระบบรองรับ + ขนาดสูงสุด สำหรับโชว์ในหน้าเพิ่มสินค้า."""
    from src.ingestion import get_supported_formats, _load_config
    config = _load_config()
    return JSONResponse({
        "supported": get_supported_formats(),
        "max_file_size_mb": config.get("max_file_size_mb", {}),
    })


@app.post("/api/ingest/{folder}")
async def api_ingest(folder: str, request: Request) -> JSONResponse:
    """สั่ง ingestion สำหรับสินค้า — user กดปุ่ม 'ประมวลผลข้อมูล'.

    รันเป็น background thread เพื่อไม่ block web server
    ส่งสถานะ progress ผ่าน product DB (UI ดึงไปโชว์)
    """
    import threading
    from src import product_db
    from src.ingestion import ingest_product

    product_dir = DATA_DIR / folder
    if not product_dir.exists() or not product_dir.is_dir():
        return JSONResponse({"error": "ไม่พบโฟลเดอร์"}, status_code=404)

    # ถ้ากำลัง ingestion อยู่แล้ว → ปฏิเสธ
    current_status = product_db.get_status(folder)
    if current_status == product_db.STATUS_PROCESSING:
        return JSONResponse({"error": "กำลังประมวลผลอยู่แล้ว กรุณารอ"}, status_code=409)

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    force = body.get("force", False)

    # รัน ingestion ใน background thread
    def _run():
        try:
            ingest_product(folder, force=force)
        except Exception as e:
            # ถ้า error ตั้งสถานะกลับ
            product_db.set_status(folder, product_db.STATUS_NO_USABLE, extra={
                "ingest_error": str(e),
            })

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return JSONResponse({"ok": True, "message": "เริ่มประมวลผลข้อมูลแล้ว"})


@app.get("/api/ingest_status/{folder}")
def api_ingest_status(folder: str) -> JSONResponse:
    """ดึงสถานะ ingestion ปัจจุบัน — UI ดึงทุก 2 วินาที ตอนกำลัง ingestion."""
    from src import product_db
    record = product_db.load(folder)
    return JSONResponse({
        "status": record.get("status", product_db.STATUS_EMPTY),
        "progress": record.get("ingest_progress"),
        "ingested_at": record.get("ingested_at"),
        "ingest_error": record.get("ingest_error"),
        "files": record.get("files", []),
    })


@app.get("/api/folder_files/{folder}")
def api_folder_files(folder: str) -> JSONResponse:
    """List files in a product folder with status from product DB.

    สถานะไฟล์: ingested (✅), unsupported (⚠️), error (❌), pending (รอ)
    แยกจาก deliverables ใน cache/ (เอกสารสเปคจาก product_spec agent)
    """
    from src import product_db
    from src.ingestion import _classify_file, _load_config

    product_dir = DATA_DIR / folder
    if not product_dir.exists() or not product_dir.is_dir():
        return JSONResponse({"error": "ไม่พบโฟลเดอร์"})

    # โหลด DB record เพื่อเช็คสถานะไฟล์
    record = product_db.load(folder)
    db_files = {f["path"]: f for f in record.get("files", [])}
    config = _load_config()

    files = []
    # User files from data/ — ไฟล์ระบบไม่แสดง
    for f in sorted(product_dir.rglob("*")):
        if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store" or f.name == "product_profile.json":
            continue
        abs_path = str(f)
        db_entry = db_files.get(abs_path, {})
        ftype = _classify_file(f.name, config)
        # สถานะไฟล์: ingested / unsupported / error / pending
        if db_entry.get("status"):
            file_status = db_entry["status"]
        elif ftype is None:
            file_status = "unsupported"
        else:
            file_status = "pending"
        files.append({
            "name": f.name,
            "path": str(f.relative_to(product_dir)),
            "abs_path": abs_path,
            "type": ftype,            # text/image/video/audio/None
            "status": file_status,    # ingested/unsupported/error/pending
            "is_ready": file_status == "ingested",  # backward compatible
            "size": f.stat().st_size,
            "error": db_entry.get("error"),
        })
    # Deliverables from cache/ (เอกสารสเปคจาก product_spec agent — สำหรับ user)
    # กรอง product.json ออก — เป็น DB ของระบบ ไม่ใช่ deliverable ที่ user ต้องเห็น
    cache_dir = CACHE_DIR / folder
    if cache_dir.exists() and cache_dir.is_dir():
        for f in sorted(cache_dir.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                continue
            if f.name == "product.json":
                continue  # DB ของระบบ — ไม่โชว์ให้ user
            files.append({
                "name": f.name,
                "path": "cache/" + f.name,
                "type": "deliverable",     # ไม่ใช่ raw data — เป็นเอกสารที่ user สั่งทำ
                "status": "deliverable",
                "is_ready": True,          # backward compatible
                "size": f.stat().st_size,
            })
    return JSONResponse(files)


@app.get("/api/product_image/{folder}")
def api_product_image(folder: str):
    """Serve the first image file found in a product folder as a thumbnail."""
    product_dir = DATA_DIR / folder
    if not product_dir.exists() or not product_dir.is_dir():
        return JSONResponse({"error": "ไม่พบโฟลเดอร์"}, status_code=404)
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    for f in sorted(product_dir.rglob("*")):
        if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
            continue
        if f.suffix.lower() in image_exts:
            return FileResponse(str(f))
    return JSONResponse({"error": "ไม่มีรูป"}, status_code=404)


@app.delete("/api/folder_file/{folder}")
async def api_delete_file(request: Request) -> JSONResponse:
    """Delete a file from a product folder or cache folder.

    ถ้าลบ raw file → sync product DB ด้วย:
      - ลบ entry ของไฟล์นั้นออกจาก files list
      - ลบข้อมูลที่ extract จากไฟล์นั้น (raw_text/image_descriptions/ฯลฯ)
      - ตั้งสถานะ stale (ข้อมูลเก่า รอ re-ingest)
    ถ้าลบ deliverable ใน cache/ → แค่ลบไฟล์ ไม่กระทบ DB
    """
    from src import product_db

    body = await request.json()
    folder = body.get("folder", "")
    filepath = body.get("filepath", "")
    if not folder or not filepath:
        return JSONResponse({"error": "ไม่ระบุโฟลเดอร์หรือไฟล์"}, status_code=400)
    # Files in cache/ are deliverables (เอกสารสเปคจาก product_spec agent) — ลบได้เลย ไม่กระทบ DB
    if filepath.startswith("cache/"):
        real_name = filepath[len("cache/"):]
        if ".." in real_name:
            return JSONResponse({"error": "เส้นทางไม่ถูกต้อง"}, status_code=400)
        full_path = CACHE_DIR / folder / real_name
        if full_path.exists() and full_path.is_file():
            full_path.unlink()
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "ไม่พบไฟล์"})
    # User files in data/ — ลบไฟล์ + sync DB
    full_path = DATA_DIR / folder / filepath
    if ".." in filepath or not full_path.resolve().is_relative_to((DATA_DIR / folder).resolve()):
        return JSONResponse({"error": "เส้นทางไม่ถูกต้อง"}, status_code=400)
    if full_path.exists() and full_path.is_file():
        full_path.unlink()
        # Sync product DB — ลบ entry ของไฟล์นั้น และตั้ง stale
        abs_path = str(full_path)
        record = product_db.load(folder)
        files = record.get("files", [])
        # หาไฟล์ที่ลด
        removed_file = None
        new_files = []
        for f in files:
            if f.get("path") == abs_path:
                removed_file = f
            else:
                new_files.append(f)
        record["files"] = new_files
        # ถ้าไม่มีไฟล์เหลือเลย → empty + clear ข้อมูลทั้งหมด (ไม่มีอะไรจะ re-ingest แล้ว)
        if not new_files:
            record["status"] = product_db.STATUS_EMPTY
            record["raw_text"] = ""
            record["image_descriptions"] = []
            record["video_transcripts"] = []
            record["audio_transcripts"] = []
            record["ingested_at"] = None
        elif removed_file and removed_file.get("status") == "ingested":
            # ยังมีไฟล์อื่นอยู่ แต่ไฟล์ที่ลบเคย ingest → ข้อมูลใน DB อาจเก่า ตั้ง stale
            # ลบข้อมูลที่ extract จากไฟล์นั้นตามประเภท
            ftype = removed_file.get("type")
            fname = removed_file.get("name")
            if ftype == "image":
                record["image_descriptions"] = [
                    d for d in record.get("image_descriptions", []) if d.get("file") != fname
                ]
            elif ftype == "video":
                record["video_transcripts"] = [
                    d for d in record.get("video_transcripts", []) if d.get("file") != fname
                ]
            elif ftype == "audio":
                record["audio_transcripts"] = [
                    d for d in record.get("audio_transcripts", []) if d.get("file") != fname
                ]
            # text ไม่ลบ raw_text เพราะมาจากหลายไฟล์ปนกัน — ต้อง re-ingest ถึงจะครบ
            # ตั้งสถานะ stale (ข้อมูลอาจไม่ครบ รอ re-ingest)
            record["status"] = product_db.STATUS_STALE
        product_db.save(folder, record)
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "ไม่พบไฟล์"})


@app.delete("/api/folder")
async def api_delete_folder(request: Request) -> JSONResponse:
    """Delete an entire product folder — both data/ and cache/."""
    import shutil
    body = await request.json()
    folder = body.get("folder", "")
    if not folder:
        return JSONResponse({"error": "ไม่ระบุโฟลเดอร์"}, status_code=400)
    folder_path = DATA_DIR / folder
    if not folder_path.exists() or not folder_path.is_dir():
        return JSONResponse({"error": "ไม่พบโฟลเดอร์"})
    shutil.rmtree(folder_path)
    # Also clean up cache/
    cache_path = CACHE_DIR / folder
    if cache_path.exists() and cache_path.is_dir():
        shutil.rmtree(cache_path)
    return JSONResponse({"ok": True})


_MEDIA_SUFFIXES = ('.png', '.jpg', '.jpeg', '.webp', '.mp4', '.mov', '.webm')
_SYSTEM_FILES = frozenset({'_session_meta.json', '_media_status.json', '_media_retry_log.json', '.DS_Store'})


@app.delete("/api/output_file")
async def api_delete_output_file(request: Request) -> JSONResponse:
    """ลบไฟล์ output + history entry ที่เกี่ยวข้อง.

    ลบไฟล์ .md + .json + รูป/วิดีโอที่เกี่ยวข้องในโฟลเดอร์เดียวกัน
    ลบ history entries ที่มี output_file ตรงกับไฟล์ที่ลบ
    ถ้าโฟลเดอร์ output ว่างแล้ว → ลบโฟลเดอร์ด้วย

    body: { file: "path/to/04_content_creator_*.md" }
    """
    import shutil
    body = await request.json()
    filepath = body.get("file", "")
    if not filepath:
        return JSONResponse({"error": "missing file"}, status_code=400)

    p = Path(filepath)
    if not p.exists():
        # ลอง relative to OUTPUT_DIR
        p = OUTPUT_DIR / filepath
    if not p.exists():
        return JSONResponse({"error": "ไม่พบไฟล์"}, status_code=404)

    # ตรวจว่าอยู่ใน OUTPUT_DIR จริง (security — ห้ามลบนอก output/)
    try:
        p.resolve().relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "path ไม่ถูกต้อง"}, status_code=400)

    output_dir = p.parent
    base_name = p.stem  # ไม่มี .md/.json

    # ลบไฟล์หลัก (.md + .json ที่ชื่อเดียวกัน)
    deleted_files = []
    for suffix in [".md", ".json"]:
        f = output_dir / f"{base_name}{suffix}"
        if f.exists():
            f.unlink()
            deleted_files.append(str(f))

    # ลบรูป/วิดีโอที่เกี่ยวข้อง
    # ถ้าเป็น multi-post (มี "โพสต์ที่X" ในชื่อ) → ลบเฉพาะรูปของโพสต์นั้น
    # ถ้าเป็น single-post → ลบรูปทั้งหมดในโฟลเดอร์ที่ไม่ได้เกี่ยวกับโพสต์อื่น
    import re
    post_match = re.search(r'โพสต์ที่(\d+)', base_name)
    post_num = post_match.group(1) if post_match else None
    for f in output_dir.iterdir():
        if not f.is_file() or f.suffix not in _MEDIA_SUFFIXES:
            continue
        if post_num:
            # multi-post — ลบเฉพาะรูปของโพสต์นี้
            if f'โพสต์{post_num}' in f.name or f.name in (f'image_{post_num}.png', f'video_{post_num}.mp4'):
                f.unlink()
                deleted_files.append(str(f))
        else:
            # single-post — ลบรูปที่ไม่มี "โพสต์" ในชื่อ
            if 'โพสต์' not in f.name:
                f.unlink()
                deleted_files.append(str(f))

    # ลบ history entries ที่เกี่ยวข้อง (history เก็บ .md path ซึ่งอยู่ใน deleted_files แล้ว)
    removed_entries = 0
    for deleted_path in deleted_files:
        removed = content_history.delete_entry_by_output_file(PROJECT_ROOT, deleted_path)
        removed_entries += removed

    # ถ้าโฟลเดอร์ output ว่างแล้ว (เหลือแค่ไฟล์ระบบ) → ลบโฟลเดอร์
    remaining = [f for f in output_dir.iterdir()
                 if f.is_file() and not f.name.startswith('.') and f.name not in _SYSTEM_FILES]
    if not remaining:
        shutil.rmtree(output_dir, ignore_errors=True)

    return JSONResponse({
        "ok": True,
        "deleted_files": len(deleted_files),
        "removed_history_entries": removed_entries,
    })


@app.post("/api/rename_folder")
async def api_rename_folder(request: Request) -> JSONResponse:
    """Rename a product folder."""
    body = await request.json()
    old_name = body.get("old_name", "").strip()
    new_name = body.get("new_name", "").strip()
    if not old_name or not new_name:
        return JSONResponse({"error": "ไม่ระบุชื่อโฟลเดอร์"}, status_code=400)
    if old_name == new_name:
        return JSONResponse({"ok": True, "folder": new_name, "renamed": False})
    old_path = DATA_DIR / old_name
    if not old_path.exists() or not old_path.is_dir():
        return JSONResponse({"error": "ไม่พบโฟลเดอร์เดิม"}, status_code=400)
    new_path = DATA_DIR / new_name
    if new_path.exists():
        return JSONResponse({"error": "มีสินค้าชื่อนี้แล้ว"}, status_code=400)
    old_path.rename(new_path)
    # Also rename cache/ folder if it exists
    old_cache = CACHE_DIR / old_name
    if old_cache.exists() and old_cache.is_dir():
        new_cache = CACHE_DIR / new_name
        if not new_cache.exists():
            old_cache.rename(new_cache)
    return JSONResponse({"ok": True, "folder": new_name, "renamed": True})


@app.get("/api/brand_files")
def api_brand_files() -> JSONResponse:
    """List brand files."""
    files = []
    if not BRAND_DIR.exists():
        return JSONResponse(files)
    for f in sorted(BRAND_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in {".md", ".txt"} and not f.name.startswith("."):
            files.append({
                "name": f.name,
                "title": f.stem.replace("_", " ").title(),
                "size": f.stat().st_size,
            })
    return JSONResponse(files)


@app.get("/api/brand_file/{filename}")
def api_brand_file_get(filename: str) -> JSONResponse:
    """Get brand file content."""
    filepath = BRAND_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "ไม่พบไฟล์"})
    return JSONResponse({"content": filepath.read_text(encoding="utf-8")})


@app.post("/api/brand_save")
async def api_brand_save(request: Request) -> JSONResponse:
    """Save brand file content."""
    body = await request.json()
    filename = body.get("filename", "")
    content = body.get("content", "")
    if not filename:
        return JSONResponse({"error": "ไม่ระบุชื่อไฟล์"}, status_code=400)
    if "/" in filename or ".." in filename:
        return JSONResponse({"error": "ชื่อไฟล์ไม่ถูกต้อง"}, status_code=400)
    filepath = BRAND_DIR / filename
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")
    return JSONResponse({"ok": True})


# ============================================================
# Brand JSON — อ่าน/เขียน brand/*.json (structured form-based UI)
# ============================================================

@app.get("/api/brand_json")
def api_brand_json_get() -> JSONResponse:
    """อ่าน brand config ทั้งหมด — voice.json, terms.json, visual.json, audience.json + brand_profile.md."""
    import json as _json
    result: dict = {}
    # JSON files
    for name in ("voice.json", "terms.json", "visual.json", "audience.json"):
        path = BRAND_DIR / name
        if path.exists():
            try:
                result[name.removesuffix(".json")] = _json.loads(path.read_text(encoding="utf-8"))
            except (_json.JSONDecodeError, OSError):
                result[name.removesuffix(".json")] = {}
        else:
            result[name.removesuffix(".json")] = {}
    # profile.md (text)
    profile_path = BRAND_DIR / "brand_profile.md"
    result["profile"] = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""
    return JSONResponse(result)


@app.post("/api/brand_json_save")
async def api_brand_json_save(request: Request) -> JSONResponse:
    """บันทึก brand config — รับ dict {voice, terms, visual, audience, profile}."""
    import json as _json
    body = await request.json()
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    # JSON files
    for key, fname in [("voice", "voice.json"), ("terms", "terms.json"),
                       ("visual", "visual.json"), ("audience", "audience.json")]:
        data = body.get(key)
        if data is not None:
            path = BRAND_DIR / fname
            path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            saved.append(fname)
    # profile.md (text)
    profile = body.get("profile")
    if profile is not None:
        (BRAND_DIR / "brand_profile.md").write_text(profile, encoding="utf-8")
        saved.append("brand_profile.md")
    # Clear visual cache เพื่อโหลดใหม่ในครั้งต่อไป
    global _BRAND_VISUAL_CACHE
    _BRAND_VISUAL_CACHE = None
    # Refresh conflict cache — brand rules เปลี่ยน ต้องตรวจ agent ทุกตัวใหม่
    _refresh_conflict_cache()
    return JSONResponse({"ok": True, "saved": saved})


@app.post("/api/brand_migrate")
async def api_brand_migrate(request: Request) -> JSONResponse:
    """Migrate brand .md → .json (one-time conversion)."""
    from src.brand_migrate import migrate_brand
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    force = bool(body.get("force", False)) if body else False
    status = migrate_brand(BRAND_DIR, force=force)
    # Clear visual cache
    global _BRAND_VISUAL_CACHE
    _BRAND_VISUAL_CACHE = None
    return JSONResponse({"ok": True, **status})


# ============================================================
# Product Profile — ตำแหน่งสินค้า (positioning) แยกจากแบรนด์
# ============================================================

@app.get("/api/product_profile/{folder}")
def api_product_profile_get(folder: str) -> JSONResponse:
    """อ่าน data/{folder}/product_profile.json — คืน {} ถ้าไม่มี."""
    import json as _json
    path = DATA_DIR / folder / "product_profile.json"
    if path.exists():
        try:
            return JSONResponse(_json.loads(path.read_text(encoding="utf-8")))
        except (_json.JSONDecodeError, OSError):
            pass
    return JSONResponse({})


@app.post("/api/product_profile_save/{folder}")
async def api_product_profile_save(folder: str, request: Request) -> JSONResponse:
    """บันทึก product_profile.json — รับ dict จาก body."""
    import json as _json
    body = await request.json()
    product_dir = DATA_DIR / folder
    if not product_dir.exists():
        return JSONResponse({"ok": False, "error": f"ไม่พบสินค้า {folder}"}, status_code=404)
    path = product_dir / "product_profile.json"
    path.write_text(_json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"ok": True, "saved": str(path.relative_to(PROJECT_ROOT))})


@app.post("/api/product_profile_suggest/{folder}")
async def api_product_profile_suggest(folder: str) -> JSONResponse:
    """AI อ่านสเปคสินค้า → สรุปตำแหน่งสินค้า (pre-fill ฟอร์ม).

    ใช้ product_db.get_agent_context_text() ดึงสเปค → analyze_product_positioning() → คืน suggested dict.
    """
    from src.product_db import get_agent_context_text, is_ready
    from src.voice_learner import analyze_product_positioning

    if not is_ready(folder):
        return JSONResponse({"ok": False, "error": f"สินค้า {folder} ยังไม่พร้อม (ต้อง ingest ก่อน)"})

    spec_text = get_agent_context_text(folder)
    if not spec_text.strip():
        return JSONResponse({"ok": False, "error": "ไม่มีสเปคสินค้าให้วิเคราะห์"})

    # สร้าง LLM client (ใช้ pattern เดียวกับ voice_learn)
    try:
        orch = Orchestrator(brand_dir="brand")
        llm = orch.make_client()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"สร้าง LLM client ไม่ได้: {e}"}, status_code=500)

    suggested = analyze_product_positioning(spec_text, llm)
    if not suggested:
        return JSONResponse({"ok": False, "error": "AI วิเคราะห์ไม่สำเร็จ ลองกรอกเองหรือลองใหม่อีกครั้ง"})

    return JSONResponse({"ok": True, "suggested": suggested})


# ============================================================
# Voice Learning — วิเคราะห์ตัวอย่างโพสต์ → voice profile
# ============================================================

@app.post("/api/voice_learn")
async def api_voice_learn(request: Request) -> JSONResponse:
    """รับตัวอย่าง (paste + files + URLs) → LLM วิเคราะห์ → คืน brand profile (voice + terms + audience).

    Body: {pasted_texts: [str], file_paths: [str], urls: [str]}
    คืน: {ok: true, brand: {voice, terms, audience}, example_count} หรือ {ok: false, error}
    """
    from src.voice_learner import collect_examples, analyze_brand, fetch_url_content, extract_file_text
    body = await request.json()
    pasted = body.get("pasted_texts", [])
    files = body.get("file_paths", [])
    urls = body.get("urls", [])

    # รวมตัวอย่างจากทุกแหล่ง
    examples = collect_examples(pasted_texts=pasted, file_paths=files, urls=urls)
    if not examples:
        # บอก user ว่าอะไรพัง — ไม่ใช่ "ไม่มีตัวอย่าง" แบบสับสน
        errors = []
        for url in urls:
            content = fetch_url_content(url)
            if content.startswith("[error]"):
                errors.append(f"URL {url}: {content[7:80]}")
        for fp in files:
            content = extract_file_text(__import__("pathlib").Path(fp))
            if content.startswith("[error]"):
                errors.append(f"ไฟล์ {fp}: {content[7:80]}")
        if errors:
            msg = "ดึงตัวอย่างไม่สำเร็จ:\n" + "\n".join(errors)
            if any("facebook.com" in u for u in urls):
                msg += "\n\nFacebook บล็อก scraper — กรุณา paste text จากโพสต์ Facebook โดยตรง"
        else:
            msg = "ไม่มีตัวอย่างให้วิเคราะห์ — กรุณา paste text, upload ไฟล์, หรือใส่ URL"
        return JSONResponse({"ok": False, "error": msg}, status_code=400)

    # สร้าง LLM client
    try:
        orch = Orchestrator(brand_dir="brand")
        llm = orch.make_client()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"สร้าง LLM client ไม่ได้: {e}"}, status_code=500)

    try:
        brand = analyze_brand(examples, llm)
        # ตรวจว่าอย่างน้อย 1 ส่วนสำเร็จ
        if not any(brand.values()):
            return JSONResponse({"ok": False, "error": "LLM วิเคราะห์ไม่สำเร็จ — ลองใหม่อีกครั้ง"}, status_code=500)
        return JSONResponse({"ok": True, "brand": brand, "example_count": len(examples)})
    finally:
        try:
            llm.close()
        except Exception:
            pass


@app.post("/api/voice_learn_upload")
async def api_voice_learn_upload(request: Request) -> JSONResponse:
    """รับไฟล์ upload (multipart) → เซฟ temp → คืน path สำหรับส่งให้ /api/voice_learn."""
    from fastapi import UploadFile, File, Form
    # ใช้ Request โดยตรงเพราะเราใช้ JSONResponse ไม่ใช่ fastapi Form
    form = await request.form()
    files = form.getlist("files")
    if not files:
        return JSONResponse({"ok": False, "error": "ไม่มีไฟล์"}, status_code=400)

    import tempfile
    saved: list[str] = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="voice_learn_"))
    for f in files:
        if hasattr(f, "filename") and f.filename:
            dest = tmp_dir / f.filename
            content = await f.read()
            dest.write_bytes(content)
            saved.append(str(dest))

    return JSONResponse({"ok": True, "paths": saved, "tmp_dir": str(tmp_dir)})


# ============================================================
# Asset Library — วัตถุดิบกลางของแบรนด์ (โลโก้ รูปคน เพลง ฯลฯ)
# agent ค้นและหยิบใช้เองผ่าน tool calling
# ============================================================

@app.get("/api/assets")
def api_assets_list() -> JSONResponse:
    """list_all() — สำหรับ UI แสดง asset ทั้งหมด."""
    from src import asset_library
    return JSONResponse({"assets": asset_library.list_all()})


@app.get("/api/assets/{asset_id}")
def api_assets_get(asset_id: str) -> JSONResponse:
    """record เต็มของ asset หนึ่ง."""
    from src import asset_library
    rec = asset_library.get_asset(asset_id)
    if not rec:
        return JSONResponse({"error": "ไม่พบ asset"}, status_code=404)
    return JSONResponse(rec)


@app.post("/api/assets/upload")
async def api_assets_upload(
    files: list[UploadFile] = File(...),
    user_note: str = Form(""),
) -> JSONResponse:
    """อัปโหลดไฟล์เข้า brand/assets/ → background ingest (auto-tag + embed).

    เหมือน /api/upload ของ product — เซฟไฟล์ก่อน แล้ว trigger ingestion ใน background thread.
    """
    from src import asset_library

    assets_dir = Path(__file__).parent / "brand" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for f in files:
        if not f.filename or f.filename.startswith(".") or f.filename == ".DS_Store":
            continue
        dest = assets_dir / f.filename
        # กันชนชื่อซ้ำ
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            i = 1
            while dest.exists():
                dest = assets_dir / f"{stem}_{i}{suffix}"
                i += 1
        content = await f.read()
        dest.write_bytes(content)
        saved.append(dest.name)

    if not saved:
        return JSONResponse({"error": "ไม่มีไฟล์ที่บันทึก"}, status_code=400)

    # trigger ingestion ใน background (auto-tag + embed)
    def _run():
        llm = asset_library.make_llm()
        try:
            for name in saved:
                asset_library.ingest_asset(assets_dir / name, llm=llm, user_note=user_note)
        except Exception as e:
            print(f"[AssetLibrary] ingest error: {e}", flush=True)
        finally:
            if llm:
                llm.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return JSONResponse({"ok": True, "files": saved})


@app.post("/api/assets/{asset_id}/update")
async def api_assets_update(asset_id: str, request: Request) -> JSONResponse:
    """HITL — user แก้ metadata (tags/description/subject/style/user_note)."""
    from src import asset_library
    body = await request.json()
    result = asset_library.update_asset(
        asset_id,
        tags=body.get("tags"),
        description=body.get("description"),
        subject=body.get("subject"),
        style=body.get("style"),
        user_note=body.get("user_note"),
    )
    if not result:
        return JSONResponse({"error": "ไม่พบ asset"}, status_code=404)
    return JSONResponse(result)


@app.post("/api/assets/{asset_id}/delete")
async def api_assets_delete(asset_id: str, request: Request) -> JSONResponse:
    """ลบ asset + ลบไฟล์จริง."""
    from src import asset_library
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    remove_file = body.get("remove_file", True)
    ok = asset_library.delete_asset(asset_id, remove_file=remove_file)
    if not ok:
        return JSONResponse({"error": "ไม่พบ asset"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/api/assets/file/{asset_id}")
def api_assets_file(asset_id: str):
    """serve ไฟล์จริง — สำหรับ thumbnail ใน UI (pattern เดียวกับ /api/product_image)."""
    from src import asset_library
    from fastapi import Response
    rec = asset_library.get_asset(asset_id)
    if not rec:
        return JSONResponse({"error": "ไม่พบ asset"}, status_code=404)
    p = Path(rec.get("path", ""))
    if not p.exists():
        return JSONResponse({"error": "ไฟล์ไม่มี"}, status_code=404)
    import mimetypes
    ct, _ = mimetypes.guess_type(str(p))
    return Response(content=p.read_bytes(), media_type=ct or "application/octet-stream")


@app.post("/api/assets/reingest")
async def api_assets_reingest(request: Request) -> JSONResponse:
    """สแกน brand/assets/ ใหม่ — สำหรับปุ่ม 'สแกนใหม่' หรือกรณี user โยนไฟล์ตรงเข้าโฟลเดอร์."""
    from src import asset_library
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    force = body.get("force", False)

    def _run():
        llm = asset_library.make_llm()
        try:
            asset_library.ingest_all(llm=llm, force=force)
        except Exception as e:
            print(f"[AssetLibrary] reingest error: {e}", flush=True)
        finally:
            if llm:
                llm.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return JSONResponse({"ok": True, "message": "เริ่มสแกนใหม่แล้ว"})


@app.get("/api/assets_config")
def api_assets_config() -> JSONResponse:
    """taxonomy + ขนาดไฟล์สูงสุด — สำหรับ UI แสดง dropdown + ข้อจำกัด."""
    from src import asset_library
    cfg = asset_library._load_config()
    return JSONResponse({
        "taxonomy": cfg.get("taxonomy", {}),
        "max_file_size_mb": cfg.get("max_file_size_mb", {}),
        "supported_formats": cfg.get("supported_formats", {}),
    })


# ============================================================
# Video Style Analysis — วิเคราะห์วิดีโอคู่แข่ง → style profile
# (Style Reverse-Engineering จาก Notion "GOODBYE CAPCUT" prompt #2)
# ============================================================

@app.post("/api/video_style_analyze")
async def api_video_style_analyze(request: Request) -> JSONResponse:
    """รับ YouTube URL หรือไฟล์วิดีโอ → LLM วิเคราะห์สไตล์ → คืน video style profile.

    Body: {video_url: str} หรือ {video_path: str}
    คืน: {ok: true, video_style: {...}} หรือ {ok: false, error}
    """
    from src.voice_learner import analyze_video_style
    body = await request.json()
    video_url = body.get("video_url")
    video_path = body.get("video_path")

    if not video_url and not video_path:
        return JSONResponse({"ok": False, "error": "ต้องส่ง video_url หรือ video_path"}, status_code=400)

    try:
        orch = Orchestrator(brand_dir="brand")
        llm = orch.make_client()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"สร้าง LLM client ไม่ได้: {e}"}, status_code=500)

    try:
        style = analyze_video_style(llm, video_url=video_url, video_path=video_path)
        if not style:
            return JSONResponse({"ok": False, "error": "LLM วิเคราะห์ไม่สำเร็จ — ตรวจสอบ URL/ไฟล์ แล้วลองใหม่"}, status_code=500)
        return JSONResponse({"ok": True, "video_style": style})
    finally:
        try:
            llm.close()
        except Exception:
            pass


@app.post("/api/video_style_upload")
async def api_video_style_upload(request: Request) -> JSONResponse:
    """รับไฟล์วิดีโอ upload (multipart) → เซฟ temp → คืน path."""
    form = await request.form()
    files = form.getlist("files")
    if not files:
        return JSONResponse({"ok": False, "error": "ไม่มีไฟล์"}, status_code=400)

    import tempfile
    saved: list[str] = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="video_style_"))
    for f in files:
        if hasattr(f, "filename") and f.filename:
            dest = tmp_dir / f.filename
            content = await f.read()
            dest.write_bytes(content)
            saved.append(str(dest))

    return JSONResponse({"ok": True, "paths": saved})



@app.get("/api/pillars")
def api_pillars_get() -> JSONResponse:
    """อ่าน pillars + pillar_keywords จาก config/content_policy.yaml."""
    import yaml as _yaml
    cfg_path = PROJECT_ROOT / "config" / "content_policy.yaml"
    if not cfg_path.exists():
        return JSONResponse({"pillars": [], "pillar_keywords": {}})
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
        pillars = data.get("pillars", [])
        keywords = data.get("pillar_keywords", {})
        # รับประกันว่าทุก pillar มี entry ใน keywords
        for p in pillars:
            if p not in keywords:
                keywords[p] = []
        return JSONResponse({"pillars": pillars, "pillar_keywords": keywords})
    except Exception as e:
        return JSONResponse({"error": f"อ่าน config ไม่ได้: {e}"}, status_code=500)


@app.post("/api/pillars_save")
async def api_pillars_save(request: Request) -> JSONResponse:
    """บันทึก pillars + pillar_keywords กลับลง content_policy.yaml.

    อ่านไฟล์เดิมทั้งหมด แก้เฉพาะส่วน pillars + pillar_keywords เก็บส่วนอื่นไว้
    """
    import yaml as _yaml
    body = await request.json()
    pillars = body.get("pillars", [])
    keywords = body.get("pillar_keywords", {})

    # กรอง keywords ให้มีแค่ pillar ที่มีอยู่
    clean_keywords = {p: keywords.get(p, []) for p in pillars}

    cfg_path = PROJECT_ROOT / "config" / "content_policy.yaml"
    try:
        # อ่านไฟล์เดิม (ถ้ามี) เพื่อรักษาส่วนอื่นไว้
        existing = {}
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                existing = _yaml.safe_load(f) or {}

        # แทนที่เฉพาะส่วน pillars + pillar_keywords
        existing["pillars"] = pillars
        existing["pillar_keywords"] = clean_keywords

        # เขียนกลับ — ใช้ default_flow_style=False เพื่อให้อ่านง่าย
        with open(cfg_path, "w", encoding="utf-8") as f:
            _yaml.dump(existing, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": f"บันทึกไม่ได้: {e}"}, status_code=500)


@app.get("/api/agent_config/{agent_key}")
def api_agent_config_get(agent_key: str) -> JSONResponse:
    """Get agent config from agents.yaml."""
    import yaml as _yaml
    config_path = PROJECT_ROOT / "config" / "agents.yaml"
    if not config_path.exists():
        return JSONResponse({"error": "ไม่พบ config/agents.yaml"})
    with open(config_path, "r", encoding="utf-8") as f:
        config = _yaml.safe_load(f)
    defaults = config.get("defaults", {})
    agent_cfg = config.get(agent_key, {})
    merged = {**defaults, **agent_cfg}
    return JSONResponse({
        "model": merged.get("model", ""),
        "temperature": merged.get("temperature", 0.7),
        "max_tokens": merged.get("max_tokens", 4096),
        "max_retry_limit": merged.get("max_retry_limit", 3),
        "max_review_iterations": merged.get("max_review_iterations", 1),
        "review_temperature": merged.get("review_temperature", 0.2),
        "timeout_seconds": merged.get("timeout_seconds", 120),
    })


@app.post("/api/agent_config/{agent_key}")
async def api_agent_config_save(agent_key: str, request: Request) -> JSONResponse:
    """Save agent config to agents.yaml."""
    import yaml as _yaml
    body = await request.json()
    config_path = PROJECT_ROOT / "config" / "agents.yaml"
    if not config_path.exists():
        return JSONResponse({"error": "ไม่พบ config/agents.yaml"}, status_code=400)
    with open(config_path, "r", encoding="utf-8") as f:
        config = _yaml.safe_load(f)
    if agent_key not in config:
        config[agent_key] = {}
    for field in ["model", "temperature", "max_tokens", "max_retry_limit",
                   "max_review_iterations", "review_temperature", "timeout_seconds"]:
        if field in body:
            config[agent_key][field] = body[field]
    with open(config_path, "w", encoding="utf-8") as f:
        _yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return JSONResponse({"ok": True})


def _load_instructions() -> dict:
    """Load agent instructions JSON. Returns dict with _presets and per-agent settings."""
    path = PROJECT_ROOT / "config" / "agent_instructions.json"
    if not path.exists():
        return {"_presets": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_instructions(data: dict) -> None:
    path = PROJECT_ROOT / "config" / "agent_instructions.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.get("/api/agent_instructions/{agent_key}")
def api_agent_instructions_get(agent_key: str, defaults: str = "", custom: str = "") -> JSONResponse:
    """Get instruction settings + available presets for an agent.

    If defaults=1, return the stored defaults instead of current settings.
    If custom=1, return the saved custom settings (user's AI-adjusted config).
    """
    data = _load_instructions()
    presets = data.get("_presets", {}).get(agent_key, [])
    if defaults == "1":
        settings = data.get("_defaults", {}).get(agent_key, {})
    else:
        settings = data.get(agent_key, {})
    return JSONResponse({"settings": settings, "presets": presets})


@app.post("/api/agent_instructions/{agent_key}")
async def api_agent_instructions_save(agent_key: str, request: Request) -> JSONResponse:
    """Save instruction settings for an agent.

    Body: { settings: {...} }
    """
    body = await request.json()
    settings = body.get("settings", body)  # backward compat: accept raw settings

    # --- Guardrail: validate custom field ---
    custom_text = settings.get("custom", "")
    guard_err = _validate_custom_instruction(custom_text)
    if guard_err:
        return guard_err
        # UI-controlled settings — reject if user tries to set via Instructions
        ui_conflict_patterns = [
            (r"สร้าง\s*\d+\s*โพสต์", "จำนวนโพสต์ตั้งในกล่องเลือกจำนวนด้านล่าง ไม่ใช่ใน Instructions"),
            (r"\d+\s*โพสต์", "จำนวนโพสต์ตั้งในกล่องเลือกจำนวนด้านล่าง ไม่ใช่ใน Instructions"),
            (r"แพลตฟอร์ม\s*(facebook|tiktok|ig|instagram)", "แพลตฟอร์มตั้งในกล่องเลือกด้านล่าง ไม่ใช่ใน Instructions"),
            (r"โพสต์\s*(facebook|tiktok|ig|instagram)", "แพลตฟอร์มตั้งในกล่องเลือกด้านล่าง ไม่ใช่ใน Instructions"),
            (r"สร้าง\s*(รูป|วิดีโอ|video|image)", "สื่อ (รูป/วิดีโอ) ตั้งในกล่องเลือกสื่อด้านล่าง ไม่ใช่ใน Instructions"),
        ]
        for pattern, msg in ui_conflict_patterns:
            if _re.search(pattern, custom_text, _re.IGNORECASE):
                return JSONResponse({"error": msg})

    data = _load_instructions()
    data[agent_key] = settings
    _save_instructions(data)
    # Refresh conflict cache — agent settings เปลี่ยน
    _refresh_conflict_cache()
    return JSONResponse({"ok": True})


@app.get("/api/conflicts")
def api_conflicts_all() -> JSONResponse:
    """คืน conflict cache ทุก agent — ใช้สำหรับ agent cards, brand page, etc.

    ตรวจครั้งเดียวตอน brand/agent settings เปลี่ยน (ดู _refresh_conflict_cache)
    ไม่ตรวจใหม่ทุกครั้ง — ลด cost + ทุกหน้าเห็นผลเดียวกัน
    """
    return JSONResponse({
        agent_key: {"conflicts": conflicts, "has_conflict": len(conflicts) > 0}
        for agent_key, conflicts in _conflict_cache.items()
    })


@app.post("/api/conflicts/check")
async def api_conflicts_check_live(request: Request) -> JSONResponse:
    """ตรวจ conflict จาก current (unsaved) settings — live detection.

    ใช้สำหรับ: Quick Brief (per-run), agent settings ก่อน save
    รับ body: { settings: {...} }
    คืน { conflicts: [...], has_conflict: bool }
    """
    from src.brand_priority import load_brand_priority, detect_conflicts
    body = await request.json()
    settings = body.get("settings", body)
    rules = load_brand_priority(BRAND_DIR)
    conflicts = detect_conflicts(rules, settings)
    return JSONResponse({
        "conflicts": [
            {
                "field": c.field,
                "brand_value": c.brand_value,
                "user_value": c.user_value,
                "severity": c.severity,
            }
            for c in conflicts
        ],
        "has_conflict": len(conflicts) > 0,
    })



@app.get("/api/sessions")
def api_sessions() -> JSONResponse:
    return JSONResponse(_scan_sessions())


@app.get("/api/session_files/{session}")
def api_session_files(session: str) -> JSONResponse:
    """List files in a single session — ใช้หาภาพประกอบที่เกี่ยวข้องกับ content_creator."""
    session_dir = OUTPUT_DIR / session
    if not session_dir.exists() or not session_dir.is_dir():
        return JSONResponse([])
    files = []
    for f in sorted(session_dir.iterdir()):
        if f.is_file() and not f.name.startswith(".") and f.name != ".DS_Store":
            files.append({"name": f.name})
    return JSONResponse(files)


@app.get("/api/content_history/{folder}")
def api_content_history_for_product(folder: str, limit: int = 20) -> JSONResponse:
    """Return content history for a specific product — ดูว่าสินค้านี้เคยทำอะไรไปแล้ว."""
    entries = content_history.get_entries_for_product(PROJECT_ROOT, folder, limit=limit)
    return JSONResponse({"entries": entries})


@app.get("/api/file/{session}/{filename:path}")
def api_file(session: str, filename: str):
    from fastapi.responses import Response
    filepath = OUTPUT_DIR / session / filename
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"content": "ไม่พบไฟล์"})
    # Binary files (images, videos) — serve raw bytes
    suffix = filepath.suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm", ".mov"):
        media_types = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp",
            ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
        }
        return Response(
            content=filepath.read_bytes(),
            media_type=media_types.get(suffix, "application/octet-stream"),
        )
    return JSONResponse({"content": filepath.read_text(encoding="utf-8")})


@app.post("/api/cancel")
async def api_cancel() -> JSONResponse:
    global _cancel_requested, _active_llms
    _cancel_requested = True
    # Abort all active LLM clients across parallel runs
    for llm in _active_llms:
        try:
            llm.abort()
        except Exception:
            pass
    _active_llms.clear()
    return JSONResponse({"ok": True})


@app.post("/api/run_agent")
async def api_run_agent(request: Request) -> StreamingResponse:
    """Run a single agent with a product folder via SSE.

    User เลือก context เอง (use_competitor, use_campaign) และจำนวนชุด (content_count)
    ไม่บังคับ dependency อีกต่อไป
    """
    body = await request.json()
    agent_key = body.get("agent", "")
    folder = body.get("folder", "")
    context = body.get("context", {"use_competitor": True, "use_campaign": True})
    content_count = max(1, min(int(body.get("content_count", 1)), 20))  # จำกัด 1-20 ชุด
    quick_brief = body.get("quick_brief", "")
    auto_image = body.get("auto_image", None)
    auto_video = body.get("auto_video", None)
    platforms = body.get("platforms", ["facebook", "tiktok"])
    media_type = body.get("media_type", "")

    if not agent_key or not folder:
        return JSONResponse({"error": "missing agent or folder"})

    global _cancel_requested, _session_ts
    _cancel_requested = False
    _session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    async def event_stream():
        global _current_llm
        orch = _get_orchestrator()
        q: _queue.Queue[str | None] = _queue.Queue()

        def worker():
            global _current_llm
            try:
                output_dir = OUTPUT_DIR / _session_ts
                output_dir.mkdir(parents=True, exist_ok=True)

                # บันทึก session metadata — สำหรับหารูปสินค้าจริงตอน generate media
                try:
                    meta_path = output_dir / "_session_meta.json"
                    meta_path.write_text(json.dumps({
                        "product_id": folder,
                        "created_at": datetime.now().isoformat(),
                    }, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass

                if _current_llm is None:
                    _current_llm = orch.make_client()
                llm = _current_llm

                if _cancel_requested:
                    q.put_nowait(_sse("status", "หยุดการทำงานแล้ว"))
                else:
                    agent_name = AGENT_INFO.get(agent_key, {}).get("name", agent_key)
                    set_label = f" ({content_count} โพสต์)" if content_count > 1 else ""
                    q.put_nowait(_sse("agent_start", f"{agent_name} — {folder}{set_label}", agent=agent_key))

                    raw_contents, image_paths, ready_contents = _read_folder(folder)

                    try:
                        def _status_cb(msg, _ak=agent_key):
                            q.put_nowait(_sse("status", msg, agent=_ak))
                        results = _run_single_agent(
                            agent_key, folder, raw_contents, image_paths,
                            ready_contents, orch, llm, output_dir,
                            quick_brief=quick_brief, context=context, content_count=content_count,
                            auto_image=auto_image, auto_video=auto_video, platforms=platforms,
                            media_type=media_type,
                            status_callback=_status_cb,
                        )
                        for i, (result, filepath) in enumerate(results):
                            set_num = i + 1 if len(results) > 1 else None
                            _disp_len = int(_sys_cfg().get("display_preview_length", 500))
                            q.put_nowait(_sse("agent_done", result[:_disp_len], agent=agent_key, file=filepath, set_num=set_num, total_sets=len(results)))
                    except Exception as e:
                        if _cancel_requested:
                            q.put_nowait(_sse("status", "หยุดการทำงานแล้ว"))
                        else:
                            q.put_nowait(_sse("error", str(e), agent=agent_key))

                if _current_llm:
                    _current_llm.close()
                    _current_llm = None
                q.put_nowait(_sse("done", ""))
                q.put_nowait(None)

            except Exception as e:
                if _cancel_requested:
                    q.put_nowait(_sse("status", "หยุดการทำงานแล้ว"))
                    q.put_nowait(_sse("done", ""))
                    q.put_nowait(None)
                    return
                q.put_nowait(_sse("error", str(e)))
                q.put_nowait(_sse("done", ""))
                q.put_nowait(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        while True:
            try:
                event = q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if event is None:
                break
            yield event

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _read_folder(folder: str) -> tuple[list[str], list[str], dict[str, str]]:
    """Read product folder → (raw_contents, image_paths, ready_contents).

    Raw files come from data/{folder}/ (user-uploaded).
    Ready files come from cache/{folder}/ (system-generated, kept separate).
    """
    product_dir = DATA_DIR / folder
    cache_dir = CACHE_DIR / folder
    raw_contents = []
    image_paths = []
    ready_contents = {}
    # Read user files from data/
    if product_dir.exists() and product_dir.is_dir():
        for f in sorted(product_dir.rglob("*")):
            if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                continue
            ext = f.suffix.lower()
            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                image_paths.append(str(f))
            elif ext in {".txt", ".md", ".pdf", ".xlsx", ".xls", ".docx", ".csv"}:
                raw_contents.append(load_file(str(f)))
    # Read system-generated files from cache/
    if cache_dir.exists() and cache_dir.is_dir():
        for f in sorted(cache_dir.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                continue
            ext = f.suffix.lower()
            if ext in {".txt", ".md"}:
                ready_contents[f.name] = f.read_text(encoding="utf-8")
    return raw_contents, image_paths, ready_contents


def _read_deliverable(folder: str, name_keyword: str) -> str:
    """อ่าน deliverable จาก cache/{folder}/ ที่ชื่อมี keyword นี้ — คืน "" ถ้าไม่มี."""
    cache_dir = CACHE_DIR / folder
    if not cache_dir.exists():
        return ""
    for f in sorted(cache_dir.iterdir()):
        if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
            continue
        if name_keyword in f.name.lower():
            return f.read_text(encoding="utf-8")
    return ""


def _check_injection(text: str) -> str | None:
    """ตรวจ prompt injection ในข้อความ — คืน error message ถ้าพบ หรือ None ถ้าปลอดภัย."""
    import re as _re
    injection_patterns = [
        r"ignore\s+(all\s+)?previous\s+instructions?",
        r"you\s+are\s+now\s+(in\s+)?developer\s+mode",
        r"system\s+override",
        r"reveal\s+(your\s+)?(system\s+)?prompt",
        r"forget\s+(all\s+)?(previous\s+)?(instructions|rules)",
        r"disregard\s+(all\s+)?(previous\s+)?instructions",
        r"act\s+as\s+(if\s+)?(you\s+are|a)\s+(jailbreak|unrestricted|dan)",
    ]
    for pattern in injection_patterns:
        if _re.search(pattern, text, _re.IGNORECASE):
            return "มีคำที่ไม่อนุญาต กรุณาเขียนเกี่ยวกับงานเท่านั้น"
    return None


def _validate_quick_brief(quick_brief: str) -> JSONResponse | None:
    """Validate quick_brief length and injection. คืน JSONResponse error ถ้าผิด หรือ None ถ้าผ่าน."""
    MAX_BRIEF_LEN = 2000
    if quick_brief and len(quick_brief) > MAX_BRIEF_LEN:
        return JSONResponse({"error": f"Quick Brief ยาวเกินไป (สูงสุด {MAX_BRIEF_LEN} ตัวอักษร)"})
    if quick_brief:
        err = _check_injection(quick_brief)
        if err:
            return JSONResponse({"error": f"Quick Brief {err}"})
    return None


def _validate_custom_instruction(custom_text: str) -> JSONResponse | None:
    """Validate custom instruction length and injection. คืน JSONResponse error ถ้าผิด หรือ None ถ้าผ่าน."""
    MAX_CUSTOM_LEN = 2000
    if custom_text and len(custom_text) > MAX_CUSTOM_LEN:
        return JSONResponse({"error": f"Instructions ยาวเกินไป (สูงสุด {MAX_CUSTOM_LEN} ตัวอักษร)"})
    if custom_text:
        err = _check_injection(custom_text)
        if err:
            return JSONResponse({"error": f"Instructions {err}"})
    return None


def _clamp_content_count(n: int) -> int:
    """จำกัดจำนวน content ระหว่าง 1-20."""
    return max(1, min(int(n), 20))


def _session_ts_label(folders: list[str], now: datetime | None = None) -> str:
    """สร้างชื่อ session folder จากวันที่ไทย + ชื่อสินค้า."""
    thai_months = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                   "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    now = now or datetime.now()
    date_str = f"{now.day}_{thai_months[now.month-1]}_{now.year+543}_{now.strftime('%H.%M')}"
    safe_folders = [f.replace("/", "-")[:int(_sys_cfg().get("filename_max_length", 30))] for f in folders]
    return f"{date_str} - {' + '.join(safe_folders)}"


def _write_to_cache(folder: str, name: str, content: str) -> Path:
    """เขียน deliverable ลง cache/{folder}/{name}.md เพื่อ downstream agent อ่านต่อ."""
    cache_dir = CACHE_DIR / folder
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _run_single_agent(agent_key: str, folder: str, raw_contents: list[str],
                      image_paths: list[str], ready_contents: dict[str, str],
                      orch: Orchestrator, llm, output_dir: Path,
                      save_output: bool = True, quick_brief: str = "",
                      context: dict | None = None, content_count: int = 1,
                      auto_image: bool | None = None, auto_video: bool | None = None,
                      platforms: list[str] | None = None,
                      media_type: str = "",
                      status_callback=None) -> list[tuple[str, str | None]]:
    """Run one agent, return list of (result_text, filepath) tuples.

    context: {use_competitor: bool, use_campaign: bool} — user เลือกว่าจะใช้ context อะไร
    content_count: จำนวนชุดสำหรับ content_creator (loop พร้อมส่งผลก่อนหน้าเข้า prompt)

    NOTE: orch must be a fresh instance per call — do NOT share across parallel runs
    because product_id/results/product_images are mutable state.
    """
    if context is None:
        context = {"use_competitor": True, "use_campaign": True}

    # Set product_id for ALL agents so _save_to_ready and save_result use the correct folder
    orch.product_id = folder
    orch.product_images = image_paths

    if agent_key == "product_spec":
        raw_data = "\n\n".join(raw_contents) if raw_contents else ""
        # ถ้าไม่มี text แต่มีรูป → ใช้รูปเป็นข้อมูลหลัก (agent เห็นรูปจริงผ่าน multimodal)
        if not raw_data and not image_paths:
            raise ValueError(f"ไม่พบข้อมูลในโฟลเดอร์ {folder} — ต้องมีไฟล์ text หรือรูปอย่างน้อย 1 ไฟล์")
        result = orch.run_product_spec(raw_data, image_paths, llm=llm, quick_brief=quick_brief)
        orch.results["product_spec"] = result
        if save_output:
            saved = orch.save_result("product_spec", str(output_dir))
            return [(result, str(saved.get("product_spec", "")))]
        return [(result, None)]

    elif agent_key == "competitor_analysis":
        # ดึงข้อมูลสินค้าจาก DB (orchestrator จัดการเอง) — ไม่ต้องมี product_spec.txt ใน cache/
        result = orch.run_competitor_analysis("", None, llm=llm, quick_brief=quick_brief)
        orch.results["competitor_analysis"] = result
        if save_output:
            saved = orch.save_result("competitor_analysis", str(output_dir))
            _write_to_cache(folder, "competitor_analysis", result)
            return [(result, str(saved.get("competitor_analysis", "")))]
        return [(result, None)]

    elif agent_key == "campaign_strategy":
        # ใช้ competitor_analysis จากผลลัพธ์ก่อนหน้าใน flow ถ้ามี
        # fallback: อ่านจาก cache/ (legacy) ถ้าไม่มีใน context แต่ user เลือก use_competitor
        if "competitor_analysis" in context:
            analysis_text = context["competitor_analysis"]
        elif context.get("use_competitor"):
            analysis_text = _read_deliverable(folder, "competitor")
        else:
            analysis_text = ""
        result = orch.run_campaign_strategy("", analysis_text, llm=llm, quick_brief=quick_brief)
        orch.results["campaign_strategy"] = result
        if save_output:
            saved = orch.save_result("campaign_strategy", str(output_dir))
            _write_to_cache(folder, "campaign_strategy", result)
            return [(result, str(saved.get("campaign_strategy", "")))]
        return [(result, None)]

    elif agent_key == "content_creator":
      with _content_creator_lock:
        # ใช้ผลลัพธ์ก่อนหน้าใน flow ถ้ามี หรืออ่านจาก cache/ (legacy)
        if "competitor_analysis" in context:
            analysis_text = context["competitor_analysis"]
        elif context.get("use_competitor"):
            analysis_text = _read_deliverable(folder, "competitor")
        else:
            analysis_text = ""
        if "campaign_strategy" in context:
            campaign_text = context["campaign_strategy"]
        elif context.get("use_campaign"):
            campaign_text = _read_deliverable(folder, "campaign")
        else:
            campaign_text = ""

        # auto media — จาก parameter เท่านั้น
        # None = ไม่ได้ส่งมา = ถือเป็น False (ห้ามอ่าน config เป็น default
        # เพราะ config อาจเป็น true แล้ว trigger auto-gen โดยไม่ได้ตั้งใจ)
        if auto_image is None:
            auto_image = False
        if auto_video is None:
            auto_video = False

        # --- Content History: บอก AI "ห้ามซ้ำมุมมองเดิม" ---
        # ใช้ content_history รวม (manual + auto) แทน angle_manager แยก
        _product_history_text = content_history.format_product_history_for_prompt(PROJECT_ROOT, folder)

        results: list[tuple[str, str | None]] = []
        # เก็บข้อมูล post ที่ทำเสร็จ เพื่อบันทึกลง history ทีหลัง
        completed_posts: list[dict] = []

        # --- วนลูปตามแพลตฟอร์ม (เหมือน auto mode) ---
        # content_count = จำนวนโพสต์ต่อแพลตฟอร์ม × จำนวนแพลตฟอร์ม
        # แต่ละแพลตฟอร์มสร้าง count_per_platform โพสต์ แล้วรวมเป็น 1 ไฟล์
        platform_names = {"facebook": "Facebook", "tiktok": "TikTok"}
        target_platforms = platforms if platforms else [""]
        # ceiling division — ถ้า content_count=3, platforms=2 → 2 โพสต์/แพลตฟอร์ม (รวม 4)
        # ไม่ใช่ floor (รวม 2) เพราะ user ขอ 3 โพสต์ ต้องไม่หาย
        _n_plat = max(1, len(target_platforms))
        count_per_platform = max(1, (content_count + _n_plat - 1) // _n_plat)

        all_posts: list[dict] = []

        for platform in target_platforms:
            platform_label = platform_names.get(platform, platform) if platform else ""
            for post_idx in range(count_per_platform):
                if _cancel_requested:
                    break

                # สร้าง brief พิเศษสำหรับหลายโพสต์
                multi_brief = quick_brief
                if count_per_platform > 1:
                    multi_brief = f"โพสต์ที่ {post_idx+1} จาก {count_per_platform} โพสต์ — สร้างคอนเทนต์ที่แตกต่างจากโพสต์ก่อนหน้า"
                    if all_posts:
                        multi_brief += "\n\n--- คอนเทนต์ที่สร้างไปแล้ว (ห้ามซ้ำ) ---\n"
                        _long_len = int(_sys_cfg().get("display_preview_long", 800))
                        for j, p in enumerate(all_posts):
                            multi_brief += f"\nโพสต์ที่ {j+1}:\n{json.dumps(p, ensure_ascii=False)[:_long_len]}\n"
                        multi_brief += "--- สิ้นสุด ---\n"
                        multi_brief += "สร้างโพสต์ใหม่ที่มีมุมมอง/concept ต่างจากโพสต์ก่อนหน้า"
                    if quick_brief:
                        multi_brief += f"\n\nคำขอเพิ่มเติมจาก user: {quick_brief}"

                # บอก AI ถึงมุมมองที่เคยใช้แล้ว (ห้ามซ้ำ)
                if _product_history_text:
                    multi_brief = (multi_brief or "") + "\n\n" + _product_history_text + "\n"
                    multi_brief += "วิเคราะห์สินค้านี้แล้วเลือกมุมมองใหม่ที่ต่างจากที่เคยใช้ แล้วสร้างโพสต์จากมุมมองนั้น"
                else:
                    multi_brief = (multi_brief or "") + "\n\nวิเคราะห์สินค้านี้แล้วเลือกมุมมองที่เหมาะสมที่สุด แล้วสร้างโพสต์จากมุมมองนั้น"

                # inject platform
                if platform_label:
                    multi_brief = (multi_brief or "") + f"\nแพลตฟอร์มที่ต้องสร้างสำหรับโพสต์นี้: {platform_label} เท่านั้น"

                result = orch.run_content_creator(
                    "", analysis_text, campaign_text,
                    llm=llm, quick_brief=multi_brief,
                    media_type=media_type,
                )

                # parse + script review (เหมือน auto mode)
                try:
                    parsed_content = json.loads(result)
                    posts = parsed_content.get("posts", [])
                    if posts and platform_label:
                        posts[0]["platform"] = platform_label

                    # script review
                    ch_cfg = content_history._DEFAULTS if hasattr(content_history, '_DEFAULTS') else {}
                    orch._review_script_in_posts(
                        posts, platform_label, llm, status_callback, ch_cfg,
                    )

                    if posts:
                        all_posts.append(posts[0])
                        completed_posts.append({
                            "concept": posts[0].get("concept", posts[0].get("angle", "")),
                            "platform": posts[0].get("platform", ""),
                            "caption": posts[0].get("caption", "")[:int(_sys_cfg().get("caption_display_length", 500))],
                        })
                except (json.JSONDecodeError, TypeError):
                    pass

        # รวมผลลัพธ์ทุกแพลตฟอร์มเป็น 1 ไฟล์ (เหมือน auto mode)
        combined = {"posts": all_posts}
        combined_json = json.dumps(combined, ensure_ascii=False, indent=2)
        try:
            from src.content_schema import render_posts_to_markdown
            combined_md = render_posts_to_markdown(combined)
        except Exception:
            combined_md = combined_json

        orch.results["content_creator"] = combined_json
        orch.results["content_creator_markdown"] = combined_md

        saved_path: str | None = None
        if save_output:
            timestamp = datetime.now().strftime("%H%M%S")
            fname_base = f"04_content_creator_{folder}_{timestamp}"
            json_path = output_dir / f"{fname_base}.json"
            json_path.write_text(combined_json, encoding="utf-8")
            md_filepath = output_dir / f"{fname_base}.md"
            md_filepath.write_text(combined_md, encoding="utf-8")
            saved_path = str(md_filepath)
            results.append((combined_json, saved_path))
            for _p in completed_posts:
                _p["output_file"] = saved_path or ""
        else:
            results.append((combined_json, None))

        # Auto-generate media ถ้าเปิด auto
        if save_output and saved_path and (auto_image or auto_video):
            try:
                parsed = media_gen.parse_media_prompts(combined_json)
                from src import asset_library as _al
                retry_llm = llm if llm is not None else None
                if retry_llm is None:
                    try:
                        retry_llm = orch.make_client()
                    except Exception:
                        retry_llm = None
                if auto_image:
                    for j, img in enumerate(parsed.get("images", [])):
                        img_path = output_dir / f"image_{j+1}.png"
                        if status_callback:
                            status_callback(f"กำลังสร้างรูปที่ {j+1}...")
                        img_kwargs: dict = {}
                        if img.get("aspect_ratio"):
                            img_kwargs["aspect_ratio"] = img["aspect_ratio"]
                        _refs = _al.build_input_references(
                            image_paths, img.get("asset_ids", []),
                        )
                        if _refs:
                            img_kwargs["input_references"] = _refs
                        visual = _get_brand_visual()
                        if visual:
                            img_kwargs["visual"] = visual
                        def _img_retry(old_p, new_p, err, idx=j):
                            if status_callback:
                                status_callback(f"รูปที่ {idx+1}: ถูกปฏิเสธ กำลังแก้ prompt แล้วลองใหม่...")
                            print(f"[MediaGen] retry รูป {idx+1}: {err[:int(_sys_cfg().get('error_preview_length', 200))]}", flush=True)
                        r = media_gen.generate_image_with_retry(
                            img["prompt"], img_path, llm=retry_llm,
                            on_retry=_img_retry, **img_kwargs,
                        )
                        media_gen.save_retry_history(
                            output_dir, "image", img_path.name, r,
                        )
                        if not r.get("ok"):
                            msg = f"รูปที่ {j+1}: {r.get('error', 'unknown')}"
                            if status_callback:
                                status_callback(msg)
                            print(f"[MediaGen] {msg}", flush=True)
                        elif r.get("retry_count"):
                            if status_callback:
                                status_callback(f"รูปที่ {j+1}: สร้างสำเร็จหลังแก้ prompt {r['retry_count']} ครั้ง")
                if auto_video:
                    for j, vid in enumerate(parsed.get("videos", [])):
                        vid_path = output_dir / f"video_{j+1}.mp4"
                        def _vid_status(s, idx=j):
                            if status_callback:
                                status_callback(f"วิดีโอที่ {idx+1}: {s}")
                        vid_kwargs: dict = {"on_status": _vid_status}
                        if vid.get("duration"):
                            vid_kwargs["duration"] = int(vid["duration"])
                        if vid.get("aspect_ratio"):
                            vid_kwargs["aspect_ratio"] = vid["aspect_ratio"]
                        if vid.get("resolution"):
                            vid_kwargs["resolution"] = vid["resolution"]
                        _refs = _al.build_input_references(
                            image_paths, vid.get("asset_ids", []),
                        )
                        if _refs:
                            vid_kwargs["input_references"] = _refs
                        visual = _get_brand_visual()
                        if visual:
                            vid_kwargs["visual"] = visual
                        def _vid_retry(old_p, new_p, err, idx=j):
                            if status_callback:
                                status_callback(f"วิดีโอที่ {idx+1}: ถูกปฏิเสธ กำลังแก้ prompt แล้วลองใหม่...")
                            print(f"[MediaGen] retry วิดีโอ {idx+1}: {err[:int(_sys_cfg().get('error_preview_length', 200))]}", flush=True)
                        vid_kwargs["on_retry"] = _vid_retry
                        r = media_gen.generate_video_with_retry(
                            vid["prompt"], vid_path, llm=retry_llm, **vid_kwargs,
                        )
                        media_gen.save_retry_history(
                            output_dir, "video", vid_path.name, r,
                        )
                        if not r.get("ok"):
                            msg = f"วิดีโอที่ {j+1}: {r.get('error', 'unknown')}"
                            if status_callback:
                                status_callback(msg)
                            print(f"[MediaGen] {msg}", flush=True)
                        elif r.get("retry_count"):
                            if status_callback:
                                status_callback(f"วิดีโอที่ {j+1}: สร้างสำเร็จหลังแก้ prompt {r['retry_count']} ครั้ง")
            except Exception as e:
                msg = f"media gen error: {e}"
                if status_callback:
                    status_callback(msg)
                print(f"[MediaGen] {msg}", flush=True)

        # บันทึกประวัติคอนเทนต์ที่ทำเสร็จ ลง content_history (รวม manual + auto)
        for _post in completed_posts:
            if _post.get("concept"):
                content_history.record_entry(
                    PROJECT_ROOT,
                    product_ids=folder,
                    concept=_post.get("concept", ""),
                    platform=_post.get("platform", ""),
                    caption_summary=_post.get("caption", ""),
                    output_file=_post.get("output_file", ""),
                )

        return results

      # end of _content_creator_lock context

    raise ValueError(f"ไม่รู้จัก agent: {agent_key}")


@app.post("/api/run_agents")
async def api_run_agents(request: Request) -> StreamingResponse:
    """Run multiple agents with dependency ordering via SSE."""
    body = await request.json()
    agents = body.get("agents", [])
    folders = body.get("folders", [])
    mode = body.get("mode", "separate")
    quick_brief = body.get("quick_brief", "")

    # --- Quick Brief input validation (guardrail) ---
    guard_err = _validate_quick_brief(quick_brief)
    if guard_err:
        return guard_err

    # Backward compat: single folder string
    if not folders and body.get("folder"):
        folders = [body.get("folder")]

    if not agents or not folders:
        return JSONResponse({"error": "missing agents or folders"})

    # User เลือก context เอง — ไม่บังคับ dependency อีกต่อไป
    context = body.get("context", {"use_competitor": True, "use_campaign": True})
    content_count = _clamp_content_count(body.get("content_count", 1))  # จำกัด 1-20 โพสต์
    # auto media — จาก dropdown ใน content_creator box
    auto_image = body.get("auto_image", None)
    auto_video = body.get("auto_video", None)
    # platform — จาก dropdown ใน content_creator box (facebook / tiktok)
    platforms = body.get("platforms", ["facebook", "tiktok"])
    # media settings — สร้างอะไร + เมื่อไหร่
    media_type = body.get("media_type", "image")
    media_when = body.get("media_when", "ask")

    # Sort agents by order (no auto-add — user เลือกเองว่าจะรันอะไร)
    sorted_agents = [a for a in AGENT_ORDER if a in agents]

    global _cancel_requested
    _cancel_requested = False
    # Use readable folder name: Thai date + product names (local, not global — parallel-safe)
    session_ts = _session_ts_label(folders)

    async def event_stream():
        # Fresh orchestrator per call — do NOT share across parallel runs
        # (product_id/results/product_images are mutable state)
        orch = Orchestrator(brand_dir="brand")
        q: _queue.Queue[str | None] = _queue.Queue()

        def worker():
            # Local LLM client — register in _active_llms for cancel
            global _active_llms
            llm = None
            try:
                output_dir = OUTPUT_DIR / session_ts
                output_dir.mkdir(parents=True, exist_ok=True)

                # บันทึก session metadata — สำหรับหารูปสินค้าจริงตอน generate media
                try:
                    meta_path = output_dir / "_session_meta.json"
                    meta_path.write_text(json.dumps({
                        "product_id": folders[0] if len(folders) == 1 else " + ".join(folders),
                        "product_ids": folders,
                        "created_at": datetime.now().isoformat(),
                    }, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass

                llm = orch.make_client()
                _active_llms.append(llm)

                if mode == "combined" and len(folders) >= 2:
                    folder_label = " + ".join(folders)
                    all_raw = []
                    all_images = []
                    all_ready = {}
                    for folder in folders:
                        raw_contents, image_paths, ready_contents = _read_folder(folder)
                        all_raw.extend(raw_contents)
                        all_images.extend(image_paths)
                        for fname, content in ready_contents.items():
                            all_ready[f"{folder}/{fname}"] = content

                    for agent_key in sorted_agents:
                        if _cancel_requested:
                            q.put_nowait(_sse("status", "หยุดการทำงานแล้ว"))
                            break

                        agent_name = AGENT_INFO.get(agent_key, {}).get("name", agent_key)
                        set_label = f" ({content_count} โพสต์)" if agent_key == "content_creator" and content_count > 1 else ""
                        q.put_nowait(_sse("agent_start", f"{agent_name} — {folder_label} (รวม){set_label}", agent=agent_key))

                        try:
                            def _status_cb_combined(msg, _ak=agent_key):
                                q.put_nowait(_sse("status", msg, agent=_ak))
                            results = _run_single_agent(
                                agent_key, folder_label, all_raw, all_images,
                                all_ready, orch, llm, output_dir,
                                save_output=True, quick_brief=quick_brief,
                                context=context, content_count=content_count,
                                auto_image=auto_image, auto_video=auto_video,
                                platforms=platforms, media_type=media_type,
                                status_callback=_status_cb_combined,
                            )
                            for i, (result, filepath) in enumerate(results):
                                set_num = i + 1 if len(results) > 1 else None
                                _dl = int(_sys_cfg().get("display_preview_length", 500))
                                q.put_nowait(_sse("agent_done", result[:_dl], agent=agent_key, file=filepath, set_num=set_num, total_sets=len(results)))
                        except Exception as e:
                            if _cancel_requested:
                                q.put_nowait(_sse("status", "หยุดการทำงานแล้ว"))
                                break
                            q.put_nowait(_sse("error", str(e), agent=agent_key))
                else:
                    for folder in folders:
                        for agent_key in sorted_agents:
                            if _cancel_requested:
                                q.put_nowait(_sse("status", "หยุดการทำงานแล้ว"))
                                break

                            agent_name = AGENT_INFO.get(agent_key, {}).get("name", agent_key)
                            set_label = f" ({content_count} โพสต์)" if agent_key == "content_creator" and content_count > 1 else ""
                            q.put_nowait(_sse("agent_start", f"{agent_name} — {folder}{set_label}", agent=agent_key))

                            raw_contents, image_paths, ready_contents = _read_folder(folder)

                            try:
                                def _status_cb_sep(msg, _ak=agent_key):
                                    q.put_nowait(_sse("status", msg, agent=_ak))
                                results = _run_single_agent(
                                    agent_key, folder, raw_contents, image_paths,
                                    ready_contents, orch, llm, output_dir,
                                    save_output=True, quick_brief=quick_brief,
                                    context=context, content_count=content_count,
                                    auto_image=auto_image, auto_video=auto_video,
                                    platforms=platforms, media_type=media_type,
                                    status_callback=_status_cb_sep,
                                )
                                for i, (result, filepath) in enumerate(results):
                                    set_num = i + 1 if len(results) > 1 else None
                                    _dl = int(_sys_cfg().get("display_preview_length", 500))
                                    q.put_nowait(_sse("agent_done", result[:_dl], agent=agent_key, file=filepath, set_num=set_num, total_sets=len(results)))
                            except Exception as e:
                                if _cancel_requested:
                                    q.put_nowait(_sse("status", "หยุดการทำงานแล้ว"))
                                    break
                                q.put_nowait(_sse("error", str(e), agent=agent_key))

                if llm:
                    llm.close()
                    try:
                        _active_llms.remove(llm)
                    except ValueError:
                        pass
                q.put_nowait(_sse("done", ""))
                q.put_nowait(None)

            except Exception as e:
                if llm:
                    try:
                        llm.close()
                    except Exception:
                        pass
                    try:
                        _active_llms.remove(llm)
                    except ValueError:
                        pass
                q.put_nowait(_sse("error", str(e)))
                q.put_nowait(_sse("done", ""))
                q.put_nowait(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        while True:
            try:
                event = q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if event is None:
                break
            yield event

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/run_flows")
async def api_run_flows(request: Request) -> StreamingResponse:
    """Run flows in parallel. Within each flow agents run sequentially and pass context forward.

    Body:
    {
        "flows": [
            {
                "folders": ["Product A", "Product B"],
                "agents": ["product_spec", "competitor_analysis", "content_creator"],
                "content_count": 1,
                "platforms": ["facebook", "tiktok"],
                "media_type": "image",
                "media_when": "ask",
                "auto_image": false,
                "auto_video": false
            }
        ],
        "quick_brief": ""
    }
    """
    body = await request.json()
    flows = body.get("flows", [])
    quick_brief = body.get("quick_brief", "")

    guard_err = _validate_quick_brief(quick_brief)
    if guard_err:
        return guard_err

    if not flows:
        return JSONResponse({"error": "missing flows"})

    # Validate flows
    for flow in flows:
        if not flow.get("folders") or not flow.get("agents"):
            return JSONResponse({"error": "แต่ละ flow ต้องมีสินค้าและ agent"})

    global _cancel_requested
    _cancel_requested = False

    # Session folder (shared across all flows, like run_agents)
    session_ts = _session_ts_label([])

    async def event_stream():
        q: _queue.Queue[str | None] = _queue.Queue()

        def _flow_worker(flow_idx: int, flow: dict, output_dir: Path):
            from src.flow_runner import run_flow_steps, build_context_for_agent
            plan_idx = flow.get("index", flow_idx)

            # ผูก LLM call ทั้งหมดใน thread นี้เข้ากับ flow_id
            flow_id = f"flow_{uuid.uuid4().hex[:8]}"
            set_flow_id(flow_id)
            flow_output_files: list[str] = []  # เก็บ output file paths ของ flow นี้

            llm = None
            try:
                orch = Orchestrator(brand_dir="brand")
                llm = orch.make_client()
                try:
                    _active_llms.append(llm)
                except NameError:
                    pass

                folders = flow.get("folders", [])
                flow_agents = [a for a in flow.get("agents", []) if a in AGENT_ORDER]
                # TEMP LOCK: แต่ละ flow รัน agent เดียวก่อน จนกว่าจะแก้ให้ agent ทำงานร่วมกันได้
                # (multi-agent code ยังอยู่ แค่ truncate ที่นี่)
                flow_agents = flow_agents[:1]
                content_count = _clamp_content_count(flow.get("content_count", 1))
                platforms = flow.get("platforms", ["facebook", "tiktok"])
                media_type = flow.get("media_type", "image")
                auto_image = flow.get("auto_image", None)
                auto_video = flow.get("auto_video", None)

                is_combined = len(folders) >= 2
                folder_label = " + ".join(folders)

                # Read product data
                if is_combined:
                    all_raw = []
                    all_images = []
                    all_ready = {}
                    for folder in folders:
                        raw_contents, image_paths, ready_contents = _read_folder(folder)
                        all_raw.extend(raw_contents)
                        all_images.extend(image_paths)
                        for fname, content in ready_contents.items():
                            all_ready[f"{folder}/{fname}"] = content
                else:
                    single_folder = folders[0]
                    all_raw, all_images, all_ready = _read_folder(single_folder)

                target_label = folder_label if is_combined else single_folder

                def run_one_agent(agent_key: str, product: str, context: dict) -> str:
                    nonlocal orch, llm, output_dir
                    if _cancel_requested:
                        return ""

                    agent_name = AGENT_INFO.get(agent_key, {}).get("name", agent_key)
                    set_label = f" ({content_count} โพสต์)" if agent_key == "content_creator" and content_count > 1 else ""
                    q.put_nowait(_sse("agent_start", f"{agent_name} — {product}{set_label}", agent=agent_key, plan=plan_idx))

                    try:
                        def _status_cb(msg, _ak=agent_key):
                            q.put_nowait(_sse("status", msg, agent=_ak, plan=plan_idx))

                        results = _run_single_agent(
                            agent_key, product, all_raw, all_images,
                            all_ready, orch, llm, output_dir,
                            save_output=True, quick_brief=quick_brief,
                            context=context, content_count=content_count,
                            auto_image=auto_image, auto_video=auto_video,
                            platforms=platforms, media_type=media_type,
                            status_callback=_status_cb,
                        )
                        result_text = results[0][0] if results else ""
                        file_path = results[0][1] if results else None
                        if file_path:
                            flow_output_files.append(str(file_path))

                        _dl = int(_sys_cfg().get("display_preview_length", 500))
                        q.put_nowait(_sse("agent_done", result_text[:_dl], agent=agent_key, file=file_path, plan=plan_idx))

                        return result_text
                    except Exception as e:
                        if _cancel_requested:
                            q.put_nowait(_sse("status", "หยุดการทำงานแล้ว"))
                            return ""
                        q.put_nowait(_sse("error", str(e), agent=agent_key, plan=plan_idx))
                        return ""

                run_flow_steps(flow_agents, folders, run_one_agent, AGENT_DEPENDENCIES)

                if llm:
                    try:
                        llm.close()
                        _active_llms.remove(llm)
                    except (ValueError, Exception):
                        pass

            except Exception as e:
                q.put_nowait(_sse("error", f"Flow {flow_idx+1}: {e}"))
                if llm:
                    try:
                        llm.close()
                        _active_llms.remove(llm)
                    except (ValueError, Exception):
                        pass
            finally:
                # เขียน cost summary + flow meta สำหรับ flow นี้ (เก็บไว้หลังบ้านสำหรับ dev)
                try:
                    agent_label = " + ".join(flow.get("agents", []))
                    product_label = " + ".join(flow.get("folders", []))
                    write_cost_summary(
                        output_dir, flow_id,
                        label=f"Flow {flow_idx+1}: {agent_label} — {product_label}",
                        agents=flow.get("agents", []),
                        products=product_label,
                    )
                    # เก็บ mapping output_file → flow_id เพื่อให้ "สร้างสื่อภายหลัง" หา flow_id ได้
                    write_flow_meta(
                        output_dir, flow_id, flow_output_files,
                        label=f"Flow {flow_idx+1}: {agent_label} — {product_label}",
                        agents=flow.get("agents", []),
                    )
                except Exception:
                    pass
                clear_flow_id()

        def master_worker():
            try:
                output_dir = OUTPUT_DIR / session_ts
                output_dir.mkdir(parents=True, exist_ok=True)

                threads = []
                for flow_idx, flow in enumerate(flows):
                    t = threading.Thread(target=_flow_worker, args=(flow_idx, flow, output_dir), daemon=True)
                    t.start()
                    threads.append(t)

                for t in threads:
                    t.join()

                q.put_nowait(_sse("done", ""))
                q.put_nowait(None)
            except Exception as e:
                q.put_nowait(_sse("error", str(e)))
                q.put_nowait(_sse("done", ""))
                q.put_nowait(None)

        master_worker_thread = threading.Thread(target=master_worker, daemon=True)
        master_worker_thread.start()

        while True:
            try:
                event = q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if event is None:
                break
            yield event

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ============================================================
# Scheduler — /api/schedule/* endpoints
# ============================================================

_scheduler_instance = None


def _get_scheduler():
    """ดึง scheduler instance — lazy init ถ้ายังไม่มี."""
    global _scheduler_instance
    if _scheduler_instance is None:
        try:
            from src.scheduler import Scheduler
            from src.config_loader import load_config, get_section
            _cfg = load_config()
            _main_sys_cfg = get_section(_cfg, "system", {"web_port": 8778})
            _port = int(os.environ.get("VIEWER_PORT", str(_main_sys_cfg.get("web_port", 8778))))
            _scheduler_instance = Scheduler(project_root=PROJECT_ROOT, web_port=_port)
            _scheduler_instance.start()
        except Exception as e:
            print(f"[Scheduler] init ไม่ได้: {e}", flush=True)
            return None
    return _scheduler_instance


def _attach_schedule_endpoints(app, scheduler):
    @app.get("/api/schedule/jobs")
    def schedule_list_jobs() -> JSONResponse:
        jobs = scheduler.list_jobs()
        return JSONResponse(jobs)

    @app.get("/api/schedule/status")
    def schedule_running_status() -> JSONResponse:
        return JSONResponse(scheduler.get_running_status())

    @app.post("/api/schedule/save")
    async def schedule_save(request: Request) -> JSONResponse:
        body = await request.json()
        job_spec = {
            "name": body.get("name", "unnamed"),
            "enabled": body.get("enabled", True),
            "schedule_type": body.get("schedule_type", "one_time"),
            "schedule": body.get("schedule", {}),
            "flow": body.get("flow", {}),
            "quick_brief": body.get("quick_brief", ""),
        }
        job_id = scheduler.add_job(job_spec)
        return JSONResponse({"job_id": job_id})

    @app.post("/api/schedule/delete")
    async def schedule_delete(request: Request) -> JSONResponse:
        body = await request.json()
        removed = scheduler.remove_job(body.get("job_id", ""))
        return JSONResponse({"ok": removed})

    @app.post("/api/schedule/toggle")
    async def schedule_toggle(request: Request) -> JSONResponse:
        body = await request.json()
        ok = scheduler.toggle_job(body.get("job_id", ""), body.get("enabled", True))
        return JSONResponse({"ok": ok})

    @app.post("/api/schedule/run_now")
    async def schedule_run_now(request: Request) -> JSONResponse:
        body = await request.json()
        ok = scheduler.run_now(body.get("job_id", ""))
        return JSONResponse({"ok": ok})

    @app.post("/api/schedule/rerun")
    async def schedule_rerun(request: Request) -> JSONResponse:
        body = await request.json()
        ok = scheduler.rerun_run(body.get("job_id", ""), body.get("started_at", ""))
        return JSONResponse({"ok": ok})

    @app.get("/api/schedule/runs")
    def schedule_runs(job_id: str = "", limit: int = 50) -> JSONResponse:
        runs = scheduler.get_run_log(job_id=job_id or None, limit=limit)
        return JSONResponse(runs)


class _SchedulerProxy:
    """Proxy สำหรับ lazy scheduler — ส่งต่อไปยัง instance จริงตอนเรียก."""
    def list_jobs(self):
        s = _get_scheduler()
        return s.list_jobs() if s else []

    def add_job(self, spec):
        s = _get_scheduler()
        return s.add_job(spec) if s else ""

    def remove_job(self, jid):
        s = _get_scheduler()
        return s.remove_job(jid) if s else False

    def toggle_job(self, jid, en):
        s = _get_scheduler()
        return s.toggle_job(jid, en) if s else False

    def run_now(self, jid):
        s = _get_scheduler()
        return s.run_now(jid) if s else False

    def rerun_run(self, jid, started_at):
        s = _get_scheduler()
        return s.rerun_run(jid, started_at) if s else False

    def get_run_log(self, **kw):
        s = _get_scheduler()
        return s.get_run_log(**kw) if s else []

    def get_running_status(self):
        s = _get_scheduler()
        return s.get_running_status() if s else {}


_attach_schedule_endpoints(app, _SchedulerProxy())


@app.post("/api/run_auto")
async def api_run_auto(request: Request) -> StreamingResponse:
    """Auto mode — agent เลือกสินค้าเอง + สร้างคอนเทนต์ที่ไม่ซ้ำ.

    ไม่ต้องส่ง folders — agent จะเลือกสินค้าจาก product DB เอง
    ส่ง quick_brief, platforms, media_type, auto_image, auto_video ได้เหมือนเดิม

    SSE events:
      - status: สถานะการทำงาน (เลือกสินค้า → สร้างคอนเทนต์ → สร้างสื่อ)
      - selection: สินค้าที่เลือก + มุมมอง + เหตุผล
      - agent_start: content_creator เริ่มทำงาน
      - agent_done: ผลลัพธ์ (JSON + file path)
      - error: ถ้ามีปัญหา
      - done: จบ
    """
    body = await request.json()
    quick_brief = body.get("quick_brief", "")
    platforms = body.get("platforms", ["facebook", "tiktok"])
    media_type = body.get("media_type", "")
    auto_image = body.get("auto_image", None)
    auto_video = body.get("auto_video", None)
    content_count = max(1, min(int(body.get("content_count", 1)), 20))
    product_count = max(1, min(int(body.get("product_count", 1)), 50))
    # agents ที่ user เลือก — default ["content_creator"] สำหรับ backward compat
    agents = body.get("agents", ["content_creator"])
    # กรองเฉพาะ agent ที่รู้จัก + เรียงตาม AGENT_ORDER
    agents = [a for a in AGENT_ORDER if a in agents]
    # TEMP LOCK: auto flow รัน agent เดียวก่อน จนกว่าจะแก้ให้ agent ทำงานร่วมกันได้
    agents = agents[:1]

    global _cancel_requested
    _cancel_requested = False

    session_ts = _session_ts_label([]).replace(' - ', ' - AUTO - ')

    async def event_stream():
        orch = Orchestrator(brand_dir="brand")
        q: _queue.Queue[str | None] = _queue.Queue()

        def worker():
            global _active_llms
            # ผูก LLM call ทั้งหมดใน thread นี้เข้ากับ flow_id
            flow_id = f"auto_{uuid.uuid4().hex[:8]}"
            set_flow_id(flow_id)
            flow_output_files: list[str] = []  # เก็บ output file paths ของ flow นี้
            llm = None
            try:
                output_dir = OUTPUT_DIR / session_ts
                output_dir.mkdir(parents=True, exist_ok=True)

                llm = orch.make_client()
                _active_llms.append(llm)

                def _status_cb(msg):
                    q.put_nowait(_sse("status", msg, agent="content_creator"))

                # --- สร้างคอนเทนต์ตามจำนวนที่ user ขอ ---
                all_results: list[tuple[str, str | None]] = []
                previous_summaries: list[str] = []

                # ============================================================
                # Multi-agent auto mode: ถ้า user เลือก agent อื่นนอกจาก content_creator
                # ให้เลือกสินค้าครั้งเดียว แล้วรัน agent ที่เลือกตามลำดับ (เหมือน flow ปกติ
                # แต่สินค้าถูกเลือกโดย AI แทน user)
                # ============================================================
                if len(agents) > 1 or (agents and agents[0] != "content_creator"):
                    _status_cb("กำลังเลือกสินค้าและแนวคิด...")
                    selection = orch.select_product_auto(
                        llm=llm, quick_brief=quick_brief, platforms=platforms,
                        product_count=product_count,
                    )
                    if "error" in selection:
                        q.put_nowait(_sse("error", selection["error"], agent="content_creator"))
                        q.put_nowait(_sse("done", ""))
                        q.put_nowait(None)
                        return

                    chosen_pids = selection.get("product_ids", [])
                    chosen_concept = selection.get("concept", "")
                    reason = selection.get("reason", "")

                    # ส่งข้อมูลการเลือกให้ frontend
                    q.put_nowait(_sse("selection", json.dumps({
                        "product_id": chosen_pids[0] if chosen_pids else "",
                        "product_ids": chosen_pids,
                        "pillar": selection.get("pillar", ""),
                        "concept": chosen_concept,
                        "reason": reason,
                        "is_duplicate": False,
                        "similarity": 0.0,
                    }), agent="content_creator"))

                    if _status_cb:
                        _status_cb(f"เลือก: {', '.join(chosen_pids)} — {chosen_concept}")

                    # อ่านข้อมูลสินค้าที่เลือก
                    folder_label = " + ".join(chosen_pids)
                    all_raw = []
                    all_images = []
                    all_ready = {}
                    for pid in chosen_pids:
                        raw, imgs, ready = _read_folder(pid)
                        all_raw.extend(raw)
                        all_images.extend(imgs)
                        for fname, content in ready.items():
                            all_ready[f"{pid}/{fname}"] = content

                    # รัน agent ที่เลือกตามลำดับ (เหมือน flow_runner)
                    flow_results: dict[str, str] = {}
                    for agent_key in agents:
                        if _cancel_requested:
                            break

                        agent_name = AGENT_INFO.get(agent_key, {}).get("name", agent_key)
                        q.put_nowait(_sse("agent_start", f"{agent_name} — AUTO", agent=agent_key))

                        try:
                            def _agent_status(msg, _ak=agent_key):
                                q.put_nowait(_sse("status", msg, agent=_ak))

                            cc_count = content_count if agent_key == "content_creator" else 1
                            results_list = _run_single_agent(
                                agent_key, folder_label, all_raw, all_images,
                                all_ready, orch, llm, output_dir,
                                save_output=True, quick_brief=quick_brief,
                                context=flow_results, content_count=cc_count,
                                auto_image=auto_image, auto_video=auto_video,
                                platforms=platforms, media_type=media_type,
                                status_callback=_agent_status,
                            )
                            result_text = results_list[0][0] if results_list else ""
                            file_path = results_list[0][1] if results_list else None
                            flow_results[agent_key] = result_text
                            if file_path:
                                flow_output_files.append(str(file_path))

                            _dl = int(_sys_cfg().get("display_preview_length", 500))
                            q.put_nowait(_sse("agent_done", result_text[:_dl],
                                              agent=agent_key, file=file_path))
                        except Exception as e:
                            if _cancel_requested:
                                break
                            q.put_nowait(_sse("error", str(e), agent=agent_key))

                    if llm:
                        llm.close()
                        try:
                            _active_llms.remove(llm)
                        except ValueError:
                            pass
                    q.put_nowait(_sse("done", ""))
                    q.put_nowait(None)
                    return

                # ============================================================
                # Single-agent auto mode (backward compat): content_creator only
                # ============================================================
                for i in range(content_count):
                    if _cancel_requested:
                        q.put_nowait(_sse("status", "หยุดการทำงานแล้ว"))
                        break

                    # สำหรับหลายโพสต์ — บอกให้หลีกเลี่ยงโพสต์ก่อนหน้า
                    multi_brief = quick_brief
                    if content_count > 1:
                        multi_brief = f"โพสต์ที่ {i+1} จาก {content_count} โพสต์ — สร้างคอนเทนต์ที่แตกต่างจากโพสต์ก่อนหน้า"
                        if previous_summaries:
                            multi_brief += "\n\n--- คอนเทนต์ที่สร้างไปแล้วในรอบนี้ (ห้ามซ้ำ) ---\n"
                            _ll = int(_sys_cfg().get("display_preview_long", 800))
                            for j, s in enumerate(previous_summaries):
                                multi_brief += f"\nโพสต์ที่ {j+1}:\n{s[:_ll]}\n"
                            multi_brief += "--- สิ้นสุด ---\n"
                        if quick_brief:
                            multi_brief += f"\n\nคำขอเพิ่มเติมจาก user: {quick_brief}"

                    q.put_nowait(_sse("agent_start", f"นักสร้างคอนเทนต์ — AUTO (โพสต์ที่ {i+1})", agent="content_creator"))

                    # ส่ง platforms ทั้งหมดไป backend — run_content_creator_auto เลือกสินค้าครั้งเดียว
                    # แล้ววนสร้างหลายแพลตฟอร์มเอง (สินค้าเดียวกันทุกแพลตฟอร์มในรอบเดียว)
                    current_platform = platforms if platforms else None
                    result = orch.run_content_creator_auto(
                        llm=llm,
                        quick_brief=multi_brief,
                        media_type=media_type,
                        platforms=current_platform,
                        product_count=product_count,
                        status_callback=_status_cb,
                    )

                    if "error" in result:
                        q.put_nowait(_sse("error", result["error"], agent="content_creator"))
                        break

                    # ส่งข้อมูลการเลือกให้ frontend
                    q.put_nowait(_sse("selection", json.dumps({
                        "product_id": result.get("product_id", ""),
                        "product_ids": result.get("product_ids", []),
                        "pillar": result.get("pillar", ""),
                        "concept": result.get("concept", ""),
                        "reason": result.get("reason", ""),
                        "is_duplicate": result.get("is_duplicate", False),
                        "similarity": result.get("similarity", 0.0),
                    }), agent="content_creator"))

                    content = result.get("content", "")
                    markdown = result.get("markdown", content)
                    chosen_pids = result.get("product_ids", [result.get("product_id", "AUTO")])
                    chosen_pid = " + ".join(chosen_pids) if len(chosen_pids) > 1 else chosen_pids[0]

                    # เซฟไฟล์
                    timestamp = datetime.now().strftime("%H%M%S")
                    fname_base = f"04_content_creator_{chosen_pid}_AUTO_โพสต์ที่{i+1}_{timestamp}"
                    json_path = output_dir / f"{fname_base}.json"
                    json_path.write_text(content, encoding="utf-8")
                    md_filepath = output_dir / f"{fname_base}.md"
                    md_filepath.write_text(markdown, encoding="utf-8")
                    saved_path = str(md_filepath)
                    flow_output_files.append(saved_path)

                    # อัปเดต history entry ล่าสุดให้มี output_file (orchestrator บันทึกก่อนเซฟไฟล์)
                    content_history.update_last_entry_output_file(PROJECT_ROOT, saved_path)

                    all_results.append((content, saved_path))
                    _ll = int(_sys_cfg().get("display_preview_long", 800))
                    previous_summaries.append(markdown[:_ll])

                    _dl = int(_sys_cfg().get("display_preview_length", 500))
                    q.put_nowait(_sse("agent_done", markdown[:_dl], agent="content_creator", file=saved_path, set_num=i+1, total_sets=content_count))

                    # Auto-generate media ถ้าเปิด
                    print(f"[DEBUG api_run_auto] auto_image={auto_image!r} auto_video={auto_video!r} media_when={body.get('media_when', 'N/A')!r}", flush=True)
                    if auto_image or auto_video:
                        try:
                            parsed_media = media_gen.parse_media_prompts(content)
                            # ดึง image_paths ของสินค้าที่เลือก
                            from src import product_db as _pdb
                            from src import asset_library as _al
                            orch.product_id = chosen_pid
                            product_img_paths = _pdb.get_product_image_paths(chosen_pid) if _pdb.is_ready(chosen_pid) else []
                            if auto_image:
                                for j, img in enumerate(parsed_media.get("images", [])):
                                    img_path = output_dir / f"image_โพสต์{i+1}_{j+1}.png"
                                    if _status_cb:
                                        _status_cb(f"กำลังสร้างรูปที่ {j+1}...")
                                    img_kwargs: dict = {}
                                    if img.get("aspect_ratio"):
                                        img_kwargs["aspect_ratio"] = img["aspect_ratio"]
                                    # ส่งรูปสินค้า + รูป asset เป็น reference — image-to-image
                                    _refs = _al.build_input_references(
                                        product_img_paths, img.get("asset_ids", []),
                                    )
                                    if _refs:
                                        img_kwargs["input_references"] = _refs
                                    # Visual brand injection
                                    visual = _get_brand_visual()
                                    if visual:
                                        img_kwargs["visual"] = visual
                                    r = media_gen.generate_image_with_retry(
                                        img["prompt"], img_path, llm=llm, **img_kwargs,
                                    )
                                    media_gen.save_retry_history(output_dir, "image", img_path.name, r)
                                    if not r.get("ok"):
                                        _status_cb(f"รูปที่ {j+1}: {r.get('error', 'unknown')}")
                            if auto_video:
                                for j, vid in enumerate(parsed_media.get("videos", [])):
                                    vid_path = output_dir / f"video_โพสต์{i+1}_{j+1}.mp4"
                                    def _vid_status(s, idx=j):
                                        _status_cb(f"วิดีโอที่ {idx+1}: {s}")
                                    vid_kwargs: dict = {"on_status": _vid_status}
                                    if vid.get("duration"):
                                        vid_kwargs["duration"] = int(vid["duration"])
                                    if vid.get("aspect_ratio"):
                                        vid_kwargs["aspect_ratio"] = vid["aspect_ratio"]
                                    if vid.get("resolution"):
                                        vid_kwargs["resolution"] = vid["resolution"]
                                    # ส่งรูปสินค้า + รูป asset เป็น reference — reference-to-video
                                    _refs = _al.build_input_references(
                                        product_img_paths, vid.get("asset_ids", []),
                                    )
                                    if _refs:
                                        vid_kwargs["input_references"] = _refs
                                    # Visual brand injection
                                    visual = _get_brand_visual()
                                    if visual:
                                        vid_kwargs["visual"] = visual
                                    r = media_gen.generate_video_with_retry(
                                        vid["prompt"], vid_path, llm=llm, **vid_kwargs,
                                    )
                                    media_gen.save_retry_history(output_dir, "video", vid_path.name, r)
                                    if not r.get("ok"):
                                        _status_cb(f"วิดีโอที่ {j+1}: {r.get('error', 'unknown')}")
                        except Exception as e:
                            _status_cb(f"สร้างสื่อไม่สำเร็จ: {e}")

                if llm:
                    llm.close()
                    try:
                        _active_llms.remove(llm)
                    except ValueError:
                        pass
                q.put_nowait(_sse("done", ""))
                q.put_nowait(None)

            except Exception as e:
                if llm:
                    try:
                        llm.close()
                    except Exception:
                        pass
                    try:
                        _active_llms.remove(llm)
                    except ValueError:
                        pass
                q.put_nowait(_sse("error", str(e)))
                q.put_nowait(_sse("done", ""))
                q.put_nowait(None)
            finally:
                # เขียน cost summary + flow meta สำหรับ auto flow นี้ (เก็บไว้หลังบ้านสำหรับ dev)
                try:
                    output_dir_for_cost = OUTPUT_DIR / session_ts
                    write_cost_summary(
                        output_dir_for_cost, flow_id,
                        label=f"AUTO — {', '.join(agents)}",
                        agents=agents,
                    )
                    # เก็บ mapping output_file → flow_id เพื่อให้ "สร้างสื่อภายหลัง" หา flow_id ได้
                    write_flow_meta(
                        output_dir_for_cost, flow_id, flow_output_files,
                        label=f"AUTO — {', '.join(agents)}",
                        agents=agents,
                    )
                except Exception:
                    pass
                clear_flow_id()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        while True:
            try:
                event = q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if event is None:
                break
            yield event

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/content_history")
def api_content_history(limit: int = 20) -> JSONResponse:
    """ดึงประวัติคอนเทนต์ที่สร้างไปแล้ว (สำหรับ auto mode — ดูว่าทำอะไรไปแล้ว)."""
    from src import content_history
    entries = content_history.get_recent_entries(PROJECT_ROOT, limit=limit)
    return JSONResponse({"entries": entries})


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MKTApp</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  input::placeholder, textarea::placeholder { color: #555; font-style: italic; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; }
  .header { background: #161821; padding: 16px 24px; border-bottom: 1px solid #2a2d3a; display: flex; align-items: center; justify-content: space-between; }
  .header-left h1 { font-size: 18px; color: #7c8aff; }
  .header-left p { font-size: 13px; color: #888; margin-top: 4px; }
  .home-header-btn { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 8px; padding: 8px 16px; font-size: 13px; cursor: pointer; }
  .home-header-btn:hover { border-color: #7c8aff; color: #7c8aff; }
  .header-right { display: flex; align-items: center; gap: 12px; }
  .credits-badge { font-size: 12px; color: #888; background: #1c1e2a; border: 1px solid #2a2d3a; border-radius: 8px; padding: 6px 12px; }
  .credits-badge .credits-remaining { color: #4ade80; font-weight: 600; }
  .credits-badge .credits-low { color: #fbbf24; font-weight: 600; }
  .credits-badge .credits-empty { color: #f87171; font-weight: 600; }
  .container { display: flex; height: calc(100vh - 60px); }
  .sidebar { width: 320px; background: #161821; border-right: 1px solid #2a2d3a; display: flex; flex-direction: column; overflow: hidden; }
  .sidebar-tabs { display: flex; border-bottom: 1px solid #2a2d3a; }
  .sidebar-tab { flex: 1; padding: 10px; text-align: center; cursor: pointer; font-size: 12px; color: #888; border-bottom: 2px solid transparent; }
  .sidebar-tab.active { color: #7c8aff; border-bottom-color: #7c8aff; }
  .sidebar-content { flex: 1; overflow-y: auto; padding: 12px; }

  .upload-box { background: #1c1e2a; border-radius: 10px; padding: 16px; margin-bottom: 12px; }
  .upload-box label { font-size: 13px; color: #ccc; display: block; margin-bottom: 6px; }
  .upload-box input[type="text"] { width: 100%; background: #0f1117; border: 1px solid #2a2d3a; border-radius: 6px; padding: 8px 10px; color: #e0e0e0; font-size: 13px; margin-bottom: 10px; }
  .upload-box input[type="file"] { width: 100%; font-size: 12px; color: #888; margin-bottom: 10px; }
  .upload-btn { width: 100%; background: #7c8aff; color: #0f1117; border: none; border-radius: 8px; padding: 10px; font-size: 14px; font-weight: 600; cursor: pointer; }
  .upload-btn:hover { background: #6470ff; }
  .upload-btn:disabled { background: #3a3d5a; color: #888; cursor: not-allowed; }
  .upload-status { font-size: 12px; margin-top: 8px; }
  .upload-status.ok { color: #4ade80; }
  .upload-status.err { color: #ff6b6b; }
  .upload-queue { margin-bottom: 10px; }
  .upload-queue-item { display: flex; align-items: center; gap: 6px; padding: 4px 8px; font-size: 12px; color: #aaa; background: #0f1117; border-radius: 6px; margin-bottom: 4px; }
  .upload-queue-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .upload-queue-del { cursor: pointer; color: #555; font-size: 16px; padding: 0 4px; }
  .upload-queue-del:hover { color: #ff6b6b; }
  .fmt-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0; }
  .fmt-card { background: #0f1117; border: 1px solid #2a2d3a; border-radius: 8px; padding: 10px; }
  .fmt-card-head { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #ccc; margin-bottom: 6px; }
  .fmt-card-icon { font-size: 16px; }
  .fmt-card-exts { display: flex; flex-wrap: wrap; gap: 4px; }
  .fmt-tag { font-size: 10px; color: #7dd3fc; background: #1a2238; border-radius: 4px; padding: 2px 6px; }
  .fmt-card-max { font-size: 10px; color: #666; margin-top: 6px; }
  .fmt-note { font-size: 11px; color: #fbbf24; margin-top: 8px; display: flex; align-items: center; gap: 4px; }
  .sidebar-toolbar { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
  .sidebar-toolbar-row { display: flex; gap: 8px; }
  .sidebar-search { flex: 1; background: #0f1117; border: 1px solid #2a2d3a; border-radius: 6px; padding: 8px 10px; color: #e0e0e0; font-size: 13px; min-width: 0; }
  .sidebar-search:focus { outline: none; border-color: #7c8aff; }
  .sidebar-add-btn { background: #7c8aff; color: #0f1117; border: none; border-radius: 6px; padding: 8px 14px; font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap; }
  .sidebar-add-btn:hover { background: #6470ff; }
  .sidebar-ms-btn { background: #1c1e2a; color: #888; border: 1px solid #2a2d3a; border-radius: 6px; padding: 8px 10px; font-size: 12px; cursor: pointer; white-space: nowrap; }
  .sidebar-ms-btn:hover { border-color: #7c8aff; color: #7c8aff; }
  .sidebar-ms-btn.active { background: #2a1a3a; border-color: #a78bfa; color: #a78bfa; }
  /* Auto item ใน sidebar — คลิกเลือกใน wizard */
  .folder-item.auto-item { border: 1px dashed #7c8aff; background: #1a1d2e; }
  .folder-item.auto-item:hover { border-color: #7c8aff; background: #1a2a4a; }
  /* Auto chip ใน agent box */
  .dropped-folder.auto-folder { border-color: #7c8aff; background: #1a2a4a; color: #a5b4ff; }
  .auto-count-input { width: 42px; padding: 2px 4px; background: #0f1117; border: 1px solid #7c8aff; border-radius: 4px; color: #a5b4ff; font-size: 12px; text-align: center; margin-left: 4px; }
  .auto-count-input::-webkit-inner-spin-button { opacity: 1; }
  .folder-item-selected { background: #2a1a3a !important; border: 1px solid #a78bfa; }
  .ms-check { width: 20px; height: 20px; border-radius: 5px; border: 1.5px solid #444; display: flex; align-items: center; justify-content: center; font-size: 14px; color: #fff; flex-shrink: 0; }
  .ms-check.checked { background: #a78bfa; border-color: #a78bfa; }
  .ms-action-bar { display: flex; align-items: center; gap: 8px; padding: 12px; background: #161821; border-top: 1px solid #2a2d3a; position: sticky; bottom: 0; margin-top: 8px; flex-wrap: wrap; }
  .ms-count { font-size: 12px; color: #ccc; flex: 1; }
  .ms-clear-btn { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 6px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
  .ms-clear-btn:hover { border-color: #ff6b6b; color: #ff6b6b; }
  .ms-send-btn { background: #4ade80; color: #0f1117; border: none; border-radius: 6px; padding: 6px 14px; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; }
  .ms-send-btn:hover:not(:disabled) { background: #3bce6a; }
  .ms-send-btn:disabled { background: #2a2d3a; color: #555; cursor: not-allowed; }
  .sendto-agent-card { display: flex; align-items: center; gap: 8px; padding: 12px; background: #1c1e2a; border: 1px solid #2a2d3a; border-radius: 10px; cursor: pointer; font-size: 13px; color: #ccc; transition: all .12s; }
  .sendto-agent-card:hover { border-color: #7c8aff; }
  .sendto-agent-card.selected { border-color: #7c8aff; background: #1a2238; color: #fff; }
  .sendto-agent-card .agent-emoji { font-size: 18px; }
  .sendto-zone-btn { flex: 1; background: #1c1e2a; border: 1px solid #2a2d3a; color: #ccc; border-radius: 8px; padding: 10px; font-size: 12px; cursor: pointer; }
  .sendto-zone-btn:hover { border-color: #7c8aff; }
  .sendto-zone-btn.selected { border-color: #a78bfa; background: #2a1a3a; color: #a78bfa; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; cursor: help; }
  .status-ready { background: #4ade80; }
  .status-pending { background: #facc15; }
  .status-empty { background: #555; }
  .folder-item-disabled { opacity: 0.45; cursor: not-allowed; }
  .folder-item-disabled .folder-info { cursor: default !important; }

  .brand-file-item { padding: 10px 12px; border-radius: 8px; cursor: pointer; font-size: 13px; color: #ccc; margin-bottom: 4px; }
  .brand-file-item:hover { background: #1e2030; }
  .brand-file-item.active { background: #2a2d4a; color: #7c8aff; }
  .brand-editor { display: none; }
  .brand-editor.visible { display: block; }
  .brand-editor textarea { width: 100%; min-height: 400px; background: #0f1117; border: 1px solid #2a2d3a; border-radius: 8px; padding: 12px; color: #e0e0e0; font-size: 13px; font-family: 'SF Mono', 'Consolas', monospace; line-height: 1.6; resize: vertical; }
  .brand-editor textarea::placeholder { color: #555; font-style: italic; opacity: 1; }
  .brand-save-btn { background: #4ade80; color: #0f1117; border: none; border-radius: 8px; padding: 8px 16px; font-size: 13px; font-weight: 600; cursor: pointer; margin-top: 8px; }
  .brand-save-btn:hover { background: #45c97c; }
  .brand-back-btn { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; margin-bottom: 10px; }

  /* Pillars modal */
  .pillar-card { background: #1c1e2a; border: 1px solid #2a2d3a; border-radius: 10px; padding: 14px; margin-bottom: 10px; }
  .pillar-card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .pillar-card-name { flex: 1; background: #0f1117; border: 1px solid #2a2d3a; border-radius: 6px; padding: 8px 10px; color: #e0e0e0; font-size: 14px; font-weight: 500; }
  .pillar-card-name:focus { outline: none; border-color: #7c8aff; }
  .pillar-del-btn { background: none; border: 1px solid #3a2030; color: #ff6b6b; border-radius: 6px; padding: 6px 10px; font-size: 13px; cursor: pointer; flex-shrink: 0; }
  .pillar-del-btn:hover { background: #2a1520; border-color: #ff6b6b; }
  .pillar-keywords-label { font-size: 11px; color: #888; margin-bottom: 6px; }
  .pillar-keywords-box { background: #0f1117; border: 1px solid #2a2d3a; border-radius: 6px; padding: 8px; min-height: 38px; display: flex; flex-wrap: wrap; gap: 6px; }
  .pillar-keyword-chip { display: inline-flex; align-items: center; gap: 4px; background: #2a2d4a; color: #a0a8ff; border-radius: 12px; padding: 4px 10px; font-size: 12px; }
  .pillar-keyword-chip .chip-x { cursor: pointer; color: #666; font-size: 14px; line-height: 1; }
  .pillar-keyword-chip .chip-x:hover { color: #ff6b6b; }
  .pillar-keyword-add { display: inline-flex; align-items: center; gap: 4px; }
  .pillar-keyword-add input { background: transparent; border: 1px dashed #3a3d5a; border-radius: 12px; padding: 4px 10px; color: #e0e0e0; font-size: 12px; width: 120px; }
  .pillar-keyword-add input:focus { outline: none; border-color: #7c8aff; border-style: solid; }
  .pillar-add-btn { background: #1a1d2e; border: 1px dashed #7c8aff; color: #7c8aff; border-radius: 10px; padding: 14px; font-size: 14px; cursor: pointer; width: 100%; margin-top: 8px; }
  .pillar-add-btn:hover { background: #1a2a4a; }

  .folder-item { display: flex; align-items: center; gap: 10px; padding: 12px; border-radius: 10px; background: #1c1e2a; margin-bottom: 8px; cursor: pointer; transition: all 0.15s; }
  .folder-item:hover { background: #252836; transform: translateY(-1px); }
  .folder-item.dragging { opacity: 0.4; }
  .folder-icon { font-size: 24px; flex-shrink: 0; width: 32px; height: 32px; display: flex; align-items: center; justify-content: center; overflow: hidden; border-radius: 6px; background: #1c1e2a; }
  .folder-icon img { width: 100%; height: 100%; object-fit: cover; }
  .folder-info { flex: 1; min-width: 0; }
  .folder-name { font-size: 14px; font-weight: 600; color: #e0e0e0; }
  .folder-meta { font-size: 11px; color: #666; margin-top: 3px; }
  .folder-badge { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 10px; margin-right: 4px; }
  .fb-green { background: #1a3a2a; color: #4ade80; }
  .fb-gray { background: #2a2d3a; color: #888; }
  .folder-files-list { margin-bottom: 8px; }
  .file-row { display: flex; align-items: center; gap: 6px; padding: 4px 8px; font-size: 12px; color: #aaa; border-radius: 4px; }
  .file-row:hover { background: #1c1e2a; }
  .file-row-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-row-del { cursor: pointer; color: #555; font-size: 16px; padding: 0 4px; }
  .file-row-del:hover { color: #ff6b6b; }
  .add-file-btn { background: #2a2d4a; color: #7c8aff; border: 1px solid #3a3d5a; border-radius: 6px; padding: 4px 10px; font-size: 11px; cursor: pointer; margin-top: 4px; }
  .add-file-btn:hover { background: #353a5a; }
  .folder-del-btn { cursor: pointer; color: #555; font-size: 18px; padding: 0 4px; flex-shrink: 0; }
  .folder-del-btn:hover { color: #ff6b6b; }
  .folder-manage-btn { cursor: pointer; color: #555; font-size: 14px; padding: 4px 6px; flex-shrink: 0; border-radius: 4px; }
  .folder-manage-btn:hover { color: #7c8aff; background: #1c1e2a; }
  .badge { font-size: 10px; padding: 2px 8px; border-radius: 4px; font-weight: 500; }
  .badge-ready { background: #1a3a2a; color: #4ade80; }
  .badge-pending { background: #2a2d3a; color: #fbbf24; }
  .badge-empty { background: #2a2d3a; color: #666; }

  .session-item { padding: 8px 12px; cursor: pointer; border-radius: 6px; font-size: 13px; color: #ccc; }
  .session-item:hover { background: #1e2030; }
  .session-item.active { background: #2a2d4a; color: #7c8aff; }
  .sub-file-item { padding: 6px 12px 6px 24px; cursor: pointer; border-radius: 6px; font-size: 12px; color: #aaa; }
  .sub-file-item:hover { background: #1e2030; color: #fff; }
  .sub-file-item.active { color: #7c8aff; }

  .main { flex: 1; overflow-y: auto; padding: 24px 32px; }
  .main h2 { font-size: 16px; color: #7c8aff; margin-bottom: 16px; }
  .content-box { background: #161821; border: 1px solid #2a2d3a; border-radius: 8px; padding: 24px; }
  .content-box pre { white-space: pre-wrap; word-wrap: break-word; font-size: 14px; line-height: 1.7; font-family: 'SF Mono', 'Consolas', monospace; }
  .content-box h1 { font-size: 20px; color: #7c8aff; margin: 20px 0 12px; }
  .content-box h2 { font-size: 18px; color: #7c8aff; margin: 18px 0 10px; }
  .content-box h3 { font-size: 16px; color: #9ea3ff; margin: 16px 0 8px; }
  .content-box p { font-size: 14px; line-height: 1.8; color: #ccc; margin-bottom: 12px; }
  .content-box ul { margin: 8px 0 16px 20px; }
  .content-box li { font-size: 14px; line-height: 1.8; color: #ccc; margin-bottom: 4px; }
  .content-box strong { color: #e0e0e0; font-weight: 600; }
  .content-box em { color: #aaa; font-style: italic; }
  .content-box hr { border: none; border-top: 1px solid #2a2d3a; margin: 20px 0; }
  .content-box table { border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 13px; display: block; overflow-x: auto; }
  .content-box th { background: #1c1e2a; border: 1px solid #2a2d3a; padding: 8px 12px; text-align: left; color: #7c8aff; font-weight: 600; white-space: nowrap; }
  .content-box td { border: 1px solid #2a2d3a; padding: 8px 12px; color: #ccc; }
  .content-box tr:nth-child(even) td { background: #161821; }
  .empty { text-align: center; padding: 60px; color: #555; }
  .file-info-display { font-size: 12px; color: #666; margin-bottom: 12px; }
  .modal-cost-info { margin-top: 6px; padding-top: 6px; border-top: 1px solid #2a2d3a; font-size: 12px; }
  .back-btn { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 8px; padding: 8px 16px; font-size: 13px; cursor: pointer; margin-bottom: 16px; display: inline-block; }
  .back-btn:hover { border-color: #7c8aff; color: #7c8aff; }
  .home-btn { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 8px; padding: 8px 16px; font-size: 13px; cursor: pointer; margin-bottom: 16px; margin-left: 8px; display: inline-block; }
  .home-btn:hover { border-color: #7c8aff; color: #7c8aff; }

  /* ===== Platform Preview ===== */
  .preview-toggle { display: flex; gap: 8px; margin-bottom: 16px; }
  .preview-toggle button { background: #161821; border: 1px solid #2a2d3a; color: #888; border-radius: 8px; padding: 8px 16px; font-size: 13px; cursor: pointer; }
  .preview-toggle button.active { background: #7c8aff; color: #0f1117; border-color: #7c8aff; font-weight: 600; }
  .preview-toggle button:hover { border-color: #7c8aff; }

  .post-selector-bar { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
  .post-selector-tab { background: #161821; border: 1px solid #2a2d3a; color: #888; border-radius: 6px; padding: 6px 14px; font-size: 13px; cursor: pointer; }
  .post-selector-tab.active { background: #1a2a4a; color: #7c9aff; border-color: #7c8aff; font-weight: 600; }
  .post-selector-tab:hover { border-color: #7c8aff; }

  .preview-container { max-width: 520px; margin: 0 auto; }
  .preview-meta { font-size: 12px; color: #666; margin-bottom: 12px; display: flex; gap: 12px; flex-wrap: wrap; }
  .preview-meta .meta-tag { background: #1c1e2a; border: 1px solid #2a2d3a; border-radius: 6px; padding: 3px 10px; }
  .preview-meta .meta-tag b { color: #7c8aff; }

  /* Facebook */
  .fb-card { background: #fff; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.2); overflow: hidden; color: #1c1e21; font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; }
  .fb-card .fb-header { display: flex; align-items: center; gap: 10px; padding: 12px; }
  .fb-card .fb-avatar { width: 40px; height: 40px; border-radius: 50%; background: linear-gradient(135deg, #1877f2, #42a5f5); display: flex; align-items: center; justify-content: center; color: #fff; font-weight: 700; font-size: 16px; flex-shrink: 0; }
  .fb-card .fb-name { font-weight: 600; font-size: 14px; color: #050505; }
  .fb-card .fb-sub { font-size: 12px; color: #65676b; }
  .fb-card .fb-body { padding: 0 12px 12px; font-size: 15px; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }
  .fb-card .fb-title { font-weight: 700; font-size: 16px; margin-bottom: 8px; }
  .fb-card .fb-media { width: 100%; display: block; }
  .fb-card .fb-media img { width: 100%; display: block; }
  .fb-card .fb-media-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 2px; }
  .fb-card .fb-media-grid img { width: 100%; height: 100%; object-fit: cover; aspect-ratio: 1; }
  .fb-card .fb-hashtags { padding: 8px 12px 12px; color: #1877f2; font-size: 13px; line-height: 1.6; }
  .fb-card .fb-actions { display: flex; gap: 16px; padding: 8px 12px; border-top: 1px solid #ced0d4; color: #65676b; font-size: 13px; font-weight: 600; }
  .fb-card .fb-actions span { display: flex; align-items: center; gap: 4px; }

  /* TikTok */
  .tk-card { position: relative; background: #000; border-radius: 12px; overflow: hidden; aspect-ratio: 9/16; max-height: 640px; margin: 0 auto; color: #fff; font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; }
  .tk-card .tk-media { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; background: #111; }
  .tk-card .tk-media img { width: 100%; height: 100%; object-fit: cover; }
  .tk-card .tk-media .tk-placeholder { color: #555; font-size: 14px; text-align: center; padding: 20px; }
  .tk-card .tk-overlay { position: absolute; inset: 0; background: linear-gradient(to bottom, rgba(0,0,0,0.3) 0%, transparent 30%, transparent 60%, rgba(0,0,0,0.7) 100%); pointer-events: none; }
  .tk-card .tk-content { position: absolute; bottom: 60px; left: 12px; right: 60px; pointer-events: none; }
  .tk-card .tk-user { font-weight: 700; font-size: 16px; margin-bottom: 6px; }
  .tk-card .tk-title { font-weight: 600; font-size: 15px; margin-bottom: 4px; line-height: 1.3; }
  .tk-card .tk-caption { font-size: 14px; line-height: 1.4; white-space: pre-wrap; }
  .tk-card .tk-caption.collapsed { max-height: 4.2em; overflow: hidden; }
  .tk-card .tk-caption-toggle { color: #ddd; font-size: 13px; font-weight: 600; cursor: pointer; pointer-events: auto; display: inline-block; margin-top: 4px; }
  .tk-card .tk-hashtags { color: #fff; font-size: 13px; margin-top: 6px; }
  .tk-card .tk-hashtags a { color: #fff; font-weight: 600; }
  .tk-script { background: #161821; border: 1px solid #2a2d3a; border-radius: 8px; padding: 16px; margin-top: 16px; }
  .tk-script-label { font-size: 13px; color: #7c8aff; font-weight: 600; margin-bottom: 10px; }
  .tk-script-body { font-size: 14px; line-height: 1.7; color: #ccc; white-space: pre-wrap; word-wrap: break-word; }
  .tk-script-body strong { color: #e0e0e0; font-weight: 600; }
  .tk-script-body em { color: #888; font-style: italic; }
  .tk-card .tk-side { position: absolute; right: 8px; bottom: 60px; display: flex; flex-direction: column; gap: 16px; align-items: center; pointer-events: none; }
  .tk-card .tk-side .tk-icon { width: 40px; height: 40px; border-radius: 50%; background: rgba(255,255,255,0.15); display: flex; align-items: center; justify-content: center; font-size: 18px; }
  .tk-card .tk-side .tk-count { font-size: 11px; font-weight: 600; }
  .tk-card .tk-top { position: absolute; top: 12px; left: 0; right: 0; text-align: center; font-size: 13px; font-weight: 600; opacity: 0.9; pointer-events: none; }

  /* Instagram */
  .ig-card { background: #fff; border-radius: 8px; overflow: hidden; color: #262626; font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; max-width: 480px; }
  .ig-card .ig-header { display: flex; align-items: center; gap: 10px; padding: 10px 12px; }
  .ig-card .ig-avatar { width: 32px; height: 32px; border-radius: 50%; background: linear-gradient(135deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888); display: flex; align-items: center; justify-content: center; color: #fff; font-weight: 700; font-size: 13px; flex-shrink: 0; }
  .ig-card .ig-name { font-weight: 600; font-size: 13px; }
  .ig-card .ig-media { width: 100%; aspect-ratio: 1; background: #fafafa; display: flex; align-items: center; justify-content: center; overflow: hidden; }
  .ig-card .ig-media img { width: 100%; height: 100%; object-fit: cover; }
  .ig-card .ig-media .ig-placeholder { color: #999; font-size: 14px; text-align: center; padding: 20px; }
  .ig-card .ig-actions { display: flex; gap: 12px; padding: 10px 12px 4px; font-size: 22px; }
  .ig-card .ig-likes { padding: 0 12px; font-size: 13px; font-weight: 600; }
  .ig-card .ig-caption { padding: 4px 12px 12px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }
  .ig-card .ig-caption b { font-weight: 600; }
  .ig-card .ig-hashtags { padding: 0 12px 12px; color: #00376b; font-size: 13px; line-height: 1.6; }

  .preview-original { display: none; }
  .preview-original.visible { display: block; }
  .preview-platform { display: none; }
  .preview-platform.visible { display: block; }

  .media-action-bar { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
  .media-gen-btn { background: #1c1e2a; border: 1px solid #7c8aff; color: #7c8aff; border-radius: 8px; padding: 8px 16px; font-size: 13px; cursor: pointer; font-weight: 600; }
  .media-gen-btn:hover { background: #7c8aff; color: #0f1117; }
  .media-status { font-size: 13px; padding: 6px 12px; border-radius: 6px; }
  .media-status.done { background: #1a3320; color: #4ade80; }
  .media-status.working { background: #1c2333; color: #7c8aff; }
  .media-status.error { background: #331a1a; color: #f87171; }
  .media-status.none { color: #555; }

  .media-retry-history { margin-bottom: 16px; }
  .retry-history-title { font-size: 13px; color: #888; margin-bottom: 8px; font-weight: 600; }
  .retry-entry { background: #0f1117; border: 1px solid #2a2d3a; border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; font-size: 12px; }
  .retry-entry.success { border-left: 3px solid #4ade80; }
  .retry-entry.failed { border-left: 3px solid #f87171; }
  .retry-entry-header { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 4px; }
  .retry-status { font-weight: bold; }
  .retry-entry.success .retry-status { color: #4ade80; }
  .retry-entry.failed .retry-status { color: #f87171; }
  .retry-type { background: #1c1e2a; padding: 2px 8px; border-radius: 4px; color: #7c8aff; text-transform: uppercase; font-size: 10px; }
  .retry-filename { color: #aaa; }
  .retry-time { color: #666; font-size: 11px; }
  .retry-count { color: #fbbf24; font-size: 11px; }
  .retry-error { color: #f87171; margin: 4px 0; font-size: 12px; }
  .retry-warnings { color: #fbbf24; margin-top: 4px; font-size: 11px; }
  .retry-details { margin-top: 6px; }
  .retry-details summary { cursor: pointer; color: #7c8aff; font-size: 11px; }
  .retry-step { background: #161821; border-radius: 6px; padding: 8px; margin: 6px 0; border-left: 2px solid #f87171; }
  .retry-step-num { color: #f87171; font-weight: 600; font-size: 11px; margin-bottom: 4px; }
  .retry-step-error { color: #f87171; font-size: 11px; margin-bottom: 4px; }
  .retry-step-prompt { color: #ccc; font-size: 11px; margin: 2px 0; word-break: break-word; }

  .settings-modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.6); display: none; align-items: center; justify-content: center; z-index: 1000; }
  .settings-modal-overlay.visible { display: flex; }
  .settings-modal { background: #161821; border: 1px solid #2a2d3a; border-radius: 16px; padding: 24px; width: 420px; max-height: 80vh; overflow-y: auto; }
  .settings-modal h3 { font-size: 16px; color: #7c8aff; margin-bottom: 16px; }
  .settings-modal label { font-size: 12px; color: #888; display: block; margin-bottom: 4px; margin-top: 12px; }
  .settings-modal input, .settings-modal select { width: 100%; background: #0f1117; border: 1px solid #2a2d3a; border-radius: 6px; padding: 8px 10px; color: #e0e0e0; font-size: 13px; }
  .settings-modal .settings-actions { display: flex; gap: 8px; margin-top: 20px; }
  .settings-modal .settings-save { background: #4ade80; color: #0f1117; border: none; border-radius: 8px; padding: 10px 20px; font-size: 14px; font-weight: 600; cursor: pointer; flex: 1; }
  .settings-modal .settings-save:disabled { background: #2a2d3a; color: #555; cursor: not-allowed; }
  .settings-modal .settings-cancel { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 8px; padding: 10px 20px; font-size: 14px; cursor: pointer; flex: 1; }
  .settings-modal .settings-reset { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer; }
  .settings-modal .settings-reset:hover { border-color: #facc15; color: #facc15; }
  /* Agent Instructions modal — 3 tiers */
  .preset-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .preset-card { background: #1c1e2a; border: 2px solid #2a2d3a; border-radius: 10px; padding: 14px; cursor: pointer; transition: border-color 0.15s; }
  .preset-card:hover { border-color: #4a4d6a; }
  .preset-card.selected { border-color: #7c8aff; background: #1e2030; }
  .preset-card-label { font-size: 14px; font-weight: 600; color: #e0e0e0; margin-bottom: 4px; }
  .preset-card-desc { font-size: 11px; color: #888; }
  .preset-preview { margin-top: 16px; padding: 12px 14px; background: #1e2030; border: 1px solid #2a2d3a; border-radius: 10px; font-size: 12px; color: #aaa; line-height: 1.6; }
  .preset-preview-label { font-size: 11px; color: #7c8aff; margin-bottom: 6px; font-weight: 600; }
  .brand-conflict-banner { background: #2a2018; border: 1px solid #fbbf24; border-radius: 10px; padding: 12px 14px; }
  .brand-conflict-title { font-size: 13px; font-weight: 600; color: #fbbf24; margin-bottom: 8px; }
  .brand-conflict-item { font-size: 12px; color: #e0e0e0; margin: 6px 0; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .brand-conflict-sep { color: #666; font-size: 11px; }
  .brand-conflict-btn { font-size: 11px; padding: 3px 10px; border-radius: 6px; cursor: pointer; border: 1px solid; }
  .brand-conflict-btn-brand { border-color: #7c8aff; color: #7c8aff; background: transparent; }
  .brand-conflict-btn-brand:hover { background: rgba(124,138,255,0.1); }
  .brand-conflict-btn-user { border-color: #4a4d6a; color: #888; background: transparent; cursor: not-allowed; }
  .brand-conflict-btn-user.enabled { border-color: #fbbf24; color: #fbbf24; cursor: pointer; }
  .brand-conflict-btn-user.enabled:hover { background: rgba(251,191,36,0.1); }
  .brand-conflict-hard-note { font-size: 11px; color: #888; font-style: italic; }
  .instr-section { margin-bottom: 16px; }
  .instr-section-label { font-size: 12px; color: #888; margin-bottom: 6px; }
  .chip-row { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { padding: 6px 12px; background: #1c1e2a; border: 1px solid #2a2d3a; border-radius: 20px; font-size: 12px; color: #aaa; cursor: pointer; user-select: none; }
  .chip:hover { border-color: #4a4d6a; }
  .chip.selected { background: #2a2d3a; border-color: #7c8aff; color: #7c8aff; }
  .radio-row { display: flex; gap: 12px; flex-wrap: wrap; }
  .radio-item { display: flex; align-items: center; gap: 6px; font-size: 13px; color: #ccc; cursor: pointer; }
  .radio-item input { width: auto; }
  .slider-row { display: flex; align-items: center; gap: 10px; }
  .slider-row input[type=range] { flex: 1; accent-color: #7c8aff; width: 100%; }
  .slider-labels { display: flex; justify-content: space-between; font-size: 10px; color: #666; margin-top: 2px; }
  .slider-value { font-size: 12px; color: #7c8aff; min-width: 80px; text-align: center; font-weight: 600; }
  .instr-rules-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .instr-rules-col h4 { font-size: 12px; color: #4ade80; margin-bottom: 6px; }
  .instr-rules-col.forbid h4 { color: #f87171; }
  .instr-rule-item { display: flex; align-items: flex-start; gap: 6px; font-size: 12px; color: #ccc; margin-bottom: 4px; cursor: pointer; }
  .instr-rule-item input { width: auto; margin-top: 2px; }
  .instr-textarea { width: 100%; min-height: 80px; background: #0f1117; border: 1px solid #2a2d3a; border-radius: 8px; padding: 10px; color: #e0e0e0; font-size: 13px; resize: vertical; }
  .instr-textarea::placeholder { color: #555; font-style: italic; opacity: 1; }
  .instr-advanced-toggle { font-size: 12px; color: #888; cursor: pointer; margin: 12px 0 8px; padding: 8px; background: #0f1117; border: 1px dashed #2a2d3a; border-radius: 6px; text-align: center; }
  .instr-advanced-toggle:hover { color: #7c8aff; border-color: #4a4d6a; }
  .instr-advanced-section { display: none; margin-top: 8px; }
  .instr-advanced-section.visible { display: block; }
  .quick-brief-box { margin: 12px 0; padding: 12px; background: #1c1e2a; border: 1px solid #2a2d3a; border-radius: 10px; }
  .quick-brief-box label { font-size: 12px; color: #7c8aff; margin-bottom: 6px; display: block; }
  .quick-brief-box textarea { width: 100%; min-height: 50px; background: #0f1117; border: 1px solid #2a2d3a; border-radius: 6px; padding: 8px; color: #e0e0e0; font-size: 13px; resize: vertical; }
  .quick-brief-box textarea::placeholder { color: #555; font-style: italic; opacity: 1; }
  .brief-actions { display: flex; gap: 8px; margin-top: 10px; align-items: center; flex-wrap: wrap; }
  .brief-mode-divider { width: 1px; height: 24px; background: #2a2d3a; margin: 0 4px; }
  .brief-hint { font-size: 11px; color: #666; margin-left: auto; }
  .context-options { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 10px; padding-top: 10px; border-top: 1px solid #2a2d3a; }
  .context-label { font-size: 12px; color: #888; margin-bottom: 0 !important; }
  .context-chk { font-size: 12px; color: #ccc; display: flex; align-items: center; gap: 4px; margin-bottom: 0 !important; cursor: pointer; }
  .context-chk input { cursor: pointer; }
  .context-chk input[type="number"] { background: #0f1117; border: 1px solid #2a2d3a; border-radius: 4px; padding: 2px 6px; color: #e0e0e0; font-size: 12px; }
  .flow-step-media-btn { display: inline-block; margin-left: 8px; padding: 2px 8px; background: #2a1a3a; border: 1px solid #4a2a6a; color: #c792ea; border-radius: 6px; font-size: 11px; cursor: pointer; }
  .flow-step-media-btn:hover { background: #3a2a4a; border-color: #7c3aed; }
  .flow-step-media-btn:disabled { opacity: 0.5; cursor: wait; }
  .flow-media-action { margin-top: 10px; padding: 10px 12px; background: #1a1d2e; border: 1px solid #4a2a6a; border-radius: 8px; display: none; }
  .flow-media-action.visible { display: block; }
  .flow-media-action-label { font-size: 12px; color: #c792ea; margin-bottom: 6px; }
  .flow-media-action-btns { display: flex; gap: 8px; flex-wrap: wrap; }
  .flow-media-action-btn { padding: 6px 14px; border-radius: 8px; font-size: 13px; cursor: pointer; border: 1px solid; transition: all 0.15s; }
  .flow-media-action-btn.primary { background: #7c3aed; border-color: #7c3aed; color: #fff; font-weight: 600; }
  .flow-media-action-btn.primary:hover { background: #6d28d9; }
  .flow-media-action-btn.secondary { background: #1c1e2a; border-color: #2a2d3a; color: #888; }
  .flow-media-action-btn.secondary:hover { border-color: #4a4d6a; color: #ccc; }
  .flow-media-action-btn:disabled { opacity: 0.5; cursor: wait; }
  .flow-media-status { font-size: 12px; color: #888; margin-top: 6px; }
  .media-viewer-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); z-index: 10000; display: flex; align-items: center; justify-content: center; }
  .result-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 9000; display: none; }
  .result-overlay.visible { display: flex; justify-content: center; align-items: flex-start; padding: 40px 20px; overflow-y: auto; }
  .result-modal { background: #161821; border: 1px solid #2a2d3a; border-radius: 12px; max-width: 900px; width: 100%; padding: 24px; }
  .result-modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #2a2d3a; }
  .result-modal-title { font-size: 16px; color: #e0e0e0; }
  .result-modal-close { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 6px; padding: 4px 12px; cursor: pointer; font-size: 13px; }
  .result-modal-close:hover { border-color: #7c8aff; color: #7c8aff; }
  .result-modal-body { color: #ccc; font-size: 14px; line-height: 1.6; }
  .result-modal-body a { color: #7c8aff; text-decoration: none; }
  .result-modal-body a:hover { text-decoration: underline; }
  .media-viewer { background: #1c1e2a; border: 1px solid #2a2d3a; border-radius: 12px; max-width: 90vw; max-height: 90vh; overflow: auto; padding: 16px; }
  .media-viewer-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; color: #e0e0e0; }
  .media-viewer-header button { background: none; border: none; color: #888; font-size: 18px; cursor: pointer; }
  .media-viewer-header button:hover { color: #fff; }
  .media-viewer-body { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .media-item { background: #0f1117; border: 1px solid #2a2d3a; border-radius: 8px; overflow: hidden; }
  .media-item img, .media-item video { width: 100%; display: block; }
  .media-caption { padding: 6px 8px; font-size: 12px; color: #888; }
  .agent-settings-btn { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer; }
  .agent-settings-btn:hover { border-color: #7c8aff; color: #7c8aff; }
  .agent-options { margin: 10px 0; display: none; flex-direction: column; gap: 8px; }
  .agent-options.visible { display: flex; }
  .opt-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .opt-row-label { font-size: 11px; color: #666; min-width: 64px; }
  .opt-divider { height: 1px; background: #2a2d3a; margin: 2px 0; }
  .opt-chip { display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; border-radius: 16px; font-size: 12px; cursor: pointer; border: 1px solid #2a2d3a; background: #1c1e2a; color: #888; transition: all 0.15s; user-select: none; }
  .opt-chip:hover { border-color: #4a4d6a; color: #ccc; }
  .opt-chip.active { background: #2a1a3a; border-color: #7c3aed; color: #c792ea; }
  .opt-chip.active.green { background: #1a2a1a; border-color: #22c55e; color: #86efac; }
  .agent-options select { background: #0f1117; border: 1px solid #2a2d3a; border-radius: 6px; padding: 4px 8px; color: #e0e0e0; font-size: 12px; cursor: pointer; }
  .agent-options select:hover { border-color: #4a4d6a; }
  .agent-options .opt-count-input { background: #0f1117; border: 1px solid #2a2d3a; border-radius: 6px; padding: 4px 8px; color: #e0e0e0; font-size: 12px; width: 48px; text-align: center; }
  .agent-options .opt-label { font-size: 12px; color: #888; }

  .agent-box { background: #161821; border: 2px dashed #2a2d3a; border-radius: 16px; padding: 24px; min-height: 280px; display: flex; flex-direction: column; transition: all 0.2s; }
  .agent-box.drag-over { border-color: #7c8aff; background: #1a1d2e; }
  .agent-box.running { border-style: solid; border-color: #7c8aff; }
  .agent-box.done { border-style: solid; border-color: #4ade80; }
  .agent-box.error { border-style: solid; border-color: #ff6b6b; }
  .agent-header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
  .agent-icon { font-size: 32px; }
  .agent-title { font-size: 16px; font-weight: 600; color: #e0e0e0; }
  .agent-desc { font-size: 12px; color: #888; margin-bottom: 16px; line-height: 1.5; }

  .drop-zone { flex: 1; border-radius: 10px; padding: 12px; min-height: 80px; margin-bottom: 12px; }
  .drop-zone-empty { text-align: center; color: #444; font-size: 13px; padding: 20px; border: 1px dashed #333; border-radius: 10px; }
  .drop-subzone { margin-bottom: 8px; padding: 6px; border-radius: 10px; border: 1px dashed transparent; transition: background .12s, border-color .12s; }
  .drop-subzone-label { font-size: 11px; color: #666; margin-bottom: 4px; padding: 0 4px; }
  .drop-subzone.drop-hover-combined { background: rgba(167,139,250,.18); border-color: #a78bfa; }
  .drop-subzone.drop-hover-separate { background: rgba(74,222,128,.16); border-color: #4ade80; }
  .drop-zone.drop-hover { background: rgba(74,222,128,.12); border: 1px dashed #4ade80; }
  .combined-folder { background: #2a1a3a !important; border: 1px solid #a78bfa; }

  .dropped-folder { display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: #1c1e2a; border-radius: 8px; margin-bottom: 6px; font-size: 13px; cursor: grab; }
  .dropped-folder:active { cursor: grabbing; }
  .dropped-folder.dragging { opacity: 0.4; }
  .dropped-folder .remove-btn { margin-left: auto; color: #ff6b6b; cursor: pointer; font-size: 18px; padding: 0 4px; }
  .dropped-folder .remove-btn:hover { color: #ff8888; }

  .agent-status { font-size: 13px; min-height: 20px; margin-bottom: 12px; }
  .agent-status.running { color: #7c8aff; }
  .agent-status.done { color: #4ade80; }
  .agent-status.error { color: #ff6b6b; }

  .agent-output { font-size: 12px; color: #aaa; background: #0f1117; border-radius: 8px; padding: 12px; max-height: 200px; overflow-y: auto; white-space: pre-wrap; word-wrap: break-word; margin-bottom: 12px; display: none; }
  .agent-output.visible { display: block; }

  .agent-file-link { font-size: 12px; color: #7c8aff; cursor: pointer; margin-bottom: 12px; display: none; }
  .agent-file-link.visible { display: block; }
  .agent-file-link:hover { text-decoration: underline; }

  .agent-actions { display: flex; gap: 8px; }
  .confirm-btn { background: #4ade80; color: #0f1117; border: none; border-radius: 8px; padding: 10px 16px; font-size: 14px; font-weight: 600; cursor: pointer; flex: 1; }
  .confirm-btn:hover { background: #45c97c; }
  .confirm-btn:disabled { background: #3a3d5a; color: #888; cursor: not-allowed; }
  .stop-btn { background: #e04848; color: #fff; border: none; border-radius: 8px; padding: 10px 16px; font-size: 14px; cursor: pointer; flex: 1; display: none; }
  .stop-btn:disabled { background: #3a3d5a; cursor: not-allowed; }
  .clear-btn { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 8px; padding: 10px 12px; font-size: 13px; cursor: pointer; }
  .clear-btn:hover { border-color: #555; color: #ccc; }

  .typing { display: inline-block; animation: blink 1s infinite; }
  @keyframes blink { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }

  .hint-bar { background: #161821; border: 1px solid #2a2d3a; border-radius: 8px; padding: 12px 16px; margin-bottom: 20px; font-size: 13px; color: #888; display: flex; align-items: center; justify-content: space-between; }
  .hint-bar b { color: #7c8aff; }
  .global-confirm-btn { background: #4ade80; color: #0f1117; border: none; border-radius: 8px; padding: 8px 24px; font-size: 14px; font-weight: 600; cursor: pointer; transition: background 0.2s; }
  .global-confirm-btn:hover:not(:disabled) { background: #45c97c; }
  .global-confirm-btn:disabled { background: #3a3d5a; color: #888; cursor: not-allowed; }
  .global-confirm-btn.running { background: #e04848; color: #fff; }
  .global-confirm-btn.running:hover { background: #c03838; }
  .global-clear-btn { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 8px; padding: 8px 16px; font-size: 14px; cursor: pointer; margin-left: 8px; }
  .global-clear-btn:hover:not(:disabled) { border-color: #ff6b6b; color: #ff6b6b; }
  .global-clear-btn:disabled { border-color: #2a2d3a; color: #444; cursor: not-allowed; }
  .flow-display { background: #161821; border: 1px solid #2a2d3a; border-radius: 10px; padding: 20px; margin-bottom: 20px; display: none; }
  .flow-display.visible { display: block; }
  .flow-title { font-size: 14px; color: #888; margin-bottom: 12px; }
  .flow-title b { color: #7c8aff; }
  .flow-steps { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
  .flow-step { display: flex; align-items: center; gap: 8px; padding: 10px 16px; background: #1c1e2a; border: 1px solid #2a2d3a; border-radius: 8px; font-size: 13px; color: #888; transition: all 0.3s ease; }
  .flow-step.done { background: #1a2a1a; border-color: #22c55e; color: #86efac; }
  .flow-step.running {
    background: #1e1b3a;
    border-color: #7c3aed;
    color: #c792ea;
    box-shadow: 0 0 15px rgba(124, 58, 237, 0.4);
    animation: flow-pulse 1.5s infinite alternate;
  }
  @keyframes flow-pulse {
    0% { box-shadow: 0 0 5px rgba(124, 58, 237, 0.2); }
    100% { box-shadow: 0 0 25px rgba(124, 58, 237, 0.7); }
  }
  .flow-spinner { display: none; width: 14px; height: 14px; border: 2px solid #7c3aed; border-top-color: transparent; border-radius: 50%; animation: flow-spin 1s linear infinite; flex-shrink: 0; }
  .flow-step.running .flow-spinner { display: block; }
  @keyframes flow-spin { 100% { transform: rotate(360deg); } }
  .flow-step.auto { border: 1px dashed #4ade80; }
  .flow-step.auto .flow-step-badge { background: #1a3a2a; color: #4ade80; }
  .flow-step-icon { font-size: 18px; }
  .flow-step-name { color: #e0e0e0; }
  .flow-step.running .flow-step-name { color: #c792ea; }
  .flow-step.done .flow-step-name { color: #86efac; }
  .flow-step-badge { font-size: 10px; padding: 2px 6px; border-radius: 4px; background: #2a2d3a; color: #888; margin-left: 4px; }
  .flow-arrow { color: #555; font-size: 18px; }
  .flow-product { margin-bottom: 16px; padding-bottom: 16px; border-bottom: 1px solid #2a2d3a; }
  .flow-product:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
  .flow-explain { margin-top: 10px; padding: 10px 12px; background: #0f1117; border-radius: 8px; font-size: 11px; color: #777; line-height: 1.8; }
  .flow-explain-item { display: flex; gap: 6px; align-items: flex-start; }
  .flow-explain-item b { color: #aaa; font-weight: 500; white-space: nowrap; }

  /* Flow wizard */
  .flow-wizard { background: #161922; border: 1px solid #252a3a; border-radius: 14px; padding: 20px; }
  .wizard-stepper { display: flex; align-items: center; gap: 8px; margin-bottom: 20px; }
  .wizard-step-dot { display: flex; align-items: center; gap: 8px; padding: 8px 14px; border-radius: 20px; font-size: 13px; font-weight: 600; transition: all 0.2s; }
  .wizard-step-dot .num { width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; }
  .wizard-step-dot.active { background: rgba(99,102,241,0.15); color: #818cf8; }
  .wizard-step-dot.active .num { background: #6366f1; color: white; }
  .wizard-step-dot.done { color: #22c55e; }
  .wizard-step-dot.done .num { background: #22c55e; color: white; }
  .wizard-step-dot.pending { color: #52525b; }
  .wizard-step-dot.pending .num { background: #252a3a; color: #71717a; }
  .wizard-step-line { flex: 1; height: 2px; background: #252a3a; max-width: 40px; }
  .wizard-step-line.done { background: #22c55e; }
  .wizard-card { background: #1c2030; border: 1px solid #252a3a; border-radius: 12px; padding: 20px; display: none; }
  .wizard-card.active { display: block; }
  .wizard-card-title { font-size: 17px; font-weight: 700; margin-bottom: 6px; }
  .wizard-card-subtitle { font-size: 13px; color: #71717a; margin-bottom: 18px; }
  .product-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; }
  .product-card {
    background: #1c2030; border: 2px solid #252a3a; border-radius: 10px; padding: 14px;
    cursor: pointer; transition: all 0.15s; text-align: center;
  }
  .product-card:hover { border-color: #6366f1; transform: translateY(-2px); }
  .product-card.selected { border-color: #6366f1; background: rgba(99,102,241,0.08); }
  .product-card .icon { font-size: 28px; margin-bottom: 6px; }
  .product-card .name { font-size: 14px; font-weight: 600; }
  .product-card .badge { font-size: 10px; color: #22c55e; margin-top: 4px; }
  .product-card.auto { border-style: dashed; border-color: #6366f1; color: #818cf8; }
  .product-card.auto.selected { background: rgba(99,102,241,0.12); }
  .selected-summary {
    background: #1c2030; border: 1px solid #252a3a; border-radius: 10px;
    padding: 12px 16px; margin-top: 16px; font-size: 13px; color: #a1a1aa;
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  .selected-chip {
    display: inline-flex; align-items: center; gap: 4px; background: #252a3a;
    border-radius: 16px; padding: 4px 12px; font-size: 12px; font-weight: 500;
  }
  .selected-chip.combined { background: rgba(139,92,246,0.15); color: #a78bfa; }
  .auto-controls { background: #1c2030; border: 1px solid #252a3a; border-radius: 10px; padding: 14px 16px; margin-top: 14px; display: none; }
  .auto-controls.visible { display: block; }
  .auto-controls label { font-size: 13px; color: #a1a1aa; display: block; margin-bottom: 8px; }
  .auto-controls .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .auto-controls select, .auto-controls input {
    background: #0d0f14; border: 1px solid #252a3a; border-radius: 8px;
    padding: 8px 12px; color: #e4e4e7; font-size: 13px;
  }
  .agent-list { display: flex; flex-direction: column; gap: 8px; }
  .agent-row {
    display: flex; align-items: center; gap: 12px; background: #1c2030;
    border: 1px solid #252a3a; border-radius: 10px; padding: 12px 16px;
    transition: all 0.15s; cursor: default;
  }
  .agent-row:hover { border-color: #3a3f5a; }
  .agent-row.dragging { opacity: 0.4; }
  .agent-row.drag-over { border-color: #6366f1; background: rgba(99,102,241,0.08); }
  .agent-row .drag-handle {
    font-size: 18px; color: #71717a; cursor: grab; flex-shrink: 0;
    user-select: none; padding: 4px; line-height: 1;
  }
  .agent-row .drag-handle:active { cursor: grabbing; }
  .agent-row .order-num {
    width: 28px; height: 28px; border-radius: 50%; background: #252a3a;
    display: flex; align-items: center; justify-content: center; font-size: 13px;
    font-weight: 700; color: #a1a1aa; flex-shrink: 0;
  }
  .agent-row .agent-icon { font-size: 20px; }
  .agent-row .agent-name { font-size: 14px; font-weight: 600; }
  .agent-row .agent-desc { font-size: 12px; color: #71717a; }
  .agent-row .agent-text { flex: 1; min-width: 0; }
  .agent-row .settings-btn {
    background: #252a3a; border: none; color: #a1a1aa; width: 28px; height: 28px;
    border-radius: 6px; cursor: pointer; font-size: 14px; display: flex; align-items: center; justify-content: center;
  }
  .agent-row .settings-btn:hover { background: #3a3f5a; color: white; }
  .agent-row .remove-btn {
    background: none; border: none; color: #71717a; cursor: pointer; font-size: 18px; padding: 4px;
  }
  .agent-row .remove-btn:hover { color: #ef4444; }
  .add-agent-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .add-agent-chip {
    display: flex; align-items: center; gap: 6px; padding: 8px 12px;
    background: #1c2030; border: 1px solid #252a3a; border-radius: 8px;
    color: #a1a1aa; font-size: 13px; cursor: pointer; transition: all 0.15s;
  }
  .add-agent-chip:hover { border-color: #6366f1; color: #e4e4e7; }
  .add-agent-empty { color: #71717a; font-size: 13px; margin-top: 12px; }
  .opt-group { margin-bottom: 18px; }
  .opt-group:last-child { margin-bottom: 0; }
  .opt-label { font-size: 13px; color: #a1a1aa; margin-bottom: 6px; display: block; font-weight: 600; }
  .opt-chips { display: flex; gap: 8px; flex-wrap: wrap; }
  .opt-chip {
    background: #1c2030; border: 1px solid #252a3a; border-radius: 8px; padding: 8px 14px;
    font-size: 13px; cursor: pointer; transition: all 0.15s; font-weight: 500;
  }
  .opt-chip:hover { border-color: #6366f1; }
  .opt-chip.active { background: rgba(99,102,241,0.15); border-color: #6366f1; color: #818cf8; }
  .opt-input-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .opt-input-row input {
    background: #1c2030; border: 1px solid #252a3a; border-radius: 8px; padding: 8px 12px;
    color: #e4e4e7; font-size: 14px; width: 80px;
  }
  .opt-input-row input:focus { outline: none; border-color: #6366f1; }
  .review-flow {
    background: #1c2030; border: 1px solid #252a3a; border-radius: 10px;
    padding: 16px; margin-bottom: 12px;
  }
  .review-flow-header { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .review-flow-title { font-size: 15px; font-weight: 700; }
  .review-flow-badge { font-size: 11px; padding: 3px 8px; border-radius: 6px; font-weight: 600; }
  .review-flow-badge.combined { background: rgba(139,92,246,0.15); color: #a78bfa; }
  .review-flow-badge.separate { background: rgba(34,197,94,0.15); color: #22c55e; }
  .review-flow-badge.auto { background: rgba(99,102,241,0.15); color: #818cf8; }
  .review-steps { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 13px; color: #a1a1aa; }
  .review-step { display: flex; align-items: center; gap: 4px; }
  .review-arrow { color: #52525b; }
  .review-opts { margin-top: 10px; font-size: 12px; color: #71717a; padding-top: 10px; border-top: 1px solid #252a3a; }
  .wizard-nav { display: flex; gap: 10px; margin-top: 20px; }
  .wizard-nav .spacer { flex: 1; }
  .btn { padding: 12px 24px; border-radius: 10px; font-size: 14px; cursor: pointer; border: none; font-weight: 600; transition: all 0.15s; }
  .btn-primary { background: linear-gradient(135deg, #6366f1, #8b5cf6); color: white; }
  .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 4px 16px rgba(99,102,241,0.3); }
  .btn-primary:disabled { background: #252a3a; color: #52525b; cursor: not-allowed; transform: none; box-shadow: none; }
  .btn-secondary { background: #1c2030; color: #a1a1aa; border: 1px solid #252a3a; }
  .btn-secondary:hover { border-color: #3a3f5a; color: #e4e4e7; }
  .flow-tabs { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; }
  .flow-tab {
    background: #1c2030; border: 1px solid #252a3a; border-radius: 8px; padding: 12px 14px;
    font-size: 13px; cursor: pointer; display: flex; align-items: center; justify-content: space-between; gap: 10px; font-weight: 500;
  }
  .flow-tab.active { border-color: #6366f1; background: rgba(99,102,241,0.1); color: #818cf8; }
  .flow-tab .tab-x { color: #71717a; font-size: 14px; }
  .flow-tab .tab-x:hover { color: #ef4444; }
  .flow-tab-add { border-style: dashed; color: #818cf8; justify-content: center; }
  #flow-wizard-list { display: flex; flex-direction: column; gap: 20px; margin-top: 20px; }
  .flow-box { background: #1c2030; border: 1px solid #252a3a; border-radius: 12px; padding: 20px; }
  .flow-box-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
  .flow-box-title { font-size: 16px; font-weight: 600; color: #a78bfa; }
  .flow-box-remove { color: #71717a; font-size: 16px; cursor: pointer; }
  .flow-box-remove:hover { color: #ef4444; }
  .flow-box-nav { display: flex; gap: 10px; margin-top: 20px; }
  .flow-box-nav .spacer { flex: 1; }
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <h1>MKTApp</h1>
    <p>เลือกสินค้า → เรียงลำดับ agent → ตั้งค่า content → กดยืนยันรัน flow</p>
  </div>
  <div class="header-right">
    <div class="credits-badge" id="credits-badge" style="display:none">กำลังโหลด...</div>
    <button class="home-header-btn" onclick="openScheduleList()" id="schedule-header-btn" style="position:relative;">📅 ตารางเวลา<span id="schedule-badge" style="display:none;position:absolute;top:-4px;right:-4px;background:#fbbf24;color:#0f1117;border-radius:10px;font-size:10px;padding:1px 6px;font-weight:700;">●</span></button>
    <button class="home-header-btn" onclick="openPillarsModal()">🎯 Pillars</button>
    <button class="home-header-btn" onclick="goHome()">🏠 หน้าหลัก</button>
  </div>
</div>
<div class="container">
  <div class="sidebar">
    <div class="sidebar-tabs">
      <div class="sidebar-tab active" onclick="switchSidebarTab('folders', event)">สินค้า</div>
      <div class="sidebar-tab" onclick="switchSidebarTab('sessions', event)">ผลลัพธ์</div>
      <div class="sidebar-tab" onclick="switchSidebarTab('brand', event)">แบรนด์</div>
    </div>
    <div class="sidebar-content" id="sidebar-content"></div>
  </div>
  <div id="main-area" class="main">
    <div class="hint-bar">
      <span><b>วิธีใช้:</b> สร้าง flow ทีละขั้น → เลือกสินค้า → เลือก agent → ตั้งค่า → ยืนยัน</span>
    </div>

    <div class="flow-wizard" id="flow-wizard">
      <div class="wizard-brief-box">
        <label class="opt-label">คำสั่งเพิ่มเติม (ถ้าขัดแย้งกับค่าเริ่มต้น ให้ทำตามคำสั่งนี้แทน)</label>
        <textarea id="quick-brief-input" class="brief-box" placeholder="เช่น 'เน้นจุดขายกันน้ำ'" style="width:100%; background:#1c2030; border:1px solid #252a3a; border-radius:10px; padding:12px 16px; color:#e4e4e7; font-size:14px; resize:vertical; min-height:80px;"></textarea>
        <div id="quick-brief-conflict-banner" style="display:none;margin-top:8px"></div>
      </div>
      <div id="flow-wizard-list"></div>
    </div>
  </div>
</div>
<script>
let currentSidebarTab = 'folders';
let runningAgents = {};
let abortController = null;
let agentFolders = {};      // legacy — ไม่ใช้แล้วแต่เก็บไว้เพื่อไม่พัง
let flows = [];             // new wizard flow data (per-flow step state lives in wizard_ui.js as flowSteps[])
let wizardFlowCounter = 0;
let savedAgentView = '';
let _currentMediaSession = '';
let _currentMediaFile = '';
let _currentMediaContent = '';
let _currentMediaPost = null;
let _currentMediaPosts = [];   // ทุก posts ในไฟล์ (รองรับหลายแพลตฟอร์ม)
let _currentMediaPostIdx = 0;  // index ของ post ที่กำลังดูอยู่
let _resultNavFiles = [];   // รายการไฟล์ทั้งหมดในการสร้างครั้งนั้น (สำหรับ navigation)
let _resultNavIdx = 0;      // index ของไฟล์ที่กำลังดูอยู่
let multiSelectMode = false;
const AUTO_ITEM = '__auto__';  // special "product" ที่แทน Auto mode
let readyProductCount = 0;  // จำนวนสินค้า ready — สำหรับ max ใน Auto count input
let selectedFolders = new Set();
let _sidebarPollTimer = null;

// Auto-poll sidebar ตอนมี ingestion ทำงาน — ไม่ต้องเปิด modal ก็เห็น progress
function startSidebarPolling() {
  if (_sidebarPollTimer) return;
  _sidebarPollTimer = setInterval(() => {
    fetch('/api/data_folders').then(r => r.json()).then(folders => {
      const anyProcessing = folders.some(f => f.status === 'processing');
      if (currentSidebarTab === 'folders') loadFolderList();
      if (!anyProcessing) {
        clearInterval(_sidebarPollTimer);
        _sidebarPollTimer = null;
      }
    }).catch(() => {});
  }, 2000);
}


const AGENT_ORDER = ['product_spec', 'competitor_analysis', 'campaign_strategy', 'content_creator'];
const AGENT_DEPENDENCIES = {
  product_spec: [],
  competitor_analysis: ['product_spec'],
  campaign_strategy: ['product_spec', 'competitor_analysis'],
  content_creator: ['product_spec', 'competitor_analysis', 'campaign_strategy'],
};
const AGENT_INFO = {
  product_spec: { name: 'นักวิเคราะห์สินค้า', desc: 'สร้างสเปคสินค้าจากข้อมูลดิบ', icon: '📋', flow_reason: 'อ่านข้อมูลดิบแล้วสรุปเป็นสเปคสินค้า ซึ่งเป็นฐานให้ agent อื่นใช้ต่อ' },
  competitor_analysis: { name: 'นักวิเคราะห์คู่แข่ง', desc: 'วิเคราะห์เปรียบเทียบคู่แข่ง (ค้นหา web เอง)', icon: '🔍', flow_reason: 'ใช้สเปคสินค้าค้นหาคู่แข่งบนเว็บ แล้วสรุปจุดเด่น/จุดอ่อนเทียบกับเรา' },
  campaign_strategy: { name: 'นักวางกลยุทธ์แคมเปญ', desc: 'วางกลยุทธ์แคมเปญ + ราคาแนะนำ', icon: '📊', flow_reason: 'ใช้สเปคสินค้า + ข้อมูลคู่แข่งเพื่อวางกลยุทธ์ขายและกำหนดราคาแนะนำ' },
  content_creator: { name: 'นักสร้างคอนเทนต์', desc: 'สร้าง content + prompt รูป + hashtag', icon: '✍️', flow_reason: 'ใช้สเปคสินค้า + กลยุทธ์แคมเปญเขียนคอนเทนต์พร้อม prompt รูปและ hashtag' },
};

function switchSidebarTab(tab, ev) {
  currentSidebarTab = tab;
  document.querySelectorAll('.sidebar-tab').forEach(t => t.classList.remove('active'));
  if (ev) ev.target.classList.add('active');
  if (tab === 'folders') loadFolderList();
  else if (tab === 'sessions') loadSessions();
  else if (tab === 'brand') loadBrandFiles();
}

function loadFolderList() {
  return fetch('/api/data_folders').then(r => r.json()).then(folders => {
    const el = document.getElementById('sidebar-content');
    // นับสินค้า ready สำหรับ max ใน Auto count input
    readyProductCount = folders.filter(f => f.status === 'ready').length;
    let html = '<div class="sidebar-toolbar">';
    html += '<div class="sidebar-toolbar-row">';
    html += '<input type="text" class="sidebar-search" id="folder-search" placeholder="ค้นหาสินค้า..." oninput="filterFolders()">';
    html += '<button class="sidebar-add-btn" onclick="openUploadModal()">+ เพิ่ม</button>';
    html += '</div>';
    html += '</div>';
    if (!folders.length) {
      html += '<div style="color:#555;font-size:12px;padding:12px">ยังไม่มีโฟลเดอร์ — กด + เพิ่ม เพื่อสร้าง</div>';
    } else {
      for (const f of folders) {
        const fileCount = f.file_count || 0;
        const deliverableCount = f.deliverable_count || 0;
        const rawCount = f.raw_count != null ? f.raw_count : fileCount;
        const status = f.status || 'empty';
        // 5 สถานะ: empty / no_usable_data / processing / ready / stale
        const statusMap = {
          'empty':          {badge: 'badge-empty',          label: 'ว่าง',           color: '#555'},
          'pending':        {badge: 'badge-pending',        label: 'รอประมวลผล',     color: '#facc15'},
          'no_usable_data': {badge: 'badge-empty',          label: 'ไม่มีข้อมูลใช้ได้', color: '#f87171'},
          'processing':     {badge: 'badge-pending',        label: 'กำลังประมวลผล',  color: '#facc15'},
          'ready':          {badge: 'badge-ready',          label: 'พร้อม',          color: '#4ade80'},
          'stale':          {badge: 'badge-pending',        label: 'ข้อมูลเก่า',      color: '#fb923c'},
        };
        const si = statusMap[status] || statusMap['empty'];
        const statusBadge = '<span class="badge ' + si.badge + '">' + si.label + '</span>';
        // ใช้งาน agent ได้เฉพาะ ready และ stale (stale ใช้ของเก่าได้)
        const canUseAgent = (status === 'ready' || status === 'stale');
        const isEmpty = (status === 'empty' || status === 'no_usable_data');
        const isProcessing = (status === 'processing');
        const isPending = (status === 'pending');
        const safePath = f.path.replace(/'/g, "\\'");
        const isSelected = selectedFolders.has(f.path);
        const selectedCls = isSelected ? ' folder-item-selected' : '';

        // progress แสดงถ้ากำลัง ingestion
        let progressHtml = '';
        if (isProcessing && f.progress) {
          const p = f.progress;
          const pct = p.total > 0 ? Math.round(p.step / p.total * 100) : 0;
          progressHtml = '<div style="margin-top:4px;font-size:10px;color:#facc15">' + escapeHtml(p.message || '') + ' (' + pct + '%)' + (p.eta_seconds != null ? ' ~' + p.eta_seconds + 's' : '') + '</div>';
        }

        if (multiSelectMode && canUseAgent) {
          // Multi-select mode: click card toggles selection, no drag
          html += '<div class="folder-item' + selectedCls + '" data-name="' + escapeHtml(f.name).toLowerCase() + '" data-folder="' + escapeHtml(f.path) + '" data-status="' + status + '" data-fname="' + escapeHtml(f.name) + '" onclick="toggleFolderSelect(\'' + safePath + '\')">';
          const checkCls = isSelected ? 'ms-check checked' : 'ms-check';
          html += '<span class="' + checkCls + '">' + (isSelected ? '✓' : '') + '</span>';
        } else if (isEmpty) {
          const title = status === 'no_usable_data' ? 'มีไฟล์แต่ไม่รองรับ — ลากไม่ได้' : 'ยังไม่มีไฟล์ข้อมูล — ลากไม่ได้';
          html += '<div class="folder-item folder-item-disabled" data-name="' + escapeHtml(f.name).toLowerCase() + '" data-folder="' + escapeHtml(f.path) + '" data-status="' + status + '" data-fname="' + escapeHtml(f.name) + '" title="' + title + '">';
        } else if (isProcessing) {
          html += '<div class="folder-item folder-item-disabled" data-name="' + escapeHtml(f.name).toLowerCase() + '" data-folder="' + escapeHtml(f.path) + '" data-status="' + status + '" data-fname="' + escapeHtml(f.name) + '" title="กำลังประมวลผลข้อมูล — รอให้พร้อมก่อน">';
        } else if (isPending) {
          html += '<div class="folder-item folder-item-disabled" data-name="' + escapeHtml(f.name).toLowerCase() + '" data-folder="' + escapeHtml(f.path) + '" data-status="' + status + '" data-fname="' + escapeHtml(f.name) + '" title="มีไฟล์แต่ยังไม่ได้ประมวลผล — กด⚙ เพื่อประมวลผลข้อมูลก่อน">';
        } else {
          // ready หรือ stale → คลิกเลือกใน wizard
          const staleTitle = status === 'stale' ? 'ข้อมูลเก่า — แนะนำให้กดประมวลผลใหม่' : '';
          html += '<div class="folder-item" data-name="' + escapeHtml(f.name).toLowerCase() + '" data-folder="' + escapeHtml(f.path) + '" data-status="' + status + '" data-fname="' + escapeHtml(f.name) + '" title="' + staleTitle + '">';
        }
        const thumb = f.thumbnail
          ? '<img src="/api/product_image/' + encodeURIComponent(f.path) + '" alt="">'
          : '📁';
        html += '<span class="folder-icon">' + thumb + '</span>';
        if (multiSelectMode && canUseAgent) {
          html += '<div class="folder-info">';
        } else {
          html += '<div class="folder-info" onclick="toggleFolderFiles(\'' + safePath + '\',this)" style="cursor:pointer">';
        }
        html += '<div class="folder-name">' + escapeHtml(f.name) + '</div>';
        let metaExtra = '';
        if (status === 'stale') metaExtra = ' · คลิก⚙ เพื่อประมวลผลใหม่';
        if (status === 'pending') metaExtra = ' · กด⚙ เพื่อประมวลผลข้อมูล';
        if (status === 'no_usable_data') metaExtra = ' · ไฟล์ไม่รองรับ';
        html += '<div class="folder-meta">' + statusBadge + ' <span style="font-size:11px;color:#888">' + metaExtra + '</span></div>';
        html += progressHtml;
        html += '</div>';
        if (!(multiSelectMode && canUseAgent)) {
          if (status === 'ready' || status === 'stale') {
            html += '<span class="folder-manage-btn" onclick="openFolderManage(\'' + safePath + '\', this.parentElement.dataset.status)" title="จัดการไฟล์/สินค้า">⚙</span>';
          } else {
            html += '<span class="folder-manage-btn" style="opacity:0.3;cursor:not-allowed" title="รอประมวลผลข้อมูล/ตั้งค่าสินค้าเสร็จก่อน">⚙</span>';
          }
        }
        html += '</div>';
        if (!(multiSelectMode && canUseAgent)) {
          html += '<div id="files-list-' + f.path.replace(/[^a-zA-Z0-9]/g,'_') + '" class="folder-files-list" style="display:none"></div>';
        }
      }
    }
    // Action bar at bottom when in multi-select mode
    if (multiSelectMode) {
      const n = selectedFolders.size;
      html += '<div class="ms-action-bar">';
      html += '<span class="ms-count">เลือกแล้ว: ' + n + ' ชิ้น</span>';
      html += '<button class="ms-clear-btn" onclick="clearFolderSelection()">ยกเลิกเลือก</button>';
      html += '<button class="ms-send-btn"' + (n === 0 ? ' disabled' : '') + ' onclick="openSendToAgentModal()">ส่งไป agent →</button>';
      html += '</div>';
    }
    el.innerHTML = html;
    // ถ้ามีสินค้ากำลัง ingestion → เริ่ม poll sidebar อัตโนมัติ
    const anyProcessing = folders.some(f => f.status === 'processing');
    if (anyProcessing) startSidebarPolling();
  });
}

function toggleMultiSelect() {
  multiSelectMode = !multiSelectMode;
  if (!multiSelectMode) selectedFolders.clear();
  loadFolderList();
}

function toggleFolderSelect(folderPath) {
  if (selectedFolders.has(folderPath)) {
    selectedFolders.delete(folderPath);
  } else {
    selectedFolders.add(folderPath);
  }
  loadFolderList();
}

function clearFolderSelection() {
  selectedFolders.clear();
  loadFolderList();
}

// ---- Send-to-agent modal ----
let _sendtoAgent = null;
let _sendtoZone = 'separate';

function openSendToAgentModal() {
  if (selectedFolders.size === 0) return;
  _sendtoAgent = null;
  _sendtoZone = 'separate';

  // Summary
  const names = Array.from(selectedFolders).map(p => p.split('/').pop());
  document.getElementById('sendto-summary').textContent = 'สินค้าที่เลือก: ' + names.join(', ');

  // Agent list
  const listEl = document.getElementById('sendto-agent-list');
  let html = '';
  for (const key of AGENT_ORDER) {
    const info = AGENT_INFO[key];
    html += '<div class="sendto-agent-card" onclick="setSendToAgent(\'' + key + '\')" id="sendto-agent-' + key + '">';
    html += '<span class="agent-emoji">' + info.icon + '</span><span>' + info.name + '</span>';
    html += '</div>';
  }
  listEl.innerHTML = html;

  // Reset zone row (hidden until agent chosen)
  document.getElementById('sendto-zone-row').style.display = 'none';
  document.getElementById('sendto-zone-combined').classList.remove('selected');
  document.getElementById('sendto-zone-separate').classList.remove('selected');
  document.getElementById('sendto-confirm').disabled = true;

  document.getElementById('sendto-overlay').className = 'settings-modal-overlay visible';
}

function closeSendToAgentModal() {
  document.getElementById('sendto-overlay').className = 'settings-modal-overlay';
}

function setSendToAgent(key) {
  _sendtoAgent = key;
  // Highlight selected card
  for (const k of AGENT_ORDER) {
    const el = document.getElementById('sendto-agent-' + k);
    if (el) el.classList.toggle('selected', k === key);
  }
  // Show zone row only for agents that support combined
  const showCombined = (key === 'content_creator' || key === 'campaign_strategy');
  const zoneRow = document.getElementById('sendto-zone-row');
  zoneRow.style.display = showCombined ? 'block' : 'none';
  // Default zone: separate (or combined if only 1 product and combined not applicable)
  _sendtoZone = 'separate';
  document.getElementById('sendto-zone-separate').classList.add('selected');
  document.getElementById('sendto-zone-combined').classList.remove('selected');
  document.getElementById('sendto-confirm').disabled = false;
}

function setSendToZone(zone) {
  _sendtoZone = zone;
  document.getElementById('sendto-zone-combined').classList.toggle('selected', zone === 'combined');
  document.getElementById('sendto-zone-separate').classList.toggle('selected', zone === 'separate');
}

function confirmSendToAgent() {
  if (!_sendtoAgent || selectedFolders.size === 0) return;
  const key = _sendtoAgent;
  if (!agentFolders[key]) agentFolders[key] = { separate: [], combined: [] };
  const showCombined = (key === 'content_creator' || key === 'campaign_strategy');
  const zone = (showCombined && _sendtoZone === 'combined') ? 'combined' : 'separate';

  for (const folderPath of selectedFolders) {
    if (zone === 'combined') {
      // Remove from separate if present, add to combined
      agentFolders[key].separate = agentFolders[key].separate.filter(f => f !== folderPath);
      if (!agentFolders[key].combined.includes(folderPath)) {
        agentFolders[key].combined.push(folderPath);
      }
    } else {
      // Remove from combined if present, add to separate
      agentFolders[key].combined = agentFolders[key].combined.filter(f => f !== folderPath);
      if (!agentFolders[key].separate.includes(folderPath)) {
        agentFolders[key].separate.push(folderPath);
      }
    }
  }
  updateDropZone(key);
  closeSendToAgentModal();
  // Exit multi-select mode after sending
  multiSelectMode = false;
  selectedFolders.clear();
  loadFolderList();
}

function filterFolders() {
  const q = document.getElementById('folder-search').value.toLowerCase();
  document.querySelectorAll('.folder-item').forEach(item => {
    const name = item.getAttribute('data-name') || '';
    item.style.display = name.includes(q) ? '' : 'none';
  });
}

function toggleFolderFiles(folder, el) {
  const listId = 'files-list-' + folder.replace(/[^a-zA-Z0-9]/g,'_');
  const listEl = document.getElementById(listId);
  if (listEl.style.display === 'block') {
    listEl.style.display = 'none';
    return;
  }
  listEl.style.display = 'block';
  fetch('/api/folder_files/' + encodeURIComponent(folder)).then(r => r.json()).then(files => {
    let html = '<div style="padding:4px 12px 8px 28px">';
    if (!files.length) {
      html += '<div style="font-size:12px;color:#555">ไม่มีไฟล์</div>';
    } else {
      for (const f of files) {
        const icon = f.is_ready ? '✅' : '📄';
        const tag = f.is_ready ? '<span style="font-size:10px;color:#4ade80">ready</span>' : '';
        html += '<div class="file-row">';
        html += '<span>' + icon + '</span><span class="file-row-name">' + escapeHtml(f.name) + '</span>' + tag;
        html += '</div>';
      }
    }
    html += '</div>';
    listEl.innerHTML = html;
  });
}

function openFolderManage(folder, status) {
  openUploadModalForFolder(folder, status);
}

function deleteFile(folder, filepath) {
  if (!confirm('ลบไฟล์ ' + filepath + ' ?')) return;
  fetch('/api/folder_file/' + encodeURIComponent(folder), {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder: folder, filepath: filepath }),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      toggleFolderFiles(folder, null);
      loadFolderList();
    }
  });
}

function deleteFolder(folder) {
  if (!confirm('ลบโฟลเดอร์ ' + folder + ' และไฟล์ทั้งหมดข้างใน ?')) return;
  fetch('/api/folder', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder: folder }),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      loadFolderList();
    }
  });
}

let _uploadQueue = [];
let _editingFolder = null;
let _supportedFormats = null;

function loadSupportedFormats() {
  if (_supportedFormats) {
    renderSupportedFormats(_supportedFormats);
    return;
  }
  fetch('/api/supported_formats').then(r => r.json()).then(data => {
    _supportedFormats = data;
    renderSupportedFormats(_supportedFormats);
  }).catch(() => {
    document.getElementById('supported-formats-info').innerHTML = '';
  });
}

function renderSupportedFormats(data) {
  const formats = data.supported || data;
  const maxSizes = data.max_file_size_mb || {};
  const labels = {text: 'ข้อมูลดิบ', image: 'รูปภาพ', video: 'วิดีโอ', audio: 'เสียง'};
  let parts = ['กดเพิ่มได้หลายไฟล์'];
  for (const [type, exts] of Object.entries(formats)) {
    const label = labels[type] || type;
    const maxMb = maxSizes[type];
    let t = `${label}: ${exts.join(' ')}`;
    if (maxMb) t += ` (สูงสุด ${maxMb} MB)`;
    parts.push(t);
  }
  parts.push('⚠️ ไฟล์อื่น: ข้ามและแจ้งให้ทราบ');
  const tooltip = parts.join(' | ');
  const tipEl = document.getElementById('upload-formats-tooltip');
  if (tipEl) tipEl.title = tooltip;
  document.getElementById('supported-formats-info').innerHTML = '';
}

function _makeProductNameFromFile(file) {
  const base = (file.name || '').replace(/\.[^.]+$/, '').trim();
  const clean = base.replace(/[^\u0E00-\u0E7A\w\s-]/g, '').replace(/\s+/g, ' ').trim();
  return clean || 'สินค้า-' + Date.now();
}

function openUploadModal() {
  _uploadQueue = [];
  _editingFolder = null;
  _ppFolder = '';
  _productProfileOriginal = null;
  _uploadModalOriginal = null;
  console.log('[openUploadModal] start');
  const ppSection = document.getElementById('pp-section');
  if (ppSection) ppSection.style.display = 'none';
  const nameInput = document.getElementById('upload-product-name-modal');
  if (nameInput) { nameInput.value = ''; nameInput.disabled = false; nameInput.style.display = 'none'; }
  const nameLabel = document.getElementById('upload-name-label');
  if (nameLabel) { nameLabel.textContent = ''; nameLabel.style.display = 'none'; }
  renderUploadQueueModal();
  _uploadModalOriginal = _getUploadModalData();
  const status = document.getElementById('upload-modal-status');
  if (status) status.textContent = '';
  const title = document.getElementById('upload-modal-title');
  if (title) title.textContent = 'เพิ่มสินค้าใหม่';
  const existing = document.getElementById('existing-files-modal');
  if (existing) existing.innerHTML = '';
  const delBtn = document.getElementById('upload-delete-product-btn');
  if (delBtn) delBtn.style.display = 'none';
  const submit = document.getElementById('upload-submit-btn');
  if (submit) submit.textContent = 'อัปโหลด';
  loadSupportedFormats();
  const overlay = document.getElementById('upload-overlay');
  if (overlay) {
    overlay.classList.remove('visible');
    overlay.classList.add('visible');
    console.log('[openUploadModal] overlay visible', overlay);
  } else {
    console.error('[openUploadModal] overlay not found');
  }
}

function openUploadModalForFolder(folder, status) {
  _uploadQueue = [];
  _editingFolder = folder;
  _ppFolder = '';
  _productProfileOriginal = null;
  _uploadModalOriginal = null;
  const section = document.getElementById('pp-section');
  if (section) section.style.display = 'none';
  const nameInput = document.getElementById('upload-product-name-modal');
  nameInput.value = folder;
  nameInput.disabled = false;
  nameInput.style.display = 'block';
  const nameLabel = document.getElementById('upload-name-label');
  nameLabel.textContent = 'ชื่อสินค้า';
  nameLabel.style.display = 'block';
  renderUploadQueueModal();
  document.getElementById('upload-modal-status').textContent = '';
  // Title is fixed in manage mode — no fetch needed
  document.getElementById('upload-modal-title').textContent = 'จัดการสินค้า: ' + folder;
  // Show existing files with delete buttons + product profile section only when data is usable
  loadExistingFilesInModal(folder);
  if (status === 'ready' || status === 'stale') {
    loadProductProfileForManage(folder);
  }
  // Show delete product button
  document.getElementById('upload-delete-product-btn').style.display = 'block';
  document.getElementById('upload-submit-btn').textContent = 'บันทึก';
  loadSupportedFormats();
  _uploadModalOriginal = _getUploadModalData();
  document.getElementById('upload-overlay').className = 'settings-modal-overlay visible';
}

function loadExistingFilesInModal(folder) {
  const el = document.getElementById('existing-files-modal');
  fetch('/api/folder_files/' + encodeURIComponent(folder)).then(r => r.json()).then(files => {
    if (!files.length) {
      el.innerHTML = '<div style="font-size:12px;color:#555;padding:8px 0">ยังไม่มีไฟล์ — เพิ่มไฟล์ด้านบนแล้วกดอัปโหลด</div>';
      return;
    }
    // สถานะ/ความคืบหน้าการประมวลผล (แสดงเฉพาะตอนกำลังทำงาน)
    let html = '<div id="ingest-status-display" style="font-size:12px;color:#888;padding:8px 0;display:none">กำลังตรวจสถานะ...</div>';
    html += '<div id="ingest-progress-bar" style="display:none;margin-top:8px"></div>';
    html += '<label style="margin-top:10px;display:block;font-size:12px;color:#888">ไฟล์ที่มีอยู่</label>';
    const statusIcons = {
      'ingested':    {icon: '✅', color: '#4ade80', label: 'ใช้แล้ว'},
      'unsupported': {icon: '⚠️', color: '#fbbf24', label: 'ไม่รองรับ'},
      'error':       {icon: '❌', color: '#f87171', label: 'error'},
      'pending':     {icon: '⏳', color: '#888',    label: 'รอประมวลผล'},
      'deliverable': {icon: '📋', color: '#7dd3fc', label: 'เอกสาร'},
    };
    for (const f of files) {
      const si = statusIcons[f.status] || statusIcons['pending'];
      const safePath = f.path.replace(/'/g, "\\'");
      const typeLabel = f.type ? ' <span style="font-size:10px;color:#666">(' + f.type + ')</span>' : '';
      const errorInfo = f.error ? ' <span style="font-size:10px;color:#f87171">' + escapeHtml(f.error) + '</span>' : '';
      html += '<div class="upload-queue-item">';
      html += '<span>' + si.icon + '</span><span class="upload-queue-name">' + escapeHtml(f.name) + typeLabel + '</span>';
      html += '<span style="font-size:10px;color:' + si.color + '">' + si.label + '</span>' + errorInfo;
      html += '<span class="upload-queue-del" onclick="deleteFileInModal(\'' + folder.replace(/'/g,"\\'") + '\',\'' + safePath + '\')">×</span>';
      html += '</div>';
    }
    el.innerHTML = html;
    // โหลดสถานะ ingestion ปัจจุบัน
    refreshIngestStatus(folder);
  });
}

function refreshIngestStatus(folder) {
  const statusEl = document.getElementById('ingest-status-display');
  const btn = document.getElementById('ingest-btn');
  const bar = document.getElementById('ingest-progress-bar');
  if (!statusEl) return;
  fetch('/api/ingest_status/' + encodeURIComponent(folder)).then(r => r.json()).then(data => {
    const status = data.status || 'empty';
    const statusLabels = {
      'empty':          {label: 'ว่าง — ยังไม่มีไฟล์',           color: '#555'},
      'pending':        {label: 'มีไฟล์ — กดประมวลผลเพื่อเตรียมข้อมูล', color: '#facc15'},
      'no_usable_data': {label: 'ไม่มีไฟล์ที่รองรับ',             color: '#f87171'},
      'processing':     {label: 'กำลังประมวลผลข้อมูล...',        color: '#facc15'},
      'ready':          {label: '✓ พร้อมใช้งาน',                  color: '#4ade80'},
      'stale':          {label: '⚠ ข้อมูลเก่า — แนะนำให้ประมวลผลใหม่', color: '#fb923c'},
    };
    const si = statusLabels[status] || statusLabels['empty'];
    if (status === 'ready') {
      statusEl.innerHTML = '';
      statusEl.style.display = 'none';
    } else {
      statusEl.innerHTML = '<span style="color:' + si.color + '">' + si.label + '</span>';
      statusEl.style.display = 'block';
    }
    if (btn) {
      btn.disabled = (status === 'processing');
      btn.textContent = (status === 'ready' || status === 'stale') ? '🔄 ประมวลผลใหม่' : '🔄 ประมวลผล';
    }
    // โชว์ progress bar ถ้ากำลัง ingestion
    if (status === 'processing' && data.progress) {
      const p = data.progress;
      const pct = p.total > 0 ? Math.round(p.step / p.total * 100) : 0;
      bar.style.display = 'block';
      bar.innerHTML = '<div style="font-size:11px;color:#facc15;margin-bottom:4px">' + escapeHtml(p.message || '') + ' (' + pct + '%)' + (p.eta_seconds != null ? ' · ~' + p.eta_seconds + 's' : '') + '</div><div style="background:#1c1e2a;border-radius:4px;height:6px;overflow:hidden"><div style="background:#facc15;height:100%;width:' + pct + '%;transition:width 0.5s"></div></div>';
    } else {
      bar.style.display = 'none';
    }
  });
}

let _ingestPollTimer = null;

function startIngestion(folder) {
  if (_productProfileSavingFor === folder) { alert('กรุณารอให้บันทึกข้อมูลเสร็จก่อน'); return; }
  if (_isProductProfileDirty() || _isUploadModalDirty()) {
    if (!confirm('คุณมีการเปลี่ยนแปลงยังไม่บันทึก การประมวลผลใหม่อาจเขียนทับข้อมูลทีแก้ไว้ ต้องการดำเนินการต่อหรือไม่?')) return;
  }
  const btn = document.getElementById('ingest-btn');
  if (btn) btn.disabled = true;
  fetch('/api/ingest/' + encodeURIComponent(folder), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({force: false}),
  }).then(r => r.json()).then(data => {
    if (data.error) {
      alert(data.error);
      if (btn) btn.disabled = false;
      return;
    }
    // เริ่ม poll progress ทุก 2 วินาที
    if (_ingestPollTimer) clearInterval(_ingestPollTimer);
    refreshIngestStatus(folder);
    _ingestPollTimer = setInterval(() => {
      refreshIngestStatus(folder);
      // หยุด poll ถ้าไม่ใช่ processing
      fetch('/api/ingest_status/' + encodeURIComponent(folder)).then(r => r.json()).then(d => {
        if (d.status !== 'processing') {
          clearInterval(_ingestPollTimer);
          _ingestPollTimer = null;
          loadExistingFilesInModal(folder);  // refresh รายการไฟล์
          loadFolderList();                  // refresh sidebar
          if (d.status === 'ready' && _editingFolder === folder) {
            loadProductProfileForManage(folder);
          }
        }
      });
    }, 2000);
  }).catch(e => {
    alert('เกิดข้อผิดพลาด: ' + e);
    if (btn) btn.disabled = false;
  });
}

function deleteFileInModal(folder, filepath) {
  if (!confirm('ลบไฟล์ ' + filepath + ' ?')) return;
  fetch('/api/folder_file/' + encodeURIComponent(folder), {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder: folder, filepath: filepath }),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      loadExistingFilesInModal(folder);
      loadFolderList();
    }
  });
}

function deleteProductInModal() {
  if (_productProfileSavingFor === _editingFolder) { alert('กรุณารอให้บันทึกข้อมูลเสร็จก่อน'); return; }
  const name = _editingFolder;
  if (!name) return;
  if (_isProductProfileDirty() || _isUploadModalDirty()) {
    if (!confirm('คุณมีการเปลี่ยนแปลงยังไม่บันทึก ต้องการลบสินค้าโดยไม่บันทึกหรือไม่?')) return;
  }
  if (!confirm('ลบสินค้า ' + name + ' และไฟล์ทั้งหมดข้างใน ?')) return;
  fetch('/api/folder', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder: name }),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      closeUploadModal(true);
      loadFolderList();
    }
  });
}

function closeUploadModal(force) {
  if (!force && _productProfileSavingFor === _ppFolder) {
    alert('กรุณารอให้บันทึกข้อมูลเสร็จก่อน');
    return;
  }
  if (!force && (_isProductProfileDirty() || _isUploadModalDirty())) {
    if (!confirm('คุณมีการเปลี่ยนแปลงทียังไม่บันทึก ต้องการปิดหน้าต่างหรือไม่?')) {
      return;
    }
  }
  document.getElementById('upload-overlay').className = 'settings-modal-overlay';
  _ppFolder = '';
  _editingFolder = null;
}

function addFilesToQueueModal() {
  const input = document.getElementById('upload-files-modal');
  for (const f of input.files) {
    _uploadQueue.push(f);
  }
  input.value = '';
  renderUploadQueueModal();
  // Prefill product name from the first file name when adding a new product
  if (!_editingFolder && _uploadQueue.length) {
    const nameInput = document.getElementById('upload-product-name-modal');
    if (!nameInput.value) {
      nameInput.value = _uploadQueue[0].name.replace(/\.[^.]+$/, '').replace(/[_-]/g, ' ').replace(/\s+/g, ' ').trim();
    }
  }
}

function renderUploadQueueModal() {
  const el = document.getElementById('upload-queue-modal');
  if (!_uploadQueue.length) {
    el.innerHTML = '<div style="font-size:12px;color:#555;padding:8px">ยังไม่มีไฟล์ในคิว</div>';
    return;
  }
  let html = '';
  for (let i = 0; i < _uploadQueue.length; i++) {
    html += '<div class="upload-queue-item">';
    html += '<span>📄</span><span class="upload-queue-name">' + escapeHtml(_uploadQueue[i].name) + '</span>';
    html += '<span class="upload-queue-del" onclick="removeFromQueue(' + i + ')">×</span>';
    html += '</div>';
  }
  el.innerHTML = html;
}

function removeFromQueue(idx) {
  _uploadQueue.splice(idx, 1);
  renderUploadQueueModal();
}

function uploadFiles() {
  if (_productProfileSavingFor === _ppFolder) { alert('กรุณารอให้บันทึกข้อมูลเสร็จก่อน'); return; }
  const status = document.getElementById('upload-modal-status');
  const isEditing = _editingFolder !== null;
  let name = document.getElementById('upload-product-name-modal').value.trim();
  if (!isEditing) {
    if (_uploadQueue.length === 0) { status.className = 'upload-status err'; status.textContent = 'กรุณาเลือกไฟล์สำหรับสินค้าใหม่'; return; }
    name = _makeProductNameFromFile(_uploadQueue[0]);
  } else if (!name) {
    status.className = 'upload-status err'; status.textContent = 'กรุณาตั้งชื่อสินค้า'; return;
  }
  const renamed = isEditing && _editingFolder !== name;

  // Step 1: rename first (if name changed) — do this alone, no upload mixed in
  const doUpload = () => {
    if (_uploadQueue.length === 0) {
      // ไม่มีไฟล์ใหม่ใน queue → บันทึก product profile แล้วปิด modal
      _editingFolder = name;
      _uploadModalOriginal = _getUploadModalData();
      status.className = 'upload-status ok';
      status.textContent = 'บันทึกเรียบร้อยแล้ว';
      setTimeout(() => closeUploadModal(true), 800);
      return;
    }
    const formData = new FormData();
    formData.append('product_name', name);
    for (const f of _uploadQueue) {
      formData.append('files', f);
    }
    status.className = 'upload-status'; status.textContent = 'กำลังอัปโหลด...';
    fetch('/api/upload', { method: 'POST', body: formData }).then(r => r.json()).then(data => {
      if (data.ok) {
        status.className = 'upload-status ok';
        status.textContent = 'เพิ่ม ' + data.files.length + ' ไฟล์ เข้า ' + data.folder + ' — กำลังประมวลผลข้อมูลอัตโนมัติ...';
        _uploadQueue = [];
        _editingFolder = data.folder;
        _uploadModalOriginal = _getUploadModalData();
        renderUploadQueueModal();
        loadFolderList();
        startSidebarPolling();  // โชว์ progress ใน sidebar ทันที
        if (isEditing) loadExistingFilesInModal(data.folder);
        setTimeout(() => closeUploadModal(true), 1200);
      } else {
        status.className = 'upload-status err';
        status.textContent = data.error || 'เกิดข้อผิดพลาด';
      }
    }).catch(e => {
      status.className = 'upload-status err';
      status.textContent = 'เกิดข้อผิดพลาด: ' + e.message;
    });
  };

  const proceed = () => {
    if (renamed) {
      status.className = 'upload-status'; status.textContent = 'กำลังเปลี่ยนชื่อ...';
      fetch('/api/rename_folder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_name: _editingFolder, new_name: name }),
      }).then(r => r.json()).then(data => {
        if (data.ok) {
          _editingFolder = name;
          doUpload();
        } else {
          status.className = 'upload-status err';
          status.textContent = data.error || 'เปลี่ยนชื่อไม่สำเร็จ';
        }
      }).catch(e => {
        status.className = 'upload-status err';
        status.textContent = 'เปลี่ยนชื่อไม่สำเร็จ: ' + e.message;
      });
    } else {
      doUpload();
    }
  };

  // ถ้า product profile มีการเปลี่ยนแปลง ให้เซฟก่อน แล้วค่อยดำเนินการต่อ
  if (_ppFolder && _isProductProfileDirty()) {
    saveProductProfile().then(proceed).catch(() => {});
  } else {
    proceed();
  }
}

function loadBrandFiles() {
  fetch('/api/brand_json').then(r => r.json()).then(data => {
    const el = document.getElementById('sidebar-content');
    if (!data) { el.innerHTML = '<div style="color:#555;font-size:12px;padding:12px">ยังไม่มีข้อมูลแบรนด์</div>'; return; }
    _brandData = data;
    let html = '';
    html += '<div class="brand-file-item" onclick="openVoiceLearnModal()" style="color:#7c8aff;font-weight:600">🎓 ฝึก AI จากตัวอย่าง (Brand Learning)</div>';
    html += '<div style="border-top:1px solid #2a2d3a;margin:8px 0"></div>';
    html += '<div class="brand-file-item" onclick="editBrandSection(\'voice\')" id="brand-section-voice">🎤 โทนเสียง (Voice)</div>';
    html += '<div class="brand-file-item" onclick="editBrandSection(\'terms\')" id="brand-section-terms">📝 คำที่ใช้/ห้ามใช้ (Terms)</div>';
    html += '<div class="brand-file-item" onclick="editBrandSection(\'profile\')">📋 ประวัติแบรนด์ (Profile)</div>';
    html += '<div class="brand-file-item" onclick="editBrandSection(\'audience\')">👥 กลุ่มเป้าหมาย (Audience)</div>';
    html += '<div class="brand-file-item" onclick="editBrandSection(\'visual\')">🎨 แนวทางภาพ (Visual)</div>';
    html += '<div style="border-top:1px solid #2a2d3a;margin:8px 0"></div>';
    html += '<div class="brand-file-item" onclick="openAssetLibraryModal()" title="โลโก้ รูปพรีเซนเตอร์ เพลง ฯลฯ ที่ใช้ซ้ำข้ามการรัน">🗂 วัตถุดิบแบรนด์</div>';
    el.innerHTML = html;
    // โหลด conflict icons สำหรับแต่ละ section ใน sidebar
    loadConflictIcons();
  });
}

let _brandData = {};
let _brandSection = '';

function editBrandSection(section) {
  _brandSection = section;
  const overlay = document.getElementById('brand-overlay');
  const title = document.getElementById('brand-modal-title');
  const body = document.getElementById('brand-modal-body');
  const labels = { voice: '🎤 โทนเสียง (Voice)', terms: '📝 คำที่ใช้/ห้ามใช้ (Terms)', profile: '📋 ประวัติแบรนด์ (Profile)', audience: '👥 กลุ่มเป้าหมาย (Audience)', visual: '🎨 แนวทางภาพ (Visual)' };
  title.textContent = labels[section] || section;
  body.innerHTML = _renderBrandForm(section, _brandData[section] || {});
  document.getElementById('brand-save-status-modal').textContent = '';
  document.getElementById('brand-save-status-modal').className = 'upload-status';
  overlay.className = 'settings-modal-overlay visible';
  // โหลด conflict info สำหรับ section นี้
  _renderBrandSectionConflicts(section);
}

// Map section → conflict fields ที่เกี่ยวข้อง
_BRAND_SECTION_CONFLICT_FIELDS = {
  voice: ['tone', 'language', 'sell_style', 'hook_style'],
  terms: ['custom_banned_phrase', 'custom_restricted_term'],
};

function _renderBrandSectionConflicts(section) {
  const relevantFields = _BRAND_SECTION_CONFLICT_FIELDS[section];
  if (!relevantFields) return;  // profile, audience, visual ไม่มี conflict
  fetch('/api/conflicts').then(r => r.json()).then(data => {
    // หา div ที่จะใส่ conflict info — สร้างถ้ายังไม่มี
    const body = document.getElementById('brand-modal-body');
    let conflictDiv = document.getElementById('brand-section-conflicts');
    if (!conflictDiv) {
      conflictDiv = document.createElement('div');
      conflictDiv.id = 'brand-section-conflicts';
      conflictDiv.style.marginTop = '16px';
      body.appendChild(conflictDiv);
    }
    // รวม conflict ที่เกี่ยวกับ section นี้จากทุก agent
    let html = '';
    for (const [agentKey, agentData] of Object.entries(data)) {
      if (!agentData.has_conflict) continue;
      const info = AGENT_INFO[agentKey] || { name: agentKey };
      for (const c of agentData.conflicts) {
        if (!relevantFields.includes(c.field)) continue;
        const brandVal = escapeHtml(String(c.brand_value || ''));
        const userVal = escapeHtml(Array.isArray(c.user_value) ? c.user_value.join(', ') : String(c.user_value || ''));
        const fieldLabel = { tone: 'Tone', language: 'ภาษา', sell_style: 'การขาย', hook_style: 'Hook', custom_banned_phrase: 'คำต้องห้ามใน Instructions', custom_restricted_term: 'คำจำกัดใน Instructions' }[c.field] || c.field;
        html += '<div style="font-size:12px;color:#fbbf24;margin:6px 0;padding:8px;background:#2a2018;border:1px solid #fbbf24;border-radius:6px">';
        html += '⚠ <strong>' + escapeHtml(info.name) + '</strong>: ' + fieldLabel + ' — brand "' + brandVal + '" vs agent "' + userVal + '"';
        html += ' <button class="brand-conflict-btn brand-conflict-btn-brand" onclick="openAgentSettings(\'' + agentKey + '\')" style="font-size:11px;padding:2px 8px;border-radius:4px;cursor:pointer;border:1px solid #fbbf24;color:#fbbf24;background:transparent">ไปแก้ที่ agent</button>';
        html += '</div>';
      }
    }
    if (html) {
      conflictDiv.innerHTML = '<div style="font-size:12px;color:#fbbf24;font-weight:600;margin-bottom:8px">⚠ Agent ที่ขัดกับส่วนนี้ของแบรนด์:</div>' + html;
    } else {
      conflictDiv.innerHTML = '';
    }
  }).catch(() => {});
}

function _renderBrandForm(section, data) {
  if (section === 'voice') {
    return _voiceForm(data);
  } else if (section === 'terms') {
    return _termsForm(data);
  } else if (section === 'profile') {
    return '<textarea id="brand-profile-textarea" style="width:100%;min-height:400px;background:#0f1117;border:1px solid #2a2d3a;border-radius:8px;padding:12px;color:#e0e0e0;font-size:13px;font-family:SF Mono,Consolas,monospace;line-height:1.6;resize:vertical">' + escapeHtml(data || '') + '</textarea>';
  } else if (section === 'audience') {
    return _audienceForm(data);
  } else if (section === 'visual') {
    return _visualForm(data);
  }
  return '';
}

function _field(label, id, value, placeholder, tip) {
  const tipIcon = tip ? ' <span style="cursor:help;color:#7c8aff;font-size:12px" title="' + escapeHtml(tip) + '">ⓘ</span>' : '';
  return '<div style="margin-bottom:12px"><label style="font-size:12px;color:#888;display:block;margin-bottom:4px">' + label + tipIcon + '</label>' +
    '<input id="' + id + '" value="' + escapeHtml(String(value || '')) + '" placeholder="' + (placeholder || '') + '" style="width:100%;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:13px"></div>';
}

function _textarea(label, id, value, placeholder, tip) {
  const tipIcon = tip ? ' <span style="cursor:help;color:#7c8aff;font-size:12px" title="' + escapeHtml(tip) + '">ⓘ</span>' : '';
  return '<div style="margin-bottom:12px"><label style="font-size:12px;color:#888;display:block;margin-bottom:4px">' + label + tipIcon + '</label>' +
    '<textarea id="' + id + '" placeholder="' + (placeholder || '') + '" style="width:100%;min-height:80px;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:13px;resize:vertical">' + escapeHtml(String(value || '')) + '</textarea></div>';
}

function _listField(label, id, items, placeholder, tip) {
  const arr = Array.isArray(items) ? items : (items ? String(items).split(',').map(s => s.trim()).filter(Boolean) : []);
  const text = arr.join(', ');
  const tipIcon = tip ? ' <span style="cursor:help;color:#7c8aff;font-size:12px" title="' + escapeHtml(tip) + '">ⓘ</span>' : '';
  const ph = placeholder ? ' placeholder="' + placeholder + '"' : '';
  return '<div style="margin-bottom:12px"><label style="font-size:12px;color:#888;display:block;margin-bottom:4px">' + label + tipIcon + '</label>' +
    '<textarea id="' + id + '"' + ph + ' style="width:100%;min-height:60px;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:13px;resize:vertical">' + escapeHtml(text) + '</textarea></div>';
}

function _chipField(label, id, items, placeholder, tip) {
  const arr = Array.isArray(items) ? items : (items ? String(items).split(',').map(s => s.trim()).filter(Boolean) : []);
  const text = arr.join(', ');
  const tipIcon = tip ? ' <span style="cursor:help;color:#7c8aff;font-size:12px" title="' + escapeHtml(tip) + '">ⓘ</span>' : '';
  const chips = arr.map(x => _chipHtml(id, x)).join('');
  const ph = placeholder ? ' placeholder="' + placeholder + '"' : '';
  return '<div style="margin-bottom:12px"><label style="font-size:12px;color:#888;display:block;margin-bottom:4px">' + label + tipIcon + '</label>' +
    '<div id="' + id + '-chips" style="margin-bottom:6px">' + chips + '</div>' +
    '<input type="text" id="' + id + '-add"' + ph + ' style="width:100%;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:13px" onkeydown="_chipKeydown(\'' + id + '\', event)" onblur="_addChip(\'' + id + '\', this.value); this.value=\'\';">' +
    '<input type="hidden" id="' + id + '" value="' + escapeHtml(text) + '"></div>';
}

function _chipHtml(id, x) {
  const v = String(x).replace(/"/g, '&quot;');
  return '<span class="pp-chip" data-value="' + v + '" style="display:inline-block;background:#2a2d3a;color:#e0e0e0;border:1px solid #3a3d4a;border-radius:4px;padding:4px 8px;margin:0 4px 4px 0;font-size:12px">' + escapeHtml(x) + ' <span style="cursor:pointer;color:#f87171" onclick="_removeChip(\'' + id + '\', this)">×</span></span>';
}

function _chipKeydown(id, e) {
  const input = e.target;
  if (e.key === 'Enter' || e.key === ',') {
    e.preventDefault();
    _addChip(id, input.value);
    input.value = '';
  } else if (e.key === 'Backspace' && input.value === '') {
    _removeLastChip(id);
  }
}

function _addChip(id, raw) {
  const val = (raw || '').trim();
  if (!val) return;
  const hidden = document.getElementById(id);
  const existing = (hidden.value || '').split(',').map(s => s.trim()).filter(Boolean);
  if (existing.includes(val)) return;
  existing.push(val);
  hidden.value = existing.join(', ');
  const chips = document.getElementById(id + '-chips');
  if (chips) {
    chips.insertAdjacentHTML('beforeend', _chipHtml(id, val));
  }
}

function _removeChip(id, el) {
  const chip = el.parentElement;
  const val = chip.getAttribute('data-value');
  chip.remove();
  const hidden = document.getElementById(id);
  const existing = (hidden.value || '').split(',').map(s => s.trim()).filter(Boolean).filter(v => v !== val);
  hidden.value = existing.join(', ');
}

function _removeLastChip(id) {
  const chips = document.getElementById(id + '-chips');
  if (chips && chips.lastElementChild) chips.lastElementChild.remove();
  const hidden = document.getElementById(id);
  if (hidden) {
    const existing = (hidden.value || '').split(',').map(s => s.trim()).filter(Boolean);
    existing.pop();
    hidden.value = existing.join(', ');
  }
}

function _renderChips(id, arr) {
  const items = Array.isArray(arr) ? arr : (arr ? String(arr).split(',').map(s => s.trim()).filter(Boolean) : []);
  const hidden = document.getElementById(id);
  const chips = document.getElementById(id + '-chips');
  if (hidden) hidden.value = items.join(', ');
  if (chips) chips.innerHTML = items.map(x => _chipHtml(id, x)).join('');
}

function _voiceForm(d) {
  let h = '';
  h += _textarea('บุคลิกของแบรนด์', 'bf-personality', d.personality, 'เช่น "เหมือนพ่อแม่ที่เข้าใจเทคโนโลยี"');
  h += _field('ภาษาที่ใช้', 'bf-language', d.language, 'เช่น ไทยเป็นหลัก สำหรับตลาดไทย');
  h += _field('ระดับความเป็นทางการ (1-5)', 'bf-formality', d.formality_level, '3');
  h += _textarea('คำอธิบายโทนเสียง', 'bf-tone', d.tone_description, 'เช่น กลาง-เป็นทางการเล็กน้อย เป็นมิตร อบอุ่น');
  h += _listField('คำ/วลีที่ห้ามใช้', 'bf-banned', d.banned_phrases);
  h += _listField('ตัวอย่างโพสต์ที่ใช่', 'bf-examples', d.examples);
  return h;
}

function _termsForm(d) {
  let h = '';
  h += _listField('คำที่อนุมัติ (Do Say)', 'bf-approved', d.approved);
  h += _listField('คำต้องห้าม (Don\'t Say)', 'bf-restricted', d.restricted);
  return h;
}

function _audienceForm(d) {
  let h = '';
  const p = d.primary || {};
  h += '<div style="font-size:13px;color:#7c8aff;margin-bottom:8px">กลุ่มเป้าหมายหลัก</div>';
  h += _field('ช่วงอายุ', 'bf-age', p.age, '30-45 ปี');
  h += _field('บทบาท', 'bf-role', p.role, 'ผู้ปกครอง');
  h += _field('เพศ', 'bf-gender', p['เพศ'] || p.gender, 'เช่น หญิง 70% / ชาย 30%');
  h += _field('อาชีพ', 'bf-occupation', p.อาชีพ || p.occupation, '');
  h += _field('รายได้', 'bf-income', p.รายได้ || p.income, '');
  h += _field('ที่อยู่', 'bf-location', p['ที่อยู่'] || p.location, 'เช่น กรุงเทพฯ และปริมณฑล');
  const eu = d.end_user || {};
  h += '<div style="font-size:13px;color:#7c8aff;margin:12px 0 8px 0">ผู้ใช้ปลายทาง (End User)</div>';
  h += _field('ช่วงอายุ', 'bf-eu-age', eu.age || eu.อายุ, 'เช่น 5-12 ปี');
  h += _field('ลักษณะ', 'bf-eu-desc', eu.desc, 'เช่น เด็กวัยเรียน');
  h += _listField('ไลฟ์สไตล์', 'bf-lifestyle', d.lifestyle);
  const bb = d.buying_behavior || {};
  h += '<div style="font-size:13px;color:#7c8aff;margin:12px 0 8px 0">พฤติกรรมการซื้อ</div>';
  h += _textarea('ตัดสินใจซื้อจาก', 'bf-bb-decision', bb.decision_factors, 'เช่น ความปลอดภัย > คุณสมบัติ > ราคา');
  h += _field('งบประมาณต่อครั้ง', 'bf-bb-budget', bb.budget_per_purchase, 'เช่น 2,000-5,000 บาท');
  h += _field('ซื้อผ่าน', 'bf-bb-channels', bb.channels_purchase, 'เช่น ออนไลน์ / หน้าร้าน');
  h += _listField('ปัญหา/ความต้องการ (Pain Points)', 'bf-pain', d.pain_points);
  h += _listField('ช่องทางที่ใช้บ่อย (Social/Shop)', 'bf-channels', d.channels);
  h += _listField('ช่องทางค้นหาข้อมูล', 'bf-search-channels', d.search_channels);
  return h;
}

function _visualForm(d) {
  let h = '';
  const c = d.colors || {};
  h += '<div style="font-size:13px;color:#7c8aff;margin-bottom:8px">สีของแบรนด์</div>';
  h += _field('Primary', 'bf-color-primary', c.primary, '#1a73e8');
  h += _field('Secondary', 'bf-color-secondary', c.secondary, '#34a853');
  h += _field('Accent', 'bf-color-accent', c.accent, '#fbbc04');
  h += _field('พื้นหลัง', 'bf-color-bg', c.background, '#ffffff');
  const s = d.image_style || {};
  h += '<div style="font-size:13px;color:#7c8aff;margin:12px 0 8px 0">สไตล์ภาพ</div>';
  h += _textarea('โทนภาพ', 'bf-style-tone', s.tone, 'อบอุ่น สดใส');
  h += _textarea('Product shot', 'bf-style-product', s.product_shot, 'สะอาด พื้นขาว');
  h += _listField('Keywords สำหรับ AI Image Prompt', 'bf-keywords', d.keywords);
  h += _listField('หลีกเลี่ยง (Avoid)', 'bf-avoid', d.avoid);
  // Video style section (Style Reverse-Engineering) — รองรับหลายวิดีโอคู่แข่ง
  const vsList = d.video_styles || [];
  const vsActive = d.video_style || {};
  h += '<div style="font-size:13px;color:#7c8aff;margin:12px 0 8px 0">สไตล์วิดีโอ (จากการวิเคราะห์คู่แข่ง)</div>';
  h += '<button onclick="analyzeVideoStyle()" style="background:#7c8aff;border:none;color:#fff;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:12px;margin-bottom:8px">🎬 วิเคราะห์วิดีโอคู่แข่ง</button>';
  h += '<div id="video-style-result" style="display:none;background:#0f1117;border:1px solid #2a2d3a;border-radius:8px;padding:12px;margin-bottom:8px;font-size:12px;color:#e0e0e0"></div>';
  // แสดงรายการวิดีโอที่วิเคราะห์แล้ว
  if (vsList.length > 0) {
    h += '<div id="video-style-list" style="margin-bottom:12px">';
    vsList.forEach((vs, i) => {
      h += '<div style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center">';
      h += '<span style="font-size:11px;color:#888">คู่แข่ง ' + (i+1) + ': ' + escapeHtml(vs.style_summary || vs.pacing || '') + '</span>';
      h += '<span><button onclick="useVideoStyle(' + i + ')" style="background:none;border:1px solid #4caf50;color:#4caf50;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px;margin-right:4px">ใช้</button>';
      h += '<button onclick="removeVideoStyle(' + i + ')" style="background:none;border:1px solid #f44336;color:#f44336;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px">×</button></span>';
      h += '</div>';
    });
    h += '</div>';
  }
  h += _field('สรุปสไตล์ (ที่ใช้)', 'bf-vs-summary', vsActive.style_summary, 'เช่น Fast-paced TikTok style with warm tones');
  h += _field('จังหวะ (Pacing)', 'bf-vs-pacing', vsActive.pacing, 'เช่น เร็ว, ปานกลาง, ช้า');
  h += _listField('Transitions', 'bf-vs-transitions', vsActive.transitions);
  h += _field('โทนสี (Color Grading)', 'bf-vs-color', vsActive.color_grading, 'เช่น warm tones, high contrast');
  h += _field('ดีไซน์เสียง (Sound Design)', 'bf-vs-sound', vsActive.sound_design, 'เช่น upbeat music, voiceover');
  h += _field('ระยะเวลาต่อ Shot', 'bf-vs-shot', vsActive.shot_duration, 'เช่น 2-3 sec');
  h += _field('จังหวะภาพ (Visual Rhythm)', 'bf-vs-rhythm', vsActive.visual_rhythm, 'เช่น energetic, calm');
  return h;
}

function _formVal(id) {
  return (document.getElementById(id) || {}).value || '';
}
function _formList(id) {
  const v = _formVal(id);
  return v ? v.split(',').map(x => x.trim()).filter(x => x) : [];
}

function _collectBrandForm(section) {
  if (section === 'voice') {
    const f = _formVal('bf-formality');
    return {
      personality: _formVal('bf-personality'),
      language: _formVal('bf-language'),
      formality_level: f ? parseInt(f) : null,
      tone_description: _formVal('bf-tone'),
      banned_phrases: _formList('bf-banned'),
      examples: _formList('bf-examples'),
    };
  } else if (section === 'terms') {
    return { approved: _formList('bf-approved'), restricted: _formList('bf-restricted') };
  } else if (section === 'profile') {
    return val('brand-profile-textarea');
  } else if (section === 'audience') {
    return {
      primary: {
        age: _formVal('bf-age'),
        role: _formVal('bf-role'),
        'เพศ': _formVal('bf-gender'),
        อาชีพ: _formVal('bf-occupation'),
        รายได้: _formVal('bf-income'),
        'ที่อยู่': _formVal('bf-location'),
      },
      end_user: { age: _formVal('bf-eu-age'), desc: _formVal('bf-eu-desc') },
      lifestyle: _formList('bf-lifestyle'),
      buying_behavior: {
        decision_factors: _formVal('bf-bb-decision'),
        budget_per_purchase: _formVal('bf-bb-budget'),
        channels_purchase: _formVal('bf-bb-channels'),
      },
      pain_points: _formList('bf-pain'),
      channels: _formList('bf-channels'),
      search_channels: _formList('bf-search-channels'),
    };
  } else if (section === 'visual') {
    return {
      colors: { primary: _formVal('bf-color-primary'), secondary: _formVal('bf-color-secondary'), accent: _formVal('bf-color-accent'), background: _formVal('bf-color-bg') },
      image_style: { tone: _formVal('bf-style-tone'), product_shot: _formVal('bf-style-product') },
      keywords: _formList('bf-keywords'),
      avoid: _formList('bf-avoid'),
      video_styles: window._videoStyles || [],
      video_style: {
        style_summary: _formVal('bf-vs-summary'),
        pacing: _formVal('bf-vs-pacing'),
        transitions: _formList('bf-vs-transitions'),
        color_grading: _formVal('bf-vs-color'),
        sound_design: _formVal('bf-vs-sound'),
        shot_duration: _formVal('bf-vs-shot'),
        visual_rhythm: _formVal('bf-vs-rhythm'),
      },
    };
  }
  return null;
}

function closeBrandModal() {
  document.getElementById('brand-overlay').className = 'settings-modal-overlay';
}

function saveBrandFileModal() {
  const status = document.getElementById('brand-save-status-modal');
  const collected = _collectBrandForm(_brandSection);
  if (collected === null) { status.className = 'upload-status err'; status.textContent = 'เกิดข้อผิดพลาด'; return; }
  // สร้าง payload ทั้งหมด แต่ส่งเฉพาะ section ที่แก้
  const payload = {};
  payload[_brandSection] = collected;
  fetch('/api/brand_json_save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      // update cache
      _brandData[_brandSection] = collected;
      status.className = 'upload-status ok';
      status.textContent = 'บันทึกแล้ว ✓';
      setTimeout(closeBrandModal, 800);
      // Refresh conflict icons — brand rules changed, agents may now conflict
      loadConflictIcons();
    } else {
      status.className = 'upload-status err';
      status.textContent = data.error || 'เกิดข้อผิดพลาด';
    }
  });
}

// ============================================================
// Product Profile Modal — ตั้งค่าสินค้าเฉพาะรุ่น
// ============================================================

let _ppFolder = '';
let _productProfileSavingFor = null;
let _productProfileOriginal = null;
let _uploadModalOriginal = null;

function _getProductProfileFormData() {
  const get = id => { const el = document.getElementById(id); return el ? String(el.value || '').trim() : ''; };
  const getList = id => { const el = document.getElementById(id); return (el ? String(el.value || '') : '').split(',').map(s => s.trim()).filter(Boolean); };
  return {
    audience: {
      primary: { age: get('pp-age'), role: get('pp-role') },
      end_user: { age: get('pp-eu-age'), desc: get('pp-eu-desc') },
    },
    competitors: getList('pp-competitors'),
    differentiators: getList('pp-differentiators'),
    use_cases: getList('pp-use-cases'),
    price_tier: get('pp-price-tier'),
    tone_adjustment: get('pp-tone'),
    visual_override: {
      image_style: { tone: get('pp-visual-tone') },
      keywords: getList('pp-visual-keywords'),
    },
  };
}

function _isProductProfileDirty() {
  if (!_productProfileOriginal) return false;
  return JSON.stringify(_getProductProfileFormData()) !== JSON.stringify(_productProfileOriginal);
}

function _getUploadModalData() {
  const nameInput = document.getElementById('upload-product-name-modal');
  const name = nameInput ? String(nameInput.value || '').trim() : '';
  const queue = _uploadQueue.map(f => ({ name: f.name, size: f.size }));
  return { productName: name, queue: queue };
}

function _isUploadModalDirty() {
  if (!_uploadModalOriginal) return false;
  return JSON.stringify(_getUploadModalData()) !== JSON.stringify(_uploadModalOriginal);
}

function loadProductProfileForManage(folder) {
  _ppFolder = folder;
  const section = document.getElementById('pp-section');
  const body = document.getElementById('pp-modal-body');
  const status = document.getElementById('pp-status');
  if (section) section.style.display = 'block';
  if (status) { status.className = 'upload-status'; status.textContent = ''; status.style.display = 'none'; }
  // โหลด profile เดิมก่อน (ถ้ามี) — ingest สร้างให้แล้ว ไม่ต้อง suggest ตอนเปิด
  fetch('/api/product_profile/' + encodeURIComponent(folder)).then(r => r.json()).then(data => {
    _renderProductProfileInto(body, data || {});
  }).catch(() => {
    _renderProductProfileInto(body, {});
  });
}

function _renderProductProfileInto(body, data) {
  body.innerHTML = _renderProductProfileForm(data);
  _productProfileOriginal = _getProductProfileFormData();
}

function _renderProductProfileForm(d) {
  let h = '';
  h += '<button id="pp-suggest-btn" onclick="suggestProductProfile()" title="วิเคราะห์สเปคสินค้าแล้วเติมค่าให้อัตโนมัติ (กดซ้ำได้ถ้าอยากให้เดาใหม่)" style="background:#7c8aff;border:none;color:#fff;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:12px;margin-bottom:12px">🎓 ตั้งค่าด้วย AI</button>';
  h += '<span id="pp-suggest-status" style="display:none;margin-left:8px;font-size:12px;vertical-align:middle"></span>';
  // Audience
  const aud = d.audience || {};
  const prim = aud.primary || {};
  const eu = aud.end_user || {};
  h += '<div style="font-size:13px;color:#7c8aff;margin-bottom:8px">กลุ่มเป้าหมาย</div>';
  h += _field('ช่วงอายุผู้ซื้อ', 'pp-age', prim.age, 'เช่น 28-45 ปี', 'ช่วงอายุของผู้ซื้อจริง มีผลต่อท่อนและข้อความของคอนเทนต์');
  h += _field('บทบาทผู้ซื้อ', 'pp-role', prim.role, 'เช่น ผู้ปกครองยุคใหม่ที่ใส่ใจเทคโนโลยี', 'บทบาทหรือตัวตนของผู้ซื้อ เช่น พ่อแม่ผู้ปกครอง นักธุรกิจ');
  h += _field('ช่วงอายุผู้ใช้ปลายทาง', 'pp-eu-age', eu.age, 'เช่น 5-12 ปี', 'ถ้าผู้ใช้งานจริงต่างจากผู้ซื้อ เช่น นาฬิกาเด็ก = ลูก แต่คนซื้อ = ผู้ปกครอง');
  h += _field('ลักษณะผู้ใช้ปลายทาง', 'pp-eu-desc', eu.desc, 'เช่น เด็กวัยประถมที่ชอบเล่นกีฬา', 'ลักษณะนิสัย/พฤติกรรมของคนใช้งานจริง ช่วยให้ภาพ/วิดีโอตรงกลุ่ม');
  // Positioning
  h += '<div style="font-size:13px;color:#7c8aff;margin:12px 0 8px 0">ตำแหน่งสินค้า</div>';
  h += _chipField('คู่แข่งหลัก', 'pp-competitors', d.competitors, 'พิมพ์แล้วกด Enter', 'รายชื่อคู่แข่งในตลาด ใช้เทียบจุดขายของเรา');
  h += _chipField('จุดขายหลัก', 'pp-differentiators', d.differentiators, 'พิมพ์แล้วกด Enter', 'สิ่งที่ทำให้สินค้านี้ต่างจากคู่แข่ง เอาไปใช้เขียนคอนเทนต์');
  h += _chipField('Use cases', 'pp-use-cases', d.use_cases, 'พิมพ์แล้วกด Enter', 'สถานการณ์ใช้งานจริง ช่วยให้คอนเทนต์สื่อตรง');
  h += '<div style="margin-bottom:12px"><label style="font-size:12px;color:#888;display:block;margin-bottom:4px">ระดับราคา <span style="cursor:help;color:#7c8aff;font-size:12px" title="กำหนดระดับราคาเพื่อปรับโทนคอนเทนต์ให้เหมาะสม เช่น entry=คุ้มค่า mid=สมดุล flagship=พรีเมียม">ⓘ</span></label>' +
    '<select id="pp-price-tier" style="width:100%;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:13px">' +
    '<option value=""' + (d.price_tier === '' || !d.price_tier ? ' selected' : '') + '>— เลือก —</option>' +
    '<option value="entry"' + (d.price_tier === 'entry' ? ' selected' : '') + '>entry (ราคาเริ่มต้น)</option>' +
    '<option value="mid"' + (d.price_tier === 'mid' ? ' selected' : '') + '>mid (กลาง)</option>' +
    '<option value="flagship"' + (d.price_tier === 'flagship' ? ' selected' : '') + '>flagship (ระดับสูง)</option>' +
    '</select></div>';
  h += _textarea('ปรับโทน', 'pp-tone', d.tone_adjustment, 'เช่น อบอุ่น วางใจได้ ให้ความรู้สึกปลอดภัย', 'ทิศทางโทนเฉพาะสินค้านี้ ไม่เปลี่ยน voice แบรนด์ทั้งหมด แค่ปรับน้ำหนัก เช่น มั่นใจ พรีเมียม สนุก คึกคัก');
  // Visual override
  const vo = d.visual_override || {};
  const vis = vo.image_style || {};
  h += '<div style="font-size:13px;color:#7c8aff;margin:12px 0 8px 0">ปรับภาพ <span style="cursor:help;color:#7c8aff;font-size:12px" title="ค่าพวกนี้จะทับแนวทางภาพของแบรนด์ถ้ากรอก">ⓘ</span></div>';
  h += _textarea('โทนภาพ', 'pp-visual-tone', vis.tone, 'เช่น ดำ-ทอง พรีเมียม แสงนุ่ม', 'คำอธิบายภาพรวมสำหรับสร้างรูป/วิดีโอของสินค้านี้');
  h += _chipField('Keywords ภาพ', 'pp-visual-keywords', vo.keywords, 'พิมพ์แล้วกด Enter', 'คำสำคัญสำหรับ AI สร้างภาพ');
  return h;
}

function suggestProductProfile(autoSave) {
  if (!_ppFolder) return;
  const btn = document.getElementById('pp-suggest-btn');
  const status = document.getElementById('pp-suggest-status');
  if (btn) btn.disabled = true;
  if (status && !autoSave) {
    status.style.display = 'inline-block';
    status.style.color = '#facc15';
    status.textContent = 'AI กำลังอ่านสเปค...';
  }
  fetch('/api/product_profile_suggest/' + encodeURIComponent(_ppFolder), {
    method: 'POST',
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      const suggested = data.suggested;
      // pre-fill ฟอร์มเฉพาะฟิลด์ที่ AI ให้ค่า ไม่ทับค่าที่ผู้ใช้กรอกไว้แล้ว
      const setVal = (id, v) => { const el = document.getElementById(id); if (el && v) el.value = v; };
      const setList = (id, arr) => { if (!arr || !arr.length) return; if (document.getElementById(id + '-chips')) { _renderChips(id, arr); } else { const el = document.getElementById(id); if (el) el.value = arr.join(', '); } };
      const prim = (suggested.audience && suggested.audience.primary) || {};
      const eu = (suggested.audience && suggested.audience.end_user) || {};
      setVal('pp-age', prim.age);
      setVal('pp-role', prim.role);
      setVal('pp-eu-age', eu.age);
      setVal('pp-eu-desc', eu.desc);
      setList('pp-competitors', suggested.competitors);
      setList('pp-differentiators', suggested.differentiators);
      setList('pp-use-cases', suggested.use_cases);
      setVal('pp-price-tier', suggested.price_tier);
      setVal('pp-tone', suggested.tone_adjustment);
      if (status) {
        status.style.display = 'inline-block';
        status.style.color = '#4ade80';
        status.textContent = '✅ AI วิเคราะห์สเปคแล้ว — กรุณากด "บันทึก" เพื่อบันทึก';
      }
      if (btn) btn.disabled = false;
      // ไม่ auto-save — ผู้ใช้ต้องกดปุ่ม "บันทึก" เอง
    } else {
      if (status) {
        status.style.display = 'inline-block';
        status.style.color = '#f87171';
        status.textContent = data.error || 'AI วิเคราะห์ไม่สำเร็จ';
      }
      if (btn) btn.disabled = false;
    }
  }).catch(e => {
    if (status) {
      status.style.display = 'inline-block';
      status.style.color = '#f87171';
      status.textContent = 'เกิดข้อผิดพลาด: ' + e.message;
    }
    if (btn) btn.disabled = false;
  });
}

function saveProductProfile() {
  if (!_ppFolder) return Promise.resolve();
  const folder = _ppFolder;
  _productProfileSavingFor = folder;
  const status = document.getElementById('pp-status');
  const primAge = _formVal('pp-age'), primRole = _formVal('pp-role');
  const euAge = _formVal('pp-eu-age'), euDesc = _formVal('pp-eu-desc');
  const payload = {
    competitors: _formList('pp-competitors'),
    differentiators: _formList('pp-differentiators'),
    use_cases: _formList('pp-use-cases'),
    price_tier: _formVal('pp-price-tier'),
    tone_adjustment: _formVal('pp-tone'),
  };
  // audience — ส่งเฉพาะถ้ามีค่า
  const audience = {};
  if (primAge || primRole) audience.primary = { age: primAge, role: primRole };
  if (euAge || euDesc) audience.end_user = { age: euAge, desc: euDesc };
  if (Object.keys(audience).length) payload.audience = audience;
  // visual_override — ส่งเฉพาะถ้ามีค่า
  const visTone = _formVal('pp-visual-tone'), visKw = _formList('pp-visual-keywords');
  if (visTone || visKw.length) {
    payload.visual_override = {};
    if (visTone) payload.visual_override.image_style = { tone: visTone };
    if (visKw.length) payload.visual_override.keywords = visKw;
  }
  status.style.display = 'none';
  status.textContent = '';
  return fetch('/api/product_profile_save/' + encodeURIComponent(folder), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(r => r.json()).then(data => {
    const same = _ppFolder === folder;
    if (data.ok) {
      if (same) _productProfileOriginal = _getProductProfileFormData();
      if (same) { status.style.display = 'none'; status.textContent = ''; }
      loadFolderList();  // refresh sidebar indicator
    } else {
      if (same) {
        status.style.display = 'block';
        status.className = 'upload-status err';
        status.textContent = data.error || 'เกิดข้อผิดพลาด';
      }
      throw new Error(data.error || 'เกิดข้อผิดพลาด');
    }
  }).catch(e => {
    if (_ppFolder === folder) {
      status.style.display = 'block';
      status.className = 'upload-status err';
      status.textContent = 'เกิดข้อผิดพลาด: ' + e.message;
    }
    throw e;
  }).finally(() => {
    if (_productProfileSavingFor === folder) _productProfileSavingFor = null;
  });
}

// ============================================================
// Asset Library — วัตถุดิบแบรนด์ (โลโก้ รูปคน เพลง ฯลฯ)
// ============================================================

let _assetConfig = { taxonomy: { subject: [], style: [] } };
let _assetsPolling = null;
let _assetRenderToken = 0;   // กัน race condition — fetch เก่าที่เสร็จทีหลังจะข้าม

function openAssetLibraryModal() {
  // โหลด config (taxonomy) ก่อน แล้วโหลด asset list
  fetch('/api/assets_config').then(r => r.json()).then(cfg => {
    _assetConfig = cfg;
    const overlay = document.getElementById('asset-overlay');
    overlay.className = 'settings-modal-overlay visible';
    loadAssetsList();
  });
}

function closeAssetModal() {
  document.getElementById('asset-overlay').className = 'settings-modal-overlay';
  if (_assetsPolling) { clearInterval(_assetsPolling); _assetsPolling = null; }
}

function loadAssetsList() {
  const myToken = ++_assetRenderToken;
  fetch('/api/assets').then(r => r.json()).then(data => {
    if (myToken !== _assetRenderToken) return;   // stale — มี render ใหม่กว่าแล้ว
    const assets = data.assets || [];
    const body = document.getElementById('asset-modal-body');
    let html = '';

    // ปุ่มอัปโหลด
    html += '<div style="margin-bottom:16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">';
    html += '<label style="background:#7c8aff;color:#fff;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">+ อัปโหลดไฟล์<input type="file" multiple style="display:none" onchange="uploadAssets(this.files)"></label>';
    html += '<button onclick="reingestAssets()" style="background:#2a2d3a;color:#e0e0e0;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:13px;border:1px solid #3a3d4a">สแกนใหม่</button>';
    html += '<span id="asset-upload-status" class="upload-status" style="font-size:12px"></span>';
    html += '</div>';

    if (assets.length === 0) {
      html += '<div style="color:#555;font-size:13px;padding:24px;text-align:center">ยังไม่มี asset — อัปโหลดไฟล์ (โลโก้ รูปคน เพลง) เพื่อให้ agent ใช้ข้ามการรัน</div>';
    } else {
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px">';
      for (const a of assets) {
        html += _renderAssetCard(a);
      }
      html += '</div>';
    }

    body.innerHTML = html;
  });
}

function _renderAssetCard(a) {
  const isImage = a.type === 'image';
  const thumb = isImage
    ? '<img src="/api/assets/file/' + a.id + '" style="width:100%;height:120px;object-fit:cover;border-radius:6px;background:#0f1117">'
    : '<div style="width:100%;height:120px;display:flex;align-items:center;justify-content:center;font-size:32px;background:#0f1117;border-radius:6px">' + _assetIcon(a.type) + '</div>';

  const statusBadge = a.status === 'ready'
    ? '<span style="color:#4caf50;font-size:10px">●พร้อม</span>'
    : '<span style="color:#f44336;font-size:10px">●' + escapeHtml(_statusTh(a.status || 'error')) + '</span>';

  return '<div style="background:#1c1e2a;border-radius:10px;padding:10px;cursor:pointer" onclick="editAsset(\'' + a.id + '\')">' +
    thumb +
    '<div style="margin-top:8px;font-size:12px;font-weight:600;color:#e0e0e0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(a.file) + '</div>' +
    '<div style="margin-top:4px;font-size:11px;color:#888">' + escapeHtml(_subjectTh(a.subject)) + ' · ' + escapeHtml(_typeTh(a.type)) + ' ' + statusBadge + '</div>' +
    (a.description ? '<div style="margin-top:4px;font-size:11px;color:#666;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(a.description) + '</div>' : '') +
    '</div>';
}

function _assetIcon(type) {
  const icons = { audio: '🎵', video: '🎬', text: '📄', other: '📦' };
  return icons[type] || '📄';
}

// แปล enum อังกฤษ (เก็บใน backend) เป็น label ไทยสำหรับโชว์ใน UI
const _ASSET_SUBJECT_TH = { person: 'คน', product: 'สินค้า', logo: 'โลโก้', background: 'พื้นหลัง', graphic: 'กราฟิก', scene: 'ฉาก', other: 'อื่นๆ' };
const _ASSET_STYLE_TH   = { photo: 'ถ่ายภาพ', illustration: 'ภาพประกอบ', graphic: 'กราฟิก', '3d': '3 มิติ', other: 'อื่นๆ' };
const _ASSET_TYPE_TH    = { image: 'รูปภาพ', audio: 'เสียง', video: 'วิดีโอ', text: 'ข้อความ', other: 'อื่นๆ' };
const _ASSET_STATUS_TH  = { ready: 'พร้อม', processing: 'กำลังประมวลผล', pending: 'รอประมวลผล', error: 'ผิดพลาด' };
function _subjectTh(s) { return _ASSET_SUBJECT_TH[s] || s || ''; }
function _styleTh(s)   { return _ASSET_STYLE_TH[s] || s || ''; }
function _typeTh(t)    { return _ASSET_TYPE_TH[t] || t || ''; }
function _statusTh(s)  { return _ASSET_STATUS_TH[s] || s || ''; }

function uploadAssets(files) {
  if (!files || !files.length) return;
  const status = document.getElementById('asset-upload-status');
  status.textContent = 'กำลังอัปโหลด...';
  status.className = 'upload-status';

  const formData = new FormData();
  for (const f of files) formData.append('files', f);
  formData.append('user_note', '');

  fetch('/api/assets/upload', { method: 'POST', body: formData })
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        status.className = 'upload-status ok';
        status.textContent = 'อัปโหลดแล้ว — กำลังติดแท็กอัตโนมัติ...';
        // poll ทุก 3 วินาทีจนกว่าจะเห็น asset ใหม่
        if (_assetsPolling) clearInterval(_assetsPolling);
        let attempts = 0;
        _assetsPolling = setInterval(() => {
          loadAssetsList();
          attempts++;
          if (attempts > 20) { clearInterval(_assetsPolling); _assetsPolling = null; }
        }, 3000);
      } else {
        status.className = 'upload-status err';
        status.textContent = data.error || 'เกิดข้อผิดพลาด';
      }
    });
}

function reingestAssets() {
  fetch('/api/assets/reingest', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ force: false }) })
    .then(r => r.json())
    .then(data => {
      const status = document.getElementById('asset-upload-status');
      if (data.ok) {
        status.className = 'upload-status ok';
        status.textContent = 'กำลังสแกนใหม่...';
        if (_assetsPolling) clearInterval(_assetsPolling);
        let attempts = 0;
        _assetsPolling = setInterval(() => {
          loadAssetsList();
          attempts++;
          if (attempts > 20) { clearInterval(_assetsPolling); _assetsPolling = null; }
        }, 3000);
      }
    });
}

function editAsset(id) {
  // หยุด polling ถ้ามี — กันทับหน้า edit ที่กำลังดู
  if (_assetsPolling) { clearInterval(_assetsPolling); _assetsPolling = null; }
  const myToken = ++_assetRenderToken;
  fetch('/api/assets/' + id).then(r => r.json()).then(a => {
    if (myToken !== _assetRenderToken) return;   // stale — มี render ใหม่กว่าแล้ว
    const body = document.getElementById('asset-modal-body');
    const subjects = (_assetConfig.taxonomy && _assetConfig.taxonomy.subject) || ['other'];
    const styles = (_assetConfig.taxonomy && _assetConfig.taxonomy.style) || ['other'];

    let h = '<div style="margin-bottom:12px"><button onclick="loadAssetsList()" style="background:#2a2d3a;color:#e0e0e0;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;border:1px solid #3a3d4a">← กลับ</button></div>';

    // preview
    if (a.type === 'image') {
      h += '<img src="/api/assets/file/' + a.id + '" style="width:100%;max-height:300px;object-fit:contain;border-radius:8px;background:#0f1117;margin-bottom:12px">';
    }

    h += '<div style="font-size:13px;color:#888;margin-bottom:4px">ไฟล์: ' + escapeHtml(a.file) + ' · ' + escapeHtml(_typeTh(a.type)) + '</div>';
    h += '<div style="font-size:11px;color:#555;margin-bottom:12px">ID: ' + escapeHtml(a.id) + ' · hash: ' + escapeHtml((a.hash || '').substring(0, 12)) + '...</div>';

    // subject dropdown
    h += '<div style="margin-bottom:12px"><label style="font-size:12px;color:#888;display:block;margin-bottom:4px" title="แยกไฟล์ตามเนื้อหา เช่น คน สินค้า โลโก้">ประเภทเนื้อหา</label><select id="asset-subject" style="width:100%;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:13px">';
    for (const s of subjects) h += '<option value="' + s + '"' + (a.subject === s ? ' selected' : '') + '>' + _subjectTh(s) + '</option>';
    h += '</select></div>';

    // style dropdown
    h += '<div style="margin-bottom:12px"><label style="font-size:12px;color:#888;display:block;margin-bottom:4px" title="รูปแบบของภาพ เช่น ถ่ายภาพ ภาพประกอบ 3 มิติ">สไตล์ภาพ</label><select id="asset-style" style="width:100%;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:13px">';
    for (const s of styles) h += '<option value="' + s + '"' + (a.style === s ? ' selected' : '') + '>' + _styleTh(s) + '</option>';
    h += '</select></div>';

    // tags
    h += '<div style="margin-bottom:12px"><label style="font-size:12px;color:#888;display:block;margin-bottom:4px" title="คำสำคัญที่ช่วยให้ AI ค้นหาไฟล์นี้เจอ">แท็ก <span style="color:#555">(คั่นด้วยจุลภาค)</span></label><textarea id="asset-tags" style="width:100%;min-height:50px;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:13px;resize:vertical">' + escapeHtml((a.tags || []).join(', ')) + '</textarea></div>';

    // description
    h += '<div style="margin-bottom:12px"><label style="font-size:12px;color:#888;display:block;margin-bottom:4px" title="AI บรรยายไฟล์นี้ให้ตอนอัปโหลด แก้ไข้ได้">คำบรรยาย <span style="color:#555">(AI สร้าง — แก้ได้)</span></label><textarea id="asset-description" style="width:100%;min-height:80px;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:13px;resize:vertical">' + escapeHtml(a.description || '') + '</textarea></div>';

    // user_note
    h += '<div style="margin-bottom:12px"><label style="font-size:12px;color:#888;display:block;margin-bottom:4px">หมายเหตุของคุณ</label><textarea id="asset-user-note" style="width:100%;min-height:50px;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:13px;resize:vertical" placeholder="เช่น โลโก้หลักใช้ทุกแพลตฟอร์ม">' + escapeHtml(a.user_note || '') + '</textarea></div>';

    // ปุ่ม
    h += '<div style="display:flex;gap:8px;align-items:center">';
    h += '<button onclick="saveAsset(\'' + a.id + '\')" style="background:#7c8aff;color:#fff;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">บันทึก</button>';
    h += '<button onclick="deleteAsset(\'' + a.id + '\',\'' + escapeHtml(a.file).replace(/'/g, "\\'") + '\')" style="background:#f44336;color:#fff;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:13px">ลบ</button>';
    h += '<span id="asset-save-status" class="upload-status" style="font-size:12px"></span>';
    h += '</div>';

    body.innerHTML = h;
  });
}

function saveAsset(id) {
  const status = document.getElementById('asset-save-status');
  const tags = document.getElementById('asset-tags').value.split(',').map(t => t.trim()).filter(Boolean);
  const payload = {
    subject: document.getElementById('asset-subject').value,
    style: document.getElementById('asset-style').value,
    tags: tags,
    description: document.getElementById('asset-description').value,
    user_note: document.getElementById('asset-user-note').value,
  };
  fetch('/api/assets/' + id + '/update', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }).then(r => r.json()).then(data => {
    if (data.id) {
      status.className = 'upload-status ok';
      status.textContent = 'บันทึกแล้ว ✓';
      setTimeout(() => loadAssetsList(), 800);
    } else {
      status.className = 'upload-status err';
      status.textContent = data.error || 'เกิดข้อผิดพลาด';
    }
  });
}

function deleteAsset(id, filename) {
  if (!confirm('ลบ ' + filename + ' ?')) return;
  fetch('/api/assets/' + id + '/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ remove_file: true }),
  }).then(r => r.json()).then(data => {
    if (data.ok) loadAssetsList();
  });
}

// ============================================================
// Video Style Analysis — วิเคราะห์วิดีโอคู่แข่ง → style profile
// ============================================================

function analyzeVideoStyle() {
  // init list จากข้อมูลเดิม
  if (!window._videoStyles) {
    const existing = (_brandData.visual && _brandData.visual.video_styles) || [];
    window._videoStyles = [...existing];
  }
  const url = prompt('ใส่ YouTube URL ของวิดีโอคู่แข่ง หรือเว้นว่างเพื่อ upload ไฟล์:');
  if (url === null) return;
  if (url.trim()) {
    _doAnalyzeVideoStyle({video_url: url.trim()});
  } else {
    // upload file
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'video/mp4,video/mpeg,video/mov,video/webm';
    input.onchange = async () => {
      if (!input.files.length) return;
      const fd = new FormData();
      fd.append('files', input.files[0]);
      const result = document.getElementById('video-style-result');
      result.style.display = 'block';
      result.textContent = 'กำลังอัปโหลดวิดีโอ...';
      try {
        const up = await fetch('/api/video_style_upload', {method: 'POST', body: fd}).then(r => r.json());
        if (!up.ok) { result.textContent = 'อัปโหลดไม่สำเร็จ: ' + (up.error || ''); return; }
        result.textContent = 'กำลังวิเคราะห์สไตล์วิดีโอ...';
        _doAnalyzeVideoStyle({video_path: up.paths[0]});
      } catch(e) { result.textContent = 'เกิดข้อผิดพลาด: ' + e; }
    };
    input.click();
  }
}

function _doAnalyzeVideoStyle(body) {
  const result = document.getElementById('video-style-result');
  result.style.display = 'block';
  result.textContent = 'กำลังวิเคราะห์สไตล์วิดีโอ... (อาจใช้เวลา 10-30 วินาที)';
  fetch('/api/video_style_analyze', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      const vs = data.video_style;
      // เก็บเข้า list
      window._videoStyles = window._videoStyles || [];
      window._videoStyles.push(vs);
      _renderVideoStyleList();
      // แสดงผล + ปุ่มใช้
      let html = '<div style="color:#7c8aff;font-weight:600;margin-bottom:6px">ผลการวิเคราะห์ (คู่แข่ง ' + window._videoStyles.length + '):</div>';
      html += '<div style="margin-bottom:4px">สรุป: ' + escapeHtml(vs.style_summary || '') + '</div>';
      html += '<div style="margin-bottom:4px">จังหวะ: ' + escapeHtml(vs.pacing || '') + '</div>';
      html += '<div style="margin-bottom:4px">Transitions: ' + escapeHtml((vs.transitions || []).join(', ')) + '</div>';
      html += '<div style="margin-bottom:4px">โทนสี: ' + escapeHtml(vs.color_grading || '') + '</div>';
      html += '<div style="margin-bottom:4px">เสียง: ' + escapeHtml(vs.sound_design || '') + '</div>';
      html += '<div style="margin-bottom:4px">Shot: ' + escapeHtml(vs.shot_duration || '') + '</div>';
      html += '<div style="margin-bottom:8px">จังหวะภาพ: ' + escapeHtml(vs.visual_rhythm || '') + '</div>';
      html += '<button onclick="applyVideoStyle()" style="background:#4caf50;border:none;color:#fff;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:11px">ใช้ค่านี้</button>';
      html += ' <button onclick="document.getElementById(\'video-style-result\').style.display=\'none\'" style="background:none;border:1px solid #555;color:#888;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:11px">ปิด</button>';
      html += ' <span style="color:#555;font-size:11px;margin-left:8px">วิเคราะห์เพิ่มได้อีก</span>';
      result.innerHTML = html;
      window._pendingVideoStyle = vs;
    } else {
      result.textContent = 'วิเคราะห์ไม่สำเร็จ: ' + (data.error || '');
    }
  }).catch(e => { result.textContent = 'เกิดข้อผิดพลาด: ' + e; });
}

function _renderVideoStyleList() {
  const listEl = document.getElementById('video-style-list');
  if (!listEl) return;
  const styles = window._videoStyles || [];
  let html = '';
  styles.forEach((vs, i) => {
    html += '<div style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center">';
    html += '<span style="font-size:11px;color:#888">คู่แข่ง ' + (i+1) + ': ' + escapeHtml(vs.style_summary || vs.pacing || '') + '</span>';
    html += '<span><button onclick="useVideoStyle(' + i + ')" style="background:none;border:1px solid #4caf50;color:#4caf50;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px;margin-right:4px">ใช้</button>';
    html += '<button onclick="removeVideoStyle(' + i + ')" style="background:none;border:1px solid #f44336;color:#f44336;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px">×</button></span>';
    html += '</div>';
  });
  listEl.innerHTML = html;
}

function useVideoStyle(idx) {
  const vs = (window._videoStyles || [])[idx];
  if (!vs) return;
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ''; };
  set('bf-vs-summary', vs.style_summary);
  set('bf-vs-pacing', vs.pacing);
  set('bf-vs-transitions', (vs.transitions || []).join(', '));
  set('bf-vs-color', vs.color_grading);
  set('bf-vs-sound', vs.sound_design);
  set('bf-vs-shot', vs.shot_duration);
  set('bf-vs-rhythm', vs.visual_rhythm);
}

function removeVideoStyle(idx) {
  if (!window._videoStyles) return;
  window._videoStyles.splice(idx, 1);
  _renderVideoStyleList();
}

function applyVideoStyle() {
  const vs = window._pendingVideoStyle;
  if (!vs) return;
  useVideoStyle(window._videoStyles.length - 1);
  document.getElementById('video-style-result').style.display = 'none';
}

// ============================================================
// Script Review — ตรวจ script หาจุดน่าเบื่อ + เสนอ hook ใหม่
// ============================================================

function showSavedReview() {
  // แสดงผล review ที่เก็บไว้ใน JSON (read-only — ระบบตรวจอัตโนมัติแล้ว)
  const overlay = document.createElement('div');
  overlay.className = 'settings-modal-overlay visible';
  overlay.id = 'script-review-overlay';
  overlay.style.zIndex = '10001';
  overlay.innerHTML = `
    <div class="settings-modal" style="max-width:700px;max-height:85vh;overflow-y:auto">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <h3 style="color:#e0e0e0;font-size:16px;margin:0">🎬 Script Review (ระบบตรวจอัตโนมัติ)</h3>
        <button onclick="document.getElementById('script-review-overlay').remove()" style="background:none;border:none;color:#888;font-size:20px;cursor:pointer">×</button>
      </div>
      <div id="script-review-body" style="color:#e0e0e0;font-size:13px">กำลังโหลด...</div>
    </div>
  `;
  document.body.appendChild(overlay);

  // ดึง review จาก _currentMediaPost (มีอยู่แล้วใน context)
  const sr = _currentMediaPost && _currentMediaPost.scriptReview;
  if (!sr || !sr.review) {
    document.getElementById('script-review-body').innerHTML = '<div style="color:#888">ไม่พบผล review</div>';
    return;
  }
  // alwaysApplied = true → ซ่อนปุ่ม apply (read-only)
  _renderReviewResult({ok: true, review: sr.review}, (_currentMediaPost && _currentMediaPost.platform) || 'TikTok', true);
}

function _renderReviewResult(data, platform, alreadyApplied) {
  const body = document.getElementById('script-review-body');
  if (!data.ok) { body.innerHTML = '<div style="color:#f44336">ตรวจไม่สำเร็จ: ' + escapeHtml(data.error || '') + '</div>'; return; }
  const rv = data.review;
  let html = '';
  // Score — แสดงคะแนนรวม + คะแนนย่อย (เหมือน Opus Clip Viral Score)
  if (rv.score !== undefined) {
    const score = rv.score;
    const threshold = 70;  // ตรงกับ config
    let scoreColor, scoreLabel, scoreBg;
    if (score >= threshold) { scoreColor = '#4caf50'; scoreLabel = 'พอใช้แล้ว'; scoreBg = '#1b3a2a'; }
    else if (score >= 50) { scoreColor = '#ff9800'; scoreLabel = 'ยังไม่ดีพอ — แก้แล้วตรวจใหม่'; scoreBg = '#3a2a1b'; }
    else { scoreColor = '#f44336'; scoreLabel = 'แย่ — ต้องแก้'; scoreBg = '#3a1b1b'; }
    html += '<div style="background:' + scoreBg + ';border:1px solid ' + scoreColor + ';border-radius:8px;padding:12px;margin-bottom:12px;text-align:center">';
    html += '<div style="font-size:32px;font-weight:bold;color:' + scoreColor + '">' + score + '<span style="font-size:14px;color:#555">/100</span></div>';
    html += '<div style="color:' + scoreColor + ';font-size:12px;margin-top:4px">' + scoreLabel + '</div>';
    html += '</div>';
    // Component scores
    const cs = rv.component_scores || {};
    const components = [
      {key: 'hook', label: 'Hook', max: 25},
      {key: 'pacing', label: 'จังหวะ', max: 25},
      {key: 'clarity', label: 'ความชัดเจน', max: 25},
      {key: 'engagement', label: 'การดูจนจบ', max: 25},
    ];
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:12px">';
    components.forEach(c => {
      const v = cs[c.key] || 0;
      const pct = (v / c.max) * 100;
      const col = v >= c.max * 0.7 ? '#4caf50' : v >= c.max * 0.5 ? '#ff9800' : '#f44336';
      html += '<div style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:6px">';
      html += '<div style="font-size:10px;color:#888">' + c.label + '</div>';
      html += '<div style="display:flex;align-items:center;gap:4px;margin-top:2px">';
      html += '<div style="flex:1;height:4px;background:#2a2d3a;border-radius:2px;overflow:hidden"><div style="height:100%;width:' + pct + '%;background:' + col + '"></div></div>';
      html += '<span style="font-size:11px;color:' + col + '">' + v + '/' + c.max + '</span>';
      html += '</div></div>';
    });
    html += '</div>';
  }
  // Issues
  if (rv.issues && rv.issues.length) {
    html += '<div style="color:#ff9800;font-weight:600;margin-bottom:8px">⚠ จุดที่น่าเบื่อ (' + rv.issues.length + '):</div>';
    rv.issues.forEach((iss, i) => {
      html += '<div style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;margin-bottom:6px">';
      html += '<div style="color:#7c8aff;font-size:11px">' + escapeHtml(iss.timestamp || '') + '</div>';
      html += '<div style="margin:4px 0">ปัญหา: ' + escapeHtml(iss.problem || '') + '</div>';
      html += '<div style="color:#4caf50">แก้: ' + escapeHtml(iss.fix || '') + '</div>';
      html += '</div>';
    });
  } else {
    html += '<div style="color:#4caf50;margin-bottom:8px">✓ ไม่พบจุดน่าเบื่อ</div>';
  }
  // Suggested hooks
  if (rv.suggested_hooks && rv.suggested_hooks.length) {
    html += '<div style="color:#7c8aff;font-weight:600;margin:12px 0 8px 0">🎯 Hook ใหม่ ' + rv.suggested_hooks.length + ' แบบ:</div>';
    rv.suggested_hooks.forEach((hook, i) => {
      html += '<div style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;margin-bottom:6px;cursor:pointer" onclick="navigator.clipboard.writeText(this.innerText.substring(' + ((i+1).toString().length + 3) + '));alert(\'คัดลอกแล้ว\')">';
      html += '<span style="color:#555;font-size:11px">' + (i+1) + '.</span> ' + escapeHtml(hook);
      html += '</div>';
    });
  }
  // Revised script (read-only — ระบบ apply เองแล้ว)
  if (rv.revised_script) {
    html += '<div style="color:#7c8aff;font-weight:600;margin:12px 0 8px 0">📝 Script ที่แก้แล้ว:</div>';
    html += '<textarea id="revised-script" readonly style="width:100%;min-height:150px;background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;padding:8px;color:#e0e0e0;font-size:12px;font-family:SF Mono,Consolas,monospace;resize:vertical">' + escapeHtml(rv.revised_script) + '</textarea>';
    html += '<div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap">';
    html += '<button onclick="navigator.clipboard.writeText(document.getElementById(\'revised-script\').value);alert(\'คัดลอกแล้ว\')" style="background:#7c8aff;border:none;color:#fff;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:11px">📋 คัดลอก</button>';
    if (alreadyApplied) {
      html += '<span style="color:#4caf50;font-size:11px;padding:6px 12px">✓ ระบบใช้ script นี้แล้ว</span>';
    }
    html += '</div>';
  }
  body.innerHTML = html;
}

// ============================================================
// Voice Learning modal — upload ตัวอย่าง → AI วิเคราะห์โทนเสียง
// ============================================================
let _voiceLearnFiles = [];
let _voiceLearnUrls = [];
let _voiceLearnTexts = [];  // ตัวอย่างโพสต์ที่ paste แล้วเป็น card แยก
let _voiceLearnAbort = null;  // AbortController สำหรับ cancel request

function openVoiceLearnModal() {
  _voiceLearnFiles = [];
  _voiceLearnUrls = [];
  _voiceLearnTexts = [];
  _voiceLearnAbort = null;
  const overlay = document.getElementById('voice-learn-overlay');
  document.getElementById('voice-learn-text-input').value = '';
  document.getElementById('voice-learn-url-input').value = '';
  document.getElementById('voice-learn-file-list').innerHTML = '';
  renderVoiceLearnTextCards();
  renderVoiceLearnUrlChips();
  document.getElementById('voice-learn-status').textContent = '';
  document.getElementById('voice-learn-status').className = 'upload-status';
  document.getElementById('voice-learn-result').innerHTML = '';
  // รีเซ็ตปุ่ม
  document.getElementById('voice-learn-analyze-btn').textContent = 'วิเคราะห์';
  document.getElementById('voice-learn-analyze-btn').disabled = false;
  overlay.className = 'settings-modal-overlay visible';
}

function closeVoiceLearnModal() {
  // ถ้ากำลังวิเคราะห์อยู่ → cancel request ก่อนปิด
  if (_voiceLearnAbort) {
    _voiceLearnAbort.abort();
    _voiceLearnAbort = null;
  }
  document.getElementById('voice-learn-overlay').className = 'settings-modal-overlay';
}

// --- Text example cards ---
function addVoiceLearnText() {
  const input = document.getElementById('voice-learn-text-input');
  const text = input.value.trim();
  if (!text) return;
  _voiceLearnTexts.push(text);
  input.value = '';
  renderVoiceLearnTextCards();
}

function removeVoiceLearnText(idx) {
  _voiceLearnTexts.splice(idx, 1);
  renderVoiceLearnTextCards();
}

function renderVoiceLearnTextCards() {
  const el = document.getElementById('voice-learn-text-cards');
  if (!_voiceLearnTexts.length) { el.innerHTML = ''; return; }
  el.innerHTML = _voiceLearnTexts.map((text, i) => {
    const preview = text.length > 120 ? text.substring(0, 120) + '...' : text;
    return '<div style="display:flex;align-items:flex-start;gap:8px;padding:10px 12px;background:#1e2030;border:1px solid #2a2d3a;border-radius:8px;margin-bottom:6px">' +
      '<span style="font-size:14px;color:#7c8aff;flex-shrink:0">📝</span>' +
      '<span style="flex:1;font-size:12px;color:#ccc;line-height:1.5;white-space:pre-wrap;word-break:break-word">' + escapeHtml(preview) + '</span>' +
      '<button onclick="removeVoiceLearnText(' + i + ')" style="background:none;border:none;color:#f44;cursor:pointer;font-size:14px;padding:0 2px;flex-shrink:0">✕</button>' +
    '</div>';
  }).join('');
}

// --- URL chip input ---
function handleVoiceLearnUrlKeydown(input, event) {
  // กด Enter → เพิ่มเป็น chip
  if (event.key === 'Enter') {
    event.preventDefault();
    if (input.value.trim()) {
      addVoiceLearnUrl(input.value);
      input.value = '';
    }
  // กด comma → เพิ่มเป็น chip (ละเว้น comma ออก)
  } else if (event.key === ',') {
    event.preventDefault();
    if (input.value.trim()) {
      addVoiceLearnUrl(input.value);
      input.value = '';
    }
  // Backspace ตอนช่องว่าง → ลบ chip สุดท้าย
  } else if (event.key === 'Backspace' && input.value === '' && _voiceLearnUrls.length) {
    _voiceLearnUrls.pop();
    renderVoiceLearnUrlChips();
  }
}

function handleVoiceLearnUrlPaste(input, event) {
  event.preventDefault();
  const text = (event.clipboardData || window.clipboardData).getData('text');
  // แยกด้วย comma หรือ newline เท่านั้น — ไม่ใช่ space (เพราะ URL มี space ไม่ได้แต่อาจมี path สับสน)
  // ถ้าไม่มี separator → ถือว่าเป็น URL เดียว ให้อยู่ใน input รอกด Enter
  if (/[\n,]/.test(text)) {
    const urls = text.split(/[\n,]+/).map(s => s.trim()).filter(s => s);
    for (const u of urls) addVoiceLearnUrl(u);
    input.value = '';
  } else {
    // URL เดียว → ใส่ใน input ให้ user ตรวจก่อนกด Enter
    input.value = text.trim();
  }
}

function addVoiceLearnUrl(url) {
  url = url.trim();
  if (!url) return;
  if (!url.startsWith('http')) url = 'https://' + url;
  if (_voiceLearnUrls.includes(url)) return;
  _voiceLearnUrls.push(url);
  renderVoiceLearnUrlChips();
}

function removeVoiceLearnUrl(idx) {
  _voiceLearnUrls.splice(idx, 1);
  renderVoiceLearnUrlChips();
}

function renderVoiceLearnUrlChips() {
  const el = document.getElementById('voice-learn-url-chips');
  if (!_voiceLearnUrls.length) { el.innerHTML = ''; return; }
  el.innerHTML = _voiceLearnUrls.map((url, i) =>
    '<span style="display:inline-flex;align-items:center;gap:4px;background:#1e2030;border:1px solid #2a2d3a;border-radius:16px;padding:4px 10px;margin:2px 4px 2px 0;font-size:12px;color:#e0e0e0;max-width:280px">' +
    '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(url) + '</span>' +
    '<button onclick="removeVoiceLearnUrl(' + i + ')" style="background:none;border:none;color:#f44;cursor:pointer;font-size:14px;padding:0 2px">✕</button>' +
    '</span>'
  ).join('');
}

// --- File chips ---
function handleVoiceLearnFiles(input) {
  const files = Array.from(input.files);
  for (const f of files) {
    _voiceLearnFiles.push(f);
  }
  input.value = '';  // clear so same file can be re-added
  renderVoiceLearnFiles();
}

function removeVoiceLearnFile(idx) {
  _voiceLearnFiles.splice(idx, 1);
  renderVoiceLearnFiles();
}

function renderVoiceLearnFiles() {
  const el = document.getElementById('voice-learn-file-list');
  if (!_voiceLearnFiles.length) { el.innerHTML = ''; return; }
  el.innerHTML = _voiceLearnFiles.map((f, i) =>
    '<span style="display:inline-flex;align-items:center;gap:4px;background:#1e2030;border:1px solid #2a2d3a;border-radius:16px;padding:4px 10px;margin:2px 4px 2px 0;font-size:12px;color:#e0e0e0;max-width:280px">' +
    '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(f.name) + ' (' + Math.round(f.size/1024) + 'KB)</span>' +
    '<button onclick="removeVoiceLearnFile(' + i + ')" style="background:none;border:none;color:#f44;cursor:pointer;font-size:14px;padding:0 2px">✕</button>' +
    '</span>'
  ).join('');
}

function runVoiceLearn() {
  const status = document.getElementById('voice-learn-status');
  const result = document.getElementById('voice-learn-result');
  const btn = document.getElementById('voice-learn-analyze-btn');

  // ถ้ากำลังวิเคราะห์อยู่ → กดปุ่ม = หยุด
  if (_voiceLearnAbort) {
    _voiceLearnAbort.abort();
    _voiceLearnAbort = null;
    status.className = 'upload-status';
    status.textContent = 'ยกเลิกการวิเคราะห์แล้ว';
    btn.textContent = 'วิเคราะห์';
    btn.disabled = false;
    return;
  }

  // ตัวอย่างมาจาก card array + URL chip array + file array
  const pastedTexts = _voiceLearnTexts.slice();
  const urlList = _voiceLearnUrls.slice();

  if (!pastedTexts.length && !_voiceLearnFiles.length && !urlList.length) {
    status.className = 'upload-status err';
    status.textContent = 'กรุณาใส่ตัวอย่างอย่างน้อย 1 อย่าง (paste text, upload ไฟล์, หรือ URL)';
    return;
  }

  status.className = 'upload-status';
  status.textContent = 'กำลังวิเคราะห์... (3 ส่วน: voice + terms + audience) — กด "หยุด" เพื่อยกเลิก';
  btn.textContent = 'หยุด';
  result.innerHTML = '';

  // สร้าง AbortController
  _voiceLearnAbort = new AbortController();

  // ถ้ามีไฟล์ → upload ก่อน แล้วค่อยส่งรวม
  const uploadPromise = _voiceLearnFiles.length > 0
    ? uploadVoiceLearnFiles(_voiceLearnAbort.signal).then(paths => paths)
    : Promise.resolve([]);

  uploadPromise.then(filePaths => {
    return fetch('/api/voice_learn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pasted_texts: pastedTexts,
        file_paths: filePaths,
        urls: urlList,
      }),
      signal: _voiceLearnAbort.signal,
    }).then(r => r.json());
  }).then(data => {
    _voiceLearnAbort = null;
    btn.textContent = 'วิเคราะห์';
    btn.disabled = false;
    if (data.ok) {
      status.className = 'upload-status ok';
      status.textContent = 'วิเคราะห์สำเร็จ ✓ (จาก ' + data.example_count + ' ตัวอย่าง)';
      renderBrandLearnResult(data.brand);
    } else {
      status.className = 'upload-status err';
      status.textContent = data.error || 'วิเคราะห์ไม่สำเร็จ';
    }
  }).catch(e => {
    _voiceLearnAbort = null;
    btn.textContent = 'วิเคราะห์';
    btn.disabled = false;
    if (e.name === 'AbortError') {
      status.className = 'upload-status';
      status.textContent = 'ยกเลิกการวิเคราะห์แล้ว';
    } else {
      status.className = 'upload-status err';
      status.textContent = 'เกิดข้อผิดพลาด: ' + e.message;
    }
  });
}

function uploadVoiceLearnFiles(signal) {
  const formData = new FormData();
  for (const f of _voiceLearnFiles) {
    formData.append('files', f);
  }
  return fetch('/api/voice_learn_upload', {
    method: 'POST',
    body: formData,
    signal: signal,
  }).then(r => r.json()).then(data => {
    if (data.ok) return data.paths;
    throw new Error(data.error || 'upload ไม่สำเร็จ');
  });
}

function renderBrandLearnResult(brand) {
  const el = document.getElementById('voice-learn-result');
  let html = '<div style="margin-top:16px;border-top:1px solid #2a2d3a;padding-top:12px">';
  html += '<div style="font-size:13px;color:#7c8aff;margin-bottom:12px">ผลการวิเคราะห์ Brand Profile (3 ส่วน)</div>';

  // Voice card
  const v = brand.voice || {};
  if (Object.keys(v).length) {
    html += '<div style="background:#1e2030;border-radius:8px;padding:12px;margin-bottom:10px">';
    html += '<div style="font-size:12px;color:#7c8aff;margin-bottom:6px">🎤 Voice</div>';
    if (v.personality) html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">บุคลิก:</span> <span style="color:#e0e0e0;font-size:12px">' + escapeHtml(v.personality) + '</span></div>';
    if (v.tone_description) html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">โทน:</span> <span style="color:#e0e0e0;font-size:12px">' + escapeHtml(v.tone_description) + '</span></div>';
    if (v.formality_level) html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">ทางการ:</span> <span style="color:#e0e0e0;font-size:12px">' + v.formality_level + '/5</span></div>';
    if (v.language) html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">ภาษา:</span> <span style="color:#e0e0e0;font-size:12px">' + escapeHtml(v.language) + '</span></div>';
    if (v.banned_phrases && v.banned_phrases.length) html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">คำต้องห้าม:</span> <span style="color:#e0e0e0;font-size:12px">' + escapeHtml(v.banned_phrases.join(', ')) + '</span></div>';
    html += '</div>';
  }

  // Terms card
  const t = brand.terms || {};
  if (Object.keys(t).length) {
    html += '<div style="background:#1e2030;border-radius:8px;padding:12px;margin-bottom:10px">';
    html += '<div style="font-size:12px;color:#7c8aff;margin-bottom:6px">📝 Terms</div>';
    if (t.approved && t.approved.length) html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">คำที่ใช้:</span> <span style="color:#e0e0e0;font-size:12px">' + escapeHtml(t.approved.join(', ')) + '</span></div>';
    if (t.restricted && t.restricted.length) html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">คำต้องห้าม:</span> <span style="color:#e0e0e0;font-size:12px">' + escapeHtml(t.restricted.join(', ')) + '</span></div>';
    if (t.replacements && Object.keys(t.replacements).length) {
      const reps = Object.entries(t.replacements).map(([k, val]) => escapeHtml(k) + ' → ' + escapeHtml(val)).join(', ');
      html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">คำที่ควรเปลี่ยน:</span> <span style="color:#e0e0e0;font-size:12px">' + reps + '</span></div>';
    }
    html += '</div>';
  }

  // Audience card
  const a = brand.audience || {};
  if (Object.keys(a).length) {
    html += '<div style="background:#1e2030;border-radius:8px;padding:12px;margin-bottom:10px">';
    html += '<div style="font-size:12px;color:#7c8aff;margin-bottom:6px">👥 Audience</div>';
    if (a.primary) {
      html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">กลุ่มหลัก:</span> <span style="color:#e0e0e0;font-size:12px">' + escapeHtml(a.primary.age || '') + ' ' + escapeHtml(a.primary.role || '') + '</span></div>';
    }
    if (a.pain_points && a.pain_points.length) html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">ปัญหา:</span> <span style="color:#e0e0e0;font-size:12px">' + escapeHtml(a.pain_points.join(', ')) + '</span></div>';
    if (a.channels && a.channels.length) html += '<div style="margin-bottom:4px"><span style="color:#888;font-size:11px">ช่องทาง:</span> <span style="color:#e0e0e0;font-size:12px">' + escapeHtml(a.channels.join(', ')) + '</span></div>';
    html += '</div>';
  }

  // Save buttons
  html += '<div style="display:flex;gap:8px;margin-top:12px">';
  html += '<button class="settings-save" onclick="saveBrandLearnResult(' + JSON.stringify(brand).replace(/"/g, '&quot;') + ', \'all\')">บันทึกทั้ง 3 ส่วน</button>';
  html += '</div>';
  html += '</div>';
  el.innerHTML = html;
}

function saveBrandLearnResult(brand, section) {
  const status = document.getElementById('voice-learn-status');
  let payload;
  if (section === 'all') {
    payload = { voice: brand.voice || {}, terms: brand.terms || {}, audience: brand.audience || {} };
  } else {
    payload = {};
    payload[section] = brand[section] || {};
  }
  fetch('/api/brand_json_save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      if (brand.voice) _brandData.voice = brand.voice;
      if (brand.terms) _brandData.terms = brand.terms;
      if (brand.audience) _brandData.audience = brand.audience;
      status.className = 'upload-status ok';
      status.textContent = 'บันทึก Brand Profile แล้ว ✓';
      setTimeout(closeVoiceLearnModal, 1200);
    } else {
      status.className = 'upload-status err';
      status.textContent = data.error || 'บันทึกไม่สำเร็จ';
    }
  });
}

// ============================================================
// Content Pillars modal
// ============================================================
let _pillarsData = { pillars: [], pillar_keywords: {} };

function openPillarsModal() {
  const status = document.getElementById('pillars-save-status');
  status.className = 'upload-status';
  status.textContent = 'กำลังโหลด...';
  fetch('/api/pillars').then(r => r.json()).then(data => {
    if (data.error) {
      status.className = 'upload-status err';
      status.textContent = data.error;
      return;
    }
    _pillarsData = data;
    renderPillarsList();
    status.textContent = '';
    status.className = 'upload-status';
    document.getElementById('pillars-overlay').className = 'settings-modal-overlay visible';
  });
}

function closePillarsModal() {
  document.getElementById('pillars-overlay').className = 'settings-modal-overlay';
}

function renderPillarsList() {
  const list = document.getElementById('pillars-list');
  list.innerHTML = '';
  _pillarsData.pillars.forEach((pillar, i) => {
    const keywords = _pillarsData.pillar_keywords[pillar] || [];
    const card = document.createElement('div');
    card.className = 'pillar-card';
    card.innerHTML = `
      <div class="pillar-card-header">
        <input class="pillar-card-name" value="${escapeHtml(pillar)}" onchange="updatePillarName(${i}, this.value)" placeholder="ชื่อ pillar">
        <button class="pillar-del-btn" onclick="removePillar(${i})">🗑️</button>
      </div>
      <div class="pillar-keywords-label">Keywords (คำที่ทำให้ระบบเดาได้ว่าอยู่ในหมวดนี้):</div>
      <div class="pillar-keywords-box" id="pillar-kw-${i}"></div>
    `;
    list.appendChild(card);
    renderKeywordChips(i, keywords);
  });
}

function renderKeywordChips(pillarIdx, keywords) {
  const box = document.getElementById('pillar-kw-' + pillarIdx);
  box.innerHTML = '';
  keywords.forEach((kw, j) => {
    const chip = document.createElement('span');
    chip.className = 'pillar-keyword-chip';
    chip.innerHTML = `${escapeHtml(kw)} <span class="chip-x" onclick="removeKeyword(${pillarIdx}, ${j})">×</span>`;
    box.appendChild(chip);
  });
  // input สำหรับเพิ่ม keyword
  const addWrap = document.createElement('span');
  addWrap.className = 'pillar-keyword-add';
  addWrap.innerHTML = `<input placeholder="+ เพิ่มคำ" onkeydown="if(event.key==='Enter'){addKeyword(${pillarIdx}, this.value); this.value='';}">`;
  box.appendChild(addWrap);
}

function addKeyword(pillarIdx, kw) {
  kw = kw.trim();
  if (!kw) return;
  const pillar = _pillarsData.pillars[pillarIdx];
  if (!_pillarsData.pillar_keywords[pillar]) _pillarsData.pillar_keywords[pillar] = [];
  if (!_pillarsData.pillar_keywords[pillar].includes(kw)) {
    _pillarsData.pillar_keywords[pillar].push(kw);
    renderKeywordChips(pillarIdx, _pillarsData.pillar_keywords[pillar]);
  }
}

function removeKeyword(pillarIdx, kwIdx) {
  const pillar = _pillarsData.pillars[pillarIdx];
  if (_pillarsData.pillar_keywords[pillar]) {
    _pillarsData.pillar_keywords[pillar].splice(kwIdx, 1);
    renderKeywordChips(pillarIdx, _pillarsData.pillar_keywords[pillar]);
  }
}

function updatePillarName(idx, newName) {
  newName = newName.trim();
  if (!newName) return;
  const oldName = _pillarsData.pillars[idx];
  // ย้าย keywords ไปชื่อใหม่
  if (_pillarsData.pillar_keywords[oldName]) {
    _pillarsData.pillar_keywords[newName] = _pillarsData.pillar_keywords[oldName];
    delete _pillarsData.pillar_keywords[oldName];
  }
  _pillarsData.pillars[idx] = newName;
}

function removePillar(idx) {
  const pillar = _pillarsData.pillars[idx];
  delete _pillarsData.pillar_keywords[pillar];
  _pillarsData.pillars.splice(idx, 1);
  renderPillarsList();
}

function addPillarCard() {
  const name = 'Pillar ใหม่';
  let n = 1;
  let finalName = name;
  while (_pillarsData.pillars.includes(finalName)) {
    n++;
    finalName = name + ' ' + n;
  }
  _pillarsData.pillars.push(finalName);
  _pillarsData.pillar_keywords[finalName] = [];
  renderPillarsList();
  // focus ช่องสุดท้าย
  const inputs = document.querySelectorAll('.pillar-card-name');
  if (inputs.length) inputs[inputs.length - 1].focus();
}

function savePillars() {
  const status = document.getElementById('pillars-save-status');
  status.className = 'upload-status';
  status.textContent = 'กำลังบันทึก...';
  fetch('/api/pillars_save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      pillars: _pillarsData.pillars,
      pillar_keywords: _pillarsData.pillar_keywords,
    }),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      status.className = 'upload-status ok';
      status.textContent = 'บันทึกแล้ว ✓';
      setTimeout(closePillarsModal, 800);
    } else {
      status.className = 'upload-status err';
      status.textContent = data.error || 'เกิดข้อผิดพลาด';
    }
  });
}

function onDragStart(ev, folderPath) {
  ev.dataTransfer.setData('text/plain', folderPath);
  // บันทึก source: 'sidebar' หรือ 'agent' + ชื่อ agent ต้นทาง
  const isAgentItem = ev.target.classList.contains('dropped-folder');
  if (isAgentItem) {
    ev.dataTransfer.setData('source', 'agent');
    // หา agent ต้นทางจาก parent box
    const box = ev.target.closest('.agent-box');
    const agentKey = box ? box.id.replace('box-', '') : '';
    ev.dataTransfer.setData('source-agent', agentKey);
  } else {
    ev.dataTransfer.setData('source', 'sidebar');
  }
  ev.target.classList.add('dragging');
}

function onDragEnd(ev) {
  ev.target.classList.remove('dragging');
}

// renderAgentBoxes ถูกแทนทีโดย flow wizard ใน src/wizard_ui.js

function loadConflictIcons() {
  // ดึงจาก central cache — ตรวจครั้งเดียวตอน save, ไม่ตรวจใหม่ทุกครั้ง
  fetch('/api/conflicts').then(r => r.json()).then(data => {
    // 1. Agent cards — ⚠ icon ข้างปุ่มตั้งค่า
    for (const key of AGENT_ORDER) {
      const icon = document.getElementById('conflict-icon-' + key);
      if (!icon) continue;
      const agentData = data[key];
      if (agentData && agentData.has_conflict) {
        icon.style.display = 'inline';
        const count = agentData.conflicts.length;
        icon.title = 'การตั้งค่านี้ขัดกับกฎแบรนด์ (' + count + ' จุด) — คลิกเพื่อดู';
      } else {
        icon.style.display = 'none';
      }
    }
    // 2. Brand sidebar — ⚠ icon ข้างชื่อ section ที่มี conflict
    _updateBrandSidebarConflicts(data);
  }).catch(() => {});
}

function _updateBrandSidebarConflicts(conflictsData) {
  // ตรวจว่า section ไหนมี conflict → ใส่ ⚠ icon ข้างชื่อใน sidebar
  const sectionFields = {
    voice: ['tone', 'language', 'sell_style', 'hook_style'],
    terms: ['custom_banned_phrase', 'custom_restricted_term'],
  };
  for (const [section, fields] of Object.entries(sectionFields)) {
    const el = document.getElementById('brand-section-' + section);
    if (!el) continue;
    let count = 0;
    for (const agentData of Object.values(conflictsData)) {
      if (!agentData.has_conflict) continue;
      for (const c of agentData.conflicts) {
        if (fields.includes(c.field)) count++;
      }
    }
    // เก็บ label เดิม (ไม่มี ⚠) ไว้ — ใช้ data attribute
    if (!el.dataset.label) el.dataset.label = el.textContent.replace(/^⚠\s*/, '');
    if (count > 0) {
      el.textContent = '⚠ ' + el.dataset.label;
      el.style.color = '#fbbf24';
      el.title = count + ' จุดขัดแย้งกับ agent — คลิกเพื่อดูรายละเอียด';
    } else {
      el.textContent = el.dataset.label;
      el.style.color = '';
      el.title = '';
    }
  }
}

function toggleOptChip(el) {
  el.classList.toggle('active');
  el.classList.toggle('green');
  // sync auto media ไปยัง backend
  if (el.id === 'opt-auto-image') {
    toggleAutoMedia('auto_generate_image', el.classList.contains('active'));
  } else if (el.id === 'opt-auto-video') {
    toggleAutoMedia('auto_generate_video', el.classList.contains('active'));
  }
  // re-render flow เพื่อให้เห็นการเปลี่ยนแปลง
  showFlow();
}

function updateCountChip() {
  const input = document.getElementById('opt-count');
  if (!input) return;
  let v = parseInt(input.value) || 1;
  if (v < 1) v = 1;
  if (v > 20) v = 20;
  input.value = v;
  // re-render flow
  showFlow();
}

function getContentCreatorOptions() {
  const platformEl = document.getElementById('opt-platform');
  const platformVal = platformEl ? platformEl.value : 'auto';
  let platforms;
  if (platformVal === 'auto') {
    platforms = ['facebook', 'tiktok'];
  } else {
    platforms = [platformVal];
  }
  const mediaTypeEl = document.getElementById('opt-media-type');
  const mediaType = mediaTypeEl ? mediaTypeEl.value : 'image';
  const mediaWhenEl = document.getElementById('opt-media-when');
  const mediaWhen = mediaWhenEl ? mediaWhenEl.value : 'ask';
  const competitorEl = document.getElementById('opt-competitor');
  const campaignEl = document.getElementById('opt-campaign');
  const countEl = document.getElementById('opt-count');
  return {
    platforms: platforms,
    platform_mode: platformVal,
    use_competitor: competitorEl ? competitorEl.classList.contains('active') : true,
    use_campaign: campaignEl ? campaignEl.classList.contains('active') : true,
    content_count: parseInt(countEl ? countEl.value : '1') || 1,
    media_type: mediaType,
    media_when: mediaWhen,
    auto_image: (mediaType === 'image' || mediaType === 'both') && mediaWhen === 'auto',
    auto_video: (mediaType === 'video' || mediaType === 'both') && mediaWhen === 'auto',
  };
}

function onMediaSettingsChange() {
  showFlow();
}

async function loadMediaConfigToChips() {
  try {
    const r = await fetch('/api/media_config');
    const cfg = await r.json();
    const imgChip = document.getElementById('opt-auto-image');
    const vidChip = document.getElementById('opt-auto-video');
    if (imgChip && cfg.auto_generate_image) { imgChip.classList.add('active', 'green'); }
    if (vidChip && cfg.auto_generate_video) { vidChip.classList.add('active', 'green'); }
  } catch (e) {}
}

function _clearDropHover(box) {
  box.classList.remove('drag-over');
  box.querySelectorAll('.drop-hover-combined, .drop-hover-separate, .drop-hover').forEach(el => {
    el.classList.remove('drop-hover-combined', 'drop-hover-separate', 'drop-hover');
  });
}

function onDragOver(ev, agentKey) {
  ev.preventDefault();
  const box = document.getElementById('box-' + agentKey);
  const target = ev.target;
  const combinedZone = target.closest && target.closest('[data-zone="combined"]');
  const separateZone = target.closest && target.closest('[data-zone="separate"]');
  const dz = document.getElementById('dropzone-' + agentKey);

  // Clear previous hover state on subzones (keep box flag)
  box.querySelectorAll('.drop-hover-combined, .drop-hover-separate').forEach(el => {
    el.classList.remove('drop-hover-combined', 'drop-hover-separate');
  });

  if (combinedZone) {
    combinedZone.classList.add('drop-hover-combined');
  } else if (separateZone) {
    separateZone.classList.add('drop-hover-separate');
  } else if (dz) {
    // Simple agent (no subzones) — highlight whole drop-zone
    dz.classList.add('drop-hover');
  }
  box.classList.add('drag-over');
}

function onDragLeave(ev, agentKey) {
  // Only clear when leaving the box entirely (relatedTarget outside box)
  const box = document.getElementById('box-' + agentKey);
  if (ev.relatedTarget && box.contains(ev.relatedTarget)) return;
  _clearDropHover(box);
}

function onDrop(ev, agentKey) {
  ev.preventDefault();
  const box = document.getElementById('box-' + agentKey);
  _clearDropHover(box);
  const folderPath = ev.dataTransfer.getData('text/plain');
  if (!folderPath) return;
  if (!agentFolders[agentKey]) agentFolders[agentKey] = { separate: [], combined: [] };

  // ตรวจ source: ถ้าลากจาก agent อื่น → ย้าย (ลบจาก agent เดิม)
  const source = ev.dataTransfer.getData('source');
  const sourceAgent = ev.dataTransfer.getData('source-agent');
  if (source === 'agent' && sourceAgent && sourceAgent !== agentKey) {
    // ลบจาก agent ต้นทางทั้ง separate และ combined
    if (agentFolders[sourceAgent]) {
      agentFolders[sourceAgent].separate = agentFolders[sourceAgent].separate.filter(f => f !== folderPath);
      agentFolders[sourceAgent].combined = agentFolders[sourceAgent].combined.filter(f => f !== folderPath);
      updateDropZone(sourceAgent);
    }
  }

  // Check which subzone was dropped on
  const target = ev.target;
  const combinedZone = target.closest && target.closest('[data-zone="combined"]');
  const separateZone = target.closest && target.closest('[data-zone="separate"]');

  if (combinedZone) {
    // Add to combined — multiple products in one job
    if (!agentFolders[agentKey].combined.includes(folderPath)) {
      // Remove from separate if present
      agentFolders[agentKey].separate = agentFolders[agentKey].separate.filter(f => f !== folderPath);
      agentFolders[agentKey].combined.push(folderPath);
    }
  } else if (separateZone) {
    // Separate zone — also move out of combined if present
    agentFolders[agentKey].combined = agentFolders[agentKey].combined.filter(f => f !== folderPath);
    if (!agentFolders[agentKey].separate.includes(folderPath)) {
      agentFolders[agentKey].separate.push(folderPath);
    }
  } else {
    // Simple agent (no subzones) — treat as separate
    agentFolders[agentKey].combined = agentFolders[agentKey].combined.filter(f => f !== folderPath);
    if (!agentFolders[agentKey].separate.includes(folderPath)) {
      agentFolders[agentKey].separate.push(folderPath);
    }
  }
  updateDropZone(agentKey);
}

function updateDropZone(agentKey) {
  const dz = document.getElementById('dropzone-' + agentKey);
  const data = agentFolders[agentKey] || { separate: [], combined: [] };
  const sep = data.separate || [];
  const comb = data.combined || [];
  const showCombined = (agentKey === 'content_creator' || agentKey === 'campaign_strategy');

  // For agents without combined/separate — simple single zone
  if (!showCombined) {
    if (!sep.length) {
      dz.innerHTML = '';
      updateConfirmBtn();
      return;
    }
    let html = '';
    for (const fpath of sep) {
      const safe = fpath.replace(/'/g, "\\'");
      html += '<div class="dropped-folder" draggable="true" ondragstart="onDragStart(event,\'' + safe + '\')" ondragend="onDragEnd(event)">';
      html += '<span>📁</span><span>' + escapeHtml(fpath) + '</span>';
      html += '<span class="remove-btn" onclick="removeFolder(\'' + agentKey + '\',\'' + safe + '\',\'separate\')">×</span>';
      html += '</div>';
    }
    dz.innerHTML = html;
    updateConfirmBtn();
    return;
  }

  // For campaign_strategy / content_creator — always show 2 subzones
  let html = '';
  // Combined zone — each product shown as its own draggable item
  html += '<div class="drop-subzone" data-zone="combined">';
  html += '<div class="drop-subzone-label">🔗 รวม — หลายชิ้นทำงานเดียว</div>';
  if (comb.length) {
    for (const fpath of comb) {
      const safe = fpath.replace(/'/g, "\\'");
      const isAuto = fpath === AUTO_ITEM;
      const icon = isAuto ? '⚡' : '📦';
      const label = isAuto ? 'Auto' : escapeHtml(fpath);
      const cls = isAuto ? 'dropped-folder combined-folder auto-folder' : 'dropped-folder combined-folder';
      html += '<div class="' + cls + '" draggable="true" ondragstart="onDragStart(event,\'' + safe + '\')" ondragend="onDragEnd(event)">';
      html += '<span>' + icon + '</span><span>' + label + '</span>';
      if (isAuto) {
        html += '<input type="number" class="auto-count-input" min="2" max="' + readyProductCount + '" value="' + Math.min(2, readyProductCount) + '" onclick="event.stopPropagation()" onchange="showFlow()" title="จำนวนสินค้าที่ AI จะเลือก">';
      }
      html += '<span class="remove-btn" onclick="removeFolder(\'' + agentKey + '\',\'' + safe + '\',\'combined\')">×</span>';
      html += '</div>';
    }
  }
  html += '</div>';
  // Separate zone — each product shown as its own draggable item
  html += '<div class="drop-subzone" data-zone="separate">';
  html += '<div class="drop-subzone-label">📋 แยก — ชิ้นละงาน</div>';
  if (sep.length) {
    for (const fpath of sep) {
      const safe = fpath.replace(/'/g, "\\'");
      const isAuto = fpath === AUTO_ITEM;
      const icon = isAuto ? '⚡' : '📁';
      const label = isAuto ? 'Auto' : escapeHtml(fpath);
      const cls = isAuto ? 'dropped-folder auto-folder' : 'dropped-folder';
      html += '<div class="' + cls + '" draggable="true" ondragstart="onDragStart(event,\'' + safe + '\')" ondragend="onDragEnd(event)">';
      html += '<span>' + icon + '</span><span>' + label + '</span>';
      html += '<span class="remove-btn" onclick="removeFolder(\'' + agentKey + '\',\'' + safe + '\',\'separate\')">×</span>';
      html += '</div>';
    }
  }
  html += '</div>';
  dz.innerHTML = html;
  // โชว์/ซ่อน toggle chips ของ content_creator — โผล่ตอนมีสินค้า (รวม Auto)
  if (agentKey === 'content_creator') {
    const hasProduct = sep.length > 0 || comb.length > 0;
    const optEl = document.getElementById('options-content_creator');
    if (optEl) optEl.classList.toggle('visible', hasProduct);
  }
  updateConfirmBtn();
}

function removeFolder(agentKey, folderPath, zone) {
  if (!agentFolders[agentKey]) return;
  if (zone === 'combined') {
    agentFolders[agentKey].combined = agentFolders[agentKey].combined.filter(f => f !== folderPath);
  } else {
    agentFolders[agentKey].separate = agentFolders[agentKey].separate.filter(f => f !== folderPath);
  }
  updateDropZone(agentKey);
}

function clearAgent(agentKey) {
  agentFolders[agentKey] = { separate: [], combined: [] };
  updateDropZone(agentKey);
  const box = document.getElementById('box-' + agentKey);
  const status = document.getElementById('status-' + agentKey);
  const output = document.getElementById('output-' + agentKey);
  const fileLink = document.getElementById('file-' + agentKey);
  box.className = 'agent-box';
  status.className = 'agent-status';
  status.textContent = '';
  output.className = 'agent-output';
  output.textContent = '';
  fileLink.className = 'agent-file-link';
  fileLink.textContent = '';
  fileLink._resultFiles = null;  // reset multi-set file list
}

function clearAll() {
  for (const key of AGENT_ORDER) {
    clearAgent(key);
  }
}

function buildExecutionPlan() {
  const plans = [];

  for (const key of AGENT_ORDER) {
    const data = agentFolders[key] || { separate: [], combined: [] };
    const sep = data.separate || [];
    const comb = data.combined || [];

    // Combined plan: multiple products → one job
    if (comb.length >= 2) {
      const info = AGENT_INFO[key] || {};
      const step = buildStep(key, info);
      const folderLabel = comb.map(f => f === AUTO_ITEM ? '⚡ Auto' : f).join(' + ');
      plans.push({ folder: folderLabel, folders: comb, mode: 'combined', steps: [step] });
    } else if (comb.length === 1) {
      // รวม 1 ชิ้น = แยก 1 ชิ้น (ไม่มีอะไรให้รวม) → ทำเป็น separate
      const info = AGENT_INFO[key] || {};
      const step = buildStep(key, info);
      const folderLabel = comb[0] === AUTO_ITEM ? '⚡ Auto' : comb[0];
      plans.push({ folder: folderLabel, folders: [comb[0]], mode: 'separate', steps: [step] });
    }

    // Separate plans: one per folder
    for (const folder of sep) {
      const info = AGENT_INFO[key] || {};
      const step = buildStep(key, info);
      const folderLabel = folder === AUTO_ITEM ? '⚡ Auto' : folder;
      plans.push({ folder: folderLabel, folders: [folder], mode: 'separate', steps: [step] });
    }
  }

  return plans;
}

function buildStep(key, info) {
  if (key === 'content_creator') {
    const opts = getContentCreatorOptions();
    const platformLabels = { facebook: '📘 FB', tiktok: '🎵 TikTok' };
    const platformText = opts.platform_mode === 'auto' ? '📘 FB หรือ 🎵 TikTok' : (platformLabels[opts.platform_mode] || '');
    const parts = ['ใช้ข้อมูลสินค้า'];
    if (opts.use_competitor) parts.push('ข้อมูลคู่แข่ง');
    if (opts.use_campaign) parts.push('กลยุทธ์แคมเปญ');
    let reason = parts.join(' + ') + ' → เขียนคอนเทนต์';
    if (platformText) reason += ` สำหรับ ${platformText}`;
    if (opts.content_count > 1) reason += ` (${opts.content_count} โพสต์ที่ไม่ซ้ำกัน)`;
    // media settings
    const mediaLabels = { prompt: 'เฉพาะ prompt', image: '🎨 รูป', video: '🎬 วิดีโอ', both: '🎨 รูป + 🎬 วิดีโอ' };
    const whenLabels = { ask: 'ถามก่อน', auto: 'สร้างทันที' };
    if (opts.media_type && opts.media_type !== 'prompt') {
      reason += ` + ${mediaLabels[opts.media_type]} (${whenLabels[opts.media_when]})`;
    }
    return { key: key, name: info.name || key, icon: info.icon || '', isAuto: false, reason, platforms: opts.platforms, platform_mode: opts.platform_mode };
  }
  return { key: key, name: info.name || key, icon: info.icon || '', isAuto: false, reason: info.flow_reason || '' };
}

function showFlow(plansArg) {
  const flowEl = document.getElementById('flow-display');
  const plans = plansArg || buildExecutionPlan();
  if (!plans.length) {
    flowEl.className = 'flow-display';
    flowEl.innerHTML = '';
    return;
  }
  let html = '';
  for (let planIdx = 0; planIdx < plans.length; planIdx++) {
    const plan = plans[planIdx];
    html += '<div class="flow-product">';
    html += '<div class="flow-title">📦 <b>' + escapeHtml(plan.folder) + '</b> — ' + plan.steps.length + ' ขั้นตอน';
    if (plan.mode === 'combined') {
      html += ' <span style="font-size:11px;padding:1px 6px;border-radius:4px;background:#2a1a3a;color:#a78bfa">รวม</span>';
    } else {
      html += ' <span style="font-size:11px;padding:1px 6px;border-radius:4px;background:#1a2a3a;color:#7c8aff">แยก</span>';
    }
    html += '</div>';
    html += '<div class="flow-steps">';
    for (let i = 0; i < plan.steps.length; i++) {
      const step = plan.steps[i];
      const stepId = 'flow-' + planIdx + '-' + i;
      if (i > 0) html += '<span class="flow-arrow">→</span>';
      html += '<div class="flow-step' + (step.isAuto ? ' auto' : '') + '" id="' + stepId + '" data-agent="' + step.key + '" title="' + escapeHtml(step.reason) + '">';
      html += '<div class="flow-spinner"></div>';
      html += '<span class="flow-step-icon">' + step.icon + '</span>';
      html += '<span class="flow-step-name">' + escapeHtml(step.name) + '</span>';
      if (step.isAuto) html += '<span class="flow-step-badge">อัตโนมัติ</span>';
      // โชว์ chips ที่เลือกเป็น badges ใน content_creator step
      if (step.key === 'content_creator') {
        const opts = getContentCreatorOptions();
        // platform badge
        if (opts.platform_mode === 'auto') {
          html += '<span class="flow-step-badge" style="background:#1a2a4a;color:#7c9aff">📘 FB</span>';
          html += '<span class="flow-step-badge" style="background:#2a1a1a;color:#ff7c9a">🎵 TikTok</span>';
        } else if (opts.platform_mode === 'facebook') {
          html += '<span class="flow-step-badge" style="background:#1a2a4a;color:#7c9aff">📘 FB</span>';
        } else if (opts.platform_mode === 'tiktok') {
          html += '<span class="flow-step-badge" style="background:#2a1a1a;color:#ff7c9a">🎵 TikTok</span>';
        }
        if (opts.content_count > 1) {
          html += '<span class="flow-step-badge" style="background:#1a2a3a;color:#7c8aff">' + opts.content_count + ' โพสต์</span>';
        }
        // media badges
        if (opts.media_type === 'image' || opts.media_type === 'both') {
          html += '<span class="flow-step-badge" style="background:#2a1a3a;color:#c792ea">🎨 รูป</span>';
        }
        if (opts.media_type === 'video' || opts.media_type === 'both') {
          html += '<span class="flow-step-badge" style="background:#2a1a3a;color:#c792ea">🎬 วิดีโอ</span>';
        }
        if (opts.media_when === 'auto' && opts.media_type !== 'prompt') {
          html += '<span class="flow-step-badge" style="background:#1a2a1a;color:#86efac">⚡ อัตโนมัติ</span>';
        }
      }
      html += '<span class="flow-step-status" id="' + stepId + '-status"></span>';
      html += '</div>';
    }
    html += '</div>';
    // media action box สำหรับ content_creator — แจ้ง user ว่าพร้อมสร้างในหน้า output
    if (plan.steps.some(s => s.key === 'content_creator')) {
      const ccStepIdx = plan.steps.findIndex(s => s.key === 'content_creator');
      const ccStepId = 'flow-' + planIdx + '-' + ccStepIdx;
      const opts = getContentCreatorOptions();
      const mediaWhen = opts.media_when || 'ask';
      html += '<div class="flow-media-action" id="' + ccStepId + '-media-action" data-folder="' + escapeHtml(plan.folder) + '">';
      if (mediaWhen === 'auto') {
        html += '<div class="flow-media-action-label">⚡ สื่อสร้างอัตโนมัติแล้ว (auto mode)</div>';
      } else {
        html += '<div class="flow-media-action-label">🎨 พร้อมสร้างสื่อ — ไปกดที่หน้าผลลัพธ์ (คลิก "ดูผลลัพธ์" แล้วกดสร้างรูป/วิดีโอได้ที่นั่น)</div>';
      }
      html += '<div class="flow-media-status" id="' + ccStepId + '-media-status"></div>';
      html += '</div>';
    }
    // Explanation
    html += '<div class="flow-explain">';
    for (const step of plan.steps) {
      html += '<div class="flow-explain-item"><span>' + step.icon + '</span> <b>' + escapeHtml(step.name) + '</b>: ' + escapeHtml(step.reason) + '</div>';
    }
    html += '</div>';
    html += '</div>';
  }
  flowEl.innerHTML = html;
  flowEl.className = 'flow-display visible';
}

function updateConfirmBtn() {
  const hasFolders = AGENT_ORDER.some(key => {
    const d = agentFolders[key];
    return d && ((d.separate && d.separate.length > 0) || (d.combined && d.combined.length > 0));
  });
  const btn = document.getElementById('global-confirm');
  if (btn) btn.disabled = !hasFolders;
  showFlow();
}

async function checkProductStatus(folder) {
  try {
    const r = await fetch('/api/ingest_status/' + encodeURIComponent(folder));
    const data = await r.json();
    return data.status || 'empty';
  } catch (e) {
    return 'empty';
  }
}

function onConfirmClick() {
  const confirmBtn = document.getElementById('global-confirm');
  if (confirmBtn && confirmBtn.classList.contains('running')) {
    // กำลังทำงานอยู่ → กด = หยุด
    stopAll();
  } else {
    // ตรวจว่า content_creator มี Auto item ไหม → ถ้ามี ทำ auto mode
    const cc = agentFolders['content_creator'] || { separate: [], combined: [] };
    const allCC = [...(cc.separate || []), ...(cc.combined || [])];
    if (allCC.includes(AUTO_ITEM)) {
      runAutoMode();
    } else {
      confirmAndRun();
    }
  }
}

async function confirmAndRun() {
  const plans = buildExecutionPlan();
  if (!plans.length) return;

  // ตรวจสถานะสินค้าทุกชิ้น — ถ้าไม่พร้อม ปฏิเสธพร้อมบอกเหตุผล
  // (product_spec agent ไม่ต้องตรวจ เพราะมันทำ deliverable จาก raw data ไม่ใช่จาก DB)
  const notReady = [];
  for (const plan of plans) {
    // ถ้า plan มีแค่ product_spec → ไม่ต้องตรวจ DB
    const needsDb = plan.steps.some(s => s.key !== 'product_spec');
    if (!needsDb) continue;
    // เช็คทุก folder ใน plan แยก (combined mode มีหลาย folder)
    for (const folder of plan.folders) {
      if (folder === AUTO_ITEM) continue;  // Auto ไม่ต้องเช็ค status
      const status = await checkProductStatus(folder);
      if (status !== 'ready' && status !== 'stale') {
        notReady.push({folder, status});
      }
    }
  }
  if (notReady.length) {
    const statusLabels = {
      'empty': 'ว่าง (ยังไม่มีไฟล์)',
      'pending': 'มีไฟล์แต่ยังไม่ได้ประมวลผลข้อมูล',
      'no_usable_data': 'ไม่มีไฟล์ที่รองรับ',
      'processing': 'กำลังประมวลผลข้อมูลอยู่',
    };
    const msg = notReady.map(n => '• ' + n.folder + ': ' + (statusLabels[n.status] || n.status)).join('\n');
    alert('สินค้าต่อไปนี้ยังไม่พร้อมใช้งาน — กรุณากด ⚙ แล้วประมวลผลข้อมูลก่อน:\n\n' + msg);
    return;
  }

  const confirmBtn = document.getElementById('global-confirm');
  const autoBtn = document.getElementById('global-auto');
  const clearBtn = document.getElementById('global-clear');
  const hintEl = document.getElementById('brief-hint');
  // toggle เป็นโหมด "กำลังทำงาน" — ปุ่มยืนยันกลายเป็นปุ่มหยุด
  confirmBtn.classList.add('running');
  confirmBtn.textContent = 'หยุด';
  if (autoBtn) autoBtn.disabled = true;
  if (clearBtn) clearBtn.disabled = true;
  if (hintEl) hintEl.textContent = 'กำลังทำงาน...';

  // ไม่เคลียร์ agent boxes และ agentFolders — ผู้ใช้ต้องกด "ล้าง" เอง
  // แค่เคลียร์ status เพื่อแสดง progress ใหม่
  for (const key of AGENT_ORDER) {
    const status = document.getElementById('status-' + key);
    if (status) { status.className = 'agent-status'; status.textContent = ''; }
  }

  showFlow(plans);

  abortController = new AbortController();
  const promises = plans.map((plan, planIdx) => runPlan(plan, planIdx, abortController.signal));
  await Promise.allSettled(promises);

  // คืนค่าปุ่ม
  confirmBtn.classList.remove('running');
  confirmBtn.textContent = 'ยืนยัน';
  if (autoBtn) autoBtn.disabled = false;
  if (clearBtn) clearBtn.disabled = false;
  if (hintEl) hintEl.textContent = 'เลือกสินค้าก่อนกดยืนยัน';
  // รีเฟรชเครดิตหลังจบการทำงาน
  loadCredits();
}

async function runAutoMode() {
  // Auto mode — agent เลือกสินค้าเอง + คอนเทนต์ไม่ซ้ำ
  const confirmBtn = document.getElementById('global-confirm');
  const clearBtn = document.getElementById('global-clear');
  const hintEl = document.getElementById('brief-hint');
  // toggle ปุ่มยืนยันเป็น "หยุด" ตอนกำลังทำงาน
  confirmBtn.classList.add('running');
  confirmBtn.textContent = 'หยุด';
  if (clearBtn) clearBtn.disabled = true;
  if (hintEl) hintEl.textContent = 'AI กำลังเลือกสินค้าและสร้างคอนเทนต์...';

  // เคลียร์ status
  for (const key of AGENT_ORDER) {
    const status = document.getElementById('status-' + key);
    if (status) { status.className = 'agent-status'; status.textContent = ''; }
  }

  // แสดง flow แบบ auto (1 step: content_creator)
  const flowDisplay = document.getElementById('flow-display');
  if (flowDisplay) {
    flowDisplay.innerHTML = '<div class="flow-plan">' +
      '<div class="flow-step running auto" id="flow-auto-0">' +
      '<span class="flow-step-name">⚡ Auto — เลือกสินค้าเอง</span>' +
      '<span class="flow-step-status" id="flow-auto-0-status"><span class="typing">●</span></span>' +
      '</div></div>';
  }

  const quickBrief = document.getElementById('quick-brief-input').value.trim();
  const opts = getContentCreatorOptions();
  // ดึง product_count จาก Auto chip ใน "รวม" (ถ้ามี)
  const autoCountInput = document.querySelector('.auto-folder .auto-count-input');
  const productCount = autoCountInput ? parseInt(autoCountInput.value) || 2 : 1;

  abortController = new AbortController();
  try {
    const res = await fetch('/api/run_auto', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        quick_brief: quickBrief,
        platforms: opts.platforms,
        media_type: opts.media_type,
        auto_image: opts.auto_image,
        auto_video: opts.auto_video,
        content_count: opts.content_count,
        product_count: productCount,
      }),
      signal: abortController.signal,
    });

    if (!res.ok) {
      const err = await res.json();
      const stepEl = document.getElementById('flow-auto-0');
      const statusEl = document.getElementById('flow-auto-0-status');
      if (stepEl) stepEl.className = 'flow-step error auto';
      if (statusEl) statusEl.textContent = err.error || 'ผิดพลาด';
    } else {
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6));
            handleAutoSSE(data);
          } catch (e) {}
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      const stepEl = document.getElementById('flow-auto-0');
      const statusEl = document.getElementById('flow-auto-0-status');
      if (stepEl) stepEl.className = 'flow-step error auto';
      if (statusEl) statusEl.textContent = 'ผิดพลาด: ' + e.message;
    }
  }

  if (confirmBtn) { confirmBtn.classList.remove('running'); confirmBtn.textContent = 'ยืนยัน'; confirmBtn.disabled = false; }
  if (clearBtn) clearBtn.disabled = false;
  if (hintEl) hintEl.textContent = 'เลือกสินค้าก่อนกดยืนยัน';
  // รีเฟรชเครดิตหลังจบการทำงาน
  loadCredits();
}

function handleAutoSSE(data) {
  const stepEl = document.getElementById('flow-auto-0');
  const statusEl = document.getElementById('flow-auto-0-status');
  if (!stepEl) return;

  if (data.type === 'status') {
    stepEl.className = 'flow-step running auto';
    if (statusEl) statusEl.innerHTML = escapeHtml(data.message || '') + ' <span class="typing">●</span>';
  } else if (data.type === 'selection') {
    // แสดงสินค้าที่เลือก
    try {
      const sel = JSON.parse(data.message || '{}');
      if (statusEl) {
        statusEl.innerHTML = 'เลือก: <b>' + escapeHtml(sel.product_id || '') + '</b> — ' + escapeHtml(sel.concept || '') + ' <span class="typing">●</span>';
      }
    } catch (e) {}
  } else if (data.type === 'agent_start') {
    stepEl.className = 'flow-step running auto';
    if (statusEl) statusEl.innerHTML = '<span class="typing">●</span>';
  } else if (data.type === 'agent_done') {
    const totalSets = data.total_sets || 1;
    const setNum = data.set_num;
    const isLastSet = !setNum || setNum >= totalSets;
    // เพิ่ม file link ของชุดนี้เสมอ (📄1, 📄2, ...)
    if (data.file) {
      const link = document.createElement('span');
      link.className = 'flow-step-link';
      link.textContent = ' 📄' + (setNum || 1);
      link.style.cursor = 'pointer';
      link.style.color = '#7c8aff';
      link.style.marginLeft = '4px';
      // เก็บรายการไฟล์ทั้งหมดของ flow step นี้
      if (!stepEl._resultFiles) stepEl._resultFiles = [];
      stepEl._resultFiles.push(data.file);
      const fileIdx = stepEl._resultFiles.length - 1;
      link.onclick = () => viewResult(data.file, stepEl._resultFiles, fileIdx);
      stepEl.appendChild(link);
    }
    if (totalSets > 1 && setNum && !isLastSet) {
      stepEl.className = 'flow-step running auto';
      if (statusEl) statusEl.textContent = setNum + '/' + totalSets;
    } else {
      stepEl.className = 'flow-step done auto';
      if (statusEl) statusEl.textContent = totalSets > 1 ? '✓ ' + totalSets + ' โพสต์' : '✓';
      // โหลด session ใหม่
      loadSessions();
    }
  } else if (data.type === 'error') {
    stepEl.className = 'flow-step error auto';
    if (statusEl) statusEl.textContent = data.message || 'ผิดพลาด';
  }
}

async function runPlan(plan, planIdx, signal) {
  const folder = plan.folder;
  const agentKeys = plan.steps.map(s => s.key);

  try {
    const quickBrief = document.getElementById('quick-brief-input').value.trim();
    // chips ของ content_creator มีผลเฉพาะ plan ที่มี content_creator
    const hasContentCreator = agentKeys.includes('content_creator');
    const opts = hasContentCreator ? getContentCreatorOptions() : { platforms: ['facebook','tiktok'], use_competitor: true, use_campaign: true, content_count: 1, media_type: 'image', media_when: 'ask', auto_image: false, auto_video: false };
    const res = await fetch('/api/run_agents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agents: agentKeys, folders: plan.folders, mode: plan.mode,
        quick_brief: quickBrief,
        context: { use_competitor: opts.use_competitor, use_campaign: opts.use_campaign },
        content_count: opts.content_count,
        auto_image: opts.auto_image,
        auto_video: opts.auto_video,
        platforms: opts.platforms,
        media_type: opts.media_type,
        media_when: opts.media_when,
      }),
      signal: signal,
    });

    if (!res.ok) {
      const err = await res.json();
      for (let i = 0; i < plan.steps.length; i++) {
        const stepEl = document.getElementById('flow-' + planIdx + '-' + i);
        const statusEl = document.getElementById('flow-' + planIdx + '-' + i + '-status');
        if (stepEl) stepEl.className = 'flow-step error';
        if (statusEl) statusEl.textContent = err.error || 'ผิดพลาด';
      }
    } else {
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6));
            const targetKey = data.agent || agentKeys[0];
            handleFlowSSE(data, targetKey, planIdx, plan);
          } catch (e) {}
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      for (let i = 0; i < plan.steps.length; i++) {
        const stepEl = document.getElementById('flow-' + planIdx + '-' + i);
        const statusEl = document.getElementById('flow-' + planIdx + '-' + i + '-status');
        if (stepEl) stepEl.className = 'flow-step error';
        if (statusEl) statusEl.textContent = 'ผิดพลาด: ' + e.message;
      }
    }
  }
}

function stopAll() {
  if (abortController) { abortController.abort(); abortController = null; }
  fetch('/api/cancel', { method: 'POST' });
  for (const key of AGENT_ORDER) {
    const box = document.getElementById('box-' + key);
    const status = document.getElementById('status-' + key);
    if (box) box.className = 'agent-box';
    if (status) { status.className = 'agent-status'; status.textContent = 'หยุดการทำงาน'; }
  }
  // Reset flow steps
  document.querySelectorAll('.flow-step').forEach(el => {
    el.className = 'flow-step' + (el.classList.contains('auto') ? ' auto' : '');
  });
  document.querySelectorAll('.flow-step-status').forEach(el => { el.textContent = ''; });
  runningAgents = {};
  // คืนค่าปุ่ม toggle
  const confirmBtn = document.getElementById('global-confirm');
  const clearBtn = document.getElementById('global-clear');
  const hintEl = document.getElementById('brief-hint');
  if (confirmBtn) { confirmBtn.classList.remove('running'); confirmBtn.textContent = 'ยืนยัน'; confirmBtn.disabled = false; }
  if (clearBtn) clearBtn.disabled = false;
  if (hintEl) hintEl.textContent = 'เลือกสินค้าก่อนกดยืนยัน';
}

function handleFlowSSE(data, agentKey, planIdx, plan) {
  // Find the step index for this agent in the plan
  let stepIdx = -1;
  for (let i = 0; i < plan.steps.length; i++) {
    if (plan.steps[i].key === agentKey) { stepIdx = i; break; }
  }
  if (stepIdx === -1) return;

  const stepEl = document.getElementById('flow-' + planIdx + '-' + stepIdx);
  const statusEl = document.getElementById('flow-' + planIdx + '-' + stepIdx + '-status');
  if (!stepEl) return;

  if (data.type === 'agent_start') {
    stepEl.className = 'flow-step running' + (plan.steps[stepIdx].isAuto ? ' auto' : '');
    if (statusEl) statusEl.innerHTML = '<span class="typing">●</span>';
  } else if (data.type === 'agent_done') {
    // Multi-content: แต่ละชุดเป็น done แยก แต่ใช้ step เดียวกัน
    const totalSets = data.total_sets || 1;
    const setNum = data.set_num;
    const isLastSet = !setNum || setNum >= totalSets;
    // เพิ่ม file link ของชุดนี้เสมอ (📄1, 📄2, ...)
    if (data.file) {
      const link = document.createElement('span');
      link.className = 'flow-step-link';
      link.textContent = ' 📄' + (setNum || 1);
      link.style.cursor = 'pointer';
      link.style.color = '#7c8aff';
      link.style.marginLeft = '4px';
      // เก็บรายการไฟล์ทั้งหมดของ flow step นี้
      if (!stepEl._resultFiles) stepEl._resultFiles = [];
      stepEl._resultFiles.push(data.file);
      const fileIdx = stepEl._resultFiles.length - 1;
      link.onclick = () => viewResult(data.file, stepEl._resultFiles, fileIdx);
      stepEl.appendChild(link);
    }
    if (totalSets > 1 && setNum && !isLastSet) {
      // ยังมีชุดถัดไป — โชว์ progress
      stepEl.className = 'flow-step running' + (plan.steps[stepIdx].isAuto ? ' auto' : '');
      if (statusEl) statusEl.textContent = setNum + '/' + totalSets;
    } else {
      // ชุดสุดท้าย หรือ ชุดเดียว → mark done
      stepEl.className = 'flow-step done' + (plan.steps[stepIdx].isAuto ? ' auto' : '');
      if (statusEl) statusEl.textContent = totalSets > 1 ? '✓ ' + totalSets + ' โพสต์' : '✓';
      // โชว์ media action box สำหรับ content_creator — เก็บไฟล์ทั้งหมด
      if (agentKey === 'content_creator') {
        const mediaAction = document.getElementById('flow-' + planIdx + '-' + stepIdx + '-media-action');
        if (mediaAction) {
          mediaAction.classList.add('visible');
          mediaAction.dataset.file = data.file || '';
          // เก็บรายการไฟล์ทั้งหมดสำหรับ media gen
          if (stepEl._resultFiles && stepEl._resultFiles.length > 1) {
            mediaAction.dataset.allFiles = JSON.stringify(stepEl._resultFiles);
          }
        }
      }
    }
  } else if (data.type === 'error') {
    stepEl.className = 'flow-step error' + (plan.steps[stepIdx].isAuto ? ' auto' : '');
    if (statusEl) statusEl.textContent = '✗';
  } else if (data.type === 'status') {
    if (statusEl) statusEl.textContent = data.text;
  }
}

// --- Media generation (image + video) ---

let mediaConfig = { auto_generate_image: false, auto_generate_video: false };

// loadMediaConfigToChips ถูกเรียกใน renderAgentBoxes() แล้ว — ไม่ต้องเรียกซ้ำ

async function toggleAutoMedia(field, value) {
  mediaConfig[field] = value;
  await fetch('/api/media_config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [field]: value }),
  });
}

function closeMediaViewer(event) {
  if (event && event.target && !event.target.classList.contains('media-viewer-overlay') && event.type === 'click') return;
  const el = document.getElementById('media-viewer-container');
  if (el) el.remove();
}

function handleAgentSSE(data, agentKey) {
  const box = document.getElementById('box-' + agentKey);
  const status = document.getElementById('status-' + agentKey);
  const output = document.getElementById('output-' + agentKey);
  const fileLink = document.getElementById('file-' + agentKey);

  if (data.type === 'agent_start') {
    status.innerHTML = '<span class="typing">●</span> ' + escapeHtml(data.text);
  } else if (data.type === 'stream') {
    output.textContent = data.text;
  } else if (data.type === 'agent_done') {
    status.className = 'agent-status done';
    const totalSets = data.total_sets || 1;
    status.textContent = totalSets > 1 ? 'เสร็จเรียบร้อย ✓ (' + totalSets + ' โพสต์)' : 'เสร็จเรียบร้อย ✓';
    box.className = 'agent-box done';
    output.textContent = data.text + '\n\n... (ดูผลลัพธ์เต็มที่แท็บผลลัพธ์)';
    if (data.file) {
      // เก็บรายการไฟล์ทั้งหมด (multi-set)
      if (!fileLink._resultFiles) fileLink._resultFiles = [];
      fileLink._resultFiles.push(data.file);
      const fileIdx = fileLink._resultFiles.length - 1;
      fileLink.className = 'agent-file-link visible';
      const label = totalSets > 1 ? '📄 โพสต์ ' + (data.set_num || fileIdx + 1) : '📄 ' + data.file.split('/').pop();
      fileLink.textContent = label;
      fileLink.onclick = () => viewResult(data.file, fileLink._resultFiles, fileIdx);
    }
  } else if (data.type === 'error') {
    status.className = 'agent-status error';
    status.textContent = data.text;
    box.className = 'agent-box error';
  } else if (data.type === 'status') {
    status.textContent = data.text;
  }
}

function stopAgent(agentKey) {
  const stopBtn = document.getElementById('stop-' + agentKey);
  if (stopBtn) stopBtn.disabled = true;
  if (abortController) {
    abortController.abort();
    abortController = null;
  }
  fetch('/api/cancel', { method: 'POST' });
  delete runningAgents[agentKey];
  const box = document.getElementById('box-' + agentKey);
  const status = document.getElementById('status-' + agentKey);
  if (box) box.className = 'agent-box';
  if (status) {
    status.className = 'agent-status';
    status.textContent = 'หยุดการทำงาน';
  }
  const confirmBtn = document.getElementById('confirm-' + agentKey);
  if (confirmBtn) confirmBtn.style.display = 'block';
  if (stopBtn) stopBtn.style.display = 'none';
}

function viewResult(filepath, navFiles, navIdx) {
  // navFiles: รายการไฟล์ทั้งหมดในการสร้างครั้งนั้น (optional)
  // navIdx: index ปัจจุบัน (optional)
  if (navFiles && navFiles.length) {
    _resultNavFiles = navFiles;
    _resultNavIdx = navIdx || 0;
  } else {
    _resultNavFiles = [filepath];
    _resultNavIdx = 0;
  }
  _viewResultInternal(filepath);
}

function _viewResultInternal(filepath) {
  const parts = filepath.split('/');
  const session = parts[parts.length - 2] || parts[0];
  const filename = parts[parts.length - 1];
  const lower = filename.toLowerCase();
  const isImage = lower.endsWith('.png') || lower.endsWith('.jpg') || lower.endsWith('.jpeg') || lower.endsWith('.gif') || lower.endsWith('.webp');
  const isVideo = lower.endsWith('.mp4') || lower.endsWith('.webm') || lower.endsWith('.mov');
  const overlay = document.getElementById('result-overlay');
  const titleEl = document.getElementById('result-modal-title');
  const bodyEl = document.getElementById('result-modal-body');
  titleEl.textContent = filename;
  // แสดง/ซ่อน navigation bar
  const navBar = document.getElementById('result-nav-bar');
  const navInfo = document.getElementById('result-nav-info');
  const navPrev = document.getElementById('result-nav-prev');
  const navNext = document.getElementById('result-nav-next');
  if (_resultNavFiles.length > 1) {
    navBar.style.display = 'flex';
    navInfo.textContent = 'โพสต์ ' + (_resultNavIdx + 1) + '/' + _resultNavFiles.length;
    navPrev.disabled = _resultNavIdx === 0;
    navPrev.style.opacity = _resultNavIdx === 0 ? '0.4' : '1';
    navNext.disabled = _resultNavIdx === _resultNavFiles.length - 1;
    navNext.style.opacity = _resultNavIdx === _resultNavFiles.length - 1 ? '0.4' : '1';
  } else {
    navBar.style.display = 'none';
  }
  if (isImage || isVideo) {
    let html = '<div class="file-info-display" style="margin-bottom:12px">Session: ' + escapeHtml(session) + '</div>';
    html += '<div style="text-align:center">';
    const url = '/api/file/' + encodeURIComponent(session) + '/' + encodeURIComponent(filename);
    if (isImage) {
      html += '<img src="' + url + '" alt="" style="max-width:100%;border-radius:8px">';
    } else {
      html += '<video src="' + url + '" controls style="max-width:100%;border-radius:8px"></video>';
    }
    html += '</div>';
    bodyEl.innerHTML = html;
    overlay.classList.add('visible');
    loadModalCost(session, filename);
    return;
  }
  fetch('/api/file/' + encodeURIComponent(session) + '/' + encodeURIComponent(filename)).then(r => r.json()).then(data => {
    if (isContentCreatorFile(filename)) {
      // ถ้าเป็น .md → ลองโหลด .json ที่ชื่อเดียวกันก่อน (structured output มีข้อมูลครบกว่า)
      if (filename.toLowerCase().endsWith('.md')) {
        const jsonFilename = filename.replace(/\.md$/i, '.json');
        fetch('/api/file/' + encodeURIComponent(session) + '/' + encodeURIComponent(jsonFilename))
          .then(r => r.ok ? r.json() : null)
          .then(jsonData => {
            if (jsonData && jsonData.content) {
              bodyEl.innerHTML = renderContentResult(session, jsonFilename, jsonData.content);
            } else {
              bodyEl.innerHTML = renderContentResult(session, filename, data.content);
            }
            overlay.classList.add('visible');
            loadModalCost(session, filename);
          })
          .catch(() => {
            bodyEl.innerHTML = renderContentResult(session, filename, data.content);
            overlay.classList.add('visible');
            loadModalCost(session, filename);
          });
      } else {
        bodyEl.innerHTML = renderContentResult(session, filename, data.content);
        overlay.classList.add('visible');
        loadModalCost(session, filename);
      }
    } else {
      let html = '<div class="file-info-display" style="margin-bottom:12px">Session: ' + escapeHtml(session) + '</div>';
      html += '<div class="content-box">' + renderMarkdown(data.content) + '</div>';
      bodyEl.innerHTML = html;
      overlay.classList.add('visible');
      loadModalCost(session, filename);
    }
  });
}

function navResult(delta) {
  const newIdx = _resultNavIdx + delta;
  if (newIdx < 0 || newIdx >= _resultNavFiles.length) return;
  _resultNavIdx = newIdx;
  _viewResultInternal(_resultNavFiles[_resultNavIdx]);
}

function closeResultOverlay(event) {
  if (event && event.target && !event.target.classList.contains('result-overlay') && event.type === 'click') return;
  document.getElementById('result-overlay').classList.remove('visible');
}

// โหลด cost summary ของ flow ที่ไฟล์สังกัด แล้วแสดงใน output modal
// วางใต้ "Session: ..." ใน .file-info-display
function loadModalCost(session, filename) {
  const bodyEl = document.getElementById('result-modal-body');
  if (!bodyEl) return;
  const infoEl = bodyEl.querySelector('.file-info-display');
  if (!infoEl) return;
  // ล้าง cost info เดิม (ถ้ามี) — กันซ้ำตอนเปลี่ยนไฟล์ใน nav
  const oldCost = infoEl.querySelector('.modal-cost-info');
  if (oldCost) oldCost.remove();
  fetch('/api/cost_summary/' + encodeURIComponent(session) + '?file=' + encodeURIComponent(filename))
    .then(r => r.json())
    .then(data => {
      if (data.status === 'none' || data.status === 'error') return;
      const cost = data.total_cost_usd || 0;
      const calls = data.total_calls || 0;
      const tokensIn = data.prompt_tokens || 0;
      const tokensOut = data.completion_tokens || 0;
      // สร้าง breakdown สั้น — top 3 sources
      const sources = data.by_source || {};
      const topSrc = Object.entries(sources).slice(0, 3)
        .filter(([,v]) => v > 0)
        .map(([k,v]) => escapeHtml(k.split('.').pop()) + ': $' + Number(v).toFixed(4))
        .join(' · ');
      let html = '<div class="modal-cost-info">';
      html += '<span style="color:#4ade80">💰 ค่าใช้จ่าย: $' + cost.toFixed(4) + '</span>';
      html += ' <span style="color:#888;font-size:11px">(' + calls + ' calls · ' + tokensIn + '→' + tokensOut + ' tokens)</span>';
      if (topSrc) html += '<div style="font-size:11px;color:#666;margin-top:2px">' + topSrc + '</div>';
      html += '</div>';
      infoEl.insertAdjacentHTML('beforeend', html);
    })
    .catch(() => {});
}

function deleteCurrentResult() {
  if (_resultNavFiles.length === 0) return;
  const filepath = _resultNavFiles[_resultNavIdx];
  if (!filepath) return;
  if (!confirm('ลบไฟล์นี้และประวัติที่เกี่ยวข้อง?\n' + filepath.split('/').pop())) return;
  fetch('/api/output_file', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file: filepath }),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      // ลบไฟล์นี้ออกจาก nav list
      _resultNavFiles.splice(_resultNavIdx, 1);
      if (_resultNavFiles.length === 0) {
        closeResultOverlay();
      } else {
        if (_resultNavIdx >= _resultNavFiles.length) _resultNavIdx = _resultNavFiles.length - 1;
        _viewResultInternal(_resultNavFiles[_resultNavIdx]);
        // อัปเดต nav bar
        const navBar = document.getElementById('result-nav-bar');
        const navInfo = document.getElementById('result-nav-info');
        if (_resultNavFiles.length > 1) {
          navBar.style.display = 'flex';
          navInfo.textContent = 'โพสต์ ' + (_resultNavIdx + 1) + '/' + _resultNavFiles.length;
        } else {
          navBar.style.display = 'none';
        }
      }
      loadSessions();
      alert('ลบเรียบร้อย — ไฟล์ ' + (data.deleted_files || 0) + ' ไฟล์, ประวัติ ' + (data.removed_history_entries || 0) + ' entries');
    } else {
      alert('เกิดข้อผิดพลาด: ' + (data.error || 'ไม่ทราบ'));
    }
  }).catch(e => alert('เกิดข้อผิดพลาด: ' + e));
}

function backToAgents() {
  closeResultOverlay();
}

function loadSessions() {
  fetch('/api/sessions').then(r => r.json()).then(sessions => {
    const el = document.getElementById('sidebar-content');
    if (!sessions.length) {
      el.innerHTML = '<div style="color:#555;font-size:12px;padding:12px">ยังไม่มี session</div>';
      return;
    }
    let html = '';
    for (const s of sessions) {
      html += '<div class="session-item" onclick="expandSession(\'' + s.name + '\',this)">' + s.name + '</div>';
      html += '<div id="files-' + s.name + '" style="display:none">';
      for (const f of s.files) {
        html += '<div class="sub-file-item" onclick="loadSessionFile(\'' + s.name + '\',\'' + f.name + '\',this)">📄 ' + f.name + '<br><span style="color:#555">' + f.size + ' B · ' + f.modified + '</span></div>';
      }
      html += '</div>';
    }
    el.innerHTML = html;
  });
}

function expandSession(name, el) {
  const filesEl = document.getElementById('files-' + name);
  const isOpen = filesEl.style.display !== 'none';
  document.querySelectorAll('[id^="files-"]').forEach(e => e.style.display = 'none');
  document.querySelectorAll('.session-item').forEach(e => e.classList.remove('active'));
  if (!isOpen) {
    filesEl.style.display = 'block';
    el.classList.add('active');
  }
}

function loadSessionFile(session, filename, el) {
  document.querySelectorAll('.sub-file-item').forEach(e => e.classList.remove('active'));
  el.classList.add('active');
  // รวมรายการไฟล์ทั้งหมดใน session นี้ (เฉพาะ content_creator .md) สำหรับ navigation
  const filesEl = document.getElementById('files-' + session);
  const allFiles = [];
  if (filesEl) {
    filesEl.querySelectorAll('.sub-file-item').forEach(item => {
      const onclickAttr = item.getAttribute('onclick') || '';
      const m = onclickAttr.match(/loadSessionFile\('([^']+)','([^']+)'/);
      if (m && m[2].endsWith('.md')) {
        allFiles.push(m[1] + '/' + m[2]);
      }
    });
  }
  const filepath = session + '/' + filename;
  const navIdx = allFiles.indexOf(filepath);
  viewResult(filepath, allFiles.length > 1 ? allFiles : null, navIdx >= 0 ? navIdx : 0);
}

function renderMarkdown(text) {
  if (!text) return '';
  let html = escapeHtml(text);
  // Links [text](url) — ต้องทำก่อน escape อื่นๆ เพราะ escapeHtml ทำแล้ว
  // แต่เนื่องจาก escapeHtml ทำแล้ว ต้องแก้ &amp; กลับใน URL
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function(m, label, url) {
    return '<a href="' + url.replace(/&amp;/g, '&') + '" target="_blank" rel="noopener">' + label + '</a>';
  });
  // Headers
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  // Bold
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Italic
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // Lists
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>\n?)+/g, function(m) { return '<ul>' + m + '</ul>'; });
  // Numbered lists
  html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');
  // Horizontal rule
  html = html.replace(/^---$/gm, '<hr>');
  // Tables — parse blocks of | separated rows
  html = html.replace(/((?:^\|[^\n]+\|\n?)+)/gm, function(tableBlock) {
    const rows = tableBlock.trim().split('\n');
    if (rows.length < 2) return tableBlock;
    // Check if second row is separator (|:---|:---|)
    if (!rows[1].match(/^\|[\s:|\-]+$/)) return tableBlock;
    let result = '<table>';
    // Header
    const headers = rows[0].split('|').slice(1, -1).map(c => c.trim());
    result += '<thead><tr>';
    for (const h of headers) result += '<th>' + h + '</th>';
    result += '</tr></thead><tbody>';
    // Body (skip separator row)
    for (let i = 2; i < rows.length; i++) {
      const cells = rows[i].split('|').slice(1, -1).map(c => c.trim());
      result += '<tr>';
      for (const c of cells) result += '<td>' + c + '</td>';
      result += '</tr>';
    }
    result += '</tbody></table>';
    return result;
  });
  // Paragraphs (split by double newline, skip if already wrapped)
  html = html.replace(/\n\n/g, '</p><p>');
  html = '<p>' + html + '</p>';
  // Clean up empty paragraphs around block elements
  html = html.replace(/<p>\s*(<h[1-3]>)/g, '$1');
  html = html.replace(/(<\/h[1-3]>)\s*<\/p>/g, '$1');
  html = html.replace(/<p>\s*(<ul>)/g, '$1');
  html = html.replace(/(<\/ul>)\s*<\/p>/g, '$1');
  html = html.replace(/<p>\s*(<hr>)/g, '$1');
  html = html.replace(/(<hr>)\s*<\/p>/g, '$1');
  html = html.replace(/<p>\s*(<table>)/g, '$1');
  html = html.replace(/(<\/table>)\s*<\/p>/g, '$1');
  html = html.replace(/<p>\s*<\/p>/g, '');
  return html;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ===== Platform Preview =====
// ตรวจว่าไฟล์เป็น content_creator output หรือไม่
// รองรับทั้ง .md (เดิม) และ .json (structured output ใหม่)
function isContentCreatorFile(filename) {
  const lower = filename.toLowerCase();
  return /content_creator/i.test(filename) && (lower.endsWith('.md') || lower.endsWith('.json'));
}

// Generate markdown จาก posts (JS version — เหมือน render_posts_to_markdown ใน Python)
// ใช้ตอน JSON ไม่มี markdown field (schema ใหม่ — LLM ส่งแค่ posts)
function renderPostsToMarkdownJS(parsed) {
  const posts = parsed.posts || [];
  if (!posts.length) return '';
  const parts = [];
  posts.forEach((post, idx) => {
    const n = idx + 1;
    parts.push('## ' + n + '. ข้อมูลโพสต์');
    parts.push('- **แพลตฟอร์ม** — ' + (post.platform || ''));
    parts.push('- **มุมมอง** — ' + (post.concept || ''));
    parts.push('- **หัวข้อ** — ' + (post.title || ''));
    parts.push('- **Caption (พร้อมโพสต์)** — ');
    parts.push(post.caption || post.content || '');
    parts.push('- **Hashtag** — ' + (post.hashtags || ''));
    parts.push('');
    if (post.script) {
      parts.push('## ' + n + '. Script สำหรับวิดีโอ');
      parts.push(post.script);
      parts.push('');
    }
    const ips = post.image_prompts || [];
    if (ips.length) {
      parts.push('## ' + n + '. Prompt สำหรับ Gen Image');
      ips.forEach((ip, i) => {
        parts.push('- **' + (i+1) + ' ภาพ**');
        parts.push('- **Prompt:** ' + (ip.prompt || ''));
        if (ip.aspect_ratio) parts.push('- **Aspect Ratio:** ' + ip.aspect_ratio);
        if (ip.resolution) parts.push('- **Resolution:** ' + ip.resolution);
      });
      parts.push('');
    }
    const vps = post.video_prompts || [];
    if (vps.length) {
      parts.push('## ' + n + '. Prompt สำหรับ Gen Video');
      vps.forEach((vp, i) => {
        parts.push('- **' + (i+1) + ' วิดีโอ**');
        parts.push('- **Prompt:** ' + (vp.prompt || ''));
        if (vp.duration) parts.push('- **Duration:** ' + vp.duration + ' วินาที');
        if (vp.aspect_ratio) parts.push('- **Aspect Ratio:** ' + vp.aspect_ratio);
        if (vp.resolution) parts.push('- **Resolution:** ' + vp.resolution);
      });
      parts.push('');
    }
  });
  return parts.join('\n');
}

// แยก fields จาก content_creator output
// รองรับ 2 รูปแบบ:
// 1. JSON (structured output): ถ้า text เป็น JSON ที่มี field "posts" → อ่านจาก structure
//    - markdown ไม่ได้ส่งจาก LLM แล้ว เรา generate เองจาก posts (renderPostsToMarkdownJS)
//    - รองรับ JSON เก่าที่มี markdown field อยู่ (backward compatible)
// 2. Markdown (เดิม): ใช้ regex parser ตามรูปแบบใน agents.yaml
// แปลง JSON ที่มีหลาย posts → คืน array ของ post objects
function parseContentPosts(text) {
  try {
    const parsed = JSON.parse(text);
    if (parsed && parsed.posts && parsed.posts.length > 0) {
      const posts = parsed.posts.map(p => ({
        platform: p.platform || '',
        concept: p.concept || p.angle || '',
        title: p.title || '',
        caption: p.caption || p.content || '',
        script: p.script || '',
        content: p.caption || p.content || '',
        hashtags: p.hashtags || '',
        imagePrompt: (p.image_prompts && p.image_prompts.length > 0) ? p.image_prompts[0].prompt : '',
        videoPrompt: (p.video_prompts && p.video_prompts.length > 0) ? p.video_prompts[0].prompt : '',
        scriptReview: p.script_review || null,
        raw: parsed.markdown || renderPostsToMarkdownJS(parsed),
      }));
      return posts;
    }
  } catch (e) {}
  // fallback: ใช้ regex parser แบบเดิม → คืน array 1 ตัว
  return [parseContentPost(text)];
}

function parseContentPost(text) {
  // --- Path 1: JSON (structured output) ---
  try {
    const parsed = JSON.parse(text);
    if (parsed && parsed.posts && parsed.posts.length > 0) {
      const p = parsed.posts[0];
      const post = {
        platform: p.platform || '',
        concept: p.concept || p.angle || '',
        title: p.title || '',
        // schema ใหม่: caption + script แยก / schema เก่า: content
        caption: p.caption || p.content || '',
        script: p.script || '',
        content: p.caption || p.content || '',  // backward compat
        hashtags: p.hashtags || '',
        imagePrompt: (p.image_prompts && p.image_prompts.length > 0) ? p.image_prompts[0].prompt : '',
        videoPrompt: (p.video_prompts && p.video_prompts.length > 0) ? p.video_prompts[0].prompt : '',
        // script review state (ถ้าเคยตรวจแล้ว)
        scriptReview: p.script_review || null,
        // ถ้ามี markdown field (JSON เก่า) → ใช้ของเดิม, ถ้าไม่มี → generate จาก posts
        raw: parsed.markdown || renderPostsToMarkdownJS(parsed),
      };
      return post;
    }
  } catch (e) {
    // ไม่ใช่ JSON → ใช้ regex parser
  }

  // --- Path 2: Markdown (regex parser) ---
  const post = { platform: '', concept: '', title: '', content: '', hashtags: '', imagePrompt: '', videoPrompt: '', raw: text };
  // แพลตฟอร์ม — รองรับช่องว่างก่อนเครื่องหมาย: "**แพลตฟอร์ม** — Facebook"
  let m = text.match(/\*\*แพลตฟอร์ม\*\*\s*[—\-:]\s*(.+)/i);
  if (m) post.platform = m[1].trim();
  // มุมมอง
  m = text.match(/\*\*มุมมอง\*\*\s*[—\-:]\s*(.+)/i);
  if (m) post.concept = m[1].trim();
  // หัวข้อ
  m = text.match(/\*\*หัวข้อ\*\*\s*[—\-:]\s*(.+)/i);
  if (m) post.title = m[1].trim();
  // เนื้อหา — จับจากหลัง **เนื้อหา** จนถึง **Hashtag** (consume ช่องว่าง + em-dash ก่อน content)
  m = text.match(/\*\*เนื้อหา\*\*[ \t]*[—\-:]?[ \t]*\n([\s\S]*?)(?=\n[-*]\s*\*\*Hashtag\*\*)/i);
  if (m) post.content = m[1].trim();
  // Hashtag — รองรับทั้ง "บรรทัดเดียวกัน" และ "ขึ้นบรรทัดใหม่"
  // กรณี 1: **Hashtag** — #tag1 #tag2 (บรรทัดเดียวกัน)
  m = text.match(/\*\*Hashtag\*\*[ \t]*[—\-:]?[ \t]*([^\n]+)/i);
  if (m) {
    post.hashtags = m[1].trim();
  } else {
    // กรณี 2: **Hashtag** —\n#tag1 #tag2 (ขึ้นบรรทัดใหม่)
    m = text.match(/\*\*Hashtag\*\*[ \t]*[—\-:]?[ \t]*\n([\s\S]*?)(?=\n##|\n[-*]\s*\*\*Prompt|\n$|$)/i);
    if (m) post.hashtags = m[1].trim();
  }
  // Image prompt — ตรวจว่ามี section Gen Image อยู่ไหม (ไม่จำเป็นต้องมี **Prompt:**)
  const imageSection = text.match(/##\s*\d+\.[^\n]*Gen Image[\s\S]*?(?=\n##\s|$)/i);
  if (imageSection) {
    // ลองหา **Prompt:** ก่อน
    m = imageSection[0].match(/\*\*Prompt:\*\*\s*(.+)/i);
    if (m) {
      post.imagePrompt = m[1].trim();
    } else {
      // fallback: รวม field values แบบเดียวกับ parser ใน Python
      post.imagePrompt = extractFieldPrompt(imageSection[0]);
    }
  }
  // Video prompt
  const videoSection = text.match(/##\s*\d+\.[^\n]*Gen Video[\s\S]*?(?=\n##\s|$)/i);
  if (videoSection) {
    m = videoSection[0].match(/\*\*Prompt:\*\*\s*(.+)/i);
    if (m) {
      post.videoPrompt = m[1].trim();
    } else {
      post.videoPrompt = extractFieldPrompt(videoSection[0]);
    }
  }
  return post;
}

// รวม field values จาก section ที่ใช้ field names (scene description, camera movement, ...)
function extractFieldPrompt(section) {
  const SKIP_FIELDS = ['usage', 'duration', 'aspect ratio', 'aspect_ratio', 'resolution', 'avoid'];
  const lines = section.split('\n');
  const parts = [];
  for (const line of lines) {
    const s = line.trim();
    if (!s) continue;
    // ข้าม heading และ label ที่ไม่ใช่ field value (เช่น "- **1 ภาพ**", "## 2. ...")
    if (s.startsWith('##') || /^-\s*\*\*\d+/.test(s)) continue;
    // รองรับ 3 รูปแบบ:
    // 1) - **field** — value  (มี bold + มี -)
    // 2) - field: value       (ไม่มี bold + มี -)
    // 3) field: value          (ไม่มี bold + ไม่มี -)
    let fm = s.match(/^-\s*\*\*([^*]+)\*\*\s*[:\-—]\s*(.+)$/);
    if (!fm) fm = s.match(/^-\s*([^:*]+?):\s*(.+)$/);
    if (!fm) fm = s.match(/^([^:*]+?):\s*(.+)$/);
    if (fm) {
      const fieldName = fm[1].trim().toLowerCase();
      const val = fm[2].trim();
      // ข้าม field ที่ไม่ใช่ prompt text
      if (SKIP_FIELDS.some(f => fieldName.includes(f))) continue;
      if (val && val.length > 10) parts.push(val);
    }
  }
  return parts.join('. ');
}

// หาไฟล์ media ใน session เดียวกัน — คืน {images, videos}
function findSessionMedia(session, callback) {
  fetch('/api/session_files/' + encodeURIComponent(session)).then(r => r.json()).then(files => {
    const images = files.filter(f => /\.(png|jpg|jpeg|webp|gif)$/i.test(f.name)).map(f => f.name);
    const videos = files.filter(f => /\.(mp4|webm|mov)$/i.test(f.name)).map(f => f.name);
    callback({images, videos});
  }).catch(() => callback({images: [], videos: []}));
}

// legacy wrapper
function findSessionImages(session, callback) {
  findSessionMedia(session, function(media) { callback(media.images); });
}

// แปลง markdown inline (bold/italic) เป็น HTML โดย escape ก่อน
function formatInlineMarkdown(text) {
  let html = escapeHtml(text);
  // bold: **text**
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // italic: *text* หรือ *(text)* — ระวังไม่ให้ทับ bold
  html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  return html;
}

// จัดรูปแบบสคริปต์ TikTok — แยก Hook/Body/CTA และทำให้อ่านง่าย
function formatTikTokScript(text) {
  if (!text) return '';
  let html = formatInlineMarkdown(text);
  // แปลงบรรทัดว่างเป็น spacer
  html = html.replace(/\n\n+/g, '\n\n');
  // ทำให้แต่ละบรรทัดขึ้นบรรทัดใหม่
  return html;
}

// สลับขยาย/ย่อ caption ของ TikTok card — เหมือน TikTok จริง
function toggleTkCaption(captionId, toggleEl) {
  const el = document.getElementById(captionId);
  if (!el) return;
  if (el.classList.contains('collapsed')) {
    el.classList.remove('collapsed');
    toggleEl.textContent = 'ย่อ';
  } else {
    el.classList.add('collapsed');
    toggleEl.textContent = 'เพิ่มเติม';
  }
}

// สร้าง platform preview HTML
function renderPlatformPreview(post, session, images, videos) {
  videos = videos || [];
  const platform = (post.platform || '').toLowerCase();

  if (platform.includes('tiktok')) {
    return renderTikTokCard(post, session, images, videos);
  } else if (platform.includes('instagram') || platform.includes('ig')) {
    return renderInstagramCard(post, session, images);
  }
  // default: Facebook
  return renderFacebookCard(post, session, images);
}

function renderFacebookCard(post, session, images) {
  let html = '<div class="fb-card">';
  html += '<div class="fb-header">';
  html += '<div class="fb-avatar">B</div>';
  html += '<div><div class="fb-name">แบรนด์ของคุณ</div><div class="fb-sub">เมื่อกี้ · 🌐</div></div>';
  html += '</div>';
  html += '<div class="fb-body">';
  if (post.title) html += '<div class="fb-title">' + escapeHtml(post.title) + '</div>';
  // caption + hashtag ต่อกันเป็นโพสต์เดียว เหมือน Facebook จริง
  let bodyText = post.caption || post.content || '';
  if (post.hashtags) bodyText += '\n' + post.hashtags;
  if (bodyText) html += escapeHtml(bodyText);
  html += '</div>';
  if (images.length === 1) {
    const url = '/api/file/' + encodeURIComponent(session) + '/' + encodeURIComponent(images[0]);
    html += '<div class="fb-media"><img src="' + url + '" alt=""></div>';
  } else if (images.length > 1) {
    html += '<div class="fb-media fb-media-grid">';
    for (const img of images) {
      const url = '/api/file/' + encodeURIComponent(session) + '/' + encodeURIComponent(img);
      html += '<img src="' + url + '" alt="">';
    }
    html += '</div>';
  }
  html += '<div class="fb-actions"><span>👍 ถูกใจ</span><span>💬 แสดงความคิดเห็น</span><span>↗ แชร์</span></div>';
  html += '</div>';
  return html;
}

function renderTikTokCard(post, session, images, videos) {
  videos = videos || [];
  const vidUrl = videos.length ? '/api/file/' + encodeURIComponent(session) + '/' + encodeURIComponent(videos[0]) : '';
  const imgUrl = images.length ? '/api/file/' + encodeURIComponent(session) + '/' + encodeURIComponent(images[0]) : '';
  let html = '<div class="tk-card">';
  html += '<div class="tk-top">สำหรับคุณ</div>';
  html += '<div class="tk-media">';
  if (vidUrl) {
    html += '<video src="' + vidUrl + '" controls autoplay muted loop playsinline style="width:100%;height:100%;object-fit:contain;background:#000"></video>';
  } else if (imgUrl) {
    html += '<img src="' + imgUrl + '" alt="">';
  } else {
    html += '<div class="tk-placeholder">🎬 วิดีโอตัวอย่าง</div>';
  }
  html += '</div>';
  html += '<div class="tk-overlay"></div>';
  html += '<div class="tk-content">';
  html += '<div class="tk-user">แบรนด์ของคุณ</div>';
  // title — TikTok จริงแสดงหัวข้อได้ (text overlay บนวิดีโอ/ภาพ)
  if (post.title) html += '<div class="tk-title">' + escapeHtml(post.title) + '</div>';
  // caption — เหมือน TikTok จริง: แสดงบางส่วน + "เพิ่มเติม" ถ้ายาว
  // ใช้ caption (พร้อมโพสต์) ไม่ใช่ content (สคริปต์)
  let tkCaption = post.caption || post.content || '';
  if (post.hashtags) tkCaption += '\n' + post.hashtags;
  if (tkCaption) {
    const captionId = 'tk-cap-' + Math.random().toString(36).slice(2, 9);
    html += '<div class="tk-caption collapsed" id="' + captionId + '">' + escapeHtml(tkCaption) + '</div>';
    // แสดงปุ่ม "เพิ่มเติม" / "ย่อ" — คลิกได้เพราะ pointer-events: auto
    html += '<div class="tk-caption-toggle" onclick="toggleTkCaption(\'' + captionId + '\', this)">เพิ่มเติม</div>';
  }
  html += '</div>';
  html += '<div class="tk-side">';
  html += '<div><div class="tk-icon">❤️</div><div class="tk-count">12.5K</div></div>';
  html += '<div><div class="tk-icon">💬</div><div class="tk-count">890</div></div>';
  html += '<div><div class="tk-icon">↗</div><div class="tk-count">3.2K</div></div>';
  html += '</div>';
  html += '</div>';
  return html;
}

function renderInstagramCard(post, session, images) {
  let html = '<div class="ig-card">';
  html += '<div class="ig-header">';
  html += '<div class="ig-avatar">B</div>';
  html += '<div class="ig-name">แบรนด์ของคุณ</div>';
  if (images.length > 1) html += '<div style="margin-left:auto;color:#999;font-size:12px">' + images.length + '/' + images.length + '</div>';
  html += '</div>';
  html += '<div class="ig-media">';
  if (images.length === 1) {
    const url = '/api/file/' + encodeURIComponent(session) + '/' + encodeURIComponent(images[0]);
    html += '<img src="' + url + '" alt="">';
  } else if (images.length > 1) {
    for (const img of images) {
      const url = '/api/file/' + encodeURIComponent(session) + '/' + encodeURIComponent(img);
      html += '<img src="' + url + '" alt="" style="width:100%;height:100%;object-fit:cover;display:none">';
    }
    html += '<img src="' + '/api/file/' + encodeURIComponent(session) + '/' + encodeURIComponent(images[0]) + '" alt="" style="width:100%;height:100%;object-fit:cover">';
  } else {
    html += '<div class="ig-placeholder">📷 รูปภาพตัวอย่าง</div>';
  }
  html += '</div>';
  html += '<div class="ig-actions"><span>♡</span><span>💬</span><span>↗</span></div>';
  html += '<div class="ig-likes">1,234 ถูกใจ</div>';
  html += '<div class="ig-caption"><b>แบรนด์ของคุณ</b> ';
  if (post.caption) html += escapeHtml(post.caption);
  else if (post.content) html += escapeHtml(post.content);
  html += '</div>';
  if (post.hashtags) {
    html += '<div class="ig-hashtags">' + escapeHtml(post.hashtags) + '</div>';
  }
  html += '</div>';
  return html;
}

// สลับระหว่าง platform preview กับต้นฉบับ
function togglePreviewView(mode) {
  const platformEl = document.getElementById('preview-platform-view');
  const originalEl = document.getElementById('preview-original-view');
  document.querySelectorAll('.preview-toggle button').forEach(b => b.classList.remove('active'));
  const btn = document.querySelector('.preview-toggle button[data-mode="' + mode + '"]');
  if (btn) btn.classList.add('active');
  if (mode === 'platform') {
    if (platformEl) platformEl.classList.add('visible');
    if (originalEl) originalEl.classList.remove('visible');
  } else {
    if (platformEl) platformEl.classList.remove('visible');
    if (originalEl) originalEl.classList.add('visible');
  }
}

// สลับดู post ต่างๆ ในไฟล์เดียวกัน (เช่น Facebook + TikTok)
function switchPostView(idx) {
  if (!_currentMediaPosts || idx < 0 || idx >= _currentMediaPosts.length) return;
  _currentMediaPostIdx = idx;
  const post = _currentMediaPosts[idx];
  _currentMediaPost = post;
  // อัปเดต tab active
  document.querySelectorAll('.post-selector-tab').forEach(t => t.classList.remove('active'));
  const tab = document.querySelector('.post-selector-tab[data-post-idx="' + idx + '"]');
  if (tab) tab.classList.add('active');
  // อัปเดต meta tags
  const metaEl = document.getElementById('preview-meta-tags');
  if (metaEl) {
    let mh = '';
    if (post.platform) mh += '<span class="meta-tag">แพลตฟอร์ม: <b>' + escapeHtml(post.platform) + '</b></span>';
    if (post.concept) mh += '<span class="meta-tag">มุมมอง: <b>' + escapeHtml(post.concept) + '</b></span>';
    metaEl.innerHTML = mh;
  }
  // อัปเดต platform preview
  findSessionMedia(_currentMediaSession, function(media) {
    const el = document.getElementById('preview-platform-content');
    if (el) el.innerHTML = renderPlatformPreview(post, _currentMediaSession, media.images, media.videos);
    fetch('/api/media_status/' + encodeURIComponent(_currentMediaSession)).then(r => r.json()).then(st => {
      renderMediaActionBar(_currentMediaSession, _currentMediaFile, post, media, st);
    }).catch(() => renderMediaActionBar(_currentMediaSession, _currentMediaFile, post, media));
  });
}

// แสดงผลลัพธ์ content_creator พร้อม toggle platform preview / ต้นฉบับ
function renderContentResult(session, filename, content) {
  const posts = parseContentPosts(content);
  const post = posts[0];  // backward compat — ใช้ post แรกเป็น default
  // เก็บ context สำหรับใช้ตอนสร้าง media
  _currentMediaSession = session;
  _currentMediaFile = filename;
  _currentMediaContent = content;
  _currentMediaPost = post;
  _currentMediaPosts = posts;  // เก็บทั้งหมดไว้สำหรับสลับ
  _currentMediaPostIdx = 0;
  let html = '<h2>' + escapeHtml(filename) + '</h2>';
  html += '<div class="file-info-display">Session: ' + escapeHtml(session) + '</div>';
  // Toggle
  html += '<div class="preview-toggle">';
  html += '<button class="active" data-mode="platform" onclick="togglePreviewView(\'platform\')">📱 ดูแบบโพสต์</button>';
  html += '<button data-mode="original" onclick="togglePreviewView(\'original\')">📄 ดูต้นฉบับ</button>';
  html += '</div>';
  // Post selector — ถ้ามีหลาย posts (เช่น Facebook + TikTok)
  if (posts.length > 1) {
    html += '<div class="post-selector-bar">';
    for (let i = 0; i < posts.length; i++) {
      const p = posts[i];
      const plat = p.platform || ('โพสต์ ' + (i+1));
      const cls = i === 0 ? 'post-selector-tab active' : 'post-selector-tab';
      html += '<button class="' + cls + '" data-post-idx="' + i + '" onclick="switchPostView(' + i + ')">' + escapeHtml(plat) + '</button>';
    }
    html += '</div>';
  }
  // Meta tags
  html += '<div class="preview-meta" id="preview-meta-tags">';
  if (post.platform) html += '<span class="meta-tag">แพลตฟอร์ม: <b>' + escapeHtml(post.platform) + '</b></span>';
  if (post.concept) html += '<span class="meta-tag">มุมมอง: <b>' + escapeHtml(post.concept) + '</b></span>';
  html += '</div>';
  // Media actions bar — แสดงปุ่มสร้างรูป/วิดีโอ ถ้ายังไม่มี
  html += '<div class="media-action-bar" id="media-action-bar">กำลังตรวจสอบ media...</div>';
  // Platform preview (visible by default)
  html += '<div class="preview-platform visible" id="preview-platform-view"><div class="preview-container" id="preview-platform-content">กำลังโหลดตัวอย่าง...</div></div>';
  // Original (hidden by default) — แสดง markdown ไม่ใช่ JSON
  // post.raw เป็น markdown แม้ตอนโหลดจาก .json (parseContentPost แยก markdown field ออกมา)
  html += '<div class="preview-original" id="preview-original-view"><div class="content-box">' + renderMarkdown(post.raw || content) + '</div></div>';
  // Load media + status then render platform card + action bar
  findSessionMedia(session, function(media) {
    const el = document.getElementById('preview-platform-content');
    if (el) el.innerHTML = renderPlatformPreview(post, session, media.images, media.videos);
    fetch('/api/media_status/' + encodeURIComponent(session)).then(r => r.json()).then(st => {
      renderMediaActionBar(session, filename, post, media, st);
    }).catch(() => renderMediaActionBar(session, filename, post, media));
  });
  return html;
}

// แสดงปุ่มสร้างรูป/วิดีโอในหน้า output — ถ้ายังไม่มี
function renderMediaActionBar(session, filename, post, media, mediaStatus) {
  const bar = document.getElementById('media-action-bar');
  if (!bar) return;
  mediaStatus = mediaStatus || {status: 'none'};
  const hasImages = media.images.length > 0;
  const hasVideos = media.videos.length > 0;
  const hasImagePrompt = !!post.imagePrompt;
  const hasVideoPrompt = !!post.videoPrompt;
  let html = '';

  // ถ้ากำลังสร้างอยู่ — แสดง progress, ไม่ให้กดซ้ำ
  if (mediaStatus.status === 'in_progress') {
    const last = mediaStatus.last_update || 'กำลังสร้าง...';
    html = '<span class="media-status working">⏳ ' + escapeHtml(last) + '</span>';
    bar.innerHTML = html;
    // poll สถานะทุก 5 วินาที
    if (!bar._polling) {
      bar._polling = true;
      setTimeout(function() {
        bar._polling = false;
        fetch('/api/media_status/' + encodeURIComponent(session)).then(r => r.json()).then(st => {
          findSessionMedia(session, function(m) {
            const el = document.getElementById('preview-platform-content');
            if (el) el.innerHTML = renderPlatformPreview(post, session, m.images, m.videos);
            renderMediaActionBar(session, filename, post, m, st);
          });
        });
      }, 5000);
    }
    return;
  }

  // ถ้าสร้างเสร็จแล้วมี error — แสดงปุ่ม retry + error message
  if (mediaStatus.status === 'failed' || mediaStatus.status === 'completed_with_errors') {
    const errs = (mediaStatus.errors || []).join('; ');
    if (errs) html += '<span class="media-status error">⚠ ' + escapeHtml(errs.slice(0, 100)) + '</span>';
    if (!hasImages && hasImagePrompt) {
      html += '<button class="media-gen-btn" onclick="retryMediaFromOutput(\'image\')">🎨 สร้างรูปใหม่</button>';
    }
    if (!hasVideos && hasVideoPrompt) {
      html += '<button class="media-gen-btn" onclick="retryMediaFromOutput(\'video\')">🎬 สร้างวิดีโอใหม่</button>';
    }
    bar.innerHTML = html;
    return;
  }

  // สถานะปกติ — แสดงปุ่มสร้างถ้ายังไม่มี media
  if (!hasImages && hasImagePrompt) {
    html += '<button class="media-gen-btn" onclick="generateMediaFromOutput(\'image\')">🎨 สร้างรูป</button>';
  }
  if (!hasVideos && hasVideoPrompt) {
    html += '<button class="media-gen-btn" onclick="generateMediaFromOutput(\'video\')">🎬 สร้างวิดีโอ</button>';
  }
  // Script review — ระบบตรวจอัตโนมัติ แสดงแค่สถานะ (read-only)
  if (post.script && post.script.trim()) {
    window._currentReviewScript = post.script;
    window._currentReviewPlatform = post.platform || 'TikTok';
    const sr = post.scriptReview;
    if (sr) {
      const score = sr.score || 0;
      const threshold = sr.threshold || 70;
      const col = score >= threshold ? '#4caf50' : score >= 50 ? '#ff9800' : '#f44336';
      const changed = sr.script_changed;
      const iter = sr.iterations || 1;
      let label;
      if (score >= threshold && changed) {
        label = '✓ ตรวจ script: ' + score + '/100 (แก้ ' + iter + ' รอบ)';
      } else if (score >= threshold) {
        label = '✓ ตรวจ script: ' + score + '/100';
      } else {
        label = '⚠ ตรวจ script: ' + score + '/100 (ยังต่ำ ใช้ script ล่าสุด)';
      }
      html += '<span class="media-status" style="background:#2a2d3a;color:' + col + ';padding:6px 12px;border-radius:6px;font-size:12px">' + label + ' — <a onclick="showSavedReview()" style="color:' + col + ';cursor:pointer;text-decoration:underline">ดูผล</a></span>';
    } else {
      // มี script แต่ยังไม่ได้ตรวจ (pipeline ยังไม่ผ่าน review)
      html += '<span class="media-status" style="background:#2a2d3a;color:#888;padding:6px 12px;border-radius:6px;font-size:12px">⏳ ยังไม่ได้ตรวจ script</span>';
    }
  } else {
    // ไม่มี script (Facebook post ไม่มี video script) — แสดงสถานะ
    html += '<span class="media-status" style="background:#2a2d3a;color:#666;padding:6px 12px;border-radius:6px;font-size:12px">— ไม่มี video script (ไม่ต้องตรวจ)</span>';
  }
  if (!hasImagePrompt && !hasVideoPrompt && !(post.script && post.script.trim())) {
    html = '<span class="media-status none">โพสต์นี้ไม่มี prompt รูป/วิดีโอ</span>';
  }
  bar.innerHTML = html;

  // โหลดประวัติ retry แล้วแสดงใต้ action bar
  fetch('/api/media_retry_log/' + encodeURIComponent(session)).then(r => r.json()).then(data => {
    renderRetryHistory(data.entries || []);
  }).catch(() => {});
}

// แสดงประวัติการ retry ใต้ action bar
function renderRetryHistory(entries) {
  let existing = document.getElementById('media-retry-history');
  if (!existing) {
    const bar = document.getElementById('media-action-bar');
    if (!bar) return;
    const div = document.createElement('div');
    div.id = 'media-retry-history';
    div.className = 'media-retry-history';
    bar.parentNode.insertBefore(div, bar.nextSibling);
    existing = div;
  }
  if (!entries.length) {
    existing.innerHTML = '';
    return;
  }
  let html = '<div class="retry-history-title">📋 ประวัติการสร้างสื่อ (' + entries.length + ' ครั้ง)</div>';
  entries.forEach(function(e) {
    const ok = e.ok;
    const statusCls = ok ? 'success' : 'failed';
    const statusIcon = ok ? '✓' : '✗';
    let entryHtml = '<div class="retry-entry ' + statusCls + '">';
    entryHtml += '<div class="retry-entry-header">';
    entryHtml += '<span class="retry-status">' + statusIcon + '</span>';
    entryHtml += '<span class="retry-type">' + escapeHtml(e.media_type) + '</span>';
    entryHtml += '<span class="retry-filename">' + escapeHtml(e.filename) + '</span>';
    entryHtml += '<span class="retry-time">' + escapeHtml(e.timestamp) + '</span>';
    if (e.retry_count > 0) {
      entryHtml += '<span class="retry-count">แก้ prompt ' + e.retry_count + ' ครั้ง</span>';
    }
    entryHtml += '</div>';
    if (!ok && e.error) {
      entryHtml += '<div class="retry-error">⚠ ' + escapeHtml(e.error) + '</div>';
    }
    if (e.retry_history && e.retry_history.length) {
      entryHtml += '<details class="retry-details"><summary>ดู prompt ที่แก้</summary>';
      e.retry_history.forEach(function(h) {
        entryHtml += '<div class="retry-step">';
        entryHtml += '<div class="retry-step-num">ครั้งที่ ' + h.attempt + '</div>';
        entryHtml += '<div class="retry-step-error">Error: ' + escapeHtml(h.error) + '</div>';
        entryHtml += '<div class="retry-step-prompt"><b>เดิม:</b> ' + escapeHtml(h.old_prompt.slice(0, 120)) + (h.old_prompt.length > 120 ? '...' : '') + '</div>';
        entryHtml += '<div class="retry-step-prompt"><b>แก้เป็น:</b> ' + escapeHtml(h.new_prompt.slice(0, 120)) + (h.new_prompt.length > 120 ? '...' : '') + '</div>';
        entryHtml += '</div>';
      });
      entryHtml += '</details>';
    }
    if (e.warnings && e.warnings.length) {
      entryHtml += '<div class="retry-warnings">⚠ ' + escapeHtml(e.warnings.join('; ')) + '</div>';
    }
    entryHtml += '</div>';
    html += entryHtml;
  });
  existing.innerHTML = html;
}

// retry — ล้างสถานะเดิมแล้วสร้างใหม่
function retryMediaFromOutput(mediaType) {
  const session = _currentMediaSession;
  if (!session) return;
  fetch('/api/media_retry/' + encodeURIComponent(session), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({auto_image: mediaType === 'image', auto_video: mediaType === 'video'}),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      generateMediaFromOutput(mediaType);
    } else {
      alert(data.error || 'ไม่สามารถ retry ได้');
    }
  });
}

// สร้างรูป/วิดีโอจากหน้า output — เรียก /api/generate_all_media
function generateMediaFromOutput(mediaType) {
  const session = _currentMediaSession;
  const filename = _currentMediaFile;
  if (!session || !filename) return;
  const filePath = 'output/' + session + '/' + filename;
  const doImage = mediaType === 'image';
  const doVideo = mediaType === 'video';
  const bar = document.getElementById('media-action-bar');
  if (bar) bar.innerHTML = '<span class="media-status working">กำลังสร้าง' + (doImage ? 'รูป' : 'วิดีโอ') + '...</span>';
  let sseBuffer = '';
  let finished = false;

  function refreshAfterGen() {
    if (finished) return;
    finished = true;
    findSessionMedia(session, function(media) {
      const el = document.getElementById('preview-platform-content');
      if (el) el.innerHTML = renderPlatformPreview(_currentMediaPost, session, media.images, media.videos);
      fetch('/api/media_status/' + encodeURIComponent(session)).then(r => r.json()).then(st => {
        renderMediaActionBar(session, filename, _currentMediaPost, media, st);
      }).catch(() => renderMediaActionBar(session, filename, _currentMediaPost, media));
      // รีเฟรช sidebar file list ด้วย
      loadSessions();
      // รีเฟรชเครดิตหลัง media gen เสร็จ
      loadCredits();
    });
  }

  fetch('/api/generate_all_media', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({file: filePath, auto_image: doImage, auto_video: doVideo}),
  }).then(r => r.body.getReader()).then(reader => {
    const decoder = new TextDecoder();
    function pump() {
      reader.read().then(({done, value}) => {
        if (done) {
          refreshAfterGen();
          return;
        }
        sseBuffer += decoder.decode(value, {stream: true});
        // แยก SSE events ด้วย \n\n (SSE standard)
        const events = sseBuffer.split('\n\n');
        sseBuffer = events.pop(); // เก็บ chunk สุดท้ายที่ไม่ครบ
        for (const evt of events) {
          const line = evt.trim();
          if (!line.startsWith('data: ')) continue;
          try {
            const d = JSON.parse(line.slice(6));
            if (d.type === 'status' && bar) {
              bar.innerHTML = '<span class="media-status working">' + escapeHtml(d.text) + '</span>';
            } else if (d.type === 'error' && bar) {
              bar.innerHTML = '<span class="media-status error">⚠ ' + escapeHtml(d.text) + '</span>';
            } else if (d.type === 'done') {
              refreshAfterGen();
            }
          } catch (e) {}
        }
        pump();
      });
    }
    pump();
  });
}

function goHome() {
  closeResultOverlay();
}

let _settingsAgentKey = null;
let _instrPresets = [];
let _instrSettings = {};

function openAgentSettings(agentKey) {
  _settingsAgentKey = agentKey;
  const overlay = document.getElementById('settings-overlay');
  const title = document.getElementById('settings-title');
  const subtitle = document.getElementById('settings-subtitle');
  const info = AGENT_INFO[agentKey] || {};
  title.textContent = (info.icon || '⚙') + ' ' + (info.name || agentKey);
  subtitle.textContent = 'กำหนดวิธีที่ Agent ควรทำงานเพื่อให้ตรงกับความต้องการของคุณ';
  // Load instructions + presets
  fetch('/api/agent_instructions/' + agentKey).then(r => r.json()).then(data => {
    _instrPresets = data.presets || [];
    _instrSettings = data.settings || {};
    renderPresets();
    document.getElementById('instr-custom').value = _instrSettings.custom || '';
    // Live conflict check on open (from saved settings)
    checkConflictsLive();
    // Attach live listeners — ตรวจทุกครั้งที่ user พิมพ์/เปลี่ยน
    const ta = document.getElementById('instr-custom');
    if (ta && !ta._conflictListener) {
      ta._conflictListener = true;
      ta.addEventListener('input', debounceConflicts);
    }
  });
  overlay.className = 'settings-modal-overlay visible';
}

let _conflictTimer = null;
function debounceConflicts() {
  // Debounce — รอ 300ms หลัง user หยุดพิมพ์ แล้วค่อยตรวจ
  clearTimeout(_conflictTimer);
  _conflictTimer = setTimeout(checkConflictsLive, 300);
}

function checkConflictsLive() {
  // รวม current form state + _instrSettings แล้วส่งไปตรวจ
  const current = Object.assign({}, _instrSettings);
  const ta = document.getElementById('instr-custom');
  if (ta) current.custom = ta.value;
  fetch('/api/conflicts/check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings: current }),
  }).then(r => r.json()).then(renderConflictBanner).catch(() => {
    const b = document.getElementById('brand-conflict-banner');
    if (b) b.style.display = 'none';
  });
}

function renderConflictBanner(data) {
  const banner = document.getElementById('brand-conflict-banner');
  if (!banner) return;
  if (!data || !data.has_conflict) { banner.style.display = 'none'; return; }
  const conflicts = data.conflicts || [];
  let html = '<div class="brand-conflict-banner">';
  html += '<div class="brand-conflict-title">⚠️ การตั้งค่าของคุณขัดกับกฎแบรนด์</div>';
  for (const c of conflicts) {
    const isHard = c.severity === 'hard';
    html += '<div class="brand-conflict-item">';
    const brandVal = escapeHtml(String(c.brand_value || ''));
    const userVal = escapeHtml(Array.isArray(c.user_value) ? c.user_value.join(', ') : String(c.user_value || ''));
    if (c.field === 'tone') {
      html += '<span>Tone: brand "' + brandVal + '" vs คุณ "' + userVal + '"</span>';
      html += '<button class="brand-conflict-btn brand-conflict-btn-brand" onclick="resolveConflictUseBrand(\'tone\')">ใช้ brand</button>';
      html += '<button class="brand-conflict-btn brand-conflict-btn-user enabled" onclick="resolveConflictKeepUser()">ใช้ของฉัน</button>';
    } else if (c.field === 'language') {
      html += '<span>ภาษา: brand "' + brandVal + '" vs คุณ "' + userVal + '"</span>';
      html += '<button class="brand-conflict-btn brand-conflict-btn-brand" onclick="resolveConflictUseBrand(\'language\')">ใช้ brand</button>';
      html += '<button class="brand-conflict-btn brand-conflict-btn-user enabled" onclick="resolveConflictKeepUser()">ใช้ของฉัน</button>';
    } else if (c.field === 'sell_style') {
      html += '<span>การขาย: brand "' + brandVal + '" vs คุณ "' + userVal + '"</span>';
      html += '<button class="brand-conflict-btn brand-conflict-btn-brand" onclick="resolveConflictUseBrand(\'sell_style\')">ใช้ brand</button>';
      html += '<button class="brand-conflict-btn brand-conflict-btn-user enabled" onclick="resolveConflictKeepUser()">ใช้ของฉัน</button>';
    } else if (c.field === 'hook_style') {
      html += '<span>Hook: brand "' + brandVal + '" vs คุณ "' + userVal + '"</span>';
      html += '<button class="brand-conflict-btn brand-conflict-btn-brand" onclick="resolveConflictUseBrand(\'hook_style\')">ใช้ brand</button>';
      html += '<button class="brand-conflict-btn brand-conflict-btn-user enabled" onclick="resolveConflictKeepUser()">ใช้ของฉัน</button>';
    } else if (c.field === 'custom_banned_phrase' || c.field === 'custom_restricted_term') {
      html += '<span>คำต้องห้าม "' + brandVal + '" อยู่ใน Instructions ของคุณ</span>';
      html += '<span class="brand-conflict-hard-note">(brand ชนะ — ไม่สามารถ override)</span>';
      html += '<button class="brand-conflict-btn brand-conflict-btn-brand" onclick="resolveConflictRemoveFromCustom(\'' + brandVal.replace(/'/g, "\\'") + '\')">ลบคำออกจาก Instructions</button>';
    }
    html += '</div>';
  }
  html += '</div>';
  banner.innerHTML = html;
  banner.style.display = 'block';
}

function resolveConflictUseBrand(field) {
  // ลบ field ที่ conflict ออกจาก instructions → ใช้ brand default
  delete _instrSettings[field];
  renderPresets();
  updatePresetPreview();
  // Persist to backend without closing modal
  _instrSettings.custom = document.getElementById('instr-custom').value;
  fetch('/api/agent_instructions/' + _settingsAgentKey, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings: _instrSettings }),
  }).then(r => r.json()).then(data => {
    if (data.error) { alert(data.error); return; }
    checkConflictsLive();
  });
}

function resolveConflictKeepUser() {
  // เก็บค่า user ไว้ (soft — อนุญาต) แค่ซ่อน banner
  document.getElementById('brand-conflict-banner').style.display = 'none';
}

function resolveConflictRemoveFromCustom(phrase) {
  // ลบคำต้องห้ามออกจาก custom instruction — live, ไม่ต้อง save ก่อน
  const ta = document.getElementById('instr-custom');
  let text = ta.value;
  text = text.split(phrase).join('');
  ta.value = text;
  _instrSettings.custom = text;
  // Re-check conflicts immediately (live)
  checkConflictsLive();
}

function renderPresets() {
  const grid = document.getElementById('preset-grid');
  const currentPreset = _instrSettings.preset || '';
  let html = '';
  for (const p of _instrPresets) {
    const selected = p.key === currentPreset ? ' selected' : '';
    html += '<div class="preset-card' + selected + '" onclick="selectPreset(\'' + p.key + '\')">';
    html += '<div class="preset-card-label">' + p.label + '</div>';
    html += '<div class="preset-card-desc">' + (p.desc || '') + '</div>';
    html += '</div>';
  }
  grid.innerHTML = html;
  updatePresetPreview();
}

function updatePresetPreview() {
  const previewBox = document.getElementById('preset-preview');
  const previewText = document.getElementById('preset-preview-text');
  if (!previewBox || !previewText) return;
  const s = _instrSettings;
  const preset = _instrPresets.find(p => p.key === s.preset);
  let lines = [];
  if (preset) lines.push('โหมด: ' + preset.label);
  lines = lines.concat(buildSettingsSummary(_settingsAgentKey, s));
  if (lines.length) {
    previewText.innerHTML = lines.map(l => '• ' + l).join('<br>');
    previewBox.style.display = 'block';
  } else {
    previewBox.style.display = 'none';
  }
}

function selectPreset(key) {
  const preset = _instrPresets.find(p => p.key === key);
  if (!preset) return;
  // Selecting a named preset: start from defaults, then apply preset fields
  // This gives a clean reset — no leftover custom rules etc.
  fetch('/api/agent_instructions/' + _settingsAgentKey + '?defaults=1').then(r => r.json()).then(data => {
    _instrSettings = data.settings || {};
    _instrSettings.preset = key;
    for (const [k, v] of Object.entries(preset)) {
      if (k !== 'key' && k !== 'label' && k !== 'desc') {
        _instrSettings[k] = v;
      }
    }
    renderPresets();
    updatePresetPreview();
    // Live conflict check after preset change
    checkConflictsLive();
  });
}

function buildSettingsSummary(key, s) {
  let lines = [];
  if (key === 'product_spec') {
    const focusMap = { USP: 'USP', differentiation: 'จุดต่าง', sales_info: 'ฝ่ายขาย', technical: 'เทคนิค', customer_benefit: 'ประโยชน์ลูกค้า' };
    if (s.focus && s.focus.length) lines.push('เน้น: ' + s.focus.map(f => focusMap[f] || f).join(', '));
    const dlMap = { concise: 'สั้นกระชับ', standard: 'ปกติ', detailed: 'ละเอียดมาก' };
    lines.push('รายละเอียด: ' + (dlMap[s.detail_level] || 'ปกติ'));
    const dsMap = { strict: 'เคร่งครัด (ห้ามเดา)', moderate: 'ยืดหยุ่น', inferential: 'อนุมานได้' };
    lines.push('ข้อมูล: ' + (dsMap[s.data_strictness] || 'เคร่งครัด'));
  } else if (key === 'competitor_analysis') {
    const ctMap = { direct: 'คู่แข่งโดยตรง', indirect: 'ทางอ้อม', premium: 'Premium', lowcost: 'ราคาถูก' };
    if (s.competitor_types && s.competitor_types.length) lines.push('คู่แข่ง: ' + s.competitor_types.map(c => ctMap[c] || c).join(', '));
    const adMap = { basic: 'Basic', standard: 'Standard', deep: 'Deep Research' };
    lines.push('ความลึก: ' + (adMap[s.analysis_depth] || 'Standard'));
    lines.push('Web Search: ' + (s.web_search === false ? 'ปิด' : 'เปิด'));
  } else if (key === 'campaign_strategy') {
    const objMap = { sales: 'เพิ่มยอดขาย', newcustomers: 'ลูกค้าใหม่', launch: 'เปิดตัว', awareness: 'Brand Awareness', repeat: 'Repeat Purchase', clearance: 'ระบาย Stock' };
    lines.push('เป้าหมาย: ' + (objMap[s.campaign_objective] || 'เพิ่มยอดขาย'));
    const rlMap = { safe: 'ปลอดภัย', balanced: 'สมดุล', aggressive: 'กล้า' };
    lines.push('ความเสี่ยง: ' + (rlMap[s.risk_level] || 'สมดุล'));
    const prMap = { margin: 'Margin', brand: 'Brand', volume: 'Volume', acquisition: 'Acquisition' };
    if (s.priority && s.priority.length) lines.push('สำคัญ: ' + s.priority.map(p => prMap[p] || p).join(', '));
  } else if (key === 'content_creator') {
    if (s.tone && s.tone.length) lines.push('Tone: ' + s.tone.join(', '));
    const ssMap = { soft: 'Soft Sell', balanced: 'สมดุล', hard: 'Hard Sell' };
    lines.push('การขาย: ' + (ssMap[s.sell_style] || 'สมดุล'));
    lines.push('ภาษา: ' + (s.language || 'Conversational'));
  }
  return lines;
}

function closeAgentSettings() {
  document.getElementById('settings-overlay').className = 'settings-modal-overlay';
}

function resetAgentSettings() {
  if (!confirm('รีเซ็ตการตั้งค่าเป็นค่าเริ่มต้น?')) return;
  fetch('/api/agent_instructions/' + _settingsAgentKey + '?defaults=1').then(r => r.json()).then(data => {
    _instrSettings = data.settings || {};
    renderPresets();
    document.getElementById('instr-custom').value = _instrSettings.custom || '';
    updatePresetPreview();
  });
}

function saveAgentSettings() {
  _instrSettings.custom = document.getElementById('instr-custom').value;
  const body = _instrSettings;
  fetch('/api/agent_instructions/' + _settingsAgentKey, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings: body }),
  }).then(r => r.json()).then(data => {
    if (data.error) {
      alert(data.error);
      return;
    }
    if (data.ok) {
      closeAgentSettings();
      // Refresh conflict icons on agent cards
      loadConflictIcons();
    }
  });
}

loadFolderList();
loadCredits();

// Quick Brief live conflict detection
(function() {
  const ta = document.getElementById('quick-brief-input');
  if (!ta) return;
  let timer = null;
  ta.addEventListener('input', function() {
    clearTimeout(timer);
    timer = setTimeout(checkQuickBriefConflicts, 300);
  });
})();

function checkQuickBriefConflicts() {
  const ta = document.getElementById('quick-brief-input');
  if (!ta) return;
  const text = ta.value.trim();
  const banner = document.getElementById('quick-brief-conflict-banner');
  if (!banner) return;
  if (!text) { banner.style.display = 'none'; return; }
  // ตรวจเฉพาะ hard rules (banned/restricted ใน text) — Quick Brief เป็น per-run soft override
  fetch('/api/conflicts/check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings: { custom: text } }),
  }).then(r => r.json()).then(data => {
    if (!data || !data.has_conflict) { banner.style.display = 'none'; return; }
    let html = '<div class="brand-conflict-banner" style="padding:8px 12px">';
    html += '<div class="brand-conflict-title" style="font-size:12px">⚠️ Quick Brief มีคำที่ขัดกับกฎแบรนด์</div>';
    for (const c of data.conflicts) {
      const phrase = escapeHtml(String(c.brand_value || ''));
      html += '<div class="brand-conflict-item" style="font-size:11px;flex-direction:column;align-items:flex-start;gap:4px">';
      html += '<span>คำต้องห้าม "' + phrase + '" — ระบบจะไม่ใช้คำนี้</span>';
      html += '<span class="brand-conflict-hard-note">ถ้าต้องการใช้จริง ต้องไปลบออกจากการตั้งค่าแบรนด์</span>';
      html += '<div style="display:flex;gap:6px">';
      html += '<button class="brand-conflict-btn brand-conflict-btn-brand" onclick="removePhraseFromQuickBrief(\'' + phrase.replace(/'/g, "\\'") + '\')">ลบคำออกจาก Quick Brief</button>';
      html += '<button class="brand-conflict-btn brand-conflict-btn-brand" onclick="openBrandSettingsFromConflict()">ไปที่ตั้งค่าแบรนด์</button>';
      html += '</div>';
      html += '</div>';
    }
    html += '</div>';
    banner.innerHTML = html;
    banner.style.display = 'block';
  }).catch(() => { banner.style.display = 'none'; });
}

function openBrandSettingsFromConflict() {
  // เปิดหน้าแบรนด์ → ไปที่ section terms (คำต้องห้าม)
  if (typeof loadBrandFiles === 'function') {
    loadBrandFiles();
    // เลือก tab brand ใน sidebar
    const tabs = document.querySelectorAll('.sidebar-tab');
    tabs.forEach(t => { if (t.textContent.includes('แบรนด์') || t.dataset.tab === 'brand') t.click(); });
  }
}

function removePhraseFromQuickBrief(phrase) {
  const ta = document.getElementById('quick-brief-input');
  if (!ta) return;
  ta.value = ta.value.split(phrase).join('');
  checkQuickBriefConflicts();
}
// รีเฟรชเครดิตทุก 60 วินาที (auto-update โดยไม่ต้องรีเฟรชหน้า)
setInterval(loadCredits, 60000);

function loadCredits() {
  const badge = document.getElementById('credits-badge');
  if (!badge) return;
  badge.style.display = 'block';
  fetch('/api/credits').then(r => r.json()).then(data => {
    if (data.error) {
      badge.innerHTML = '⚠ <span class="credits-empty">ไม่สามารถดึงข้อมูลเครดิต</span>';
      return;
    }
    const remaining = data.limit_remaining;
    const limit = data.limit;
    const daily = data.usage_daily;
    if (remaining === null || remaining === undefined) {
      badge.innerHTML = 'เครดิต: <span class="credits-remaining">ไม่จำกัด</span>' + (daily != null ? ' · ใช้วันนี้: $' + Number(daily).toFixed(2) : '');
    } else {
      const cls = remaining <= 0 ? 'credits-empty' : (remaining < 1 ? 'credits-low' : 'credits-remaining');
      const limitStr = limit !== null && limit !== undefined ? ' / $' + Number(limit).toFixed(2) : '';
      badge.innerHTML = 'เครดิต: <span class="' + cls + '">$' + Number(remaining).toFixed(2) + limitStr + '</span>' + (daily != null ? ' · ใช้วันนี้: $' + Number(daily).toFixed(2) : '');
    }
  }).catch(() => {
    badge.innerHTML = '⚠ <span class="credits-empty">โหลดเครดิตไม่สได้</span>';
  });
}
</script>
<div class="settings-modal-overlay" id="upload-overlay">
  <div class="settings-modal" style="width:720px;max-width:92vw">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h3 id="upload-modal-title" style="margin:0">เพิ่มสินค้าใหม่</h3>
      <button onclick="closeUploadModal()" title="ปิด" style="background:none;border:none;color:#888;font-size:24px;cursor:pointer;line-height:1;padding:0 4px">&times;</button>
    </div>
    <label id="upload-name-label">ชื่อสินค้า</label>
    <input type="text" id="upload-product-name-modal" placeholder="เช่น Lagenio K2">
    <label>เลือกไฟล์ <span id="upload-formats-tooltip" style="cursor:help;color:#7c8aff;font-size:12px" title="กำลังโหลด...">ⓘ</span></label>
    <input type="file" id="upload-files-modal" multiple onchange="addFilesToQueueModal()">
    <div id="upload-queue-modal" class="upload-queue"></div>
    <div id="supported-formats-info" style="display:none"></div>
    <div id="existing-files-modal"></div>
    <div id="pp-section" style="display:none;margin-top:16px;padding:12px;background:#0f1117;border:1px solid #2a2d3a;border-radius:8px">
      <div style="font-size:13px;color:#7c8aff;margin-bottom:8px">🎯 ตำแหน่งสินค้า</div>
      <p style="font-size:12px;color:#888;margin:0 0 12px 0">ตำแหน่งสินค้าเฉพาะรุ่น — ค่าเริ่มต้นจะมาจากการคาดเดาของ AI</p>
      <div id="pp-modal-body"></div>
      <div class="upload-status" id="pp-status"></div>
    </div>
    <div class="upload-status" id="upload-modal-status"></div>
    <div class="settings-actions">
      <button id="upload-delete-product-btn" class="settings-cancel" style="display:none;border-color:#f87171;color:#f87171" onclick="deleteProductInModal()">ลบสินค้า</button>
      <button id="upload-submit-btn" class="settings-save" onclick="uploadFiles()">อัปโหลด</button>
    </div>
  </div>
</div>
<div class="settings-modal-overlay" id="settings-overlay">
  <div class="settings-modal" style="width:560px">
    <div style="display:flex;justify-content:space-between;align-items:flex-start">
      <div>
        <h3 id="settings-title" style="margin-bottom:2px">⚙ ตั้งค่า Agent</h3>
        <p id="settings-subtitle" style="font-size:12px;color:#888;margin:0"></p>
      </div>
      <button class="settings-reset" onclick="resetAgentSettings()" style="flex:none">↺ รีเซ็ต</button>
    </div>
    <div style="margin-top:16px">
      <div id="brand-conflict-banner" style="display:none;margin-bottom:14px"></div>
      <div class="preset-grid" id="preset-grid"></div>
      <div class="preset-preview" id="preset-preview" style="display:none">
        <div class="preset-preview-label">📋 Agent จะทำงานแบบนี้:</div>
        <div id="preset-preview-text"></div>
      </div>
      <div style="margin-top:16px;border-top:1px solid #2a2d3a;padding-top:14px">
        <label>💬 Instructions <span style="color:#666;font-size:11px">(พิมพ์คำสั่งเกี่ยวกับวิธีทำงาน — ห้ามพิมพ์เรื่องจำนวนโพสต์/แพลตฟอร์ม/สื่อ เพราะตั้งในกล่องด้านล่างได้แล้ว)</span></label>
        <textarea class="instr-textarea" id="instr-custom" placeholder="ตัวอย่าง:&#10;• วิเคราะห์สินค้าตามหัวข้อ 1.ชื่อ 2.ราคา 3.จุดขาย 4.กลุ่มเป้าหมาย&#10;• พูดแบบวัยรุ่น ใช้คำว่า มากก่า สุดยอด&#10;• ตอบไม่เกิน 200 คำ เน้นราคาเป็นหลัก"></textarea>
      </div>
    </div>
    <div class="settings-actions">
      <button class="settings-cancel" onclick="closeAgentSettings()">ยกเลิก</button>
      <button class="settings-save" onclick="saveAgentSettings()">บันทึก</button>
    </div>
  </div>
</div>
<div class="settings-modal-overlay" id="brand-overlay">
  <div class="settings-modal" style="width:600px;max-height:85vh;overflow-y:auto">
    <h3 id="brand-modal-title">✎ แก้ไข</h3>
    <div id="brand-modal-body"></div>
    <div class="upload-status" id="brand-save-status-modal"></div>
    <div class="settings-actions">
      <button class="settings-cancel" onclick="closeBrandModal()">ยกเลิก</button>
      <button class="settings-save" onclick="saveBrandFileModal()">บันทึก</button>
    </div>
  </div>
</div>

<div class="settings-modal-overlay" id="asset-overlay">
  <div class="settings-modal" style="width:720px;max-height:85vh;overflow-y:auto">
    <h3>🗂 วัตถุดิบแบรนด์</h3>
    <p style="font-size:12px;color:#888;margin:0 0 14px 0">อัปโหลดไฟล์ที่ใช้ซ้ำข้ามการรัน (โลโก้ รูปพรีเซนเตอร์ เพลง) — AI บรรยายและติดแท็กอัตโนมัติ แก้ไขรายละเอียดได้ทุกไฟล์</p>
    <div id="asset-modal-body"></div>
    <div class="settings-actions">
      <button class="settings-cancel" onclick="closeAssetModal()">ปิด</button>
    </div>
  </div>
</div>
<div class="settings-modal-overlay" id="voice-learn-overlay">
  <div class="settings-modal" style="width:600px;max-height:85vh;overflow-y:auto">
    <h3>🎓 Brand Learning — ฝึก AI จากตัวอย่าง</h3>
    <p style="font-size:12px;color:#888;margin:0 0 14px 0">ใส่ตัวอย่างโพสต์ที่สะท้อนแบรนด์ AI จะวิเคราะห์และสร้าง <b>3 ส่วนพร้อมกัน</b>: Voice (โทนเสียง) + Terms (คำที่ใช้/ห้ามใช้) + Audience (กลุ่มเป้าหมาย) — ส่วน Profile และ Visual ต้องกรอกเอง</p>
    <div style="margin-bottom:14px">
      <label style="font-size:12px;color:#888;display:block;margin-bottom:6px">📝 ตัวอย่างโพสต์ (พิมพ์แล้วกด Enter เพื่อเพิ่มเป็น card แยก)</label>
      <div id="voice-learn-text-cards" style="margin-bottom:8px"></div>
      <textarea id="voice-learn-text-input" placeholder="วางตัวอย่างโพสต์ที่นี่ แล้วกด Enter..." style="width:100%;min-height:60px;background:#0f1117;border:1px solid #2a2d3a;border-radius:8px;padding:12px;color:#e0e0e0;font-size:13px;line-height:1.6;resize:vertical" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();addVoiceLearnText()}"></textarea>
    </div>
    <div style="margin-bottom:14px">
      <label style="font-size:12px;color:#888;display:block;margin-bottom:6px">📎 Upload ไฟล์ (.txt, .md, .pdf, .docx)</label>
      <input type="file" multiple accept=".txt,.md,.pdf,.docx" onchange="handleVoiceLearnFiles(this)" style="font-size:12px;color:#ccc;margin-bottom:8px">
      <div id="voice-learn-file-list"></div>
    </div>
    <div style="margin-bottom:14px">
      <label style="font-size:12px;color:#888;display:block;margin-bottom:6px">🔗 URL (กด Enter หรือ paste หลายอันพร้อมกันได้)</label>
      <div style="width:100%;min-height:44px;background:#0f1117;border:1px solid #2a2d3a;border-radius:8px;padding:8px;display:flex;flex-wrap:wrap;align-items:center;gap:2px;cursor:text" onclick="document.getElementById('voice-learn-url-input').focus()">
        <div id="voice-learn-url-chips" style="display:flex;flex-wrap:wrap;align-items:center"></div>
        <input id="voice-learn-url-input" type="text" placeholder="วาง URL ที่นี่..." style="flex:1;min-width:120px;background:none;border:none;outline:none;color:#e0e0e0;font-size:13px;padding:4px" onkeydown="handleVoiceLearnUrlKeydown(this, event)" onpaste="handleVoiceLearnUrlPaste(this, event)">
      </div>
    </div>
    <div class="upload-status" id="voice-learn-status"></div>
    <div id="voice-learn-result"></div>
    <div class="settings-actions">
      <button class="settings-cancel" onclick="closeVoiceLearnModal()">ยกเลิก</button>
      <button class="settings-save" id="voice-learn-analyze-btn" onclick="runVoiceLearn()">วิเคราะห์</button>
    </div>
  </div>
</div>
<div class="settings-modal-overlay" id="pillars-overlay">
  <div class="settings-modal" style="width:560px;max-height:85vh">
    <h3>🎯 Content Pillars (เสาหลักคอนเทนต์)</h3>
    <p style="font-size:12px;color:#888;margin:0 0 14px 0">หมวดใหญ่ที่แบรนด์พูดเสมอ — AI จะหมุนเวียน ไม่ซ้ำหมวดเดิมบ่อยเกินไป ใส่ keywords ที่ทำให้ระบบเดาได้ว่าคอนเทนต์ไหนอยู่ในหมวดนี้</p>
    <div id="pillars-list" style="max-height:50vh;overflow-y:auto"></div>
    <button class="pillar-add-btn" onclick="addPillarCard()">+ เพิ่ม Pillar</button>
    <div class="upload-status" id="pillars-save-status"></div>
    <div class="settings-actions">
      <button class="settings-cancel" onclick="closePillarsModal()">ยกเลิก</button>
      <button class="settings-save" onclick="savePillars()">บันทึก</button>
    </div>
  </div>
</div>
<div class="settings-modal-overlay" id="sendto-overlay">
  <div class="settings-modal" style="width:480px">
    <h3>ส่งสินค้าไป agent</h3>
    <div id="sendto-summary" style="font-size:12px;color:#888;margin-bottom:12px"></div>
    <label style="font-size:12px;color:#888;display:block;margin-bottom:6px">เลือก agent ปลายทาง</label>
    <div id="sendto-agent-list" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px"></div>
    <div id="sendto-zone-row" style="display:none">
      <label style="font-size:12px;color:#888;display:block;margin-bottom:6px">โซน</label>
      <div style="display:flex;gap:8px;margin-bottom:16px">
        <button class="sendto-zone-btn" id="sendto-zone-combined" onclick="setSendToZone('combined')">🔗 รวม — หลายชิ้นทำงานเดียว</button>
        <button class="sendto-zone-btn" id="sendto-zone-separate" onclick="setSendToZone('separate')">📋 แยก — ชิ้นละงาน</button>
      </div>
    </div>
    <div class="settings-actions">
      <button class="settings-cancel" onclick="closeSendToAgentModal()">ยกเลิก</button>
      <button class="settings-save" id="sendto-confirm" onclick="confirmSendToAgent()" disabled>ส่ง</button>
    </div>
  </div>
</div>
<div class="result-overlay" id="result-overlay" onclick="closeResultOverlay(event)">
  <div class="result-modal" onclick="event.stopPropagation()">
    <div class="result-modal-header">
      <span class="result-modal-title" id="result-modal-title">ผลลัพธ์</span>
      <div style="display:flex;gap:8px;align-items:center">
        <button id="result-delete-btn" onclick="deleteCurrentResult()" style="background:#f44336;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px;font-weight:600">ลบผลลัพธ์</button>
        <button class="result-modal-close" onclick="closeResultOverlay()">✕ ปิด</button>
      </div>
    </div>
    <div id="result-nav-bar" style="display:none;align-items:center;justify-content:space-between;padding:8px 0 12px 0;border-bottom:1px solid #2a2d3a;margin-bottom:12px">
      <button id="result-nav-prev" onclick="navResult(-1)" style="background:none;border:1px solid #2a2d3a;color:#e0e0e0;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:13px">‹ ก่อนหน้า</button>
      <span id="result-nav-info" style="color:#888;font-size:12px"></span>
      <button id="result-nav-next" onclick="navResult(1)" style="background:none;border:1px solid #2a2d3a;color:#e0e0e0;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:13px">ถัดไป ›</button>
    </div>
    <div class="result-modal-body" id="result-modal-body"></div>
  </div>
</div>
<script src="/wizard_ui.js"></script>
<div class="settings-modal-overlay" id="schedule-overlay">
  <div class="settings-modal" style="width:520px">
    <h3>📅 ตั้งเวลารัน Flow</h3>
    <input type="hidden" id="schedule-flow-idx" value="">
    <div style="margin-bottom:12px;">
      <label for="schedule-name" style="font-size:12px;color:#888;display:block;margin-bottom:4px;">ชื่องาน</label>
      <input type="text" id="schedule-name" placeholder="เช่น โพสต์รายสัปดาห์" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid #2a2d3a;background:#0f111a;color:#e0e0e0;">
    </div>
    <div style="display:flex;gap:12px;margin-bottom:12px;">
      <div style="flex:1;">
        <label for="schedule-date" style="font-size:12px;color:#888;display:block;margin-bottom:4px;">วันที</label>
        <input type="date" id="schedule-date" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid #2a2d3a;background:#0f111a;color:#e0e0e0;color-scheme:dark;">
      </div>
      <div style="flex:0 0 120px;">
        <label for="schedule-time" style="font-size:12px;color:#888;display:block;margin-bottom:4px;">เวลา</label>
        <input type="time" id="schedule-time" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid #2a2d3a;background:#0f111a;color:#e0e0e0;color-scheme:dark;">
      </div>
    </div>
    <div style="display:flex;align-items:flex-start;gap:10px;margin-top:12px;">
      <input type="checkbox" id="schedule-repeat" style="width:auto; accent-color:#6366f1; margin-top:2px; flex:none;">
      <label for="schedule-repeat" style="font-size:13px;color:#e0e0e0;line-height:1.4;cursor:pointer;">ซ้ำทุกวันเวลานี้ (ทุกวัน)</label>
    </div>
    <div class="settings-actions">
      <button class="settings-cancel" onclick="closeScheduleModal()">ยกเลิก</button>
      <button class="settings-save" onclick="saveScheduleJob()">บันทึก</button>
    </div>
  </div>
</div>
<script>
function openScheduleModal(idx) {
  document.getElementById('schedule-flow-idx').value = idx;
  document.getElementById('schedule-name').value = '';
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const y = now.getFullYear();
  const m = pad(now.getMonth() + 1);
  const d = pad(now.getDate());
  const h = pad(now.getHours());
  const min = pad(now.getMinutes());
  const dateStr = y + '-' + m + '-' + d;
  document.getElementById('schedule-date').min = dateStr;
  document.getElementById('schedule-date').value = dateStr;
  document.getElementById('schedule-time').value = h + ':' + min;
  document.getElementById('schedule-repeat').checked = false;
  document.getElementById('schedule-overlay').className = 'settings-modal-overlay visible';
}
function closeScheduleModal() {
  document.getElementById('schedule-overlay').className = 'settings-modal-overlay';
}
async function saveScheduleJob() {
  const idx = parseInt(document.getElementById('schedule-flow-idx').value);
  const name = document.getElementById('schedule-name').value.trim() || 'งานตั้งเวลา';
  const date = document.getElementById('schedule-date').value;
  const time = document.getElementById('schedule-time').value;
  const repeat = document.getElementById('schedule-repeat').checked;
  if (!date || !time) { alert('เลือกวันและเวลาก่อน'); return; }
  const when = new Date(date + 'T' + time);
  if (isNaN(when.getTime())) { alert('วันเวลาไม่ถูกต้อง'); return; }
  if (!repeat && when.getTime() < Date.now()) { alert('วันเวลาต้องไม่อยู่ในอดีต'); return; }
  const all = (typeof buildFlowsForBackend === 'function') ? buildFlowsForBackend() : [];
  const flow = all[idx] || null;
  if (!flow) { alert('ไม่พบ flow นี้'); return; }
  const localISO = date + 'T' + time + ':00';
  const schedule = repeat
    ? { type: 'cron', value: time.split(':').reverse().join(' ') + ' * * *' }
    : { type: 'date', value: localISO };
  const payload = {
    name: name,
    schedule_type: repeat ? 'recurring' : 'one_time',
    schedule: schedule,
    flow: flow,
    quick_brief: (document.getElementById('quick-brief-input') || {}).value || ''
  };
  const res = await fetch('/api/schedule/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (!res.ok) { alert('บันทึกไม่ได้'); return; }
  const data = await res.json();
  alert('ตั้งเวลาสำเร็จ: ' + data.job_id);
  closeScheduleModal();
}
</script>
<div class="settings-modal-overlay" id="schedule-list-overlay">
  <div class="settings-modal" style="width:680px;max-height:88vh">
    <h3>📅 ตารางเวลา — ทำงานอัตโนมัติ</h3>
    <p style="font-size:12px;color:#888;margin:0 0 14px 0">ตั้งเวลาให้ flow ทำงานเองตอนถึงเวลาที่กำหนด — ปิดเว็บได้ ผลลัพธ์จะอยู่ในหน้าผลลัพธ์ (server ต้องเปิดอยู่ตอนถึงเวลา)</p>
    <div style="display:flex;gap:8px;margin-bottom:14px">
      <button class="home-header-btn" onclick="loadScheduleJobs();loadScheduleRuns()">รีเฟรช</button>
    </div>
    <div id="schedule-jobs-list" style="max-height:35vh;overflow-y:auto;margin-bottom:16px"></div>
    <h4 style="font-size:13px;color:#888;margin:14px 0 8px 0">ประวัติการรัน</h4>
    <div id="schedule-runs-list" style="max-height:25vh;overflow-y:auto"></div>
    <div class="settings-actions">
      <button class="settings-cancel" onclick="closeScheduleList()">ปิด</button>
    </div>
  </div>
</div>
<script>
function openScheduleList() {
  loadScheduleJobs();
  loadScheduleRuns();
  document.getElementById('schedule-list-overlay').className = 'settings-modal-overlay visible';
  if (window._scheduleRefreshTimer) clearInterval(window._scheduleRefreshTimer);
  window._scheduleRefreshTimer = setInterval(() => { loadScheduleJobs(); loadScheduleRuns(); }, 2000);
}
function closeScheduleList() {
  document.getElementById('schedule-list-overlay').className = 'settings-modal-overlay';
  if (window._scheduleRefreshTimer) { clearInterval(window._scheduleRefreshTimer); window._scheduleRefreshTimer = null; }
}
function loadScheduleJobs() {
  Promise.all([
    fetch('/api/schedule/jobs').then(r => r.json()),
    fetch('/api/schedule/runs?limit=50').then(r => r.json()),
    fetch('/api/schedule/status').then(r => r.json()).catch(() => ({}))
  ]).then(([jobs, runs, runningStatus]) => {
    const list = document.getElementById('schedule-jobs-list');
    jobs = jobs || [];
    // เพิ่ม virtual entry สำหรับ rerun ที่กำลังรัน แต่ไม่มี job entry ใน list
    // (เช่น one_time job ที่ถูกลบไปแล้ว แต่ user กดรันใหม่จากประวัติ)
    const jobIds = new Set(jobs.map(j => j.id));
    const virtualJobs = [];
    if (runningStatus) {
      for (const [rid, info] of Object.entries(runningStatus)) {
        if (!jobIds.has(rid)) {
          // หาชื่อ + flow จากประวัติล่าสุดของ job_id นี้
          const latestRun = (runs || []).filter(r => r.job_id === rid).sort((a, b) => new Date(b.started_at || 0) - new Date(a.started_at || 0))[0];
          virtualJobs.push({
            id: rid,
            name: (latestRun && latestRun.job_name) || 'รันใหม่',
            enabled: true,
            schedule_type: 'one_time',
            schedule: {},
            flow: latestRun ? latestRun.flow : {},
            run_count: 0,
            next_run: '',
            _isVirtual: true,
          });
        }
      }
    }
    const allJobs = virtualJobs.concat(jobs);
    if (allJobs.length === 0) {
      list.innerHTML = '<div style="color:#555;font-size:12px;padding:12px">ยังไม่มีงานตั้งเวลา</div>';
      return;
    }
    list.innerHTML = allJobs.map(j => {
      const isVirtual = !!j._isVirtual;
      const isEnabled = j.enabled;
      const hasNext = !!j.next_run;
      const isDone = !isVirtual && j.schedule_type === 'one_time' && !hasNext && (j.run_count || 0) > 0;
      const isRunning = runningStatus && runningStatus[j.id];
      let statusText, statusColor, subText;
      if (isRunning) {
        statusText = 'กำลังรัน';
        statusColor = '#fbbf24';
        const msg = runningStatus[j.id].message || '';
        const agent = runningStatus[j.id].agent || '';
        subText = agent ? (agent + ': ' + msg) : msg;
      } else if (isDone) { statusText = 'รันแล้ว'; statusColor = '#888'; subText = 'รันล่าสุด: ' + (j.last_run ? new Date(j.last_run).toLocaleString('th-TH') : '-'); }
      else if (!isEnabled) { statusText = 'หยุดไว้'; statusColor = '#888'; subText = 'หยุดไว้'; }
      else { statusText = 'เปิดใช้งาน'; statusColor = '#22c55e'; subText = 'รอเวลา'; }
      const scheduleLabel = isVirtual ? 'รันใหม่' : (j.schedule_type === 'one_time' ? 'ครั้งเดียว' : 'ทุกวัน');
      // เวลาที่ตั้งไว้ — ดึงจาก schedule.value
      // one_time: ISO date string → parse เป็น Date ปกติ
      // recurring: cron string "MM HH * * *" → แปลงเป็น "HH:MM ทุกวัน"
      let schedTimeStr = '';
      if (j.schedule && j.schedule.value) {
        if (j.schedule.type === 'cron') {
          const parts = j.schedule.value.split(' ');
          if (parts.length >= 2) {
            const minute = parts[0].padStart(2, '0');
            const hour = parts[1].padStart(2, '0');
            schedTimeStr = hour + ':' + minute + ' ทุกวัน';
          }
        } else {
          const schedTime = new Date(j.schedule.value);
          if (!isNaN(schedTime.getTime())) {
            schedTimeStr = schedTime.toLocaleTimeString('th-TH');
          }
        }
      }
      const latestRun = (runs || []).filter(r => r.job_id === j.id).sort((a, b) => new Date(b.started_at || 0) - new Date(a.started_at || 0))[0];
      const latestStatus = (!isRunning && latestRun) ? (' · ผลลัพธ์ล่าสุด: <span style="color:' + (latestRun.status === 'success' ? '#22c55e' : '#f44336') + '">' + (latestRun.status || '-') + '</span>') : '';
      const latestTime = (!isRunning && latestRun && latestRun.started_at) ? ' (' + new Date(latestRun.started_at).toLocaleString('th-TH') + ')' : '';
      const toggleLabel = j.enabled ? 'ปิด' : 'เปิด';
      const toggleIcon = j.enabled ? '⏸' : '▶';
      const runBtn = isRunning
        ? '<button class="home-header-btn" style="font-size:11px;padding:4px 8px;background:#f44336;color:#fff" onclick="stopScheduleJob()" title="หยุดการทำงานทันที">⏹ หยุด</button>'
        : '<button class="home-header-btn" style="font-size:11px;padding:4px 8px" onclick="runNowScheduleJob(\'' + j.id + '\')" title="รันทันที ไม่ต้องรอเวลา">▶ รันทันที</button>';
      // เส้นคั่นวันที่ (ใช้ next_run หรือ created_at)
      const dateSrc = j.next_run || j.created_at || '';
      const dateStr = dateSrc ? new Date(dateSrc).toLocaleDateString('th-TH') : '';
      const dateDivider = dateStr ? '<div style="border-top:1px solid #444;margin:10px 0 8px 0;padding-top:6px;font-size:11px;color:#888;font-weight:600">' + dateStr + '</div>' : '';
      return dateDivider +
      '<div style="background:#1a1a2e;border:1px solid ' + (isRunning ? '#fbbf24' : '#333') + ';border-radius:8px;padding:12px;margin-bottom:10px">' +
        '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px">' +
          '<div style="flex:1;min-width:0">' +
            '<div style="font-size:14px;font-weight:600;color:#e0e0e0;margin-bottom:4px">' + escapeHtml(j.name) + ' <span style="font-size:11px;color:#888;font-weight:normal">(' + scheduleLabel + ')</span></div>' +
            '<div style="font-size:11px;color:#888;line-height:1.5">สถานะ: <span style="color:' + statusColor + ';font-weight:600">' + statusText + '</span> · ' + escapeHtml(subText) + latestStatus + latestTime + '</div>' +
            (schedTimeStr ? '<div style="font-size:11px;color:#a1a1aa;margin-top:2px">⏰ ' + schedTimeStr + '</div>' : '') +
          '</div>' +
          '<div style="display:flex;gap:6px;flex-shrink:0">' +
            runBtn +
            (!isVirtual && !isRunning ? '<button class="home-header-btn" style="font-size:11px;padding:4px 8px" onclick="toggleScheduleJob(\'' + j.id + '\', ' + !j.enabled + ')" title="' + (j.enabled ? 'ปิดชั่วคราว' : 'เปิดใช้งาน') + '">' + toggleIcon + ' ' + toggleLabel + '</button>' : '') +
            (!isVirtual && !isRunning ? '<button class="home-header-btn" style="font-size:11px;padding:4px 8px;background:#f44336;color:#fff" onclick="deleteScheduleJob(\'' + j.id + '\')" title="ลบงานทิ้ง">🗑️ ลบ</button>' : '') +
          '</div>' +
        '</div>' +
      '</div>';
    }).join('');
  }).catch(e => {
    document.getElementById('schedule-jobs-list').innerHTML = '<div style="color:#f44336;font-size:12px;padding:12px">โหลดไม่ได้: ' + e + '</div>';
  });
}
function loadScheduleRuns() {
  fetch('/api/schedule/runs?limit=50').then(r => r.json()).then(runs => {
    const list = document.getElementById('schedule-runs-list');
    if (!runs || runs.length === 0) {
      list.innerHTML = '<div style="color:#555;font-size:12px;padding:8px">ยังไม่มีประวัติการรัน</div>';
      return;
    }
    // เรียงจากใหม่ไปเก่า — เพื่อใส่เส้นคั่นวันที่เมื่อวันเปลี่ยน
    const sorted = runs.slice().sort((a, b) => new Date(b.started_at || 0) - new Date(a.started_at || 0));
    let lastDateStr = '';
    let html = '';
    for (const r of sorted) {
      const d = r.started_at ? new Date(r.started_at) : null;
      const dateStr = d ? d.toLocaleDateString('th-TH') : '';
      if (dateStr && dateStr !== lastDateStr) {
        html += '<div style="border-top:1px solid #444;margin:10px 0 8px 0;padding-top:6px;font-size:11px;color:#888;font-weight:600">' + dateStr + '</div>';
        lastDateStr = dateStr;
      }
      const statusColor = r.status === 'success' ? '#22c55e' : (r.status === 'error' ? '#f44336' : '#3b82f6');
      const time = d ? d.toLocaleTimeString('th-TH') : '-';
      const triggerText = r.trigger === 'manual' ? 'รันด้วยมือ' : (r.trigger === 'rerun' ? 'รันใหม่' : 'รันตามเวลา');
      const files = (r.output_files || []).map(f => '<div style="font-size:11px;color:#3b82f6;cursor:pointer;margin-top:4px" onclick="viewResult(\'' + f.replace(/\\/g, '\\\\').replace(/'/g, "\\'") + '\')">' + escapeHtml(f.split('/').pop()) + '</div>').join('');
      const rerunBtn = (r.status === 'success' || r.status === 'error')
        ? '<button class="home-header-btn" style="font-size:10px;padding:2px 6px" onclick="rerunScheduleRun(\'' + r.job_id + '\', \'' + (r.started_at || '').replace(/'/g, "\\'") + '\')" title="รันใหม่จากการตั้งค่าเดิม">↻ รันใหม่</button>'
        : '';
      html += '<div style="background:#1a1a2e;border:1px solid #333;border-radius:8px;padding:10px;margin-bottom:8px">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">' +
          '<strong style="font-size:13px;color:#e0e0e0">' + escapeHtml(r.job_name || r.job_id) + '</strong>' +
          '<div style="display:flex;gap:6px;align-items:center">' +
            '<span style="color:' + statusColor + ';font-size:11px;font-weight:600">' + (r.status || '') + '</span>' +
            rerunBtn +
          '</div>' +
        '</div>' +
        '<div style="font-size:11px;color:#888;margin-bottom:4px">' + time + ' · ' + triggerText + '</div>' +
        (r.error ? '<div style="font-size:11px;color:#fbbf24;margin-bottom:4px">' + escapeHtml(r.error) + '</div>' : '') +
        (r.output_files && r.output_files.length ? '<div style="font-size:11px;color:#888;margin-bottom:2px">output files:</div>' + files : '') +
      '</div>';
    }
    list.innerHTML = html;
  }).catch(e => {
    document.getElementById('schedule-runs-list').innerHTML = '<div style="color:#555;font-size:12px;padding:8px">โหลดประวัติไม่ได้</div>';
  });
}
async function rerunScheduleRun(jobId, startedAt) {
  const res = await fetch('/api/schedule/rerun', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({job_id: jobId, started_at: startedAt}) });
  const data = await res.json();
  if (!data.ok) { alert('รันใหม่ไม่ได้ — อาจไม่พบประวัติที่เลือก'); return; }
  // รอให้ backend เริ่ม job แล้ว refresh จนกว่าจะเห็นสถานะ "กำลังรัน"
  // pollScheduleStatus ที่หน้าหลักจะแจ้งเตือนเอง — แค่ refresh list ใน modal
  let attempts = 0;
  const poll = () => {
    if (attempts >= 20) return; // รอ ~30 วินาที
    loadScheduleJobs();
    loadScheduleRuns();
    attempts++;
    setTimeout(poll, 1500);
  };
  setTimeout(poll, 500);
}
async function stopScheduleJob() {
  await fetch('/api/cancel', { method: 'POST' });
  loadScheduleJobs();
  loadScheduleRuns();
}
async function toggleScheduleJob(id, enabled) {
  await fetch('/api/schedule/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({job_id: id, enabled: enabled}) });
  loadScheduleJobs();
}
// Global poller — แจ้งเตือนบนหน้าหลักตอนมี job กำลังรัน
let _lastRunningJobs = new Set();
async function pollScheduleStatus() {
  try {
    const status = await fetch('/api/schedule/status').then(r => r.json());
    const runningIds = Object.keys(status || {});
    const badge = document.getElementById('schedule-badge');
    if (badge) badge.style.display = runningIds.length > 0 ? 'inline' : 'none';
    // แจ้งเตือน job ที่เริ่มรันใหม่
    for (const id of runningIds) {
      if (!_lastRunningJobs.has(id)) {
        const info = status[id];
        showScheduleToast('📅 งานตั้งเวลาเริ่มรัน: ' + (info.message || 'กำลังรัน...'));
      }
    }
    _lastRunningJobs = new Set(runningIds);
  } catch (e) {}
}
function showScheduleToast(msg) {
  let toast = document.getElementById('schedule-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'schedule-toast';
    toast.style.cssText = 'position:fixed;bottom:20px;right:20px;background:#1a1a2e;border:1px solid #fbbf24;border-radius:10px;padding:12px 16px;font-size:13px;color:#e0e0e0;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.4);transition:opacity 0.3s;max-width:400px;';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.style.opacity = '1';
  clearTimeout(window._scheduleToastTimer);
  window._scheduleToastTimer = setTimeout(() => { toast.style.opacity = '0'; }, 5000);
}
setInterval(pollScheduleStatus, 3000);
pollScheduleStatus();
async function runNowScheduleJob(id) {
  const res = await fetch('/api/schedule/run_now', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({job_id: id}) });
  const data = await res.json();
  if (!data.ok) { alert('รันไม่ได้'); return; }
  const list = document.getElementById('schedule-runs-list');
  list.insertAdjacentHTML('afterbegin', '<div id="schedule-pending" style="background:#1a1a2e;border:1px solid #333;border-radius:8px;padding:10px;margin-bottom:8px"><div style="font-size:13px;color:#fbbf24">⏳ กำลังรัน...</div><div style="font-size:11px;color:#888">รอผลลัพธ์สักครู่</div></div>');
  let attempts = 0;
  const poll = () => {
    if (attempts >= 20) { // รอ ~30 วินาที
      const p = document.getElementById('schedule-pending');
      if (p) { p.innerHTML = '<div style="font-size:13px;color:#f44336">หมดเวลารอ กดรีเฟรชดูผล</div>'; }
      return;
    }
    loadScheduleJobs();
    loadScheduleRuns();
    attempts++;
    const existing = Array.from(document.querySelectorAll('#schedule-runs-list > div')).some(el => el.id !== 'schedule-pending');
    if (existing) {
      const p = document.getElementById('schedule-pending');
      if (p) p.remove();
    } else {
      setTimeout(poll, 1500);
    }
  };
  setTimeout(poll, 1500);
}
async function deleteScheduleJob(id) {
  if (!confirm('ลบงานนี้?')) return;
  await fetch('/api/schedule/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({job_id: id}) });
  loadScheduleJobs();
  loadScheduleRuns();
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import atexit
    from src.config_loader import load_config, get_section
    _cfg = load_config()
    _main_sys_cfg = get_section(_cfg, "system", {"web_port": 8778})
    port = int(os.environ.get("VIEWER_PORT", str(_main_sys_cfg.get("web_port", 8778))))
    # Initialize conflict cache on startup
    try:
        _refresh_conflict_cache()
    except Exception:
        pass  # brand files might not exist yet
    # Initialize scheduler on startup
    try:
        _sched_cfg = get_section(_cfg, "scheduler", {})
        if _sched_cfg.get("enabled", True):
            _sched = _get_scheduler()
            if _sched:
                atexit.register(_sched.stop)
                print("  Scheduler: เปิดใช้งาน", flush=True)
    except Exception as e:
        print(f"  Scheduler: เปิดไม่ได้ ({e})", flush=True)
    print(f"\n  MKTApp Viewer → http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
