"""Pending AI enrichment proposals — FREE-IMPORT-AI-PROPOSAL-01.

Authority model (existing hierarchy, extended — no migration):

  source/extracted data → product.json (files, raw_text, derived_facts)
  canonical/user data   → product_profile.json (facts + positioning fields)
                          + product.json["metadata"] (summary, category)
  pending AI proposals  → product.json["ai_proposal"]  — proposal only
  AI workflow state     → product.json["ai_enrichment"] — status/errors,
                          separate from product readiness

Hard rules enforced here:
  * Generation writes ONLY to ai_proposal + ai_enrichment.  Canonical data
    is byte-identical before and after a generation run.
  * resolve_proposal is the ONLY path that mutates canonical data, per
    field, and only after the user picks accept/edit for that field.
  * accept is Git-like: the canonical value must still equal the baseline
    snapshot taken at generation time.  Drift → the field is returned as a
    conflict, untouched; the user re-decides against refreshed current.
  * edit writes the user's explicit value unconditionally (user authority
    outranks everything — they chose the value knowing the suggestion).
  * keep resolves the field with no write.
  * Re-running generation requires replace=True when a proposal is still
    pending — two generations are never silently merged.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import product_db
from .brand_loader import load_product_profile

# Canonical product_profile fields a marketing-scope proposal may suggest.
PROFILE_FIELDS = (
    "audience", "competitors", "differentiators", "use_cases",
    "price_tier", "tone_adjustment", "visual_override",
)

# Scopes accepted by the enrich endpoint.
SCOPES = ("facts", "marketing")


class ProposalPending(RuntimeError):
    """An unresolved proposal already exists — rerun needs replace=True."""


class StaleProposal(RuntimeError):
    """proposal_id does not match the stored pending proposal."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_root() -> Path:
    """Brand-scoped state root (same resolution as staging/product_db)."""
    from .workspace_context import brand_state_root
    return brand_state_root(Path(__file__).resolve().parent.parent)


def _save_profile(product_id: str, profile: dict) -> None:
    """Write product_profile.json into cache/{product_id}/ — same file and
    merge semantics as /api/product_profile_save."""
    from .workspace_context import contain_path
    pdir = contain_path(product_id, _state_root() / "cache")
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def _eff_text(profile: dict, meta: dict, key: str) -> str:
    """Effective canonical text for summary/category: the user's explicit
    profile value wins over the deterministic metadata mirror (same rule
    _generate_metadata_summary applies on every ingest)."""
    return str(profile.get(key) or meta.get(key) or "")


def _source_revision(product_id: str, record: dict) -> str:
    """Deterministic fingerprint of the source-derived evidence product AI
    actually sees — the guard against accepting obsolete proposals.

    Covers the source-derived record fields both scopes consume:
    ``raw_text`` (facts scope), per-file ``text_extracts``, image
    descriptions, video/audio transcripts, ``derived_facts``, the
    source-file manifest and ``scope`` (marketing context is composed
    from these).  Canonical user facts are deliberately excluded — a
    manual edit is canonical drift, handled per-field below, not a
    source change.  Content hashes are the identity; names/paths are
    reduced to basenames, and ordering is sorted — nothing here depends
    on absolute paths, timestamps, dict order, process state, or
    product/brand naming.
    """
    def _fname(entry):
        if not isinstance(entry, dict):
            return ""
        return entry.get("name") or entry.get("file") \
            or Path(entry.get("path", "")).name

    evidence = {
        "raw_text": record.get("raw_text") or "",
        "files": sorted(
            (_fname(f), f.get("hash") or "")
            for f in record.get("files", []) if isinstance(f, dict)),
        "text_extracts": sorted(
            (t.get("file") or "", t.get("text") or "")
            for t in record.get("text_extracts", [])
            if isinstance(t, dict)),
        "images": sorted(
            (d.get("file") or Path(d.get("path", "")).name,
             d.get("description") or "")
            for d in record.get("image_descriptions", [])
            if isinstance(d, dict)),
        "video": sorted(
            (t.get("file") or "", t.get("transcript") or "")
            for t in record.get("video_transcripts", [])
            if isinstance(t, dict)),
        "audio": sorted(
            (t.get("file") or "", t.get("transcript") or "")
            for t in record.get("audio_transcripts", [])
            if isinstance(t, dict)),
        "derived_facts": record.get("derived_facts") or {},
        "scope": record.get("scope") or {},
    }
    blob = json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _source_stale(product_id: str, record: dict, baseline: dict) -> bool:
    """Is the evidence the proposal was generated against still current?

    Two checks, one answer:
      - ``source_rev`` mismatch (or missing — legacy proposals fail safe)
        covers mutations already persisted to product.json by re-ingest.
      - ``find_stale_files`` covers disk mutations still PENDING ingest —
        ``save_uploaded_files`` lands bytes before product.json is
        rebuilt, and deletes/replaces move disk first too.  The shared
        re-ingest scanner is the single definition of "source changed
        on disk" — reused here instead of a second filesystem walk.
    """
    if baseline.get("source_rev") != _source_revision(product_id, record):
        return True
    return bool(product_db.find_stale_files(product_id))


# --- Deterministic unchanged-suppression -----------------------------------
# The review surface exists to approve CHANGES.  Before a proposal is
# stored, each proposed value is compared against the canonical baseline
# snapshot — deterministically: no model call, no semantic matching
# ("กันน้ำ" ≠ "กันน้ำระดับ IP68").  Equality is FIELD-AWARE and declared
# below — never inferred from how persistence happens to store bytes:
#
#   * scalars normalize trim + repeated whitespace (+ the shared URL
#     normalization); case is significant;
#   * collections compare ORDER-SENSITIVELY — only fields declared
#     set-like in _UNORDERED_PROFILE_FIELDS ignore member order;
#   * missing ≡ empty only where the field's canonical schema has a single
#     "absent" state — and only at field level.  Inside a value an
#     explicit "" member or empty-valued key stays significant;
#   * facts are never missing-equivalent: a fact's presence is itself
#     information — an empty proposed fact still creates a fact row (and
#     shadows a same-keyed derived fact).

# Profile fields whose members carry no positional meaning — parallel
# attribute lists where the same items in another order are the same
# profile.  ``audience`` is a structured dict; the flag propagates into
# its nested attribute lists (lifestyle, pain_points, channels, ...).
# ``visual_override`` and fact values stay order-sensitive: if order ever
# encodes priority there, an unordered compare would hide a real diff.
_UNORDERED_PROFILE_FIELDS = frozenset({
    "audience", "competitors", "differentiators", "use_cases",
})


def _canon_text(v) -> str:
    """Scalar comparison form — trim + collapse whitespace runs; URL values
    go through the shared URL normalization helper (same rule validators
    use).  Case is significant for non-URL text."""
    s = " ".join(str(v).split())
    if s.lower().startswith(("http://", "https://")):
        from .output_validators import _normalize_url
        return _normalize_url(s)
    return s


def _cmp_form(v, *, unordered: bool):
    """Strict structural comparison form: NO empty collapse — None, "",
    [], {} stay distinct — and member order is preserved unless the field
    was declared set-like (``unordered`` propagates to nested collections
    of that field).  Dict keys compare sorted (JSON object order is never
    meaningful).  Returns a deterministically sortable form."""
    if v is None:
        return ("none",)
    if isinstance(v, bool):
        return ("scalar", str(v))
    if isinstance(v, (int, float)):
        return ("scalar", str(v))
    if isinstance(v, str):
        return ("scalar", _canon_text(v))
    if isinstance(v, dict):
        return ("dict", tuple(sorted(
            (str(k), _cmp_form(x, unordered=unordered))
            for k, x in v.items())))
    if isinstance(v, (list, tuple)):
        items = tuple(_cmp_form(x, unordered=unordered) for x in v)
        if unordered:
            items = tuple(sorted(items, key=repr))
        return ("list", items)
    return ("scalar", _canon_text(v))


def _is_empty(v) -> bool:
    """Canonical 'no value' shapes — consulted only for fields whose schema
    treats every empty representation as 'absent'.  A dict/list holding
    any content (even an empty string member) is not empty."""
    if v is None:
        return True
    if isinstance(v, str):
        return _canon_text(v) == ""
    return isinstance(v, (list, tuple, dict)) and not v


def _unchanged(proposed, baseline, *, unordered: bool,
               missing_equiv: bool) -> bool:
    """Field-level equality: empty-collapse applies only where the field's
    schema declares all empty shapes equivalent to 'absent'; everything
    inside a value compares strictly per _cmp_form."""
    if missing_equiv and _is_empty(proposed) and _is_empty(baseline):
        return True
    return (_cmp_form(proposed, unordered=unordered)
            == _cmp_form(baseline, unordered=unordered))


def _suppress_unchanged(proposal: dict) -> None:
    """Drop proposed fields identical to the canonical baseline (in place).

    facts/profile: per-key compare — a key absent from the baseline is a
    new value and always kept.  summary/category: scalar compare.  Fields
    the AI simply did not propose are never touched (silence ≠ deletion).
    """
    baseline = proposal.get("baseline") or {}
    bf = baseline.get("facts") or {}
    facts = proposal.get("facts") or {}
    for key in [k for k, e in facts.items()
                if _unchanged(e.get("value") if isinstance(e, dict) else e,
                              bf.get(k),
                              unordered=False, missing_equiv=False)]:
        facts.pop(key, None)
    bp = baseline.get("profile") or {}
    prof = proposal.get("profile") or {}
    for key in [k for k, v in prof.items()
                if _unchanged(v, bp.get(k),
                              unordered=k in _UNORDERED_PROFILE_FIELDS,
                              missing_equiv=True)]:
        prof.pop(key, None)
    for kind in ("summary", "category"):
        if kind in proposal and _unchanged(
                proposal[kind], baseline.get(kind),
                unordered=False, missing_equiv=True):
            proposal.pop(kind, None)


def _has_proposed_changes(proposal: dict) -> bool:
    """True when any reviewable diff remains after suppression.  Key
    presence — not truthiness — decides: suppression pops unchanged keys,
    so a surviving 'summary': '' is a real diff (canonical non-empty)
    that the user must be able to reject, not a silent no-op."""
    return bool(
        (proposal.get("facts") or {})
        or (proposal.get("profile") or {})
        or "summary" in proposal
        or "category" in proposal)


def _baseline(product_id: str, record: dict) -> dict:
    """Canonical + evidence snapshot the proposal is generated against.

    Resolve compares each field's CURRENT canonical value to this snapshot;
    a mismatch means the user changed the field after generation — the stale
    suggestion must never overwrite it silently.  ``source_rev`` extends the
    same rule to the evidence itself: a file/raw_text/transcript/scope
    change invalidates every pending accept even when the canonical field
    in question did not move.
    """
    eff = product_db.get_effective_facts(product_id)
    profile = load_product_profile(product_id) or {}
    meta = record.get("metadata") or {}
    return {
        "facts": {k: str(v.get("value", "")) for k, v in eff.items()},
        "summary": _eff_text(profile, meta, "summary"),
        "category": _eff_text(profile, meta, "category"),
        "profile": {k: profile.get(k) for k in PROFILE_FIELDS},
        "source_rev": _source_revision(product_id, record),
    }


def generate_proposal(
    product_id: str,
    scopes: list[str],
    llm,
    *,
    replace: bool = False,
) -> dict:
    """Run the requested AI scopes and store results as a pending proposal.

    The ONLY model-call entry point for product enrichment — reached solely
    through the explicit เริ่มใช้ AI confirmation (POST .../enrich).

    Raises ProposalPending when an unresolved proposal exists (unless
    replace=True), KeyError for unknown product, ValueError when there is no
    source text to analyze.  Any generation failure leaves the product fully
    usable: ai_enrichment.status="failed", canonical data untouched.
    """
    record = product_db.load(product_id)
    if record is None:
        raise KeyError(product_id)
    if record.get("ai_proposal") and not replace:
        raise ProposalPending(product_id)

    enrichment = dict(record.get("ai_enrichment") or {})
    enrichment.update({"status": "generating", "updated_at": _now(), "error": None})
    record["ai_enrichment"] = enrichment
    product_db.save(product_id, record)

    try:
        proposal: dict[str, Any] = {
            "proposal_id": uuid.uuid4().hex[:12],
            "generated_at": _now(),
            "scopes": [s for s in scopes if s in SCOPES],
        }
        if not proposal["scopes"]:
            raise ValueError("ไม่ได้เลือกขอบเขต AI")

        # Baseline BEFORE any model call — a canonical edit made while the
        # model is running must surface as a conflict at resolve time, not
        # fold silently into the snapshot.
        proposal["baseline"] = _baseline(product_id, record)

        if "facts" in proposal["scopes"]:
            from .ingestion import compute_metadata_facts
            comp = compute_metadata_facts(product_id, llm)
            proposal["summary"] = comp.get("summary", "")
            proposal["category"] = comp.get("category", "")
            # Canonical landing key = the derived machine key when it
            # overrides an existing derived fact, otherwise the Thai label —
            # the same identity the effective-facts merge uses.
            derived = record.get("derived_facts") or {}
            facts_prop: dict[str, dict[str, str]] = {}
            for mk, v in (comp.get("derived_facts") or {}).items():
                label = v.get("label") or mk
                ckey = mk if mk in derived else label
                facts_prop[ckey] = {
                    "label": label,
                    "value": str(v.get("value", "")),
                    "ai_key": mk,
                }
            proposal["facts"] = facts_prop

        if "marketing" in proposal["scopes"]:
            from .voice_learner import analyze_product_positioning
            spec_text = product_db.get_agent_context_text(product_id)
            if not spec_text.strip():
                raise ValueError("ไม่มีข้อมูลต้นฉบับให้ AI วิเคราะห์")
            suggested = analyze_product_positioning(spec_text, llm, strict=True) or {}
            proposal["profile"] = {
                k: v for k, v in suggested.items() if k in PROFILE_FIELDS
            }

        # Changed-only proposal — suggestions identical to canonical are
        # not reviewable changes; the user approves diffs, not echoes.
        _suppress_unchanged(proposal)

        record = product_db.load(product_id) or record
        if _has_proposed_changes(proposal):
            record["ai_proposal"] = proposal
            status = "proposal_ready"
        else:
            # Successful enrichment with nothing new — a consented rerun
            # also consumes the stale review it replaced.  Never an error,
            # never an empty approval checklist.
            record.pop("ai_proposal", None)
            status = "no_changes"
        enrichment = dict(record.get("ai_enrichment") or {})
        enrichment.update({
            "status": status,
            "updated_at": _now(),
            "error": None,
        })
        if status == "proposal_ready":
            enrichment["last_proposal_id"] = proposal["proposal_id"]
        record["ai_enrichment"] = enrichment
        product_db.save(product_id, record)
        return proposal
    except Exception as exc:
        # Failure never destroys state: a prior pending proposal (being
        # replaced) survives — it is still the user's unresolved review.
        record = product_db.load(product_id) or {}
        enrichment = dict(record.get("ai_enrichment") or {})
        enrichment.update({
            "status": "failed",
            "updated_at": _now(),
            "error": str(exc)[:300],
        })
        record["ai_enrichment"] = enrichment
        product_db.save(product_id, record)
        raise


def get_proposal_view(product_id: str) -> dict:
    """Diff view-model for the UI: pending proposal + workflow state +
    current canonical values (CURRENT side of the review)."""
    record = product_db.load(product_id) or {}
    profile = load_product_profile(product_id) or {}
    eff = product_db.get_effective_facts(product_id)
    meta = record.get("metadata") or {}
    proposal = record.get("ai_proposal")
    source_stale = bool(proposal) and _source_stale(
        product_id, record, proposal.get("baseline") or {})
    return {
        "proposal": proposal,
        "ai_enrichment": record.get("ai_enrichment") or {"status": "never_run"},
        "source_stale": source_stale,
        "current": {
            "facts": eff,
            "summary": _eff_text(profile, meta, "summary"),
            "category": _eff_text(profile, meta, "category"),
            "profile": {k: profile.get(k) for k in PROFILE_FIELDS},
        },
    }


def resolve_proposal(
    product_id: str,
    proposal_id: str,
    resolutions: list[dict],
) -> dict:
    """Apply per-field resolutions to canonical data — the only canonical
    write path for AI output.

    resolution = {"kind": "fact"|"summary"|"category"|"profile",
                  "key": str,          # fact canonical key / profile field
                  "action": "accept"|"keep"|"edit",
                  "value": Any}        # edit only — the user's chosen value

    Returns {"applied": [...], "conflicts": [...], "remaining": int}.
    Conflicted fields stay in the proposal for a fresh decision.
    """
    record = product_db.load(product_id)
    proposal = (record or {}).get("ai_proposal")
    if not proposal or proposal.get("proposal_id") != proposal_id:
        raise StaleProposal(proposal_id)

    baseline = proposal.get("baseline") or {}
    # Evidence gate: a missing fingerprint (legacy proposal) is stale,
    # never assumed current — and disk mutations still pending re-ingest
    # (upload/delete/replace landed before product.json was rebuilt)
    # count too.  When the source moved on, every accept becomes a
    # conflict — the AI output was generated from evidence that no
    # longer exists.  keep/edit stay available: keep writes nothing and
    # edit is the user's own explicit value.
    source_stale = _source_stale(product_id, record, baseline)
    eff = product_db.get_effective_facts(product_id)
    profile = load_product_profile(product_id) or {}
    meta = record.get("metadata") or {}
    facts_canonical = profile.get("facts")
    if not isinstance(facts_canonical, dict):
        facts_canonical = {}

    applied: list[dict] = []
    conflicts: list[dict] = []
    accepted_fields = dict((record.get("ai_enrichment") or {}).get("accepted") or {})
    profile_dirty = False

    def _current(kind: str, key: str):
        if kind == "fact":
            return str((eff.get(key) or {}).get("value", ""))
        if kind == "summary":
            return _eff_text(profile, meta, "summary")
        if kind == "category":
            return _eff_text(profile, meta, "category")
        if kind == "profile":
            return profile.get(key)
        return None

    def _baseline_val(kind: str, key: str):
        if kind == "fact":
            return (baseline.get("facts") or {}).get(key, "")
        if kind == "summary":
            return baseline.get("summary", "")
        if kind == "category":
            return baseline.get("category", "")
        if kind == "profile":
            return (baseline.get("profile") or {}).get(key)
        return None

    def _proposed(kind: str, key: str):
        if kind == "fact":
            return (proposal.get("facts") or {}).get(key, {}).get("value")
        if kind == "summary":
            return proposal.get("summary")
        if kind == "category":
            return proposal.get("category")
        if kind == "profile":
            return (proposal.get("profile") or {}).get(key)
        return None

    def _write(kind: str, key: str, value) -> None:
        nonlocal profile_dirty
        if kind == "fact":
            facts_canonical[key] = str(value)
            profile["facts"] = facts_canonical
        elif kind == "summary":
            # Canonical = the user-authority profile layer; the metadata
            # mirror is refreshed too so runtime reads stay consistent
            # before the next ingest rebuilds it from the same profile.
            profile["summary"] = str(value)
            meta["summary"] = str(value)
        elif kind == "category":
            profile["category"] = str(value)
            meta["category"] = str(value)
        elif kind == "profile":
            profile[key] = value
        profile_dirty = True

    def _consume(kind: str, key: str) -> None:
        if kind == "fact":
            (proposal.get("facts") or {}).pop(key, None)
        elif kind == "summary":
            proposal.pop("summary", None)
        elif kind == "category":
            proposal.pop("category", None)
        elif kind == "profile":
            (proposal.get("profile") or {}).pop(key, None)

    for res in resolutions or []:
        kind = res.get("kind")
        key = res.get("key") or ""
        action = res.get("action")
        if kind not in ("fact", "summary", "category", "profile"):
            continue
        if kind in ("summary", "category"):
            key = kind
        if action not in ("accept", "keep", "edit"):
            continue
        proposed = _proposed(kind, key)
        if proposed is None:
            continue  # field was never proposed — nothing to resolve

        if action == "accept":
            label = ((proposal.get("facts") or {}).get(key) or {}).get("label") or key
            if source_stale:
                # The evidence the AI analyzed has changed since
                # generation — never accept its output silently.
                conflicts.append({
                    "kind": kind, "key": key, "label": label,
                    "current": _current(kind, key), "suggestion": proposed,
                    "baseline": _baseline_val(kind, key),
                    "reason": "source_changed",
                })
                continue
            current = _current(kind, key)
            base = _baseline_val(kind, key)
            if current != base:
                # Stale proposal vs drifted canonical — never overwrite.
                conflicts.append({
                    "kind": kind, "key": key,
                    "label": label,
                    "current": current, "suggestion": proposed,
                    "baseline": base,
                })
                continue
            _write(kind, key, proposed)
            accepted_fields[f"{kind}:{key}"] = proposal["proposal_id"]
            applied.append({"kind": kind, "key": key, "action": "accept"})
        elif action == "edit":
            _write(kind, key, res.get("value"))
            accepted_fields[f"{kind}:{key}"] = proposal["proposal_id"]
            applied.append({"kind": kind, "key": key, "action": "edit"})
        else:  # keep
            applied.append({"kind": kind, "key": key, "action": "keep"})
        _consume(kind, key)

    # Persist canonical writes — profile only when something was accepted/
    # edited; the metadata mirror mirrors the same accepted values.
    record["metadata"] = meta
    if profile_dirty:
        _save_profile(product_id, profile)

    # Resolve bookkeeping — a fully consumed proposal clears pending state.
    remaining = (
        len(proposal.get("facts") or {})
        + len(proposal.get("profile") or {})
        + (1 if proposal.get("summary") else 0)
        + (1 if proposal.get("category") else 0)
    )
    took_ai = any(a["action"] in ("accept", "edit") for a in applied)
    enrichment = dict(record.get("ai_enrichment") or {})
    enrichment["accepted"] = accepted_fields
    if remaining == 0:
        record.pop("ai_proposal", None)
        enrichment.update({
            "status": "accepted" if took_ai else "discarded",
            "updated_at": _now(),
        })
    else:
        record["ai_proposal"] = proposal
        if took_ai:
            enrichment.update({"status": "partially_accepted",
                               "updated_at": _now()})
    record["ai_enrichment"] = enrichment
    product_db.save(product_id, record)

    return {
        "applied": applied,
        "conflicts": conflicts,
        "remaining": remaining,
        "ai_enrichment": enrichment,
    }


def discard_proposal(product_id: str, proposal_id: str) -> None:
    """Drop the pending proposal without touching canonical data.

    A mismatched proposal_id means the caller's view is stale — surface it
    instead of silently no-oping."""
    record = product_db.load(product_id)
    proposal = (record or {}).get("ai_proposal")
    if not proposal:
        return
    if proposal.get("proposal_id") != proposal_id:
        raise StaleProposal(proposal_id)
    record.pop("ai_proposal", None)
    enrichment = dict(record.get("ai_enrichment") or {})
    enrichment.update({"status": "discarded", "updated_at": _now()})
    record["ai_enrichment"] = enrichment
    product_db.save(product_id, record)
