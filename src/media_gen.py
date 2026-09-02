"""Media generation — image + video via OpenRouter dedicated APIs.

Image API: POST /api/v1/images (synchronous, returns base64)
Video API: POST /api/v1/videos (async — submit → poll → download)

ใช้ API key ตัวเดียวกับ LLM client — ผ่าน OpenRouter ทั้งหมด
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

try:
    from .ai_usage import record_ai_usage, make_entry
except ImportError:
    from ai_usage import record_ai_usage, make_entry  # type: ignore


# ---------------------------------------------------------------------------
# Helpers — แปลงไฟล์รูปจริง → data URL สำหรับ input_references
# ---------------------------------------------------------------------------

def _image_to_data_url(image_path: str | Path) -> str | None:
    """แปลงไฟล์รูปเป็น base64 data URL — สำหรับส่งเป็น input_references.

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


def _paths_to_input_references(paths: list[str]) -> list[dict]:
    """แปลง list ของ image path → list ของ input_references objects สำหรับ API.

    ข้าม path ที่ไม่มีไฟล์อยู่จริง
    รูปแบบ: [{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]
    """
    refs: list[dict] = []
    for p in paths:
        url = _image_to_data_url(p)
        if url:
            refs.append({"type": "image_url", "image_url": {"url": url}})
    return refs

from .config_loader import get_env


# ---------------------------------------------------------------------------
# Lazy config loaders — อ่านจาก config/agents.yaml (sections: media_gen, system)
# ใช้ lazy loading เพื่อหลีกเลี่ยง circular import
# ---------------------------------------------------------------------------

def _media_cfg() -> dict:
    """อ่าน media config จาก config/media.yaml — fallback {} ถ้าโหลดไม่ได้."""
    cfg_path = Path("config/media.yaml")
    if not cfg_path.exists():
        return {}
    try:
        with cfg_path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _system_cfg() -> dict:
    """อ่าน system section จาก config — fallback {} ถ้าโหลดไม่ได้."""
    try:
        from .config_loader import load_config, get_section
        return get_section(load_config(), "system", {})
    except Exception:
        return {}


# Default models — fallback ถ้า config ไม่มี (override ได้ใน config/agents.yaml → media_gen)
_FALLBACK_IMAGE_MODEL = "google/gemini-3.1-flash-image"
_FALLBACK_VIDEO_MODEL = "bytedance/seedance-2.0-fast"


def _default_image_model() -> str:
    """คืน default image model — อ่านจาก config ก่อน ถ้าไม่มีใช้ fallback."""
    return _media_cfg().get("image_model", _FALLBACK_IMAGE_MODEL)


def _default_video_model() -> str:
    """คืน default video model — อ่านจาก config ก่อน ถ้าไม่มีใช้ fallback."""
    return _media_cfg().get("video_model", _FALLBACK_VIDEO_MODEL)


# Backward-compatible module-level constants (lazy — อ่านจาก config ตอน import)
DEFAULT_IMAGE_MODEL = _default_image_model()
DEFAULT_VIDEO_MODEL = _default_video_model()


def _load_media_config() -> dict[str, Any]:
    """อ่าน config/media.yaml — alias สำหรับ backward compat."""
    return _media_cfg()


def _get_api_key() -> str:
    key = get_env("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not found — ตั้งใน .env")
    return key


# ---------------------------------------------------------------------------
# Model capabilities — ดึงจาก API พร้อม cache (in-memory + disk)
# ---------------------------------------------------------------------------

# in-memory cache — ป้องกันเรียก API ซ้ำในเซสชั่นเดียวกัน
_CAPABILITIES_CACHE: dict[str, dict[str, Any]] = {}

# cache ไฟล์ — ข้ามเซสชั่น อยู่ใน cache/ เพื่อไม่ปน data/
_CAPABILITIES_CACHE_DIR = Path("cache") / "_media_capabilities"


def _capabilities_cache_ttl() -> int:
    """คืน cache TTL — อ่านจาก config (system.cache_ttl_capabilities), fallback 3600."""
    return int(_system_cfg().get("cache_ttl_capabilities", 3600))


def get_model_capabilities(model_id: str, kind: str = "video") -> dict[str, Any]:
    """ดึง capabilities ของ model จาก OpenRouter — มี cache.

    kind: "video" หรือ "image"
    คืน dict เช่น:
      video: {durations: [4,5,...15], aspect_ratios: ["16:9","9:16",...], resolutions: ["720p"], ...}
      image: {aspect_ratios: [...], ...}  (image API อาจไม่มี dedicated endpoint)
    ถ้าดึงไม่ได้ → คืน {} (caller ใช้ default)
    """
    cache_key = f"{kind}:{model_id}"

    # 1. in-memory cache
    if cache_key in _CAPABILITIES_CACHE:
        return _CAPABILITIES_CACHE[cache_key]

    # 2. disk cache (เช็ค TTL)
    cache_file = _CAPABILITIES_CACHE_DIR / f"{cache_key.replace('/', '_')}.json"
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < _capabilities_cache_ttl():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                _CAPABILITIES_CACHE[cache_key] = data
                return data
            except Exception:
                pass  # ไฟล์เสีย → ดึงใหม่

    # 3. ดึงจาก API
    api_key = _get_api_key()
    endpoint = (
        "https://openrouter.ai/api/v1/videos/models"
        if kind == "video"
        else "https://openrouter.ai/api/v1/images/models"
    )
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        with httpx.Client(timeout=int(_system_cfg().get("api_timeout_capabilities", 30))) as client:
            resp = client.get(endpoint, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        # ดึงไม่ได้ → คืน {} (caller ใช้ default)
        return {}

    models = data.get("data", [])
    for m in models:
        if m.get("id") == model_id:
            caps = {
                "id": m.get("id"),
                "name": m.get("name"),
                "durations": m.get("supported_durations") or [],
                "aspect_ratios": m.get("supported_aspect_ratios") or [],
                "resolutions": m.get("supported_resolutions") or [],
                "sizes": m.get("supported_sizes") or [],
                "generate_audio": m.get("generate_audio"),
                "frame_images": m.get("supported_frame_images") or [],
            }
            # เก็บ cache
            _CAPABILITIES_CACHE[cache_key] = caps
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(caps, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass  # เขียน cache ไม่ได้ก็ไม่เป็นไร
            return caps

    # ไม่เจอ model ใน list → คืน {}
    return {}


def format_capabilities_for_prompt(model_id: str, kind: str = "video") -> str:
    """สร้างข้อความบอก agent ว่า model นี้ทำได้อะไร — ใส่ใน prompt.

    ถ้าดึง capabilities ไม่ได้ → คืน "" (ไม่บังคับ)
    """
    caps = get_model_capabilities(model_id, kind=kind)
    if not caps:
        return ""

    parts = []
    if caps.get("durations"):
        durs = caps["durations"]
        parts.append(f"duration: {durs[0]}-{durs[-1]} วินาที (ค่าที่ใช้ได้: {', '.join(str(d) for d in durs)})")
    if caps.get("aspect_ratios"):
        parts.append(f"aspect ratio: {', '.join(caps['aspect_ratios'])}")
    if caps.get("resolutions"):
        parts.append(f"resolution: {', '.join(caps['resolutions'])}")
    if caps.get("generate_audio"):
        parts.append("audio: สร้างเสียงได้")
    if not parts:
        return ""
    return f"Model {model_id} รองรับ: {' | '.join(parts)}"


def clamp_to_capabilities(
    value: Any,
    caps: dict[str, Any],
    field: str,
    default: Any,
) -> tuple[Any, str | None]:
    """ปรับค่าให้อยู่ในกรอบที่ model รองรับ — คืน (adjusted_value, warning_msg).

    ถ้า caps ว่าง → ใช้ default ไม่ clamp
    ถ้า value อยู่ในกรอบ → คืน value เดิม
    ถ้าเกิน → ปัดใกล้สุด + คืน warning
    """
    allowed = caps.get(field)
    if not allowed:
        return default, None  # ไม่รู้ capabilities → ใช้ default

    if field == "durations":
        # value เป็น int (วินาที)
        if value in allowed:
            return value, None
        # ปัดใกล้สุด
        closest = min(allowed, key=lambda d: abs(d - value))
        if closest != value:
            return closest, f"duration {value}s → {closest}s (model รองรับ {allowed})"
        return value, None

    if field == "aspect_ratios":
        # value เป็น string เช่น "9:16"
        if value in allowed:
            return value, None
        # หาใกล้สุด (เทียบ ratio)
        def ratio(s: str) -> float:
            try:
                w, h = s.split(":")
                return float(w) / float(h)
            except Exception:
                return 1.0
        target = ratio(value)
        closest = min(allowed, key=lambda s: abs(ratio(s) - target))
        if closest != value:
            return closest, f"aspect {value} → {closest} (model รองรับ {allowed})"
        return value, None

    if field == "resolutions":
        if value in allowed:
            return value, None
        # แปลงเป็นตัวเลขเพื่อเทียบความใกล้ (480p → 480, 1080p → 1080, 4K → 2160)
        def res_num(s: str) -> int:
            s = s.lower().strip()
            if s == "4k":
                return 2160
            if s == "2k":
                return 1440
            m = re.search(r"(\d+)p?", s)
            return int(m.group(1)) if m else 0
        target = res_num(str(value))
        closest = min(allowed, key=lambda s: abs(res_num(s) - target)) if allowed else default
        if closest != value:
            return closest, f"resolution {value} → {closest} (model รองรับ {allowed})"
        return value, None

    return default, None


# ---------------------------------------------------------------------------
# Visual brand injection — แป๊ะ visual keywords ต่อท้าย prompt
# ---------------------------------------------------------------------------

def build_visual_suffix(visual: dict[str, Any]) -> str:
    """สร้าง suffix string จาก visual.json dict แป๊ะต่อท้าย image/video prompt.

    รวม: keywords (ใช้), avoid (หลีกเลี่ยง), colors (hex), image_style.tone
    รองรับ legacy visual_override ที่ image_style หรือ keywords เป็น string.
    ถ้า visual ว่าง → คืน string ว่าง (ไม่แป๊ะอะไร)
    """
    if not visual:
        return ""

    parts: list[str] = []

    # Keywords — คำที่ควรใช้ใน prompt
    keywords_raw = visual.get("keywords", [])
    if isinstance(keywords_raw, str):
        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    elif isinstance(keywords_raw, list):
        keywords = keywords_raw
    else:
        keywords = []
    if keywords:
        parts.append("Style keywords: " + ", ".join(keywords))

    # Colors — hex color hints
    colors = visual.get("colors", {})
    if isinstance(colors, dict):
        color_hints = [f"{k} {v}" for k, v in colors.items() if v]
        if color_hints:
            parts.append("Brand colors: " + ", ".join(color_hints))

    # Image style tone + product shot
    image_style = visual.get("image_style", {})
    if isinstance(image_style, str):
        tone = image_style
        product_shot = ""
    elif isinstance(image_style, dict):
        tone = image_style.get("tone", "")
        product_shot = image_style.get("product_shot", "")
    else:
        tone = ""
        product_shot = ""
    if tone:
        parts.append(f"Overall tone: {tone}")
    if product_shot:
        parts.append(f"Product shot style: {product_shot}")

    # Avoid — คำที่หลีกเลี่ยง
    avoid_raw = visual.get("avoid", [])
    if isinstance(avoid_raw, str):
        avoid = [a.strip() for a in avoid_raw.split(",") if a.strip()]
    elif isinstance(avoid_raw, list):
        avoid = avoid_raw
    else:
        avoid = []
    if avoid:
        parts.append("Avoid: " + ", ".join(avoid))

    if not parts:
        return ""

    return " | " + " | ".join(parts)


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def generate_image(
    prompt: str,
    output_path: Path,
    *,
    model: str | None = None,
    aspect_ratio: str | None = None,
    timeout: float | None = None,
    input_references: list[dict] | list[str] | None = None,
    visual: dict[str, Any] | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """สร้างรูปจาก prompt — เซฟลง output_path แล้วคืน metadata.

    ก่อนส่ง API จะเช็ค + clamp aspect_ratio กับ capabilities ของ model
    input_references: list ของ dict (API format) หรือ list ของ path รูปจริง
        - ถ้าเป็น path (str) → แปลงเป็น base64 data URL อัตโนมัติ
        - ใช้เป็น reference image สำหรับ image-to-image generation
    visual: dict จาก brand/visual.json — แป๊ะ keywords/colors/tone ต่อท้าย prompt
    คืน: {ok, path, model, prompt, error?, warnings?}
    """
    cfg = _load_media_config()
    mcfg = _media_cfg()
    model = model or mcfg.get("image_model", DEFAULT_IMAGE_MODEL)
    if aspect_ratio is None:
        aspect_ratio = mcfg.get("image_aspect_ratio", "16:9")
    if timeout is None:
        timeout = float(mcfg.get("image_timeout_seconds", 180))
    api_key = _get_api_key()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Visual brand injection — แป๊ะ keywords/colors/tone ต่อท้าย prompt
    if visual:
        prompt = prompt + build_visual_suffix(visual)

    # Clamp aspect_ratio กับ model capabilities
    warnings: list[str] = []
    caps = get_model_capabilities(model, kind="image")
    if caps:
        aspect_ratio, warn = clamp_to_capabilities(aspect_ratio, caps, "aspect_ratios", aspect_ratio)
        if warn:
            warnings.append(warn)

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
    }
    # n: จำนวนภาพ — default จาก config (media_gen.image_n)
    n = cfg.get("image_n", mcfg.get("image_n", 1))
    if n > 1:
        payload["n"] = n

    # input_references — รูปสินค้าจริงสำหรับ image-to-image
    if input_references:
        refs: list[dict] = []
        for r in input_references:
            if isinstance(r, str):
                # path → data URL
                url = _image_to_data_url(r)
                if url:
                    refs.append({"type": "image_url", "image_url": {"url": url}})
            elif isinstance(r, dict):
                refs.append(r)
        if refs:
            payload["input_references"] = refs

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    t0 = time.time()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                "https://openrouter.ai/api/v1/images",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        # log usage ไป AI Usage Hub
        _log_media_usage(
            "image", model, data.get("usage"),
            request_id=data.get("id"),
            duration_ms=int((time.time() - t0) * 1000),
            attempt=attempt,
            units={"images_generated": n},
        )

        images = data.get("data", [])
        if not images:
            return {"ok": False, "error": "API ไม่คืนภาพ", "model": model, "prompt": prompt}

        # เซฟภาพแรก (ถ้า n>1 เซฟเป็น _1, _2, ...)
        saved_paths: list[str] = []
        for i, img in enumerate(images):
            b64 = img.get("b64_json")
            if not b64:
                continue
            img_bytes = base64.b64decode(b64)
            if len(images) > 1:
                p = output_path.with_name(f"{output_path.stem}_{i+1}{output_path.suffix}")
            else:
                p = output_path
            p.write_bytes(img_bytes)
            saved_paths.append(str(p))

        if not saved_paths:
            return {"ok": False, "error": "ไม่ได้รับ b64_json", "model": model, "prompt": prompt}

        return {
            "ok": True,
            "path": saved_paths[0],
            "paths": saved_paths,
            "model": model,
            "prompt": prompt,
            "warnings": warnings,
        }
    except httpx.HTTPStatusError as e:
        _log_media_usage("image", model, None, duration_ms=int((time.time() - t0) * 1000), status="error",
                         http_status=e.response.status_code, attempt=attempt, units={"images_generated": n},
                         error_message=str(e))
        _err_len = int(_system_cfg().get("error_preview_length", 200))
        et, ec = _parse_error_response(e.response.text)
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:_err_len]}",
                "error_type": et, "error_code": ec, "http_status": e.response.status_code,
                "model": model, "prompt": prompt, "warnings": warnings}
    except Exception as e:
        _log_media_usage("image", model, None, duration_ms=int((time.time() - t0) * 1000), status="error",
                         attempt=attempt, units={"images_generated": n}, error_message=str(e))
        return {"ok": False, "error": str(e), "model": model, "prompt": prompt, "warnings": warnings}


# ---------------------------------------------------------------------------
# Video generation (async — submit, poll, download)
# ---------------------------------------------------------------------------

def generate_video(
    prompt: str,
    output_path: Path,
    *,
    model: str | None = None,
    duration: int | None = None,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    poll_interval: float | None = None,
    max_wait: float | None = None,
    on_status=None,
    input_references: list[dict] | list[str] | None = None,
    frame_images: list[dict] | list[str] | None = None,
    visual: dict[str, Any] | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """สร้างวิดีโอจาก prompt — async รอจนเสร็จ — เซฟลง output_path.

    ก่อนส่ง API จะเช็ค + clamp duration/aspect_ratio/resolution กับ capabilities ของ model
    on_status: callback(status_str) สำหรับโชว์ progress
    input_references: list ของ path รูปจริง (str) หรือ dict (API format)
        - ใช้เป็น reference สำหรับ reference-to-video (style/subject guidance)
    frame_images: list ของ path รูปจริง (str) หรือ dict (API format)
        - ใช้เป็น first_frame / last_frame สำหรับ image-to-video
        - ถ้าเป็น str → ใช้เป็น first_frame อัตโนมัติ
    visual: dict จาก brand/visual.json — แป๊ะ keywords/colors/tone ต่อท้าย prompt
    คืน: {ok, path, model, prompt, error?, warnings?}
    """
    cfg = _load_media_config()
    mcfg = _media_cfg()
    model = model or mcfg.get("video_model", DEFAULT_VIDEO_MODEL)
    if duration is None:
        duration = int(mcfg.get("video_duration", 5))
    if aspect_ratio is None:
        aspect_ratio = mcfg.get("video_aspect_ratio", "16:9")
    if resolution is None:
        resolution = mcfg.get("video_resolution", "720p")
    if poll_interval is None:
        poll_interval = float(mcfg.get("video_poll_interval_seconds", 5.0))
    if max_wait is None:
        max_wait = float(mcfg.get("video_max_wait_seconds", 600.0))
    api_key = _get_api_key()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Visual brand injection — แป๊ะ keywords/colors/tone ต่อท้าย prompt
    if visual:
        prompt = prompt + build_visual_suffix(visual)

    # Clamp กับ model capabilities
    warnings: list[str] = []
    caps = get_model_capabilities(model, kind="video")
    if caps:
        duration, warn = clamp_to_capabilities(duration, caps, "durations", duration)
        if warn:
            warnings.append(warn)
        aspect_ratio, warn = clamp_to_capabilities(aspect_ratio, caps, "aspect_ratios", aspect_ratio)
        if warn:
            warnings.append(warn)
        resolution, warn = clamp_to_capabilities(resolution, caps, "resolutions", resolution)
        if warn:
            warnings.append(warn)

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "duration": duration,
    }

    # input_references — reference-to-video (style/subject guidance)
    if input_references:
        refs: list[dict] = []
        for r in input_references:
            if isinstance(r, str):
                url = _image_to_data_url(r)
                if url:
                    refs.append({"type": "image_url", "image_url": {"url": url}})
            elif isinstance(r, dict):
                refs.append(r)
        if refs:
            payload["input_references"] = refs

    # frame_images — image-to-video (first/last frame)
    if frame_images:
        frames: list[dict] = []
        for fi in frame_images:
            if isinstance(fi, str):
                url = _image_to_data_url(fi)
                if url:
                    frames.append({
                        "type": "image_url",
                        "image_url": {"url": url},
                        "frame_type": "first_frame",
                    })
            elif isinstance(fi, dict):
                frames.append(fi)
        if frames:
            payload["frame_images"] = frames

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    units = {"videos_generated": 1, "duration_seconds": duration, "resolution": resolution, "aspect_ratio": aspect_ratio}
    job_id: str | None = None

    t0 = time.time()
    try:
        if on_status:
            on_status("submitting")
        with httpx.Client(timeout=int(_system_cfg().get("api_timeout_video", 60))) as client:
            resp = client.post(
                "https://openrouter.ai/api/v1/videos",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()

        job_id = result.get("id")
        polling_url = result.get("polling_url")
        if not job_id or not polling_url:
            _log_media_usage("video", model, None, duration_ms=int((time.time() - t0) * 1000), status="error",
                             request_id=job_id, attempt=attempt, units=units, error_message="no job_id")
            return {"ok": False, "error": f"API ไม่คืน job_id: {result}", "model": model, "prompt": prompt}

        # Poll จนเสร็จ
        elapsed = 0.0
        with httpx.Client(timeout=int(_system_cfg().get("api_timeout_video", 60))) as client:
            while elapsed < max_wait:
                if on_status:
                    on_status(f"generating ({int(elapsed)}s)")
                time.sleep(poll_interval)
                elapsed += poll_interval

                poll = client.get(polling_url, headers=headers)
                poll.raise_for_status()
                status_data = poll.json()
                status = status_data.get("status", "")

                if status == "completed":
                    urls = status_data.get("unsigned_urls") or status_data.get("urls") or []
                    if not urls:
                        _log_media_usage("video", model, status_data.get("usage"),
                                         duration_ms=int((time.time() - t0) * 1000), status="error",
                                         request_id=job_id, attempt=attempt, units=units,
                                         error_message="completed but no url")
                        return {"ok": False, "error": "completed แต่ไม่มี url", "model": model, "prompt": prompt, "warnings": warnings}
                    # log usage จาก poll response
                    _log_media_usage("video", model, status_data.get("usage"),
                                     duration_ms=int((time.time() - t0) * 1000),
                                     request_id=job_id, attempt=attempt, units=units)
                    # download — ส่ง Authorization header เหมือนโค้ดที่ใช้งานได้ใน my-agent-app
                    if on_status:
                        on_status("downloading")
                    video_resp = httpx.get(
                        urls[0],
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=int(_system_cfg().get("api_timeout_video_download", 120)),
                    )
                    video_resp.raise_for_status()
                    output_path.write_bytes(video_resp.content)
                    return {
                        "ok": True,
                        "path": str(output_path),
                        "model": model,
                        "prompt": prompt,
                        "url": urls[0],
                        "warnings": warnings,
                    }
                elif status == "failed":
                    err = status_data.get("error", "unknown")
                    _log_media_usage("video", model, status_data.get("usage"),
                                     duration_ms=int((time.time() - t0) * 1000), status="error",
                                     request_id=job_id, attempt=attempt, units=units,
                                     error_message=str(err))
                    return {"ok": False, "error": f"video gen failed: {err}", "model": model, "prompt": prompt, "warnings": warnings}

        _log_media_usage("video", model, None, duration_ms=int((time.time() - t0) * 1000), status="timeout",
                         request_id=job_id, attempt=attempt, units=units,
                         error_message=f"timeout after {max_wait}s")
        return {"ok": False, "error": f"timeout after {max_wait}s", "model": model, "prompt": prompt, "warnings": warnings}
    except httpx.HTTPStatusError as e:
        _log_media_usage("video", model, None, duration_ms=int((time.time() - t0) * 1000), status="error",
                         request_id=job_id, attempt=attempt, units=units,
                         http_status=e.response.status_code, error_message=str(e))
        _err_len = int(_system_cfg().get("error_preview_length", 200))
        et, ec = _parse_error_response(e.response.text)
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:_err_len]}",
                "error_type": et, "error_code": ec, "http_status": e.response.status_code,
                "model": model, "prompt": prompt, "warnings": warnings}
    except Exception as e:
        _log_media_usage("video", model, None, duration_ms=int((time.time() - t0) * 1000), status="error",
                         request_id=job_id, attempt=attempt, units=units, error_message=str(e))
        return {"ok": False, "error": str(e), "model": model, "prompt": prompt, "warnings": warnings}


# ---------------------------------------------------------------------------
# AI Usage Hub — log การใช้ media gen API
# ---------------------------------------------------------------------------

def _log_media_usage(
    media_type: str,
    model: str,
    usage: dict[str, Any] | None,
    *,
    duration_ms: int,
    attempt: int = 1,
    status: str = "success",
    http_status: int | None = None,
    error_message: str | None = None,
    request_id: str | None = None,
    units: dict[str, Any] | None = None,
) -> None:
    """บันทึก image/video generation usage — fire-and-forget."""
    operation = "images.generate" if media_type == "image" else "videos.generate"
    cost_usd: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    if usage:
        cost = usage.get("cost") or usage.get("total_cost")
        if cost is not None:
            cost_usd = float(cost)
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
    entry = make_entry(
        provider="openrouter",
        model=model,
        operation=operation,
        source=f"media_gen.generate_{media_type}",
        request_id=request_id,
        duration_ms=duration_ms,
        attempt=attempt,
        status=status,
        http_status=http_status,
        error_message=error_message,
        cost_usd=cost_usd,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        raw_usage=usage,
        units=units,
    )
    record_ai_usage(entry)


# ---------------------------------------------------------------------------
# Retry-on-rejection — parse structured error_type จาก OpenRouter แทน keyword matching
# OpenRouter คืน error_type ใน error.metadata.error_type (หรือ nested ใน message)
# ดู: https://openrouter.ai/docs/api/reference/errors-and-debugging
# ---------------------------------------------------------------------------

# error_type ที่ OpenRouter ใช้ — ครอบคลุม error ทุกประเภด ไม่ต้องเดาจาก keyword
_CONTENT_POLICY_TYPES = {
    "content_policy_violation",
    "image_content_policy_violation",
}
_TRANSIENT_TYPES = {
    "rate_limit_exceeded",
    "server_error",
    "timeout",
    "provider_overloaded",
    "provider_unavailable",
}


def _parse_error_response(response_text: str) -> tuple[str | None, str | None]:
    """Parse error_type และ error_code จาก OpenRouter error response.

    OpenRouter format: {"error": {"code": 400, "message": "...", "metadata": {"error_type": "..."}}}
    บางครั้ง provider error ซ้อนอยู่ใน message เป็น stringified JSON.

    คืน: (error_type, error_code) — ทั้งคู่อาจเป็น None ถ้า parse ไม่ได้
    """
    try:
        outer = json.loads(response_text)
    except (json.JSONDecodeError, TypeError):
        return None, None

    err = outer.get("error") if isinstance(outer, dict) else None
    if not isinstance(err, dict):
        return None, None

    # 1) error_type จาก metadata (OpenRouter canonical)
    metadata = err.get("metadata") or {}
    if isinstance(metadata, dict):
        et = metadata.get("error_type")
        if et:
            return str(et), None

    # 2) error_type ระดับบนสุด (Responses API format)
    et = outer.get("error_type")
    if et:
        return str(et), None

    # 3) provider error ซ้อนใน message — ลอง parse JSON ที่ฝังอยู่
    message = err.get("message", "")
    json_start = message.find("{")
    if json_start >= 0:
        try:
            inner = json.loads(message[json_start:])
            inner_err = inner.get("error") if isinstance(inner, dict) else None
            if isinstance(inner_err, dict):
                inner_code = inner_err.get("code")
                if inner_code:
                    return None, str(inner_code)
        except (json.JSONDecodeError, TypeError):
            pass

    return None, None


def _classify_media_error(
    error_type: str | None,
    error_code: str | None,
    http_status: int | None,
) -> str:
    """จำแนกประเภท error เพื่อเลือก retry strategy.

    คืน: 'content_policy' | 'transient' | 'other'
    - content_policy: โดน content filter (flaky — retry กับ provider เดิมมีโอกาสผ่าน)
    - transient: rate limit / server error (retry กับ backoff)
    - other: ไม่ retry
    """
    et = (error_type or "").lower()
    ec = (error_code or "").lower()

    # content policy — จาก OpenRouter error_type
    if et in _CONTENT_POLICY_TYPES:
        return "content_policy"
    # content policy — จาก provider error code (เช่น InputImageSensitiveContentDetected.PrivacyInformation)
    if "sensitivecontent" in ec or "privacy" in ec or "policyviolation" in ec or "contentpolicy" in ec:
        return "content_policy"
    # transient — จาก error_type
    if et in _TRANSIENT_TYPES:
        return "transient"
    # transient — จาก HTTP status
    if http_status and http_status >= 500:
        return "transient"
    if http_status == 429:
        return "transient"
    return "other"


def _rewrite_prompt_with_llm(
    original_prompt: str,
    error: str,
    media_type: str,
    llm,
    model: str,
) -> str | None:
    """ให้ LLM แก้ prompt ที่ถูก reject — คืน prompt ใหม่ หรือ None ถ้าแก้ไม่ได้.

    llm: LLMClient instance
    model: model slug สำหรับ LLM (ไม่ใช่ media model)
    """
    system = (
        "You are a prompt rewriter for media generation. "
        "The user's prompt was rejected by the image/video generation API. "
        "Rewrite the prompt to avoid the rejection while keeping the same creative intent. "
        "Rules:\n"
        "1. Replace copyrighted characters with generic equivalents "
        "(e.g. 'Super Girl' → 'a brave girl in a colorful superhero cape', "
        "'Mickey Mouse' → 'a cheerful cartoon mouse').\n"
        "2. Replace real celebrity names with generic descriptions.\n"
        "3. Remove brand names and logos if they caused the rejection.\n"
        "4. Keep the same mood, scene, and composition.\n"
        "5. Output ONLY the rewritten prompt, nothing else."
    )
    user_msg = (
        f"Media type: {media_type}\n"
        f"Original prompt: {original_prompt}\n"
        f"Rejection error: {error}\n\n"
        f"Rewrite the prompt to avoid rejection. Output only the new prompt."
    )
    try:
        mcfg = _media_cfg()
        new_prompt = llm.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            model=model,
            temperature=float(mcfg.get("retry_temperature", 0.3)),
            max_tokens=int(mcfg.get("retry_max_tokens", 1024)),
            stream=False,
            source="media_gen.rewrite_prompt",
        )
        new_prompt = new_prompt.strip().strip('"').strip("'").strip()
        if new_prompt and len(new_prompt) > 10 and new_prompt != original_prompt:
            return new_prompt
    except Exception:
        pass
    return None


def generate_image_with_retry(
    prompt: str,
    output_path: Path,
    *,
    llm=None,
    model: str | None = None,
    aspect_ratio: str | None = None,
    timeout: float | None = None,
    on_retry=None,
    input_references: list[dict] | list[str] | None = None,
    visual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """สร้างรูป — retry strategy ตามประเภท error (parse error_type ไม่ใช่ keyword).

    content_policy: retry กับ provider เดิม (flaky — โอกาสผ่าน)
      - retry 1-2: ส่ง request เดิม (รอ content filter คลาย)
      - retry 3: แก้ prompt ด้วย LLM (กรณีลิขสิทธิ์ เช่น "Super Girl")
      - ครบ 3 ครั้งแล้วไม่ผ่าน → error ให้ user กดทีหลัง
    transient (rate limit / server error): retry กับ backoff
    other: ไม่ retry คืน error เลย

    on_retry: callback(old_prompt, new_prompt, error) สำหรับโชว์สถานะ
    input_references: รูปสินค้าจริงสำหรับ image-to-image (path หรือ dict)
    visual: dict จาก brand/visual.json — แป๊ะ keywords/colors/tone ต่อท้าย prompt
    คืน: เหมือน generate_image + เพิ่ม retry_count, original_prompt, retry_history
    """
    cfg = _load_media_config()
    mcfg = _media_cfg()
    retry_model = cfg.get("media_retry_model", mcfg.get("media_retry_model", "anthropic/claude-sonnet-4"))
    max_retries = int(cfg.get("max_content_policy_retries", 3))
    retry_delay = float(cfg.get("content_policy_retry_delay_seconds", 2.0))

    current_prompt = prompt
    attempt = 0
    retry_history: list[dict[str, str]] = []

    while True:
        result = generate_image(
            current_prompt, output_path,
            model=model, aspect_ratio=aspect_ratio, timeout=timeout,
            input_references=input_references, visual=visual,
            attempt=attempt + 1,
        )
        if result.get("ok"):
            if attempt > 0:
                result["retry_count"] = attempt
                result["original_prompt"] = prompt
                result["retry_history"] = retry_history
            return result

        error = result.get("error", "")
        error_type = result.get("error_type")
        error_code = result.get("error_code")
        http_status = result.get("http_status")
        category = _classify_media_error(error_type, error_code, http_status)

        # other → ไม่ retry
        if category == "other":
            result["retry_history"] = retry_history
            return result

        # ครบจำนวน retry แล้ว → คืน error ให้ user กดทีหลัง
        if attempt >= max_retries:
            result["retry_history"] = retry_history
            result["retry_count"] = attempt
            result["original_prompt"] = prompt
            return result

        attempt += 1

        # transient → retry กับ backoff (ไม่แก้ prompt)
        if category == "transient":
            time.sleep(retry_delay * attempt)
            retry_history.append({
                "attempt": attempt,
                "old_prompt": current_prompt,
                "new_prompt": current_prompt,
                "error": error,
                "strategy": "transient_retry",
            })
            if on_retry:
                on_retry(current_prompt, current_prompt, error)
            continue

        # content_policy → retry 1-2 ส่งเดิม, retry สุดท้ายแก้ prompt
        if attempt < max_retries:
            # retry แรกๆ — ส่ง request เดิม (content filter เป็น flaky)
            time.sleep(retry_delay)
            retry_history.append({
                "attempt": attempt,
                "old_prompt": current_prompt,
                "new_prompt": current_prompt,
                "error": error,
                "strategy": "flaky_retry",
            })
            if on_retry:
                on_retry(current_prompt, current_prompt, error)
            continue

        # retry สุดท้าย — ลองแก้ prompt ด้วย LLM (กรณีลิขสิทธิ์)
        if llm is not None:
            new_prompt = _rewrite_prompt_with_llm(
                current_prompt, error, "image", llm, retry_model,
            )
            if new_prompt:
                retry_history.append({
                    "attempt": attempt,
                    "old_prompt": current_prompt,
                    "new_prompt": new_prompt,
                    "error": error,
                    "strategy": "prompt_rewrite",
                })
                if on_retry:
                    on_retry(current_prompt, new_prompt, error)
                current_prompt = new_prompt
                continue

        # LLM แก้ไม่ได้ หรือไม่มี LLM → คืน error
        result["retry_history"] = retry_history
        result["retry_count"] = attempt - 1
        result["original_prompt"] = prompt
        return result


def generate_video_with_retry(
    prompt: str,
    output_path: Path,
    *,
    llm=None,
    model: str | None = None,
    duration: int | None = None,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    poll_interval: float | None = None,
    max_wait: float | None = None,
    on_status=None,
    on_retry=None,
    input_references: list[dict] | list[str] | None = None,
    frame_images: list[dict] | list[str] | None = None,
    visual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """สร้างวิดีโอ — retry strategy ตามประเภท error (parse error_type ไม่ใช่ keyword).

    content_policy: retry กับ provider เดิม (flaky — โอกาสผ่าน)
      - retry 1-2: ส่ง request เดิม (รอ content filter คลาย)
      - retry 3: แก้ prompt ด้วย LLM (กรณีลิขสิทธิ์)
      - ครบ 3 ครั้งแล้วไม่ผ่าน → error ให้ user กดทีหลัง
    transient (rate limit / server error): retry กับ backoff
    other: ไม่ retry คืน error เลย

    on_retry: callback(old_prompt, new_prompt, error) สำหรับโชว์สถานะ
    on_status: callback(status_str) สำหรับโชว์ progress
    input_references: รูปสินค้าจริงสำหรับ reference-to-video
    frame_images: รูปสินค้าจริงสำหรับ image-to-video (first/last frame)
    visual: dict จาก brand/visual.json — แป๊ะ keywords/colors/tone ต่อท้าย prompt
    คืน: เหมือน generate_video + เพิ่ม retry_count, original_prompt, retry_history
    """
    cfg = _load_media_config()
    mcfg = _media_cfg()
    retry_model = cfg.get("media_retry_model", mcfg.get("media_retry_model", "anthropic/claude-sonnet-4"))
    max_retries = int(cfg.get("max_content_policy_retries", 3))
    retry_delay = float(cfg.get("content_policy_retry_delay_seconds", 2.0))

    current_prompt = prompt
    attempt = 0
    retry_history: list[dict[str, str]] = []

    while True:
        result = generate_video(
            current_prompt, output_path,
            model=model, duration=duration, aspect_ratio=aspect_ratio,
            resolution=resolution, poll_interval=poll_interval,
            max_wait=max_wait, on_status=on_status,
            input_references=input_references, frame_images=frame_images,
            visual=visual,
            attempt=attempt + 1,
        )
        if result.get("ok"):
            if attempt > 0:
                result["retry_count"] = attempt
                result["original_prompt"] = prompt
                result["retry_history"] = retry_history
            return result

        error = result.get("error", "")
        error_type = result.get("error_type")
        error_code = result.get("error_code")
        http_status = result.get("http_status")
        category = _classify_media_error(error_type, error_code, http_status)

        # other → ไม่ retry
        if category == "other":
            result["retry_history"] = retry_history
            return result

        # ครบจำนวน retry แล้ว → คืน error ให้ user กดทีหลัง
        if attempt >= max_retries:
            result["retry_history"] = retry_history
            result["retry_count"] = attempt
            result["original_prompt"] = prompt
            return result

        attempt += 1

        # transient → retry กับ backoff (ไม่แก้ prompt)
        if category == "transient":
            time.sleep(retry_delay * attempt)
            retry_history.append({
                "attempt": attempt,
                "old_prompt": current_prompt,
                "new_prompt": current_prompt,
                "error": error,
                "strategy": "transient_retry",
            })
            if on_retry:
                on_retry(current_prompt, current_prompt, error)
            if on_status:
                on_status(f"retry {attempt}: server busy รอแล้วลองใหม่")
            continue

        # content_policy → retry 1-2 ส่งเดิม, retry สุดท้ายแก้ prompt
        if attempt < max_retries:
            # retry แรกๆ — ส่ง request เดิม (content filter เป็น flaky)
            time.sleep(retry_delay)
            retry_history.append({
                "attempt": attempt,
                "old_prompt": current_prompt,
                "new_prompt": current_prompt,
                "error": error,
                "strategy": "flaky_retry",
            })
            if on_retry:
                on_retry(current_prompt, current_prompt, error)
            if on_status:
                on_status(f"retry {attempt}: content filter รอแล้วลองใหม่")
            continue

        # retry สุดท้าย — ลองแก้ prompt ด้วย LLM (กรณีลิขสิทธิ์)
        if llm is not None:
            new_prompt = _rewrite_prompt_with_llm(
                current_prompt, error, "video", llm, retry_model,
            )
            if new_prompt:
                retry_history.append({
                    "attempt": attempt,
                    "old_prompt": current_prompt,
                    "new_prompt": new_prompt,
                    "error": error,
                    "strategy": "prompt_rewrite",
                })
                if on_retry:
                    on_retry(current_prompt, new_prompt, error)
                if on_status:
                    on_status(f"retry {attempt}: แก้ prompt แล้วลองใหม่")
                current_prompt = new_prompt
                continue

        # LLM แก้ไม่ได้ หรือไม่มี LLM → คืน error
        result["retry_history"] = retry_history
        result["retry_count"] = attempt - 1
        result["original_prompt"] = prompt
        return result


# ---------------------------------------------------------------------------
# Retry history — เก็บประวัติการ retry ลงไฟล์ใน session เพื่อดูผ่านเว็บ
# ---------------------------------------------------------------------------

def save_retry_history(
    session_dir: Path,
    media_type: str,
    filename: str,
    result: dict[str, Any],
) -> None:
    """บันทึกประวัติการ retry ของ media 1 ไฟล์ ลง _media_retry_log.json.

    session_dir: path ของ session (เช่น output/14_ส.ค._...)
    media_type: "image" หรือ "video"
    filename: ชื่อไฟล์ที่สร้าง (เช่น video_1.mp4)
    result: result จาก generate_with_retry หรือ generate_*
    """
    log_file = session_dir / "_media_retry_log.json"
    try:
        # อ่าน log เดิม
        existing: list[dict[str, Any]] = []
        if log_file.exists():
            existing = json.loads(log_file.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []

        # เพิ่ม entry ใหม่
        entry: dict[str, Any] = {
            "media_type": media_type,
            "filename": filename,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ok": result.get("ok", False),
            "error": result.get("error", ""),
            "retry_count": result.get("retry_count", 0),
            "original_prompt": result.get("original_prompt", ""),
            "final_prompt": result.get("prompt", ""),
            "retry_history": result.get("retry_history", []),
            "warnings": result.get("warnings", []),
        }
        existing.append(entry)

        log_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # ไม่ให้ log error ทำลาย flow หลัก


def load_retry_history(session_dir: Path) -> list[dict[str, Any]]:
    """อ่านประวัติ retry ของ session — คืน list ของ entries."""
    log_file = session_dir / "_media_retry_log.json"
    try:
        if log_file.exists():
            data = json.loads(log_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Parser — แยก image/video prompts จาก content_creator output
# ---------------------------------------------------------------------------

def parse_media_prompts(content: str) -> dict[str, list[dict[str, str]]]:
    """Parse content_creator output แยก image + video prompts.

    คืน: {"images": [{prompt, usage, aspect_ratio?}], "videos": [{prompt, usage, duration?, aspect_ratio?, resolution?}]}

    รองรับ 2 รูปแบบ:
    1. JSON (structured output ใหม่): ถ้า content เป็น JSON ที่มี field "posts"
       → อ่าน image_prompts/video_prompts จาก structure โดยตรง (ไม่ต้อง regex)
    2. Markdown (เดิม): ถ้าไม่ใช่ JSON → ใช้ regex parser เหมือนเดิม
       รองรับหลายรูปแบบ heading: ## 3. Prompt สำหรับ Gen Image, ## 3. Image Prompts, etc.
       รองรับ output แบบ "1 โพสต์ = 1 prompt" ที่ใช้ field names (scene description, camera movement, ...)
       ดึง duration, aspect_ratio, resolution ออกมาด้วยถ้ามี
    """
    # --- Path 1: JSON (structured output) ---
    warnings: list[str] = []
    try:
        import json as _json
        parsed = _json.loads(content)
        if isinstance(parsed, dict) and "posts" in parsed:
            images: list[dict[str, str]] = []
            videos: list[dict[str, str]] = []
            for post in parsed.get("posts", []):
                # asset_ids ของโพสต์ — ติดไปกับทุก image/video item
                # (media_gen ใช้ดึงรูป asset เป็น input_references)
                if not isinstance(post, dict):
                    warnings.append(f"expected post dict, got {type(post).__name__}")
                    continue
                post_asset_ids = post.get("asset_ids", [])
                for ip in post.get("image_prompts", []):
                    if not isinstance(ip, dict):
                        warnings.append(f"expected image prompt dict, got {type(ip).__name__}")
                        continue
                    p = ip.get("prompt", "").strip()
                    if p:
                        img_item = {"prompt": p}
                        if ip.get("aspect_ratio"):
                            img_item["aspect_ratio"] = ip["aspect_ratio"]
                        if ip.get("resolution"):
                            img_item["resolution"] = ip["resolution"]
                        if post_asset_ids:
                            img_item["asset_ids"] = post_asset_ids
                        images.append(img_item)
                for vp in post.get("video_prompts", []):
                    if not isinstance(vp, dict):
                        warnings.append(f"expected video prompt dict, got {type(vp).__name__}")
                        continue
                    p = vp.get("prompt", "").strip()
                    if p:
                        vid_item = {"prompt": p}
                        if vp.get("duration"):
                            vid_item["duration"] = vp["duration"]
                        if vp.get("aspect_ratio"):
                            vid_item["aspect_ratio"] = vp["aspect_ratio"]
                        if vp.get("resolution"):
                            vid_item["resolution"] = vp["resolution"]
                        if post_asset_ids:
                            vid_item["asset_ids"] = post_asset_ids
                        videos.append(vid_item)
            return {"images": images, "videos": videos, "warnings": warnings}
    except (_json.JSONDecodeError, TypeError, ValueError):
        pass

    # --- Path 2: Markdown (regex parser — สำหรับไฟล์เก่า) ---
    images = []
    videos = []

    # field names ที่เป็นส่วนประกอบของ prompt เดียว — ไม่ใช่ prompt แยก
    FIELD_NAMES = {
        "scene description", "camera movement", "duration", "mood",
        "subject", "style", "lighting", "composition", "usage",
        "scene", "camera", "setting", "action", "dialogue",
        "aspect ratio", "resolution", "prompt",
    }

    # regex รองรับทั้ง **field:** value (colon ใน bold) และ **field** — value (colon นอก bold)
    _FIELD_RE = re.compile(r"^-\s*\*\*([^*:]+?):?\s*\*\*\s*[:\-—]?\s*(.*)$")

    def is_field_name(line: str) -> bool:
        """เช็คว่าบรรทัดนี้เป็น field name เช่น '- **scene description:** ...'"""
        m = _FIELD_RE.match(line.strip())
        if not m:
            return False
        name = m.group(1).strip().lower()
        return any(name.startswith(fn) or fn in name for fn in FIELD_NAMES if fn != "prompt")

    def is_prompt_field(line: str) -> bool:
        """เช็คว่าบรรทัดนี้เป็น '- **Prompt:** ...' (field หลักที่เก็บ prompt text)"""
        m = _FIELD_RE.match(line.strip())
        if not m:
            return False
        return m.group(1).strip().lower().startswith("prompt")

    def extract_field_value(line: str) -> str:
        """แยก value จาก '- **field name:** value' หรือ '- **field name** — value'"""
        m = _FIELD_RE.match(line.strip())
        if m:
            return m.group(2).strip()
        return line.strip()

    def extract_field_name(line: str) -> str:
        """แยก field name จาก '- **field name:** value'"""
        m = _FIELD_RE.match(line.strip())
        if m:
            return m.group(1).strip().lower()
        return ""

    def parse_duration(value: str) -> int | None:
        """แปลง '15 seconds', '15s', '15 วินาที' → 15"""
        m = re.search(r"(\d+)\s*(?:s|sec|second|วินาที)", value, re.IGNORECASE)
        if m:
            return int(m.group(1))
        # ถ้าเป็นแค่ตัวเลข
        m = re.search(r"^\s*(\d+)\s*$", value)
        if m:
            return int(m.group(1))
        return None

    def parse_aspect_ratio(value: str) -> str | None:
        """แปลง '9:16 (vertical)', '9:16' → '9:16'"""
        m = re.search(r"(\d+:\d+)", value)
        if m:
            return m.group(1)
        return None

    def parse_resolution(value: str) -> str | None:
        """แปลง '720p', '1080p', '4K' → '720p'"""
        m = re.search(r"(\d+p|4k|2k)", value, re.IGNORECASE)
        if m:
            return m.group(1).lower()
        return None

    # แบ่งตาม heading ## ทั้มด
    sections = re.split(r"\n(?=##\s)", content)

    for section in sections:
        header_line = section.split("\n", 1)[0].lower()
        body = section.split("\n", 1)[1] if "\n" in section else ""

        is_image = any(k in header_line for k in ["gen image", "image prompt", "prompt สำหรับ gen image"])
        is_video = any(k in header_line for k in ["gen video", "video prompt", "prompt สำหรับ gen video"])

        if not is_image and not is_video:
            continue

        # ตรวจว่า section นี้ใช้ field names (แบบ 1 prompt ต่อ 1 โพสต์) หรือหลาย prompts
        body_lines = [l for l in body.split("\n") if l.strip()]
        field_count = sum(1 for l in body_lines if is_field_name(l))

        if field_count >= 2:
            # แบบ field names — ถ้ามี - **Prompt:** ... ให้ใช้ค่านั้นเป็น prompt หลัก
            # ถ้าไม่มี ให้รวม scene description + camera movement + mood เป็น prompt
            main_prompt = ""
            field_parts = []
            current_usage = ""
            current_duration: int | None = None
            current_aspect: str | None = None
            current_resolution: str | None = None
            for line in body_lines:
                if is_prompt_field(line):
                    # - **Prompt:** <prompt text ยาวๆ> → ใช้เป็น prompt หลัก
                    val = extract_field_value(line)
                    if val and len(val) > 10:
                        main_prompt = val
                elif is_field_name(line):
                    field_lower = extract_field_name(line)
                    val = extract_field_value(line)
                    # เช็คแต่ละ field แบบเจาะจง
                    if "usage" in field_lower or "ใช้ที่" in field_lower or "placement" in field_lower:
                        current_usage = val
                    elif "duration" in field_lower:
                        d = parse_duration(val)
                        if d:
                            current_duration = d
                    elif "aspect ratio" in field_lower or "aspect_ratio" in field_lower:
                        ar = parse_aspect_ratio(val)
                        if ar:
                            current_aspect = ar
                    elif "resolution" in field_lower:
                        r = parse_resolution(val)
                        if r:
                            current_resolution = r
                    else:
                        # scene description, camera movement, mood, etc.
                        # ถ้าค่าว่าง (เช่น **Scene description:** ไม่มีค่าในบรรทัดเดียวกัน)
                        # ให้อ่านบรรทัด indented ถัดไป
                        if not val:
                            # หาบรรทัด indented ถัดไป
                            idx = body_lines.index(line)
                            sub_lines = []
                            for next_line in body_lines[idx + 1:]:
                                if next_line.strip() and not next_line.startswith(" ") and not next_line.startswith("\t"):
                                    break
                                if next_line.strip():
                                    sub_lines.append(next_line.strip())
                            if sub_lines:
                                val = " ".join(sub_lines)
                        if val and len(val) > 10:
                            field_parts.append(val)
            # ใช้ main_prompt ถ้ามี ไม่งั้นรวม field_parts
            prompt_text = main_prompt if main_prompt else ". ".join(field_parts)
            if prompt_text:
                entry: dict[str, Any] = {"prompt": prompt_text, "usage": current_usage}
                if current_duration:
                    entry["duration"] = current_duration
                if current_aspect:
                    entry["aspect_ratio"] = current_aspect
                if current_resolution:
                    entry["resolution"] = current_resolution
                if is_image:
                    images.append(entry)
                elif is_video:
                    videos.append(entry)
            continue

        # แบบหลาย prompts — แยกตาม ### หรือ 1. 2. 3.
        sub_sections = re.split(r"\n(?=###\s|\d+\.\s|\*\*\d+\.\s)", body)

        current_usage = ""
        for sub in sub_sections:
            sub_stripped = sub.strip()
            if not sub_stripped:
                continue

            # ข้ามบรรทัดที่เป็น "หมายเหตุ" / "note"
            first_line = sub_stripped.split("\n", 1)[0].lower()
            if "หมายเหตุ" in first_line or first_line.startswith("note") or "**note**" in first_line:
                continue

            # หา usage
            usage_match = re.search(
                r"(ใช้ที่|use(?:d)?(?:\s+at|for)?|สำหรับ|placement)\s*[:\-]?\s*(.+?)(?:\n|$)",
                sub_stripped,
                re.IGNORECASE,
            )
            if usage_match:
                current_usage = usage_match.group(2).strip()

            # หา duration, aspect_ratio, resolution ใน sub-section
            sub_duration: int | None = None
            sub_aspect: str | None = None
            sub_resolution: str | None = None
            for line in sub_stripped.split("\n"):
                line_lower = line.strip().lower()
                if "duration" in line_lower:
                    d = parse_duration(extract_field_value(line))
                    if d:
                        sub_duration = d
                elif "aspect ratio" in line_lower or "aspect_ratio" in line_lower:
                    ar = parse_aspect_ratio(extract_field_value(line))
                    if ar:
                        sub_aspect = ar
                elif "resolution" in line_lower:
                    r = parse_resolution(extract_field_value(line))
                    if r:
                        sub_resolution = r

            # หา prompt
            prompt_match = re.search(
                r"(?:prompt\s*[:\-]\s*\**\s*|>\s*)([A-Za-z][^\n]{40,})",
                sub_stripped,
                re.IGNORECASE,
            )
            if prompt_match:
                prompt_text = prompt_match.group(1).strip()
            else:
                # fallback: หาบรรทัดภาษาอังกฤษ
                en_lines = []
                for line in sub_stripped.split("\n"):
                    s = line.strip()
                    if len(s) <= 40 or s.startswith("#") or s.startswith("**"):
                        continue
                    alpha_chars = sum(1 for c in s if c.isalpha())
                    ascii_alpha = sum(1 for c in s if c.isascii() and c.isalpha())
                    if alpha_chars > 0 and ascii_alpha / alpha_chars >= 0.7:
                        en_lines.append(s)
                prompt_text = en_lines[0] if en_lines else ""

            if prompt_text and len(prompt_text) > 30:
                entry = {"prompt": prompt_text, "usage": current_usage}
                if sub_duration:
                    entry["duration"] = sub_duration
                if sub_aspect:
                    entry["aspect_ratio"] = sub_aspect
                if sub_resolution:
                    entry["resolution"] = sub_resolution
                if is_image:
                    images.append(entry)
                elif is_video:
                    videos.append(entry)

    return {"images": images, "videos": videos, "warnings": warnings}
