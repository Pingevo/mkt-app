"""Gate 1+2+3 tests — legacy compatibility, key stability, URL provenance.

Gate 1: Legacy manual-facts compatibility
  - legacy manual key matches derived label exactly (normalized)
  - no duplicate effective rows
  - ambiguous match preserved as separate manual fact
  - no destructive migration

Gate 2: Machine-key stability
  - existing derived keys supplied to LLM on re-ingestion
  - manual correction survives re-ingestion when LLM reuses key
  - removed/new attributes behave safely
  - no duplicate effective rows

Gate 3: Source provenance
  - URL-imported product shows มาจาก URL
  - ordinary product shows มาจากไฟล์

No paid/live calls — FakeLLM/deterministic fixtures only.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class _FakeLLM:
    """Deterministic LLM double for ingestion.metadata_summary."""

    def __init__(self, derived_facts: dict | None = None,
                 summary: str = "Test summary",
                 category: str = "Test Category"):
        self._derived = derived_facts or {}
        self._summary = summary
        self._category = category
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return json.dumps({
            "summary": self._summary,
            "category": self._category,
            "derived_facts": self._derived,
        }, ensure_ascii=False)

    def close(self):
        pass

    def abort(self):
        pass


@pytest.fixture
def _ws(tmp_path, monkeypatch):
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
    yield {"root": brand_root, "ws": ws}
    set_workspace(None)
    reset_workspace(token)


def _save_profile(_ws, product_id: str, profile: dict) -> None:
    from src.workspace_context import contain_path
    from src.product_db import _project_root
    profile_dir = contain_path(product_id, _project_root() / "cache")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _set_derived_facts(_ws, product_id: str, derived_facts: dict) -> None:
    from src import product_db
    record = product_db.load(product_id)
    record["derived_facts"] = derived_facts
    product_db.save(product_id, record)


def _ingest_txt(_ws, product_id: str, text: str, filename: str = "spec.txt",
                llm=None) -> None:
    import src.openrouter_gateway as gateway
    from src import ingestion

    data_dir = _ws["root"] / "data" / product_id
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / filename).write_text(text, encoding="utf-8")

    old_key = gateway.get_api_key
    old_env = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        if llm is not None:
            gateway.get_api_key = lambda: "fake-key"
            ingestion.ingest_product(product_id, force=True, llm=llm)
        else:
            gateway.get_api_key = lambda: ""
            ingestion.ingest_product(product_id, force=True)
    finally:
        gateway.get_api_key = old_key
        if old_env is not None:
            os.environ["OPENROUTER_API_KEY"] = old_env


# ===========================================================================
# Gate 1: Legacy manual-facts compatibility
# ===========================================================================

def test_legacy_thai_label_overrides_derived_by_label_match(_ws):
    """Legacy manual key 'ราคา' matches derived label 'ราคา' (key=price).
    Effective must show price=3,590 (manual), not two duplicate rows.
    """
    from src.product_db import get_effective_facts

    _ingest_txt(_ws, "LegacyLabel", "spec text")
    _set_derived_facts(_ws, "LegacyLabel", {
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _save_profile(_ws, "LegacyLabel", {"facts": {"ราคา": "3,590 บาท"}})

    effective = get_effective_facts("LegacyLabel")
    # Must NOT have two rows (one for "price" and one for "ราคา")
    assert len(effective) == 1, f"Expected 1 effective fact, got {len(effective)}: {effective}"
    # The single row must be the manual correction
    assert "price" in effective, "Legacy label match must map to derived machine key 'price'"
    assert effective["price"]["value"] == "3,590 บาท"
    assert effective["price"]["source"] == "manual"
    assert effective["price"]["label"] == "ราคา"


def test_legacy_label_normalized_match(_ws):
    """Legacy key '  ราคา  ' (with whitespace) matches derived label 'ราคา'."""
    from src.product_db import get_effective_facts

    _ingest_txt(_ws, "LegacyNorm", "spec text")
    _set_derived_facts(_ws, "LegacyNorm", {
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _save_profile(_ws, "LegacyNorm", {"facts": {"  ราคา  ": "3,590 บาท"}})

    effective = get_effective_facts("LegacyNorm")
    assert len(effective) == 1
    assert effective["price"]["value"] == "3,590 บาท"
    assert effective["price"]["source"] == "manual"


def test_legacy_unrelated_manual_key_remains_independent(_ws):
    """Legacy manual key that doesn't match any derived label stays separate."""
    from src.product_db import get_effective_facts

    _ingest_txt(_ws, "LegacyUnrelated", "spec text")
    _set_derived_facts(_ws, "LegacyUnrelated", {
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _save_profile(_ws, "LegacyUnrelated", {
        "facts": {"ราคา": "3,590 บาท", "custom_note": "my note"},
    })

    effective = get_effective_facts("LegacyUnrelated")
    assert len(effective) == 2
    assert effective["price"]["value"] == "3,590 บาท"
    assert effective["price"]["source"] == "manual"
    assert "custom_note" in effective
    assert effective["custom_note"]["value"] == "my note"
    assert effective["custom_note"]["source"] == "manual"


def test_legacy_ambiguous_label_match_preserved_as_separate(_ws):
    """If legacy key matches multiple derived labels, don't guess — keep separate."""
    from src.product_db import get_effective_facts

    _ingest_txt(_ws, "LegacyAmbiguous", "spec text")
    _set_derived_facts(_ws, "LegacyAmbiguous", {
        "price_a": {"label": "ราคา", "value": "3,990 บาท"},
        "price_b": {"label": "ราคา", "value": "2,990 บาท"},
    })
    _save_profile(_ws, "LegacyAmbiguous", {"facts": {"ราคา": "1,990 บาท"}})

    effective = get_effective_facts("LegacyAmbiguous")
    # Both derived facts appear with derived values; legacy manual stays separate
    assert "price_a" in effective
    assert effective["price_a"]["value"] == "3,990 บาท"
    assert effective["price_a"]["source"] == "derived"
    assert "price_b" in effective
    assert effective["price_b"]["value"] == "2,990 บาท"
    assert effective["price_b"]["source"] == "derived"
    # Legacy manual key preserved as separate (not merged into either)
    assert "ราคา" in effective
    assert effective["ราคา"]["value"] == "1,990 บาท"
    assert effective["ราคา"]["source"] == "manual"


def test_legacy_no_destructive_migration(_ws):
    """product_profile.json must not be modified by get_effective_facts."""
    from src.product_db import get_effective_facts
    from src.brand_loader import load_product_profile

    _ingest_txt(_ws, "NoMigrate", "spec text")
    _set_derived_facts(_ws, "NoMigrate", {
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _save_profile(_ws, "NoMigrate", {"facts": {"ราคา": "3,590 บาท"}})

    # Read before
    before = load_product_profile("NoMigrate")
    # Call effective
    get_effective_facts("NoMigrate")
    # Read after
    after = load_product_profile("NoMigrate")
    assert before == after, "product_profile.json must not be modified by get_effective_facts"


def test_legacy_save_reload_preserves_behavior(_ws):
    """Save+reload preserves the legacy label-match behavior."""
    from src.product_db import get_effective_facts

    _ingest_txt(_ws, "LegacyReload", "spec text")
    _set_derived_facts(_ws, "LegacyReload", {
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _save_profile(_ws, "LegacyReload", {"facts": {"ราคา": "3,590 บาท"}})

    effective1 = get_effective_facts("LegacyReload")
    # Re-save same profile (simulates UI save round-trip)
    _save_profile(_ws, "LegacyReload", {"facts": {"ราคา": "3,590 บาท"}})
    effective2 = get_effective_facts("LegacyReload")

    assert effective1 == effective2


# ===========================================================================
# Gate 2: Machine-key stability
# ===========================================================================

def test_reingestion_supplies_existing_keys_to_llm(_ws):
    """Re-ingestion must pass existing derived_facts keys to the LLM call."""
    # First ingestion with derived_facts
    fake_llm1 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
        "battery_capacity": {"label": "แบตเตอรี่", "value": "800 mAh"},
    })
    _ingest_txt(_ws, "KeyStability", "spec v1", llm=fake_llm1)

    # Second ingestion — FakeLLM should receive existing keys in prompt
    fake_llm2 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
        "battery_capacity": {"label": "แบตเตอรี่", "value": "800 mAh"},
    })
    _ingest_txt(_ws, "KeyStability", "spec v1", llm=fake_llm2)

    # The second LLM call's prompt must mention existing keys
    # Filter to the metadata_summary call (not the positioning call)
    summary_calls = [c for c in fake_llm2.calls
                     if c["kwargs"].get("source") == "ingestion.metadata_summary"]
    assert len(summary_calls) >= 1, "metadata_summary call must exist on re-ingestion"
    second_call = summary_calls[-1]
    user_msg = second_call["messages"][0]["content"]
    assert "price" in user_msg, "Existing key 'price' must be supplied to LLM on re-ingestion"
    assert "battery_capacity" in user_msg, "Existing key 'battery_capacity' must be supplied"


def test_manual_correction_survives_reingestion_with_key_reuse(_ws):
    """Manual correction survives re-ingestion when LLM reuses the same key."""
    from src.product_db import get_effective_facts

    # First ingestion
    fake_llm1 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "SurviveReingest", "spec v1", llm=fake_llm1)
    _save_profile(_ws, "SurviveReingest", {"facts": {"price": "3,590 บาท"}})

    # Re-ingest with same key (LLM reuses key as instructed)
    fake_llm2 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "SurviveReingest", "spec v1", llm=fake_llm2)

    effective = get_effective_facts("SurviveReingest")
    assert effective["price"]["value"] == "3,590 บาท"
    assert effective["price"]["source"] == "manual"


def test_removed_attribute_behaves_safely(_ws):
    """If LLM drops an attribute on re-ingestion, manual correction remains
    as manual-only (no crash, no duplicate)."""
    from src.product_db import get_effective_facts

    # First ingestion with price + battery
    fake_llm1 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
        "battery_capacity": {"label": "แบตเตอรี่", "value": "800 mAh"},
    })
    _ingest_txt(_ws, "RemovedAttr", "spec v1", llm=fake_llm1)
    _save_profile(_ws, "RemovedAttr", {"facts": {"price": "3,590 บาท"}})

    # Re-ingest — LLM drops battery, keeps price
    fake_llm2 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "RemovedAttr", "spec v1", llm=fake_llm2)

    effective = get_effective_facts("RemovedAttr")
    # price: manual correction still wins
    assert effective["price"]["value"] == "3,590 บาท"
    assert effective["price"]["source"] == "manual"
    # battery: gone from derived, no manual → not in effective
    assert "battery_capacity" not in effective


def test_new_attribute_behaves_safely(_ws):
    """If LLM adds a new attribute on re-ingestion, it appears as derived."""
    from src.product_db import get_effective_facts

    # First ingestion with price only
    fake_llm1 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "NewAttr", "spec v1", llm=fake_llm1)

    # Re-ingest — LLM adds display_size
    fake_llm2 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
        "display_size": {"label": "หน้าจอ", "value": "1.4 inch"},
    })
    _ingest_txt(_ws, "NewAttr", "spec v1", llm=fake_llm2)

    effective = get_effective_facts("NewAttr")
    assert effective["price"]["value"] == "3,990 บาท"
    assert effective["price"]["source"] == "derived"
    assert effective["display_size"]["value"] == "1.4 inch"
    assert effective["display_size"]["source"] == "derived"


def test_no_duplicate_effective_rows_after_reingestion(_ws):
    """No duplicate effective rows appear after re-ingestion."""
    from src.product_db import get_effective_facts

    fake_llm1 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "NoDup", "spec v1", llm=fake_llm1)
    _save_profile(_ws, "NoDup", {"facts": {"price": "3,590 บาท"}})

    fake_llm2 = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "NoDup", "spec v1", llm=fake_llm2)

    effective = get_effective_facts("NoDup")
    assert len(effective) == 1, f"Expected 1 row, got {len(effective)}: {effective}"


# ===========================================================================
# Gate 3: Source provenance (API-level test)
# ===========================================================================

def test_product_info_api_returns_url_provenance(tmp_path, monkeypatch):
    """/api/product_info returns url_provenance when product was URL-imported."""
    import web_viewer
    from tests.conftest import make_brand_client

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "_current_llm", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_session_ts", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_cancel_requested", {}, raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    client, uid, bid, brand_root = make_brand_client(
        web_viewer.app, tmp_path, monkeypatch
    )

    # Create product file directly, then ingest via API (sets workspace context)
    data_dir = brand_root / "data" / "UrlProd"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "spec.txt").write_text("spec text", encoding="utf-8")

    resp = client.post("/api/ingest/UrlProd", json={})
    assert resp.status_code == 200, f"ingest failed: {resp.text}"
    # Wait for ingestion
    import time
    for _ in range(30):
        resp = client.get("/api/ingest_status/UrlProd")
        status = resp.json().get("status", "")
        if status in ("ready", "no_usable_data", "empty"):
            break
        time.sleep(1)

    # Add source_import via direct product_db access (needs workspace context)
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    ws = WorkspaceContext.for_brand(uid, bid, tmp_path)
    token = set_workspace(ws)
    try:
        from src import product_db
        record = product_db.load("UrlProd")
        record["source_import"] = {
            "original_url": "https://example.com/product",
            "final_url": "https://example.com/product",
            "page_title": "Example Product",
            "fetched_at": "2026-01-01T00:00:00Z",
        }
        product_db.save("UrlProd", record)
    finally:
        reset_workspace(token)

    resp = client.get("/api/product_info/UrlProd")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["url_provenance"] is not None
    assert data["url_provenance"]["original_url"] == "https://example.com/product"
    assert data["source_type"] == "url"


def test_product_info_api_no_url_provenance_for_ordinary_product(tmp_path, monkeypatch):
    """/api/product_info returns url_provenance=null for ordinary uploaded product."""
    import web_viewer
    from tests.conftest import make_brand_client

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "_current_llm", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_session_ts", {}, raising=False)
    monkeypatch.setattr(web_viewer, "_cancel_requested", {}, raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}, "campaign_strategy": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    client, uid, bid, brand_root = make_brand_client(
        web_viewer.app, tmp_path, monkeypatch
    )

    data_dir = brand_root / "data" / "OrdinaryProd"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "spec.txt").write_text("spec text", encoding="utf-8")

    resp = client.post("/api/ingest/OrdinaryProd", json={})
    assert resp.status_code == 200, f"ingest failed: {resp.text}"
    import time
    for _ in range(30):
        resp = client.get("/api/ingest_status/OrdinaryProd")
        status = resp.json().get("status", "")
        if status in ("ready", "no_usable_data", "empty"):
            break
        time.sleep(1)

    resp = client.get("/api/product_info/OrdinaryProd")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["url_provenance"] is None
    assert data["source_type"] == "file"
