"""Unit tests for the campaign_strategy real-world evaluation harness.

These tests check only the deterministic, non-LLM parts of the evaluator:
the 3 case contexts and the result-saving logic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.eval_campaign_strategy import build_contexts, save_results


def test_build_contexts_returns_three_cases():
    product = "สินค้า: LAGENIO K2\nหน้าจอ AMOLED"
    competitor = "คู่แข่ง: imoo Z1\nราคา 2,990"
    contexts = build_contexts(product, competitor)

    assert set(contexts.keys()) == {"A_product_only", "B_with_competitor", "C_missing_financials"}

    # Test A: only product
    assert contexts["A_product_only"]["product"] == product
    assert "competitors" not in contexts["A_product_only"]
    assert "market" not in contexts["A_product_only"]
    assert "business" not in contexts["A_product_only"]

    # Test B: product + competitor
    assert contexts["B_with_competitor"]["product"] == product
    assert contexts["B_with_competitor"]["competitors"] == competitor

    # Test C: product + business context without financial data
    assert contexts["C_missing_financials"]["product"] == product
    business = contexts["C_missing_financials"]["business"]
    assert business
    for forbidden in ("COGS", "ต้นทุน", "margin", "กำไร", "งบประมาณ", "budget"):
        assert forbidden not in business, f"business context must not contain {forbidden}"


def test_save_results_writes_markdown_and_json(tmp_path: Path):
    results = [
        {
            "case_id": "A",
            "case_name": "product_only",
            "output": "## ราคาแนะนำ\n- ราคา: 1,000",
        }
    ]
    paths = save_results(results, tmp_path, timestamp="20240101_120000")

    assert len(paths) == 1
    md_path = paths[0]
    json_path = md_path.with_suffix(".json")
    assert md_path.exists()
    assert json_path.exists()
    assert "## ราคาแนะนำ" in md_path.read_text(encoding="utf-8")
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved[0]["case_id"] == "A"
    assert saved[0]["case_name"] == "product_only"
