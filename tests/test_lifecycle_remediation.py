"""Regression tests for PRODUCT-LIFECYCLE-REMEDIATION-01.

Covers the defects proven by the PRODUCT-LIFECYCLE-E2E-01 qualification:
  - staging.commit_batch must fully validate ALL choices before ANY
    filesystem mutation, product creation, or enrichment side effect;
  - /api/upload must not create+enrich a brand-new product, bypassing
    staging/human review;
  - enrichment failure must not end as READY (truthful status +
    ingest_error);
  - an explicitly empty derived_facts result clears stale facts;
  - URL import product_name is carried into the staging preview instead
    of being validated-then-ignored.
"""
import json
from pathlib import Path

import pytest


@pytest.fixture
def _stage(monkeypatch, tmp_path):
    """Tmp brand root for staging/product_db — same seam as test_staging.py."""
    import src.product_db as product_db

    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    import src.staging as staging
    monkeypatch.setattr(staging, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(staging, "product_db", product_db)
    monkeypatch.setattr(staging, "_generate_product_profile", lambda *a, **kw: None)
    monkeypatch.setattr(staging, "_generate_metadata_summary", lambda *a, **kw: None)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return staging


class _RecordingLLM:
    """Deterministic LLM double — counts calls, never touches a provider."""

    def __init__(self, result=None, fail=False):
        self.calls = 0
        self.result = result
        self.fail = fail

    def chat(self, *a, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("deterministic llm failure")
        return self.result or "{}"

    def close(self):
        pass


def _mk_batch(staging, tmp_path, segments):
    """Create a batch and inject a deterministic segmentation result."""
    batch_id = staging.create_batch([("seed.txt", b"seed content")])
    batch = staging._load_batch(batch_id)
    batch["status"] = "segmented"
    batch["segments"] = segments
    staging._save_batch(batch_id, batch)
    return batch_id


_SEG = {"product_key": "K1", "suggested_name": "Seg Product",
        "category": "C", "summary": "s", "source_refs": [], "common_refs": []}


def _data_products(tmp_path):
    d = tmp_path / "data"
    return {p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")}


class TestCommitValidation:
    """commit_batch must reject malformed requests BEFORE any side effect."""

    def test_absolute_name_rejected_zero_effects(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        escape = tmp_path.parent / f"{tmp_path.name}_escape"
        with pytest.raises(Exception):
            st.commit_batch(bid, [{"segment_index": 0, "action": "create",
                                   "name": str(escape)}], llm=_RecordingLLM())
        assert not escape.exists(), "absolute name must not create a dir"
        assert not _data_products(tmp_path)

    def test_traversal_name_rejected_zero_effects(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        with pytest.raises(Exception):
            st.commit_batch(bid, [{"segment_index": 0, "action": "create",
                                   "name": "../escape_dir"}], llm=_RecordingLLM())
        assert not (tmp_path / "escape_dir").exists()
        assert not _data_products(tmp_path)

    def test_separator_name_rejected(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        with pytest.raises(Exception):
            st.commit_batch(bid, [{"segment_index": 0, "action": "create",
                                   "name": "a/b"}], llm=_RecordingLLM())
        assert not (tmp_path / "data" / "a").exists()
        assert not _data_products(tmp_path)

    def test_negative_index_rejected_zero_effects(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        llm = _RecordingLLM()
        with pytest.raises(Exception):
            st.commit_batch(bid, [{"segment_index": -1, "action": "create",
                                   "name": "Neg"}], llm=llm)
        assert not _data_products(tmp_path)
        assert llm.calls == 0

    def test_out_of_range_index_rejected_zero_effects(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        llm = _RecordingLLM()
        with pytest.raises(Exception):
            st.commit_batch(bid, [{"segment_index": 99, "action": "create",
                                   "name": "OOB"}], llm=llm)
        assert not _data_products(tmp_path)
        assert llm.calls == 0

    def test_non_integer_index_rejected(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        with pytest.raises(Exception):
            st.commit_batch(bid, [{"segment_index": "0", "action": "create",
                                   "name": "StrIdx"}], llm=_RecordingLLM())
        assert not _data_products(tmp_path)

    def test_duplicate_segment_rejected(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        with pytest.raises(Exception):
            st.commit_batch(bid, [
                {"segment_index": 0, "action": "create", "name": "Dup"},
                {"segment_index": 0, "action": "create", "name": "Dup"},
            ], llm=_RecordingLLM())
        assert not _data_products(tmp_path)

    def test_unknown_action_rejected(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        with pytest.raises(Exception):
            st.commit_batch(bid, [{"segment_index": 0, "action": "bogus",
                                   "name": "Bogus"}], llm=_RecordingLLM())
        assert not _data_products(tmp_path)

    def test_update_target_traversal_rejected(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        with pytest.raises(Exception):
            st.commit_batch(bid, [{"segment_index": 0, "action": "update",
                                   "target": ".."}], llm=_RecordingLLM())
        # staging copy must not leak outside data/
        assert not (tmp_path / "seed.txt").exists()
        assert not _data_products(tmp_path)

    def test_update_target_must_exist(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        with pytest.raises(Exception):
            st.commit_batch(bid, [{"segment_index": 0, "action": "update",
                                   "target": "NoSuchProduct"}], llm=_RecordingLLM())
        assert not _data_products(tmp_path)

    def test_valid_then_invalid_is_atomic(self, _stage, tmp_path):
        """A bad choice later in the list must not let earlier choices commit."""
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        llm = _RecordingLLM()
        with pytest.raises(Exception):
            st.commit_batch(bid, [
                {"segment_index": 0, "action": "create", "name": "First"},
                {"segment_index": 99, "action": "create", "name": "Boom"},
            ], llm=llm)
        assert not _data_products(tmp_path), "partial commit happened"
        assert llm.calls == 0

    def test_valid_request_still_commits(self, _stage, tmp_path):
        st = _stage
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        out = st.commit_batch(bid, [{"segment_index": 0, "action": "create",
                                     "name": "Good Product"}],
                              llm=_RecordingLLM())
        assert out["created"] == ["Good Product"]
        assert (tmp_path / "data" / "Good Product" / "seed.txt").exists()

    def test_collision_dedup_preserved(self, _stage, tmp_path):
        """Existing product name → deterministic 'name (N)' dedup."""
        st = _stage
        (tmp_path / "data" / "Existing").mkdir(parents=True)
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        out = st.commit_batch(bid, [{"segment_index": 0, "action": "create",
                                     "name": "Existing"}], llm=_RecordingLLM())
        assert out["created"] == ["Existing (1)"]

    def test_update_existing_product_works(self, _stage, tmp_path):
        st = _stage
        (tmp_path / "data" / "Old Product").mkdir(parents=True)
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        out = st.commit_batch(bid, [{"segment_index": 0, "action": "update",
                                     "target": "Old Product"}],
                              llm=_RecordingLLM())
        assert out["updated"] == ["Old Product"]
        assert (tmp_path / "data" / "Old Product" / "seed.txt").exists()


# ---------------------------------------------------------------------------
# Truthful enrichment state + stale derived_facts (ingestion seam)
# ---------------------------------------------------------------------------

@pytest.fixture
def _ing(monkeypatch, tmp_path):
    """Tmp brand root for ingestion + product_db."""
    import src.product_db as product_db
    import src.ingestion as ingestion

    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(ingestion, "_project_root", lambda: tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    return ingestion, product_db


class _SeqLLM:
    """Deterministic LLM — replies per source, records every call."""

    def __init__(self, responses=None, fail_sources=()):
        self.responses = responses or {}
        self.fail_sources = set(fail_sources)
        self.calls: list[str] = []

    def chat(self, messages, *, source="", **kw):
        self.calls.append(source)
        if source in self.fail_sources:
            raise RuntimeError("deterministic enrichment failure")
        return self.responses.get(source, "{}")

    def close(self):
        pass


_SUMMARY = json.dumps({
    "summary": "sum", "category": "Cat",
    "derived_facts": {"k1": {"label": "ฟิลด์", "value": "V1"}},
})


def _mk_product(tmp_path, name="P1"):
    d = tmp_path / "data" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.txt").write_text("Product spec content", encoding="utf-8")
    return d


class TestTruthfulEnrichment:
    """FREE-IMPORT contract: ready = deterministic ingest complete.  AI
    enrichment (explicit-llm path only — dormant split machinery / tests)
    can never demote a usable product; failures land on ai_error."""

    def test_enrichment_failure_keeps_product_ready(self, _ing, tmp_path):
        """LLM metadata-summary failure → still ready; ai_error recorded."""
        ingestion, product_db = _ing
        _mk_product(tmp_path)
        llm = _SeqLLM(fail_sources={"ingestion.metadata_summary"})
        ingestion.ingest_product("P1", force=True, llm=llm)
        rec = product_db.load("P1")
        assert rec["status"] == "ready", rec["status"]
        assert rec.get("ai_error"), "ai_error must be recorded"
        assert "enrichment" in rec["ai_error"]

    def test_profile_failure_keeps_product_ready(self, _ing, tmp_path):
        """Profile/positioning failure → still ready either."""
        ingestion, product_db = _ing
        _mk_product(tmp_path)
        llm = _SeqLLM(
            responses={"ingestion.metadata_summary": _SUMMARY},
            fail_sources={"voice_learner.analyze_product_positioning"},
        )
        ingestion.ingest_product("P1", force=True, llm=llm)
        rec = product_db.load("P1")
        assert rec["status"] == "ready", rec["status"]
        assert rec.get("ai_error")

    def test_no_llm_import_is_free_and_ready(self, _ing, tmp_path):
        """Normal import (llm=None) — zero model calls, deterministic
        parse → ready with no derived_facts and no ai_error."""
        ingestion, product_db = _ing
        _mk_product(tmp_path)
        ingestion.ingest_product("P1", force=True)
        rec = product_db.load("P1")
        assert rec["status"] == "ready"
        assert not rec.get("ai_error")
        assert rec["raw_text"], "deterministic parse must still populate raw_text"

    def test_successful_enrichment_ready(self, _ing, tmp_path):
        ingestion, product_db = _ing
        _mk_product(tmp_path)
        llm = _SeqLLM(responses={"ingestion.metadata_summary": _SUMMARY})
        ingestion.ingest_product("P1", force=True, llm=llm)
        rec = product_db.load("P1")
        assert rec["status"] == "ready"
        assert rec["derived_facts"]["k1"]["value"] == "V1"

    def test_empty_derived_facts_clears_stale(self, _ing, tmp_path):
        """Successful extraction returning derived_facts:{} must REPLACE
        (clear) the previous model-derived facts — not retain stale ones."""
        ingestion, product_db = _ing
        _mk_product(tmp_path)
        llm = _SeqLLM(responses={"ingestion.metadata_summary": _SUMMARY})
        ingestion.ingest_product("P1", force=True, llm=llm)
        assert product_db.load("P1")["derived_facts"]["k1"]["value"] == "V1"

        empty = json.dumps({"summary": "s2", "category": "Cat",
                            "derived_facts": {}})
        llm2 = _SeqLLM(responses={"ingestion.metadata_summary": empty})
        ingestion.ingest_product("P1", force=True, llm=llm2)
        rec = product_db.load("P1")
        assert rec.get("derived_facts", {}) == {}, \
            f"stale derived_facts retained: {rec.get('derived_facts')}"
        assert rec["status"] == "ready"


class TestCommitEnrichmentHonesty:
    """commit_batch: AI enrichment failure → product stays ready,
    ai_error recorded (never demoted by a paid-call failure)."""

    def test_commit_enrichment_failure_keeps_ready(self, _stage, tmp_path):
        st = _stage
        import src.product_db as product_db
        bid = _mk_batch(st, tmp_path, [dict(_SEG)])
        calls = {"n": 0}

        def _boom(*a, **kw):
            calls["n"] += 1
            raise RuntimeError("deterministic enrichment failure")

        import src.staging as staging_mod
        orig_sum = staging_mod._generate_metadata_summary
        staging_mod._generate_metadata_summary = _boom
        try:
            out = st.commit_batch(bid, [{"segment_index": 0,
                                         "action": "create",
                                         "name": "Honest"}],
                                  llm=_RecordingLLM())
        finally:
            staging_mod._generate_metadata_summary = orig_sum
        assert out["created"] == ["Honest"]
        rec = product_db.load("Honest")
        assert rec["status"] == "ready", rec["status"]
        assert rec.get("ai_error")


# ---------------------------------------------------------------------------
# /api/upload must not bypass staging/review for brand-new products
# ---------------------------------------------------------------------------

@pytest.fixture
def _brand_client(tmp_path, monkeypatch):
    from tests.conftest import make_brand_client
    import web_viewer
    client, user_id, brand_id, brand_root = make_brand_client(
        web_viewer.app, tmp_path, monkeypatch)
    return client, brand_root


class TestUploadRequiresReview:

    def test_upload_new_product_rejected(self, _brand_client):
        """POST /api/upload for a NEW folder → rejected; nothing created,
        nothing enriched (must go through staging/review instead)."""
        client, brand_root = _brand_client
        r = client.post("/api/upload", data={"product_name": "SneakyNew"},
                        files=[("files", ("s.txt", b"sneaky", "text/plain"))])
        assert r.status_code == 400, r.text
        assert not (brand_root / "data" / "SneakyNew").exists(), \
            "new product materialized without staging review"

    def test_upload_existing_product_still_works(self, _brand_client):
        """Product Detail add-file flow: upload into an EXISTING product
        stays allowed (established workflow), and triggers ingestion."""
        client, brand_root = _brand_client
        data_dir = brand_root / "data" / "ExistingProd"
        data_dir.mkdir(parents=True)
        (data_dir / "old.txt").write_text("old", encoding="utf-8")
        r = client.post("/api/upload", data={"product_name": "ExistingProd"},
                        files=[("files", ("new.txt", b"new file", "text/plain"))])
        assert r.status_code == 200, r.text
        assert (data_dir / "new.txt").exists()


# ---------------------------------------------------------------------------
# URL import: validated product_name must reach the staging preview
# ---------------------------------------------------------------------------

class TestUrlImportName:

    def test_product_name_becomes_staged_default(self, _brand_client,
                                                 monkeypatch):
        """POST /api/product_from_url with product_name → staged batch whose
        single segment's suggested_name is the supplied name (human still
        reviews before commit)."""
        client, brand_root = _brand_client
        import web_viewer

        def _fake_fetch(url):
            return {
                "original_url": url, "final_url": url,
                "canonical_url": url, "page_title": "Page Title",
                "og_title": "Page Title", "text": "product page text body",
                "images": [], "fetched_at": "2026-01-01T00:00:00",
                "fetched_via": "static",
                # deterministic single-product evidence (no model involved)
                "page_class": "single",
                "page_signals": {"product_count": 1, "multi_signals": []},
            }

        # the endpoint does `from src.url_import import fetch_product_page`
        # inside the function — patch the attribute it resolves at call time
        monkeypatch.setattr("src.url_import.fetch_product_page", _fake_fetch)

        r = client.post("/api/product_from_url",
                        json={"url": "https://example.com/p/1",
                              "product_name": "My Named Product"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("staged") is True
        assert data["segments"], "no segments staged"
        assert data["segments"][0]["suggested_name"] == "My Named Product", \
            data["segments"]
        # nothing materialized before human commit
        assert not (brand_root / "data" / "My Named Product").exists()


# ---------------------------------------------------------------------------
# Diagnostic: overlapping source_refs across segments (unproven in E2E-01)
# ---------------------------------------------------------------------------

def test_source_refs_overlap_diagnostic():
    """DIAGNOSTIC — characterize current scoped-evidence contract.

    Two segments whose source_refs claim the SAME source lines: scoped
    evidence rules say a line should belong to one product (common material
    belongs in common_refs).  This test records what the current code
    actually does.  If it starts rejecting overlap after a contract change,
    the failure here is the signal to review the new semantics — do NOT
    blindly update this test.
    """
    import src.product_segmentation as seg

    catalog = ("HEADER shared catalog\n"
               "Product: Alpha\n"
               "Spec: ALPHA-ONLY\n"
               "Product: Bravo\n"
               "Spec: BRAVO-ONLY\n")
    files = [{"name": "cat.txt", "path": "", "text": catalog, "type": "text"}]

    overlap = {"file": "cat.txt", "line_start": 2, "line_end": 3}
    plan = {"products": [
        {"product_key": "A", "suggested_name": "Alpha", "category": "C",
         "summary": "a", "source_refs": [dict(overlap)], "common_refs": []},
        {"product_key": "B", "suggested_name": "Bravo", "category": "C",
         "summary": "b",
         # Bravo ALSO owns Alpha's lines (overlap) plus its own
         "source_refs": [dict(overlap),
                         {"file": "cat.txt", "line_start": 4, "line_end": 5}],
         "common_refs": []},
    ], "mode": "multi"}

    class _LLM:
        def chat(self, *a, **kw):
            return json.dumps(plan)

        def close(self):
            pass

    result = seg.segment_products(files, _LLM())
    products = result.get("products") or []
    overlap_accepted = (
        len(products) == 2
        and any(r["line_start"] == 2 for r in products[0].get("source_refs", []))
        and any(r["line_start"] == 2 for r in products[1].get("source_refs", []))
    )
    # CURRENT OBSERVED BEHAVIOR: overlapping source_refs across different
    # product_keys are accepted — ALPHA-ONLY lines also land in Bravo's
    # scoped evidence (cross-product evidence bleed).  Reported to Codex as
    # a contract gap; NOT changed in this phase.
    assert overlap_accepted, (
        "source_refs overlap is now rejected — contract changed; "
        "review whether this is the intended new semantics")
