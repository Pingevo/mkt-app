"""Red tests for AUTH-ISO-01-A: worker-context propagation.

These tests prove that the current production code loses the workspace
ContextVar when launching background threads, and that the proposed
``with_workspace_context`` helper fixes it.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.workspace_context import (
    WorkspaceContext,
    set_workspace,
    reset_workspace,
    get_workspace,
    user_state_root,
)


# --- Documentation: bare threading pattern loses workspace (why we need the helper) ---

def test_production_worker_loses_workspace(tmp_path: Path):
    """The bare ``threading.Thread(target=worker)`` pattern does NOT propagate
    the workspace ContextVar. Worker sees None and falls back to the global
    project root. This is why production code must use ``with_workspace_context``.
    """
    ws = WorkspaceContext.for_user("user_aaa", tmp_path)
    token = set_workspace(ws)
    try:
        seen: dict = {}
        def worker():
            seen["ws"] = get_workspace()
            seen["root"] = user_state_root(tmp_path)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join()

        # Bare pattern loses context — this is the bug the helper fixes
        assert seen["ws"] is None, "Bare thread should NOT see workspace (contextvar not propagated)"
    finally:
        reset_workspace(token)


# --- Red: helper does not exist yet ---

def test_with_workspace_context_helper_exists():
    """The with_workspace_context helper must exist and return a callable."""
    from src.workspace_context import with_workspace_context
    assert callable(with_workspace_context)


def test_with_workspace_context_propagates(tmp_path: Path):
    """with_workspace_context must capture the current context and propagate
    it into the thread."""
    from src.workspace_context import with_workspace_context

    ws = WorkspaceContext.for_user("user_aaa", tmp_path)
    token = set_workspace(ws)
    try:
        seen: dict = {}
        def worker():
            seen["ws"] = get_workspace()
            seen["root"] = user_state_root(tmp_path)

        wrapped = with_workspace_context(worker)
        t = threading.Thread(target=wrapped, daemon=True)
        t.start()
        t.join()

        assert seen["ws"] is ws, "Wrapped worker must see the workspace"
        assert seen["root"] == ws.root, "Wrapped worker must resolve to user workspace"
    finally:
        reset_workspace(token)


def test_with_workspace_context_does_not_create_thread():
    """with_workspace_context must NOT create or join a thread itself.
    It returns a callable; the caller controls thread lifecycle."""
    from src.workspace_context import with_workspace_context

    ws = WorkspaceContext.for_user("user_aaa", Path("/tmp/fake"))
    token = set_workspace(ws)
    try:
        wrapped = with_workspace_context(lambda: 42)
        # Must be callable but not a Thread
        assert callable(wrapped)
        assert not isinstance(wrapped, threading.Thread)
        # Calling it directly should work (no thread created)
        result = wrapped()
        assert result == 42
    finally:
        reset_workspace(token)


# --- Production acceptance: real endpoint launches a worker that sees the workspace ---

def test_production_ingest_worker_sees_authenticated_workspace(tmp_path: Path, monkeypatch):
    """Exercise the real ``POST /api/ingest/{folder}`` endpoint via TestClient.

    The endpoint launches a background ``threading.Thread`` wrapped with
    ``with_workspace_context``.  We replace ``ingest_product`` with a probe
    that captures the ``WorkspaceContext`` actually visible inside the
    worker, then assert it matches the authenticated test user's workspace.

    This proves the production launch path propagates the workspace — not
    just the helper in isolation.
    """
    import web_viewer
    from src import product_db, staging, ingestion
    from src.workspace_context import user_state_root, get_workspace
    from tests.conftest import make_brand_client

    # Ingestion requires an active brand context (MB-02): authenticate,
    # create + select a brand — PROJECT_ROOT is patched to tmp_path so all
    # state resolves inside the per-user brand workspace.
    monkeypatch.setattr(ingestion, "_make_llm", lambda: None)
    client, user_id, brand_id, brand_root = make_brand_client(
        web_viewer.app, tmp_path, monkeypatch)
    ws_root = brand_root.parents[1]  # users/<user_id>

    # Create a product folder inside the authenticated brand's workspace
    product_dir = brand_root / "data" / "TestProduct"
    product_dir.mkdir(parents=True, exist_ok=True)
    (product_dir / "sample.txt").write_text("hello", encoding="utf-8")

    # Probe: capture the workspace the worker actually sees
    captured: dict = {}
    done = threading.Event()

    def _probe(product_id, force=False, **kw):
        ws = get_workspace()
        captured["ws"] = ws
        captured["user_id"] = ws.user_id if ws else None
        captured["root"] = user_state_root(tmp_path)
        captured["global_fallback"] = tmp_path
        done.set()

    monkeypatch.setattr(ingestion, "ingest_product", _probe)

    # Trigger the endpoint — AuthMiddleware sets workspace, worker inherits it
    resp = client.post("/api/ingest/TestProduct", json={"force": True})
    assert resp.status_code == 200, f"ingest failed: {resp.status_code} {resp.text}"

    # Wait for the worker thread to run the probe
    assert done.wait(timeout=5), "Worker did not execute within 5s"

    # The worker must have seen the authenticated user's workspace
    assert captured["ws"] is not None, "Worker saw no WorkspaceContext"
    assert captured["user_id"] == user_id, \
        f"Worker saw user_id={captured['user_id']!r}, expected {user_id!r}"
    assert captured["ws"].brand_id == brand_id, \
        "Worker lost the active brand context"
    assert captured["root"] == ws_root, \
        f"Worker resolved root={captured['root']!r}, expected {ws_root!r}"
    assert captured["root"] != captured["global_fallback"], \
        "Worker fell back to global project root instead of user workspace"


# --- Defense-in-depth: every threading.Thread in web_viewer uses the helper ---

def test_all_web_viewer_thread_launches_use_with_workspace_context():
    """Static check: every ``threading.Thread(target=...)`` in web_viewer.py
    must wrap its target with ``with_workspace_context(...)``.

    This is defense-in-depth — it does not replace the runtime test above.
    """
    import re
    web_viewer_path = Path(__file__).resolve().parent.parent / "web_viewer.py"
    source = web_viewer_path.read_text(encoding="utf-8")

    # Find every threading.Thread(target=... line
    thread_lines = [
        (i + 1, line)
        for i, line in enumerate(source.splitlines())
        if "threading.Thread(target=" in line
    ]
    assert thread_lines, "Expected at least one threading.Thread launch in web_viewer.py"

    missing = []
    for lineno, line in thread_lines:
        if "with_workspace_context" not in line:
            missing.append((lineno, line.strip()))
    assert not missing, (
        f"{len(missing)} thread launch(es) in web_viewer.py missing with_workspace_context:\n"
        + "\n".join(f"  line {n}: {l}" for n, l in missing)
    )
