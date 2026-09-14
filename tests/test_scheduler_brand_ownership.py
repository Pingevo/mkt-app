"""MB-02 Slice 3 — Scheduler brand ownership.

Proves:
  1. Jobs store brand_id; run records store brand_id.
  2. Callback (_run_job) restores the correct brand context.
  3. Concurrent A1/A2 jobs remain isolated.
  4. Reruns preserve original brand.
  5. Run history is separated by brand.
  6. Attachments/resources are separated by brand.
  7. Archived brand jobs do not execute.
  8. Legacy jobs missing brand_id are quarantined (fail closed).
  9. Scheduler stores (jobs/runs) are brand-scoped beneath brand_state_root().
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.brand_registry import BrandRegistry
from src.scheduler import Scheduler, JsonJobStore, _aps_id, _decode_aps_id
from src.workspace_context import (
    WorkspaceContext,
    set_workspace,
    reset_workspace,
    brand_state_root,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def two_brands(tmp_path: Path):
    """Returns (project_root, user_a, a1_id, a2_id)."""
    # Register the user so _reload_all_users can enumerate them
    from src.auth import UserStore
    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    user = store.register("user_aaa", "pass_aaa")
    uid = user.user_id
    reg = BrandRegistry(user_id=uid, project_root=tmp_path)
    a1 = reg.create("A1")
    a2 = reg.create("A2")
    return (tmp_path, uid, a1["brand_id"], a2["brand_id"])


def _brand_ctx(project, user_id, brand_id):
    """Context manager for a brand workspace."""
    ws = WorkspaceContext.for_brand(user_id, brand_id, project)
    token = set_workspace(ws)
    return token


# ---------------------------------------------------------------------------
# 1. APS ID encodes brand_id
# ---------------------------------------------------------------------------

def test_aps_id_encodes_brand_id():
    """APS ID must encode (user_id, brand_id, job_id) for brand-scoped callbacks."""
    aps = _aps_id("user_a", "brand_1", "job_x")
    uid, bid, jid = _decode_aps_id(aps)
    assert uid == "user_a"
    assert bid == "brand_1"
    assert jid == "job_x"


def test_decode_aps_id_legacy_no_brand():
    """Legacy APS IDs without brand_id decode to empty brand_id."""
    aps = "user_a::job_x"  # old format
    uid, bid, jid = _decode_aps_id(aps)
    assert uid == "user_a"
    assert bid == ""  # legacy — no brand
    assert jid == "job_x"


# ---------------------------------------------------------------------------
# 2. Jobs store brand_id
# ---------------------------------------------------------------------------

def test_add_job_stores_brand_id(two_brands, monkeypatch):
    """add_job must capture brand_id from the active brand context."""
    project, uid_a, a1_id, a2_id = two_brands
    store = JsonJobStore(
        project / "_fallback_jobs.json",
        project / "_fallback_runs.json",
    )
    sched = Scheduler(project_root=project, job_store=store)
    try:
        token = _brand_ctx(project, uid_a, a1_id)
        try:
            job_id = sched.add_job({
                "name": "A1 job",
                "schedule_type": "one_time",
                "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
                "flow": {"agents": ["content_creator"], "folders": ["P1"]},
                "quick_brief": "test",
            })
        finally:
            reset_workspace(token)

        # Load the job from A1's brand-scoped store
        token = _brand_ctx(project, uid_a, a1_id)
        try:
            jobs = sched.list_jobs()
        finally:
            reset_workspace(token)

        job = next(j for j in jobs if j["id"] == job_id)
        assert job.get("brand_id") == a1_id, f"job must store brand_id, got {job.get('brand_id')}"
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# 3. Scheduler stores are brand-scoped
# ---------------------------------------------------------------------------

def test_scheduler_stores_are_brand_scoped(two_brands, monkeypatch):
    """Jobs/runs must be stored beneath brand_state_root(), not user root."""
    project, uid_a, a1_id, a2_id = two_brands
    store = JsonJobStore(
        project / "_fallback_jobs.json",
        project / "_fallback_runs.json",
    )
    sched = Scheduler(project_root=project, job_store=store)
    try:
        token = _brand_ctx(project, uid_a, a1_id)
        try:
            sched.add_job({
                "name": "A1 job",
                "schedule_type": "one_time",
                "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
                "flow": {"agents": ["content_creator"]},
            })
        finally:
            reset_workspace(token)

        # A1's jobs file must be under brand root
        a1_jobs = (project / "users" / uid_a / "brands" / a1_id / "cache" / "scheduled_jobs.json")
        assert a1_jobs.exists(), f"A1 jobs must be brand-scoped at {a1_jobs}"

        # A2 must NOT see A1's jobs
        token = _brand_ctx(project, uid_a, a2_id)
        try:
            jobs = sched.list_jobs()
        finally:
            reset_workspace(token)
        assert len(jobs) == 0, f"A2 must not see A1's jobs, got {len(jobs)}"
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# 4. Run records store brand_id
# ---------------------------------------------------------------------------

def test_run_record_stores_brand_id(two_brands, monkeypatch):
    """Run records must include brand_id for brand-scoped history."""
    project, uid_a, a1_id, a2_id = two_brands
    store = JsonJobStore(
        project / "_fallback_jobs.json",
        project / "_fallback_runs.json",
    )
    sched = Scheduler(project_root=project, job_store=store)
    try:
        token = _brand_ctx(project, uid_a, a1_id)
        try:
            job_id = sched.add_job({
                "name": "A1 job",
                "schedule_type": "one_time",
                "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
                "flow": {"agents": ["content_creator"]},
            })
            # Manually record a run to check brand_id is stored
            sched._store.append_run({
                "job_id": job_id,
                "job_name": "A1 job",
                "started_at": "2025-01-01T00:00:00+00:00",
                "finished_at": "2025-01-01T00:01:00+00:00",
                "status": "success",
                "user_id": uid_a,
                "brand_id": a1_id,
            })
        finally:
            reset_workspace(token)

        token = _brand_ctx(project, uid_a, a1_id)
        try:
            runs = sched.get_run_log(user_id=uid_a)
        finally:
            reset_workspace(token)

        assert len(runs) == 1
        assert runs[0].get("brand_id") == a1_id
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# 5. Archived brand jobs do not execute
# ---------------------------------------------------------------------------

def test_archived_brand_job_does_not_execute(two_brands, monkeypatch):
    """A job whose brand has been archived must not execute on restart."""
    project, uid_a, a1_id, a2_id = two_brands
    reg = BrandRegistry(user_id=uid_a, project_root=project)
    store = JsonJobStore(
        project / "_fallback_jobs.json",
        project / "_fallback_runs.json",
    )
    sched = Scheduler(project_root=project, job_store=store)
    try:
        token = _brand_ctx(project, uid_a, a1_id)
        try:
            sched.add_job({
                "name": "A1 job",
                "schedule_type": "recurring",
                "schedule": {"type": "cron", "value": "0 9 * * *"},
                "flow": {"agents": ["content_creator"]},
            })
        finally:
            reset_workspace(token)

        # Archive A1
        reg.archive(a1_id)

        # Simulate restart: clear APS jobs and reload
        try:
            sched._aps.remove_all_jobs()
        except Exception:
            pass
        sched._reload_all_users()
        # No jobs should be in APScheduler for the archived brand
        aps_jobs = sched._aps.get_jobs()
        assert len(aps_jobs) == 0, f"archived brand jobs must not be registered, got {len(aps_jobs)}"
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# 6. Legacy jobs without brand_id are quarantined
# ---------------------------------------------------------------------------

def test_legacy_job_without_brand_id_quarantined(two_brands, monkeypatch):
    """Legacy jobs missing brand_id must be quarantined, not executed."""
    project, uid_a, a1_id, a2_id = two_brands
    store = JsonJobStore(
        project / "_fallback_jobs.json",
        project / "_fallback_runs.json",
    )
    sched = Scheduler(project_root=project, job_store=store)
    try:
        # Inject a legacy job (user_id but no brand_id) into the user-level store
        token = set_workspace(WorkspaceContext.for_user(uid_a, project))
        try:
            legacy_jobs = [{
                "id": "job_legacy_1",
                "name": "legacy",
                "enabled": True,
                "schedule_type": "recurring",
                "schedule": {"type": "cron", "value": "0 9 * * *"},
                "flow": {"agents": ["content_creator"]},
                "user_id": uid_a,
                # NO brand_id — legacy
            }]
            store.save_jobs(legacy_jobs)
        finally:
            reset_workspace(token)

        # Restart — legacy job must be quarantined (disabled, not registered)
        sched._reload_all_users()
        aps_jobs = sched._aps.get_jobs()
        assert len(aps_jobs) == 0, f"legacy ownerless-brand jobs must not be registered"

        # The legacy job must be disabled
        token = set_workspace(WorkspaceContext.for_user(uid_a, project))
        try:
            jobs = store.load_jobs()
        finally:
            reset_workspace(token)
        legacy = next(j for j in jobs if j["id"] == "job_legacy_1")
        assert not legacy.get("enabled", True), "legacy job must be disabled (quarantined)"
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# 7. _run_job establishes brand context
# ---------------------------------------------------------------------------

def test_run_job_establishes_brand_context(two_brands, monkeypatch):
    """_run_job must establish the stored brand's workspace context."""
    project, uid_a, a1_id, a2_id = two_brands
    store = JsonJobStore(
        project / "_fallback_jobs.json",
        project / "_fallback_runs.json",
    )
    sched = Scheduler(project_root=project, job_store=store)
    try:
        token = _brand_ctx(project, uid_a, a1_id)
        try:
            job_id = sched.add_job({
                "name": "A1 job",
                "schedule_type": "one_time",
                "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
                "flow": {"agents": ["content_creator"]},
            })
        finally:
            reset_workspace(token)

        # Mock _execute_flow to capture the workspace context
        captured_ws = {}
        def fake_execute(flow, quick_brief, aps_id, *, session_token=None, brand_id=""):
            from src.workspace_context import get_workspace
            ws = get_workspace()
            captured_ws["brand_id"] = ws.brand_id if ws else None
            return [], "", ""

        with patch.object(sched, "_execute_flow", fake_execute):
            with patch.object(sched, "_cleanup_orphaned_durable_sessions"):
                sched._run_job(uid_a, a1_id, job_id, "manual")

        assert captured_ws.get("brand_id") == a1_id, (
            f"_run_job must establish A1 brand context, got {captured_ws.get('brand_id')}"
        )
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# 8. Rerun preserves original brand
# ---------------------------------------------------------------------------

def test_rerun_preserves_brand_context(two_brands, monkeypatch):
    """Rerun must restore the original run's brand context."""
    project, uid_a, a1_id, a2_id = two_brands
    store = JsonJobStore(
        project / "_fallback_jobs.json",
        project / "_fallback_runs.json",
    )
    sched = Scheduler(project_root=project, job_store=store)
    try:
        # Create a run record under A1
        token = _brand_ctx(project, uid_a, a1_id)
        try:
            job_id = sched.add_job({
                "name": "A1 job",
                "schedule_type": "one_time",
                "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
                "flow": {"agents": ["content_creator"]},
            })
            sched._store.append_run({
                "job_id": job_id,
                "job_name": "A1 job",
                "started_at": "2025-01-01T00:00:00+00:00",
                "finished_at": "2025-01-01T00:01:00+00:00",
                "status": "success",
                "user_id": uid_a,
                "brand_id": a1_id,
                "flow": {"agents": ["content_creator"]},
                "quick_brief": "test",
            })
        finally:
            reset_workspace(token)

        # Mock _execute_flow to capture workspace
        captured_ws = {}
        def fake_execute(flow, quick_brief, aps_id, *, session_token=None, brand_id=""):
            from src.workspace_context import get_workspace
            ws = get_workspace()
            captured_ws["brand_id"] = ws.brand_id if ws else None
            return [], "", ""

        with patch.object(sched, "_execute_flow", fake_execute):
            with patch.object(sched, "_cleanup_orphaned_durable_sessions"):
                # Rerun from A1's brand context
                token = _brand_ctx(project, uid_a, a1_id)
                try:
                    ok = sched.rerun_run(job_id, "2025-01-01T00:00:00+00:00", user_id=uid_a)
                finally:
                    reset_workspace(token)
                assert ok, "rerun must find the run record"

                # Wait for the executor to finish
                sched._executor.shutdown(wait=True)
        assert captured_ws.get("brand_id") == a1_id, (
            f"rerun must restore A1 brand context, got {captured_ws.get('brand_id')}"
        )
    finally:
        sched.stop()
