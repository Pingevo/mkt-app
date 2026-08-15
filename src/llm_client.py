"""OpenRouter API client — shared LLM backend for all agents."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from rich.console import Console
from rich.live import Live
from rich.text import Text

try:
    from .ai_usage import log_ai_usage, make_entry
except ImportError:
    from ai_usage import log_ai_usage, make_entry  # type: ignore

console = Console()


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
        source: str = "llm_client.chat",
    ) -> str:
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

        source: label สำหรับ AI Usage Hub log (เช่น "content_creator", "ingestion")
        """
        used_model = model or self._default_model
        # Structured Outputs ไม่รองรับ stream — บังคับ non-stream
        if response_format:
            stream = False
        payload: dict[str, Any] = {
            "model": used_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if stream:
            # เปิด include_usage เพื่อให้ chunk สุดท้ายมี usage ส่งกลับมา
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
        if plugins:
            payload["plugins"] = plugins
        if response_format:
            payload["response_format"] = response_format

        last_error: Exception | None = None
        for attempt in range(1, max_retry_limit + 1):
            t0 = time.time()
            try:
                if stream:
                    text, usage = self._chat_stream(payload)
                    self._log_usage(used_model, source, usage, duration_ms=int((time.time() - t0) * 1000), attempt=attempt)
                    return text
                else:
                    resp = self._client.post("/chat/completions", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    usage = data.get("usage")
                    self._log_usage(used_model, source, usage, duration_ms=int((time.time() - t0) * 1000), attempt=attempt)
                    return data["choices"][0]["message"]["content"]
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                # log error path ด้วย
                self._log_usage(used_model, source, None, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, status="error", error_message=str(exc))
                if self._aborted:
                    raise RuntimeError("Request aborted")
                if attempt < max_retry_limit:
                    wait = 2**attempt
                    time.sleep(wait)
                continue

        raise RuntimeError(f"LLM request failed after {max_retry_limit} retries: {last_error}")

    @staticmethod
    def _log_usage(
        model: str,
        source: str,
        usage: dict[str, Any] | None,
        *,
        duration_ms: int,
        attempt: int = 1,
        status: str = "success",
        error_message: str | None = None,
    ) -> None:
        """ยิง log ไป AI Usage Hub — fire-and-forget.

        ห้ามให้ error ใน logging ทำลาย LLM call หลัก — wrap ด้วย try/except
        """
        try:
            entry = make_entry(
                provider="openrouter",
                model=model,
                operation="chat.completions",
                source=source,
                duration_ms=duration_ms,
                status=status,
                error_message=error_message,
            )
            if usage:
                entry["prompt_tokens"] = usage.get("prompt_tokens")
                entry["completion_tokens"] = usage.get("completion_tokens")
                cost = usage.get("cost")
                if cost is not None:
                    entry["cost_usd"] = float(cost)
                entry["raw_usage"] = usage
            log_ai_usage(entry)
        except Exception:
            pass  # fire-and-forget — ไม่ให้ logging error ทำลาย main flow

    def _chat_stream(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Stream chat completion and display real-time output.

        คืน (text, usage) — usage มาจาก chunk สุดท้ายเมื่อเปิด stream_options.include_usage
        """
        import json

        collected: list[str] = []
        text = Text()
        usage: dict[str, Any] | None = None

        with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            with Live(text, console=console, refresh_per_second=int(_system_cfg().get("stream_refresh_rate", 15)), transient=False) as live:
                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        # chunk สุดท้ายมี usage อยู่ระดับ top-level (ไม่ใช่ใน choices)
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            collected.append(content)
                            text.append(content)
                            live.update(text)
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue

        console.print()
        return "".join(collected), usage

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
        import json

        used_model = model or self._default_model
        payload: dict[str, Any] = {
            "model": used_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
        if plugins:
            payload["plugins"] = plugins

        last_error: Exception | None = None
        for attempt in range(1, max_retry_limit + 1):
            t0 = time.time()
            try:
                usage: dict[str, Any] | None = None
                with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            if chunk.get("usage"):
                                usage = chunk["usage"]
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue
                self._log_usage(used_model, source, usage, duration_ms=int((time.time() - t0) * 1000), attempt=attempt)
                return
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                self._log_usage(used_model, source, None, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, status="error", error_message=str(exc))
                if attempt < max_retry_limit:
                    time.sleep(2**attempt)
                continue

        raise RuntimeError(f"LLM request failed after {max_retry_limit} retries: {last_error}")

    def close(self) -> None:
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
                self._log_usage(used_model, source, usage.model_dump() if usage else None,
                                duration_ms=int((time.time() - t0) * 1000), attempt=iteration + 1)

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
                self._log_usage(used_model, source, None,
                                duration_ms=int((time.time() - t0) * 1000),
                                attempt=iteration + 1, status="error", error_message=str(exc))
                if iteration < max_retry_limit:
                    time.sleep(2 ** (iteration + 1))
                    continue
                raise RuntimeError(f"tool calling failed: {last_error}")

        return "ครบจำนวนรอบสูงสุดแล้ว แต่ LLM ยังไม่ตอบจบ"

    def abort(self) -> None:
        """Force-close the connection, aborting any in-flight request."""
        self._aborted = True
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
