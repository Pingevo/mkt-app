"""BrandRegistry — JSON-backed, user-scoped brand ownership + CRUD.

Single source of truth for brands owned by a user. Each brand is an
immutable-identity entity (brand_id) with a mutable display name.

Storage: ``users/<user_id>/brand_registry.json`` — user-scoped, isolated
from other users by filesystem layout (the user's workspace root).

Security:
  - ``brand_id`` is internally generated (``secrets.token_hex(8)``),
    filesystem-safe, immutable, and independent from display name.
  - Every get/rename/archive verifies the brand belongs to the bound
    ``user_id``. A registry bound to user A cannot see or mutate user B's
    brands, even if A knows B's brand_id.
  - Archive (not delete) preserves the brand_id so it is never reused.
  - By default ``get()`` returns only ACTIVE brands.  Archived brands are
    excluded so they cannot be selected or establish a brand context.
    Management/history code may pass ``active_only=False`` to retrieve
    archived records.
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


def _brand_id() -> str:
    """Generate a filesystem-safe, immutable brand identifier."""
    return secrets.token_hex(8)


def _now() -> str:
    return datetime.now().isoformat()


class BrandRegistry:
    """JSON-backed brand registry scoped to a single authenticated user.

    The ``user_id`` is authoritative — it is set once at construction from
    the authenticated WorkspaceContext and never overridden by callers.
    All reads/writes go to ``users/<user_id>/brand_registry.json``.
    """

    _lock = threading.Lock()

    def __init__(self, user_id: str, project_root: Path):
        self.user_id = user_id
        self._filepath = (
            project_root.resolve() / "users" / user_id / "brand_registry.json"
        )

    # --- persistence ---

    def _load(self) -> list[dict[str, Any]]:
        if not self._filepath.exists():
            return []
        try:
            data = json.loads(self._filepath.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, brands: list[dict[str, Any]]) -> None:
        self._filepath.parent.mkdir(parents=True, exist_ok=True)
        self._filepath.write_text(
            json.dumps(brands, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _find(self, brands: list[dict], brand_id: str,
              active_only: bool = True) -> dict | None:
        for b in brands:
            if b.get("brand_id") == brand_id and b.get("user_id") == self.user_id:
                if active_only and b.get("status") != "active":
                    return None
                return b
        return None

    # --- public interface ---

    def create(self, name: str) -> dict[str, Any]:
        """Create a new brand owned by this user. Returns the brand record."""
        brand = {
            "brand_id": _brand_id(),
            "user_id": self.user_id,
            "name": name,
            "status": "active",
            "created_at": _now(),
            "updated_at": _now(),
        }
        with self._lock:
            brands = self._load()
            brands.append(brand)
            self._save(brands)
        return brand

    def list(self) -> list[dict[str, Any]]:
        """Return this user's active brands, newest first."""
        brands = [
            b for b in self._load()
            if b.get("user_id") == self.user_id and b.get("status") == "active"
        ]
        return sorted(brands, key=lambda b: b.get("created_at", ""), reverse=True)

    def get(self, brand_id: str, *, active_only: bool = True) -> dict[str, Any] | None:
        """Return the brand if it belongs to this user, else None.

        By default (``active_only=True``) archived brands return ``None`` —
        use this for selection, context establishment, and normal business
        operations.  Pass ``active_only=False`` for management/history lookups
        that need to inspect archived records.
        """
        return self._find(self._load(), brand_id, active_only=active_only)

    def rename(self, brand_id: str, new_name: str) -> bool:
        """Rename a brand owned by this user. Returns True on success."""
        with self._lock:
            brands = self._load()
            brand = self._find(brands, brand_id)
            if brand is None:
                return False
            brand["name"] = new_name
            brand["updated_at"] = _now()
            self._save(brands)
        return True

    def archive(self, brand_id: str) -> bool:
        """Archive a brand owned by this user. Returns True on success.

        Archive (not delete) preserves the brand_id so it is never reused.
        """
        with self._lock:
            brands = self._load()
            brand = self._find(brands, brand_id)
            if brand is None:
                return False
            brand["status"] = "archived"
            brand["updated_at"] = _now()
            self._save(brands)
        return True
