"""Scheduler attachment handling — two production defects:

1. Auto-mode scheduled jobs drop upload_session_id + resource_refs at fire time.
2. Scheduled attachments depend on a 24h temporary-resource TTL — resources
   expire before future fires, making the job fail with "missing resource refs".

Tests prove:
- Auto payload forwards upload_session_id + resource_refs (finding 1)
- add_job clones attachments to a durable (no-expiry) session (finding 2)
- Durable resources have no expires_at
- remove_job cleans up the durable session
- One-time job post-fire cleans up the durable session
- If original resources are already expired, add_job records a visible error
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.run_resources import RunResourceStore
from src.scheduler import JsonJobStore, Scheduler


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def _store(tmp_path):
    return JsonJobStore(tmp_path / "jobs.json", tmp_path / "runs.json", retention_days=30)


@pytest.fixture
def _resource_store(tmp_path):
    return RunResourceStore(
        project_root=tmp_path,
        storage_dir=tmp_path / "run_resources",
        config={"enabled": True, "ttl_hours": 24, "max_files_per_flow": 5,
                "max_file_size_mb": 15, "max_total_size_mb": 30,
                "max_extracted_chars_total": 120000,
                "max_extracted_chars_per_file": 50000,
                "allowed_extensions": [".txt", ".md", ".csv", ".pdf", ".png", ".jpg"]},
    )


def _fake_httpx_stream(lines):
    """Fake httpx context manager yielding SSE lines."""
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


def _sse_lines_ok():
    return [
        'data: {"type":"agent_done","file":"/tmp/out.md","session_ts":"ts1"}',
        'data: {"type":"done"}',
    ]


# ------------------------------------------------------------------
# Finding 1: Auto payload must forward attachments
# ------------------------------------------------------------------

class TestAutoForwardsAttachments:
    """Scheduled auto flow must forward upload_session_id + resource_refs."""

    def test_auto_payload_includes_upload_session_id_and_resource_refs(self, tmp_path, _store):
        """_execute_flow auto branch must forward upload_session_id + resource_refs."""
        sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)

        flow = {
            "is_auto": True,
            "agents": ["content_creator"],
            "content_count": 1,
            "upload_session_id": "upload_session_abc123",
            "resource_refs": ["resource:res_xyz789"],
        }

        captured = {}

        # Build the inner context manager (for client.stream) that captures the payload
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "text/event-stream"}
        resp.iter_lines = MagicMock(return_value=iter(_sse_lines_ok()))

        def _capture_stream(method, url, json=None, **kw):
            captured["url"] = url
            captured["json"] = json
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=resp)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        client = MagicMock()
        client.stream = MagicMock(side_effect=_capture_stream)
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=client)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("src.scheduler.httpx.Client", return_value=cm):
            sched._execute_flow(flow, "test brief", "job_1")

        assert "/api/run_auto" in captured["url"], "auto flow must call /api/run_auto"
        assert captured["json"].get("upload_session_id") == "upload_session_abc123", \
            "auto payload must forward upload_session_id"
        assert captured["json"].get("resource_refs") == ["resource:res_xyz789"], \
            "auto payload must forward resource_refs"


# ------------------------------------------------------------------
# Finding 2: Durable attachment cloning
# ------------------------------------------------------------------

class TestDurableAttachmentCloning:
    """add_job must clone attachments to a no-expiry session for durability."""

    def test_add_job_clones_attachments_to_durable_session(self, tmp_path, _store, _resource_store):
        """add_job with resource_refs must clone to a durable (no-expiry) session."""
        # Upload an ephemeral resource
        rec = _resource_store.upload(
            filename="brief.txt", content=b"test attachment", media_type="text/plain",
        )
        session_id = rec["scope_id"]
        resource_ref = f"resource:{rec['resource_id']}"

        sched = Scheduler(
            project_root=tmp_path, web_port=9999, job_store=_store,
            resource_store=_resource_store,
        )

        flow = {
            "is_auto": True,
            "agents": ["content_creator"],
            "content_count": 1,
            "upload_session_id": session_id,
            "resource_refs": [resource_ref],
        }

        job_id = sched.add_job({
            "name": "test-durable",
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": "2099-01-01T00:00:00"},
            "flow": flow,
            "quick_brief": "test",
        })

        jobs = _store.load_jobs()
        assert len(jobs) == 1
        job = jobs[0]

        # The job's flow must point to a DIFFERENT session (durable clone)
        durable_session = job["flow"]["upload_session_id"]
        assert durable_session != session_id, \
            "job must use a durable session, not the ephemeral one"

        # The durable resource must not expire
        durable_refs = job["flow"]["resource_refs"]
        assert len(durable_refs) == 1
        durable_res_id = durable_refs[0].split(":", 1)[1]
        durable_rec = _resource_store.get_resource(durable_res_id, durable_session)
        assert durable_rec is not None, "durable resource must be resolvable"
        assert not durable_rec.get("expires_at"), \
            "durable resource must have no expires_at (never expires)"

    def test_add_job_without_attachments_unchanged(self, tmp_path, _store, _resource_store):
        """add_job without resource_refs must not create a durable session."""
        sched = Scheduler(
            project_root=tmp_path, web_port=9999, job_store=_store,
            resource_store=_resource_store,
        )

        flow = {"is_auto": False, "agents": ["content_creator"], "content_count": 1}
        job_id = sched.add_job({
            "name": "no-attachments",
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": "2099-01-01T00:00:00"},
            "flow": flow,
            "quick_brief": "test",
        })

        job = next(j for j in _store.load_jobs() if j["id"] == job_id)
        assert not job.get("durable_session_id"), \
            "job without attachments must not have a durable session"

    def test_remove_job_cleans_up_durable_session(self, tmp_path, _store, _resource_store):
        """remove_job must delete the durable session directory."""
        rec = _resource_store.upload(
            filename="brief.txt", content=b"cleanup test", media_type="text/plain",
        )
        session_id = rec["scope_id"]
        resource_ref = f"resource:{rec['resource_id']}"

        sched = Scheduler(
            project_root=tmp_path, web_port=9999, job_store=_store,
            resource_store=_resource_store,
        )

        job_id = sched.add_job({
            "name": "test-cleanup",
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": "2099-01-01T00:00:00"},
            "flow": {"is_auto": True, "agents": ["content_creator"],
                     "upload_session_id": session_id, "resource_refs": [resource_ref]},
            "quick_brief": "test",
        })

        job = next(j for j in _store.load_jobs() if j["id"] == job_id)
        durable_session = job["flow"]["upload_session_id"]
        durable_dir = _resource_store.storage_dir / durable_session

        assert durable_dir.exists(), "durable session dir must exist after add_job"

        sched.remove_job(job_id)

        assert not durable_dir.exists(), \
            "durable session dir must be deleted after remove_job"

    def test_one_time_job_post_fire_retains_durable_session(self, tmp_path, _store, _resource_store):
        """After a one-time job fires, its durable session must be RETAINED for rerun.

        The run history record retains the flow with upload_session_id/resource_refs.
        The existing rerun feature reads that flow and calls _execute_flow again.
        Deleting the durable session immediately after fire would break rerun.
        """
        rec = _resource_store.upload(
            filename="brief.txt", content=b"fire test", media_type="text/plain",
        )
        session_id = rec["scope_id"]
        resource_ref = f"resource:{rec['resource_id']}"

        sched = Scheduler(
            project_root=tmp_path, web_port=9999, job_store=_store,
            resource_store=_resource_store,
        )

        job_id = sched.add_job({
            "name": "test-fire-retain",
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": "2099-01-01T00:00:00"},
            "flow": {"is_auto": True, "agents": ["content_creator"],
                     "upload_session_id": session_id, "resource_refs": [resource_ref]},
            "quick_brief": "test",
        })

        job = next(j for j in _store.load_jobs() if j["id"] == job_id)
        durable_session = job["flow"]["upload_session_id"]
        durable_dir = _resource_store.storage_dir / durable_session
        assert durable_dir.exists()

        # Mock execution and fire the job
        with patch("src.scheduler.httpx.Client", return_value=_fake_httpx_stream(_sse_lines_ok())):
            sched._run_job(job_id, trigger="auto")

        # One-time job must be removed from job list
        jobs = _store.load_jobs()
        assert all(j["id"] != job_id for j in jobs), \
            "one-time job must be removed after firing"

        # Durable session must STILL exist — run history retains the flow for rerun
        assert durable_dir.exists(), \
            "durable session must be retained after one-time job fires (for rerun)"

        # Run history must contain the flow with the durable session refs
        runs = _store.load_runs(job_id=job_id, limit=10)
        assert len(runs) >= 1
        run = runs[-1]
        assert run["flow"]["upload_session_id"] == durable_session
        assert len(run["flow"]["resource_refs"]) == 1

    def test_orphaned_durable_session_cleaned_after_retention_eviction(
        self, tmp_path, _resource_store,
    ):
        """When run history is evicted by retention, orphaned durable sessions are cleaned up."""
        from src.scheduler import JsonJobStore

        # Use a store with max_runs=1 so the second append evicts the first run
        store = JsonJobStore(
            tmp_path / "jobs.json", tmp_path / "runs.json", max_runs=1,
        )

        rec = _resource_store.upload(
            filename="brief.txt", content=b"retention test", media_type="text/plain",
        )
        session_id = rec["scope_id"]
        resource_ref = f"resource:{rec['resource_id']}"

        sched = Scheduler(
            project_root=tmp_path, web_port=9999, job_store=store,
            resource_store=_resource_store,
        )

        job_id = sched.add_job({
            "name": "test-retention",
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": "2099-01-01T00:00:00"},
            "flow": {"is_auto": True, "agents": ["content_creator"],
                     "upload_session_id": session_id, "resource_refs": [resource_ref]},
            "quick_brief": "test",
        })

        job = next(j for j in store.load_jobs() if j["id"] == job_id)
        durable_session = job["flow"]["upload_session_id"]
        durable_dir = _resource_store.storage_dir / durable_session
        assert durable_dir.exists()

        # Fire the job — creates run record #1
        with patch("src.scheduler.httpx.Client", return_value=_fake_httpx_stream(_sse_lines_ok())):
            sched._run_job(job_id, trigger="auto")

        # Durable session still exists (run history references it)
        assert durable_dir.exists(), \
            "durable session must exist while run history references it"

        # Now append a second run for a DIFFERENT job with no attachments.
        # max_runs=1 → this evicts the first run, orphaning the durable session.
        store.append_run({
            "job_id": "other_job",
            "job_name": "other",
            "started_at": "2099-01-01T00:00:01+00:00",
            "finished_at": "2099-01-01T00:00:02+00:00",
            "status": "success",
            "session_ts": "",
            "output_files": [],
            "error": "",
            "trigger": "auto",
            "flow": {"agents": ["content_creator"]},
            "quick_brief": "",
        })

        # Trigger cleanup (normally called by _run_job/_rerun_from_record after append_run)
        sched._cleanup_orphaned_durable_sessions()

        # The orphaned durable session must now be cleaned up
        assert not durable_dir.exists(), \
            "orphaned durable session must be cleaned up after retention evicts its runs"

    def test_expired_original_records_visible_error(self, tmp_path, _store, _resource_store):
        """If original resources are already expired, add_job must not silently drop them."""
        # Upload a resource and manually expire it
        rec = _resource_store.upload(
            filename="brief.txt", content=b"expired test", media_type="text/plain",
        )
        session_id = rec["scope_id"]
        resource_ref = f"resource:{rec['resource_id']}"

        # Manually set expires_at to the past
        res_dir = _resource_store._resource_dir(session_id, rec["resource_id"])
        record_path = res_dir / "resource.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

        sched = Scheduler(
            project_root=tmp_path, web_port=9999, job_store=_store,
            resource_store=_resource_store,
        )

        # add_job must raise — the attachment is already gone
        with pytest.raises(ValueError, match="missing or expired"):
            sched.add_job({
                "name": "test-expired",
                "schedule_type": "one_time",
                "schedule": {"type": "date", "value": "2099-01-01T00:00:00"},
                "flow": {"is_auto": True, "agents": ["content_creator"],
                         "upload_session_id": session_id, "resource_refs": [resource_ref]},
                "quick_brief": "test",
            })


# ------------------------------------------------------------------
# Rerun lifecycle — completed one-time job must remain rerunnable
# ------------------------------------------------------------------

class TestRerunLifecycle:
    """Regression: a completed one-time scheduled job with attachments must
    remain rerunnable from run history.  The durable session must survive
    after the active job is removed so the existing rerun feature works."""

    def test_rerun_after_one_time_fire_forwards_same_attachments(
        self, tmp_path, _store, _resource_store,
    ):
        """Fire one-time job → rerun from history → same attachment forwarded."""
        rec = _resource_store.upload(
            filename="brief.txt", content=b"rerun test", media_type="text/plain",
        )
        session_id = rec["scope_id"]
        resource_ref = f"resource:{rec['resource_id']}"

        sched = Scheduler(
            project_root=tmp_path, web_port=9999, job_store=_store,
            resource_store=_resource_store,
        )

        job_id = sched.add_job({
            "name": "rerun-test",
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": "2099-01-01T00:00:00"},
            "flow": {"is_auto": True, "agents": ["content_creator"],
                     "upload_session_id": session_id, "resource_refs": [resource_ref]},
            "quick_brief": "rerun brief",
        })

        job = next(j for j in _store.load_jobs() if j["id"] == job_id)
        durable_session = job["flow"]["upload_session_id"]
        durable_refs = job["flow"]["resource_refs"]

        # 1. Fire the job successfully (mocked execution)
        fire_payloads = []

        def _capture_stream_factory(captured_list):
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/event-stream"}

            def _capture(method, url, json=None, **kw):
                captured_list.append({"url": url, "json": json})
                resp.iter_lines = MagicMock(return_value=iter(_sse_lines_ok()))
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(return_value=resp)
                ctx.__exit__ = MagicMock(return_value=False)
                return ctx

            client = MagicMock()
            client.stream = MagicMock(side_effect=_capture)
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=client)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with patch("src.scheduler.httpx.Client",
                   return_value=_capture_stream_factory(fire_payloads)):
            sched._run_job(job_id, trigger="auto")

        # 2. Active one-time job is removed
        assert all(j["id"] != job_id for j in _store.load_jobs()), \
            "one-time job must be removed from active list after firing"

        # 3. Run history still contains the flow with durable session refs
        runs = _store.load_runs(job_id=job_id, limit=10)
        assert len(runs) >= 1
        source_run = runs[-1]
        assert source_run["flow"]["upload_session_id"] == durable_session
        assert source_run["flow"]["resource_refs"] == durable_refs

        # 4. The first fire must have forwarded the durable session
        assert len(fire_payloads) == 1
        assert fire_payloads[0]["json"]["upload_session_id"] == durable_session
        assert fire_payloads[0]["json"]["resource_refs"] == durable_refs

        # 5. Invoke the existing rerun path (patch must stay active during async execution)
        rerun_payloads = []
        runs_before = len(_store.load_runs(job_id=job_id, limit=10))
        with patch("src.scheduler.httpx.Client",
                   return_value=_capture_stream_factory(rerun_payloads)):
            ok = sched.rerun_run(job_id, source_run["started_at"])
            assert ok, "rerun_run must return True for an existing run record"

            # Wait for the async executor to complete (run record appended)
            import time
            for _ in range(50):
                if len(_store.load_runs(job_id=job_id, limit=10)) > runs_before:
                    break
                time.sleep(0.1)
        assert len(rerun_payloads) >= 1, "rerun must trigger exactly one execution"

        # 6. The rerun must forward the SAME durable session and resource_refs
        assert rerun_payloads[0]["json"]["upload_session_id"] == durable_session, \
            "rerun must forward the same durable upload_session_id"
        assert rerun_payloads[0]["json"]["resource_refs"] == durable_refs, \
            "rerun must forward the same resource_refs"

        # 7. Exactly one rerun logical execution (not zero, not multiple)
        assert len(rerun_payloads) == 1, \
            f"expected exactly 1 rerun execution, got {len(rerun_payloads)}"

        # 8. A new run-history record must be appended for the rerun
        runs_after = _store.load_runs(job_id=job_id, limit=10)
        assert len(runs_after) >= 2, \
            "rerun must append a new run-history record"
        # load_runs sorts by started_at descending — newest first
        assert runs_after[0]["trigger"] == "rerun", \
            "newest run record must be the rerun"


# ------------------------------------------------------------------
# Recurring delete with retained history — durable session must survive
# ------------------------------------------------------------------

class TestRecurringDeleteRerunLifecycle:
    """Regression: deleting an active recurring schedule that has already
    produced run history must NOT delete the durable session while that
    history is still retained and rerunnable."""

    def test_recurring_delete_retains_durable_session_for_history_rerun(
        self, tmp_path, _store, _resource_store,
    ):
        """Delete recurring job after fire → history rerun still works."""
        rec = _resource_store.upload(
            filename="brief.txt", content=b"recurring rerun test", media_type="text/plain",
        )
        session_id = rec["scope_id"]
        resource_ref = f"resource:{rec['resource_id']}"

        sched = Scheduler(
            project_root=tmp_path, web_port=9999, job_store=_store,
            resource_store=_resource_store,
        )

        job_id = sched.add_job({
            "name": "recurring-rerun-test",
            "schedule_type": "recurring",
            "schedule": {"type": "cron", "value": "0 9 * * *"},
            "flow": {"is_auto": True, "agents": ["content_creator"],
                     "upload_session_id": session_id, "resource_refs": [resource_ref]},
            "quick_brief": "recurring brief",
        })

        job = next(j for j in _store.load_jobs() if j["id"] == job_id)
        durable_session = job["flow"]["upload_session_id"]
        durable_refs = job["flow"]["resource_refs"]
        durable_dir = _resource_store.storage_dir / durable_session
        assert durable_dir.exists()

        # 1. Fire the recurring job once (mocked)
        fire_payloads = []

        def _capture_stream_factory(captured_list):
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/event-stream"}

            def _capture(method, url, json=None, **kw):
                captured_list.append({"url": url, "json": json})
                resp.iter_lines = MagicMock(return_value=iter(_sse_lines_ok()))
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(return_value=resp)
                ctx.__exit__ = MagicMock(return_value=False)
                return ctx

            client = MagicMock()
            client.stream = MagicMock(side_effect=_capture)
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=client)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with patch("src.scheduler.httpx.Client",
                   return_value=_capture_stream_factory(fire_payloads)):
            sched._run_job(job_id, trigger="auto")

        # 2. Run history retains the durable session refs
        runs = _store.load_runs(job_id=job_id, limit=10)
        assert len(runs) >= 1
        source_run = runs[0]
        assert source_run["flow"]["upload_session_id"] == durable_session
        assert source_run["flow"]["resource_refs"] == durable_refs

        # 3. Delete the active recurring job
        ok = sched.remove_job(job_id)
        assert ok, "remove_job must return True"
        assert all(j["id"] != job_id for j in _store.load_jobs()), \
            "recurring job must be removed from active list"

        # 4. Durable session must STILL exist — run history references it
        assert durable_dir.exists(), \
            "durable session must survive job deletion while run history references it"

        # 5. Rerun from history must still work
        rerun_payloads = []
        runs_before = len(_store.load_runs(job_id=job_id, limit=10))
        with patch("src.scheduler.httpx.Client",
                   return_value=_capture_stream_factory(rerun_payloads)):
            ok = sched.rerun_run(job_id, source_run["started_at"])
            assert ok, "rerun_run must return True for retained history"

            import time
            for _ in range(50):
                if len(_store.load_runs(job_id=job_id, limit=10)) > runs_before:
                    break
                time.sleep(0.1)

        # 6. Rerun forwarded the SAME durable session and resource_refs
        assert len(rerun_payloads) == 1, \
            f"expected exactly 1 rerun execution, got {len(rerun_payloads)}"
        assert rerun_payloads[0]["json"]["upload_session_id"] == durable_session
        assert rerun_payloads[0]["json"]["resource_refs"] == durable_refs

        # 7. New run-history record appended
        runs_after = _store.load_runs(job_id=job_id, limit=10)
        assert len(runs_after) > runs_before, "rerun must append a new run record"
        assert runs_after[0]["trigger"] == "rerun"

    def test_delete_never_run_job_cleans_durable_session(
        self, tmp_path, _store, _resource_store,
    ):
        """Deleting a job that never fired must clean its durable session."""
        rec = _resource_store.upload(
            filename="brief.txt", content=b"never run delete", media_type="text/plain",
        )
        session_id = rec["scope_id"]
        resource_ref = f"resource:{rec['resource_id']}"

        sched = Scheduler(
            project_root=tmp_path, web_port=9999, job_store=_store,
            resource_store=_resource_store,
        )

        job_id = sched.add_job({
            "name": "never-run-delete",
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": "2099-01-01T00:00:00"},
            "flow": {"is_auto": True, "agents": ["content_creator"],
                     "upload_session_id": session_id, "resource_refs": [resource_ref]},
            "quick_brief": "test",
        })

        job = next(j for j in _store.load_jobs() if j["id"] == job_id)
        durable_session = job["flow"]["upload_session_id"]
        durable_dir = _resource_store.storage_dir / durable_session
        assert durable_dir.exists()

        # No run history exists — deleting must clean the durable session
        sched.remove_job(job_id)

        assert not durable_dir.exists(), \
            "durable session must be cleaned when no history references it"

    def test_history_eviction_cleans_orphaned_durable_session(
        self, tmp_path, _resource_store,
    ):
        """After run history is evicted by retention, orphan cleanup deletes the session."""
        from src.scheduler import JsonJobStore

        # max_runs=1 so the second append evicts the first run
        store = JsonJobStore(
            tmp_path / "jobs.json", tmp_path / "runs.json", max_runs=1,
        )

        rec = _resource_store.upload(
            filename="brief.txt", content=b"eviction test", media_type="text/plain",
        )
        session_id = rec["scope_id"]
        resource_ref = f"resource:{rec['resource_id']}"

        sched = Scheduler(
            project_root=tmp_path, web_port=9999, job_store=store,
            resource_store=_resource_store,
        )

        job_id = sched.add_job({
            "name": "eviction-test",
            "schedule_type": "recurring",
            "schedule": {"type": "cron", "value": "0 9 * * *"},
            "flow": {"is_auto": True, "agents": ["content_creator"],
                     "upload_session_id": session_id, "resource_refs": [resource_ref]},
            "quick_brief": "test",
        })

        job = next(j for j in store.load_jobs() if j["id"] == job_id)
        durable_session = job["flow"]["upload_session_id"]
        durable_dir = _resource_store.storage_dir / durable_session
        assert durable_dir.exists()

        # Fire the job → run history references the durable session
        with patch("src.scheduler.httpx.Client", return_value=_fake_httpx_stream(_sse_lines_ok())):
            sched._run_job(job_id, trigger="auto")
        assert durable_dir.exists(), "durable session must exist while history references it"

        # Delete the active job → durable session still retained (history references it)
        sched.remove_job(job_id)
        assert durable_dir.exists(), \
            "durable session must survive job deletion while history references it"

        # Append a run for a DIFFERENT job → max_runs=1 evicts the first run
        store.append_run({
            "job_id": "other_job",
            "job_name": "other",
            "started_at": "2099-01-01T00:00:01+00:00",
            "finished_at": "2099-01-01T00:00:02+00:00",
            "status": "success",
            "session_ts": "",
            "output_files": [],
            "error": "",
            "trigger": "auto",
            "flow": {"agents": ["content_creator"]},
            "quick_brief": "",
        })

        # Orphan cleanup must now delete the unreferenced durable session
        sched._cleanup_orphaned_durable_sessions()
        assert not durable_dir.exists(), \
            "orphaned durable session must be cleaned after history eviction"
