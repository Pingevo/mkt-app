"""Scheduler run log cleanup — ล้างประวัติเกิน 30 วัน

ปัจจุบัน JsonJobStore ล้างตามจำนวน (max_runs=200)
เปลี่ยนเป็นล้างตามอายุ — ลบ entry ที่เก่ากว่า 30 วัน

test นี้ตรวจว่า append_run ลบ entry เก่าออกอัตโนมัติ
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.scheduler import JsonJobStore


@pytest.fixture
def _store(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    runs_path = tmp_path / "runs.json"
    # retention_days=30 — ลบ entry ที่เก่ากว่า 30 วัน
    return JsonJobStore(jobs_path, runs_path, retention_days=30)


def _make_run(days_ago: int, job_id: str = "job_1") -> dict:
    """สร้าง run record ที่เกิดขึ้น N วันที่แล้ว."""
    ts = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat()
    return {
        "job_id": job_id,
        "job_name": "test",
        "started_at": ts,
        "finished_at": ts,
        "status": "success",
        "flow": {},
        "quick_brief": "",
        "output_files": [],
        "error": "",
        "trigger": "auto",
    }


def test_old_runs_deleted_on_append(_store):
    """เมื่อ append run ใหม่ — run ที่เก่ากว่า 30 วัน ต้องถูกลบออก."""
    # ใส่ run เก่า 35 วัน + run ใหม่ 1 วัน
    _store.append_run(_make_run(days_ago=35, job_id="old"))
    _store.append_run(_make_run(days_ago=1, job_id="recent"))

    # ใส่ run ใหม่ — ควร trigger cleanup
    _store.append_run(_make_run(days_ago=0, job_id="new"))

    runs = _store.load_runs()
    job_ids = {r["job_id"] for r in runs}
    assert "old" not in job_ids, "run ที่เก่ากว่า 30 วัน ต้องถูกลบ"
    assert "recent" in job_ids, "run ที่อายุ 1 วัน ต้องอยู่"
    assert "new" in job_ids, "run ใหม่ต้องอยู่"


def test_runs_exactly_30_days_kept(_store):
    """run ที่อายุ 30 วันพอดี ยังต้องอยู่ (เก็บถึง 30 วัน รวม)."""
    _store.append_run(_make_run(days_ago=30, job_id="boundary"))
    _store.append_run(_make_run(days_ago=0, job_id="new"))

    runs = _store.load_runs()
    job_ids = {r["job_id"] for r in runs}
    assert "boundary" in job_ids, "run ที่อายุ 30 วันพอดี ต้องยังอยู่"


def test_no_runs_deleted_when_all_recent(_store):
    """ถ้าทุก run ใหม่กว่า 30 วัน — ไม่ลบอะไร."""
    for i in range(5):
        _store.append_run(_make_run(days_ago=i, job_id=f"job_{i}"))

    runs = _store.load_runs()
    assert len(runs) == 5, "ทุก run ใหม่ ต้องไม่ถูกลบ"


def test_empty_store_no_error(_store):
    """append_run บน store ว่าง ต้องไม่ error."""
    _store.append_run(_make_run(days_ago=0, job_id="first"))
    runs = _store.load_runs()
    assert len(runs) == 1
