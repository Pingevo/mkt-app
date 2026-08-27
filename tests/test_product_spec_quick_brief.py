"""Tests: product_spec quick_brief steering + hierarchy.

TDD: red → green สำหรับ scope ที่ user กำหนด
- quick_brief ต้องปรากฏในข้อความที่ส่ง LLM
- StepRunContext brief ต้องปรากฏครั้งเดียว ไม่ซ้ำ
- prompt ต้องบอกชัดว่า brief ห้าม override raw data, hard rules, output format
- default Agent 1 ไม่มีคำสั่งบังคับให้ยืนยันว่าเห็นรูป
- tests เก่ายังผ่าน
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.product_spec import ProductSpecAgent
from src.config_loader import load_config, get_agent_config
from src.run_context import StepRunContext


def _make_agent(system_prompt: str = "คุณคือ product spec agent",
                instructions: dict | None = None) -> ProductSpecAgent:
    """สร้าง ProductSpecAgent พร้อม config ทีเน้นทดสอบ prompt/flow."""
    cfg = {
        "system_prompt": system_prompt,
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "required_output_sections": ["ชื่อสินค้า"],
        "model": "x",
        "temperature": 0.3,
        "max_tokens": 100,
        "max_retry_limit": 0,
    }
    return ProductSpecAgent(cfg, MagicMock(), instructions=instructions or {})


class CapturingLLM:
    """Fake LLM ที่เก็บ messages ทีส่งให้ chat."""

    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = 0
        self.captured_messages: list[list[dict]] = []

    def chat(self, messages: list[dict], **kwargs) -> str:
        self.calls += 1
        self.captured_messages.append(messages)
        return self.output

    def close(self) -> None:
        pass


def _first_user_content(messages: list[dict]) -> str:
    """ดึง content จาก message user แรก."""
    for m in messages:
        if m.get("role") == "user":
            return m.get("content", "")
    return ""


def test_quick_brief_appears_in_llm_user_message():
    """quick_brief ต้องปรากฏในข้อความ user ที่ส่งให้ LLM ของ Agent 1."""
    agent = _make_agent()
    brief = "เน้นข้อมูลสำหรับทีมขาย ห้ามเดาสเปค"
    llm = CapturingLLM("ชื่อสินค้า: K77")
    agent.llm = llm

    prompt = agent.build_prompt("K77 smartwatch แบต 1000mAh")
    agent.run(prompt, quick_brief=brief)

    assert llm.calls >= 1
    first_user = _first_user_content(llm.captured_messages[0])
    assert brief in first_user, "quick_brief ต้องปรากฏในข้อความ user แรก"


def test_step_run_context_brief_appears_once():
    """เมื่อมี StepRunContext brief ต้องปรากฏครั้งเดียว ไม่ซ้ำกับ build_prompt หรือ run."""
    agent = _make_agent()
    brief = "เน้นประโยชน์ต่อลูกค้า"
    llm = CapturingLLM("ชื่อสินค้า: K77")
    agent.llm = llm

    prompt = agent.build_prompt("K77 smartwatch แบต 1000mAh")
    ctx = StepRunContext(
        workflow_id="wf",
        step_id="s1",
        agent_key="product_spec",
        quick_brief=brief,
        input_refs=(),
        product_refs=(),
        resource_refs=(),
        resource_text="",
        resource_image_paths=(),
        resource_trace=(),
        warnings=(),
    )
    agent.run(prompt, step_context=ctx)

    first_user = _first_user_content(llm.captured_messages[0])
    assert first_user.count(brief) == 1, f"brief ต้องปรากฏครั้งเดียว แต่พบ {first_user.count(brief)} ครั้ง"


def test_step_run_context_overrides_run_quick_brief():
    """StepRunContext.quick_brief ต้อง override quick_brief parameter โดยไม่ทิ้งค่าเก่าให้ปรากฏ."""
    agent = _make_agent()
    old_brief = "คำสั่งเก่าที่ไม่ต้องการ"
    real_brief = "ค่าจริงจาก step context"
    llm = CapturingLLM("ชื่อสินค้า: K77")
    agent.llm = llm

    prompt = agent.build_prompt("K77 smartwatch แบต 1000mAh")
    ctx = StepRunContext(
        workflow_id="wf",
        step_id="s2",
        agent_key="product_spec",
        quick_brief=real_brief,
        input_refs=(),
        product_refs=(),
        resource_refs=(),
        resource_text="",
        resource_image_paths=(),
        resource_trace=(),
        warnings=(),
    )
    agent.run(prompt, quick_brief=old_brief, step_context=ctx)

    first_user = _first_user_content(llm.captured_messages[0])
    assert first_user.count(real_brief) == 1, f"ค่าจริงจาก StepRunContext ต้องปรากฏครั้งเดียว แต่พบ {first_user.count(real_brief)} ครั้ง"
    assert old_brief not in first_user, "ค่าเก่าจาก parameter quick_brief ต้องไม่ปรากฏเมื่อมี StepRunContext"


def test_prompt_hierarchy_in_system_and_user_prompts():
    """system/user prompt ต้องระบุ hierarchy: brief ห้าม override raw data, hard rules, output format."""
    # 1) system prompt จาก agents.yaml ต้องมี hierarchy
    cfg = get_agent_config(load_config(), "product_spec")
    system_prompt = cfg["system_prompt"]

    assert "ข้อมูลดิบของสินค้า" in system_prompt
    assert "source of truth" in system_prompt
    assert "quick_brief" in system_prompt
    assert "ห้ามสร้างหรืออนุมานข้อเท็จจริง" in system_prompt
    assert "required output" in system_prompt or "รูปแบบ output ที่กำหนด" in system_prompt
    assert "hard brand" in system_prompt or "hard rules" in system_prompt

    # 2) user prompt wrapper ต้องบอกใช้ brief เพื่อ steer และห้าม override
    agent = _make_agent(system_prompt)
    brief = "เน้นข้อมูลสำหรับทีมขาย"
    llm = CapturingLLM("ชื่อสินค้า: K77")
    agent.llm = llm

    prompt = agent.build_prompt("K77 ข้อมูลดิบ")
    agent.run(prompt, quick_brief=brief)

    first_user = _first_user_content(llm.captured_messages[0])
    assert "steer รูปแบบ" in first_user or "หัวข้อที่เน้น" in first_user
    assert "ห้ามสร้างหรืออนุมานข้อเท็จจริง" in first_user
    assert "ห้ามขัด" in first_user or "ห้าม override" in first_user


def test_default_product_spec_no_image_confirmation_instruction():
    """default Agent 1 ไม่มีคำสั่งบังคับให้ยืนยันว่าเห็นรูป (custom ต้องว่าง)."""
    path = Path(__file__).resolve().parent.parent / "config" / "agent_instructions.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data.get("product_spec", {}).get("custom", "") == "", \
        "product_spec.custom ต้องเป็นค่าว่าง เพื่อ baseline สะอาด"


def test_existing_prompt_tests_still_work():
    """sanity: เรียก build_prompt แบบเดิมไม่พังหลังเพิ่ม quick_brief flow."""
    agent = _make_agent()
    prompt = agent.build_prompt("ข้อมูล K77", product_images=None)
    assert "ข้อมูลดิบ" in prompt
    assert "K77" in prompt
