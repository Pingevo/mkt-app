"""OpenRouter API client — shared LLM backend for all agents."""

from __future__ import annotations

import time
from typing import Any

import httpx
from rich.console import Console
from rich.live import Live
from rich.text import Text

console = Console()


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
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        max_retry_limit: int = 3,
        stream: bool = True,
        plugins: list[dict[str, Any]] | None = None,
    ) -> str:
        """Send a chat completion request and return the assistant's text reply.

        If stream=True, displays real-time output as the LLM generates.
        Retries on transient errors (5xx, timeouts) up to ``max_retry_limit`` times
        with exponential backoff.

        If plugins is provided (e.g. [{"id": "web"}]), enables OpenRouter plugins
        such as web search.
        """
        payload: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if plugins:
            payload["plugins"] = plugins

        last_error: Exception | None = None
        for attempt in range(1, max_retry_limit + 1):
            try:
                if stream:
                    return self._chat_stream(payload)
                else:
                    resp = self._client.post("/chat/completions", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                if self._aborted:
                    raise RuntimeError("Request aborted")
                if attempt < max_retry_limit:
                    wait = 2**attempt
                    time.sleep(wait)
                continue

        raise RuntimeError(f"LLM request failed after {max_retry_limit} retries: {last_error}")

    def _chat_stream(self, payload: dict[str, Any]) -> str:
        """Stream chat completion and display real-time output."""
        collected: list[str] = []
        text = Text()

        with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            with Live(text, console=console, refresh_per_second=15, transient=False) as live:
                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        import json
                        chunk = json.loads(data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            collected.append(content)
                            text.append(content)
                            live.update(text)
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue

        console.print()
        return "".join(collected)

    def chat_stream_yield(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        max_retry_limit: int = 3,
        plugins: list[dict[str, Any]] | None = None,
    ):
        """Stream chat completion, yielding chunks. For web SSE."""
        import json

        payload: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if plugins:
            payload["plugins"] = plugins

        last_error: Exception | None = None
        for attempt in range(1, max_retry_limit + 1):
            try:
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
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue
                return
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                if attempt < max_retry_limit:
                    time.sleep(2**attempt)
                continue

        raise RuntimeError(f"LLM request failed after {max_retry_limit} retries: {last_error}")

    def close(self) -> None:
        self._client.close()

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
