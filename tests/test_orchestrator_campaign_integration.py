"""Integration test: Orchestrator.run_campaign_strategy with FakeLLM.

This test exercises the real Orchestrator.run_campaign_strategy implementation
(not mocked) to verify:
  1. The orchestrator passes product + competitor data as a context dict
     to CampaignStrategyAgent.build_prompt (not as two positional args).
  2. No TypeError is raised.
  3. Product and competitor context appear in the prompt sent to the LLM.
  4. With max_review_iterations=0 and verify_urls=false, only 1 generate
     call is made (no review, no URL-verification fetch).
"""

from __future__ import annotations

from src.orchestrator import Orchestrator
from src.agents.campaign_strategy import CampaignStrategyAgent


class FakeLLM:
    """Deterministic LLM double that captures all chat calls.

    Returns (text, annotations) tuple when return_annotations=True,
    matching the real LLMClient.chat contract.
    """

    def __init__(self, output: str = "", annotations: list | None = None):
        self.output = output
        self.annotations = annotations or []
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("return_annotations"):
            return self.output, self.annotations
        return self.output

    def close(self):
        pass


def _valid_campaign_output() -> str:
    """A valid campaign output that passes all semantic guardrails."""
    return (
        "## ราคาแนะนำ\n"
        "- กลุ่มราคาเป้าหมาย: ระดับ mid-range (ต้องกำหนดหลังมีต้นทุน)\n"
        "- ราคาโปรโมชัน: ไม่สามารถเสนอตัวเลขได้เนื่องจากไม่มีข้อมูลต้นทุน (pending validation)\n"
        "- ราคาส่ง/ตัวแทน: ไม่สามารถระบุได้เนื่องจากขาดข้อมูลต้นทุน\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch Campaign\n\n"
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


def _make_orchestrator() -> Orchestrator:
    """Create an Orchestrator without requiring brand files or API key."""
    orch = Orchestrator.__new__(Orchestrator)
    from src.config_loader import load_config
    orch.config = load_config()
    orch.brand_context = ""
    orch.brand_reference = ""
    orch.brand_visual = ""
    orch.brand_rules = {}
    orch.product_images = []
    orch.product_id = None
    orch.results = {}
    return orch


def test_orchestrator_run_campaign_strategy_passes_context_dict():
    """Orchestrator must pass product+competitor as a context dict, not two args."""
    orch = _make_orchestrator()
    fake_llm = FakeLLM(output=_valid_campaign_output())

    result = orch.run_campaign_strategy(
        product_spec="Lagenio K2 smartwatch for kids",
        competitor_analysis="imoo Z1 is a direct competitor at ฿2,500",
        llm=fake_llm,
    )

    # Result should be the valid output
    assert result == _valid_campaign_output()

    # At least one generate call must have happened
    generate_calls = [
        c for c in fake_llm.calls
        if "generate" in c["kwargs"].get("source", "")
    ]
    assert len(generate_calls) >= 1

    # The user prompt (messages[1] content) must contain both product and competitor
    user_content = generate_calls[0]["messages"][1]["content"]
    assert "Lagenio K2" in user_content, "Product data must appear in prompt"
    assert "imoo Z1" in user_content, "Competitor data must appear in prompt"
    assert "--- สินค้า (product) ---" in user_content
    assert "--- ผลวิเคราะห์คู่แข่ง (competitors) ---" in user_content


def test_orchestrator_run_campaign_strategy_no_type_error():
    """Calling run_campaign_strategy must not raise TypeError."""
    orch = _make_orchestrator()
    fake_llm = FakeLLM(output=_valid_campaign_output())

    # This should not raise TypeError (the old code passed two positional args)
    result = orch.run_campaign_strategy(
        product_spec="Product X",
        competitor_analysis="Competitor Y",
        llm=fake_llm,
    )
    assert result is not None


def test_orchestrator_run_campaign_strategy_single_generate_call():
    """Orchestrator must produce exactly 1 generate call when no repair is needed.

    With max_review_iterations=0 and verify_urls=false (campaign_strategy
    config), the only LLM call should be the generate call.  No review,
    no URL-verification fetch calls.
    """
    orch = _make_orchestrator()
    fake_llm = FakeLLM(output=_valid_campaign_output())

    orch.run_campaign_strategy(
        product_spec="Product with COGS 500 baht, margin 30%, budget 100k",
        competitor_analysis="Competitor at ฿2,500",
        llm=fake_llm,
    )

    generate_calls = [
        c for c in fake_llm.calls
        if "generate" in c["kwargs"].get("source", "")
    ]
    review_calls = [
        c for c in fake_llm.calls
        if "review" in c["kwargs"].get("source", "")
    ]
    fetch_calls = [
        c for c in fake_llm.calls
        if "fetch" in c["kwargs"].get("source", "")
    ]
    assert len(generate_calls) == 1
    assert len(review_calls) == 0
    assert len(fetch_calls) == 0


def test_orchestrator_run_campaign_strategy_empty_competitor():
    """Orchestrator must handle empty competitor analysis gracefully."""
    orch = _make_orchestrator()
    fake_llm = FakeLLM(output=_valid_campaign_output())

    result = orch.run_campaign_strategy(
        product_spec="Product X",
        competitor_analysis="",
        llm=fake_llm,
    )
    assert result is not None

    generate_calls = [
        c for c in fake_llm.calls
        if "generate" in c["kwargs"].get("source", "")
    ]
    assert len(generate_calls) >= 1
    user_content = generate_calls[0]["messages"][1]["content"]
    assert "Product X" in user_content


# ---------------------------------------------------------------------------
# New verification tests — Agent 3 minimal-simplification patch
# ---------------------------------------------------------------------------


def test_campaign_strategy_web_search_tools_still_sent():
    """web_search=true must still send openrouter:web_search tools to the LLM."""
    orch = _make_orchestrator()
    fake_llm = FakeLLM(output=_valid_campaign_output(), annotations=[])

    orch.run_campaign_strategy(
        product_spec="Product X",
        competitor_analysis="",
        llm=fake_llm,
    )

    generate_calls = [
        c for c in fake_llm.calls
        if "generate" in c["kwargs"].get("source", "")
    ]
    assert len(generate_calls) == 1
    tools = generate_calls[0]["kwargs"].get("tools", [])
    assert any(t.get("type") == "openrouter:web_search" for t in tools), (
        "web_search tools must still be sent when web_search=true"
    )


def test_campaign_strategy_url_verification_not_called():
    """verify_urls=false for campaign_strategy must NOT trigger _verify_urls_with_fetch.

    Even when the generate call returns annotations (URLs), the BaseAgent must
    not make any `.fetch` calls because campaign_strategy.verify_urls=false.
    """
    orch = _make_orchestrator()
    fake_llm = FakeLLM(
        output=_valid_campaign_output(),
        annotations=[
            {"url": "https://shopee.co.th/product/123", "title": "Shopee product"},
        ],
    )

    orch.run_campaign_strategy(
        product_spec="Product X",
        competitor_analysis="",
        llm=fake_llm,
    )

    fetch_calls = [
        c for c in fake_llm.calls
        if "fetch" in c["kwargs"].get("source", "")
    ]
    assert len(fetch_calls) == 0, (
        "URL verification fetch calls must NOT happen when verify_urls=false"
    )


def test_campaign_strategy_review_not_called():
    """max_review_iterations=0 must NOT trigger _review_and_refine."""
    orch = _make_orchestrator()
    fake_llm = FakeLLM(output=_valid_campaign_output())

    orch.run_campaign_strategy(
        product_spec="Product X",
        competitor_analysis="",
        llm=fake_llm,
    )

    review_calls = [
        c for c in fake_llm.calls
        if "review" in c["kwargs"].get("source", "")
    ]
    assert len(review_calls) == 0, (
        "Review calls must NOT happen when max_review_iterations=0"
    )


def test_campaign_strategy_single_generate_call_no_repair():
    """When output passes validation, exactly 1 generate call and 0 repair calls."""
    orch = _make_orchestrator()
    fake_llm = FakeLLM(output=_valid_campaign_output())

    orch.run_campaign_strategy(
        product_spec="Product X",
        competitor_analysis="",
        llm=fake_llm,
    )

    generate_calls = [
        c for c in fake_llm.calls
        if "generate" in c["kwargs"].get("source", "")
    ]
    repair_calls = [
        c for c in fake_llm.calls
        if "repair" in c["kwargs"].get("source", "")
    ]
    assert len(generate_calls) == 1
    assert len(repair_calls) == 0


def _with_web_citation_output() -> str:
    """A compliant output that already contains an inline citation for the URL
    that the web search annotation would provide."""
    return (
        "## ราคาแนะนำ\n"
        "- กลุ่มราคาเป้าหมาย: ระดับ mid-range (ต้องกำหนดหลังมีต้นทุน)\n"
        "- ราคาโปรโมชัน: ไม่สามารถเสนอตัวเลขได้เนื่องจากไม่มีข้อมูลต้นทุน (pending validation)\n"
        "- ราคาคู่แข่ง: ฿2,500 [Shopee](https://shopee.co.th/product/123)\n"
        "- ราคาส่ง/ตัวแทน: ไม่สามารถระบุได้เนื่องจากขาดข้อมูลต้นทุน\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch Campaign\n\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n\n"
        "## KPI ที่ควรวัดผล\n"
        "- ยอดขาย (target ต้องกำหนดหลังมี baseline)\n\n"
        "## งบประมาณประมาณการ\n"
        "- ประมาณ 50,000 บาท (estimate)\n\n"
        "## แหล่งอ้างอิง\n"
        "- [Shopee product](https://shopee.co.th/product/123)\n"
    )


def test_campaign_strategy_citations_appended_without_verification():
    """With inline_first citation policy, the model's own inline citation is kept
    and no fallback URL dump/verification section is appended."""
    orch = _make_orchestrator()
    fake_llm = FakeLLM(
        output=_with_web_citation_output(),
        annotations=[
            {"url": "https://shopee.co.th/product/123", "title": "Product X smartwatch"},
        ],
    )

    result = orch.run_campaign_strategy(
        product_spec="Product X",
        competitor_analysis="",
        llm=fake_llm,
    )

    # The inline citation is preserved in the result.
    assert "https://shopee.co.th/product/123" in result
    # No fallback dump section and no verification section are appended.
    assert "แหล่งอ้างอิงจริงจากการค้นหา" not in result
    assert "URL ที่ verify ผ่าน" not in result


def test_campaign_strategy_output_section_validation_still_works():
    """required_output_sections validation must still reject missing sections."""
    from src.output_validators import validate_output
    from src.config_loader import load_config, get_agent_config

    cfg = load_config()
    agent_cfg = get_agent_config(cfg, "campaign_strategy")
    required = agent_cfg.get("required_output_sections")

    # Output missing "แหล่งอ้างอิง" section
    incomplete_output = (
        "## ราคาแนะนำ\n- ราคา: ฿1,000\n\n"
        "## แคมเปญหลัก\n- ชื่อ: A\n\n"
        "## แคมเปญเสริม\n- B\n\n"
        "## ช่องทางโปรโมท\n- TikTok\n\n"
        "## KPI ที่ควรวัดผล\n- sales\n\n"
        "## งบประมาณประมาณการ\n- 50k\n"
    )
    ok, err = validate_output("campaign_strategy", incomplete_output, required_sections=required)
    assert not ok
    assert "แหล่งอ้างอิง" in err

    # Complete output passes
    ok, err = validate_output("campaign_strategy", _valid_campaign_output(), required_sections=required)
    assert ok, f"Valid output should pass: {err}"


def test_campaign_strategy_orchestrator_button_path_works():
    """The orchestrator button path (run_campaign_strategy) must still work
    end-to-end with the simplified config — no exceptions, valid output returned."""
    orch = _make_orchestrator()
    fake_llm = FakeLLM(output=_valid_campaign_output())

    result = orch.run_campaign_strategy(
        product_spec="Lagenio K2 smartwatch",
        competitor_analysis="imoo Z1 at ฿2,500",
        llm=fake_llm,
    )

    assert result is not None
    assert "ราคาแนะนำ" in result
    assert "แคมเปญหลัก" in result
