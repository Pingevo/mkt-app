"""Regression tests for AI-USAGE-OPENROUTER-CONSOLIDATE-01.

Verifies accounting-by-construction for all paid-capable OpenRouter operations:
- Chat (m6_judge_runner routes through LLMClient)
- Embeddings (asset_library + content_history route through llm_client.generate_embedding)
- Image/video (existing media_gen seam — already tested elsewhere)

Contract:
    1. m6_judge_runner._call_judge_raw routes through LLMClient.chat()
    2. Exactly one normalized AI Usage event per real Judge call
    3. Judge actual provider cost reaches cost_usd at the Hub boundary
    4. Existing Judge budget accounting remains correct (M6JudgeGuard)
    5. Asset-library embedding routes through canonical seam and emits Hub usage
    6. Content-history embedding does the same
    7. Successful embedding emits exactly one event
    8. Embedding provider errors/timeouts emit appropriate events without invented cost
    9. Hub delivery failure does not break successful chat, embedding, or media operations

All tests are offline: no provider calls, no Hub POST, no paid calls, budget $0.
"""
import json
import sys
from pathlib import Path
from unittest import mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helpers — mock HTTP + capture usage events
# ---------------------------------------------------------------------------

def _patch_http(monkeypatch, handler):
    """Inject a MockTransport into every httpx.Client."""
    transport = httpx.MockTransport(handler)
    original_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)


def _capture_usage(monkeypatch):
    """Capture record_ai_usage calls. Returns the list of captured entries."""
    from src import ai_usage
    captured: list[dict] = []

    def _capture(entry):
        captured.append(dict(entry))

    monkeypatch.setattr(ai_usage, "record_ai_usage", _capture)
    # Patch in the gate module since it imports record_ai_usage at module level
    from src import openrouter_gateway
    monkeypatch.setattr(openrouter_gateway, "record_ai_usage", _capture)
    return captured


def _capture_usage_with_hub_fail(monkeypatch):
    """Capture record_ai_usage calls but simulate Hub delivery failure."""
    from src import ai_usage
    from src import openrouter_gateway

    def _failing(entry):
        raise RuntimeError("simulated Hub failure")

    monkeypatch.setattr(ai_usage, "record_ai_usage", _failing)
    monkeypatch.setattr(openrouter_gateway, "record_ai_usage", _failing)


# ---------------------------------------------------------------------------
# Judge: routes through LLMClient, exactly one event, actual cost
# ---------------------------------------------------------------------------

def test_judge_routes_through_llm_client(monkeypatch):
    """_call_judge_raw uses LLMClient.chat() — no direct httpx.Client.post
    to /chat/completions."""
    from scripts import m6_judge_runner

    captured = _capture_usage(monkeypatch)

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url.endswith("/chat/completions"):
            return httpx.Response(200, json={
                "id": "req-judge-1",
                "model": "openai/gpt-5.6-sol",
                "choices": [{"message": {"content": '{"s1": 3}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.002},
            })
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    result = m6_judge_runner._call_judge_raw(
        [{"role": "user", "content": "test"}],
        "fake-api-key",
    )
    assert result["model"] == "openai/gpt-5.6-sol"
    assert result["request_id"] == "req-judge-1"
    assert result["http_status"] == 200
    # Exactly one Hub event from LLMClient
    assert len(captured) == 1
    assert captured[0]["operation"] == "chat.completions"
    assert captured[0]["source"] == "m6_judge"


def test_judge_actual_cost_reaches_hub(monkeypatch):
    """Judge provider-reported cost ($0.002) reaches cost_usd at the Hub boundary."""
    from scripts import m6_judge_runner

    captured = _capture_usage(monkeypatch)

    def handler(request):
        return httpx.Response(200, json={
            "id": "req-judge-cost",
            "model": "openai/gpt-5.6-sol",
            "choices": [{"message": {"content": '{"s1": 3}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.002},
        })

    _patch_http(monkeypatch, handler)

    result = m6_judge_runner._call_judge_raw(
        [{"role": "user", "content": "test"}],
        "fake-api-key",
    )
    assert result["cost"] == 0.002
    assert captured[0]["cost_usd"] == 0.002


def test_judge_exactly_one_event_per_call(monkeypatch):
    """Exactly one normalized AI Usage event per real Judge call — no duplicates."""
    from scripts import m6_judge_runner

    captured = _capture_usage(monkeypatch)

    def handler(request):
        return httpx.Response(200, json={
            "id": "req-judge-once",
            "model": "openai/gpt-5.6-sol",
            "choices": [{"message": {"content": '{"s1": 3}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        })

    _patch_http(monkeypatch, handler)

    m6_judge_runner._call_judge_raw(
        [{"role": "user", "content": "test"}],
        "fake-api-key",
    )
    assert len(captured) == 1


def test_judge_error_still_logs_event(monkeypatch):
    """Judge provider error logs an error event without inventing cost.

    Without an M6JudgeGuard active, a 500 error means no guard audit is
    stored, so _call_judge_raw re-raises (pre-call rejection semantics).
    LLMClient still logs the error event to the Hub before raising.
    """
    from scripts import m6_judge_runner

    captured = _capture_usage(monkeypatch)

    def handler(request):
        return httpx.Response(500, json={"error": {"message": "server error"}})

    _patch_http(monkeypatch, handler)

    # Without a guard, the 500 error propagates (no charged response to preserve)
    with pytest.raises(Exception):
        m6_judge_runner._call_judge_raw(
            [{"role": "user", "content": "test"}],
            "fake-api-key",
        )
    # LLMClient logged the error event before raising
    assert len(captured) == 1
    assert captured[0]["status"] == "error"
    # No invented cost
    assert captured[0].get("cost_usd") is None


def test_judge_budget_guard_still_works(monkeypatch):
    """M6JudgeGuard budget preflight still prevents the call when budget exceeded."""
    from scripts import m6_judge_runner

    captured = _capture_usage(monkeypatch)

    call_made = {"count": 0}

    def handler(request):
        call_made["count"] += 1
        return httpx.Response(200, json={
            "id": "req-judge-guard",
            "model": "openai/gpt-5.6-sol",
            "choices": [{"message": {"content": '{"s1": 3}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        })

    _patch_http(monkeypatch, handler)

    # Budget guard raises before the call
    def budget_guard_fn():
        raise RuntimeError("Budget exceeded — stopping before paid call")

    # The guard raises, which propagates out of _call_judge_raw
    # (LLMClient doesn't catch RuntimeError, only httpx exceptions)
    with pytest.raises(RuntimeError, match="Budget exceeded"):
        m6_judge_runner._call_judge_raw(
            [{"role": "user", "content": "test"}],
            "fake-api-key",
            budget_guard_fn=budget_guard_fn,
        )
    # No call was made, no Hub event
    assert call_made["count"] == 0
    assert len(captured) == 0


def test_judge_hub_failure_does_not_break_chat(monkeypatch):
    """Hub delivery failure does not break the Judge call — LLMClient returns text."""
    from scripts import m6_judge_runner

    _capture_usage_with_hub_fail(monkeypatch)

    def handler(request):
        return httpx.Response(200, json={
            "id": "req-judge-hubfail",
            "model": "openai/gpt-5.6-sol",
            "choices": [{"message": {"content": '{"s1": 3}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        })

    _patch_http(monkeypatch, handler)

    # record_ai_usage raises, but LLMClient should still return the text
    # because Hub delivery is fire-and-forget (swallowed by ai_usage internals)
    # However, if record_ai_usage raises directly (not swallowed), LLMClient
    # would propagate the error. The test verifies that the call itself
    # completes and returns a valid result.
    result = m6_judge_runner._call_judge_raw(
        [{"role": "user", "content": "test"}],
        "fake-api-key",
    )
    # If Hub failure is swallowed by ai_usage, we get a success result.
    # If not, the RuntimeError propagates. Either way, the provider call
    # was made and the response was received.
    assert result["model"] == "openai/gpt-5.6-sol"


# ---------------------------------------------------------------------------
# Embeddings: asset_library routes through canonical seam
# ---------------------------------------------------------------------------

def test_asset_library_embedding_routes_through_seam(monkeypatch):
    """asset_library._default_embedder calls llm_client.generate_embedding."""
    from src import asset_library
    from src import llm_client

    captured = _capture_usage(monkeypatch)

    call_args = {"model": None, "source": None}

    original_generate = llm_client.generate_embedding

    def _spy_generate(text, *, model="openai/text-embedding-3-small",
                      timeout=30, source="embedding.generate"):
        call_args["model"] = model
        call_args["source"] = source
        return original_generate(text, model=model, timeout=timeout, source=source)

    monkeypatch.setattr(llm_client, "generate_embedding", _spy_generate)

    def handler(request):
        return httpx.Response(200, json={
            "id": "emb-1",
            "data": [{"embedding": [0.1, 0.2, 0.3]}],
            "usage": {"prompt_tokens": 5, "cost": 0.0001},
        })

    _patch_http(monkeypatch, handler)

    result = asset_library._default_embedder("test text", {"embedding": {"model": "openai/text-embedding-3-small"}})
    assert result == [0.1, 0.2, 0.3]
    assert call_args["source"] == "asset_library.embedding"
    assert len(captured) == 1
    assert captured[0]["source"] == "asset_library.embedding"
    assert captured[0]["operation"] == "embeddings.create"
    assert captured[0]["cost_usd"] == 0.0001


def test_content_history_embedding_routes_through_seam(monkeypatch):
    """content_history._generate_embedding calls llm_client.generate_embedding."""
    from src import content_history
    from src import llm_client

    captured = _capture_usage(monkeypatch)

    call_args = {"model": None, "source": None}

    original_generate = llm_client.generate_embedding

    def _spy_generate(text, *, model="openai/text-embedding-3-small",
                      timeout=30, source="embedding.generate"):
        call_args["model"] = model
        call_args["source"] = source
        return original_generate(text, model=model, timeout=timeout, source=source)

    monkeypatch.setattr(llm_client, "generate_embedding", _spy_generate)

    def handler(request):
        return httpx.Response(200, json={
            "id": "emb-2",
            "data": [{"embedding": [0.4, 0.5, 0.6]}],
            "usage": {"prompt_tokens": 8, "cost": 0.0002},
        })

    _patch_http(monkeypatch, handler)

    result = content_history._generate_embedding("test text", {"dedup_model": "openai/text-embedding-3-small"})
    assert result == [0.4, 0.5, 0.6]
    assert call_args["source"] == "content_history.generate_embedding"
    assert len(captured) == 1
    assert captured[0]["source"] == "content_history.generate_embedding"
    assert captured[0]["operation"] == "embeddings.create"
    assert captured[0]["cost_usd"] == 0.0002


def test_embedding_success_exactly_one_event(monkeypatch):
    """Successful embedding emits exactly one Hub event."""
    from src import llm_client

    captured = _capture_usage(monkeypatch)
    monkeypatch.setattr("os.environ.get", lambda key, default=None: "fake-key" if key == "OPENROUTER_API_KEY" else default)

    def handler(request):
        return httpx.Response(200, json={
            "id": "emb-once",
            "data": [{"embedding": [0.1]}],
            "usage": {"prompt_tokens": 3, "cost": 0.0001},
        })

    _patch_http(monkeypatch, handler)

    result = llm_client.generate_embedding("test", source="test.embedding")
    assert result == [0.1]
    assert len(captured) == 1


def test_embedding_error_logs_event_without_invented_cost(monkeypatch):
    """Embedding provider error emits an error event without inventing cost."""
    from src import llm_client

    captured = _capture_usage(monkeypatch)
    monkeypatch.setattr("os.environ.get", lambda key, default=None: "fake-key" if key == "OPENROUTER_API_KEY" else default)

    def handler(request):
        return httpx.Response(500, json={"error": "server error"})

    _patch_http(monkeypatch, handler)

    result = llm_client.generate_embedding("test", source="test.embedding")
    assert result is None
    assert len(captured) == 1
    assert captured[0]["status"] == "error"
    assert captured[0].get("cost_usd") is None


def test_embedding_timeout_logs_event_without_invented_cost(monkeypatch):
    """Embedding timeout emits a timeout event without inventing cost."""
    from src import llm_client

    captured = _capture_usage(monkeypatch)
    monkeypatch.setattr("os.environ.get", lambda key, default=None: "fake-key" if key == "OPENROUTER_API_KEY" else default)

    def handler(request):
        raise httpx.TimeoutException("simulated timeout")

    _patch_http(monkeypatch, handler)

    result = llm_client.generate_embedding("test", source="test.embedding", timeout=1)
    assert result is None
    assert len(captured) == 1
    assert captured[0]["status"] == "timeout"
    assert captured[0].get("cost_usd") is None


def test_embedding_hub_failure_does_not_break_caller(monkeypatch):
    """Hub delivery failure does not break the embedding caller — returns the
    embedding vector (Hub delivery is fire-and-forget)."""
    from src import llm_client

    _capture_usage_with_hub_fail(monkeypatch)
    monkeypatch.setattr("os.environ.get", lambda key, default=None: "fake-key" if key == "OPENROUTER_API_KEY" else default)

    def handler(request):
        return httpx.Response(200, json={
            "id": "emb-hubfail",
            "data": [{"embedding": [0.1]}],
            "usage": {"prompt_tokens": 3, "cost": 0.0001},
        })

    _patch_http(monkeypatch, handler)

    # The embedding seam wraps record_ai_usage in try/except (fire-and-forget).
    # Hub failure should not break the caller — the embedding is returned.
    result = llm_client.generate_embedding("test", source="test.embedding")
    assert result == [0.1]


def test_embedding_no_api_key_returns_none_no_event(monkeypatch):
    """No API key → returns None, no Hub event (no call was made)."""
    from src import llm_client

    captured = _capture_usage(monkeypatch)
    monkeypatch.setattr("os.environ.get", lambda key, default=None: None if key == "OPENROUTER_API_KEY" else default)

    result = llm_client.generate_embedding("test", source="test.embedding")
    assert result is None
    assert len(captured) == 0
