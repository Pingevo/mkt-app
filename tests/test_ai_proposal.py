"""FREE-IMPORT-AI-PROPOSAL-01 — pending AI proposal storage, Git-like
per-field resolution, stale-proposal conflict safety.

Authority model under test:
  * generation writes ONLY product.json["ai_proposal"] + ["ai_enrichment"]
  * canonical (profile.facts, profile fields, metadata) is untouched until
    an explicit per-field resolution
  * accept is baseline-checked: canonical drift since generation →
    conflict, no silent overwrite
  * edit writes the user's exact value unconditionally
  * rerun requires replace — two generations never merge silently
"""
from __future__ import annotations

import json

import pytest

import src.ai_enrichment as ai_enrichment
import src.product_db as product_db
from src.brand_loader import load_product_profile


class _FakeLLM:
    """Deterministic model double — counts calls, serves per-source JSON.
    `on_chat` fires during the call (used to simulate canonical drift while
    the model is still running)."""

    def __init__(self, responses=None, fail=False, on_chat=None, last_truncated=False):
        self.responses = responses or {}
        self.fail = fail
        self.on_chat = on_chat
        self.last_truncated = last_truncated
        self.calls: list[str] = []
        self.kw_by_source: dict[str, dict] = {}

    def chat(self, messages, *, source="", **kw):
        self.calls.append(source)
        self.kw_by_source[source] = kw
        if self.on_chat:
            self.on_chat(source)
        if self.fail:
            raise RuntimeError("deterministic provider failure")
        return self.responses.get(source, "{}")

    def close(self):
        pass


_SUMMARY_JSON = json.dumps({
    "summary": "สรุปจาก AI",
    "category": "Smartwatch",
    "derived_facts": {
        "battery": {"label": "แบตเตอรี่", "value": "750mAh"},
        "screen": {"label": "หน้าจอ", "value": "1.4 inch"},
    },
})

_PROFILE_JSON = json.dumps({
    "price_tier": "mid",
    "differentiators": ["กันน้ำ IP68"],
    "tone_adjustment": "อบอุ่น",
})


def _mk_product(brand_ws, name="P1", raw="spec text Battery 770mAh"):
    """Create a product record with source text — deterministic ingest state.

    The on-disk spec.txt is registered in ``files[]`` with its real path +
    content hash + ``ingested`` status — the state a completed deterministic
    ingest leaves behind, so ``find_stale_files()`` sees a clean tree."""
    brand_root = brand_ws["brand_root"]
    pdir = brand_root / "data" / name
    pdir.mkdir(parents=True, exist_ok=True)
    spec = pdir / "spec.txt"
    spec.write_text(raw, encoding="utf-8")
    rec = product_db.load(name)
    rec["raw_text"] = raw
    rec["metadata"] = {"summary": "seed summary", "category": "", "file_count": 1}
    rec["derived_facts"] = {"battery": {"label": "แบตเตอรี่", "value": "770mAh"}}
    rec["files"] = [{"name": "spec.txt", "path": str(spec),
                     "hash": product_db.compute_file_hash(spec),
                     "status": "ingested"}]
    product_db.save(name, rec)
    return name


def _llm(facts=_SUMMARY_JSON, profile=_PROFILE_JSON, fail=False,
         on_chat=None):
    return _FakeLLM({
        "ingestion.metadata_summary": facts,
        "voice_learner.analyze_product_positioning": profile,
    }, fail=fail, on_chat=on_chat)


class TestGenerate:
    def test_proposal_stored_canonical_untouched(self, brand_ws, tmp_path):
        name = _mk_product(brand_ws)
        llm = _llm()
        proposal = ai_enrichment.generate_proposal(name, ["facts", "marketing"], llm)

        assert len(llm.calls) == 2  # one metadata_summary + one positioning call
        assert proposal["proposal_id"]
        assert proposal["baseline"]["facts"]["battery"] == "770mAh"

        rec = product_db.load(name)
        # Pending proposal stored on product.json
        assert rec["ai_proposal"]["proposal_id"] == proposal["proposal_id"]
        assert rec["ai_enrichment"]["status"] == "proposal_ready"
        # Canonical UNCHANGED — derived facts, metadata, profile all as before
        assert rec["derived_facts"]["battery"]["value"] == "770mAh"
        assert rec["metadata"]["summary"] == "seed summary"
        profile = load_product_profile(name)
        assert "facts" not in profile or "battery" not in (profile.get("facts") or {})

    def test_pending_blocks_rerun_until_replace(self, brand_ws):
        name = _mk_product(brand_ws)
        p1 = ai_enrichment.generate_proposal(name, ["facts"], _llm())
        with pytest.raises(ai_enrichment.ProposalPending):
            ai_enrichment.generate_proposal(name, ["facts"], _llm())
        # Explicit replace → NEW proposal_id against CURRENT canonical
        p2 = ai_enrichment.generate_proposal(name, ["facts"], _llm(), replace=True)
        assert p2["proposal_id"] != p1["proposal_id"]

    def test_failed_generation_keeps_product_usable(self, brand_ws):
        name = _mk_product(brand_ws)
        with pytest.raises(RuntimeError):
            ai_enrichment.generate_proposal(name, ["facts"], _llm(fail=True))
        rec = product_db.load(name)
        assert rec["ai_enrichment"]["status"] == "failed"
        assert "ai_proposal" not in rec
        # Canonical untouched
        assert rec["derived_facts"]["battery"]["value"] == "770mAh"
        assert product_db.get_status(name) != "processing"

    def test_truncated_generation_fails_with_controlled_message(self, brand_ws):
        """When the LLM response is truncated (finish_reason=length, last_truncated=True),
        generate_proposal must fail with a controlled recovery message rather than
        letting json.loads() leak low-level 'Unterminated string...' details.
        Canonical data remains untouched and status is 'failed'."""
        name = _mk_product(brand_ws)
        # partial JSON string truncated mid-stream
        truncated_json = '{"summary": "สรุปสิ'
        llm = _FakeLLM(
            responses={"ingestion.metadata_summary": truncated_json},
            last_truncated=True,
        )
        with pytest.raises(Exception) as exc_info:
            ai_enrichment.generate_proposal(name, ["facts"], llm)

        err_msg = str(exc_info.value)
        assert "Unterminated string" not in err_msg, f"low-level json parser error leaked: {err_msg}"
        assert ("ไม่ครบ" in err_msg or "truncated" in err_msg.lower() or "ความยาว" in err_msg), \
            f"expected controlled truncation message, got: {err_msg}"

        rec = product_db.load(name)
        assert rec["ai_enrichment"]["status"] == "failed"
        assert "ai_proposal" not in rec
        assert "Unterminated string" not in (rec["ai_enrichment"].get("error") or "")
        # Canonical facts untouched
        assert rec["derived_facts"]["battery"]["value"] == "770mAh"
        # Configured ceiling reaches the single summary call — no retry
        assert llm.calls.count("ingestion.metadata_summary") == 1
        assert llm.kw_by_source["ingestion.metadata_summary"]["max_tokens"] == 2048
        # Reasoning budget is bounded so thinking cannot starve the JSON output
        assert llm.kw_by_source["ingestion.metadata_summary"]["reasoning"] == {"max_tokens": 512}

    def test_no_source_text_rejected_before_model(self, brand_ws):
        brand_root = brand_ws["brand_root"]
        (brand_root / "data" / "EMPTY").mkdir(parents=True, exist_ok=True)
        rec = product_db.load("EMPTY")
        rec["raw_text"] = ""
        product_db.save("EMPTY", rec)
        llm = _llm()
        with pytest.raises(ValueError):
            ai_enrichment.generate_proposal("EMPTY", ["facts"], llm)
        assert llm.calls == []  # rejected before any model call

    def test_failed_replace_preserves_pending_proposal(self, brand_ws):
        """A rerun the user consented to that FAILS must not destroy the
        pending proposal it was going to replace."""
        name = _mk_product(brand_ws)
        p1 = ai_enrichment.generate_proposal(name, ["facts"], _llm())
        with pytest.raises(RuntimeError):
            ai_enrichment.generate_proposal(
                name, ["facts"], _llm(fail=True), replace=True)
        rec = product_db.load(name)
        assert rec["ai_enrichment"]["status"] == "failed"
        # the original pending proposal survives the failed replacement
        assert (rec.get("ai_proposal") or {}).get("proposal_id") == \
            p1["proposal_id"]

    def test_canonical_drift_during_generation_conflicts(self, brand_ws):
        """Baseline is snapshotted BEFORE the model runs — a canonical edit
        landing mid-generation must still conflict at accept time."""
        name = _mk_product(brand_ws)

        def drift(source):
            profile = load_product_profile(name) or {}
            profile.setdefault("facts", {})["battery"] = "800mAh"
            ai_enrichment._save_profile(name, profile)

        p = ai_enrichment.generate_proposal(
            name, ["facts"], _llm(on_chat=drift))
        assert p["baseline"]["facts"]["battery"] == "770mAh"  # pre-call
        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
        ])
        assert len(out["conflicts"]) == 1
        assert product_db.get_effective_facts(name)["battery"]["value"] == \
            "800mAh"


class TestResolve:
    def _gen(self, brand_ws, name="P1"):
        _mk_product(brand_ws, name)
        return ai_enrichment.generate_proposal(name, ["facts", "marketing"], _llm())

    def test_accept_fact_lands_in_manual_layer(self, brand_ws):
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
        ])
        assert out["conflicts"] == []
        # Canonical manual layer — overrides derived, wins the merge
        profile = load_product_profile(name)
        assert profile["facts"]["battery"] == "750mAh"
        eff = product_db.get_effective_facts(name)
        assert eff["battery"]["value"] == "750mAh"
        assert eff["battery"]["source"] == "manual"
        # Provenance: accepted value traced to this proposal
        rec = product_db.load(name)
        assert rec["ai_enrichment"]["accepted"]["fact:battery"] == p["proposal_id"]

    def test_new_fact_accepted_by_label(self, brand_ws):
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "หน้าจอ", "action": "accept"},
        ])
        profile = load_product_profile(name)
        assert profile["facts"]["หน้าจอ"] == "1.4 inch"
        eff = product_db.get_effective_facts(name)
        assert eff["หน้าจอ"]["value"] == "1.4 inch"

    def test_keep_writes_nothing(self, brand_ws):
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "keep"},
        ])
        profile = load_product_profile(name)
        assert "battery" not in (profile.get("facts") or {})
        eff = product_db.get_effective_facts(name)
        assert eff["battery"]["value"] == "770mAh"  # derived unchanged

    def test_stale_proposal_conflict_blocks_silent_overwrite(self, brand_ws):
        """Generate → user edits Battery to 800mAh → accept AI 750mAh must
        be refused as conflict; canonical keeps 800mAh."""
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        # User edits canonical AFTER generation
        profile = load_product_profile(name) or {}
        profile.setdefault("facts", {})["battery"] = "800mAh"
        ai_enrichment._save_profile(name, profile)

        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
        ])
        assert len(out["conflicts"]) == 1
        c = out["conflicts"][0]
        assert c["current"] == "800mAh" and c["suggestion"] == "750mAh"
        # Canonical NOT overwritten
        eff = product_db.get_effective_facts(name)
        assert eff["battery"]["value"] == "800mAh"
        # Field stays pending for a fresh decision
        rec = product_db.load(name)
        assert "battery" in (rec["ai_proposal"]["facts"] or {})

    def test_edit_writes_exact_user_value(self, brand_ws):
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "edit", "value": "800mAh"},
        ])
        profile = load_product_profile(name)
        assert profile["facts"]["battery"] == "800mAh"

    def test_summary_accept_updates_metadata(self, brand_ws):
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "summary", "action": "accept"},
        ])
        rec = product_db.load(name)
        assert rec["metadata"]["summary"] == "สรุปจาก AI"

    def test_summary_category_accept_is_durable(self, brand_ws):
        """Accepted summary/category land in the canonical profile layer —
        a later deterministic ingest must NOT silently revert them."""
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "summary", "action": "accept"},
            {"kind": "category", "action": "accept"},
        ])
        profile = load_product_profile(name)
        assert profile["summary"] == "สรุปจาก AI"
        assert profile["category"] == "Smartwatch"
        # deterministic rebuild honors the user-accepted values
        from src.ingestion import _generate_metadata_summary
        _generate_metadata_summary(name)
        rec = product_db.load(name)
        assert rec["metadata"]["summary"] == "สรุปจาก AI"
        assert rec["metadata"]["category"] == "Smartwatch"

    def test_unproposed_keys_write_nothing(self, brand_ws):
        """The only canonical write path must refuse fields that were never
        proposed — no 'None' literal, no arbitrary canonical keys."""
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "bogus", "action": "accept"},
            {"kind": "profile", "key": "facts", "action": "accept"},
            {"kind": "profile", "key": "evil", "action": "edit",
             "value": "x"},
            {"kind": "fact", "key": "bogus2", "action": "keep"},
        ])
        assert out["applied"] == []
        profile = load_product_profile(name)
        assert "bogus" not in (profile.get("facts") or {})
        assert "evil" not in profile
        rec = product_db.load(name)
        assert "bogus" not in str(rec.get("metadata"))

    def test_profile_accept_updates_only_that_field(self, brand_ws):
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "profile", "key": "price_tier", "action": "accept"},
        ])
        profile = load_product_profile(name)
        assert profile["price_tier"] == "mid"
        # Other proposed profile fields NOT written
        assert "differentiators" not in profile
        assert "tone_adjustment" not in profile

    def test_wrong_proposal_id_rejected(self, brand_ws):
        name = _mk_product(brand_ws)
        self._gen(brand_ws)
        with pytest.raises(ai_enrichment.StaleProposal):
            ai_enrichment.resolve_proposal(name, "deadbeef", [
                {"kind": "fact", "key": "battery", "action": "accept"},
            ])

    def test_full_resolution_clears_pending(self, brand_ws):
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        resolutions = (
            [{"kind": "fact", "key": k, "action": "keep"} for k in p["facts"]]
            + [{"kind": "summary", "action": "keep"}, {"kind": "category", "action": "keep"}]
            + [{"kind": "profile", "key": k, "action": "keep"} for k in p["profile"]]
        )
        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], resolutions)
        assert out["remaining"] == 0
        rec = product_db.load(name)
        assert "ai_proposal" not in rec
        assert rec["ai_enrichment"]["status"] == "discarded"  # all kept → nothing taken

    def test_partial_resolution_marks_partially_accepted(self, brand_ws):
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
        ])
        assert out["remaining"] > 0
        rec = product_db.load(name)
        assert rec["ai_enrichment"]["status"] == "partially_accepted"
        assert "ai_proposal" in rec

    def test_discard_clears_proposal_only(self, brand_ws):
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        ai_enrichment.discard_proposal(name, p["proposal_id"])
        rec = product_db.load(name)
        assert "ai_proposal" not in rec
        assert rec["ai_enrichment"]["status"] == "discarded"
        assert rec["derived_facts"]["battery"]["value"] == "770mAh"

    def test_discard_wrong_id_raises(self, brand_ws):
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        with pytest.raises(ai_enrichment.StaleProposal):
            ai_enrichment.discard_proposal(name, "deadbeef")
        # the real proposal survives the stale discard attempt
        rec = product_db.load(name)
        assert rec["ai_proposal"]["proposal_id"] == p["proposal_id"]

    def test_keep_only_writes_no_profile(self, brand_ws):
        """All-keep resolution must not touch the canonical profile file."""
        name = _mk_product(brand_ws)
        p = self._gen(brand_ws)
        profile_before = load_product_profile(name)
        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "keep"},
        ])
        assert out["applied"][0]["action"] == "keep"
        assert load_product_profile(name) == profile_before


class TestSourceRevision:
    """SOURCE-STALE-01 — a proposal generated against obsolete source
    evidence must never be silently accepted.

    The canonical-drift baseline only sees canonical fields; source files,
    raw_text, transcripts and scope can change underneath a proposal
    without touching the specific canonical value being accepted.  The
    proposal baseline carries a deterministic source_rev fingerprint;
    accept is gated on it still matching."""

    def _canonical_snapshot(self, name):
        """Byte-level canonical state: profile file + canonical record keys.
        Capture AFTER any source mutation — the assertion is "resolve wrote
        nothing", so the baseline is the post-mutation state."""
        rec = product_db.load(name)
        profile = load_product_profile(name) or {}
        return (
            json.dumps(profile, sort_keys=True, ensure_ascii=False),
            json.dumps({
                "metadata": rec.get("metadata"),
                "derived_facts": rec.get("derived_facts"),
                "raw_text": rec.get("raw_text"),
            }, sort_keys=True, ensure_ascii=False),
        )

    def test_source_change_blocks_accept(self, brand_ws):
        """Generate from evidence A → deterministic re-ingest changes
        raw_text to B → accept must conflict, canonical untouched."""
        name = _mk_product(brand_ws)
        p = ai_enrichment.generate_proposal(name, ["facts"], _llm())

        rec = product_db.load(name)
        rec["raw_text"] = "spec text Battery 900mAh (rev B)"
        product_db.save(name, rec)
        before = self._canonical_snapshot(name)

        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
        ])
        assert len(out["conflicts"]) == 1
        assert out["conflicts"][0].get("reason") == "source_changed"
        assert out["applied"] == []
        assert self._canonical_snapshot(name) == before
        # Proposal stays pending — nothing silently resolved or discarded.
        assert "battery" in (product_db.load(name)["ai_proposal"]["facts"])

    def test_source_file_added_blocks_accept(self, brand_ws):
        name = _mk_product(brand_ws)
        p = ai_enrichment.generate_proposal(name, ["facts"], _llm())

        rec = product_db.load(name)
        rec["files"] = list(rec.get("files") or []) + [
            {"name": "extra.txt", "hash": "0" * 64, "status": "ingested"}]
        product_db.save(name, rec)
        before = self._canonical_snapshot(name)

        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
        ])
        assert len(out["conflicts"]) == 1
        assert out["conflicts"][0].get("reason") == "source_changed"
        assert self._canonical_snapshot(name) == before

    def test_source_file_deleted_or_changed_blocks_accept(self, brand_ws):
        """Both deletion and content-hash change are evidence changes."""
        for i, mutate in enumerate((
            lambda f: f[1:],                       # deleted
            lambda f: [{**f[0], "hash": "f" * 64}]  # content replaced
            if f else f,
        )):
            name = _mk_product(brand_ws, name=f"P{i + 1}")
            p = ai_enrichment.generate_proposal(name, ["facts"], _llm())

            rec = product_db.load(name)
            rec["files"] = mutate(list(rec["files"]))
            product_db.save(name, rec)
            before = self._canonical_snapshot(name)

            out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
                {"kind": "fact", "key": "battery", "action": "accept"},
            ])
            assert len(out["conflicts"]) == 1, mutate
            assert out["conflicts"][0].get("reason") == "source_changed"
            assert self._canonical_snapshot(name) == before

    def test_unchanged_source_still_allows_accept(self, brand_ws):
        name = _mk_product(brand_ws)
        p = ai_enrichment.generate_proposal(name, ["facts"], _llm())
        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
        ])
        assert out["conflicts"] == []
        assert load_product_profile(name)["facts"]["battery"] == "750mAh"

    def test_stale_source_blocks_all_accepts_no_partial_apply(self, brand_ws):
        """A stale proposal cannot partially apply: every accept becomes a
        conflict, canonical stays byte-identical, proposal stays pending."""
        name = _mk_product(brand_ws)
        p = ai_enrichment.generate_proposal(name, ["facts", "marketing"], _llm())

        rec = product_db.load(name)
        rec["raw_text"] = "rev B source text"
        product_db.save(name, rec)
        before = self._canonical_snapshot(name)

        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
            {"kind": "summary", "action": "accept"},
            {"kind": "category", "action": "accept"},
            {"kind": "profile", "key": "price_tier", "action": "accept"},
        ])
        assert out["applied"] == []
        assert len(out["conflicts"]) == 4
        assert all(c.get("reason") == "source_changed" for c in out["conflicts"])
        assert self._canonical_snapshot(name) == before
        rec = product_db.load(name)
        assert "ai_proposal" in rec  # pending proposal preserved

    def test_edit_and_keep_still_work_on_stale_source(self, brand_ws):
        """User authority is preserved: an explicit edit value applies even
        when the source moved on — the user chose it knowing the state."""
        name = _mk_product(brand_ws)
        p = ai_enrichment.generate_proposal(name, ["facts"], _llm())

        rec = product_db.load(name)
        rec["raw_text"] = "rev B source text"
        product_db.save(name, rec)

        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "edit",
             "value": "800mAh"},
            {"kind": "fact", "key": "หน้าจอ", "action": "keep"},
            {"kind": "summary", "action": "accept"},
        ])
        kinds = {c["key"] for c in out["conflicts"]}
        assert kinds == {"summary"}  # only the AI-derived accept conflicts
        assert load_product_profile(name)["facts"]["battery"] == "800mAh"

    def test_canonical_drift_still_conflicts_when_source_unchanged(self, brand_ws):
        """Existing field-level drift behavior preserved on fresh evidence."""
        name = _mk_product(brand_ws)
        p = ai_enrichment.generate_proposal(name, ["facts"], _llm())
        profile = load_product_profile(name) or {}
        profile.setdefault("facts", {})["battery"] = "800mAh"
        ai_enrichment._save_profile(name, profile)

        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
        ])
        assert len(out["conflicts"]) == 1
        c = out["conflicts"][0]
        assert c["current"] == "800mAh" and c["suggestion"] == "750mAh"
        assert product_db.get_effective_facts(name)["battery"]["value"] == "800mAh"

    def test_legacy_proposal_without_source_rev_fails_safe(self, brand_ws):
        """A proposal stored before the fingerprint existed must be treated
        as stale — never assumed current."""
        name = _mk_product(brand_ws)
        p = ai_enrichment.generate_proposal(name, ["facts"], _llm())

        rec = product_db.load(name)
        rec["ai_proposal"]["baseline"].pop("source_rev", None)
        product_db.save(name, rec)
        before = self._canonical_snapshot(name)

        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
        ])
        assert len(out["conflicts"]) == 1
        assert out["conflicts"][0].get("reason") == "source_changed"
        assert self._canonical_snapshot(name) == before

    def test_view_marks_source_stale(self, brand_ws):
        """The review surface must expose staleness so the user sees the
        source moved before deciding."""
        name = _mk_product(brand_ws)
        ai_enrichment.generate_proposal(name, ["facts"], _llm())
        assert ai_enrichment.get_proposal_view(name)["source_stale"] is False

        rec = product_db.load(name)
        rec["raw_text"] = "rev B source text"
        product_db.save(name, rec)
        assert ai_enrichment.get_proposal_view(name)["source_stale"] is True

    def test_resolve_keep_discard_view_make_no_model_calls(self, brand_ws):
        """keep/discard/view are free paths — they take no llm and must not
        need one even on a stale proposal."""
        name = _mk_product(brand_ws)
        p = ai_enrichment.generate_proposal(name, ["facts"], _llm())
        rec = product_db.load(name)
        rec["raw_text"] = "rev B"
        product_db.save(name, rec)

        # None of these callables accept an llm — free by signature.
        view = ai_enrichment.get_proposal_view(name)
        assert view["proposal"]["proposal_id"] == p["proposal_id"]
        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "keep"},
        ])
        assert out["applied"][0]["action"] == "keep"
        ai_enrichment.discard_proposal(name, p["proposal_id"])
        assert "ai_proposal" not in product_db.load(name)

    # --- Disk-pending mutations (SOURCE-STALE-01 revision) ---
    # save_uploaded_files / direct disk writes change the evidence BEFORE
    # background re-ingest rebuilds product.json.  The record fingerprint
    # alone cannot see that window — the proposal must go stale from the
    # pending disk diff itself.

    def _assert_stale_blocks_accept(self, name, p, llm, before):
        """Shared post-mutation contract: view reports stale, accept
        conflicts source_changed, canonical unchanged, proposal pending,
        zero model calls on view/resolve."""
        view = ai_enrichment.get_proposal_view(name)
        assert view["source_stale"] is True
        out = ai_enrichment.resolve_proposal(name, p["proposal_id"], [
            {"kind": "fact", "key": "battery", "action": "accept"},
        ])
        assert out["applied"] == []
        assert len(out["conflicts"]) == 1
        assert out["conflicts"][0].get("reason") == "source_changed"
        assert self._canonical_snapshot(name) == before
        assert "battery" in (product_db.load(name)
                             .get("ai_proposal", {}).get("facts", {}))
        # view + resolve are free paths — the generation call is the only
        # model invocation ever recorded.
        assert len(llm.calls) == 1

    def test_disk_file_added_before_reingest_blocks_accept(self, brand_ws):
        """The real source-add path: save_uploaded_files() lands bytes on
        disk first — product.json still holds the old manifest."""
        name = _mk_product(brand_ws)
        llm = _llm()
        p = ai_enrichment.generate_proposal(name, ["facts"], llm)

        saved = product_db.save_uploaded_files(
            name, [("extra_spec.txt", b"Battery 900mAh rev B")])
        assert saved == ["extra_spec.txt"]
        before = self._canonical_snapshot(name)
        # product.json is untouched — record fields still show evidence A.
        assert "extra_spec.txt" not in [
            f.get("name") for f in product_db.load(name).get("files", [])]

        self._assert_stale_blocks_accept(name, p, llm, before)

    def test_disk_file_deleted_before_reingest_blocks_accept(self, brand_ws):
        name = _mk_product(brand_ws)
        llm = _llm()
        p = ai_enrichment.generate_proposal(name, ["facts"], llm)

        (brand_ws["brand_root"] / "data" / name / "spec.txt").unlink()
        before = self._canonical_snapshot(name)

        self._assert_stale_blocks_accept(name, p, llm, before)

    def test_disk_file_replaced_before_reingest_blocks_accept(self, brand_ws):
        name = _mk_product(brand_ws)
        llm = _llm()
        p = ai_enrichment.generate_proposal(name, ["facts"], llm)

        spec = brand_ws["brand_root"] / "data" / name / "spec.txt"
        spec.write_text("spec text Battery 900mAh (rev B)", encoding="utf-8")
        before = self._canonical_snapshot(name)

        self._assert_stale_blocks_accept(name, p, llm, before)


class TestViewModel:
    def test_proposal_view_has_current_for_diff(self, brand_ws):
        name = _mk_product(brand_ws)
        p = ai_enrichment.generate_proposal(name, ["facts"], _llm())
        view = ai_enrichment.get_proposal_view(name)
        assert view["proposal"]["proposal_id"] == p["proposal_id"]
        assert view["current"]["facts"]["battery"]["value"] == "770mAh"
        assert view["ai_enrichment"]["status"] == "proposal_ready"

    def test_missing_keys_mean_never_run(self, brand_ws):
        _mk_product(brand_ws)
        view = ai_enrichment.get_proposal_view("P1")
        assert view["proposal"] is None
        assert view["ai_enrichment"]["status"] == "never_run"
