"""BrandRegistry — JSON-backed, user-scoped brand ownership + CRUD.

Seam under test: the public interface of BrandRegistry, exercised through
a tmp_path project root (same pattern as test_workspace_context.py).

MB-01: proves user/brand isolation, immutable brand_id, ownership
verification, and the brand-scoped storage contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.brand_registry import BrandRegistry
from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def user_a_registry(tmp_path: Path) -> BrandRegistry:
    return BrandRegistry(user_id="user_aaa", project_root=tmp_path)


@pytest.fixture
def user_b_registry(tmp_path: Path) -> BrandRegistry:
    return BrandRegistry(user_id="user_bbb", project_root=tmp_path)


def _set_user_ws(user_id: str, project_root: Path):
    """Context manager setting the authenticated user's workspace."""
    ws = WorkspaceContext.for_user(user_id, project_root)
    token = set_workspace(ws)
    return token


# ---------------------------------------------------------------------------
# 1. create — basic shape
# ---------------------------------------------------------------------------

def test_create_returns_brand_with_id_and_name(user_a_registry: BrandRegistry):
    brand = user_a_registry.create("Acme")
    assert brand["name"] == "Acme"
    assert brand["brand_id"]
    assert brand["user_id"] == "user_aaa"


def test_create_persists_to_user_scoped_path(tmp_path: Path):
    reg = BrandRegistry(user_id="user_aaa", project_root=tmp_path)
    reg.create("Acme")
    registry_file = tmp_path / "users" / "user_aaa" / "brand_registry.json"
    assert registry_file.exists(), "brand registry must live under users/<user_id>/"
    data = json.loads(registry_file.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["name"] == "Acme"


def test_create_has_timestamps(user_a_registry: BrandRegistry):
    brand = user_a_registry.create("Acme")
    assert brand["created_at"]
    assert brand["updated_at"]


def test_create_default_status_active(user_a_registry: BrandRegistry):
    brand = user_a_registry.create("Acme")
    assert brand["status"] == "active"


# ---------------------------------------------------------------------------
# 2. brand_id — immutable, internally generated, filesystem-safe
# ---------------------------------------------------------------------------

def test_brand_id_generated_internally(user_a_registry: BrandRegistry):
    brand = user_a_registry.create("Acme")
    assert brand["brand_id"] != "Acme", "brand_id must not be the display name"


def test_brand_id_unique(user_a_registry: BrandRegistry):
    b1 = user_a_registry.create("Acme")
    b2 = user_a_registry.create("Acme")
    assert b1["brand_id"] != b2["brand_id"]


def test_brand_id_filesystem_safe(user_a_registry: BrandRegistry):
    """brand_id must be safe to use as a path component."""
    import re
    for _ in range(20):
        brand = user_a_registry.create(f"Brand {_}")
        assert re.match(r"^[A-Za-z0-9_-]+$", brand["brand_id"]), \
            f"brand_id not filesystem-safe: {brand['brand_id']!r}"


def test_display_name_unicode_thai_works(user_a_registry: BrandRegistry):
    brand = user_a_registry.create("แบรนด์ทดสอบ")
    assert brand["name"] == "แบรนด์ทดสอบ"
    fetched = user_a_registry.get(brand["brand_id"])
    assert fetched["name"] == "แบรนด์ทดสอบ"


def test_duplicate_display_names_allowed(user_a_registry: BrandRegistry):
    """No existing product requirement forbids duplicate display names."""
    b1 = user_a_registry.create("Same Name")
    b2 = user_a_registry.create("Same Name")
    assert b1["brand_id"] != b2["brand_id"]
    assert len(user_a_registry.list()) == 2


def test_rename_does_not_change_brand_id(user_a_registry: BrandRegistry):
    brand = user_a_registry.create("Acme")
    original_id = brand["brand_id"]
    assert user_a_registry.rename(brand["brand_id"], "Acme Co")
    fetched = user_a_registry.get(original_id)
    assert fetched["name"] == "Acme Co"
    assert fetched["brand_id"] == original_id, "brand_id must be immutable"


# ---------------------------------------------------------------------------
# 3. list — only the authenticated user's brands
# ---------------------------------------------------------------------------

def test_list_empty(user_a_registry: BrandRegistry):
    assert user_a_registry.list() == []


def test_list_returns_only_this_users_brands(user_a_registry, user_b_registry):
    user_a_registry.create("A1")
    user_a_registry.create("A2")
    user_b_registry.create("B1")
    a_brands = user_a_registry.list()
    b_brands = user_b_registry.list()
    assert {b["name"] for b in a_brands} == {"A1", "A2"}
    assert {b["name"] for b in b_brands} == {"B1"}


def test_list_excludes_archived(user_a_registry: BrandRegistry):
    b1 = user_a_registry.create("Active")
    b2 = user_a_registry.create("To Archive")
    user_a_registry.archive(b2["brand_id"])
    names = {b["name"] for b in user_a_registry.list()}
    assert names == {"Active"}


# ---------------------------------------------------------------------------
# 4. get — ownership-verified
# ---------------------------------------------------------------------------

def test_get_returns_brand_by_id(user_a_registry: BrandRegistry):
    brand = user_a_registry.create("Acme")
    fetched = user_a_registry.get(brand["brand_id"])
    assert fetched is not None
    assert fetched["name"] == "Acme"


def test_get_unknown_returns_none(user_a_registry: BrandRegistry):
    assert user_a_registry.get("nonexistent") is None


def test_get_archived_returns_brand(user_a_registry: BrandRegistry):
    """Archive is not delete — archived brands are still retrievable via management lookup."""
    brand = user_a_registry.create("Acme")
    user_a_registry.archive(brand["brand_id"])
    fetched = user_a_registry.get(brand["brand_id"], active_only=False)
    assert fetched is not None
    assert fetched["status"] == "archived"


# ---------------------------------------------------------------------------
# 5. CROSS-USER ISOLATION — the core security proof
# ---------------------------------------------------------------------------

def test_user_a_cannot_get_user_b_brand(user_a_registry, user_b_registry):
    b_brand = user_b_registry.create("B Secret")
    # User A tries to get B's brand using A's own registry
    assert user_a_registry.get(b_brand["brand_id"]) is None


def test_user_a_cannot_rename_user_b_brand(user_a_registry, user_b_registry):
    b_brand = user_b_registry.create("B Secret")
    assert user_a_registry.rename(b_brand["brand_id"], "Hacked") is False
    # B's brand name unchanged
    assert user_b_registry.get(b_brand["brand_id"])["name"] == "B Secret"


def test_user_a_cannot_archive_user_b_brand(user_a_registry, user_b_registry):
    b_brand = user_b_registry.create("B Secret")
    assert user_a_registry.archive(b_brand["brand_id"]) is False
    assert user_b_registry.get(b_brand["brand_id"])["status"] == "active"


def test_user_b_cannot_operate_user_a_brands(user_a_registry, user_b_registry):
    a1 = user_a_registry.create("A1")
    a2 = user_a_registry.create("A2")
    assert user_b_registry.get(a1["brand_id"]) is None
    assert user_b_registry.rename(a2["brand_id"], "Stolen") is False
    assert user_b_registry.archive(a1["brand_id"]) is False


def test_brand_registry_files_are_separate_per_user(tmp_path: Path):
    reg_a = BrandRegistry(user_id="user_aaa", project_root=tmp_path)
    reg_b = BrandRegistry(user_id="user_bbb", project_root=tmp_path)
    reg_a.create("A Brand")
    reg_b.create("B Brand")
    file_a = tmp_path / "users" / "user_aaa" / "brand_registry.json"
    file_b = tmp_path / "users" / "user_bbb" / "brand_registry.json"
    data_a = json.loads(file_a.read_text(encoding="utf-8"))
    data_b = json.loads(file_b.read_text(encoding="utf-8"))
    assert len(data_a) == 1 and data_a[0]["name"] == "A Brand"
    assert len(data_b) == 1 and data_b[0]["name"] == "B Brand"


# ---------------------------------------------------------------------------
# 6. archive — not delete (brand_id not reused)
# ---------------------------------------------------------------------------

def test_archive_sets_status_and_keeps_record(user_a_registry: BrandRegistry):
    brand = user_a_registry.create("Acme")
    assert user_a_registry.archive(brand["brand_id"]) is True
    # Archived brands are not returned by default get(); use active_only=False
    fetched = user_a_registry.get(brand["brand_id"], active_only=False)
    assert fetched["status"] == "archived"


def test_archive_unknown_returns_false(user_a_registry: BrandRegistry):
    assert user_a_registry.archive("nonexistent") is False


def test_archived_brand_id_not_reused(user_a_registry: BrandRegistry):
    brand = user_a_registry.create("Acme")
    bid = brand["brand_id"]
    user_a_registry.archive(bid)
    # Creating a new brand must not reuse the archived brand_id
    new_brand = user_a_registry.create("New Acme")
    assert new_brand["brand_id"] != bid


# ---------------------------------------------------------------------------
# 6b. Archived brands — active_only contract
# ---------------------------------------------------------------------------

def test_get_archived_default_returns_none(user_a_registry: BrandRegistry):
    """Default get() must return None for archived brands (active_only=True)."""
    brand = user_a_registry.create("Acme")
    user_a_registry.archive(brand["brand_id"])
    assert user_a_registry.get(brand["brand_id"]) is None


def test_get_archived_with_active_only_false(user_a_registry: BrandRegistry):
    """Management lookup using active_only=False retrieves archived record."""
    brand = user_a_registry.create("Acme")
    user_a_registry.archive(brand["brand_id"])
    fetched = user_a_registry.get(brand["brand_id"], active_only=False)
    assert fetched is not None
    assert fetched["status"] == "archived"
    assert fetched["brand_id"] == brand["brand_id"]


def test_rename_bumps_updated_at(user_a_registry: BrandRegistry):
    brand = user_a_registry.create("Acme")
    old_updated = brand["updated_at"]
    user_a_registry.rename(brand["brand_id"], "Acme Co")
    fetched = user_a_registry.get(brand["brand_id"])
    assert fetched["updated_at"] != old_updated


# ---------------------------------------------------------------------------
# 7. Does not trust caller-supplied user_id — uses registry's bound user_id
# ---------------------------------------------------------------------------

def test_registry_user_id_is_authoritative(tmp_path: Path):
    """The registry's user_id (from authenticated context) is authoritative.
    A registry bound to user_aaa only ever reads/writes user_aaa's file,
    regardless of any other user_id floating around.
    """
    reg = BrandRegistry(user_id="user_aaa", project_root=tmp_path)
    reg.create("A Brand")
    # Even if someone constructs a registry with a different user_id,
    # it reads a different file — no cross-contamination.
    reg_hacker = BrandRegistry(user_id="user_hacker", project_root=tmp_path)
    assert reg_hacker.list() == []
    assert reg_hacker.get("anything") is None
