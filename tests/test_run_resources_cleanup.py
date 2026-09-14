"""MB-02 run-resource lifecycle — per-brand expired cleanup via the actual
startup helper/handler, no user-root fallback.

Proves:
  1. The startup handler awaits completion (TestClient startup completes only
     after cleanup finishes).
  2. Expired resources for A1 and A2 are cleaned in their own brand contexts.
  3. One brand's cleanup cannot touch another brand — verified by instrumenting
     ``cleanup_expired`` to assert the active ``(user_id, brand_id)`` and the
     corresponding brand storage root on every call.
  4. No user-root ``run_resources`` directory is created.
  5. Per-brand failures are surfaced in the returned result list, not silently
     discarded.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.brand_registry import BrandRegistry
from src.run_resources import RunResourceStore
from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace, get_workspace


@pytest.fixture
def _setup(tmp_path):
    """Create a user + two brands; return (project_root, user_id, a1_id, a2_id)."""
    from src.auth import UserStore

    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    user = store.register("test_user", "pass")
    uid = user.user_id
    reg = BrandRegistry(user_id=uid, project_root=tmp_path)
    a1 = reg.create("A1")
    a2 = reg.create("A2")
    return tmp_path, uid, a1["brand_id"], a2["brand_id"]


def _upload_expired(project, uid, bid, name):
    """Upload one expired resource under the given brand context."""
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    ws = WorkspaceContext.for_brand(uid, bid, project)
    token = set_workspace(ws)
    try:
        store = RunResourceStore(project, config={"enabled": True})
        sid = store.create_upload_session()
        store.upload(name, b"data", session_id=sid, expires_at=past)
        return store.storage_dir
    finally:
        reset_workspace(token)


def _upload_alive(project, uid, bid, name):
    """Upload one non-expired resource under the given brand context."""
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    ws = WorkspaceContext.for_brand(uid, bid, project)
    token = set_workspace(ws)
    try:
        store = RunResourceStore(project, config={"enabled": True})
        sid = store.create_upload_session()
        store.upload(name, b"data", session_id=sid, expires_at=future)
        return store.storage_dir
    finally:
        reset_workspace(token)


def _names(storage_dir: Path) -> list[str]:
    files = list(storage_dir.rglob("resource.json"))
    return [json.loads(f.read_text(encoding="utf-8"))["name"] for f in files]


def test_startup_helper_cleans_expired_per_brand(_setup, monkeypatch):
    """The real startup helper cleans expired A1 and A2 in their own contexts."""
    project, uid, a1_id, a2_id = _setup
    a1_storage = _upload_expired(project, uid, a1_id, "a1_expired.txt")
    _upload_alive(project, uid, a1_id, "a1_alive.txt")
    a2_storage = _upload_expired(project, uid, a2_id, "a2_expired.txt")
    _upload_alive(project, uid, a2_id, "a2_alive.txt")

    import web_viewer
    test_store = RunResourceStore(project, config={"enabled": True})
    monkeypatch.setattr(web_viewer, "_resource_store", test_store)

    results = web_viewer._cleanup_run_resources_all_brands(project)

    assert len(results) == 2, f"expected 2 brand results, got {results}"
    cleaned_ids = {r["brand_id"] for r in results}
    assert cleaned_ids == {a1_id, a2_id}
    for r in results:
        assert r["removed"] >= 1, f"brand {r['brand_id']} removed {r['removed']}"
        assert r["error"] == "", f"brand {r['brand_id']} error: {r['error']}"

    a1_names = _names(a1_storage)
    assert "a1_alive.txt" in a1_names
    assert "a1_expired.txt" not in a1_names

    a2_names = _names(a2_storage)
    assert "a2_alive.txt" in a2_names
    assert "a2_expired.txt" not in a2_names


def test_startup_helper_no_user_root_run_resources(_setup, monkeypatch):
    """No run_resources directory appears under user root after startup cleanup."""
    project, uid, a1_id, a2_id = _setup
    _upload_expired(project, uid, a1_id, "a1.txt")
    _upload_expired(project, uid, a2_id, "a2.txt")

    import web_viewer
    test_store = RunResourceStore(project, config={"enabled": True})
    monkeypatch.setattr(web_viewer, "_resource_store", test_store)

    web_viewer._cleanup_run_resources_all_brands(project)

    user_root = project / "users" / uid
    user_rr = user_root / "cache" / "run_resources"
    assert not user_rr.exists(), "run_resources must not exist under user root"


def test_startup_helper_one_brand_cannot_touch_another(_setup, monkeypatch):
    """Each cleanup_expired call observes the expected active (user_id, brand_id)
    and the corresponding brand storage root — A1 cleanup cannot touch A2."""
    project, uid, a1_id, a2_id = _setup
    a1_storage = _upload_expired(project, uid, a1_id, "a1_expired.txt")
    a2_storage = _upload_expired(project, uid, a2_id, "a2_expired.txt")

    import web_viewer
    test_store = RunResourceStore(project, config={"enabled": True})
    monkeypatch.setattr(web_viewer, "_resource_store", test_store)

    # Instrument cleanup_expired: record the active workspace + storage root
    # on every call and assert they match the expected brand.
    observed_calls: list[dict] = []
    original_cleanup = test_store.cleanup_expired

    def _instrumented_cleanup():
        ws = get_workspace()
        observed_calls.append({
            "user_id": ws.user_id if ws else None,
            "brand_id": ws.brand_id if ws else None,
            "storage_dir": str(test_store.storage_dir),
        })
        return original_cleanup()

    monkeypatch.setattr(test_store, "cleanup_expired", _instrumented_cleanup)

    results = web_viewer._cleanup_run_resources_all_brands(project)

    # Exactly two calls — one per brand.
    assert len(observed_calls) == 2, (
        f"expected exactly 2 cleanup_expired calls, got {observed_calls}"
    )

    # Every call observed the expected active context for its brand.  Match
    # by brand_id (enumeration order is not guaranteed).
    expected_by_bid = {
        a1_id: {"user_id": uid, "brand_id": a1_id, "storage_dir": str(a1_storage)},
        a2_id: {"user_id": uid, "brand_id": a2_id, "storage_dir": str(a2_storage)},
    }
    for obs in observed_calls:
        bid = obs["brand_id"]
        assert bid in expected_by_bid, f"unexpected brand in cleanup call: {obs}"
        exp = expected_by_bid[bid]
        assert obs["user_id"] == exp["user_id"], (
            f"cleanup call observed wrong user: {obs} != {exp}"
        )
        assert obs["storage_dir"] == exp["storage_dir"], (
            f"cleanup call observed wrong storage root: {obs} != {exp}"
        )

    # Each brand's expired resource was removed by its own pass.
    assert "a1_expired.txt" not in _names(a1_storage)
    assert "a2_expired.txt" not in _names(a2_storage)


def test_startup_helper_surfaces_failures(_setup, monkeypatch):
    """If cleanup_expired raises, the error is surfaced in the result list."""
    project, uid, a1_id, a2_id = _setup
    _upload_expired(project, uid, a1_id, "a1.txt")

    import web_viewer
    test_store = RunResourceStore(project, config={"enabled": True})
    monkeypatch.setattr(web_viewer, "_resource_store", test_store)

    original = test_store.cleanup_expired
    call_count = {"n": 0}

    def flaky():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure")
        return original()

    monkeypatch.setattr(test_store, "cleanup_expired", flaky)

    results = web_viewer._cleanup_run_resources_all_brands(project)

    errors = [r for r in results if r["error"]]
    assert errors, f"expected at least one surfaced error, got {results}"
    assert any("simulated failure" in r["error"] for r in errors)


def test_startup_handler_awaits_completion(_setup, monkeypatch):
    """The actual @app.on_event('startup') handler awaits cleanup completion.

    Uses TestClient's lifespan handling: by the time the client context
    enters, startup (including cleanup) must have finished.  We assert this
    by recording a marker that cleanup ran and checking it after the client
    is up.
    """
    project, uid, a1_id, a2_id = _setup
    _upload_expired(project, uid, a1_id, "a1.txt")
    _upload_expired(project, uid, a2_id, "a2.txt")

    import web_viewer
    test_store = RunResourceStore(project, config={"enabled": True})
    monkeypatch.setattr(web_viewer, "_resource_store", test_store)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", project)

    cleanup_done = {"v": False}
    original = web_viewer._cleanup_run_resources_all_brands

    def _mark_done(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            cleanup_done["v"] = True

    monkeypatch.setattr(web_viewer, "_cleanup_run_resources_all_brands", _mark_done)

    from starlette.testclient import TestClient

    with TestClient(web_viewer.app) as _client:
        # Startup has completed by the time we reach here.
        assert cleanup_done["v"], (
            "startup handler must await cleanup completion before finishing"
        )
        # And the actual cleanup happened — both expired resources gone.
        a1_storage = project / "users" / uid / "brands" / a1_id / "cache" / "run_resources"
        a2_storage = project / "users" / uid / "brands" / a2_id / "cache" / "run_resources"
        if a1_storage.exists():
            assert "a1.txt" not in _names(a1_storage)
        if a2_storage.exists():
            assert "a2.txt" not in _names(a2_storage)

    # No user-root run_resources created.
    user_rr = project / "users" / uid / "cache" / "run_resources"
    assert not user_rr.exists()
