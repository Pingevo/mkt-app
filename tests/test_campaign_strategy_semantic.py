"""Deterministic semantic tests for Agent 3 (campaign_strategy) using FakeLLM.

These tests exercise the guardrails added for Beta readiness without any
real model calls.  They cover the required cases from AGENT_PRODUCTION_READINESS_SPEC.
"""

from __future__ import annotations

import pytest

from src.agents.campaign_strategy import CampaignStrategyAgent
from src.config_loader import get_agent_config, load_config
from src.run_context import StepRunContext
from tests.fixtures.campaign_fixtures import CASES


class FakeLLM:
    """Deterministic LLM that returns a scripted output for every call."""

    def __init__(self, generate_output: str = ""):
        self.generate_output = generate_output
        self.calls: list[dict] = []
        self.last_truncated = False

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            return '{"grounded": true, "unsupported_claims": []}'
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.generate_output

    def close(self):
        pass


def _campaign_config():
    cfg = load_config()
    return get_agent_config(cfg, "campaign_strategy")


def _make_agent(output: str, instructions: dict | None = None):
    cfg = dict(_campaign_config())
    cfg["web_search"] = False
    cfg["max_review_iterations"] = 0
    return CampaignStrategyAgent(cfg, FakeLLM(output), instructions=instructions or {})


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["expected_ok"]], ids=lambda c: c["case_id"]
)
def test_passing_cases(case):
    """Cases that should flow through generate + validation."""
    agent = _make_agent(case["output"], instructions=case.get("instructions", {}))
    prompt = agent.build_prompt(case["context"])
    result = agent.run(prompt, quick_brief=case.get("quick_brief", ""))
    assert result == case["output"]


@pytest.mark.parametrize(
    "case", [c for c in CASES if not c["expected_ok"]], ids=lambda c: c["case_id"]
)
def test_failing_cases(case):
    """Cases that must be rejected by the deterministic validator."""
    agent = _make_agent(case["output"], instructions=case.get("instructions", {}))
    prompt = agent.build_prompt(case["context"])
    with pytest.raises(ValueError) as exc:
        agent.run(prompt, quick_brief=case.get("quick_brief", ""))
    assert case["expected_rule"] in str(exc.value)


def _step(quick_brief: str = "") -> StepRunContext:
    return StepRunContext(
        workflow_id="w1",
        step_id="s1",
        agent_key="campaign_strategy",
        quick_brief=quick_brief,
        input_refs=(),
        product_refs=(),
        resource_refs=(),
        resource_text="",
        resource_image_paths=(),
        resource_trace=(),
        warnings=(),
    )


def _output_with_guaranteed_sales() -> str:
    return (
        "## ราคาแนะนำ\n"
        "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
        "- ราคาโปรโมชัน: ฿2,240 (indicative)\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n\n"
        "## KPI ที่ควรวัดผล\n"
        "- เป้าหมายยอดขาย 1,000 ชิ้น (target)\n\n"
        "## งบประมาณประมาณการ\n"
        "- ประมาณ 50,000 บาท (estimate)\n\n"
        "## แหล่งอ้างอิง\n"
        "- ไม่มี external factual claim\n"
    )


def _output_over_budget() -> str:
    return (
        "## ราคาแนะนำ\n"
        "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
        "- ราคาโปรโมชัน: ฿2,240 (indicative)\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n\n"
        "## KPI ที่ควรวัดผล\n"
        "- ยอดขาย (target ต้องกำหนดหลังมี baseline)\n\n"
        "## งบประมาณประมาณการ\n"
        "- งบประมาณ 150,000 บาท\n\n"
        "## แหล่งอ้างอิง\n"
        "- ไม่มี external factual claim\n"
    )


def test_step_context_quick_brief_overrides_run_param():
    """validator ต้องใช้ quick_brief จาก StepRunContext ไม่ใช่ quick_brief ที่ส่งผ่าน run()."""
    agent = _make_agent(_output_with_guaranteed_sales(), instructions={})
    prompt = agent.build_prompt({"product": "LAGENIO K2"})
    with pytest.raises(ValueError) as exc:
        agent.run(prompt, quick_brief="safe", step_context=_step("ตั้งเป้ายอดขาย 1,000 ชิ้น"))
    assert "no_guaranteed_numeric_targets_without_baseline" in str(exc.value)


def test_step_context_constraints_preserved():
    """StepRunContext ไม่ override budget_max / discount_max จาก instructions."""
    agent = _make_agent(_output_over_budget(), instructions={"budget_max": "100000"})
    prompt = agent.build_prompt({"product": "LAGENIO K2"})
    with pytest.raises(ValueError) as exc:
        agent.run(prompt, step_context=_step())
    assert "budget_max_enforced" in str(exc.value)


def test_step_context_safe_quick_brief_passes():
    """เมื่อ StepRunContext ไม่ demanding guarantee และ output ถูกต้อง ต้องผ่าน."""
    from tests.test_campaign_strategy import _valid_output
    agent = _make_agent(_valid_output(), instructions={})
    prompt = agent.build_prompt({"product": "LAGENIO K2"})
    result = agent.run(prompt, step_context=_step("เน้น TikTok"))
    assert result == _valid_output()
