"""Product Facts — user correction layer for factual product information.

Proves:
  - product with no ``facts`` remains backward compatible
  - save factual overrides (via the /api/product_profile_save endpoint path)
  - reload persists facts
  - edit existing fact
  - remove fact
  - manual facts survive re-ingestion
  - manual facts appear BEFORE raw evidence in shared agent context
  - Agent-facing context reflects corrected value
  - raw evidence remains unchanged
  - A1/A2 brand isolation

No model call is required for these tests.  Ingestion runs its deterministic
(no-LLM) path — the OpenRouter key is stubbed empty so ``_make_llm()``
returns None and ``_generate_product_profile`` exits early.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Workspace fixture — brand-scoped, same pattern as test_local_workspace.py
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
    # brand-scoped dirs
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
    """Write product_profile.json into cache/{product_id}/ (same as the API)."""
    from src.workspace_context import contain_path
    from src.product_db import _project_root
    profile_dir = contain_path(product_id, _project_root() / "cache")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _ingest_txt(_ws, product_id: str, text: str, filename: str = "spec.txt") -> None:
    """Create a product with a .txt source file and ingest it (no LLM)."""
    import src.openrouter_gateway as gateway
    # Stub the API key empty so _make_llm() returns None — deterministic path.
    monkeypatch_key = getattr(gateway, "get_api_key", None)

    # We can't use monkeypatch here (not a fixture param), so set env directly.
    import os
    old = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        gateway.get_api_key = lambda: ""  # type: ignore[method-assign]
        from src import product_db, ingestion

        data_dir = _ws["root"] / "data" / product_id
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / filename).write_text(text, encoding="utf-8")
        ingestion.ingest_product(product_id, force=True)
    finally:
        gateway.get_api_key = monkeypatch_key  # type: ignore[method-assign]
        if old is not None:
            os.environ["OPENROUTER_API_KEY"] = old


# ---------------------------------------------------------------------------
# 1. Backward compatibility — no facts field
# ---------------------------------------------------------------------------

def test_no_facts_backward_compatible(_ws):
    """Product profile without ``facts`` → agent context unchanged."""
    from src import product_db

    _ingest_txt(_ws, "NoFactsProd", "Raw spec text line 1\nRaw spec line 2")
    # No product_profile.json at all
    ctx = product_db.get_agent_context_text("NoFactsProd")
    assert "Raw spec text line 1" in ctx
    assert "USER-VERIFIED" not in ctx  # no facts section when no facts

    # get_agent_context (multimodal) — text also unchanged
    mc = product_db.get_agent_context("NoFactsProd")
    assert "Raw spec text line 1" in mc["text"]
    assert "USER-VERIFIED" not in mc["text"]


def test_empty_facts_backward_compatible(_ws):
    """Profile with facts={} → no facts section rendered."""
    from src import product_db

    _ingest_txt(_ws, "EmptyFacts", "Some raw text here")
    _save_profile("EmptyFacts", {"facts": {}, "tone_adjustment": "warm"})
    ctx = product_db.get_agent_context_text("EmptyFacts")
    assert "Some raw text here" in ctx
    assert "USER-VERIFIED" not in ctx


# ---------------------------------------------------------------------------
# 2-4. Save / reload / edit / remove facts
# ---------------------------------------------------------------------------

def test_save_and_reload_facts(_ws):
    """Save facts → reload → facts persisted."""
    from src.brand_loader import load_product_profile

    _ingest_txt(_ws, "FactsProd", "Original raw text")
    _save_profile("FactsProd", {
        "facts": {"ราคา": "3,590 บาท", "แบตเตอรี่": "900 mAh"},
        "tone_adjustment": "premium",
    })

    profile = load_product_profile("FactsProd")
    assert profile["facts"]["ราคา"] == "3,590 บาท"
    assert profile["facts"]["แบตเตอรี่"] == "900 mAh"


def test_edit_existing_fact(_ws):
    """Edit an existing fact → new value replaces old."""
    from src.brand_loader import load_product_profile

    _ingest_txt(_ws, "EditProd", "raw")
    _save_profile("EditProd", {"facts": {"ราคา": "3,590 บาท"}})

    # Edit
    profile = load_product_profile("EditProd")
    profile["facts"]["ราคา"] = "4,900 บาท"
    _save_profile("EditProd", profile)

    reloaded = load_product_profile("EditProd")
    assert reloaded["facts"]["ราคา"] == "4,900 บาท"


def test_remove_fact(_ws):
    """Remove a fact key → key absent on reload."""
    from src.brand_loader import load_product_profile

    _ingest_txt(_ws, "RemoveProd", "raw")
    _save_profile("RemoveProd", {"facts": {"ราคา": "3,590 บาท", "GPS": "รองรับ"}})

    profile = load_product_profile("RemoveProd")
    del profile["facts"]["GPS"]
    _save_profile("RemoveProd", profile)

    reloaded = load_product_profile("RemoveProd")
    assert "GPS" not in reloaded["facts"]
    assert reloaded["facts"]["ราคา"] == "3,590 บาท"


# ---------------------------------------------------------------------------
# 5. Re-ingestion protection — facts survive re-ingest
# ---------------------------------------------------------------------------

def test_facts_survive_reingestion(_ws):
    """Save facts → re-ingest → facts unchanged, raw_text rebuilt."""
    from src import product_db, ingestion
    import src.openrouter_gateway as gateway

    _ingest_txt(_ws, "ReingestProd", "First version of raw text")
    _save_profile("ReingestProd", {"facts": {"ราคา": "3,590 บาท"}})

    # Re-ingest with force=True (simulates user pressing "ประมวลผลใหม่")
    # Change the source file content to prove raw_text changes
    data_file = _ws["root"] / "data" / "ReingestProd" / "spec.txt"
    data_file.write_text("Second version — raw text changed", encoding="utf-8")

    old_key = gateway.get_api_key
    import os
    old_env = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        gateway.get_api_key = lambda: ""  # type: ignore[method-assign]
        ingestion.ingest_product("ReingestProd", force=True)
    finally:
        gateway.get_api_key = old_key  # type: ignore[method-assign]
        if old_env is not None:
            os.environ["OPENROUTER_API_KEY"] = old_env

    # Facts still there
    from src.brand_loader import load_product_profile
    profile = load_product_profile("ReingestProd")
    assert profile.get("facts", {}).get("ราคา") == "3,590 บาท"

    # Raw text was rebuilt (changed)
    record = product_db.load("ReingestProd")
    assert "Second version" in record["raw_text"]


# ---------------------------------------------------------------------------
# 6-7. Precedence — facts before raw evidence in agent context
# ---------------------------------------------------------------------------

def test_facts_appear_before_raw_in_context(_ws):
    """Facts section appears BEFORE raw evidence in get_agent_context_text."""
    from src import product_db

    _ingest_txt(_ws, "PrecProd", "RAW_MARKER_12345 raw evidence text")
    _save_profile("PrecProd", {"facts": {"ราคา": "FACT_MARKER_999 บาท"}})

    ctx = product_db.get_agent_context_text("PrecProd")
    facts_pos = ctx.find("FACT_MARKER_999")
    raw_pos = ctx.find("RAW_MARKER_12345")
    assert facts_pos != -1, "Fact marker must appear in context"
    assert raw_pos != -1, "Raw marker must appear in context"
    assert facts_pos < raw_pos, \
        f"Facts must appear BEFORE raw evidence (facts@{facts_pos}, raw@{raw_pos})"


def test_facts_appear_before_raw_in_multimodal(_ws):
    """Facts section appears BEFORE raw evidence in get_agent_context.text."""
    from src import product_db

    _ingest_txt(_ws, "MmProd", "RAW_MM_MARKER raw text")
    _save_profile("MmProd", {"facts": {"สี": "FACT_MM_MARKER ดำ"}})

    mc = product_db.get_agent_context("MmProd")
    text = mc["text"]
    facts_pos = text.find("FACT_MM_MARKER")
    raw_pos = text.find("RAW_MM_MARKER")
    assert facts_pos != -1
    assert raw_pos != -1
    assert facts_pos < raw_pos


def test_facts_appear_before_raw_in_multi_product_context(_ws):
    """Facts appear before raw in build_multi_product_profile_context."""
    from src.brand_loader import build_multi_product_profile_context

    _ingest_txt(_ws, "MultiA", "RAW_MULTI_A text")
    _ingest_txt(_ws, "MultiB", "RAW_MULTI_B text")
    _save_profile("MultiA", {"facts": {"ราคา": "FACT_A 100"}})
    _save_profile("MultiB", {"facts": {"ราคา": "FACT_B 200"}})

    ctx = build_multi_product_profile_context(["MultiA", "MultiB"])
    assert "FACT_A 100" in ctx
    assert "FACT_B 200" in ctx


# ---------------------------------------------------------------------------
# 8. Agent-facing context reflects corrected value
# ---------------------------------------------------------------------------

def test_agent_context_reflects_corrected_value(_ws):
    """Changing a fact changes the context consumed by Agents."""
    from src import product_db

    _ingest_txt(_ws, "AgentProd", "Original raw says price is 999")
    _save_profile("AgentProd", {"facts": {"ราคา": "CORRECTED_PRICE 3,590"}})

    ctx = product_db.get_agent_context_text("AgentProd")
    assert "CORRECTED_PRICE 3,590" in ctx
    # The corrected fact is visible alongside the raw text
    assert "Original raw says price is 999" in ctx


# ---------------------------------------------------------------------------
# 9. Raw evidence remains unchanged
# ---------------------------------------------------------------------------

def test_raw_evidence_unchanged_by_facts(_ws):
    """Adding facts does not alter raw_text or source files."""
    from src import product_db

    raw_content = "UNTOUCHABLE_RAW_EVIDENCE text"
    _ingest_txt(_ws, "RawCheck", raw_content)
    _save_profile("RawCheck", {"facts": {"ราคา": "3,590 บาท"}})

    # raw_text in DB unchanged
    record = product_db.load("RawCheck")
    assert record["raw_text"] == raw_content

    # source file on disk unchanged
    src = _ws["root"] / "data" / "RawCheck" / "spec.txt"
    assert src.read_text(encoding="utf-8") == raw_content


# ---------------------------------------------------------------------------
# 10. A1/A2 brand isolation
# ---------------------------------------------------------------------------

def test_facts_brand_isolation(_ws, tmp_path, monkeypatch):
    """A1 facts visible/editable only in A1; absent in A2; return on switch."""
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    from src.brand_registry import BrandRegistry
    from src.brand_loader import load_product_profile

    # Create brand A2 under the same user
    reg = BrandRegistry(user_id="test_user", project_root=tmp_path)
    brand2 = reg.create("BrandB")
    bid2 = brand2["brand_id"]
    ws2 = WorkspaceContext.for_brand("test_user", bid2, tmp_path)
    brand2_root = tmp_path / "users" / "test_user" / "brands" / bid2
    for d in ("data", "cache", "output", "brand"):
        (brand2_root / d).mkdir(parents=True, exist_ok=True)

    # In A1: create product + facts
    _ingest_txt(_ws, "IsoProd", "A1 raw text")
    _save_profile("IsoProd", {"facts": {"ราคา": "A1_PRICE 3,590"}})

    # Verify A1 sees facts
    profile_a1 = load_product_profile("IsoProd")
    assert profile_a1.get("facts", {}).get("ราคา") == "A1_PRICE 3,590"

    # Switch to A2 — product IsoProd does not exist there
    token2 = set_workspace(ws2)
    try:
        profile_a2 = load_product_profile("IsoProd")
        assert profile_a2 == {}, \
            f"A2 must not see A1's product profile/facts, got {profile_a2}"
    finally:
        reset_workspace(token2)

    # Switch back to A1 — facts return
    profile_a1_back = load_product_profile("IsoProd")
    assert profile_a1_back.get("facts", {}).get("ราคา") == "A1_PRICE 3,590"


# ---------------------------------------------------------------------------
# 11. Save endpoint preserves facts (merge semantics)
# ---------------------------------------------------------------------------

def test_save_endpoint_preserves_facts(_ws, tmp_path, monkeypatch):
    """The /api/product_profile_save endpoint must preserve existing facts
    when saving other profile fields (audience, tone, etc.).

    Simulates the UI flow: user edits tone_adjustment and saves → facts
    must not be lost.
    """
    import importlib
    import web_viewer
    from starlette.testclient import TestClient
    from tests.conftest import make_brand_client

    importlib.reload(web_viewer)
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

    # Create a product with facts already saved
    product_id = "MergeProd"
    cache_dir = brand_root / "cache" / product_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "product_profile.json").write_text(
        json.dumps({"facts": {"ราคา": "3,590 บาท"}, "tone_adjustment": "old"},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Save via the API — only tone_adjustment (no facts in payload)
    # This simulates the UI sending the form data without the facts field.
    resp = client.post(
        f"/api/product_profile_save/{product_id}",
        json={"tone_adjustment": "new tone"},
    )
    assert resp.status_code == 200, resp.text

    # Facts must be preserved
    saved = json.loads(
        (cache_dir / "product_profile.json").read_text(encoding="utf-8")
    )
    assert saved.get("facts", {}).get("ราคา") == "3,590 บาท", \
        "Facts must be preserved when saving other profile fields"
    assert saved.get("tone_adjustment") == "new tone"


def test_save_endpoint_updates_facts(_ws, tmp_path, monkeypatch):
    """Saving with facts in the payload updates facts."""
    import importlib
    import web_viewer
    from starlette.testclient import TestClient
    from tests.conftest import make_brand_client

    importlib.reload(web_viewer)
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

    product_id = "UpdateFactsProd"
    cache_dir = brand_root / "cache" / product_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Save with facts
    resp = client.post(
        f"/api/product_profile_save/{product_id}",
        json={"facts": {"ราคา": "3,590 บาท", "GPS": "รองรับ"}},
    )
    assert resp.status_code == 200

    saved = json.loads(
        (cache_dir / "product_profile.json").read_text(encoding="utf-8")
    )
    assert saved["facts"]["ราคา"] == "3,590 บาท"
    assert saved["facts"]["GPS"] == "รองรับ"

    # Update facts — remove GPS, edit ราคา
    resp = client.post(
        f"/api/product_profile_save/{product_id}",
        json={"facts": {"ราคา": "4,900 บาท"}},
    )
    assert resp.status_code == 200

    saved = json.loads(
        (cache_dir / "product_profile.json").read_text(encoding="utf-8")
    )
    assert saved["facts"]["ราคา"] == "4,900 บาท"
    assert "GPS" not in saved["facts"]
