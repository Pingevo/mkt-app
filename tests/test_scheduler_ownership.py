"""Scheduler ownership tests — a user may only mutate their own jobs.

Proves:
- User A can toggle/remove/run their own job
- User B cannot toggle/remove/run A's job even when B knows the job_id
- Jobs without user_id (legacy) are unaffected when no user_id is passed
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.scheduler import JsonJobStore, Scheduler


@pytest.fixture
def _store(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    runs_path = tmp_path / "runs.json"
    return JsonJobStore(jobs_path, runs_path, max_runs=200)


@pytest.fixture
def _scheduler(_store, tmp_path):
    """Scheduler that does not start APScheduler threads."""
    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    return sched


def _add_job_for_user(scheduler, user_id, job_name="test-job"):
    """Add a job owned by user_id and return the job_id."""
    job_spec = {
        "name": job_name,
        "enabled": False,
        "schedule_type": "one_time",
        "schedule": {},
        "flow": {"is_auto": False, "agents": ["product_spec"], "folders": ["Test"], "content_count": 1},
        "quick_brief": "",
        "user_id": user_id,
    }
    # add_job reads user_id from job_spec when no workspace is active
    return scheduler.add_job(job_spec)


# --- remove_job ownership ---

def test_owner_can_remove_own_job(_scheduler, _store):
    jid = _add_job_for_user(_scheduler, "user_aaa")
    assert _scheduler.remove_job(jid, user_id="user_aaa") is True
    assert _store.load_jobs() == []


def test_non_owner_cannot_remove_job(_scheduler, _store):
    jid = _add_job_for_user(_scheduler, "user_aaa")
    # User B knows the job_id but does not own it
    assert _scheduler.remove_job(jid, user_id="user_bbb") is False
    # Job must still exist
    jobs = _store.load_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == jid


def test_remove_job_without_user_id_still_works_for_legacy(_scheduler, _store):
    """Legacy callers that don't pass user_id can still remove (backward compat)."""
    jid = _add_job_for_user(_scheduler, "user_aaa")
    assert _scheduler.remove_job(jid) is True
    assert _store.load_jobs() == []


# --- toggle_job ownership ---

def test_owner_can_toggle_own_job(_scheduler, _store):
    jid = _add_job_for_user(_scheduler, "user_aaa")
    assert _scheduler.toggle_job(jid, enabled=True, user_id="user_aaa") is True
    jobs = _store.load_jobs()
    assert jobs[0]["enabled"] is True


def test_non_owner_cannot_toggle_job(_scheduler, _store):
    jid = _add_job_for_user(_scheduler, "user_aaa")
    assert _scheduler.toggle_job(jid, enabled=True, user_id="user_bbb") is False
    # Job must remain unchanged
    jobs = _store.load_jobs()
    assert jobs[0]["enabled"] is False


# --- run_now ownership ---

def test_owner_can_run_own_job(_scheduler, _store):
    jid = _add_job_for_user(_scheduler, "user_aaa")
    # run_now submits to executor — patch _run_job to avoid real execution
    with patch.object(_scheduler, "_run_job"):
        assert _scheduler.run_now(jid, user_id="user_aaa") is True


def test_non_owner_cannot_run_job(_scheduler, _store):
    jid = _add_job_for_user(_scheduler, "user_aaa")
    with patch.object(_scheduler, "_run_job"):
        assert _scheduler.run_now(jid, user_id="user_bbb") is False


# --- non-existent job ---

def test_remove_nonexistent_job_returns_false(_scheduler):
    assert _scheduler.remove_job("nonexistent", user_id="user_aaa") is False


def test_toggle_nonexistent_job_returns_false(_scheduler):
    assert _scheduler.toggle_job("nonexistent", enabled=True, user_id="user_aaa") is False


# --- running status isolation ---

def test_running_status_isolated_by_user(_scheduler):
    """User B must not see User A's running status."""
    from src.scheduler import _aps_id
    aps_id = _aps_id("user_aaa", "job_running_a")
    with _scheduler._lock:
        _scheduler._running_status[aps_id] = {
            "status": "running", "message": "working", "agent": "x", "started_at": "ts",
        }
    # User A sees their job
    status_a = _scheduler.get_running_status(user_id="user_aaa")
    assert "job_running_a" in status_a
    # User B does NOT see User A's job
    status_b = _scheduler.get_running_status(user_id="user_bbb")
    assert "job_running_a" not in status_b
    assert len(status_b) == 0


# --- rerun ownership ---

def test_rerun_cross_user_blocked(_scheduler, _store):
    """User B cannot rerun User A's run even if B knows the job_id and started_at."""
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    _project_root = Path(_scheduler._project_root)

    # User A creates a run record
    ws = WorkspaceContext.for_user("user_aaa", _project_root)
    token = set_workspace(ws)
    try:
        _store.append_run({
            "job_id": "job_a1",
            "job_name": "A job",
            "started_at": "2026-09-11T10:00:00+07:00",
            "finished_at": "2026-09-11T10:01:00+07:00",
            "status": "success",
            "flow": {"is_auto": True, "agents": ["content_creator"], "content_count": 1},
            "quick_brief": "A's brief",
            "output_files": [],
            "error": "",
            "trigger": "auto",
            "user_id": "user_aaa",
        })
    finally:
        reset_workspace(token)

    # User B tries to rerun A's run (B's workspace has no such run)
    ws = WorkspaceContext.for_user("user_bbb", _project_root)
    token = set_workspace(ws)
    try:
        assert _scheduler.rerun_run("job_a1", "2026-09-11T10:00:00+07:00", user_id="user_bbb") is False
    finally:
        reset_workspace(token)

    # User A can rerun their own run
    ws = WorkspaceContext.for_user("user_aaa", _project_root)
    token = set_workspace(ws)
    try:
        with patch.object(_scheduler, "_rerun_from_record"):
            assert _scheduler.rerun_run("job_a1", "2026-09-11T10:00:00+07:00", user_id="user_aaa") is True
    finally:
        reset_workspace(token)


# --- same public job_id independently controllable ---

def test_same_job_id_independently_controllable(_scheduler, _store):
    """Toggle/remove User A's job must not affect User B's job with the same public job_id."""
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    _project_root = Path(_scheduler._project_root)

    shared_jid = "job_shared_xyz"
    for uid in ["user_aaa", "user_bbb"]:
        ws = WorkspaceContext.for_user(uid, _project_root)
        token = set_workspace(ws)
        try:
            _store.save_jobs([{
                "id": shared_jid,
                "name": f"{uid} job",
                "enabled": True,
                "schedule_type": "one_time",
                "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
                "flow": {"is_auto": False, "agents": ["content_creator"]},
                "quick_brief": "test",
                "user_id": uid,
                "created_at": "2026-09-11T00:00:00+07:00",
                "last_run": "", "next_run": "", "run_count": 0,
            }])
        finally:
            reset_workspace(token)

    # User A removes their job
    ws = WorkspaceContext.for_user("user_aaa", _project_root)
    token = set_workspace(ws)
    try:
        assert _scheduler.remove_job(shared_jid, user_id="user_aaa") is True
        a_jobs = _store.load_jobs()
        assert all(j["id"] != shared_jid for j in a_jobs), "A's job removed"
    finally:
        reset_workspace(token)

    # User B's job must still exist
    ws = WorkspaceContext.for_user("user_bbb", _project_root)
    token = set_workspace(ws)
    try:
        b_jobs = _store.load_jobs()
        assert len(b_jobs) == 1
        assert b_jobs[0]["id"] == shared_jid
        assert b_jobs[0]["user_id"] == "user_bbb"
    finally:
        reset_workspace(token)
