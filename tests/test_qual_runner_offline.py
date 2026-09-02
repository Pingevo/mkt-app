"""Offline tests for the qualification runner.

These tests do not call the model or perform any paid work.  They verify the
dry-run cost planner and the hard PaidCallGuard defined in scripts/qual_runner.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

# qual_runner is in scripts/, not src/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_dry_run_cost_plan_content_creator():
    """dry_run_cost_plan returns a bounded, actionable cost plan without network."""
    import qual_runner

    plan = qual_runner.dry_run_cost_plan(
        agent_key="content_creator",
        product_id="Lagenio K2",
        quick_brief="Focus on family lifestyle",
        content_count=2,
        platforms=["facebook", "tiktok"],
        auto_image=True,
        auto_video=False,
    )
    assert plan["agent_key"] == "content_creator"
    assert plan["max_calls"] > 0
    assert plan["media_calls"] > 0
    assert plan["conservative_estimate"] > 0
    assert plan["conservative_estimate"] <= plan["ceiling_stage_a"]
    assert any(c["kind"] == "image" for c in plan["expected_calls"])
    assert all(c.get("kind") for c in plan["expected_calls"])


def test_dry_run_cost_plan_competitor_analysis_counts_web_search():
    """competitor_analysis plan separates web-search calls from text calls,
    and the configured hard call cap is enforced."""
    import qual_runner

    plan = qual_runner.dry_run_cost_plan(
        agent_key="competitor_analysis",
        product_id="Lagenio K2",
        quick_brief="Compare with imoo Z1 and myFirst R1s",
    )
    assert plan["web_search_calls"] > 0
    assert plan["conservative_estimate"] > 0
    assert plan["max_calls"] == qual_runner._load_qualification_config()["default_max_calls"]["competitor_analysis"]
    assert plan["status"].startswith("exceeds call cap") or len(plan["expected_calls"]) <= plan["max_calls"]


def test_paid_call_guard_blocks_after_max_calls(monkeypatch):
    """PaidCallGuard hard-caps the number of paid httpx calls."""
    import qual_runner

    # Zero out cumulative spend so the USD guard does not fire first.
    monkeypatch.setattr(qual_runner, "_read_total_cost", lambda: 0.0)
    monkeypatch.setattr(qual_runner, "_SESSION_BASELINE", 0.0)

    calls = []

    def fake_post(client, url, **kwargs):
        calls.append(url)
        return MagicMock()

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    with qual_runner.PaidCallGuard("A", 2) as guard:
        httpx.Client.post(None, "https://openrouter.ai/api/v1/chat/completions")
        httpx.Client.post(None, "https://openrouter.ai/api/v1/chat/completions")
        with pytest.raises(qual_runner.QualificationBudgetExhausted):
            httpx.Client.post(None, "https://openrouter.ai/api/v1/chat/completions")

    assert len(calls) == 2
    assert guard.actual_calls() == [
        {"url": "https://openrouter.ai/api/v1/chat/completions", "kind": "text", "allowed": True},
        {"url": "https://openrouter.ai/api/v1/chat/completions", "kind": "text", "allowed": True},
    ]


def test_paid_call_guard_blocks_by_usd_ceiling(monkeypatch):
    """PaidCallGuard blocks the next call when the conservative per-call reserve
    would push cumulative spend over the stage-A ceiling."""
    import qual_runner

    monkeypatch.setattr(qual_runner, "_read_total_cost", lambda: 0.78)
    monkeypatch.setattr(qual_runner, "_SESSION_BASELINE", 0.0)

    with qual_runner.PaidCallGuard("A", 100):
        with pytest.raises(qual_runner.QualificationBudgetExhausted):
            httpx.Client.post(None, "https://openrouter.ai/api/v1/chat/completions")


def test_paid_call_guard_blocks_relative_chat_completions(monkeypatch):
    """Production calls /chat/completions relative to an openrouter base_url
    must be counted and blocked at the hard call cap."""
    import qual_runner

    monkeypatch.setattr(qual_runner, "_read_total_cost", lambda: 0.0)
    monkeypatch.setattr(qual_runner, "_SESSION_BASELINE", 0.0)

    calls = []

    def fake_post(client, url, **kwargs):
        calls.append(url)
        return MagicMock()

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    client = httpx.Client(base_url="https://openrouter.ai/api/v1/")
    with qual_runner.PaidCallGuard("A", 1) as guard:
        client.post("/chat/completions")
        with pytest.raises(qual_runner.QualificationBudgetExhausted):
            client.post("/chat/completions")

    assert calls == ["/chat/completions"]
    assert guard.actual_calls()[0] == {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "kind": "text",
        "allowed": True,
    }


def test_paid_call_guard_blocks_relative_stream(monkeypatch):
    """Production streaming requests (relative /chat/completions) are guarded."""
    import qual_runner

    monkeypatch.setattr(qual_runner, "_read_total_cost", lambda: 0.0)
    monkeypatch.setattr(qual_runner, "_SESSION_BASELINE", 0.0)

    calls = []

    def fake_stream(client, method, url, **kwargs):
        calls.append((method, url))
        return MagicMock()

    monkeypatch.setattr(httpx.Client, "stream", fake_stream)

    client = httpx.Client(base_url="https://openrouter.ai/api/v1/")
    with qual_runner.PaidCallGuard("A", 1) as guard:
        client.stream("POST", "/chat/completions")
        with pytest.raises(qual_runner.QualificationBudgetExhausted):
            client.stream("POST", "/chat/completions")

    assert calls == [("POST", "/chat/completions")]
    assert guard.actual_calls()[0] == {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "kind": "text",
        "allowed": True,
    }


def test_paid_call_guard_uses_web_search_reserve_for_tool_calls(monkeypatch):
    """A /chat/completions call carrying the real OpenRouter web tools
    reserves the web_search ceiling, not the cheaper text ceiling,
    for both relative post and relative stream."""
    import qual_runner

    # Zero out cumulative spend so the reserve does not block; we want to
    # see the allowed call log and confirm the higher reserve class.
    monkeypatch.setattr(qual_runner, "_read_total_cost", lambda: 0.0)
    monkeypatch.setattr(qual_runner, "_SESSION_BASELINE", 0.0)

    posts = []
    streams = []

    def fake_post(client, url, **kwargs):
        posts.append((url, kwargs.get("json", {})))
        return MagicMock()

    def fake_stream(client, method, url, **kwargs):
        streams.append((method, url, kwargs.get("json", {})))
        return MagicMock()

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    monkeypatch.setattr(httpx.Client, "stream", fake_stream)

    # Same tool shape that BaseAgent._build_web_search_tools() returns.
    web_search_tools = [
        {"type": "openrouter:web_search", "parameters": {"max_results": 5}},
        {"type": "openrouter:web_fetch"},
    ]

    client = httpx.Client(base_url="https://openrouter.ai/api/v1/")
    with qual_runner.PaidCallGuard("A", 3) as guard:
        # Plain chat completion (no tools) is classified as text.
        client.post(
            "/chat/completions",
            json={"model": "test", "messages": []},
        )
        # Post and stream with real OpenRouter web tools are classified as
        # web_search and therefore reserve the higher web_search ceiling.
        client.post(
            "/chat/completions",
            json={"model": "test", "messages": [], "tools": web_search_tools},
        )
        client.stream(
            "POST",
            "/chat/completions",
            json={"model": "test", "messages": [], "tools": web_search_tools},
        )

    assert qual_runner._per_call_ceiling("text") < qual_runner._per_call_ceiling("web_search")
    assert len(posts) == 2
    assert len(streams) == 1
    assert guard.actual_calls() == [
        {"url": "https://openrouter.ai/api/v1/chat/completions", "kind": "text", "allowed": True},
        {"url": "https://openrouter.ai/api/v1/chat/completions", "kind": "web_search", "allowed": True},
        {"url": "https://openrouter.ai/api/v1/chat/completions", "kind": "web_search", "allowed": True},
    ]
