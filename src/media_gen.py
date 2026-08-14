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
    from .ai_usage import log_ai_usage, make_entry
except ImportError:
    from ai_usage import log_ai_usage, make_entry  # type: ignore


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


# Default models — override ได้ใน config/media.yaml
DEFAULT_IMAGE_MODEL = "google/gemini-3.1-flash-image"
DEFAULT_VIDEO_MODEL = "bytedance/seedance-2.0-fast"


def _load_media_config() -> dict[str, Any]:
    """อ่าน config/media.yaml — มีไม่มีก็ได้ ใช้ default ถ้าไม่มี."""
    cfg_path = Path("config/media.yaml")
    if not cfg_path.exists():
        return {}
    try:
        with cfg_path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


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
_CAPABILITIES_CACHE_TTL = 3600  # 1 ชม.


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
        if age < _CAPABILITIES_CACHE_TTL:
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
        with httpx.Client(timeout=30) as client:
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
# Image generation
# ---------------------------------------------------------------------------

def generate_image(
    prompt: str,
    output_path: Path,
    *,
    model: str | None = None,
    aspect_ratio: str = "16:9",
    timeout: float = 180,
    input_references: list[dict] | list[str] | None = None,
) -> dict[str, Any]:
    """สร้างรูปจาก prompt — เซฟลง output_path แล้วคืน metadata.

    ก่อนส่ง API จะเช็ค + clamp aspect_ratio กับ capabilities ของ model
    input_references: list ของ dict (API format) หรือ list ของ path รูปจริง
        - ถ้าเป็น path (str) → แปลงเป็น base64 data URL อัตโนมัติ
        - ใช้เป็น reference image สำหรับ image-to-image generation
    คืน: {ok, path, model, prompt, error?, warnings?}
    """
    cfg = _load_media_config()
    model = model or cfg.get("image_model", DEFAULT_IMAGE_MODEL)
    api_key = _get_api_key()

    output_path.parent.mkdir(parents=True, exist_ok=True)

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
    # n: จำนวนภาพ — default 1
    n = cfg.get("image_n", 1)
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
        _log_media_usage("image", model, data.get("usage"), duration_ms=int((time.time() - t0) * 1000))

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
        _log_media_usage("image", model, None, duration_ms=int((time.time() - t0) * 1000), status="error", error_message=str(e))
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}", "model": model, "prompt": prompt, "warnings": warnings}
    except Exception as e:
        _log_media_usage("image", model, None, duration_ms=int((time.time() - t0) * 1000), status="error", error_message=str(e))
        return {"ok": False, "error": str(e), "model": model, "prompt": prompt, "warnings": warnings}


# ---------------------------------------------------------------------------
# Video generation (async — submit, poll, download)
# ---------------------------------------------------------------------------

def generate_video(
    prompt: str,
    output_path: Path,
    *,
    model: str | None = None,
    duration: int = 5,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    poll_interval: float = 5.0,
    max_wait: float = 600.0,
    on_status=None,
    input_references: list[dict] | list[str] | None = None,
    frame_images: list[dict] | list[str] | None = None,
) -> dict[str, Any]:
    """สร้างวิดีโอจาก prompt — async รอจนเสร็จ — เซฟลง output_path.

    ก่อนส่ง API จะเช็ค + clamp duration/aspect_ratio/resolution กับ capabilities ของ model
    on_status: callback(status_str) สำหรับโชว์ progress
    input_references: list ของ path รูปจริง (str) หรือ dict (API format)
        - ใช้เป็น reference สำหรับ reference-to-video (style/subject guidance)
    frame_images: list ของ path รูปจริง (str) หรือ dict (API format)
        - ใช้เป็น first_frame / last_frame สำหรับ image-to-video
        - ถ้าเป็น str → ใช้เป็น first_frame อัตโนมัติ
    คืน: {ok, path, model, prompt, error?, warnings?}
    """
    cfg = _load_media_config()
    model = model or cfg.get("video_model", DEFAULT_VIDEO_MODEL)
    api_key = _get_api_key()

    output_path.parent.mkdir(parents=True, exist_ok=True)

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

    t0 = time.time()
    try:
        if on_status:
            on_status("submitting")
        with httpx.Client(timeout=60) as client:
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
            _log_media_usage("video", model, None, duration_ms=int((time.time() - t0) * 1000), status="error", error_message="no job_id")
            return {"ok": False, "error": f"API ไม่คืน job_id: {result}", "model": model, "prompt": prompt}

        # Poll จนเสร็จ
        elapsed = 0.0
        with httpx.Client(timeout=60) as client:
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
                        _log_media_usage("video", model, status_data.get("usage"), duration_ms=int((time.time() - t0) * 1000), status="error", error_message="completed but no url")
                        return {"ok": False, "error": "completed แต่ไม่มี url", "model": model, "prompt": prompt, "warnings": warnings}
                    # log usage จาก poll response
                    _log_media_usage("video", model, status_data.get("usage"), duration_ms=int((time.time() - t0) * 1000))
                    # download — ส่ง Authorization header เหมือนโค้ดที่ใช้งานได้ใน my-agent-app
                    if on_status:
                        on_status("downloading")
                    video_resp = httpx.get(
                        urls[0],
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=120,
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
                    _log_media_usage("video", model, status_data.get("usage"), duration_ms=int((time.time() - t0) * 1000), status="error", error_message=str(err))
                    return {"ok": False, "error": f"video gen failed: {err}", "model": model, "prompt": prompt, "warnings": warnings}

        _log_media_usage("video", model, None, duration_ms=int((time.time() - t0) * 1000), status="timeout", error_message=f"timeout after {max_wait}s")
        return {"ok": False, "error": f"timeout after {max_wait}s", "model": model, "prompt": prompt, "warnings": warnings}
    except httpx.HTTPStatusError as e:
        _log_media_usage("video", model, None, duration_ms=int((time.time() - t0) * 1000), status="error", error_message=str(e))
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}", "model": model, "prompt": prompt, "warnings": warnings}
    except Exception as e:
        _log_media_usage("video", model, None, duration_ms=int((time.time() - t0) * 1000), status="error", error_message=str(e))
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
    status: str = "success",
    error_message: str | None = None,
) -> None:
    """ยิง log ไป AI Usage Hub สำหรับ image/video gen — fire-and-forget."""
    operation = "images.generate" if media_type == "image" else "videos.generate"
    entry = make_entry(
        provider="openrouter",
        model=model,
        operation=operation,
        source=f"media_gen.generate_{media_type}",
        duration_ms=duration_ms,
        status=status,
        error_message=error_message,
    )
    if usage:
        # OpenRouter images/videos response อาจมี cost ใน usage หรือ top-level
        cost = usage.get("cost") or usage.get("total_cost")
        if cost is not None:
            entry["cost_usd"] = float(cost)
        if usage.get("prompt_tokens") is not None:
            entry["prompt_tokens"] = usage.get("prompt_tokens")
        if usage.get("completion_tokens") is not None:
            entry["completion_tokens"] = usage.get("completion_tokens")
        entry["raw_usage"] = usage
    log_ai_usage(entry)


# ---------------------------------------------------------------------------
# Retry-on-rejection — ถ้า media gen ถูก reject ให้ LLM แก้ prompt แล้วลองใหม่
# ---------------------------------------------------------------------------

# คำสำคัญใน error ที่บอกว่าเป็น rejection ที่แก้ได้ด้วยการเขียน prompt ใหม่
_REJECT_KEYWORDS = [
    "copyright", "trademark", "intellectual property", "ip policy",
    "safety", "content policy", "content filter", "blocked",
    "prohibited", "violation", "inappropriate", "nsfw",
    "celebrity", "public figure", "real person",
    "brand", "logo",
]


def _is_rejectable_error(error: str) -> bool:
    """เช็คว่า error นี้แก้ได้ด้วยการเขียน prompt ใหม่ไหม."""
    err_lower = (error or "").lower()
    return any(kw in err_lower for kw in _REJECT_KEYWORDS)


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
        new_prompt = llm.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            model=model,
            temperature=0.3,
            max_tokens=1024,
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
    aspect_ratio: str = "16:9",
    timeout: float = 180,
    on_retry=None,
    input_references: list[dict] | list[str] | None = None,
) -> dict[str, Any]:
    """สร้างรูป — ถ้าถูก reject ให้ LLM แก้ prompt แล้วลองใหม่ ไม่จำกัดจำนวนครั้ง.

    หยุดเฉพาะเมื่อ: สำเร็จ / ไม่ใช่ reject error / LLM แก้ prompt ไม่ได้
    on_retry: callback(old_prompt, new_prompt, error) สำหรับโชว์สถานะ
    input_references: รูปสินค้าจริงสำหรับ image-to-image (path หรือ dict)
    คืน: เหมือน generate_image + เพิ่ม retry_count, original_prompt, retry_history
    """
    cfg = _load_media_config()
    retry_model = cfg.get("media_retry_model", "anthropic/claude-sonnet-4")

    current_prompt = prompt
    attempt = 0
    retry_history: list[dict[str, str]] = []

    while True:
        result = generate_image(
            current_prompt, output_path,
            model=model, aspect_ratio=aspect_ratio, timeout=timeout,
            input_references=input_references,
        )
        if result.get("ok"):
            if attempt > 0:
                result["retry_count"] = attempt
                result["original_prompt"] = prompt
                result["retry_history"] = retry_history
            return result

        error = result.get("error", "")

        # ถ้าไม่ใช่ rejection ที่แก้ได้ หรือไม่มี LLM → คืน error เลย
        if not _is_rejectable_error(error) or llm is None:
            result["retry_history"] = retry_history
            return result

        # ลองแก้ prompt
        new_prompt = _rewrite_prompt_with_llm(
            current_prompt, error, "image", llm, retry_model,
        )
        if not new_prompt:
            result["retry_history"] = retry_history
            return result  # LLM แก้ไม่ได้ → คืน error เดิม

        # เก็บประวัติก่อนเปลี่ยน
        retry_history.append({
            "attempt": attempt + 1,
            "old_prompt": current_prompt,
            "new_prompt": new_prompt,
            "error": error,
        })

        if on_retry:
            on_retry(current_prompt, new_prompt, error)

        current_prompt = new_prompt
        attempt += 1


def generate_video_with_retry(
    prompt: str,
    output_path: Path,
    *,
    llm=None,
    model: str | None = None,
    duration: int = 5,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    poll_interval: float = 5.0,
    max_wait: float = 600.0,
    on_status=None,
    on_retry=None,
    input_references: list[dict] | list[str] | None = None,
    frame_images: list[dict] | list[str] | None = None,
) -> dict[str, Any]:
    """สร้างวิดีโอ — ถ้าถูก reject ให้ LLM แก้ prompt แล้วลองใหม่ ไม่จำกัดจำนวนครั้ง.

    หยุดเฉพาะเมื่อ: สำเร็จ / ไม่ใช่ reject error / LLM แก้ prompt ไม่ได้
    on_retry: callback(old_prompt, new_prompt, error) สำหรับโชว์สถานะ
    input_references: รูปสินค้าจริงสำหรับ reference-to-video
    frame_images: รูปสินค้าจริงสำหรับ image-to-video (first/last frame)
    คืน: เหมือน generate_video + เพิ่ม retry_count, original_prompt, retry_history
    """
    cfg = _load_media_config()
    retry_model = cfg.get("media_retry_model", "anthropic/claude-sonnet-4")

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
        )
        if result.get("ok"):
            if attempt > 0:
                result["retry_count"] = attempt
                result["original_prompt"] = prompt
                result["retry_history"] = retry_history
            return result

        error = result.get("error", "")

        # ถ้าไม่ใช่ rejection ที่แก้ได้ หรือไม่มี LLM → คืน error เลย
        if not _is_rejectable_error(error) or llm is None:
            result["retry_history"] = retry_history
            return result

        # ลองแก้ prompt
        new_prompt = _rewrite_prompt_with_llm(
            current_prompt, error, "video", llm, retry_model,
        )
        if not new_prompt:
            result["retry_history"] = retry_history
            return result  # LLM แก้ไม่ได้ → คืน error เดิม

        # เก็บประวัติก่อนเปลี่ยน
        retry_history.append({
            "attempt": attempt + 1,
            "old_prompt": current_prompt,
            "new_prompt": new_prompt,
            "error": error,
        })

        if on_retry:
            on_retry(current_prompt, new_prompt, error)

        if on_status:
            on_status(f"retry {attempt+1}: แก้ prompt แล้วลองใหม่")

        current_prompt = new_prompt
        attempt += 1


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
    รองรับหลายรูปแบบ heading: ## 3. Prompt สำหรับ Gen Image, ## 3. Image Prompts, etc.
    รองรับ output แบบ "1 โพสต์ = 1 prompt" ที่ใช้ field names (scene description, camera movement, ...)
    ดึง duration, aspect_ratio, resolution ออกมาด้วยถ้ามี
    """
    images: list[dict[str, str]] = []
    videos: list[dict[str, str]] = []

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

    return {"images": images, "videos": videos}
