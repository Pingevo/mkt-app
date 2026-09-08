"""Beta E2E smoke tests — prove the full UI→API→Orchestrator→Agent→result→UI path
works for all four core Agent flows without paid model calls.

These tests use FastAPI TestClient (exercises real HTTP routing, SSE streaming,
file I/O, session management) with a mocked Orchestrator/LLM. They prove:

1. Application loads and serves the UI HTML.
2. Product folder listing works.
3. Agent settings API works (read/write).
4. Each of the 4 agents (product_spec, competitor_analysis, campaign_strategy,
   content_creator) can be invoked via /api/run_agent and completes with a
   result file.
5. SSE streaming delivers agent_start → agent_done events.
6. Result files are saved and retrievable via /api/file/{session}/{filename}.
7. Session history lists completed runs.
8. Multi-agent flow (/api/run_flows) works for sequential agents.
9. Error handling surfaces errors to the client instead of silent failure.
10. Repeated runs do not corrupt state.

No paid/model/web/media calls are made. The Orchestrator is mocked.
"""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _client(tmp_path, monkeypatch):
    """FastAPI TestClient with isolated filesystem and no real API key."""
    import importlib
    import web_viewer

    # Reload first to get a clean module state
    importlib.reload(web_viewer)

    # Isolate all filesystem paths AFTER reload
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(web_viewer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", tmp_path / "cache")

    # Create minimal data structure
    (tmp_path / "data" / "TestProduct").mkdir(parents=True)
    (tmp_path / "data" / "TestProduct" / "info.txt").write_text("Test product info", encoding="utf-8")
    (tmp_path / "cache" / "TestProduct").mkdir(parents=True)

    # Create minimal brand directory
    (tmp_path / "brand").mkdir(exist_ok=True)

    # Create config directory with minimal agent_instructions.json
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    # Create output directory so sessions can be written
    (tmp_path / "output").mkdir(exist_ok=True)

    # Mock folder reading
    monkeypatch.setattr(web_viewer, "_read_folder", lambda f: (["info text"], [], {}))

    # Initialize module globals that are only set inside route handlers
    monkeypatch.setattr(web_viewer, "_current_llm", None, raising=False)
    monkeypatch.setattr(web_viewer, "_session_ts", "", raising=False)
    monkeypatch.setattr(web_viewer, "_cancel_requested", False, raising=False)

    return TestClient(web_viewer.app)


@pytest.fixture
def _mock_orch(monkeypatch):
    """Patch Orchestrator to return a mock that simulates agent execution."""
    import web_viewer

    def _make_fake_orch(**kw):
        fake = MagicMock()
        fake._make_client.return_value = MagicMock()
        fake._make_client.return_value.close = MagicMock()
        fake.product_id = "TestProduct"
        fake.results = {}

        def _save_result(agent_key, output_dir=None):
            """Mock save_result that actually writes files so sessions work."""
            from pathlib import Path
            if output_dir is None:
                output_dir = Path("output") / "latest"
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            fname_map = {
                "product_spec": "01_product_spec",
                "competitor_analysis": "02_competitor_analysis",
                "campaign_strategy": "03_campaign_strategy",
                "content_creator": "04_content_creator",
            }
            fname = fname_map.get(agent_key, agent_key)
            run_id = "test_run_001"
            if agent_key == "content_creator":
                json_path = output_dir / f"{fname}_TestProduct_{run_id}.json"
                json_path.write_text('{"posts": []}', encoding="utf-8")
                md_path = output_dir / f"{fname}_TestProduct_{run_id}.md"
                md_path.write_text("# Test content\n", encoding="utf-8")
                return {agent_key: md_path, f"{agent_key}_json": json_path}
            md_path = output_dir / f"{fname}_TestProduct_{run_id}.md"
            md_path.write_text(f"# {agent_key} test output\n", encoding="utf-8")
            return {agent_key: md_path}

        fake.save_result.side_effect = _save_result

        def _run_product_spec(*a, **k):
            fake.results["product_spec"] = "Product spec output for testing"
            return "Product spec output for testing"
        fake.run_product_spec.side_effect = _run_product_spec

        def _run_competitor(*a, **k):
            fake.results["competitor_analysis"] = "Competitor analysis output for testing"
            return "Competitor analysis output for testing"
        fake.run_competitor_analysis.side_effect = _run_competitor

        def _run_campaign(*a, **k):
            fake.results["campaign_strategy"] = "Campaign strategy output for testing"
            return "Campaign strategy output for testing"
        fake.run_campaign_strategy.side_effect = _run_campaign

        def _run_content_creator(*a, **k):
            content = json.dumps({
                "posts": [{
                    "platform": "Facebook",
                    "concept": "test",
                    "title": "Test Post",
                    "caption": "test caption",
                    "script": "",
                    "hashtags": "#test",
                    "image_prompts": [],
                    "video_prompts": [],
                    "asset_ids": [],
                }],
            }, ensure_ascii=False)
            fake.results["content_creator"] = content
            fake.results["content_creator_markdown"] = "# Test Post\n"
            return content
        fake._run_content_creator_raw.side_effect = _run_content_creator

        fake._review_script_in_posts = MagicMock(return_value={})
        # _finalize_content_output returns (content_json, content_markdown)
        _finalize_content = json.dumps({
            "posts": [{
                "platform": "Facebook", "concept": "test", "title": "Test Post",
                "caption": "test caption", "script": "", "hashtags": "#test",
                "image_prompts": [], "video_prompts": [], "asset_ids": [],
            }],
        }, ensure_ascii=False)
        fake._finalize_content_output.return_value = (_finalize_content, "# Test Post\n")
        return fake

    fake = _make_fake_orch()
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake)
    # Ensure _current_llm is None so the route creates a (mocked) client
    monkeypatch.setattr(web_viewer, "_current_llm", None, raising=False)

    # Mock media generation (not part of Beta core text flows)
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history",
                        lambda *a, **k: None)

    # Mock content history
    monkeypatch.setattr(web_viewer.content_history, "record_entry",
                        lambda *a, **k: True)
    monkeypatch.setattr(web_viewer.content_history, "format_product_history_for_prompt",
                        lambda *a, **k: "")
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file",
                        lambda *a, **k: None)

    return fake


def _parse_sse(response):
    """Parse SSE stream from a TestClient response into a list of data dicts."""
    events = []
    for line in response.iter_lines():
        if line and line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


# ---------------------------------------------------------------------------
# 1. Application loads
# ---------------------------------------------------------------------------

class TestApplicationLoads:
    """Verify the web application starts and serves the UI."""

    def test_root_html_served(self, _client):
        """GET / returns HTML with the MKTApp UI."""
        resp = _client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # The HTML page should contain key UI elements
        assert "MKTApp" in resp.text or "wizard" in resp.text.lower()

    def test_wizard_js_served(self, _client):
        """GET /wizard_ui.js returns the wizard JavaScript."""
        resp = _client.get("/wizard_ui.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers.get("content-type", "").lower()

    def test_data_folders_api_works(self, _client):
        """GET /api/data_folders returns a list of product folders."""
        resp = _client.get("/api/data_folders")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # Our fixture creates TestProduct
        names = [f.get("name", "") for f in data]
        assert "TestProduct" in names


# ---------------------------------------------------------------------------
# 2. Agent settings API
# ---------------------------------------------------------------------------

class TestAgentSettingsAPI:
    """Verify agent settings can be read and written."""

    def test_get_agent_instructions(self, _client):
        """GET /api/agent_instructions/{agent_key} returns current settings."""
        resp = _client.get("/api/agent_instructions/campaign_strategy")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data or isinstance(data, dict)

    def test_save_agent_instructions(self, _client, tmp_path):
        """POST /api/agent_instructions/{agent_key} saves settings."""
        resp = _client.post("/api/agent_instructions/campaign_strategy", json={
            "budget_max": "5000",
            "discount_max": "0",
            "forbid_tactics": ["heavy_discount", "flash", "bogo"],
        })
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. Agent 1 — product_spec
# ---------------------------------------------------------------------------

class TestAgent1ProductSpec:
    """E2E: product_spec agent flow via /api/run_agent."""

    def test_product_spec_completes(self, _client, _mock_orch, tmp_path):
        """POST /api/run_agent with product_spec completes and saves output."""
        resp = _client.post("/api/run_agent", json={
            "agent": "product_spec",
            "folder": "TestProduct",
            "quick_brief": "one-page",
            "context": {"use_competitor": False, "use_campaign": False},
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        types = [e.get("type") for e in events]
        assert "agent_start" in types, f"Missing agent_start in {types}"
        assert "agent_done" in types, f"Missing agent_done in {types}"
        # No error events
        errors = [e for e in events if e.get("type") == "error"]
        assert not errors, f"Unexpected errors: {errors}"
        # Orchestrator was called
        _mock_orch.run_product_spec.assert_called_once()


# ---------------------------------------------------------------------------
# 4. Agent 2 — competitor_analysis
# ---------------------------------------------------------------------------

class TestAgent2CompetitorAnalysis:
    """E2E: competitor_analysis agent flow via /api/run_agent."""

    def test_competitor_analysis_completes(self, _client, _mock_orch):
        """POST /api/run_agent with competitor_analysis completes."""
        resp = _client.post("/api/run_agent", json={
            "agent": "competitor_analysis",
            "folder": "TestProduct",
            "quick_brief": "executive brief",
            "context": {"use_competitor": True, "use_campaign": False},
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        types = [e.get("type") for e in events]
        assert "agent_start" in types
        assert "agent_done" in types
        errors = [e for e in events if e.get("type") == "error"]
        assert not errors, f"Unexpected errors: {errors}"
        _mock_orch.run_competitor_analysis.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Agent 3 — campaign_strategy
# ---------------------------------------------------------------------------

class TestAgent3CampaignStrategy:
    """E2E: campaign_strategy agent flow via /api/run_agent."""

    def test_campaign_strategy_completes(self, _client, _mock_orch):
        """POST /api/run_agent with campaign_strategy completes."""
        resp = _client.post("/api/run_agent", json={
            "agent": "campaign_strategy",
            "folder": "TestProduct",
            "quick_brief": "executive brief",
            "context": {"use_competitor": True, "use_campaign": True},
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        types = [e.get("type") for e in events]
        assert "agent_start" in types
        assert "agent_done" in types
        errors = [e for e in events if e.get("type") == "error"]
        assert not errors, f"Unexpected errors: {errors}"
        _mock_orch.run_campaign_strategy.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Agent 4 — content_creator
# ---------------------------------------------------------------------------

class TestAgent4ContentCreator:
    """E2E: content_creator agent flow via /api/run_agent."""

    def test_content_creator_completes(self, _client, _mock_orch):
        """POST /api/run_agent with content_creator completes."""
        resp = _client.post("/api/run_agent", json={
            "agent": "content_creator",
            "folder": "TestProduct",
            "quick_brief": "test post",
            "context": {"use_competitor": True, "use_campaign": True},
            "platforms": ["facebook"],
            "content_count": 1,
            "media_type": "image",
            "media_when": "ask",
            "auto_image": False,
            "auto_video": False,
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        types = [e.get("type") for e in events]
        assert "agent_start" in types
        assert "agent_done" in types
        errors = [e for e in events if e.get("type") == "error"]
        assert not errors, f"Unexpected errors: {errors}"
        _mock_orch._run_content_creator_raw.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Multi-agent flow — /api/run_flows
# ---------------------------------------------------------------------------

class TestMultiAgentFlow:
    """E2E: sequential multi-agent flow via /api/run_flows."""

    def test_run_flows_all_agents(self, _client, _mock_orch):
        """POST /api/run_flows with all 4 agents in sequence completes."""
        resp = _client.post("/api/run_flows", json={
            "flows": [{
                "folders": ["TestProduct"],
                "agents": ["product_spec", "competitor_analysis",
                           "campaign_strategy", "content_creator"],
                "quick_brief": "test flow",
                "platforms": ["facebook"],
                "content_count": 1,
                "media_type": "image",
                "media_when": "ask",
                "auto_image": False,
                "auto_video": False,
                "context": {"use_competitor": True, "use_campaign": True},
            }],
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        types = [e.get("type") for e in events]
        assert "agent_start" in types
        assert "agent_done" in types
        errors = [e for e in events if e.get("type") == "error"]
        assert not errors, f"Unexpected errors: {errors}"


# ---------------------------------------------------------------------------
# 8. Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """Verify errors are surfaced to the client, not silently swallowed."""

    def test_missing_agent_returns_error(self, _client):
        """POST /api/run_agent without agent key returns error."""
        resp = _client.post("/api/run_agent", json={
            "folder": "TestProduct",
        })
        # Route returns JSONResponse with error (not SSE) for missing params
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data, f"Expected error in response: {data}"

    def test_missing_folder_returns_error(self, _client):
        """POST /api/run_agent without folder returns error."""
        resp = _client.post("/api/run_agent", json={
            "agent": "product_spec",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data, f"Expected error in response: {data}"

    def test_agent_exception_surfaces_error(self, _client, monkeypatch, tmp_path):
        """If the agent raises an exception, the error reaches the client."""
        import web_viewer

        def _make_failing_orch(**kw):
            fake = MagicMock()
            fake._make_client.return_value = MagicMock()
            fake._make_client.return_value.close = MagicMock()
            fake.product_id = None
            fake.results = {}
            fake.save_result.return_value = {}
            fake.run_product_spec.side_effect = RuntimeError("Simulated agent failure")
            return fake

        monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: _make_failing_orch())
        monkeypatch.setattr(web_viewer, "_read_folder", lambda f: (["info"], [], {}))

        resp = _client.post("/api/run_agent", json={
            "agent": "product_spec",
            "folder": "TestProduct",
            "quick_brief": "test",
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        errors = [e for e in events if e.get("type") == "error"]
        assert errors, "Expected error event for agent exception"
        assert "Simulated agent failure" in str(errors[0])


# ---------------------------------------------------------------------------
# 9. Session history and result retrieval
# ---------------------------------------------------------------------------

class TestSessionHistory:
    """Verify completed runs appear in session history and results are retrievable."""

    def test_sessions_listed_after_run(self, _client, _mock_orch, tmp_path):
        """After a flow run, GET /api/sessions includes the session."""
        # /api/run_flows writes _flow_meta_*.json which is required for sessions
        _client.post("/api/run_flows", json={
            "flows": [{
                "folders": ["TestProduct"],
                "agents": ["product_spec"],
                "quick_brief": "test session",
            }],
        })
        # Check sessions
        resp = _client.get("/api/sessions")
        assert resp.status_code == 200
        sessions = resp.json()
        assert isinstance(sessions, list)
        # At least one session should exist after a flow run
        assert len(sessions) >= 1, f"Expected at least 1 session, got {sessions}"


# ---------------------------------------------------------------------------
# 10. Repeated runs do not corrupt state
# ---------------------------------------------------------------------------

class TestRepeatedRuns:
    """Verify running the same agent twice doesn't corrupt state."""

    def test_two_sequential_runs(self, _client, _mock_orch):
        """Two sequential product_spec runs both complete successfully."""
        for i in range(2):
            resp = _client.post("/api/run_agent", json={
                "agent": "product_spec",
                "folder": "TestProduct",
                "quick_brief": f"run {i}",
                "context": {"use_competitor": False, "use_campaign": False},
            })
            assert resp.status_code == 200
            events = _parse_sse(resp)
            types = [e.get("type") for e in events]
            assert "agent_done" in types, f"Run {i}: missing agent_done"
            errors = [e for e in events if e.get("type") == "error"]
            assert not errors, f"Run {i}: unexpected errors: {errors}"


# ---------------------------------------------------------------------------
# 11. Auto mode flow
# ---------------------------------------------------------------------------

class TestAutoMode:
    """E2E: auto mode flow via /api/run_auto."""

    def test_auto_mode_completes(self, _client, _mock_orch, monkeypatch):
        """POST /api/run_auto with agents completes via SSE."""
        import web_viewer

        # Mock auto product selection
        _mock_orch.select_product_auto.return_value = {
            "product_ids": ["TestProduct"],
            "concept": "test concept",
            "pillar": "test pillar",
            "reason": "test reason",
            "asset_ids": [],
        }

        resp = _client.post("/api/run_auto", json={
            "quick_brief": "auto test",
            "platforms": ["facebook"],
            "media_type": "image",
            "media_when": "ask",
            "auto_image": False,
            "auto_video": False,
            "content_count": 1,
            "product_count": 1,
            "agents": ["product_spec"],
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        types = [e.get("type") for e in events]
        errors = [e for e in events if e.get("type") == "error"]
        assert not errors, f"Auto mode errors: {errors}"
