"""Acceptance runner → local HTTP server integration test.

Proves that runner posts the same event to local JSONL and to the Hub,
and that the process can exit after flushing the daemon thread.
No real OpenRouter or production Hub is called.
"""
import http.server
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.ai_usage as ai_usage
import src.flow_context as flow_context
import src.llm_client as llm_client


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
        self._log_usage(
            model, source, usage,
            duration_ms=1234, attempt=1, request_id="req-acceptance-001",
        )
        if kwargs.get("return_annotations"):
            return output, annotations
        return output

    def close(self):
        pass


def test_acceptance_runner_posts_to_local_hub_and_local_log(monkeypatch, tmp_path):
    """End-to-end: runner logs one event locally and POSTs one event to a local HTTP server."""
    received: list[dict] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            received.append(json.loads(body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    original_log_path = ai_usage.USAGE_LOG_PATH
    try:
        monkeypatch.setattr(ai_usage, "_HUB_ENDPOINT", "/test-logs")
        monkeypatch.setattr(ai_usage, "_read_hub_credentials", lambda: (f"http://127.0.0.1:{port}", "testtoken"))
        log_path = _tmp_log()
        ai_usage.USAGE_LOG_PATH = log_path

        runner = _load_runner()
        FakeLLMClient._log_usage = llm_client.LLMClient._log_usage
        runner.LLMClient = FakeLLMClient

        cfg = runner.load_config()
        agent_cfg = dict(runner.get_agent_config(cfg, "competitor_analysis"))
        agent_cfg["web_search"] = False
        case = runner.CASES[1]

        result = runner._run_one(1, 1, "minimax/minimax-m3:free", agent_cfg, case)

        # runner already calls flush_usage_log before close
        assert len(received) == 1
        payload = received[0]
        assert payload["user"] == "acceptance:competitor_analysis"
        assert payload["reference"] == "acceptance:1:1"
        assert payload["request_id"] == "req-acceptance-001"
        assert payload["cost_usd"] == 0.012
        assert payload["source"] == "competitor_analysis.generate"
        assert payload["model"] == "minimax/minimax-m3:free"
        assert payload["environment"] == "production"

        # local log is the same single event
        entries = _entries(log_path)
        assert len(entries) == 1
        assert entries[0]["request_id"] == "req-acceptance-001"
        assert entries[0]["cost_usd"] == 0.012
        assert entries[0]["reference"] == "acceptance:1:1"

        # artifact and log match
        assert result["total_cost_usd"] == 0.012
        assert any(entry.get("request_id") == "req-acceptance-001" for entry in result["usage_log"])
    finally:
        ai_usage.USAGE_LOG_PATH = original_log_path
        flow_context.clear_usage_context()
        server.shutdown()
        server.server_close()
        if log_path.exists():
            shutil.rmtree(log_path.parent)
