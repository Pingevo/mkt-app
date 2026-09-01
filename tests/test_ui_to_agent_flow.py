"""จำลอง flow จริงจาก UI → save → reload → agent ได้รับค่า.

ตรวจว่า:
1. user save ผ่าน /api/agent_instructions/{key} → ไฟล์เปลี่ยนจริง
2. orchestrator._load_agent_instructions() อ่านค่าใหม่ได้
3. agent ที่สร้างจาก orchestrator ได้รับ instructions ที่ user ตั้ง
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_config
from src.orchestrator import Orchestrator


class FakeLLM:
    def __init__(self):
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        out = (
            "## ราคาแนะนำ\n- ราคา pending financial validation\n"
            "## แคมเปญหลัก\n- ชื่อ: Launch Campaign เปิดตัวสินค้ารุ่นใหม่\n"
            "- วัตถุประสงค์: สร้างการรับรู้และกระตุ้นยอดขาย\n"
            "## แคมเปญเสริม\n- Influencer Review ใช้บล็อกเกอร์ทดลองสินค้า\n"
            "## ช่องทางโปรโมท\n- Facebook Ads และ TikTok สำหรับ Gen Z\n"
            "## KPI ที่ควรวัดผล\n- Reach และ CTR ต้องกำหนดหลังมี baseline\n"
            "## งบประมาณประมาณการ\n- ต้องอนุมัติทางการเงินก่อนกำหนดสัดส่วนงบ\n"
            "## แหล่งอ้างอิง\n- ไม่มี URL ภายนอกใน context นี้\n"
        )
        if kwargs.get("return_annotations"):
            return out, []
        return out

    def close(self):
        pass


INSTR_PATH = Path(__file__).resolve().parent.parent / "config" / "agent_instructions.json"


def _backup_instructions() -> str:
    return INSTR_PATH.read_text(encoding="utf-8")


def _restore_instructions(content: str) -> None:
    INSTR_PATH.write_text(content, encoding="utf-8")


def _make_orchestrator() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = load_config()
    orch.brand_context = ""
    orch.brand_reference = ""
    orch.brand_visual = ""
    orch.brand_rules = None
    orch.product_images = []
    orch.product_id = None
    orch.results = {}
    return orch


def test_save_instructions_then_orchestrator_reads_new_value():
    """user save ผ่าน API → orchestrator อ่านค่าใหม่ได้."""
    original = _backup_instructions()
    try:
        # 1. อ่านค่าปัจจุบัน
        data = json.loads(original)
        old_custom = data.get("campaign_strategy", {}).get("custom", "")

        # 2. เขียนค่าใหม่เหมือนที่ API ทำ
        data["campaign_strategy"]["custom"] = "เน้นราคาเฉพาะเจาะจง ไม่ต้องเป็น range กว้าง"
        data["campaign_strategy"]["rules_must"] = ["ราคาต้องจบด้วยเลข 9"]
        INSTR_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 3. orchestrator อ่านค่าใหม่
        orch = _make_orchestrator()
        instructions = orch._load_agent_instructions("campaign_strategy")

        assert instructions.get("custom") == "เน้นราคาเฉพาะเจาะจง ไม่ต้องเป็น range กว้าง"
        assert "ราคาต้องจบด้วยเลข 9" in instructions.get("rules_must", [])

        # 4. agent ที่สร้างจาก orchestrator ได้รับค่าจริง
        from src.agents.campaign_strategy import CampaignStrategyAgent
        agent = orch._make_agent("campaign_strategy", CampaignStrategyAgent, FakeLLM())
        block = agent._format_instructions()
        assert "เน้นราคาเฉพาะเจาะจง" in block
        assert "ราคาต้องจบด้วยเลข 9" in block
    finally:
        _restore_instructions(original)


def test_quick_brief_from_ui_reaches_agent_run():
    """user พิมพ์ quick_brief ตอนกดปุ่ม → ส่งถึง agent.run จริง."""
    original = _backup_instructions()
    try:
        # เคลียร์ custom ให้เทสสะอาด
        data = json.loads(original)
        data["campaign_strategy"]["custom"] = ""
        INSTR_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        orch = _make_orchestrator()
        llm = FakeLLM()
        result = orch.run_campaign_strategy(
            product_spec="Lagenio K2 smartwatch",
            competitor_analysis="",
            llm=llm,
            quick_brief="อยากได้ราคาเฉพาะเจาะจง และเน้น TikTok",
        )

        # quick_brief ต้องส่งถึง LLM
        user_content = llm.calls[0]["messages"][1]["content"]
        assert "อยากได้ราคาเฉพาะเจาะจง" in user_content
        assert "เน้น TikTok" in user_content
    finally:
        _restore_instructions(original)


def test_preset_change_in_ui_affects_agent_output():
    """user เปลี่ยน preset ใน UI → save → agent ได้รับ preset ใหม่."""
    original = _backup_instructions()
    try:
        data = json.loads(original)
        # เปลี่ยนเป็น sales_focus
        data["campaign_strategy"]["preset"] = "sales_focus"
        data["campaign_strategy"]["campaign_objective"] = "sales"
        data["campaign_strategy"]["risk_level"] = "aggressive"
        data["campaign_strategy"]["priority"] = ["volume", "acquisition"]
        INSTR_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        orch = _make_orchestrator()
        from src.agents.campaign_strategy import CampaignStrategyAgent
        agent = orch._make_agent("campaign_strategy", CampaignStrategyAgent, FakeLLM())
        block = agent._format_instructions()

        assert "sales_focus" in block
        assert "กล้า" in block  # aggressive
        assert "Sales Volume" in block
    finally:
        _restore_instructions(original)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
