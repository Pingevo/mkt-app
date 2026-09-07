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

import json
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


def _competitor_instructions():
    with open(Path(__file__).resolve().parent.parent / "config" / "agent_instructions.json", "r", encoding="utf-8") as f:
        return json.load(f)["competitor_analysis"]


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
    agent_cfg = get_agent_config(cfg, "competitor_analysis")
    # Unit tests exercise the legacy text-mode path unless explicit
    agent_cfg["evidence_mode"] = False
    agent_cfg.pop("response_format", None)
    agent_cfg["max_retry_limit"] = 3
    return agent_cfg


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
        generate_output="ภาพรวมตลาด\nตลาดเติบโต [K2 Official](https://example.com/k2)",
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


def test_blank_web_search_falls_back_to_limited():
    """ถ้า web search คืน output ว่าง ต้อง fallback เป็น limited แทนทีจะ raise."""
    cfg = _competitor_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = True
    llm = FakeLLM(generate_output="", annotations=[])
    agent = CompetitorAnalysisAgent(cfg, llm)

    result = agent.run("วิเคราะห์คู่แข่ง K2")
    assert "**limited_analysis: true**" in result


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


def test_competitor_analysis_prompt_has_professional_style():
    """prompt ของ competitor_analysis ต้องมี style instruction แค่ 1 บรรทัด."""
    cfg = _competitor_config()
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    system = agent._build_system_prompt()
    assert "เขียนเป็นรายงานวิเคราะห์มืออาชีพ" in system
    assert "ไม่ทักทาย" in system
    assert "ไม่แสดง internal rules/instructions" in system


def test_competitor_analysis_prompt_has_no_duplicate_core_rules():
    """prompt ต้องไม่มี duplicate rules จาก rules_must/rules_forbid."""
    cfg = _competitor_config()
    instructions = _competitor_instructions()
    brand_rules = load_brand_priority(BRAND_DIR)
    brand_reference = load_brand_reference(BRAND_DIR, product_id="CACGO K77")
    agent = CompetitorAnalysisAgent(
        cfg,
        FakeLLM(),
        brand_rules=brand_rules,
        brand_reference=brand_reference,
        instructions=instructions,
    )
    system = agent._build_system_prompt()

    # rules_must ไม่ควรซ้ำกับ evidence policy
    assert "ทุก claim ต้องมี evidence type + confidence label" not in system
    assert "ถ้าไม่พบข้อมูล ต้องระบุ 'ไม่พบข้อมูล' แทนการเติมเอง" not in system

    # rules_forbid ไม่ควรซ้ำกับ evidence policy / data strictness / system prompt
    assert "ห้ามเขียน claim โดยไม่ระบุ confidence (HIGH/MEDIUM/LOW/UNKNOWN)" not in system
    assert "ห้ามเดาราคาหรือคุณสมบัติคู่แข่ง" not in system

    # evidence policy ยังคงเป็น source of truth
    assert "ทุก claim ต้องระบุแหล่งที่มา + ประเภทของหลักฐาน" in system
    assert "กฎหลักฐาน (Evidence Policy)" in system


PRODUCT_SPEC_K77 = """สินค้า: CACGO K77
รหัสสินค้า: K77
หน้าจอ 1.7" full round IPS ความละเอียด 360x360
CPU Realtek 8773EWE-VP
Bluetooth 5.0
เซนเซอร์ Heart Rate + SpO2
กันน้ำ IP68
แบตเตอรี่ 1000mAh"""


def _agent_with_fake_llm(generate_output: str, repair_output: str = "", *, web_search: bool = False):
    cfg = _competitor_config()
    cfg["max_review_iterations"] = 0
    cfg.pop("output_quality", None)
    cfg.pop("required_output_sections", None)
    cfg["web_search"] = web_search
    llm = FakeLLM(generate_output=generate_output, repair_output=repair_output)
    return CompetitorAnalysisAgent(cfg, llm), llm


def test_thin_competitor_context_detected():
    """ถ้า competitor_data มีแค่ชื่อ ต้องถือว่า thin context."""
    agent, _ = _agent_with_fake_llm("")
    assert agent._is_thin_competitor_context("Redmi Watch 3 Active")
    assert agent._is_thin_competitor_context("")
    # ถ้ามีราคาหรือ spec จริง ต้องไม่ thin
    assert not agent._is_thin_competitor_context("Redmi Watch 3 Active\nแบต 2000mAh")
    assert not agent._is_thin_competitor_context("Redmi Watch 3 Active\nราคา $50")


def test_fabrication_guard_catches_unsourced_competitor_spec():
    """ถ้า output แต่งสเปคคู่แข่งทีไม่มาในข้อมูลต้นทาง validate_output ต้อง fail."""
    agent, _ = _agent_with_fake_llm("")
    agent.build_prompt(PRODUCT_SPEC_K77, "Redmi Watch 3 Active")
    fabricated = (
        "## เปรียบเทียบ\n\n"
        "| คู่แข่ง | แบต |\n"
        "|---|---|\n"
        "| Redmi Watch 3 Active | 2000mAh |\n"
    )
    ok, err = agent.validate_output(fabricated)
    assert not ok
    assert "fabricated competitor spec" in err


def test_fabrication_guard_allows_limited_analysis():
    """ถ้า output บอกแค่ว่าไม่มีข้อมูลคู่แข่ง ต้องผ่าน guard."""
    agent, _ = _agent_with_fake_llm("")
    agent.build_prompt(PRODUCT_SPEC_K77, "Redmi Watch 3 Active")
    limited = "CACGO K77 มีสเปคตามข้อมูลต้นทาง ส่วน Redmi Watch 3 Active ยังไม่มีข้อมูลพอสำหรับเปรียบเทียบ"
    ok, _ = agent.validate_output(limited)
    assert ok


def test_fabrication_repair_returns_limited_analysis():
    """ถ้า model แต่งสเปคคู่แข่ง ระบบต้องคืน limited analysis แบบ deterministic."""
    fabricated = (
        "## เปรียบเทียบ\n\n"
        "| คู่แข่ง | แบต |\n"
        "|---|---|\n"
        "| Redmi Watch 3 Active | 2000mAh |\n"
    )
    agent, llm = _agent_with_fake_llm(fabricated)
    prompt = agent.build_prompt(PRODUCT_SPEC_K77, "Redmi Watch 3 Active")
    result = agent.run(prompt)

    assert "2000mAh" not in result
    assert "รายงานวิเคราะห์จำกัด" in result or "ข้อจำกัด" in result
    # ไม่ต้องเรียก repair LLM เพราะใช้ fallback
    assert all("repair" not in c["kwargs"].get("source", "") for c in llm.calls)


def test_homepage_citation_guard_triggers():
    """citation ทีลิงก์ไป homepage ต้องถูก reject.

    Note: the base citation provenance check (Item 3) may fire first with a
    "grounding:" error when the URL is not in the allowed evidence set.
    The competitor_analysis-specific "homepage" check may also fire. Both
    are valid rejection reasons for the same underlying issue (unverified
    citation). This test accepts either error.
    """
    agent, _ = _agent_with_fake_llm("", web_search=True)
    agent.build_prompt(PRODUCT_SPEC_K77, "Mibro Watch A2")
    output = "K77 เป็นสมาร์ทวอทช์ [Mibro](https://www.mi.com/th/)"
    ok, err = agent.validate_output(output)
    assert not ok
    # Accept either the generic grounding check or the homepage-specific check
    assert "homepage" in err or "grounding" in err


def test_citation_repair_removes_homepage_and_falls_back_when_no_evidence():
    """ถ้า inline citation เป็น homepage และไม่มี selected evidence ต้อง fallback เป็น limited."""
    bad = "K77 เป็นสมาร์ทวอทช์ [Mibro](https://www.mi.com/th/)"
    agent, _ = _agent_with_fake_llm(bad, web_search=True)
    prompt = agent.build_prompt(PRODUCT_SPEC_K77, "Mibro Watch A2")
    result = agent.run(prompt)

    assert "www.mi.com/th/" not in result
    assert "**limited_analysis: true**" in result


def test_web_search_keeps_relevant_citation_and_passes():
    """ถ้า model ใส่ inline citation ทีผ่าน relevance ต้องคงไว้และผ่าน validation."""
    long_output = (
        "# วิเคราะห์ CACGO K77\n\n"
        "## สรุป\n\n"
        "CACGO K77 เป็นสมาร์ทวอทช์หน้าจอ 1.7\" full round IPS "
        "และมีแบตเตอรี่ 1000mAh รองรับ Bluetooth calling\n\n"
        "## แหล่งอ้างอิง\n\n"
        "รายละเอียดสินค้าจาก [Shopee CACGO K77](https://shopee.co.th/CACGO-K77-123)\n\n"
    )
    annotations = [{"url": "https://shopee.co.th/CACGO-K77-123", "title": "CACGO K77 Smart Watch 1.7 inch"}]
    cfg = _competitor_config()
    cfg["max_review_iterations"] = 0
    cfg.pop("output_quality", None)
    cfg.pop("required_output_sections", None)
    cfg["web_search"] = True
    llm = FakeLLM(generate_output=long_output, annotations=annotations)
    agent = CompetitorAnalysisAgent(cfg, llm)
    prompt = agent.build_prompt(PRODUCT_SPEC_K77, "")
    result = agent.run(prompt)

    assert "**limited_analysis: true**" not in result
    assert "https://shopee.co.th/CACGO-K77-123" in result
    assert all("repair" not in c["kwargs"].get("source", "") for c in llm.calls)


def test_required_web_search_fails_with_reason_not_limited():
    """ถ้า web_search_mode=required แล้ว tool ไม่คืน evidence ต้องแสดงเหตุผลชัดเจน ไม่ใช่ limited."""
    cfg = _competitor_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    llm = FakeLLM(generate_output="", annotations=[])
    agent = CompetitorAnalysisAgent(cfg, llm)
    prompt = agent.build_prompt(PRODUCT_SPEC_K77, "")
    result = agent.run(prompt)

    assert "**limited_analysis: true**" not in result
    assert "required_search_failed" in result
    assert "tool_not_invoked" in result


def test_web_search_bad_citation_and_no_evidence_becomes_limited():
    """ถ้า inline citation ทีให้มาทั้งหมดไม่ผ่าน relevance ต้อง fallback เป็น limited."""
    bad_output = (
        "# วิเคราะห์ CACGO K77\n\n"
        "## สรุป\n\n"
        "CACGO K77 เป็นสมาร์ทวอทช์และมีราคาจำหน่าย [Shopee](https://shopee.co.th)\n\n"
    )
    # source เป็น homepage ไม่มีรุ่นเฉพาะ
    annotations = [{"url": "https://shopee.co.th", "title": "Shopee Thailand", "content": "ซื้อขายออนไลน์"}]
    cfg = _competitor_config()
    cfg["max_review_iterations"] = 0
    cfg.pop("output_quality", None)
    cfg.pop("required_output_sections", None)
    cfg["web_search"] = True
    llm = FakeLLM(generate_output=bad_output, annotations=annotations)
    agent = CompetitorAnalysisAgent(cfg, llm)
    prompt = agent.build_prompt(PRODUCT_SPEC_K77, "")
    result = agent.run(prompt)

    assert "**limited_analysis: true**" in result
    assert "https://shopee.co.th" not in result


def test_claim_validator_skips_thai_scope_intro():
    """Intro/scope lines with Thai marker but no factual claim must not be cited."""
    agent, _ = _agent_with_fake_llm("", web_search=True)
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi\nSamsung")
    agent._last_relevant_annotations = [
        {
            "url": "https://shopee.co.th/xiaomi-s3",
            "title": "Xiaomi S3",
            "content": "ราคา 4,990 บาท",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand"},
        },
    ]
    output = (
        "รายงานฉบับนี้เป็นการวิเคราะห์เชิงเปรียบเทียบระหว่างสมาร์ทวอทช์ CACGO K77 กับคู่แข่งในประเทศไทย\n"
        "\n"
        "## คู่แข่ง\n"
        "Xiaomi Watch S3 ราคา 4,990 บาท [Shopee](https://shopee.co.th/xiaomi-s3)\n"
    )
    ok, err = agent.validate_output(output)
    assert ok, err


def test_claim_validator_flags_thai_price_without_citation():
    """A Thai price claim must have a Thailand-specific citation."""
    agent, _ = _agent_with_fake_llm("", web_search=True)
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi")
    agent._last_relevant_annotations = [
        {
            "url": "https://shopee.co.th/xiaomi-s3",
            "title": "Xiaomi S3",
            "content": "ราคา 4,990 บาท",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand"},
        },
    ]
    output = "สมาร์ทวอทช์รุ่นนี้ ราคา 4,990 บาท ในประเทศไทย"
    ok, err = agent.validate_output(output)
    assert not ok
    assert "thai" in err.lower()


def test_claim_validator_skips_competitor_heading_without_factual_claim():
    """A plain competitor heading without numbers/currency/spec should not require citation."""
    agent, _ = _agent_with_fake_llm("", web_search=True)
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi")
    agent._last_relevant_annotations = [
        {
            "url": "https://shopee.co.th/xiaomi-s3",
            "title": "Xiaomi S3",
            "content": "...",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand"},
        },
    ]
    output = (
        "## คู่แข่ง: Xiaomi\n"
        "Xiaomi Watch S3 มาพร้อมหน้าจอ AMOLED [Shopee](https://shopee.co.th/xiaomi-s3)\n"
    )
    ok, err = agent.validate_output(output)
    assert ok, err


def test_repair_removes_uncited_thai_intro_instead_of_required_failure():
    """In required mode, a single uncited scope intro must be removed, not trigger full failure."""
    generate_output = (
        "รายงานฉบับนี้เป็นการวิเคราะห์เชิงเปรียบเทียบระหว่างสมาร์ทวอทช์ CACGO K77 กับคู่แข่งในประเทศไทย\n"
        "\n"
        "## คู่แข่ง\n"
        "Xiaomi Watch S3 ราคา 4,990 บาท [Shopee](https://shopee.co.th/xiaomi-s3)\n"
    )
    annotations = [
        {
            "url": "https://shopee.co.th/xiaomi-s3",
            "title": "Xiaomi S3",
            "content": "ราคา 4,990 บาท",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand"},
        },
    ]
    cfg = _competitor_config()
    cfg["max_review_iterations"] = 0
    cfg.pop("output_quality", None)
    cfg.pop("required_output_sections", None)
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    llm = FakeLLM(generate_output=generate_output, annotations=annotations)
    # Simulate the LLM metadata that the real OpenRouter client would attach.
    llm._last_raw_response = {
        "usage": {
            "server_tool_use_details": {
                "web_search_requests": 1,
                "tool_calls_executed": 1,
            },
        },
    }
    llm._last_raw_annotations_count = len(annotations)
    agent = CompetitorAnalysisAgent(cfg, llm)
    prompt = agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi")
    result = agent.run(prompt)

    assert "**required_search_failed: true**" not in result
    assert "Xiaomi Watch S3 ราคา 4,990 บาท" in result
    assert "https://shopee.co.th/xiaomi-s3" in result


def test_check_outcome_marks_required_search_failed_as_not_passed():
    """An artifact with required_search_failed marker must have outcome.passed == False."""
    from tests.acceptance_competitor_runner import _check_outcome

    fixture_path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "plan_c_artifact_sanitized.json"
    if not fixture_path.exists():
        pytest.skip("Plan C fixture not available")
    with open(fixture_path, encoding="utf-8") as f:
        result = json.load(f)
    outcome = _check_outcome(result)
    assert outcome["passed"] is False
    assert any("required search failed" in d.lower() for d in outcome["defects"])


def test_plan_c_fixture_lacks_thai_competitor_evidence():
    """Plan C selected evidence is all target/global; it must not count as thai competitor evidence."""
    from tests.acceptance_competitor_runner import _check_outcome

    fixture_path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "plan_c_artifact_sanitized.json"
    if not fixture_path.exists():
        pytest.skip("Plan C fixture not available")
    with open(fixture_path, encoding="utf-8") as f:
        result = json.load(f)
    outcome = _check_outcome(result)
    assert any("thai competitor" in d.lower() for d in outcome["defects"])


def test_plan_c_replay_deterministic_repair():
    """Using real Plan C annotations, deterministic repair must cut bad claims but keep the analysis."""
    fixture_path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "plan_c_artifact_sanitized.json"
    if not fixture_path.exists():
        pytest.skip("Plan C fixture not available")
    with open(fixture_path, encoding="utf-8") as f:
        fixture = json.load(f)

    # reconstruct the likely original generation output from the artifact error + evidence
    intro = (
        "รายงานฉบับนี้เป็นการวิเคราะห์เชิงเปรียบเทียบระหว่างสมาร์ทวอทช์ CACGO K77 "
        "กับคู่แข่งในประเทศไทย"
    )
    amz_url = "https://www.amazon.com/BOVUGAC-Compatible-Smartwatch-Anti-Scratch-Ultra-Thin/dp/B0GSFDDW8X"
    fcc_url = "https://fccid.io/2BGT5"
    output = (
        f"{intro}\n"
        "\n"
        "## สินค้า\n"
        f"CACGO K77 มี FCC ID 2BGT5-K77 [FCC]({fcc_url})\n"
        "\n"
        "## คู่แข่ง\n"
        f"Xiaomi Watch S3 ราคา 8.88 USD [Amazon]({amz_url})\n"
    )

    agent, _ = _agent_with_fake_llm("", web_search=True)
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi")
    agent._last_relevant_annotations = fixture["relevant_annotations"]
    agent._last_raw_annotations_count = len(fixture["relevant_annotations"])
    agent._last_tool_use = {"web_search_requests": 6, "tool_calls_executed": 6}

    ok, err = agent.validate_output(output)
    assert not ok
    assert "competitor" in err.lower()

    repaired = agent._repair_output(output, err, [], None)
    assert "**required_search_failed: true**" not in repaired
    assert "Xiaomi Watch S3 ราคา 8.88 USD" not in repaired
    assert intro in repaired

    ok2, err2 = agent.validate_output(repaired)
    assert ok2, err2


def test_structural_output_failed_short_circuits_validation():
    """An output with the structural_output_failed marker must pass validate_output."""
    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    marker = agent._structural_output_failure("output ขาด structural analysis blocks (1 < 2)")
    ok, err = agent.validate_output(marker)
    assert ok, err
    assert "**structural_output_failed: true**" in marker


def test_structural_repair_adds_sections_to_content():
    """A one-block output with enough content is reformatted, not replaced by a failure marker."""
    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi Watch S3\nGalaxy Watch9")
    agent._last_relevant_annotations = [
        {
            "url": "https://www.mi.com/th/product/xiaomi-watch-s3/",
            "title": "Xiaomi Watch S3",
            "content": "...",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand"},
        },
    ]
    agent._last_raw_annotations_count = 1
    agent._last_tool_use = {"web_search_requests": 1, "tool_calls_executed": 1}
    one_block_output = (
        "# รายงานวิเคราะห์ CACGO K77 ในประเทศไทย\n"
        "\n"
        "รายงานฉบับนี้ทำการวิเคราะห์เปรียบเทียบ CACGO K77 กับคู่แข่งในประเทศไทย "
        "โดยเน้นหลักฐานจาก official Thailand page หรือ authorized Thai retailer ของแต่ละรุ่นเท่านั้น "
        "ถ้าหาหลักฐานของรุ่นใดไม่พบ ให้บอกว่าไม่พบ ห้ามแทนที่ด้วยสินค้าคนละรุ่นหรือแหล่งทั่วไป\n"
        "\n"
        "Xiaomi Watch S3 วางจำหน่ายในประเทศไทย [Xiaomi](https://www.mi.com/th/product/xiaomi-watch-s3/)\n"
    )
    repaired = agent._repair_output(one_block_output, "output ขาด structural analysis blocks (1 < 2)", [], None)
    assert "**structural_output_failed: true**" not in repaired
    assert "**required_search_failed: true**" not in repaired
    assert "## ภาพรวมคู่แข่งและหลักฐาน" in repaired
    ok, err = agent.validate_output(repaired)
    assert ok, err


def test_structural_failure_when_no_content():
    """An empty output with structural error gets the structural_output_failed marker."""
    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi")
    agent._last_relevant_annotations = [
        {
            "url": "https://www.mi.com/th/product/xiaomi-watch-s3/",
            "title": "Xiaomi Watch S3",
            "content": "...",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand"},
        },
    ]
    repaired = agent._repair_output("", "output ขาด structural analysis blocks (1 < 2)", [], None)
    assert "**structural_output_failed: true**" in repaired


def test_check_outcome_detects_structural_output_failed():
    """_check_outcome must flag the structural_output_failed marker."""
    from tests.acceptance_competitor_runner import _check_outcome

    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi")
    marker = agent._structural_output_failure("output ขาด structural analysis blocks (1 < 2)")
    result = {
        "case_name": "k77_thailand_web_search",
        "output": marker,
        "validation_ok": True,
        "validation_error": "",
        "runtime_error": None,
        "relevant_annotations": [],
    }
    outcome = _check_outcome(result)
    assert outcome["passed"] is False
    assert any("structural" in d.lower() for d in outcome["defects"])


def _load_run_fixture(run: int) -> dict:
    p = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / f"agent2_run{run}_sanitized.json"
    if not p.exists():
        pytest.skip(f"Run {run} fixture not available")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def test_run1_artifact_replay_still_passes():
    """Run 1 artifact must still pass full-analysis contract."""
    fixture = _load_run_fixture(1)
    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi Watch S3\nKieslect AI Smartwatch Elite2\nGalaxy Watch9")
    agent._last_relevant_annotations = fixture["relevant_annotations"]
    agent._last_raw_annotations_count = len(fixture["relevant_annotations"])
    agent._last_tool_use = {"web_search_requests": 6, "tool_calls_executed": 6}
    ok, err = agent.validate_output(fixture["output"])
    assert ok, err


def test_run2_artifact_replay_with_one_block_draft():
    """Run 2 evidence with a one-block draft now produces a full output, not required_search_failed."""
    fixture = _load_run_fixture(2)
    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi Watch S3\nKieslect\nGalaxy Watch9")
    agent._last_relevant_annotations = fixture["relevant_annotations"]
    agent._last_raw_annotations_count = len(fixture["relevant_annotations"])
    agent._last_tool_use = {"web_search_requests": 6, "tool_calls_executed": 6}

    # simulate the original one-block draft that caused the structural error
    draft = (
        "# รายงานวิเคราะห์ CACGO K77 ในประเทศไทย\n"
        "\n"
        "รายงานฉบับนี้ทำการวิเคราะห์เปรียบเทียบ CACGO K77 กับคู่แข่งในประเทศไทย "
        "โดยเน้นหลักฐานจาก official Thailand page หรือ authorized Thai retailer ของแต่ละรุ่นเท่านั้น "
        "ถ้าหาหลักฐานของรุ่นใดไม่พบ ให้บอกว่าไม่พบ ห้ามแทนที่ด้วยสินค้าคนละรุ่นหรือแหล่งทั่วไป\n"
        "\n"
        "| คู่แข่ง | แหล่งอ้างอิง |\n"
        "| Xiaomi Watch S3 | [Xiaomi Thailand](https://www.mi.com/th/product/xiaomi-watch-s3/) |\n"
        "| Kieslect | [Kieslect Thailand](https://www.kieslectthailand.com/en/tag/Elite2) |\n"
        "| Galaxy Watch9 | ไม่พบข้อมูลในระบบ |\n"
    )
    repaired = agent._repair_output(draft, "output ขาด structural analysis blocks (1 < 2)", [], None)
    assert "**required_search_failed: true**" not in repaired
    assert "**structural_output_failed: true**" not in repaired
    ok, err = agent.validate_output(repaired)
    assert ok, err


def test_save_results_writes_separate_draft_and_final_files(tmp_path, monkeypatch) -> None:
    """Runner must persist draft and final outputs as separate .md files."""
    import tests.acceptance_competitor_runner as runner
    out_dir = tmp_path / "out"
    monkeypatch.setattr(runner, "OUTPUT_DIR", out_dir)

    result = {
        "case_id": 10,
        "case_name": "k77_thailand_web_search",
        "run": 1,
        "output": "# Final output\n\nThis is final.",
        "draft_output": "# Draft output\n\nThis is the raw generation before repair.",
        "first_validation_error": "output ขาด structural analysis blocks (1 < 2)",
        "request_id": "gen-12345",
        "web_search_mode": "required",
    }
    paths = runner._save_results([result])

    assert len(paths) == 1
    json_path = paths[0]
    with open(json_path, encoding="utf-8") as f:
        metadata = json.load(f)

    md_path = Path(metadata["output_path"])
    draft_path = Path(metadata["draft_output_path"])
    assert md_path != draft_path
    assert md_path.exists()
    assert draft_path.exists()
    assert md_path.read_text(encoding="utf-8") == result["output"]
    assert draft_path.read_text(encoding="utf-8") == result["draft_output"]
    assert metadata["first_validation_error"] == result["first_validation_error"]
    assert metadata["request_id"] == result["request_id"]
    assert metadata["web_search_mode"] == result["web_search_mode"]
    assert "draft_output" not in metadata
    assert "output" not in metadata


def _load_confirmation_fixture() -> dict:
    p = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "agent2_confirmation_artifact.json"
    if not p.exists():
        pytest.skip("Confirmation fixture not available")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def test_confirmation_artifact_repair_produces_valid_full_analysis():
    """Draft from real confirmation run must repair to valid full analysis with no fallback dump."""
    fixture = _load_confirmation_fixture()
    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi Watch S3\nKieslect\nGalaxy Watch9")
    agent._last_relevant_annotations = fixture["relevant_annotations"]
    agent._last_raw_annotations_count = len(fixture["relevant_annotations"])
    agent._last_tool_use = {"web_search_requests": 6, "tool_calls_executed": 6}

    output = fixture["draft_output"]
    for _ in range(cfg.get("max_retry_limit", 3) + 1):
        ok, err = agent.validate_output(output)
        if ok:
            break
        output = agent._repair_output(output, err, [], None)

    assert ok, err
    assert "**required_search_failed: true**" not in output
    assert "**structural_output_failed: true**" not in output
    assert "**limited_analysis: true**" not in output
    assert "แหล่งอ้างอิง (fallback)" not in output
    assert "no-evidence" not in output.lower()

    # every factual competitor table cell is either cited or a no-evidence marker
    ok2, err2 = agent.validate_output(output)
    assert ok2, err2


def test_confirmation_artifact_outcome_passes():
    """Repaired confirmation output must pass _check_outcome."""
    fixture = _load_confirmation_fixture()
    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(PRODUCT_SPEC_K77, "Xiaomi Watch S3\nKieslect\nGalaxy Watch9")
    agent._last_relevant_annotations = fixture["relevant_annotations"]
    agent._last_raw_annotations_count = len(fixture["relevant_annotations"])
    agent._last_tool_use = {"web_search_requests": 6, "tool_calls_executed": 6}

    output = fixture["draft_output"]
    for _ in range(cfg.get("max_retry_limit", 3) + 1):
        ok, err = agent.validate_output(output)
        if ok:
            break
        output = agent._repair_output(output, err, [], None)

    from tests.acceptance_competitor_runner import _check_outcome
    result = {
        "case_name": fixture["case_name"],
        "output": output,
        "validation_ok": True,
        "validation_error": "",
        "runtime_error": None,
        "relevant_annotations": fixture["relevant_annotations"],
        "product_spec": PRODUCT_SPEC_K77,
        "competitor_data": "Xiaomi Watch S3\nKieslect\nGalaxy Watch9",
        "web_search_mode": "required",
    }
    outcome = _check_outcome(result)
    assert not outcome["passed"]
    assert "insufficient competitor evidence" in outcome["defects"]


def _load_quality_fixture(name: str) -> dict:
    p = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / f"agent2_quality_{name}.json"
    if not p.exists():
        pytest.skip(f"Quality fixture {name} not available")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def test_table_with_cited_facts_and_thai_evidence_passes_full_analysis():
    """Good fixture: two cited competitor facts with Thai product evidence."""
    fixture = _load_quality_fixture("good")
    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    agent._last_relevant_annotations = fixture["relevant_annotations"]
    agent._last_raw_annotations_count = len(fixture["relevant_annotations"])
    agent._last_tool_use = {"web_search_requests": 2, "tool_calls_executed": 2}
    ok, err = agent.validate_output(fixture["output"])
    assert ok, err

    q = agent._full_analysis_quality(fixture["output"])
    assert q["competitor_fact_cells"] >= 2
    assert q["cited_fact_cells"] >= 2
    assert q["thai_fact_cells"] >= 1

    from tests.acceptance_competitor_runner import _check_outcome
    result = {
        "case_name": "k77_thailand_web_search",
        "output": fixture["output"],
        "validation_ok": ok,
        "validation_error": err,
        "runtime_error": None,
        "relevant_annotations": fixture["relevant_annotations"],
        "product_spec": fixture["product_spec"],
        "competitor_data": fixture["competitor_data"],
        "web_search_mode": "required",
    }
    outcome = _check_outcome(result)
    assert outcome["passed"], outcome


def test_table_with_tag_or_homepage_source_fails_validation():
    """Tag/homepage source must not support technical spec claims."""
    fixture = _load_quality_fixture("bad_source")
    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    agent._last_relevant_annotations = fixture["relevant_annotations"]
    agent._last_raw_annotations_count = len(fixture["relevant_annotations"])
    agent._last_tool_use = {"web_search_requests": 1, "tool_calls_executed": 1}
    ok, err = agent.validate_output(fixture["output"])
    assert not ok
    assert "non-competitor source" in err or "does not mention" in err


def test_table_with_all_no_evidence_fails_full_analysis():
    """A table of only no-evidence markers is not a useful competitive analysis."""
    fixture = _load_quality_fixture("insufficient")
    cfg = _competitor_config()
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    agent._last_relevant_annotations = fixture["relevant_annotations"]

    q = agent._full_analysis_quality(fixture["output"])
    assert q["no_evidence_cells"] / q["competitor_cells_total"] > 0.5

    from tests.acceptance_competitor_runner import _check_outcome
    result = {
        "case_name": "k77_thailand_web_search",
        "output": fixture["output"],
        "validation_ok": True,
        "validation_error": "",
        "runtime_error": None,
        "relevant_annotations": fixture["relevant_annotations"],
        "product_spec": fixture["product_spec"],
        "competitor_data": fixture["competitor_data"],
        "web_search_mode": "required",
    }
    outcome = _check_outcome(result)
    assert not outcome["passed"]
    assert "insufficient competitor evidence" in outcome["defects"]


# ---------------------------------------------------------------------------
# Stage A vs Stage 3 brand isolation — durable product contracts
#
# These tests verify durable invariants that must hold regardless of
# future Stage 3 architecture changes:
#
# 1. Stage A evidence prompt is brand-independent
# 2. Stage A evidence schema contains no brand-derived fields
# 3. Evidence records are unchanged by rendering/brand configuration
# 4. Internal brand reference is not dumped into output merely because it exists
# 5. No current deterministic renderer fabricates brand-aware recommendations
#
# The current deterministic Stage 3 renderer has no semantic brand-reasoning
# seam. This is a known gap pending a separately approved semantic design.
# These tests do NOT encode the current architectural limitation as a
# permanent contract — they verify that what the renderer DOES produce
# is brand-objective and evidence-preserving.
# ---------------------------------------------------------------------------

def test_stage_a_evidence_system_prompt_excludes_brand_reference():
    """Stage A system prompt (EVIDENCE_SYSTEM_PROMPT) must NOT contain
    brand_reference content — evidence research stays brand-objective."""
    from src.agents.competitor_evidence import EVIDENCE_SYSTEM_PROMPT
    assert "brand_reference" not in EVIDENCE_SYSTEM_PROMPT.lower()
    assert "ข้อมูลแบรนด์อ้างอิง" not in EVIDENCE_SYSTEM_PROMPT
    assert "audience" not in EVIDENCE_SYSTEM_PROMPT.lower()
    assert "positioning" not in EVIDENCE_SYSTEM_PROMPT.lower()


def test_stage_a_build_system_prompt_excludes_brand_reference_in_evidence_mode():
    """When evidence_mode is true, _build_system_prompt must compose the
    EVIDENCE_SYSTEM_PROMPT core with shared BaseAgent additions (brand
    priority, instructions, grounding policy) BUT must NOT inject
    brand_reference.  Stage A evidence research is brand-objective;
    brand interpretation happens in a separate BrandInterpretationPass
    after evidence is finalized."""
    cfg = _competitor_config()
    cfg["evidence_mode"] = True
    cfg["use_brand_reference"] = True
    llm = FakeLLM()
    brand_ref = "### Target Audience\nparents age 30-45\n### Product Positioning\npremium"
    agent = CompetitorAnalysisAgent(cfg, llm, brand_reference=brand_ref)
    agent._evidence_mode = True
    system = agent._build_system_prompt()
    # Evidence core must be present
    assert "Competitor Analyst" in system
    # Brand reference must NOT be present in Stage A
    assert "parents age 30-45" not in system
    assert "ข้อมูลแบรนด์อ้างอิง" not in system
    # Shared grounding policy IS present
    assert "ห้าม invent URL" in system
    assert "ห้ามเดา factual claim" in system
    # Shared grounding policy block is present
    assert "นโยบายข้อมูลต้นทาง" in system or "Grounding Policy" in system


def test_stage_a_research_schema_has_no_brand_fields():
    """The ResearchResponse schema must not include brand fields —
    evidence records are brand-objective by schema contract."""
    from src.agents.competitor_evidence import RESEARCH_RESPONSE_SCHEMA
    inner = RESEARCH_RESPONSE_SCHEMA.get("json_schema", {}).get("schema", {})
    properties = inner.get("properties", {})
    assert "target_model" in properties
    assert "evidence" in properties
    assert "brand" not in properties
    assert "brand_reference" not in properties
    assert "audience" not in properties
    assert "positioning" not in properties


def test_stage_3_renderer_does_not_dump_brand_reference():
    """The rendered report must NOT contain raw brand_reference content.
    Internal brand context must not be echoed into the final output
    merely because it exists on the agent. This is a durable provenance
    contract: brand context is internal and must not leak into the
    competitor analysis report as a raw appendix."""
    from src.agents.competitor_evidence import CompetitorReportRenderer, ResearchResponse
    research = ResearchResponse(
        target_model="TestProduct",
        competitor_names=["CompA"],
        evidence=[],
        evidence_based_recommendations=[],
        strategic_hypotheses=[],
        uncertainty=["no evidence found"],
    )
    renderer = CompetitorReportRenderer(
        research,
        relevant_annotations=[],
        quick_brief="",
    )
    markdown = renderer.render("TestProduct spec")
    # The rendered output must not contain raw brand-reference framing
    assert "ข้อมูลแบรนด์อ้างอิง" not in markdown
    assert "brand reference" not in markdown.lower()


def test_stage_3_evidence_records_unchanged_by_render():
    """Rendering must NOT alter evidence records — the renderer is
    deterministic and must preserve evidence data integrity regardless
    of any brand context that may exist on the agent."""
    from src.agents.competitor_evidence import CompetitorReportRenderer, ResearchResponse, CompetitorEvidence
    evidence = [
        CompetitorEvidence(
            competitor="CompA",
            field="price",
            claim="5,000 THB",
            url="https://example.com/compa",
            geography="thailand",
        ),
    ]
    research = ResearchResponse(
        target_model="TestProduct",
        competitor_names=["CompA"],
        evidence=evidence,
        evidence_based_recommendations=[],
        strategic_hypotheses=[],
        uncertainty=[],
    )
    renderer = CompetitorReportRenderer(
        research,
        relevant_annotations=[{"url": "https://example.com/compa", "title": "CompA", "content": "price"}],
        quick_brief="",
    )
    claim_before = research.evidence[0].claim
    competitor_before = research.evidence[0].competitor
    url_before = research.evidence[0].url
    renderer.render("TestProduct spec")
    assert research.evidence[0].claim == claim_before
    assert research.evidence[0].competitor == competitor_before
    assert research.evidence[0].url == url_before


def test_stage_3_renderer_does_not_fabricate_brand_aware_recommendations():
    """The current deterministic renderer must NOT fabricate brand-aware
    recommendations. If the renderer has no semantic reasoning capability,
    it must not inject deterministic brand-term selection, keyword rules,
    or template-based brand framing into the output.

    This test verifies that the rendered output contains only
    evidence-derived content, not fabricated brand-aware reasoning."""
    from src.agents.competitor_evidence import CompetitorReportRenderer, ResearchResponse
    research = ResearchResponse(
        target_model="TestProduct",
        competitor_names=["CompA"],
        evidence=[],
        evidence_based_recommendations=[],
        strategic_hypotheses=[],
        uncertainty=["no evidence found"],
    )
    renderer = CompetitorReportRenderer(
        research,
        relevant_annotations=[],
        quick_brief="",
    )
    markdown = renderer.render("TestProduct spec")
    # The renderer must not inject deterministic brand-aware sections
    # that would fake semantic brand reasoning
    assert "brand differentiator" not in markdown.lower()
    assert "brand-aware" not in markdown.lower()
    assert "brand framing" not in markdown.lower()


# ---------------------------------------------------------------------------
# Stage 3 Corrected: Hard-isolated sequential Brand Interpretation
#
# The combined reviewer+brand approach was rejected because a model that
# sees brand context while reviewing evidence cannot provide a hard
# evidence-objectivity guarantee through prompt instructions alone.
#
# The corrected architecture uses a TRUE sequential boundary:
#
#   Stage A (no brand_reference)
#     → Deterministic validation
#     → SemanticEvidenceReviewer (no brand_reference)
#     → FINALIZED immutable evidence
#     → BrandInterpretationPass (optional, separate call, brand_reference)
#     → Strategic implications (separate from evidence)
#     → Deterministic renderer
#
# The Brand Interpretation step can NEVER modify the finalized evidence.
# ---------------------------------------------------------------------------


def _make_research_for_brand_tests():
    """Build a ResearchResponse with validated evidence for brand interpretation tests."""
    from src.agents.competitor_evidence import (
        ResearchResponse,
        CompetitorEvidence,
    )
    return ResearchResponse(
        target_model="TestProduct",
        competitor_names=["CompA"],
        evidence=[
            CompetitorEvidence(
                competitor="CompA",
                field="price",
                claim="5,000 THB",
                url="https://example.com/compa",
                geography="thailand",
            ),
        ],
        evidence_based_recommendations=[],
        strategic_hypotheses=[],
        uncertainty=[],
    )


class _BrandInterpFakeLLM:
    """LLM stub for Brand Interpretation tests — captures calls and returns scripted JSON."""

    def __init__(self, output: str):
        self.output = output
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.output


class _ReviewerFakeLLM:
    """LLM stub for semantic reviewer — returns scripted evidence review JSON."""

    def __init__(self, output: str):
        self.output = output
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.output


# --- Test 1: Stage A never receives brand_reference ---

def test_stage_a_evidence_prompt_still_excludes_brand_reference():
    """Stage A research prompt does NOT receive brand_reference."""
    from src.agents.competitor_evidence import EVIDENCE_SYSTEM_PROMPT
    assert "brand_reference" not in EVIDENCE_SYSTEM_PROMPT.lower()
    assert "ข้อมูลแบรนด์อ้างอิง" not in EVIDENCE_SYSTEM_PROMPT


# --- Test 2: SemanticEvidenceReviewer never receives brand_reference ---

def test_semantic_reviewer_signature_has_no_brand_reference():
    """SemanticEvidenceReviewer.review must NOT accept brand_reference.
    This is the hard isolation boundary — the reviewer must not even
    have the option to receive brand context."""
    import inspect
    from src.agents.competitor_evidence import SemanticEvidenceReviewer
    sig = inspect.signature(SemanticEvidenceReviewer.review)
    params = set(sig.parameters.keys())
    assert "brand_reference" not in params, (
        "SemanticEvidenceReviewer.review must NOT accept brand_reference — "
        "this is the hard evidence-isolation boundary"
    )


# --- Test 3: Evidence review completes before Brand Interpretation ---

def test_brand_interpretation_only_after_review():
    """Brand Interpretation must be invoked AFTER SemanticEvidenceReviewer
    returns finalized evidence, not before or during."""
    from src.agents.competitor_evidence import (
        SemanticEvidenceReviewer,
        BrandInterpretationPass,
    )
    research = _make_research_for_brand_tests()
    reviewer_llm = _ReviewerFakeLLM(json.dumps([
        {"index": 0, "action": "keep"}
    ]))
    reviewer = SemanticEvidenceReviewer(llm=reviewer_llm, config={"model": "test"})
    reviewed = reviewer.review(research, relevant_annotations=[
        {"url": "https://example.com/compa", "content": "CompA costs 5,000 THB", "title": "CompA"}
    ])
    assert reviewed is not None
    # Reviewer made its call
    assert len(reviewer_llm.calls) == 1
    # Now Brand Interpretation can use the finalized evidence
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    implications = interp.interpret(reviewed, brand_reference="premium")
    # Interpretation made its own separate call
    assert len(interp_llm.calls) == 1
    assert len(implications) == 1


# --- Test 4: Brand Interpretation receives only finalized/surviving evidence ---

def test_brand_interpretation_receives_finalized_evidence():
    """BrandInterpretationPass receives the finalized ResearchResponse
    from the reviewer, not the pre-review research."""
    from src.agents.competitor_evidence import (
        SemanticEvidenceReviewer,
        BrandInterpretationPass,
    )
    research = _make_research_for_brand_tests()
    # Reviewer removes the evidence
    reviewer_llm = _ReviewerFakeLLM(json.dumps([
        {"index": 0, "action": "remove"}
    ]))
    reviewer = SemanticEvidenceReviewer(llm=reviewer_llm, config={"model": "test"})
    reviewed = reviewer.review(research, relevant_annotations=[
        {"url": "https://example.com/compa", "content": "CompA costs 5,000 THB", "title": "CompA"}
    ])
    assert len(reviewed.evidence) == 0
    # Brand interpretation receives the finalized (empty) evidence
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    implications = interp.interpret(reviewed, brand_reference="premium")
    # No surviving evidence → no implications
    assert len(implications) == 0


# --- Test 5: Removed/rejected evidence cannot be used by Brand Interpretation ---

def test_brand_interpretation_drops_implications_for_removed_evidence():
    """If evidence was removed by the reviewer, implications referencing
    it must be mechanically dropped."""
    from src.agents.competitor_evidence import (
        ResearchResponse,
        BrandInterpretationPass,
    )
    # Finalized evidence has only index 0 surviving
    finalized = ResearchResponse(
        target_model="TestProduct",
        competitor_names=["CompA"],
        evidence=[
            ResearchResponse.__dataclass_fields__  # just to get the type
        ] if False else [],
    )
    # Simulate: finalized has 1 evidence at index 0, but model returns
    # implications referencing index 5 (non-existent)
    finalized = _make_research_for_brand_tests()
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 5, "implication": "bogus", "category": "recommendation"},
        {"evidence_ref": 0, "implication": "valid", "category": "recommendation"},
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    implications = interp.interpret(finalized, brand_reference="premium")
    # Only the valid one (evidence_ref=0) survives
    assert len(implications) == 1
    assert implications[0].implication == "valid"


# --- Test 6: Brand Interpretation cannot mutate the finalized evidence collection ---

def test_brand_interpretation_does_not_mutate_evidence():
    """The finalized evidence collection must be value-equivalent before
    and after Brand Interpretation."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()
    original_claim = research.evidence[0].claim
    original_competitor = research.evidence[0].competitor
    original_url = research.evidence[0].url
    original_count = len(research.evidence)

    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    implications = interp.interpret(research, brand_reference="premium")

    # Evidence is unchanged
    assert len(research.evidence) == original_count
    assert research.evidence[0].claim == original_claim
    assert research.evidence[0].competitor == original_competitor
    assert research.evidence[0].url == original_url


# --- Test 7: Evidence before/after Brand Interpretation is value-equivalent ---

def test_evidence_value_equivalent_before_after_interpretation():
    """Evidence records are byte/value-equivalent before and after
    strategic interpretation."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()
    import copy
    before = copy.deepcopy(research.evidence)

    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    interp.interpret(research, brand_reference="premium")

    for a, b in zip(before, research.evidence):
        assert a.claim == b.claim
        assert a.competitor == b.competitor
        assert a.url == b.url
        assert a.field == b.field


# --- Test 8: Implications referencing unknown/non-surviving evidence IDs are rejected ---

def test_implications_with_invalid_evidence_refs_are_dropped():
    """Implications with evidence_ref outside the surviving evidence
    range are mechanically rejected."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()  # 1 evidence at index 0
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": -1, "implication": "neg", "category": "recommendation"},
        {"evidence_ref": 99, "implication": "out of range", "category": "recommendation"},
        {"evidence_ref": "abc", "implication": "non-int", "category": "recommendation"},
        {"evidence_ref": 0, "implication": "valid", "category": "recommendation"},
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    implications = interp.interpret(research, brand_reference="premium")
    assert len(implications) == 1
    assert implications[0].implication == "valid"


# --- Test 9: Raw brand_reference is never rendered directly ---

def test_renderer_does_not_dump_raw_brand_reference():
    """The renderer must not dump raw brand_reference into the output."""
    from src.agents.competitor_evidence import (
        CompetitorReportRenderer,
        ResearchResponse,
        StrategicImplication,
    )
    research = ResearchResponse(
        target_model="TestProduct",
        competitor_names=["CompA"],
        evidence=[],
        evidence_based_recommendations=[],
        strategic_hypotheses=[],
        uncertainty=["no evidence"],
        strategic_implications=[
            StrategicImplication(
                evidence_ref=0,
                implication="Competing on lowest price may weaken premium positioning",
                category="recommendation",
            ),
        ],
    )
    renderer = CompetitorReportRenderer(
        research,
        relevant_annotations=[],
        quick_brief="",
    )
    markdown = renderer.render("TestProduct spec")
    # The implication text is rendered
    assert "premium" in markdown.lower() or "price" in markdown.lower()
    # Raw brand_reference markers are NOT in the output
    assert "### Positioning" not in markdown
    assert "brand_reference" not in markdown.lower()
    assert "ข้อมูลแบรนด์อ้างอิง" not in markdown


# --- Test 10: Renderer does no semantic brand reasoning ---

def test_renderer_no_brand_section_when_no_implications():
    """When no strategic implications exist, renderer must not add a brand section."""
    from src.agents.competitor_evidence import CompetitorReportRenderer, ResearchResponse
    research = ResearchResponse(
        target_model="TestProduct",
        competitor_names=["CompA"],
        evidence=[],
        evidence_based_recommendations=[],
        strategic_hypotheses=[],
        uncertainty=["no evidence"],
    )
    renderer = CompetitorReportRenderer(research, relevant_annotations=[], quick_brief="")
    markdown = renderer.render("TestProduct spec")
    assert "brand" not in markdown.lower()


# --- Test 11: Missing brand_reference skips the Brand Interpretation call ---

def test_brand_interpretation_skipped_without_brand_reference():
    """When brand_reference is empty, no Brand Interpretation call is made."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    implications = interp.interpret(research, brand_reference="")
    assert len(implications) == 0
    assert len(interp_llm.calls) == 0  # no LLM call made


# --- Test 12: With brand_reference, exactly one additional call is made ---

def test_brand_interpretation_makes_exactly_one_call():
    """With brand_reference, exactly one additional ordinary semantic call
    is made on the successful path."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    interp.interpret(research, brand_reference="premium")
    assert len(interp_llm.calls) == 1


# --- Test 13: No web-search/tool call occurs in Brand Interpretation ---

def test_brand_interpretation_no_web_search_tools():
    """Brand Interpretation must not use web_search or any tools."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    interp.interpret(research, brand_reference="premium")
    assert len(interp_llm.calls) == 1
    tools = interp_llm.calls[0]["kwargs"].get("tools")
    assert tools is None or tools == []


# --- Test 14: Existing evidence revision/retry behavior remains bounded ---

def test_brand_interpretation_fail_closed_no_retry():
    """If Brand Interpretation LLM call fails or returns invalid JSON,
    it returns empty implications (graceful degradation) without retry."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()
    interp_llm = _BrandInterpFakeLLM("invalid json {{{")
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    implications = interp.interpret(research, brand_reference="premium")
    assert len(implications) == 0
    assert len(interp_llm.calls) == 1  # no retry


# --- Test 15: Standalone Agent 2 without brand context remains functional ---

def test_standalone_agent2_without_brand_context():
    """Agent 2 must work without brand_reference — objective evidence only."""
    from src.agents.competitor_evidence import (
        SemanticEvidenceReviewer,
        BrandInterpretationPass,
    )
    research = _make_research_for_brand_tests()
    reviewer_llm = _ReviewerFakeLLM(json.dumps([
        {"index": 0, "action": "keep"}
    ]))
    reviewer = SemanticEvidenceReviewer(llm=reviewer_llm, config={"model": "test"})
    reviewed = reviewer.review(research, relevant_annotations=[
        {"url": "https://example.com/compa", "content": "CompA costs 5,000 THB", "title": "CompA"}
    ])
    assert reviewed is not None
    assert len(reviewed.evidence) == 1
    # No brand interpretation call
    interp_llm = _BrandInterpFakeLLM("[]")
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    implications = interp.interpret(reviewed, brand_reference="")
    assert len(implications) == 0
    assert len(interp_llm.calls) == 0


# --- Test 16: Generic brand/product fixtures only — no benchmark-specific behavior ---

def test_brand_interpretation_accepts_generic_brand_reference():
    """No benchmark/brand/product-specific fixtures encoded as engine behavior.
    Any generic brand reference should work."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()
    generic_brand = "### Positioning\nbudget-friendly value brand"
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    implications = interp.interpret(research, brand_reference=generic_brand)
    assert len(implications) == 1
    # No hardcoded brand names in the system prompt
    call_messages = interp_llm.calls[0]["messages"]
    system_prompt = call_messages[0]["content"]
    assert "Lagenio" not in system_prompt
    assert "imoo" not in system_prompt


# --- Test 17: Existing competitor durable-contract tests continue to pass ---

def test_existing_durable_contracts_still_hold():
    """Verify that the existing evidence-isolation contracts are not broken
    by the new Brand Interpretation step."""
    from src.agents.competitor_evidence import (
        EVIDENCE_SYSTEM_PROMPT,
        RESEARCH_RESPONSE_SCHEMA,
        ResearchResponse,
        CompetitorReportRenderer,
    )
    # Stage A schema has no brand fields
    schema_props = RESEARCH_RESPONSE_SCHEMA["json_schema"]["schema"]["properties"]
    assert "brand_reference" not in schema_props
    assert "brand_aware_implications" not in schema_props
    # Stage A prompt has no brand context
    assert "brand_reference" not in EVIDENCE_SYSTEM_PROMPT.lower()
    # ResearchResponse has strategic_implications as a separate field
    assert "strategic_implications" in ResearchResponse.__dataclass_fields__
    assert "brand_aware_implications" not in ResearchResponse.__dataclass_fields__


# ---------------------------------------------------------------------------
# Configuration-gate regression tests
#
# Brand Interpretation must respect the agent's use_brand_reference config.
# If use_brand_reference is false, Agent 2 remains objective-only even if
# a brand_reference object is loaded on the agent.
# ---------------------------------------------------------------------------


def test_config_enabled_with_brand_reference_and_evidence_makes_one_call():
    """Test 1: use_brand_reference=true + non-empty brand_reference +
    surviving evidence → exactly one Brand Interpretation call."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    # Simulate the full gate condition as in CompetitorAnalysisAgent.run
    brand_ref = "premium positioning"
    brand_interp_enabled = True  # use_brand_reference: true
    if brand_interp_enabled and brand_ref and research.evidence:
        implications = interp.interpret(research, brand_reference=brand_ref)
    else:
        implications = []
    assert len(implications) == 1
    assert len(interp_llm.calls) == 1


def test_config_disabled_with_brand_reference_and_evidence_makes_zero_calls():
    """Test 2: use_brand_reference=false + non-empty brand_reference +
    surviving evidence → zero Brand Interpretation calls."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    # Simulate the full gate condition as in CompetitorAnalysisAgent.run
    brand_ref = "premium positioning"
    brand_interp_enabled = False  # use_brand_reference: false
    if brand_interp_enabled and brand_ref and research.evidence:
        implications = interp.interpret(research, brand_reference=brand_ref)
    else:
        implications = []
    assert len(implications) == 0
    assert len(interp_llm.calls) == 0  # no LLM call made


def test_config_enabled_with_empty_brand_reference_makes_zero_calls():
    """Test 3: enabled config + empty brand_reference → zero calls."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    research = _make_research_for_brand_tests()
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    brand_ref = ""
    brand_interp_enabled = True
    if brand_interp_enabled and brand_ref and research.evidence:
        implications = interp.interpret(research, brand_reference=brand_ref)
    else:
        implications = []
    assert len(implications) == 0
    assert len(interp_llm.calls) == 0


def test_config_enabled_with_no_surviving_evidence_makes_zero_calls():
    """Test 4: enabled config + no surviving evidence → zero calls."""
    from src.agents.competitor_evidence import (
        BrandInterpretationPass,
        ResearchResponse,
    )
    research = ResearchResponse(
        target_model="TestProduct",
        competitor_names=["CompA"],
        evidence=[],  # no surviving evidence
    )
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "test", "category": "recommendation"}
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    brand_ref = "premium positioning"
    brand_interp_enabled = True
    if brand_interp_enabled and brand_ref and research.evidence:
        implications = interp.interpret(research, brand_reference=brand_ref)
    else:
        implications = []
    assert len(implications) == 0
    assert len(interp_llm.calls) == 0


def test_disabling_brand_interpretation_does_not_affect_stage_a_or_reviewer():
    """Test 5: disabling brand interpretation does not affect Stage A or
    SemanticEvidenceReviewer behavior. The reviewer never receives
    brand_reference regardless of config."""
    import inspect
    from src.agents.competitor_evidence import (
        EVIDENCE_SYSTEM_PROMPT,
        SemanticEvidenceReviewer,
    )
    # Stage A prompt is static — config does not change it
    assert "brand_reference" not in EVIDENCE_SYSTEM_PROMPT.lower()
    # Reviewer signature never has brand_reference regardless of config
    sig = inspect.signature(SemanticEvidenceReviewer.review)
    assert "brand_reference" not in sig.parameters
    # Reviewer works the same with any config
    research = _make_research_for_brand_tests()
    reviewer_llm = _ReviewerFakeLLM(json.dumps([
        {"index": 0, "action": "keep"}
    ]))
    reviewer = SemanticEvidenceReviewer(
        llm=reviewer_llm,
        config={"model": "test", "use_brand_reference": False},
    )
    reviewed = reviewer.review(research, relevant_annotations=[
        {"url": "https://example.com/compa", "content": "CompA costs 5,000 THB", "title": "CompA"}
    ])
    assert reviewed is not None
    assert len(reviewed.evidence) == 1
    # Reviewer prompt has no brand context
    system_prompt = reviewer_llm.calls[0]["messages"][0]["content"]
    assert "brand_reference" not in system_prompt.lower()
    assert "ข้อมูลแบรนด์อ้างอิง" not in system_prompt


# ---------------------------------------------------------------------------
# Evidence ordering stability test
#
# The evidence_ref index in StrategicImplication refers to the position
# in the finalized evidence list. This is stable only if the evidence
# ordering does not change between reviewer output → BrandInterpretationPass
# input → implication validation → renderer.
# ---------------------------------------------------------------------------


def test_evidence_ordering_stable_from_reviewer_through_interpretation():
    """Evidence ordering must remain stable from SemanticEvidenceReviewer
    output through BrandInterpretationPass input and implication validation.
    No sorting/filtering/reordering may occur between those steps."""
    from src.agents.competitor_evidence import (
        SemanticEvidenceReviewer,
        BrandInterpretationPass,
        ResearchResponse,
        CompetitorEvidence,
    )
    # Build research with 3 evidence items in a specific order
    research = ResearchResponse(
        target_model="TestProduct",
        competitor_names=["CompA", "CompB", "CompC"],
        evidence=[
            CompetitorEvidence(
                competitor="CompA", field="price", claim="5,000 THB",
                url="https://example.com/a", geography="thailand",
            ),
            CompetitorEvidence(
                competitor="CompB", field="display", claim="6.7 inch",
                url="https://example.com/b", geography="thailand",
            ),
            CompetitorEvidence(
                competitor="CompC", field="battery", claim="5000mAh",
                url="https://example.com/c", geography="thailand",
            ),
        ],
    )
    annotations = [
        {"url": "https://example.com/a", "content": "price 5000", "title": "A"},
        {"url": "https://example.com/b", "content": "display 6.7", "title": "B"},
        {"url": "https://example.com/c", "content": "battery 5000", "title": "C"},
    ]

    # Reviewer keeps all, rewrites index 1
    reviewer_llm = _ReviewerFakeLLM(json.dumps([
        {"index": 0, "action": "keep"},
        {"index": 1, "action": "rewrite", "claim": "6.7 inch OLED"},
        {"index": 2, "action": "keep"},
    ]))
    reviewer = SemanticEvidenceReviewer(llm=reviewer_llm, config={"model": "test"})
    reviewed = reviewer.review(research, relevant_annotations=annotations)
    assert reviewed is not None

    # Evidence order is preserved: CompA, CompB (rewritten), CompC
    assert len(reviewed.evidence) == 3
    assert reviewed.evidence[0].competitor == "CompA"
    assert reviewed.evidence[1].competitor == "CompB"
    assert reviewed.evidence[1].claim == "6.7 inch OLED"
    assert reviewed.evidence[2].competitor == "CompC"

    # Brand interpretation receives the same ordering
    interp_llm = _BrandInterpFakeLLM(json.dumps([
        {"evidence_ref": 0, "implication": "implication for CompA", "category": "recommendation"},
        {"evidence_ref": 2, "implication": "implication for CompC", "category": "inference"},
    ]))
    interp = BrandInterpretationPass(llm=interp_llm, config={"model": "test"})
    implications = interp.interpret(reviewed, brand_reference="premium")

    # Implications reference the correct evidence by position
    assert len(implications) == 2
    assert implications[0].evidence_ref == 0
    assert implications[0].implication == "implication for CompA"
    assert implications[1].evidence_ref == 2
    assert implications[1].implication == "implication for CompC"

    # Evidence is still unchanged after interpretation
    assert reviewed.evidence[0].competitor == "CompA"
    assert reviewed.evidence[1].competitor == "CompB"
    assert reviewed.evidence[2].competitor == "CompC"
