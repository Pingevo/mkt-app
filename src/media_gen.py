"""Media generation — image + video via OpenRouter dedicated APIs.

Image API: POST /api/v1/images (synchronous, returns base64)
Video API: POST /api/v1/videos (async — submit → poll → download)

ใช้ API key ตัวเดียวกับ LLM client — ผ่าน OpenRouter ทั้งหมด
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

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
# Image generation
# ---------------------------------------------------------------------------

def generate_image(
    prompt: str,
    output_path: Path,
    *,
    model: str | None = None,
    aspect_ratio: str = "16:9",
    timeout: float = 180,
) -> dict[str, Any]:
    """สร้างรูปจาก prompt — เซฟลง output_path แล้วคืน metadata.

    คืน: {ok, path, model, prompt, error?}
    """
    cfg = _load_media_config()
    model = model or cfg.get("image_model", DEFAULT_IMAGE_MODEL)
    api_key = _get_api_key()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
    }
    # n: จำนวนภาพ — default 1
    n = cfg.get("image_n", 1)
    if n > 1:
        payload["n"] = n

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                "https://openrouter.ai/api/v1/images",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

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
        }
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}", "model": model, "prompt": prompt}
    except Exception as e:
        return {"ok": False, "error": str(e), "model": model, "prompt": prompt}


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
) -> dict[str, Any]:
    """สร้างวิดีโอจาก prompt — async รอจนเสร็จ — เซฟลง output_path.

    on_status: callback(status_str) สำหรับโชว์ progress
    คืน: {ok, path, model, prompt, error?}
    """
    cfg = _load_media_config()
    model = model or cfg.get("video_model", DEFAULT_VIDEO_MODEL)
    api_key = _get_api_key()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "duration": duration,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

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
                        return {"ok": False, "error": "completed แต่ไม่มี url", "model": model, "prompt": prompt}
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
                    }
                elif status == "failed":
                    err = status_data.get("error", "unknown")
                    return {"ok": False, "error": f"video gen failed: {err}", "model": model, "prompt": prompt}

        return {"ok": False, "error": f"timeout after {max_wait}s", "model": model, "prompt": prompt}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}", "model": model, "prompt": prompt}
    except Exception as e:
        return {"ok": False, "error": str(e), "model": model, "prompt": prompt}


# ---------------------------------------------------------------------------
# Parser — แยก image/video prompts จาก content_creator output
# ---------------------------------------------------------------------------

def parse_media_prompts(content: str) -> dict[str, list[dict[str, str]]]:
    """Parse content_creator output แยก image + video prompts.

    คืน: {"images": [{prompt, usage}], "videos": [{prompt, usage}]}
    รองรับหลายรูปแบบ heading: ## 3. Prompt สำหรับ Gen Image, ## 3. Image Prompts, etc.
    รองรับ output แบบ "1 โพสต์ = 1 prompt" ที่ใช้ field names (scene description, camera movement, ...)
    """
    images: list[dict[str, str]] = []
    videos: list[dict[str, str]] = []

    # field names ที่เป็นส่วนประกอบของ prompt เดียว — ไม่ใช่ prompt แยก
    FIELD_NAMES = {
        "scene description", "camera movement", "duration", "mood",
        "subject", "style", "lighting", "composition", "usage",
        "scene", "camera", "setting", "action", "dialogue",
    }

    def is_field_name(line: str) -> bool:
        """เช็คว่าบรรทัดนี้เป็น field name เช่น '- **scene description** — ...'"""
        s = line.strip().lower()
        # ลบ prefix '- **' และ suffix '** —'
        s = re.sub(r"^-\s*\*\*", "", s).strip()
        for fn in FIELD_NAMES:
            if s.startswith(fn):
                return True
        return False

    def extract_field_value(line: str) -> str:
        """แยก value จาก '- **field name** — value'"""
        # หา pattern: **field** — value หรือ **field**: value
        m = re.match(r"^-\s*\*\*[^*]+\*\*\s*[:\-—]\s*(.+)$", line.strip())
        if m:
            return m.group(1).strip()
        return line.strip()

    # แบ่งตาม heading ## ทั้งหมด
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
            # แบบ field names — รวมทุก field เป็น prompt เดียว
            field_parts = []
            current_usage = ""
            for line in body_lines:
                if is_field_name(line):
                    field_lower = line.strip().lower()
                    # เช็ค usage field
                    if "usage" in field_lower or "ใช้ที่" in field_lower or "placement" in field_lower:
                        current_usage = extract_field_value(line)
                    else:
                        val = extract_field_value(line)
                        if val and len(val) > 10:
                            field_parts.append(val)
            if field_parts:
                prompt_text = ". ".join(field_parts)
                entry = {"prompt": prompt_text, "usage": current_usage}
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
                if is_image:
                    images.append(entry)
                elif is_video:
                    videos.append(entry)

    return {"images": images, "videos": videos}
