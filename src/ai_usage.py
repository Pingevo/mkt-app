"""AI Usage Hub — fire-and-forget logging ไปยัง central hub (digital.in.th).

ทุกครั้งที่เรียก AI/scraping provider จริง ให้เรียก ``record_ai_usage()`` หลังได้ response
กลับมา (รวม error path) — ฟังก์ชันนี้ไม่มีวัน throw ออกมาทำลาย flow หลัก

อ่านเอกสารเต็ม: AI_USAGE_HUB_DEVELOPER_API.md
"""

from __future__ import annotations

import json
import threading
import time
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

try:
    from .flow_context import get_usage_context
except ImportError:
    from flow_context import get_usage_context  # type: ignore


_DEFAULT_URL = "https://digital.in.th"
_HUB_ENDPOINT = get_env("AI_USAGE_HUB_ENDPOINT", "/internal/ai-usage/logs")

# Local usage log — เก็บทุก AI call ลงไฟล์เพื่อ track ค่าใช้จ่าย (ไม่ต้องมี Hub token)
# shared constant — cost_summary.py import จากที่นี่แทนการประกาศซ้ำ
USAGE_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "llm_usage.jsonl"


def _default_actor() -> str:
    """คืน actor default ถ้าไม่มี context ตั้งไว้ — อ่านจาก env หรือ fallback."""
    return get_env("AI_USAGE_HUB_USER", "mktapp")


def _merge_context(entry: dict[str, Any]) -> dict[str, Any]:
    """Merge thread-local usage context เข้า entry ถ้า caller ยังไม่ได้ระบุเอง."""
    entry = dict(entry)
    ctx = get_usage_context()

    if "user" not in entry:
        user = ctx.get("user") or _default_actor()
        if user:
            entry["user"] = user

    if "reference" not in entry:
        reference = ctx.get("reference") or ctx.get("flow_id")
        if reference:
            entry["reference"] = reference

    # merge metadata: context เป็นฐาน, caller override ทับ และแนบ flow_id ไว้ correlation
    ctx_metadata = ctx.get("metadata", {}) or {}
    flow_id = ctx.get("flow_id", "")
    if flow_id:
        ctx_metadata = {**ctx_metadata, "flow_id": ctx_metadata.get("flow_id") or flow_id}
    if ctx_metadata:
        existing = entry.get("metadata", {}) or {}
        if isinstance(existing, dict):
            merged = {**ctx_metadata, **existing}
            if merged:
                entry["metadata"] = merged

    return entry


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


HUB_POST_CALLBACK: Any | None = None
"""Optional callback for acceptance/tests to observe Hub POST result."""


def _post(endpoint: str, token: str, payload: dict[str, Any]) -> None:
    """POST จริง — ครอบ try/except ทุกกรณี."""
    if not _HTTPX_OK:
        return
    result: dict[str, Any] = {"endpoint": endpoint, "request_id": payload.get("request_id")}
    try:
        with httpx.Client(timeout=int(_system_cfg().get("api_timeout_ai_usage", 5))) as client:
            resp = client.post(
                endpoint,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-service-token": token,
                },
            )
            resp.raise_for_status()
            result["status_code"] = resp.status_code
            result["hub_status"] = "hub_delivered"
    except httpx.HTTPStatusError as e:
        result["status_code"] = e.response.status_code
        result["error"] = str(e)
        result["hub_status"] = "hub_http_error"
    except Exception as e:
        result["error"] = str(e)
        result["hub_status"] = "hub_transport_error"
    if HUB_POST_CALLBACK is not None:
        try:
            HUB_POST_CALLBACK(result)
        except Exception:
            pass


def record_ai_usage(entry: dict[str, Any]) -> None:
    """บันทึก 1 event ลง local และยิงไป Hub แบบ fire-and-forget.

    - รวม context (actor, reference, metadata) เข้า entry อัตโนมัติ
    - เขียน local ทันที (ไม่ block, ไม่ throw)
    - ยิง Hub ใน background thread (ไม่ block)
    """
    if not entry.get("provider"):
        return

    try:
        merged = _merge_context(entry)
        # 1. local first เพื่อไม่สูญหายแม้ process ตายหลังนี้
        log_local_usage(merged)

        # 2. Hub fire-and-forget
        url, token = _read_hub_credentials()
        if not url or not token:
            return

        payload = _build_payload(merged)
        t = threading.Thread(
            target=_post,
            args=(url.rstrip("/") + "/" + _HUB_ENDPOINT.lstrip("/"), token, payload),
            name="ai-usage-hub",
            daemon=True,
        )
        t.start()
    except Exception:
        pass  # fire-and-forget — ไม่ให้ logging error ทำลาย flow หลัก


def flush_usage_log(timeout: float = 5.0) -> bool:
    """รอให้ daemon threads ทียิงไป Hub ทำงานเสร็จภายใน timeout วินาที.

    ใช้สำหรับ CLI / runner ทีต้องการ ensure delivery ก่อน process จบ
    โดยไม่ทำให้ production UI ต้อง block.

    คืน True ถ้า threads ทั้งหมดจบทัน timeout, False ถ้ามี thread ยังทำงานอยู่
    (หมายถึงยังไม่ทราบผล delivery).
    """
    try:
        deadline = time.monotonic() + timeout
        for t in threading.enumerate():
            if t.name != "ai-usage-hub" or not t.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(timeout=remaining)
        # ถ้ายังมี ai-usage-hub ไมตาย แปลว่า timeout
        return not any(
            t.name == "ai-usage-hub" and t.is_alive() for t in threading.enumerate()
        )
    except Exception:
        return False  # ไม่ให้ flush error ทำลาย flow หลัก


def log_ai_usage(entry: dict[str, Any]) -> None:
    """Deprecated alias: ยิง log 1 event ไป AI Usage Hub + เซฟ local.

    ใช้ ``record_ai_usage()`` แทน — function นี้คงไว้เพื่อ backward compatibility.
    """
    record_ai_usage(entry)


def make_entry(
    *,
    provider: str = "openrouter",
    model: str | None = None,
    operation: str = "chat.completions",
    source: str | None = None,
    user: str | None = None,
    reference: str | None = None,
    request_id: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    units: dict[str, Any] | None = None,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    attempt: int = 1,
    status: str = "success",
    http_status: int | None = None,
    error_message: str | None = None,
    raw_usage: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """สร้าง entry dict สำหรับส่งให้ ``record_ai_usage`` — กรอกเฉพาะที่มี."""
    entry: dict[str, Any] = {
        "provider": provider,
        "operation": operation,
        "status": status,
        "attempt": attempt,
    }
    if model:
        entry["model"] = model
    if source:
        entry["source"] = source
    if user:
        entry["user"] = user
    if reference:
        entry["reference"] = reference
    if request_id:
        entry["request_id"] = request_id
    if prompt_tokens is not None:
        entry["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        entry["completion_tokens"] = completion_tokens
    if units:
        entry["units"] = units
    if cost_usd is not None:
        entry["cost_usd"] = cost_usd
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    if http_status is not None:
        entry["http_status"] = http_status
    if error_message:
        entry["error_message"] = error_message
    if raw_usage:
        entry["raw_usage"] = raw_usage
    if metadata:
        entry["metadata"] = metadata
    return entry
