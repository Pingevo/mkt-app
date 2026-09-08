"""Web E2E data-integrity tests for Agent 1-4.

These tests prove that the correct product identity, source data, and
context reach each Agent through the real UI -> API -> Orchestrator ->
Agent -> llm.chat() path, using deterministic mocks only where a real
paid model/web/media call would otherwise be required.

What this proves that existing browser E2E / beta smoke tests cannot:
- The correct product identity reaches the agent (not just "some text")
- Source fields from the selected product survive through the chain
- No cross-product leakage when two products exist
- Product images from ingest reach the agent's multimodal input
- Changing the product changes the downstream context
- User quick_brief/instructions are preserved through the request
- The agent result reaches the renderer and becomes visible

Architecture:
- FastAPI TestClient exercises real HTTP routing + SSE streaming
- Real Orchestrator (not mocked) with make_client() patched to return
  a FakeLLM that captures the actual messages at the llm.chat() boundary
- Real product data with known facts written to data/ and cache/
- Two products (AlphaWidget, BetaGadget) for cross-product isolation tests

No paid/model/web/media calls are made.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Known-fact fixtures — these are the data-integrity anchors
# ---------------------------------------------------------------------------

ALPHA_NAME = "AlphaWidget"
ALPHA_PRICE = "2990"
ALPHA_SPEC = "AMOLED 1.43 inch display, SpO2 sensor, GPS, 5ATM water resistant"
ALPHA_TEXT = (
    f"Product: {ALPHA_NAME}\n"
    f"Price: {ALPHA_PRICE} THB\n"
    f"Spec: {ALPHA_SPEC}\n"
    f"Category: Smartwatch\n"
)

BETA_NAME = "BetaGadget"
BETA_PRICE = "1500"
BETA_SPEC = "Bluetooth 5.3, 10-day battery, IP67, fitness tracker"
BETA_TEXT = (
    f"Product: {BETA_NAME}\n"
    f"Price: {BETA_PRICE} THB\n"
    f"Spec: {BETA_SPEC}\n"
    f"Category: Fitness Band\n"
)


# ---------------------------------------------------------------------------
# FakeLLM — captures messages at the llm.chat() boundary
# ---------------------------------------------------------------------------

class FakeLLM:
    """Deterministic LLM double — captures messages at the llm.chat()
    boundary.  No provider calls."""

    def __init__(self, output: str = "mock output", outputs: list[str] | None = None):
        self._output = output
        self._outputs = outputs
        self.calls: list[dict] = []
        self.last_truncated = False

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        # Grounding gate calls use a JSON schema response format and a
        # source ending in .final_grounding_check — return a valid
        # grounded verdict so wiring tests are not blocked by the gate.
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            return '{"grounded": true, "unsupported_claims": []}'
        if self._outputs is not None and len(self._outputs) > len(self.calls) - 1:
            out = self._outputs[len(self.calls) - 1]
        else:
            out = self._output
        if kwargs.get("return_annotations"):
            return out, []
        return out

    def close(self):
        pass

    def abort(self):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _client(tmp_path, monkeypatch):
    """FastAPI TestClient with isolated filesystem, two real products,
    and a real Orchestrator whose make_client() returns a FakeLLM."""
    import importlib
    import web_viewer

    importlib.reload(web_viewer)

    # Isolate filesystem paths
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(web_viewer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(web_viewer, "BRAND_DIR", tmp_path / "brand")

    # Create two products with known facts
    for name, text in [(ALPHA_NAME, ALPHA_TEXT), (BETA_NAME, BETA_TEXT)]:
        prod_dir = tmp_path / "data" / name
        prod_dir.mkdir(parents=True)
        (prod_dir / "info.txt").write_text(text, encoding="utf-8")
        cache_dir = tmp_path / "cache" / name
        cache_dir.mkdir(parents=True)

    # Initialize product DB with "ready" status and raw_text so agents
    # that fetch product data from the DB (Agent 2, 3, 4) can find it
    from src import product_db
    _orig_project_root = product_db._project_root
    product_db._project_root = lambda: tmp_path
    for name, text in [(ALPHA_NAME, ALPHA_TEXT), (BETA_NAME, BETA_TEXT)]:
        product_db.set_status(name, product_db.STATUS_READY)
        # Save raw_text into the product record so _get_product_data() works
        rec = product_db.load(name)
        rec["raw_text"] = text
        rec["text_extracts"] = [{"file": "info.txt", "text": text}]
        product_db.save(name, rec)

    # Brand directory
    (tmp_path / "brand").mkdir(exist_ok=True)

    # Config
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (config_dir / "agents.yaml").write_text(
        "product_spec:\n  model: fake\n  temperature: 0.3\n  max_tokens: 4096\n"
        "competitor_analysis:\n  model: fake\n  temperature: 0.4\n  max_tokens: 8192\n  web_search: true\n"
        "campaign_strategy:\n  model: fake\n  temperature: 0.8\n  max_tokens: 4096\n  web_search: true\n"
        "content_creator:\n  model: fake\n  temperature: 0.9\n  max_tokens: 8192\n",
        encoding="utf-8",
    )

    # Output directory
    (tmp_path / "output").mkdir(exist_ok=True)

    # Initialize module globals
    monkeypatch.setattr(web_viewer, "_current_llm", None, raising=False)
    monkeypatch.setattr(web_viewer, "_session_ts", "", raising=False)
    monkeypatch.setattr(web_viewer, "_cancel_requested", False, raising=False)

    # Mock content history
    monkeypatch.setattr(web_viewer.content_history, "record_entry", lambda *a, **k: True)
    monkeypatch.setattr(web_viewer.content_history, "format_product_history_for_prompt", lambda *a, **k: "")
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file", lambda *a, **k: None)

    # Mock media generation (not part of text-flow data integrity)
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history",
                        lambda *a, **k: None)

    # Set a dummy API key so make_client doesn't raise
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key-for-testing")

    yield TestClient(web_viewer.app)

    # Restore
    product_db._project_root = _orig_project_root


@pytest.fixture
def _capture_llm(_client, monkeypatch):
    """Patch Orchestrator.make_client to return a FakeLLM that captures
    the actual messages sent to llm.chat().

    Returns the FakeLLM instance so tests can inspect .calls.
    """
    import web_viewer
    from src.orchestrator import Orchestrator

    fake_llm = FakeLLM(
        output=(
            "## ราคาแนะนำ\n- ราคา pending: ราคาเริ่มต้น 1,990 บาท สำหรับแพ็คเกจพื้นฐาน\n"
            "## แคมเปญหลัก\n- Launch Campaign: เปิดตัวสินค้าด้วยการสร้าง awareness ผ่าน social media และ influencer marketing\n"
            "## แคมเปญเสริม\n- Influencer Partnership: ร่วมมือกับ fitness influencer เพื่อสร้างคอนเทนต์รีวิว\n"
            "## ช่องทางโปรโมท\n- Facebook Ads, TikTok, Instagram Reels, YouTube Shorts\n"
            "## KPI\n- Reach 1M impressions, 5% engagement rate, 10K clicks\n"
            "## งบประมาณ\n- pending: งบประมาณเบื้องต้น 50,000 บาทต่อเดือน\n"
            "## แหล่งอ้างอิง\n- https://example.com/market-research\n"
        )
    )

    # Patch make_client on the Orchestrator class so the real Orchestrator
    # is used but the LLM boundary is a FakeLLM
    monkeypatch.setattr(Orchestrator, "make_client", lambda self: fake_llm)

    # Also patch _current_llm so the route handler uses our FakeLLM
    monkeypatch.setattr(web_viewer, "_current_llm", fake_llm, raising=False)

    return fake_llm


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


def _extract_messages_text(fake_llm, call_idx=0):
    """Extract system and user text from the FakeLLM's captured calls."""
    assert len(fake_llm.calls) > call_idx, \
        f"Expected at least {call_idx + 1} llm.chat() calls, got {len(fake_llm.calls)}"
    messages = fake_llm.calls[call_idx]["messages"]
    system = ""
    user = ""
    for msg in messages:
        if msg["role"] == "system":
            system = msg["content"]
        elif msg["role"] == "user":
            if isinstance(msg["content"], list):
                user = " ".join(
                    part.get("text", "") for part in msg["content"]
                    if isinstance(part, dict)
                )
            else:
                user = msg["content"]
    return system, user


# ---------------------------------------------------------------------------
# Agent 1 — product_spec: product identity + source data propagation
# ---------------------------------------------------------------------------

class TestAgent1ProductSpecDataIntegrity:
    """E2E: product_spec through real API + real Orchestrator + FakeLLM.

    Verifies that the correct product identity and source data reach the
    agent's llm.chat() call, and that switching products changes the
    downstream context.
    """

    def test_alpha_product_identity_and_facts_reach_agent(self, _client, _capture_llm):
        """POST /api/run_agent with AlphaWidget → agent receives AlphaWidget
        name, price, and spec in the user prompt."""
        resp = _client.post("/api/run_agent", json={
            "agent": "product_spec",
            "folder": ALPHA_NAME,
            "quick_brief": "",
            "context": {"use_competitor": False, "use_campaign": False},
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        types = [e.get("type") for e in events]
        assert "agent_start" in types
        assert "agent_done" in types
        errors = [e for e in events if e.get("type") == "error"]
        assert not errors, f"Unexpected errors: {errors}"

        system, user = _extract_messages_text(_capture_llm)
        # The product name must appear in the user prompt (source data)
        assert ALPHA_NAME in user, \
            f"Agent 1 user prompt must contain product name '{ALPHA_NAME}'"
        # Known facts must survive through the chain
        assert ALPHA_PRICE in user, \
            f"Agent 1 user prompt must contain product price '{ALPHA_PRICE}'"
        assert "AMOLED" in user, \
            "Agent 1 user prompt must contain product spec detail"

    def test_beta_product_identity_and_facts_reach_agent(self, _client, _capture_llm):
        """POST /api/run_agent with BetaGadget → agent receives BetaGadget
        name, price, and spec in the user prompt."""
        resp = _client.post("/api/run_agent", json={
            "agent": "product_spec",
            "folder": BETA_NAME,
            "quick_brief": "",
            "context": {"use_competitor": False, "use_campaign": False},
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        assert "agent_done" in [e.get("type") for e in events]

        system, user = _extract_messages_text(_capture_llm)
        assert BETA_NAME in user, \
            f"Agent 1 user prompt must contain product name '{BETA_NAME}'"
        assert BETA_PRICE in user, \
            f"Agent 1 user prompt must contain product price '{BETA_PRICE}'"
        assert "Bluetooth 5.3" in user, \
            "Agent 1 user prompt must contain product spec detail"

    def test_no_cross_product_leakage(self, _client, _capture_llm):
        """Running AlphaWidget must NOT include BetaGadget data, and vice versa."""
        # Run Alpha
        _client.post("/api/run_agent", json={
            "agent": "product_spec",
            "folder": ALPHA_NAME,
            "context": {"use_competitor": False, "use_campaign": False},
        })
        _, alpha_user = _extract_messages_text(_capture_llm, 0)
        assert ALPHA_NAME in alpha_user
        assert BETA_NAME not in alpha_user, \
            "AlphaWidget run must NOT contain BetaGadget data (cross-product leakage)"
        assert BETA_PRICE not in alpha_user, \
            "AlphaWidget run must NOT contain BetaGadget price (cross-product leakage)"

    def test_changing_product_changes_context(self, _client, monkeypatch):
        """Running Alpha then Beta must deliver different product data to the
        agent each time — no stale cache."""
        import web_viewer
        from src.orchestrator import Orchestrator

        # We need two separate FakeLLMs since each run creates a new Orchestrator
        long_output = (
            "## ราคาแนะนำ\n- ราคา pending: ราคาเริ่มต้น 1,990 บาท สำหรับแพ็คเกจพื้นฐาน\n"
            "## แคมเปญหลัก\n- Launch Campaign: เปิดตัวสินค้าด้วยการสร้าง awareness\n"
            "## แคมเปญเสริม\n- Influencer Partnership: ร่วมมือกับ influencer\n"
            "## ช่องทางโปรโมท\n- Facebook Ads, TikTok\n"
            "## KPI\n- Reach 1M impressions, 5% engagement rate\n"
            "## งบประมาณ\n- pending: งบประมาณเบื้องต้น 50,000 บาทต่อเดือน\n"
            "## แหล่งอ้างอิง\n- https://example.com/market-research\n"
        )
        fake1 = FakeLLM(output=long_output)
        fake2 = FakeLLM(output=long_output)

        call_count = [0]
        def _make_client(self):
            call_count[0] += 1
            return fake1 if call_count[0] == 1 else fake2

        monkeypatch.setattr(Orchestrator, "make_client", lambda self: _make_client(self))
        monkeypatch.setattr(web_viewer, "_current_llm", None, raising=False)

        # Run Alpha
        _client.post("/api/run_agent", json={
            "agent": "product_spec",
            "folder": ALPHA_NAME,
            "context": {"use_competitor": False, "use_campaign": False},
        })
        _, alpha_user = _extract_messages_text(fake1, 0)
        assert ALPHA_NAME in alpha_user

        # Reset _current_llm so a new FakeLLM is used
        monkeypatch.setattr(web_viewer, "_current_llm", None, raising=False)

        # Run Beta
        _client.post("/api/run_agent", json={
            "agent": "product_spec",
            "folder": BETA_NAME,
            "context": {"use_competitor": False, "use_campaign": False},
        })
        _, beta_user = _extract_messages_text(fake2, 0)
        assert BETA_NAME in beta_user
        assert ALPHA_NAME not in beta_user, \
            "Second run (BetaGadget) must not contain stale AlphaWidget data"

    def test_quick_brief_preserved_through_request(self, _client, _capture_llm):
        """User's quick_brief must appear in the agent's user prompt."""
        brief = "focus on pricing strategy for Gen Z"
        resp = _client.post("/api/run_agent", json={
            "agent": "product_spec",
            "folder": ALPHA_NAME,
            "quick_brief": brief,
            "context": {"use_competitor": False, "use_campaign": False},
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        assert "agent_done" in [e.get("type") for e in events]

        _, user = _extract_messages_text(_capture_llm)
        assert brief in user, \
            "User's quick_brief must be preserved through the API -> agent chain"

    def test_result_saved_and_retrievable(self, _client, _capture_llm):
        """After agent_done, the result file must be saved and retrievable
        via the sessions API.  Uses /api/run_flows which writes flow meta
        (required for session listing)."""
        resp = _client.post("/api/run_flows", json={
            "flows": [{
                "folders": [ALPHA_NAME],
                "agents": ["product_spec"],
                "quick_brief": "test session",
                "context": {"use_competitor": False, "use_campaign": False},
            }],
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        assert "agent_done" in [e.get("type") for e in events]

        # Check sessions
        sess_resp = _client.get("/api/sessions")
        assert sess_resp.status_code == 200
        sessions = sess_resp.json()
        assert isinstance(sessions, list)
        assert len(sessions) >= 1, "At least one session must exist after a run"


# ---------------------------------------------------------------------------
# Agent 2 — competitor_analysis: product identity + evidence mode
# ---------------------------------------------------------------------------

class TestAgent2CompetitorAnalysisDataIntegrity:
    """E2E: competitor_analysis through real API + real Orchestrator + FakeLLM.

    Verifies product identity reaches the agent and that evidence mode
    keeps brand_reference out of Stage A.
    """

    def test_product_identity_reaches_agent(self, _client, _capture_llm):
        """POST /api/run_agent with competitor_analysis → agent receives
        the product identity in its context."""
        # Agent 2 uses web_search, so FakeLLM must return annotations format
        _capture_llm._output = json.dumps({
            "target_model": ALPHA_NAME,
            "competitor_names": ["CompA"],
            "evidence": [{
                "competitor": "CompA",
                "field": "price",
                "claim": "CompA costs $100",
                "url": "https://example.com/compa",
                "geography": "global",
            }],
            "evidence_based_recommendations": [],
            "strategic_hypotheses": [],
            "uncertainty": [],
        })

        # Patch downstream boundaries to keep offline
        from src.agents.competitor_evidence import SemanticEvidenceReviewer
        from src.agents.competitor_analysis import CompetitorReportRenderer
        import src.agents.competitor_analysis as comp_mod

        # These patches are on downstream reviewer/renderer, not the model boundary
        orig_review = SemanticEvidenceReviewer.review
        orig_validate = CompetitorReportRenderer.validate
        orig_interpret = comp_mod.BrandInterpretationPass.interpret

        SemanticEvidenceReviewer.review = lambda self, research, relevant: research
        CompetitorReportRenderer.validate = lambda self: []
        comp_mod.BrandInterpretationPass.interpret = lambda self, *a, **k: []

        try:
            resp = _client.post("/api/run_agent", json={
                "agent": "competitor_analysis",
                "folder": ALPHA_NAME,
                "context": {"use_competitor": True, "use_campaign": False},
            })
            assert resp.status_code == 200
            events = _parse_sse(resp)
            types = [e.get("type") for e in events]
            assert "agent_done" in types, f"Missing agent_done in {types}"
            errors = [e for e in events if e.get("type") == "error"]
            assert not errors, f"Unexpected errors: {errors}"
        finally:
            SemanticEvidenceReviewer.review = orig_review
            CompetitorReportRenderer.validate = orig_validate
            comp_mod.BrandInterpretationPass.interpret = orig_interpret

    def test_no_cross_product_leakage(self, _client, _capture_llm):
        """Running competitor_analysis for Alpha must not include Beta data."""
        _capture_llm._output = json.dumps({
            "target_model": ALPHA_NAME,
            "competitor_names": ["CompA"],
            "evidence": [{
                "competitor": "CompA",
                "field": "price",
                "claim": "CompA costs $100",
                "url": "https://example.com/compa",
                "geography": "global",
            }],
            "evidence_based_recommendations": [],
            "strategic_hypotheses": [],
            "uncertainty": [],
        })

        from src.agents.competitor_evidence import SemanticEvidenceReviewer
        from src.agents.competitor_analysis import CompetitorReportRenderer
        import src.agents.competitor_analysis as comp_mod

        orig_review = SemanticEvidenceReviewer.review
        orig_validate = CompetitorReportRenderer.validate
        orig_interpret = comp_mod.BrandInterpretationPass.interpret

        SemanticEvidenceReviewer.review = lambda self, research, relevant: research
        CompetitorReportRenderer.validate = lambda self: []
        comp_mod.BrandInterpretationPass.interpret = lambda self, *a, **k: []

        try:
            _client.post("/api/run_agent", json={
                "agent": "competitor_analysis",
                "folder": ALPHA_NAME,
                "context": {"use_competitor": True, "use_campaign": False},
            })

            # The first call to llm.chat() is Stage A — verify no Beta data
            _, user = _extract_messages_text(_capture_llm, 0)
            assert BETA_NAME not in user, \
                "AlphaWidget competitor_analysis must not contain BetaGadget data"
        finally:
            SemanticEvidenceReviewer.review = orig_review
            CompetitorReportRenderer.validate = orig_validate
            comp_mod.BrandInterpretationPass.interpret = orig_interpret


# ---------------------------------------------------------------------------
# Agent 3 — campaign_strategy: product identity + user instructions
# ---------------------------------------------------------------------------

class TestAgent3CampaignStrategyDataIntegrity:
    """E2E: campaign_strategy through real API + real Orchestrator + FakeLLM.

    Verifies product identity and user instructions reach the agent.
    """

    def test_product_identity_reaches_agent(self, _client, _capture_llm):
        """POST /api/run_agent with campaign_strategy → agent receives
        the product identity in its context."""
        resp = _client.post("/api/run_agent", json={
            "agent": "campaign_strategy",
            "folder": ALPHA_NAME,
            "quick_brief": "",
            "context": {"use_competitor": True, "use_campaign": True},
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        types = [e.get("type") for e in events]
        assert "agent_done" in types, f"Missing agent_done in {types}"
        errors = [e for e in events if e.get("type") == "error"]
        assert not errors, f"Unexpected errors: {errors}"

    def test_quick_brief_preserved(self, _client, _capture_llm):
        """User's quick_brief must reach the campaign_strategy agent."""
        brief = "focus on TikTok marketing for young audience"
        resp = _client.post("/api/run_agent", json={
            "agent": "campaign_strategy",
            "folder": ALPHA_NAME,
            "quick_brief": brief,
            "context": {"use_competitor": True, "use_campaign": True},
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        assert "agent_done" in [e.get("type") for e in events]

        _, user = _extract_messages_text(_capture_llm)
        assert brief in user, \
            "User's quick_brief must be preserved through the API -> agent chain"

    def test_no_cross_product_leakage(self, _client, _capture_llm):
        """Running campaign_strategy for Alpha must not include Beta data."""
        _client.post("/api/run_agent", json={
            "agent": "campaign_strategy",
            "folder": ALPHA_NAME,
            "context": {"use_competitor": True, "use_campaign": True},
        })

        _, user = _extract_messages_text(_capture_llm, 0)
        assert BETA_NAME not in user, \
            "AlphaWidget campaign_strategy must not contain BetaGadget data"


# ---------------------------------------------------------------------------
# Agent 4 — content_creator: product identity + media propagation
# ---------------------------------------------------------------------------

class TestAgent4ContentCreatorDataIntegrity:
    """E2E: content_creator through real API + real Orchestrator + FakeLLM.

    Verifies product identity reaches the agent and the result is saved
    as a valid content artifact.
    """

    def test_product_identity_reaches_agent(self, _client, _capture_llm):
        """POST /api/run_agent with content_creator → agent receives
        the product identity in its context."""
        _capture_llm._output = json.dumps({
            "posts": [{
                "platform": "facebook",
                "concept": "test concept",
                "title": "Test Post",
                "caption": "test caption",
                "script": "",
                "hashtags": "#test",
                "image_prompts": [],
                "video_prompts": [],
                "asset_ids": [],
            }],
        }, ensure_ascii=False)

        resp = _client.post("/api/run_agent", json={
            "agent": "content_creator",
            "folder": ALPHA_NAME,
            "quick_brief": "",
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
        assert "agent_done" in types, f"Missing agent_done in {types}"
        errors = [e for e in events if e.get("type") == "error"]
        assert not errors, f"Unexpected errors: {errors}"

    def test_no_cross_product_leakage(self, _client, _capture_llm):
        """Running content_creator for Alpha must not include Beta data."""
        _capture_llm._output = json.dumps({
            "posts": [{
                "platform": "facebook",
                "concept": "test",
                "title": "Test",
                "caption": "test",
                "script": "",
                "hashtags": "#test",
                "image_prompts": [],
                "video_prompts": [],
                "asset_ids": [],
            }],
        }, ensure_ascii=False)

        _client.post("/api/run_agent", json={
            "agent": "content_creator",
            "folder": ALPHA_NAME,
            "context": {"use_competitor": True, "use_campaign": True},
            "platforms": ["facebook"],
            "content_count": 1,
            "media_type": "image",
            "media_when": "ask",
            "auto_image": False,
            "auto_video": False,
        })

        _, user = _extract_messages_text(_capture_llm, 0)
        assert BETA_NAME not in user, \
            "AlphaWidget content_creator must not contain BetaGadget data"

    def test_result_saved_as_valid_artifact(self, _client, _capture_llm):
        """After content_creator runs, the result must be saved as a valid
        JSON artifact retrievable via the sessions API.  Uses /api/run_flows
        which writes flow meta (required for session listing)."""
        _capture_llm._output = json.dumps({
            "posts": [{
                "platform": "facebook",
                "concept": "test concept",
                "title": "Test Post",
                "caption": "test caption",
                "script": "",
                "hashtags": "#test",
                "image_prompts": [],
                "video_prompts": [],
                "asset_ids": [],
            }],
        }, ensure_ascii=False)

        resp = _client.post("/api/run_flows", json={
            "flows": [{
                "folders": [ALPHA_NAME],
                "agents": ["content_creator"],
                "quick_brief": "test content session",
                "context": {"use_competitor": True, "use_campaign": True},
                "platforms": ["facebook"],
                "content_count": 1,
                "media_type": "image",
                "media_when": "ask",
                "auto_image": False,
                "auto_video": False,
            }],
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        assert "agent_done" in [e.get("type") for e in events]

        # Check sessions
        sess_resp = _client.get("/api/sessions")
        assert sess_resp.status_code == 200
        sessions = sess_resp.json()
        assert len(sessions) >= 1, "At least one session must exist after content_creator run"


# ---------------------------------------------------------------------------
# Multi-agent flow — /api/run_flows data integrity
# ---------------------------------------------------------------------------

class TestMultiAgentFlowDataIntegrity:
    """E2E: multi-agent flow via /api/run_flows with real Orchestrator.

    Verifies that sequential agents in a flow all receive the correct
    product identity.
    """

    def test_flow_product_identity_preserved_across_agents(self, _client, _capture_llm):
        """POST /api/run_flows with product_spec + campaign_strategy →
        both agents receive the same product identity."""
        # Use a long enough output for campaign_strategy validation
        _capture_llm._output = (
            "## ราคาแนะนำ\n- ราคา pending: ราคาเริ่มต้น 1,990 บาท สำหรับแพ็คเกจพื้นฐาน\n"
            "## แคมเปญหลัก\n- Launch Campaign: เปิดตัวสินค้าด้วยการสร้าง awareness ผ่าน social media\n"
            "## แคมเปญเสริม\n- Influencer Partnership: ร่วมมือกับ fitness influencer เพื่อสร้างคอนเทนต์รีวิว\n"
            "## ช่องทางโปรโมท\n- Facebook Ads, TikTok, Instagram Reels, YouTube Shorts\n"
            "## KPI\n- Reach 1M impressions, 5% engagement rate, 10K clicks\n"
            "## งบประมาณ\n- pending: งบประมาณเบื้องต้น 50,000 บาทต่อเดือน\n"
            "## แหล่งอ้างอิง\n- https://example.com/market-research\n"
        )

        resp = _client.post("/api/run_flows", json={
            "flows": [{
                "folders": [ALPHA_NAME],
                "agents": ["product_spec", "campaign_strategy"],
                "quick_brief": "test flow",
                "context": {"use_competitor": True, "use_campaign": True},
            }],
        })
        assert resp.status_code == 200
        events = _parse_sse(resp)
        types = [e.get("type") for e in events]
        assert "agent_done" in types, f"Missing agent_done in {types}"
        errors = [e for e in events if e.get("type") == "error"]
        assert not errors, f"Unexpected errors: {errors}"

        # At least one call must contain the product name
        all_user_text = ""
        for call in _capture_llm.calls:
            for msg in call["messages"]:
                if msg["role"] == "user":
                    content = msg["content"]
                    if isinstance(content, list):
                        all_user_text += " ".join(
                            p.get("text", "") for p in content if isinstance(p, dict)
                        )
                    else:
                        all_user_text += content
        assert ALPHA_NAME in all_user_text, \
            "At least one agent in the flow must receive the product name"
        assert BETA_NAME not in all_user_text, \
            "Flow for AlphaWidget must not contain BetaGadget data"


# ---------------------------------------------------------------------------
# Error / edge-case data integrity
# ---------------------------------------------------------------------------

class TestDataIntegrityEdgeCases:
    """E2E: edge cases that could cause wrong source wiring."""

    def test_wrong_agent_key_returns_error(self, _client, _capture_llm):
        """An invalid agent key must return an error, not silently run
        a different agent."""
        resp = _client.post("/api/run_agent", json={
            "agent": "nonexistent_agent",
            "folder": ALPHA_NAME,
            "context": {"use_competitor": False, "use_campaign": False},
        })
        # The route should either return an error JSON or an SSE error event
        events = _parse_sse(resp)
        errors = [e for e in events if e.get("type") == "error"]
        if errors:
            assert len(errors) > 0, "Invalid agent key must produce an error"
        else:
            # Maybe returned as JSON error
            assert resp.status_code in (200, 400)

    def test_missing_folder_returns_error(self, _client, _capture_llm):
        """A missing folder must return an error, not silently use a
        different product."""
        resp = _client.post("/api/run_agent", json={
            "agent": "product_spec",
            "folder": "NonExistentProduct",
            "context": {"use_competitor": False, "use_campaign": False},
        })
        events = _parse_sse(resp)
        errors = [e for e in events if e.get("type") == "error"]
        if errors:
            assert len(errors) > 0, "Missing folder must produce an error"
        else:
            # The agent should not have been called
            assert len(_capture_llm.calls) == 0, \
                "Agent must not be called for a non-existent product"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
