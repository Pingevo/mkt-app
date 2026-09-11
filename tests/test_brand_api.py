"""Brand API — MB-01 minimal CRUD + select/activate, ownership-verified.

Exercises the real FastAPI endpoints via TestClient.  Proves:
- User A lists/creates/gets/renames/archives own brands
- User A cannot operate User B's brands via API
- Select sets a cookie; subsequent requests carry brand_id
- Missing/invalid brand selection fails closed for brand-scoped ops
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import make_authed_client


def _make_two_authed_clients(web_viewer, tmp_path, monkeypatch):
    """Create two authenticated clients (User A, User B)."""
    import src.auth as auth_mod
    from src.auth import UserStore, SessionManager

    users_path = tmp_path / "data" / "auth" / "users.json"
    sessions_path = tmp_path / "data" / "auth" / "sessions.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    sess = SessionManager(sessions_path)
    monkeypatch.setattr(auth_mod, "_user_store", store)
    monkeypatch.setattr(auth_mod, "_session_manager", sess)

    user_a = store.register("userA", "passA")
    user_b = store.register("userB", "passB")

    from starlette.testclient import TestClient
    client_a = TestClient(web_viewer.app)
    resp = client_a.post("/api/auth/login", json={"username": "userA", "password": "passA"})
    assert resp.status_code == 200
    client_b = TestClient(web_viewer.app)
    resp = client_b.post("/api/auth/login", json={"username": "userB", "password": "passB"})
    assert resp.status_code == 200
    return client_a, user_a.user_id, client_b, user_b.user_id


@pytest.fixture
def _web_viewer(tmp_path, monkeypatch):
    import web_viewer
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    return web_viewer


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_create_and_list_brands(_web_viewer, tmp_path, monkeypatch):
    client_a, uid_a, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_a.post("/api/brands", json={"name": "Acme"})
    assert resp.status_code == 200
    brand = resp.json()
    assert brand["name"] == "Acme"
    assert brand["brand_id"]
    assert brand["user_id"] == uid_a

    resp = client_a.get("/api/brands")
    assert resp.status_code == 200
    brands = resp.json()["brands"]
    assert len(brands) == 1
    assert brands[0]["name"] == "Acme"


def test_get_brand(_web_viewer, tmp_path, monkeypatch):
    client_a, _, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_a.post("/api/brands", json={"name": "Acme"})
    bid = resp.json()["brand_id"]
    resp = client_a.get(f"/api/brands/{bid}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Acme"


def test_rename_brand(_web_viewer, tmp_path, monkeypatch):
    client_a, _, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_a.post("/api/brands", json={"name": "Acme"})
    bid = resp.json()["brand_id"]
    resp = client_a.patch(f"/api/brands/{bid}", json={"name": "Acme Co"})
    assert resp.status_code == 200
    resp = client_a.get(f"/api/brands/{bid}")
    assert resp.json()["name"] == "Acme Co"


def test_archive_brand(_web_viewer, tmp_path, monkeypatch):
    client_a, _, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_a.post("/api/brands", json={"name": "Acme"})
    bid = resp.json()["brand_id"]
    resp = client_a.delete(f"/api/brands/{bid}")
    assert resp.status_code == 200
    # Archived brand not in list
    resp = client_a.get("/api/brands")
    assert len(resp.json()["brands"]) == 0
    # But still retrievable
    resp = client_a.get(f"/api/brands/{bid}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"


def test_get_unknown_brand_404(_web_viewer, tmp_path, monkeypatch):
    client_a, _, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_a.get("/api/brands/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cross-user isolation via API
# ---------------------------------------------------------------------------

def test_user_a_cannot_get_user_b_brand(_web_viewer, tmp_path, monkeypatch):
    client_a, _, client_b, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_b.post("/api/brands", json={"name": "B Secret"})
    bid = resp.json()["brand_id"]
    # User A tries to get B's brand
    resp = client_a.get(f"/api/brands/{bid}")
    assert resp.status_code == 404


def test_user_a_cannot_rename_user_b_brand(_web_viewer, tmp_path, monkeypatch):
    client_a, _, client_b, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_b.post("/api/brands", json={"name": "B Secret"})
    bid = resp.json()["brand_id"]
    resp = client_a.patch(f"/api/brands/{bid}", json={"name": "Hacked"})
    assert resp.status_code == 404
    # B's brand unchanged
    resp = client_b.get(f"/api/brands/{bid}")
    assert resp.json()["name"] == "B Secret"


def test_user_a_cannot_archive_user_b_brand(_web_viewer, tmp_path, monkeypatch):
    client_a, _, client_b, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_b.post("/api/brands", json={"name": "B Secret"})
    bid = resp.json()["brand_id"]
    resp = client_a.delete(f"/api/brands/{bid}")
    assert resp.status_code == 404
    resp = client_b.get(f"/api/brands/{bid}")
    assert resp.json()["status"] == "active"


def test_user_a_cannot_select_user_b_brand(_web_viewer, tmp_path, monkeypatch):
    client_a, _, client_b, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_b.post("/api/brands", json={"name": "B Secret"})
    bid = resp.json()["brand_id"]
    resp = client_a.post(f"/api/brands/{bid}/select")
    assert resp.status_code == 404


def test_user_lists_only_own_brands(_web_viewer, tmp_path, monkeypatch):
    client_a, _, client_b, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    client_a.post("/api/brands", json={"name": "A1"})
    client_a.post("/api/brands", json={"name": "A2"})
    client_b.post("/api/brands", json={"name": "B1"})
    a_brands = client_a.get("/api/brands").json()["brands"]
    b_brands = client_b.get("/api/brands").json()["brands"]
    assert {b["name"] for b in a_brands} == {"A1", "A2"}
    assert {b["name"] for b in b_brands} == {"B1"}


# ---------------------------------------------------------------------------
# Select / activate / deselect
# ---------------------------------------------------------------------------

def test_select_sets_brand_context(_web_viewer, tmp_path, monkeypatch):
    client_a, uid_a, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_a.post("/api/brands", json={"name": "Acme"})
    bid = resp.json()["brand_id"]
    resp = client_a.post(f"/api/brands/{bid}/select")
    assert resp.status_code == 200
    # Active brand endpoint reflects selection
    resp = client_a.get("/api/brands/active")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] is True
    assert data["brand"]["brand_id"] == bid


def test_deselect_clears_brand_context(_web_viewer, tmp_path, monkeypatch):
    client_a, _, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_a.post("/api/brands", json={"name": "Acme"})
    bid = resp.json()["brand_id"]
    client_a.post(f"/api/brands/{bid}/select")
    resp = client_a.post("/api/brands/deselect")
    assert resp.status_code == 200
    resp = client_a.get("/api/brands/active")
    assert resp.json()["active"] is False


def test_invalid_brand_cookie_fails_closed(_web_viewer, tmp_path, monkeypatch):
    """A tampered brand cookie (unknown brand) must not grant brand context."""
    client_a, _, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    client_a.cookies.set("mktapp_brand", "nonexistent_brand", domain="testserver")
    resp = client_a.get("/api/brands/active")
    assert resp.status_code == 200
    assert resp.json()["active"] is False


def test_brand_cookie_from_other_user_fails_closed(_web_viewer, tmp_path, monkeypatch):
    """User A with User B's brand_id in cookie must not get brand context."""
    client_a, _, client_b, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_b.post("/api/brands", json={"name": "B Secret"})
    bid = resp.json()["brand_id"]
    client_a.cookies.set("mktapp_brand", bid, domain="testserver")
    resp = client_a.get("/api/brands/active")
    assert resp.json()["active"] is False


# ---------------------------------------------------------------------------
# Archived brand — must NOT be selectable or activatable
# ---------------------------------------------------------------------------

def test_archived_brand_cannot_be_selected(_web_viewer, tmp_path, monkeypatch):
    """POST select on an archived brand must return 404 and NOT set cookie."""
    client_a, _, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_a.post("/api/brands", json={"name": "To Archive"})
    bid = resp.json()["brand_id"]
    # Archive it
    resp = client_a.delete(f"/api/brands/{bid}")
    assert resp.status_code == 200
    # Select must fail
    resp = client_a.post(f"/api/brands/{bid}/select")
    assert resp.status_code == 404
    # No brand cookie should have been set by the failed select
    # Verify active endpoint reports no active brand
    resp = client_a.get("/api/brands/active")
    assert resp.json()["active"] is False


def test_archived_brand_cookie_fails_closed(_web_viewer, tmp_path, monkeypatch):
    """A stale mktapp_brand cookie pointing to an archived brand must NOT
    reactivate it. Middleware must produce user-only context (brand_id=None)."""
    client_a, _, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    # Create and select an active brand
    resp = client_a.post("/api/brands", json={"name": "Will Archive"})
    bid = resp.json()["brand_id"]
    resp = client_a.post(f"/api/brands/{bid}/select")
    assert resp.status_code == 200
    # Confirm it's active
    resp = client_a.get("/api/brands/active")
    assert resp.json()["active"] is True
    # Archive it (cookie is still in the browser, now stale)
    resp = client_a.delete(f"/api/brands/{bid}")
    assert resp.status_code == 200
    # Stale cookie still present — middleware must NOT reactivate archived brand
    resp = client_a.get("/api/brands/active")
    assert resp.json()["active"] is False, \
        "Archived brand cookie must not reactivate brand context"


def test_archived_brand_get_via_management_endpoint(_web_viewer, tmp_path, monkeypatch):
    """GET /api/brands/{brand_id} on an archived brand should still return
    the record (management/history access), but it must NOT be selectable."""
    client_a, _, _, _ = _make_two_authed_clients(_web_viewer, tmp_path, monkeypatch)
    resp = client_a.post("/api/brands", json={"name": "To Archive"})
    bid = resp.json()["brand_id"]
    client_a.delete(f"/api/brands/{bid}")
    # Management endpoint retrieves archived brand
    resp = client_a.get(f"/api/brands/{bid}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    # But select still fails
    resp = client_a.post(f"/api/brands/{bid}/select")
    assert resp.status_code == 404
