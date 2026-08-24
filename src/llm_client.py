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
    from .ai_usage import record_ai_usage, make_entry
except ImportError:
    from ai_usage import record_ai_usage, make_entry  # type: ignore

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
                    text, usage, request_id = self._chat_stream(payload)
                    self._log_usage(used_model, source, usage, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, request_id=request_id)
                    return text
                else:
                    resp = self._client.post("/chat/completions", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    request_id = data.get("id")
                    usage = data.get("usage")
                    self._log_usage(used_model, source, usage, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, request_id=request_id)
                    msg = data["choices"][0]["message"]
                    text = msg.get("content", "")
                    if return_annotations:
                        annotations = self._extract_url_annotations(msg.get("annotations", []))
                        return text, annotations
                    return text
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                # log error path ด้วย
                status = "timeout" if isinstance(exc, httpx.TimeoutException) else "error"
                http_status: int | None = None
                request_id: str | None = None
                if isinstance(exc, httpx.HTTPStatusError):
                    http_status = exc.response.status_code
                    try:
                        request_id = exc.response.json().get("id")
                    except Exception:
                        pass
                self._log_usage(used_model, source, None, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, status=status, http_status=http_status, error_message=str(exc), request_id=request_id)
                if self._aborted:
                    raise RuntimeError("Request aborted")
                if attempt < max_retry_limit:
                    wait = 2**attempt
                    time.sleep(wait)
                continue

        raise RuntimeError(f"LLM request failed after {max_retry_limit} retries: {last_error}")

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

    def _chat_stream(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str | None]:
        """Stream chat completion and display real-time output.

        คืน (text, usage, request_id) — usage มาจาก chunk สุดท้ายเมื่อเปิด stream_options.include_usage
        request_id มาจาก chunk แรก/สุดท้ายที่ระบุ id
        """
        import json

        collected: list[str] = []
        text = Text()
        usage: dict[str, Any] | None = None
        request_id: str | None = None

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
                        if chunk.get("id"):
                            request_id = chunk.get("id")
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
                request_id: str | None = None
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
                            if chunk.get("id"):
                                request_id = chunk.get("id")
                            if chunk.get("usage"):
                                usage = chunk["usage"]
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue
                self._log_usage(used_model, source, usage, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, request_id=request_id)
                return
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                status = "timeout" if isinstance(exc, httpx.TimeoutException) else "error"
                http_status: int | None = None
                request_id: str | None = None
                if isinstance(exc, httpx.HTTPStatusError):
                    http_status = exc.response.status_code
                    try:
                        request_id = exc.response.json().get("id")
                    except Exception:
                        pass
                self._log_usage(used_model, source, None, duration_ms=int((time.time() - t0) * 1000), attempt=attempt, status=status, http_status=http_status, error_message=str(exc), request_id=request_id)
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
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
