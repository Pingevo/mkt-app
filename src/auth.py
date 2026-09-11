"""Authentication — local/Beta user accounts, sessions, and FastAPI dependency.

Reuses proven concepts from the old repo (``/Users/its-dev2/my-agent-app``):
  - ``User`` dataclass identity
  - ``SessionManager`` lifecycle with ``secrets.token_urlsafe()``
  - JSON-backed account/session persistence

Does NOT reuse:
  - SHA-256 + fixed-salt password hashing  (replaced by bcrypt)
  - token-in-query-string transport        (replaced by HttpOnly cookie)
  - permissive CORS / unauthenticated fallback
  - Chainlit / React / System81 integration

Storage:
  - ``data/auth/users.json``    — account records (bcrypt hash, no plaintext)
  - ``data/auth/sessions.json`` — active session tokens

Session transport: HttpOnly + SameSite=Lax cookie named ``mktapp_session``.
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import bcrypt
from fastapi import Cookie, HTTPException, Request

from .workspace_context import WorkspaceContext, set_workspace, reset_workspace
from .system81 import ExternalIdentity

SESSION_COOKIE_NAME = "mktapp_session"
SESSION_TTL_HOURS = 24 * 7  # 7 days

# Bcrypt cost factor — 12 is the OWASP-recommended minimum as of 2024.
_BCRYPT_ROUNDS = 12


# ---------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------

def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _auth_dir() -> Path:
    """Global auth data directory — NOT per-user (accounts are global)."""
    return _project_root() / "data" / "auth"


def _users_path() -> Path:
    return _auth_dir() / "users.json"


def _sessions_path() -> Path:
    return _auth_dir() / "sessions.json"


# ---------------------------------------------------------------------
# Password hashing (bcrypt)
# ---------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a password with bcrypt. Returns a UTF-8 string."""
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash. Constant-time comparison."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------
# User record
# ---------------------------------------------------------------------

class User:
    """Authenticated user identity (reused concept from old repo)."""

    __slots__ = ("user_id", "username", "created_at")

    def __init__(self, user_id: str, username: str, created_at: str = ""):
        self.user_id = user_id
        self.username = username
        self.created_at = created_at

    def __repr__(self) -> str:
        return f"User(user_id={self.user_id!r}, username={self.username!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, User):
            return NotImplemented
        return self.user_id == other.user_id

    def __hash__(self) -> int:
        return hash(self.user_id)


# ---------------------------------------------------------------------
# UserStore — JSON-backed account storage
# ---------------------------------------------------------------------

class UserStore:
    """JSON-backed user account storage with bcrypt password hashing.

    Accounts are global (not per-user) — they live in ``data/auth/users.json``.
    This is the registry of who can log in, not per-user workspace state.
    """

    _lock = threading.Lock()

    def __init__(self, filepath: Path | None = None):
        self._filepath = filepath or _users_path()

    def _load(self) -> list[dict[str, Any]]:
        if not self._filepath.exists():
            return []
        try:
            data = json.loads(self._filepath.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, users: list[dict[str, Any]]) -> None:
        self._filepath.parent.mkdir(parents=True, exist_ok=True)
        self._filepath.write_text(
            json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def register(self, username: str, password: str) -> User | None:
        """Register a new user. Returns User if success, None if username taken."""
        username = username.strip()
        if not username or not password:
            return None
        with self._lock:
            users = self._load()
            # Check if username already exists (case-insensitive)
            for u in users:
                if u.get("username", "").lower() == username.lower():
                    return None
            user_id = f"user_{secrets.token_hex(8)}"
            record = {
                "user_id": user_id,
                "username": username,
                "password_hash": hash_password(password),
                "created_at": datetime.now().isoformat(),
            }
            users.append(record)
            self._save(users)
            return User(user_id=user_id, username=username, created_at=record["created_at"])

    def verify(self, username: str, password: str) -> User | None:
        """Verify credentials. Returns User if valid, None if not.

        Skips records without a ``password_hash`` (e.g. System81 profiles)
        and continues searching so a dev account with the same username
        can still be found later in the registry.
        """
        username = username.strip()
        if not username or not password:
            return None
        users = self._load()
        for u in users:
            if u.get("username", "").lower() != username.lower():
                continue
            stored_hash = u.get("password_hash", "")
            if not stored_hash:
                continue  # passwordless (System81) profile — skip
            if verify_password(password, stored_hash):
                return User(
                    user_id=u["user_id"],
                    username=u.get("username", username),
                    created_at=u.get("created_at", ""),
                )
        return None

    def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        users = self._load()
        for u in users:
            if u.get("user_id") == user_id:
                return u
        return None

    def list_users(self) -> list[dict[str, Any]]:
        """Return all user records (without password_hash)."""
        return [
            {k: v for k, v in u.items() if k != "password_hash"}
            for u in self._load()
        ]

    def get_or_create(self, external: ExternalIdentity) -> User:
        """Get or create a local profile for a System81-verified user.

        No password required — System81 is the authentication authority.
        On repeat login, updates username/email/metadata and last_login.
        Never stores the System81 access token.
        """
        now = datetime.now().isoformat()
        with self._lock:
            users = self._load()
            for u in users:
                if u.get("user_id") == external.user_id:
                    u["username"] = external.username
                    if external.email:
                        u["email"] = external.email
                    u["provider"] = external.provider
                    u["external_subject"] = external.external_subject
                    u["metadata"] = external.metadata
                    u["updated_at"] = now
                    u["last_login"] = now
                    self._save(users)
                    return User(
                        user_id=external.user_id,
                        username=external.username,
                        created_at=u.get("created_at", ""),
                    )
            record = {
                "user_id": external.user_id,
                "username": external.username,
                "email": external.email,
                "provider": external.provider,
                "external_subject": external.external_subject,
                "metadata": external.metadata,
                "created_at": now,
                "updated_at": now,
                "last_login": now,
            }
            users.append(record)
            self._save(users)
            return User(
                user_id=external.user_id,
                username=external.username,
                created_at=now,
            )


# ---------------------------------------------------------------------
# SessionManager — JSON-backed session tokens
# ---------------------------------------------------------------------

class SessionManager:
    """JSON-backed session token management.

    Reuses the ``secrets.token_urlsafe()`` pattern from the old repo.
    Sessions are stored as a list of ``{id, user_id, created_at, expires_at}``.
    """

    _lock = threading.Lock()

    def __init__(self, filepath: Path | None = None):
        self._filepath = filepath or _sessions_path()

    def _load(self) -> list[dict[str, Any]]:
        if not self._filepath.exists():
            return []
        try:
            data = json.loads(self._filepath.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, sessions: list[dict[str, Any]]) -> None:
        self._filepath.parent.mkdir(parents=True, exist_ok=True)
        self._filepath.write_text(
            json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def create_session(self, user_id: str) -> str:
        """Create a new session token for a user. Returns the token."""
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        session = {
            "id": token,
            "user_id": user_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=SESSION_TTL_HOURS)).isoformat(),
        }
        with self._lock:
            sessions = self._load()
            sessions.append(session)
            self._save(sessions)
        return token

    def verify_token(self, token: str) -> str | None:
        """Verify a session token. Returns user_id if valid, None if invalid/expired."""
        if not token:
            return None
        with self._lock:
            sessions = self._load()
            for s in sessions:
                if s.get("id") == token:
                    try:
                        expires_at = datetime.fromisoformat(s.get("expires_at", ""))
                    except (ValueError, TypeError):
                        return None
                    if datetime.now() > expires_at:
                        # Expired — remove it
                        sessions = [x for x in sessions if x.get("id") != token]
                        self._save(sessions)
                        return None
                    return s.get("user_id")
            return None

    def revoke_session(self, token: str) -> bool:
        """Revoke a session token. Returns True if it was found and removed."""
        with self._lock:
            sessions = self._load()
            before = len(sessions)
            sessions = [x for x in sessions if x.get("id") != token]
            if len(sessions) < before:
                self._save(sessions)
                return True
            return False

    def revoke_all_for_user(self, user_id: str) -> int:
        """Revoke all sessions for a user. Returns count removed."""
        with self._lock:
            sessions = self._load()
            before = len(sessions)
            sessions = [x for x in sessions if x.get("user_id") != user_id]
            removed = before - len(sessions)
            if removed > 0:
                self._save(sessions)
            return removed


# ---------------------------------------------------------------------
# Singleton instances (lazy)
# ---------------------------------------------------------------------

_user_store: UserStore | None = None
_session_manager: SessionManager | None = None
_store_lock = threading.Lock()


def get_user_store(project_root: Path | None = None) -> UserStore:
    """Return the UserStore singleton.

    When ``project_root`` is provided, returns a UserStore bound to that
    root's ``data/auth/users.json``.  This is used by Scheduler so its
    authoritative user enumeration matches its own project root.  The
    default (no-arg) behavior is unchanged for normal application startup.
    """
    global _user_store
    if project_root is not None:
        return UserStore(project_root / "data" / "auth" / "users.json")
    if _user_store is None:
        with _store_lock:
            if _user_store is None:
                _user_store = UserStore()
    return _user_store


def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        with _store_lock:
            if _session_manager is None:
                _session_manager = SessionManager()
    return _session_manager


# ---------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------

def get_current_user_id(
    request: Request,
    session_token: str | None = Cookie(None, alias=SESSION_COOKIE_NAME),
) -> str:
    """FastAPI dependency: resolve the authenticated user from the session cookie.

    Raises HTTPException(401) if not authenticated.
    Returns the ``user_id`` string.
    """
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = get_session_manager().verify_token(session_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user_id


def get_workspace_ctx(request: Request) -> WorkspaceContext:
    """FastAPI dependency: resolve the WorkspaceContext from the session cookie.

    Also sets the context-var so state modules pick up the workspace
    automatically without changing every function signature.

    Raises HTTPException(401) if not authenticated.
    """
    user_id = get_current_user_id(request)
    ws = WorkspaceContext.for_user(user_id, _project_root())
    set_workspace(ws)
    return ws


def get_workspace_ctx_optional(request: Request) -> WorkspaceContext | None:
    """Like get_workspace_ctx but returns None instead of 401 for public routes.

    Used for the login/register pages where we want to redirect to the app
    if already logged in, but don't want to 401 if not.
    """
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        return None
    user_id = get_session_manager().verify_token(session_token)
    if not user_id:
        return None
    ws = WorkspaceContext.for_user(user_id, _project_root())
    set_workspace(ws)
    return ws
