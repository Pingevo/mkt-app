"""Offline tests for the real-model Beta evaluation harness.

These tests use FakeLLM so no OpenRouter credit is consumed.  They verify
budget caps, resume, critical/medium defect classification, and report
generation without executing real models.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.agents.campaign_strategy import CampaignStrategyAgent
from src.config_loader import get_agent_config, load_config
from src.evaluation.campaign_real_eval import CampaignRealEvalHarness


def _campaign_config():
    cfg = load_config()
    return get_agent_config(cfg, "campaign_strategy")


class FakeLLM:
    """Deterministic LLM that returns a scripted output for every call."""

    def __init__(self, generate_output: str = ""):
        self.generate_output = generate_output
        self.calls: list[dict] = []
        self.last_truncated = False

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            return '{"grounded": true, "unsupported_claims": []}'
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.generate_output

    def close(self):
        pass


def _make_llm(output: str):
    return FakeLLM(output)


def _mini_plan(case_id: str, n_runs: int = 1, est: float = 0.0285):
    return {
        "stages": [
            {
                "stage_id": "1",
                "description": "smoke",
                "cases": [
                    {
                        "case_id": case_id,
                        "n_runs": n_runs,
                        "web_search": False,
                        "estimated_cost_usd": est,
                    }
                ],
            }
        ]
    }


@pytest.fixture
def tmp_paths(tmp_path):
    return {
        "state": tmp_path / "state.json",
        "report": tmp_path / "report.md",
    }


def test_harness_records_all_fields_with_valid_output(tmp_paths):
    """A valid case should be recorded with hashes, latency 0, cost 0, and pass."""
    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor")
    llm = _make_llm(_with_competitor_source())
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_paths["state"],
        report_path=tmp_paths["report"],
        budget_cap=1.0,
        dry_run=False,
    )
    result = harness.run_plan(stage=0)

    assert len(result.stages) == 1
    case = result.stages[0].cases[0]
    assert case.case_id == "B_verified_competitor"
    assert case.pass_count == 1
    assert case.fail_count == 0
    run = case.runs[0]
    assert run.validation_ok
    assert run.prompt_hash
    assert run.config_hash
    assert run.latency_ms >= 0
    assert run.cost_usd == 0.0
    assert run.repair_count == 0
    assert run.error == ""


def test_harness_stops_on_critical_defect(tmp_paths):
    """A critical rule violation stops the stage immediately."""
    from tests.fixtures.campaign_fixtures import CASES

    case = next(c for c in CASES if c["case_id"] == "Q_product_identity_mutation")
    plan = _mini_plan("Q_product_identity_mutation")
    llm = _make_llm(case["output"])
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_paths["state"],
        report_path=tmp_paths["report"],
        budget_cap=1.0,
        dry_run=False,
    )
    result = harness.run_plan(stage=0)

    stage = result.stages[0]
    assert stage.stopped
    assert "product_identity_intact" in stage.stop_reason
    assert stage.cases[0].critical_hits == ["product_identity_intact"]


def test_harness_stops_case_on_medium_defect_but_stage_continues(tmp_paths):
    """A medium rule violation stops the affected case but the stage continues."""
    from tests.fixtures.campaign_fixtures import CASES

    budget_case = next(c for c in CASES if c["case_id"] == "R_budget_wording_bypass")
    pass_case = next(c for c in CASES if c["case_id"] == "B_verified_competitor")
    plan = {
        "stages": [
            {
                "stage_id": "1",
                "description": "mixed",
                "cases": [
                    {
                        "case_id": "R_budget_wording_bypass",
                        "n_runs": 1,
                        "web_search": False,
                        "estimated_cost_usd": 0.0285,
                    },
                    {
                        "case_id": "B_verified_competitor",
                        "n_runs": 1,
                        "web_search": False,
                        "estimated_cost_usd": 0.0285,
                    },
                ],
            }
        ]
    }
    # Use a single FakeLLM; the first call gets the budget output, second gets the pass output.
    # FakeLLM returns the same string every call, so we use the budget output for both.
    # The second case will therefore also fail, which still proves the harness moved on.
    llm = _make_llm(budget_case["output"])
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_paths["state"],
        report_path=tmp_paths["report"],
        budget_cap=1.0,
        dry_run=False,
    )
    result = harness.run_plan(stage=0)

    case_results = result.stages[0].cases
    assert case_results[0].medium_hits == ["budget_max_enforced"]
    assert case_results[0].runs[0].stopped_by_medium
    # The stage did not stop: it moved on and evaluated the second case.
    assert not result.stages[0].stopped
    assert len(case_results) == 2


def test_harness_stops_before_call_when_budget_insufficient(tmp_paths):
    """The harness should refuse to run if the estimated cost exceeds the cap."""
    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor", est=0.05)
    llm = _make_llm(_with_competitor_source())
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_paths["state"],
        report_path=tmp_paths["report"],
        budget_cap=0.01,
        dry_run=False,
    )
    result = harness.run_plan(stage=0)

    run = result.stages[0].cases[0].runs[0]
    assert run.stopped_by_budget
    assert "insufficient budget" in run.error


def test_harness_resume_skips_completed_runs(tmp_paths):
    """If state already contains a completed run, it should not be repeated."""
    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor", n_runs=1)
    llm = _make_llm(_with_competitor_source())
    # First run writes state with the full resume key (mode+model+hashes).
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_paths["state"],
        report_path=tmp_paths["report"],
        budget_cap=1.0,
        dry_run=False,
    )
    harness.run_plan(stage=0)

    # Now ask for 2 runs with the same state: run_1 should be skipped.
    plan = _mini_plan("B_verified_competitor", n_runs=2)
    harness2 = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_paths["state"],
        report_path=tmp_paths["report"],
        budget_cap=1.0,
        dry_run=False,
    )
    result = harness2.run_plan(stage=0)

    case = result.stages[0].cases[0]
    assert case.runs[0].error == "already completed (resume)"
    # The second run should still execute.
    assert case.runs[1].validation_ok


def test_harness_generates_markdown_report(tmp_paths):
    """The markdown report should contain the key human-review sections."""
    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor")
    llm = _make_llm(_with_competitor_source())
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_paths["state"],
        report_path=tmp_paths["report"],
        budget_cap=1.0,
        dry_run=False,
    )
    result = harness.run_plan(stage=0)
    report = harness.generate_report(result, fmt="markdown")

    assert "# Campaign Strategy Real-Model Evaluation Report" in report
    assert "## Stage 1: smoke" in report
    assert "## Recommendation" in report
    assert "B_verified_competitor" in report


def test_free_preflight_forces_openrouter_free_and_no_web_search(tmp_paths):
    """Free preflight must use openrouter/free and never web search."""
    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor")
    llm = _make_llm(_with_competitor_source())
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_paths["state"],
        report_path=tmp_paths["report"],
        budget_cap=0.0,
        dry_run=False,
        mode="free_preflight",
    )
    result = harness.run_plan(stage=0)

    run = result.stages[0].cases[0].runs[0]
    assert run.mode == "free_preflight"
    assert run.model == "openrouter/free"
    assert run.web_search is False


def test_free_preflight_records_infrastructure_limitation(tmp_paths):
    """Free model rate-limit / unsupported tool must not trigger paid fallback."""
    class FakeFreeErrorLLM:
        def chat(self, messages, **kwargs):
            raise RuntimeError("Rate limit exceeded for openrouter/free")

        def close(self):
            pass

    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor")
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=FakeFreeErrorLLM(),
        state_path=tmp_paths["state"],
        report_path=tmp_paths["report"],
        budget_cap=0.0,
        dry_run=False,
        mode="free_preflight",
    )
    result = harness.run_plan(stage=0)

    run = result.stages[0].cases[0].runs[0]
    assert run.model == "openrouter/free"
    assert run.actual_model == "provider_infrastructure_limitation"
    assert "Rate limit" in run.first_validation_error


def test_free_preflight_report_is_not_production_certification(tmp_paths):
    """The markdown report must carry a clear non-certification banner."""
    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor")
    llm = _make_llm(_with_competitor_source())
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_paths["state"],
        report_path=tmp_paths["report"],
        budget_cap=0.0,
        dry_run=False,
        mode="free_preflight",
    )
    result = harness.run_plan(stage=0)
    report = harness.generate_report(result, fmt="markdown")

    assert "FREE PREFLIGHT — NOT PRODUCTION CERTIFICATION" in report
    assert "cannot satisfy the production Beta gate" in report
    assert "Mode: **free_preflight**" in report


def test_pseudo_tool_markup_detected_and_stops_batch(tmp_path):
    """Pseudo-tool markup in free output must be a protocol/format failure and stop the batch."""
    from tests.fixtures.campaign_fixtures import _with_competitor_source
    base_output = _with_competitor_source()
    pseudo_output = (
        base_output
        + "\n\n<tool_call>web_search\n<arg_key>query</arg_key>\n"
        "<arg_value>LAGENIO K2 price</arg_value>\n</tool_call>\n"
    )
    from tests.fixtures.campaign_fixtures import CASES

    case = next(c for c in CASES if c["case_id"] == "B_verified_competitor")
    plan = {
        "stages": [
            {
                "stage_id": "1",
                "description": "pseudo tool",
                "cases": [
                    {
                        "case_id": "B_verified_competitor",
                        "n_runs": 1,
                        "web_search": False,
                        "estimated_cost_usd": 0.0,
                    }
                ],
            }
        ]
    }
    llm = FakeLLM(pseudo_output)
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_path / "state.json",
        report_path=tmp_path / "report.md",
        budget_cap=0.0,
        dry_run=False,
        mode="free_preflight",
        artifact_dir=tmp_path / "eval",
    )
    result = harness.run_plan(stage=0)

    run = result.stages[0].cases[0].runs[0]
    assert run.diagnosis_category == "protocol_or_format_failure"
    assert result.stages[0].cases[0].format_protocol_hits == ["no_pseudo_tool_markup"]
    assert result.stopped
    assert "protocol/format failure" in result.stop_reason


def test_actual_model_unknown_when_no_metadata(tmp_path):
    """If provider response metadata is missing, actual_model must not be the router ID."""
    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor")
    llm = FakeLLM(_with_competitor_source())
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_path / "state.json",
        report_path=tmp_path / "report.md",
        budget_cap=1.0,
        dry_run=False,
        mode="free_preflight",
        artifact_dir=tmp_path / "eval",
    )
    result = harness.run_plan(stage=0)

    run = result.stages[0].cases[0].runs[0]
    assert run.actual_model == "actual_model_unknown"


def test_actual_model_from_provider_metadata(tmp_path):
    """If the LLM client carries response metadata, actual_model must record it."""
    class FakeLLMWithMetadata(FakeLLM):
        _last_raw_response = {
            "model": "google/gemma-2-9b-it:free",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 15,
                "total_tokens": 25,
            },
        }

    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor")
    llm = FakeLLMWithMetadata(_with_competitor_source())
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_path / "state.json",
        report_path=tmp_path / "report.md",
        budget_cap=1.0,
        dry_run=False,
        mode="free_preflight",
        artifact_dir=tmp_path / "eval",
    )
    result = harness.run_plan(stage=0)

    run = result.stages[0].cases[0].runs[0]
    assert run.actual_model == "google/gemma-2-9b-it:free"
    assert run.raw_provider_metadata.get("model") == "google/gemma-2-9b-it:free"


def test_partial_plan_report_integrity():
    """A partial plan must be reported as partial, not as a full Stage 1."""
    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = {
        "name": "partial probe",
        "plan_kind": "free_preflight_partial_probe",
        "partial": True,
        "partial_reason": "Session-time minimal probe",
        "stages": [
            {
                "stage_id": "1",
                "description": "partial",
                "cases": [
                    {
                        "case_id": "B_verified_competitor",
                        "n_runs": 1,
                        "web_search": False,
                        "estimated_cost_usd": 0.0,
                    }
                ],
            }
        ],
    }
    llm = FakeLLM(_with_competitor_source())
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=Path("/tmp/dummy_state4.json"),
        report_path=Path("/tmp/dummy_report4.md"),
        budget_cap=0.0,
        dry_run=False,
        mode="free_preflight",
        artifact_dir=Path("/tmp/dummy_eval_artifacts4"),
    )
    result = harness.run_plan(stage=0)
    report = harness.generate_report(result, fmt="markdown")

    assert result.partial is True
    assert result.plan_kind == "free_preflight_partial_probe"
    assert "Partial run: **yes**" in report
    assert "Session-time minimal probe" in report
    assert "Planned runs: 1" in report
    assert "Executed runs: 1" in report


def test_output_artifact_saved_and_reported():
    """Full output must be written to an artifact file and linked from the report."""
    import tempfile
    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor")
    llm = FakeLLM(_with_competitor_source())
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        harness = CampaignRealEvalHarness(
            plan=plan,
            llm_client=llm,
            state_path=td_path / "state.json",
            report_path=td_path / "report.md",
            budget_cap=1.0,
            dry_run=False,
            mode="free_preflight",
            artifact_dir=td_path / "eval",
        )
        result = harness.run_plan(stage=0)
        run = result.stages[0].cases[0].runs[0]

        assert run.output_artifact_path
        artifact = Path(__file__).resolve().parent.parent / run.output_artifact_path
        assert artifact.exists()
        report = harness.generate_report(result, fmt="markdown")
        assert f"`{run.output_artifact_path}`" in report


def test_all_validator_verdicts_recorded(tmp_path):
    """A run must record every rule verdict, not just the first failure."""
    from tests.fixtures.campaign_fixtures import _with_competitor_source

    plan = _mini_plan("B_verified_competitor")
    llm = FakeLLM(_with_competitor_source())
    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=tmp_path / "state.json",
        report_path=tmp_path / "report.md",
        budget_cap=1.0,
        dry_run=False,
        mode="free_preflight",
        artifact_dir=tmp_path / "eval",
    )
    result = harness.run_plan(stage=0)
    run = result.stages[0].cases[0].runs[0]

    assert run.all_validation_errors
    rules = {v["rule"] for v in run.all_validation_errors}
    assert "product_identity_intact" in rules
    assert "no_bare_urls" in rules
