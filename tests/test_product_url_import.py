"""Product URL import — endpoint + ingestion-seam integration. NO live network.

External HTTP goes through httpx.MockTransport injected into
``src.url_import._make_client``; DNS through a ``socket.getaddrinfo``
wrapper. The OpenRouter key is stubbed empty so ingestion runs its
deterministic (no-LLM) path — zero paid/provider/network calls.

Proves:
  POST /api/product_from_url under U1/Brand A1
    → normalized artifacts in A1's data/ + product record in cache/
    → existing ingest_product seam → status ready
    → raw_text carries fetched content; image file lands in image paths
    → /api/data_folders lists it; get_scoped_context_text sees the text
    → provenance (original_url/final_url/fetched_at) on the record
  Ownership: 401 without auth, 403 without brand, A1↔A2 isolation,
  controlled errors for bad/SSRF/existing-name inputs.
"""
from __future__ import annotations

import json
import socket
import time

import httpx
import pytest

from tests.conftest import make_authed_client, make_brand_client


PUBLIC_IP = "93.184.216.34"
MARKER = "TURBOBLEND-9000-BROWSERLESS-OK"
PRODUCT_URL = "https://shop.example.com/products/acme-blender"

PRODUCT_HTML = f"""<!DOCTYPE html>
<html><head>
<title>ACME Turbo Blender</title>
<meta property="og:title" content="ACME Turbo Blender 9000">
<meta property="og:description" content="Powerful 900W kitchen blender">
<meta property="og:image" content="https://shop.example.com/img/blender.png">
</head><body>
<h1>ACME Turbo Blender 9000</h1>
<p>Price: 2,590 THB</p>
<p>Flagship kitchen blender with a 900W copper motor, BPA-free 1.5L jug,
six stainless-steel blades, three speed settings, and dishwasher-safe
parts. Ships nationwide within 2-4 business days.</p>
<ul><li>900W motor</li><li>{MARKER}</li><li>2-year warranty</li></ul>
<table><tr><th>Power</th><td>900W</td></tr></table>
<script>var tracker=1;</script>
</body></html>""".encode()

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 500


def _routes() -> dict:
    return {
        PRODUCT_URL: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=PRODUCT_HTML,
        ),
        "https://shop.example.com/img/blender.png": httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        ),
    }


def _handler(request: httpx.Request) -> httpx.Response:
    entry = _routes().get(str(request.url))
    return entry if entry is not None else httpx.Response(404, content=b"nf")


def _stub_fetch(monkeypatch):
    """Patch url_import's client factory + DNS + the LLM key."""
    from src import url_import
    import src.openrouter_gateway as gateway

    monkeypatch.setattr(
        url_import, "_make_client",
        lambda: httpx.Client(transport=httpx.MockTransport(_handler)),
    )

    orig = socket.getaddrinfo

    def fake_gai(host, port=0, *a, **kw):
        if host == "shop.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 443))]
        return orig(host, port, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    monkeypatch.setattr(gateway, "get_api_key", lambda: "")
    # Browser fallback is exercised in test_url_import_browser.py — this
    # suite must never launch Chromium or touch real network.
    monkeypatch.setattr(
        url_import, "_browser_fetch",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("browser must not launch in this suite")),
    )


@pytest.fixture
def _app(tmp_path, monkeypatch):
    import importlib
    import web_viewer

    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "_current_llm", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_session_ts", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_cancel_requested", {}, raising=False)
    _stub_fetch(monkeypatch)

    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    client, uid, bid, brand_root = make_brand_client(
        web_viewer.app, tmp_path, monkeypatch
    )
    return {
        "client": client, "uid": uid, "bid": bid,
        "brand_root": brand_root, "tmp": tmp_path,
    }


def _wait_ready(client, folder: str, timeout: float = 20.0) -> dict:
    """Poll the real ingest-status endpoint until the product is ready."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = client.get(f"/api/ingest_status/{folder}")
        last = r.json()
        if last.get("status") in ("ready", "no_usable_data", "stale"):
            return last
        time.sleep(0.2)
    raise AssertionError(f"ingest never finished: {last}")


# ---------------------------------------------------------------------------
# Happy path — URL → existing ingestion seam → same product lifecycle
# ---------------------------------------------------------------------------

def test_import_creates_ready_product_via_existing_seam(_app):
    c = _app["client"]
    r = c.post("/api/product_from_url", json={"url": PRODUCT_URL})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    folder = data["folder"]

    st = _wait_ready(c, folder)
    assert st["status"] == "ready", st

    brand_root = _app["brand_root"]
    # source artifact on disk under the brand's data root
    src = brand_root / "data" / folder / "source_page.txt"
    assert src.exists(), "normalized source_page.txt must exist in data/"
    assert MARKER in src.read_text(encoding="utf-8")

    # product record — same shape as an uploaded product
    rec_path = brand_root / "cache" / folder / "product.json"
    record = json.loads(rec_path.read_text(encoding="utf-8"))
    assert record["status"] == "ready"
    assert MARKER in record["raw_text"]
    assert any(f["name"] == "source_page.txt" for f in record["files"])
    assert record["image_descriptions"], "downloaded image must be registered"

    # provenance — original/final URL + timestamp retrievable
    prov = record.get("source_import")
    assert prov and prov["original_url"] == PRODUCT_URL
    assert prov["final_url"] == PRODUCT_URL
    assert prov["fetched_at"]

    # product list seam
    folders = c.get("/api/data_folders").json()
    names = [f["name"] for f in folders]
    assert folder in names

    # Agent-context seam — same one upload-created products use
    from src.workspace_context import WorkspaceContext, set_workspace
    set_workspace(WorkspaceContext.for_brand(_app["uid"], _app["bid"], _app["tmp"]))
    try:
        from src import product_db
        ctx_text = product_db.get_scoped_context_text([folder])
        assert MARKER in ctx_text
        img_paths = product_db.get_product_image_paths(folder)
        assert img_paths, "imported product image must reach the image seam"
        assert all(
            str(_app["brand_root"]) in p for p in img_paths
        ), "image paths must live inside the brand workspace"
    finally:
        set_workspace(None)


# ---------------------------------------------------------------------------
# Ownership guards
# ---------------------------------------------------------------------------

def test_no_auth_401(tmp_path, monkeypatch):
    import importlib
    import web_viewer
    from starlette.testclient import TestClient

    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    _stub_fetch(monkeypatch)
    r = TestClient(web_viewer.app).post(
        "/api/product_from_url", json={"url": PRODUCT_URL}
    )
    assert r.status_code == 401


def test_no_brand_403(tmp_path, monkeypatch):
    import importlib
    import web_viewer

    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    _stub_fetch(monkeypatch)
    client, _uid, _ws = make_authed_client(web_viewer.app, tmp_path, monkeypatch)
    r = client.post("/api/product_from_url", json={"url": PRODUCT_URL})
    assert r.status_code == 403
    assert "brand" in r.json().get("error", "").lower()


def test_a1_import_invisible_from_a2(_app):
    c = _app["client"]
    r = c.post("/api/product_from_url", json={"url": PRODUCT_URL})
    assert r.status_code == 200
    folder = r.json()["folder"]
    _wait_ready(c, folder)

    # Create + select brand A2 through the real brand API
    r2 = c.post("/api/brands", json={"name": "BrandA2"})
    assert r2.status_code == 200
    bid2 = r2.json()["brand_id"]
    assert c.post(f"/api/brands/{bid2}/select").status_code == 200

    names_a2 = [f["name"] for f in c.get("/api/data_folders").json()]
    assert folder not in names_a2
    a2_data = _app["tmp"] / "users" / _app["uid"] / "brands" / bid2 / "data"
    assert not (a2_data / folder).exists()

    # Switch back to A1 — product returns
    assert c.post(f"/api/brands/{_app['bid']}/select").status_code == 200
    names_a1 = [f["name"] for f in c.get("/api/data_folders").json()]
    assert folder in names_a1


# ---------------------------------------------------------------------------
# Controlled failures
# ---------------------------------------------------------------------------

def test_invalid_and_ssrf_urls_rejected(_app):
    c = _app["client"]
    for bad in ["", "not-a-url", "file:///etc/passwd", "ftp://x/y"]:
        r = c.post("/api/product_from_url", json={"url": bad})
        assert r.status_code == 400, (bad, r.status_code)
        assert r.json().get("error")

    # SSRF — localhost must be refused by the fetch layer, controlled error
    r = c.post("/api/product_from_url", json={"url": "http://127.0.0.1/x"})
    assert r.status_code == 400
    assert r.json().get("error")
    assert "127.0.0.1" not in r.json()["error"], "no internals in user message"

    # nothing was written to the brand data root
    data_dir = _app["brand_root"] / "data"
    leftovers = [p for p in data_dir.iterdir() if p.is_dir()]
    assert leftovers == [], f"rejected imports must not create products: {leftovers}"


def test_explicit_name_collision_rejected(_app):
    c = _app["client"]
    data_dir = _app["brand_root"] / "data" / "ExistingProd"
    data_dir.mkdir(parents=True)
    (data_dir / "info.txt").write_text("existing data", encoding="utf-8")

    r = c.post("/api/product_from_url",
               json={"url": PRODUCT_URL, "product_name": "ExistingProd"})
    assert r.status_code == 409
    assert "ExistingProd" in r.json()["error"]


def test_derived_name_dedup_on_reimport(_app):
    """Same URL imported twice → second product gets a deduped name, never merged."""
    c = _app["client"]
    r1 = c.post("/api/product_from_url", json={"url": PRODUCT_URL})
    assert r1.status_code == 200
    f1 = r1.json()["folder"]
    _wait_ready(c, f1)

    r2 = c.post("/api/product_from_url", json={"url": PRODUCT_URL})
    assert r2.status_code == 200
    f2 = r2.json()["folder"]
    assert f2 != f1
    _wait_ready(c, f2)


def test_explicit_name_used(_app):
    c = _app["client"]
    r = c.post("/api/product_from_url",
               json={"url": PRODUCT_URL, "product_name": "My Named Product"})
    assert r.status_code == 200
    assert r.json()["folder"] == "My Named Product"
    _wait_ready(c, "My Named Product")
