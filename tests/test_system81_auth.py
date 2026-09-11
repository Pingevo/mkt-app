"""System81 auth — HTTP flow tests via FastAPI TestClient.

All System81 HTTP calls are mocked — no live network access.

Tests:
- GET /login displays System81 login button, no username/password form, no register tab
- GET /api/auth/login-url returns provider URL
- POST /api/auth/login with valid mocked token creates session cookie
- POST /api/auth/login with invalid token creates no session (401)
- POST /api/auth/login with forged/random token creates no session
- POST /api/auth/login does not accept user_id from client
- GET /api/auth/me works after login
- POST /api/auth/logout revokes session + clears session and brand cookies
- Authenticated System81 user gets correct WorkspaceContext + BrandRegistry
- Another user's brands not visible
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.system81 import ExternalIdentity


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _web_viewer(tmp_path, monkeypatch):
    """Import web_viewer with PROJECT_ROOT redirected to tmp_path."""
    import web_viewer
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    return web_viewer


@pytest.fixture
def _auth_stores(tmp_path, monkeypatch):
    """Set up isolated UserStore + SessionManager for testing."""
    import src.auth as auth_mod
    from src.auth import UserStore, SessionManager

    users_path = tmp_path / "data" / "auth" / "users.json"
    sessions_path = tmp_path / "data" / "auth" / "sessions.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    sess = SessionManager(sessions_path)
    monkeypatch.setattr(auth_mod, "_user_store", store)
    monkeypatch.setattr(auth_mod, "_session_manager", sess)
    return store, sess


def _mock_identity(sub="1001", username="testuser"):
    """Create a mock ExternalIdentity for testing."""
    return ExternalIdentity(
        user_id=f"system81_{sub}",
        username=username,
        email=f"{username}@test.com",
        provider="system81",
        external_subject=sub,
        metadata={"name": username.title()},
    )


def _make_client_with_system81_login(web_viewer, store, sess, monkeypatch, sub="1001", username="testuser"):
    """Create a TestClient and log in via System81 token (mocked).

    Returns (client, user_id).
    """
    identity = _mock_identity(sub, username)

    # Mock System81Provider to return our identity for any token
    from src.system81 import System81Provider
    original_init = System81Provider.__init__

    def _mock_verify(self, token):
        if token == "valid-mock-token":
            return identity
        return None

    monkeypatch.setattr(System81Provider, "verify_token", _mock_verify)

    client = TestClient(web_viewer.app)
    resp = client.post("/api/auth/login", json={"token": "valid-mock-token"})
    assert resp.status_code == 200, f"login failed: {resp.status_code} {resp.text}"
    return client, identity.user_id


# ---------------------------------------------------------------------------
# Login page UI
# ---------------------------------------------------------------------------

def test_login_page_shows_system81_button(_web_viewer):
    """GET /login must display a System81 login button."""
    client = TestClient(_web_viewer.app)
    resp = client.get("/login")
    assert resp.status_code == 200
    html = resp.text
    assert "System81" in html or "system81" in html.lower()


def test_login_page_no_username_field(_web_viewer):
    """GET /login must NOT show a username input field in production."""
    client = TestClient(_web_viewer.app)
    resp = client.get("/login")
    html = resp.text
    # No username/password inputs in production login
    assert 'id="username"' not in html
    assert 'type="password"' not in html


def test_login_page_no_register_tab(_web_viewer):
    """GET /login must NOT show a 'สมัครใหม่' / register tab."""
    client = TestClient(_web_viewer.app)
    resp = client.get("/login")
    html = resp.text
    assert "สมัครใหม่" not in html
    assert 'tab-register' not in html


# ---------------------------------------------------------------------------
# Login URL endpoint
# ---------------------------------------------------------------------------

def test_login_url_endpoint(_web_viewer, monkeypatch):
    """GET /api/auth/login-url must return the System81 provider URL."""
    monkeypatch.setenv("SELLERCENTER_OAUTH_BASE_URL", "https://s81.test.example")
    monkeypatch.setenv("SELLERCENTER_OAUTH_REDIRECT_URI", "http://localhost:8778/login")
    client = TestClient(_web_viewer.app)
    resp = client.get("/api/auth/login-url")
    assert resp.status_code == 200
    data = resp.json()
    assert "login_url" in data
    assert "s81.test.example" in data["login_url"]
    assert "/system81/login" in data["login_url"]


# ---------------------------------------------------------------------------
# Login with System81 token
# ---------------------------------------------------------------------------

def test_valid_token_creates_session(_web_viewer, _auth_stores, monkeypatch):
    """A valid mocked System81 token must create an MKTApp session cookie."""
    store, sess = _auth_stores
    client, uid = _make_client_with_system81_login(
        _web_viewer, store, sess, monkeypatch
    )
    assert uid == "system81_1001"
    # Session cookie must be set
    cookies = client.cookies
    assert "mktapp_session" in cookies
    # Profile must exist in store
    record = store.get_by_id(uid)
    assert record is not None
    assert record["username"] == "testuser"


def test_invalid_token_no_session(_web_viewer, _auth_stores, monkeypatch):
    """An invalid token must return 401 and create no session."""
    from src.system81 import System81Provider
    monkeypatch.setattr(System81Provider, "verify_token", lambda self, t: None)
    client = TestClient(_web_viewer.app)
    resp = client.post("/api/auth/login", json={"token": "bad-token"})
    assert resp.status_code == 401
    assert "mktapp_session" not in client.cookies


def test_forged_token_no_session(_web_viewer, _auth_stores, monkeypatch):
    """A forged/random token must not login."""
    from src.system81 import System81Provider
    monkeypatch.setattr(System81Provider, "verify_token", lambda self, t: None)
    client = TestClient(_web_viewer.app)
    resp = client.post("/api/auth/login", json={"token": "completely-forged-xyz"})
    assert resp.status_code == 401
    # No profile created
    assert _auth_stores[0].list_users() == []


def test_client_cannot_supply_user_id(_web_viewer, _auth_stores, monkeypatch):
    """The client must not be able to supply a user_id — only token is accepted."""
    from src.system81 import System81Provider
    monkeypatch.setattr(System81Provider, "verify_token", lambda self, t: None)
    client = TestClient(_web_viewer.app)
    # Try to login with user_id instead of token
    resp = client.post("/api/auth/login", json={"user_id": "system81_admin"})
    assert resp.status_code == 400
    assert "mktapp_session" not in client.cookies


def test_no_token_returns_400(_web_viewer, _auth_stores):
    """POST /api/auth/login without a token must return 400."""
    client = TestClient(_web_viewer.app)
    resp = client.post("/api/auth/login", json={})
    assert resp.status_code == 400


def test_failed_verification_creates_no_user(_web_viewer, _auth_stores, monkeypatch):
    """Failed userinfo verification must create no user/session."""
    from src.system81 import System81Provider
    monkeypatch.setattr(System81Provider, "verify_token", lambda self, t: None)
    client = TestClient(_web_viewer.app)
    resp = client.post("/api/auth/login", json={"token": "some-token"})
    assert resp.status_code == 401
    assert _auth_stores[0].list_users() == []


# ---------------------------------------------------------------------------
# /api/auth/me after login
# ---------------------------------------------------------------------------

def test_auth_me_after_login(_web_viewer, _auth_stores, monkeypatch):
    """GET /api/auth/me must work after System81 login."""
    store, sess = _auth_stores
    client, uid = _make_client_with_system81_login(
        _web_viewer, store, sess, monkeypatch
    )
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert data["user_id"] == uid
    assert data["username"] == "testuser"


def test_auth_me_without_session(_web_viewer, _auth_stores):
    """GET /api/auth/me without session must return authenticated=False."""
    client = TestClient(_web_viewer.app)
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

def test_logout_revokes_session(_web_viewer, _auth_stores, monkeypatch):
    """POST /api/auth/logout must revoke the session."""
    store, sess = _auth_stores
    client, uid = _make_client_with_system81_login(
        _web_viewer, store, sess, monkeypatch
    )
    # Verify logged in
    assert client.get("/api/auth/me").json()["authenticated"] is True
    # Logout
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    # Session must be revoked
    assert client.get("/api/auth/me").json()["authenticated"] is False


def test_logout_clears_brand_cookie(_web_viewer, _auth_stores, monkeypatch):
    """POST /api/auth/logout must also clear the mktapp_brand cookie."""
    store, sess = _auth_stores
    client, uid = _make_client_with_system81_login(
        _web_viewer, store, sess, monkeypatch
    )
    # Set a brand cookie
    client.cookies.set("mktapp_brand", "some_brand", domain="testserver")
    # Logout
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    # Brand cookie must be cleared (delete_cookie sets it to empty/expired)
    # The response should contain a Set-Cookie that expires mktapp_brand
    set_cookie_headers = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else []
    # TestClient may combine headers — check raw response
    raw = str(resp.headers)
    assert "mktapp_brand" in raw


# ---------------------------------------------------------------------------
# WorkspaceContext + BrandRegistry isolation
# ---------------------------------------------------------------------------

def test_system81_user_gets_workspace_context(_web_viewer, _auth_stores, monkeypatch):
    """An authenticated System81 user must get a correct WorkspaceContext."""
    store, sess = _auth_stores
    client, uid = _make_client_with_system81_login(
        _web_viewer, store, sess, monkeypatch
    )
    # Create a brand via API — this requires WorkspaceContext
    resp = client.post("/api/brands", json={"name": "TestBrand"})
    assert resp.status_code == 200
    brand = resp.json()
    assert brand["user_id"] == uid
    # List brands — must see own brand
    resp = client.get("/api/brands")
    assert resp.status_code == 200
    brands = resp.json()["brands"]
    assert len(brands) == 1
    assert brands[0]["name"] == "TestBrand"


def test_user_cannot_see_other_users_brands(_web_viewer, _auth_stores, monkeypatch, tmp_path):
    """User A's brands must not be visible to User B."""
    store, sess = _auth_stores

    # Login as User A
    from src.system81 import System81Provider
    id_a = _mock_identity("aaa", "userA")
    id_b = _mock_identity("bbb", "userB")

    def _mock_verify(self, token):
        if token == "token-a":
            return id_a
        if token == "token-b":
            return id_b
        return None

    monkeypatch.setattr(System81Provider, "verify_token", _mock_verify)

    client_a = TestClient(_web_viewer.app)
    resp = client_a.post("/api/auth/login", json={"token": "token-a"})
    assert resp.status_code == 200

    client_b = TestClient(_web_viewer.app)
    resp = client_b.post("/api/auth/login", json={"token": "token-b"})
    assert resp.status_code == 200

    # User A creates a brand
    resp = client_a.post("/api/brands", json={"name": "A Secret Brand"})
    assert resp.status_code == 200
    bid = resp.json()["brand_id"]

    # User B must not see A's brand
    resp = client_b.get("/api/brands")
    brands_b = resp.json()["brands"]
    assert len(brands_b) == 0

    # User B cannot get A's brand by ID
    resp = client_b.get(f"/api/brands/{bid}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AuthMiddleware does not bypass for unrelated APIs
# ---------------------------------------------------------------------------

def test_unauthenticated_api_call_returns_401(_web_viewer, _auth_stores):
    """An unauthenticated call to a protected API must return 401."""
    client = TestClient(_web_viewer.app)
    resp = client.get("/api/brands")
    assert resp.status_code == 401


def test_system81_callback_does_not_bypass_middleware(_web_viewer, _auth_stores, monkeypatch):
    """A System81 token in the request must not bypass AuthMiddleware for
    unrelated API routes — only /api/auth/* is public."""
    from src.system81 import System81Provider
    monkeypatch.setattr(System81Provider, "verify_token",
                        lambda self, t: _mock_identity() if t == "valid" else None)
    client = TestClient(_web_viewer.app)
    # Try to access protected route with token as cookie (should not work)
    client.cookies.set("mktapp_session", "valid", domain="testserver")
    resp = client.get("/api/brands")
    # The session cookie "valid" is not a real session token — must 401
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Dev-only auth (MKTAPP_DEV_AUTH=1)
# ---------------------------------------------------------------------------

def test_dev_auth_disabled_by_default(_web_viewer, _auth_stores, monkeypatch):
    """Without MKTAPP_DEV_AUTH=1, username/password login must be rejected."""
    monkeypatch.delenv("MKTAPP_DEV_AUTH", raising=False)
    client = TestClient(_web_viewer.app)
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "pass"})
    assert resp.status_code in (400, 403)


def test_register_disabled_in_production(_web_viewer, _auth_stores, monkeypatch):
    """POST /api/auth/register must be disabled in production."""
    monkeypatch.delenv("MKTAPP_DEV_AUTH", raising=False)
    client = TestClient(_web_viewer.app)
    resp = client.post("/api/auth/register", json={"username": "hacker", "password": "pass"})
    assert resp.status_code == 403
