"""Scheduler failure handling — ทุกกรณีที่ job ไม่สำเร็จ ใช้สถานะ 'error'

ไม่ว่าจะ:
- พลาดเวลา (server ไม่ได้รันตอนถึงเวลา)
- ถูกตัดระหว่างรัน (server ดับตอนกำลังรัน)
- error จริงจาก agent

ทั้งหมด = status 'error' + error message บอกสาเหตุ รันใหม่ได้เหมือนกัน

MB-02: scheduler state is brand-scoped.  Tests establish a brand workspace
context before scheduler operations.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.scheduler import JsonJobStore, Scheduler, _aps_id
from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
from src.brand_registry import BrandRegistry


TEST_USER_ID = "user_missed"


def _register_user(tmp_path, user_id):
    """Register a user in tmp_path's auth store (root-aware seam)."""
    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    users_path.write_text(json.dumps([
        {"user_id": user_id, "username": user_id,
         "password_hash": "$2b$12$dummy", "created_at": "2026-09-11T00:00:00"},
    ]), encoding="utf-8")


def _brand_ctx(project_root, user_id, brand_name="TestBrand"):
    """Create or reuse a brand for user_id and set a brand workspace context.

    Returns (user_id, brand_id, token).  Caller must reset_workspace(token).
    """
    reg = BrandRegistry(user_id=user_id, project_root=project_root)
    brands = reg.list()
    if brands:
        brand_id = brands[0]["brand_id"]
    else:
        brand = reg.create(brand_name)
        brand_id = brand["brand_id"]
    ws = WorkspaceContext.for_brand(user_id, brand_id, project_root)
    token = set_workspace(ws)
    return user_id, brand_id, token


@pytest.fixture
def _store(tmp_path):
    return JsonJobStore(tmp_path / "jobs.json", tmp_path / "runs.json", retention_days=30)


@pytest.fixture
def _scheduler(_store, tmp_path):
    return Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)


def test_missed_event_records_as_error(_scheduler, _store, tmp_path):
    """เมื่อได้ EVENT_JOB_MISSED — บันทึกเป็น 'error' (ไม่ใช่ 'missed')."""
    uid, bid, token = _brand_ctx(tmp_path, TEST_USER_ID)
    try:
        job = {
            "id": "job_missed_1",
            "name": "พลาดไป",
            "enabled": True,
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": "2026-08-19T09:00:00"},
            "flow": {"is_auto": True, "agents": ["content_creator"], "content_count": 1},
            "quick_brief": "test missed",
            "user_id": uid,
            "brand_id": bid,
        }
        _store.save_jobs([job])
        _scheduler._job_specs[_aps_id(uid, bid, job["id"])] = job
    finally:
        reset_workspace(token)

    missed_event = MagicMock()
    missed_event.code = 2  # EVENT_JOB_MISSED
    missed_event.job_id = _aps_id(uid, bid, "job_missed_1")

    _scheduler._on_job_missed(missed_event)

    # Runs are brand-scoped — re-establish brand context to read them
    ws = WorkspaceContext.for_brand(uid, bid, tmp_path)
    tok = set_workspace(ws)
    try:
        runs = _store.load_runs()
    finally:
        reset_workspace(tok)

    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "error", "พลาดเวลา = error เหมือนกรณีอื่น"
    assert run["trigger"] == "auto"
    assert run["job_name"] == "พลาดไป"
    assert run["flow"] == job["flow"], "ต้องเก็บ flow ไว้เพื่อให้ rerun ได้"
    assert run["quick_brief"] == "test missed"
    assert "พลาดเวลา" in run["error"], "error message ต้องบอกสาเหตุ"


def test_start_records_past_one_time_jobs_as_error(_scheduler, _store, tmp_path):
    """start() ต้องตรวจ one_time job ที่เวลาผ่านไปแล้ว — บันทึกเป็น 'error' และลบออก."""
    _register_user(tmp_path, TEST_USER_ID)
    uid, bid, token = _brand_ctx(tmp_path, TEST_USER_ID)
    try:
        past_time = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat()
        job = {
            "id": "job_past_1",
            "name": "เลยเวลาไปแล้ว",
            "enabled": True,
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": past_time},
            "flow": {"is_auto": True, "agents": ["content_creator"], "content_count": 1},
            "quick_brief": "past job",
            "user_id": uid,
            "brand_id": bid,
        }
        _store.save_jobs([job])
    finally:
        reset_workspace(token)

    _scheduler.start()

    # Runs/jobs are brand-scoped — re-establish brand context to read them
    ws = WorkspaceContext.for_brand(uid, bid, tmp_path)
    tok = set_workspace(ws)
    try:
        runs = _store.load_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == "error", "เลยเวลา = error"
        assert runs[0]["job_name"] == "เลยเวลาไปแล้ว"

        jobs = _store.load_jobs()
        assert len(jobs) == 0, "one_time job ที่ error ต้องถูกลบออกจากรายการ"
    finally:
        reset_workspace(tok)

    _scheduler.stop()


def test_start_marks_stuck_running_as_error(_scheduler, _store, tmp_path):
    """start() ต้องตรวจ run record ที่ค้างเป็น 'running' — mark เป็น 'error'.

    เกิดจาก server ดับตอน job กำลังรัน — ใช้สถานะ 'error' เหมือนกรณีอื่น
    """
    _register_user(tmp_path, TEST_USER_ID)
    uid, bid, token = _brand_ctx(tmp_path, TEST_USER_ID)
    try:
        stuck_record = {
            "job_id": "job_stuck",
            "job_name": "ค้าง running",
            "started_at": "2026-08-19T10:54:37+07:00",
            "finished_at": "",
            "status": "running",
            "flow": {},
            "quick_brief": "",
            "output_files": [],
            "error": "",
            "trigger": "rerun",
            "user_id": uid,
            "brand_id": bid,
        }
        _store.append_run(stuck_record)
    finally:
        reset_workspace(token)

    _scheduler.start()

    # Runs are brand-scoped — re-establish brand context to read them
    ws = WorkspaceContext.for_brand(uid, bid, tmp_path)
    tok = set_workspace(ws)
    try:
        runs = _store.load_runs()
    finally:
        reset_workspace(tok)

    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "error", "ถูกตัดระหว่างรัน = error เหมือนกรณีอื่น"
    assert run["error"], "ต้องมี error message บอกสาเหตุ"

    _scheduler.stop()


def test_missed_event_unknown_job_no_error(_scheduler, _store):
    """ถ้าได้ event สำหรับ job ที่ไม่มีใน store — ต้องไม่ error."""
    missed_event = MagicMock()
    missed_event.code = 2
    missed_event.job_id = "nonexistent"

    _scheduler._on_job_missed(missed_event)

    runs = _store.load_runs()
    assert len(runs) == 0, "ไม่มี job ต้นทาง ต้องไม่บันทึกอะไร"
