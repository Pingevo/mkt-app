"""Tests for flow runner — sequential agent execution with context passing.

TDD: test context building first, then flow execution.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


DEPS = {
    "product_spec": [],
    "competitor_analysis": ["product_spec"],
    "campaign_strategy": ["product_spec", "competitor_analysis"],
    "content_creator": ["product_spec", "competitor_analysis", "campaign_strategy"],
}


def test_build_context_for_agent_no_results():
    """ยังไม่มีผลลัพธ์ใดๆ → context ว่าง (agent ทำงาน standalone)."""
    from src.flow_runner import build_context_for_agent

    context = build_context_for_agent("campaign_strategy", {}, DEPS)
    assert context == {}

    context = build_context_for_agent("content_creator", {}, DEPS)
    assert context == {}


def test_build_context_for_agent_with_competitor():
    """มี competitor_analysis แล้ว → campaign_strategy และ content_creator ใช้ได้."""
    from src.flow_runner import build_context_for_agent

    results = {"competitor_analysis": "Competitor ABC ราคา 999"}
    ctx = build_context_for_agent("campaign_strategy", results, DEPS)
    assert ctx.get("competitor_analysis") == "Competitor ABC ราคา 999"

    ctx = build_context_for_agent("content_creator", results, DEPS)
    assert ctx.get("competitor_analysis") == "Competitor ABC ราคา 999"
    assert "campaign_strategy" not in ctx


def test_build_context_for_agent_with_all():
    """มีทั้ง competitor + campaign → content_creator ใช้ทั้งสอง."""
    from src.flow_runner import build_context_for_agent

    results = {
        "competitor_analysis": "Competitor data",
        "campaign_strategy": "Campaign plan",
        "product_spec": "Product spec",
    }
    ctx = build_context_for_agent("content_creator", results, DEPS)
    assert ctx.get("competitor_analysis") == "Competitor data"
    assert ctx.get("campaign_strategy") == "Campaign plan"
    assert ctx.get("product_spec") == "Product spec"


def test_build_context_for_product_spec():
    """product_spec ไม่ต้องการ context จาก agent ก่อนหน้า."""
    from src.flow_runner import build_context_for_agent

    results = {"competitor_analysis": "Competitor data"}
    ctx = build_context_for_agent("product_spec", results, DEPS)
    assert "competitor_analysis" not in ctx
    assert "campaign_strategy" not in ctx


def test_run_flow_steps_passes_context():
    """รัน flow ตามลำดับและส่ง context ต่อกัน."""
    from src.flow_runner import run_flow_steps

    calls = []

    def fake_run(agent, product, context):
        calls.append((agent, product, context))
        if agent == "product_spec":
            return "spec result"
        if agent == "competitor_analysis":
            return "competitor result"
        if agent == "campaign_strategy":
            assert context.get("competitor_analysis") == "competitor result"
            return "campaign result"
        if agent == "content_creator":
            assert context.get("competitor_analysis") == "competitor result"
            assert context.get("campaign_strategy") == "campaign result"
            return "content result"
        return ""

    results = run_flow_steps(
        ["product_spec", "competitor_analysis", "campaign_strategy", "content_creator"],
        ["Product A", "Product B"],
        fake_run,
        DEPS,
    )

    assert results["product_spec"] == "spec result"
    assert results["competitor_analysis"] == "competitor result"
    assert results["campaign_strategy"] == "campaign result"
    assert results["content_creator"] == "content result"
    assert calls[3][1] == "Product A + Product B"


def test_run_flow_steps_empty_agents():
    """flow ไม่มี agent → คืนผลลัพธ์ว่าง."""
    from src.flow_runner import run_flow_steps

    results = run_flow_steps([], ["Product A"], lambda a, p, c: "")
    assert results == {}
