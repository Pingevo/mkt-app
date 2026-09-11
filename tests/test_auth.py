"""Auth tests — register, login, logout, invalid password, unauth denied, tampered session.

Tests the auth module directly (no HTTP) for speed and isolation.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from src.auth import (
    UserStore,
    SessionManager,
    hash_password,
    verify_password,
    SESSION_COOKIE_NAME,
    SESSION_TTL_HOURS,
)


@pytest.fixture
def tmp_auth_dir(tmp_path: Path) -> Path:
    """Create a temp auth directory with users.json and sessions.json."""
    auth_dir = tmp_path / "data" / "auth"
    auth_dir.mkdir(parents=True)
    return auth_dir


@pytest.fixture
def user_store(tmp_auth_dir: Path) -> UserStore:
    return UserStore(tmp_auth_dir / "users.json")


@pytest.fixture
def session_mgr(tmp_auth_dir: Path) -> SessionManager:
    return SessionManager(tmp_auth_dir / "sessions.json")


# --- Password hashing ---

def test_password_hashed_with_bcrypt():
    """Password must be hashed with bcrypt, not plaintext."""
    h = hash_password("mypassword123")
    assert h != "mypassword123"
    assert h.startswith("$2")  # bcrypt format
    assert verify_password("mypassword123", h)
    assert not verify_password("wrongpassword", h)


def test_no_plaintext_password_persistence(user_store: UserStore):
    """Plaintext password must never appear in storage."""
    user = user_store.register("alice", "secret123")
    assert user is not None
    raw = json.loads(user_store._filepath.read_text(encoding="utf-8"))
    for record in raw:
        assert "password_hash" in record
        assert "password" not in record
        assert record["password_hash"] != "secret123"
        assert record["password_hash"].startswith("$2")


def test_each_user_gets_unique_hash(user_store: UserStore):
    """Same password → different hashes (bcrypt salt)."""
    user_store.register("alice", "samepass")
    user_store.register("bob", "samepass")
    raw = json.loads(user_store._filepath.read_text(encoding="utf-8"))
    hashes = [r["password_hash"] for r in raw]
    assert len(hashes) == 2
    assert hashes[0] != hashes[1]


# --- Register ---

def test_register_success(user_store: UserStore):
    user = user_store.register("alice", "pass123")
    assert user is not None
    assert user.user_id.startswith("user_")
    assert user.username == "alice"


def test_register_duplicate_username_rejected(user_store: UserStore):
    user_store.register("alice", "pass123")
    again = user_store.register("alice", "different")
    assert again is None


def test_register_case_insensitive_duplicate(user_store: UserStore):
    user_store.register("Alice", "pass123")
    again = user_store.register("ALICE", "different")
    assert again is None


def test_register_empty_rejected(user_store: UserStore):
    assert user_store.register("", "pass") is None
    assert user_store.register("alice", "") is None


# --- Login ---

def test_login_success(user_store: UserStore):
    user_store.register("alice", "correctpass")
    user = user_store.verify("alice", "correctpass")
    assert user is not None
    assert user.username == "alice"


def test_login_wrong_password_rejected(user_store: UserStore):
    user_store.register("alice", "correctpass")
    user = user_store.verify("alice", "wrongpass")
    assert user is None


def test_login_unknown_user_rejected(user_store: UserStore):
    user = user_store.verify("ghost", "anything")
    assert user is None


def test_verify_skips_passwordless_profile_and_finds_dev_user(user_store: UserStore):
    """verify() must skip System81 (passwordless) profiles and continue
    searching to find a dev account with the same username later in the list.

    Setup:
      - System81 profile first (no password_hash)
      - dev profile second (with password_hash)
      - same username

    Expected: correct dev password authenticates as the dev user,
    not the System81 user.
    """
    from src.system81 import ExternalIdentity
    identity = ExternalIdentity(
        user_id="system81_test",
        username="alice",
        email="alice@s81.com",
        provider="system81",
        external_subject="test",
        metadata={},
    )
    user_store.get_or_create(identity)  # System81 profile first
    # register() would reject duplicate username, so write dev record
    # directly to simulate a pre-existing dev account
    import json
    from datetime import datetime
    users = user_store._load()
    users.append({
        "user_id": "user_dev_alice",
        "username": "alice",
        "password_hash": hash_password("devpass"),
        "created_at": datetime.now().isoformat(),
    })
    user_store._save(users)

    # Verify: correct dev password authenticates as dev user
    result = user_store.verify("alice", "devpass")
    assert result is not None
    assert result.user_id == "user_dev_alice"
    assert result.username == "alice"

    # Wrong password still rejected
    assert user_store.verify("alice", "wrongpass") is None

    # System81 profile remains unaffected
    s81_record = user_store.get_by_id("system81_test")
    assert s81_record is not None
    assert "password_hash" not in s81_record


# --- Sessions ---

def test_session_create_and_verify(user_store: UserStore, session_mgr: SessionManager):
    user = user_store.register("alice", "pass123")
    token = session_mgr.create_session(user.user_id)
    assert token  # non-empty
    assert session_mgr.verify_token(token) == user.user_id


def test_session_logout_invalidates(session_mgr: SessionManager, user_store: UserStore):
    user = user_store.register("alice", "pass123")
    token = session_mgr.create_session(user.user_id)
    assert session_mgr.verify_token(token) is not None
    assert session_mgr.revoke_session(token) is True
    assert session_mgr.verify_token(token) is None


def test_tampered_session_rejected(session_mgr: SessionManager):
    """A random/invalid token must be rejected."""
    assert session_mgr.verify_token("tampered_token_12345") is None
    assert session_mgr.verify_token("") is None


def test_session_has_ttl(user_store: UserStore, session_mgr: SessionManager):
    """Sessions must have an expiry (7 days)."""
    user = user_store.register("alice", "pass123")
    token = session_mgr.create_session(user.user_id)
    sessions = session_mgr._load()
    s = next(x for x in sessions if x["id"] == token)
    assert "expires_at" in s
    assert s["expires_at"]  # non-empty


def test_revoke_all_for_user(session_mgr: SessionManager, user_store: UserStore):
    user = user_store.register("alice", "pass123")
    t1 = session_mgr.create_session(user.user_id)
    t2 = session_mgr.create_session(user.user_id)
    removed = session_mgr.revoke_all_for_user(user.user_id)
    assert removed == 2
    assert session_mgr.verify_token(t1) is None
    assert session_mgr.verify_token(t2) is None
