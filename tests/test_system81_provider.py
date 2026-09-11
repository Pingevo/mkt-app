"""System81 provider — unit tests for URL construction, token verification,
identity mapping, and error handling.

All System81 HTTP calls are mocked — no live network access.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.system81 import System81Provider, ExternalIdentity


# ---------------------------------------------------------------------------
# Login URL construction
# ---------------------------------------------------------------------------

def test_login_url_contains_base_url_and_params(monkeypatch):
    """Login URL must point to System81 base with app_name + redirect_uri."""
    monkeypatch.setenv("SELLERCENTER_OAUTH_BASE_URL", "https://s81.test.example")
    monkeypatch.setenv("SELLERCENTER_OAUTH_APP_NAME", "MKTApp")
    monkeypatch.setenv("SELLERCENTER_OAUTH_REDIRECT_URI", "http://localhost:8778/login")
    monkeypatch.setenv("SELLERCENTER_OAUTH_CLIENT_ID", "mktapp_prod")
    provider = System81Provider()
    url = provider.get_login_url()
    assert url.startswith("https://s81.test.example/system81/login?")
    assert "app_name=MKTApp" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8778%2Flogin" in url
    assert "client_id=mktapp_prod" in url


def test_login_url_no_trailing_slash_in_base(monkeypatch):
    """Base URL trailing slash must not cause double slash."""
    monkeypatch.setenv("SELLERCENTER_OAUTH_BASE_URL", "https://s81.test.example/")
    provider = System81Provider()
    url = provider.get_login_url()
    assert "//system81" not in url


# ---------------------------------------------------------------------------
# Token verification — success
# ---------------------------------------------------------------------------

def test_verify_token_success_returns_identity(monkeypatch):
    """A valid token verified by userinfo returns a mapped ExternalIdentity."""
    provider = System81Provider()
    fake_userinfo = {
        "sub": "12345",
        "username": "john.doe",
        "email": "john@example.com",
        "name": "John Doe",
        "emp_id": "E001",
        "department": "Marketing",
        "position": "Manager",
    }
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: fake_userinfo)
    identity = provider.verify_token("valid-token-abc")
    assert identity is not None
    expected_id = "system81_" + hashlib.sha256(b"12345").hexdigest()
    assert identity.user_id == expected_id
    assert identity.username == "john.doe"
    assert identity.email == "john@example.com"
    assert identity.provider == "system81"
    assert identity.external_subject == "12345"
    assert identity.metadata["emp_id"] == "E001"
    assert identity.metadata["department"] == "Marketing"


def test_verify_token_fallback_subject(monkeypatch):
    """When sub is absent, fall back to user_id then username (old contract)."""
    provider = System81Provider()
    fake_userinfo = {"user_id": "67890", "username": "jane"}
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: fake_userinfo)
    identity = provider.verify_token("token")
    assert identity is not None
    expected_id = "system81_" + hashlib.sha256(b"67890").hexdigest()
    assert identity.user_id == expected_id
    assert identity.external_subject == "67890"


def test_verify_token_fallback_username(monkeypatch):
    """When sub and user_id are absent, fall back to username."""
    provider = System81Provider()
    fake_userinfo = {"username": "alice"}
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: fake_userinfo)
    identity = provider.verify_token("token")
    assert identity is not None
    expected_id = "system81_" + hashlib.sha256(b"alice").hexdigest()
    assert identity.user_id == expected_id


# ---------------------------------------------------------------------------
# Token verification — failures
# ---------------------------------------------------------------------------

def test_verify_token_rejected_returns_none(monkeypatch):
    """An invalid token (userinfo returns None) must return None."""
    provider = System81Provider()
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: None)
    assert provider.verify_token("forged-token-xyz") is None


def test_verify_token_empty_returns_none():
    """An empty token must return None without calling userinfo."""
    provider = System81Provider()
    assert provider.verify_token("") is None


def test_verify_token_no_subject_returns_none(monkeypatch):
    """Userinfo with no sub/user_id/username must return None."""
    provider = System81Provider()
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: {"email": "x@y.com"})
    assert provider.verify_token("token") is None


def test_verify_token_malformed_response_returns_none(monkeypatch):
    """A non-dict response from userinfo must not crash — return None."""
    provider = System81Provider()
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: "not a dict")
    assert provider.verify_token("token") is None


def test_verify_token_timeout_returns_none(monkeypatch):
    """A provider timeout/error must become a controlled auth failure (None)."""
    provider = System81Provider()

    def _raise(token):
        raise TimeoutError("System81 timed out")
    monkeypatch.setattr(provider, "_get_userinfo", _raise)
    assert provider.verify_token("token") is None


# ---------------------------------------------------------------------------
# Identity mapping — determinism + filesystem safety
# ---------------------------------------------------------------------------

def test_identity_deterministic(monkeypatch):
    """Same System81 subject must always map to the same user_id."""
    provider = System81Provider()
    info = {"sub": "999", "username": "bob"}
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: info)
    id1 = provider.verify_token("token1")
    id2 = provider.verify_token("token2")
    expected = "system81_" + hashlib.sha256(b"999").hexdigest()
    assert id1.user_id == id2.user_id == expected


def test_different_subjects_different_users(monkeypatch):
    """Different System81 subjects must produce different local users."""
    provider = System81Provider()
    monkeypatch.setattr(
        provider, "_get_userinfo",
        lambda token: {"sub": "111"} if token == "t1" else {"sub": "222"},
    )
    id1 = provider.verify_token("t1")
    id2 = provider.verify_token("t2")
    assert id1.user_id != id2.user_id
    assert id1.user_id == "system81_" + hashlib.sha256(b"111").hexdigest()
    assert id2.user_id == "system81_" + hashlib.sha256(b"222").hexdigest()


def test_safe_subject_also_hashed(monkeypatch):
    """A subject that is already filesystem-safe must also be hashed —
    one mapping rule for all subjects, no raw-vs-hash domain overlap."""
    provider = System81Provider()
    raw_sub = "abc123_-"
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: {"sub": raw_sub})
    identity = provider.verify_token("token")
    expected = "system81_" + hashlib.sha256(raw_sub.encode("utf-8")).hexdigest()
    assert identity.user_id == expected
    # Must NOT be the raw subject
    assert identity.user_id != "system81_abc123_-"


def test_unsafe_subject_hashed_to_filesystem_safe(monkeypatch):
    """A subject with unsafe path characters must be hashed to a safe ID."""
    provider = System81Provider()
    raw_sub = "user/../../etc/passwd"
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: {"sub": raw_sub})
    identity = provider.verify_token("token")
    assert identity is not None
    # Must be filesystem-safe: only [a-f0-9] (full SHA-256 hex digest)
    safe_part = identity.user_id.replace("system81_", "")
    assert all(c in "0123456789abcdef" for c in safe_part)
    # Must be deterministic — full SHA-256 of the raw sub
    expected = hashlib.sha256(raw_sub.encode("utf-8")).hexdigest()
    assert identity.user_id == f"system81_{expected}"


def test_unsafe_subject_deterministic(monkeypatch):
    """Same unsafe subject must always hash to the same user_id."""
    provider = System81Provider()
    raw_sub = "user with spaces and/slashes"
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: {"sub": raw_sub})
    id1 = provider.verify_token("t1")
    id2 = provider.verify_token("t2")
    assert id1.user_id == id2.user_id


def test_safe_and_unsafe_subjects_never_collide(monkeypatch):
    """A safe 16-hex-char subject and an unsafe subject hashed to the same
    16-hex prefix must NOT collide — all subjects go through the same hash."""
    provider = System81Provider()
    # A safe subject that is exactly 16 hex chars
    safe_sub = "abcdef0123456789"
    # An unsafe subject — under the old code, if its hash started with
    # "abcdef0123456789" it would collide. Under the new code, the safe
    # subject is also hashed, so they can never collide.
    unsafe_sub = "user/with spaces"
    monkeypatch.setattr(
        provider, "_get_userinfo",
        lambda token: {"sub": safe_sub} if token == "t1" else {"sub": unsafe_sub},
    )
    id1 = provider.verify_token("t1")
    id2 = provider.verify_token("t2")
    assert id1.user_id != id2.user_id
    assert id1.user_id == "system81_" + hashlib.sha256(safe_sub.encode("utf-8")).hexdigest()
    assert id2.user_id == "system81_" + hashlib.sha256(unsafe_sub.encode("utf-8")).hexdigest()


def test_username_change_preserves_user_id(monkeypatch):
    """When the canonical external identifier (sub) stays the same,
    username/display-name changes must NOT alter the user_id."""
    provider = System81Provider()
    monkeypatch.setattr(
        provider, "_get_userinfo",
        lambda token: {"sub": "999", "username": "old_name"} if token == "t1"
        else {"sub": "999", "username": "new_name"},
    )
    id1 = provider.verify_token("t1")
    id2 = provider.verify_token("t2")
    assert id1.user_id == id2.user_id  # same sub → same user_id
    assert id1.username == "old_name"
    assert id2.username == "new_name"  # username updated, user_id stable


# ---------------------------------------------------------------------------
# Token is never logged
# ---------------------------------------------------------------------------

def test_token_not_logged(monkeypatch, capsys):
    """The provider must never print/log the token value."""
    provider = System81Provider()
    monkeypatch.setattr(provider, "_get_userinfo", lambda token: {"sub": "1"})
    provider.verify_token("SECRET-TOKEN-DO-NOT-LOG")
    captured = capsys.readouterr()
    assert "SECRET-TOKEN-DO-NOT-LOG" not in captured.out
    assert "SECRET-TOKEN-DO-NOT-LOG" not in captured.err


# ---------------------------------------------------------------------------
# Provisioning — UserStore.get_or_create
# ---------------------------------------------------------------------------

def test_first_login_creates_profile(tmp_path):
    """First System81 login must create a local profile with no password."""
    from src.auth import UserStore
    store = UserStore(tmp_path / "users.json")
    identity = ExternalIdentity(
        user_id="system81_123",
        username="newuser",
        email="new@test.com",
        provider="system81",
        external_subject="123",
        metadata={"name": "New User"},
    )
    user = store.get_or_create(identity)
    assert user is not None
    assert user.user_id == "system81_123"
    assert user.username == "newuser"
    # Profile must not require a password
    record = store.get_by_id("system81_123")
    assert record is not None
    assert "password_hash" not in record or not record["password_hash"]
    assert record["provider"] == "system81"
    assert record["external_subject"] == "123"


def test_repeat_login_returns_same_user_id(tmp_path):
    """Repeat login must return the same user_id, not create a new one."""
    from src.auth import UserStore
    store = UserStore(tmp_path / "users.json")
    identity = ExternalIdentity(
        user_id="system81_456",
        username="alice",
        email="alice@test.com",
        provider="system81",
        external_subject="456",
        metadata={},
    )
    user1 = store.get_or_create(identity)
    user2 = store.get_or_create(identity)
    assert user1.user_id == user2.user_id == "system81_456"
    # Only one profile in the store
    users = store.list_users()
    assert len(users) == 1


def test_repeat_login_updates_profile(tmp_path):
    """Repeat login must update username/email/metadata, not duplicate."""
    from src.auth import UserStore
    store = UserStore(tmp_path / "users.json")
    identity1 = ExternalIdentity(
        user_id="system81_789",
        username="old_name",
        email="old@test.com",
        provider="system81",
        external_subject="789",
        metadata={"department": "Old"},
    )
    store.get_or_create(identity1)
    identity2 = ExternalIdentity(
        user_id="system81_789",
        username="new_name",
        email="new@test.com",
        provider="system81",
        external_subject="789",
        metadata={"department": "New"},
    )
    user = store.get_or_create(identity2)
    assert user.username == "new_name"
    record = store.get_by_id("system81_789")
    assert record["username"] == "new_name"
    assert record["email"] == "new@test.com"
    assert record["metadata"]["department"] == "New"
    assert "last_login" in record


def test_two_identities_distinct_profiles(tmp_path):
    """Two different System81 identities must have distinct profiles."""
    from src.auth import UserStore
    store = UserStore(tmp_path / "users.json")
    id1 = ExternalIdentity("system81_aaa", "userA", "a@test.com", "system81", "aaa", {})
    id2 = ExternalIdentity("system81_bbb", "userB", "b@test.com", "system81", "bbb", {})
    store.get_or_create(id1)
    store.get_or_create(id2)
    users = store.list_users()
    assert len(users) == 2
    ids = {u["user_id"] for u in users}
    assert ids == {"system81_aaa", "system81_bbb"}


def test_no_system81_token_stored_in_profile(tmp_path):
    """The System81 access token must never be stored in the user profile."""
    from src.auth import UserStore
    store = UserStore(tmp_path / "users.json")
    identity = ExternalIdentity(
        user_id="system81_tok",
        username="tokuser",
        email="t@test.com",
        provider="system81",
        external_subject="tok",
        metadata={},
    )
    store.get_or_create(identity)
    import json
    raw = json.loads((tmp_path / "users.json").read_text())
    for record in raw:
        assert "token" not in record
        assert "access_token" not in record
        assert "system81_token" not in record
