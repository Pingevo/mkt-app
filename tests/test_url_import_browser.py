"""Browser fallback for src/url_import.py — real Chromium, zero internet.

Static path stays first choice; a thin/JS-shell static page routes to the
headless-Chromium fallback.  Every browser request (navigation, redirect
hops, iframe, XHR/fetch, image/subresource) goes through the same
``_validate_url`` DNS+IP gate — private destinations are aborted, proven
here by fulfill-side spies and the ``blocked`` collection seam.

Test seams:
  - ``_make_client``      → MockTransport for static fetch + image downloads
  - ``_BROWSER_FULFILL``  → fulfills allowed browser requests with fixtures
  - ``blocked`` param     → collects URLs the guard aborted
  - ``socket.getaddrinfo``→ maps test hosts to chosen IPs (pass-through rest)
"""
from __future__ import annotations

import socket

import httpx
import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

PUBLIC_IP = "93.184.216.34"
PRODUCT_URL = "https://shop.example.com/products/acme-blender"
MARKER = "RENDERED-TURBOBLEND-MARKER"

JS_SHELL_HTML = b"""<!DOCTYPE html>
<html><head><title>Shop</title></head>
<body><div id="app"></div><script src="/bundle.js"></script></body></html>"""

RENDERED_HTML = f"""<!DOCTYPE html>
<html><head>
<title>ACME Turbo Blender 9000</title>
<meta property="og:title" content="ACME Turbo Blender 9000">
<meta property="og:image" content="https://shop.example.com/img/blender.png">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"Product","name":"ACME Turbo Blender 9000"}}</script>
</head><body>
<div id="app">
<h1>ACME Turbo Blender 9000</h1>
<p>Price: 2,590 THB — the fastest blender in its class, with a 900W motor,
BPA-free 1.5L jug, 6 stainless blades, 3-speed dial, and a 2-year warranty.
Ships nationwide in 2-4 days. {MARKER}</p>
<ul><li>900W motor</li><li>6 stainless blades</li><li>2-year warranty</li></ul>
</div>
</body></html>"""

STATIC_RICH_HTML = b"""<!DOCTYPE html>
<html><head><title>Static Product</title>
<script type="application/ld+json">{"@context":"https://schema.org",
"@type":"Product","name":"Static Product"}</script>
</head>
<body><h1>Static Product</h1>
<p>This page carries real product content in the initial HTML: full specs,
pricing, warranty terms, shipping options, and a feature list long enough
to look like an actual ecommerce product page rather than an app shell.
STATIC-RICH-MARKER appears here alongside model numbers and stock info.</p>
<ul><li>Spec one</li><li>Spec two</li><li>Spec three</li></ul>
</body></html>"""

# A client-rendered storefront's initial HTML: fat enough to pass
# MIN_BODY_CHARS on boilerplate alone, even carries og:type=product —
# but the JSON-LD Product only exists after the client render, so the
# static document cannot confirm a single product.  (Real shape observed
# on AliExpress item pages 2026-09: nav/footer shell, zero JSON-LD.)
FAT_SHELL_HTML = b"""<!DOCTYPE html>
<html><head><title>Shop</title>
<meta property="og:type" content="product">
<meta name="description" content="Smarter Shopping, Better Living!">
</head><body>
<nav>Help Center | Disputes &amp; Reports | Return &amp; refund policy |
IPR infringement report | Transparency center | Recall | Free returns</nav>
<footer>Multilingual site: Russian, Portuguese, Spanish, French, German,
Italian, Dutch, Turkish, Japanese, Korean, Thai, Vietnamese, Arabic,
Hebrew, Polish - browse by category, coupons, new user zone, help
center, disputes and reports, buyer protection.</footer>
</body></html>"""

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300


def _dns(host_map):
    orig = socket.getaddrinfo

    def fake(host, port=0, *a, **kw):
        if host in host_map:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (host_map[host], port or 443))]
        return orig(host, port, *a, **kw)

    return fake


def _static_client(html: bytes, extra: dict | None = None) -> httpx.Client:
    routes = {
        PRODUCT_URL: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=html
        ),
        "https://shop.example.com/img/blender.png": httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        ),
    }
    if extra:
        routes.update(extra)

    def handler(request: httpx.Request) -> httpx.Response:
        entry = routes.get(str(request.url))
        return entry if entry is not None else httpx.Response(404, content=b"nf")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _patch_base(monkeypatch, client, host_map=None):
    from src import url_import
    monkeypatch.setattr(url_import, "_make_client", lambda: client)
    monkeypatch.setattr(socket, "getaddrinfo",
                        _dns(host_map or {"shop.example.com": PUBLIC_IP}))


def _browser_fulfill(routes: dict):
    """Return a fulfill(route) serving fixture responses for allowed URLs."""
    def fulfill(route):
        url = route.request.url
        entry = routes.get(url)
        if entry is None:
            return route.abort()
        status, content_type, body = entry
        return route.fulfill(status=status, content_type=content_type, body=body)
    return fulfill


# ---------------------------------------------------------------------------
# Fallback decision
# ---------------------------------------------------------------------------

def test_usable_static_never_launches_browser(monkeypatch):
    from src import url_import

    called = []
    monkeypatch.setattr(url_import, "_browser_fetch",
                        lambda *a, **k: called.append(1) or ("", b""))
    client = _static_client(STATIC_RICH_HTML)
    _patch_base(monkeypatch, client)
    result = url_import.fetch_product_page(PRODUCT_URL)
    assert "STATIC-RICH-MARKER" in result["text"]
    assert result["fetched_via"] == "static"
    assert not called, "browser must not launch for sufficient static HTML"


def test_js_shell_falls_back_to_browser(monkeypatch):
    from src import url_import

    client = _static_client(JS_SHELL_HTML)
    _patch_base(monkeypatch, client)
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", RENDERED_HTML),
    }))
    result = url_import.fetch_product_page(PRODUCT_URL)
    assert result["fetched_via"] == "browser"
    assert MARKER in result["text"]
    assert "2,590 THB" in result["text"]
    assert result["page_title"] == "ACME Turbo Blender 9000"


def test_rendered_image_candidate_reaches_safe_downloader(monkeypatch):
    """og:image discovered in the rendered DOM is downloaded via the SAME
    safe httpx path (DNS+IP+MIME+size), not the browser."""
    from src import url_import

    client = _static_client(JS_SHELL_HTML)
    _patch_base(monkeypatch, client)
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", RENDERED_HTML),
    }))
    result = url_import.fetch_product_page(PRODUCT_URL)
    assert result["fetched_via"] == "browser"
    assert result["images"], "rendered og:image must reach the image downloader"
    assert result["images"][0]["content"] == PNG_BYTES


def test_browser_failure_is_controlled_error(monkeypatch):
    from src import url_import

    client = _static_client(JS_SHELL_HTML)
    _patch_base(monkeypatch, client)

    def boom(route):
        return route.abort()  # main document never fulfills

    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", boom)
    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page(PRODUCT_URL)


def test_thin_page_and_failed_browser_yields_error_not_thin_content(monkeypatch):
    """Static thin + browser thin → empty_page — never silent thin import."""
    from src import url_import

    client = _static_client(JS_SHELL_HTML)
    _patch_base(monkeypatch, client)
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html",
                      "<html><body><div id='app'></div></body></html>"),
    }))
    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page(PRODUCT_URL)


# ---------------------------------------------------------------------------
# Fat-shell fallback — usable boilerplate but ZERO structured product
# evidence is still an incomplete document (client-rendered JSON-LD)
# ---------------------------------------------------------------------------

def test_fat_shell_falls_back_to_rendered_product(monkeypatch):
    """Boilerplate shell ≥MIN_BODY_CHARS with no JSON-LD must still render —
    the real AliExpress shape.  Rendered doc carries Product → single."""
    from src import url_import

    client = _static_client(FAT_SHELL_HTML)
    _patch_base(monkeypatch, client)
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", RENDERED_HTML),
    }))
    result = url_import.fetch_product_page(PRODUCT_URL)
    assert result["fetched_via"] == "browser"
    assert result["page_class"] == "single"
    assert MARKER in result["text"]


def test_fat_shell_rendered_still_ambiguous_rejects(monkeypatch):
    """Render that still confirms nothing → ambiguous (fail closed)."""
    from src import url_import

    client = _static_client(FAT_SHELL_HTML)
    _patch_base(monkeypatch, client)
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", FAT_SHELL_HTML),
    }))
    result = url_import.fetch_product_page(PRODUCT_URL)
    assert result["page_class"] == "ambiguous"
    assert result["images"] == []  # rejected pages never download media


def test_fat_shell_browser_unusable_keeps_static_verdict(monkeypatch):
    """Static doc was readable; rendered doc is a challenge page → the
    static verdict (ambiguous) stands — no empty_page override."""
    from src import url_import

    client = _static_client(FAT_SHELL_HTML)
    _patch_base(monkeypatch, client)
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", CHALLENGE_HTML),
    }))
    result = url_import.fetch_product_page(PRODUCT_URL)
    assert result["fetched_via"] == "static"
    assert result["page_class"] == "ambiguous"


def test_fat_shell_browser_failure_keeps_static_verdict(monkeypatch):
    """Browser fetch error on a usable-but-unconfirmed page → the static
    ambiguous verdict surfaces, not a fetch error."""
    from src import url_import

    client = _static_client(FAT_SHELL_HTML)
    _patch_base(monkeypatch, client)
    monkeypatch.setattr(
        url_import, "_browser_fetch",
        lambda *a, **k: (_ for _ in ()).throw(
            url_import.UrlImportError("fetch_failed", "no chromium")))
    result = url_import.fetch_product_page(PRODUCT_URL)
    assert result["fetched_via"] == "static"
    assert result["page_class"] == "ambiguous"


# ---------------------------------------------------------------------------
# Browser SSRF — every request class routed through the same gate
# ---------------------------------------------------------------------------

def test_browser_navigation_to_private_blocked(monkeypatch):
    from src import url_import

    _patch_base(monkeypatch, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))))
    seen: list[str] = []
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL",
                        lambda route: seen.append(route.request.url) or route.fulfill(
                            status=200, content_type="text/html", body="<html></html>"))
    with pytest.raises(url_import.UrlImportError):
        url_import._browser_fetch("http://127.0.0.1/admin")
    assert not seen, "navigation must be blocked before any request"


def test_browser_redirect_to_private_blocked(monkeypatch):
    """A page-issued navigation to a private destination must be aborted.

    Meta refresh issues a real document request through the route guard —
    the same protection class as a server redirect hop (which Chromium also
    routes when the response comes from real network, not fulfill()).
    """
    from src import url_import

    _patch_base(monkeypatch, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))))
    blocked: list[str] = []
    page_html = (
        "<html><head>"
        "<meta http-equiv='refresh' content='0;url=http://169.254.169.254/x'>"
        "</head><body><h1>ok</h1></body></html>"
    )
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", page_html),
    }))
    url_import._browser_fetch(PRODUCT_URL, blocked=blocked)
    assert "http://169.254.169.254/x" in blocked


def test_browser_iframe_to_private_blocked(monkeypatch):
    from src import url_import

    _patch_base(monkeypatch, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))))
    blocked: list[str] = []
    page_html = (
        f"<html><body><h1>ok</h1>"
        f"<iframe src='http://169.254.169.254/frame'></iframe></body></html>"
    )
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", page_html),
    }))
    url_import._browser_fetch(PRODUCT_URL, blocked=blocked)
    assert "http://169.254.169.254/frame" in blocked


def test_browser_xhr_to_private_blocked(monkeypatch):
    from src import url_import

    _patch_base(monkeypatch, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))))
    blocked: list[str] = []
    page_html = (
        "<html><body><h1>ok</h1><script>"
        "fetch('http://10.0.0.9/secret').catch(()=>{});"
        "</script></body></html>"
    )
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", page_html),
    }))
    url_import._browser_fetch(PRODUCT_URL, blocked=blocked)
    assert "http://10.0.0.9/secret" in blocked


def test_browser_image_to_private_blocked(monkeypatch):
    from src import url_import

    _patch_base(monkeypatch, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))))
    blocked: list[str] = []
    page_html = (
        "<html><body><h1>ok</h1>"
        "<img src='http://192.168.1.10/track.png'></body></html>"
    )
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", page_html),
    }))
    url_import._browser_fetch(PRODUCT_URL, blocked=blocked)
    assert "http://192.168.1.10/track.png" in blocked


def test_browser_page_fetch_still_returns_rendered_html(monkeypatch):
    """Public subresources work — the guard blocks only non-public targets."""
    from src import url_import

    _patch_base(monkeypatch, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))))
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", RENDERED_HTML),
        "https://shop.example.com/app.js": (200, "application/javascript", "1;"),
    }))
    final_url, html = url_import._browser_fetch(PRODUCT_URL)
    assert MARKER in html.decode("utf-8", errors="replace")
    assert final_url == PRODUCT_URL


# ---------------------------------------------------------------------------
# Escape paths — service workers / WebSockets / workers (Playwright 1.60)
# ---------------------------------------------------------------------------

def test_service_workers_cannot_register_in_blocked_context():
    """service_workers='block' is honored by the installed Playwright —
    a page cannot install a fetch-intercepting SW that bypasses route()."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(service_workers="block")
        try:
            def fulfill(route):
                if route.request.url == "https://shop.example.com/x":
                    return route.fulfill(
                        status=200, content_type="text/html",
                        body=("<html><body><script>"
                              "navigator.serviceWorker.register('/sw.js')"
                              "  .catch(e=>{});"
                              "</script></body></html>"))
                return route.abort()
            ctx.route("**/*", fulfill)
            page = ctx.new_page()
            page.goto("https://shop.example.com/x")
            page.wait_for_timeout(500)
            regs = page.evaluate(
                "navigator.serviceWorker.getRegistrations().then(r=>r.length)")
            assert regs == 0, "service worker must not register in blocked context"
        finally:
            browser.close()


def test_browser_websocket_to_private_blocked(monkeypatch):
    """WS to a private destination — route_web_socket closes + records it."""
    from src import url_import

    _patch_base(monkeypatch, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))))
    blocked: list[str] = []
    page_html = (
        "<html><body><h1>ok</h1><script>"
        "try{new WebSocket('ws://169.254.169.254/x')}catch(e){}"
        "</script></body></html>"
    )
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", page_html),
    }))
    url_import._browser_fetch(PRODUCT_URL, blocked=blocked)
    assert "ws://169.254.169.254/x" in blocked


def test_browser_workers_disabled_by_init_script(monkeypatch):
    """Worker/SharedWorker/WebSocket constructors are removed — worker-created
    WebSockets bypass route_web_socket (verified on 1.60), so the WS surface
    is closed by removing workers entirely rather than intercepting them."""
    from src import url_import

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(service_workers="block")
        ctx.add_init_script(url_import._BROWSER_INIT_SCRIPT)
        try:
            ctx.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html",
                body="<html><body><h1>ok</h1></body></html>"))
            page = ctx.new_page()
            page.goto("https://shop.example.com/x")
            kinds = page.evaluate(
                "[typeof Worker, typeof SharedWorker, typeof WebSocket]")
            assert kinds == ["undefined", "undefined", "undefined"], (
                "worker + websocket constructors must be removed "
                "(worker WS is uninterceptable)")
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Challenge / unusable-page gate
# ---------------------------------------------------------------------------

CHALLENGE_HTML = """<!DOCTYPE html>
<html><head><title>Just a moment...</title></head>
<body>
<h1>Checking your browser before accessing the site</h1>
<p>This process is automatic. Your browser will redirect to your requested
content shortly. Please allow up to 5 seconds while we verify that the
connection is secure and that you are a real visitor.</p>
<p>Performance &amp; security by Cloudflare — DDoS protection and bot
mitigation for this website.</p>
</body></html>"""  # > MIN_BODY_CHARS of body text


def test_long_challenge_page_triggers_browser_fallback(monkeypatch):
    """A challenge page LONGER than MIN_BODY_CHARS still routes to browser."""
    from src import url_import

    client = _static_client(CHALLENGE_HTML.encode())
    _patch_base(monkeypatch, client)
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", RENDERED_HTML),
    }))
    result = url_import.fetch_product_page(PRODUCT_URL)
    assert result["fetched_via"] == "browser"
    assert MARKER in result["text"]


def test_long_rendered_challenge_is_rejected_not_imported(monkeypatch):
    """Rendered content that is still a challenge must NOT become a product."""
    from src import url_import

    client = _static_client(CHALLENGE_HTML.encode())
    _patch_base(monkeypatch, client)
    monkeypatch.setattr(url_import, "_BROWSER_FULFILL", _browser_fulfill({
        PRODUCT_URL: (200, "text/html", CHALLENGE_HTML),
    }))
    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page(PRODUCT_URL)


def test_genuine_long_product_page_stays_static(monkeypatch):
    """A real long product page is never flagged as a challenge."""
    from src import url_import

    assert not url_import._is_unusable(
        "Static Product", "This page carries real product content: specs, "
        "pricing, warranty, security features and shipping details.")
    client = _static_client(STATIC_RICH_HTML)
    _patch_base(monkeypatch, client)
    result = url_import.fetch_product_page(PRODUCT_URL)
    assert result["fetched_via"] == "static"
    assert "STATIC-RICH-MARKER" in result["text"]


def test_unusable_detector_unit():
    from src import url_import

    assert url_import._is_unusable("Just a moment...", "checking your browser")
    assert url_import._is_unusable("", "please verify you are human")
    assert url_import._is_unusable("Error 403 Forbidden", "")
    assert url_import._is_unusable("", "You must log in to continue")
    assert not url_import._is_unusable(
        "ACME Blender", "900W motor, warranty included")
