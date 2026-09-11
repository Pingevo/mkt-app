"""WorkspaceContext — central per-user path resolver.

All user-derived state lives under ``users/{user_id}/``.  Product Factory
config (``config/``, ``brand/`` templates) remains global and read-only.

This module provides:
  - ``WorkspaceContext`` — frozen dataclass carrying ``user_id`` + ``root``
  - A context-var so request-scoped code can set the workspace once and
    all state modules pick it up without changing every function signature.
  - ``user_state_root()`` — the single helper every module calls instead
    of ``_project_root()`` for user-state paths.
  - ``contain_path()`` — shared resolved-containment check for filesystem
    boundaries that receive request-controlled path components.
  - ``with_workspace_context()`` — wraps a callable so worker threads
    inherit the current workspace ContextVar.

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
from typing import Any, Callable

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

    ``brand_id`` is ``None`` for user-level contexts (auth, brand registry,
    listing/creating brands).  When set, it identifies an ownership-verified
    brand and ``brand_state_root()`` resolves beneath this user's root.
    """

    user_id: str
    root: Path
    brand_id: str | None = None

    @classmethod
    def for_user(cls, user_id: str, project_root: Path) -> WorkspaceContext:
        """Create a workspace for ``user_id`` under ``project_root / users/``.

        ``brand_id`` is ``None`` — this is a user-level context.
        """
        safe = _sanitize_user_id(user_id)
        root = (project_root.resolve() / "users" / safe)
        # Defence in depth: verify the resolved path is inside project_root.
        try:
            root.relative_to(project_root.resolve())
        except ValueError:
            raise ValueError(f"path traversal detected for user_id: {user_id!r}")
        return cls(user_id=safe, root=root)

    @classmethod
    def for_brand(cls, user_id: str, brand_id: str,
                  project_root: Path) -> WorkspaceContext:
        """Create a brand-scoped workspace for ``user_id`` / ``brand_id``.

        Ownership is verified via BrandRegistry before the context is
        established.  Raises ``ValueError`` if the brand does not belong to
        ``user_id`` or does not exist.  Never falls back to a user-only or
        default-brand context.
        """
        from .brand_registry import BrandRegistry
        reg = BrandRegistry(user_id=user_id, project_root=project_root)
        if reg.get(brand_id) is None:
            raise ValueError(
                f"brand {brand_id!r} not owned by user {user_id!r}"
            )
        ws = cls.for_user(user_id, project_root)
        return cls(user_id=ws.user_id, root=ws.root, brand_id=brand_id)

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

    Semantics are always the **user** root (``users/<user_id>/``), never a
    brand root, even when ``brand_id`` is set on the active workspace.
    """
    ws = _current_ws.get()
    if ws is not None:
        return ws.root
    if project_root is not None:
        return project_root
    return Path(__file__).resolve().parent.parent


def brand_state_root(project_root: Path | None = None) -> Path:
    """Return the brand-scoped root directory.

    Requires an active workspace with ``brand_id`` set (ownership-verified
    at context establishment).  Returns ``users/<user_id>/brands/<brand_id>/``.

    Fails closed — raises ``ValueError`` if no workspace is active or the
    active workspace has no ``brand_id``.  Never falls back to a user root,
    a default brand, or the project root.
    """
    ws = _current_ws.get()
    if ws is None or ws.brand_id is None:
        raise ValueError("brand_state_root requires an active brand context")
    return ws.root / "brands" / ws.brand_id


def require_brand_context() -> WorkspaceContext:
    """Return the active workspace, requiring a verified ``brand_id``.

    Fails closed — raises ``ValueError`` if no workspace is active or the
    active workspace has no ``brand_id``.  Use for brand-scoped operations
    that must not silently fall back to a user-only or default-brand context.
    """
    ws = _current_ws.get()
    if ws is None or ws.brand_id is None:
        raise ValueError("brand-scoped operation requires an active brand context")
    return ws


# --- Path containment helper -----------------------------------------

def contain_path(child: str, root: Path) -> Path:
    """Resolve ``child`` under ``root``; reject absolute paths, ``..`` escapes,
    and symlink-based escapes.

    Uses ``Path.resolve()`` so symlinks that point outside ``root`` are
    detected even if the symlink itself lives inside ``root``.  Works for
    paths that do not exist yet (``resolve(strict=False)`` is the default).

    Returns the resolved path inside ``root``.
    Raises ``ValueError`` if the resolved path escapes ``root``.
    """
    if not child:
        raise ValueError("empty path component")
    root_resolved = root.resolve()
    candidate = (root_resolved / child).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ValueError(f"path escapes root: {child!r}")
    return candidate


# --- Worker context propagation helper --------------------------------

def with_workspace_context(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Callable[[], Any]:
    """Return a zero-arg callable that runs ``fn`` under the current
    context (including the workspace ContextVar).

    Use as::

        threading.Thread(target=with_workspace_context(worker))
        executor.submit(with_workspace_context(lambda: do_work()))

    This helper does **not** create or join a thread — the caller controls
    thread lifecycle, daemon flag, and timing.  It only captures the
    current ``contextvars`` context so the worker sees the same workspace
    as the launching request.
    """
    ctx = contextvars.copy_context()

    def _run() -> Any:
        return fn(*args, **kwargs)

    return lambda: ctx.run(_run)
