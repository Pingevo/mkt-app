"""URL import — fetch a public product page and normalize it into upload artifacts.

Thin adapter between a public product URL and the existing file-ingestion seam.
Produces the same ``[(filename, bytes)]`` payloads that ``/api/upload`` creates
from multipart files, plus provenance — nothing here writes to the workspace;
the caller feeds the result through ``product_db.save_uploaded_files``.

Safety contract (SSRF):
  - only http/https, no userinfo, no malformed URLs
  - hostname is resolved BEFORE every fetch; every resolved IP must be
    globally routable (loopback/private/link-local/multicast/reserved/
    unspecified all rejected via ipaddress.is_global)
  - redirects are followed manually with a bounded count and the same
    DNS/IP validation re-applied to each hop
  - bodies are streamed with hard byte caps — never buffered unboundedly
  - main page must be HTML/text; images must be image/{jpeg,png,webp}
    (the three types the ingestion pipeline classifies)

ponytail: DNS is validated before connect, but httpx re-resolves at connect
time — a fast-rebinding DNS attacker could theoretically swap IPs between the
two lookups. Fully closing that TOCTOU needs connect-by-IP + SNI plumbing;
accepted ceiling for v1 (same exposure class as the existing voice_learn
fetcher, minus every other hole).

Test seam: ``_make_client()`` is the internal adapter — tests inject an
httpx.MockTransport client there; ``socket.getaddrinfo`` is patched for DNS.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # optional — static fetch works without it
    sync_playwright = None

# --- Centralized limits (spec: keep constants in this module) ---------------
MAX_PAGE_BYTES = 2_000_000          # 2 MB HTML cap
MAX_IMAGE_BYTES = 5_000_000         # 5 MB per image
MAX_TOTAL_IMAGE_BYTES = 15_000_000  # 15 MB across all images
MAX_IMAGES = 8                      # max images downloaded per page
MAX_REDIRECTS = 5                   # bounded manual redirect chain
MAX_TEXT_CHARS = 60_000             # normalized text cap (not the old 5k)
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
USER_AGENT = "Mozilla/5.0 (compatible; MKTApp-ProductImport/1.0)"
MIN_BODY_CHARS = 240                # static page thinner than this → JS shell → browser
BROWSER_NAV_TIMEOUT_MS = 30_000     # playwright navigation timeout
BROWSER_IDLE_TIMEOUT_MS = 8_000     # best-effort networkidle wait
BROWSER_SETTLE_MS = 500             # post-idle beat so post-load scripts/workers fire

# Generic unusable-page markers (lexical requirement — the page CLASS is the
# signal, not product semantics): challenge/anti-bot, access denied, login
# wall.  Matched against title + extracted body text only, never the URL.
_UNUSABLE_MARKERS = (
    "access denied", "access forbidden", "403 forbidden",
    "verify you are human", "verify you're human",
    "are you a robot", "confirm you are human",
    "captcha", "checking your browser",
    "ddos protection", "security check",
    "just a moment", "attention required",
    "log in to continue", "sign in to continue",
)

# Test seam — callable(route) that fulfills allowed requests in place of
# route.continue_().  Production keeps None → real network after validation.
_BROWSER_FULFILL = None

# Injected before page scripts — removes the WebSocket constructor and every
# worker type whose network activity cannot be route-intercepted.
_BROWSER_INIT_SCRIPT = (
    "window.WebSocket = undefined;"
    "window.Worker = undefined;"
    "window.SharedWorker = undefined;"
)

_PAGE_MIMES = ("text/html", "application/xhtml", "text/plain")
_IMAGE_MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class UrlImportError(Exception):
    """Controlled fetch/validation failure — carries a user-safe message only.

    ``reason`` is a machine key for tests/logs; ``str(e)`` is the Thai message
    shown in the UI. Internal details (IPs, schemes, paths) never leak through
    the message — they stay in ``detail`` for server-side debugging.
    """

    _MESSAGES = {
        "invalid_url": "ลิงก์ไม่ถูกต้อง",
        "unsupported_scheme": "รองรับเฉพาะลิงก์ http/https เท่านั้น",
        "unreachable": "เข้าถึงลิงก์นี้ไม่ได้",
        "redirect_limit": "ลิงก์เปลี่ยนเส้นทางมากเกินไป",
        "http_error": "เซิร์ฟเวอร์ปลายทางตอบกลับผิดพลาด",
        "unsupported_content": "หน้านี้ไม่ใช่หน้าเว็บที่อ่านได้",
        "too_large": "เนื้อหาหน้าเว็บใหญ่เกินไป",
        "empty_page": "ไม่พบเนื้อหาสินค้าที่อ่านได้ในหน้านี้",
        "fetch_failed": "ดึงข้อมูลจากลิงก์ไม่สำเร็จ — ลองอีกครั้งหรือใช้ลิงก์อื่น",
    }

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(self._MESSAGES.get(reason, self._MESSAGES["fetch_failed"]))


# ---------------------------------------------------------------------------
# Internal network seams (private — patched by tests)
# ---------------------------------------------------------------------------

def _make_client() -> httpx.Client:
    return httpx.Client(
        timeout=HTTP_TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )


def _resolve_and_check(host: str, port: int) -> None:
    """Resolve host and require EVERY resolved IP to be globally routable."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UrlImportError("unreachable", f"DNS failed for {host}: {e}")
    ips = {info[4][0].split("%", 1)[0] for info in infos}
    if not ips:
        raise UrlImportError("unreachable", f"DNS returned no addresses for {host}")
    for raw in ips:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise UrlImportError("unreachable", f"unparseable resolved IP {raw!r}")
        if not ip.is_global or ip.is_multicast:
            raise UrlImportError("unreachable", f"{host} resolves to non-public {raw}")


def _validate_url(url: str) -> tuple[str, int]:
    """Validate scheme/host/userinfo, then DNS-check. Returns (host, port)."""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        raise UrlImportError("invalid_url", f"unparseable URL {url!r}")
    if parsed.scheme not in ("http", "https"):
        raise UrlImportError("unsupported_scheme", f"scheme {parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise UrlImportError("invalid_url", "URL contains userinfo")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise UrlImportError("invalid_url", f"bad host/port in {url!r}")
    if not host:
        raise UrlImportError("invalid_url", f"no host in {url!r}")
    port = port or (443 if parsed.scheme == "https" else 80)
    _resolve_and_check(host, port)
    return host, port


def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    """Stream a response body with a hard cap — never buffer unboundedly."""
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes(chunk_size=65536):
        total += len(chunk)
        if total > max_bytes:
            raise UrlImportError("too_large", f"body exceeded {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_document(client: httpx.Client, url: str, max_bytes: int,
                    allowed_mimes: tuple[str, ...] | set[str]) -> tuple[str, str, bytes]:
    """Fetch one document with a bounded, re-validated manual redirect chain.

    Returns (final_url, content_type, body_bytes).
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current)
        try:
            with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.status_code in _REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location:
                        raise UrlImportError("http_error", f"{resp.status_code} with no Location")
                    current = urljoin(current, location)
                    continue
                if resp.status_code != 200:
                    raise UrlImportError("http_error", f"{resp.status_code} for {current}")
                content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type not in allowed_mimes:
                    raise UrlImportError("unsupported_content",
                                         f"content-type {content_type!r} for {current}")
                body = _read_capped(resp, max_bytes)
                return current, content_type, body
        except UrlImportError:
            raise
        except httpx.HTTPError as e:
            raise UrlImportError("fetch_failed", f"{type(e).__name__}: {e}")
    raise UrlImportError("redirect_limit", f"more than {MAX_REDIRECTS} redirects")


# ---------------------------------------------------------------------------
# HTML normalization — stdlib only
# ---------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "li", "tr", "td", "th", "br",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "header",
    "footer", "nav", "main", "aside", "figure", "figcaption", "dl",
    "dt", "dd", "blockquote", "pre", "label", "option", "span",
}
_META_KEYS = {
    "description": "description",
    "og:title": "og_title",
    "og:description": "og_description",
    "og:image": "og_image",
    "og:url": "og_url",
    "twitter:title": "twitter_title",
    "twitter:description": "twitter_description",
    "twitter:image": "twitter_image",
}
_IMG_SRC_ATTRS = ("src", "data-src", "data-lazy-src", "data-original", "data-url")


class _PageExtractor(HTMLParser):
    """Pull title/meta/canonical/text/image candidates from initial HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.canonical = ""
        self.image_candidates: list[str] = []
        self.text_chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._in_head = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "head":
            self._in_head = True
        elif tag == "title":
            self._in_title = True
        elif tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").strip().lower()
            if key in _META_KEYS and a.get("content"):
                self.meta.setdefault(_META_KEYS[key], a["content"].strip())
        elif tag == "link" and "canonical" in (a.get("rel") or "").lower():
            if a.get("href"):
                self.canonical = a["href"].strip()
        elif tag == "img" and self._skip_depth == 0:
            for attr in _IMG_SRC_ATTRS:
                if a.get(attr):
                    self.image_candidates.append(a[attr].strip())
                    break
            else:
                srcset = a.get("srcset") or a.get("data-srcset")
                if srcset:
                    first = srcset.split(",")[0].strip().split(" ")[0]
                    if first:
                        self.image_candidates.append(first)
        if tag in _BLOCK_TAGS:
            self.text_chunks.append("\n")

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False
        elif tag == "title":
            self._in_title = False
        elif tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in _BLOCK_TAGS:
            self.text_chunks.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif self._skip_depth == 0 and not self._in_head:
            self.text_chunks.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())


def _normalized_text(ex: _PageExtractor, final_url: str) -> tuple[str, str]:
    """Build the source_page.txt payload — bounded, structured, readable.

    Returns (text, body_text) — body_text is the extracted body lines only
    (no header), so the caller can decide whether the page carried enough
    usable content or is a thin JS shell.
    """
    lines: list[str] = []
    if ex.title:
        lines.append(f"Title: {ex.title}")
    desc = ex.meta.get("og_description") or ex.meta.get("description") or ex.meta.get("twitter_description")
    if desc:
        lines.append(f"Description: {desc}")
    lines.append(f"Source: {final_url}")
    lines.append("")

    body: list[str] = []
    buf: list[str] = []
    for chunk in ex.text_chunks:
        if chunk == "\n":
            if buf:
                line = " ".join("".join(buf).split())
                if line:
                    body.append(line)
                buf = []
        else:
            buf.append(chunk)
    if buf:
        line = " ".join("".join(buf).split())
        if line:
            body.append(line)

    lines.extend(body)
    return "\n".join(lines)[:MAX_TEXT_CHARS], "\n".join(body)


def _is_unusable(title: str, body_text: str) -> bool:
    """Generic unusable-page detector — challenge/anti-bot/login-wall classes.

    Conservative: only fires on explicit challenge/denied phrasing in the
    title or extracted body, never on URL shape.  A long page can still be
    unusable (Cloudflare interstitials exceed MIN_BODY_CHARS).
    """
    hay = f"{title}\n{body_text}".lower()
    return any(m in hay for m in _UNUSABLE_MARKERS)


def _usable(ex: "_PageExtractor", body_text: str) -> bool:
    return len(body_text) >= MIN_BODY_CHARS and not _is_unusable(ex.title, body_text)


def _browser_fetch(url: str, *, blocked: list[str] | None = None) -> tuple[str, bytes]:
    """Render `url` in headless Chromium under the SAME public-network policy.

    Every request the page makes — document, redirect hops, frames/iframes,
    XHR/fetch, images, scripts, fonts — routes through ``_validate_url``, the
    identical scheme+DNS+IP gate as the static path, and is aborted when
    non-public.  ``window.WebSocket`` is neutered because route interception
    does not cover ws:// connections.

    Returns (final_url, rendered_html_bytes) — same contract the static path
    feeds to _PageExtractor, so downstream normalization is identical.

    Network isolation (Playwright 1.60):
      - ``route("**/*")`` validates every http/https request — document,
        frames, XHR/fetch, images, scripts — through ``_validate_url``.
      - ``service_workers="block"`` stops fetch-intercepting SW installs.
      - ``route_web_socket`` intercepts page/frame WebSockets — the handler
        never calls ``connect_to_server()``, so no socket ever opens.
        IMPORTANT: route() must be registered BEFORE route_web_socket —
        the reverse order deadlocks the sync dispatcher (verified 1.60).
      - Worker/SharedWorker are undefined via init script — worker-created
        WebSockets bypass route_web_socket entirely (verified 1.60), so the
        WS surface is closed by removing workers rather than intercepting
        inside them.  Trade-off: pages needing workers to render degrade to
        thin content → controlled error, not a security hole.
    """
    if sync_playwright is None:
        raise UrlImportError("fetch_failed", "playwright not installed")
    _validate_url(url)
    fulfill = _BROWSER_FULFILL
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=USER_AGENT,
                                          service_workers="block")
                ctx.add_init_script(_BROWSER_INIT_SCRIPT)

                def _guard(route):
                    rurl = route.request.url
                    try:
                        _validate_url(rurl)
                    except UrlImportError:
                        if blocked is not None:
                            blocked.append(rurl)
                        return route.abort()
                    if fulfill is not None:
                        return fulfill(route)
                    return route.continue_()

                ctx.route("**/*", _guard)

                def _ws_guard(ws):
                    # Never connect_to_server() → the socket never opens.
                    # ws:/wss: also fail _validate_url's http(s)-only check,
                    # so every intercepted ws is recorded as blocked.
                    try:
                        _validate_url(ws.url)
                    except UrlImportError:
                        if blocked is not None:
                            blocked.append(ws.url)

                ctx.route_web_socket(re.compile(r".*"), _ws_guard)
                page = ctx.new_page()
                resp = page.goto(url, wait_until="domcontentloaded",
                                 timeout=BROWSER_NAV_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle",
                                             timeout=BROWSER_IDLE_TIMEOUT_MS)
                except Exception:
                    pass  # best-effort — DOM may already be sufficient
                page.wait_for_timeout(BROWSER_SETTLE_MS)
                if resp is None:
                    raise UrlImportError("http_error", "no navigation response")
                if resp.status >= 400:
                    raise UrlImportError("http_error", f"browser got {resp.status}")
                return page.url, page.content().encode("utf-8")[:MAX_PAGE_BYTES]
            finally:
                browser.close()
    except UrlImportError:
        raise
    except Exception as e:
        raise UrlImportError("fetch_failed", f"browser: {type(e).__name__}: {e}")


def _image_candidates(ex: _PageExtractor, final_url: str) -> list[str]:
    """og:image/twitter:image first, then img candidates — deduped, resolved."""
    ordered: list[str] = []
    for key in ("og_image", "twitter_image"):
        if ex.meta.get(key):
            ordered.append(ex.meta[key])
    ordered.extend(ex.image_candidates)
    seen: set[str] = set()
    out: list[str] = []
    for raw in ordered:
        full = urljoin(final_url, raw)
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def fetch_product_page(url: str) -> dict:
    """Fetch a public product page → normalized upload artifacts.

    Returns:
        {
          "original_url": str,        # as submitted
          "final_url": str,           # after redirects / canonical
          "page_title": str,
          "text": str,                # normalized text (goes to source_page.txt)
          "images": [{"name": str, "content": bytes, "source_url": str}],
          "fetched_at": str,          # ISO-8601 UTC
        }

    Raises UrlImportError with a user-safe message on any rejection.
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlImportError("invalid_url", "empty URL")
    url = url.strip()

    def _parse(html_bytes: bytes, page_url: str) -> tuple[_PageExtractor, str, str]:
        ex = _PageExtractor()
        try:
            ex.feed(html_bytes.decode("utf-8", errors="replace"))
            ex.close()
        except Exception:
            # HTMLParser is forgiving; a pathological doc just yields less text
            pass
        text, body = _normalized_text(ex, page_url)
        return ex, text, body

    client = _make_client()
    fetched_via = "static"
    try:
        final_url, _ctype, html_bytes = _fetch_document(
            client, url, MAX_PAGE_BYTES, _PAGE_MIMES
        )

        ex, text, body_text = _parse(html_bytes, final_url)
        if not _usable(ex, body_text):
            # Thin initial HTML / JS shell / challenge page. Best-effort
            # browser fallback under the same URL/IP policy; failure surfaces
            # the controlled error, not silently thin or challenged content.
            fetched_via = "browser"
            final_url, html_bytes = _browser_fetch(url)
            ex, text, body_text = _parse(html_bytes, final_url)
            if not _usable(ex, body_text):
                raise UrlImportError(
                    "empty_page", f"insufficient usable content from {final_url}")


        canonical = ex.meta.get("og_url") or ex.canonical
        images: list[dict] = []
        total_image_bytes = 0
        for img_url in _image_candidates(ex, final_url)[:MAX_IMAGES]:
            if total_image_bytes >= MAX_TOTAL_IMAGE_BYTES:
                break
            try:
                _f, ctype, data = _fetch_document(
                    client, img_url, MAX_IMAGE_BYTES, set(_IMAGE_MIMES)
                )
            except UrlImportError:
                continue  # best-effort — one bad image never kills the import
            if total_image_bytes + len(data) > MAX_TOTAL_IMAGE_BYTES:
                continue
            images.append({
                "name": f"page_img_{len(images) + 1:03d}{_IMAGE_MIMES[ctype]}",
                "content": data,
                "source_url": img_url,
            })
            total_image_bytes += len(data)
    finally:
        try:
            client.close()
        except Exception:
            pass

    return {
        "original_url": url,
        "final_url": final_url,
        "canonical_url": (urljoin(final_url, canonical) if canonical else ""),
        "fetched_via": fetched_via,
        "page_title": ex.title,
        "og_title": ex.meta.get("og_title", ""),
        "text": text,
        "images": images,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
