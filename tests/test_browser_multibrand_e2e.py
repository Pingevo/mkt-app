"""True browser E2E — multi-brand account flow through the real UI (MB-UI).

Proves via real DOM controls on a real uvicorn + Chromium stack:

  U1 (session cookie) → no active brand → blocking brand picker appears
  → create Brand A1 through the UI → A1 active, workspace opens
  → create Brand A2 through the UI → both brands listed → A2 active
  → switch A1 → A2 → A1 through the UI → workspace shows only the
    active brand's data each time
  → reload preserves the selected brand
  → a browser context with a valid session but no mktapp_brand cookie
    sees the picker with U1's existing brands and can select one

Rules honored:
  - Session-cookie injection only (System81 auth already qualified).
  - Brand create/select/switch ALWAYS go through real DOM controls —
    never via /api/brands from the fixture, never by setting the
    mktapp_brand cookie, never by setting WorkspaceContext for browser
    requests.
  - Test data under users/<uid>/brands/<bid>/ is seeded directly on the
    filesystem (accepted prep); brand identity itself is only ever
    created/selected through the UI.
  - No LLM/media/provider/web calls.  The OpenRouter key lookup is
    stubbed so /api/credits returns its no-key 500 without any network
    call.
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.request

import pytest

# Skip all tests if Playwright is not installed
playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Server fixture — real uvicorn against an isolated tmp project root
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def _mb_server(tmp_path_factory):
    """Start a real uvicorn server against a tmp project root.

    No Orchestrator/media mocks are needed: this module never runs agents.
    The OpenRouter key lookup is stubbed so /api/credits fails fast (500)
    instead of making an external network call.
    """
    import web_viewer
    import src.auth as auth_mod
    import src.openrouter_gateway as gateway
    from src.auth import UserStore, SessionManager

    tmp = tmp_path_factory.mktemp("multibrand_e2e")

    _orig_wv = {
        "PROJECT_ROOT": web_viewer.PROJECT_ROOT,
        "_scheduler_instance": getattr(web_viewer, "_scheduler_instance", None),
    }
    _orig_auth = {
        "_user_store": getattr(auth_mod, "_user_store", None),
        "_session_manager": getattr(auth_mod, "_session_manager", None),
    }
    _orig_get_api_key = gateway.get_api_key

    # Auth storage under tmp; register the test user.
    users_path = tmp / "data" / "auth" / "users.json"
    sessions_path = tmp / "data" / "auth" / "sessions.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    sess = SessionManager(sessions_path)
    auth_mod._user_store = store
    auth_mod._session_manager = sess
    user = store.register("mb_user", "mb_pass")

    web_viewer.PROJECT_ROOT = tmp
    web_viewer._scheduler_instance = None  # fresh scheduler bound to tmp root

    # Prevent the /api/credits OpenRouter network call — no paid/network calls.
    gateway.get_api_key = lambda: ""

    port = _find_free_port()
    import uvicorn

    config = uvicorn.Config(web_viewer.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("Server did not start")

    token = sess.create_session(user.user_id)

    yield {
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "tmp": tmp,
        "session_token": token,
        "user_id": user.user_id,
    }

    server.should_exit = True
    thread.join(timeout=5)
    sched = getattr(web_viewer, "_scheduler_instance", None)
    if sched is not None and sched is not _orig_wv["_scheduler_instance"]:
        try:
            sched.stop()
        except Exception:
            pass
    for name, val in _orig_wv.items():
        setattr(web_viewer, name, val)
    for name, val in _orig_auth.items():
        setattr(auth_mod, name, val)
    gateway.get_api_key = _orig_get_api_key


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def _launch(pw, server: dict) -> dict:
    """Launch Chromium with ONLY the session cookie (no mktapp_brand)."""
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_cookies([{
        "name": "mktapp_session",
        "value": server["session_token"],
        "url": server["url"],
    }])
    page = context.new_page()

    console_errors: list = []
    page.on("console", lambda m: console_errors.append(m) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(e))

    http_responses: list = []
    def _record(resp):
        try:
            http_responses.append({"url": resp.url, "status": resp.status})
        except Exception:
            pass
    page.on("response", _record)

    return {
        "browser": browser,
        "context": context,
        "page": page,
        "console_errors": console_errors,
        "http_responses": http_responses,
    }


def _close(env: dict) -> None:
    try:
        env["context"].close()
    except Exception:
        pass
    try:
        env["browser"].close()
    except Exception:
        pass


def _seed_product(server: dict, brand_id: str, product_name: str) -> None:
    """Seed one product folder under the brand's data root (filesystem prep
    only — brand identity itself was created through the real UI)."""
    d = (
        server["tmp"] / "users" / server["user_id"]
        / "brands" / brand_id / "data" / product_name
    )
    d.mkdir(parents=True, exist_ok=True)
    (d / "info.txt").write_text(
        f"seed product data for {product_name}", encoding="utf-8"
    )


def _browser_active_brand_id(env: dict) -> str:
    """Read the mktapp_brand cookie the browser actually holds — proves the
    cookie was set by the real UI select call, not by the test."""
    cookies = env["context"].cookies()
    bid = next((c["value"] for c in cookies if c["name"] == "mktapp_brand"), None)
    assert bid, "mktapp_brand cookie must be present after UI select"
    return bid


def _create_brand_ui(page, name: str) -> None:
    """Create a brand through the picker DOM; the page reloads on success."""
    page.fill("#brand-gate-name", name)
    with page.expect_navigation(wait_until="load", timeout=15000):
        page.click("#brand-gate-create")
    page.wait_for_load_state("networkidle")


def _select_brand_ui(page, brand_id: str) -> None:
    """Click the select button on a brand row in the picker; reloads."""
    row = page.wait_for_selector(
        f".brand-gate-item[data-brand-id='{brand_id}']", timeout=10000
    )
    assert row is not None, f"brand row {brand_id} not listed in picker"
    btn = row.query_selector(".brand-gate-select-btn")
    assert btn is not None, "brand row must have a select button"
    with page.expect_navigation(wait_until="load", timeout=15000):
        btn.click()
    page.wait_for_load_state("networkidle")


def _open_picker(page) -> None:
    page.click("#brand-switcher-btn")
    page.wait_for_selector("#brand-gate-overlay.visible", timeout=10000)


def _wait_workspace_ready(page, timeout: int = 15000) -> None:
    """Brand gate closed + brand switcher visible = workspace is live."""
    page.wait_for_selector("#brand-switcher-btn", state="visible", timeout=timeout)


def _sidebar_folder_names(page) -> list[str]:
    return [
        el.get_attribute("data-folder")
        for el in page.query_selector_all("#sidebar-content .folder-item")
    ]


def _assert_no_fatal_console_errors(console_errors: list) -> None:
    """Allow only the two expected messages:
    - generic 403s while no brand is selected (data_folders / schedule status)
    - the /api/credits 500 when no OpenRouter key is configured (stubbed)
    """
    fatal = []
    for e in console_errors:
        text = str(e)
        if any(skip in text for skip in ["favicon", "404", "CSS", "Deprecation"]):
            continue
        if "403" in text or "500" in text:
            continue
        fatal.append(text)
    assert not fatal, f"Fatal console errors: {fatal}"


def _assert_no_auth_or_brand_4xx(http_responses: list) -> None:
    bad = [r for r in http_responses if r["status"] in (401, 403)]
    assert not bad, f"unexpected 401/403 after brand active: {bad}"


# ---------------------------------------------------------------------------
# The acceptance flow
# ---------------------------------------------------------------------------

class TestMultiBrandUI:
    """U1 manages multiple brands entirely through the real UI."""

    def test_create_switch_isolation_and_reload(self, _mb_server):
        server = _mb_server
        with sync_playwright() as p:
            env = _launch(p, server)
            page = env["page"]
            try:
                page.goto(f"{server['url']}/", wait_until="networkidle", timeout=15000)

                # -- 1. No active brand → blocking picker, not silent workspace --
                gate = page.wait_for_selector(
                    "#brand-gate-overlay.visible", timeout=10000
                )
                assert gate is not None, "brand picker must appear when no brand is active"
                assert "blocking" in (gate.get_attribute("class") or ""), \
                    "picker must be blocking when no brand is active"
                close_btn = page.query_selector("#brand-gate-close")
                assert close_btn is None or not close_btn.is_visible(), \
                    "blocking picker must not offer a dismiss control"
                assert page.query_selector("#brand-gate-list") is not None

                # -- 2. Create A1 through the DOM → active → workspace opens --
                _create_brand_ui(page, "AlphaBrand")
                _wait_workspace_ready(page)
                assert "AlphaBrand" in page.inner_text("#brand-switcher-btn")
                assert page.query_selector("#brand-gate-overlay.visible") is None

                a1 = _browser_active_brand_id(env)
                _seed_product(server, a1, "ProductA1")
                page.reload()
                page.wait_for_load_state("networkidle")
                page.wait_for_selector(
                    ".folder-item[data-folder='ProductA1']", timeout=10000
                )
                assert "ProductA1" in _sidebar_folder_names(page)

                # -- 3. Switcher → create A2 through DOM → both listed → A2 active --
                _open_picker(page)
                page.wait_for_selector(
                    f".brand-gate-item[data-brand-id='{a1}']", timeout=10000
                )
                _create_brand_ui(page, "BetaBrand")
                _wait_workspace_ready(page)
                assert "BetaBrand" in page.inner_text("#brand-switcher-btn")
                a2 = _browser_active_brand_id(env)
                assert a2 != a1
                _seed_product(server, a2, "ProductA2")

                # Both brands visible in the picker for the same account.
                _open_picker(page)
                page.wait_for_selector(
                    f".brand-gate-item[data-brand-id='{a1}']", timeout=10000
                )
                page.wait_for_selector(
                    f".brand-gate-item[data-brand-id='{a2}']", timeout=10000
                )
                # Non-blocking picker may be dismissed.
                page.click("#brand-gate-close")
                page.wait_for_selector(
                    "#brand-gate-overlay", state="hidden", timeout=5000
                )

                # -- 4. Isolation: only the active brand's data is shown --
                env["http_responses"].clear()
                page.reload()
                page.wait_for_load_state("networkidle")
                page.wait_for_selector(
                    ".folder-item[data-folder='ProductA2']", timeout=10000
                )
                names = _sidebar_folder_names(page)
                assert "ProductA2" in names
                assert "ProductA1" not in names, "A1 product must not show under A2"

                # Switch A2 → A1 through the picker.
                _open_picker(page)
                _select_brand_ui(page, a1)
                _wait_workspace_ready(page)
                assert "AlphaBrand" in page.inner_text("#brand-switcher-btn")
                page.wait_for_selector(
                    ".folder-item[data-folder='ProductA1']", timeout=10000
                )
                names = _sidebar_folder_names(page)
                assert "ProductA1" in names
                assert "ProductA2" not in names, "A2 product must not show under A1"

                # Explicit UI switch A1 → A2 → only A2 data.
                _open_picker(page)
                _select_brand_ui(page, a2)
                _wait_workspace_ready(page)
                assert "BetaBrand" in page.inner_text("#brand-switcher-btn")
                page.wait_for_selector(
                    ".folder-item[data-folder='ProductA2']", timeout=10000
                )
                names = _sidebar_folder_names(page)
                assert "ProductA2" in names
                assert "ProductA1" not in names

                # Switch back A2 → A1 → A1 data returns.
                _open_picker(page)
                _select_brand_ui(page, a1)
                _wait_workspace_ready(page)
                page.wait_for_selector(
                    ".folder-item[data-folder='ProductA1']", timeout=10000
                )
                names = _sidebar_folder_names(page)
                assert "ProductA1" in names
                assert "ProductA2" not in names

                # -- 5. Reload persistence: A1 stays active, A1 data remains --
                env["http_responses"].clear()
                page.reload()
                page.wait_for_load_state("networkidle")
                _wait_workspace_ready(page)
                assert "AlphaBrand" in page.inner_text("#brand-switcher-btn")
                assert page.query_selector(
                    ".folder-item[data-folder='ProductA1']"
                ) is not None, "A1 workspace data must survive reload"

                _assert_no_auth_or_brand_4xx(env["http_responses"])
                _assert_no_fatal_console_errors(env["console_errors"])
            finally:
                _close(env)

    def test_existing_brands_listed_without_brand_cookie(self, _mb_server):
        """A session without mktapp_brand sees existing brands in the picker
        and can open one through the DOM."""
        server = _mb_server
        with sync_playwright() as p:
            # Context A: session only → create a brand through the real DOM.
            env_a = _launch(p, server)
            page_a = env_a["page"]
            try:
                page_a.goto(
                    f"{server['url']}/", wait_until="networkidle", timeout=15000
                )
                page_a.wait_for_selector(
                    "#brand-gate-overlay.visible", timeout=10000
                )
                _create_brand_ui(page_a, "GammaBrand")
                _wait_workspace_ready(page_a)
                assert "GammaBrand" in page_a.inner_text("#brand-switcher-btn")
                gamma_id = _browser_active_brand_id(env_a)
                _seed_product(server, gamma_id, "ProductGamma")
            finally:
                # Closing the context drops its mktapp_brand cookie.
                _close(env_a)

            # Context B: fresh browser state — session cookie ONLY.
            env_b = _launch(p, server)
            page_b = env_b["page"]
            try:
                page_b.goto(
                    f"{server['url']}/", wait_until="networkidle", timeout=15000
                )

                gate = page_b.wait_for_selector(
                    "#brand-gate-overlay.visible", timeout=10000
                )
                assert gate is not None, "picker must appear without a brand cookie"

                # U1's existing brands are listed — including the one just
                # created through the DOM in context A.
                row = page_b.wait_for_selector(
                    f".brand-gate-item[data-brand-id='{gamma_id}']",
                    timeout=10000,
                )
                assert row is not None, "existing brand must be listed for selection"
                assert "GammaBrand" in row.inner_text()

                env_b["http_responses"].clear()
                # Select it through the DOM → workspace opens.
                _select_brand_ui(page_b, gamma_id)
                _wait_workspace_ready(page_b)
                assert "GammaBrand" in page_b.inner_text("#brand-switcher-btn")
                assert page_b.query_selector(
                    "#brand-gate-overlay.visible"
                ) is None, "picker must close once a brand is active"
                page_b.wait_for_selector(
                    ".folder-item[data-folder='ProductGamma']", timeout=10000
                )

                _assert_no_auth_or_brand_4xx(env_b["http_responses"])
                _assert_no_fatal_console_errors(env_b["console_errors"])
            finally:
                _close(env_b)
