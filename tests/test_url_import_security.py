"""SSRF / network-safety contract for src/url_import.py — NO live network.

All external HTTP goes through httpx.MockTransport injected via the module's
``_make_client`` seam; DNS goes through a ``socket.getaddrinfo`` wrapper that
maps test hostnames to chosen IPs and passes everything else through.

Proves:
  - only public http/https URLs are fetched
  - DNS-resolved IPs are validated on EVERY hop (incl. redirects)
  - bounded redirects, bounded downloads, MIME gates
  - optional image failures never destroy a usable text import
"""
from __future__ import annotations

import socket

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PUBLIC_IP = "93.184.216.34"  # example.com-range public IP for fake DNS


def _dns_map(mapping: dict[str, str]):
    """Return a getaddrinfo wrapper mapping host->IP; everything else passes
    through to the real resolver (literal IPs resolve without network)."""
    orig = socket.getaddrinfo

    def fake(host, port=0, *args, **kwargs):
        if host in mapping:
            ip = mapping[host]
            if ":" in ip:
                return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port or 443, 0, 0))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 443))]
        return orig(host, port, *args, **kwargs)

    return fake


def _client(routes) -> httpx.Client:
    """Build a MockTransport client.

    routes: {exact_url: httpx.Response | callable(request)->Response}
    or a raw handler callable(request)->Response for catch-all behavior.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if callable(routes):
            return routes(request)
        entry = routes.get(url)
        if entry is None:
            return httpx.Response(404, content=b"not found")
        return entry(request) if callable(entry) else entry

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client._test_calls = calls  # type: ignore[attr-defined]
    return client


def _patch(monkeypatch, client: httpx.Client, dns: dict[str, str] | None = None):
    """Patch url_import's network seams: client factory + DNS resolver.

    Also stubs ``_browser_fetch`` — this suite tests the fetch/validation
    layer; browser fallback lives in test_url_import_browser.py and no unit
    test here may launch Chromium or touch real network.
    """
    from src import url_import

    monkeypatch.setattr(url_import, "_make_client", lambda: client)
    monkeypatch.setattr(socket, "getaddrinfo", _dns_map(dns or {}))
    monkeypatch.setattr(
        url_import, "_browser_fetch",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("browser must not launch in this suite")),
    )


PRODUCT_HTML = b"""<!DOCTYPE html>
<html><head>
<title>ACME Turbo Blender 9000</title>
<meta name="description" content="The fastest blender in its class">
<meta property="og:title" content="ACME Turbo Blender 9000 Official">
<meta property="og:image" content="https://shop.example.com/img/blender.png">
<link rel="canonical" href="https://shop.example.com/products/acme-blender">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "@id":"https://shop.example.com/products/acme-blender",
 "name":"ACME Turbo Blender 9000",
 "image":"https://shop.example.com/img/blender.png"}
</script>
</head><body>
<h1>ACME Turbo Blender</h1>
<p>Price: 2,590 THB</p>
<p>Flagship kitchen blender with a 900W copper motor, BPA-free 1.5L jug,
six stainless-steel blades, three speed settings, and dishwasher-safe
parts. Ships nationwide within 2-4 business days.</p>
<ul><li>900W motor</li><li>TURBOBLEND-9000-MARKER</li></ul>
<script>var tracker=1;</script>
<style>.x{color:red}</style>
</body></html>"""

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def _page_routes(html: bytes = PRODUCT_HTML, **extra) -> dict:
    routes = {
        "https://shop.example.com/products/acme-blender": httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=html
        ),
        "https://shop.example.com/img/blender.png": httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        ),
    }
    routes.update(extra)
    return routes


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------

def test_https_public_url_accepted(monkeypatch):
    from src import url_import

    client = _client(_page_routes())
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    result = url_import.fetch_product_page("https://shop.example.com/products/acme-blender")
    assert "TURBOBLEND-9000-MARKER" in result["text"]
    assert result["final_url"] == "https://shop.example.com/products/acme-blender"
    assert result["page_title"]
    assert result["images"], "og:image should have been downloaded"
    assert result["fetched_at"]


def test_http_public_url_accepted(monkeypatch):
    from src import url_import

    html = PRODUCT_HTML.replace(
        b"https://shop.example.com/img/blender.png", b"/img/blender.png"
    )
    routes = {
        "http://shop.example.com/p": httpx.Response(
            200, headers={"content-type": "text/html"}, content=html
        ),
        "http://shop.example.com/img/blender.png": httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        ),
    }
    client = _client(routes)
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    result = url_import.fetch_product_page("http://shop.example.com/p")
    assert "TURBOBLEND-9000-MARKER" in result["text"]
    # relative og:image resolved against final URL
    assert result["images"]


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["not-a-url", "http://", "", "  ", "https:///path"])
def test_malformed_url_rejected(bad):
    from src import url_import

    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page(bad)


@pytest.mark.parametrize("scheme_url", [
    "ftp://shop.example.com/x",
    "file:///etc/passwd",
    "data:text/html;base64,PGI+",
    "gopher://shop.example.com/",
    "//shop.example.com/no-scheme",
])
def test_unsupported_scheme_rejected(scheme_url):
    from src import url_import

    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page(scheme_url)


def test_userinfo_rejected(monkeypatch):
    from src import url_import

    client = _client(_page_routes())
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page("https://user:pass@shop.example.com/products/acme-blender")
    assert not client._test_calls, "fetch must not run for userinfo URLs"


# ---------------------------------------------------------------------------
# SSRF — loopback / private / link-local / non-public
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,dns", [
    ("http://localhost/admin", {"localhost": "127.0.0.1"}),
    ("http://127.0.0.1/internal", {}),
    ("http://192.168.1.10/x", {}),
    ("http://10.0.0.9/x", {}),
    ("http://172.16.5.5/x", {}),
    ("http://169.254.169.254/latest/meta-data", {}),
    ("http://[::1]/x", {}),
    ("http://[fd00::5]/x", {}),
    ("http://[fe80::1]/x", {}),
    ("http://[ff02::1]/x", {}),
])
def test_non_public_targets_rejected(url, dns, monkeypatch):
    from src import url_import

    client = _client(_page_routes())
    _patch(monkeypatch, client, dns)
    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page(url)
    assert not client._test_calls, f"fetch must be blocked before connect: {url}"


def test_hostname_resolving_to_private_ip_rejected(monkeypatch):
    from src import url_import

    client = _client(_page_routes())
    _patch(monkeypatch, client, {"sneaky.example.com": "10.1.2.3"})
    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page("https://sneaky.example.com/products/acme-blender")
    assert not client._test_calls


def test_public_url_redirecting_to_private_rejected(monkeypatch):
    from src import url_import

    routes = {
        "https://shop.example.com/products/acme-blender": httpx.Response(
            302, headers={"location": "http://169.254.169.254/steal"}
        ),
    }
    client = _client(routes)
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page("https://shop.example.com/products/acme-blender")
    # first request happened; the redirect target must never be fetched
    assert client._test_calls == ["https://shop.example.com/products/acme-blender"]


def test_redirect_limit_enforced(monkeypatch):
    from src import url_import

    def hop(request: httpx.Request) -> httpx.Response:
        # every hop redirects to the next — infinite chain
        n = int(str(request.url).rsplit("/", 1)[1]) + 1
        return httpx.Response(302, headers={"location": f"https://shop.example.com/r/{n}"})

    client = _client(hop)
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})

    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page("https://shop.example.com/r/0")
    assert len(client._test_calls) == url_import.MAX_REDIRECTS + 1


def test_public_redirect_chain_accepted(monkeypatch):
    """One sane public redirect must work end-to-end."""
    from src import url_import

    routes = {
        "https://shop.example.com/old": httpx.Response(
            301, headers={"location": "https://cdn.example.com/products/acme-blender"}
        ),
        "https://cdn.example.com/products/acme-blender": httpx.Response(
            200, headers={"content-type": "text/html"}, content=PRODUCT_HTML
        ),
        "https://shop.example.com/img/blender.png": httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        ),
    }
    client = _client(routes)
    _patch(monkeypatch, client, {
        "shop.example.com": PUBLIC_IP, "cdn.example.com": "151.101.1.69",
    })
    result = url_import.fetch_product_page("https://shop.example.com/old")
    assert result["final_url"] == "https://cdn.example.com/products/acme-blender"
    assert "TURBOBLEND-9000-MARKER" in result["text"]


# ---------------------------------------------------------------------------
# Bounded downloads + MIME gates
# ---------------------------------------------------------------------------

def test_oversized_html_aborted(monkeypatch):
    from src import url_import

    big = b"<html><body>" + b"x" * (url_import.MAX_PAGE_BYTES + 100) + b"</body></html>"
    routes = {
        "https://shop.example.com/big": httpx.Response(
            200, headers={"content-type": "text/html"}, content=big
        ),
    }
    client = _client(routes)
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page("https://shop.example.com/big")


@pytest.mark.parametrize("ctype", ["application/octet-stream", "image/png",
                                   "application/pdf", "application/json"])
def test_non_html_main_mime_rejected(ctype, monkeypatch):
    from src import url_import

    routes = {
        "https://shop.example.com/bin": httpx.Response(
            200, headers={"content-type": ctype}, content=b"\x00" * 64
        ),
    }
    client = _client(routes)
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    with pytest.raises(url_import.UrlImportError):
        url_import.fetch_product_page("https://shop.example.com/bin")


def test_oversized_image_skipped_text_survives(monkeypatch):
    from src import url_import

    big_img = b"\x89PNG" + b"\x00" * (url_import.MAX_IMAGE_BYTES + 10)
    routes = _page_routes()
    routes["https://shop.example.com/img/blender.png"] = httpx.Response(
        200, headers={"content-type": "image/png"}, content=big_img
    )
    client = _client(routes)
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    result = url_import.fetch_product_page("https://shop.example.com/products/acme-blender")
    assert "TURBOBLEND-9000-MARKER" in result["text"]
    assert result["images"] == []


def test_non_image_image_candidate_rejected(monkeypatch):
    from src import url_import

    routes = _page_routes()
    routes["https://shop.example.com/img/blender.png"] = httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"<html>nope</html>"
    )
    client = _client(routes)
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    result = url_import.fetch_product_page("https://shop.example.com/products/acme-blender")
    assert result["images"] == []
    assert "TURBOBLEND-9000-MARKER" in result["text"]


def test_image_redirect_to_private_skipped_text_survives(monkeypatch):
    from src import url_import

    routes = _page_routes()
    routes["https://shop.example.com/img/blender.png"] = httpx.Response(
        302, headers={"location": "http://169.254.169.254/x.png"}
    )
    client = _client(routes)
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    result = url_import.fetch_product_page("https://shop.example.com/products/acme-blender")
    assert result["images"] == []
    assert "TURBOBLEND-9000-MARKER" in result["text"]


def test_image_fetch_failure_does_not_break_import(monkeypatch):
    from src import url_import

    routes = _page_routes()
    routes["https://shop.example.com/img/blender.png"] = httpx.Response(
        500, content=b"boom"
    )
    client = _client(routes)
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    result = url_import.fetch_product_page("https://shop.example.com/products/acme-blender")
    assert result["images"] == []
    assert "TURBOBLEND-9000-MARKER" in result["text"]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalization_strips_script_style_and_keeps_structure(monkeypatch):
    from src import url_import

    client = _client(_page_routes())
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    result = url_import.fetch_product_page("https://shop.example.com/products/acme-blender")
    text = result["text"]
    assert "tracker" not in text
    assert "color:red" not in text
    assert "ACME Turbo Blender" in text
    assert "2,590 THB" in text
    assert result["page_title"] == "ACME Turbo Blender 9000"


def test_relative_image_resolved_against_final_url(monkeypatch):
    from src import url_import

    html = PRODUCT_HTML.replace(
        b"https://shop.example.com/img/blender.png", b"/img/blender.png"
    )
    routes = _page_routes(html=html)
    client = _client(routes)
    _patch(monkeypatch, client, {"shop.example.com": PUBLIC_IP})
    result = url_import.fetch_product_page("https://shop.example.com/products/acme-blender")
    assert result["images"]
    assert result["images"][0]["source_url"] == "https://shop.example.com/img/blender.png"
    assert result["images"][0]["content"] == PNG_BYTES
