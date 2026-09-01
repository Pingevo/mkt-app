from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from unittest.mock import MagicMock

from src.evaluation.campaign_qualification import (
    BudgetGuard,
    QualificationBudgetExhausted,
    read_usage_log_for_run,
    aggregate_run_usage,
    evaluate_gate3_behavior,
)
from src.llm_client import LLMClient


@pytest.fixture(autouse=True)
def _disable_hub_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    # AI Usage Hub POST is not the LLM call we are budgeting; silence it in tests.
    monkeypatch.setattr("src.llm_client.record_ai_usage", lambda *a, **k: None)


class _FakeResponse:
    status_code = 200

    def __init__(self, text: str = "ok") -> None:
        self._text = text

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {
            "id": "req-test",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            "choices": [
                {
                    "message": {
                        "content": self._text,
                        "annotations": [
                            {"url": "https://lagenio.com/k2", "title": "k2", "content": "x"}
                        ],
                    }
                }
            ],
        }


class _FakeStream:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = lines or ["data: {\"id\":\"req-test\",\"usage\":null,\"choices\":[]}"]

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def iter_lines(self) -> list[str]:
        return self.lines

    def raise_for_status(self) -> None:
        pass


def _install_fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    fake_post = MagicMock(return_value=_FakeResponse("ok"))
    fake_stream = MagicMock(return_value=_FakeStream())
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    monkeypatch.setattr(httpx.Client, "stream", fake_stream)
    return fake_post, fake_stream


def test_first_and_second_post_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch)
    with BudgetGuard(budget=2) as guard:
        client = httpx.Client()
        client.post("/chat/completions", json={})
        client.post("/chat/completions", json={})
    assert guard.used == 2
    assert len(guard.attempt_log) == 2
    assert all(e["status"] == "sent" for e in guard.attempt_log)


def test_third_post_blocked_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_post, _ = _install_fakes(monkeypatch)
    with BudgetGuard(budget=2) as guard:
        client = httpx.Client()
        client.post("/chat/completions", json={})
        client.post("/chat/completions", json={})
        with pytest.raises(QualificationBudgetExhausted) as exc:
            client.post("/chat/completions", json={})
    assert guard.used == 2
    assert exc.value.attempts == 2
    assert exc.value.budget == 2
    assert exc.value.next_request == "/chat/completions"
    # The original httpx post should only have been called twice.
    assert fake_post.call_count == 2


def test_stream_counts_as_outbound_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake_stream = _install_fakes(monkeypatch)
    with BudgetGuard(budget=2) as guard:
        client = httpx.Client()
        with client.stream("POST", "/chat/completions", json={}):
            pass
        with client.stream("POST", "/chat/completions", json={}):
            pass
        with pytest.raises(QualificationBudgetExhausted):
            with client.stream("POST", "/chat/completions", json={}):
                pass
    assert guard.used == 2
    assert fake_stream.call_count == 2


def test_http_retry_counts_as_separate_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single llm.chat() logical call may retry HTTP; each post counts."""
    responses = [httpx.TimeoutException("timeout"), _FakeResponse("ok")]
    fake_post = MagicMock(side_effect=responses)
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with BudgetGuard(budget=2) as guard:
        client = LLMClient(api_key="test-key")
        text, annotations = client.chat(
            [{"role": "user", "content": "hello"}],
            return_annotations=True,
            max_retry_limit=3,
        )
    assert text == "ok"
    assert len(annotations) == 1
    assert guard.used == 2
    assert guard.attempt_log[0]["status"] == "error"
    assert guard.attempt_log[1]["status"] == "sent"


def test_repair_call_counts_as_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two logical llm.chat() calls (generate + repair) consume two attempts."""
    fake_post = MagicMock(return_value=_FakeResponse("ok"))
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with BudgetGuard(budget=2) as guard:
        client = LLMClient(api_key="test-key")
        client.chat([{"role": "user", "content": "gen"}], return_annotations=True)
        client.chat([{"role": "user", "content": "repair"}], return_annotations=True)
    assert guard.used == 2
    assert fake_post.call_count == 2


def test_third_logical_call_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_post = MagicMock(return_value=_FakeResponse("ok"))
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with BudgetGuard(budget=2) as guard:
        client = LLMClient(api_key="test-key")
        client.chat([{"role": "user", "content": "1"}], return_annotations=True)
        client.chat([{"role": "user", "content": "2"}], return_annotations=True)
        with pytest.raises(QualificationBudgetExhausted):
            client.chat([{"role": "user", "content": "3"}], return_annotations=True)
    assert guard.used == 2
    assert fake_post.call_count == 2


def test_no_fallback_after_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_post = MagicMock(return_value=_FakeResponse("ok"))
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with BudgetGuard(budget=1) as guard:
        client = httpx.Client()
        client.post("/chat/completions", json={})
        with pytest.raises(QualificationBudgetExhausted):
            client.post("/chat/completions", json={})
    # No extra calls happened after the budget exception.
    assert fake_post.call_count == 1


def test_exhausted_attempt_is_logged_with_block_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch)
    with BudgetGuard(budget=2) as guard:
        client = httpx.Client()
        client.post("/chat/completions", json={})
        client.post("/chat/completions", json={})
        with pytest.raises(QualificationBudgetExhausted):
            client.post("/chat/completions", json={})
    blocked = [e for e in guard.attempt_log if e["status"] == "blocked"]
    assert len(blocked) == 1
    assert blocked[0]["url"] == "/chat/completions"


def test_hub_post_is_not_counted_as_paid_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI Usage Hub POSTs must not consume the LLM attempt budget."""
    fake_post = MagicMock(return_value=_FakeResponse("ok"))
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with BudgetGuard(budget=2) as guard:
        client = httpx.Client()
        client.post("https://digital.in.th/internal/ai-usage/logs", json={"entry": 1})
        client.post("/chat/completions", json={})
    assert guard.used == 1
    assert fake_post.call_count == 2


def _capture_usage(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr("src.llm_client.record_ai_usage", captured.append)
    return captured


def test_generation_usage_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx.Client, "post", MagicMock(return_value=_FakeResponse("ok")))
    captured = _capture_usage(monkeypatch)
    client = LLMClient(api_key="test-key")
    client.chat(
        [{"role": "user", "content": "hello"}],
        model="openrouter/free",
        source="campaign_strategy.generate",
        max_retry_limit=1,
        stream=False,
    )
    assert len(captured) == 1
    assert captured[0]["model"] == "openrouter/free"
    assert captured[0]["source"] == "campaign_strategy.generate"
    assert captured[0]["prompt_tokens"] == 10
    assert captured[0]["completion_tokens"] == 5


def test_repair_usage_is_recorded_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each logical LLM call (generate + repair) creates a distinct usage entry."""
    monkeypatch.setattr(httpx.Client, "post", MagicMock(return_value=_FakeResponse("ok")))
    captured = _capture_usage(monkeypatch)
    client = LLMClient(api_key="test-key")
    client.chat(
        [{"role": "user", "content": "gen"}],
        source="campaign_strategy.generate",
        max_retry_limit=1,
        stream=False,
    )
    client.chat(
        [{"role": "user", "content": "repair"}],
        source="campaign_strategy.repair",
        max_retry_limit=1,
        stream=False,
    )
    assert len(captured) == 2
    assert captured[0]["source"] == "campaign_strategy.generate"
    assert captured[1]["source"] == "campaign_strategy.repair"
    assert captured[0]["attempt"] == 1
    assert captured[1]["attempt"] == 1


def test_usage_logging_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx.Client, "post", MagicMock(return_value=_FakeResponse("ok")))
    monkeypatch.setattr("src.llm_client.record_ai_usage", MagicMock(side_effect=RuntimeError("hub down")))
    client = LLMClient(api_key="test-key")
    text = client.chat([{"role": "user", "content": "hello"}], max_retry_limit=1, stream=False)
    assert text == "ok"


def test_streaming_call_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streaming path still logs usage when the SSE response contains usage."""
    captured = _capture_usage(monkeypatch)
    client = LLMClient(api_key="test-key")
    monkeypatch.setattr(
        client,
        "_chat_stream",
        lambda _payload: ("ok", {"prompt_tokens": 8, "completion_tokens": 3}, "req-stream"),
    )
    text = client.chat(
        [{"role": "user", "content": "hello"}],
        model="openrouter/free",
        source="campaign_strategy.generate",
        max_retry_limit=1,
        stream=True,
    )
    assert text == "ok"
    assert len(captured) == 1
    assert captured[0]["source"] == "campaign_strategy.generate"
    assert captured[0]["prompt_tokens"] == 8
    assert captured[0]["completion_tokens"] == 3


def test_no_secret_in_usage_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx.Client, "post", MagicMock(return_value=_FakeResponse("ok")))
    captured = _capture_usage(monkeypatch)
    client = LLMClient(api_key="super-secret-test-key-12345")
    client.chat([{"role": "user", "content": "hello"}], max_retry_limit=1, stream=False)
    assert len(captured) == 1
    entry_text = json.dumps(captured[0], ensure_ascii=False)
    assert "super-secret-test-key-12345" not in entry_text
    assert "api_key" not in entry_text.lower()


def test_run_usage_log_excludes_unrelated_and_outside_time(tmp_path: Path) -> None:
    """Cost aggregation reads only the run's own entries by source and time."""
    log_path = tmp_path / "llm_usage.jsonl"
    window = datetime(2026, 8, 31, 10, 0, 0, tzinfo=timezone.utc)
    entries = [
        {"timestamp": "2026-08-31T09:59:00+00:00", "source": "campaign_strategy.generate", "request_id": "before", "cost_usd": 0.001, "prompt_tokens": 1, "completion_tokens": 1},
        {"timestamp": "2026-08-31T10:00:05+00:00", "source": "campaign_strategy.generate", "request_id": "gen-1", "cost_usd": 0.011, "prompt_tokens": 100, "completion_tokens": 200},
        {"timestamp": "2026-08-31T10:00:06+00:00", "source": "campaign_strategy.repair", "request_id": "repair-1", "cost_usd": 0.009, "prompt_tokens": 80, "completion_tokens": 120},
        {"timestamp": "2026-08-31T10:00:07+00:00", "source": "content_creator.generate", "request_id": "other", "cost_usd": 0.005, "prompt_tokens": 50, "completion_tokens": 50},
        {"timestamp": "2026-08-31T10:01:00+00:00", "source": "campaign_strategy.generate", "request_id": "after", "cost_usd": 0.002, "prompt_tokens": 10, "completion_tokens": 10},
    ]
    log_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries), encoding="utf-8")

    start = window
    end = window.replace(second=30)
    selected = read_usage_log_for_run(log_path, start, end, source_prefix="campaign_strategy")
    summary = aggregate_run_usage(selected)

    assert summary.call_count == 2
    assert {c["request_id"] for c in summary.per_call} == {"gen-1", "repair-1"}
    assert summary.total_cost == pytest.approx(0.020)
    assert summary.total_prompt_tokens == 180
    assert summary.total_completion_tokens == 320
    assert summary.total_tokens == 500


def test_gate3_behavior_fails_when_evidence_omitted_despite_validators():
    raw = [{"url": "https://www.central.co.th/th/imoo-z1", "title": "imoo Z1", "content": "฿3,999"}]
    output = "ไม่พบข้อมูลแหล่งอ้างอิง จึงไม่สามารถเสนอราคาได้"
    ok, reason = evaluate_gate3_behavior(output, raw, [], "หาข้อมูลคู่แข่ง imoo Z1 ราคา")
    assert not ok
    assert "denies evidence" in reason


def test_gate3_behavior_fails_when_no_citation():
    raw = [{"url": "https://www.central.co.th/th/imoo-z1", "title": "imoo Z1", "content": "฿3,999"}]
    output = "imoo Z1 ราคาประมาณ ฿3,999"
    ok, reason = evaluate_gate3_behavior(output, raw, ["https://www.central.co.th/th/imoo-z1"], "หาราคา imoo Z1")
    assert not ok
    assert "did not cite" in reason


def test_gate3_behavior_passes_when_price_cited_to_selected_evidence():
    raw = [{"url": "https://www.central.co.th/th/imoo-z1", "title": "imoo Z1", "content": "฿3,999"}]
    output = "imoo Z1 ราคา [฿3,999](https://www.central.co.th/th/imoo-z1)"
    ok, reason = evaluate_gate3_behavior(output, raw, ["https://www.central.co.th/th/imoo-z1"], "หาราคา imoo Z1")
    assert ok
    assert reason == ""
