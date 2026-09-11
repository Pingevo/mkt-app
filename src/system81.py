"""System81 authentication provider — OAuth-style login via Sellercenter System81.

Ported from /Users/its-dev2/my-agent-app/backend/auth/system81.py — only the
token verification + login URL contract needed by MKTApp.

Does NOT include:
  - Chainlit/Team/Agent architecture
  - Direct username/password mode (production uses redirect flow only)
  - Session handling (that stays in src/auth.py)

Config (env):
  SELLERCENTER_OAUTH_BASE_URL    — System81 base URL
  SELLERCENTER_OAUTH_CLIENT_ID   — client ID
  SELLERCENTER_OAUTH_APP_NAME    — app display name
  SELLERCENTER_OAUTH_REDIRECT_URI — callback URI

Security:
  - Never logs tokens.
  - Never stores the System81 access token in user profiles.
  - Maps external subject to a deterministic, filesystem-safe internal user_id.
"""
from __future__ import annotations

import hashlib
import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class ExternalIdentity:
    """Identity verified by System81 — mapped to an internal MKTApp user_id.

    ``user_id`` is always ``system81_{sha256_hex(external_subject)}`` —
    deterministic, collision-domain-safe, and filesystem-safe.
    ``external_subject`` preserves the raw System81 subject for audit/
    provenance but is never used as a path component.
    """
    user_id: str
    username: str
    email: str
    provider: str  # always "system81"
    external_subject: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _safe_external_id(raw: str) -> str:
    """Deterministic collision-domain-safe mapping for an external subject ID.

    Always hashes with SHA-256 (full 64-char hex digest) — one mapping rule
    for every subject, no raw-vs-hash domain overlap, deterministic, and
    filesystem-safe.  Does not leak the raw subject in paths.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class System81Provider:
    """Verify System81 tokens and map them to internal MKTApp identities.

    Responsibilities:
      - build the System81 login URL
      - verify a System81 token via the userinfo endpoint
      - map the System81 response into an ExternalIdentity

    Does NOT handle MKTApp sessions — that is src.auth.SessionManager's job.
    """

    def __init__(self):
        self.base_url = (
            os.getenv("SELLERCENTER_OAUTH_BASE_URL", "https://data.digital.in.th").rstrip("/")
        )
        self.app_name = os.getenv("SELLERCENTER_OAUTH_APP_NAME", "MKTApp")
        self.app_logo = os.getenv("SELLERCENTER_OAUTH_APP_LOGO", "")
        self.client_id = os.getenv("SELLERCENTER_OAUTH_CLIENT_ID", "mktapp")
        self.redirect_uri = os.getenv(
            "SELLERCENTER_OAUTH_REDIRECT_URI",
            "http://localhost:8778/login",
        )
        self.timeout = 15

    # --- Login URL ---

    def get_login_url(self) -> str:
        """Return the URL to redirect the user to the System81 login page."""
        params: dict[str, str] = {
            "app_name": self.app_name,
            "redirect_uri": self.redirect_uri,
        }
        if self.app_logo:
            params["app_logo"] = self.app_logo
        if self.client_id:
            params["client_id"] = self.client_id
        return f"{self.base_url}/system81/login?{urllib.parse.urlencode(params)}"

    # --- Token verification ---

    def _get_userinfo(self, token: str) -> dict[str, Any] | None:
        """Verify a System81 token by calling /system81/userinfo.

        Returns the ``user`` dict from the response, or None on failure.
        Never raises — all errors become None (controlled auth failure).
        """
        if not token:
            return None
        url = f"{self.base_url}/system81/userinfo"
        # Try Bearer header first, then query-param fallback (old contract)
        for kwargs in (
            {"headers": {"Authorization": f"Bearer {token}"}},
            {"params": {"token": token}},
        ):
            try:
                resp = httpx.get(url, timeout=self.timeout, **kwargs)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        return data.get("user")
            except Exception:
                pass
        return None

    def _map_identity(self, info: dict[str, Any]) -> ExternalIdentity | None:
        """Convert System81 userinfo into an ExternalIdentity.

        Returns None if no usable subject is present.
        """
        if not isinstance(info, dict):
            return None
        sub = info.get("sub") or info.get("user_id") or info.get("username")
        if not sub:
            return None
        sub_str = str(sub)
        safe_sub = _safe_external_id(sub_str)
        return ExternalIdentity(
            user_id=f"system81_{safe_sub}",
            username=info.get("username", ""),
            email=info.get("email", ""),
            provider="system81",
            external_subject=sub_str,
            metadata={
                "name": info.get("name", ""),
                "emp_id": info.get("emp_id", ""),
                "department": info.get("department", ""),
                "position": info.get("position", ""),
            },
        )

    def verify_token(self, token: str) -> ExternalIdentity | None:
        """Verify a System81 token and return the mapped identity, or None.

        All failures (invalid token, timeout, malformed response, missing
        subject) become None — never raises.
        """
        if not token:
            return None
        try:
            info = self._get_userinfo(token)
            if info is None:
                return None
            return self._map_identity(info)
        except Exception:
            return None
