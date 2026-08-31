"""Hardening tests for Agent 3 (CampaignStrategy) structural contracts.

These tests prove the output-quality contract, citation-policy config, and
BaseAgent run-time contract introduced for `campaign_strategy`.  They use a
fake LLM and do not make any real model calls.
"""

from __future__ import annotations

from src.agents.campaign_strategy import CampaignStrategyAgent
from src.config_loader import get_agent_config, load_config
from src.run_context import StepRunContext


class FakeLLM:
    """Deterministic LLM double for BaseAgent.run() contract tests."""

    def __init__(self, generate_output: str = ""):
        self.generate_output = generate_output
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.generate_output

    def close(self):
        pass


def _campaign_config():
    cfg = load_config()
    return dict(get_agent_config(cfg, "campaign_strategy"))


def _build_output() -> str:
    """A default valid campaign output."""
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


def _make_agent():
    cfg = _campaign_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = False
    return CampaignStrategyAgent(cfg, FakeLLM())


# ---------------------------------------------------------------------------
# Output quality contract
# ---------------------------------------------------------------------------


def test_output_quality_rejects_too_short():
    agent = _make_agent()
    # All required headings are present, but the body is essentially empty.
    short_output = (
        "## ราคาแนะนำ\n-\n\n"
        "## แคมเปญหลัก\n-\n\n"
        "## แคมเปญเสริม\n-\n\n"
        "## ช่องทางโปรโมท\n-\n\n"
        "## KPI ที่ควรวัดผล\n-\n\n"
        "## งบประมาณประมาณการ\n-\n\n"
        "## แหล่งอ้างอิง\n-"
    )

    ok, err = agent.validate_output(short_output)
    assert not ok
    assert "สั้น" in err or "short" in err.lower()


def test_output_quality_rejects_citation_only():
    agent = _make_agent()
    citation_only = (
        "## ราคาแนะนำ\n- [a](https://example.com/very/long/citation/url/one/that/pushes/total/past/minimum/threshold)\n\n"
        "## แคมเปญหลัก\n- [b](https://example.com/very/long/citation/url/two)\n\n"
        "## แคมเปญเสริม\n- [c](https://example.com/very/long/citation/url/three)\n\n"
        "## ช่องทางโปรโมท\n- [d](https://example.com/very/long/citation/url/four)\n\n"
        "## KPI ที่ควรวัดผล\n- [e](https://example.com/very/long/citation/url/five)\n\n"
        "## งบประมาณประมาณการ\n- [f](https://example.com/very/long/citation/url/six)\n\n"
        "## แหล่งอ้างอิง\n- [g](https://example.com/very/long/citation/url/seven)"
    )

    ok, err = agent.validate_output(citation_only)
    assert not ok
    assert "citation" in err.lower() or "อ้างอิง" in err


# ---------------------------------------------------------------------------
# Citation policy config
# ---------------------------------------------------------------------------


def test_citation_policy_does_not_append_fallback_dump():
    cfg = _campaign_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = False
    agent = CampaignStrategyAgent(cfg, FakeLLM())

    # Output contains no inline URL, so inline_first + no fallback must NOT
    # append a URL dump from search annotations.
    output = "## ราคาแนะนำ\n- ราคาขายปลีก: ฿2,790\n\n## แหล่งอ้างอิง\n- ไม่มี URL ในตัว output"
    annotations = [
        {"url": "https://example.com/one", "title": "One"},
        {"url": "https://example.com/two", "title": "Two"},
    ]
    result = agent._append_citations_and_verify(output, annotations)

    assert result == output
    assert "example.com/one" not in result
    assert "example.com/two" not in result


# ---------------------------------------------------------------------------
# quick_brief and StepRunContext contract
# ---------------------------------------------------------------------------


def test_quick_brief_reaches_base_agent_run_once():
    cfg = _campaign_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = False
    agent = CampaignStrategyAgent(cfg, FakeLLM(_build_output()))

    prompt = agent.build_prompt({"product": "K2"})
    result = agent.run(prompt, quick_brief="เน้น TikTok")

    assert result == _build_output()
    generate_calls = [c for c in agent.llm.calls if c["kwargs"].get("source", "").endswith(".generate")]
    assert len(generate_calls) == 1
    user_content = generate_calls[0]["messages"][1]["content"]
    assert "เน้น TikTok" in user_content
    assert user_content.count("เน้น TikTok") == 1
    assert "--- คำสั่งเฉพาะรอบนี้" in user_content


def test_step_run_context_overrides_quick_brief_and_resource():
    cfg = _campaign_config()
    cfg["max_review_iterations"] = 0
    cfg["web_search"] = False
    agent = CampaignStrategyAgent(cfg, FakeLLM(_build_output()))

    prompt = agent.build_prompt({"product": "K2"})
    step = StepRunContext(
        workflow_id="w1",
        step_id="s1",
        agent_key="campaign_strategy",
        quick_brief="from step",
        input_refs=(),
        product_refs=(),
        resource_refs=(),
        resource_text="resource from step",
        resource_image_paths=(),
        resource_trace=(),
        warnings=(),
    )
    result = agent.run(
        prompt,
        quick_brief="from param",
        resource_context="param resource",
        step_context=step,
    )

    assert result == _build_output()
    generate_calls = [c for c in agent.llm.calls if c["kwargs"].get("source", "").endswith(".generate")]
    user_content = generate_calls[0]["messages"][1]["content"]
    assert "from step" in user_content
    assert "resource from step" in user_content
    assert "from param" not in user_content
    assert "param resource" not in user_content
