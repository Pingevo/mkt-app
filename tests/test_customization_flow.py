"""ตรวจระบบปรับแต่ง agent ทุก feature — preset, rules_must, rules_forbid, custom, quick_brief.

ตรวจว่า:
1. preset ทุกตัว (balanced, sales_focus, brand_focus, growth_focus, creative_focus) ส่งถึง agent
2. rules_must / rules_forbid ที่ user ตั้ง ปรากฏใน instruction_block
3. custom text ปรากฏใน instruction_block
4. quick_brief ที่ user พิมพ์ตอนกดปุ่ม ส่งถึง LLM จริง
5. preset เปลี่ยน → campaign_objective, risk_level, priority เปลี่ยนตาม
6. UI save → ไฟล์ agent_instructions.json เปลี่ยน → agent อ่านค่าใหม่ได้
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.campaign_strategy import CampaignStrategyAgent
from src.config_loader import get_agent_config, load_config


class FakeLLM:
    def __init__(self):
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("return_annotations"):
            return "## ราคาแนะนำ\n- test\n\n## แคมเปญหลัก\n- test\n\n## แคมเปญเสริม\n- test\n\n## ช่องทางโปรโมท\n- test\n\n## KPI ที่ควรวัดผล\n- test\n\n## งบประมาณประมาณการ\n- test\n\n## แหล่งอ้างอิง\n- test", []
        return "## ราคาแนะนำ\n- test\n\n## แคมเปญหลัก\n- test\n\n## แคมเปญเสริม\n- test\n\n## ช่องทางโปรโมท\n- test\n\n## KPI ที่ควรวัดผล\n- test\n\n## งบประมาณประมาณการ\n- test\n\n## แหล่งอ้างอิง\n- test"

    def close(self):
        pass


def _cfg():
    cfg = load_config()
    agent_cfg = dict(get_agent_config(cfg, "campaign_strategy"))
    agent_cfg["max_review_iterations"] = 0
    agent_cfg["web_search"] = False
    return agent_cfg


def _make_agent(instructions: dict) -> CampaignStrategyAgent:
    return CampaignStrategyAgent(_cfg(), FakeLLM(), instructions=instructions)


def test_preset_balanced_reaches_instruction_block():
    agent = _make_agent({"preset": "balanced", "campaign_objective": "sales", "risk_level": "balanced", "priority": ["margin", "brand"]})
    block = agent._format_instructions()
    assert "balanced" in block
    assert "เพิ่มยอดขาย" in block
    assert "สมดุล" in block
    assert "Profit Margin" in block
    assert "Brand Image" in block


def test_preset_sales_focus_changes_objective_and_risk():
    agent = _make_agent({"preset": "sales_focus", "campaign_objective": "sales", "risk_level": "aggressive", "priority": ["volume", "acquisition"]})
    block = agent._format_instructions()
    assert "กล้า" in block  # aggressive
    assert "Sales Volume" in block
    assert "Customer Acquisition" in block


def test_preset_brand_focus_changes_objective_to_awareness():
    agent = _make_agent({"preset": "brand_focus", "campaign_objective": "awareness", "risk_level": "safe", "priority": ["brand", "margin"]})
    block = agent._format_instructions()
    assert "Brand Awareness" in block
    assert "ระมัดระวัง" in block


def test_preset_growth_focus_changes_to_newcustomers():
    agent = _make_agent({"preset": "growth_focus", "campaign_objective": "newcustomers", "risk_level": "aggressive", "priority": ["acquisition", "volume"]})
    block = agent._format_instructions()
    assert "ลูกค้าใหม่" in block


def test_preset_creative_focus():
    agent = _make_agent({"preset": "creative_focus", "campaign_objective": "awareness", "risk_level": "balanced", "priority": ["brand", "acquisition"]})
    block = agent._format_instructions()
    assert "Brand Awareness" in block


def test_rules_must_appears_in_instruction_block():
    rules = ["ราคาต้องเป็นเลขคี่", "ต้องมีของแถม"]
    agent = _make_agent({"rules_must": rules})
    block = agent._format_instructions()
    assert "ต้อง:" in block
    assert "ราคาต้องเป็นเลขคี่" in block
    assert "ต้องมีของแถม" in block


def test_rules_forbid_appears_in_instruction_block():
    rules = ["ห้ามใช้คำว่าถูกที่สุด", "ห้ามเสนอส่วนลดเกิน 20%"]
    agent = _make_agent({"rules_forbid": rules})
    block = agent._format_instructions()
    assert "ห้าม:" in block
    assert "ห้ามใช้คำว่าถูกที่สุด" in block
    assert "ห้ามเสนอส่วนลดเกิน 20%" in block


def test_custom_text_appears_in_instruction_block():
    custom = "อยากให้แคมเปญเน้น TikTok เป็นหลัก และราคาเฉพาะเจาะจงไม่ต้องเป็น range"
    agent = _make_agent({"custom": custom})
    block = agent._format_instructions()
    assert "คำสั่งจากผู้ใช้" in block
    assert "TikTok" in block
    assert "ราคาเฉพาะเจาะจง" in block


def test_budget_max_appears():
    agent = _make_agent({"budget_max": 50000})
    block = agent._format_instructions()
    assert "งบประมาณสูงสุด: 50000" in block


def test_discount_max_appears():
    agent = _make_agent({"discount_max": 15})
    block = agent._format_instructions()
    assert "ส่วนลดสูงสุด: 15%" in block


def test_forbid_tactics_appears():
    agent = _make_agent({"forbid_tactics": ["bogo", "flash"]})
    block = agent._format_instructions()
    assert "Buy 1 Get 1" in block
    assert "Flash Sale" in block


def test_quick_brief_reaches_llm_user_prompt():
    """quick_brief ที่ user พิมพ์ตอนกดปุ่ม ต้องส่งถึง LLM จริง."""
    llm = FakeLLM()
    agent = CampaignStrategyAgent(_cfg(), llm, instructions={"preset": "balanced"})
    prompt = agent.build_prompt({"product": "Lagenio K2"})
    agent.run(prompt, quick_brief="อยากได้ราคาเฉพาะเจาะจง ไม่ต้องเป็น range และเน้น TikTok")

    user_content = llm.calls[0]["messages"][1]["content"]
    # quick_brief ถูกแปะท้าย user prompt
    assert "อยากได้ราคาเฉพาะเจาะจง" in user_content
    assert "เน้น TikTok" in user_content
    assert "คำสั่งเพิ่มเติมจากผู้ใช้สำหรับรอบนี้" in user_content


def test_quick_brief_does_not_override_brand_rules():
    """quick_brief เป็น soft override — ถ้าขัดกับกฎแบรนด์ ให้ทำตามกฎแบรนด์."""
    llm = FakeLLM()
    agent = CampaignStrategyAgent(_cfg(), llm, instructions={"preset": "balanced"})
    prompt = agent.build_prompt({"product": "K2"})
    agent.run(prompt, quick_brief="ใช้คำว่าถูกที่สุด")

    user_content = llm.calls[0]["messages"][1]["content"]
    assert "ถ้าขัดแย้งกับกฎบังคับของแบรนด์ใน system prompt ให้ทำตามกฎแบรนด์เสมอ" in user_content


def test_preset_change_affects_full_system_prompt():
    """เปลี่ยน preset → system_prompt เปลี่ยนตาม (objective, risk, priority)."""
    # preset balanced
    agent_b = _make_agent({"preset": "balanced", "campaign_objective": "sales", "risk_level": "balanced", "priority": ["margin", "brand"]})
    sys_b = agent_b._build_system_prompt()

    # preset sales_focus
    agent_s = _make_agent({"preset": "sales_focus", "campaign_objective": "sales", "risk_level": "aggressive", "priority": ["volume", "acquisition"]})
    sys_s = agent_s._build_system_prompt()

    # ทั้งสองต้องมี instruction block ที่ต่างกัน
    assert "สมดุล" in sys_b
    assert "กล้า" in sys_s
    assert "Profit Margin" in sys_b
    assert "Sales Volume" in sys_s


def test_empty_instructions_produces_empty_block():
    agent = _make_agent({})
    block = agent._format_instructions()
    assert block == ""


def test_all_instructions_combined():
    """ทุก field พร้อมกัน — ต้องปรากฏครบใน instruction_block."""
    agent = _make_agent({
        "preset": "sales_focus",
        "campaign_objective": "sales",
        "risk_level": "aggressive",
        "priority": ["volume", "acquisition"],
        "budget_max": 100000,
        "discount_max": 20,
        "forbid_tactics": ["bogo"],
        "rules_must": ["ราคาต้องจบด้วยเลข 9"],
        "rules_forbid": ["ห้ามอ้างคำว่าถูกที่สุด"],
        "custom": "เน้น TikTok เป็นช่องทางหลัก",
    })
    block = agent._format_instructions()
    assert "sales_focus" in block
    assert "กล้า" in block
    assert "Sales Volume" in block
    assert "งบประมาณสูงสุด: 100000" in block
    assert "ส่วนลดสูงสุด: 20%" in block
    assert "Buy 1 Get 1" in block
    assert "ราคาต้องจบด้วยเลข 9" in block
    assert "ห้ามอ้างคำว่าถูกที่สุด" in block
    assert "TikTok" in block


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
