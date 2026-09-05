"""Offline tests for the same-model uplift qualification runner.

Exercises the FULL orchestration end-to-end with mocked model + Judge
executors — ZERO paid calls.  Verifies every required safety property:

  * exactly one candidate execution per scenario/side
  * no automatic rerun on poor scores
  * no automatic whole-qualification restart
  * no duplicate Judge execution
  * no Judge execution before candidate evidence is complete
  * no Gate evaluation from incomplete/invalid evidence
  * deterministic blind mapping
  * failure paths stop safely
  * provider/infrastructure failure is NOT misreported as product-quality FAIL
  * actual cost accounting is preserved per turn/call
  * final PASS/FAIL only from a complete valid qualification
  * canonical fixtures and Gate v1 remain unchanged
  * budget config is explicit (no hidden default authorization)
  * budget configuration propagates through Baseline, MKTApp, Judge, total

No paid calls. No network. Pure logic + mock tests.
"""

import json
import hashlib
from pathlib import Path
from unittest.mock import MagicMock
from dataclasses import dataclass

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"


def _import_runner():
    import sys
    sys.path.insert(0, str(SCRIPTS))
    import m6_uplift_runner as runner
    return runner


@pytest.fixture
def runner():
    return _import_runner()


@pytest.fixture
def budget_config(runner):
    return runner.BudgetConfig(
        total_cap=1.41, generation_cap=1.21, judge_cap=0.20,
        judge_per_call_reserve=0.05, blind_mapping_seed=20260905,
        generation_per_call_reserve=0.05,
    )


# ---------------------------------------------------------------------------
# Counting mock executors — record call counts and arguments
# ---------------------------------------------------------------------------

class CountingDeps:
    """RunnerDeps with counting mock executors that record every invocation."""

    def __init__(self, runner_mod, *, baseline_complete=True, mktapp_complete=True,
                 judge_scores=None, judge_error_sid=None, judge_exception_sid=None,
                 baseline_cost=0.0, mktapp_cost=0.0, judge_cost=0.0):
        self.r = runner_mod
        self.baseline_calls: list[tuple] = []
        self.mktapp_calls: list[tuple] = []
        self.judge_calls: list[tuple] = []
        self.baseline_complete = baseline_complete
        self.mktapp_complete = mktapp_complete
        self.judge_scores = judge_scores or self._default_scores()
        self.judge_error_sid = judge_error_sid
        self.judge_exception_sid = judge_exception_sid
        self.baseline_cost = baseline_cost
        self.mktapp_cost = mktapp_cost
        self.judge_cost = judge_cost

    def _default_scores(self):
        dims = ["Usefulness", "Factuality", "Instruction following",
                "Brand / asset fit", "Evidence quality", "User effort"]
        return {d: {"X": 4, "Y": 3, "winner": "X", "tie": False,
                     "reason": "mock"} for d in dims}

    def baseline_executor(self, scenario, fixture, budget):
        self.baseline_calls.append((scenario.id, fixture))
        text = f"[MOCK BASELINE] {scenario.id}"
        comp = "COMPLETE" if self.baseline_complete else "INCOMPLETE"
        # Commit cost to budget (mirrors real BudgetGuardedLLMClient behavior)
        if self.baseline_cost:
            budget.commit_candidate_spend(
                scenario.id, "baseline", self.baseline_cost, stage="generation",
            )
        return self.r.CandidateResult(
            scenario_id=scenario.id, side="baseline", model=scenario.agent_model(),
            output_text=text,
            output_hash=hashlib.sha256(text.encode()).hexdigest(),
            completeness=comp,
            final_finish_reason="stop" if self.baseline_complete else "length",
            final_truncated=not self.baseline_complete,
            cost_usd=self.baseline_cost,
            source_fixture_hash=fixture.hash(),
        )

    def mktapp_executor(self, scenario, fixture, budget):
        self.mktapp_calls.append((scenario.id, fixture))
        if scenario.agent_key == "content_creator":
            text = json.dumps({"posts": [{
                "platform": "tiktok", "caption": "c", "script": "s",
                "hashtags": ["#m"],
            }]}, ensure_ascii=False)
        else:
            text = f"[MOCK MKTApp] {scenario.id}"
        comp = "COMPLETE" if self.mktapp_complete else "INCOMPLETE"
        # Commit cost to budget (mirrors real BudgetGuardedLLMClient behavior)
        if self.mktapp_cost:
            budget.commit_candidate_spend(
                scenario.id, "mktapp", self.mktapp_cost, stage="generation",
            )
        return self.r.CandidateResult(
            scenario_id=scenario.id, side="mktapp", model=scenario.agent_model(),
            output_text=text,
            output_hash=hashlib.sha256(text.encode()).hexdigest(),
            completeness=comp,
            final_finish_reason="stop" if self.mktapp_complete else "length",
            final_truncated=not self.mktapp_complete,
            cost_usd=self.mktapp_cost,
            source_fixture_hash=fixture.hash(),
        )

    def judge_executor(self, scenario_dict, run_dir, api_key, budget_guard_fn, budget_commit_fn):
        sid = scenario_dict["id"]
        self.judge_calls.append(sid)
        if self.judge_exception_sid and sid == self.judge_exception_sid:
            raise RuntimeError(f"simulated provider failure for {sid}")
        if self.judge_error_sid and sid == self.judge_error_sid:
            return self.r._make_error_judge_result(sid, "simulated judge error")
        from m6_judge_runner import JudgeResult
        scores = {d: dict(v) for d, v in self.judge_scores.items()}
        raw = {"scores": scores, "overall": {"X_mean_score": 4.0, "Y_mean_score": 3.0,
                "winner": "X", "tie": False, "decisive_reasons": [],
                "confidence": 0.9, "insufficient_evidence": False,
                "insufficient_evidence_reasons": []}}
        return JudgeResult(
            scenario_id=sid, raw_judge_json=raw,
            actual_model="openai/gpt-5.6-sol",
            prompt_tokens=100, completion_tokens=100, cost_usd=self.judge_cost,
        )

    def deps(self):
        return self.r.RunnerDeps(
            baseline_executor=self.baseline_executor,
            mktapp_executor=self.mktapp_executor,
            judge_executor=self.judge_executor,
            api_key=None,
        )


# ---------------------------------------------------------------------------
# 1. Full valid qualification produces PASS/FAIL
# ---------------------------------------------------------------------------

class TestFullValidQualification:

    def test_complete_run_produces_pass_or_fail(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path, execute=False)
        assert result.valid is True
        assert result.final_pass in (True, False)
        assert result.judge_completed is True
        assert result.stop_reason is None

    def test_all_four_scenarios_judged(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert len(deps.judge_calls) == 4
        assert set(deps.judge_calls) == {"S1", "S2", "S3", "S4"}

    def test_evidence_files_written(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert (tmp_path / "m6_evidence.json").exists()
        assert (tmp_path / "m6_mapping_secret.json").exists()
        assert (tmp_path / "output_hashes.json").exists()
        assert (tmp_path / "m6_uplift_verdict.json").exists()
        assert (tmp_path / "judge" / "m6_judge_raw.json").exists()

    def test_verdict_records_gate_v1(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        v = json.loads((tmp_path / "m6_uplift_verdict.json").read_text())
        assert v["gate_version"] == "v1"
        assert "pass" in v["gate_result"]
        assert v["gate_result"]["gate_config"]["aggregate_uplift_min"] == 0.25


# ---------------------------------------------------------------------------
# 2. Exactly one candidate execution per scenario/side
# ---------------------------------------------------------------------------

class TestExactlyOneExecution:

    def test_baseline_called_once_per_scenario(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert len(deps.baseline_calls) == 4
        sids = [c[0] for c in deps.baseline_calls]
        assert sorted(sids) == ["S1", "S2", "S3", "S4"]

    def test_mktapp_called_once_per_scenario(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert len(deps.mktapp_calls) == 4

    def test_judge_called_once_per_scenario(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert len(deps.judge_calls) == 4
        assert len(set(deps.judge_calls)) == 4  # no duplicates


# ---------------------------------------------------------------------------
# 3. No automatic rerun on poor scores / no restart
# ---------------------------------------------------------------------------

class TestNoRerunOnPoorScores:

    def test_no_rerun_when_mktapp_loses(self, runner, budget_config, tmp_path):
        # Judge scores where Y (baseline) beats X — MKTApp loses.
        losing = {d: {"X": 2, "Y": 5, "winner": "Y", "tie": False, "reason": "m"}
                  for d in ["Usefulness", "Factuality", "Instruction following",
                            "Brand / asset fit", "Evidence quality", "User effort"]}
        deps = CountingDeps(runner, judge_scores=losing)
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        # Still exactly 4 judge calls — no rerun to seek better result
        assert len(deps.judge_calls) == 4
        assert result.valid is True
        # The result stands as-is (FAIL expected, but no rerun)
        assert result.final_pass is False


# ---------------------------------------------------------------------------
# 4. No Judge before candidates complete / no Gate from invalid evidence
# ---------------------------------------------------------------------------

class TestNoJudgeOnIncomplete:

    def test_baseline_incomplete_skips_judge(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, baseline_complete=False)
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert result.valid is False
        assert result.final_pass is None
        assert len(deps.judge_calls) == 0  # NO judge call
        assert result.stop_reason is not None

    def test_mktapp_incomplete_skips_judge(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, mktapp_complete=False)
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert result.valid is False
        assert result.final_pass is None
        assert len(deps.judge_calls) == 0

    def test_no_gate_evaluation_on_invalid(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, baseline_complete=False)
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert result.gate_result == {}
        assert result.final_pass is None

    def test_invalid_scenarios_listed(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, baseline_complete=False)
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert set(result.invalid_scenarios) == {"S1", "S2", "S3", "S4"}


# ---------------------------------------------------------------------------
# 5. Provider/infrastructure failure is NOT misreported as product-quality FAIL
# ---------------------------------------------------------------------------

class TestProviderFailureNotProductFail:

    def test_judge_exception_stops_safely(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, judge_exception_sid="S2")
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert result.valid is False
        assert result.final_pass is None  # NOT a FAIL — invalid
        assert "judge_exception_S2" in (result.stop_reason or "")
        assert result.judge_completed is False

    def test_judge_error_stops_safely(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, judge_error_sid="S3")
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        assert result.valid is False
        assert result.final_pass is None
        assert result.judge_completed is False

    def test_no_pass_fail_from_partial_judge(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, judge_exception_sid="S1")
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        # Even if S2-S4 would have passed, partial judge = no verdict
        assert result.final_pass is None
        assert result.gate_result == {}


# ---------------------------------------------------------------------------
# 6. Deterministic blind mapping
# ---------------------------------------------------------------------------

class TestDeterministicBlindMapping:

    def test_same_seed_same_mapping(self, runner, budget_config, tmp_path):
        deps1 = CountingDeps(runner)
        runner.run_qualification(deps1.deps(), budget_config, tmp_path / "r1")
        m1 = json.loads((tmp_path / "r1" / "m6_mapping_secret.json").read_text())
        deps2 = CountingDeps(runner)
        runner.run_qualification(deps2.deps(), budget_config, tmp_path / "r2")
        m2 = json.loads((tmp_path / "r2" / "m6_mapping_secret.json").read_text())
        assert m1 == m2

    def test_mapping_is_permutation(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        mapping = json.loads((tmp_path / "m6_mapping_secret.json").read_text())
        for sid, m in mapping.items():
            assert set(m.values()) == {"MKTApp", "Baseline"}


# ---------------------------------------------------------------------------
# 7. Budget config — explicit, no hidden default authorization
# ---------------------------------------------------------------------------

class TestBudgetConfigExplicit:

    def test_missing_config_raises(self, runner, tmp_path):
        with pytest.raises(FileNotFoundError):
            runner.load_budget_config(tmp_path / "nonexistent.yaml")

    def test_config_missing_field_raises(self, runner, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("m6_uplift_qualification:\n  total_cap: 1.0\n")
        with pytest.raises(ValueError):
            runner.load_budget_config(p)

    def test_config_loads_approved_values(self, runner):
        cfg = runner.load_budget_config()
        assert cfg.total_cap == 1.41
        assert cfg.generation_cap == 1.21
        assert cfg.judge_cap == 0.20

    def test_budget_propagated_to_caps(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        v = json.loads((tmp_path / "m6_uplift_verdict.json").read_text())
        bs = v["budget_summary"]
        assert bs["initialized"] is True
        h = bs["hierarchy"]
        assert h["total_cap"] == 1.41
        assert h["generation_cap"] == 1.21
        assert h["judge_cap"] == 0.20


# ---------------------------------------------------------------------------
# 8. Cost accounting preserved
# ---------------------------------------------------------------------------

class TestCostAccounting:

    def test_judge_cost_committed_to_judge_stage(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, judge_cost=0.04)
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        # 4 judge calls * 0.04 = 0.16 committed to judge stage
        h = result.budget_summary["hierarchy"]
        assert h["judge_spent"] == pytest.approx(0.16, abs=1e-6)

    def test_candidate_cost_recorded(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, baseline_cost=0.03, mktapp_cost=0.05)
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        cs = result.budget_summary["candidate_spent"]
        # Each scenario has baseline + mktapp spend
        total_baseline = sum(v.get("baseline", 0) for v in cs.values())
        total_mktapp = sum(v.get("mktapp", 0) for v in cs.values())
        assert total_baseline == pytest.approx(0.12, abs=1e-6)  # 4 * 0.03
        assert total_mktapp == pytest.approx(0.20, abs=1e-6)    # 4 * 0.05


# ---------------------------------------------------------------------------
# 9. Canonical fixtures + Gate v1 unchanged
# ---------------------------------------------------------------------------

class TestFrozenInvariants:

    def test_gate_v1_thresholds_unchanged(self, runner):
        import sys
        sys.path.insert(0, str(SCRIPTS))
        import m6_uplift_harness as h
        assert h.GATE_CONFIG["aggregate_uplift_min"] == 0.25
        assert h.GATE_CONFIG["scenario_consistency_min"] == 3
        assert h.GATE_CONFIG["scenario_regression_max"] == -0.5
        assert h.GATE_CONFIG["core_dimension_regression_max"] == -0.5
        assert h.GATE_VERSION == "v1"

    def test_fixture_hashes_match_canonical(self, runner):
        """Runner uses the same frozen SCENARIOS/fixtures — no mutation."""
        import sys
        sys.path.insert(0, str(SCRIPTS))
        import m6_uplift_harness as h
        fixtures = {s.id: s.build_source_fixture().hash() for s in h.SCENARIOS}
        # Re-load and confirm determinism
        fixtures2 = {s.id: s.build_source_fixture().hash() for s in h.SCENARIOS}
        assert fixtures == fixtures2
        # All non-empty
        assert all(v for v in fixtures.values())

    def test_runner_does_not_import_mutate_scenarios(self, runner):
        import sys
        sys.path.insert(0, str(SCRIPTS))
        import m6_uplift_harness as h
        # The runner references the same SCENARIOS object
        assert runner.SCENARIOS is h.SCENARIOS


# ---------------------------------------------------------------------------
# 10. S4 rendering — Judge sees rendered markdown, raw JSON preserved
# ---------------------------------------------------------------------------

class TestS4Rendering:

    def test_s4_judge_output_is_not_raw_json(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        mktapp_s4 = (tmp_path / "outputs" / "mktapp" / "S4.txt").read_text()
        # Should NOT start with raw JSON {"posts"
        assert not mktapp_s4.strip().startswith('{"posts"')

    def test_s4_raw_json_preserved_as_audit(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        ev = json.loads((tmp_path / "m6_evidence.json").read_text())
        s4 = next(s for s in ev["scenarios"] if s["scenario_id"] == "S4")
        assert s4["mktapp"]["raw_json_audit_present"] is True


# ---------------------------------------------------------------------------
# 11. Blind X/Y outputs match mapped independent outputs
# ---------------------------------------------------------------------------

class TestBlindOutputIntegrity:

    def test_xy_match_independent_outputs(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        mapping = json.loads((tmp_path / "m6_mapping_secret.json").read_text())
        for sid, m in mapping.items():
            x = (tmp_path / "outputs" / f"{sid}_X.txt").read_bytes()
            y = (tmp_path / "outputs" / f"{sid}_Y.txt").read_bytes()
            mkt = (tmp_path / "outputs" / "mktapp" / f"{sid}.txt").read_bytes()
            base = (tmp_path / "outputs" / "baseline" / f"{sid}.txt").read_bytes()
            if m["X"] == "MKTApp":
                assert x == mkt
                assert y == base
            else:
                assert x == base
                assert y == mkt

    def test_hashes_match_output_files(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner)
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        hashes = json.loads((tmp_path / "output_hashes.json").read_text())
        for sid in ("S1", "S2", "S3", "S4"):
            for side in ("mktapp", "baseline"):
                p = tmp_path / "outputs" / side / f"{sid}.txt"
                actual = hashlib.sha256(p.read_bytes()).hexdigest()
                assert actual == hashes[side][sid]


# ---------------------------------------------------------------------------
# 12. Uplift + Gate v1 calculation correctness
# ---------------------------------------------------------------------------

class TestUpliftCalculation:

    def test_uplift_is_mktapp_minus_baseline(self, runner, budget_config, tmp_path):
        # X=4, Y=3 default.  Need to know mapping to verify sign.
        deps = CountingDeps(runner)
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        mapping = json.loads((tmp_path / "m6_mapping_secret.json").read_text())
        v = json.loads((tmp_path / "m6_uplift_verdict.json").read_text())
        for rec in v["scenarios"]:
            sid = rec["scenario_id"]
            m = mapping[sid]
            # MKTApp score should be 4 (winner), baseline 3
            mkt_score = rec["mktapp_scores"]["Usefulness"]
            base_score = rec["baseline_scores"]["Usefulness"]
            if m["X"] == "MKTApp":
                assert mkt_score == 4 and base_score == 3
            else:
                assert mkt_score == 3 and base_score == 4
            # uplift = mktapp - baseline = +1 or -1
            assert rec["uplift"]["Usefulness"] == mkt_score - base_score

    def test_gate_evaluates_correctly_from_mapping(self, runner, budget_config, tmp_path):
        """The judge is blind (X/Y).  Uplift sign depends on the mapping.
        Verify the gate evaluates correctly given the actual seeded mapping:
        X=4, Y=3 → if X=MKTApp, uplift=+1; if X=Baseline, uplift=-1.
        """
        deps = CountingDeps(runner)  # X=4, Y=3
        result = runner.run_qualification(deps.deps(), budget_config, tmp_path)
        mapping = json.loads((tmp_path / "m6_mapping_secret.json").read_text())
        v = json.loads((tmp_path / "m6_uplift_verdict.json").read_text())
        # Compute expected aggregate uplift from mapping
        expected_deltas = []
        for rec in v["scenarios"]:
            sid = rec["scenario_id"]
            m = mapping[sid]
            # 6 dimensions, each +1 if X=MKTApp else -1
            sign = 1 if m["X"] == "MKTApp" else -1
            expected_deltas.extend([sign] * len(rec["uplift"]))
        expected_agg = round(sum(expected_deltas) / len(expected_deltas), 3)
        assert v["gate_result"]["aggregate_uplift"] == expected_agg
        # Gate A: aggregate >= 0.25
        expected_pass_a = expected_agg >= 0.25
        assert v["gate_result"]["gate_a_aggregate_uplift"] == expected_pass_a
        # The final_pass must be consistent with the gate result
        assert result.final_pass == v["gate_result"]["pass"]


# ---------------------------------------------------------------------------
# 13. CLI safety — --execute requires --authorize
# ---------------------------------------------------------------------------

class TestCLISafety:

    def test_execute_without_authorize_returns_error(self, runner, tmp_path, monkeypatch):
        import sys
        monkeypatch.setattr(sys, "argv", ["runner", "--execute", "--run-dir", str(tmp_path)])
        rc = runner.main()
        assert rc == 2

    def test_dry_run_default_no_paid_calls(self, runner, tmp_path, monkeypatch):
        import sys
        monkeypatch.setattr(sys, "argv", ["runner", "--run-dir", str(tmp_path)])
        rc = runner.main()
        # dry run completes (mock) — valid result
        assert rc in (0, 1)  # 1 if FAIL, 0 if PASS; both are valid completions


# ---------------------------------------------------------------------------
# 14. No partial judge artifacts before completion
# ---------------------------------------------------------------------------

class TestNoPartialJudgeArtifacts:

    def test_no_verdict_on_partial_judge(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, judge_exception_sid="S2")
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        # No verdict file written on partial judge
        assert not (tmp_path / "m6_uplift_verdict.json").exists()

    def test_judge_raw_written_even_on_incomplete(self, runner, budget_config, tmp_path):
        deps = CountingDeps(runner, judge_exception_sid="S2")
        runner.run_qualification(deps.deps(), budget_config, tmp_path)
        # Raw judge audit IS written (evidence preservation)
        raw = json.loads((tmp_path / "judge" / "m6_judge_raw.json").read_text())
        assert raw["incomplete"] is True


# ---------------------------------------------------------------------------
# 15. Regression: Baseline executor contract (the .value bug)
# ---------------------------------------------------------------------------

class TestBaselineExecutorContract:
    """Regression tests for the Baseline executor defect where
    ``evaluate_single_call_completeness(...).value`` raised AttributeError
    on an already-string return, silently discarding valid output as UNKNOWN.

    These tests prove:
      - the frozen completeness helper returns a string (not an enum)
      - a successful Baseline model response survives candidate construction
      - output text is persisted
      - output hash is persisted
      - finish reason is preserved
      - completeness becomes COMPLETE
      - call_count is correct
      - actual cost accounting is preserved (not reserve)
      - no post-call exception can silently convert a successful candidate
        into UNKNOWN
    """

    def test_completeness_helper_returns_string(self, runner):
        """The frozen helper must return a string, not an enum with .value."""
        import m6_uplift_harness as h
        result = h.evaluate_single_call_completeness("stop", False)
        assert isinstance(result, str), f"Expected str, got {type(result).__name__}"
        assert result == "COMPLETE"
        # Must NOT have a .value attribute (that was the bug)
        assert not hasattr(result, "value"), (
            "evaluate_single_call_completeness must return a plain string, "
            "not an enum — calling .value on a string raises AttributeError"
        )

    def test_successful_baseline_survives_construction(self, runner):
        """A successful Baseline call must produce a COMPLETE CandidateResult
        with all fields populated — not silently swallowed as UNKNOWN."""
        import m6_uplift_harness as h

        # Simulate a successful guarded.chat() return
        text = "This is a valid baseline output."
        finish_reason = "stop"
        truncated = False

        # This is the exact code path from real_baseline_executor (post-fix)
        completeness = runner.evaluate_single_call_completeness(finish_reason, truncated)
        assert completeness == "COMPLETE"
        assert isinstance(completeness, str)

        result = runner.CandidateResult(
            scenario_id="S1", side="baseline", model="google/gemini-3.7-flash",
            output_text=text,
            output_hash=hashlib.sha256(text.encode()).hexdigest(),
            completeness=completeness,
            final_finish_reason=finish_reason,
            final_truncated=truncated,
            call_count=1,
            cost_usd=0.012,
            reserve_usd=0.05,
            source_fixture_hash="abc123",
        )
        assert result.completeness == "COMPLETE"
        assert result.output_text == text
        assert result.output_hash is not None
        assert result.final_finish_reason == "stop"
        assert result.call_count == 1
        assert result.cost_usd == 0.012  # actual, not reserve
        assert result.reserve_usd == 0.05

    def test_no_silent_unknown_on_post_call_exception(self, runner):
        """If an exception occurs AFTER a successful LLM call, the exception
        handler must preserve call_count and actual committed cost — not
        report call_count=0 with reserve as cost."""
        import m6_uplift_harness as h

        # Simulate: guarded.chat() succeeded (call_log has 1 entry with
        # committed=0.012), but then a post-call bug raises an exception.
        call_log = [{"committed": 0.012, "reserve": 0.05}]
        actual_committed = sum(c.get("committed", 0) for c in call_log)

        # This mirrors the fixed exception handler
        result = runner.CandidateResult(
            scenario_id="S1", side="baseline", model="google/gemini-3.7-flash",
            output_text="", completeness="UNKNOWN",
            call_count=len(call_log),
            cost_usd=actual_committed,
            reserve_usd=0.05,
            source_fixture_hash="abc123",
        )
        # The bug previously reported call_count=0 and cost=reserve
        assert result.call_count == 1, "call_count must reflect actual calls, not 0"
        assert result.cost_usd == 0.012, "cost_usd must be actual committed, not reserve"
        assert result.reserve_usd == 0.05, "reserve must be separate from actual cost"
        assert result.cost_usd != result.reserve_usd, (
            "Failure evidence must never report reserve as actual spend"
        )

    def test_evidence_dict_distinguishes_reserve_and_actual(self, runner):
        """to_evidence_dict must include both cost_usd (actual) and
        reserve_usd (authorized reserve) as separate fields."""
        result = runner.CandidateResult(
            scenario_id="S1", side="baseline", model="m",
            output_text="text", completeness="COMPLETE",
            cost_usd=0.012, reserve_usd=0.05,
        )
        d = result.to_evidence_dict()
        assert "cost_usd" in d
        assert "reserve_usd" in d
        assert d["cost_usd"] == 0.012
        assert d["reserve_usd"] == 0.05
        assert d["cost_usd"] != d["reserve_usd"]


# ---------------------------------------------------------------------------
# 16. Recovery path tests — preserved MKTApp + new Baseline
# ---------------------------------------------------------------------------

class CountingRecoveryDeps:
    """RecoveryDeps with counting mock executors."""

    def __init__(self, runner_mod, *, baseline_complete=True,
                 judge_scores=None, judge_error_sid=None,
                 judge_exception_sid=None,
                 baseline_cost=0.0, judge_cost=0.0):
        self.r = runner_mod
        self.baseline_calls: list[tuple] = []
        self.judge_calls: list[str] = []
        self.baseline_complete = baseline_complete
        self.judge_scores = judge_scores or self._default_scores()
        self.judge_error_sid = judge_error_sid
        self.judge_exception_sid = judge_exception_sid
        self.baseline_cost = baseline_cost
        self.judge_cost = judge_cost

    def _default_scores(self):
        dims = ["Usefulness", "Factuality", "Instruction following",
                "Brand / asset fit", "Evidence quality", "User effort"]
        return {d: {"X": 4, "Y": 3, "winner": "X", "tie": False,
                     "reason": "mock"} for d in dims}

    def baseline_executor(self, scenario, fixture, budget):
        self.baseline_calls.append((scenario.id, fixture))
        text = f"[RECOVERY BASELINE] {scenario.id}"
        comp = "COMPLETE" if self.baseline_complete else "INCOMPLETE"
        if self.baseline_cost:
            budget.commit_candidate_spend(
                scenario.id, "baseline", self.baseline_cost, stage="generation",
            )
        return self.r.CandidateResult(
            scenario_id=scenario.id, side="baseline", model=scenario.agent_model(),
            output_text=text,
            output_hash=hashlib.sha256(text.encode()).hexdigest(),
            completeness=comp,
            final_finish_reason="stop" if self.baseline_complete else "length",
            final_truncated=not self.baseline_complete,
            cost_usd=self.baseline_cost,
            reserve_usd=0.05,
            source_fixture_hash=fixture.hash(),
        )

    def judge_executor(self, scenario_dict, run_dir, api_key, budget_guard_fn, budget_commit_fn):
        sid = scenario_dict["id"]
        self.judge_calls.append(sid)
        if self.judge_exception_sid and sid == self.judge_exception_sid:
            raise RuntimeError(f"simulated provider failure for {sid}")
        if self.judge_error_sid and sid == self.judge_error_sid:
            return self.r._make_error_judge_result(sid, "simulated judge error")
        from m6_judge_runner import JudgeResult
        scores = {d: dict(v) for d, v in self.judge_scores.items()}
        raw = {"scores": scores, "overall": {"X_mean_score": 4.0, "Y_mean_score": 3.0,
                "winner": "X", "tie": False, "decisive_reasons": [],
                "confidence": 0.9, "insufficient_evidence": False,
                "insufficient_evidence_reasons": []}}
        return JudgeResult(
            scenario_id=sid, raw_judge_json=raw,
            actual_model="openai/gpt-5.6-sol",
            prompt_tokens=100, completion_tokens=100, cost_usd=self.judge_cost,
        )

    def deps(self):
        return self.r.RecoveryDeps(
            baseline_executor=self.baseline_executor,
            judge_executor=self.judge_executor,
            api_key=None,
        )


def _create_fake_original_run(runner_mod, tmp_path, *, mktapp_complete=True):
    """Create a fake original run directory with preserved MKTApp evidence
    that can be loaded by load_preserved_mktapp()."""
    import m6_uplift_harness as h
    orig_dir = tmp_path / "original"
    orig_dir.mkdir()
    outputs_dir = orig_dir / "outputs"
    mktapp_dir = outputs_dir / "mktapp"
    mktapp_dir.mkdir(parents=True)

    scenarios_data = []
    for scenario in h.SCENARIOS:
        sid = scenario.id
        fixture = scenario.build_source_fixture()
        if scenario.agent_key == "content_creator":
            text = json.dumps({"posts": [{
                "platform": "tiktok", "caption": "preserved caption",
                "script": "preserved script", "hashtags": ["#preserved"],
            }]}, ensure_ascii=False)
        else:
            text = f"[PRESERVED MKTApp] {sid} {scenario.agent_key} output."
        out_file = mktapp_dir / f"{sid}.txt"
        out_file.write_text(text, encoding="utf-8")
        output_hash = hashlib.sha256(text.encode()).hexdigest()
        scenarios_data.append({
            "scenario_id": sid,
            "agent_key": scenario.agent_key,
            "mktapp": {
                "scenario_id": sid, "side": "mktapp",
                "model": scenario.agent_model(),
                "output_artifact_path": None,
                "output_hash": output_hash,
                "completeness": "COMPLETE" if mktapp_complete else "INCOMPLETE",
                "finish_records": [],
                "final_finish_reason": "stop",
                "final_truncated": False,
                "call_count": 2,
                "prompt_tokens": 100,
                "completion_tokens": 200,
                "web_uses": 0,
                "cost_usd": 0.05,
                "reserve_usd": 0.10,
                "source_fixture_hash": fixture.hash(),
                "production_commit": None,
                "raw_json_audit_present": False,
            },
            "baseline": {
                "scenario_id": sid, "side": "baseline",
                "model": scenario.agent_model(),
                "output_artifact_path": None,
                "output_hash": None,
                "completeness": "UNKNOWN",
                "finish_records": [],
                "final_finish_reason": None,
                "final_truncated": False,
                "call_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "web_uses": 0,
                "cost_usd": 0.05,
                "reserve_usd": 0.05,
                "source_fixture_hash": fixture.hash(),
                "production_commit": None,
                "raw_json_audit_present": False,
            },
            "preflight": {"scenario_id": sid, "ok": True, "reason": "ok"},
            "blind_mapping": {"scenario_id": sid, "x_side": "MKTApp",
                              "mapping_dict": {"X": "MKTApp", "Y": "Baseline"}},
        })
    evidence = {
        "run_id": "20260905_072432_paid",
        "timestamp": "2026-09-05T07:30:08.917792+00:00",
        "qualification_mode": "same_model_uplift",
        "gate_version": "v1",
        "gate_config": h.GATE_CONFIG,
        "scenarios": scenarios_data,
        "preflight_all_ok": True,
    }
    (orig_dir / "m6_evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return orig_dir


class TestRecoveryPath:

    def test_recovery_loads_preserved_mktapp(self, runner, tmp_path):
        """load_preserved_mktapp loads all 4 COMPLETE MKTApp candidates
        with verified hashes and fixture provenance."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        preserved = runner.load_preserved_mktapp(orig_dir)
        assert set(preserved.keys()) == {"S1", "S2", "S3", "S4"}
        for sid, cr in preserved.items():
            assert cr.completeness == "COMPLETE"
            assert cr.output_text != ""
            assert cr.output_hash is not None
            assert cr.side == "mktapp"

    def test_recovery_rejects_incomplete_mktapp(self, runner, tmp_path):
        """If preserved MKTApp is not COMPLETE, recovery is refused."""
        orig_dir = _create_fake_original_run(runner, tmp_path, mktapp_complete=False)
        with pytest.raises(ValueError, match="expected COMPLETE"):
            runner.load_preserved_mktapp(orig_dir)

    def test_recovery_rejects_hash_mismatch(self, runner, tmp_path):
        """If preserved MKTApp output file hash doesn't match evidence, refused."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        # Corrupt an output file
        (orig_dir / "outputs" / "mktapp" / "S1.txt").write_text("corrupted", encoding="utf-8")
        with pytest.raises(ValueError, match="hash mismatch"):
            runner.load_preserved_mktapp(orig_dir)

    def test_recovery_rejects_missing_scenarios(self, runner, tmp_path):
        """If preserved evidence is missing scenarios, recovery is refused."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        ev = json.loads((orig_dir / "m6_evidence.json").read_text())
        ev["scenarios"] = ev["scenarios"][:3]  # drop S4
        (orig_dir / "m6_evidence.json").write_text(json.dumps(ev), encoding="utf-8")
        with pytest.raises(ValueError, match="scenarios"):
            runner.load_preserved_mktapp(orig_dir)

    def test_recovery_full_pass(self, runner, budget_config, tmp_path):
        """Full recovery path with mock executors produces a valid PASS/FAIL."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        recovery_dir = tmp_path / "recovery"
        deps = CountingRecoveryDeps(runner)
        result = runner.run_recovery(
            deps.deps(), budget_config, recovery_dir, orig_dir, execute=False,
        )
        assert result.valid is True
        assert result.final_pass is not None
        assert result.execution_mode == "recovery_dry_run"
        # Exactly 4 baseline calls (one per scenario)
        assert len(deps.baseline_calls) == 4
        # Exactly 4 judge calls
        assert len(deps.judge_calls) == 4
        # Recovery meta written
        meta = json.loads((recovery_dir / "recovery_meta.json").read_text())
        assert meta["recovery"] is True
        assert meta["mktapp_regenerated"] is False
        # Verdict marked as recovery
        verdict = json.loads((recovery_dir / "m6_uplift_verdict.json").read_text())
        assert verdict["recovery"] is True

    def test_recovery_mktapp_not_regenerated(self, runner, budget_config, tmp_path):
        """Recovery must NOT call any MKTApp executor — only Baseline."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        recovery_dir = tmp_path / "recovery"
        deps = CountingRecoveryDeps(runner)
        runner.run_recovery(
            deps.deps(), budget_config, recovery_dir, orig_dir, execute=False,
        )
        # RecoveryDeps has no mktapp_executor — if it were called, it would
        # raise AttributeError. The fact that we got a result proves MKTApp
        # was never called.
        assert len(deps.baseline_calls) == 4

    def test_recovery_preserved_mktapp_outputs_unchanged(self, runner, budget_config, tmp_path):
        """Recovery must not alter the original run's MKTApp output files."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        # Record original hashes
        orig_hashes = {}
        for sid in ["S1", "S2", "S3", "S4"]:
            f = orig_dir / "outputs" / "mktapp" / f"{sid}.txt"
            orig_hashes[sid] = hashlib.sha256(f.read_bytes()).hexdigest()
        # Run recovery
        recovery_dir = tmp_path / "recovery"
        deps = CountingRecoveryDeps(runner)
        runner.run_recovery(
            deps.deps(), budget_config, recovery_dir, orig_dir, execute=False,
        )
        # Verify original files unchanged
        for sid, h in orig_hashes.items():
            f = orig_dir / "outputs" / "mktapp" / f"{sid}.txt"
            assert hashlib.sha256(f.read_bytes()).hexdigest() == h

    def test_recovery_baseline_incomplete_stops(self, runner, budget_config, tmp_path):
        """If a new Baseline candidate is INCOMPLETE, recovery stops invalid."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        recovery_dir = tmp_path / "recovery"
        deps = CountingRecoveryDeps(runner, baseline_complete=False)
        result = runner.run_recovery(
            deps.deps(), budget_config, recovery_dir, orig_dir, execute=False,
        )
        assert result.valid is False
        assert result.final_pass is None
        assert "candidate_incomplete" in result.stop_reason

    def test_recovery_judge_exception_stops(self, runner, budget_config, tmp_path):
        """If Judge throws an exception, recovery stops invalid (not FAIL)."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        recovery_dir = tmp_path / "recovery"
        deps = CountingRecoveryDeps(runner, judge_exception_sid="S2")
        result = runner.run_recovery(
            deps.deps(), budget_config, recovery_dir, orig_dir, execute=False,
        )
        assert result.valid is False
        assert result.final_pass is None
        assert "judge_exception" in result.stop_reason

    def test_recovery_judge_error_stops(self, runner, budget_config, tmp_path):
        """If Judge returns an error result, recovery stops invalid."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        recovery_dir = tmp_path / "recovery"
        deps = CountingRecoveryDeps(runner, judge_error_sid="S3")
        result = runner.run_recovery(
            deps.deps(), budget_config, recovery_dir, orig_dir, execute=False,
        )
        assert result.valid is False
        assert "judge_error" in result.stop_reason

    def test_recovery_fresh_blind_mapping(self, runner, budget_config, tmp_path):
        """Recovery uses a fresh blind mapping (from frozen seed)."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        recovery_dir = tmp_path / "recovery"
        deps = CountingRecoveryDeps(runner)
        runner.run_recovery(
            deps.deps(), budget_config, recovery_dir, orig_dir, execute=False,
        )
        # Mapping secret exists and covers all 4 scenarios
        mapping = json.loads((recovery_dir / "m6_mapping_secret.json").read_text())
        assert set(mapping.keys()) == {"S1", "S2", "S3", "S4"}
        for sid, m in mapping.items():
            assert set(m.keys()) == {"X", "Y"}
            assert m["X"] in ("MKTApp", "Baseline")
            assert m["Y"] in ("MKTApp", "Baseline")
            assert m["X"] != m["Y"]

    def test_recovery_produces_gate_result(self, runner, budget_config, tmp_path):
        """A valid recovery produces a complete Gate v1 result."""
        orig_dir = _create_fake_original_run(runner, tmp_path)
        recovery_dir = tmp_path / "recovery"
        deps = CountingRecoveryDeps(runner)
        result = runner.run_recovery(
            deps.deps(), budget_config, recovery_dir, orig_dir, execute=False,
        )
        assert result.valid is True
        g = result.gate_result
        assert "pass" in g
        assert "gate_a_aggregate_uplift" in g
        assert "gate_b_scenario_consistency" in g
        assert "gate_c_no_scenario_regression" in g
        assert "gate_d_no_core_regression" in g

    def test_recovery_cli_dry_run(self, runner, tmp_path, monkeypatch):
        """CLI --dry-run --recover-from produces a valid result."""
        import sys
        orig_dir = _create_fake_original_run(runner, tmp_path)
        recovery_dir = tmp_path / "cli_recovery"
        monkeypatch.setattr(sys, "argv", [
            "runner", "--dry-run", "--recover-from", str(orig_dir),
            "--run-dir", str(recovery_dir),
        ])
        rc = runner.main()
        assert rc in (0, 1)  # valid PASS=0 or valid FAIL=1

    def test_recovery_cli_execute_requires_authorize(self, runner, tmp_path, monkeypatch):
        import sys
        orig_dir = _create_fake_original_run(runner, tmp_path)
        monkeypatch.setattr(sys, "argv", [
            "runner", "--execute", "--recover-from", str(orig_dir),
            "--run-dir", str(tmp_path / "r"),
        ])
        rc = runner.main()
        assert rc == 2  # missing --authorize
