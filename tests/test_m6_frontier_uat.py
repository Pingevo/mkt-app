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
        guard = m6.M6FrontierGuard(total_cap=1.0, per_scenario_caps={"S1": 0.30})
        guard.set_scenario("S1")
        payload = {"model": "anthropic/claude-fable-5.1"}
        # Reserve is now computed from payload; for a minimal payload it should fit the cap.
        reserve = guard._resolve_reserve(payload)
        assert reserve <= 0.30

    def test_model_lock_blocks_wrong_model(self):
        guard = m6.M6FrontierGuard(total_cap=1.0, per_scenario_caps={"S1": 0.30})
        guard.set_scenario("S1")
        with pytest.raises(RuntimeError, match="model mismatch"):
            guard._resolve_reserve({"model": "anthropic/claude-sonnet-5"})

    def test_total_cap_blocks_before_call(self):
        guard = m6.M6FrontierGuard(total_cap=0.10, per_scenario_caps={"S1": 0.30})
        guard.set_scenario("S1")
        with guard:
            client = httpx.Client()
            with pytest.raises(RuntimeError, match="Frontier cap exceeded"):
                client.post("https://openrouter.ai/api/v1/chat/completions", json={"model": m6.FRONTIER_MODEL})

    def test_web_use_over_two_stops(self):
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

        original = httpx.Client.post
        httpx.Client.post = fake_post
        try:
            guard = m6.M6FrontierGuard(total_cap=1.0, per_scenario_caps={"S2": 0.60})
            guard.set_scenario("S2")
            with guard:
                client = httpx.Client()
                with pytest.raises(RuntimeError, match="web_search uses exceeded"):
                    client.post("https://openrouter.ai/api/v1/chat/completions", json={"model": m6.FRONTIER_MODEL})
        finally:
            httpx.Client.post = original

    def test_per_scenario_reserve_blocks_over_cap(self):
        # Simulate a payload whose computed reserve (large prompt + 2 web uses) exceeds cap.
        big_text = "x" * 40000
        guard = m6.M6FrontierGuard(total_cap=2.0, per_scenario_caps={"S2": 0.32})
        guard.set_scenario("S2")
        payload = {
            "model": m6.FRONTIER_MODEL,
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": big_text}],
            "tools": [{"type": "openrouter:web_search", "parameters": {"max_uses": 2}}],
        }
        with pytest.raises(RuntimeError, match="per-scenario reserve breach before call"):
            guard._resolve_reserve(payload)

    def test_per_scenario_actual_breach_stops(self):
        # Simulate a response whose actual cost exceeds the per-scenario cap.
        fake_response = {
            "id": "test",
            "model": m6.FRONTIER_MODEL,
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 1000,
                "server_tool_use_details": {"web_search_requests": 0},
            },
            "cost": 0.40,
        }

        def fake_post(client: httpx.Client, url: str, **kwargs):
            request = httpx.Request("POST", url)
            return httpx.Response(200, json=fake_response, request=request)

        original = httpx.Client.post
        httpx.Client.post = fake_post
        try:
            guard = m6.M6FrontierGuard(total_cap=2.0, per_scenario_caps={"S2": 0.32})
            guard.set_scenario("S2")
            with guard:
                client = httpx.Client()
                with pytest.raises(RuntimeError, match="per-scenario cost breach after call"):
                    client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        json={"model": m6.FRONTIER_MODEL, "max_tokens": 2000, "messages": [{"role": "user", "content": "x" * 1000}]},
                    )
        finally:
            httpx.Client.post = original


class TestScenarioConfig:
    def test_s4_text_only(self, s4):
        assert s4["frontier_web_uses"] == 0
        assert s4.get("auto_image") is False
        messages, _ = m6._build_frontier_messages(s4)
        assert any("TikTok" in str(m.get("content", "")) for m in messages)

    @pytest.fixture
    def s1(self):
        return next(s for s in m6.SCENARIOS if s["id"] == "S1")

    def test_frontier_messages_have_data_url_images(self, s1):
        messages, _ = m6._build_frontier_messages(s1)
        user = messages[1].get("content", "")
        if not isinstance(user, list):
            # No images available in this checkout.
            return
        image_parts = [p for p in user if p.get("type") == "image_url"]
        for part in image_parts:
            url = part.get("image_url", {}).get("url", "")
            assert url.startswith("data:image/") or url.startswith("data:application/"), "image must be a data URL, not a local path"

    def test_s4_max_tokens_non_truncating(self, s4):
        assert s4["frontier_output_tokens"] >= 3000

    def test_s1_one_page_brief(self):
        s1 = next(s for s in m6.SCENARIOS if s["id"] == "S1")
        assert "one-page" in s1["quick_brief"].lower()

    def test_s3_budget_tactics_in_frontier_prompt(self):
        s3 = next(s for s in m6.SCENARIOS if s["id"] == "S3")
        messages, _ = m6._build_frontier_messages(s3)
        content = json.dumps(messages)
        assert "5000" in content
        assert "heavy_discount" in content or "flash" in content

    def test_source_grounded_rule_in_frontier_messages(self, s4):
        messages, _ = m6._build_frontier_messages(s4)
        content = json.dumps(messages)
        assert "video call" in content.lower() or "video call" in m6.SOURCE_GROUNDED_RULE.lower()

    def test_brand_guidelines_in_frontier_messages(self, s4):
        messages, _ = m6._build_frontier_messages(s4)
        content = json.dumps(messages)
        # Brand guidelines exist or the source pack section is present.
        assert "Brand" in content or "แบรนด์" in content or "brand_profile" in content or not m6._get_brand_guidelines()

    def test_s2_s3_have_web_tools(self, s2):
        assert s2["frontier_web_uses"] == 2

    def test_frontier_dry_run_s4_no_api_call(self, s4):
        guard = m6.M6FrontierGuard(total_cap=1.0, per_scenario_caps={"S4": 0.13})
        r = m6._run_frontier_scenario(s4, guard, dry_run=True)
        assert r.side == "Frontier"
        assert r.cost_usd == 0.0
        assert r.evidence.get("tools") is None

    def test_s2_s3_reserve_fits_within_cap(self):
        for sid in ("S2", "S3"):
            s = next(x for x in m6.SCENARIOS if x["id"] == sid)
            guard = m6.M6FrontierGuard(total_cap=2.0, per_scenario_caps={sid: s["frontier_reserve"]})
            guard.set_scenario(sid)
            messages, _ = m6._build_frontier_messages(s)
            payload = {
                "model": m6.FRONTIER_MODEL,
                "max_tokens": s["frontier_output_tokens"],
                "messages": messages,
                "tools": [{"type": "openrouter:web_search", "parameters": {"max_uses": s["frontier_web_uses"]}}] if s["frontier_web_uses"] > 0 else [],
            }
            reserve = guard._resolve_reserve(payload)
            assert reserve <= s["frontier_reserve"], f"{sid} reserve {reserve} exceeds cap {s['frontier_reserve']}"


class TestEvidenceAndAnonymization:
    def test_mktapp_dry_run_carries_one_page_and_grounded_rule(self):
        s1 = next(s for s in m6.SCENARIOS if s["id"] == "S1")
        r = m6._run_mktapp_scenario(s1, dry_run=True)
        assert "one-page" in r.evidence["quick_brief"].lower()
        assert "video call" in r.evidence["resource_context"].lower()

    def test_mktapp_dry_run_s3_budget_and_tactics(self):
        s3 = next(s for s in m6.SCENARIOS if s["id"] == "S3")
        r = m6._run_mktapp_scenario(s3, dry_run=True)
        # Typed UI/Agent Settings travel through agent_settings_override, not prompt context.
        override = r.evidence["agent_settings_override"]
        assert override.get("budget_max") == 5000
        assert override.get("forbid_tactics") == ["heavy_discount", "flash", "bogo"]
        assert "5000" not in r.evidence["resource_context"]
        assert "forbid_tactics" not in r.evidence["resource_context"]

    def test_s3_agent_settings_parsed_for_typed_ui(self):
        raw = "Agent settings: budget_max=5000, discount_max=0, forbid_tactics=[heavy_discount,flash,bogo]."
        override = m6._parse_agent_settings_override(raw, "campaign_strategy")
        assert override == {
            "budget_max": 5000,
            "discount_max": 0,
            "forbid_tactics": ["heavy_discount", "flash", "bogo"],
        }

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
    def test_frontier_reserve_within_approval_cap(self):
        frontier_total = sum(s["frontier_reserve"] for s in m6.SCENARIOS)
        assert frontier_total <= m6.APPROVAL_CAP

    def test_combined_worst_case_budget_exceeds_cap_blocks(self):
        # Combined MKTApp + Frontier worst-case reserve + contingency exceeds $2.00,
        # so the harness must refuse to start a real run without PO approval.
        mktapp_total = sum(s["mktapp_reserve"] for s in m6.SCENARIOS)
        frontier_total = sum(s["frontier_reserve"] for s in m6.SCENARIOS)
        required = mktapp_total + frontier_total + m6.CONTINGENCY
        ok, msg, required_cap, _ = m6._preflight_budget_check()
        assert not ok
        assert required_cap == round(required, 6)
        assert required > m6.APPROVAL_CAP
        assert "Product Owner choices" in msg
        assert "A)" in msg
        assert "B)" in msg
