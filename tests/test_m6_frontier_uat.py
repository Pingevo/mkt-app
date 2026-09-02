"""Offline regression tests for scripts/m6_frontier_uat.py.

These tests never make network or billable calls.
"""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import m6_frontier_uat as m6


@pytest.fixture
def s4():
    return next(s for s in m6.SCENARIOS if s["id"] == "S4")


@pytest.fixture
def s2():
    return next(s for s in m6.SCENARIOS if s["id"] == "S2")


class TestM6FrontierGuard:
    def test_model_lock_allows_fable(self):
        guard = m6.M6FrontierGuard(total_cap=1.0, reserves={"S1": 0.30})
        guard.set_scenario("S1")
        payload = {"model": "anthropic/claude-fable-5.1"}
        assert guard._resolve_reserve(payload) == 0.30

    def test_model_lock_blocks_wrong_model(self):
        guard = m6.M6FrontierGuard(total_cap=1.0, reserves={"S1": 0.30})
        guard.set_scenario("S1")
        with pytest.raises(RuntimeError, match="model mismatch"):
            guard._resolve_reserve({"model": "anthropic/claude-sonnet-5"})

    def test_total_cap_blocks_before_call(self):
        # Patch httpx so the guard sees a chat/completions request but
        # does not need a real network call (it blocks before original_post).
        guard = m6.M6FrontierGuard(total_cap=0.10, reserves={"S1": 0.30})
        guard.set_scenario("S1")
        with guard:
            client = httpx.Client()
            with pytest.raises(RuntimeError, match="Frontier cap exceeded"):
                client.post("https://openrouter.ai/api/v1/chat/completions", json={"model": m6.FRONTIER_MODEL})

    def test_web_use_over_two_stops(self):
        # Provide a fake response with 3 web_search requests.
        fake_response = {
            "id": "test",
            "model": m6.FRONTIER_MODEL,
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 100,
                "server_tool_use_details": {"web_search_requests": 3},
            },
            "cost": 0.01,
        }

        def fake_post(client: httpx.Client, url: str, **kwargs):
            request = httpx.Request("POST", url)
            return httpx.Response(200, json=fake_response, request=request)

        # Monkey-patch the original httpx.Client.post to fake_post before guard.
        original = httpx.Client.post
        httpx.Client.post = fake_post
        try:
            guard = m6.M6FrontierGuard(total_cap=1.0, reserves={"S2": 0.32})
            guard.set_scenario("S2")
            with guard:
                client = httpx.Client()
                with pytest.raises(RuntimeError, match="web_search uses exceeded"):
                    client.post("https://openrouter.ai/api/v1/chat/completions", json={"model": m6.FRONTIER_MODEL})
        finally:
            httpx.Client.post = original


class TestScenarioConfig:
    def test_s4_text_only(self, s4):
        assert s4["frontier_web_uses"] == 0
        assert s4.get("auto_image") is False
        messages, _ = m6._build_frontier_messages(s4)
        assert any("TikTok" in str(m.get("content", "")) for m in messages)

    def test_s2_s3_have_web_tools(self, s2):
        assert s2["frontier_web_uses"] == 2

    def test_frontier_dry_run_s4_no_api_call(self, s4):
        guard = m6.M6FrontierGuard(total_cap=1.0, reserves={"S4": 0.16})
        r = m6._run_frontier_scenario(s4, guard, dry_run=True)
        assert r.side == "Frontier"
        assert r.cost_usd == 0.0
        assert r.evidence.get("tools") is None


class TestEvidenceAndAnonymization:
    def test_write_evidence_creates_anonymized_outputs(self):
        m = m6.RunResult(
            scenario_id="S1", side="MKTApp", output="MKTApp S1 output",
            actual_model="m", cost_usd=0.1, prompt_tokens=100, completion_tokens=50,
            web_uses=0, error=None, stopped=False, evidence={},
        )
        f = m6.RunResult(
            scenario_id="S1", side="Frontier", output="Frontier S1 output",
            actual_model="f", cost_usd=0.3, prompt_tokens=100, completion_tokens=100,
            web_uses=0, error=None, stopped=False, evidence={},
        )
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            m6._write_evidence(run_dir, [m], [f], False, "")
            assert (run_dir / "m6_mapping_secret.json").exists()
            assert (run_dir / "m6_scorecard.csv").exists()
            mapping = json.loads((run_dir / "m6_mapping_secret.json").read_text())
            assert mapping["S1"]["mktapp_model"] == "m"
            assert mapping["S1"]["frontier_model"] == "f"
            # Outputs exist and are not empty; blind reviewer does not see source labels.
            assert (run_dir / "outputs" / "S1_X.txt").exists()
            assert (run_dir / "outputs" / "S1_Y.txt").exists()
            # Scorecard template must not contain source names.
            scorecard = (run_dir / "m6_scorecard.csv").read_text()
            assert "MKTApp" not in scorecard
            assert "Frontier" not in scorecard


class TestBudgetAccounting:
    def test_total_reserve_within_approval_cap(self):
        mktapp_total = sum(s["mktapp_reserve"] for s in m6.SCENARIOS)
        frontier_total = sum(s["frontier_reserve"] for s in m6.SCENARIOS)
        assert mktapp_total + frontier_total <= m6.APPROVAL_CAP
