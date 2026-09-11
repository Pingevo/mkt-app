"""Brand-aware WorkspaceContext — user root vs brand root, propagation, fail-closed.

MB-01: proves the explicit user-root / brand-root invariant, thread
propagation of brand_id, and fail-closed behavior for missing brand context.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.brand_registry import BrandRegistry
from src.workspace_context import (
    WorkspaceContext,
    set_workspace,
    reset_workspace,
    get_workspace,
    user_state_root,
    brand_state_root,
    require_brand_context,
    with_workspace_context,
)


# ---------------------------------------------------------------------------
# Fixtures — create User A / Brand A1, Brand A2; User B / Brand B1
# ---------------------------------------------------------------------------

@pytest.fixture
def three_brands(tmp_path: Path):
    """Returns (project_root, user_a_id, a1_id, a2_id, user_b_id, b1_id)."""
    reg_a = BrandRegistry(user_id="user_aaa", project_root=tmp_path)
    reg_b = BrandRegistry(user_id="user_bbb", project_root=tmp_path)
    a1 = reg_a.create("A1")
    a2 = reg_a.create("A2")
    b1 = reg_b.create("B1")
    return (tmp_path, "user_aaa", a1["brand_id"], a2["brand_id"],
            "user_bbb", b1["brand_id"])


# ---------------------------------------------------------------------------
# 1. user_state_root — unchanged semantics, always user root
# ---------------------------------------------------------------------------

def test_user_state_root_same_for_both_brands(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    ws_a1 = WorkspaceContext.for_brand(uid_a, a1_id, project)
    ws_a2 = WorkspaceContext.for_brand(uid_a, a2_id, project)
    token1 = set_workspace(ws_a1)
    try:
        root1 = user_state_root()
    finally:
        reset_workspace(token1)
    token2 = set_workspace(ws_a2)
    try:
        root2 = user_state_root()
    finally:
        reset_workspace(token2)
    assert root1 == root2, "user_state_root must be same for both brands of same user"
    assert root1 == (project / "users" / uid_a).resolve()


def test_user_state_root_is_user_root_not_brand_root(three_brands, tmp_path):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    ws = WorkspaceContext.for_brand(uid_a, a1_id, project)
    token = set_workspace(ws)
    try:
        assert user_state_root() == (project / "users" / uid_a).resolve()
        assert user_state_root() != (project / "users" / uid_a / "brands" / a1_id).resolve()
    finally:
        reset_workspace(token)


# ---------------------------------------------------------------------------
# 2. brand_state_root — distinct per brand, beneath user root
# ---------------------------------------------------------------------------

def test_brand_state_root_distinct_per_brand_same_user(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    ws_a1 = WorkspaceContext.for_brand(uid_a, a1_id, project)
    ws_a2 = WorkspaceContext.for_brand(uid_a, a2_id, project)
    token1 = set_workspace(ws_a1)
    try:
        br1 = brand_state_root()
    finally:
        reset_workspace(token1)
    token2 = set_workspace(ws_a2)
    try:
        br2 = brand_state_root()
    finally:
        reset_workspace(token2)
    assert br1 != br2


def test_brand_state_root_distinct_across_users(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    ws_a1 = WorkspaceContext.for_brand(uid_a, a1_id, project)
    ws_b1 = WorkspaceContext.for_brand(uid_b, b1_id, project)
    token1 = set_workspace(ws_a1)
    try:
        br_a1 = brand_state_root()
    finally:
        reset_workspace(token1)
    token2 = set_workspace(ws_b1)
    try:
        br_b1 = brand_state_root()
    finally:
        reset_workspace(token2)
    assert br_a1 != br_b1


def test_brand_root_beneath_user_root(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    ws = WorkspaceContext.for_brand(uid_a, a1_id, project)
    token = set_workspace(ws)
    try:
        ur = user_state_root()
        br = brand_state_root()
        assert br.is_relative_to(ur), "brand root must be beneath user root"
        assert br == (ur / "brands" / a1_id).resolve()
    finally:
        reset_workspace(token)


# ---------------------------------------------------------------------------
# 3. for_brand — carries brand_id, ownership-verified
# ---------------------------------------------------------------------------

def test_for_brand_sets_brand_id(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    ws = WorkspaceContext.for_brand(uid_a, a1_id, project)
    assert ws.brand_id == a1_id
    assert ws.user_id == uid_a


def test_for_user_has_no_brand_id(tmp_path):
    ws = WorkspaceContext.for_user("user_aaa", tmp_path)
    assert ws.brand_id is None


def test_for_brand_unknown_brand_fails_closed(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    with pytest.raises(ValueError):
        WorkspaceContext.for_brand(uid_a, "nonexistent_brand", project)


def test_for_brand_wrong_user_fails_closed(three_brands):
    """User B cannot establish a brand context for User A's brand."""
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    with pytest.raises(ValueError):
        WorkspaceContext.for_brand(uid_b, a1_id, project)


def test_for_brand_rejects_archived(three_brands):
    """for_brand must reject an archived brand — archived brands must not
    establish a brand context."""
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    # Archive A1
    reg = BrandRegistry(user_id=uid_a, project_root=project)
    reg.archive(a1_id)
    # for_brand must raise ValueError for the archived brand
    with pytest.raises(ValueError):
        WorkspaceContext.for_brand(uid_a, a1_id, project)
    # A2 (still active) must still work
    ws_a2 = WorkspaceContext.for_brand(uid_a, a2_id, project)
    assert ws_a2.brand_id == a2_id


# ---------------------------------------------------------------------------
# 4. Propagation — worker sees user + brand + correct roots
# ---------------------------------------------------------------------------

def test_with_workspace_context_propagates_brand(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    ws = WorkspaceContext.for_brand(uid_a, a1_id, project)
    token = set_workspace(ws)
    try:
        seen: dict = {}
        def worker():
            w = get_workspace()
            seen["user_id"] = w.user_id if w else None
            seen["brand_id"] = w.brand_id if w else None
            seen["user_root"] = user_state_root()
            seen["brand_root"] = brand_state_root()

        wrapped = with_workspace_context(worker)
        t = threading.Thread(target=wrapped, daemon=True)
        t.start()
        t.join()

        assert seen["user_id"] == uid_a
        assert seen["brand_id"] == a1_id
        assert seen["user_root"] == (project / "users" / uid_a).resolve()
        assert seen["brand_root"] == (project / "users" / uid_a / "brands" / a1_id).resolve()
    finally:
        reset_workspace(token)


def test_no_fallback_to_other_brand(three_brands):
    """Worker must not fall back to A2 or default when A1 is active."""
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    ws = WorkspaceContext.for_brand(uid_a, a1_id, project)
    token = set_workspace(ws)
    try:
        seen: dict = {}
        def worker():
            w = get_workspace()
            seen["brand_id"] = w.brand_id if w else None
            seen["brand_root"] = brand_state_root()

        wrapped = with_workspace_context(worker)
        t = threading.Thread(target=wrapped, daemon=True)
        t.start()
        t.join()

        assert seen["brand_id"] == a1_id
        assert seen["brand_id"] != a2_id
        a2_root = (project / "users" / uid_a / "brands" / a2_id).resolve()
        assert seen["brand_root"] != a2_root
    finally:
        reset_workspace(token)


# ---------------------------------------------------------------------------
# 5. Fail-closed — missing brand context
# ---------------------------------------------------------------------------

def test_require_brand_context_user_only_fails_closed(tmp_path):
    """User context with no brand → require_brand_context raises."""
    ws = WorkspaceContext.for_user("user_aaa", tmp_path)
    token = set_workspace(ws)
    try:
        with pytest.raises(ValueError):
            require_brand_context()
    finally:
        reset_workspace(token)


def test_require_brand_context_no_workspace_fails_closed(tmp_path):
    """No workspace at all → require_brand_context raises."""
    assert get_workspace() is None
    with pytest.raises(ValueError):
        require_brand_context()


def test_require_brand_context_with_brand_succeeds(three_brands):
    project, uid_a, a1_id, a2_id, uid_b, b1_id = three_brands
    ws = WorkspaceContext.for_brand(uid_a, a1_id, project)
    token = set_workspace(ws)
    try:
        ctx = require_brand_context()
        assert ctx.user_id == uid_a
        assert ctx.brand_id == a1_id
    finally:
        reset_workspace(token)


# ---------------------------------------------------------------------------
# 6. Stage A backward compatibility — for_user unchanged
# ---------------------------------------------------------------------------

def test_for_user_still_works_without_brand(tmp_path):
    """Stage A: for_user must still produce a valid user-only context."""
    ws = WorkspaceContext.for_user("user_abc", tmp_path)
    assert ws.user_id == "user_abc"
    assert ws.brand_id is None
    assert ws.root == (tmp_path / "users" / "user_abc").resolve()


def test_user_state_root_without_workspace_unchanged(tmp_path):
    """Stage A: user_state_root without workspace falls back to project_root."""
    assert get_workspace() is None
    assert user_state_root(tmp_path) == tmp_path


def test_brand_state_root_without_brand_fails_closed(tmp_path):
    """brand_state_root without a brand context must fail closed, not guess."""
    ws = WorkspaceContext.for_user("user_aaa", tmp_path)
    token = set_workspace(ws)
    try:
        with pytest.raises(ValueError):
            brand_state_root()
    finally:
        reset_workspace(token)


def test_brand_state_root_without_workspace_fails_closed(tmp_path):
    assert get_workspace() is None
    with pytest.raises(ValueError):
        brand_state_root()
