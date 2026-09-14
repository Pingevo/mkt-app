"""MB-02: RunResourceStore lifecycle — brand-scoped storage, no fallback.

Proves:
- Application startup with no brand context → no error, no user-root fallback
- Brand A1 active → RunResourceStore uses A1 root
- Brand A2 active → RunResourceStore uses A2 root
- A1 resources unavailable to A2 (cross-brand isolation)
- No brand context → brand-scoped operations fail closed, never fall back to user root
- No brand directory created implicitly at startup
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.run_resources import RunResourceStore
from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
from src.brand_registry import BrandRegistry


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


# --- Application startup (no brand context) ---

def test_run_resource_store_constructs_without_brand_context(tmp_path):
    """Constructing RunResourceStore with no brand context must not raise."""
    store = RunResourceStore(tmp_path)
    assert store is not None


def test_no_brand_directory_created_at_startup(tmp_path):
    """No brand-scoped directory should be created when no brand context is active."""
    store = RunResourceStore(tmp_path)
    # The store should not have created any brand-scoped directory
    users_dir = tmp_path / "users"
    if users_dir.exists():
        for user_dir in users_dir.iterdir():
            brands_dir = user_dir / "brands"
            assert not brands_dir.exists(), (
                "brand directory must not be created at startup without brand context"
            )


def test_cleanup_expired_no_brand_context_is_noop(tmp_path):
    """cleanup_expired() with no brand context must not raise or fall back."""
    store = RunResourceStore(tmp_path)
    # Should not raise, should not create any directory
    try:
        result = store.cleanup_expired()
        assert result == 0, "cleanup with no brand context should return 0"
    except ValueError:
        pass  # acceptable — fail closed is also valid


def test_storage_dir_without_brand_context_raises(tmp_path):
    """storage_dir property without brand context must raise ValueError, not fall back."""
    store = RunResourceStore(tmp_path)
    with pytest.raises(ValueError, match="brand_state_root"):
        _ = store.storage_dir


# --- Brand-scoped storage ---

def test_a1_uses_a1_root(_setup):
    """With A1 active, RunResourceStore resolves to A1's brand root."""
    project, uid, a1_id, a2_id = _setup
    ws = WorkspaceContext.for_brand(uid, a1_id, project)
    token = set_workspace(ws)
    try:
        store = RunResourceStore(project)
        expected = project / "users" / uid / "brands" / a1_id / "cache" / "run_resources"
        assert store.storage_dir == expected
    finally:
        reset_workspace(token)


def test_a2_uses_a2_root(_setup):
    """With A2 active, RunResourceStore resolves to A2's brand root."""
    project, uid, a1_id, a2_id = _setup
    ws = WorkspaceContext.for_brand(uid, a2_id, project)
    token = set_workspace(ws)
    try:
        store = RunResourceStore(project)
        expected = project / "users" / uid / "brands" / a2_id / "cache" / "run_resources"
        assert store.storage_dir == expected
    finally:
        reset_workspace(token)


def test_a1_resources_unavailable_to_a2(_setup):
    """Resources uploaded under A1 must not be accessible under A2."""
    project, uid, a1_id, a2_id = _setup

    # Upload under A1
    ws_a1 = WorkspaceContext.for_brand(uid, a1_id, project)
    token_a1 = set_workspace(ws_a1)
    try:
        store_a1 = RunResourceStore(project)
        session_id = store_a1.create_upload_session()
        rec = store_a1.upload(filename="test.txt", content=b"A1 data", session_id=session_id)
        a1_resource_id = rec["resource_id"]
        assert a1_resource_id is not None
    finally:
        reset_workspace(token_a1)

    # Try to access under A2
    ws_a2 = WorkspaceContext.for_brand(uid, a2_id, project)
    token_a2 = set_workspace(ws_a2)
    try:
        store_a2 = RunResourceStore(project)
        result = store_a2.get_resource(a1_resource_id, session_id)
        assert result is None, "A1 resource must not be accessible from A2 context"
    finally:
        reset_workspace(token_a2)


# --- No brand → fail closed ---

def test_upload_without_brand_context_raises(_setup):
    """Upload without brand context must raise, not fall back to user root."""
    project, uid, a1_id, a2_id = _setup
    store = RunResourceStore(project)
    with pytest.raises(ValueError, match="brand_state_root"):
        store.upload(filename="test.txt", content=b"data")


def test_no_user_root_fallback_directory(_setup):
    """No run_resources directory should appear under user root (only under brand root)."""
    project, uid, a1_id, a2_id = _setup

    # Use A1 brand context
    ws = WorkspaceContext.for_brand(uid, a1_id, project)
    token = set_workspace(ws)
    try:
        store = RunResourceStore(project)
        session_id = store.create_upload_session()
        store.upload(filename="test.txt", content=b"brand data", session_id=session_id)
    finally:
        reset_workspace(token)

    # Check that no run_resources directory exists under user root
    user_root = project / "users" / uid
    user_cache = user_root / "cache" / "run_resources"
    assert not user_cache.exists(), (
        "run_resources must not exist under user root — only under brand root"
    )

    # It should exist under brand root
    brand_cache = user_root / "brands" / a1_id / "cache" / "run_resources"
    assert brand_cache.exists(), "run_resources should exist under A1 brand root"
