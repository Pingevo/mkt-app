"""Offline tests for web-search capability probe.

Seams:
  - probe.run_probe() accepts an LLMClient and returns a report dict
  - probe builds the correct OpenRouter payload for server tool vs plugin
  - probe captures tool state, annotations, and usage without calling real API
  - Hub callback lifecycle is correct (set before, flush before clear, restore previous)
  - plugin mechanism is classified by raw annotations, not server_tool_use_details
"""
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.ai_usage as ai_usage
import src.flow_context as flow_context
import src.llm_client as llm_client


class _FakeLLMClient:
    _default_model = "minimax/minimax-m3:free"

    def __init__(self):
        self.calls: list[dict] = []

    def chat(self, messages, *args, **kwargs):
        self.calls.append({
            "messages": messages,
            "tools": kwargs.get("tools"),
            "plugins": kwargs.get("plugins"),
            "return_annotations": kwargs.get("return_annotations"),
        })
        is_server = kwargs.get("tools") is not None
        is_plugin = kwargs.get("plugins") is not None
        # Simulate real OpenRouter: only server tool reports server_tool_use_details.
        if is_server:
            usage = {"cost": 0.0, "server_tool_use_details": {"web_search_requests": 1, "tool_calls_executed": 1}}
            annotations = [
                {"url": "https://shopee.co.th/CACGO-K77-123", "title": "CACGO K77", "content": "ราคา 1,500 บาท"},
            ]
            output = "K77 ราคา 1,500 บาท [Shopee](https://shopee.co.th/CACGO-K77-123)"
            raw_annotations = annotations
        elif is_plugin:
            usage = {"cost": 0.0}
            annotations = [
                {"url": "https://www.mi.com/th/product/xiaomi-watch-s3/", "title": "Xiaomi Watch S3", "content": "ราคา 4,890"},
            ]
            output = "Xiaomi Watch S3 ราคา 4,890 บาท"
            raw_annotations = annotations
        else:
            usage = {"cost": 0.0}
            annotations = []
            output = "ไม่พบหลักฐาน"
            raw_annotations = []
        self._last_raw_response = {
            "id": "probe-req-1",
            "choices": [{"message": {"content": output, "annotations": raw_annotations}}],
            "usage": usage,
        }
        self._last_raw_annotations_count = len(raw_annotations)
        self._last_annotations_count = len(annotations)
        self._log_usage(
            kwargs.get("model") or self._default_model,
            kwargs.get("source", "web_search.probe"),
            usage,
            duration_ms=1234,
            attempt=1,
            request_id="probe-req-1",
        )
        if kwargs.get("return_annotations"):
            return output, annotations
        return output

    def _log_usage(self, *args, **kwargs):
        llm_client.LLMClient._log_usage(*args, **kwargs)

    def close(self):
        pass


def _fake_post_factory(status_code: Optional[int] = 200, delay: float = 0.0, fail: bool = False):
    def _post(endpoint, token, payload):
        if fail:
            result = {
                "endpoint": endpoint,
                "request_id": payload.get("request_id"),
                "error": "timeout",
                "hub_status": "hub_transport_error",
            }
        elif status_code is not None:
            result = {
                "endpoint": endpoint,
                "request_id": payload.get("request_id"),
                "status_code": status_code,
                "hub_status": "hub_delivered" if (status_code // 100) == 2 else "hub_http_error",
            }
        else:
            result = {"endpoint": endpoint, "request_id": payload.get("request_id"), "error": "timeout"}
        if delay:
            time.sleep(delay)
        cb = ai_usage.HUB_POST_CALLBACK
        if cb:
            cb(result)
    return _post


def _run_with_patches(monkeypatch):
    path = Path("/tmp/probe_test_llm_usage.jsonl")
    original_path = ai_usage.USAGE_LOG_PATH
    ai_usage.USAGE_LOG_PATH = path
    flow_context.clear_usage_context()
    monkeypatch.setattr(ai_usage, "_read_hub_credentials", lambda: ("http://localhost", "tok"))
    return path, original_path


def test_run_probe_returns_tool_status_and_payload_for_server_tool(monkeypatch):
    """Server-tool probe must call LLMClient.chat with tools, capture annotations and pass the gate."""
    path, original_path = _run_with_patches(monkeypatch)
    monkeypatch.setattr(ai_usage, "_post", _fake_post_factory(status_code=200, delay=0.1))
    try:
        from tests.competitor_search_probe import run_probe

        llm = _FakeLLMClient()
        result = run_probe(
            llm,
            model="minimax/minimax-m3:free",
            mechanism="server_tool",
            probe_id="test-server",
        )

        assert result["model"] == "minimax/minimax-m3:free"
        assert result["mechanism"] == "server_tool"
        assert result["tool_status"] == "tool_invoked_with_annotations"
        assert result["raw_annotations_count"] == 1
        assert result["annotations_count"] == 1
        assert result["request_id"] == "probe-req-1"
        assert result["tool_use"]["tool_calls_executed"] == 1
        assert result["relevant_annotations_count"] >= 1
        assert result["hub_status"] == "hub_delivered"
        assert result["probe_passed"] is True
        assert result["local_event"] is not None
        assert result["local_event"]["request_id"] == "probe-req-1"

        assert len(result["hub_results"]) == 1
        assert result["hub_results"][0]["status_code"] == 200
        assert result["hub_results"][0]["request_id"] == "probe-req-1"

        assert len(llm.calls) == 1
        assert llm.calls[0]["tools"] is not None
        assert llm.calls[0]["plugins"] is None
        assert any(t.get("type") == "openrouter:web_search" for t in llm.calls[0]["tools"])
    finally:
        ai_usage.USAGE_LOG_PATH = original_path
        if path.exists():
            path.unlink()


def test_run_probe_returns_correct_tool_status_for_plugin(monkeypatch):
    """Plugin probe must call LLMClient.chat with plugins payload and classify by annotations."""
    path, original_path = _run_with_patches(monkeypatch)
    monkeypatch.setattr(ai_usage, "_post", _fake_post_factory(status_code=200))
    try:
        from tests.competitor_search_probe import run_probe

        llm = _FakeLLMClient()
        result = run_probe(
            llm,
            model="minimax/minimax-m3:free",
            mechanism="plugin",
            probe_id="test-plugin",
        )

        assert result["mechanism"] == "plugin"
        assert result["tool_status"] == "plugin_invoked_with_annotations"
        assert result["raw_annotations_count"] == 1
        assert result["relevant_annotations_count"] >= 1
        assert result["hub_status"] == "hub_delivered"
        assert result["probe_passed"] is True
        assert len(llm.calls) == 1
        assert llm.calls[0]["plugins"] == [{"id": "web"}]
        assert llm.calls[0]["tools"] is None
    finally:
        ai_usage.USAGE_LOG_PATH = original_path
        if path.exists():
            path.unlink()


def test_run_probe_plugin_without_annotations(monkeypatch):
    """Plugin with no annotations must classify as plugin_no_annotations, not tool_not_invoked."""
    path, original_path = _run_with_patches(monkeypatch)
    monkeypatch.setattr(ai_usage, "_post", _fake_post_factory(status_code=200))
    try:
        from tests.competitor_search_probe import run_probe

        class _FakeNoAnnotations(_FakeLLMClient):
            def chat(self, messages, *args, **kwargs):
                self.calls.append(kwargs)
                self._last_raw_response = {
                    "id": "probe-req-2",
                    "choices": [{"message": {"content": "ไม่พบ", "annotations": []}}],
                    "usage": {"cost": 0.0},
                }
                self._last_raw_annotations_count = 0
                self._last_annotations_count = 0
                llm_client.LLMClient._log_usage(
                    "minimax", "web_search.probe", {"cost": 0.0},
                    duration_ms=100, attempt=1, request_id="probe-req-2",
                )
                return "ไม่พบ", []

        llm = _FakeNoAnnotations()
        result = run_probe(llm, model="minimax", mechanism="plugin", probe_id="test-plugin-empty")

        assert result["tool_status"] == "plugin_no_annotations"
        assert result["probe_passed"] is False
        assert "plugin_no_annotations" in (result.get("probe_fail_reasons") or [])
    finally:
        ai_usage.USAGE_LOG_PATH = original_path
        if path.exists():
            path.unlink()


def test_callback_is_restored_after_run(monkeypatch):
    """run_probe must restore the previous HUB_POST_CALLBACK."""
    path, original_path = _run_with_patches(monkeypatch)
    monkeypatch.setattr(ai_usage, "_post", _fake_post_factory(status_code=200))

    sentinel = lambda r: None
    ai_usage.HUB_POST_CALLBACK = sentinel
    try:
        from tests.competitor_search_probe import run_probe

        llm = _FakeLLMClient()
        run_probe(llm, model="minimax/minimax-m3:free", mechanism="server_tool", probe_id="test-restore")

        assert ai_usage.HUB_POST_CALLBACK is sentinel
    finally:
        ai_usage.HUB_POST_CALLBACK = None
        ai_usage.USAGE_LOG_PATH = original_path
        if path.exists():
            path.unlink()


def test_hub_timeout_is_reported_not_failure(monkeypatch):
    """A delayed Hub POST beyond flush timeout must be hub_timeout_unknown, not hub_delivery_failed."""
    path, original_path = _run_with_patches(monkeypatch)
    # delay > probe flush timeout (20s) is too long for tests; patch flush to return False
    def _post(endpoint, token, payload):
        pass  # never calls back
    monkeypatch.setattr(ai_usage, "_post", _post)
    monkeypatch.setattr(ai_usage, "flush_usage_log", lambda timeout: False)
    try:
        from tests.competitor_search_probe import run_probe

        llm = _FakeLLMClient()
        result = run_probe(
            llm,
            model="minimax/minimax-m3:free",
            mechanism="server_tool",
            probe_id="test-hub-timeout",
        )

        assert result["probe_passed"] is False
        assert result["hub_status"] == "hub_timeout_unknown"
        assert "hub_timeout_unknown" in (result.get("probe_fail_reasons") or [])
        assert "hub_delivery_failed" not in (result.get("probe_fail_reasons") or [])
    finally:
        ai_usage.USAGE_LOG_PATH = original_path
        if path.exists():
            path.unlink()


def test_hub_http_error_and_transport_error(monkeypatch):
    """HTTP 5xx and transport error must yield distinct hub statuses."""
    path, original_path = _run_with_patches(monkeypatch)
    monkeypatch.setattr(ai_usage, "_post", _fake_post_factory(status_code=500))
    try:
        from tests.competitor_search_probe import run_probe

        llm = _FakeLLMClient()
        result = run_probe(
            llm,
            model="minimax/minimax-m3:free",
            mechanism="server_tool",
            probe_id="test-hub-500",
        )

        assert result["hub_status"] == "hub_http_error"
        assert result["probe_passed"] is False
        assert "hub_http_error" in (result.get("probe_fail_reasons") or [])
    finally:
        ai_usage.USAGE_LOG_PATH = original_path
        if path.exists():
            path.unlink()
