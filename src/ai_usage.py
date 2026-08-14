"""AI Usage Hub — fire-and-forget logging ไปยัง central hub (digital.in.th).

ทุกครั้งที่เรียก AI/scraping provider จริง ให้เรียก ``log_ai_usage()`` หลังได้ response
กลับมา (รวม error path) — ฟังก์ชันนี้ไม่มีวัน throw ออกมาทำลาย flow หลัก

อ่านเอกสารเต็ม: AI_USAGE_HUB_DEVELOPER_API.md
"""

from __future__ import annotations

import threading
import time
from typing import Any

try:
    import httpx
    _HTTPX_OK = True
except ImportError:
    _HTTPX_OK = False

try:
    from .config_loader import get_env
except ImportError:
    # กรณี import ตรงจาก src/ โดยไม่ผ่าน package
    from config_loader import get_env  # type: ignore


_DEFAULT_URL = "https://digital.in.th"


def _read_cfg() -> tuple[str | None, str | None]:
    """คืน (url, token) — ถ้ายังไม่ตั้งค่า คืน (None, None) แล้วข้ามเงียบๆ."""
    url = get_env("AI_USAGE_HUB_URL", _DEFAULT_URL)
    token = get_env("AI_USAGE_HUB_TOKEN")
    return url, token


def log_ai_usage(entry: dict[str, Any]) -> None:
    """ยิง log 1 event ไป AI Usage Hub — fire-and-forget.

    ไม่มีวัน throw ออกมา — ถ้ายิงไม่สำเร็จ แค่ปล่อยผ่าน
    ถ้ายังไม่ตั้งค่า ``AI_USAGE_HUB_TOKEN`` จะข้ามเงียบๆ (return เลย)

    field บังคับใน entry: ``provider``
    field แนะนำ: model, operation, source, user, reference,
                 prompt_tokens, completion_tokens, cost_usd,
                 duration_ms, status, error_message
    """
    url, token = _read_cfg()
    if not url or not token:
        return  # ยังไม่ตั้งค่า — ข้ามเงียบๆ

    if not entry.get("provider"):
        return  # ไม่มี provider ไม่ยิง

    # ค่า default
    payload = {
        "environment": get_env("AI_USAGE_HUB_ENV", "production"),
        "attempt": 1,
        "status": "success",
    }
    payload.update(entry)

    # ยิงใน background thread เพื่อไม่ block caller
    t = threading.Thread(
        target=_post,
        args=(url.rstrip("/") + "/internal/ai-usage/logs", token, payload),
        daemon=True,
    )
    t.start()


def _post(endpoint: str, token: str, payload: dict[str, Any]) -> None:
    """POST จริง — ครอบ try/except ทุกกรณี."""
    if not _HTTPX_OK:
        return
    try:
        with httpx.Client(timeout=5) as client:
            client.post(
                endpoint,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-service-token": token,
                },
            )
    except Exception:
        pass  # ไม่ให้ log error ทำลาย flow หลัก


def log_ai_usage_sync(entry: dict[str, Any]) -> None:
    """เหมือน ``log_ai_usage`` แต่ยิง synchronous (รอผล) — ใช้เฉพาะตอน debug.

    ปกติใช้ ``log_ai_usage`` แบบ fire-and-forget พอ
    """
    url, token = _read_cfg()
    if not url or not token:
        return
    if not entry.get("provider"):
        return
    payload = {
        "environment": get_env("AI_USAGE_HUB_ENV", "production"),
        "attempt": 1,
        "status": "success",
    }
    payload.update(entry)
    _post(url.rstrip("/") + "/internal/ai-usage/logs", token, payload)


def make_entry(
    *,
    provider: str = "openrouter",
    model: str | None = None,
    operation: str = "chat.completions",
    source: str | None = None,
    user: str | None = None,
    reference: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    status: str = "success",
    error_message: str | None = None,
    http_status: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """สร้าง entry dict สำหรับส่งให้ ``log_ai_usage`` — กรอกเฉพาะที่มี."""
    entry: dict[str, Any] = {"provider": provider, "operation": operation, "status": status}
    if model:
        entry["model"] = model
    if source:
        entry["source"] = source
    if user:
        entry["user"] = user
    if reference:
        entry["reference"] = reference
    if prompt_tokens is not None:
        entry["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        entry["completion_tokens"] = completion_tokens
    if cost_usd is not None:
        entry["cost_usd"] = cost_usd
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    if error_message:
        entry["error_message"] = error_message
    if http_status is not None:
        entry["http_status"] = http_status
    if metadata:
        entry["metadata"] = metadata
    return entry
