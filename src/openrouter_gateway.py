"""Single canonical OpenRouter access gate.

This module is the ONLY active code in the repository that:
- Reads OPENROUTER_API_KEY for paid provider traffic
- Creates authenticated OpenRouter httpx.Client instances
- Executes authenticated OpenRouter HTTP requests
- Owns the AI Usage Hub accounting helper

All paid OpenRouter operations (chat, embeddings, images, video) flow
through this gate's operation functions.  No other module may read the
key, create an authenticated client, or perform a paid OpenRouter
request directly.

Public consumers:
    LLMClient        — chat, chat_stream_yield, chat_with_tools
    media_gen        — generate_image, generate_video, get_model_capabilities
    generate_embedding — canonical embedding seam
    web_viewer       — /api/credits

Each consumer calls a gate operation function which:
1. creates an authenticated client internally
2. executes the provider HTTP request
3. performs accounting (for chat/embeddings — automatic; for image/video
   the caller calls account() after post-response logic like save/download)
4. returns a normalized result to the caller

The caller NEVER receives an authenticated client or transport object.
``make_client`` is intentionally NOT exported — it is a private internal
helper (``_make_client``).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

try:
    from .ai_usage import record_ai_usage, make_entry
except ImportError:
    from ai_usage import record_ai_usage, make_entry  # type: ignore

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def get_api_key() -> str | None:
    """The ONLY function that reads OPENROUTER_API_KEY for paid provider access."""
    return os.environ.get("OPENROUTER_API_KEY")


# ---------------------------------------------------------------------------
# Private client factory — NOT exported to callers
# ---------------------------------------------------------------------------

def _make_client(
    *,
    timeout: float | httpx.Timeout = 120,
    base_url: str | None = None,
    api_key: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Client:
    """Create an authenticated OpenRouter httpx.Client (internal only).

    This is the ONLY function that creates an authenticated OpenRouter client.
    If api_key is None, reads from environment (the gate owns key access).
    If api_key is provided (testing / backward compat), uses it directly.
    """
    key = api_key if api_key is not None else get_api_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not found — ตั้งใน .env")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    # base_url="" means no base_url (for downloads to arbitrary CDN URLs).
    # base_url=None means use the default OpenRouter base.
    effective_base = base_url if base_url is not None else OPENROUTER_BASE
    return httpx.Client(
        base_url=effective_base.rstrip("/") if effective_base else "",
        headers=headers,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Accounting — fire-and-forget, Hub failure is non-fatal
# ---------------------------------------------------------------------------

def account(
    *,
    model: str,
    source: str,
    operation: str,
    usage: dict[str, Any] | None = None,
    duration_ms: int = 0,
    attempt: int = 1,
    status: str = "success",
    http_status: int | None = None,
    error_message: str | None = None,
    request_id: str | None = None,
    finish_reason: str | None = None,
    truncated: bool | None = None,
    units: dict[str, Any] | None = None,
    cost_usd: float | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
) -> None:
    """Log a usage event to the AI Usage Hub — fire-and-forget.

    This is the canonical accounting helper.  All paid OpenRouter operations
    must log through this function so accounting is centralized and cannot
    be bypassed.  Hub failure is non-fatal (swallowed) so the main provider
    operation is never broken by logging.
    """
    try:
        entry = make_entry(
            provider="openrouter",
            model=model,
            operation=operation,
            source=source,
            request_id=request_id,
            duration_ms=duration_ms,
            attempt=attempt,
            status=status,
            http_status=http_status,
            error_message=error_message,
            raw_usage=usage,
            finish_reason=finish_reason,
            truncated=truncated,
            units=units,
        )
        if usage:
            if usage.get("prompt_tokens") is not None:
                entry["prompt_tokens"] = usage.get("prompt_tokens")
            if usage.get("completion_tokens") is not None:
                entry["completion_tokens"] = usage.get("completion_tokens")
            if usage.get("total_tokens") is not None:
                entry["total_tokens"] = usage.get("total_tokens")
            cost = usage.get("cost") or usage.get("total_cost")
            if cost is not None:
                entry["cost_usd"] = float(cost)
        # Explicit overrides for callers that compute cost separately
        if cost_usd is not None:
            entry["cost_usd"] = cost_usd
        if prompt_tokens is not None:
            entry["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            entry["completion_tokens"] = completion_tokens
        if total_tokens is not None:
            entry["total_tokens"] = total_tokens
        record_ai_usage(entry)
    except Exception:
        pass  # fire-and-forget — Hub failure must not break the caller


# ---------------------------------------------------------------------------
# Helpers for guard-audit extraction
# ---------------------------------------------------------------------------

def _extract_audit(client: httpx.Client) -> tuple[dict, Any]:
    """Extract guard audit data stored on an httpx.Client by M6 guards."""
    audit = getattr(client, "_m6_last_audit", None) or {}
    raw = getattr(client, "_m6_last_raw", None)
    return audit, raw


def _attach_audit(exc: Exception, client: httpx.Client) -> None:
    """Attach guard audit to an exception so callers can read it after catch."""
    audit, raw = _extract_audit(client)
    exc._m6_audit = audit  # type: ignore[attr-defined]
    exc._m6_raw = raw  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Chat — non-stream (POST /chat/completions)
# ---------------------------------------------------------------------------

@dataclass
class ChatPostResult:
    """Result of a non-stream chat POST. Accounting already done by the gate."""
    data: dict[str, Any]
    response: httpx.Response
    audit: dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    @property
    def usage(self) -> dict[str, Any] | None:
        return self.data.get("usage") if self.data else None

    @property
    def request_id(self) -> str | None:
        return self.data.get("id") if self.data else None


def chat_post(
    payload: dict[str, Any],
    *,
    timeout: float = 120,
    source: str = "llm_client.chat",
    model: str = "unknown",
    attempt: int = 1,
    request_id: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ChatPostResult:
    """Execute POST /chat/completions with automatic accounting.

    Returns ChatPostResult (accounting already logged).
    On HTTP/timeout error, logs error accounting and re-raises
    (with _m6_audit / _m6_raw attached to the exception for guard consumers).
    """
    t0 = time.time()
    client = _make_client(timeout=timeout, api_key=api_key, base_url=base_url)
    try:
        resp = client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage")
        choices = data.get("choices") or []
        finish_reason = choices[0].get("finish_reason") if choices else None
        account(
            model=model, source=source, operation="chat.completions",
            usage=usage, duration_ms=int((time.time() - t0) * 1000),
            attempt=attempt, request_id=request_id or data.get("id"),
            finish_reason=finish_reason,
        )
        audit, raw = _extract_audit(client)
        return ChatPostResult(data=data, response=resp, audit=audit, raw=raw)
    except httpx.HTTPStatusError as e:
        # Try to extract request_id from error body
        err_request_id = request_id
        err_message = str(e)[:200]
        try:
            body = e.response.json()
            if err_request_id is None:
                err_request_id = body.get("id")
            provider_error = body.get("error", {})
            detail = provider_error.get("message", "") if isinstance(provider_error, dict) else str(provider_error)
            if detail:
                err_message = f"OpenRouter {e.response.status_code}: {detail}"[:200]
        except Exception:
            pass
        account(
            model=model, source=source, operation="chat.completions",
            duration_ms=int((time.time() - t0) * 1000), attempt=attempt,
            status="error", http_status=e.response.status_code,
            error_message=err_message, request_id=err_request_id,
        )
        _attach_audit(e, client)
        raise
    except httpx.TimeoutException as e:
        account(
            model=model, source=source, operation="chat.completions",
            duration_ms=int((time.time() - t0) * 1000), attempt=attempt,
            status="timeout", error_message=str(e)[:200], request_id=request_id,
        )
        _attach_audit(e, client)
        raise
    except Exception as e:
        account(
            model=model, source=source, operation="chat.completions",
            duration_ms=int((time.time() - t0) * 1000), attempt=attempt,
            status="error", error_message=str(e)[:200], request_id=request_id,
        )
        _attach_audit(e, client)
        raise
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Chat — stream (POST /chat/completions with stream=True)
# ---------------------------------------------------------------------------

class ChatStreamHandle:
    """Handle for a streaming chat request. The gate owns the authenticated client.

    Usage:
        with gate.chat_stream_open(payload, ...) as handle:
            for line in handle.iter_lines():
                # parse SSE, set handle.usage / handle.finish_reason / handle.actual_request_id
            # handle.close_transport() can be called by a watchdog to interrupt
        # accounting done automatically on __exit__

    The caller NEVER receives the authenticated httpx.Client — only this
    handle which exposes line iteration and a transport-close method.
    """

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        timeout: float = 120,
        source: str = "llm_client.chat_stream",
        model: str = "unknown",
        attempt: int = 1,
        request_id: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self._payload = payload
        self._timeout = timeout
        self._source = source
        self._model = model
        self._attempt = attempt
        self._request_id = request_id
        self._api_key = api_key
        self._base_url = base_url
        self._client: httpx.Client | None = None
        self._stream_cm: Any = None
        self._response: httpx.Response | None = None
        self._t0 = 0.0
        self._closed = False
        # Set by caller during SSE parsing
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None
        self.actual_request_id: str | None = None
        # Set by __exit__ for caller to read
        self.audit: dict[str, Any] = {}
        self.raw: Any = None

    def __enter__(self) -> "ChatStreamHandle":
        self._t0 = time.time()
        self._client = _make_client(
            timeout=self._timeout, api_key=self._api_key, base_url=self._base_url,
        )
        self._stream_cm = self._client.stream("POST", "/chat/completions", json=self._payload)
        self._response = self._stream_cm.__enter__()
        return self

    @property
    def response(self) -> httpx.Response | None:
        return self._response

    def iter_lines(self) -> Iterator[str]:
        if self._response is None:
            return iter(())
        return self._response.iter_lines()

    def close_transport(self) -> None:
        """Close the underlying transport to interrupt blocked reads (watchdog)."""
        if self._client and not self._closed:
            try:
                self._client.close()
            except Exception:
                pass

    def close(self) -> None:
        """Alias for close_transport — for _register_attempt compatibility."""
        self.close_transport()

    def _do_accounting(self, exc_type: type | None, exc_val: Exception | None) -> None:
        duration_ms = int((time.time() - self._t0) * 1000)
        rid = self.actual_request_id or self._request_id
        if exc_type is None:
            account(
                model=self._model, source=self._source, operation="chat.completions",
                usage=self.usage, duration_ms=duration_ms, attempt=self._attempt,
                request_id=rid, finish_reason=self.finish_reason,
            )
        else:
            status = "timeout" if issubclass(exc_type, httpx.TimeoutException) else "error"
            http_status = None
            error_message = str(exc_val)[:200] if exc_val else None
            if issubclass(exc_type, httpx.HTTPStatusError) and hasattr(exc_val, "response"):
                http_status = exc_val.response.status_code
            account(
                model=self._model, source=self._source, operation="chat.completions",
                duration_ms=duration_ms, attempt=self._attempt, status=status,
                http_status=http_status, error_message=error_message, request_id=rid,
            )

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self._closed = True
        # Close the stream context manager
        try:
            if self._stream_cm:
                self._stream_cm.__exit__(exc_type, exc_val, exc_tb)
        except Exception:
            pass
        # Extract guard audit before closing client
        if self._client:
            self.audit, self.raw = _extract_audit(self._client)
        # Accounting
        self._do_accounting(exc_type, exc_val)
        # Close client
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None  # Clear reference — no authenticated client escapes
        return False


def chat_stream_open(
    payload: dict[str, Any],
    *,
    timeout: float = 120,
    source: str = "llm_client.chat_stream",
    model: str = "unknown",
    attempt: int = 1,
    request_id: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ChatStreamHandle:
    """Create a ChatStreamHandle for streaming chat (caller uses `with`).

    The gate owns the authenticated client.  Accounting is done automatically
    when the handle's context exits (success or error).
    """
    return ChatStreamHandle(
        payload, timeout=timeout, source=source, model=model,
        attempt=attempt, request_id=request_id, api_key=api_key, base_url=base_url,
    )


# ---------------------------------------------------------------------------
# Chat — OpenAI SDK (chat.completions.create for tool calling)
# ---------------------------------------------------------------------------

def chat_completions_create(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    timeout: float = 120,
    source: str = "llm_client.chat_with_tools",
    attempt: int = 1,
    request_id: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> Any:
    """Execute chat.completions.create via OpenAI SDK with automatic accounting.

    Returns the OpenAI SDK response object.  On error, logs error accounting
    and re-raises.
    """
    from openai import OpenAI

    t0 = time.time()
    key = api_key if api_key is not None else get_api_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not found — ตั้งใน .env")
    sdk_base = base_url if base_url is not None else OPENROUTER_BASE
    client = OpenAI(base_url=sdk_base, api_key=key, timeout=timeout)
    try:
        kwargs: dict[str, Any] = dict(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
        response = client.chat.completions.create(**kwargs)
        usage = response.usage.model_dump() if response.usage else None
        finish_reason = None
        if response.choices:
            finish_reason = getattr(response.choices[0], "finish_reason", None)
        account(
            model=model, source=source, operation="chat.completions",
            usage=usage, duration_ms=int((time.time() - t0) * 1000),
            attempt=attempt,
            request_id=request_id or getattr(response, "id", None),
            finish_reason=finish_reason,
        )
        return response
    except httpx.TimeoutException as e:
        account(
            model=model, source=source, operation="chat.completions",
            duration_ms=int((time.time() - t0) * 1000), attempt=attempt,
            status="timeout", error_message=str(e)[:200], request_id=request_id,
        )
        raise
    except Exception as e:
        status = "timeout" if "timeout" in type(e).__name__.lower() else "error"
        http_status = getattr(e, "status_code", None)
        account(
            model=model, source=source, operation="chat.completions",
            duration_ms=int((time.time() - t0) * 1000), attempt=attempt,
            status=status, http_status=http_status,
            error_message=str(e)[:200], request_id=request_id,
        )
        raise
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Embeddings (POST /embeddings)
# ---------------------------------------------------------------------------

def embeddings_post(
    payload: dict[str, Any],
    *,
    timeout: float = 30,
    source: str = "embedding.generate",
    model: str = "openai/text-embedding-3-small",
    api_key: str | None = None,
) -> dict[str, Any]:
    """Execute POST /embeddings with automatic accounting.

    Returns parsed response data.  Raises on error (with accounting done).
    """
    t0 = time.time()
    client = _make_client(timeout=timeout, api_key=api_key)
    try:
        resp = client.post("/embeddings", json=payload)
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage")
        account(
            model=model, source=source, operation="embeddings.create",
            usage=usage, duration_ms=int((time.time() - t0) * 1000),
            request_id=data.get("id"),
        )
        return data
    except httpx.HTTPStatusError as e:
        account(
            model=model, source=source, operation="embeddings.create",
            duration_ms=int((time.time() - t0) * 1000), status="error",
            http_status=e.response.status_code, error_message=str(e)[:200],
        )
        raise
    except httpx.TimeoutException as e:
        account(
            model=model, source=source, operation="embeddings.create",
            duration_ms=int((time.time() - t0) * 1000), status="timeout",
            error_message=str(e)[:200],
        )
        raise
    except Exception as e:
        account(
            model=model, source=source, operation="embeddings.create",
            duration_ms=int((time.time() - t0) * 1000), status="error",
            error_message=str(e)[:200],
        )
        raise
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Image (POST /images) — gateway owns accounting lifecycle
# ---------------------------------------------------------------------------

def image_post(
    payload: dict[str, Any],
    *,
    timeout: float | httpx.Timeout = 180,
    source: str = "media_gen.generate_image",
    model: str = "unknown",
    attempt: int = 1,
    units: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Execute POST /images with automatic accounting.

    Returns parsed response data.  Raises on error (with accounting done).
    The gateway owns the accounting lifecycle — callers must NOT separately
    account the image provider request.
    """
    t0 = time.time()
    client = _make_client(timeout=timeout, api_key=api_key)
    try:
        resp = client.post(f"{OPENROUTER_BASE}/images", json=payload)
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage")
        account(
            model=model, source=source, operation="images.generate",
            usage=usage, duration_ms=int((time.time() - t0) * 1000),
            attempt=attempt, status="success",
            request_id=data.get("id"), units=units,
        )
        return data
    except httpx.HTTPStatusError as e:
        account(
            model=model, source=source, operation="images.generate",
            duration_ms=int((time.time() - t0) * 1000), attempt=attempt,
            status="error", http_status=e.response.status_code,
            error_message=str(e)[:200], units=units,
        )
        raise
    except httpx.TimeoutException as e:
        account(
            model=model, source=source, operation="images.generate",
            duration_ms=int((time.time() - t0) * 1000), attempt=attempt,
            status="timeout", error_message=str(e)[:200], units=units,
        )
        raise
    except Exception as e:
        account(
            model=model, source=source, operation="images.generate",
            duration_ms=int((time.time() - t0) * 1000), attempt=attempt,
            status="error", error_message=str(e)[:200], units=units,
        )
        raise
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Video — async lifecycle owned by the gateway
#
# The public operation is video_generate(), which owns the complete paid
# lifecycle: submit → poll until terminal → obtain actual provider usage/cost
# → accounting → return normalized result.
#
# Low-level submit/poll/download helpers are PRIVATE (_video_submit,
# _video_poll, _video_download) so no caller can initiate a paid video
# generation and omit the accounting lifecycle.
# ---------------------------------------------------------------------------

@dataclass
class VideoGenerateResult:
    """Normalized result of a video generation lifecycle.

    The gateway has already accounted the terminal provider cost by the time
    this is returned.  Callers must NOT separately account.
    """
    ok: bool
    job_id: str | None
    status: str  # "completed" | "failed" | "timeout" | "error"
    usage: dict[str, Any] | None
    urls: list[str]
    error: str | None
    http_status: int | None
    accounted: bool = False


def video_generate(
    payload: dict[str, Any],
    *,
    model: str = "unknown",
    source: str = "media_gen.generate_video",
    poll_interval: float = 5.0,
    max_wait: float = 600.0,
    poll_timeout: float = 60.0,
    submit_timeout: float = 60.0,
    attempt: int = 1,
    units: dict[str, Any] | None = None,
    on_status: Any = None,
    api_key: str | None = None,
) -> VideoGenerateResult:
    """Execute the complete paid video generation lifecycle:

        submit → poll until terminal → obtain actual provider usage/cost
        → accounting → return normalized result.

    The gateway owns the full lifecycle.  Callers cannot initiate a paid
    video generation and omit the accounting — there is no public
    submit/poll/download API.

    Accounting represents provider generation cost (from the terminal poll
    response), NOT artifact download success.  Download is the caller's
    responsibility (via video_download_artifact) and does not affect
    accounting.

    on_status: optional callback(status_str) called with progress updates.
    """
    t0 = time.time()
    job_id: str | None = None

    def _account(usage, status, error_message=None, http_status=None, request_id=None):
        account(
            model=model, source=source, operation="videos.generate",
            usage=usage, duration_ms=int((time.time() - t0) * 1000),
            attempt=attempt, status=status, http_status=http_status,
            error_message=error_message, request_id=request_id or job_id,
            units=units,
        )

    try:
        if on_status:
            on_status("submitting")
        submit_data = _video_submit(
            payload, timeout=submit_timeout, api_key=api_key,
        )
        job_id = submit_data.get("id")
        polling_url = submit_data.get("polling_url")
        if not job_id or not polling_url:
            _account(None, "error", error_message="no job_id or polling_url")
            return VideoGenerateResult(
                ok=False, job_id=job_id, status="error", usage=None,
                urls=[], error=f"API did not return job_id/polling_url: {submit_data}",
                http_status=None, accounted=True,
            )

        # Make polling URL absolute (OpenRouter may return relative path)
        if not polling_url.startswith("http"):
            polling_url = "https://openrouter.ai" + polling_url

        # Poll until terminal
        poll_start = time.time()
        while (time.time() - poll_start) < max_wait:
            if on_status:
                on_status(f"generating ({int(time.time() - poll_start)}s)")
            time.sleep(poll_interval)

            status_data = _video_poll(polling_url, timeout=poll_timeout, api_key=api_key)
            status = status_data.get("status", "")

            if status == "completed":
                urls = status_data.get("unsigned_urls") or status_data.get("urls") or []
                usage = status_data.get("usage")
                if not urls:
                    _account(usage, "error", error_message="completed but no url")
                    return VideoGenerateResult(
                        ok=False, job_id=job_id, status="error", usage=usage,
                        urls=[], error="completed but no url", http_status=None,
                        accounted=True,
                    )
                # Terminal success — account the actual provider cost
                _account(usage, "success")
                return VideoGenerateResult(
                    ok=True, job_id=job_id, status="completed", usage=usage,
                    urls=urls, error=None, http_status=None, accounted=True,
                )

            if status == "failed":
                err = status_data.get("error", "unknown")
                usage = status_data.get("usage")
                _account(usage, "error", error_message=str(err))
                return VideoGenerateResult(
                    ok=False, job_id=job_id, status="failed", usage=usage,
                    urls=[], error=f"video gen failed: {err}", http_status=None,
                    accounted=True,
                )
            # else: still processing — keep polling

        # Timeout — account as timeout
        _account(None, "timeout", error_message=f"timeout after {max_wait}s")
        return VideoGenerateResult(
            ok=False, job_id=job_id, status="timeout", usage=None,
            urls=[], error=f"timeout after {max_wait}s", http_status=None,
            accounted=True,
        )

    except httpx.HTTPStatusError as e:
        _account(None, "error", error_message=str(e),
                 http_status=e.response.status_code)
        return VideoGenerateResult(
            ok=False, job_id=job_id, status="error", usage=None,
            urls=[], error=str(e), http_status=e.response.status_code,
            accounted=True,
        )
    except Exception as e:
        _account(None, "error", error_message=str(e))
        return VideoGenerateResult(
            ok=False, job_id=job_id, status="error", usage=None,
            urls=[], error=str(e), http_status=None, accounted=True,
        )


def video_download_artifact(
    url: str,
    *,
    timeout: float = 120,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    api_key: str | None = None,
) -> bytes:
    """Download a video artifact (CDN URL) with retries.

    This is a post-accounting artifact retrieval — it does NOT account
    because provider generation cost is already accounted by video_generate.
    Download success/failure does not affect the cost event.
    """
    return _video_download(
        url, timeout=timeout, max_retries=max_retries,
        retry_delay=retry_delay, api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Private low-level video helpers — NOT public API
# ---------------------------------------------------------------------------

def _video_submit(
    payload: dict[str, Any],
    *,
    timeout: float = 60,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Execute POST /videos. Returns parsed response data."""
    client = _make_client(timeout=timeout, api_key=api_key)
    try:
        resp = client.post(f"{OPENROUTER_BASE}/videos", json=payload)
        resp.raise_for_status()
        return resp.json()
    finally:
        client.close()


def _video_poll(
    poll_url: str,
    *,
    timeout: float = 60,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Execute GET poll_url. Returns parsed response data."""
    client = _make_client(timeout=timeout, api_key=api_key)
    try:
        resp = client.get(poll_url)
        resp.raise_for_status()
        return resp.json()
    finally:
        client.close()


def _video_download(
    url: str,
    *,
    timeout: float = 120,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    api_key: str | None = None,
) -> bytes:
    """Download a video artifact with retries. Returns video bytes."""
    last_error: str | None = None
    for dl_attempt in range(max_retries):
        try:
            client = _make_client(timeout=timeout, base_url="", api_key=api_key)
            try:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.content
            finally:
                client.close()
        except Exception as e:
            last_error = str(e)
            if dl_attempt < max_retries - 1:
                time.sleep(retry_delay)
    raise RuntimeError(f"download failed after {max_retries} retries: {last_error}")


# ---------------------------------------------------------------------------
# Capability discovery (GET /videos/models or /images/models)
# ---------------------------------------------------------------------------

def capability_get(
    endpoint: str,
    *,
    timeout: float = 30,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Execute GET for capability discovery (videos/models or images/models).

    Returns parsed response data.  Raises on error (caller catches and
    returns empty dict — capability discovery is non-critical).
    """
    client = _make_client(timeout=timeout, api_key=api_key)
    try:
        resp = client.get(endpoint)
        resp.raise_for_status()
        return resp.json()
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Key info (GET /key — for credits display, not a paid operation)
# ---------------------------------------------------------------------------

def key_get(
    *,
    timeout: float = 10,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Execute GET /key for credits/usage info. Returns parsed data dict.

    This is NOT a paid operation — it reads account info.  No accounting.
    """
    client = _make_client(timeout=timeout, api_key=api_key)
    try:
        resp = client.get("/key")
        resp.raise_for_status()
        return resp.json().get("data", {})
    finally:
        client.close()
