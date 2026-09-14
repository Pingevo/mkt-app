"""MB-02: /api/data_folders is brand-scoped.

Proves:
- Active Brand A1 → only A1 data folders returned
- Active Brand A2 → only A2 data folders returned
- No active brand → controlled 403
- Tampered/foreign brand cookie → fail-closed (user-only context, 403)
- No fallback to legacy user-root product data
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.conftest import make_authed_client


@pytest.fixture
def _app(tmp_path, monkeypatch):
    """Create an isolated app with auth + two brands for the test user."""
    import importlib
    import web_viewer

    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    # Do NOT patch DATA_DIR/OUTPUT_DIR/etc — they resolve via workspace context
    monkeypatch.setattr(web_viewer, "_read_folder", lambda f: (["info"], [], {}))
    monkeypatch.setattr(web_viewer, "_current_llm", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_session_ts", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_cancel_requested", {}, raising=False)

    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    client, uid, _ = make_authed_client(web_viewer.app, tmp_path, monkeypatch)

    # Create two brands
    resp = client.post("/api/brands", json={"name": "A1"})
    assert resp.status_code == 200
    a1_id = resp.json()["brand_id"]
    resp = client.post("/api/brands", json={"name": "A2"})
    assert resp.status_code == 200
    a2_id = resp.json()["brand_id"]

    # Create brand-scoped data dirs for each brand
    for bid, product_name in [(a1_id, "ProductA1"), (a2_id, "ProductA2")]:
        brand_root = tmp_path / "users" / uid / "brands" / bid
        (brand_root / "data" / product_name).mkdir(parents=True)
        (brand_root / "data" / product_name / "info.txt").write_text(
            f"info for {product_name}", encoding="utf-8"
        )
        (brand_root / "cache" / product_name).mkdir(parents=True)
        (brand_root / "output").mkdir(parents=True)
        (brand_root / "brand").mkdir(parents=True)

    return {"client": client, "uid": uid, "a1_id": a1_id, "a2_id": a2_id, "tmp": tmp_path}


def test_a1_data_folders_only_a1(_app):
    """Active Brand A1 → only A1's product folders returned."""
    c = _app["client"]
    resp = c.post(f"/api/brands/{_app['a1_id']}/select")
    assert resp.status_code == 200
    resp = c.get("/api/data_folders")
    assert resp.status_code == 200
    folders = resp.json()
    names = [f.get("name", "") for f in folders]
    assert "ProductA1" in names
    assert "ProductA2" not in names, "A2 product must not leak into A1"


def test_a2_data_folders_only_a2(_app):
    """Active Brand A2 → only A2's product folders returned."""
    c = _app["client"]
    resp = c.post(f"/api/brands/{_app['a2_id']}/select")
    assert resp.status_code == 200
    resp = c.get("/api/data_folders")
    assert resp.status_code == 200
    folders = resp.json()
    names = [f.get("name", "") for f in folders]
    assert "ProductA2" in names
    assert "ProductA1" not in names, "A1 product must not leak into A2"


def test_no_active_brand_returns_403(_app):
    """No active brand → controlled 403, not 500."""
    c = _app["client"]
    # Deselect brand (if any was selected)
    c.delete("/api/brands/active")
    resp = c.get("/api/data_folders")
    assert resp.status_code == 403
    assert "brand" in resp.json().get("error", "").lower()


def test_tampered_brand_cookie_fails_closed(_app):
    """Tampered/foreign brand cookie → fail-closed (403, not 500)."""
    c = _app["client"]
    # Set a fake brand cookie
    c.cookies.set("mktapp_brand", "nonexistent_brand_id")
    resp = c.get("/api/data_folders")
    assert resp.status_code == 403, "invalid brand cookie must fail closed"


def test_no_fallback_to_user_root_data(_app):
    """Brand-scoped data_folders must not return user-root product data."""
    c = _app["client"]
    # Create user-root product data (legacy)
    uid = _app["uid"]
    tmp = _app["tmp"]
    (tmp / "users" / uid / "data" / "LegacyProduct").mkdir(parents=True)
    (tmp / "users" / uid / "data" / "LegacyProduct" / "info.txt").write_text(
        "legacy", encoding="utf-8"
    )

    resp = c.post(f"/api/brands/{_app['a1_id']}/select")
    assert resp.status_code == 200
    resp = c.get("/api/data_folders")
    assert resp.status_code == 200
    folders = resp.json()
    names = [f.get("name", "") for f in folders]
    assert "LegacyProduct" not in names, "user-root product must not leak into brand scope"
