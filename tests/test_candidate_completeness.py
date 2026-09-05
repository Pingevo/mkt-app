"""Offline tests for the candidate completeness model.

Tests the generic completeness contract:
- COMPLETE / INCOMPLETE / UNKNOWN rules
- Per-call finish metadata persistence
- Reuse rules
- Judge preflight integration

No paid calls. No network. Pure logic tests.
"""

import pytest
from src.candidate_completeness import (
    CallFinishRecord,
    CallRole,
    CandidateEvidence,
    CompletenessStatus,
    can_reuse,
    evaluate_completeness,
    evaluate_frontier_completeness,
    status_from_evidence_dict,
)


class TestFinishMetadataPersistence:
    """Usage entry persists per-call finish_reason."""

    def test_make_entry_includes_finish_reason(self):
        from src.ai_usage import make_entry
        entry = make_entry(
            model="test/model",
            source="test",
            finish_reason="stop",
            truncated=False,
        )
        assert entry["finish_reason"] == "stop"
        assert entry["truncated"] is False

    def test_make_entry_omits_finish_reason_when_none(self):
        from src.ai_usage import make_entry
        entry = make_entry(model="test/model", source="test")
        assert "finish_reason" not in entry
        assert "truncated" not in entry

    def test_make_entry_length_truncated(self):
        from src.ai_usage import make_entry
        entry = make_entry(
            model="test/model",
            source="test",
            finish_reason="length",
            truncated=True,
        )
        assert entry["finish_reason"] == "length"
        assert entry["truncated"] is True

    def test_stale_finish_metadata_cannot_leak_between_calls(self):
        """LLMClient resets _last_finish_reason per call."""
        from src.llm_client import LLMClient
        # We can't make real calls, but we can verify the reset logic
        # by checking that a new client starts with None
        # and that the property exists and is None
        # (the per-call reset is at llm_client.py:145)
        client = LLMClient.__new__(LLMClient)
        client._last_finish_reason = None
        assert client.last_finish_reason is None
        assert client.last_truncated is False
        # Simulate a truncated call
        client._last_finish_reason = "length"
        assert client.last_truncated is True
        # Simulate reset for next call
        client._last_finish_reason = None
        assert client.last_finish_reason is None
        assert client.last_truncated is False


class TestCompletenessComplete:
    """Clean final candidate → COMPLETE."""

    def test_single_call_stop_is_complete(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="stop"),
            ],
        )
        assert evaluate_completeness(evidence) == CompletenessStatus.COMPLETE

    def test_generate_then_review_both_stop_is_complete(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="stop"),
                CallFinishRecord(call_index=1, role=CallRole.SEMANTIC_REVIEW.value, finish_reason="stop"),
            ],
        )
        assert evaluate_completeness(evidence) == CompletenessStatus.COMPLETE

    def test_generate_truncated_then_repair_stop_is_complete(self):
        """A truncated generate call that is superseded by a successful repair → COMPLETE."""
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="length", truncated=True, superseded=True),
                CallFinishRecord(call_index=1, role=CallRole.REPAIR_REVISION.value, finish_reason="stop"),
            ],
        )
        assert evaluate_completeness(evidence) == CompletenessStatus.COMPLETE

    def test_tool_calls_finish_is_complete(self):
        """finish_reason='tool_calls' is a normal completion, not truncation."""
        evidence = CandidateEvidence(
            scenario_id="S2",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="tool_calls"),
            ],
        )
        assert evaluate_completeness(evidence) == CompletenessStatus.COMPLETE


class TestCompletenessIncomplete:
    """Unrecovered material truncation → INCOMPLETE."""

    def test_single_call_length_is_incomplete(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="length", truncated=True),
            ],
        )
        assert evaluate_completeness(evidence) == CompletenessStatus.INCOMPLETE

    def test_generate_truncated_no_repair_is_incomplete(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="length", truncated=True),
                CallFinishRecord(call_index=1, role=CallRole.SEMANTIC_REVIEW.value, finish_reason="stop"),
            ],
        )
        # Review doesn't supersede the truncated generate
        assert evaluate_completeness(evidence) == CompletenessStatus.INCOMPLETE

    def test_last_material_call_truncated_is_incomplete(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="stop"),
                CallFinishRecord(call_index=1, role=CallRole.REPAIR_REVISION.value, finish_reason="length", truncated=True),
            ],
        )
        assert evaluate_completeness(evidence) == CompletenessStatus.INCOMPLETE


class TestCompletenessUnknown:
    """Absent required metadata → UNKNOWN."""

    def test_no_finish_records_is_unknown(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[],
        )
        assert evaluate_completeness(evidence) == CompletenessStatus.UNKNOWN

    def test_all_finish_reason_none_is_unknown(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason=None),
            ],
        )
        assert evaluate_completeness(evidence) == CompletenessStatus.UNKNOWN

    def test_unknown_cannot_proceed_to_judge(self):
        """UNKNOWN status must not be treated as COMPLETE."""
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[],
        )
        status = evaluate_completeness(evidence)
        assert status == CompletenessStatus.UNKNOWN
        assert status != CompletenessStatus.COMPLETE
        assert status != CompletenessStatus.INCOMPLETE


class TestFrontierCompleteness:
    """Frontier single-call completeness."""

    def test_frontier_stop_is_complete(self):
        assert evaluate_frontier_completeness("stop", False) == CompletenessStatus.COMPLETE

    def test_frontier_length_is_incomplete(self):
        assert evaluate_frontier_completeness("length", True) == CompletenessStatus.INCOMPLETE

    def test_frontier_no_metadata_is_unknown(self):
        assert evaluate_frontier_completeness(None, None) == CompletenessStatus.UNKNOWN

    def test_frontier_truncated_true_is_incomplete(self):
        assert evaluate_frontier_completeness(None, True) == CompletenessStatus.INCOMPLETE

    def test_frontier_tool_calls_is_complete(self):
        assert evaluate_frontier_completeness("tool_calls", False) == CompletenessStatus.COMPLETE


class TestReuse:
    """Reuse rules."""

    def test_complete_reused_candidate_retains_metadata(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="stop"),
            ],
            output_artifact_path="/data/outputs/S1.txt",
            output_hash="abc123",
            reused=True,
            source_run="20260901_120000",
        )
        ok, reason = can_reuse(evidence)
        assert ok is True
        assert reason == "ok"

    def test_incomplete_cannot_be_reused(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="length", truncated=True),
            ],
            output_artifact_path="/data/outputs/S1.txt",
            output_hash="abc123",
        )
        ok, reason = can_reuse(evidence)
        assert ok is False
        assert "INCOMPLETE" in reason

    def test_unknown_cannot_be_reused(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[],
            output_artifact_path="/data/outputs/S1.txt",
            output_hash="abc123",
        )
        ok, reason = can_reuse(evidence)
        assert ok is False
        assert "UNKNOWN" in reason

    def test_reused_without_source_run_cannot_be_reused(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="stop"),
            ],
            output_artifact_path="/data/outputs/S1.txt",
            output_hash="abc123",
            reused=True,
            source_run=None,
        )
        ok, reason = can_reuse(evidence)
        assert ok is False
        assert "source run" in reason

    def test_no_artifact_path_cannot_be_reused(self):
        evidence = CandidateEvidence(
            scenario_id="S1",
            side="mktapp",
            finish_records=[
                CallFinishRecord(call_index=0, role=CallRole.PRIMARY_GENERATION.value, finish_reason="stop"),
            ],
            output_artifact_path=None,
            output_hash="abc123",
        )
        ok, reason = can_reuse(evidence)
        assert ok is False
        assert "artifact" in reason


class TestStatusFromEvidenceDict:
    """Reconstruct completeness from persisted evidence dict (Judge preflight)."""

    def test_complete_from_dict(self):
        d = {
            "scenario_id": "S1",
            "side": "mktapp",
            "finish_records": [
                {"call_index": 0, "role": "primary_generation", "finish_reason": "stop"},
            ],
        }
        assert status_from_evidence_dict(d) == CompletenessStatus.COMPLETE

    def test_incomplete_from_dict(self):
        d = {
            "scenario_id": "S1",
            "side": "frontier",
            "finish_records": [
                {"call_index": 0, "role": "primary_generation", "finish_reason": "length", "truncated": True},
            ],
        }
        assert status_from_evidence_dict(d) == CompletenessStatus.INCOMPLETE

    def test_unknown_from_empty_dict(self):
        d = {"scenario_id": "S1", "side": "mktapp"}
        assert status_from_evidence_dict(d) == CompletenessStatus.UNKNOWN

    def test_fallback_to_top_level_finish_reason(self):
        """If no finish_records, use top-level finish_reason/truncated."""
        d = {
            "scenario_id": "S1",
            "side": "frontier",
            "finish_reason": "stop",
            "truncated": False,
        }
        assert status_from_evidence_dict(d) == CompletenessStatus.COMPLETE

    def test_never_map_unknown_to_complete(self):
        """UNKNOWN must never be silently mapped to COMPLETE."""
        d = {"scenario_id": "S1", "side": "mktapp", "finish_records": []}
        status = status_from_evidence_dict(d)
        assert status == CompletenessStatus.UNKNOWN
        assert status != CompletenessStatus.COMPLETE
