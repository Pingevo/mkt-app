"""Thread-local flow + usage context — ผูก LLM/AI call เข้ากับ flow ที่กำลังรันอยู่.

แต่ละ flow worker รันใน thread ของตัวเอง:
  - set_flow_id() ตอนเริ่ม flow
  - set_usage_actor/reference/metadata() ตาม context ที่มีจริง
  - clear_usage_context() ตอน flow จบ
ทุกจุดที่เรียก log_local_usage() / record_ai_usage() จะอ่านค่าพวกนี้ใส่ลง entry อัตโนมัติ
"""
from __future__ import annotations

import threading
from typing import Any

_local = threading.local()


def _ensure_ctx() -> dict[str, Any]:
    if not getattr(_local, "usage", None):
        _local.usage = {}
    return _local.usage


def set_flow_id(flow_id: str) -> None:
    """ตั้ง flow_id สำหรับ thread ปัจจุบัน."""
    _ensure_ctx()["flow_id"] = flow_id


def get_flow_id() -> str:
    """อ่าน flow_id ของ thread ปัจจุบัน — คืน '' ถ้าไม่ได้อยู่ใน flow."""
    return _ensure_ctx().get("flow_id", "")


def set_usage_actor(actor: str | None = None) -> None:
    """ตั้ง actor (user) ของ AI call ในส่วนนี้."""
    if actor is not None:
        _ensure_ctx()["user"] = actor


def set_usage_reference(reference: str | None = None) -> None:
    """ตั้ง subject (reference) ของ AI call — เช่น product_id หรือ job_id."""
    if reference is not None:
        _ensure_ctx()["reference"] = reference


def set_usage_metadata(metadata: dict[str, Any] | None = None) -> None:
    """ตั้ง metadata ของ usage context; จะถูก merge เข้า entry ทีหลัง."""
    if metadata is not None:
        _ensure_ctx()["metadata"] = dict(metadata)


def get_usage_context() -> dict[str, Any]:
    """คืน context ปัจจุบันเป็น dict โดยไม่แก้ _local."""
    ctx = getattr(_local, "usage", None) or {}
    return {
        "flow_id": ctx.get("flow_id", ""),
        "user": ctx.get("user", ""),
        "reference": ctx.get("reference", ""),
        "metadata": ctx.get("metadata", {}) or {},
    }


def clear_flow_id() -> None:
    """ล้าง flow_id ของ thread ปัจจุบัน."""
    _ensure_ctx().pop("flow_id", None)


def clear_usage_context() -> None:
    """ล้าง usage context ทั้งหมดของ thread ปัจจุบัน — ใช้ตอน flow จบ."""
    _local.usage = {}
