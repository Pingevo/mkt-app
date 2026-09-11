# DEVELOPER_LOG.md

## 2026-09-11 — Auth + Per-User Workspace Isolation

### What changed

Implemented minimal local/Beta authentication with complete per-user state isolation.

**New files:**
- `src/auth.py` — UserStore (bcrypt password hashing), SessionManager (server-side sessions with TTL, tamper detection, logout invalidation), get_current_user dependency.
- `src/workspace_context.py` — WorkspaceContext dataclass (per-user root under `users/{user_id}/`), context-var based workspace resolution, `user_state_root()` single helper for all state modules, path traversal protection.
- `tests/test_auth.py` — 15 tests: register, login, password hashing, session create/verify/logout, tampered session rejection, TTL, revoke all.
- `tests/test_workspace_context.py` — 16 tests: path traversal, cross-user isolation, two-user product/history isolation, new-user clean defaults, factory config integrity.

**Modified files:**
- `web_viewer.py` — Auth middleware (protects `/api/*` except `/api/auth/*`), login/register/logout/me endpoints, WorkspaceContext setup per request, path constants converted to callable resolvers.
- `src/local_workspace.py` — `local_root()` resolves through `user_state_root()`, archive directory timestamp collision fix.
- `src/product_db.py` — `_project_root()` resolves through `user_state_root()`.
- `src/content_history.py` — `_history_path()` resolves through `user_state_root()`.
- `src/run_resources.py` — `_resolve_storage_dir()` resolves through `user_state_root()`.
- `src/scheduler.py` — JobStore resolves paths through workspace, scheduler passes session cookie in httpx calls, rerun sets per-user workspace.
- `src/ai_usage.py` — `usage_log_path()` resolves through `user_state_root()`.
- `src/cost_summary.py` — Uses `usage_log_path()` from ai_usage.
- `src/staging.py` — Staging paths resolve through `user_state_root()`.
- `src/ingestion.py` — Ingestion paths resolve through `user_state_root()`.
- `tests/conftest.py` — Added `make_authed_client()` helper for authenticated test clients.
- `tests/test_browser_e2e.py` — Browser fixture authenticates via login API, cleanup restores all web_viewer globals to prevent cross-module leakage.
- `tests/test_browser_integration_real_orch.py` — Browser fixture authenticates, per-user workspace setup.
- `tests/test_browser_upload_real_source.py` — Browser fixture authenticates, session token propagation.
- `tests/test_schedule_api.py` — Resource store uses per-user workspace path resolution.
- `tests/test_scheduler_rerun.py` — Fixed race condition in rerun test (wait for executor inside patch context).
- `tests/test_competitor_search_probe.py` — Patch `usage_log_path()` instead of `USAGE_LOG_PATH` constant.

### Design decisions

- **Context-var based workspace resolution**: A single `contextvars.ContextVar` holds the current `WorkspaceContext`. The auth middleware sets it per-request. All state modules call `user_state_root()` which checks the context-var. This avoids changing every function signature.
- **Factory config stays global**: `config/agent_instructions.json`, `config/agents.yaml`, `config/content_policy.yaml`, `brand/` templates remain at the project root. Only user-derived state (products, cache, history, outputs, settings) moves to per-user workspaces.
- **Server-side sessions**: Sessions stored in JSON file with opaque high-entropy server-side tokens (`secrets.token_urlsafe(32)`) with TTL and server-side revocation. Logout invalidates the server-side record. Tampered/unknown tokens are rejected.
- **Scheduler ownership**: Scheduler jobs store `user_id`. When a job runs, it sets the per-user workspace context and passes the session cookie in httpx calls to the web server.

### Test results

- Auth tests: 15/15 pass
- Workspace context tests: 16/16 pass
- Non-browser suite: 93 pre-existing failures, 0 new failures
- Browser suite: 33 pre-existing failures, 0 new failures
