"""End-to-end runner for the frozen same-model uplift qualification.

This is a THIN ORCHESTRATOR.  It introduces NO new qualification logic.
It only wires together already-frozen components from
``scripts/m6_uplift_harness.py`` and ``scripts/m6_judge_runner.py``:

  preflight (frozen)
    -> initialize EXPLICIT budget configuration (no hidden default authorization)
    -> per scenario S1-S4:
         baseline candidate  (frozen neutral task spec + frozen budget guard)
         MKTApp candidate    (frozen run_mktapp_candidate)
         completeness        (frozen completeness rules)
    -> blind X/Y mapping     (frozen, deterministic seed)
    -> S4 judge rendering    (frozen render_s4_for_judge)
    -> uplift judge preflight (this runner: both sides COMPLETE, no partial judge)
    -> Judge execution       (frozen run_judge per scenario)
    -> score extraction + mapping reveal
    -> uplift calculation    (frozen calculate_uplift / calculate_overall_uplift)
    -> Gate v1 evaluation    (frozen evaluate_uplift_gate — LOCKED thresholds)
    -> evidence persistence  (frozen write_evidence)
    -> final PASS / FAIL

Execution modes
---------------
``--dry-run`` (default): uses MOCK executors — zero paid calls.  Exercises the
  full orchestration path offline so the runner itself can be frozen and tested.

``--execute --authorize``: uses REAL executors (real LLMClient, real Judge).
  Requires the explicit budget config file and the ``--authorize`` flag.
  The runner never creates its own default authorization.

Safety properties enforced by the orchestration (tested offline):
  * exactly one candidate execution per scenario/side
  * no automatic rerun on poor scores
  * no automatic whole-qualification restart
  * no duplicate Judge execution
  * no Judge execution before candidate evidence is complete
  * no Gate evaluation from incomplete/invalid evidence
  * deterministic blind mapping
  * failure paths stop safely (provider/infra failure is NOT misreported as
    product-quality FAIL)
  * actual cost accounting preserved per turn/call
  * final PASS/FAIL only from a complete valid qualification
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import m6_uplift_harness as harness
import m6_judge_runner as judge_runner

from m6_uplift_harness import (
    SCENARIOS,
    BlindMapping,
    BudgetGuardedLLMClient,
    CandidateBudgetCaps,
    CandidateResult,
    PreflightResult,
    ScenarioComparison,
    SourceFixture,
    UpliftScenario,
    build_budget_caps,
    build_neutral_task_spec,
    calculate_overall_uplift,
    calculate_uplift,
    create_all_blind_mappings,
    evaluate_mktapp_completeness,
    evaluate_single_call_completeness,
    evaluate_uplift_gate,
    render_s4_for_judge,
    uplift_preflight_all,
    write_evidence,
)

CONFIG_PATH = PROJECT_ROOT / "config" / "m6_uplift_qualification.yaml"


# ---------------------------------------------------------------------------
# Budget configuration — explicit, no hidden default authorization
# ---------------------------------------------------------------------------

@dataclass
class BudgetConfig:
    """Explicit, Product-Owner-approved budget configuration.

    The runner never invents a default authorization.  ALL fields must come
    from the config file — there are no hidden in-code defaults for budget
    values.  If any field is missing, execution is refused.
    """
    total_cap: float
    generation_cap: float
    judge_cap: float
    judge_per_call_reserve: float
    blind_mapping_seed: int
    generation_per_call_reserve: float


def load_budget_config(path: Path | None = None) -> BudgetConfig:
    """Load the explicit budget configuration from YAML.

    Raises FileNotFoundError if the config file is missing — the runner must
    NOT fall back to a hidden default.  Raises ValueError if any required
    field is missing — ALL budget values must be explicit.
    """
    if path is None:
        path = CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Budget config not found: {path}. The runner refuses to invent a "
            f"default authorization. Provide config/m6_uplift_qualification.yaml."
        )
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = data.get("m6_uplift_qualification", {})
    required = (
        "total_cap", "generation_cap", "judge_cap",
        "judge_per_call_reserve", "blind_mapping_seed",
        "generation_per_call_reserve",
    )
    for k in required:
        if cfg.get(k) is None:
            raise ValueError(
                f"Budget config missing required field '{k}' in {path}. "
                f"All budget values must be explicit — no hidden defaults."
            )
    return BudgetConfig(
        total_cap=float(cfg["total_cap"]),
        generation_cap=float(cfg["generation_cap"]),
        judge_cap=float(cfg["judge_cap"]),
        judge_per_call_reserve=float(cfg["judge_per_call_reserve"]),
        blind_mapping_seed=int(cfg["blind_mapping_seed"]),
        generation_per_call_reserve=float(cfg["generation_per_call_reserve"]),
    )


# ---------------------------------------------------------------------------
# Executor protocols — injectable for offline testing
# ---------------------------------------------------------------------------

class BaselineExecutor(Protocol):
    """Run the direct same-model baseline for one scenario."""
    def __call__(
        self, scenario: UpliftScenario, fixture: SourceFixture,
        budget: CandidateBudgetCaps,
    ) -> CandidateResult: ...


class MktappExecutor(Protocol):
    """Run the MKTApp candidate for one scenario (delegates to frozen component)."""
    def __call__(
        self, scenario: UpliftScenario, fixture: SourceFixture,
        budget: CandidateBudgetCaps,
    ) -> CandidateResult: ...


class JudgeExecutor(Protocol):
    """Run the blind Judge for one scenario (delegates to frozen run_judge)."""
    def __call__(
        self, scenario_dict: dict, run_dir: Path, api_key: str | None,
        budget_guard_fn: Callable[[], None] | None,
        budget_commit_fn: Callable[[float], None] | None,
    ) -> Any: ...


@dataclass
class RunnerDeps:
    """Injectable execution dependencies.

    In ``--dry-run`` mode these are mocks (zero paid calls).
    In ``--execute`` mode these are the real production seams.
    """
    baseline_executor: BaselineExecutor
    mktapp_executor: MktappExecutor
    judge_executor: JudgeExecutor
    api_key: str | None = None


# ---------------------------------------------------------------------------
# Qualification result
# ---------------------------------------------------------------------------

@dataclass
class QualificationResult:
    """Final result of one qualification attempt."""
    run_id: str
    run_dir: Path
    valid: bool                      # True only if a complete valid qualification
    final_pass: bool | None          # True/False only if valid; None if invalid
    gate_result: dict[str, Any]      # frozen evaluate_uplift_gate output (or {})
    scenario_results: list[dict[str, Any]]
    invalid_scenarios: list[str]     # scenarios that could not be judged
    stop_reason: str | None          # why execution stopped (None = completed)
    budget_summary: dict[str, Any]
    judge_completed: bool            # all 4 judge calls completed
    execution_mode: str              # "dry_run" or "paid"

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "valid": self.valid,
            "final_pass": self.final_pass,
            "gate_result": self.gate_result,
            "invalid_scenarios": self.invalid_scenarios,
            "stop_reason": self.stop_reason,
            "judge_completed": self.judge_completed,
            "execution_mode": self.execution_mode,
            "budget_summary": self.budget_summary,
            "scenarios": self.scenario_results,
        }


# ---------------------------------------------------------------------------
# Core orchestration — orchestrates ONLY frozen components
# ---------------------------------------------------------------------------

def run_qualification(
    deps: RunnerDeps,
    budget_config: BudgetConfig,
    run_dir: Path,
    *,
    execute: bool = False,
) -> QualificationResult:
    """Run the frozen same-model uplift qualification end-to-end.

    This function orchestrates frozen components.  It does NOT:
    - rerun scenarios because of poor scores;
    - restart the qualification;
    - tune prompts/models/scoring/gate;
    - judge incomplete candidates;
    - evaluate the gate from invalid evidence.

    Returns a QualificationResult.  ``valid=True`` only when all four
    scenarios produced complete candidates AND all four judge calls
    completed, yielding a real PASS/FAIL.
    """
    execution_mode = "paid" if execute else "dry_run"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + (
        "_paid" if execute else "_dryrun"
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generation preflight (frozen)
    preflight_ok, preflight_results = uplift_preflight_all()
    if not preflight_ok:
        reasons = [f"{r.scenario_id}: {r.reason}" for r in preflight_results if not r.ok]
        return _stop_invalid(run_id, run_dir, execution_mode,
                             stop_reason="generation_preflight_failed: " + "; ".join(reasons),
                             budget_summary=_budget_summary(None))

    # 2. Budget — explicit config, no hidden default
    budget = build_budget_caps(
        total_cap=budget_config.total_cap,
        generation_cap=budget_config.generation_cap,
        judge_cap=budget_config.judge_cap,
    )

    # 3. Deterministic blind mapping (frozen, seeded)
    blind_mappings = create_all_blind_mappings(seed=budget_config.blind_mapping_seed)

    # 4. Per-scenario candidate generation
    comparisons: list[ScenarioComparison] = []
    invalid_scenarios: list[str] = []
    candidate_outputs: dict[str, dict[str, str]] = {}  # sid -> {"mktapp": text, "baseline": text}
    raw_json_audits: dict[str, str | None] = {}        # sid -> raw mktapp JSON (S4 audit)

    for scenario in SCENARIOS:
        sid = scenario.id
        fixture = scenario.build_source_fixture()

        # --- Candidate A: baseline (frozen neutral task spec + frozen budget guard) ---
        baseline_result = deps.baseline_executor(scenario, fixture, budget)

        # --- Candidate B: MKTApp (frozen run_mktapp_candidate) ---
        mktapp_result = deps.mktapp_executor(scenario, fixture, budget)

        # --- Completeness (frozen rules) ---
        # Both sides must be COMPLETE to proceed to Judge.
        if baseline_result.completeness != "COMPLETE" or mktapp_result.completeness != "COMPLETE":
            invalid_scenarios.append(sid)

        # --- S4 rendering: Judge sees rendered markdown, raw JSON preserved as audit ---
        # The content_creator agent produces machine-readable JSON (architectural
        # requirement).  The frozen run_mktapp_candidate → run_case ALREADY
        # renders S4 to markdown (qual_runner.py line 651-653) and writes the
        # raw JSON as a separate audit artifact ({case_id}_output_raw.json).
        #
        # So for S4, the mktapp_result.output_text is ALREADY rendered markdown.
        # We do NOT re-render.  We only need to obtain the raw JSON audit from
        # the run_case output file.  If the output is still raw JSON (e.g., from
        # a mock executor that didn't render), we render it here.
        #
        # This branch is mechanically knowable (agent_key is a config value,
        # not a semantic decision) and applies the frozen S4 presentation
        # contract from M6_QUALIFICATION_PLAN.md §"S4 presentation".
        mktapp_judge_text = mktapp_result.output_text
        raw_json_audit: str | None = None
        if scenario.agent_key == "content_creator" and mktapp_result.completeness == "COMPLETE":
            # Check if output is already rendered markdown (not raw JSON)
            output_stripped = mktapp_result.output_text.strip()
            if output_stripped.startswith("{") and '"posts"' in output_stripped:
                # Raw JSON — render it (mock executor path or unrendered output)
                rendered, err = render_s4_for_judge(mktapp_result.output_text)
                if err is not None:
                    invalid_scenarios.append(sid)
                    mktapp_judge_text = ""
                else:
                    mktapp_judge_text = rendered
                    raw_json_audit = mktapp_result.output_text
            else:
                # Already rendered markdown (real run_case path) — use as-is.
                # The raw JSON audit was written by run_case to a separate file.
                raw_json_audit = None  # audit is in the run_case output file
        raw_json_audits[sid] = raw_json_audit

        # Store judge-facing outputs (rendered for S4 mktapp; raw otherwise)
        candidate_outputs[sid] = {
            "mktapp": mktapp_judge_text,
            "baseline": baseline_result.output_text,
        }

        # Attach raw JSON audit to the mktapp result for evidence.
        # IMPORTANT: update output_hash to match the judge-facing (rendered)
        # text so the frozen write_evidence hashes match the actual files.
        mktapp_result.raw_json_audit = raw_json_audit
        if mktapp_judge_text:
            mktapp_result.output_hash = hashlib.sha256(
                mktapp_judge_text.encode("utf-8"),
            ).hexdigest()

        # Build preflight result for evidence
        pf = next((r for r in preflight_results if r.scenario_id == sid), preflight_results[0])

        comparisons.append(ScenarioComparison(
            scenario_id=sid,
            agent_key=scenario.agent_key,
            baseline=baseline_result,
            mktapp=mktapp_result,
            preflight=pf,
            blind_mapping=blind_mappings[sid],
        ))

    # 5. Write candidate outputs + hashes + mapping + evidence (frozen write_evidence)
    _write_candidate_outputs(run_dir, comparisons, candidate_outputs, blind_mappings)
    write_evidence(run_dir, comparisons, run_id, blind_mappings)

    # 6. If any scenario invalid, STOP — no Judge, no Gate, no PASS/FAIL
    if invalid_scenarios:
        return _stop_invalid(
            run_id, run_dir, execution_mode,
            stop_reason=f"candidate_incomplete: {invalid_scenarios}",
            budget_summary=_budget_summary(budget),
            invalid_scenarios=invalid_scenarios,
            scenario_results=[c.to_evidence_dict() for c in comparisons],
        )

    # 7. Uplift judge preflight (this runner) — no partial judge artifacts
    jp_ok, jp_reason = _uplift_judge_preflight(run_dir, comparisons)
    if not jp_ok:
        return _stop_invalid(run_id, run_dir, execution_mode,
                             stop_reason=f"judge_preflight_failed: {jp_reason}",
                             budget_summary=_budget_summary(budget),
                             scenario_results=[c.to_evidence_dict() for c in comparisons])

    # 8. Judge execution — frozen run_judge per scenario, budget-gated
    # Budget protection: the pre-call reserve is conservative, but if actual
    # cost exceeds the reserve, we check remaining budget BEFORE the next call.
    # This prevents runaway overspend while never stopping a normally
    # progressing qualification mid-call.
    judge_results: list[Any] = []
    judge_completed = True
    judge_stopped_reason: str | None = None
    for scenario in SCENARIOS:
        sid = scenario.id
        judge_scenario_dict = _get_judge_scenario_dict(sid)

        # Budget guard: check judge stage cap before each call.
        # The reserve is conservative; actual cost is committed after.
        reserve = budget_config.judge_per_call_reserve

        def _guard(_sid=sid, _reserve=reserve) -> None:
            budget.hierarchy.check_call(
                _sid, _reserve, stage="judge", label=f"{_sid}/judge",
            )

        try:
            jr = deps.judge_executor(
                judge_scenario_dict, run_dir, deps.api_key,
                _guard, None,  # commit handled below with actual cost
            )
        except Exception as e:
            # Provider/infrastructure failure — NOT a product-quality FAIL.
            judge_results.append(_make_error_judge_result(sid, str(e)))
            judge_completed = False
            judge_stopped_reason = f"judge_exception_{sid}: {e}"
            break

        # Commit actual judge cost to the judge stage
        actual_cost = getattr(jr, "cost_usd", None)
        if actual_cost is not None:
            budget.hierarchy.commit_spend(sid, float(actual_cost), stage="judge")

        judge_results.append(jr)
        if getattr(jr, "error", None) or getattr(jr, "stopped", False):
            judge_completed = False
            judge_stopped_reason = f"judge_error_{sid}: {getattr(jr, 'error', '?')}"
            break

        # Post-call budget check: if remaining judge budget cannot cover
        # the next conservative reserve, stop before the next call.
        # This catches overspend from actual > reserved without stopping
        # a normally progressing qualification mid-call.
        if scenario is not SCENARIOS[-1]:
            try:
                next_sid = SCENARIOS[SCENARIOS.index(scenario) + 1].id
                budget.hierarchy.check_call(
                    next_sid, reserve, stage="judge",
                    label=f"{next_sid}/judge/precheck",
                )
            except Exception:
                judge_completed = False
                judge_stopped_reason = (
                    f"judge_budget_exhausted_after_{sid}: "
                    f"cannot cover next call reserve"
                )
                break

    # 9. If judge did not complete all 4, STOP — no official PASS/FAIL
    if not judge_completed or len(judge_results) < len(SCENARIOS):
        _write_judge_raw(run_dir, judge_results, incomplete=True)
        return _stop_invalid(
            run_id, run_dir, execution_mode,
            stop_reason=judge_stopped_reason or "judge_incomplete",
            budget_summary=_budget_summary(budget),
            scenario_results=[c.to_evidence_dict() for c in comparisons],
            judge_completed=False,
        )

    # 10. Score extraction + mapping reveal (mirrors frozen _reveal logic)
    scenario_uplifts: list[dict[str, float]] = []
    scenario_overall_uplifts: list[float] = []
    scenario_score_records: list[dict[str, Any]] = []

    for i, scenario in enumerate(SCENARIOS):
        sid = scenario.id
        jr = judge_results[i]
        mapping = blind_mappings[sid].mapping_dict  # {"X": ..., "Y": ...}
        scores = _extract_scores(jr)
        mktapp_scores: dict[str, float] = {}
        baseline_scores: dict[str, float] = {}
        for dim, ds in scores.items():
            x_score = ds.get("X")
            y_score = ds.get("Y")
            if x_score is None or y_score is None:
                continue
            if mapping["X"] == "MKTApp":
                mktapp_scores[dim] = float(x_score)
                baseline_scores[dim] = float(y_score)
            else:
                mktapp_scores[dim] = float(y_score)
                baseline_scores[dim] = float(x_score)

        uplift = calculate_uplift(mktapp_scores, baseline_scores)
        overall = calculate_overall_uplift(uplift)
        scenario_uplifts.append(uplift)
        scenario_overall_uplifts.append(overall)
        scenario_score_records.append({
            "scenario_id": sid,
            "mktapp_scores": mktapp_scores,
            "baseline_scores": baseline_scores,
            "uplift": uplift,
            "overall_uplift": overall,
            "judge_cost_usd": getattr(jr, "cost_usd", 0.0),
        })

        # Attach to comparison for evidence
        comparisons[i].judge_scores = {
            "mktapp": mktapp_scores, "baseline": baseline_scores,
        }
        comparisons[i].uplift = uplift
        comparisons[i].overall_uplift = overall

    # 11. Gate v1 evaluation (frozen — LOCKED thresholds)
    gate_result = evaluate_uplift_gate(scenario_uplifts, scenario_overall_uplifts)
    final_pass = bool(gate_result["pass"])

    # 12. Persist final evidence + judge artifacts
    write_evidence(run_dir, comparisons, run_id, blind_mappings)
    _write_judge_raw(run_dir, judge_results, incomplete=False)
    _write_uplift_verdict(run_dir, run_id, execution_mode, gate_result,
                          scenario_score_records, budget, comparisons)

    return QualificationResult(
        run_id=run_id,
        run_dir=run_dir,
        valid=True,
        final_pass=final_pass,
        gate_result=gate_result,
        scenario_results=scenario_score_records,
        invalid_scenarios=[],
        stop_reason=None,
        budget_summary=_budget_summary(budget),
        judge_completed=True,
        execution_mode=execution_mode,
    )


# ---------------------------------------------------------------------------
# Recovery path — consume preserved MKTApp evidence + generate only Baseline
# ---------------------------------------------------------------------------

@dataclass
class RecoveryDeps:
    """Injectable dependencies for recovery execution.

    Only ``baseline_executor`` and ``judge_executor`` are used — MKTApp
    candidates are loaded from preserved evidence, never regenerated.
    """
    baseline_executor: BaselineExecutor
    judge_executor: JudgeExecutor
    api_key: str | None = None


def load_preserved_mktapp(
    original_run_dir: Path,
) -> dict[str, CandidateResult]:
    """Load preserved MKTApp S1-S4 evidence from a prior failed run.

    Validates:
      - all 4 scenarios present
      - all 4 COMPLETE
      - output files exist and hashes match evidence
      - source fixture hashes match frozen canonical fixtures

    Returns a dict {sid: CandidateResult} for the preserved MKTApp candidates.
    Raises ValueError if any validation fails.
    """
    evidence_path = original_run_dir / "m6_evidence.json"
    if not evidence_path.exists():
        raise ValueError(f"No m6_evidence.json in {original_run_dir}")

    ev = json.loads(evidence_path.read_text(encoding="utf-8"))
    expected_sids = {s.id for s in SCENARIOS}
    found_sids = {s["scenario_id"] for s in ev.get("scenarios", [])}
    if found_sids != expected_sids:
        raise ValueError(
            f"Preserved evidence scenarios {found_sids} != expected {expected_sids}"
        )

    preserved: dict[str, CandidateResult] = {}
    for s in ev["scenarios"]:
        sid = s["scenario_id"]
        m = s["mktapp"]

        # Completeness check
        if m["completeness"] != "COMPLETE":
            raise ValueError(
                f"Preserved MKTApp {sid} completeness={m['completeness']}, "
                f"expected COMPLETE — cannot recover from incomplete MKTApp"
            )

        # Output file exists and hash matches
        out_file = original_run_dir / "outputs" / "mktapp" / f"{sid}.txt"
        if not out_file.exists():
            raise ValueError(f"Preserved MKTApp {sid} output file missing: {out_file}")
        actual_hash = hashlib.sha256(out_file.read_bytes()).hexdigest()
        if actual_hash != m["output_hash"]:
            raise ValueError(
                f"Preserved MKTApp {sid} hash mismatch: "
                f"file={actual_hash[:16]}... evidence={m['output_hash'][:16]}..."
            )

        # Source fixture hash matches frozen canonical
        scenario = next(sc for sc in SCENARIOS if sc.id == sid)
        canonical_fixture_hash = scenario.build_source_fixture().hash()
        if m["source_fixture_hash"] != canonical_fixture_hash:
            raise ValueError(
                f"Preserved MKTApp {sid} fixture hash mismatch — "
                f"canonical fixtures may have changed since the original run"
            )

        text = out_file.read_text(encoding="utf-8")
        preserved[sid] = CandidateResult(
            scenario_id=sid, side="mktapp", model=m["model"],
            output_text=text,
            output_hash=m["output_hash"],
            completeness=m["completeness"],
            final_finish_reason=m["final_finish_reason"],
            final_truncated=m["final_truncated"],
            call_count=m["call_count"],
            prompt_tokens=m.get("prompt_tokens", 0),
            completion_tokens=m.get("completion_tokens", 0),
            cost_usd=m["cost_usd"],
            reserve_usd=m.get("reserve_usd", 0.0),
            source_fixture_hash=m["source_fixture_hash"],
        )

    return preserved


def run_recovery(
    deps: RecoveryDeps,
    budget_config: BudgetConfig,
    run_dir: Path,
    original_run_dir: Path,
    *,
    execute: bool = False,
) -> QualificationResult:
    """Run a recovery qualification using preserved MKTApp evidence.

    This function:
      1. Loads preserved MKTApp S1-S4 from original_run_dir (no paid MKTApp calls)
      2. Validates provenance (hashes, completeness, fixture hashes)
      3. Generates ONLY new Baseline S1-S4 (one call each)
      4. Creates a FRESH blind mapping for the recovered complete set
      5. Proceeds through Judge → uplift → Gate v1 → PASS/FAIL
      6. Marks evidence explicitly as recovery from harness failure

    MKTApp candidates are NEVER regenerated.  Baseline candidates execute
    exactly once each.  No rerun based on quality or score.
    """
    execution_mode = "recovery_paid" if execute else "recovery_dry_run"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + (
        "_recovery" if execute else "_recovery_dryrun"
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    # 0. Record recovery provenance
    recovery_meta = {
        "recovery": True,
        "original_run_dir": str(original_run_dir),
        "original_run_id": "20260905_072432_paid",
        "recovery_reason": "harness_failure_baseline_value_bug",
        "mktapp_regenerated": False,
        "baseline_regenerated": True,
    }
    (run_dir / "recovery_meta.json").write_text(
        json.dumps(recovery_meta, indent=2, ensure_ascii=False), encoding="utf-8",
    )

    # 1. Generation preflight (frozen)
    preflight_ok, preflight_results = uplift_preflight_all()
    if not preflight_ok:
        reasons = [f"{r.scenario_id}: {r.reason}" for r in preflight_results if not r.ok]
        return _stop_invalid(run_id, run_dir, execution_mode,
                             stop_reason="generation_preflight_failed: " + "; ".join(reasons),
                             budget_summary=_budget_summary(None))

    # 2. Load preserved MKTApp evidence (no paid calls)
    try:
        preserved_mktapp = load_preserved_mktapp(original_run_dir)
    except ValueError as e:
        return _stop_invalid(run_id, run_dir, execution_mode,
                             stop_reason=f"preserved_evidence_invalid: {e}",
                             budget_summary=_budget_summary(None))

    # 3. Budget — explicit config, no hidden default
    budget = build_budget_caps(
        total_cap=budget_config.total_cap,
        generation_cap=budget_config.generation_cap,
        judge_cap=budget_config.judge_cap,
    )

    # 4. FRESH blind mapping (different seed from original if desired,
    #    but frozen seed from config for determinism)
    blind_mappings = create_all_blind_mappings(seed=budget_config.blind_mapping_seed)

    # 5. Generate ONLY new Baseline candidates + assemble comparisons
    comparisons: list[ScenarioComparison] = []
    invalid_scenarios: list[str] = []
    candidate_outputs: dict[str, dict[str, str]] = {}
    raw_json_audits: dict[str, str | None] = {}

    for scenario in SCENARIOS:
        sid = scenario.id
        fixture = scenario.build_source_fixture()

        # --- Candidate A: NEW baseline (one paid call each) ---
        baseline_result = deps.baseline_executor(scenario, fixture, budget)

        # --- Candidate B: PRESERVED MKTApp (no paid call) ---
        mktapp_result = preserved_mktapp[sid]

        # --- Completeness (frozen rules) ---
        if baseline_result.completeness != "COMPLETE" or mktapp_result.completeness != "COMPLETE":
            invalid_scenarios.append(sid)

        # --- S4 rendering (same logic as run_qualification) ---
        mktapp_judge_text = mktapp_result.output_text
        raw_json_audit: str | None = None
        if scenario.agent_key == "content_creator" and mktapp_result.completeness == "COMPLETE":
            output_stripped = mktapp_result.output_text.strip()
            if output_stripped.startswith("{") and '"posts"' in output_stripped:
                rendered, err = render_s4_for_judge(mktapp_result.output_text)
                if err is not None:
                    invalid_scenarios.append(sid)
                    mktapp_judge_text = ""
                else:
                    mktapp_judge_text = rendered
                    raw_json_audit = mktapp_result.output_text
            else:
                raw_json_audit = None
        raw_json_audits[sid] = raw_json_audit

        candidate_outputs[sid] = {
            "mktapp": mktapp_judge_text,
            "baseline": baseline_result.output_text,
        }

        mktapp_result.raw_json_audit = raw_json_audit
        if mktapp_judge_text:
            mktapp_result.output_hash = hashlib.sha256(
                mktapp_judge_text.encode("utf-8"),
            ).hexdigest()

        pf = next((r for r in preflight_results if r.scenario_id == sid), preflight_results[0])

        comparisons.append(ScenarioComparison(
            scenario_id=sid,
            agent_key=scenario.agent_key,
            baseline=baseline_result,
            mktapp=mktapp_result,
            preflight=pf,
            blind_mapping=blind_mappings[sid],
        ))

    # 6. Write candidate outputs + evidence
    _write_candidate_outputs(run_dir, comparisons, candidate_outputs, blind_mappings)
    write_evidence(run_dir, comparisons, run_id, blind_mappings)

    # 7. If any scenario invalid, STOP
    if invalid_scenarios:
        return _stop_invalid(
            run_id, run_dir, execution_mode,
            stop_reason=f"candidate_incomplete: {invalid_scenarios}",
            budget_summary=_budget_summary(budget),
            invalid_scenarios=invalid_scenarios,
            scenario_results=[c.to_evidence_dict() for c in comparisons],
        )

    # 8. Judge preflight
    jp_ok, jp_reason = _uplift_judge_preflight(run_dir, comparisons)
    if not jp_ok:
        return _stop_invalid(run_id, run_dir, execution_mode,
                             stop_reason=f"judge_preflight_failed: {jp_reason}",
                             budget_summary=_budget_summary(budget),
                             scenario_results=[c.to_evidence_dict() for c in comparisons])

    # 9. Judge execution (same as run_qualification)
    judge_results: list[Any] = []
    judge_completed = True
    judge_stopped_reason: str | None = None
    for scenario in SCENARIOS:
        sid = scenario.id
        judge_scenario_dict = _get_judge_scenario_dict(sid)
        reserve = budget_config.judge_per_call_reserve

        def _guard(_sid=sid, _reserve=reserve) -> None:
            budget.hierarchy.check_call(
                _sid, _reserve, stage="judge", label=f"{_sid}/judge",
            )

        try:
            jr = deps.judge_executor(
                judge_scenario_dict, run_dir, deps.api_key,
                _guard, None,
            )
        except Exception as e:
            judge_results.append(_make_error_judge_result(sid, str(e)))
            judge_completed = False
            judge_stopped_reason = f"judge_exception_{sid}: {e}"
            break

        actual_cost = getattr(jr, "cost_usd", None)
        if actual_cost is not None:
            budget.hierarchy.commit_spend(sid, float(actual_cost), stage="judge")

        judge_results.append(jr)
        if getattr(jr, "error", None) or getattr(jr, "stopped", False):
            judge_completed = False
            judge_stopped_reason = f"judge_error_{sid}: {getattr(jr, 'error', '?')}"
            break

        if scenario is not SCENARIOS[-1]:
            try:
                next_sid = SCENARIOS[SCENARIOS.index(scenario) + 1].id
                budget.hierarchy.check_call(
                    next_sid, reserve, stage="judge",
                    label=f"{next_sid}/judge/precheck",
                )
            except Exception:
                judge_completed = False
                judge_stopped_reason = (
                    f"judge_budget_exhausted_after_{sid}: "
                    f"cannot cover next call reserve"
                )
                break

    if not judge_completed or len(judge_results) < len(SCENARIOS):
        _write_judge_raw(run_dir, judge_results, incomplete=True)
        return _stop_invalid(
            run_id, run_dir, execution_mode,
            stop_reason=judge_stopped_reason or "judge_incomplete",
            budget_summary=_budget_summary(budget),
            scenario_results=[c.to_evidence_dict() for c in comparisons],
            judge_completed=False,
        )

    # 10. Score extraction + mapping reveal (same as run_qualification)
    scenario_uplifts: list[dict[str, float]] = []
    scenario_overall_uplifts: list[float] = []
    scenario_score_records: list[dict[str, Any]] = []

    for i, scenario in enumerate(SCENARIOS):
        sid = scenario.id
        jr = judge_results[i]
        mapping = blind_mappings[sid].mapping_dict
        scores = _extract_scores(jr)
        mktapp_scores: dict[str, float] = {}
        baseline_scores: dict[str, float] = {}
        for dim, ds in scores.items():
            x_score = ds.get("X")
            y_score = ds.get("Y")
            if x_score is None or y_score is None:
                continue
            if mapping["X"] == "MKTApp":
                mktapp_scores[dim] = float(x_score)
                baseline_scores[dim] = float(y_score)
            else:
                mktapp_scores[dim] = float(y_score)
                baseline_scores[dim] = float(x_score)

        uplift = calculate_uplift(mktapp_scores, baseline_scores)
        overall = calculate_overall_uplift(uplift)
        scenario_uplifts.append(uplift)
        scenario_overall_uplifts.append(overall)
        scenario_score_records.append({
            "scenario_id": sid,
            "mktapp_scores": mktapp_scores,
            "baseline_scores": baseline_scores,
            "uplift": uplift,
            "overall_uplift": overall,
            "judge_cost_usd": getattr(jr, "cost_usd", 0.0),
        })

        comparisons[i].judge_scores = {
            "mktapp": mktapp_scores, "baseline": baseline_scores,
        }
        comparisons[i].uplift = uplift
        comparisons[i].overall_uplift = overall

    # 11. Gate v1 evaluation (frozen — LOCKED thresholds)
    gate_result = evaluate_uplift_gate(scenario_uplifts, scenario_overall_uplifts)
    final_pass = bool(gate_result["pass"])

    # 12. Persist final evidence + judge artifacts + recovery marker
    write_evidence(run_dir, comparisons, run_id, blind_mappings)
    _write_judge_raw(run_dir, judge_results, incomplete=False)
    _write_uplift_verdict(run_dir, run_id, execution_mode, gate_result,
                          scenario_score_records, budget, comparisons)

    # Mark verdict as recovery
    verdict_path = run_dir / "m6_uplift_verdict.json"
    if verdict_path.exists():
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        verdict["recovery"] = True
        verdict["original_run_dir"] = str(original_run_dir)
        verdict["recovery_reason"] = "harness_failure_baseline_value_bug"
        verdict_path.write_text(
            json.dumps(verdict, indent=2, ensure_ascii=False), encoding="utf-8",
        )

    return QualificationResult(
        run_id=run_id,
        run_dir=run_dir,
        valid=True,
        final_pass=final_pass,
        gate_result=gate_result,
        scenario_results=scenario_score_records,
        invalid_scenarios=[],
        stop_reason=None,
        budget_summary=_budget_summary(budget),
        judge_completed=True,
        execution_mode=execution_mode,
    )


# ---------------------------------------------------------------------------
# Helpers — output layout, preflight, score extraction
# ---------------------------------------------------------------------------

def _write_candidate_outputs(
    run_dir: Path,
    comparisons: list[ScenarioComparison],
    candidate_outputs: dict[str, dict[str, str]],
    blind_mappings: dict[str, BlindMapping],
) -> None:
    """Write independent + blind X/Y outputs and hashes in the layout
    expected by the frozen Judge (run_judge reads outputs/S{id}_X.txt, _Y.txt).
    """
    outputs_dir = run_dir / "outputs"
    mktapp_dir = outputs_dir / "mktapp"
    baseline_dir = outputs_dir / "baseline"
    mktapp_dir.mkdir(parents=True, exist_ok=True)
    baseline_dir.mkdir(parents=True, exist_ok=True)

    for comp in comparisons:
        sid = comp.scenario_id
        m_text = candidate_outputs[sid]["mktapp"]
        b_text = candidate_outputs[sid]["baseline"]
        (mktapp_dir / f"{sid}.txt").write_text(m_text, encoding="utf-8")
        (baseline_dir / f"{sid}.txt").write_text(b_text, encoding="utf-8")

        # Blind X/Y files — bytes must match the mapped independent output
        bm = blind_mappings[sid]
        if bm.x_side == "MKTApp":
            x_text, y_text = m_text, b_text
        else:
            x_text, y_text = b_text, m_text
        (outputs_dir / f"{sid}_X.txt").write_text(x_text, encoding="utf-8")
        (outputs_dir / f"{sid}_Y.txt").write_text(y_text, encoding="utf-8")

    # output_hashes.json is written by the frozen write_evidence() using
    # CandidateResult.output_hash — which we keep consistent with the file
    # content (updated after S4 rendering).  Do NOT write a duplicate here.


def _uplift_judge_preflight(
    run_dir: Path,
    comparisons: list[ScenarioComparison],
) -> tuple[bool, str]:
    """Uplift-appropriate judge preflight.

    Unlike the historical Frontier judge_preflight (which requires
    resume_link.json + recovery_manifest.json + a pinned remediation baseline),
    this preflight checks only the uplift-relevant invariants:
      * all scenarios have COMPLETE candidates on both sides;
      * mapping covers all S1-S4;
      * X/Y and independent outputs exist and hashes match;
      * no partial judge artifacts already present.
    """
    import hashlib as _hl
    expected = {s.id for s in SCENARIOS}
    outputs_dir = run_dir / "outputs"

    # No partial judge artifacts
    judge_dir = run_dir / "judge"
    if judge_dir.exists():
        for artifact in ("m6_judge_raw.json", "m6_judge_scores.json"):
            if (judge_dir / artifact).exists():
                return False, f"partial judge artifact exists: {artifact}"

    # Completeness
    for comp in comparisons:
        if comp.baseline.completeness != "COMPLETE":
            return False, f"{comp.scenario_id} baseline not COMPLETE"
        if comp.mktapp.completeness != "COMPLETE":
            return False, f"{comp.scenario_id} mktapp not COMPLETE"

    # Hashes + outputs
    hash_path = run_dir / "output_hashes.json"
    if not hash_path.exists():
        return False, "output_hashes.json missing"
    hashes = json.loads(hash_path.read_text(encoding="utf-8"))
    for sid in expected:
        for side in ("mktapp", "baseline"):
            side_path = outputs_dir / side / f"{sid}.txt"
            if not side_path.exists():
                return False, f"missing independent output: {side}/{sid}.txt"
            actual = _hl.sha256(side_path.read_bytes()).hexdigest()
            if actual != hashes.get(side, {}).get(sid):
                return False, f"hash mismatch for {side}/{sid}"
        for label in ("X", "Y"):
            p = outputs_dir / f"{sid}_{label}.txt"
            if not p.exists() or not p.read_text(encoding="utf-8").strip():
                return False, f"missing/empty blind output: {sid}_{label}.txt"

    return True, "ok"


def _extract_scores(judge_result: Any) -> dict[str, dict[str, Any]]:
    """Extract the scores dict from a JudgeResult (frozen structure)."""
    raw = getattr(judge_result, "raw_judge_json", None) or {}
    return raw.get("scores", {}) if isinstance(raw, dict) else {}


def _get_judge_scenario_dict(sid: str) -> dict:
    """Return the frozen Judge scenario dict matching sid.

    The Judge runner's SCENARIOS are frozen and semantically match the uplift
    scenarios (same product_ids, user_request, quick_brief, resource_context).
    """
    for s in judge_runner.SCENARIOS:
        if s["id"] == sid:
            return s
    raise KeyError(f"Judge scenario dict not found for {sid}")


def _make_error_judge_result(sid: str, error: str) -> Any:
    """Create a minimal error JudgeResult-like object (no paid call made)."""
    from m6_judge_runner import JudgeResult
    return JudgeResult(
        scenario_id=sid, raw_judge_json={}, actual_model="unknown",
        prompt_tokens=0, completion_tokens=0, cost_usd=0.0,
        error=error, stopped=True,
    )


def _write_judge_raw(run_dir: Path, judge_results: list[Any], *, incomplete: bool) -> None:
    """Persist raw judge results (audit trail)."""
    judge_dir = run_dir / "judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge_model": judge_runner.JUDGE_MODEL,
        "incomplete": incomplete,
        "completed": sum(1 for r in judge_results if getattr(r, "error", None) is None and not getattr(r, "stopped", False)),
        "expected": len(SCENARIOS),
        "results": [
            {
                "scenario_id": getattr(r, "scenario_id", "?"),
                "raw_judge_json": getattr(r, "raw_judge_json", {}),
                "actual_model": getattr(r, "actual_model", "unknown"),
                "prompt_tokens": getattr(r, "prompt_tokens", 0),
                "completion_tokens": getattr(r, "completion_tokens", 0),
                "cost_usd": getattr(r, "cost_usd", 0.0),
                "error": getattr(r, "error", None),
                "stopped": getattr(r, "stopped", False),
                "charged": getattr(r, "charged", False),
                "request_id": getattr(r, "request_id", None),
                "audit_artifact_path": getattr(r, "audit_artifact_path", None),
                "cost_source": getattr(r, "cost_source", None),
                "pre_call_reserve": getattr(r, "pre_call_reserve", 0.0),
            }
            for r in judge_results
        ],
    }
    (judge_dir / "m6_judge_raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _write_uplift_verdict(
    run_dir: Path, run_id: str, execution_mode: str,
    gate_result: dict[str, Any], scenario_score_records: list[dict[str, Any]],
    budget: CandidateBudgetCaps, comparisons: list[ScenarioComparison],
) -> None:
    """Persist the final uplift verdict (Gate v1 PASS/FAIL)."""
    verdict = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "qualification_mode": "same_model_uplift",
        "gate_version": harness.GATE_VERSION,
        "gate_config": harness.GATE_CONFIG,
        "execution_mode": execution_mode,
        "final_pass": bool(gate_result["pass"]),
        "gate_result": gate_result,
        "scenarios": scenario_score_records,
        "budget_summary": _budget_summary(budget),
    }
    (run_dir / "m6_uplift_verdict.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    # Human-readable report
    lines = [
        "# M6 Same-Model Uplift Qualification — Final Verdict",
        "",
        f"**Run ID:** {run_id}",
        f"**Execution mode:** {execution_mode}",
        f"**Gate version:** {harness.GATE_VERSION}",
        f"**Final result:** {'PASS' if gate_result['pass'] else 'FAIL'}",
        "",
        "## Gate v1 (locked)",
        f"- A (aggregate mean uplift >= +0.25): {gate_result['gate_a_aggregate_uplift']} "
        f"(value={gate_result['aggregate_uplift']})",
        f"- B (>= 3 of 4 scenario deltas >= 0): {gate_result['gate_b_scenario_consistency']} "
        f"(count={gate_result['scenario_consistency']})",
        f"- C (no scenario overall delta < -0.5): {gate_result['gate_c_no_scenario_regression']} "
        f"(regressed={gate_result['scenario_regression']})",
        f"- D (no core dimension delta < -0.5): {gate_result['gate_d_no_core_regression']} "
        f"(regressions={gate_result['core_dimension_regression']})",
        "",
        "## Per-scenario uplift",
    ]
    for rec in scenario_score_records:
        lines.append(f"### {rec['scenario_id']}")
        lines.append(f"- overall uplift: {rec['overall_uplift']:+.3f}")
        for dim, d in rec["uplift"].items():
            lines.append(f"  - {dim}: {d:+.2f}")
        lines.append("")
    (run_dir / "m6_uplift_verdict_report.md").write_text(
        "\n".join(lines), encoding="utf-8",
    )


def _budget_summary(budget: CandidateBudgetCaps | None) -> dict[str, Any]:
    if budget is None:
        return {"initialized": False}
    return {
        "initialized": True,
        "candidate_caps": budget.to_dict().get("candidate_caps", {}),
        "candidate_spent": budget.to_dict().get("candidate_spent", {}),
        "hierarchy": budget.to_dict().get("hierarchy", {}),
    }


def _stop_invalid(
    run_id: str, run_dir: Path, execution_mode: str, *,
    stop_reason: str,
    budget_summary: dict[str, Any],
    invalid_scenarios: list[str] | None = None,
    scenario_results: list[dict[str, Any]] | None = None,
    judge_completed: bool = False,
) -> QualificationResult:
    """Build a result for an invalid/stopped qualification (no PASS/FAIL)."""
    return QualificationResult(
        run_id=run_id,
        run_dir=run_dir,
        valid=False,
        final_pass=None,
        gate_result={},
        scenario_results=scenario_results or [],
        invalid_scenarios=invalid_scenarios or [],
        stop_reason=stop_reason,
        budget_summary=budget_summary,
        judge_completed=judge_completed,
        execution_mode=execution_mode,
    )


# ---------------------------------------------------------------------------
# Real executors — used in --execute mode (delegating to frozen components)
# ---------------------------------------------------------------------------

def real_baseline_executor(
    scenario: UpliftScenario, fixture: SourceFixture, budget: CandidateBudgetCaps,
) -> CandidateResult:
    """Run the direct same-model baseline via the real LLMClient.

    This is orchestration GLUE assembling frozen components — not new
    qualification logic.  No frozen ``run_baseline_candidate`` function
    exists in the harness (only ``run_mktapp_candidate`` does).  This
    function assembles:

    - prompt:       frozen ``build_neutral_task_spec``
    - budget guard: frozen ``BudgetGuardedLLMClient``
    - model:        frozen ``scenario.agent_model()`` (same model as MKTApp — parity)
    - tools:        server-side ``openrouter:web_search`` for S2/S3 via
                    ``chat(tools=...)`` (tool parity with MKTApp, per
                    M6_QUALIFICATION_PLAN.md §"Tool parity")
    - completeness: frozen ``evaluate_single_call_completeness``

    The frozen ``BaselineToolLoopExecutor`` is for CLIENT-SIDE tool loops
    (requires ``tool_handlers``).  The production baseline uses SERVER-SIDE
    web search via ``chat(tools=...)`` (OpenRouter plugin), which the
    executor does not support — it only passes tools through
    ``chat_with_tools`` when handlers are provided.  This executor uses
    ``chat(tools=...)`` directly, matching the production server-side
    pattern documented in the qualification plan.
    """
    import scripts.qual_runner as qr

    orch = qr.make_orchestrator(scenario.product_id)
    real_llm = qr.make_llm(orch)
    guarded = BudgetGuardedLLMClient(real_llm, budget, scenario.id, "baseline")

    prompt = build_neutral_task_spec(scenario, fixture)
    model = scenario.agent_model()
    tools = [{"type": "openrouter:web_search"}] if scenario.web_search_enabled else None
    reserve = scenario.baseline_reserve

    try:
        text = guarded.chat(
            [{"role": "user", "content": prompt}],
            model=model,
            temperature=scenario.agent_temperature(),
            max_tokens=scenario.baseline_max_tokens,
            source=f"baseline.{scenario.id}.generate",
            reserve=reserve,
            tools=tools,
        )
        finish_reason = guarded.last_finish_reason
        truncated = guarded.last_truncated
        # BUG FIX: evaluate_single_call_completeness already returns a string
        # (the harness helper calls .value internally).  The previous code
        # called .value on a string, raising AttributeError AFTER the LLM
        # call succeeded, silently discarding valid output as UNKNOWN.
        completeness = evaluate_single_call_completeness(finish_reason, truncated)
        call_log = guarded.call_log
        # Actual cost = sum of committed spend from call_log (provider-reported).
        # Reserve = the pre-call authorized reserve (conservative, not actual).
        cost = sum(c.get("committed", 0) for c in call_log)
        return CandidateResult(
            scenario_id=scenario.id, side="baseline", model=model,
            output_text=text,
            output_hash=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
            completeness=completeness,
            final_finish_reason=finish_reason, final_truncated=truncated,
            call_count=len(call_log),
            cost_usd=cost,
            reserve_usd=reserve,
            source_fixture_hash=fixture.hash(),
        )
    except Exception as e:
        # Provider/infra failure — NOT a product-quality FAIL.  Mark UNKNOWN.
        # Preserve call_log data so failure evidence shows actual committed
        # spend (not the reserve) and the actual call_count.
        from src.budget_hierarchy import BudgetExceededError
        call_log = guarded.call_log
        actual_committed = sum(c.get("committed", 0) for c in call_log)
        return CandidateResult(
            scenario_id=scenario.id, side="baseline", model=model,
            output_text="", completeness="UNKNOWN",
            call_count=len(call_log),
            cost_usd=actual_committed,
            reserve_usd=reserve,
            source_fixture_hash=fixture.hash(),
        )
    finally:
        real_llm.close()


def real_mktapp_executor(
    scenario: UpliftScenario, fixture: SourceFixture, budget: CandidateBudgetCaps,
) -> CandidateResult:
    """Run the MKTApp candidate via the frozen run_mktapp_candidate."""
    raw = harness.run_mktapp_candidate(scenario, fixture, budget)
    model = scenario.agent_model()
    status = raw.get("status", "UNKNOWN")
    completeness = status if status in ("COMPLETE", "INCOMPLETE", "UNKNOWN") else "UNKNOWN"
    text = raw.get("output_text", "")
    finish_reason = raw.get("finish_reason")
    call_log = raw.get("call_log", [])
    cost = raw.get("cost_usd", 0.0)
    return CandidateResult(
        scenario_id=scenario.id, side="mktapp", model=model,
        output_text=text,
        output_hash=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
        completeness=completeness,
        final_finish_reason=finish_reason,
        call_count=len(call_log),
        cost_usd=cost,
        reserve_usd=scenario.mktapp_reserve,
        source_fixture_hash=fixture.hash(),
    )


def real_judge_executor(
    scenario_dict: dict, run_dir: Path, api_key: str | None,
    budget_guard_fn: Callable[[], None] | None,
    budget_commit_fn: Callable[[float], None] | None,
) -> Any:
    """Run the frozen Judge for one scenario."""
    if not api_key:
        raise RuntimeError("No API key for paid judge execution")
    return judge_runner.run_judge(
        scenario_dict, run_dir, api_key,
        budget_guard_fn=budget_guard_fn,
        budget_commit_fn=budget_commit_fn,
    )


def make_real_deps(api_key: str) -> RunnerDeps:
    """Build real execution dependencies for paid execution."""
    return RunnerDeps(
        baseline_executor=real_baseline_executor,
        mktapp_executor=real_mktapp_executor,
        judge_executor=real_judge_executor,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="M6 same-model uplift qualification runner (frozen).",
    )
    parser.add_argument(
        "--run-dir", type=Path, default=None,
        help="Directory to write evidence/outputs. If omitted, a fresh "
             "timestamped directory under data/m6_uplift/runs/ is created. "
             "A fresh directory avoids stale judge artifacts that would "
             "block preflight.",
    )
    parser.add_argument(
        "--config", type=Path, default=CONFIG_PATH,
        help="Budget config YAML (default: config/m6_uplift_qualification.yaml).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Offline mock execution — zero paid calls (default).",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Paid execution mode.  Requires --authorize.",
    )
    parser.add_argument(
        "--authorize", action="store_true",
        help="Explicit authorization for ONE paid qualification attempt. "
             "Required with --execute.  One authorization = one attempt; "
             "no automatic rerun.",
    )
    parser.add_argument(
        "--recover-from", type=Path, default=None,
        help="Recovery mode: load preserved MKTApp S1-S4 evidence from this "
             "prior run directory and generate ONLY new Baseline candidates. "
             "MKTApp candidates are NEVER regenerated.  Requires --execute "
             "and --authorize for paid recovery (Baseline + Judge calls only).",
    )
    args = parser.parse_args()

    if args.execute and not args.authorize:
        print("ERROR: --execute requires --authorize (explicit Product Owner "
              "authorization for one paid attempt).", file=sys.stderr)
        return 2

    if args.recover_from and not args.execute and not args.dry_run:
        print("ERROR: --recover-from requires --execute (paid recovery) or "
              "--dry-run (offline mock recovery).", file=sys.stderr)
        return 2

    if not args.execute and not args.dry_run:
        args.dry_run = True  # default to dry-run

    # Create a fresh timestamped run_dir if none specified.
    # A fresh directory avoids stale judge/ artifacts that would block
    # _uplift_judge_preflight (which rejects partial judge artifacts).
    if args.run_dir is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        suffix = "_recovery" if args.recover_from else ""
        args.run_dir = PROJECT_ROOT / "data" / "m6_uplift" / "runs" / (ts + suffix)

    # Load explicit budget config
    try:
        budget_config = load_budget_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.execute:
        # Load .env via dotenv (same as m6_judge_runner.py — never prints values)
        import dotenv
        from src.openrouter_gateway import get_api_key as _gate_get_api_key
        dotenv.load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
        api_key = _gate_get_api_key()
        if not api_key:
            print("ERROR: OPENROUTER_API_KEY not set for paid execution.", file=sys.stderr)
            return 2

    if args.recover_from:
        # Recovery mode: preserved MKTApp + new Baseline only
        if args.execute:
            recovery_deps = RecoveryDeps(
                baseline_executor=real_baseline_executor,
                judge_executor=real_judge_executor,
                api_key=api_key,
            )
            print(f"=== PAID RECOVERY (preserved MKTApp + new Baseline only) ===",
                  flush=True)
            print(f"    Original run: {args.recover_from}", flush=True)
        else:
            recovery_deps = RecoveryDeps(
                baseline_executor=_mock_baseline_executor,
                judge_executor=_mock_judge_executor,
                api_key=None,
            )
            print(f"=== DRY RUN RECOVERY (mock Baseline + preserved MKTApp) ===",
                  flush=True)
            print(f"    Original run: {args.recover_from}", flush=True)
        result = run_recovery(
            recovery_deps, budget_config, args.run_dir,
            args.recover_from, execute=args.execute,
        )
    elif args.execute:
        deps = make_real_deps(api_key)
        print("=== PAID EXECUTION (one authorized attempt) ===", flush=True)
        result = run_qualification(deps, budget_config, args.run_dir, execute=args.execute)
    else:
        deps = _make_dry_run_deps()
        print("=== DRY RUN (zero paid calls) ===", flush=True)
        result = run_qualification(deps, budget_config, args.run_dir, execute=args.execute)

    # Print final result
    print(f"Run ID: {result.run_id}", flush=True)
    print(f"Execution mode: {result.execution_mode}", flush=True)
    print(f"Valid: {result.valid}", flush=True)
    if result.valid:
        print(f"FINAL RESULT: {'PASS' if result.final_pass else 'FAIL'}", flush=True)
        g = result.gate_result
        print(f"  Gate A (aggregate uplift >= +0.25): {g['gate_a_aggregate_uplift']} "
              f"(value={g['aggregate_uplift']})", flush=True)
        print(f"  Gate B (>= 3/4 scenarios >= 0): {g['gate_b_scenario_consistency']} "
              f"(count={g['scenario_consistency']})", flush=True)
        print(f"  Gate C (no scenario < -0.5): {g['gate_c_no_scenario_regression']}", flush=True)
        print(f"  Gate D (no core dim < -0.5): {g['gate_d_no_core_regression']}", flush=True)
    else:
        print(f"FINAL RESULT: INVALID (no PASS/FAIL) — stop_reason: {result.stop_reason}",
              flush=True)
        if result.invalid_scenarios:
            print(f"  Invalid scenarios: {result.invalid_scenarios}", flush=True)
    print(f"Evidence: {result.run_dir}", flush=True)
    # Exit 0 only on a valid PASS.  Valid FAIL = 1.  Invalid = 1.
    # (A valid FAIL is a completed qualification — the CLI signals the result,
    # not an error.  But conventional CLI exit codes treat non-pass as 1 so
    # downstream tooling can distinguish pass/fail by exit code.)
    return 0 if (result.valid and result.final_pass) else 1


def _make_dry_run_deps() -> RunnerDeps:
    """Build mock deps for offline dry-run (zero paid calls)."""
    return RunnerDeps(
        baseline_executor=_mock_baseline_executor,
        mktapp_executor=_mock_mktapp_executor,
        judge_executor=_mock_judge_executor,
        api_key=None,
    )


def _mock_baseline_executor(
    scenario: UpliftScenario, fixture: SourceFixture, budget: CandidateBudgetCaps,
) -> CandidateResult:
    """Mock baseline — produces a deterministic COMPLETE candidate, no paid call."""
    text = f"[MOCK BASELINE] {scenario.id} {scenario.agent_key} output."
    return CandidateResult(
        scenario_id=scenario.id, side="baseline", model=scenario.agent_model(),
        output_text=text,
        output_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        completeness="COMPLETE",
        final_finish_reason="stop", final_truncated=False,
        call_count=0, cost_usd=0.0,
        source_fixture_hash=fixture.hash(),
    )


def _mock_mktapp_executor(
    scenario: UpliftScenario, fixture: SourceFixture, budget: CandidateBudgetCaps,
) -> CandidateResult:
    """Mock MKTApp — produces a deterministic COMPLETE candidate, no paid call.

    For S4 (content_creator) produces valid JSON so the frozen renderer works.
    """
    if scenario.agent_key == "content_creator":
        text = json.dumps({
            "posts": [{
                "platform": "tiktok",
                "caption": "[MOCK MKTApp] S4 caption",
                "script": "[MOCK MKTApp] S4 script",
                "hashtags": ["#mock"],
            }],
        }, ensure_ascii=False)
    else:
        text = f"[MOCK MKTApp] {scenario.id} {scenario.agent_key} output."
    return CandidateResult(
        scenario_id=scenario.id, side="mktapp", model=scenario.agent_model(),
        output_text=text,
        output_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        completeness="COMPLETE",
        final_finish_reason="stop", final_truncated=False,
        call_count=0, cost_usd=0.0,
        source_fixture_hash=fixture.hash(),
    )


def _mock_judge_executor(
    scenario_dict: dict, run_dir: Path, api_key: str | None,
    budget_guard_fn: Callable[[], None] | None,
    budget_commit_fn: Callable[[float], None] | None,
) -> Any:
    """Mock Judge — produces deterministic X/Y scores, no paid call."""
    from m6_judge_runner import JudgeResult
    sid = scenario_dict["id"]
    # Deterministic scores: X=4, Y=3 on every dimension (MKTApp wins regardless of mapping)
    dims = ["Usefulness", "Factuality", "Instruction following",
            "Brand / asset fit", "Evidence quality", "User effort"]
    scores = {d: {"X": 4, "Y": 3, "winner": "X", "tie": False,
                  "reason": "mock"} for d in dims}
    raw_judge_json = {
        "scores": scores,
        "overall": {"X_mean_score": 4.0, "Y_mean_score": 3.0, "winner": "X",
                    "tie": False, "decisive_reasons": ["mock"],
                    "confidence": 0.9, "insufficient_evidence": False,
                    "insufficient_evidence_reasons": []},
    }
    return JudgeResult(
        scenario_id=sid, raw_judge_json=raw_judge_json,
        actual_model=judge_runner.JUDGE_MODEL,
        prompt_tokens=100, completion_tokens=100, cost_usd=0.0,
    )


if __name__ == "__main__":
    sys.exit(main())
