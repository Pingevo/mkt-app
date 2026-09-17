"""Product Information — derived facts + effective merge layer.

Proves:
  - legacy product with no ``derived_facts`` remains compatible
  - existing ingestion LLM call produces summary/category + derived_facts in ONE call
  - generic derived facts persist into ``product.json``
  - effective merge uses derived value when no correction exists
  - manual correction wins over derived
  - reset/removal restores derived value
  - raw evidence remains unchanged
  - re-ingestion refreshes derived data but preserves manual corrections
  - stable machine-key behavior for unchanged facts
  - agent-facing context contains effective structured facts before raw evidence
  - A1/A2 brand isolation

Uses FakeLLM/deterministic fixtures.  No paid/live model calls.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# FakeLLM — returns structured JSON for ingestion.metadata_summary
# ---------------------------------------------------------------------------

class _FakeLLM:
    """Deterministic LLM double for ingestion.metadata_summary.

    Returns JSON with summary, category, and derived_facts — matching the
    extended contract of _generate_metadata_summary.
    """

    def __init__(self, derived_facts: dict | None = None,
                 summary: str = "Test summary",
                 category: str = "Test Category"):
        self._derived = derived_facts or {}
        self._summary = summary
        self._category = category
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        # Return JSON matching the extended metadata_summary contract
        return json.dumps({
            "summary": self._summary,
            "category": self._category,
            "derived_facts": self._derived,
        }, ensure_ascii=False)

    def close(self):
        pass

    def abort(self):
        pass


# ---------------------------------------------------------------------------
# Workspace fixture — brand-scoped, same pattern as test_product_facts.py
# ---------------------------------------------------------------------------

@pytest.fixture
def _ws(tmp_path, monkeypatch):
    """Brand-scoped workspace under tmp_path."""
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    from src.brand_registry import BrandRegistry

    monkeypatch.setattr("src.local_workspace._archive_root",
                        lambda: tmp_path / ".recovery_archive")
    reg = BrandRegistry(user_id="test_user", project_root=tmp_path)
    brand = reg.create("TestBrand")
    ws = WorkspaceContext.for_brand("test_user", brand["brand_id"], tmp_path)
    token = set_workspace(ws)
    brand_root = tmp_path / "users" / "test_user" / "brands" / brand["brand_id"]
    for d in ("data", "cache", "output", "brand"):
        (brand_root / d).mkdir(parents=True, exist_ok=True)
    yield {
        "root": brand_root,
        "ws": ws,
    }
    set_workspace(None)
    reset_workspace(token)


def _save_profile(product_id: str, profile: dict) -> None:
    """Write product_profile.json into cache/{product_id}/."""
    from src.workspace_context import contain_path
    from src.product_db import _project_root
    profile_dir = contain_path(product_id, _project_root() / "cache")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _ingest_txt(_ws, product_id: str, text: str, filename: str = "spec.txt",
                llm=None) -> None:
    """Create a product with a .txt source file and ingest it.

    If llm is provided, passes it explicitly to ingest_product (the
    explicit-AI seam — normal import never constructs a client).
    If llm is None, stubs the API key empty so _make_llm() returns None
    (deterministic no-LLM path — for legacy compatibility tests).
    """
    import src.openrouter_gateway as gateway
    from src import product_db, ingestion

    data_dir = _ws["root"] / "data" / product_id
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / filename).write_text(text, encoding="utf-8")

    old_key = gateway.get_api_key
    old_env = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        if llm is not None:
            gateway.get_api_key = lambda: "fake-key"  # type: ignore
            ingestion.ingest_product(product_id, force=True, llm=llm)
        else:
            gateway.get_api_key = lambda: ""  # type: ignore
            ingestion.ingest_product(product_id, force=True)
    finally:
        gateway.get_api_key = old_key  # type: ignore
        if old_env is not None:
            os.environ["OPENROUTER_API_KEY"] = old_env


# ---------------------------------------------------------------------------
# 1. Legacy compatibility — no derived_facts
# ---------------------------------------------------------------------------

def test_legacy_product_no_derived_facts(_ws):
    """Product ingested without LLM → no derived_facts → still loadable."""
    from src import product_db

    _ingest_txt(_ws, "LegacyProd", "Legacy raw text with price 999")
    record = product_db.load("LegacyProd")
    # derived_facts absent or empty
    assert not record.get("derived_facts"), \
        "Legacy product must not have derived_facts when no LLM was used"

    # Agent context still works
    ctx = product_db.get_agent_context_text("LegacyProd")
    assert "Legacy raw text" in ctx


def test_legacy_product_with_manual_facts_only(_ws):
    """Legacy product (no derived_facts) + manual facts → effective = manual."""
    from src import product_db
    from src.product_db import get_effective_facts

    _ingest_txt(_ws, "LegacyManual", "Legacy raw text")
    _save_profile("LegacyManual", {"facts": {"price": "3,590 บาท"}})

    effective = get_effective_facts("LegacyManual")
    # Manual fact appears even without derived_facts
    assert "price" in effective
    assert effective["price"]["value"] == "3,590 บาท"
    assert effective["price"]["source"] == "manual"


# ---------------------------------------------------------------------------
# 2. Ingestion LLM produces derived_facts in ONE call
# ---------------------------------------------------------------------------

def test_ingestion_produces_derived_facts(_ws):
    """Ingestion with LLM → derived_facts stored in product.json."""
    from src import product_db

    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
        "battery_capacity": {"label": "แบตเตอรี่", "value": "800 mAh"},
    })
    _ingest_txt(_ws, "DerivedProd",
                "Product spec: price 3,990 baht, battery 800mAh",
                llm=fake_llm)

    record = product_db.load("DerivedProd")
    assert "derived_facts" in record, "derived_facts must be stored in product.json"
    df = record["derived_facts"]
    assert df["price"]["value"] == "3,990 บาท"
    assert df["price"]["label"] == "ราคา"
    assert df["battery_capacity"]["value"] == "800 mAh"


def test_ingestion_llm_call_count_unchanged(_ws):
    """The metadata_summary LLM call happens exactly once per ingestion."""
    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "999 บาท"},
    })
    _ingest_txt(_ws, "CallCountProd", "spec text", llm=fake_llm)

    # Count calls to ingestion.metadata_summary source
    summary_calls = [c for c in fake_llm.calls
                     if c["kwargs"].get("source") == "ingestion.metadata_summary"]
    assert len(summary_calls) == 1, \
        f"metadata_summary must be called exactly once, got {len(summary_calls)}"


# ---------------------------------------------------------------------------
# 3. Effective merge — derived value when no correction
# ---------------------------------------------------------------------------

def test_effective_uses_derived_when_no_correction(_ws):
    """No manual correction → effective value = derived value."""
    from src.product_db import get_effective_facts

    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "EffDerived", "spec", llm=fake_llm)

    effective = get_effective_facts("EffDerived")
    assert effective["price"]["value"] == "3,990 บาท"
    assert effective["price"]["source"] == "derived"
    assert effective["price"]["label"] == "ราคา"


# ---------------------------------------------------------------------------
# 4. Manual correction wins over derived
# ---------------------------------------------------------------------------

def test_manual_correction_wins_over_derived(_ws):
    """Manual fact with same machine key overrides derived value."""
    from src.product_db import get_effective_facts

    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "OverrideProd", "spec", llm=fake_llm)
    _save_profile("OverrideProd", {"facts": {"price": "3,590 บาท"}})

    effective = get_effective_facts("OverrideProd")
    assert effective["price"]["value"] == "3,590 บาท"
    assert effective["price"]["source"] == "manual"


# ---------------------------------------------------------------------------
# 5. Reset/removal restores derived value
# ---------------------------------------------------------------------------

def test_reset_restores_derived_value(_ws):
    """Remove manual correction → effective reverts to derived value."""
    from src.product_db import get_effective_facts
    from src.brand_loader import load_product_profile

    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "ResetProd", "spec", llm=fake_llm)
    _save_profile("ResetProd", {"facts": {"price": "3,590 บาท"}})

    # Before reset: manual wins
    effective = get_effective_facts("ResetProd")
    assert effective["price"]["value"] == "3,590 บาท"

    # Reset: remove the manual correction
    profile = load_product_profile("ResetProd")
    del profile["facts"]["price"]
    _save_profile("ResetProd", profile)

    # After reset: derived value restored
    effective = get_effective_facts("ResetProd")
    assert effective["price"]["value"] == "3,990 บาท"
    assert effective["price"]["source"] == "derived"


def test_reset_does_not_mutate_derived_facts(_ws):
    """Reset must NOT delete or mutate derived_facts in product.json."""
    from src import product_db
    from src.product_db import get_effective_facts
    from src.brand_loader import load_product_profile

    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "NoMutateProd", "spec", llm=fake_llm)
    _save_profile("NoMutateProd", {"facts": {"price": "3,590 บาท"}})

    # Reset
    profile = load_product_profile("NoMutateProd")
    del profile["facts"]["price"]
    _save_profile("NoMutateProd", profile)

    # derived_facts in product.json unchanged
    record = product_db.load("NoMutateProd")
    assert record["derived_facts"]["price"]["value"] == "3,990 บาท"


# ---------------------------------------------------------------------------
# 6. Raw evidence remains unchanged
# ---------------------------------------------------------------------------

def test_raw_evidence_unchanged_by_derived_facts(_ws):
    """Adding derived_facts does not alter raw_text or source files."""
    from src import product_db

    raw_content = "UNTOUCHABLE_RAW_EVIDENCE price 999"
    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "RawCheck", raw_content, llm=fake_llm)

    record = product_db.load("RawCheck")
    assert raw_content in record["raw_text"]

    src = _ws["root"] / "data" / "RawCheck" / "spec.txt"
    assert src.read_text(encoding="utf-8") == raw_content


# ---------------------------------------------------------------------------
# 7. Re-ingestion refreshes derived but preserves manual corrections
# ---------------------------------------------------------------------------

def test_reingestion_refreshes_derived_preserves_manual(_ws):
    """Re-ingest → derived_facts refreshed, manual facts preserved."""
    from src import product_db
    from src.product_db import get_effective_facts
    from src.brand_loader import load_product_profile

    # First ingestion with derived_facts
    fake_llm1 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "OLD_PRICE 999"},
    })
    _ingest_txt(_ws, "ReingestDerived", "First version raw text",
                llm=fake_llm1)
    _save_profile("ReingestDerived", {"facts": {"price": "MANUAL_PRICE 500"}})

    # Change source file and re-ingest with new derived_facts
    data_file = _ws["root"] / "data" / "ReingestDerived" / "spec.txt"
    data_file.write_text("Second version raw text", encoding="utf-8")

    fake_llm2 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "NEW_PRICE 888"},
    })
    _ingest_txt(_ws, "ReingestDerived", "Second version raw text",
                llm=fake_llm2)

    # Manual fact preserved
    profile = load_product_profile("ReingestDerived")
    assert profile["facts"]["price"] == "MANUAL_PRICE 500"

    # Derived fact refreshed
    record = product_db.load("ReingestDerived")
    assert record["derived_facts"]["price"]["value"] == "NEW_PRICE 888"

    # Effective uses manual (still wins)
    effective = get_effective_facts("ReingestDerived")
    assert effective["price"]["value"] == "MANUAL_PRICE 500"
    assert effective["price"]["source"] == "manual"


# ---------------------------------------------------------------------------
# 8. Stable machine-key behavior for unchanged facts
# ---------------------------------------------------------------------------

def test_stable_machine_keys_across_reingestion(_ws):
    """Re-ingestion with same source → same machine keys (no unnecessary rename)."""
    from src import product_db

    derived = {
        "price": {"label": "ราคา", "value": "3,990 บาท"},
        "battery_capacity": {"label": "แบตเตอรี่", "value": "800 mAh"},
        "display_size": {"label": "หน้าจอ", "value": "1.4 inch"},
    }
    fake_llm1 = _FakeLLM(derived_facts=derived)
    _ingest_txt(_ws, "StableKeys", "spec text v1", llm=fake_llm1)

    record1 = product_db.load("StableKeys")
    keys1 = set(record1["derived_facts"].keys())

    # Re-ingest with same derived_facts (same source → same extraction)
    fake_llm2 = _FakeLLM(derived_facts=derived)
    _ingest_txt(_ws, "StableKeys", "spec text v1", llm=fake_llm2)

    record2 = product_db.load("StableKeys")
    keys2 = set(record2["derived_facts"].keys())

    assert keys1 == keys2, \
        f"Machine keys must be stable across re-ingestion: {keys1} vs {keys2}"


# ---------------------------------------------------------------------------
# 9. Agent context contains effective facts before raw evidence
# ---------------------------------------------------------------------------

def test_effective_facts_in_agent_context_before_raw(_ws):
    """Effective facts appear BEFORE raw evidence in agent context."""
    from src import product_db

    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "DERIVED_PRICE 3,990"},
    })
    _ingest_txt(_ws, "CtxProd", "RAW_MARKER_777 raw evidence", llm=fake_llm)
    _save_profile("CtxProd", {"facts": {"price": "MANUAL_PRICE 3,590"}})

    ctx = product_db.get_agent_context_text("CtxProd")
    manual_pos = ctx.find("MANUAL_PRICE 3,590")
    raw_pos = ctx.find("RAW_MARKER_777")
    assert manual_pos != -1, "Effective (manual) fact must appear in context"
    assert raw_pos != -1, "Raw evidence must appear in context"
    assert manual_pos < raw_pos, \
        f"Effective facts must appear BEFORE raw evidence ({manual_pos} > {raw_pos})"


def test_derived_facts_in_agent_context_without_manual(_ws):
    """Derived facts appear in agent context even without manual corrections."""
    from src import product_db

    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "DERIVED_ONLY 3,990"},
    })
    _ingest_txt(_ws, "DerivedOnly", "RAW_MARKER_888 raw text", llm=fake_llm)

    ctx = product_db.get_agent_context_text("DerivedOnly")
    derived_pos = ctx.find("DERIVED_ONLY 3,990")
    raw_pos = ctx.find("RAW_MARKER_888")
    assert derived_pos != -1, "Derived fact must appear in context"
    assert raw_pos != -1, "Raw evidence must appear in context"
    assert derived_pos < raw_pos, \
        "Derived facts must appear BEFORE raw evidence"


# ---------------------------------------------------------------------------
# 10. A1/A2 brand isolation
# ---------------------------------------------------------------------------

def test_derived_facts_brand_isolation(_ws, tmp_path, monkeypatch):
    """A1 derived_facts visible only in A1; absent in A2."""
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    from src.brand_registry import BrandRegistry
    from src.product_db import get_effective_facts, load

    # Create brand A2
    reg = BrandRegistry(user_id="test_user", project_root=tmp_path)
    brand2 = reg.create("BrandB")
    bid2 = brand2["brand_id"]
    ws2 = WorkspaceContext.for_brand("test_user", bid2, tmp_path)
    brand2_root = tmp_path / "users" / "test_user" / "brands" / bid2
    for d in ("data", "cache", "output", "brand"):
        (brand2_root / d).mkdir(parents=True, exist_ok=True)

    # In A1: create product with derived_facts
    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "A1_PRICE 3,990"},
    })
    _ingest_txt(_ws, "IsoDerived", "A1 raw text", llm=fake_llm)

    # A1 sees derived_facts
    record_a1 = load("IsoDerived")
    assert record_a1["derived_facts"]["price"]["value"] == "A1_PRICE 3,990"

    # Switch to A2 — product IsoDerived does not exist there
    token2 = set_workspace(ws2)
    try:
        record_a2 = load("IsoDerived")
        assert not record_a2.get("derived_facts"), \
            "A2 must not see A1's derived_facts"
    finally:
        reset_workspace(token2)

    # Switch back to A1 — derived_facts return
    record_a1_back = load("IsoDerived")
    assert record_a1_back["derived_facts"]["price"]["value"] == "A1_PRICE 3,990"


# ---------------------------------------------------------------------------
# 11. Manual-only facts (no derived counterpart) still appear
# ---------------------------------------------------------------------------

def test_manual_only_facts_appear_in_effective(_ws):
    """Manual fact with a key that has no derived counterpart appears standalone."""
    from src.product_db import get_effective_facts

    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "ManualOnly", "spec", llm=fake_llm)
    _save_profile("ManualOnly", {
        "facts": {"price": "3,590 บาท", "custom_field": "custom value"},
    })

    effective = get_effective_facts("ManualOnly")
    # price: manual override of derived
    assert effective["price"]["value"] == "3,590 บาท"
    assert effective["price"]["source"] == "manual"
    # custom_field: manual-only (no derived counterpart)
    assert "custom_field" in effective
    assert effective["custom_field"]["value"] == "custom value"
    assert effective["custom_field"]["source"] == "manual"


# ---------------------------------------------------------------------------
# 12. get_effective_facts returns empty for nonexistent product
# ---------------------------------------------------------------------------

def test_effective_facts_nonexistent_product(_ws):
    """Nonexistent product → empty effective facts."""
    from src.product_db import get_effective_facts

    effective = get_effective_facts("NonExistent")
    assert effective == {}


# ---------------------------------------------------------------------------
# Field deletion — deleted_facts tombstones remove wrong fields entirely
# ---------------------------------------------------------------------------

def test_deleted_facts_tombstone_removes_derived_field(_ws):
    """A wrong FIELD (bad title/key, not just a bad value) must be
    deletable entirely: profile ``deleted_facts`` drops the derived fact
    from the effective view — the UI's delete-field path lands here."""
    from src.product_db import get_effective_facts

    fake_llm = _FakeLLM(derived_facts={
        "front_camera_yes": {"label": "Front Camera YES", "value": "5MP"},
        "battery_capacity": {"label": "แบตเตอรี่", "value": "800 mAh"},
    })
    _ingest_txt(_ws, "DelFieldProd", "spec", llm=fake_llm)

    eff = get_effective_facts("DelFieldProd")
    assert "front_camera_yes" in eff and "battery_capacity" in eff

    # User deletes the wrongly-titled field → tombstone recorded
    _save_profile("DelFieldProd", {"deleted_facts": ["front_camera_yes"]})
    eff = get_effective_facts("DelFieldProd")
    assert "front_camera_yes" not in eff, \
        "tombstoned field must be removed from the effective view"
    assert eff["battery_capacity"]["value"] == "800 mAh", \
        "untouched fields survive"


def test_deleted_facts_readded_manual_key_wins(_ws):
    """A tombstoned key the user re-added manually stays — deletion then
    re-addition is a correction, not a deletion."""
    from src.product_db import get_effective_facts

    fake_llm = _FakeLLM(derived_facts={
        "camera": {"label": "กล้อง", "value": "5MP"},
    })
    _ingest_txt(_ws, "ReaddProd", "spec", llm=fake_llm)
    _save_profile("ReaddProd", {
        "deleted_facts": ["camera"],
        "facts": {"camera": "8MP"},
    })
    eff = get_effective_facts("ReaddProd")
    assert eff["camera"]["value"] == "8MP"
    assert eff["camera"]["source"] == "manual"


def test_deleted_facts_does_not_hide_manual_only(_ws):
    """Tombstoning a key that has no derived counterpart and no manual
    fact is a no-op — nothing to remove."""
    from src.product_db import get_effective_facts

    _ingest_txt(_ws, "DelNoopProd", "plain text spec")
    _save_profile("DelNoopProd", {
        "deleted_facts": ["nonexistent_key"],
        "facts": {"color": "black"},
    })
    eff = get_effective_facts("DelNoopProd")
    assert eff["color"]["value"] == "black"
