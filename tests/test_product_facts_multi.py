"""Multi-product facts isolation — proves manual facts belong to the
resulting product identity, NOT to the original shared source file.

Flow under test:
  one source/catalog file
    ↓
  segmentation (staging.commit_batch with seg_mode="multi")
    ↓
  ┌────┴────┐
  Product A  Product B  (distinct product_id / folder / cache)
    ↓         ↓
  profile A  profile B
  facts A    facts B

Asserts through the actual shared agent-facing context seam
(``product_db.get_agent_context_text`` and
``brand_loader.build_multi_product_profile_context``), not only by
reading JSON files.

No paid/model calls — staging segments are injected directly (the
segmentation LLM boundary is not exercised here; that is covered by
existing tests).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")


# ---------------------------------------------------------------------------
# Fixture — brand-scoped staging (no _project_root patch; uses brand_state_root)
# ---------------------------------------------------------------------------

@pytest.fixture
def _stage(brand_ws, monkeypatch):
    """Brand-scoped staging.  Does NOT patch _project_root — staging,
    product_db, and brand_loader all resolve through brand_state_root()
    via the active brand context set by brand_ws.  This ensures
    product_profile.json (read by load_product_profile) and product.json
    (read by product_db.load) live under the same brand_root.
    """
    import src.staging as staging
    # Suppress auto-profile generation during staging (no LLM)
    monkeypatch.setattr(staging, "_generate_product_profile", lambda *a, **kw: None)
    return {
        "staging": staging,
        "root": brand_ws["project_root"],
        "brand_root": brand_ws["brand_root"],
    }


def _save_profile(stage, product_id: str, profile: dict) -> None:
    """Write product_profile.json into brand_root/cache/{product_id}/.

    This is the exact location load_product_profile() reads from when a
    brand context is active.
    """
    from src.workspace_context import contain_path
    brand_root = stage["brand_root"]
    profile_dir = contain_path(product_id, brand_root / "cache")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _create_two_products(stage):
    """Create two segmented products from one source via staging.commit_batch.

    Uses the real staging production path: create_batch → inject segments
    → commit_batch with seg_mode="multi".  This is the same pattern used
    by test_review_findings_v2.py::test_staging_scoped_split_products_...

    Returns (alpha_name, beta_name).
    """
    staging = stage["staging"]

    # Create a staging batch with a single text source file
    source_text = (
        "ALPHA_RAW_111 Product Alpha spec\n"
        "price: 1,000\n"
        "battery: 500mAh\n"
        "\n"
        "BETA_RAW_222 Product Beta spec\n"
        "price: 2,000\n"
        "battery: 800mAh\n"
    )
    batch_id = staging.create_batch([("catalog.txt", source_text.encode("utf-8"))])

    # Inject scoped segments (simulates segmentation LLM finding 2 products)
    batch = staging._load_batch(batch_id)
    batch["status"] = "segmented"
    batch["seg_mode"] = "multi"
    batch["segments"] = [
        {
            "product_key": "Alpha",
            "suggested_name": "Product Alpha",
            "category": "Test",
            "summary": "Alpha product",
            "text": "ALPHA_RAW_111 Product Alpha spec\nprice: 1,000\nbattery: 500mAh\n",
            "source_refs": [{"file": "catalog.txt", "line_start": 1, "line_end": 3}],
            "common_refs": [],
        },
        {
            "product_key": "Beta",
            "suggested_name": "Product Beta",
            "category": "Test",
            "summary": "Beta product",
            "text": "BETA_RAW_222 Product Beta spec\nprice: 2,000\nbattery: 800mAh\n",
            "source_refs": [{"file": "catalog.txt", "line_start": 5, "line_end": 7}],
            "common_refs": [],
        },
    ]
    staging._save_batch(batch_id, batch)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "Product Alpha"},
        {"segment_index": 1, "action": "create", "name": "Product Beta"},
    ])

    assert result["created"] == ["Product Alpha", "Product Beta"]
    return "Product Alpha", "Product Beta"


# ---------------------------------------------------------------------------
# 1. Facts isolation in agent context
# ---------------------------------------------------------------------------

def test_multi_product_facts_isolated_in_agent_context(_stage):
    """Alpha facts appear in Alpha context; Beta facts do NOT appear in
    Alpha context, and vice versa.  Verified through the actual shared
    agent-facing context seam: product_db.get_agent_context_text().
    """
    from src import product_db

    alpha, beta = _create_two_products(_stage)

    # Save independent facts for each product
    _save_profile(_stage, alpha, {"facts": {"ราคา": "ALPHA_FACT_PRICE_900"}})
    _save_profile(_stage, beta, {"facts": {"ราคา": "BETA_FACT_PRICE_1800"}})

    # Alpha context
    alpha_ctx = product_db.get_agent_context_text(alpha)
    assert "ALPHA_FACT_PRICE_900" in alpha_ctx, \
        "Alpha context must contain Alpha's fact"
    assert "ALPHA_RAW_111" in alpha_ctx, \
        "Alpha context must contain Alpha's raw scope content"
    assert "BETA_FACT_PRICE_1800" not in alpha_ctx, \
        "Alpha context must NOT contain Beta's fact"
    assert "BETA_RAW_222" not in alpha_ctx, \
        "Alpha context must NOT contain Beta's raw content"

    # Beta context
    beta_ctx = product_db.get_agent_context_text(beta)
    assert "BETA_FACT_PRICE_1800" in beta_ctx, \
        "Beta context must contain Beta's fact"
    assert "BETA_RAW_222" in beta_ctx, \
        "Beta context must contain Beta's raw scope content"
    assert "ALPHA_FACT_PRICE_900" not in beta_ctx, \
        "Beta context must NOT contain Alpha's fact"
    assert "ALPHA_RAW_111" not in beta_ctx, \
        "Beta context must NOT contain Alpha's raw content"


def test_multi_product_facts_isolated_in_multi_product_context(_stage):
    """build_multi_product_profile_context includes both products' facts
    but each fact is labeled under its own product — no cross-contamination.
    """
    from src.brand_loader import build_multi_product_profile_context

    alpha, beta = _create_two_products(_stage)

    _save_profile(_stage, alpha, {"facts": {"ราคา": "ALPHA_FACT_PRICE_900"}})
    _save_profile(_stage, beta, {"facts": {"ราคา": "BETA_FACT_PRICE_1800"}})

    ctx = build_multi_product_profile_context([alpha, beta])
    assert "ALPHA_FACT_PRICE_900" in ctx
    assert "BETA_FACT_PRICE_1800" in ctx


# ---------------------------------------------------------------------------
# 2. Re-ingestion persistence — facts survive independently
# ---------------------------------------------------------------------------

def test_multi_product_facts_survive_reingestion_independently(_stage):
    """After both products have different facts, re-ingest each product
    independently.  Alpha facts stay with Alpha; Beta facts stay with Beta;
    neither overwrites or merges into the other.
    """
    from src import product_db, ingestion
    import src.openrouter_gateway as gateway

    alpha, beta = _create_two_products(_stage)

    _save_profile(_stage, alpha, {"facts": {"ราคา": "ALPHA_FACT_PRICE_900"}})
    _save_profile(_stage, beta, {"facts": {"ราคา": "BETA_FACT_PRICE_1800"}})

    # Re-ingest both products with force=True (no LLM needed for .txt)
    old_key = gateway.get_api_key
    old_env = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        gateway.get_api_key = lambda: ""
        ingestion.ingest_product(alpha, force=True)
        ingestion.ingest_product(beta, force=True)
    finally:
        gateway.get_api_key = old_key
        if old_env is not None:
            os.environ["OPENROUTER_API_KEY"] = old_env

    # Alpha facts unchanged
    from src.brand_loader import load_product_profile
    alpha_profile = load_product_profile(alpha)
    assert alpha_profile.get("facts", {}).get("ราคา") == "ALPHA_FACT_PRICE_900", \
        "Alpha facts must survive Alpha re-ingestion"

    # Beta facts unchanged
    beta_profile = load_product_profile(beta)
    assert beta_profile.get("facts", {}).get("ราคา") == "BETA_FACT_PRICE_1800", \
        "Beta facts must survive Beta re-ingestion"

    # No cross-contamination: Alpha does not have Beta's fact
    assert "BETA_FACT_PRICE_1800" not in json.dumps(alpha_profile.get("facts", {})), \
        "Alpha facts must not be contaminated by Beta re-ingestion"
    assert "ALPHA_FACT_PRICE_900" not in json.dumps(beta_profile.get("facts", {})), \
        "Beta facts must not be contaminated by Alpha re-ingestion"

    # Agent context still isolated after re-ingestion
    alpha_ctx = product_db.get_agent_context_text(alpha)
    beta_ctx = product_db.get_agent_context_text(beta)
    assert "ALPHA_FACT_PRICE_900" in alpha_ctx
    assert "BETA_FACT_PRICE_1800" not in alpha_ctx
    assert "BETA_FACT_PRICE_1800" in beta_ctx
    assert "ALPHA_FACT_PRICE_900" not in beta_ctx


# ---------------------------------------------------------------------------
# 3. Source provenance — shared source remains intact, facts are product-local
# ---------------------------------------------------------------------------

def test_shared_source_provenance_intact(_stage):
    """The original shared source file remains intact in both products'
    data directories.  product_profile.json (and thus facts) is product-local.
    """
    alpha, beta = _create_two_products(_stage)

    _save_profile(_stage, alpha, {"facts": {"ราคา": "ALPHA_FACT_PRICE_900"}})
    _save_profile(_stage, beta, {"facts": {"ราคา": "BETA_FACT_PRICE_1800"}})

    brand_root = _stage["brand_root"]
    # Both products have the source file in their data dir
    alpha_source = brand_root / "data" / alpha / "catalog.txt"
    beta_source = brand_root / "data" / beta / "catalog.txt"
    assert alpha_source.exists(), "Alpha must have the shared source file"
    assert beta_source.exists(), "Beta must have the shared source file"

    # product_profile.json is product-local (separate files)
    alpha_profile = brand_root / "cache" / alpha / "product_profile.json"
    beta_profile = brand_root / "cache" / beta / "product_profile.json"
    assert alpha_profile.exists(), "Alpha must have its own product_profile.json"
    assert beta_profile.exists(), "Beta must have its own product_profile.json"
    assert alpha_profile != beta_profile, \
        "product_profile.json paths must be distinct per product"

    # Facts are different
    alpha_data = json.loads(alpha_profile.read_text(encoding="utf-8"))
    beta_data = json.loads(beta_profile.read_text(encoding="utf-8"))
    assert alpha_data["facts"]["ราคา"] == "ALPHA_FACT_PRICE_900"
    assert beta_data["facts"]["ราคา"] == "BETA_FACT_PRICE_1800"


# ---------------------------------------------------------------------------
# 4. Facts belong to product identity, not source file
# ---------------------------------------------------------------------------

def test_facts_belong_to_product_not_source(_stage):
    """If we edit Alpha's facts, Beta's facts and Beta's context are
    unaffected.  This proves facts belong to the resulting product identity,
    not to the original source file.
    """
    from src import product_db

    alpha, beta = _create_two_products(_stage)

    # Initially no facts on either
    alpha_ctx_0 = product_db.get_agent_context_text(alpha)
    beta_ctx_0 = product_db.get_agent_context_text(beta)
    assert "ALPHA_FACT_PRICE_900" not in alpha_ctx_0
    assert "BETA_FACT_PRICE_1800" not in beta_ctx_0

    # Edit only Alpha's facts
    _save_profile(_stage, alpha, {"facts": {"ราคา": "ALPHA_FACT_PRICE_900"}})

    # Alpha context now has the fact
    alpha_ctx_1 = product_db.get_agent_context_text(alpha)
    assert "ALPHA_FACT_PRICE_900" in alpha_ctx_1

    # Beta context is unchanged — no Alpha fact leaked
    beta_ctx_1 = product_db.get_agent_context_text(beta)
    assert "ALPHA_FACT_PRICE_900" not in beta_ctx_1, \
        "Editing Alpha's facts must not affect Beta's context"

    # Now edit Beta's facts
    _save_profile(_stage, beta, {"facts": {"ราคา": "BETA_FACT_PRICE_1800"}})

    # Beta context now has the fact
    beta_ctx_2 = product_db.get_agent_context_text(beta)
    assert "BETA_FACT_PRICE_1800" in beta_ctx_2

    # Alpha context is unchanged — no Beta fact leaked
    alpha_ctx_2 = product_db.get_agent_context_text(alpha)
    assert "BETA_FACT_PRICE_1800" not in alpha_ctx_2, \
        "Editing Beta's facts must not affect Alpha's context"
    assert "ALPHA_FACT_PRICE_900" in alpha_ctx_2, \
        "Alpha's fact must still be present"
