"""Tests: ProductSpecAgent.build_prompt — explicit image availability + multi-product.

Bug 1 (Flow 4): K2+K3 had 0 images but LLM claimed "เห็นรูปภาพจริง" and
described colors/watch faces. The prompt was silent when no images were
sent, so the model filled in imaginary visual details.

Bug 2 (Flow 1/2): When raw_data contains multiple product scopes (e.g.
K69 + K72 from a shared catalog), the prompt didn't tell the model to
write separate specs per product — leading to confusion or merged specs.

Fix:
  - When no images: prompt must explicitly say "ไม่มีรูปภาพ" and forbid
    claiming to have seen images.
  - When multiple scopes present in raw_data: prompt must instruct the
    model to write separate specs per product and not mix data across
    products.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.product_spec import ProductSpecAgent


def _make_agent():
    """Create ProductSpecAgent with minimal config (no LLM needed for prompt tests)."""
    from unittest.mock import MagicMock
    cfg = {
        "system_prompt": "คุณคือ นักวิเคราะห์สินค้า",
        "use_brand_context": False,
        "use_brand_reference": False,
    }
    return ProductSpecAgent(cfg, MagicMock())


# ------------------------------------------------------------------
#  Image availability — must be explicit in both directions
# ------------------------------------------------------------------

def test_prompt_with_images_mentions_count():
    """มีรูป → prompt ต้องระบุจำนวนรูปที่ถูกต้อง."""
    agent = _make_agent()
    prompt = agent.build_prompt("ข้อมูลดิบของสินค้า", product_images=["a.png", "b.jpg"])
    assert "2 รูป" in prompt
    assert "รูปภาพสินค้า" in prompt


def test_prompt_without_images_says_no_images():
    """ไม่มีรูป → prompt ต้องบอกชัดว่าไม่มีรูปและห้ามอ้างว่าเห็นภาพ.

    สถานการณ์จริง: Flow 4 (K2+K3) — LLM อ้างว่าเห็นรูปและบรรยายสีทั้งที่
    _read_folder() คืน image_paths = []
    """
    agent = _make_agent()
    prompt = agent.build_prompt("ข้อมูลดิบของสินค้า", product_images=None)
    assert "ไม่มีรูป" in prompt or "ไม่มีภาพ" in prompt
    # ต้องห้ามอ้างว่าเห็นภาพจริง
    assert "ห้ามอ้าง" in prompt or "ห้ามบรรยาย" in prompt or "ไม่เห็นรูป" in prompt


def test_prompt_without_images_does_not_claim_images():
    """ไม่มีรูป → prompt ต้องไม่บอกว่ามีรูปประกอบ."""
    agent = _make_agent()
    prompt = agent.build_prompt("ข้อมูลดิบ", product_images=[])
    # ห้ามมีคำว่า "มีรูปภาพสินค้า N รูป" เมื่อไม่มีรูป (แต่ "ไม่มีรูปภาพสินค้า" ได้)
    import re
    # หา pattern "มีรูปภาพสินค้า <number> รูป" ที่ไม่ใช่ "ไม่มี"
    assert not re.search(r"(?<!ไม่)มีรูปภาพสินค้า\s+\d+\s+รูป", prompt)
    assert "ไม่มีรูป" in prompt or "ไม่มีภาพ" in prompt


# ------------------------------------------------------------------
#  Multi-product — must instruct separate specs per scope
# ------------------------------------------------------------------

def test_prompt_with_multi_scope_data_instructs_separate_specs():
    """raw_data มีหลาย scope headers → prompt ต้องสั่งแยกสเปคตามสินค้า.

    สถานการณ์จริง: K69+K72 จาก catalog ร่วม — raw_data มี 2 scope headers
    prompt ต้องบอก LLM ให้เขียนสเปคแยกและห้ามปนข้อมูลข้ามรุ่น
    """
    agent = _make_agent()
    raw_data = (
        "--- ขอบเขตสินค้า ---\nรหัสสินค้า: K69\n--- สิ้นสุดขอบเขตสินค้า ---\n"
        "ข้อมูล K69\n"
        "--- ขอบเขตสินค้า ---\nรหัสสินค้า: K72\n--- สิ้นสุดขอบเขตสินค้า ---\n"
        "ข้อมูล K72"
    )
    prompt = agent.build_prompt(raw_data)
    # ต้องสั่งให้แยกสเปคตามสินค้า
    assert "แยก" in prompt or "เฉพาะ" in prompt
    # ต้องห้ามปนข้อมูลข้ามรุ่น
    assert "ห้ามปน" in prompt or "ไม่ปน" in prompt or "แยกจากกัน" in prompt


def test_prompt_with_single_scope_does_not_need_multi_instruction():
    """raw_data มี scope เดียว → ไม่จำเป็นต้องสั่งแยก (แต่ก็ไม่ผิดถ้ามี)."""
    agent = _make_agent()
    raw_data = (
        "--- ขอบเขตสินค้า ---\nรหัสสินค้า: K73\n--- สิ้นสุดขอบเขตสินค้า ---\n"
        "ข้อมูล K73"
    )
    prompt = agent.build_prompt(raw_data)
    # อย่างน้อยต้องมีข้อมูลดิบ
    assert "ข้อมูลดิบ" in prompt
    assert "K73" in prompt
