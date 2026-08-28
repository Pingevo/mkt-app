"""Acceptance runner logging tests — ensure all real AI calls hit AI Usage Hub.

Seams under test:
  - runner.wrap_log calls the original LLMClient._log_usage (record_ai_usage)
  - local JSONL log gets user, reference, request_id, cost_usd, source, model
  - Hub dispatch works when token present (synchronous fake thread)
  - logging failure does not break the agent run
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.ai_usage as ai_usage
import src.flow_context as flow_context
import src.llm_client as llm_client


class _FakeThread:
    def __init__(self, target, args=(), kwargs=None, daemon=False, name=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        pass


def _tmp_log():
    d = tempfile.mkdtemp()
    return Path(d) / "llm_usage.jsonl"


def _entries(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "acceptance_competitor_runner",
        str(Path(__file__).resolve().parent / "acceptance_competitor_runner.py"),
    )
    runner = importlib.util.module_from_spec(spec)
    sys.modules["acceptance_competitor_runner"] = runner
    spec.loader.exec_module(runner)
    return runner


class FakeLLMClient:
    def __init__(self, api_key=None, base_url=None, default_model="minimax/minimax-m3:free", timeout=120):
        self._default_model = default_model
        self.calls = []

    def chat(self, messages, *args, **kwargs):
        source = kwargs.get("source", "")
        model = kwargs.get("model") or self._default_model
        self.calls.append({
            "source": source,
            "model": model,
            "tools": bool(kwargs.get("tools")),
            "return_annotations": bool(kwargs.get("return_annotations")),
        })
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost": 0.012,
            "server_tool_use_details": {"web_search_requests": 1, "tool_calls_executed": 1},
        }
        output = (
            "# รายงานวิเคราะห์ CACGO K77\n\n"
            "## สรุปสินค้าของเรา\n\n"
            "CACGO K77 เป็นสมาร์ทวอทช์รุ่นใหม่ หน้าจอ 1.7\" full round IPS ความละเอียด 360x360 "
            "พิกเซล CPU Realtek 8773EWE-VP รองรับ Bluetooth 5.0 เซนเซอร์ Heart Rate + SpO2 "
            "มาตรฐานกันน้ำ IP68 แบตเตอรี่ 1000mAh โหมดกีฬา 100+ โหมด ฟังก์ชัน Flashlight "
            "BT calling BT music ตัวเรือนสี Black/Silver สายซิลิโคน 22mm ถอดได้ "
            "ราคาต้นทุน FOB US$15.00\n\n"
            "## ข้อจำกัดของรายงาน\n\n"
            "เนื่องจากยังไม่มีข้อมูลคู่แข่งทีเฉพาะเจาะจง รายงานนี้จึงไม่สามารถเปรียบเทียบเชิงลึกได้ "
            "ข้อมูลทีจำเป็นต้องขอเพิ่มเติมประกอบด้วย สเปคคู่แข่ง ราคาขายปลีก ช่องทางจำหน่าย "
            "และรีวิวผู้ใช้งานจริง\n\n"
            "## คำแนะนำเบื้องต้น\n\n"
            "เน้นจุดขายแบตเตอรี่ 1000mAh หน้าจอ 1.7\" สีสัน และราคาต้นทุนทีต่ำ "
            "เป็นจุดแข็งหลักของสินค้า\n\n"
        )
        annotations = []
        if kwargs.get("return_annotations"):
            self._last_raw_response = {
                "choices": [{"message": {"content": output, "annotations": annotations}}],
                "usage": usage,
            }
        else:
            self._last_raw_response = {
                "choices": [{"message": {"content": output}}],
                "usage": usage,
            }
        # simulate real LLMClient behaviour: _log_usage is called per API response
        self._log_usage(
            model, source, usage,
            duration_ms=1234, attempt=1, request_id="req-acceptance-001",
        )
        if kwargs.get("return_annotations"):
            return output, annotations
        return output

    def close(self):
        pass


def _make_fixture_runner(tmp_log_path):
    """Load the runner module and bind it to the fake client with real _log_usage."""
    runner = _load_runner()
    FakeLLMClient._log_usage = llm_client.LLMClient._log_usage  # type: ignore
    runner.LLMClient = FakeLLMClient
    ai_usage.USAGE_LOG_PATH = tmp_log_path
    return runner


def test_acceptance_runner_records_local_usage_with_reference(monkeypatch):
    """Runner must call record_ai_usage so local log gets user, reference, request_id and cost."""
    path = _tmp_log()
    original_path = ai_usage.USAGE_LOG_PATH
    try:
        monkeypatch.setattr(ai_usage, "_read_hub_credentials", lambda: (None, None))
        runner = _make_fixture_runner(path)

        cfg = runner.load_config()
        agent_cfg = dict(runner.get_agent_config(cfg, "competitor_analysis"))
        agent_cfg["web_search"] = False
        case = runner.CASES[1]

        result = runner._run_one(1, 1, "minimax/minimax-m3:free", agent_cfg, case)

        # artifact must carry usage from API
        assert result["runtime_error"] is None
        assert result["llm_calls"] == 1
        assert result["total_cost_usd"] == 0.012
        assert any(entry.get("request_id") == "req-acceptance-001" for entry in result["usage_log"])

        # local log must contain the same event with actor and reference
        entries = _entries(path)
        assert len(entries) == 1
        assert entries[0]["user"] == "acceptance:competitor_analysis"
        assert entries[0]["reference"] == "acceptance:1:1"
        assert entries[0]["request_id"] == "req-acceptance-001"
        assert entries[0]["cost_usd"] == 0.012
        assert entries[0]["model"] == "minimax/minimax-m3:free"
        assert entries[0]["source"] == "competitor_analysis.generate"
        assert entries[0]["status"] == "success"
        assert entries[0]["metadata"]["case_id"] == 1
    finally:
        ai_usage.USAGE_LOG_PATH = original_path
        flow_context.clear_usage_context()
        if path.exists():
            shutil.rmtree(path.parent)


def test_acceptance_runner_dispatches_to_hub_when_token_present(monkeypatch):
    """When Hub token is configured the same payload must be POSTed synchronously."""
    path = _tmp_log()
    original_path = ai_usage.USAGE_LOG_PATH
    posted = []

    def _capture_post(endpoint, token, payload):
        posted.append((endpoint, token, payload))

    try:
        monkeypatch.setattr(ai_usage, "_read_hub_credentials", lambda: ("https://digital.in.th", "svc_testtoken"))
        runner = _make_fixture_runner(path)

        cfg = runner.load_config()
        agent_cfg = dict(runner.get_agent_config(cfg, "competitor_analysis"))
        agent_cfg["web_search"] = False
        case = runner.CASES[1]

        with mock.patch.object(ai_usage, "_post", _capture_post):
            with mock.patch.object(ai_usage.threading, "Thread", _FakeThread):
                result = runner._run_one(1, 1, "minimax/minimax-m3:free", agent_cfg, case)

        assert result["runtime_error"] is None
        assert len(posted) == 1
        _, token, payload = posted[0]
        assert token == "svc_testtoken"
        assert payload["user"] == "acceptance:competitor_analysis"
        assert payload["reference"] == "acceptance:1:1"
        assert payload["request_id"] == "req-acceptance-001"
        assert payload["cost_usd"] == 0.012
        assert payload["source"] == "competitor_analysis.generate"
        assert payload["metadata"]["case_id"] == 1
        assert _entries(path)
    finally:
        ai_usage.USAGE_LOG_PATH = original_path
        flow_context.clear_usage_context()
        if path.exists():
            shutil.rmtree(path.parent)


def test_acceptance_runner_logging_failure_does_not_break_run(monkeypatch):
    """If record_ai_usage throws, the agent run must still return output."""
    path = _tmp_log()
    original_path = ai_usage.USAGE_LOG_PATH

    def _boom(*args, **kwargs):
        raise RuntimeError("hub down")

    try:
        monkeypatch.setattr(llm_client, "record_ai_usage", _boom)
        monkeypatch.setattr(ai_usage, "_read_hub_credentials", lambda: (None, None))
        runner = _make_fixture_runner(path)

        cfg = runner.load_config()
        agent_cfg = dict(runner.get_agent_config(cfg, "competitor_analysis"))
        agent_cfg["web_search"] = False
        case = runner.CASES[1]

        result = runner._run_one(1, 1, "minimax/minimax-m3:free", agent_cfg, case)

        assert result["runtime_error"] is None
        assert "CACGO K77" in result["output"]
        assert result["validation_ok"] is True
    finally:
        ai_usage.USAGE_LOG_PATH = original_path
        flow_context.clear_usage_context()
        if path.exists():
            shutil.rmtree(path.parent)
