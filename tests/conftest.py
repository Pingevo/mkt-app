"""Shared pytest fixtures — applied to every test automatically.

ป้องกันเทสต์ยิงข้อมูลเข้า AI Usage Hub จริงและเขียน local log จริง
โดย default — เทสต์ที่ต้องการทดสอบ Hub behavior จริงสามารถ override
``_read_hub_credentials`` เองได้ (override ทีหลังทำงานทับ)

เหตุผล: ก่อนหน้านี้เทสต์หลายตัว (เช่น test_llm_client.py) เรียก LLMClient.chat
ที่เรียก record_ai_usage โดยไม่ได้ isolate ทั้ง USAGE_LOG_PATH และ Hub credentials
ทำให้ข้อมูล test (model=test-model, source=test.merge, user=overridden_user)
หลุดเข้า Hub จริงและ local log จริง → ปนเปื้อนรายงานค่าใช้จ่าย production

ใช้ monkeypatch ``_read_hub_credentials`` โดยตรงแทนการลบ env var
เพราะโมดูลอื่น (asset_library, content_history, web_viewer) เรียก
``load_dotenv()`` เองใน import time ซึ่งอาจโหลด token จริงกลับมา
แม้ conftest จะลบ env var ไปแล้ว — patch ที่ function level แข็งแรงกว่า
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolate_ai_usage_hub(monkeypatch, tmp_path):
    """ป้องกันเทสต์ยิง Hub จริงและเขียน local log จริงโดย default.

    - patch ``_read_hub_credentials`` ให้คืน ``(None, None)`` เสมอ
      → ``record_ai_usage`` ข้าม Hub POST ไม่ว่า env จะถูกโหลดใหม่กี่ครั้ง
    - redirect ``usage_log_path`` ไป tmp_path → ไม่เขียนทับ logs/llm_usage.jsonl จริง
    - enable MKTAPP_DEV_AUTH=1 so existing tests can use username/password login
      (production login is System81-only; dev auth is disabled by default)

    เทสต์ที่ต้องการ Hub behavior จริง override โดย:
    ``monkeypatch.setattr(ai_usage, "_read_hub_credentials", lambda: (url, token))``
    หรือ ``mock.patch.object(ai_usage, "_read_hub_credentials", ...)``
    ทำงานทับ fixture นี้เพราะรันหลัง (ภายใน test body)
    """
    monkeypatch.setattr("src.ai_usage._read_hub_credentials", lambda: (None, None))
    monkeypatch.setenv("MKTAPP_DEV_AUTH", "1")

    log_path = tmp_path / "llm_usage.jsonl"
    monkeypatch.setattr("src.ai_usage.usage_log_path", lambda: log_path)
    monkeypatch.setattr("src.ai_usage.USAGE_LOG_PATH", log_path)


@pytest.fixture(autouse=True)
def _restore_gate_make_client(monkeypatch):
    """Restore openrouter_gateway._make_client and record_ai_usage after each test.

    Some test helpers (e.g. in test_llm_client.py) directly assign to
    openrouter_gateway._make_client to inject mock clients.  This fixture
    ensures the original is restored after each test so patches don't leak
    into subsequent tests.
    """
    from src import openrouter_gateway
    original_make_client = openrouter_gateway._make_client
    original_record = openrouter_gateway.record_ai_usage
    yield
    openrouter_gateway._make_client = original_make_client
    openrouter_gateway.record_ai_usage = original_record


@pytest.fixture(autouse=True)
def _reset_workspace_context():
    """Clear any leaked WorkspaceContext before AND after each test (MB-02 isolation).

    Tests that set a workspace via ``set_workspace`` should reset it in a
    finally block, but if they don't, the ContextVar leaks into subsequent
    tests and causes path-resolution cross-talk.  This fixture guarantees a
    clean slate both before the test runs and after it finishes — it never
    restores a leaked context.
    """
    from src.workspace_context import set_workspace
    # Clear before — discard any context leaked by a prior test.
    set_workspace(None)
    yield
    # Clear after — never restore a leaked context; leave a clean slate.
    set_workspace(None)


# ---------------------------------------------------------------------------
# Auth helper — creates an authenticated TestClient for API tests
# ---------------------------------------------------------------------------

def make_authed_client(app, tmp_path, monkeypatch=None):
    """Create an authenticated TestClient for the given FastAPI app.

    Registers a test user, creates a session, and sets the session cookie.

    Returns (client, user_id, workspace_root).
    """
    from starlette.testclient import TestClient
    from src.auth import UserStore, SessionManager, SESSION_COOKIE_NAME
    import src.auth as auth_mod

    _users_path = tmp_path / "data" / "auth" / "users.json"
    _sessions_path = tmp_path / "data" / "auth" / "sessions.json"
    _users_path.parent.mkdir(parents=True, exist_ok=True)
    _store = UserStore(_users_path)
    _sess = SessionManager(_sessions_path)
    if monkeypatch:
        monkeypatch.setattr(auth_mod, "_user_store", _store)
        monkeypatch.setattr(auth_mod, "_session_manager", _sess)
    else:
        auth_mod._user_store = _store
        auth_mod._session_manager = _sess
    user = _store.register("testuser", "testpass")
    token = _sess.create_session(user.user_id)

    # Create per-user workspace dirs
    ws_root = tmp_path / "users" / user.user_id
    (ws_root / "data").mkdir(parents=True, exist_ok=True)
    (ws_root / "cache").mkdir(parents=True, exist_ok=True)
    (ws_root / "output").mkdir(parents=True, exist_ok=True)
    (ws_root / "brand").mkdir(parents=True, exist_ok=True)

    client = TestClient(app)
    # Login via API to set the session cookie naturally (more robust than manual cookie set)
    resp = client.post("/api/auth/login", json={"username": "testuser", "password": "testpass"})
    assert resp.status_code == 200, f"login failed: {resp.status_code} {resp.text}"
    return client, user.user_id, ws_root


def make_brand_client(app, tmp_path, monkeypatch=None, brand_name="TestBrand"):
    """Create an authenticated TestClient with a brand created and selected.

    Extends ``make_authed_client`` by also creating a brand via the real
    Brand API and selecting it, so the ``mktapp_brand`` cookie is set and
    the auth middleware establishes a brand-scoped WorkspaceContext.

    Patches ``web_viewer.PROJECT_ROOT`` to ``tmp_path`` so the BrandRegistry
    creates and resolves brands under the test workspace, not the real
    project root.

    Returns (client, user_id, brand_id, brand_root).
    """
    import web_viewer
    client, user_id, ws_root = make_authed_client(app, tmp_path, monkeypatch)
    # Patch PROJECT_ROOT so BrandRegistry uses the test workspace
    if monkeypatch:
        monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    else:
        web_viewer.PROJECT_ROOT = tmp_path
    resp = client.post("/api/brands", json={"name": brand_name})
    assert resp.status_code == 200, f"brand create failed: {resp.status_code} {resp.text}"
    brand_id = resp.json()["brand_id"]
    resp = client.post(f"/api/brands/{brand_id}/select")
    assert resp.status_code == 200, f"brand select failed: {resp.status_code} {resp.text}"
    brand_root = ws_root / "brands" / brand_id
    for d in ("data", "cache", "output", "brand"):
        (brand_root / d).mkdir(parents=True, exist_ok=True)
    return client, user_id, brand_id, brand_root


# ---------------------------------------------------------------------------
# Brand workspace helper — for scheduler/state tests that need brand context
# ---------------------------------------------------------------------------

def make_brand_workspace(tmp_path, user_id="test_user", brand_name="TestBrand"):
    """Create a user + brand and return (user_id, brand_id, project_root).

    Registers the user in the UserStore so scheduler restart can enumerate them.
    Creates a brand via BrandRegistry.  Does NOT set the workspace context —
    the caller must set it via ``WorkspaceContext.for_brand(...)`` before
    accessing brand-scoped state.
    """
    from src.auth import UserStore
    from src.brand_registry import BrandRegistry

    users_path = tmp_path / "data" / "auth" / "users.json"
    users_path.parent.mkdir(parents=True, exist_ok=True)
    store = UserStore(users_path)
    user = store.register(user_id, f"pass_{user_id}")
    uid = user.user_id
    reg = BrandRegistry(user_id=uid, project_root=tmp_path)
    brand = reg.create(brand_name)
    return uid, brand["brand_id"], tmp_path


# ---------------------------------------------------------------------------
# Shared brand workspace fixture — for legacy unit/offline tests that call
# brand-scoped modules directly (product_db, ingestion, staging, etc.)
# ---------------------------------------------------------------------------

@pytest.fixture
def brand_ws(tmp_path):
    """Create an isolated user + brand and set WorkspaceContext for the test.

    MB-02: product/ingestion/staging/output modules resolve paths through
    ``brand_state_root()`` which requires an active brand context.  Legacy
    tests that previously assumed one project root == one implicit brand
    can use this fixture to get a real brand context without duplicating
    setup code.

    Yields a dict with: user_id, brand_id, project_root, brand_root.

    Tests that explicitly verify no-brand/fail-closed behavior must NOT
    use this fixture.
    """
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    uid, bid, project_root = make_brand_workspace(tmp_path)
    brand_root = project_root / "users" / uid / "brands" / bid
    # Create brand-scoped dirs
    for d in ("data", "cache", "output", "brand"):
        (brand_root / d).mkdir(parents=True, exist_ok=True)
    # Set workspace context
    ws = WorkspaceContext.for_brand(uid, bid, project_root)
    token = set_workspace(ws)
    try:
        yield {
            "user_id": uid,
            "brand_id": bid,
            "project_root": project_root,
            "brand_root": brand_root,
        }
    finally:
        reset_workspace(token)
