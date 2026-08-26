"""Campaign strategy tests using a fake LLM.

These tests verify the refactored CampaignStrategyAgent:

1. Accepts a generic `context` dict and renders only provided context blocks.
2. Requires `product` context; other context keys are optional.
3. Does not perform pricing/campaign reasoning inside `build_prompt`.
4. Receives a system prompt that supports standalone operation, indicative
   pricing, and evidence-driven web search.
5. Produces output that passes the existing Markdown section validation.
"""

from __future__ import annotations

import pytest

from src.agents.campaign_strategy import CampaignStrategyAgent
from src.config_loader import get_agent_config, load_config


class FakeLLM:
    """Deterministic LLM double that captures the request and returns scripted output."""

    def __init__(self, generate_output: str = "", annotations: list | None = None):
        self.generate_output = generate_output
        self.annotations = annotations or []
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("return_annotations"):
            return self.generate_output, self.annotations
        return self.generate_output

    def close(self):
        pass


def _campaign_config():
    cfg = load_config()
    return get_agent_config(cfg, "campaign_strategy")


def _valid_output():
    """A minimal valid output containing all required Markdown section headings."""
    return (
        "## ราคาแนะนำ\n"
        "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
        "- ราคาโปรโมชัน: ฿2,240 (indicative)\n"
        "- ราคาส่ง/ตัวแทน: ไม่สามารถระบุได้เนื่องจากขาดข้อมูลต้นทุน\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n\n"
        "## KPI ที่ควรวัดผล\n"
        "- ยอดขาย (target ต้องกำหนดหลังมี baseline)\n\n"
        "## งบประมาณประมาณการ\n"
        "- ประมาณ 50,000 บาท (estimate)\n\n"
        "## แหล่งอ้างอิง\n"
        "- [ตัวอย่าง](https://shopee.co.th/product/123456)"
    )


def test_build_prompt_requires_product():
    agent = CampaignStrategyAgent(_campaign_config(), FakeLLM())

    with pytest.raises(ValueError, match="product context"):
        agent.build_prompt({})

    with pytest.raises(ValueError, match="product context"):
        agent.build_prompt({"product": "   ", "competitors": "some"})


def test_build_prompt_product_only():
    agent = CampaignStrategyAgent(_campaign_config(), FakeLLM())
    prompt = agent.build_prompt({"product": "LAGENIO K2"})

    assert "--- สินค้า (product) ---" in prompt
    assert "LAGENIO K2" in prompt
    assert "--- ผลวิเคราะห์คู่แข่ง" not in prompt
    assert "--- ข้อมูลตลาด" not in prompt
    assert "--- กลุ่มเป้าหมาย" not in prompt
    assert "--- ข้อมูลทางธุรกิจ" not in prompt


def test_build_prompt_renders_optional_contexts():
    agent = CampaignStrategyAgent(_campaign_config(), FakeLLM())
    prompt = agent.build_prompt(
        {
            "product": "K2",
            "competitors": "imoo Z1",
            "market": "ตลาดเติบโต",
            "customers": "เด็ก 6-12 ปี",
            "business": "งบ 100,000",
        }
    )

    assert "--- สินค้า (product) ---" in prompt
    assert "--- ผลวิเคราะห์คู่แข่ง (competitors) ---" in prompt
    assert "--- ข้อมูลตลาดและเทรนด์ (market) ---" in prompt
    assert "--- กลุ่มเป้าหมาย (customers) ---" in prompt
    assert "--- ข้อมูลทางธุรกิจและงบประมาณ (business) ---" in prompt
    assert "imoo Z1" in prompt


def test_build_prompt_ignores_empty_and_quick_brief():
    agent = CampaignStrategyAgent(_campaign_config(), FakeLLM())
    prompt = agent.build_prompt(
        {
            "product": "K2",
            "competitors": "",
            "quick_brief": "เน้น TikTok",
        }
    )

    assert "เน้น TikTok" not in prompt
    assert "quick_brief" not in prompt
    assert "--- ผลวิเคราะห์คู่แข่ง" not in prompt


def test_system_prompt_allows_indicative_pricing():
    cfg = _campaign_config()
    agent = CampaignStrategyAgent(cfg, FakeLLM())
    system = agent._build_system_prompt()

    assert "estimate" in system or "สมมติฐาน" in system


def test_system_prompt_prohibits_financial_certainty():
    cfg = _campaign_config()
    agent = CampaignStrategyAgent(cfg, FakeLLM())
    system = agent._build_system_prompt()

    assert "wholesale" in system or "ราคาส่ง" in system
    assert "margin" in system or "profitability" in system


def test_system_prompt_requires_source_urls():
    cfg = _campaign_config()
    agent = CampaignStrategyAgent(cfg, FakeLLM())
    system = agent._build_system_prompt()

    assert "direct source URL" in system or "URL หน้าสินค้า" in system


def test_system_prompt_prohibits_spec_to_benefit():
    cfg = _campaign_config()
    agent = CampaignStrategyAgent(cfg, FakeLLM())
    system = agent._build_system_prompt()

    assert "AMOLED" in system
    assert "ถนอมสายตา" in system


def test_system_prompt_addresses_conflicting_context():
    cfg = _campaign_config()
    agent = CampaignStrategyAgent(cfg, FakeLLM())
    system = agent._build_system_prompt()

    assert "ขัดแย้ง" in system


def test_system_prompt_uses_thai_and_practical_language():
    """Rule 1: ใช้ภาษาไทย และให้คำแนะนำที่นำไปปฏิบัติได้จริง ไม่ใช้คำโฆษณาเกินจริง."""
    cfg = _campaign_config()
    agent = CampaignStrategyAgent(cfg, FakeLLM())
    system = agent._build_system_prompt()

    assert "ภาษาไทย" in system
    assert "ปฏิบัติได้จริง" in system
    assert "โฆษณาเกินจริง" in system


def test_system_prompt_web_search_only_when_context_insufficient():
    """Rule 2: ใช้ web search เฉพาะเมื่อข้อมูลปัจจุบันที่จำเป็นไม่มีใน context."""
    cfg = _campaign_config()
    agent = CampaignStrategyAgent(cfg, FakeLLM())
    system = agent._build_system_prompt()

    assert "web search" in system.lower() or "ค้นเพิ่ม" in system
    assert "ไม่มีใน context" in system or "ไม่ต้องค้นเพิ่ม" in system


def test_system_prompt_indicative_range_with_estimate_label():
    """Rule 3: เมื่อไม่มีต้นทุนครบ สามารถเสนอ retail/promo price เป็น indicative range
    หากมี positioning/market evidence รองรับ และต้องระบุว่าเป็น estimate
    ส่วน wholesale/margin ยังห้ามระบุตัวเลข."""
    cfg = _campaign_config()
    agent = CampaignStrategyAgent(cfg, FakeLLM())
    system = agent._build_system_prompt()

    assert "indicative range" in system
    assert "positioning" in system or "market evidence" in system
    assert "estimate" in system
    assert "wholesale" in system
    assert "margin" in system


def test_run_standalone_product_only():
    cfg = _campaign_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = False
    llm = FakeLLM(generate_output=_valid_output())
    agent = CampaignStrategyAgent(cfg, llm)

    prompt = agent.build_prompt({"product": "K2"})
    result = agent.run(prompt)

    assert result == _valid_output()
    generate_calls = [c for c in llm.calls if "generate" in c["kwargs"].get("source", "")]
    assert len(generate_calls) == 1
    assert "K2" in generate_calls[0]["messages"][1]["content"]


def test_run_with_competitor_analysis():
    cfg = _campaign_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = False
    llm = FakeLLM(generate_output=_valid_output())
    agent = CampaignStrategyAgent(cfg, llm)

    prompt = agent.build_prompt({"product": "K2", "competitors": "imoo Z1"})
    result = agent.run(prompt)

    assert result == _valid_output()
    generate_calls = [c for c in llm.calls if "generate" in c["kwargs"].get("source", "")]
    assert "imoo Z1" in generate_calls[0]["messages"][1]["content"]


def test_run_does_not_force_web_search_when_disabled():
    cfg = _campaign_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = False
    llm = FakeLLM(generate_output=_valid_output())
    agent = CampaignStrategyAgent(cfg, llm)

    agent.run(agent.build_prompt({"product": "K2"}))

    generate_calls = [c for c in llm.calls if "generate" in c["kwargs"].get("source", "")]
    tools = generate_calls[0]["kwargs"].get("tools", [])
    assert not any(t.get("type") == "openrouter:web_search" for t in tools)
