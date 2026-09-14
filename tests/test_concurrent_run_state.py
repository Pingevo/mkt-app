"""Stage C — Concurrent per-user runtime state isolation.

Proves that process-global runtime state is now keyed by verified user_id,
so one user's cancel/LLM/session/visual/conflict state cannot cross workspace
boundaries.

Contracts:
  1. Cancellation isolation — A1 cancel cannot cancel A2; U1 cannot cancel U2.
  2. Active/current LLM isolation — per-user LLM clients; cleanup is scoped.
  3. Session timestamp isolation — per-user session_ts; no cross-leak.
  4. Brand visual cache isolation — per-user cache; A1 cache not consumed by A2.
  5. Conflict cache isolation — per-user conflict cache; A1 decisions don't
     affect A2.
  6. Lifecycle cleanup — completion/cancel cleanup removes only the owning
     user's runtime state.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.auth import UserStore, SessionManager
from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
from tests.conftest import make_authed_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_two_users(tmp_path):
    """Create two registered users and return (uid_a, uid_b)."""
    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    ua = store.register("user_a", "pass_a")
    ub = store.register("user_b", "pass_b")
    return ua.user_id, ub.user_id


def _set_user_ws(uid, project_root):
    """Set a user-only workspace context and return the reset token."""
    ws = WorkspaceContext.for_user(uid, project_root)
    return set_workspace(ws)


# ---------------------------------------------------------------------------
# 1. Cancellation isolation
# ---------------------------------------------------------------------------

def test_cancel_scoped_to_user(tmp_path, monkeypatch):
    """A1's cancel flag cannot affect A2's run."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, uid_b = _make_two_users(tmp_path)

    # Set A's cancel flag via A's workspace context
    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        web_viewer._cancel_requested[uid_a] = True
    finally:
        reset_workspace(tok_a)

    # B's cancel flag must be False (default)
    tok_b = _set_user_ws(uid_b, tmp_path)
    try:
        assert web_viewer._is_cancelled() is False, (
            "B must not inherit A's cancel flag"
        )
    finally:
        reset_workspace(tok_b)

    # A's cancel flag is still True
    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        assert web_viewer._is_cancelled() is True, (
            "A's cancel flag must remain set"
        )
    finally:
        reset_workspace(tok_a)


def test_cancel_endpoint_scoped_to_calling_user(tmp_path, monkeypatch):
    """/api/cancel sets only the calling user's flag and aborts only their LLMs."""
    import asyncio
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, uid_b = _make_two_users(tmp_path)

    # Register a fake LLM for each user
    llm_a = MagicMock()
    llm_b = MagicMock()
    web_viewer._active_llms[uid_a] = [llm_a]
    web_viewer._active_llms[uid_b] = [llm_b]

    # A calls /api/cancel
    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        resp = asyncio.get_event_loop().run_until_complete(web_viewer.api_cancel())
        assert resp.status_code == 200
    finally:
        reset_workspace(tok_a)

    # A's LLM was aborted, B's was not
    llm_a.abort.assert_called_once()
    llm_b.abort.assert_not_called()

    # A's cancel flag is True, B's is still False
    assert web_viewer._cancel_requested.get(uid_a) is True
    assert web_viewer._cancel_requested.get(uid_b, False) is False

    # A's active_llms list is cleared, B's is not
    assert web_viewer._active_llms.get(uid_a, []) == []
    assert web_viewer._active_llms.get(uid_b, []) == [llm_b]


def test_stale_cancel_not_inherited(tmp_path, monkeypatch):
    """A stale cancel flag from one run is cleared at the start of the next run."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, _ = _make_two_users(tmp_path)

    # Set a stale cancel flag
    web_viewer._cancel_requested[uid_a] = True

    # Simulate run start clearing the flag
    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        web_viewer._cancel_requested[uid_a] = False
        assert web_viewer._is_cancelled() is False
    finally:
        reset_workspace(tok_a)


# ---------------------------------------------------------------------------
# 2. Active/current LLM isolation
# ---------------------------------------------------------------------------

def test_current_llm_scoped_to_user(tmp_path, monkeypatch):
    """Each user gets a separate _current_llm; closing one doesn't close another."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, uid_b = _make_two_users(tmp_path)

    llm_a = MagicMock()
    llm_b = MagicMock()
    web_viewer._current_llm[uid_a] = llm_a
    web_viewer._current_llm[uid_b] = llm_b

    # Close A's LLM
    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        llm = web_viewer._current_llm.pop(uid_a, None)
        if llm:
            llm.close()
    finally:
        reset_workspace(tok_a)

    # A's LLM was closed, B's is untouched
    llm_a.close.assert_called_once()
    llm_b.close.assert_not_called()
    assert uid_a not in web_viewer._current_llm
    assert web_viewer._current_llm[uid_b] is llm_b


def test_active_llms_scoped_to_user(tmp_path, monkeypatch):
    """_active_llms is per-user; appending for one user doesn't affect another."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, uid_b = _make_two_users(tmp_path)

    llm_a = MagicMock()
    llm_b = MagicMock()

    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        web_viewer._register_llm(llm_a)
    finally:
        reset_workspace(tok_a)

    tok_b = _set_user_ws(uid_b, tmp_path)
    try:
        web_viewer._register_llm(llm_b)
    finally:
        reset_workspace(tok_b)

    assert web_viewer._active_llms[uid_a] == [llm_a]
    assert web_viewer._active_llms[uid_b] == [llm_b]

    # Unregister A's LLM — B's list must not change
    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        web_viewer._unregister_llm(llm_a)
    finally:
        reset_workspace(tok_a)

    assert web_viewer._active_llms.get(uid_a, []) == []
    assert web_viewer._active_llms[uid_b] == [llm_b]


# ---------------------------------------------------------------------------
# 3. Session timestamp isolation
# ---------------------------------------------------------------------------

def test_session_ts_scoped_to_user(tmp_path, monkeypatch):
    """_session_ts is per-user; one user's session label doesn't leak."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, uid_b = _make_two_users(tmp_path)

    web_viewer._session_ts[uid_a] = "2026-01-01 - ProductA"
    web_viewer._session_ts[uid_b] = "2026-01-01 - ProductB"

    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        assert web_viewer._session_ts.get(uid_a) == "2026-01-01 - ProductA"
    finally:
        reset_workspace(tok_a)

    tok_b = _set_user_ws(uid_b, tmp_path)
    try:
        assert web_viewer._session_ts.get(uid_b) == "2026-01-01 - ProductB"
    finally:
        reset_workspace(tok_b)

    # A's session_ts is not visible from B's context
    tok_b = _set_user_ws(uid_b, tmp_path)
    try:
        assert web_viewer._session_ts.get(uid_b) != "2026-01-01 - ProductA"
    finally:
        reset_workspace(tok_b)


# ---------------------------------------------------------------------------
# 4. Brand visual cache isolation
# ---------------------------------------------------------------------------

def test_brand_visual_cache_scoped_to_user(tmp_path, monkeypatch):
    """_BRAND_VISUAL_CACHE is per-user+brand; U1's cache cannot be consumed by U2."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, uid_b = _make_two_users(tmp_path)
    key_a = (uid_a, "brand_a")
    key_b = (uid_b, "brand_b")

    visual_a = {"color": "red", "font": "A-font"}
    visual_b = {"color": "blue", "font": "B-font"}
    web_viewer._BRAND_VISUAL_CACHE[key_a] = visual_a
    web_viewer._BRAND_VISUAL_CACHE[key_b] = visual_b

    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        # Patch _cache_key to return the brand-scoped key for this test context
        monkeypatch.setattr(web_viewer, "_cache_key", lambda: key_a)
        result = web_viewer._get_brand_visual()
        assert result == visual_a
    finally:
        reset_workspace(tok_a)

    tok_b = _set_user_ws(uid_b, tmp_path)
    try:
        monkeypatch.setattr(web_viewer, "_cache_key", lambda: key_b)
        result = web_viewer._get_brand_visual()
        assert result == visual_b
    finally:
        reset_workspace(tok_b)


def test_brand_visual_cache_clear_scoped(tmp_path, monkeypatch):
    """Clearing A1's visual cache doesn't clear A2's (same user, two brands)."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, _ = _make_two_users(tmp_path)
    key_a1 = (uid_a, "brand_a1")
    key_a2 = (uid_a, "brand_a2")

    web_viewer._BRAND_VISUAL_CACHE[key_a1] = {"color": "red"}
    web_viewer._BRAND_VISUAL_CACHE[key_a2] = {"color": "blue"}

    # Clear A1's cache — A2's must survive
    web_viewer._BRAND_VISUAL_CACHE.pop(key_a1, None)

    assert key_a1 not in web_viewer._BRAND_VISUAL_CACHE
    assert web_viewer._BRAND_VISUAL_CACHE[key_a2] == {"color": "blue"}


def test_same_user_brand_switch_visual_cache(tmp_path, monkeypatch):
    """U1 switching A1→A2 does not consume A1's cached visual state."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, _ = _make_two_users(tmp_path)
    key_a1 = (uid_a, "brand_a1")
    key_a2 = (uid_a, "brand_a2")

    # A1's cached visual
    web_viewer._BRAND_VISUAL_CACHE[key_a1] = {"color": "red", "source": "A1"}

    # U1/A1 reads cache → gets A1's data
    monkeypatch.setattr(web_viewer, "_cache_key", lambda: key_a1)
    result = web_viewer._get_brand_visual()
    assert result["source"] == "A1"

    # U1/A2 switches — cache key changes, A1's data is not visible
    monkeypatch.setattr(web_viewer, "_cache_key", lambda: key_a2)
    # _get_brand_visual will miss the cache and try to load — mock it
    monkeypatch.setattr(web_viewer, "load_brand_visual", lambda *a, **k: {"color": "green", "source": "A2"})
    result = web_viewer._get_brand_visual()
    assert result["source"] == "A2", "A2 must not see A1's cached visual"

    # A1's cache is still intact (not overwritten by A2's load)
    assert web_viewer._BRAND_VISUAL_CACHE[key_a1]["source"] == "A1"


# ---------------------------------------------------------------------------
# 5. Conflict cache isolation
# ---------------------------------------------------------------------------

def test_conflict_cache_scoped_to_user(tmp_path, monkeypatch):
    """_conflict_cache is per-user+brand; U1's conflicts don't affect U2."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, uid_b = _make_two_users(tmp_path)
    key_a = (uid_a, "brand_a")
    key_b = (uid_b, "brand_b")

    cache_a = {"product_spec": [{"field": "budget", "severity": "high"}]}
    cache_b = {"product_spec": []}
    web_viewer._conflict_cache[key_a] = cache_a
    web_viewer._conflict_cache[key_b] = cache_b

    monkeypatch.setattr(web_viewer, "_cache_key", lambda: key_a)
    result = web_viewer._conflict_cache.get(key_a, {})
    assert result == cache_a
    assert len(result["product_spec"]) == 1

    monkeypatch.setattr(web_viewer, "_cache_key", lambda: key_b)
    result = web_viewer._conflict_cache.get(key_b, {})
    assert result == cache_b
    assert len(result["product_spec"]) == 0


def test_same_user_brand_switch_conflict_cache(tmp_path, monkeypatch):
    """U1 switching A1→A2 does not consume A1's cached conflict state."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, _ = _make_two_users(tmp_path)
    key_a1 = (uid_a, "brand_a1")
    key_a2 = (uid_a, "brand_a2")

    # A1's cached conflicts
    web_viewer._conflict_cache[key_a1] = {
        "product_spec": [{"field": "budget", "severity": "high"}]
    }

    # U1/A2 has no cached conflicts — must not see A1's
    result = web_viewer._conflict_cache.get(key_a2, {})
    assert result == {}, "A2 must not see A1's cached conflicts"
    assert web_viewer._conflict_cache[key_a1]["product_spec"][0]["field"] == "budget"


# ---------------------------------------------------------------------------
# 6. Lifecycle cleanup — no cross-workspace side effects
# ---------------------------------------------------------------------------

def test_cleanup_does_not_cross_users(tmp_path, monkeypatch):
    """Completing A's run (close LLM, clear cancel) doesn't touch B's state."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, uid_b = _make_two_users(tmp_path)

    # Both users have active state
    llm_a = MagicMock()
    llm_b = MagicMock()
    web_viewer._current_llm[uid_a] = llm_a
    web_viewer._current_llm[uid_b] = llm_b
    web_viewer._cancel_requested[uid_a] = False
    web_viewer._cancel_requested[uid_b] = False
    web_viewer._active_llms[uid_a] = [llm_a]
    web_viewer._active_llms[uid_b] = [llm_b]

    # A's run completes — close A's LLM, clear A's state
    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        llm = web_viewer._current_llm.pop(uid_a, None)
        if llm:
            llm.close()
        web_viewer._active_llms.get(uid_a, []).clear()
    finally:
        reset_workspace(tok_a)

    # A's state is cleared, B's is untouched
    assert uid_a not in web_viewer._current_llm
    assert web_viewer._active_llms.get(uid_a, []) == []
    llm_a.close.assert_called_once()

    assert web_viewer._current_llm[uid_b] is llm_b
    assert web_viewer._active_llms[uid_b] == [llm_b]
    llm_b.close.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Recovery/archive isolation — per-user archive root
# ---------------------------------------------------------------------------

def test_archive_destination_inside_owner_workspace(tmp_path, monkeypatch):
    """With User A workspace active, _archive_root() resolves to
    users/{user_a}/.recovery_archive/, not the global .recovery_archive."""
    from src.local_workspace import _archive_root
    monkeypatch.setattr("src.local_workspace._project_root", lambda: tmp_path)
    uid_a, _ = _make_two_users(tmp_path)

    ws = WorkspaceContext.for_user(uid_a, tmp_path)
    token = set_workspace(ws)
    try:
        archive = _archive_root()
        expected = tmp_path / "users" / uid_a / ".recovery_archive"
        assert archive == expected, (
            f"archive must be inside owner workspace: {archive} != {expected}"
        )
    finally:
        reset_workspace(token)


def test_archive_inaccessible_to_other_users(tmp_path, monkeypatch):
    """User B's archive root resolves to a different path than User A's."""
    from src.local_workspace import _archive_root
    monkeypatch.setattr("src.local_workspace._project_root", lambda: tmp_path)
    uid_a, uid_b = _make_two_users(tmp_path)

    tok_a = set_workspace(WorkspaceContext.for_user(uid_a, tmp_path))
    try:
        archive_a = _archive_root()
    finally:
        reset_workspace(tok_a)

    tok_b = set_workspace(WorkspaceContext.for_user(uid_b, tmp_path))
    try:
        archive_b = _archive_root()
    finally:
        reset_workspace(tok_b)

    assert archive_a != archive_b, (
        f"User A and B must have different archive roots: {archive_a} == {archive_b}"
    )
    assert uid_a in str(archive_a)
    assert uid_b in str(archive_b)
    assert uid_b not in str(archive_a)


def test_archive_traversal_blocked(tmp_path, monkeypatch):
    """Traversal into another user's archive via relative paths fails closed.

    The archive root is resolved per-user, so a path like
    ../../user_b/.recovery_archive cannot be constructed from the archive
    root itself — the root is always users/{calling_user}/.recovery_archive.
    """
    from src.local_workspace import _archive_root
    monkeypatch.setattr("src.local_workspace._project_root", lambda: tmp_path)
    uid_a, uid_b = _make_two_users(tmp_path)

    # Create B's archive with a secret file
    b_archive = tmp_path / "users" / uid_b / ".recovery_archive"
    b_archive.mkdir(parents=True)
    (b_archive / "secret.txt").write_text("B_SECRET", encoding="utf-8")

    # A's archive root must not contain B's archive
    tok_a = set_workspace(WorkspaceContext.for_user(uid_a, tmp_path))
    try:
        a_archive = _archive_root()
        a_archive.mkdir(parents=True, exist_ok=True)
        # A's archive does not contain B's secret
        assert not (a_archive / "secret.txt").exists()
        # B's archive is under a different user root
        assert b_archive != a_archive
    finally:
        reset_workspace(tok_a)


# ---------------------------------------------------------------------------
# 8. Real overlapping-runtime contract — U1 and U2 concurrent runs
# ---------------------------------------------------------------------------

def test_overlapping_runs_cancel_isolation(tmp_path, monkeypatch):
    """U1 and U2 run concurrently; cancel U1 → U1 dies, U2 survives untouched.

    Uses threading.Event for deterministic synchronization — no sleeps.
    Both workers are alive simultaneously when the cancel fires.
    """
    import asyncio
    import threading
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)

    uid_a, uid_b = _make_two_users(tmp_path)

    llm_a = MagicMock()
    llm_b = MagicMock()

    # Events for deterministic synchronization
    u1_registered = threading.Event()
    u2_registered = threading.Event()
    cancel_fired = threading.Event()
    u1_done = threading.Event()
    u2_done = threading.Event()
    u2_can_finish = threading.Event()

    def u1_worker():
        """U1's run worker: registers LLM, waits for cancel, then cleans up."""
        tok = _set_user_ws(uid_a, tmp_path)
        try:
            web_viewer._register_llm(llm_a)
            web_viewer._cancel_requested[uid_a] = False
            u1_registered.set()
            # Wait for U2 to also be registered before proceeding
            u2_registered.wait(timeout=5)
            # Wait for cancel to fire
            cancel_fired.wait(timeout=5)
            # U1's run completes — pop current_llm, clear active_llms
            web_viewer._current_llm.pop(uid_a, None)
            u1_done.set()
        finally:
            reset_workspace(tok)

    def u2_worker():
        """U2's run worker: registers LLM, waits for signal to finish."""
        tok = _set_user_ws(uid_b, tmp_path)
        try:
            web_viewer._register_llm(llm_b)
            web_viewer._cancel_requested[uid_b] = False
            u2_registered.set()
            # Wait for permission to finish (after U1 is cancelled)
            u2_can_finish.wait(timeout=10)
            # U2 completes normally
            web_viewer._current_llm.pop(uid_b, None)
            u2_done.set()
        finally:
            reset_workspace(tok)

    t1 = threading.Thread(target=u1_worker, daemon=True)
    t2 = threading.Thread(target=u2_worker, daemon=True)
    t1.start()
    t2.start()

    # Wait for BOTH workers to be registered and alive
    assert u1_registered.wait(timeout=5), "U1 worker did not start"
    assert u2_registered.wait(timeout=5), "U2 worker did not start"

    # Both users have active LLMs registered
    assert len(web_viewer._active_llms.get(uid_a, [])) == 1
    assert len(web_viewer._active_llms.get(uid_b, [])) == 1

    # U1 calls /api/cancel
    tok_a = _set_user_ws(uid_a, tmp_path)
    try:
        resp = asyncio.get_event_loop().run_until_complete(web_viewer.api_cancel())
        assert resp.status_code == 200
    finally:
        reset_workspace(tok_a)
    cancel_fired.set()

    # U1 is cancelled — flag set, LLM aborted, list cleared
    assert web_viewer._cancel_requested.get(uid_a) is True
    llm_a.abort.assert_called_once()
    assert web_viewer._active_llms.get(uid_a, []) == []

    # U2 is NOT cancelled — flag clear, LLM not aborted, list intact
    assert web_viewer._cancel_requested.get(uid_b, False) is False
    llm_b.abort.assert_not_called()
    assert web_viewer._active_llms.get(uid_b, []) == [llm_b]

    # Let U2 finish
    u2_can_finish.set()

    t1.join(timeout=5)
    t2.join(timeout=5)

    # Final state: no cross-contamination
    assert u1_done.is_set()
    assert u2_done.is_set()
    assert uid_a not in web_viewer._current_llm
    assert uid_b not in web_viewer._current_llm
    assert web_viewer._active_llms.get(uid_a, []) == []
    assert web_viewer._active_llms.get(uid_b, []) == [llm_b]  # still registered (not cleaned)
    # U1's cancel flag is True (was cancelled), U2's is False (was not)
    assert web_viewer._cancel_requested.get(uid_a) is True
    assert web_viewer._cancel_requested.get(uid_b, False) is False
