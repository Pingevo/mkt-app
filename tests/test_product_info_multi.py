"""Multi-product derived facts + manual correction isolation.

Proves:
  - Alpha derived_facts do not leak into Beta
  - Beta derived_facts do not leak into Alpha
  - Manual correction on Alpha affects only Alpha
  - Re-ingestion preserves Alpha/Beta manual corrections independently
  - Effective facts are product-local after segmentation

Uses the same staging-based multi-product flow as test_product_facts_multi.py.
No paid/model calls — derived_facts are injected directly into product.json.
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
# Fixture — brand-scoped staging (same as test_product_facts_multi.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def _stage(brand_ws, monkeypatch):
    """Brand-scoped staging."""
    import src.staging as staging
    monkeypatch.setattr(staging, "_generate_product_profile", lambda *a, **kw: None)
    return {
        "staging": staging,
        "root": brand_ws["project_root"],
        "brand_root": brand_ws["brand_root"],
    }


def _save_profile(stage, product_id: str, profile: dict) -> None:
    """Write product_profile.json into brand_root/cache/{product_id}/."""
    from src.workspace_context import contain_path
    brand_root = stage["brand_root"]
    profile_dir = contain_path(product_id, brand_root / "cache")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _set_derived_facts(stage, product_id: str, derived_facts: dict) -> None:
    """Inject derived_facts into product.json (simulates LLM extraction)."""
    from src import product_db
    record = product_db.load(product_id)
    record["derived_facts"] = derived_facts
    product_db.save(product_id, record)


def _create_two_products(stage):
    """Create two segmented products from one source via staging.commit_batch."""
    staging = stage["staging"]

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
# 1. Derived facts isolation in agent context
# ---------------------------------------------------------------------------

def test_multi_product_derived_facts_isolated(_stage):
    """Alpha derived_facts appear in Alpha context; Beta derived_facts do NOT."""
    from src import product_db

    alpha, beta = _create_two_products(_stage)

    _set_derived_facts(_stage, alpha, {
        "price": {"label": "ราคา", "value": "ALPHA_DERIVED_1000"},
    })
    _set_derived_facts(_stage, beta, {
        "price": {"label": "ราคา", "value": "BETA_DERIVED_2000"},
    })

    alpha_ctx = product_db.get_agent_context_text(alpha)
    assert "ALPHA_DERIVED_1000" in alpha_ctx
    assert "ALPHA_RAW_111" in alpha_ctx
    assert "BETA_DERIVED_2000" not in alpha_ctx
    assert "BETA_RAW_222" not in alpha_ctx

    beta_ctx = product_db.get_agent_context_text(beta)
    assert "BETA_DERIVED_2000" in beta_ctx
    assert "BETA_RAW_222" in beta_ctx
    assert "ALPHA_DERIVED_1000" not in beta_ctx
    assert "ALPHA_RAW_111" not in beta_ctx


# ---------------------------------------------------------------------------
# 2. Manual correction isolation
# ---------------------------------------------------------------------------

def test_multi_product_manual_correction_isolated(_stage):
    """Manual correction on Alpha does not affect Beta."""
    from src.product_db import get_effective_facts

    alpha, beta = _create_two_products(_stage)

    _set_derived_facts(_stage, alpha, {
        "price": {"label": "ราคา", "value": "ALPHA_DERIVED_1000"},
    })
    _set_derived_facts(_stage, beta, {
        "price": {"label": "ราคา", "value": "BETA_DERIVED_2000"},
    })

    # Correct only Alpha
    _save_profile(_stage, alpha, {"facts": {"price": "ALPHA_MANUAL_900"}})
    # Beta: no manual correction
    _save_profile(_stage, beta, {"facts": {}})

    alpha_eff = get_effective_facts(alpha)
    assert alpha_eff["price"]["value"] == "ALPHA_MANUAL_900"
    assert alpha_eff["price"]["source"] == "manual"

    beta_eff = get_effective_facts(beta)
    assert beta_eff["price"]["value"] == "BETA_DERIVED_2000"
    assert beta_eff["price"]["source"] == "derived"

    # Alpha context has manual, Beta context has derived
    from src import product_db
    alpha_ctx = product_db.get_agent_context_text(alpha)
    beta_ctx = product_db.get_agent_context_text(beta)
    assert "ALPHA_MANUAL_900" in alpha_ctx
    assert "BETA_DERIVED_2000" in beta_ctx
    assert "ALPHA_MANUAL_900" not in beta_ctx
    assert "BETA_DERIVED_2000" not in alpha_ctx


# ---------------------------------------------------------------------------
# 3. Independent manual corrections on both products
# ---------------------------------------------------------------------------

def test_multi_product_independent_manual_corrections(_stage):
    """Both products have manual corrections; each stays isolated."""
    from src.product_db import get_effective_facts

    alpha, beta = _create_two_products(_stage)

    _set_derived_facts(_stage, alpha, {
        "price": {"label": "ราคา", "value": "ALPHA_DERIVED_1000"},
        "battery_capacity": {"label": "แบตเตอรี่", "value": "ALPHA_DERIVED_500mAh"},
    })
    _set_derived_facts(_stage, beta, {
        "price": {"label": "ราคา", "value": "BETA_DERIVED_2000"},
        "battery_capacity": {"label": "แบตเตอรี่", "value": "BETA_DERIVED_800mAh"},
    })

    _save_profile(_stage, alpha, {"facts": {
        "price": "ALPHA_MANUAL_900",
        "battery_capacity": "ALPHA_MANUAL_600mAh",
    }})
    _save_profile(_stage, beta, {"facts": {
        "price": "BETA_MANUAL_1800",
        "battery_capacity": "BETA_MANUAL_700mAh",
    }})

    alpha_eff = get_effective_facts(alpha)
    assert alpha_eff["price"]["value"] == "ALPHA_MANUAL_900"
    assert alpha_eff["battery_capacity"]["value"] == "ALPHA_MANUAL_600mAh"

    beta_eff = get_effective_facts(beta)
    assert beta_eff["price"]["value"] == "BETA_MANUAL_1800"
    assert beta_eff["battery_capacity"]["value"] == "BETA_MANUAL_700mAh"


# ---------------------------------------------------------------------------
# 4. Re-ingestion preserves manual corrections independently
# ---------------------------------------------------------------------------

def test_multi_product_reingestion_preserves_manual(_stage):
    """Re-ingest refreshes derived_facts but preserves manual corrections."""
    from src.product_db import get_effective_facts
    from src.brand_loader import load_product_profile

    alpha, beta = _create_two_products(_stage)

    _set_derived_facts(_stage, alpha, {
        "price": {"label": "ราคา", "value": "ALPHA_OLD_DERIVED"},
    })
    _set_derived_facts(_stage, beta, {
        "price": {"label": "ราคา", "value": "BETA_OLD_DERIVED"},
    })

    _save_profile(_stage, alpha, {"facts": {"price": "ALPHA_MANUAL_900"}})
    _save_profile(_stage, beta, {"facts": {"price": "BETA_MANUAL_1800"}})

    # Simulate re-ingestion: refresh derived_facts (new values)
    _set_derived_facts(_stage, alpha, {
        "price": {"label": "ราคา", "value": "ALPHA_NEW_DERIVED"},
    })
    _set_derived_facts(_stage, beta, {
        "price": {"label": "ราคา", "value": "BETA_NEW_DERIVED"},
    })

    # Manual corrections preserved
    alpha_profile = load_product_profile(alpha)
    assert alpha_profile["facts"]["price"] == "ALPHA_MANUAL_900"
    beta_profile = load_product_profile(beta)
    assert beta_profile["facts"]["price"] == "BETA_MANUAL_1800"

    # Effective uses manual (still wins over new derived)
    alpha_eff = get_effective_facts(alpha)
    assert alpha_eff["price"]["value"] == "ALPHA_MANUAL_900"
    beta_eff = get_effective_facts(beta)
    assert beta_eff["price"]["value"] == "BETA_MANUAL_1800"


# ---------------------------------------------------------------------------
# 5. Multi-product context includes both without cross-contamination
# ---------------------------------------------------------------------------

def test_multi_product_derived_facts_in_multi_context(_stage):
    """build_multi_product_profile_context includes both products' facts."""
    from src.brand_loader import build_multi_product_profile_context

    alpha, beta = _create_two_products(_stage)

    _set_derived_facts(_stage, alpha, {
        "price": {"label": "ราคา", "value": "ALPHA_DERIVED_1000"},
    })
    _set_derived_facts(_stage, beta, {
        "price": {"label": "ราคา", "value": "BETA_DERIVED_2000"},
    })

    ctx = build_multi_product_profile_context([alpha, beta])
    assert "ALPHA_DERIVED_1000" in ctx
    assert "BETA_DERIVED_2000" in ctx
