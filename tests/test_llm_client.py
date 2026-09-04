"""Tests: LLMClient retries empty content responses like transient failures.

Bug: OpenRouter can return HTTP 200 with empty `content` (model timeout,
refusal, or upstream issue). The original code treated this as success and
returned "" — letting agents believe generation succeeded with no output.

Fix: Treat empty content as a failed attempt and retry up to
``max_retry_limit`` (same config value used for HTTP/timeout retries).
Log as ``status="error"`` with ``error_message="empty response"`` — no new
status enum. After exhausting retries, raise a clear error.
"""
import json as _json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
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


# ---------------------------------------------------------------------------
# Test helpers for per-attempt client (watchdog timeout design)
# ---------------------------------------------------------------------------

class _AttemptResponse:
    """Mock httpx stream response with predefined SSE lines."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.raise_for_status = MagicMock()
        self.close = MagicMock()

    def iter_lines(self):
        for line in self._lines:
            yield line


class _BlockingAttemptResponse:
    """Response where iter_lines() blocks until close_event is set, then raises.

    Simulates a stalled OpenRouter stream: socket read blocks indefinitely,
    only an external close() (from the watchdog) can interrupt it.
    """

    def __init__(self, lines_before_block, close_event):
        self._lines = list(lines_before_block)
        self._close_event = close_event
        self.raise_for_status = MagicMock()
        self.close = MagicMock()

    def iter_lines(self):
        for line in self._lines:
            yield line
        # Block — simulates waiting for next chunk that never arrives
        self._close_event.wait(timeout=30)
        raise httpx.ReadTimeout("connection closed by watchdog")


class _AttemptClient:
    """Mock httpx.Client for a single stream attempt.

    ``lines`` are yielded by iter_lines() before the stream ends normally.
    If ``block`` is True, iter_lines() blocks after yielding ``lines`` and
    only returns when ``close()`` is called (simulating a stalled stream).
    """

    def __init__(self, lines, *, block=False):
        self._lines = list(lines)
        self._block = block
        self._close_event = threading.Event()
        self._response = (
            _BlockingAttemptResponse(self._lines, self._close_event)
            if block
            else _AttemptResponse(self._lines)
        )
        self.close = MagicMock(side_effect=self._do_close)
        self.stream = MagicMock(side_effect=self._stream)

    def _stream(self, *args, **kwargs):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=self._response)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    def _do_close(self):
        self._close_event.set()


def _patch_attempt_clients(client, attempt_clients):
    """Patch ``_make_attempt_client`` to return the given clients in order.

    Returns the list of clients so tests can inspect them (e.g. assert close was called).
    """
    clients = list(attempt_clients)
    client._make_attempt_client = MagicMock(side_effect=clients)
    return clients


def _patch_timeouts(client, attempt_timeout, progress_timeout):
    """Patch ``_system_cfg`` to return short timeouts for testing."""
    cfg = {
        "stream_refresh_rate": 15,
        "stream_attempt_timeout_seconds": attempt_timeout,
        "stream_progress_timeout_seconds": progress_timeout,
    }
    return patch("src.llm_client._system_cfg", return_value=cfg)


def test_empty_stream_content_retries_then_returns_nonempty():
    """First stream returns no content → retry → second stream returns content."""
    client = _make_client()

    _patch_attempt_clients(client, [
        _AttemptClient(['data: [DONE]']),
        _AttemptClient(_stream_lines(["hello world"])),
    ])

    with patch('time.sleep'), _patch_timeouts(client, 30, 10):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=True,
        )

    assert result == "hello world"
    assert client._make_attempt_client.call_count == 2


def test_empty_stream_exhausts_retries_then_raises():
    """All retries return empty content → raise clear error."""
    client = _make_client()

    _patch_attempt_clients(client, [
        _AttemptClient(['data: [DONE]']),
    ] * 3)

    with patch('time.sleep'), _patch_timeouts(client, 30, 10):
        with pytest.raises(RuntimeError) as exc:
            client.chat(
                [{"role": "user", "content": "hi"}],
                max_retry_limit=3,
                stream=True,
            )

    assert "empty" in str(exc.value).lower() or "no content" in str(exc.value).lower()
    assert client._make_attempt_client.call_count == 3


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


# ---------------------------------------------------------------------------
# Tests: watchdog timeout + SSE protocol correctness
# ---------------------------------------------------------------------------

def test_blocked_stream_is_closed_at_progress_deadline_and_retried():
    """Stream sends nothing and blocks → watchdog closes at progress deadline → retry succeeds."""
    client = _make_client()

    clients = _patch_attempt_clients(client, [
        _AttemptClient([], block=True),  # blocks immediately, no lines
        _AttemptClient(_stream_lines(["hello after retry"])),
    ])

    with patch('time.sleep'), _patch_timeouts(client, 30, 0.3):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=True,
        )

    assert result == "hello after retry"
    # First attempt client was closed by watchdog
    assert clients[0].close.called, "watchdog should have closed the blocked attempt client"
    assert client._make_attempt_client.call_count == 2


def test_keepalive_does_not_reset_progress_deadline():
    """SSE comment lines (: OPENROUTER PROCESSING) do not reset progress deadline."""
    client = _make_client()

    keepalive_lines = [': OPENROUTER PROCESSING'] * 5
    _patch_attempt_clients(client, [
        _AttemptClient(keepalive_lines, block=True),
        _AttemptClient(_stream_lines(["ok"])),
    ])

    with patch('time.sleep'), _patch_timeouts(client, 30, 0.3):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=True,
        )

    assert result == "ok"
    assert client._make_attempt_client.call_count == 2


def test_model_content_resets_progress_deadline():
    """Real content resets progress deadline so short gaps don't trigger false timeout."""
    client = _make_client()

    # Content arrives, then a gap (block), but progress timeout should be generous enough
    # to not fire during the gap. We use a long progress timeout and short content.
    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "part1"}}]}),
    ]
    _patch_attempt_clients(client, [
        _AttemptClient(lines + ['data: [DONE]']),
    ])

    with _patch_timeouts(client, 30, 5):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=True,
        )

    assert result == "part1"
    assert client._make_attempt_client.call_count == 1


def test_total_deadline_closes_continuously_progressing_stream():
    """Stream keeps sending content but never terminates → total deadline closes it."""
    client = _make_client()

    # Content keeps coming but no [DONE] — and we block after the lines
    content_lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": f"chunk{i}"}}]})
        for i in range(3)
    ]
    _patch_attempt_clients(client, [
        _AttemptClient(content_lines, block=True),
        _AttemptClient(_stream_lines(["recovered"])),
    ])

    with patch('time.sleep'), _patch_timeouts(client, 0.3, 30):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=True,
        )

    # Should have retried because total deadline closed the first attempt
    assert client._make_attempt_client.call_count == 2


def test_midstream_error_event_retries_buffered_chat():
    """SSE error event mid-stream → RemoteProtocolError → retry → success."""
    client = _make_client()

    error_lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "partial"}}]}),
        'data: ' + _json.dumps({
            "error": {"code": "server_error", "message": "Provider disconnected"},
            "choices": [{"delta": {}, "finish_reason": "error"}],
        }),
    ]
    _patch_attempt_clients(client, [
        _AttemptClient(error_lines),
        _AttemptClient(_stream_lines(["full response"])),
    ])

    with patch('time.sleep'), _patch_timeouts(client, 30, 10):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=True,
        )

    # _chat_stream buffers, so partial content from first attempt is discarded
    assert result == "full response"
    assert client._make_attempt_client.call_count == 2


def test_eof_without_done_or_finish_reason_retries():
    """Stream ends without [DONE] or finish_reason → dropped → retry → success."""
    client = _make_client()

    dropped_lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "partial"}}]}),
        # EOF — no [DONE], no finish_reason
    ]
    _patch_attempt_clients(client, [
        _AttemptClient(dropped_lines),
        _AttemptClient(_stream_lines(["complete response"])),
    ])

    with patch('time.sleep'), _patch_timeouts(client, 30, 10):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=True,
        )

    assert result == "complete response"
    assert client._make_attempt_client.call_count == 2


def test_finish_reason_allows_eof_without_done():
    """finish_reason present but no [DONE] → treated as successful completion."""
    client = _make_client()

    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "hello"}}]}),
        'data: ' + _json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        # EOF — no [DONE], but finish_reason was seen
    ]
    _patch_attempt_clients(client, [
        _AttemptClient(lines),
    ])

    with _patch_timeouts(client, 30, 10):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=True,
        )

    assert result == "hello"
    assert client._make_attempt_client.call_count == 1


def test_empty_choices_usage_chunk_is_preserved():
    """Chunk with empty choices + usage (final usage chunk) doesn't crash or lose usage."""
    client = _make_client()

    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "text"}}]}),
        'data: ' + _json.dumps({
            "id": "req-123",
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        }),
        'data: [DONE]',
    ]
    _patch_attempt_clients(client, [
        _AttemptClient(lines),
    ])

    with _patch_timeouts(client, 30, 10):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=3,
            stream=True,
        )

    assert result == "text"
    assert client._make_attempt_client.call_count == 1


def test_yield_stream_does_not_retry_after_partial_content():
    """chat_stream_yield: after yielding content, error must NOT trigger retry."""
    client = _make_client()

    error_lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "partial"}}]}),
        'data: ' + _json.dumps({
            "error": {"message": "mid-stream failure"},
            "choices": [{"delta": {}, "finish_reason": "error"}],
        }),
    ]
    _patch_attempt_clients(client, [
        _AttemptClient(error_lines),
        _AttemptClient(_stream_lines(["should not appear"])),
    ])

    chunks = []
    with patch('time.sleep'), _patch_timeouts(client, 30, 10):
        with pytest.raises(httpx.RemoteProtocolError):
            for chunk in client.chat_stream_yield(
                [{"role": "user", "content": "hi"}],
                max_retry_limit=3,
            ):
                chunks.append(chunk)

    # Only the partial content from first attempt should be yielded
    assert chunks == ["partial"]
    # Second attempt must NOT have been started
    assert client._make_attempt_client.call_count == 1


def test_abort_closes_active_attempt_client():
    """abort() closes the currently active attempt client."""
    client = _make_client()

    clients = _patch_attempt_clients(client, [
        _AttemptClient([], block=True),
    ])

    def _abort_after_delay():
        import time as _time
        _time.sleep(0.2)
        client.abort()

    abort_thread = threading.Thread(target=_abort_after_delay, daemon=True)
    abort_thread.start()

    with _patch_timeouts(client, 30, 30):
        with pytest.raises(RuntimeError, match="abort"):
            client.chat(
                [{"role": "user", "content": "hi"}],
                max_retry_limit=1,
                stream=True,
            )

    abort_thread.join(timeout=5)
    assert clients[0].close.called, "abort() should have closed the active attempt client"


# ---------------------------------------------------------------------------
# Provider routing preferences (Agent 2 evidence mode)
# ---------------------------------------------------------------------------


def test_payload_includes_provider_when_given():
    """provider=require_parameters must appear in the OpenRouter payload."""
    client = _make_client()
    client._client.post.return_value.json.return_value = {
        "id": "req-1",
        "usage": {"total_tokens": 10},
        "choices": [{"message": {"content": "{}"}}],
    }

    client.chat(
        [{"role": "user", "content": "hi"}],
        response_format={"type": "json_schema"},
        provider={"require_parameters": True},
        stream=False,
    )

    payload = client._client.post.call_args.kwargs["json"]
    assert payload["provider"] == {"require_parameters": True}
    assert payload["response_format"] == {"type": "json_schema"}


def test_payload_omits_provider_when_not_given():
    """No provider field is sent unless explicitly requested."""
    client = _make_client()
    client._client.post.return_value.json.return_value = {
        "id": "req-2",
        "usage": {"total_tokens": 10},
        "choices": [{"message": {"content": "hi"}}],
    }

    client.chat(
        [{"role": "user", "content": "hi"}],
        stream=False,
    )

    payload = client._client.post.call_args.kwargs["json"]
    assert "provider" not in payload


def test_routing_failure_422_is_clear_and_not_repaired():
    """OpenRouter 422 routing failure must raise a clear error and be logged, no output repair."""
    from httpx import Request, Response, HTTPStatusError

    client = _make_client()
    req = Request("POST", "http://test")
    resp = Response(422, json={"error": {"message": "No provider available"}}, request=req)
    client._client.post.side_effect = HTTPStatusError("routing failure", request=req, response=resp)

    with pytest.raises(RuntimeError, match="No provider available"):
        client.chat([{"role": "user", "content": "hi"}], max_retry_limit=1, stream=False)

    # Only one attempt; no repair/output fabrication.
    assert client._client.post.call_count == 1


# ---------------------------------------------------------------------------
# Tests: truncation metadata — finish_reason surfacing (Item 1)
# ---------------------------------------------------------------------------

def test_nonstream_finish_reason_length_marks_truncated():
    """Non-stream response with finish_reason=length → last_truncated=True, content unchanged."""
    client = _make_client()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "id": "req-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        "choices": [{"message": {"content": "partial output"}, "finish_reason": "length"}],
    })
    client._client.post = MagicMock(side_effect=[resp])

    result = client.chat(
        [{"role": "user", "content": "hi"}],
        max_retry_limit=1,
        stream=False,
    )

    assert result == "partial output"
    assert client.last_finish_reason == "length"
    assert client.last_truncated is True


def test_nonstream_finish_reason_stop_not_truncated():
    """Non-stream response with finish_reason=stop → last_truncated=False."""
    client = _make_client()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "id": "req-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        "choices": [{"message": {"content": "complete output"}, "finish_reason": "stop"}],
    })
    client._client.post = MagicMock(side_effect=[resp])

    result = client.chat(
        [{"role": "user", "content": "hi"}],
        max_retry_limit=1,
        stream=False,
    )

    assert result == "complete output"
    assert client.last_finish_reason == "stop"
    assert client.last_truncated is False


def test_nonstream_no_finish_reason_means_none():
    """Non-stream response without finish_reason field → last_finish_reason=None."""
    client = _make_client()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "id": "req-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        "choices": [{"message": {"content": "output"}}],
    })
    client._client.post = MagicMock(side_effect=[resp])

    client.chat(
        [{"role": "user", "content": "hi"}],
        max_retry_limit=1,
        stream=False,
    )

    assert client.last_finish_reason is None
    assert client.last_truncated is False


def test_stream_finish_reason_length_marks_truncated():
    """Stream response with finish_reason=length → last_truncated=True, content unchanged."""
    client = _make_client()
    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "streamed partial"}}]}),
        'data: ' + _json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}),
        'data: [DONE]',
    ]
    _patch_attempt_clients(client, [_AttemptClient(lines)])

    with _patch_timeouts(client, 30, 10):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=1,
            stream=True,
        )

    assert result == "streamed partial"
    assert client.last_finish_reason == "length"
    assert client.last_truncated is True


def test_stream_finish_reason_stop_not_truncated():
    """Stream response with finish_reason=stop → last_truncated=False."""
    client = _make_client()
    lines = [
        'data: ' + _json.dumps({"choices": [{"delta": {"content": "streamed complete"}}]}),
        'data: ' + _json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        'data: [DONE]',
    ]
    _patch_attempt_clients(client, [_AttemptClient(lines)])

    with _patch_timeouts(client, 30, 10):
        result = client.chat(
            [{"role": "user", "content": "hi"}],
            max_retry_limit=1,
            stream=True,
        )

    assert result == "streamed complete"
    assert client.last_finish_reason == "stop"
    assert client.last_truncated is False


def test_finish_reason_reset_between_calls():
    """_last_finish_reason is reset to None at the start of each chat() call — no stale leak."""
    client = _make_client()

    # First call: finish_reason=length
    truncated_resp = MagicMock()
    truncated_resp.raise_for_status = MagicMock()
    truncated_resp.json = MagicMock(return_value={
        "id": "req-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        "choices": [{"message": {"content": "truncated"}, "finish_reason": "length"}],
    })
    # Second call: no finish_reason field at all
    no_finish_resp = MagicMock()
    no_finish_resp.raise_for_status = MagicMock()
    no_finish_resp.json = MagicMock(return_value={
        "id": "req-2",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        "choices": [{"message": {"content": "no finish reason"}}],
    })
    client._client.post = MagicMock(side_effect=[truncated_resp, no_finish_resp])

    client.chat([{"role": "user", "content": "first"}], max_retry_limit=1, stream=False)
    assert client.last_finish_reason == "length"

    client.chat([{"role": "user", "content": "second"}], max_retry_limit=1, stream=False)
    assert client.last_finish_reason is None, "stale finish_reason leaked from previous call"
    assert client.last_truncated is False


def test_annotations_path_preserves_finish_reason():
    """return_annotations path also captures finish_reason."""
    client = _make_client()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "id": "req-1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        "choices": [{"message": {"content": "text", "annotations": []}, "finish_reason": "length"}],
    })
    client._client.post = MagicMock(side_effect=[resp])

    text, annotations = client.chat(
        [{"role": "user", "content": "hi"}],
        max_retry_limit=1,
        return_annotations=True,
    )

    assert text == "text"
    assert annotations == []
    assert client.last_finish_reason == "length"
    assert client.last_truncated is True
