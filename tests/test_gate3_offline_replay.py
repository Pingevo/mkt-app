"""Phase 6: Offline replay of the paid Gate 3 run.

Replays the actual output from the 2026-08-31T05-46-01 paid Gate 3 run
through the *current* validator to confirm that the Phase 2-4 fixes
address the original failures — without making any new API calls.

Original failures:
  - indicative_retail_promo_labelled: false
    (strategic price hypothesis with positioning was flagged)
  - benchmarks_cited_or_removed: true (BUG — should have been false)
    (uncited 1.5%-3.0% benchmark was not detected)

After fixes:
  - indicative_retail_promo_labelled: should be true
    (labelled estimate with positioning rationale is now accepted)
  - benchmarks_cited_or_removed: should be false
    (uncited benchmark is now detected)
  - product_identity_intact: should be true
    (URL percent-encoded fragments no longer produce false model tokens)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.campaign_validator import audit_campaign_output, extract_context_flags
from src.config_loader import get_agent_config, load_config

RUN_DIR = Path(__file__).resolve().parent.parent / "data" / "campaign_paid_acceptance" / "run-2026-08-31T05-46-01.25174"


def _load_gate3_state() -> dict:
    state_path = RUN_DIR / "gate_3_state.json"
    if not state_path.exists():
        pytest.skip(f"Gate 3 state artifact not found: {state_path}")
    return json.loads(state_path.read_text(encoding="utf-8"))


def _load_gate3_output() -> str:
    output_path = RUN_DIR / "gate_3.md"
    if not output_path.exists():
        pytest.skip(f"Gate 3 output artifact not found: {output_path}")
    return output_path.read_text(encoding="utf-8")


def _rules() -> dict:
    return get_agent_config(load_config(), "campaign_strategy").get("semantic_rules", {})


def _build_context_from_state(state: dict) -> dict[str, str]:
    """Reconstruct the context dict that was used for the Gate 3 run."""
    relevant = state.get("relevant_annotations", [])
    competitor_urls = "\n".join(
        f"- [{a.get('title', a.get('url', ''))}]({a.get('url', '')})" for a in relevant
    )
    return {
        "product": "Product: LAGENIO K2 smartwatch for kids. Official model K2.",
        "business": "",
        "competitors": competitor_urls,
        "market": "Mid-range kids smartwatch market in Thailand.",
        "customers": "Urban parents, working moms aged 25-45.",
    }


def _build_instructions_from_state(state: dict) -> dict:
    """Reconstruct selected_evidence_urls and evidence snippets from the Gate 3 state."""
    relevant = state.get("relevant_annotations", [])
    return {
        "selected_evidence_urls": [a.get("url", "") for a in relevant if a.get("url")],
        "selected_evidence": relevant,
    }


@pytest.fixture
def gate3_state() -> dict:
    return _load_gate3_state()


@pytest.fixture
def gate3_output() -> str:
    return _load_gate3_output()


class TestGate3OfflineReplay:
    """Replay the paid Gate 3 output through the current validator."""

    def test_strategic_price_hypothesis_now_fails_strict_rule(self, gate3_output: str, gate3_state: dict):
        """Stricter rule: the old Gate 3 price line has a pending label but
        NO inline citation on the price line itself.  Under the stricter
        Basis B enforcement, this must now fail.

        Original: false (flagged for wrong reason — no COGS)
        Intermediate fix: true (labelled estimate with positioning accepted)
        Final stricter fix: false (no inline citation on price line → no basis)

        The old output's rationale paragraph cites the competitor on a
        separate line, but the price line itself carries no citation.
        The stricter rule requires the citation on the same line as the
        recommended price, so that each numeric claim is individually grounded.
        """
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            gate3_output, flags, instructions, _rules()
        )
        retail = next(r for r in results if r.rule == "indicative_retail_promo_labelled")
        assert not retail.ok, (
            f"Old Gate 3 price line lacks inline citation → must fail stricter rule. "
            f"Reason: {retail.reason}"
        )

    def test_uncited_benchmark_now_detected(self, gate3_output: str, gate3_state: dict):
        """Phase 4 fix: the uncited 1.5%-3.0% benchmark should now be detected.

        Original: true (BUG — benchmark detection missed it)
        After fix: false (uncited benchmark is now correctly flagged)
        """
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            gate3_output, flags, instructions, _rules()
        )
        bench = next(r for r in results if r.rule == "benchmarks_cited_or_removed")
        assert not bench.ok, (
            f"Uncited 1.5%-3.0% benchmark should now be detected after Phase 4 fix. "
            f"Reason: {bench.reason}"
        )

    def test_product_identity_intact(self, gate3_output: str, gate3_state: dict):
        """Phase 2 fix: URL percent-encoded fragments should not produce
        false model tokens.

        The Gate 3 output contains URLs with percent-encoded Thai text
        (e.g. %E0%B8%AA) which previously produced false tokens like 'e0'.
        """
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            gate3_output, flags, instructions, _rules()
        )
        identity = next(r for r in results if r.rule == "product_identity_intact")
        assert identity.ok, (
            f"Product identity should be intact — URL fragments must not "
            "produce false model tokens. Reason: {identity.reason}"
        )

    def test_competitor_mentions_grounded(self, gate3_output: str, gate3_state: dict):
        """Competitor mentions should be grounded with inline citations
        to selected evidence URLs."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            gate3_output, flags, instructions, _rules()
        )
        competitor = next(r for r in results if r.rule == "competitor_mentions_grounded")
        assert competitor.ok, (
            f"Competitor mentions should be grounded. Reason: {competitor.reason}"
        )

    def test_no_bare_urls(self, gate3_output: str, gate3_state: dict):
        """No bare URLs should appear in the output."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            gate3_output, flags, instructions, _rules()
        )
        bare = next(r for r in results if r.rule == "no_bare_urls")
        assert bare.ok, f"No bare URLs should appear. Reason: {bare.reason}"

    def test_cost_mechanics_pending(self, gate3_output: str, gate3_state: dict):
        """Cost-bearing mechanics should be marked pending financial validation."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            gate3_output, flags, instructions, _rules()
        )
        cost = next(r for r in results if r.rule == "cost_mechanics_pending_financial_validation")
        assert cost.ok, f"Cost mechanics should be pending. Reason: {cost.reason}"

    def test_no_guaranteed_targets_without_baseline(self, gate3_output: str, gate3_state: dict):
        """No guaranteed numeric targets without baseline."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            gate3_output, flags, instructions, _rules()
        )
        target = next(r for r in results if r.rule == "no_guaranteed_numeric_targets_without_baseline")
        assert target.ok, f"No guaranteed targets without baseline. Reason: {target.reason}"

    def test_original_gate3_validator_results_recorded(self, gate3_state: dict):
        """Sanity check: the original Gate 3 state records the failures we
        expect to fix."""
        validator_results = gate3_state.get("validator_results", [])
        rules_by_name = {r["rule"]: r for r in validator_results}

        # Original failure: indicative_retail_promo_labelled was false
        retail = rules_by_name.get("indicative_retail_promo_labelled", {})
        assert not retail.get("ok"), (
            "Original Gate 3 should have recorded indicative_retail_promo_labelled as false"
        )

        # Original bug: benchmarks_cited_or_removed was true (should have been false)
        bench = rules_by_name.get("benchmarks_cited_or_removed", {})
        assert bench.get("ok"), (
            "Original Gate 3 should have recorded benchmarks_cited_or_removed as true (bug)"
        )
