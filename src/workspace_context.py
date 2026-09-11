"""WorkspaceContext — central per-user path resolver.

All user-derived state lives under ``users/{user_id}/``.  Product Factory
config (``config/``, ``brand/`` templates) remains global and read-only.

This module provides:
  - ``WorkspaceContext`` — frozen dataclass carrying ``user_id`` + ``root``
  - A context-var so request-scoped code can set the workspace once and
    all state modules pick it up without changing every function signature.
  - ``user_state_root()`` — the single helper every module calls instead
    of ``_project_root()`` for user-state paths.

Security:
  - ``user_id`` is sanitized to ``[A-Za-z0-9_-]`` only.
  - The resolved root is verified to stay inside ``project_root``.
  - No ``..``, no absolute-path injection, no cross-user namespace access.
"""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass
from pathlib import Path

# Context var — set per-request by the auth dependency, read by state modules.
_current_ws: contextvars.ContextVar[WorkspaceContext | None] = contextvars.ContextVar(
    "_mktapp_workspace", default=None
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _sanitize_user_id(user_id: str) -> str:
    """Reject any user_id that is not a safe path component."""
    if not user_id or not _SAFE_ID.match(user_id):
        raise ValueError(f"unsafe user_id: {user_id!r}")
    return user_id


@dataclass(frozen=True)
class WorkspaceContext:
    """Per-user workspace root.  Carries ``user_id`` and the resolved ``root`` path.

    All user-derived state paths derive from ``root``:
      ``root / cache/``, ``root / data/``, ``root / output/``, etc.

    Product Factory config is NOT here — it stays at the global project root.
    """

    user_id: str
    root: Path

    @classmethod
    def for_user(cls, user_id: str, project_root: Path) -> WorkspaceContext:
        """Create a workspace for ``user_id`` under ``project_root / users/``."""
        safe = _sanitize_user_id(user_id)
        root = (project_root.resolve() / "users" / safe)
        # Defence in depth: verify the resolved path is inside project_root.
        try:
            root.relative_to(project_root.resolve())
        except ValueError:
            raise ValueError(f"path traversal detected for user_id: {user_id!r}")
        return cls(user_id=safe, root=root)

    # --- Convenience path accessors ----------------------------------

    def cache_dir(self) -> Path:
        return self.root / "cache"

    def data_dir(self) -> Path:
        return self.root / "data"

    def output_dir(self) -> Path:
        return self.root / "output"

    def logs_dir(self) -> Path:
        return self.root / "logs"

    def workspace_local_dir(self) -> Path:
        return self.root / "workspace" / "local"

    def workspace_local_config_dir(self) -> Path:
        return self.workspace_local_dir() / "config"

    def workspace_local_brand_dir(self) -> Path:
        return self.workspace_local_dir() / "brand"


# --- Context-var helpers ---------------------------------------------

def set_workspace(ws: WorkspaceContext | None) -> contextvars.Token[WorkspaceContext | None]:
    """Set the current workspace for this async/thread context.

    Returns the token for ``reset_workspace()``.
    """
    return _current_ws.set(ws)


def reset_workspace(token: contextvars.Token[WorkspaceContext | None]) -> None:
    """Reset the workspace to its previous value."""
    _current_ws.reset(token)


def get_workspace() -> WorkspaceContext | None:
    """Return the current workspace, or ``None`` if not set (CLI / global mode)."""
    return _current_ws.get()


def user_state_root(project_root: Path | None = None) -> Path:
    """Return the root directory for user state.

    When a workspace is active (set via ``set_workspace``), returns
    ``ws.root``.  Otherwise falls back to ``project_root`` (or the
    directory two levels up from this file).

    This is the **single function** all state modules should call instead
    of ``_project_root()`` for user-derived state paths.
    """
    ws = _current_ws.get()
    if ws is not None:
        return ws.root
    if project_root is not None:
        return project_root
    return Path(__file__).resolve().parent.parent
