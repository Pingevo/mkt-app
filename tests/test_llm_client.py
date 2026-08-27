"""Tests: LLMClient retries empty content responses like transient failures.

Bug: OpenRouter can return HTTP 200 with empty `content` (model timeout,
refusal, or upstream issue). The original code treated this as success and
returned "" — letting agents believe generation succeeded with no output.

Fix: Treat empty content as a failed attempt and retry up to
``max_retry_limit`` (same config value used for HTTP/timeout retries).
Log as ``status="error"`` with ``error_message="empty response"`` — no new
status enum. After exhausting retries, raise a clear error.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_client():
    """Build LLMClient without touching network — httpx.Client is mocked."""
    from src.llm_client import LLMClient
    client = LLMClient(api_key="test-key", default_model="test-model")
    client._client = MagicMock()
    return client


def _stream_lines(chunks):
    """Build SSE lines that yield the given delta-content chunks then [DONE]."""
    import json as _json
    lines = []
    for c in chunks:
        lines.append('data: ' + _json.dumps({"choices": [{"delta": {"content": c}}]}))
    lines.append('data: [DONE]')
    return lines


def _make_stream_response(lines):
    """Mock httpx stream context manager yielding SSE lines.

    httpx.Client.stream() returns a context manager whose __enter__ gives
    the response. We replicate that shape so `with client.stream(...) as resp:`
    works and `resp.iter_lines()` yields the given lines.
    """
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.iter_lines = MagicMock(return_value=iter(lines))

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=resp)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def test_empty_stream_content_retries_then_returns_nonempty():
    """First stream returns no content → retry → second stream returns content."""
    client = _make_client()

    empty_stream = _make_stream_response(['data: [DONE]'])
    good_stream = _make_stream_response(_stream_lines(["hello world"]))
    client._client.stream = MagicMock(side_effect=[empty_stream, good_stream])

    with patch('time.sleep'):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=True,
        )

    assert result == "hello world"
    assert client._client.stream.call_count == 2


def test_empty_stream_exhausts_retries_then_raises():
    """All retries return empty content → raise clear error."""
    client = _make_client()

    empty_stream = _make_stream_response(['data: [DONE]'])
    client._client.stream = MagicMock(side_effect=[empty_stream] * 3)

    with patch('time.sleep'):
        with pytest.raises(RuntimeError) as exc:
            client.chat(
                [{"role": "user", "content": "hi"}],
                max_retry_limit=3,
                stream=True,
            )

    assert "empty" in str(exc.value).lower() or "no content" in str(exc.value).lower()
    assert client._client.stream.call_count == 3


def test_empty_nonstream_content_retries_then_returns_nonempty():
    """Non-stream path: first response has empty content → retry → second has content."""
    client = _make_client()

    empty_resp = MagicMock()
    empty_resp.raise_for_status = MagicMock()
    empty_resp.json = MagicMock(return_value={
        "id": "req-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "cost": 0.0},
        "choices": [{"message": {"content": ""}}],
    })

    good_resp = MagicMock()
    good_resp.raise_for_status = MagicMock()
    good_resp.json = MagicMock(return_value={
        "id": "req-2",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        "choices": [{"message": {"content": "real output"}}],
    })

    client._client.post = MagicMock(side_effect=[empty_resp, good_resp])

    with patch('time.sleep'):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=False,
        )

    assert result == "real output"
    assert client._client.post.call_count == 2


def test_empty_nonstream_exhausts_retries_then_raises():
    """Non-stream: all retries empty → raise clear error."""
    client = _make_client()

    empty_resp = MagicMock()
    empty_resp.raise_for_status = MagicMock()
    empty_resp.json = MagicMock(return_value={
        "id": "req-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "cost": 0.0},
        "choices": [{"message": {"content": ""}}],
    })

    client._client.post = MagicMock(side_effect=[empty_resp] * 3)

    with patch('time.sleep'):
        with pytest.raises(RuntimeError) as exc:
            client.chat(
                [{"role": "user", "content": "hi"}],
                max_retry_limit=3,
                stream=False,
            )

    assert "empty" in str(exc.value).lower() or "no content" in str(exc.value).lower()
    assert client._client.post.call_count == 3


def test_empty_response_with_annotations_still_retries():
    """web_search path (return_annotations=True): empty content + empty annotations → retry."""
    client = _make_client()

    empty_resp = MagicMock()
    empty_resp.raise_for_status = MagicMock()
    empty_resp.json = MagicMock(return_value={
        "id": "req-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "cost": 0.0},
        "choices": [{"message": {"content": "", "annotations": []}}],
    })

    good_resp = MagicMock()
    good_resp.raise_for_status = MagicMock()
    good_resp.json = MagicMock(return_value={
        "id": "req-2",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        "choices": [{"message": {"content": "real output", "annotations": []}}],
    })

    client._client.post = MagicMock(side_effect=[empty_resp, good_resp])

    with patch('time.sleep'):
        text, annotations = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            return_annotations=True,
        )

    assert text == "real output"
    assert annotations == []
    assert client._client.post.call_count == 2


def test_nonempty_response_does_not_retry():
    """Sanity: non-empty response returns immediately, no retry."""
    client = _make_client()

    good_resp = MagicMock()
    good_resp.raise_for_status = MagicMock()
    good_resp.json = MagicMock(return_value={
        "id": "req-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        "choices": [{"message": {"content": "ok"}}],
    })

    client._client.post = MagicMock(side_effect=[good_resp])

    result = client.chat(
        [{"role": "user", "content": "hi"}],
        max_retry_limit=3,
        stream=False,
    )

    assert result == "ok"
    assert client._client.post.call_count == 1
