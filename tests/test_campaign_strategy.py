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
        self.last_truncated = False

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            return '{"grounded": true, "unsupported_claims": []}'
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
        "- กลุ่มราคาเป้าหมาย: ระดับ mid-range (ต้องกำหนดหลังมีต้นทุน)\n"
        "- ราคาโปรโมชัน: ไม่สามารถเสนอตัวเลขได้เนื่องจากไม่มีข้อมูลต้นทุน (pending validation)\n"
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
        "- ไม่มี external factual claim ที่ต้องอ้างอิง"
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


def test_extracts_requested_competitor_from_quick_brief():
    quick = "หาข้อมูลคู่แข่งราคาปัจจุบัน imoo Z1 แล้วเสนอราคาแนะนำ"
    assert CampaignStrategyAgent._extract_competitor_tokens(quick) == {"z1"}


def test_ignores_model_like_token_not_requested_as_competitor():
    quick = "LAGENIO K2 เปิดตัว Q1"
    assert CampaignStrategyAgent._extract_competitor_tokens(quick) == set()


def test_annotation_confirms_competitor_only_with_corroborating_evidence():
    annotation = {"title": "imoo Z1 price", "content": "imoo Z1 is ฿3,999", "url": "https://example.com/z1"}
    assert CampaignStrategyAgent._annotation_confirms_competitor(annotation, {"z1"})
    assert not CampaignStrategyAgent._annotation_confirms_competitor(annotation, {"k3"})


def test_on_annotations_ready_creates_selected_evidence_and_confirmed_models():
    cfg = _campaign_config()
    cfg["web_search"] = False
    llm = FakeLLM(generate_output=_valid_output())
    agent = CampaignStrategyAgent(cfg, llm)
    agent._last_quick_brief = "หาข้อมูลคู่แข่ง imoo Z1"
    agent._requested_competitor_models = {"z1"}
    agent._last_annotations = [
        {"title": "imoo Z1 official", "content": "imoo Z1 ฿3,999", "url": "https://example.com/z1"},
        {"title": "random tablet", "content": "some tablet", "url": "https://example.com/tablet"},
    ]
    # all raw are relevant because CampaignStrategyAgent has no _assess_source_relevance
    agent._last_relevant_annotations = agent._last_annotations
    agent._on_annotations_ready()

    assert agent._selected_evidence == agent._last_annotations
    assert set(agent._selected_evidence_urls) == {"https://example.com/z1", "https://example.com/tablet"}
    assert agent._evidence_confirmed_competitor_models == {"z1"}


def test_repair_receives_compact_evidence_bundle():
    cfg = _campaign_config()
    cfg["web_search"] = False
    llm = FakeLLM(generate_output="repaired output")
    agent = CampaignStrategyAgent(cfg, llm)
    agent._selected_evidence = [
        {
            "title": "imoo Z1 Central",
            "url": "https://www.central.co.th/th/imoo-z1",
            "content": "IMOO Z1 สีเขียว ฿3,999",
        }
    ]
    agent._repair_output("bad output", "test error", [{"role": "user", "content": "prompt"}])
    repair_calls = [c for c in llm.calls if "repair" in c["kwargs"].get("source", "")]
    assert len(repair_calls) == 1
    user_msg = repair_calls[0]["messages"][-1]["content"]
    assert "Selected evidence" in user_msg or "ข้อมูลหลักฐาน" in user_msg
    assert "https://www.central.co.th/th/imoo-z1" in user_msg
    assert "฿3,999" in user_msg


def test_repair_does_not_dump_full_page():
    cfg = _campaign_config()
    cfg["web_search"] = False
    llm = FakeLLM(generate_output="repaired output")
    agent = CampaignStrategyAgent(cfg, llm)
    long_content = "x " * 5000
    agent._selected_evidence = [
        {"title": "long page", "url": "https://example.com", "content": long_content}
    ]
    agent._repair_output("bad output", "test error", [{"role": "user", "content": "prompt"}])
    repair_calls = [c for c in llm.calls if "repair" in c["kwargs"].get("source", "")]
    user_msg = repair_calls[0]["messages"][-1]["content"]
    assert user_msg.count("x ") < 2000


# ---------------------------------------------------------------------------
# Content Pillars — optional content-strategy guidance
# ---------------------------------------------------------------------------

def test_campaign_strategy_receives_configured_pillars():
    """campaign_strategy can receive optional configured Content Pillars
    via the context dict."""
    agent = CampaignStrategyAgent(_campaign_config(), FakeLLM())
    prompt = agent.build_prompt({
        "product": "TestProduct",
        "content_pillars": "- Safety\n- Fun\n- Value",
    })
    assert "Safety" in prompt
    assert "Fun" in prompt
    assert "Value" in prompt
    assert "Content Pillars" in prompt


def test_campaign_strategy_receives_selected_pillar():
    """campaign_strategy can receive a selected Content Pillar when one
    legitimately exists in the flow."""
    agent = CampaignStrategyAgent(_campaign_config(), FakeLLM())
    prompt = agent.build_prompt({
        "product": "TestProduct",
        "selected_pillar": "Safety",
    })
    assert "Safety" in prompt
    assert "Content Pillar ที่เลือก" in prompt


def test_campaign_strategy_works_without_pillars():
    """campaign_strategy must work normally when no Pillars are provided."""
    agent = CampaignStrategyAgent(_campaign_config(), FakeLLM())
    prompt = agent.build_prompt({"product": "TestProduct"})
    assert "Content Pillar" not in prompt
    assert "TestProduct" in prompt


def test_campaign_strategy_pillars_are_guidance_not_mandatory():
    """Pillar context must include guidance that usage is optional,
    not mandatory keyword inclusion."""
    agent = CampaignStrategyAgent(_campaign_config(), FakeLLM())
    prompt = agent.build_prompt({
        "product": "TestProduct",
        "content_pillars": "- Safety",
        "selected_pillar": "Safety",
    })
    assert "ไม่บังคับ" in prompt


def test_campaign_strategy_no_pillar_names_hardcoded():
    """No specific Pillar names are hardcoded into the agent logic.
    The agent renders whatever pillars are passed via context."""
    agent = CampaignStrategyAgent(_campaign_config(), FakeLLM())
    prompt_a = agent.build_prompt({"product": "P", "content_pillars": "- Alpha"})
    prompt_b = agent.build_prompt({"product": "P", "content_pillars": "- Beta"})
    assert "Alpha" in prompt_a and "Beta" not in prompt_a
    assert "Beta" in prompt_b and "Alpha" not in prompt_b
