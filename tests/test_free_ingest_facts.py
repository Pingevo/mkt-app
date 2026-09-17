"""FREE-INGEST-FACTS-AND-URL-IDENTITY-01 — GAP A regression tests.

FREE INGEST contract: explicit ``label | value`` rows already present in a
source (xlsx/csv/docx tables are normalized to ``a | b`` lines by the
loaders) must become usable derived_facts deterministically — no model,
no provider call, no guessing.  Rows that are not confident two-cell
key/value pairs stay raw evidence only.

Generic fixtures only — no brand/product-specific production rules.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.ingestion as ingestion
import src.product_db as product_db
import src.openrouter_gateway as gateway
import src.staging as staging


pytestmark = pytest.mark.usefixtures("brand_ws")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_xlsx(path: Path, rows: list[list]) -> None:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)


def _mk_product_dir(brand_root: Path, name: str) -> Path:
    d = brand_root / "data" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def no_model_calls(monkeypatch):
    """Tripwire — any provider call or LLM-client construction fails the test."""
    calls = {"provider": 0, "make_llm": 0}
    for fn in ("chat_post", "chat_stream_open", "image_post",
               "video_generate", "embeddings_post"):
        def _blocked(*a, _fn=fn, **kw):
            calls["provider"] += 1
            raise RuntimeError(f"provider call in free ingest ({_fn})")
        monkeypatch.setattr(gateway, fn, _blocked)

    def _make_llm():
        calls["make_llm"] += 1
        raise AssertionError("free ingest must never construct an LLM client")
    monkeypatch.setattr(ingestion, "_make_llm", _make_llm)
    return calls


# ---------------------------------------------------------------------------
# Core contract — explicit key/value rows become derived_facts for free
# ---------------------------------------------------------------------------

class TestDeterministicFacts:
    def test_xlsx_key_value_table_produces_facts(self, brand_ws, no_model_calls):
        """Ordinary spec sheet (label | value rows) → structured facts, free."""
        d = _mk_product_dir(brand_ws["brand_root"], "DetProd")
        _write_xlsx(d / "spec.xlsx", [
            ["Battery", "680mAh"],
            ["RAM", "1GB"],
            ["ROM", "8GB"],
        ])
        ingestion.ingest_product("DetProd", force=True)

        rec = product_db.load("DetProd")
        facts = rec.get("derived_facts") or {}
        assert facts, "explicit key/value rows must become derived_facts"
        assert facts["battery"]["value"] == "680mAh"
        assert facts["ram"]["value"] == "1GB"
        assert facts["rom"]["value"] == "8GB"
        assert rec["status"] == "ready"
        assert no_model_calls["provider"] == 0 and no_model_calls["make_llm"] == 0

    def test_multiple_spec_rows_all_extracted(self, brand_ws, no_model_calls):
        d = _mk_product_dir(brand_ws["brand_root"], "MultiRow")
        _write_xlsx(d / "spec.xlsx", [
            ["Display", "1.78 AMOLED"],
            ["Waterproof", "IP68"],
            ["Front camera", "5MP"],
            ["GPS", "Yes"],
            ["OS", "Android 8.1"],
        ])
        ingestion.ingest_product("MultiRow", force=True)

        facts = product_db.load("MultiRow").get("derived_facts") or {}
        values = {f["value"] for f in facts.values()}
        assert {"1.78 AMOLED", "IP68", "5MP", "Yes", "Android 8.1"} <= values

    def test_raw_text_still_available(self, brand_ws, no_model_calls):
        """Facts are additive — raw source text remains the evidence."""
        d = _mk_product_dir(brand_ws["brand_root"], "RawKept")
        _write_xlsx(d / "spec.xlsx", [["Battery", "680mAh"]])
        ingestion.ingest_product("RawKept", force=True)

        rec = product_db.load("RawKept")
        assert "Battery | 680mAh" in rec["raw_text"]
        assert rec["text_extracts"], "per-file text_extracts must persist"

    def test_fact_carries_source_provenance(self, brand_ws, no_model_calls):
        """Each structured fact links back to the source file it came from."""
        d = _mk_product_dir(brand_ws["brand_root"], "ProvProd")
        _write_xlsx(d / "spec.xlsx", [["Battery", "680mAh"]])
        ingestion.ingest_product("ProvProd", force=True)

        fact = (product_db.load("ProvProd").get("derived_facts") or {})["battery"]
        assert fact.get("source_file") == "spec.xlsx"


class TestFailClosed:
    def test_multi_column_rows_are_not_facts(self, brand_ws, no_model_calls):
        """3+ cell rows are ambiguous (multi-column table) → evidence only."""
        d = _mk_product_dir(brand_ws["brand_root"], "WideTable")
        _write_xlsx(d / "spec.xlsx", [
            ["Spec", "Model A", "Model B"],
            ["Battery", "680mAh", "800mAh"],
        ])
        ingestion.ingest_product("WideTable", force=True)

        rec = product_db.load("WideTable")
        assert not rec.get("derived_facts"), \
            "multi-column rows must not be guessed into facts"
        assert "680mAh" in rec["raw_text"], "content stays as source evidence"

    def test_empty_and_ragged_cells_not_facts(self, brand_ws, no_model_calls):
        d = _mk_product_dir(brand_ws["brand_root"], "Ragged")
        _write_xlsx(d / "spec.xlsx", [
            ["Battery", "680mAh"],
            ["Notes", None, None],
            [None, None],
            ["LoneCell"],
        ])
        ingestion.ingest_product("Ragged", force=True)

        facts = product_db.load("Ragged").get("derived_facts") or {}
        assert set(facts) == {"battery"}, f"only the clean pair may extract: {facts}"

    def test_non_tabular_text_produces_no_facts(self, brand_ws, no_model_calls):
        """Plain prose has no key/value rows → derived_facts stays empty."""
        d = _mk_product_dir(brand_ws["brand_root"], "Prose")
        (d / "notes.txt").write_text(
            "A compact wearable with a bright screen and long battery life.",
            encoding="utf-8")
        ingestion.ingest_product("Prose", force=True)

        rec = product_db.load("Prose")
        assert not rec.get("derived_facts")
        assert rec["status"] == "ready"


class TestLifecycleContract:
    def test_reingest_is_deterministic_and_free(self, brand_ws, no_model_calls):
        d = _mk_product_dir(brand_ws["brand_root"], "ReProd")
        _write_xlsx(d / "spec.xlsx", [["Battery", "680mAh"]])
        ingestion.ingest_product("ReProd", force=True)
        first = dict(product_db.load("ReProd").get("derived_facts") or {})

        _write_xlsx(d / "spec.xlsx", [["Battery", "700mAh"], ["RAM", "2GB"]])
        ingestion.ingest_product("ReProd", force=True)
        facts = product_db.load("ReProd").get("derived_facts") or {}

        assert facts["battery"]["value"] == "700mAh", \
            "re-ingest must re-derive from the current source"
        assert facts["ram"]["value"] == "2GB"
        assert set(first) != set(facts) or facts["battery"]["value"] != first["battery"]["value"]
        assert no_model_calls["provider"] == 0 and no_model_calls["make_llm"] == 0

    def test_removed_rows_do_not_leave_stale_facts(self, brand_ws, no_model_calls):
        """derived_facts mirrors the CURRENT source — dropped rows drop."""
        d = _mk_product_dir(brand_ws["brand_root"], "StaleRow")
        _write_xlsx(d / "spec.xlsx", [["Battery", "680mAh"], ["RAM", "1GB"]])
        ingestion.ingest_product("StaleRow", force=True)

        _write_xlsx(d / "spec.xlsx", [["Battery", "680mAh"]])
        ingestion.ingest_product("StaleRow", force=True)

        facts = product_db.load("StaleRow").get("derived_facts") or {}
        assert "ram" not in facts, "facts from a removed row must not persist"

    def test_manual_facts_keep_authority(self, brand_ws, no_model_calls):
        """User-entered canonical facts still override derived values."""
        from src.brand_loader import load_product_profile
        from src.workspace_context import contain_path

        d = _mk_product_dir(brand_ws["brand_root"], "ManualWin")
        _write_xlsx(d / "spec.xlsx", [["Battery", "680mAh"]])
        ingestion.ingest_product("ManualWin", force=True)

        # User corrects the derived fact via the canonical manual layer.
        profile_dir = contain_path(
            "ManualWin", brand_ws["brand_root"] / "cache")
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "product_profile.json").write_text(json.dumps(
            {"facts": {"battery": "700mAh"}}, ensure_ascii=False),
            encoding="utf-8")

        eff = product_db.get_effective_facts("ManualWin")
        assert eff["battery"]["value"] == "700mAh"
        assert eff["battery"]["source"] == "manual"

        # Manual canonical layer is untouched by re-ingest.
        ingestion.ingest_product("ManualWin", force=True)
        eff = product_db.get_effective_facts("ManualWin")
        assert eff["battery"]["value"] == "700mAh"
        assert load_product_profile("ManualWin")["facts"]["battery"] == "700mAh"


class TestStagingPath:
    def test_staged_commit_produces_facts_free(self, brand_ws, no_model_calls):
        """The real user path — upload_stage → commit → facts present."""
        import io
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        for r in (["Battery", "680mAh"], ["RAM", "1GB"]):
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)

        batch_id = staging.create_batch([("spec.xlsx", buf.getvalue())])
        staging.run_segmentation(batch_id)  # no llm → deterministic single
        out = staging.commit_batch(batch_id, [
            {"segment_index": 0, "action": "create", "name": "StagedProd"}])
        assert out["created"] == ["StagedProd"]

        rec = product_db.load("StagedProd")
        facts = rec.get("derived_facts") or {}
        assert facts["battery"]["value"] == "680mAh"
        assert facts["ram"]["value"] == "1GB"
        assert rec["status"] == "ready"
        assert no_model_calls["provider"] == 0 and no_model_calls["make_llm"] == 0


class TestEffectiveFactsRendering:
    def test_facts_reach_effective_view_and_agent_context(
            self, brand_ws, no_model_calls):
        """Deterministic facts must surface through the existing Product
        Facts seams — effective_facts + agent context — so Product Detail
        no longer claims the product has no data."""
        d = _mk_product_dir(brand_ws["brand_root"], "UiProd")
        _write_xlsx(d / "spec.xlsx", [["Battery", "680mAh"]])
        ingestion.ingest_product("UiProd", force=True)

        eff = product_db.get_effective_facts("UiProd")
        assert eff["battery"]["value"] == "680mAh"
        assert eff["battery"]["source"] == "derived"

        ctx = product_db.get_agent_context_text("UiProd")
        assert "680mAh" in ctx


class TestDeterministicAuthority:
    """Authority ladder: manual > explicit deterministic source > AI.
    AI may fill gaps but must never override verbatim source truth."""

    def test_ai_must_not_override_source_fact(self, brand_ws, no_model_calls):
        d = _mk_product_dir(brand_ws["brand_root"], "AuthProd")
        _write_xlsx(d / "spec.xlsx", [["Battery", "680mAh"]])
        ingestion.ingest_product("AuthProd", force=True)

        class FakeLLM:
            def chat(self, *a, **k):
                return json.dumps({
                    "summary": "s", "category": "c",
                    "derived_facts": {
                        # conflicting value for a source-stated fact
                        "battery": {"label": "Battery", "value": "999mAh"},
                        # AI filling a gap the source does not state
                        "ai_only": {"label": "AI gap-fill", "value": "filler"},
                    },
                })
            def close(self):
                pass

        ingestion._generate_metadata_summary("AuthProd", FakeLLM())
        facts = product_db.load("AuthProd")["derived_facts"]
        assert facts["battery"]["value"] == "680mAh", \
            "explicit source fact must outrank a conflicting AI value"
        assert facts["ai_only"]["value"] == "filler", \
            "AI may fill facts the source does not state"

    def test_ai_failure_preserves_prior_facts(self, brand_ws, no_model_calls):
        """A failed enrichment must not erase unrelated prior facts —
        prior data is kept and current source truth is overlaid."""
        d = _mk_product_dir(brand_ws["brand_root"], "FailKeep")
        _write_xlsx(d / "spec.xlsx", [["Battery", "680mAh"]])
        ingestion.ingest_product("FailKeep", force=True)
        rec = product_db.load("FailKeep")
        rec["derived_facts"]["legacy_key"] = {
            "label": "Legacy", "value": "keep-me"}
        product_db.save("FailKeep", rec)

        class BoomLLM:
            def chat(self, *a, **k):
                raise RuntimeError("provider down")
            def close(self):
                pass

        with pytest.raises(RuntimeError):
            ingestion._generate_metadata_summary("FailKeep", BoomLLM())
        facts = product_db.load("FailKeep")["derived_facts"]
        assert facts["legacy_key"]["value"] == "keep-me"
        assert facts["battery"]["value"] == "680mAh"


class TestRealK2Acceptance:
    """PO reproduction — the real `Lagenio K2 -spec 20241024.xlsx` is a
    hierarchical spec sheet (section | feature | value, sparse col 0).
    Free ingest must produce structured facts for it — zero model calls.
    The fixture is real user data (gitignored): skip when absent rather
    than fabricate it."""

    K2_XLSX = (
        Path(__file__).resolve().parent.parent
        / "evaluation_artifacts/pre_real_uat_backup_20260907_165928"
        / "data/Lagenio K2/Lagenio K2 -spec 20241024.xlsx"
    )

    def test_real_k2_spec_sheet_extracts_explicit_facts_free(
            self, brand_ws, no_model_calls):
        if not self.K2_XLSX.exists():
            pytest.skip("real K2 fixture not present in this workspace")
        import shutil

        d = _mk_product_dir(brand_ws["brand_root"], "RealK2")
        shutil.copy2(self.K2_XLSX, d / self.K2_XLSX.name)

        ingestion.ingest_product("RealK2", force=True)

        rec = product_db.load("RealK2")
        assert rec["status"] == "ready"
        assert "AMOLED" in rec["raw_text"], "raw evidence must remain"
        facts = rec.get("derived_facts") or {}
        assert facts, "real spec sheet must produce derived_facts"

        def _has(label_frag: str, value_frag: str) -> bool:
            return any(
                label_frag in f.get("label", "").lower()
                and value_frag in f.get("value", "").lower()
                for f in facts.values()
            )

        # Representative set of the explicit facts the PO listed —
        # generic assertions on label/value text, no K2-specific rules.
        assert _has("battery", "680mah")
        assert _has("ram", "1gb")
        assert _has("rom", "8gb")
        assert _has("display", "amoled")
        assert _has("size", "1.78")
        assert _has("camera", "5mp")
        assert _has("waterproof", "ip68")
        assert _has("os", "android 8.1")
        assert _has("gps", "yes")
        assert _has("wifi", "yes")

        # effective_facts is exactly what Product Detail gates its empty
        # state on — non-empty means the misleading 'no product data'
        # state cannot be reached for this import.
        eff = product_db.get_effective_facts("RealK2")
        assert len(eff) == len(facts)
        assert "680mAh" in product_db.get_agent_context_text("RealK2")
        assert no_model_calls["provider"] == 0
        assert no_model_calls["make_llm"] == 0
