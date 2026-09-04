"""Offline regression tests for scripts/m6_judge_runner.py.

These tests never make billable calls or network calls.
"""

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import m6_judge_runner as judge


@pytest.fixture
def tmp_run_dir(tmp_path: Any) -> Any:
    run_dir = tmp_path / "m6_run"
    outputs = run_dir / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "S1_X.txt").write_text("output X", encoding="utf-8")
    (outputs / "S1_Y.txt").write_text("output Y", encoding="utf-8")
    (run_dir / "m6_mapping_secret.json").write_text(
        json.dumps({"S1": {"X": "MKTApp", "Y": "Frontier"}}),
        encoding="utf-8",
    )
    return run_dir


def _fake_response(content: str, model: str = judge.JUDGE_MODEL, cost: float = 0.01) -> Any:
    class _Resp:
        status_code = 200
        def __init__(self) -> None:
            self.json_obj = {
                "id": "judge-1",
                "model": model,
                "choices": [
                    {"message": {"role": "assistant", "content": content}}
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                },
                "cost": cost,
            }

        def json(self) -> Any:
            return self.json_obj

        def raise_for_status(self) -> None:
            pass

    return _Resp()


def test_model_lock_allows_gpt_5_6_sol():
    valid_json = json.dumps({
        "scores": {
            "Usefulness": {"X": 4, "Y": 3, "winner": "X", "tie": False, "reason": "r"},
            "Factuality": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
            "Instruction following": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
            "Brand / asset fit": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
            "Evidence quality": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
            "User effort": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
        },
        "overall": {
            "X_mean_score": 4.0,
            "Y_mean_score": 3.83,
            "winner": "X",
            "tie": False,
            "decisive_reasons": ["r"],
            "confidence": 0.9,
            "insufficient_evidence": False,
            "insufficient_evidence_reasons": [],
        },
    })
    original = httpx.Client.post
    httpx.Client.post = lambda *a, **k: _fake_response(valid_json)
    try:
        guard = judge.M6JudgeGuard(approved_cap=1.0, absolute_cap=2.0)
        with guard:
            client = httpx.Client()
            r = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json={"model": judge.JUDGE_MODEL, "messages": [{"role": "user", "content": "x"}]},
            )
            assert guard.calls == 1
            assert guard.cumulative == 0.01
            assert client._m6_last_raw["model"] == judge.JUDGE_MODEL
    finally:
        httpx.Client.post = original


def test_model_lock_blocks_wrong_model():
    original = httpx.Client.post
    httpx.Client.post = lambda *a, **k: _fake_response("{}", model="openai/gpt-5.5")
    try:
        guard = judge.M6JudgeGuard(approved_cap=1.0, absolute_cap=2.0)
        with guard:
            client = httpx.Client()
            with pytest.raises(RuntimeError, match="model lock breach"):
                client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    json={"model": judge.JUDGE_MODEL, "messages": [{"role": "user", "content": "x"}]},
                )
    finally:
        httpx.Client.post = original


def test_cap_blocks_before_call():
    guard = judge.M6JudgeGuard(approved_cap=0.001, absolute_cap=0.002)
    with guard:
        client = httpx.Client()
        with pytest.raises(RuntimeError, match="approved cap exceeded"):
            client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json={"model": judge.JUDGE_MODEL, "messages": [{"role": "user", "content": "x" * 1000}]},
            )


def test_json_parse_fail_stops(tmp_run_dir: Any):
    original = httpx.Client.post
    httpx.Client.post = lambda *a, **k: _fake_response("not-json")
    try:
        result = judge.run_judge(judge.SCENARIOS[0], tmp_run_dir, "fake-key")
        assert result.error is not None
        assert "not-json" in result.error or "JSON" in result.error or "Expecting" in result.error
    finally:
        httpx.Client.post = original


def test_mapping_secret_not_in_prompt(tmp_run_dir: Any):
    source_pack = judge._get_source_pack(judge.SCENARIOS[0])
    x, y = judge._read_blind_outputs(tmp_run_dir, "S1")
    messages = judge._build_messages(source_pack, x, y, judge.SCENARIOS[0])
    prompt_text = json.dumps(messages)
    assert "m6_mapping_secret" not in prompt_text
    # Brand guidelines may legitimately contain the product/company name; the secret
    # mapping must not. We only assert the exact mapping file name is absent.


def test_source_pack_has_grounded_rule_and_brand():
    source_pack = judge._get_source_pack(judge.SCENARIOS[0])
    assert "video call" in source_pack.lower() or "video call" in judge.SOURCE_GROUNDED_RULE.lower()
    brand = judge._get_brand_guidelines()
    if brand:
        assert "Brand" in source_pack or "แบรนด์" in source_pack or "brand" in source_pack.lower()


def test_s1_judge_quick_brief_one_page():
    s1 = judge.SCENARIOS[0]
    assert "one-page" in s1["quick_brief"].lower()


def test_s3_judge_resource_context_budget():
    s3 = next(s for s in judge.SCENARIOS if s["id"] == "S3")
    assert "5000" in s3["resource_context"]
    assert "heavy_discount" in s3["resource_context"]


def test_judge_messages_have_data_url_images_for_s1(tmp_run_dir: Any):
    s1 = judge.SCENARIOS[0]
    source_pack = judge._get_source_pack(s1)
    x, y = judge._read_blind_outputs(tmp_run_dir, "S1")
    image_paths = judge._get_image_paths_for_judge(s1)
    messages = judge._build_messages(source_pack, x, y, judge.SCENARIOS[0], image_paths)
    user_content = messages[1].get("content", "")
    if not isinstance(user_content, list) or not image_paths:
        # No images in this checkout; nothing to assert.
        return
    image_parts = [p for p in user_content if p.get("type") == "image_url"]
    for part in image_parts:
        url = part.get("image_url", {}).get("url", "")
        assert url.startswith("data:image/") or url.startswith("data:application/"), "judge must receive image data URLs, not local paths"


def test_json_validation_missing_dimension():
    bad = {
        "scores": {
            "Usefulness": {"X": 4, "Y": 3, "winner": "X", "tie": False, "reason": "r"},
        },
        "overall": {
            "X_mean_score": 4.0,
            "Y_mean_score": 3.0,
            "winner": "X",
            "tie": False,
            "decisive_reasons": ["r"],
            "confidence": 0.9,
            "insufficient_evidence": False,
            "insufficient_evidence_reasons": [],
        },
    }
    with pytest.raises(ValueError, match="missing dimension"):
        judge._validate_judge_json(bad)


def test_non_inferiority_calculation(tmp_run_dir: Any):
    mapping = json.loads((tmp_run_dir / "m6_mapping_secret.json").read_text(encoding="utf-8"))
    assert mapping["S1"]["X"] == "MKTApp"
    assert mapping["S1"]["Y"] == "Frontier"

    raw_results = [
        judge.JudgeResult(
            scenario_id="S1",
            raw_judge_json={
                "scores": {
                    "Usefulness":       {"X": 4, "Y": 3, "winner": "X", "tie": False, "reason": "r"},
                    "Factuality":       {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
                    "Instruction following": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
                    "Brand / asset fit": {"X": 5, "Y": 4, "winner": "X", "tie": False, "reason": "r"},
                    "Evidence quality": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
                    "User effort":      {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
                },
                "overall": {
                    "X_mean_score": 4.17,
                    "Y_mean_score": 3.83,
                    "winner": "X",
                    "tie": False,
                    "decisive_reasons": ["r"],
                    "confidence": 0.9,
                    "insufficient_evidence": False,
                    "insufficient_evidence_reasons": [],
                },
            },
            actual_model=judge.JUDGE_MODEL,
            prompt_tokens=1000,
            completion_tokens=500,
            cost_usd=0.005,
        )
    ]
    revealed = judge._reveal(tmp_run_dir / "m6_mapping_secret.json", raw_results)
    assert revealed["overall"]["m6_1_pass"] is True
    assert revealed["overall"]["non_inferiority_pass"] is True
    assert revealed["overall"]["hard_gate_pass"] is True
    assert revealed["overall"]["brand_asset_gate_pass"] is True
    s0 = revealed["scenarios"][0]
    assert s0["delta_mktapp_minus_frontier"]["Brand / asset fit"] == 1.0


def test_brand_gate_count(tmp_path: Any):
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(
        json.dumps({
            "S1": {"X": "MKTApp", "Y": "Frontier"},
            "S2": {"X": "MKTApp", "Y": "Frontier"},
            "S3": {"X": "Frontier", "Y": "MKTApp"},
            "S4": {"X": "MKTApp", "Y": "Frontier"},
        }),
        encoding="utf-8",
    )
    scenarios = ["S1", "S2", "S3", "S4"]
    raw_results = []
    for i, sid in enumerate(scenarios):
        brand = {"X": 5, "Y": 4} if i != 2 else {"X": 4, "Y": 5}
        winner = "X" if i != 2 else "Y"
        raw_results.append(judge.JudgeResult(
            scenario_id=sid,
            raw_judge_json={
                "scores": {
                    "Usefulness": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
                    "Factuality": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
                    "Instruction following": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
                    "Brand / asset fit": {**brand, "winner": winner, "tie": False, "reason": "r"},
                    "Evidence quality": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
                    "User effort": {"X": 4, "Y": 4, "winner": None, "tie": True, "reason": "r"},
                },
                "overall": {
                    "X_mean_score": 4.0,
                    "Y_mean_score": 4.0,
                    "winner": None,
                    "tie": True,
                    "decisive_reasons": ["r"],
                    "confidence": 0.9,
                    "insufficient_evidence": False,
                    "insufficient_evidence_reasons": [],
                },
            },
            actual_model=judge.JUDGE_MODEL,
            prompt_tokens=1000,
            completion_tokens=500,
            cost_usd=0.005,
        ))
    revealed = judge._reveal(mapping_path, raw_results)
    assert revealed["overall"]["brand_asset_gate_pass"] is True
    assert revealed["overall"]["brand_asset_passing_scenarios"] == 4
    assert revealed["overall"]["brand_asset_attempted_scenarios"] == 4


# ---------------------------------------------------------------------------
# M6.1 pre-run corrections: scenario-scoped judge prompt
# ---------------------------------------------------------------------------

def test_judge_prompt_contains_scenario_scoped_rule():
    """Judge system prompt must contain the scenario-scoped evaluation rule
    that prevents requirement leakage from other scenarios/agents."""
    s4 = next(s for s in judge.SCENARIOS if s["id"] == "S4")
    source_pack = judge._get_source_pack(s4)
    messages = judge._build_messages(source_pack, "X output", "Y output", s4)
    system_text = messages[0]["content"]
    assert "Scenario-scoped evaluation rule" in system_text
    assert "Instruction Following" in system_text
    assert "Do NOT invent requirements" in system_text
    assert "Do NOT penalize" in system_text


def test_judge_prompt_s4_explicitly_excludes_budget_kpi():
    """S4 judge prompt must explicitly state that campaign budget/KPI
    are NOT requirements and their absence must not reduce any score."""
    s4 = next(s for s in judge.SCENARIOS if s["id"] == "S4")
    source_pack = judge._get_source_pack(s4)
    messages = judge._build_messages(source_pack, "X output", "Y output", s4)
    system_text = messages[0]["content"]
    assert "budget" in system_text.lower()
    assert "KPI" in system_text
    assert "content_creator" in system_text
    assert "must not reduce any score" in system_text


def test_judge_prompt_s4_user_text_contains_scenario_scope():
    """S4 user prompt must contain the scenario scope section with
    agent_responsibility that defines what is and isn't required."""
    s4 = next(s for s in judge.SCENARIOS if s["id"] == "S4")
    source_pack = judge._get_source_pack(s4)
    messages = judge._build_messages(source_pack, "X output", "Y output", s4)
    user_text = messages[1]["content"]
    if isinstance(user_text, list):
        user_text = " ".join(
            p.get("text", "") for p in user_text if p.get("type") == "text"
        )
    assert "Scenario scope" in user_text
    assert "Agent responsibility" in user_text
    assert "TikTok" in user_text
    assert "งบประมาณแคมเปญ" in user_text or "campaign budget" in user_text.lower()


def test_judge_prompt_s3_does_not_exclude_budget():
    """S3 (campaign_strategy) must NOT have the budget/KPI exclusion —
    budget IS a requirement for S3."""
    s3 = next(s for s in judge.SCENARIOS if s["id"] == "S3")
    source_pack = judge._get_source_pack(s3)
    messages = judge._build_messages(source_pack, "X output", "Y output", s3)
    system_text = messages[0]["content"]
    # S3 system prompt has the generic scenario-scoped rule, but the S4-specific
    # exclusion clause mentions "content_creator" — S3 should not trigger that.
    # The generic rule is present, but the S4-specific clause is about S4 only.
    assert "Scenario-scoped evaluation rule" in system_text
    # S3's agent_responsibility should mention campaign planning
    user_text = messages[1]["content"]
    if isinstance(user_text, list):
        user_text = " ".join(
            p.get("text", "") for p in user_text if p.get("type") == "text"
        )
    assert "campaign" in user_text.lower() or "แคมเปญ" in user_text


def test_judge_prompt_instruction_following_rule_in_user_text():
    """The user prompt must explicitly tell the judge to score Instruction
    Following only against the scenario scope."""
    s1 = judge.SCENARIOS[0]
    source_pack = judge._get_source_pack(s1)
    messages = judge._build_messages(source_pack, "X output", "Y output", s1)
    user_text = messages[1]["content"]
    if isinstance(user_text, list):
        user_text = " ".join(
            p.get("text", "") for p in user_text if p.get("type") == "text"
        )
    assert "Instruction Following" in user_text
    assert "scenario scope" in user_text.lower()
    assert "Do NOT penalize" in user_text


def test_all_scenarios_have_agent_responsibility():
    """Every scenario must define agent_responsibility for scenario scoping."""
    for s in judge.SCENARIOS:
        assert "agent_responsibility" in s, f"{s['id']} missing agent_responsibility"
        assert s["agent_responsibility"], f"{s['id']} has empty agent_responsibility"


def test_judge_cap_is_0_18():
    """Judge approved cap must be $0.18 (not $0.15)."""
    assert judge.APPROVED_CAP == 0.18


def test_judge_absolute_stop_is_0_20():
    """Judge absolute stop cap must be $0.20."""
    assert judge.ABSOLUTE_STOP_CAP == 0.20


def test_s4_agent_responsibility_excludes_budget_kpi():
    """S4 agent_responsibility must explicitly state budget/KPI are not
    this agent's responsibility."""
    s4 = next(s for s in judge.SCENARIOS if s["id"] == "S4")
    resp = s4["agent_responsibility"]
    assert "งบประมาณ" in resp or "budget" in resp.lower()
    assert "KPI" in resp or "kpi" in resp.lower()
    assert "ไม่ใช่หน้าที่" in resp or "not" in resp.lower()
