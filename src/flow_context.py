"""Thread-local flow_id — ผูก LLM call เข้ากับ flow ที่กำลังรันอยู่.

แต่ละ flow worker รันใน thread ของตัวเอง — ตอนเริ่ม flow ให้ ``set_flow_id()``
แล้วทุกจุดที่เรียก ``log_local_usage()`` จะอ่าน flow_id นี้ใส่ลง entry อัตโนมัติ
พอ flow จบให้ ``clear_flow_id()``

การเรียก AI นอก flow (เช่น ingestion/อัปโหลดสินค้า) — flow_id ว่าง = ไม่นับเข้า flow ไหน
"""

from __future__ import annotations

import threading

_local = threading.local()


def set_flow_id(flow_id: str) -> None:
    """ตั้ง flow_id สำหรับ thread ปัจจุบัน — เรียกตอนเริ่ม flow worker."""
    _local.flow_id = flow_id


def get_flow_id() -> str:
    """อ่าน flow_id ของ thread ปัจจุบัน — คืน '' ถ้าไม่ได้อยู่ใน flow."""
    return getattr(_local, "flow_id", "")


def clear_flow_id() -> None:
    """ล้าง flow_id ตอน flow จบ — กัน leak ไปยัง thread reuse."""
    _local.flow_id = ""
