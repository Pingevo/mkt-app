"""Candidate completeness model for qualification harness.

Defines a first-class completeness status for every qualification candidate
(MKTApp or direct baseline) so the Judge preflight can mechanically reject
incomplete or unknown-state candidates instead of silently scoring them.

Status values:
  COMPLETE   — final candidate has acceptable finish metadata and no
               unrecovered material truncation.
  INCOMPLETE — final candidate has an unrecovered truncation that
               materially contributes to the accepted output.
  UNKNOWN    — required metadata is absent; completeness cannot be
               established.  UNKNOWN is NOT treated as COMPLETE.

Design principles (per AGENTS.md / mktapp-remediation skill):
  - Generic across all scenarios, agents, brands, and models.
  - Mechanically verifiable only — no semantic judgment, no keyword lists.
  - A truncation that materially contributes to the accepted final candidate
    and is not successfully superseded/recovered invalidates the candidate.
  - A failed attempt that is explicitly discarded and successfully replaced
    may remain audit evidence without invalidating the final candidate.
  - Unknown is not false — absent metadata cannot be treated as complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CompletenessStatus(str, Enum):
    """First-class candidate completeness state."""
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNKNOWN = "UNKNOWN"


# Call roles within a multi-call agent scenario.
# Used to classify which LLM calls materially contribute to the final
# candidate vs. which are discarded/replaced intermediate attempts.
class CallRole(str, Enum):
    PRIMARY_GENERATION = "primary_generation"
    TOOL_CONTINUATION = "tool_continuation"
    SEMANTIC_REVIEW = "semantic_review"
    REPAIR_REVISION = "repair_revision"
    DOWNSTREAM_INTERPRETATION = "downstream_interpretation"


# Roles whose truncation materially contributes to the final accepted
# candidate.  If any of these finish with "length" and the output is not
# subsequently superseded, the candidate is INCOMPLETE.
MATERIAL_ROLES = frozenset({
    CallRole.PRIMARY_GENERATION,
    CallRole.TOOL_CONTINUATION,
    CallRole.REPAIR_REVISION,
})

# Roles that are advisory/intermediate.  Truncation in these does NOT
# automatically invalidate the candidate — they may be discarded or
# superseded by a later successful call.
ADVISORY_ROLES = frozenset({
    CallRole.SEMANTIC_REVIEW,
    CallRole.DOWNSTREAM_INTERPRETATION,
})


@dataclass
class CallFinishRecord:
    """Per-call finish metadata for one LLM call within a scenario.

    Persisted as evidence so completeness can be determined post-hoc
    without relying on the last-call-only ``LLMClient.last_finish_reason``.
    """
    call_index: int
    role: str  # CallRole value
    finish_reason: str | None = None
    truncated: bool | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    max_tokens: int | None = None
    request_id: str | None = None
    model: str | None = None
    source: str | None = None
    # Whether this call's output was superseded by a later successful call.
    # True = this was an intermediate attempt that was discarded/replaced.
    superseded: bool = False

    def is_truncated(self) -> bool:
        """Mechanically determine if this call was truncated by token limit."""
        if self.truncated is not None:
            return bool(self.truncated)
        if self.finish_reason == "length":
            return True
        return False


@dataclass
class CandidateEvidence:
    """Evidence persisted for each qualification candidate.

    Contains enough information to determine completeness mechanically
    without re-running the agent or making paid calls.
    """
    scenario_id: str
    side: str  # "mktapp" or "baseline"
    model: str | None = None
    run_id: str | None = None
    output_artifact_path: str | None = None
    output_hash: str | None = None
    finish_records: list[CallFinishRecord] = field(default_factory=list)
    # Whether this candidate was reused from a prior run or freshly generated.
    reused: bool = False
    source_run: str | None = None  # if reused, the original run ID
    # Provenance fingerprints (for reuse validation)
    scenario_fingerprint: str | None = None
    input_fingerprint: str | None = None
    model_fingerprint: str | None = None
    # Final answer metadata
    final_finish_reason: str | None = None
    final_truncated: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "side": self.side,
            "model": self.model,
            "run_id": self.run_id,
            "output_artifact_path": self.output_artifact_path,
            "output_hash": self.output_hash,
            "finish_records": [
                {
                    "call_index": r.call_index,
                    "role": r.role,
                    "finish_reason": r.finish_reason,
                    "truncated": r.truncated,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "max_tokens": r.max_tokens,
                    "request_id": r.request_id,
                    "model": r.model,
                    "source": r.source,
                    "superseded": r.superseded,
                }
                for r in self.finish_records
            ],
            "reused": self.reused,
            "source_run": self.source_run,
            "scenario_fingerprint": self.scenario_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "model_fingerprint": self.model_fingerprint,
            "final_finish_reason": self.final_finish_reason,
            "final_truncated": self.final_truncated,
        }


def evaluate_completeness(evidence: CandidateEvidence) -> CompletenessStatus:
    """Determine candidate completeness from persisted evidence.

    Algorithm (generic, no scenario-specific logic):

    1. If no finish_records exist at all → UNKNOWN
       (we have no metadata to make any determination)

    2. If the final call (last non-superseded material call) has
       finish_reason == "length" → INCOMPLETE
       (the accepted output was truncated and not recovered)

    3. If any non-superseded material-role call has finish_reason == "length"
       and no later material-role call superseded it → INCOMPLETE

    4. If finish metadata is absent for all calls (all finish_reason == None)
       → UNKNOWN (we cannot prove completeness)

    5. Otherwise → COMPLETE
       (all material calls finished with "stop" or "tool_calls",
        or truncated calls were superseded by later successful calls)

    Advisory-role truncations (semantic_review, downstream_interpretation)
    do NOT invalidate the candidate because they do not produce the
    accepted final output.
    """
    records = evidence.finish_records
    if not records:
        return CompletenessStatus.UNKNOWN

    # Check if we have ANY finish metadata at all
    has_any_metadata = any(r.finish_reason is not None for r in records)
    if not has_any_metadata:
        return CompletenessStatus.UNKNOWN

    # Find material calls that were NOT superseded
    material_calls = [
        r for r in records
        if r.role in [mr.value for mr in MATERIAL_ROLES] and not r.superseded
    ]

    if not material_calls:
        # No material calls — only advisory calls existed.
        # If advisory calls have finish_reason, treat as COMPLETE
        # (the candidate was produced by some other mechanism).
        # But if final_finish_reason is None, we can't be sure.
        if evidence.final_finish_reason is None:
            return CompletenessStatus.UNKNOWN
        if evidence.final_finish_reason == "length":
            return CompletenessStatus.INCOMPLETE
        return CompletenessStatus.COMPLETE

    # Check the last material call (the one that produced the accepted output)
    last_material = material_calls[-1]
    if last_material.is_truncated():
        return CompletenessStatus.INCOMPLETE

    # Check if any earlier non-superseded material call was truncated
    # and was NOT followed by a successful (non-truncated) material call
    for i, call in enumerate(material_calls):
        if call.is_truncated():
            # Was this truncation superseded by a later material call?
            later_calls = material_calls[i + 1:]
            if not any(not lc.is_truncated() for lc in later_calls):
                # No later successful material call → truncation stands
                return CompletenessStatus.INCOMPLETE

    return CompletenessStatus.COMPLETE


def evaluate_single_call_completeness(
    finish_reason: str | None,
    truncated: bool | None,
    completion_tokens: int | None = None,
    max_tokens: int | None = None,
) -> CompletenessStatus:
    """Evaluate completeness for a single-call candidate (direct baseline).

    A direct baseline makes exactly one /chat/completions call per scenario
    (web search is a server-side tool within that call).

    Rules:
    - finish_reason == "length" → INCOMPLETE (truncated by token budget)
    - finish_reason == "stop" or "tool_calls" → COMPLETE
    - finish_reason is None → UNKNOWN (metadata absent)
    - truncated == True (explicitly) → INCOMPLETE
    - truncated == False and finish_reason is not None → COMPLETE
    """
    if finish_reason is None and truncated is None:
        return CompletenessStatus.UNKNOWN
    if finish_reason == "length":
        return CompletenessStatus.INCOMPLETE
    if truncated is True:
        return CompletenessStatus.INCOMPLETE
    if finish_reason in ("stop", "tool_calls"):
        return CompletenessStatus.COMPLETE
    if truncated is False and finish_reason is not None:
        return CompletenessStatus.COMPLETE
    # finish_reason is some unknown value — can't determine
    return CompletenessStatus.UNKNOWN


# Backward-compatible alias for historical callers.
evaluate_frontier_completeness = evaluate_single_call_completeness


def can_reuse(evidence: CandidateEvidence) -> tuple[bool, str]:
    """Determine if a candidate can be reused for a clean comparison.

    Returns (can_reuse, reason).

    A candidate is reusable only when ALL of:
    - artifact verified (output_artifact_path and output_hash present)
    - scenario fingerprint matches (caller must verify externally)
    - input fingerprint matches (caller must verify externally)
    - model identity permits reuse (caller must verify externally)
    - completeness is COMPLETE
    - source provenance is available (source_run for reused candidates)

    INCOMPLETE and UNKNOWN candidates cannot be reused as clean candidates.
    """
    if not evidence.output_artifact_path:
        return False, "no output artifact path"
    if not evidence.output_hash:
        return False, "no output hash"
    if evidence.reused and not evidence.source_run:
        return False, "reused candidate has no source run provenance"

    status = evaluate_completeness(evidence)
    if status == CompletenessStatus.INCOMPLETE:
        return False, f"candidate is {status.value} — truncation not recovered"
    if status == CompletenessStatus.UNKNOWN:
        return False, f"candidate is {status.value} — completeness cannot be established"

    return True, "ok"


def status_from_evidence_dict(evidence_dict: dict[str, Any]) -> CompletenessStatus:
    """Reconstruct completeness status from a persisted evidence dict.

    Used by Judge preflight to read m6_evidence.json and determine
    candidate completeness without re-running anything.
    """
    finish_records_data = evidence_dict.get("finish_records", [])
    if not finish_records_data:
        # Fallback: check top-level finish metadata
        fr = evidence_dict.get("finish_reason")
        trunc = evidence_dict.get("truncated")
        if fr is None and trunc is None:
            return CompletenessStatus.UNKNOWN
        return evaluate_single_call_completeness(fr, trunc)

    records = [
        CallFinishRecord(
            call_index=r.get("call_index", i),
            role=r.get("role", CallRole.PRIMARY_GENERATION.value),
            finish_reason=r.get("finish_reason"),
            truncated=r.get("truncated"),
            superseded=r.get("superseded", False),
        )
        for i, r in enumerate(finish_records_data)
    ]
    evidence = CandidateEvidence(
        scenario_id=evidence_dict.get("scenario_id", ""),
        side=evidence_dict.get("side", ""),
        finish_records=records,
        final_finish_reason=evidence_dict.get("final_finish_reason"),
        final_truncated=evidence_dict.get("final_truncated"),
    )
    return evaluate_completeness(evidence)
