"""WorkspaceContext + security tests — path traversal, cross-user isolation."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.workspace_context import (
    WorkspaceContext,
    set_workspace,
    reset_workspace,
    get_workspace,
    user_state_root,
    _sanitize_user_id,
)


# --- Sanitization ---

def test_safe_user_id_accepted():
    assert _sanitize_user_id("user_abc123") == "user_abc123"
    assert _sanitize_user_id("user-A-B_123") == "user-A-B_123"


def test_path_traversal_rejected():
    """``..`` in user_id must be rejected."""
    with pytest.raises(ValueError):
        _sanitize_user_id("../etc/passwd")
    with pytest.raises(ValueError):
        _sanitize_user_id("user/../../etc")
    with pytest.raises(ValueError):
        _sanitize_user_id("")


def test_absolute_path_injection_rejected():
    with pytest.raises(ValueError):
        _sanitize_user_id("/etc/passwd")
    with pytest.raises(ValueError):
        _sanitize_user_id("user/absolute")


def test_special_chars_rejected():
    with pytest.raises(ValueError):
        _sanitize_user_id("user!@#")
    with pytest.raises(ValueError):
        _sanitize_user_id("user with spaces")


# --- WorkspaceContext ---

def test_workspace_root_under_project(tmp_path: Path):
    ws = WorkspaceContext.for_user("user_abc", tmp_path)
    assert ws.root == (tmp_path / "users" / "user_abc").resolve()
    assert ws.user_id == "user_abc"


def test_workspace_path_traversal_blocked(tmp_path: Path):
    """A user_id with path traversal must not escape project_root."""
    with pytest.raises(ValueError):
        WorkspaceContext.for_user("../escape", tmp_path)
    with pytest.raises(ValueError):
        WorkspaceContext.for_user("user/../../escape", tmp_path)


def test_workspace_convenience_paths(tmp_path: Path):
    ws = WorkspaceContext.for_user("user_abc", tmp_path)
    assert ws.cache_dir() == ws.root / "cache"
    assert ws.data_dir() == ws.root / "data"
    assert ws.output_dir() == ws.root / "output"
    assert ws.logs_dir() == ws.root / "logs"
    assert ws.workspace_local_dir() == ws.root / "workspace" / "local"
    assert ws.workspace_local_config_dir() == ws.workspace_local_dir() / "config"
    assert ws.workspace_local_brand_dir() == ws.workspace_local_dir() / "brand"


# --- Context var ---

def test_context_var_set_get_reset():
    ws = WorkspaceContext.for_user("user_test", Path("/tmp/fake_project"))
    token = set_workspace(ws)
    assert get_workspace() is ws
    reset_workspace(token)
    assert get_workspace() is None


def test_user_state_root_with_workspace(tmp_path: Path):
    ws = WorkspaceContext.for_user("user_abc", tmp_path)
    token = set_workspace(ws)
    try:
        assert user_state_root() == ws.root
    finally:
        reset_workspace(token)


def test_user_state_root_without_workspace(tmp_path: Path):
    assert get_workspace() is None
    assert user_state_root(tmp_path) == tmp_path


def test_two_users_different_roots(tmp_path: Path):
    ws_a = WorkspaceContext.for_user("user_aaa", tmp_path)
    ws_b = WorkspaceContext.for_user("user_bbb", tmp_path)
    assert ws_a.root != ws_b.root
    assert ws_a.root.parent == ws_b.root.parent  # both under users/


# --- Two-user state isolation: product_db ---

def test_two_user_product_isolation(tmp_path: Path):
    """User A's product data must not be visible to User B."""
    from src import product_db

    ws_a = WorkspaceContext.for_user("user_aaa", tmp_path)
    ws_b = WorkspaceContext.for_user("user_bbb", tmp_path)

    # User A creates a product
    token = set_workspace(ws_a)
    try:
        product_db.set_status("ProductA", product_db.STATUS_READY)
        rec = product_db.load("ProductA")
        rec["raw_text"] = "User A's private product data"
        product_db.save("ProductA", rec)
    finally:
        reset_workspace(token)

    # User B should not see User A's product
    token = set_workspace(ws_b)
    try:
        rec_b = product_db.load("ProductA")
        assert rec_b.get("raw_text", "") == "", "User B must not see User A's product data"
    finally:
        reset_workspace(token)


# --- Two-user state isolation: content_history ---

def test_two_user_content_history_isolation(tmp_path: Path):
    """User A's content history must not be visible to User B."""
    from src import content_history

    ws_a = WorkspaceContext.for_user("user_aaa", tmp_path)
    ws_b = WorkspaceContext.for_user("user_bbb", tmp_path)

    token = set_workspace(ws_a)
    try:
        content_history.record_entry(
            tmp_path, "ProductA", "test concept", "facebook", "test caption",
            output_file="output_a.md",
        )
    finally:
        reset_workspace(token)

    token = set_workspace(ws_b)
    try:
        entries = content_history.load_history(tmp_path).get("entries", [])
        assert len(entries) == 0, "User B must not see User A's content history"
    finally:
        reset_workspace(token)


# --- New-user clean defaults ---

def test_new_user_starts_clean(tmp_path: Path):
    """A brand-new user must start with zero products, zero history."""
    from src import product_db, content_history

    # First, populate User A's state
    ws_a = WorkspaceContext.for_user("user_aaa", tmp_path)
    token = set_workspace(ws_a)
    try:
        product_db.set_status("ProductA", product_db.STATUS_READY)
        rec = product_db.load("ProductA")
        rec["raw_text"] = "contaminated data"
        product_db.save("ProductA", rec)
        content_history.record_entry(
            tmp_path, "ProductA", "test concept", "facebook", "test caption",
            output_file="output.md",
        )
    finally:
        reset_workspace(token)

    # New user B must start clean
    ws_b = WorkspaceContext.for_user("user_new", tmp_path)
    token = set_workspace(ws_b)
    try:
        rec_b = product_db.load("ProductA")
        assert rec_b.get("raw_text", "") == "", "New user must not inherit contaminated state"
        entries = content_history.load_history(tmp_path).get("entries", [])
        assert len(entries) == 0, "New user must not inherit content history"
    finally:
        reset_workspace(token)


# --- Factory config integrity: global, read-only ---

def test_factory_config_not_in_user_workspace(tmp_path: Path):
    """Factory config (config/) must NOT be redirected to per-user workspace."""
    ws = WorkspaceContext.for_user("user_aaa", tmp_path)
    # Factory config dir is always the project root's config/, not per-user
    factory_config = tmp_path / "config"
    user_config_like = ws.root / "config"
    assert factory_config != user_config_like, \
        "Factory config must remain global, not per-user"


def test_factory_config_unchanged_after_user_state(tmp_path: Path):
    """Writing user state must not mutate factory config files."""
    import json
    from src import product_db

    factory_config = tmp_path / "config" / "agent_instructions.json"
    factory_config.parent.mkdir(parents=True, exist_ok=True)
    factory_config.write_text(json.dumps({"_presets": {}, "product_spec": {}}), encoding="utf-8")
    hash_before = factory_config.read_bytes().__hash__()

    ws = WorkspaceContext.for_user("user_aaa", tmp_path)
    token = set_workspace(ws)
    try:
        product_db.set_status("ProductA", product_db.STATUS_READY)
        rec = product_db.load("ProductA")
        rec["raw_text"] = "user data"
        product_db.save("ProductA", rec)
    finally:
        reset_workspace(token)

    hash_after = factory_config.read_bytes().__hash__()
    assert hash_before == hash_after, "Factory config must not be mutated by user state writes"
