"""MB-02 Slice 2 — run-endpoint brand_dir security + fail-closed.

Proves:
  1. Run endpoints (/api/run_agent, /api/run_agents, /api/run_flows,
     /api/run_auto) fail with a controlled 4xx when no active brand context
     exists — no legacy/default-brand fallback.
  2. A client-supplied ``brand_dir`` in the request body can no longer redirect
     execution to an arbitrary filesystem path or another brand's state.
     The server derives brand state from the verified active brand context.
  3. ``_get_orchestrator`` ignores client ``brand_dir`` when a brand context
     is active — the browser never chooses a filesystem path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import make_authed_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _web_viewer(tmp_path, monkeypatch):
    import web_viewer
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    return web_viewer


def _authed_with_brand(web_viewer, tmp_path, monkeypatch):
    """Create an authed client + select a brand (sets brand cookie)."""
    client, uid, _ = make_authed_client(web_viewer.app, tmp_path, monkeypatch)
    resp = client.post("/api/brands", json={"name": "A1"})
    assert resp.status_code == 200
    bid = resp.json()["brand_id"]
    resp = client.post(f"/api/brands/{bid}/select")
    assert resp.status_code == 200
    return client, uid, bid


def _authed_no_brand(web_viewer, tmp_path, monkeypatch):
    """Create an authed client with NO brand selected."""
    client, uid, _ = make_authed_client(web_viewer.app, tmp_path, monkeypatch)
    return client, uid


# ---------------------------------------------------------------------------
# 1. Fail-closed — no active brand → 4xx
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", [
    "/api/run_agent",
    "/api/run_agents",
    "/api/run_flows",
    "/api/run_auto",
])
def test_run_endpoint_fails_closed_without_brand(_web_viewer, tmp_path, monkeypatch, endpoint):
    """Without an active brand, brand-scoped run endpoints must return 4xx."""
    client, _ = _authed_no_brand(_web_viewer, tmp_path, monkeypatch)
    payload = {
        "agent": "content_creator",
        "folder": "SomeProduct",
        "quick_brief": "test",
    }
    resp = client.post(endpoint, json=payload)
    assert resp.status_code in (400, 403), (
        f"{endpoint} must fail closed (4xx) without active brand, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 2. Client brand_dir cannot redirect execution
# ---------------------------------------------------------------------------

def test_get_orchestrator_ignores_client_brand_dir_when_brand_active(_web_viewer, tmp_path, monkeypatch):
    """_get_orchestrator must ignore client brand_dir when a brand context is active.

    The orchestrator's brand state must resolve to the active brand root,
    not the client-supplied path.
    """
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    from src.brand_registry import BrandRegistry

    reg = BrandRegistry(user_id="test_user", project_root=tmp_path)
    brand = reg.create("A1")
    ws = WorkspaceContext.for_brand("test_user", brand["brand_id"], tmp_path)
    token = set_workspace(ws)
    try:
        # Write A1 brand voice
        from src.local_workspace import local_brand_dir
        bdir = local_brand_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "voice.json").write_text(
            '{"personality": "A1 only"}', encoding="utf-8")

        # Client tries to redirect to an arbitrary path
        _web_viewer._get_orchestrator("../../etc/passwd")
        from src.brand_loader import load_brand_rules
        rules = load_brand_rules()
        assert "A1 only" in rules, "client brand_dir must not redirect brand state"
        assert "../../etc" not in rules
    finally:
        reset_workspace(token)


def test_run_agent_ignores_client_brand_dir(_web_viewer, tmp_path, monkeypatch):
    """POST /api/run_agent with a malicious brand_dir must not error or redirect.

    With A1 active, a client-supplied brand_dir="../../etc" must be ignored.
    The endpoint should start normally (200 SSE) using A1's brand state.
    """
    client, uid, bid = _authed_with_brand(_web_viewer, tmp_path, monkeypatch)

    # Create a product in A1 so the run doesn't fail on missing product
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    ws = WorkspaceContext.for_brand(uid, bid, tmp_path)
    token = set_workspace(ws)
    try:
        from src import product_db
        product_db.save("P1", {"product_id": "P1", "status": "ready"})
        (product_db._project_root() / "data" / "P1").mkdir(parents=True, exist_ok=True)
    finally:
        reset_workspace(token)

    payload = {
        "agent": "content_creator",
        "folder": "P1",
        "quick_brief": "test",
        "brand_dir": "../../etc/passwd",  # malicious — must be ignored
    }
    resp = client.post("/api/run_agent", json=payload)
    # Should start SSE (200) — brand_dir is ignored, not used for filesystem access.
    # A 4xx here would mean the path was used (security failure).
    assert resp.status_code == 200, (
        f"client brand_dir caused rejection: {resp.status_code} {resp.text[:200]}"
    )
