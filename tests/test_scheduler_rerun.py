"""Scheduler rerun — กดรันใหม่จากประวัติได้ แม้ job ต้นทางถูกลบไปแล้ว

หลังจาก one_time job รันเสร็จ มันจะถูกลบจาก job list
แต่ run record ยังอยู่ในประวัติ — user ต้องกด "รันใหม่" จากประวัติได้
โดยไม่ต้องมี job ต้นทางอยู่

flow + quick_brief ต้องถูกเก็บใน run record ทุกครั้ง
เพื่อให้ rerun พึ่งตัวเองได้
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.scheduler import JsonJobStore, Scheduler


@pytest.fixture
def _store(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    runs_path = tmp_path / "runs.json"
    return JsonJobStore(jobs_path, runs_path, max_runs=200)


@pytest.fixture
def _scheduler(_store, tmp_path):
    """Scheduler ที่ไม่ start APScheduler จริง — ใช้สำหรับ unit test."""
    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    return sched


def _fake_httpx_stream(lines):
    """สร้าง fake context manager ที่ yield response พร้อม SSE lines."""
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "text/event-stream"}
    resp.iter_lines = MagicMock(return_value=iter(lines))
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=resp)
    ctx.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.stream = MagicMock(return_value=ctx)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _sse_lines():
    """SSE response สำหรับ run สำเร็จ 1 ไฟล์."""
    return [
        'data: {"type":"agent_start","agent":"content_creator"}',
        'data: {"type":"agent_done","file":"/tmp/out.md","session_ts":"ts1"}',
        'data: {"type":"done"}',
    ]


def test_run_record_stores_flow_and_quick_brief(_scheduler, _store):
    """run record ทุกครั้งต้องเก็บ flow + quick_brief เพื่อให้ rerun ได้."""
    flow = {"is_auto": True, "agents": ["content_creator"], "content_count": 1}
    quick_brief = "ทดสอบ rerun"

    # สร้าง job แล้วยิงผ่าน _run_job (mock httpx)
    job_spec = {
        "name": "test",
        "schedule_type": "one_time",
        "schedule": {"type": "date", "value": "2026-08-19T10:00:00"},
        "flow": flow,
        "quick_brief": quick_brief,
    }
    job_id = _scheduler.add_job(job_spec)

    with patch("src.scheduler.httpx.Client", return_value=_fake_httpx_stream(_sse_lines())):
        _scheduler._run_job(job_id, trigger="auto")

    runs = _store.load_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run["flow"] == flow, "run record ต้องเก็บ flow ไว้"
    assert run["quick_brief"] == quick_brief, "run record ต้องเก็บ quick_brief ไว้"


def test_rerun_run_loads_flow_from_run_record(_scheduler, _store):
    """rerun_run(run_id) ต้องโหลด flow จาก run record แล้วยิงใหม่ได้
    แม้ job ต้นทางจะถูกลบไปแล้ว (one_time job ถูกลบหลังรัน)
    """
    flow = {"is_auto": True, "agents": ["content_creator"], "content_count": 1}
    quick_brief = "rerun me"

    # สร้าง run record โดยตรง (จำลอง job ที่รันแล้วและถูกลบ)
    run_record = {
        "job_id": "job_deleted",
        "job_name": "deleted job",
        "started_at": "2026-08-19T09:00:00+07:00",
        "finished_at": "2026-08-19T09:01:00+07:00",
        "status": "success",
        "flow": flow,
        "quick_brief": quick_brief,
        "output_files": ["/tmp/old.md"],
        "error": "",
        "trigger": "auto",
    }
    _store.append_run(run_record)

    # job ต้นทางไม่มีใน store (ถูกลบไปแล้ว)
    assert _store.load_jobs() == []

    # mock httpx เพื่อจับ payload ที่ยิงออกไป
    captured_payload = {}

    def _capture_stream(method, url, json=None, **kw):
        captured_payload["method"] = method
        captured_payload["url"] = url
        captured_payload["json"] = json
        return _fake_httpx_stream(_sse_lines())

    client = MagicMock()
    client.stream = MagicMock(side_effect=_capture_stream)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("src.scheduler.httpx.Client", return_value=cm):
        ok = _scheduler.rerun_run(run_record["job_id"], run_record["started_at"])

    assert ok is True, "rerun_run ต้องคืน True เมื่อยิงสำเร็จ"
    assert captured_payload["json"]["quick_brief"] == quick_brief, "rerun ต้องส่ง quick_brief เดิม"
    assert captured_payload["json"]["content_count"] == 1, "rerun ต้องส่ง flow เดิม"


def test_rerun_run_returns_false_when_run_not_found(_scheduler, _store):
    """rerun_run ต้องคืน False เมื่อไม่พบ run record ที่ระบุ."""
    ok = _scheduler.rerun_run("nonexistent_job", "2026-08-19T09:00:00+07:00")
    assert ok is False


def test_rerun_run_appends_new_run_record(_scheduler, _store):
    """rerun จาก success — ต้องสร้าง run record ใหม่ (เก็บเดิมไว้เปรียบเทียบ)."""
    flow = {"is_auto": True, "agents": ["content_creator"], "content_count": 1}
    run_record = {
        "job_id": "job_x",
        "job_name": "old job",
        "started_at": "2026-08-19T09:00:00+07:00",
        "finished_at": "2026-08-19T09:01:00+07:00",
        "status": "success",
        "flow": flow,
        "quick_brief": "rerun",
        "output_files": [],
        "error": "",
        "trigger": "auto",
    }
    _store.append_run(run_record)

    with patch("src.scheduler.httpx.Client", return_value=_fake_httpx_stream(_sse_lines())):
        _scheduler.rerun_run("job_x", "2026-08-19T09:00:00+07:00")
        # รอ executor ทำงานเสร็จ (rerun_run ส่งเข้า background thread)
        _scheduler._executor.shutdown(wait=True)

    runs = _store.load_runs()
    assert len(runs) == 2, "rerun จาก success ต้องมี 2 records: original + rerun"
    rerun = [r for r in runs if r.get("trigger") == "rerun"]
    assert len(rerun) == 1, "rerun record ต้องมี trigger='rerun'"
    assert rerun[0]["status"] == "success"
    # original ต้องยังอยู่ (ไม่ถูกแทนที่)
    original = [r for r in runs if r.get("trigger") == "auto"]
    assert len(original) == 1, "original success record ต้องยังอยู่"


def test_rerun_from_error_without_output_updates_original(_scheduler, _store):
    """rerun จาก error ที่ไม่มี output (พลาดเวลา/ถูกตัด) — update original record
    เพราะเป็น placeholder ไม่มีค่าใช้งาน ไม่ต้องเก็บประวัติ
    """
    flow = {"is_auto": True, "agents": ["content_creator"], "content_count": 1}
    error_record = {
        "job_id": "job_err",
        "job_name": "error job",
        "started_at": "2026-08-19T09:00:00+07:00",
        "finished_at": "2026-08-19T09:00:00+07:00",
        "status": "error",
        "flow": flow,
        "quick_brief": "was error",
        "output_files": [],
        "error": "พลาดเวลา",
        "trigger": "auto",
    }
    _store.append_run(error_record)

    with patch("src.scheduler.httpx.Client", return_value=_fake_httpx_stream(_sse_lines())):
        _scheduler.rerun_run("job_err", "2026-08-19T09:00:00+07:00")
        _scheduler._executor.shutdown(wait=True)

    runs = _store.load_runs()
    assert len(runs) == 1, "rerun จาก error ไม่มี output ต้อง update original ไม่เพิ่ม record ใหม่"
    run = runs[0]
    assert run["status"] == "success", "ต้องเปลี่ยนสถานะจาก error เป็น success"
    assert run["trigger"] == "rerun", "trigger ต้องเปลี่ยนเป็น 'rerun'"
    assert run["job_id"] == "job_err"
    assert run["flow"] == flow


def test_rerun_from_error_appends_new_record(_scheduler, _store):
    """rerun จาก error ที่มี output — ต้องสร้าง record ใหม่ (เก็บ error เดิมไว้ audit)."""
    flow = {"is_auto": True, "agents": ["content_creator"], "content_count": 1}
    error_record = {
        "job_id": "job_err",
        "job_name": "error job",
        "started_at": "2026-08-19T09:00:00+07:00",
        "finished_at": "2026-08-19T09:01:00+07:00",
        "status": "error",
        "flow": flow,
        "quick_brief": "errored",
        "output_files": ["/tmp/partial.md"],  # มี output บางส่วน → เก็บไว้ audit
        "error": "some error",
        "trigger": "auto",
    }
    _store.append_run(error_record)

    with patch("src.scheduler.httpx.Client", return_value=_fake_httpx_stream(_sse_lines())):
        _scheduler.rerun_run("job_err", "2026-08-19T09:00:00+07:00")
        _scheduler._executor.shutdown(wait=True)

    runs = _store.load_runs()
    assert len(runs) == 2, "rerun จาก error ต้องมี 2 records: original error + rerun"
    errors = [r for r in runs if r["status"] == "error"]
    assert len(errors) == 1, "original error ต้องยังอยู่"
    successes = [r for r in runs if r["status"] == "success"]
    assert len(successes) == 1, "rerun ต้องสำเร็จ"
