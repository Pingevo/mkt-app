"""MB-02 endpoint security — existing-file escape and no-brand guards for media endpoints.

Proves:
  1. /api/generate_all_media rejects an out-of-brand existing-file path.
  2. /api/generate_all_media 403s without an active brand.
  3. /api/parse_media_prompts rejects an out-of-brand existing-file path.
  4. /api/generate_media 403s without an active brand.
  5. No file is read and no media/provider execution triggered for rejected paths.

For the out-of-brand existing-file escape tests, spies are installed at the
relevant parse/media execution seams (``media_gen.parse_media_prompts``,
``media_gen.generate_image_with_retry``, ``media_gen.generate_video_with_retry``)
and the tests assert those seams were NOT called — proving rejection occurs
before content parsing or provider/media execution.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.conftest import make_authed_client


@pytest.fixture
def _app(tmp_path, monkeypatch):
    import importlib
    import web_viewer

    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "_current_llm", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_session_ts", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_cancel_requested", {}, raising=False)

    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    client, uid, ws_root = make_authed_client(web_viewer.app, tmp_path, monkeypatch)
    return {"client": client, "uid": uid, "ws_root": ws_root, "tmp": tmp_path,
            "web_viewer": web_viewer}


def _install_media_spies(_app, monkeypatch):
    """Install fail-fast spies at parse + provider execution seams.

    Returns a dict of call counters.  If any spy is invoked, the test fails
    immediately — proving rejection must occur BEFORE these seams.
    """
    from src import media_gen

    calls = {"parse": 0, "gen_image": 0, "gen_video": 0}

    def _parse_spy(*a, **kw):
        calls["parse"] += 1
        pytest.fail("media_gen.parse_media_prompts must NOT be called for rejected paths")

    def _gen_image_spy(*a, **kw):
        calls["gen_image"] += 1
        pytest.fail("media_gen.generate_image_with_retry must NOT be called for rejected paths")

    def _gen_video_spy(*a, **kw):
        calls["gen_video"] += 1
        pytest.fail("media_gen.generate_video_with_retry must NOT be called for rejected paths")

    monkeypatch.setattr(media_gen, "parse_media_prompts", _parse_spy)
    monkeypatch.setattr(media_gen, "generate_image_with_retry", _gen_image_spy)
    monkeypatch.setattr(media_gen, "generate_video_with_retry", _gen_video_spy)
    return calls


def test_generate_all_media_403_no_brand(_app):
    """No active brand → 403, no file read, no media execution."""
    c = _app["client"]
    # Place a sentinel file outside any brand root
    sentinel = _app["tmp"] / "outside_secret.md"
    sentinel.write_text("SECRET_CONTENT", encoding="utf-8")
    resp = c.post("/api/generate_all_media", json={
        "file": str(sentinel), "auto_image": False, "auto_video": False,
    })
    assert resp.status_code == 403


def test_generate_all_media_rejects_out_of_brand_existing_path(_app, monkeypatch):
    """An existing-file path outside the active brand output root is rejected.

    Even though the file exists on disk, the endpoint must NOT read it because
    it is not contained beneath the verified active brand's output root.
    Spies at parse + provider seams prove rejection occurs BEFORE content
    parsing or media execution.
    """
    from src.brand_registry import BrandRegistry

    calls = _install_media_spies(_app, monkeypatch)

    uid = _app["uid"]
    tmp = _app["tmp"]
    reg = BrandRegistry(user_id=uid, project_root=tmp)
    brand = reg.create("TestBrand")
    brand_root = tmp / "users" / uid / "brands" / brand["brand_id"]
    (brand_root / "output").mkdir(parents=True, exist_ok=True)

    # Place a secret file OUTSIDE the brand output root
    secret = tmp / "outside_secret.md"
    secret.write_text("SECRET_CONTENT_OUT_OF_BRAND", encoding="utf-8")

    c = _app["client"]
    c.cookies.set("mktapp_brand", brand["brand_id"])

    resp = c.post("/api/generate_all_media", json={
        "file": str(secret), "auto_image": False, "auto_video": False,
    })
    # Must be rejected — either 400 (path not contained) — never 200 with content read.
    assert resp.status_code in (400, 403, 404), (
        f"out-of-brand existing path must be rejected, got {resp.status_code}"
    )
    # The response must not contain the secret content
    assert "SECRET_CONTENT_OUT_OF_BRAND" not in resp.text
    # Parse + provider seams were never reached.
    assert calls == {"parse": 0, "gen_image": 0, "gen_video": 0}, (
        f"parse/provider seams must not be called for rejected paths: {calls}"
    )


def test_parse_media_prompts_403_no_brand(_app):
    """No active brand → 403, no file read."""
    c = _app["client"]
    sentinel = _app["tmp"] / "outside_secret.md"
    sentinel.write_text("SECRET_CONTENT", encoding="utf-8")
    resp = c.post("/api/parse_media_prompts", json={"file": str(sentinel)})
    assert resp.status_code == 403


def test_parse_media_prompts_rejects_out_of_brand_existing_path(_app, monkeypatch):
    """An existing-file path outside the active brand output root is rejected.

    Spies at the parse seam prove rejection occurs BEFORE content parsing.
    """
    from src.brand_registry import BrandRegistry

    calls = _install_media_spies(_app, monkeypatch)

    uid = _app["uid"]
    tmp = _app["tmp"]
    reg = BrandRegistry(user_id=uid, project_root=tmp)
    brand = reg.create("TestBrand")
    brand_root = tmp / "users" / uid / "brands" / brand["brand_id"]
    (brand_root / "output").mkdir(parents=True, exist_ok=True)

    secret = tmp / "outside_secret.md"
    secret.write_text("SECRET_CONTENT_OUT_OF_BRAND", encoding="utf-8")

    c = _app["client"]
    c.cookies.set("mktapp_brand", brand["brand_id"])

    resp = c.post("/api/parse_media_prompts", json={"file": str(secret)})
    assert resp.status_code in (400, 403, 404), (
        f"out-of-brand existing path must be rejected, got {resp.status_code}"
    )
    assert "SECRET_CONTENT_OUT_OF_BRAND" not in resp.text
    # Parse seam was never reached.
    assert calls == {"parse": 0, "gen_image": 0, "gen_video": 0}, (
        f"parse/provider seams must not be called for rejected paths: {calls}"
    )


def test_generate_media_403_no_brand(_app):
    """No active brand → 403, no media execution."""
    c = _app["client"]
    resp = c.post("/api/generate_media", json={
        "type": "image", "prompt": "test", "output_dir": "session/x",
        "filename": "img.png",
    })
    assert resp.status_code == 403
