"""Scheduler execution and restart ownership tests.

Proves that timed APScheduler fire, missed-job callback, run-now, and
restart/re-registration all preserve owner identity and write state
only to the owning user's brand workspace.

MB-02: scheduler state is brand-scoped.  Tests establish a brand workspace
context before scheduler operations.

No real model/web/provider calls — _execute_flow is mocked.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.scheduler import JsonJobStore, Scheduler
from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
from src.brand_registry import BrandRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register_users(tmp_path, user_ids, monkeypatch):
    """Register users with known user_ids in the auth UserStore for tmp_path.

    Uses the real root-aware seam: writes to ``tmp_path/data/auth/users.json``
    so ``Scheduler(project_root=tmp_path)`` enumerates them via
    ``get_user_store(tmp_path)`` without monkeypatching the global singleton.
    """
    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for uid in user_ids:
        records.append({
            "user_id": uid,
            "username": uid,
            "password_hash": "$2b$12$dummy",
            "created_at": "2026-09-11T00:00:00",
        })
    users_path.write_text(json.dumps(records), encoding="utf-8")


def _make_brand(project_root, user_id, brand_name="TestBrand"):
    """Create (or reuse) a brand for user_id and return its brand_id.

    Does NOT set a workspace context — callers that need one must set it
    explicitly (e.g. via ``_brand_ctx`` or ``WorkspaceContext.for_brand``).
    """
    reg = BrandRegistry(user_id=user_id, project_root=Path(project_root))
    brands = reg.list()
    if brands:
        return brands[0]["brand_id"]
    brand = reg.create(brand_name)
    return brand["brand_id"]


def _save_job_in_brand_ws(store, project_root, user_id, brand_id, job_id,
                          enabled=True, schedule_value="2099-01-01T09:00:00"):
    """Save a job directly into user_id/brand_id's brand workspace store."""
    ws = WorkspaceContext.for_brand(user_id, brand_id, Path(project_root))
    token = set_workspace(ws)
    try:
        jobs = store.load_jobs()
        jobs.append({
            "id": job_id,
            "name": f"{user_id} job",
            "enabled": enabled,
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": schedule_value},
            "flow": {"is_auto": False, "agents": ["content_creator"]},
            "quick_brief": "test",
            "user_id": user_id,
            "brand_id": brand_id,
            "created_at": datetime.now().astimezone().isoformat(),
            "last_run": "", "next_run": "", "run_count": 0,
        })
        store.save_jobs(jobs)
    finally:
        reset_workspace(token)


@pytest.fixture
def _store(tmp_path):
    return JsonJobStore(tmp_path / "fallback_jobs.json", tmp_path / "fallback_runs.json",
                        max_runs=200)


@pytest.fixture
def _scheduler(_store, tmp_path):
    """Scheduler that does not start APScheduler threads."""
    return Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)


def _fake_execute_ok(self, flow, quick_brief, job_id_for_status, *, session_token=None,
                     brand_id=""):
    return (["/tmp/out.md"], "ts1", "")


# ---------------------------------------------------------------------------
# 1. APS ID collision — same public job_id for two users
# ---------------------------------------------------------------------------

def test_identical_job_id_no_aps_collision(tmp_path, _store, monkeypatch):
    """User A and User B can use identical public job_id without APScheduler collision."""
    _register_users(tmp_path, ["user_aaa", "user_bbb"], monkeypatch)
    bid_a = _make_brand(tmp_path, "user_aaa")
    bid_b = _make_brand(tmp_path, "user_bbb")

    shared_jid = "job_collide1"
    _save_job_in_brand_ws(_store, tmp_path, "user_aaa", bid_a, shared_jid)
    _save_job_in_brand_ws(_store, tmp_path, "user_bbb", bid_b, shared_jid)

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    from src.scheduler import _aps_id
    assert sched._aps.get_job(_aps_id("user_aaa", bid_a, shared_jid)) is not None, \
        "User A's job must be in APScheduler"
    assert sched._aps.get_job(_aps_id("user_bbb", bid_b, shared_jid)) is not None, \
        "User B's job must be in APScheduler"

    sched.stop()


# ---------------------------------------------------------------------------
# 2. Timed fire finds and executes User A's job from background thread
# ---------------------------------------------------------------------------

def test_timed_fire_finds_user_job(tmp_path, _store, monkeypatch):
    """Timed APScheduler fire can find and execute User A's job from a background thread."""
    _register_users(tmp_path, ["user_aaa"], monkeypatch)
    bid_a = _make_brand(tmp_path, "user_aaa")

    future = (datetime.now().astimezone() + timedelta(seconds=2)).isoformat()
    _save_job_in_brand_ws(_store, tmp_path, "user_aaa", bid_a, "job_fire1",
                          schedule_value=future)

    fired = []

    def _capture_run(user_id, brand_id, job_id, trigger="auto"):
        fired.append((user_id, brand_id, job_id, trigger))

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    with patch.object(sched, "_run_job", _capture_run):
        sched.start()
        import time
        time.sleep(5)

    assert len(fired) == 1, f"expected 1 fire, got {fired}"
    assert fired[0][0] == "user_aaa", "timed fire must carry owner identity"
    assert fired[0][1] == bid_a, "timed fire must carry brand identity"
    assert fired[0][2] == "job_fire1"

    sched.stop()


# ---------------------------------------------------------------------------
# 3. Timed fire writes run history only in User A workspace
# ---------------------------------------------------------------------------

def test_timed_fire_writes_to_user_workspace(tmp_path, _store, monkeypatch):
    """Timed fire writes run history/status only in User A's brand workspace, not User B's."""
    _register_users(tmp_path, ["user_aaa", "user_bbb"], monkeypatch)
    bid_a = _make_brand(tmp_path, "user_aaa")
    bid_b = _make_brand(tmp_path, "user_bbb")

    _save_job_in_brand_ws(_store, tmp_path, "user_aaa", bid_a, "job_hist1",
                          schedule_value="2099-01-01T09:00:00")

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    # Fire manually via _run_job (workspace is set inside _run_job)
    with patch.object(Scheduler, "_execute_flow", _fake_execute_ok):
        sched._run_job("user_aaa", bid_a, "job_hist1", trigger="auto")

    # User A's brand workspace has the run record
    ws_a = WorkspaceContext.for_brand("user_aaa", bid_a, tmp_path)
    token_a = set_workspace(ws_a)
    try:
        runs_a = _store.load_runs()
        assert len(runs_a) == 1, "User A must have 1 run record"
        assert runs_a[0]["job_id"] == "job_hist1"
        assert runs_a[0]["user_id"] == "user_aaa"
        assert runs_a[0]["brand_id"] == bid_a
    finally:
        reset_workspace(token_a)

    # User B's brand workspace has no run records
    ws_b = WorkspaceContext.for_brand("user_bbb", bid_b, tmp_path)
    token_b = set_workspace(ws_b)
    try:
        runs_b = _store.load_runs()
        assert len(runs_b) == 0, "User B must have 0 run records"
    finally:
        reset_workspace(token_b)

    sched.stop()


# ---------------------------------------------------------------------------
# 4. _on_job_missed preserves owner
# ---------------------------------------------------------------------------

def test_on_job_missed_preserves_owner(tmp_path, _store, monkeypatch):
    """_on_job_missed recovers owner from APS ID and records in that user's brand workspace."""
    _register_users(tmp_path, ["user_aaa", "user_bbb"], monkeypatch)
    bid_a = _make_brand(tmp_path, "user_aaa")
    bid_b = _make_brand(tmp_path, "user_bbb")

    _save_job_in_brand_ws(_store, tmp_path, "user_aaa", bid_a, "job_miss1",
                          schedule_value="2099-01-01T09:00:00")

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    from src.scheduler import _aps_id
    event = MagicMock()
    event.job_id = _aps_id("user_aaa", bid_a, "job_miss1")

    sched._on_job_missed(event)

    # User A's brand workspace has the missed run record
    ws_a = WorkspaceContext.for_brand("user_aaa", bid_a, tmp_path)
    token_a = set_workspace(ws_a)
    try:
        runs_a = _store.load_runs()
        assert len(runs_a) == 1, "User A must have 1 missed record"
        assert runs_a[0]["status"] == "error"
        assert runs_a[0]["job_id"] == "job_miss1"
    finally:
        reset_workspace(token_a)

    # User B's brand workspace has nothing
    ws_b = WorkspaceContext.for_brand("user_bbb", bid_b, tmp_path)
    token_b = set_workspace(ws_b)
    try:
        runs_b = _store.load_runs()
        assert len(runs_b) == 0
    finally:
        reset_workspace(token_b)

    sched.stop()


def test_on_job_missed_malformed_aps_id_fails_closed(tmp_path, _store, monkeypatch):
    """Malformed/unrecognized APS IDs must fail closed — no fallback to global state."""
    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    event = MagicMock()
    event.job_id = "garbage_no_separator"

    sched._on_job_missed(event)

    # No run record should be created in the fallback store
    assert len(_store.load_runs()) == 0

    sched.stop()


# ---------------------------------------------------------------------------
# 5. Restart reloads enabled jobs for multiple registered users
# ---------------------------------------------------------------------------

def test_restart_reloads_multiple_users(tmp_path, _store, monkeypatch):
    """Restart reloads enabled jobs for multiple registered users' brands."""
    _register_users(tmp_path, ["user_aaa", "user_bbb"], monkeypatch)
    bid_a = _make_brand(tmp_path, "user_aaa")
    bid_b = _make_brand(tmp_path, "user_bbb")

    _save_job_in_brand_ws(_store, tmp_path, "user_aaa", bid_a, "job_r1",
                          schedule_value="2099-01-01T09:00:00")
    _save_job_in_brand_ws(_store, tmp_path, "user_bbb", bid_b, "job_r2",
                          schedule_value="2099-01-01T10:00:00")

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    from src.scheduler import _aps_id
    assert sched._aps.get_job(_aps_id("user_aaa", bid_a, "job_r1")) is not None, \
        "User A's job must be reloaded"
    assert sched._aps.get_job(_aps_id("user_bbb", bid_b, "job_r2")) is not None, \
        "User B's job must be reloaded"

    sched.stop()


# ---------------------------------------------------------------------------
# 6. Restart does not load from unregistered users/ directory
# ---------------------------------------------------------------------------

def test_restart_ignores_unregistered_user_dir(tmp_path, _store, monkeypatch):
    """Restart must not load jobs from an arbitrary unregistered user's brand directory."""
    _register_users(tmp_path, ["user_aaa"], monkeypatch)

    # Create an unregistered user's brand + job (BrandRegistry does not require
    # the user to be in UserStore, but the scheduler enumerates only registered
    # users, so this brand is never touched).
    bid_hacker = _make_brand(tmp_path, "user_hacker")
    _save_job_in_brand_ws(_store, tmp_path, "user_hacker", bid_hacker, "job_hack1",
                          schedule_value="2099-01-01T09:00:00")

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    from src.scheduler import _aps_id
    # The hacker's job must NOT be in APScheduler
    assert sched._aps.get_job(_aps_id("user_hacker", bid_hacker, "job_hack1")) is None, \
        "Unregistered user's job must not be loaded"

    sched.stop()


# ---------------------------------------------------------------------------
# 7. Restart cleanup executes separately per user/brand
# ---------------------------------------------------------------------------

def test_restart_cleanup_per_user(tmp_path, _store, monkeypatch):
    """Restart cleanup (stuck running) executes separately per brand workspace."""
    _register_users(tmp_path, ["user_aaa", "user_bbb"], monkeypatch)
    bid_a = _make_brand(tmp_path, "user_aaa")
    bid_b = _make_brand(tmp_path, "user_bbb")

    # Save stuck runs for both users in their brand workspaces
    for uid, bid, jid in [("user_aaa", bid_a, "job_stuck_a"),
                          ("user_bbb", bid_b, "job_stuck_b")]:
        ws = WorkspaceContext.for_brand(uid, bid, tmp_path)
        token = set_workspace(ws)
        try:
            _store.append_run({
                "job_id": jid, "job_name": "stuck",
                "started_at": "2026-09-11T10:00:00+07:00",
                "finished_at": "", "status": "running",
                "flow": {}, "quick_brief": "", "output_files": [],
                "error": "", "trigger": "auto", "user_id": uid,
                "brand_id": bid,
            })
        finally:
            reset_workspace(token)

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    # Both users' stuck runs should be marked as error
    for uid, bid in [("user_aaa", bid_a), ("user_bbb", bid_b)]:
        ws = WorkspaceContext.for_brand(uid, bid, tmp_path)
        token = set_workspace(ws)
        try:
            runs = _store.load_runs()
            assert len(runs) == 1, f"{uid} must have 1 run"
            assert runs[0]["status"] == "error", f"{uid}'s stuck run must be marked error"
        finally:
            reset_workspace(token)

    sched.stop()


# ---------------------------------------------------------------------------
# 8. Run-now preserves owner
# ---------------------------------------------------------------------------

def test_run_now_preserves_owner(tmp_path, _store, monkeypatch):
    """run_now passes owner + brand identity to _run_job."""
    _register_users(tmp_path, ["user_aaa"], monkeypatch)
    bid_a = _make_brand(tmp_path, "user_aaa")

    _save_job_in_brand_ws(_store, tmp_path, "user_aaa", bid_a, "job_run1",
                          schedule_value="2099-01-01T09:00:00")

    fired = []

    def _capture_run(user_id, brand_id, job_id, trigger="auto"):
        fired.append((user_id, brand_id, job_id, trigger))

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    ws = WorkspaceContext.for_brand("user_aaa", bid_a, tmp_path)
    token = set_workspace(ws)
    try:
        with patch.object(sched, "_run_job", _capture_run):
            assert sched.run_now("job_run1", user_id="user_aaa") is True
    finally:
        reset_workspace(token)

    sched._executor.shutdown(wait=True)
    assert len(fired) == 1
    assert fired[0][0] == "user_aaa", "run_now must pass owner to _run_job"
    assert fired[0][1] == bid_a, "run_now must pass brand_id to _run_job"
    assert fired[0][2] == "job_run1"

    sched.stop()


# ---------------------------------------------------------------------------
# 9. Rerun preserves owner and blocks cross-user
# ---------------------------------------------------------------------------

def test_rerun_preserves_owner_workspace(tmp_path, _store, monkeypatch):
    """Rerun executes inside the original owning user's brand workspace."""
    _register_users(tmp_path, ["user_aaa", "user_bbb"], monkeypatch)
    bid_a = _make_brand(tmp_path, "user_aaa")
    bid_b = _make_brand(tmp_path, "user_bbb")

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    # User A has a run record in A's brand store
    ws = WorkspaceContext.for_brand("user_aaa", bid_a, tmp_path)
    token = set_workspace(ws)
    try:
        _store.append_run({
            "job_id": "job_re1", "job_name": "A run",
            "started_at": "2026-09-11T10:00:00+07:00",
            "finished_at": "2026-09-11T10:01:00+07:00",
            "status": "success",
            "flow": {"is_auto": True, "agents": ["content_creator"], "content_count": 1},
            "quick_brief": "A brief",
            "output_files": [], "error": "", "trigger": "auto",
            "user_id": "user_aaa", "brand_id": bid_a,
        })
    finally:
        reset_workspace(token)

    # User B cannot rerun (B's brand store has no such run)
    ws = WorkspaceContext.for_brand("user_bbb", bid_b, tmp_path)
    token = set_workspace(ws)
    try:
        assert sched.rerun_run("job_re1", "2026-09-11T10:00:00+07:00", user_id="user_bbb") is False
    finally:
        reset_workspace(token)

    # User A can rerun — executes in A's brand workspace
    ws = WorkspaceContext.for_brand("user_aaa", bid_a, tmp_path)
    token = set_workspace(ws)
    try:
        with patch.object(Scheduler, "_execute_flow", _fake_execute_ok):
            assert sched.rerun_run("job_re1", "2026-09-11T10:00:00+07:00", user_id="user_aaa") is True
            sched._executor.shutdown(wait=True)
    finally:
        reset_workspace(token)

    # New run record in User A's brand workspace
    ws = WorkspaceContext.for_brand("user_aaa", bid_a, tmp_path)
    token = set_workspace(ws)
    try:
        runs = _store.load_runs()
        assert len(runs) == 2, "A must have original + rerun"
        rerun = [r for r in runs if r.get("trigger") == "rerun"]
        assert len(rerun) == 1
        assert rerun[0]["user_id"] == "user_aaa"
        assert rerun[0]["brand_id"] == bid_a
    finally:
        reset_workspace(token)

    # User B's brand workspace still empty
    ws = WorkspaceContext.for_brand("user_bbb", bid_b, tmp_path)
    token = set_workspace(ws)
    try:
        assert len(_store.load_runs()) == 0
    finally:
        reset_workspace(token)

    sched.stop()


# ===========================================================================
# Fix 1 — Ownerless legacy jobs must never execute
# ===========================================================================

def test_ownerless_legacy_job_registered_after_restart(tmp_path, _store, monkeypatch):
    """RED: ownerless fallback job IS currently registered into APScheduler after restart."""
    _register_users(tmp_path, [], monkeypatch)

    # Create an ownerless legacy job in the fallback store
    _store.save_jobs([{
        "id": "job_legacy_1", "name": "legacy", "enabled": True,
        "schedule_type": "one_time",
        "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
        "flow": {"is_auto": False, "agents": ["content_creator"]},
        "quick_brief": "legacy", "user_id": "", "brand_id": "",
        "created_at": "2026-09-11T00:00:00", "last_run": "", "next_run": "", "run_count": 0,
    }])

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    # RED: currently registered — after fix must be None
    aps_job = sched._aps.get_job("job_legacy_1")
    assert aps_job is None, "ownerless legacy job must NOT be registered in APScheduler"

    sched.stop()


def test_run_job_empty_user_id_fails_closed(tmp_path, _store, monkeypatch):
    """_run_job with empty user_id must fail closed — never call _execute_flow."""
    _register_users(tmp_path, [], monkeypatch)

    # Save the job directly (bypass start() which would quarantine it)
    _store.save_jobs([{
        "id": "job_legacy_2", "name": "legacy", "enabled": True,
        "schedule_type": "one_time",
        "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
        "flow": {"is_auto": False, "agents": ["content_creator"]},
        "quick_brief": "legacy", "user_id": "", "brand_id": "",
        "created_at": "2026-09-11T00:00:00", "last_run": "", "next_run": "", "run_count": 0,
    }])

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)

    execute_called = []
    def _capture_execute(*args, **kwargs):
        execute_called.append(True)
        return ([], "ts", "")

    with patch.object(Scheduler, "_execute_flow", _capture_execute):
        sched._run_job("", "", "job_legacy_2", trigger="auto")

    assert len(execute_called) == 0, "_run_job with empty user_id must NOT call _execute_flow"
    # No run record should be created by _run_job (quarantine is separate)
    assert len(_store.load_runs()) == 0, "no run record for ownerless execution"

    sched.stop()


def test_ownerless_legacy_job_quarantined_after_restart(tmp_path, _store, monkeypatch):
    """After fix: ownerless job is disabled/quarantined, not registered, not executed."""
    _register_users(tmp_path, [], monkeypatch)

    _store.save_jobs([{
        "id": "job_legacy_3", "name": "legacy", "enabled": True,
        "schedule_type": "recurring",
        "schedule": {"type": "interval", "value": "1h"},
        "flow": {"is_auto": False, "agents": ["content_creator"]},
        "quick_brief": "legacy", "user_id": "", "brand_id": "",
        "created_at": "2026-09-11T00:00:00", "last_run": "", "next_run": "", "run_count": 0,
    }])

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    # Not registered in APScheduler
    assert sched._aps.get_job("job_legacy_3") is None, "quarantined job must not be in APScheduler"

    # Job is disabled in the store (quarantined, not removed for recurring)
    jobs = _store.load_jobs()
    assert len(jobs) == 1
    assert jobs[0]["enabled"] is False, "quarantined job must be disabled"

    sched.stop()


def test_owned_jobs_still_reload_after_legacy_quarantine(tmp_path, _store, monkeypatch):
    """Owned User A/B brand jobs still reload normally after legacy quarantine."""
    _register_users(tmp_path, ["user_aaa", "user_bbb"], monkeypatch)
    bid_a = _make_brand(tmp_path, "user_aaa")
    bid_b = _make_brand(tmp_path, "user_bbb")

    _save_job_in_brand_ws(_store, tmp_path, "user_aaa", bid_a, "job_a1",
                          schedule_value="2099-01-01T09:00:00")
    _save_job_in_brand_ws(_store, tmp_path, "user_bbb", bid_b, "job_b1",
                          schedule_value="2099-01-01T10:00:00")

    # Also add an ownerless legacy job in fallback
    _store.save_jobs([{
        "id": "job_legacy_4", "name": "legacy", "enabled": True,
        "schedule_type": "one_time",
        "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
        "flow": {"is_auto": False, "agents": ["content_creator"]},
        "quick_brief": "legacy", "user_id": "", "brand_id": "",
        "created_at": "2026-09-11T00:00:00", "last_run": "", "next_run": "", "run_count": 0,
    }])

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    from src.scheduler import _aps_id
    assert sched._aps.get_job(_aps_id("user_aaa", bid_a, "job_a1")) is not None, "A's job must reload"
    assert sched._aps.get_job(_aps_id("user_bbb", bid_b, "job_b1")) is not None, "B's job must reload"
    assert sched._aps.get_job("job_legacy_4") is None, "legacy job must NOT reload"

    sched.stop()


# ===========================================================================
# Fix 2 — Authoritative UserStore root-aware enumeration
# ===========================================================================

def test_scheduler_enumerates_users_from_its_own_project_root(tmp_path):
    """Scheduler(root_A) enumerates only root_A's registered users' brands, not root_B's."""
    import src.auth as auth_mod

    # Reset global singleton to ensure clean state
    auth_mod._user_store = None

    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"

    # Register users A1, A2 in root_a's auth store
    (root_a / "data" / "auth").mkdir(parents=True, exist_ok=True)
    (root_a / "data" / "auth" / "users.json").write_text(json.dumps([
        {"user_id": "user_a1", "username": "a1", "password_hash": "x", "created_at": "2026"},
        {"user_id": "user_a2", "username": "a2", "password_hash": "x", "created_at": "2026"},
    ]), encoding="utf-8")

    # Register user B1 in root_b's auth store
    (root_b / "data" / "auth").mkdir(parents=True, exist_ok=True)
    (root_b / "data" / "auth" / "users.json").write_text(json.dumps([
        {"user_id": "user_b1", "username": "b1", "password_hash": "x", "created_at": "2026"},
    ]), encoding="utf-8")

    # Create brands + save a job for each user in their respective brand workspaces
    store_a = JsonJobStore(root_a / "fb_jobs.json", root_a / "fb_runs.json")
    bid_a1 = _make_brand(root_a, "user_a1")
    bid_a2 = _make_brand(root_a, "user_a2")
    _save_job_in_brand_ws(store_a, root_a, "user_a1", bid_a1, "job_a1x",
                          schedule_value="2099-01-01T09:00:00")
    _save_job_in_brand_ws(store_a, root_a, "user_a2", bid_a2, "job_a2x",
                          schedule_value="2099-01-01T09:00:00")

    store_b = JsonJobStore(root_b / "fb_jobs.json", root_b / "fb_runs.json")
    bid_b1 = _make_brand(root_b, "user_b1")
    _save_job_in_brand_ws(store_b, root_b, "user_b1", bid_b1, "job_b1x",
                          schedule_value="2099-01-01T09:00:00")

    # Create a fake filesystem user dir in root_a (not registered)
    (root_a / "users" / "user_fake").mkdir(parents=True, exist_ok=True)

    # Scheduler for root_a must enumerate only A1, A2
    sched_a = Scheduler(project_root=root_a, web_port=9999, job_store=store_a)
    sched_a.start()

    from src.scheduler import _aps_id
    assert sched_a._aps.get_job(_aps_id("user_a1", bid_a1, "job_a1x")) is not None, "A1's job must reload"
    assert sched_a._aps.get_job(_aps_id("user_a2", bid_a2, "job_a2x")) is not None, "A2's job must reload"
    assert sched_a._aps.get_job(_aps_id("user_b1", bid_b1, "job_b1x")) is None, "B1's job must NOT be in root_a scheduler"
    assert sched_a._aps.get_job(_aps_id("user_fake", "brand_fake", "any")) is None, "fake user must NOT be loaded"

    sched_a.stop()

    # Scheduler for root_b must enumerate only B1
    sched_b = Scheduler(project_root=root_b, web_port=9999, job_store=store_b)
    sched_b.start()

    assert sched_b._aps.get_job(_aps_id("user_b1", bid_b1, "job_b1x")) is not None, "B1's job must reload"
    assert sched_b._aps.get_job(_aps_id("user_a1", bid_a1, "job_a1x")) is None, "A1's job must NOT be in root_b scheduler"

    sched_b.stop()


# ===========================================================================
# Fix 3 — Running status fail-closed
# ===========================================================================

def test_get_running_status_none_returns_empty(_scheduler):
    """get_running_status(user_id=None) must return {} — fail closed."""
    from src.scheduler import _aps_id
    with _scheduler._lock:
        _scheduler._running_status[_aps_id("user_aaa", "brand_x", "job_x")] = {
            "status": "running", "message": "x", "agent": "", "started_at": "ts",
        }
    assert _scheduler.get_running_status(user_id=None) == {}, "None user_id must return empty"
    assert _scheduler.get_running_status(user_id="") == {}, "empty user_id must return empty"


def test_get_running_status_per_user_isolated(_scheduler):
    """User A sees only A's status; User B sees only B's; same job_id independent."""
    from src.scheduler import _aps_id
    with _scheduler._lock:
        _scheduler._running_status[_aps_id("user_aaa", "brand_a", "job_shared")] = {
            "status": "running", "message": "A running", "agent": "a", "started_at": "ts1",
        }
        _scheduler._running_status[_aps_id("user_bbb", "brand_b", "job_shared")] = {
            "status": "running", "message": "B running", "agent": "b", "started_at": "ts2",
        }

    status_a = _scheduler.get_running_status(user_id="user_aaa")
    status_b = _scheduler.get_running_status(user_id="user_bbb")

    assert "job_shared" in status_a
    assert status_a["job_shared"]["message"] == "A running"
    assert "job_shared" in status_b
    assert status_b["job_shared"]["message"] == "B running"
    assert len(status_a) == 1
    assert len(status_b) == 1


# ===========================================================================
# Fix 4 — Run log ownership explicit
# ===========================================================================

def test_get_run_log_user_filtered(_scheduler, _store, tmp_path):
    """get_run_log returns only the requesting user's run records (brand-scoped)."""
    bid_a = _make_brand(tmp_path, "user_aaa")
    bid_b = _make_brand(tmp_path, "user_bbb")

    # User A has a run in A's brand store
    ws = WorkspaceContext.for_brand("user_aaa", bid_a, tmp_path)
    token = set_workspace(ws)
    try:
        _store.append_run({
            "job_id": "job_log_a", "job_name": "A", "started_at": "2026-09-11T10:00:00+07:00",
            "finished_at": "2026-09-11T10:01:00+07:00", "status": "success",
            "flow": {}, "quick_brief": "", "output_files": [], "error": "",
            "trigger": "auto", "user_id": "user_aaa", "brand_id": bid_a,
        })
    finally:
        reset_workspace(token)

    # User B has a run in B's brand store
    ws = WorkspaceContext.for_brand("user_bbb", bid_b, tmp_path)
    token = set_workspace(ws)
    try:
        _store.append_run({
            "job_id": "job_log_b", "job_name": "B", "started_at": "2026-09-11T11:00:00+07:00",
            "finished_at": "2026-09-11T11:01:00+07:00", "status": "success",
            "flow": {}, "quick_brief": "", "output_files": [], "error": "",
            "trigger": "auto", "user_id": "user_bbb", "brand_id": bid_b,
        })
    finally:
        reset_workspace(token)

    # User A sees only A's runs (within A's brand context)
    ws = WorkspaceContext.for_brand("user_aaa", bid_a, tmp_path)
    token = set_workspace(ws)
    try:
        runs_a = _scheduler.get_run_log(user_id="user_aaa")
        assert len(runs_a) == 1
        assert runs_a[0]["user_id"] == "user_aaa"
        assert runs_a[0]["job_id"] == "job_log_a"
    finally:
        reset_workspace(token)

    # User B sees only B's runs (within B's brand context)
    ws = WorkspaceContext.for_brand("user_bbb", bid_b, tmp_path)
    token = set_workspace(ws)
    try:
        runs_b = _scheduler.get_run_log(user_id="user_bbb")
        assert len(runs_b) == 1
        assert runs_b[0]["user_id"] == "user_bbb"
        assert runs_b[0]["job_id"] == "job_log_b"
    finally:
        reset_workspace(token)

    # No user identity → fail closed
    assert _scheduler.get_run_log(user_id=None) == []
    assert _scheduler.get_run_log(user_id="") == []


def test_get_run_log_legacy_missing_owner_not_visible(_scheduler, _store, tmp_path):
    """Legacy run record with missing user_id must not be returned to any authenticated user."""
    bid_a = _make_brand(tmp_path, "user_aaa")
    bid_b = _make_brand(tmp_path, "user_bbb")

    # An ownerless legacy run lands in A's brand store with no user_id — it must
    # be filtered out by the user_id ownership check.
    ws = WorkspaceContext.for_brand("user_aaa", bid_a, tmp_path)
    token = set_workspace(ws)
    try:
        _store.append_run({
            "job_id": "job_legacy_log", "job_name": "legacy",
            "started_at": "2026-09-11T10:00:00+07:00",
            "finished_at": "2026-09-11T10:01:00+07:00", "status": "success",
            "flow": {}, "quick_brief": "", "output_files": [], "error": "",
            "trigger": "auto", "user_id": "", "brand_id": "",
        })
    finally:
        reset_workspace(token)

    ws = WorkspaceContext.for_brand("user_aaa", bid_a, tmp_path)
    token = set_workspace(ws)
    try:
        runs_a = _scheduler.get_run_log(user_id="user_aaa")
    finally:
        reset_workspace(token)
    assert len(runs_a) == 0, "legacy ownerless run must not be visible to A"

    ws = WorkspaceContext.for_brand("user_bbb", bid_b, tmp_path)
    token = set_workspace(ws)
    try:
        runs_b = _scheduler.get_run_log(user_id="user_bbb")
    finally:
        reset_workspace(token)
    assert len(runs_b) == 0, "legacy ownerless run must not be visible to B"
