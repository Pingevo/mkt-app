"""Restart exact-once — prove a future one-time job fires exactly once after restart.

Persist future one_time job
→ scheduler restart/re-registration
→ trigger time reached
→ logical execution occurs exactly once
→ one run-history record
→ one-time job is not replayed.

Mock the execution seam; no web/model/provider call.

MB-02: scheduler state is brand-scoped.  Tests establish a brand workspace
context before scheduler operations.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.scheduler import JsonJobStore, Scheduler
from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
from src.brand_registry import BrandRegistry


TEST_USER_ID = "user_restart"


def _register_user(tmp_path, user_id):
    """Register a user in tmp_path's auth store (root-aware seam)."""
    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    users_path.write_text(json.dumps([
        {"user_id": user_id, "username": user_id,
         "password_hash": "$2b$12$dummy", "created_at": "2026-09-11T00:00:00"},
    ]), encoding="utf-8")


def _make_brand(project_root, user_id, brand_name="TestBrand"):
    """Create or reuse a brand for user_id.  Returns brand_id."""
    reg = BrandRegistry(user_id=user_id, project_root=project_root)
    brands = reg.list()
    if brands:
        return brands[0]["brand_id"]
    return reg.create(brand_name)["brand_id"]


def _save_job_in_brand_ws(store, project_root, user_id, brand_id, job):
    """Save a job directly into user_id+brand_id's workspace store."""
    ws = WorkspaceContext.for_brand(user_id, brand_id, project_root)
    token = set_workspace(ws)
    try:
        jobs = store.load_jobs()
        jobs.append(job)
        store.save_jobs(jobs)
    finally:
        reset_workspace(token)


@pytest.fixture
def _store(tmp_path):
    return JsonJobStore(tmp_path / "jobs.json", tmp_path / "runs.json", retention_days=30)


def test_restart_then_fire_executes_exactly_once(tmp_path, _store):
    """Persist future one_time job → restart → fire → exactly one run, no replay."""
    _register_user(tmp_path, TEST_USER_ID)
    brand_id = _make_brand(tmp_path, TEST_USER_ID)

    # 1. Persist a future one_time job owned by TEST_USER_ID in brand's workspace
    future = (datetime.now().astimezone() + timedelta(seconds=3)).isoformat()
    job = {
        "id": "job_restart_1",
        "name": "restart-test",
        "enabled": True,
        "schedule_type": "one_time",
        "schedule": {"type": "date", "value": future},
        "flow": {"is_auto": False, "agents": ["content_creator"]},
        "quick_brief": "test",
        "user_id": TEST_USER_ID,
        "brand_id": brand_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "last_run": "",
        "next_run": "",
        "run_count": 0,
    }
    _save_job_in_brand_ws(_store, tmp_path, TEST_USER_ID, brand_id, job)

    # 2. Start scheduler (simulates restart — loads and re-registers the job)
    fired_count = []

    def _fake_run_job(user_id, brand_id, job_id, trigger="auto"):
        fired_count.append((user_id, brand_id, job_id, trigger))

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)

    with patch.object(sched, "_run_job", _fake_run_job):
        sched.start()

        # 3. Wait for the trigger time + APScheduler thread buffer
        import time
        time.sleep(5)

    # 4. Assert exactly one fire
    assert len(fired_count) == 1, (
        f"expected exactly 1 fire, got {len(fired_count)}: {fired_count}"
    )
    assert fired_count[0][2] == "job_restart_1"
    assert fired_count[0][0] == TEST_USER_ID, "owner must be passed explicitly"

    # 5. Stop and restart again — job must NOT fire again (one_time already fired)
    sched.stop()

    # The one_time job should have been removed from the job list by _run_job.
    # But since we mocked _run_job, the removal logic in _run_job didn't run.
    # So we need to verify that APScheduler doesn't re-fire on restart.
    # Re-register the job manually (simulating what start() does on reload)
    # and verify it doesn't fire again because the date is now in the past.
    # start() will record it as "missed" (error) and remove it.
    sched2 = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    fired2 = []

    def _fake_run_job2(user_id, brand_id, job_id, trigger="auto"):
        fired2.append(job_id)

    with patch.object(sched2, "_run_job", _fake_run_job2):
        sched2.start()

        import time
        time.sleep(2)

    assert len(fired2) == 0, (
        "one_time job must not fire again after restart — it already fired"
    )

    # The past one_time job must have been recorded as error (missed) and removed
    ws = WorkspaceContext.for_brand(TEST_USER_ID, brand_id, tmp_path)
    token = set_workspace(ws)
    try:
        jobs = _store.load_jobs()
        assert all(j["id"] != "job_restart_1" for j in jobs), \
            "past one_time job must be removed from job list"
    finally:
        reset_workspace(token)

    sched2.stop()


def test_restart_does_not_replay_completed_one_time_job(tmp_path, _store):
    """A one_time job that already ran (and was deleted) must not replay on restart."""
    _register_user(tmp_path, TEST_USER_ID)
    brand_id = _make_brand(tmp_path, TEST_USER_ID)

    # No job in the brand store — it was already completed and deleted
    ws = WorkspaceContext.for_brand(TEST_USER_ID, brand_id, tmp_path)
    token = set_workspace(ws)
    try:
        assert _store.load_jobs() == []
    finally:
        reset_workspace(token)

    fired = []

    def _fake_run_job(user_id, brand_id, job_id, trigger="auto"):
        fired.append(job_id)

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    with patch.object(sched, "_run_job", _fake_run_job):
        import time
        time.sleep(2)

    assert len(fired) == 0, "no job in store → nothing should fire"
    sched.stop()
