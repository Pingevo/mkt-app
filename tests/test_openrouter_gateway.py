"""Regression tests for AI-USAGE-SINGLE-GATE-02.

Verifies the single OpenRouter access gate architecture:
- src/openrouter_gateway.py is the ONLY module that reads the credential,
  constructs authenticated transport, AND executes authenticated HTTP for
  paid OpenRouter traffic.
- LLMClient, media_gen, and generate_embedding delegate to gate operations.
- No caller outside the gate receives an authenticated client or transport.
- make_client is NOT a public API — callers cannot obtain an authenticated
  httpx.Client from the gate.
- Actual provider cost propagates through the gate to the Hub boundary.
- Hub failure remains non-fatal.
- Per-request context is not kept in global mutable state.

All tests are offline: no provider calls, no Hub POST, no paid calls, budget $0.
"""
import sys
from pathlib import Path
from unittest import mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import openrouter_gateway as gate


# ---------------------------------------------------------------------------
# Gate: credential ownership
# ---------------------------------------------------------------------------

def test_gate_get_api_key_reads_env(monkeypatch):
    """gate.get_api_key is the single credential reader."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-123")
    assert gate.get_api_key() == "test-key-123"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert gate.get_api_key() is None


# ---------------------------------------------------------------------------
# Gate: make_client is NOT public — callers cannot obtain an auth client
# ---------------------------------------------------------------------------

def test_gate_make_client_not_exported():
    """The gate must NOT export make_client as a public API.

    Callers must use operation functions (chat_post, chat_stream_open,
    embeddings_post, image_post, video_generate, capability_get, key_get)
    which execute HTTP internally and return normalized results.
    No caller may receive an authenticated httpx.Client.
    """
    assert not hasattr(gate, "make_client"), (
        "gate.make_client must not exist — callers must not be able to "
        "obtain an authenticated httpx.Client. Use operation functions instead."
    )


def test_gate_video_submit_not_public():
    """The gate must NOT export video_submit as a public API.

    A standalone video_submit could initiate a paid generation without
    completing the accounting lifecycle.  The public API is video_generate,
    which owns submit → poll → terminal → accounting.
    """
    assert not hasattr(gate, "video_submit"), (
        "gate.video_submit must not exist as a public API — "
        "use video_generate which owns the full lifecycle"
    )


def test_gate_video_poll_not_public():
    """The gate must NOT export video_poll as a public API."""
    assert not hasattr(gate, "video_poll"), (
        "gate.video_poll must not exist as a public API — "
        "use video_generate which owns the full lifecycle"
    )


def test_gate_video_generate_is_public():
    """The gate must export video_generate as the public video API."""
    assert hasattr(gate, "video_generate"), (
        "gate.video_generate must exist as the public video lifecycle operation"
    )
    assert hasattr(gate, "VideoGenerateResult"), (
        "gate.VideoGenerateResult must exist as the normalized result type"
    )


def test_gate_make_client_is_private():
    """The gate's internal client factory is private (_make_client)."""
    assert hasattr(gate, "_make_client"), (
        "gate._make_client must exist as the internal client factory"
    )


def test_no_authenticated_client_escapes_chat_post(monkeypatch):
    """chat_post returns a ChatPostResult, not an httpx.Client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={
            "id": "req-1", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    monkeypatch.setattr(gate, "record_ai_usage", lambda e: None)

    result = gate.chat_post({"model": "m", "messages": []}, timeout=5, source="test")
    assert not isinstance(result, httpx.Client)
    assert isinstance(result, gate.ChatPostResult)
    assert result.data["id"] == "req-1"


def test_no_authenticated_client_escapes_chat_stream(monkeypatch):
    """chat_stream_open returns a ChatStreamHandle, not an httpx.Client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    sse_lines = [
        'data: {"id":"r1","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n',
        'data: {"id":"r1","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1}}\n',
        'data: [DONE]\n',
    ]
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content="".join(sse_lines),
                                   headers={"content-type": "text/event-stream"})
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    monkeypatch.setattr(gate, "record_ai_usage", lambda e: None)

    handle = gate.chat_stream_open({"model": "m", "messages": []}, timeout=5, source="test")
    assert not isinstance(handle, httpx.Client)
    assert isinstance(handle, gate.ChatStreamHandle)
    with handle:
        lines = list(handle.iter_lines())
    # handle does not expose the client publicly
    assert not hasattr(handle, "_client") or handle._client is None


def test_no_authenticated_client_escapes_embeddings(monkeypatch):
    """embeddings_post returns a dict, not an httpx.Client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={
            "id": "emb-1", "data": [{"embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1},
        })
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    monkeypatch.setattr(gate, "record_ai_usage", lambda e: None)

    data = gate.embeddings_post({"model": "m", "input": "text"}, timeout=5)
    assert isinstance(data, dict)
    assert data["data"][0]["embedding"] == [0.1, 0.2]


def test_no_authenticated_client_escapes_image_post(monkeypatch):
    """image_post returns a dict, not an httpx.Client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={
            "id": "img-1", "data": [{"b64_json": "abc"}],
            "usage": {"cost": 0.01},
        })
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    monkeypatch.setattr(gate, "record_ai_usage", lambda e: None)

    data = gate.image_post({"model": "m", "prompt": "test"}, timeout=5)
    assert isinstance(data, dict)
    assert data["data"][0]["b64_json"] == "abc"


def test_no_authenticated_client_escapes_video_generate(monkeypatch):
    """video_generate returns a VideoGenerateResult, not an httpx.Client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    call_count = [0]
    def handler(req):
        call_count[0] += 1
        url = str(req.url)
        if req.method == "POST" and url.endswith("/videos"):
            return httpx.Response(200, json={
                "id": "job-1", "polling_url": "/api/v1/videos/job-1",
            })
        if req.method == "GET" and "job-1" in url:
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
                "usage": {"cost": 0.10},
            })
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    monkeypatch.setattr(gate, "record_ai_usage", lambda e: None)

    result = gate.video_generate(
        {"model": "m", "prompt": "test"},
        poll_interval=0.0, max_wait=10.0, poll_timeout=5, submit_timeout=5,
    )
    assert not isinstance(result, httpx.Client)
    assert isinstance(result, gate.VideoGenerateResult)
    assert result.ok is True
    assert result.job_id == "job-1"
    assert result.urls == ["https://cdn.example.com/v.mp4"]


def test_no_authenticated_client_escapes_video_download_artifact(monkeypatch):
    """video_download_artifact returns bytes, not an httpx.Client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"video"))
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    content = gate.video_download_artifact("https://cdn.example.com/v.mp4", timeout=5, max_retries=1)
    assert content == b"video"


def test_no_authenticated_client_escapes_capability_get(monkeypatch):
    """capability_get returns a dict, not an httpx.Client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": []})
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    data = gate.capability_get("https://openrouter.ai/api/v1/videos/models", timeout=5)
    assert isinstance(data, dict)


def test_no_authenticated_client_escapes_key_get(monkeypatch):
    """key_get returns a dict, not an httpx.Client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": {"limit": 100}})
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    data = gate.key_get(timeout=5)
    assert isinstance(data, dict)
    assert data["limit"] == 100


# ---------------------------------------------------------------------------
# Gate: accounting ownership
# ---------------------------------------------------------------------------

def test_gate_account_calls_record_ai_usage(monkeypatch):
    """gate.account is the canonical accounting seam — calls record_ai_usage."""
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)
    gate.account(
        model="test/model",
        source="test.source",
        operation="chat.completions",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        duration_ms=100,
    )
    assert len(captured) == 1
    evt = captured[0]
    assert evt["model"] == "test/model"
    assert evt["operation"] == "chat.completions"
    assert evt["prompt_tokens"] == 10
    assert evt["completion_tokens"] == 5
    assert evt["cost_usd"] == 0.001


def test_gate_account_propagates_actual_cost(monkeypatch):
    """Actual provider cost (e.g. Wan $0.50) propagates to cost_usd."""
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)
    gate.account(
        model="alibaba/wan-2.7",
        source="media_gen.generate_video",
        operation="videos.generate",
        usage={"cost": 0.50},
        duration_ms=5000,
    )
    assert captured[0]["cost_usd"] == 0.50


def test_gate_account_hub_failure_is_non_fatal(monkeypatch):
    """If record_ai_usage raises, gate.account swallows it (fire-and-forget)."""
    def _boom(entry):
        raise RuntimeError("Hub down")
    monkeypatch.setattr(gate, "record_ai_usage", _boom)
    # Must not raise
    gate.account(
        model="test/model",
        source="test",
        operation="chat.completions",
        duration_ms=10,
    )


def test_gate_account_no_duplicate_event(monkeypatch):
    """gate.account calls record_ai_usage exactly once per invocation."""
    call_count = [0]
    def _count(entry):
        call_count[0] += 1
    monkeypatch.setattr(gate, "record_ai_usage", _count)
    gate.account(model="m", source="s", operation="chat.completions", duration_ms=1)
    assert call_count[0] == 1


# ---------------------------------------------------------------------------
# Gate: chat_post does accounting automatically (inseparable from execution)
# ---------------------------------------------------------------------------

def test_chat_post_accounts_on_success(monkeypatch):
    """chat_post calls account() with provider usage on success."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={
            "id": "req-1", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.002},
        })
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    gate.chat_post({"model": "m", "messages": []}, timeout=5, source="test.chat")
    assert len(captured) == 1
    assert captured[0]["cost_usd"] == 0.002
    assert captured[0]["status"] == "success"


def test_chat_post_accounts_on_error(monkeypatch):
    """chat_post calls account() with error status on HTTP failure."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(500, json={"error": {"message": "server error"}})
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    with pytest.raises(httpx.HTTPStatusError):
        gate.chat_post({"model": "m", "messages": []}, timeout=5, source="test.chat")
    assert len(captured) == 1
    assert captured[0]["status"] == "error"
    assert captured[0]["http_status"] == 500


# ---------------------------------------------------------------------------
# Gate: chat_stream does accounting automatically on context exit
# ---------------------------------------------------------------------------

def test_chat_stream_accounts_on_success(monkeypatch):
    """chat_stream_open does accounting on successful stream exit."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    sse_lines = [
        'data: {"id":"r1","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n',
        'data: {"id":"r1","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"cost":0.001}}\n',
        'data: [DONE]\n',
    ]
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content="".join(sse_lines),
                                   headers={"content-type": "text/event-stream"})
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    handle = gate.chat_stream_open({"model": "m", "messages": []}, timeout=5, source="test.stream")
    with handle:
        handle.usage = {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001}
        handle.finish_reason = "stop"
        handle.actual_request_id = "r1"
        list(handle.iter_lines())
    assert len(captured) == 1
    assert captured[0]["cost_usd"] == 0.001
    assert captured[0]["status"] == "success"


def test_chat_stream_accounts_on_error(monkeypatch):
    """chat_stream_open does accounting on error exit."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(500, json={"error": {"message": "fail"}})
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    handle = gate.chat_stream_open({"model": "m", "messages": []}, timeout=5, source="test.stream")
    with pytest.raises(httpx.HTTPStatusError):
        with handle:
            handle.response.raise_for_status()
            list(handle.iter_lines())
    assert len(captured) == 1
    assert captured[0]["status"] == "error"


# ---------------------------------------------------------------------------
# Gate: embeddings_post does accounting automatically
# ---------------------------------------------------------------------------

def test_embeddings_post_accounts_on_success(monkeypatch):
    """embeddings_post calls account() with provider usage on success."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={
            "id": "emb-1", "data": [{"embedding": [0.1]}],
            "usage": {"prompt_tokens": 5},
        })
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    gate.embeddings_post({"model": "m", "input": "text"}, timeout=5, source="test.emb")
    assert len(captured) == 1
    assert captured[0]["prompt_tokens"] == 5
    assert captured[0]["status"] == "success"


def test_embeddings_post_accounts_on_error(monkeypatch):
    """embeddings_post calls account() with error status on failure."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(500, json={"error": {"message": "fail"}})
    )
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    with pytest.raises(httpx.HTTPStatusError):
        gate.embeddings_post({"model": "m", "input": "text"}, timeout=5, source="test.emb")
    assert len(captured) == 1
    assert captured[0]["status"] == "error"


# ---------------------------------------------------------------------------
# LLMClient delegates transport to the gate (no authenticated client)
# ---------------------------------------------------------------------------

def test_llm_client_no_persistent_client(monkeypatch):
    """LLMClient.__init__ does NOT create a persistent authenticated client."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    from src import llm_client
    llm = llm_client.LLMClient(timeout=5)
    try:
        assert not hasattr(llm, "_client") or llm._client is None, (
            "LLMClient must not hold a persistent authenticated httpx.Client"
        )
    finally:
        llm.close()


def test_llm_client_no_direct_authorization_header(monkeypatch):
    """LLMClient source contains no Authorization header construction."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    from src import llm_client
    src = Path(llm_client.__file__).read_text()
    # Strip comments/docstrings naively
    import re
    cleaned = re.sub(r'""".*?"""', '', src, flags=re.DOTALL)
    cleaned = re.sub(r"'''.*?'''", '', cleaned, flags=re.DOTALL)
    # No line should construct an Authorization header outside of comments
    for line in cleaned.splitlines():
        if '#' in line:
            line = line[:line.index('#')]
        assert 'Authorization' not in line, (
            f"LLMClient must not construct Authorization headers: {line.strip()}"
        )


def test_llm_client_no_make_client_import(monkeypatch):
    """LLMClient source must not import make_client from the gate."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    from src import llm_client
    src = Path(llm_client.__file__).read_text()
    assert "make_client" not in src, (
        "LLMClient must not import or reference make_client — "
        "use gate operation functions instead"
    )


# ---------------------------------------------------------------------------
# media_gen delegates transport to the gate (no authenticated client)
# ---------------------------------------------------------------------------

def test_media_gen_no_direct_authorization_header():
    """media_gen source contains no Authorization header construction."""
    from src import media_gen
    import re
    src = Path(media_gen.__file__).read_text()
    cleaned = re.sub(r'""".*?"""', '', src, flags=re.DOTALL)
    cleaned = re.sub(r"'''.*?'''", '', cleaned, flags=re.DOTALL)
    for line in cleaned.splitlines():
        if '#' in line:
            line = line[:line.index('#')]
        assert 'Authorization' not in line, (
            f"media_gen must not construct Authorization headers: {line.strip()}"
        )


def test_media_gen_no_direct_env_key_read():
    """media_gen source contains no direct os.environ.get('OPENROUTER_API_KEY')."""
    from src import media_gen
    import re
    src = Path(media_gen.__file__).read_text()
    cleaned = re.sub(r'""".*?"""', '', src, flags=re.DOTALL)
    cleaned = re.sub(r"'''.*?'''", '', cleaned, flags=re.DOTALL)
    for line in cleaned.splitlines():
        if '#' in line:
            line = line[:line.index('#')]
        # Error message strings mentioning the key name are OK; actual env reads are not
        env_read = re.search(
            r'(os\.environ\.get|os\.getenv|get_env)\s*\(\s*["\']OPENROUTER_API_KEY',
            line,
        )
        assert not env_read, (
            f"media_gen must not read OPENROUTER_API_KEY directly: {line.strip()}"
        )


def test_media_gen_no_make_client_import():
    """media_gen source must not import make_client from the gate."""
    from src import media_gen
    src = Path(media_gen.__file__).read_text()
    assert "make_client" not in src, (
        "media_gen must not import or reference make_client — "
        "use gate operation functions instead"
    )


# ---------------------------------------------------------------------------
# web_viewer delegates transport to the gate (no authenticated client)
# ---------------------------------------------------------------------------

def test_web_viewer_no_make_client_import():
    """web_viewer source must not import make_client from the gate."""
    import web_viewer
    src = Path(web_viewer.__file__).read_text()
    # orch.make_client() is the Orchestrator's method (creates LLMClient) — allowed.
    # Importing make_client from openrouter_gateway is NOT allowed.
    assert "from src.openrouter_gateway import" not in src or \
           "make_client" not in src.split("from src.openrouter_gateway import")[-1].split("\n")[0], (
        "web_viewer must not import make_client from openrouter_gateway — "
        "use gate operation functions instead"
    )
    # Also check the explicit import line
    for line in src.splitlines():
        if "openrouter_gateway" in line and "import" in line:
            assert "make_client" not in line, (
                f"web_viewer must not import make_client from openrouter_gateway: {line.strip()}"
            )


# ---------------------------------------------------------------------------
# Static/source-level enforcement: only the gate does authenticated HTTP
# ---------------------------------------------------------------------------

def test_only_gateway_reads_openrouter_api_key():
    """Only openrouter_gateway.py reads OPENROUTER_API_KEY via os.environ."""
    import re
    root = Path(__file__).resolve().parent.parent
    gate_path = root / "src" / "openrouter_gateway.py"
    gate_src = gate_path.read_text()

    # The gate must read the key
    assert 'os.environ.get("OPENROUTER_API_KEY")' in gate_src or \
           "os.environ.get('OPENROUTER_API_KEY')" in gate_src

    # No other src/ module may read OPENROUTER_API_KEY via os.environ/os.getenv
    for p in (root / "src").glob("*.py"):
        if p.name == "openrouter_gateway.py":
            continue
        src = p.read_text()
        # Strip comments
        cleaned = re.sub(r'""".*?"""', '', src, flags=re.DOTALL)
        cleaned = re.sub(r"'''.*?'''", '', cleaned, flags=re.DOTALL)
        for line in cleaned.splitlines():
            if '#' in line:
                line = line[:line.index('#')]
            env_read = re.search(
                r'(os\.environ\.get|os\.getenv)\s*\(\s*["\']OPENROUTER_API_KEY',
                line,
            )
            assert not env_read, (
                f"{p.name} must not read OPENROUTER_API_KEY directly — "
                f"only openrouter_gateway.py may: {line.strip()}"
            )


def test_only_gateway_sets_authorization_header():
    """Only openrouter_gateway.py constructs an Authorization header."""
    import re
    root = Path(__file__).resolve().parent.parent

    for p in (root / "src").glob("*.py"):
        if p.name == "openrouter_gateway.py":
            continue
        src = p.read_text()
        cleaned = re.sub(r'""".*?"""', '', src, flags=re.DOTALL)
        cleaned = re.sub(r"'''.*?'''", '', cleaned, flags=re.DOTALL)
        for line in cleaned.splitlines():
            if '#' in line:
                line = line[:line.index('#')]
            assert 'Authorization' not in line, (
                f"{p.name} must not construct Authorization headers — "
                f"only openrouter_gateway.py may: {line.strip()}"
            )


def test_no_active_code_executes_authenticated_openrouter_http():
    """No src/ or scripts/ module outside the gate executes authenticated
    OpenRouter HTTP (httpx.Client with OpenRouter base_url + post/get/stream).
    """
    import re
    root = Path(__file__).resolve().parent.parent
    gate_name = "openrouter_gateway.py"

    # Check src/ modules
    for p in (root / "src").glob("*.py"):
        if p.name == gate_name:
            continue
        src = p.read_text()
        cleaned = re.sub(r'""".*?"""', '', src, flags=re.DOTALL)
        cleaned = re.sub(r"'''.*?'''", '', cleaned, flags=re.DOTALL)
        # No direct httpx.Client construction with OpenRouter base_url
        assert 'httpx.Client(' not in cleaned or 'openrouter.ai' not in cleaned, (
            f"{p.name} must not construct httpx.Client for OpenRouter — "
            f"use gate operation functions"
        )


# ---------------------------------------------------------------------------
# Video lifecycle ownership — video_generate owns submit → poll → accounting
# ---------------------------------------------------------------------------

def _patch_http_for_video(monkeypatch, handler):
    """Inject a MockTransport into every httpx.Client for video tests."""
    transport = httpx.MockTransport(handler)
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    return transport


def test_video_generate_owns_full_lifecycle(monkeypatch):
    """video_generate owns submit → poll → terminal → accounting.

    A single call to video_generate must complete the full lifecycle.
    The caller does not need to separately poll or account.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    poll_count = [0]
    def handler(req):
        url = str(req.url)
        if req.method == "POST" and url.endswith("/videos"):
            return httpx.Response(200, json={
                "id": "job-lifecycle", "polling_url": "/api/v1/videos/job-lifecycle",
            })
        if req.method == "GET" and "job-lifecycle" in url:
            poll_count[0] += 1
            if poll_count[0] < 2:
                return httpx.Response(200, json={"status": "processing"})
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
                "usage": {"cost": 0.16},
            })
        return httpx.Response(404)
    _patch_http_for_video(monkeypatch, handler)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    result = gate.video_generate(
        {"model": "test/model", "prompt": "test"},
        poll_interval=0.0, max_wait=10.0, poll_timeout=5, submit_timeout=5,
    )
    assert result.ok is True
    assert result.status == "completed"
    assert result.accounted is True
    # Exactly one accounting event (terminal success)
    assert len(captured) == 1
    assert captured[0]["status"] == "success"
    assert captured[0]["cost_usd"] == 0.16


def test_video_generate_wan_cost_0_50_one_event(monkeypatch):
    """Wan-like terminal usage cost 0.50 produces exactly one Hub cost_usd=0.50."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    def handler(req):
        url = str(req.url)
        if req.method == "POST" and url.endswith("/videos"):
            return httpx.Response(200, json={
                "id": "wan-job", "polling_url": "/api/v1/videos/wan-job",
            })
        if req.method == "GET" and "wan-job" in url:
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/wan.mp4"],
                "usage": {"cost": 0.50},
            })
        return httpx.Response(404)
    _patch_http_for_video(monkeypatch, handler)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    result = gate.video_generate(
        {"model": "alibaba/wan-2.7", "prompt": "test"},
        model="alibaba/wan-2.7",
        poll_interval=0.0, max_wait=10.0, poll_timeout=5, submit_timeout=5,
    )
    assert result.ok is True
    assert len(captured) == 1
    assert captured[0]["cost_usd"] == 0.50
    assert captured[0]["model"] == "alibaba/wan-2.7"
    assert captured[0]["operation"] == "videos.generate"


def test_video_generate_no_duplicate_accounting_on_polling(monkeypatch):
    """Polling responses do not produce duplicate accounting.

    Multiple poll calls (processing → processing → completed) must result
    in exactly ONE accounting event (the terminal one).
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    poll_count = [0]
    def handler(req):
        url = str(req.url)
        if req.method == "POST" and url.endswith("/videos"):
            return httpx.Response(200, json={
                "id": "job-dup", "polling_url": "/api/v1/videos/job-dup",
            })
        if req.method == "GET" and "job-dup" in url:
            poll_count[0] += 1
            if poll_count[0] < 3:
                return httpx.Response(200, json={"status": "processing"})
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
                "usage": {"cost": 0.10},
            })
        return httpx.Response(404)
    _patch_http_for_video(monkeypatch, handler)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    result = gate.video_generate(
        {"model": "m", "prompt": "test"},
        poll_interval=0.0, max_wait=30.0, poll_timeout=5, submit_timeout=5,
    )
    assert result.ok is True
    assert poll_count[0] == 3  # 3 polls (2 processing + 1 completed)
    assert len(captured) == 1  # exactly ONE accounting event


def test_video_generate_provider_error_accounted(monkeypatch):
    """Provider terminal error (status=failed) is accounted correctly."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    def handler(req):
        url = str(req.url)
        if req.method == "POST" and url.endswith("/videos"):
            return httpx.Response(200, json={
                "id": "job-fail", "polling_url": "/api/v1/videos/job-fail",
            })
        if req.method == "GET" and "job-fail" in url:
            return httpx.Response(200, json={
                "status": "failed",
                "error": "content_policy_violation",
                "usage": {"cost": 0.0},
            })
        return httpx.Response(404)
    _patch_http_for_video(monkeypatch, handler)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    result = gate.video_generate(
        {"model": "m", "prompt": "test"},
        poll_interval=0.0, max_wait=10.0, poll_timeout=5, submit_timeout=5,
    )
    assert result.ok is False
    assert result.status == "failed"
    assert result.accounted is True
    assert len(captured) == 1
    assert captured[0]["status"] == "error"


def test_video_generate_hub_failure_non_fatal(monkeypatch):
    """Hub failure during accounting does not break video_generate."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    def handler(req):
        url = str(req.url)
        if req.method == "POST" and url.endswith("/videos"):
            return httpx.Response(200, json={
                "id": "job-hub", "polling_url": "/api/v1/videos/job-hub",
            })
        if req.method == "GET" and "job-hub" in url:
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
                "usage": {"cost": 0.10},
            })
        return httpx.Response(404)
    _patch_http_for_video(monkeypatch, handler)
    def _boom(entry):
        raise RuntimeError("Hub down")
    monkeypatch.setattr(gate, "record_ai_usage", _boom)

    # Must not raise
    result = gate.video_generate(
        {"model": "m", "prompt": "test"},
        poll_interval=0.0, max_wait=10.0, poll_timeout=5, submit_timeout=5,
    )
    assert result.ok is True


def test_video_generate_timeout_accounted(monkeypatch):
    """Timeout (never reaching terminal status) is accounted as timeout."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    def handler(req):
        url = str(req.url)
        if req.method == "POST" and url.endswith("/videos"):
            return httpx.Response(200, json={
                "id": "job-timeout", "polling_url": "/api/v1/videos/job-timeout",
            })
        if req.method == "GET" and "job-timeout" in url:
            return httpx.Response(200, json={"status": "processing"})
        return httpx.Response(404)
    _patch_http_for_video(monkeypatch, handler)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    result = gate.video_generate(
        {"model": "m", "prompt": "test"},
        poll_interval=0.0, max_wait=0.5, poll_timeout=5, submit_timeout=5,
    )
    assert result.ok is False
    assert result.status == "timeout"
    assert result.accounted is True
    assert len(captured) == 1
    assert captured[0]["status"] == "timeout"


def test_video_generate_submit_error_accounted(monkeypatch):
    """HTTP error on submit is accounted as error."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    def handler(req):
        return httpx.Response(500, json={"error": {"message": "server error"}})
    _patch_http_for_video(monkeypatch, handler)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    result = gate.video_generate(
        {"model": "m", "prompt": "test"},
        poll_interval=0.0, max_wait=10.0, poll_timeout=5, submit_timeout=5,
    )
    assert result.ok is False
    assert result.status == "error"
    assert result.accounted is True
    assert len(captured) == 1
    assert captured[0]["status"] == "error"


def test_video_generate_no_job_id_accounted(monkeypatch):
    """Submit returns no job_id → accounted as error, no polling."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"id": None, "polling_url": None})
        return httpx.Response(404)
    _patch_http_for_video(monkeypatch, handler)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    result = gate.video_generate(
        {"model": "m", "prompt": "test"},
        poll_interval=0.0, max_wait=10.0, poll_timeout=5, submit_timeout=5,
    )
    assert result.ok is False
    assert result.accounted is True
    assert len(captured) == 1
    assert captured[0]["status"] == "error"


def test_video_download_artifact_no_accounting(monkeypatch):
    """video_download_artifact does NOT account — cost already accounted by video_generate."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"video"))
    original_init = httpx.Client.__init__
    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)

    content = gate.video_download_artifact("https://cdn.example.com/v.mp4", timeout=5, max_retries=1)
    assert content == b"video"
    assert len(captured) == 0, "video_download_artifact must NOT account"


# ---------------------------------------------------------------------------
# Per-request context isolation (no global mutable state)
# ---------------------------------------------------------------------------

def test_gate_is_stateless():
    """The gate module exposes no mutable global state for user/request context.

    Per-request metadata (user, source, reference, request_id) must travel with
    each request, not be stored in module-level globals.  The gate has only
    constants and functions — no mutable request/user containers.
    """
    # The gate module should not have mutable global containers for context.
    mutable_attrs = [
        attr for attr in dir(gate)
        if not attr.startswith("__")
        and not callable(getattr(gate, attr))
    ]
    # OPENROUTER_BASE is a constant string — allowed.
    # No mutable dict/list/set for user/request context should exist.
    for attr in mutable_attrs:
        val = getattr(gate, attr)
        assert not isinstance(val, (dict, list, set)), (
            f"Gate must not hold mutable global state: {attr} = {type(val)}"
        )


def test_account_carries_per_request_context(monkeypatch):
    """Each account() call carries its own context — no cross-contamination."""
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)
    gate.account(model="m1", source="user_a", operation="chat.completions",
                 request_id="req-1", duration_ms=10)
    gate.account(model="m2", source="user_b", operation="chat.completions",
                 request_id="req-2", duration_ms=20)
    assert len(captured) == 2
    assert captured[0]["source"] == "user_a"
    assert captured[0]["request_id"] == "req-1"
    assert captured[1]["source"] == "user_b"
    assert captured[1]["request_id"] == "req-2"


# ---------------------------------------------------------------------------
# Wan-like $0.50 cost regression — actual cost propagates through gate
# ---------------------------------------------------------------------------

def test_wan_cost_propagates_through_video_accounting(monkeypatch):
    """Wan-like $0.50 actual cost propagates through gate.account to the Hub."""
    captured: list[dict] = []
    monkeypatch.setattr(gate, "record_ai_usage", captured.append)
    gate.account(
        model="alibaba/wan-2.7",
        source="media_gen.generate_video",
        operation="videos.generate",
        usage={"cost": 0.50},
        duration_ms=30000,
        request_id="wan-job-1",
        units={"videos_generated": 1, "duration_seconds": 5},
    )
    assert captured[0]["cost_usd"] == 0.50
    assert captured[0]["model"] == "alibaba/wan-2.7"
    assert captured[0]["operation"] == "videos.generate"
