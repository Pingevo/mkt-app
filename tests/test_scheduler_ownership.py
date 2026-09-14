"""Scheduler ownership tests — a user may only mutate their own jobs.

Proves:
- User A can toggle/remove/run their own job
- User B cannot toggle/remove/run A's job even when B knows the job_id
- Jobs without user_id (legacy) are unaffected when no user_id is passed

MB-02: scheduler state is brand-scoped.  Tests establish a brand workspace
context before scheduler operations.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.scheduler import JsonJobStore, Scheduler
from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
from src.brand_registry import BrandRegistry


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


def _brand_ctx(scheduler, user_id, brand_name="TestBrand"):
    """Create or reuse a brand for user_id and set a brand workspace context.

    Returns (user_id, brand_id, token).  Caller must reset_workspace(token)
    when done.
    """
    _project_root = Path(scheduler._project_root)
    reg = BrandRegistry(user_id=user_id, project_root=_project_root)
    brands = reg.list()
    if brands:
        brand_id = brands[0]["brand_id"]
    else:
        brand = reg.create(brand_name)
        brand_id = brand["brand_id"]
    ws = WorkspaceContext.for_brand(user_id, brand_id, _project_root)
    token = set_workspace(ws)
    return user_id, brand_id, token


def _add_job_for_brand(scheduler, user_id, brand_id, job_name="test-job"):
    """Add a job within the current brand workspace context."""
    job_spec = {
        "name": job_name,
        "enabled": False,
        "schedule_type": "one_time",
        "schedule": {},
        "flow": {"is_auto": False, "agents": ["product_spec"], "folders": ["Test"], "content_count": 1},
        "quick_brief": "",
        "user_id": user_id,
        "brand_id": brand_id,
    }
    return scheduler.add_job(job_spec)


# --- remove_job ownership ---

def test_owner_can_remove_own_job(_scheduler, _store):
    uid, bid, token = _brand_ctx(_scheduler, "user_aaa")
    try:
        jid = _add_job_for_brand(_scheduler, uid, bid)
        assert _scheduler.remove_job(jid, user_id=uid) is True
        assert _store.load_jobs() == []
    finally:
        reset_workspace(token)


def test_non_owner_cannot_remove_job(_scheduler, _store):
    uid_a, bid_a, token_a = _brand_ctx(_scheduler, "user_aaa")
    try:
        jid = _add_job_for_brand(_scheduler, uid_a, bid_a)
    finally:
        reset_workspace(token_a)

    uid_b, bid_b, token_b = _brand_ctx(_scheduler, "user_bbb")
    try:
        # User B knows the job_id but does not own it
        assert _scheduler.remove_job(jid, user_id=uid_b) is False
        # Job must still exist in A's store
    finally:
        reset_workspace(token_b)

    token_a2 = _brand_ctx(_scheduler, "user_aaa")[2]
    try:
        jobs = _store.load_jobs()
        assert len(jobs) == 1
        assert jobs[0]["id"] == jid
    finally:
        reset_workspace(token_a2)


def test_remove_job_without_user_id_still_works_for_legacy(_scheduler, _store):
    """Legacy callers that don't pass user_id can still remove (backward compat)."""
    uid, bid, token = _brand_ctx(_scheduler, "user_aaa")
    try:
        jid = _add_job_for_brand(_scheduler, uid, bid)
        assert _scheduler.remove_job(jid) is True
        assert _store.load_jobs() == []
    finally:
        reset_workspace(token)


# --- toggle_job ownership ---

def test_owner_can_toggle_own_job(_scheduler, _store):
    uid, bid, token = _brand_ctx(_scheduler, "user_aaa")
    try:
        jid = _add_job_for_brand(_scheduler, uid, bid)
        assert _scheduler.toggle_job(jid, enabled=True, user_id=uid) is True
        jobs = _store.load_jobs()
        assert jobs[0]["enabled"] is True
    finally:
        reset_workspace(token)


def test_non_owner_cannot_toggle_job(_scheduler, _store):
    uid_a, bid_a, token_a = _brand_ctx(_scheduler, "user_aaa")
    try:
        jid = _add_job_for_brand(_scheduler, uid_a, bid_a)
    finally:
        reset_workspace(token_a)

    uid_b, bid_b, token_b = _brand_ctx(_scheduler, "user_bbb")
    try:
        assert _scheduler.toggle_job(jid, enabled=True, user_id=uid_b) is False
    finally:
        reset_workspace(token_b)

    token_a2 = _brand_ctx(_scheduler, "user_aaa")[2]
    try:
        jobs = _store.load_jobs()
        assert jobs[0]["enabled"] is False
    finally:
        reset_workspace(token_a2)


# --- run_now ownership ---

def test_owner_can_run_own_job(_scheduler, _store):
    uid, bid, token = _brand_ctx(_scheduler, "user_aaa")
    try:
        jid = _add_job_for_brand(_scheduler, uid, bid)
        # run_now submits to executor — patch _run_job to avoid real execution
        with patch.object(_scheduler, "_run_job"):
            assert _scheduler.run_now(jid, user_id=uid) is True
    finally:
        reset_workspace(token)


def test_non_owner_cannot_run_job(_scheduler, _store):
    uid_a, bid_a, token_a = _brand_ctx(_scheduler, "user_aaa")
    try:
        jid = _add_job_for_brand(_scheduler, uid_a, bid_a)
    finally:
        reset_workspace(token_a)

    uid_b, bid_b, token_b = _brand_ctx(_scheduler, "user_bbb")
    try:
        with patch.object(_scheduler, "_run_job"):
            assert _scheduler.run_now(jid, user_id=uid_b) is False
    finally:
        reset_workspace(token_b)


# --- non-existent job ---

def test_remove_nonexistent_job_returns_false(_scheduler):
    uid, bid, token = _brand_ctx(_scheduler, "user_aaa")
    try:
        assert _scheduler.remove_job("nonexistent", user_id=uid) is False
    finally:
        reset_workspace(token)


def test_toggle_nonexistent_job_returns_false(_scheduler):
    uid, bid, token = _brand_ctx(_scheduler, "user_aaa")
    try:
        assert _scheduler.toggle_job("nonexistent", enabled=True, user_id=uid) is False
    finally:
        reset_workspace(token)


# --- running status isolation ---

def test_running_status_isolated_by_user(_scheduler):
    """User B must not see User A's running status."""
    from src.scheduler import _aps_id
    uid_a, bid_a, _ = _brand_ctx(_scheduler, "user_aaa")
    aps_id = _aps_id(uid_a, bid_a, "job_running_a")
    with _scheduler._lock:
        _scheduler._running_status[aps_id] = {
            "status": "running", "message": "working", "agent": "x", "started_at": "ts",
        }
    # User A sees their job
    status_a = _scheduler.get_running_status(user_id=uid_a)
    assert "job_running_a" in status_a
    # User B does NOT see User A's job
    status_b = _scheduler.get_running_status(user_id="user_bbb")
    assert "job_running_a" not in status_b
    assert len(status_b) == 0


# --- rerun ownership ---

def test_rerun_cross_user_blocked(_scheduler, _store):
    """User B cannot rerun User A's run even if B knows the job_id and started_at."""
    _project_root = Path(_scheduler._project_root)

    # User A creates a run record under A's brand
    uid_a, bid_a, token_a = _brand_ctx(_scheduler, "user_aaa")
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
            "user_id": uid_a,
            "brand_id": bid_a,
        })
    finally:
        reset_workspace(token_a)

    # User B tries to rerun A's run (B's brand store has no such run)
    uid_b, bid_b, token_b = _brand_ctx(_scheduler, "user_bbb")
    try:
        assert _scheduler.rerun_run("job_a1", "2026-09-11T10:00:00+07:00", user_id=uid_b) is False
    finally:
        reset_workspace(token_b)

    # User A can rerun their own run
    token_a2 = _brand_ctx(_scheduler, "user_aaa")[2]
    try:
        with patch.object(_scheduler, "_rerun_from_record"):
            assert _scheduler.rerun_run("job_a1", "2026-09-11T10:00:00+07:00", user_id=uid_a) is True
    finally:
        reset_workspace(token_a2)


# --- same public job_id independently controllable ---

def test_same_job_id_independently_controllable(_scheduler, _store):
    """Toggle/remove User A's job must not affect User B's job with the same public job_id."""
    _project_root = Path(_scheduler._project_root)

    shared_jid = "job_shared_xyz"
    for uid in ["user_aaa", "user_bbb"]:
        reg = BrandRegistry(user_id=uid, project_root=_project_root)
        brand = reg.create(f"Brand_{uid}")
        ws = WorkspaceContext.for_brand(uid, brand["brand_id"], _project_root)
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
                "brand_id": brand["brand_id"],
                "created_at": "2026-09-11T00:00:00+07:00",
                "last_run": "", "next_run": "", "run_count": 0,
            }])
        finally:
            reset_workspace(token)

    # User A removes their job
    reg_a = BrandRegistry(user_id="user_aaa", project_root=_project_root)
    bid_a = reg_a.list()[0]["brand_id"]
    ws = WorkspaceContext.for_brand("user_aaa", bid_a, _project_root)
    token = set_workspace(ws)
    try:
        assert _scheduler.remove_job(shared_jid, user_id="user_aaa") is True
        a_jobs = _store.load_jobs()
        assert all(j["id"] != shared_jid for j in a_jobs), "A's job removed"
    finally:
        reset_workspace(token)

    # User B's job must still exist
    reg_b = BrandRegistry(user_id="user_bbb", project_root=_project_root)
    bid_b = reg_b.list()[0]["brand_id"]
    ws = WorkspaceContext.for_brand("user_bbb", bid_b, _project_root)
    token = set_workspace(ws)
    try:
        b_jobs = _store.load_jobs()
        assert len(b_jobs) == 1
        assert b_jobs[0]["id"] == shared_jid
        assert b_jobs[0]["user_id"] == "user_bbb"
    finally:
        reset_workspace(token)
