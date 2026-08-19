"""AI Usage Hub — fire-and-forget logging ไปยัง central hub (digital.in.th).

ทุกครั้งที่เรียก AI/scraping provider จริง ให้เรียก ``log_ai_usage()`` หลังได้ response
กลับมา (รวม error path) — ฟังก์ชันนี้ไม่มีวัน throw ออกมาทำลาย flow หลัก

อ่านเอกสารเต็ม: AI_USAGE_HUB_DEVELOPER_API.md
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
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

# Local usage log — เก็บทุก AI call ลงไฟล์เพื่อ track ค่าใช้จ่าย (ไม่ต้องมี Hub token)
# shared constant — cost_summary.py import จากที่นี่แทนการประกาศซ้ำ
USAGE_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "llm_usage.jsonl"


def log_local_usage(entry: dict[str, Any]) -> None:
    """เซฟ usage ลง local file (JSONL) — ไม่ต้อง Hub ก็ดูย้อนหลังได้.

    เพิ่ม timestamp อัตโนมัติ ไม่มีวัน throw ออกมา
    อ่าน flow_id จาก thread-local (flow_context) — ใส่ลง entry ถ้าไม่ว่าง
    """
    try:
        USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = dict(entry)  # copy ไม่แก้ของเดิม
        entry["timestamp"] = datetime.now().isoformat()
        # ผูก flow_id จาก thread-local — ว่าง = ไม่อยู่ใน flow (เช่น ingestion)
        try:
            from .flow_context import get_flow_id
        except ImportError:
            from flow_context import get_flow_id  # type: ignore
        fid = get_flow_id()
        if fid:
            entry["flow_id"] = fid
        with open(USAGE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # ไม่ให้ logging error ทำลาย main flow


def _system_cfg() -> dict:
    """อ่าน system section จาก config — fallback {} ถ้าโหลดไม่ได้ (lazy, กัน circular import)."""
    try:
        from .config_loader import load_config, get_section
        return get_section(load_config(), "system", {})
    except Exception:
        return {}


def _read_hub_credentials() -> tuple[str | None, str | None]:
    """คืน (url, token) — ถ้ายังไม่ตั้งค่า คืน (None, None) แล้วข้ามเงียบๆ."""
    url = get_env("AI_USAGE_HUB_URL", _DEFAULT_URL)
    token = get_env("AI_USAGE_HUB_TOKEN")
    return url, token


def _build_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """สร้าง payload สำหรับส่งให้ Hub — ค่า default + merge entry."""
    payload = {
        "environment": get_env("AI_USAGE_HUB_ENV", "production"),
        "attempt": 1,
        "status": "success",
    }
    payload.update(entry)
    return payload


def log_ai_usage(entry: dict[str, Any]) -> None:
    """ยิง log 1 event ไป AI Usage Hub — fire-and-forget.

    ไม่มีวัน throw ออกมา — ถ้ายิงไม่สำเร็จ แค่ปล่อยผ่าน
    ถ้ายังไม่ตั้งค่า ``AI_USAGE_HUB_TOKEN`` จะข้ามเงียบๆ (return เลย)

    field บังคับใน entry: ``provider``
    field แนะนำ: model, operation, source, user, reference,
                 prompt_tokens, completion_tokens, cost_usd,
                 duration_ms, status, error_message
    """
    url, token = _read_hub_credentials()
    if not url or not token:
        return  # ยังไม่ตั้งค่า — ข้ามเงียบๆ

    if not entry.get("provider"):
        return  # ไม่มี provider ไม่ยิง

    payload = _build_payload(entry)

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
        with httpx.Client(timeout=int(_system_cfg().get("api_timeout_ai_usage", 5))) as client:
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
