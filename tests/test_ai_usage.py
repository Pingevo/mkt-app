"""Tests for ai_usage — build entry, record to Hub, and persist locally.

Seams under test:
  - make_entry() builds a Hub-ready dict with all expected fields
  - record_ai_usage() writes the same event to local JSONL and dispatches to Hub
  - flow/usage context (actor, reference, metadata) is merged automatically
  - missing Hub credentials result in local-only write; dispatch failure is swallowed
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.ai_usage as ai_usage
import src.flow_context as flow_context


def _tmp_log():
    """Create a temporary JSONL file for a single test."""
    d = tempfile.mkdtemp()
    return Path(d) / "llm_usage.jsonl"


def _entries(path):
    """Read all JSON entries from a JSONL file."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _FakeThread:
    """Run the target synchronously so tests can observe side effects."""
    def __init__(self, target, args=(), kwargs=None, daemon=False, **extra):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self._daemon = daemon
        # Accept and ignore any additional kwargs (e.g. name=) that the
        # production code passes to threading.Thread.

    def start(self):
        self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        pass


def test_make_entry_includes_all_hub_fields():
    """make_entry() must expose every field used by the Hub contract."""
    entry = ai_usage.make_entry(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        operation="chat.completions",
        source="product_spec.generate",
        user="cli",
        reference="K5",
        request_id="gen-001",
        prompt_tokens=100,
        completion_tokens=50,
        units={"images_processed": 2},
        cost_usd=0.0012,
        duration_ms=850,
        attempt=2,
        status="success",
        http_status=200,
        raw_usage={"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.0012},
        metadata={"product_id": "K5"},
    )
    assert entry["provider"] == "openrouter"
    assert entry["model"] == "anthropic/claude-sonnet-4"
    assert entry["operation"] == "chat.completions"
    assert entry["source"] == "product_spec.generate"
    assert entry["user"] == "cli"
    assert entry["reference"] == "K5"
    assert entry["request_id"] == "gen-001"
    assert entry["prompt_tokens"] == 100
    assert entry["completion_tokens"] == 50
    assert entry["units"]["images_processed"] == 2
    assert entry["cost_usd"] == 0.0012
    assert entry["duration_ms"] == 850
    assert entry["attempt"] == 2
    assert entry["status"] == "success"
    assert entry["http_status"] == 200
    assert entry["raw_usage"]["cost"] == 0.0012
    assert entry["metadata"]["product_id"] == "K5"


def test_record_ai_usage_writes_local_even_without_hub_token():
    """When no Hub token is configured, only the local JSONL should be written."""
    path = _tmp_log()
    original_log_path = ai_usage.USAGE_LOG_PATH
    try:
        ai_usage.USAGE_LOG_PATH = path
        # Ensure no Hub credentials leak from environment
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("AI_USAGE_HUB_URL", "AI_USAGE_HUB_TOKEN"):
                os.environ.pop(key, None)
            flow_context.clear_usage_context()
            flow_context.set_flow_id("flow-xyz")
            flow_context.set_usage_actor("web")
            flow_context.set_usage_reference("K5")

            ai_usage.record_ai_usage(ai_usage.make_entry(source="test.no_token"))

            entries = _entries(path)
            assert len(entries) == 1
            assert entries[0]["source"] == "test.no_token"
            assert entries[0]["flow_id"] == "flow-xyz"
            assert entries[0]["user"] == "web"
            assert entries[0]["reference"] == "K5"
            assert "timestamp" in entries[0]
    finally:
        ai_usage.USAGE_LOG_PATH = original_log_path
        shutil.rmtree(path.parent)
        flow_context.clear_usage_context()


def test_record_ai_usage_dispatches_to_hub_when_token_present():
    """When Hub token is present, the same payload (plus defaults) is POSTed."""
    path = _tmp_log()
    original_log_path = ai_usage.USAGE_LOG_PATH
    posted = []
    fake_thread = threading.Thread

    def _capture_post(endpoint, token, payload):
        posted.append((endpoint, token, payload))

    try:
        ai_usage.USAGE_LOG_PATH = path
        with mock.patch.dict(os.environ, {
            "AI_USAGE_HUB_URL": "https://digital.in.th",
            "AI_USAGE_HUB_TOKEN": "svc_testtoken",
        }):
            flow_context.clear_usage_context()
            flow_context.set_flow_id("flow-abc")
            flow_context.set_usage_actor("scheduler")
            flow_context.set_usage_reference("K5")
            flow_context.set_usage_metadata({"job_id": "job-1"})

            with mock.patch.object(ai_usage, "_post", _capture_post):
                with mock.patch.object(ai_usage.threading, "Thread", _FakeThread):
                    ai_usage.record_ai_usage(ai_usage.make_entry(source="test.hub"))

            assert len(posted) == 1
            endpoint, token, payload = posted[0]
            assert endpoint.endswith("/internal/ai-usage/logs")
            assert token == "svc_testtoken"
            assert payload["source"] == "test.hub"
            assert "flow_id" not in payload  # flow_id อยู่ใน metadata / local ไม่ใช่ top-level
            assert payload["metadata"]["flow_id"] == "flow-abc"
            assert payload["user"] == "scheduler"
            assert payload["reference"] == "K5"
            assert payload["metadata"]["job_id"] == "job-1"
            assert payload["environment"] == "production"
            assert payload["attempt"] == 1
            assert payload["status"] == "success"

            # local should still be written
            entries = _entries(path)
            assert len(entries) == 1
            assert entries[0]["flow_id"] == "flow-abc"
    finally:
        ai_usage.USAGE_LOG_PATH = original_log_path
        if path.exists():
            shutil.rmtree(path.parent)
        flow_context.clear_usage_context()


def test_record_ai_usage_swallows_hub_errors():
    """A Hub dispatch exception must not escape the public interface."""
    path = _tmp_log()
    original_log_path = ai_usage.USAGE_LOG_PATH

    def _boom(endpoint, token, payload):
        raise RuntimeError("network down")

    try:
        ai_usage.USAGE_LOG_PATH = path
        with mock.patch.dict(os.environ, {
            "AI_USAGE_HUB_URL": "https://digital.in.th",
            "AI_USAGE_HUB_TOKEN": "svc_testtoken",
        }):
            flow_context.clear_usage_context()
            with mock.patch.object(ai_usage, "_post", _boom):
                with mock.patch.object(ai_usage.threading, "Thread", _FakeThread):
                    ai_usage.record_ai_usage(ai_usage.make_entry(source="test.hub_error"))
            # local still written, no exception raised
            assert len(_entries(path)) == 1
    finally:
        ai_usage.USAGE_LOG_PATH = original_log_path
        if path.exists():
            shutil.rmtree(path.parent)
        flow_context.clear_usage_context()


def test_context_merge_does_not_override_caller_fields():
    """Caller-provided fields win over context; missing fields use context."""
    path = _tmp_log()
    original_log_path = ai_usage.USAGE_LOG_PATH
    try:
        ai_usage.USAGE_LOG_PATH = path
        flow_context.clear_usage_context()
        flow_context.set_usage_actor("web")
        flow_context.set_usage_reference("K9")

        entry = ai_usage.make_entry(
            source="test.merge",
            user="overridden_user",
            reference="overridden_ref",
        )
        ai_usage.record_ai_usage(entry)

        entries = _entries(path)
        assert entries[0]["user"] == "overridden_user"
        assert entries[0]["reference"] == "overridden_ref"
    finally:
        ai_usage.USAGE_LOG_PATH = original_log_path
        if path.exists():
            shutil.rmtree(path.parent)
        flow_context.clear_usage_context()
