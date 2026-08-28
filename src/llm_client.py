"""OpenRouter API client — shared LLM backend for all agents."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from rich.console import Console
from rich.live import Live
from rich.text import Text

try:
    from .ai_usage import record_ai_usage, make_entry
except ImportError:
    from ai_usage import record_ai_usage, make_entry  # type: ignore

console = Console()


class _EmptyResponseError(Exception):
    """Internal signal: OpenRouter returned HTTP 200 but content was empty.

    Treated like a transient failure — retried up to ``max_retry_limit``
    using the same config value as HTTP/timeout retries. No new status enum
    is added; logged as ``status="error"`` with the message as-is.
    """


@dataclass
class _WatchdogState:
    """Shared state between main thread and watchdog thread for one stream attempt.

    ใช้ dataclass แทน list/dict ที่ผ่าน closure เพื่อให้ชัดเจนว่า field ไหนเป็นอะไร
    และกันการ access ผิด index/key — timeout_reason เขียนจาก watchdog, อ่านจาก main
    last_progress เขียนจาก main, อ่านจาก watchdog
    """
    timeout_reason: str | None = None
    last_progress: float = field(default_factory=time.monotonic)


def _system_cfg() -> dict:
    """อ่าน system section จาก config — fallback {} ถ้าโหลดไม่ได้ (lazy, กัน circular import)."""
    try:
        from .config_loader import load_config, get_section
        return get_section(load_config(), "system", {})
    except Exception:
        return {}


class LLMClient:
    """Thin wrapper around OpenRouter chat completions API with streaming."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        default_model: str = "anthropic/claude-sonnet-4",
        timeout: float = 120,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout = timeout
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        self._aborted = False
        self._attempt_lock = threading.Lock()
        self._active_attempts: list[Any] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        max_retry_limit: int = 3,
        stream: bool = True,
        tools: list[dict[str, Any]] | None = None,
        plugins: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        provider: dict[str, Any] | None = None,
        source: str = "llm_client.chat",
        return_annotations: bool = False,
    ) -> str | tuple[str, list[dict[str, Any]]]:
        """Send a chat completion request and return the assistant's text reply.

        If stream=True, displays real-time output as the LLM generates.
        Retries on transient errors (5xx, timeouts) up to ``max_retry_limit`` times
        with exponential backoff.

        If tools is provided (e.g. [{"type": "openrouter:web_search"}]),
        enables OpenRouter server tools — model controls when/how often to search.
        If plugins is provided (e.g. [{"id": "web"}]), enables OpenRouter plugins
        (auto-search once per request).

        If response_format is provided (e.g. {"type": "json_schema", "json_schema": {...}}),
        enables OpenRouter Structured Outputs — model returns JSON conforming to schema.
        Note: when response_format is set, stream is forced to False (OpenRouter limitation).

        If return_annotations=True, returns (text, annotations) where annotations
        is a list of {url, title, content} dicts extracted from OpenRouter
        url_citation annotations. Used by web search to get real source URLs.
        Note: return_annotations forces stream=False (annotations only available
        in non-stream responses).

        source: label สำหรับ AI Usage Hub log (เช่น "content_creator", "ingestion")
        """
        used_model = model or self._default_model
        # Structured Outputs ไม่รองรับ stream — บังคับ non-stream
        if response_format:
            stream = False
        # annotations มีเฉพาะ non-stream response — บังคับ non-stream
        if return_annotations:
            stream = False
        payload: dict[str, Any] = {
            "model": used_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if stream:
            # stream_options.include_usage ถูก OpenRouter deprecate แล้ว — usage ส่งอัตโนมัติ
            pass
        if tools:
            payload["tools"] = tools
        if plugins:
            payload["plugins"] = plugins
        if response_format:
            payload["response_format"] = response_format
        if provider:
            payload["provider"] = provider

        last_error: Exception | None = None
        last_error_message: str | None = None
        # max_retry_limit = number of attempts; ensure at least one try
        for attempt in range(1, max(1, max_retry_limit) + 1):
            t0 = time.time()
            try:
                if stream:
                    text, usage, request_id = self._chat_stream(payload)
                    self._log_usage(used_model, source, usage, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, request_id=request_id)
                else:
                    resp = self._client.post("/chat/completions", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    self._last_raw_response = data
                    request_id = data.get("id")
                    usage = data.get("usage")
                    self._log_usage(used_model, source, usage, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, request_id=request_id)
                    msg = data["choices"][0]["message"]
                    text = msg.get("content", "")
                    self._last_raw_annotations_count = len(msg.get("annotations") or [])
                    if return_annotations:
                        annotations = self._extract_url_annotations(msg.get("annotations", []))
                        self._last_annotations_count = len(annotations)
                        # Empty content = failed attempt — retry like transient errors
                        if not (text and text.strip()) and not annotations:
                            raise _EmptyResponseError("empty response (no content and no annotations)")
                        return text, annotations
                    # Empty content = failed attempt — retry like transient errors
                    if not (text and text.strip()):
                        raise _EmptyResponseError("empty response (no content)")
                    return text

                # Stream path: empty content = failed attempt — retry
                if not (text and text.strip()):
                    raise _EmptyResponseError("empty response (no content from stream)")
                return text

            except (_EmptyResponseError, httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                # log error path ด้วย — empty response ใช้ status "error" เดิม + message บอกสาเหตุ
                if isinstance(exc, _EmptyResponseError):
                    status = "error"
                    http_status = None
                    request_id = None
                    error_message = str(exc)
                else:
                    status = "timeout" if isinstance(exc, httpx.TimeoutException) else "error"
                    http_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    request_id = None
                    error_message = str(exc)
                    if isinstance(exc, httpx.HTTPStatusError):
                        try:
                            body = exc.response.json()
                            request_id = body.get("id")
                            provider_error = body.get("error", {})
                            if isinstance(provider_error, dict):
                                detail = provider_error.get("message", "")
                            else:
                                detail = str(provider_error)
                            if detail:
                                error_message = f"OpenRouter {http_status}: {detail}"
                                last_error_message = error_message
                        except Exception:
                            pass
                self._log_usage(used_model, source, None, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, status=status, http_status=http_status, error_message=error_message, request_id=request_id)
                if self._aborted:
                    raise RuntimeError("Request aborted")
                if attempt < max_retry_limit:
                    wait = 2**attempt
                    time.sleep(wait)
                continue

        raise RuntimeError(f"LLM request failed after {max_retry_limit} retries: {last_error_message or last_error}")

    @staticmethod
    def _extract_url_annotations(raw_annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """แปลง OpenRouter url_citation annotations เป็น list ของ {url, title, content}.

        OpenRouter ส่ง annotations ในรูปแบบ:
            {"type": "url_citation", "url_citation": {"url": "...", "title": "...", "content": "..."}}
        คืน list ของ dict ที่ flat แล้ว เรียงตามลำดับที่ปรากฏ
        """
        out: list[dict[str, Any]] = []
        for ann in raw_annotations or []:
            if not isinstance(ann, dict):
                continue
            # รองรับทั้ง url_citation (OpenRouter) และรูปแบบ flat
            citation = ann.get("url_citation") or ann
            if not isinstance(citation, dict):
                continue
            url = citation.get("url")
            if not url:
                continue
            out.append({
                "url": url,
                "title": citation.get("title", ""),
                "content": citation.get("content", ""),
            })
        return out

    @staticmethod
    def _log_usage(
        model: str,
        source: str,
        usage: dict[str, Any] | None,
        *,
        duration_ms: int,
        attempt: int = 1,
        status: str = "success",
        http_status: int | None = None,
        error_message: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """ยิง log ไป AI Usage Hub + เซฟ local — fire-and-forget.

        ห้ามให้ error ใน logging ทำลาย LLM call หลัก — wrap ด้วย try/except
        """
        try:
            entry = make_entry(
                provider="openrouter",
                model=model,
                operation="chat.completions",
                source=source,
                request_id=request_id,
                duration_ms=duration_ms,
                attempt=attempt,
                status=status,
                http_status=http_status,
                error_message=error_message,
                raw_usage=usage,
            )
            if usage:
                if usage.get("prompt_tokens") is not None:
                    entry["prompt_tokens"] = usage.get("prompt_tokens")
                if usage.get("completion_tokens") is not None:
                    entry["completion_tokens"] = usage.get("completion_tokens")
                cost = usage.get("cost")
                if cost is not None:
                    entry["cost_usd"] = float(cost)
            record_ai_usage(entry)
        except Exception:
            pass  # fire-and-forget — ไม่ให้ logging error ทำลาย main flow

    def _make_attempt_client(self) -> httpx.Client:
        """Create a per-attempt httpx client with same config as the main client.

        แยก client ต่อ attempt เพื่อให้ watchdog ปิด socket ของ attempt นี้ได้โดยไม่ทำลาย
        client ของ retry ถัดไปหรือ parallel flow อื่น — ค่าใช้จ่ายคือเปิด connection ใหม่
        ต่อ streaming attempt ซึ่งยอมรับได้สำหรับจำนวน request ของโปรเจกต์
        """
        return httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )

    def _register_attempt(self, attempt_client: Any) -> None:
        with self._attempt_lock:
            self._active_attempts.append(attempt_client)

    def _unregister_attempt(self, attempt_client: Any) -> None:
        with self._attempt_lock:
            try:
                self._active_attempts.remove(attempt_client)
            except ValueError:
                pass

    def _stream_attempt(
        self, payload: dict[str, Any], *, attempt_timeout: float, progress_timeout: float,
    ):
        """หนึ่ง stream attempt พร้อม watchdog ที่ interrupt blocked read ได้จริง.

        สร้าง httpx.Client แยกสำหรับ attempt นี้ แล้วปล่อย watchdog daemon thread คอยนับเวลา
        สองแบบ: total attempt deadline และ no-model-progress deadline เมื่อถึง deadline
        watchdog ปิด attempt client เพื่อ interrupt socket read ที่ block อยู่ใน iter_lines()

        ค่า "model progress" รีเซ็ตเฉพาะเมื่อได้รับ content/reasoning/tool-call delta จริง
        ไม่รีเซ็ตจาก SSE comment (: OPENROUTER PROCESSING) หรือ usage-only chunk

        Yields (event_type, value) tuples:
            ("content", str) — content delta จาก model
            ("usage", dict) — usage info จาก chunk สุดท้าย
            ("request_id", str) — request ID
            ("finish", str) — finish_reason ที่ไม่ใช่ error

        Raises:
            httpx.ReadTimeout — watchdog ปิด connection เพราะ total หรือ progress deadline
            httpx.RemoteProtocolError — mid-stream error event หรือ dropped connection
        """
        attempt_client = self._make_attempt_client()
        self._register_attempt(attempt_client)

        done_event = threading.Event()
        state = _WatchdogState()

        def watchdog() -> None:
            deadline_total = time.monotonic() + attempt_timeout
            poll = min(0.1, attempt_timeout / 10, progress_timeout / 10)
            while not done_event.wait(poll):
                now = time.monotonic()
                if now > deadline_total:
                    state.timeout_reason = "total"
                    break
                if now - state.last_progress > progress_timeout:
                    state.timeout_reason = "progress"
                    break
            if state.timeout_reason:
                try:
                    attempt_client.close()
                except Exception:
                    pass
            done_event.set()

        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()

        try:
            with attempt_client.stream("POST", "/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                saw_done = False
                saw_finish_reason = False

                for line in resp.iter_lines():
                    if not line:
                        continue
                    if not line.startswith("data: "):
                        continue  # skip SSE comments เช่น : OPENROUTER PROCESSING
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        saw_done = True
                        break

                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    # mid-stream error: HTTP 200 + SSE error event
                    if chunk.get("error"):
                        msg = chunk["error"].get("message", "unknown stream error")
                        raise httpx.RemoteProtocolError(f"OpenRouter stream error: {msg}")

                    if chunk.get("id"):
                        yield ("request_id", chunk["id"])

                    if chunk.get("usage"):
                        yield ("usage", chunk["usage"])

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue  # usage-only/debug chunk — ไม่มี IndexError

                    choice = choices[0]
                    finish_reason = choice.get("finish_reason")
                    if finish_reason:
                        saw_finish_reason = True
                        if finish_reason == "error":
                            raise httpx.RemoteProtocolError(
                                "stream terminated with finish_reason=error"
                            )
                        yield ("finish", finish_reason)

                    delta = choice.get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        state.last_progress = time.monotonic()
                        yield ("content", content)
                    elif delta.get("reasoning") or delta.get("tool_calls"):
                        state.last_progress = time.monotonic()

                # EOF โดยไม่มี [DONE] และไม่มี successful finish_reason = dropped stream
                if not saw_done and not saw_finish_reason:
                    raise httpx.RemoteProtocolError(
                        "stream ended without [DONE] or finish_reason"
                    )

        except httpx.ReadTimeout:
            reason = state.timeout_reason or "unknown"
            raise httpx.ReadTimeout(f"stream {reason} timeout") from None

        finally:
            done_event.set()
            try:
                attempt_client.close()
            except Exception:
                pass
            self._unregister_attempt(attempt_client)
            watchdog_thread.join(timeout=5)

    def _chat_stream(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str | None]:
        """Stream chat completion and display real-time output.

        คืน (text, usage, request_id) — usage มาจาก chunk สุดท้าย (OpenRouter ส่งอัตโนมัติ)
        request_id มาจาก chunk แรก/สุดท้ายที่ระบุ id
        """
        cfg = _system_cfg()
        attempt_timeout = float(cfg.get("stream_attempt_timeout_seconds", self._timeout))
        progress_timeout = float(cfg.get("stream_progress_timeout_seconds", 60))

        collected: list[str] = []
        text = Text()
        usage: dict[str, Any] | None = None
        request_id: str | None = None

        with Live(text, console=console, refresh_per_second=int(cfg.get("stream_refresh_rate", 15)), transient=False) as live:
            for event_type, value in self._stream_attempt(
                payload, attempt_timeout=attempt_timeout, progress_timeout=progress_timeout,
            ):
                if event_type == "content":
                    collected.append(value)
                    text.append(value)
                    live.update(text)
                elif event_type == "usage":
                    usage = value
                elif event_type == "request_id":
                    request_id = value

        console.print()
        return "".join(collected), usage, request_id

    def chat_stream_yield(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        max_retry_limit: int = 3,
        tools: list[dict[str, Any]] | None = None,
        plugins: list[dict[str, Any]] | None = None,
        source: str = "llm_client.chat_stream_yield",
    ):
        """Stream chat completion, yielding chunks. For web SSE.

        source: label สำหรับ AI Usage Hub log
        """
        used_model = model or self._default_model
        payload: dict[str, Any] = {
            "model": used_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            # stream_options.include_usage ถูก OpenRouter deprecate — usage ส่งอัตโนมัติ
        }
        if tools:
            payload["tools"] = tools
        if plugins:
            payload["plugins"] = plugins

        cfg = _system_cfg()
        attempt_timeout = float(cfg.get("stream_attempt_timeout_seconds", self._timeout))
        progress_timeout = float(cfg.get("stream_progress_timeout_seconds", 60))

        last_error: Exception | None = None
        # max_retry_limit = number of attempts; ensure at least one try
        for attempt in range(1, max(1, max_retry_limit) + 1):
            t0 = time.time()
            try:
                usage: dict[str, Any] | None = None
                request_id: str | None = None
                yielded_any = False
                for event_type, value in self._stream_attempt(
                    payload, attempt_timeout=attempt_timeout, progress_timeout=progress_timeout,
                ):
                    if event_type == "content":
                        yielded_any = True
                        yield value
                    elif event_type == "usage":
                        usage = value
                    elif event_type == "request_id":
                        request_id = value
                self._log_usage(used_model, source, usage, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, request_id=request_id)
                # Empty content = failed attempt — retry like chat() does
                # (can only retry if nothing was yielded yet)
                if not yielded_any:
                    raise _EmptyResponseError("empty response (no content from stream)")
                return
            except (_EmptyResponseError, httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                # หลัง yield content แล้ว = committed — ห้าม retry เพราะจะส่งข้อความซ้ำ
                if yielded_any:
                    status = "timeout" if isinstance(exc, httpx.TimeoutException) else "error"
                    self._log_usage(used_model, source, None, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, status=status, error_message=str(exc), request_id=request_id)
                    raise
                if isinstance(exc, _EmptyResponseError):
                    status = "error"
                    http_status = None
                    request_id = None
                else:
                    status = "timeout" if isinstance(exc, httpx.TimeoutException) else "error"
                    http_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    request_id = None
                    if isinstance(exc, httpx.HTTPStatusError):
                        try:
                            request_id = exc.response.json().get("id")
                        except Exception:
                            pass
                self._log_usage(used_model, source, None, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, status=status, http_status=http_status, error_message=str(exc), request_id=request_id)
                if self._aborted:
                    raise RuntimeError("Request aborted")
                if attempt < max_retry_limit:
                    time.sleep(2**attempt)
                continue

        raise RuntimeError(f"LLM request failed after {max_retry_limit} retries: {last_error}")

    def close(self) -> None:
        with self._attempt_lock:
            for ac in self._active_attempts:
                try:
                    ac.close()
                except Exception:
                    pass
            self._active_attempts.clear()
        self._client.close()

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Any],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        max_retry_limit: int = 3,
        max_iterations: int = 10,
        source: str = "llm_client.chat_with_tools",
    ) -> str:
        """Tool calling loop — LLM เรียก function เอง เรา execute แล้วส่งผลกลับ.

        ใช้ openai SDK (point ไป OpenRouter) เพราะมี tool calling parsing พร้อม.

        Args:
            messages: ประวัติการสนทนา (system + user)
            tools: tool definitions แบบ OpenAI schema
            tool_handlers: dict {tool_name: callable} — function จริงที่จะ execute
            max_iterations: จำกัดรอบ tool calling (กัน LLM วนไม่จบ)
            source: label สำหรับ log

        Returns:
            ข้อความตอบสุดท้ายของ LLM (หลังใช้ tool จนจบ)
        """
        from openai import OpenAI

        client = OpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._timeout,
        )
        used_model = model or self._default_model

        # copy messages เพื่อไม่แก้ของเดิม
        convo = list(messages)

        for iteration in range(max_iterations):
            t0 = time.time()
            try:
                response = client.chat.completions.create(
                    model=used_model,
                    messages=convo,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                msg = response.choices[0].message
                usage = response.usage
                request_id = getattr(response, "id", None)
                self._log_usage(used_model, source, usage.model_dump() if usage else None,
                                duration_ms=int((time.time() - t0) * 1000), attempt=iteration + 1, request_id=request_id)

                # ถ้า LLM ไม่ขอเรียก tool → ตอบจบ
                if not msg.tool_calls:
                    return msg.content or ""

                # append assistant message (มี tool_calls) เข้า conversation
                # ต้องแปลงเป็น dict เพราะ openai SDK ต้องการ dict ตอนส่งกลับ
                assistant_msg = {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in msg.tool_calls
                    ],
                }
                convo.append(assistant_msg)

                # execute ทุก tool call แล้วส่งผลกลับ
                for call in msg.tool_calls:
                    tool_name = call.function.name
                    handler = tool_handlers.get(tool_name)
                    if handler is None:
                        result = json.dumps({"error": f"unknown tool: {tool_name}"})
                    else:
                        try:
                            args = json.loads(call.function.arguments)
                            handler_result = handler(**args)
                            result = json.dumps(handler_result, ensure_ascii=False)
                        except Exception as e:
                            result = json.dumps({"error": str(e)}, ensure_ascii=False)
                    convo.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    })

            except Exception as exc:
                last_error = exc
                http_status = getattr(exc, "status_code", None)
                status = "timeout" if "timeout" in type(exc).__name__.lower() else "error"
                self._log_usage(used_model, source, None,
                                duration_ms=int((time.time() - t0) * 1000),
                                attempt=iteration + 1, status=status, http_status=http_status,
                                error_message=str(exc))
                if iteration < max_retry_limit:
                    time.sleep(2 ** (iteration + 1))
                    continue
                raise RuntimeError(f"tool calling failed: {last_error}")

        return "ครบจำนวนรอบสูงสุดแล้ว แต่ LLM ยังไม่ตอบจบ"

    def abort(self) -> None:
        """Force-close the connection, aborting any in-flight request."""
        self._aborted = True
        with self._attempt_lock:
            for ac in self._active_attempts:
                try:
                    ac.close()
                except Exception:
                    pass
            self._active_attempts.clear()
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
