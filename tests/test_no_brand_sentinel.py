"""MB-02 no-brand sentinel + scheduler API brand-security tests.

Proves authenticated user-only context cannot read, list, overwrite, or
create user-root business state, and that scheduler APIs enforce brand
context and cross-brand isolation.

Every representative brand-scoped endpoint must return a controlled 403 when
no brand is active, and no user-root file may be touched.  Sentinel files are
placed under the user root before each test; after the request they must
still exist unchanged, and no new user-root file may appear.

Representative paths covered:
  - Product state        (/api/upload, /api/data_folders, /api/folder_files)
  - Brand files          (/api/brand_files, /api/brand_json)
  - Product profiles     (/api/product_profile/{folder})
  - Content history      (/api/content_history)
  - Output/session state (/api/sessions)
  - Content pillars      (/api/pillars)
  - Run resources        (/api/run-resources/upload)
  - Scheduler            (/api/schedule/save + all /api/schedule/* endpoints)

Scheduler internals covered:
  - get_running_status filters by brand_id (A1 cannot see A2)
  - add_job requires brand context, ignores job_spec identity
  - rerun of brandless records fails closed
  - A1/A2 save/list isolation

Uses real path resolvers — does NOT patch DATA_DIR/OUTPUT_DIR/CACHE_DIR/BRAND_DIR/_project_root.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.brand_registry import BrandRegistry
from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
from tests.conftest import make_authed_client


@pytest.fixture
def _app(tmp_path, monkeypatch):
    """Isolated app + authenticated user with NO brand selected.

    Places sentinel files under the user root to prove they are never read,
    listed, overwritten, or created by no-brand requests.
    """
    import importlib
    import web_viewer

    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "_read_folder", lambda f: (["info"], [], {}))
    monkeypatch.setattr(web_viewer, "_current_llm", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_session_ts", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_cancel_requested", {}, raising=False)

    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    client, uid, ws_root = make_authed_client(web_viewer.app, tmp_path, monkeypatch)

    # Place sentinel files under the user root (NOT brand root)
    sentinel_dirs = ["data", "cache", "output", "brand"]
    for d in sentinel_dirs:
        (ws_root / d).mkdir(parents=True, exist_ok=True)
    (ws_root / "data" / "SentinelProduct").mkdir(parents=True, exist_ok=True)
    (ws_root / "data" / "SentinelProduct" / "info.txt").write_text(
        "SENTINEL_USER_ROOT", encoding="utf-8"
    )
    (ws_root / "cache" / "content_history.json").write_text(
        json.dumps({"entries": [{"concept": "SENTINEL_HISTORY"}]}), encoding="utf-8"
    )
    (ws_root / "brand" / "voice.json").write_text(
        '{"personality": "SENTINEL_BRAND"}', encoding="utf-8"
    )
    (ws_root / "output" / "sentinel_session").mkdir(parents=True, exist_ok=True)
    (ws_root / "output" / "sentinel_session" / "content.md").write_text(
        "SENTINEL_OUTPUT", encoding="utf-8"
    )

    return {
        "client": client,
        "uid": uid,
        "ws_root": ws_root,
        "tmp": tmp_path,
        "web_viewer": web_viewer,
    }


# ---------------------------------------------------------------------------
# Product state — no-brand requests must 403, sentinel untouched
# ---------------------------------------------------------------------------

def test_data_folders_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/data_folders")
    assert resp.status_code == 403
    # Sentinel must still exist
    sentinel = _app["ws_root"] / "data" / "SentinelProduct" / "info.txt"
    assert sentinel.read_text() == "SENTINEL_USER_ROOT"


def test_upload_403_no_brand(_app):
    c = _app["client"]
    resp = c.post("/api/upload", data={"product_name": "NewProduct"},
                  files=[("files", ("info.txt", b"new", "text/plain"))])
    assert resp.status_code == 403
    # No new product dir under user root
    assert not (_app["ws_root"] / "data" / "NewProduct").exists()


def test_folder_files_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/folder_files/SentinelProduct")
    assert resp.status_code == 403


def test_ingest_403_no_brand(_app):
    c = _app["client"]
    resp = c.post("/api/ingest/SentinelProduct", json={})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Brand files — no-brand requests must 403, sentinel untouched
# ---------------------------------------------------------------------------

def test_brand_files_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/brand_files")
    assert resp.status_code == 403
    # Sentinel brand file must still exist
    assert (_app["ws_root"] / "brand" / "voice.json").exists()


def test_brand_json_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/brand_json")
    assert resp.status_code == 403


def test_brand_json_save_403_no_brand(_app):
    c = _app["client"]
    resp = c.post("/api/brand_json_save", json={"voice": {"personality": "OVERWRITE"}})
    assert resp.status_code == 403
    # Sentinel must not be overwritten
    content = (_app["ws_root"] / "brand" / "voice.json").read_text()
    assert "SENTINEL_BRAND" in content


# ---------------------------------------------------------------------------
# Product profiles/history — no-brand requests must 403
# ---------------------------------------------------------------------------

def test_product_profile_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/product_profile/SentinelProduct")
    assert resp.status_code == 403


def test_content_history_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/content_history")
    assert resp.status_code == 403
    # Sentinel history must not be read
    # (403 proves it was not returned)


def test_content_history_for_product_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/content_history/SentinelProduct")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Output/session state — no-brand requests must 403
# ---------------------------------------------------------------------------

def test_sessions_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/sessions")
    assert resp.status_code == 403
    # Sentinel output must still exist
    assert (_app["ws_root"] / "output" / "sentinel_session" / "content.md").exists()


def test_session_files_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/session_files/sentinel_session")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Content pillars — no-brand requests must 403
# ---------------------------------------------------------------------------

def test_pillars_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/pillars")
    assert resp.status_code == 403


def test_pillars_save_403_no_brand(_app):
    c = _app["client"]
    resp = c.post("/api/pillars_save", json={"pillars": ["overwrite"], "pillar_keywords": {}})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Run resources — no-brand requests must 403, no user-root storage
# ---------------------------------------------------------------------------

def test_run_resources_upload_403_no_brand(_app):
    c = _app["client"]
    resp = c.post("/api/run-resources/upload",
                  files=[("files", ("test.txt", b"data", "text/plain"))])
    assert resp.status_code == 403
    # No run_resources dir under user root
    assert not (_app["ws_root"] / "cache" / "run_resources").exists()


# ---------------------------------------------------------------------------
# Scheduler — no-brand requests must 403, no user-root scheduler file
# ---------------------------------------------------------------------------

def test_schedule_save_403_no_brand(_app):
    c = _app["client"]
    resp = c.post("/api/schedule/save", json={
        "name": "no-brand job",
        "schedule_type": "one_time",
        "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
        "flow": {"agents": ["content_creator"]},
    })
    assert resp.status_code == 403
    # No scheduler file under user root
    assert not (_app["ws_root"] / "cache" / "scheduled_jobs.json").exists()


def test_schedule_jobs_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/schedule/jobs")
    assert resp.status_code == 403


def test_schedule_status_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/schedule/status")
    assert resp.status_code == 403


def test_schedule_runs_403_no_brand(_app):
    c = _app["client"]
    resp = c.get("/api/schedule/runs")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# No user-root business-state file created by any no-brand request
# ---------------------------------------------------------------------------

def test_no_user_root_scheduler_file_created(_app):
    """After all no-brand requests, no scheduled_jobs.json under user root."""
    c = _app["client"]
    # Hit several no-brand endpoints
    c.get("/api/schedule/jobs")
    c.get("/api/schedule/runs")
    c.post("/api/schedule/save", json={
        "name": "x", "schedule_type": "one_time",
        "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
        "flow": {"agents": ["content_creator"]},
    })
    assert not (_app["ws_root"] / "cache" / "scheduled_jobs.json").exists()
    assert not (_app["ws_root"] / "cache" / "scheduled_runs.json").exists()


# ---------------------------------------------------------------------------
# Scheduler API — parametrized no-brand 403 for every /api/schedule/* endpoint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint,method", [
    ("/api/schedule/jobs", "GET"),
    ("/api/schedule/status", "GET"),
    ("/api/schedule/runs", "GET"),
    ("/api/schedule/save", "POST"),
    ("/api/schedule/delete", "POST"),
    ("/api/schedule/toggle", "POST"),
    ("/api/schedule/run_now", "POST"),
    ("/api/schedule/rerun", "POST"),
])
def test_schedule_endpoint_403_no_brand(_app, endpoint, method):
    c = _app["client"]
    if method == "GET":
        resp = c.get(endpoint)
    else:
        body = {"job_id": "x", "name": "x", "schedule_type": "one_time",
                "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
                "flow": {"agents": ["content_creator"]}}
        resp = c.post(endpoint, json=body)
    assert resp.status_code == 403, f"{endpoint} must 403 without brand, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Scheduler internals — cross-brand isolation, identity, brandless rerun
# ---------------------------------------------------------------------------

def test_a1_cannot_see_a2_running_status(tmp_path):
    """get_running_status must filter by brand_id — A1 cannot see A2's jobs."""
    from src.auth import UserStore
    from src.scheduler import Scheduler, JsonJobStore, _aps_id

    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    user = store.register("user_aaa", "pass")
    uid = user.user_id
    reg = BrandRegistry(user_id=uid, project_root=tmp_path)
    a1 = reg.create("A1")
    a2 = reg.create("A2")

    sched = Scheduler(project_root=tmp_path,
                      job_store=JsonJobStore(tmp_path / "_fj.json",
                                             tmp_path / "_fr.json"))
    try:
        a2_aps_id = _aps_id(uid, a2["brand_id"], "job_a2")
        with sched._lock:
            sched._running_status[a2_aps_id] = {
                "status": "running", "message": "A2 running",
                "agent": "", "started_at": "2025-01-01T00:00:00",
            }
        a1_status = sched.get_running_status(user_id=uid, brand_id=a1["brand_id"])
        assert a1_status == {}, f"A1 must not see A2 status, got {a1_status}"
        a2_status = sched.get_running_status(user_id=uid, brand_id=a2["brand_id"])
        assert "job_a2" in a2_status, f"A2 must see own status, got {a2_status}"
    finally:
        sched.stop()


def test_add_job_without_brand_raises(tmp_path):
    """add_job without an active brand context must raise ValueError."""
    from src.scheduler import Scheduler, JsonJobStore
    sched = Scheduler(project_root=tmp_path,
                      job_store=JsonJobStore(tmp_path / "_fj.json",
                                             tmp_path / "_fr.json"))
    try:
        ws = WorkspaceContext.for_user("user_aaa", tmp_path)
        token = set_workspace(ws)
        try:
            with pytest.raises(ValueError, match="brand"):
                sched.add_job({
                    "name": "no-brand", "schedule_type": "one_time",
                    "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
                    "flow": {"agents": ["content_creator"]},
                })
        finally:
            reset_workspace(token)
    finally:
        sched.stop()


def test_add_job_ignores_job_spec_identity(tmp_path):
    """add_job must NOT accept user_id/brand_id from job_spec — only from context."""
    from src.auth import UserStore
    from src.scheduler import Scheduler, JsonJobStore

    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    user = store.register("user_aaa", "pass")
    uid = user.user_id
    reg = BrandRegistry(user_id=uid, project_root=tmp_path)
    a1 = reg.create("A1")

    sched = Scheduler(project_root=tmp_path,
                      job_store=JsonJobStore(tmp_path / "_fj.json",
                                             tmp_path / "_fr.json"))
    try:
        ws = WorkspaceContext.for_brand(uid, a1["brand_id"], tmp_path)
        token = set_workspace(ws)
        try:
            job_id = sched.add_job({
                "name": "A1 job", "schedule_type": "one_time",
                "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
                "flow": {"agents": ["content_creator"]},
                "user_id": "attacker",
                "brand_id": "attacker_brand",
            })
            jobs = sched.list_jobs()
            job = next(j for j in jobs if j["id"] == job_id)
            assert job["user_id"] == uid, "job_spec user_id must be ignored"
            assert job["brand_id"] == a1["brand_id"], "job_spec brand_id must be ignored"
        finally:
            reset_workspace(token)
    finally:
        sched.stop()


def test_rerun_brandless_record_fails_closed(tmp_path):
    """Rerun of a brandless (legacy) run record must fail closed."""
    from src.auth import UserStore
    from src.scheduler import Scheduler, JsonJobStore

    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    user = store.register("user_aaa", "pass")
    uid = user.user_id
    reg = BrandRegistry(user_id=uid, project_root=tmp_path)
    a1 = reg.create("A1")

    sched = Scheduler(project_root=tmp_path,
                      job_store=JsonJobStore(tmp_path / "_fj.json",
                                             tmp_path / "_fr.json"))
    try:
        ws = WorkspaceContext.for_user(uid, tmp_path)
        token = set_workspace(ws)
        try:
            sched._store.append_run({
                "job_id": "job_legacy", "job_name": "legacy",
                "started_at": "2025-01-01T00:00:00+00:00",
                "finished_at": "2025-01-01T00:01:00+00:00",
                "status": "success", "user_id": uid,
                "flow": {"agents": ["content_creator"]},
                "quick_brief": "test",
            })
        finally:
            reset_workspace(token)

        ws = WorkspaceContext.for_brand(uid, a1["brand_id"], tmp_path)
        token = set_workspace(ws)
        try:
            ok = sched.rerun_run("job_legacy", "2025-01-01T00:00:00+00:00", user_id=uid)
            assert not ok, "rerun of brandless record must fail closed"
        finally:
            reset_workspace(token)
    finally:
        sched.stop()


def test_a1_a2_save_list_isolation(tmp_path):
    """A1's saved jobs are not visible to A2 and vice versa."""
    from src.auth import UserStore
    from src.scheduler import Scheduler, JsonJobStore

    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    user = store.register("user_aaa", "pass")
    uid = user.user_id
    reg = BrandRegistry(user_id=uid, project_root=tmp_path)
    a1 = reg.create("A1")
    a2 = reg.create("A2")

    sched = Scheduler(project_root=tmp_path,
                      job_store=JsonJobStore(tmp_path / "_fj.json",
                                             tmp_path / "_fr.json"))
    try:
        for bid, name in [(a1["brand_id"], "A1 job"), (a2["brand_id"], "A2 job")]:
            ws = WorkspaceContext.for_brand(uid, bid, tmp_path)
            token = set_workspace(ws)
            try:
                sched.add_job({
                    "name": name, "schedule_type": "one_time",
                    "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
                    "flow": {"agents": ["content_creator"]},
                })
            finally:
                reset_workspace(token)

        for bid, expected in [(a1["brand_id"], "A1 job"), (a2["brand_id"], "A2 job")]:
            ws = WorkspaceContext.for_brand(uid, bid, tmp_path)
            token = set_workspace(ws)
            try:
                jobs = sched.list_jobs()
            finally:
                reset_workspace(token)
            assert len(jobs) == 1 and jobs[0]["name"] == expected
    finally:
        sched.stop()
