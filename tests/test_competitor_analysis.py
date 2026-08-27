"""Competitor analysis tests using a fake LLM.

These tests exercise the full `BaseAgent.run()` flow for the
`competitor_analysis` agent without calling a real LLM.  They prove that:

1. The prompt is built from product spec and user input.
2. The agent passes `openrouter:web_search` + `openrouter:web_fetch` tools.
3. Citations are appended to a non-empty response.
4. Empty responses raise a clear error instead of entering repair loops.
5. Plain-text headings (the real-world format) do not trigger validation failures.
6. Output is returned without repair when validation is disabled.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agents.competitor_analysis import CompetitorAnalysisAgent
from src.agents.base_agent import BaseAgent
from src.llm_client import LLMClient
from src.config_loader import load_config, get_agent_config
from src.brand_priority import load_brand_priority
from src.brand_loader import load_brand_reference

BRAND_DIR = Path(__file__).resolve().parent.parent / "brand"


class FakeLLM:
    """Deterministic LLM double that captures the request and returns scripted output."""

    def __init__(self, generate_output: str = "", annotations: list | None = None, fetch_output: str = "", repair_output: str = ""):
        self.generate_output = generate_output
        self.annotations = annotations or []
        self.fetch_output = fetch_output
        self.repair_output = repair_output
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        source = kwargs.get("source", "")
        if ".fetch" in source:
            return self.fetch_output
        if ".repair" in source:
            return self.repair_output
        if kwargs.get("return_annotations"):
            return self.generate_output, self.annotations
        return self.generate_output

    def close(self):
        pass


def _competitor_config():
    cfg = load_config()
    return get_agent_config(cfg, "competitor_analysis")


def test_build_prompt_includes_product_and_user_input():
    llm = FakeLLM()
    agent = CompetitorAnalysisAgent(_competitor_config(), llm)
    product = "LAGENIO K2 สมาร์ทวอทช์เด็ก"
    competitor = "imoo Z1"
    prompt = agent.build_prompt(product, competitor)
    assert product in prompt
    assert competitor in prompt
    assert "วิเคราะห์เปรียบเทียบ" in prompt


def test_run_sends_web_search_and_fetch_tools():
    cfg = _competitor_config()
    cfg.pop("output_quality", None)
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = True
    llm = FakeLLM(
        generate_output="ภาพรวมตลาด (Market Overview)\nตลาดสมาร์ทวอทช์เด็กเติบโต",
    )
    agent = CompetitorAnalysisAgent(cfg, llm)

    result = agent.run("วิเคราะห์คู่แข่ง K2")

    assert result is not None
    # The generate call should pass the two server tools.
    generate_calls = [c for c in llm.calls if "generate" in c["kwargs"].get("source", "")]
    assert len(generate_calls) == 1
    tools = generate_calls[0]["kwargs"].get("tools", [])
    assert any(t.get("type") == "openrouter:web_search" for t in tools)
    assert any(t.get("type") == "openrouter:web_fetch" for t in tools)


def test_run_appends_citations_for_non_empty_output():
    cfg = _competitor_config()
    cfg.pop("output_quality", None)
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = True
    annotations = [
        {"url": "https://example.com/k2", "title": "K2 Official", "content": "K2 specs"},
        {"url": "https://example.com/imoo", "title": "imoo Z1", "content": "imoo info"},
    ]
    llm = FakeLLM(
        generate_output="ภาพรวมตลาด\nตลาดเติบโต",
        annotations=annotations,
        fetch_output="หน้าเกี่ยวข้องจริง",
    )
    agent = CompetitorAnalysisAgent(cfg, llm)
    agent.build_prompt(
        "--- ขอบเขตสินค้า ---\nรหัสสินค้า: K2\n---\nK2 smart watch",
        "imoo Z1",
    )

    result = agent.run("วิเคราะห์คู่แข่ง K2")

    assert "https://example.com/k2" in result
    assert "[K2 Official](https://example.com/k2)" in result


def test_run_skips_validation_when_no_required_sections():
    cfg = _competitor_config()
    cfg.pop("output_quality", None)
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = False
    # Ensure no required sections remain.
    cfg.pop("required_output_sections", None)
    llm = FakeLLM(generate_output="ผลลัพธ์ไม่มี section เป็นทางการ")
    agent = CompetitorAnalysisAgent(cfg, llm)

    result = agent.run("วิเคราะห์คู่แข่ง K2")

    assert result == "ผลลัพธ์ไม่มี section เป็นทางการ"
    # No repair calls should be made.
    assert all("repair" not in c["kwargs"].get("source", "") for c in llm.calls)


def test_run_raises_for_blank_output():
    cfg = _competitor_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = True
    llm = FakeLLM(generate_output="", annotations=[])
    agent = CompetitorAnalysisAgent(cfg, llm)

    with pytest.raises(ValueError, match="model คืนคำตอบว่างเปล่า"):
        agent.run("วิเคราะห์คู่แข่ง K2")


def test_run_plain_text_headings_pass_without_repair():
    cfg = _competitor_config()
    cfg.pop("output_quality", None)
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = False
    # Simulate a response like the one the real model produced.
    output = (
        "ภาพรวมตลาด (Market Overview)\n"
        "ตลาดเติบโต\n\n"
        "ตารางเปรียบเทียบ (Comparison Table)\n"
        "คุณสมบัติ | K2 | imoo Z1\n\n"
        "จุดแข็งของเรา vs คู่แข่ง (Our Strengths)\n"
        "1. จอ AMOLED\n\n"
        "จุดอ่อนของเรา vs คู่แข่ง (Our Weaknesses)\n"
        "1. ราคาสูง\n\n"
        "ช่องว่างในตลาด (Market Gaps)\n"
        "ยังไม่พบหลักฐานเพียงพอ\n\n"
        "ภัยคุกคาม (Threats)\n"
        "imoo แข็งแกร่ง\n\n"
        "คำแนะนำเชิงกลยุทธ์ (Strategic Recommendations)\n"
        "เน้นจอใหญ่\n\n"
        "แหล่งอ้างอิง (Sources)\n"
        "- [imoo](https://imoo.com)"
    )
    # No required_output_sections are set for competitor_analysis,
    # so the validator accepts plain-text headings without repair.
    llm = FakeLLM(generate_output=output)
    agent = CompetitorAnalysisAgent(cfg, llm)

    result = agent.run("วิเคราะห์คู่แข่ง K2")

    assert "ภาพรวมตลาด" in result
    assert not any("repair" in c["kwargs"].get("source", "") for c in llm.calls)


def test_run_triggers_repair_when_output_quality_fails():
    """ถ้า fake output ไม่ผ่าน output_quality contract → BaseAgent.run() ต้องเข้า repair flow."""
    cfg = _competitor_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = False

    bad_output = "นี่คือรายงานสั้น ๆ ทีไม่มี structure ใด ๆ"
    good_output = (
        "## ภาพรวมตลาด\n\n"
        "ตลาดสมาร์ทวอทช์เด็กเติบโตขึ้นอย่างต่อเนื่องจากความต้องการติดต่อลูกของผู้ปกครอง "
        "ทำให้สินค้ากลุ่มนี้มาแรงในหลายประเทศเอเชียตะวันออกเฉียงใต้ "
        "โดยเฉพาะในเมืองใหญ่ทีผู้ปกครองมีงานประจำและต้องการติดตามลูกน้อย\n\n"
        "## คำแนะนำ\n\n"
        "ควรเน้นจุดขายด้านแบตเตอรี่และกล้องหน้าคมชัดเพื่อแข่งขันกับคู่แข่ง "
        "โดยเฉพาะการสื่อสารความปลอดภัยและใช้งานง่ายให้ชัดเจน "
        "นอกจากนี้ควรยกระดับข้อความทีเน้นความโปร่งใสของข้อมูลและการดูแลลูกแบบ real-time "
        "เพื่อสร้างความมั่นใจให้ผู้ปกครองและเพิ่มโอกาสในการตัดสินใจซื้าอย่างรวดเร็ว\n\n"
    )
    llm = FakeLLM(generate_output=bad_output, repair_output=good_output)
    agent = CompetitorAnalysisAgent(cfg, llm)

    result = agent.run("วิเคราะห์คู่แข่ง K2")

    assert any("repair" in c["kwargs"].get("source", "") for c in llm.calls)
    assert "## ภาพรวมตลาด" in result


def test_brand_reference_includes_silent_application_reminder():
    """Brand context/guidelines must be followed silently, not emitted."""
    cfg = _competitor_config()
    cfg["use_brand_reference"] = True
    agent = CompetitorAnalysisAgent(
        cfg,
        FakeLLM(),
        brand_reference="บุคลิก: อบอุ่น\nคำต้องห้าม: ตัว -> ระบุตำแหน่ง",
    )
    system = agent._build_system_prompt()
    assert "ห้ามนำข้อความเหล่านั้น" in system
    assert "ห้ามเขียนประโยคแบบ" in system


def test_system_prompt_forbids_future_extrapolation():
    """Competitor analysis must not forecast future markets without evidence."""
    cfg = _competitor_config()
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    system = agent._build_system_prompt()
    assert "อนาคต" in system or "คาดการณ์" in system, (
        "system prompt ควรห้ามคาดการณ์อนาคตหรือข้อมูลที่ไม่มีหลักฐาน"
    )


def test_competitor_analysis_config_uses_brand_reference_not_brand_context():
    """competitor_analysis ต้องเปิด brand_reference และปิด brand_context"""
    cfg = _competitor_config()
    assert cfg.get("use_brand_context") is False
    assert cfg.get("use_brand_reference") is True


def test_competitor_analysis_system_prompt_has_reference_no_rules():
    """prompt ของ competitor_analysis ต้องมี brand_reference แต่ไม่มี brand hard/soft rules."""
    cfg = _competitor_config()
    brand_rules = load_brand_priority(BRAND_DIR)
    brand_reference = load_brand_reference(BRAND_DIR, product_id="CACGO K77")
    agent = CompetitorAnalysisAgent(
        cfg,
        FakeLLM(),
        brand_rules=brand_rules,
        brand_reference=brand_reference,
    )
    system = agent._build_system_prompt()

    # ไม่ควรมี writing rules จาก brand
    assert "คำ/วลีที่ห้ามใช้" not in system
    assert "คำที่ควรใช้แทน" not in system
    assert "บุคลิก" not in system
    assert "โทนเสียง" not in system

    # ต้องมี analytical context จาก brand_reference
    assert "Target Audience" in system
    assert "Product Positioning" in system
    assert "CACGO K77" in system


def test_competitor_analysis_prompt_shorter_without_brand_context():
    """ปิด brand_context ต้องทำให้ system prompt สั้นลง."""
    brand_rules = load_brand_priority(BRAND_DIR)
    brand_reference = load_brand_reference(BRAND_DIR, product_id="CACGO K77")
    base_cfg = _competitor_config()

    cfg_with_context = {**base_cfg, "use_brand_context": True, "use_brand_reference": False}
    cfg_without_context = {**base_cfg, "use_brand_context": False, "use_brand_reference": True}

    agent_with = CompetitorAnalysisAgent(cfg_with_context, FakeLLM(), brand_rules=brand_rules, brand_reference=brand_reference)
    agent_without = CompetitorAnalysisAgent(cfg_without_context, FakeLLM(), brand_rules=brand_rules, brand_reference=brand_reference)

    prompt_with = agent_with._build_system_prompt()
    prompt_without = agent_without._build_system_prompt()

    assert len(prompt_without) < len(prompt_with)


def test_content_creator_still_uses_brand_context():
    """agent อื่น เช่น content_creator ต้องยังได้รับ brand hard/soft rules."""
    cfg = get_agent_config(load_config(), "content_creator")
    brand_rules = load_brand_priority(BRAND_DIR)
    brand_reference = load_brand_reference(BRAND_DIR)
    agent = BaseAgent(cfg, MagicMock(), brand_rules=brand_rules, brand_reference=brand_reference)
    system = agent._build_system_prompt()

    assert "คำ/วลีที่ห้ามใช้" in system
    assert "บุคลิก" in system
    assert "คำที่ควรใช้แทน" in system
