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
from pathlib import Path

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
<script type="application/ld+json">{{"@context":"https://schema.org",
"@type":"Product","name":"ACME Turbo Blender 9000",
"offers":{{"@type":"Offer","price":"2590","priceCurrency":"THB"}}}}</script>
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

MULTI_URL = "https://shop.example.com/catalog/blenders"
MULTI_HTML = b"""<!DOCTYPE html>
<html><head><title>Blender Catalog</title>
<script type="application/ld+json">{"@context":"https://schema.org",
"@type":"ItemList","itemListElement":[
{"@type":"Product","name":"Blender A"},
{"@type":"Product","name":"Blender B"}]}</script>
</head><body><h1>All blenders</h1>
<p>Compare our full blender range side by side. From compact personal
blenders to commercial 2-litre machines, every model is listed here
with prices, wattage, jug capacity and warranty terms so shoppers can
pick the right one for their kitchen.</p>
<ul><li>Blender A - 900W - 2,590 THB</li>
<li>Blender B - 1200W - 3,990 THB</li>
<li>Blender C - 1500W - 5,490 THB</li></ul>
</body></html>"""

AMBIG_URL = "https://shop.example.com/maybe-product"
AMBIG_HTML = b"""<!DOCTYPE html>
<html><head><title>Something</title>
<meta property="og:type" content="product">
<meta property="og:title" content="Mystery Item">
</head><body><h1>Mystery Item</h1>
<p>A page with og:type=product but no JSON-LD product evidence -
per contract, og:type alone is insufficient confidence. The page
still carries enough ordinary body text to be a usable fetched
document, so the rejection must come from the classifier and not
from the thin-content browser fallback path.</p>
</body></html>"""

# Client-rendered storefront shape (real AliExpress shell, 2026-09): the
# static HTML is fat boilerplate that passes MIN_BODY_CHARS but carries
# zero JSON-LD — the Product node only exists after a browser render.
SHELL_URL = "https://shop.example.com/item/shell-product"
SHELL_HTML = b"""<!DOCTYPE html>
<html><head><title>Shop</title>
<meta property="og:type" content="product">
<meta name="description" content="Marketplace tagline">
</head><body><h1>Shop</h1>
<nav>Help Center | Disputes and Reports | Return and refund policy |
IPR report | Transparency center | Free returns | Buyer protection</nav>
<footer>Multilingual site: English, Thai, Vietnamese, Arabic, Polish -
browse by category, coupons, new user zone, help center and disputes.</footer>
</body></html>"""
SHELL_RENDERED_HTML = b"""<!DOCTYPE html>
<html><head><title>Shell Product</title>
<script type="application/ld+json">{"@context":"https://schema.org",
"@type":"Product","name":"Shell Rendered Widget",
"offers":{"@type":"Offer","price":"499","priceCurrency":"THB"}}</script>
</head><body><h1>Shell Rendered Widget</h1>
<p>Real product content that only exists after the client render: full
specs, price 499 THB, warranty terms, shipping options and a feature
list long enough to be a real product document rather than a shell.
SHELL-RENDERED-MARKER proves the body came from the browser document.</p>
</body></html>"""

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 500

# A single-product page whose spec rows flatten to literal
# ``label : value`` lines — the real K5 page shape (FREE-URL-FACTS-01).
# Body rows are rebuilt from the sanitized real-page text fixture; the
# JSON-LD chunks (Product + isRelatedTo recommendations) come from the
# existing classification fixture, so this page is also a live proof
# that related products never become facts.
_K5_TEXT_FIXTURE = Path(__file__).parent / "fixtures" / "url_thaisuperphone_k5_page.json"
_K5_JSONLD_FIXTURE = Path(__file__).parent / "fixtures" / "url_thaisuperphone_k5.json"
K5LIKE_URL = "https://shop.example.com/products/k5-shaped"


def _k5like_html() -> bytes:
    import html as _html
    page = json.loads(_K5_TEXT_FIXTURE.read_text(encoding="utf-8"))
    cls = json.loads(_K5_JSONLD_FIXTURE.read_text(encoding="utf-8"))
    rows = "".join(
        f"<div>{_html.escape(ln)}</div>"
        for ln in page["text"].splitlines() if ln.strip())
    scripts = "".join(
        f'<script type="application/ld+json">{c}</script>'
        for c in cls["jsonld_chunks"])
    return ("<!DOCTYPE html><html><head><title>K5-shaped product page</title>"
            + scripts + "</head><body>" + rows + "</body></html>"
            ).encode("utf-8")


# Same generic `label : value` structure, unrelated English product —
# proves the extraction is structural, not page-specific.
GENSPEC_URL = "https://shop.example.com/products/acme-torch"
GENSPEC_HTML = b"""<!DOCTYPE html>
<html><head><title>Acme Torch Pro</title>
<script type="application/ld+json">{"@context":"https://schema.org",
"@type":"Product","name":"Acme Torch Pro"}</script>
</head><body>
<h1>Acme Torch Pro</h1>
<p>Rechargeable LED torch for camping and emergency kits with a machined
aluminium body, USB-C charging and three beam modes for everyday carry.
Built for outdoor use with a sealed tail cap and impact resistance.</p>
<div>Brand : Acme</div>
<div>Battery : 900mAh</div>
<div>Waterproof : IP67</div>
<div>Warranty : 12 months</div>
</body></html>"""


def _routes() -> dict:
    return {
        PRODUCT_URL: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=PRODUCT_HTML,
        ),
        MULTI_URL: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=MULTI_HTML,
        ),
        AMBIG_URL: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=AMBIG_HTML,
        ),
        "https://shop.example.com/img/blender.png": httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        ),
        K5LIKE_URL: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=_k5like_html(),
        ),
        GENSPEC_URL: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=GENSPEC_HTML,
        ),
        SHELL_URL: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=SHELL_HTML,
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
    # Browser fallback is exercised for real in test_url_import_browser.py —
    # here a virtual browser returns the URL's rendered document: the
    # _BROWSER_ROUTES override when a page is client-rendered, otherwise the
    # same static fixture (verdicts unchanged, no Chromium, no network).
    rendered = {SHELL_URL: SHELL_RENDERED_HTML}

    def _virtual_browser(url, **kw):
        body = rendered.get(url)
        if body is None:
            entry = _routes().get(url)
            if entry is None:
                raise AssertionError(f"unexpected browser fetch: {url}")
            body = entry.content
        return url, body

    monkeypatch.setattr(url_import, "_browser_fetch", _virtual_browser)


class _FakeLLM:
    """Deterministic detector + enrichment — SINGLE-PRODUCT-URL-CONTRACT-01
    requires a real detection result, so the suite supplies one: single
    product for segmentation, valid JSON for summary/profile calls.
    Pass ``seg`` to simulate multi/ambiguous detection outcomes."""

    def __init__(self, seg=None):
        self._seg = seg
        self.calls: list[str] = []

    def chat(self, messages, *, response_format=None, source="", **kw):
        self.calls.append(source)
        if "segment" in (source or ""):
            if isinstance(self._seg, Exception):
                raise self._seg
            seg = self._seg if self._seg is not None else {
                "mode": "single",
                "products": [{
                    "product_key": "", "suggested_name": "source_page",
                    "category": "", "summary": "",
                    "source_refs": [], "common_refs": [],
                    "text": "",
                }],
            }
            return json.dumps(seg, ensure_ascii=False)
        if "metadata_summary" in (source or ""):
            return json.dumps({"summary": "s", "category": "",
                               "derived_facts": {}})
        return json.dumps({
            "audience": {}, "competitors": [], "differentiators": [],
            "use_cases": [], "price_tier": "mid",
            "tone_adjustment": "", "visual_override": {},
        })

    def close(self):
        pass


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
    # URL single-product contract needs a real detector — provide a
    # deterministic one (tests that need multi/ambiguous patch it again).
    import src.ingestion as _ing
    _state = {"llm": _FakeLLM()}
    monkeypatch.setattr(_ing, "_make_llm", lambda: _state["llm"])

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
        "llm_state": _state,
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


def _import_and_commit(client, url: str, product_name: str | None = None):
    """Drive the staged URL-import contract: import → review → commit.

    Returns (commit_response_json, created_folder_name)."""
    body = {"url": url}
    if product_name:
        body["product_name"] = product_name
    r = client.post("/api/product_from_url", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True and data.get("staged") is True, data
    bid = data["batch_id"]
    # human review: create the first segment using its previewed name
    name = data["segments"][0].get("suggested_name") or "imported"
    rc = client.post(f"/api/stage/{bid}/commit", json={
        "choices": [{"segment_index": 0, "action": "create", "name": name}]})
    assert rc.status_code == 200, rc.text
    out = rc.json()
    assert out["created"], out
    return out, out["created"][0]


# ---------------------------------------------------------------------------
# Happy path — URL → existing ingestion seam → same product lifecycle
# ---------------------------------------------------------------------------

def test_import_creates_ready_product_via_existing_seam(_app):
    c = _app["client"]
    _out, folder = _import_and_commit(c, PRODUCT_URL)

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
    _out, folder = _import_and_commit(c, PRODUCT_URL)
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
    """Same URL imported twice → second commit gets a deduped name, never merged."""
    c = _app["client"]
    _o1, f1 = _import_and_commit(c, PRODUCT_URL)
    _wait_ready(c, f1)

    _o2, f2 = _import_and_commit(c, PRODUCT_URL)
    assert f2 != f1
    _wait_ready(c, f2)


def test_explicit_name_used(_app):
    """Explicit product_name flows into the staging preview's suggested_name
    and becomes the created product's name after human commit."""
    c = _app["client"]
    r = c.post("/api/product_from_url",
               json={"url": PRODUCT_URL, "product_name": "My Named Product"})
    assert r.status_code == 200
    data = r.json()
    assert data.get("staged") is True
    assert data["segments"][0]["suggested_name"] == "My Named Product"
    # nothing materialized before review
    assert not (_app["brand_root"] / "data" / "My Named Product").exists()

    bid = data["batch_id"]
    rc = c.post(f"/api/stage/{bid}/commit", json={
        "choices": [{"segment_index": 0, "action": "create",
                     "name": "My Named Product"}]})
    assert rc.status_code == 200
    assert rc.json()["created"] == ["My Named Product"]
    _wait_ready(c, "My Named Product")


# ---------------------------------------------------------------------------
# FREE-IMPORT-AI-PROPOSAL-01 — deterministic multi/ambiguous rejection:
# structured page evidence only, no model call ever, nothing materialized.
# ---------------------------------------------------------------------------


def _data_products(brand_root):
    d = brand_root / "data"
    return {p.name for p in d.iterdir()
            if p.is_dir() and not p.name.startswith(".")}


def test_multi_product_url_rejected_zero_effects(_app):
    """ItemList/multi-product page → 400 + Thai single-product message;
    zero products, zero model calls, no staging leftovers."""
    c = _app["client"]
    before = _data_products(_app["brand_root"])

    r = c.post("/api/product_from_url", json={"url": MULTI_URL})
    assert r.status_code == 400, r.text
    data = r.json()
    assert data["ok"] is False
    assert data.get("multi_product") is True
    assert "สินค้า" in data["error"]

    # zero materialization
    assert _data_products(_app["brand_root"]) == before
    # classification is deterministic — the LLM seam was never touched
    assert _app["llm_state"]["llm"].calls == []
    # batch discarded — no staging leftovers
    staging_dir = _app["brand_root"] / "data" / ".staging"
    assert not staging_dir.exists() or not any(staging_dir.iterdir())


def test_ambiguous_url_fails_safe(_app):
    """og:type=product without JSON-LD Product evidence → reject as
    ambiguous; zero products, zero model calls."""
    c = _app["client"]
    before = _data_products(_app["brand_root"])

    r = c.post("/api/product_from_url", json={"url": AMBIG_URL})
    assert r.status_code == 400, r.text
    data = r.json()
    assert data["ok"] is False
    assert data.get("ambiguous_product") is True
    assert _data_products(_app["brand_root"]) == before
    assert _app["llm_state"]["llm"].calls == []
    staging_dir = _app["brand_root"] / "data" / ".staging"
    assert not staging_dir.exists() or not any(staging_dir.iterdir())


def test_client_rendered_shell_imports_single_product(_app):
    """Fat boilerplate shell (no static JSON-LD) → the browser-rendered
    document confirms exactly one product → staged, committed, provenance
    kept.  This is the AliExpress item-page defect end to end."""
    c = _app["client"]
    out, folder = _import_and_commit(c, SHELL_URL)

    st = _wait_ready(c, folder)
    assert st["status"] == "ready", st
    rec_path = _app["brand_root"] / "cache" / folder / "product.json"
    record = json.loads(rec_path.read_text(encoding="utf-8"))
    assert "SHELL-RENDERED-MARKER" in record["raw_text"]
    prov = record.get("source_import")
    assert prov and prov["original_url"] == SHELL_URL
    assert prov["fetched_via"] == "browser"


# ---------------------------------------------------------------------------
# FREE-URL-FACTS-01 — a confirmed single-product URL whose page carries
# explicit `label : value` spec rows must surface Product Information
# through the ordinary free commit path — verbatim, source-traced, no model.
# ---------------------------------------------------------------------------

def _record(_app, folder: str) -> dict:
    p = _app["brand_root"] / "cache" / folder / "product.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _pairs(rec: dict) -> set:
    return {(f.get("label"), f.get("value"))
            for f in (rec.get("derived_facts") or {}).values()}


def test_url_import_exposes_source_facts_on_product(_app):
    """K5-shaped real-page text: import → stage → commit → derived_facts
    carry the page's own spec rows."""
    c = _app["client"]
    _out, folder = _import_and_commit(c, K5LIKE_URL)
    st = _wait_ready(c, folder)
    assert st["status"] == "ready", st

    rec = _record(_app, folder)
    pairs = _pairs(rec)
    assert pairs, "page with explicit spec rows produced empty Product Information"
    assert ("แบรนด์", "Lagenio K5") in pairs
    assert ("หน่วยประมวลผล", "W117") in pairs
    assert ("แบตเตอรี่", "570 mAh") in pairs
    assert ("มาตรฐานการกันน้ำ", "IP68") in pairs
    # every fact traces back to the stored source artifact
    for f in rec["derived_facts"].values():
        assert f.get("source_file") == "source_page.txt", f


def test_url_facts_generic_structure_not_page_specific(_app):
    """The unrelated English fixture with the same structure also yields
    facts — no K5/Thai/brand-specific rule involved."""
    c = _app["client"]
    _out, folder = _import_and_commit(c, GENSPEC_URL)
    st = _wait_ready(c, folder)
    assert st["status"] == "ready", st

    pairs = _pairs(_record(_app, folder))
    assert ("Brand", "Acme") in pairs
    assert ("Battery", "900mAh") in pairs
    assert ("Waterproof", "IP67") in pairs


def test_url_facts_free_path_invokes_no_model(_app):
    """The entire import→commit→facts chain is free: the supplied FakeLLM
    is a tripwire — it must see zero calls for this path."""
    llm = _app["llm_state"]["llm"]
    before = len(llm.calls)
    _out, folder = _import_and_commit(_app["client"], GENSPEC_URL)
    _wait_ready(_app["client"], folder)
    assert llm.calls[before:] == [], llm.calls[before:]


def test_related_products_never_become_facts(_app):
    """isRelatedTo recommendation names in the page's JSON-LD (Black
    Shark / KOSPET / KIESLECT) must not appear as facts of the primary
    product."""
    c = _app["client"]
    _out, folder = _import_and_commit(c, K5LIKE_URL)
    _wait_ready(c, folder)
    rec = _record(_app, folder)
    blob = " ".join(
        f"{f.get('label','')} {f.get('value','')}"
        for f in (rec.get("derived_facts") or {}).values())
    for related in ("Black Shark", "KOSPET", "KIESLECT"):
        assert related not in blob, f"related product leaked into facts: {related}"


def test_importer_provenance_lines_not_facts(_app):
    """Source URL / Final URL / Fetched / Title header lines are importer
    metadata — never Product Information."""
    c = _app["client"]
    _out, folder = _import_and_commit(c, GENSPEC_URL)
    _wait_ready(c, folder)
    facts = (_record(_app, folder).get("derived_facts") or {})
    for bad in ("source_url", "final_url", "fetched", "title",
                "description", "source"):
        assert bad not in facts, f"importer metadata became a fact: {bad}"
