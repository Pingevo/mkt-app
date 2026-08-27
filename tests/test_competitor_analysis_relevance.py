"""TDD tests for CompetitorAnalysisAgent source relevance hook.

Offline — no OpenRouter calls.
"""

from __future__ import annotations

import pytest

from src.agents.competitor_analysis import CompetitorAnalysisAgent


def _make_agent() -> CompetitorAnalysisAgent:
    config = {
        "relevance_policy": {
            "target_category_keywords": ["smartwatch", "smart watch"],
            "mismatch_keywords": [
                "keyboard",
                "mechanical keyboard",
                "gaming keyboard",
                "keycaps",
                "mouse",
                "gaming mouse",
                "headphones",
                "earbuds",
            ],
            "subcategory_mismatch_keywords": ["kids", "children", "elderly"],
            "market_keywords": [
                "smart watch",
                "smartwatch",
                "smartband",
            ],
        }
    }
    return CompetitorAnalysisAgent(agent_config=config, llm_client=None)


def _build_context(agent: CompetitorAnalysisAgent) -> None:
    product_spec = (
        "--- ขอบเขตสินค้า ---\n"
        "รหัสสินค้า: K77\n"
        "--- สิ้นสุดขอบเขตสินค้า ---\n\n"
        "--- ข้อมูลดิบ (text) ---\n"
        "CACGO K77 smart watch with Bluetooth calling, 1000mAh battery\n"
        "--- สิ้นสุดข้อมูลดิบ ---\n"
    )
    competitor_data = (
        "Redmi Watch 3 Active\n"
        "Haylou Solar Plus RT3\n"
        "Mibro Watch A2"
    )
    agent.build_prompt(product_spec, competitor_data)


@pytest.fixture
def agent() -> CompetitorAnalysisAgent:
    a = _make_agent()
    _build_context(a)
    return a


@pytest.mark.parametrize(
    "url,title,expected_relevant,expected_type",
    [
        # obvious category mismatch
        (
            "https://www.amazon.com/Redragon-Wireless-Mechanical-Keyboard/dp/B0GZYT25QJ",
            "Redragon Wireless Mechanical Gaming Keyboard",
            False,
            "category_mismatch",
        ),
        (
            "https://teamplush.by/th/product/e-3lue-k771-keyboard/",
            "E-3LUE K771 Mechanical Keyboard",
            False,
            "category_mismatch",
        ),
        # target
        (
            "https://shopee.co.th/CACGO-K77-Smart-Watch-p-123456789",
            "CACGO K77 Smart Watch Bluetooth Calling 1000mAh",
            True,
            "target",
        ),
        # competitors
        (
            "https://www.mi.com/redmi-watch-3-active",
            "Redmi Watch 3 Active",
            True,
            "competitor",
        ),
        (
            "https://haylou.com/solar-plus-rt3",
            "Haylou Solar Plus RT3",
            True,
            "competitor",
        ),
        (
            "https://mibro.com/watch-a2",
            "Mibro Watch A2",
            True,
            "competitor",
        ),
        # market / category
        (
            "https://www.techradar.com/best-cheap-smartwatches",
            "Best cheap smartwatches 2024",
            True,
            "market",
        ),
        # ambiguous
        (
            "https://www.casio.com/g-shock",
            "Casio G-Shock",
            None,
            "unknown",
        ),
        (
            "https://www.alibaba.com/product-detail/Kids-Smart-Watch-4G",
            "Kids Smart Watch 4G GPS",
            None,
            "unknown",
        ),
    ],
)
def test_competitor_relevance_cases(
    agent: CompetitorAnalysisAgent,
    url: str,
    title: str,
    expected_relevant: bool | None,
    expected_type: str,
) -> None:
    annotation = {"url": url, "title": title, "content": ""}
    result = agent._assess_source_relevance(annotation)
    assert result["relevant"] is expected_relevant
    assert result.get("relevance_type") == expected_type


@pytest.mark.parametrize(
    "source_text,should_be_target",
    [
        ("K77", True),
        ("CACGO K77 Smart Watch", True),
        ("CACGO-K77-smartwatch", True),
        ("K77-smart-watch", True),
        ("K771", False),
        ("E-3LUE K771 Mechanical Keyboard", False),
        ("K77A", False),
    ],
)
def test_target_model_uses_word_boundary(
    agent: CompetitorAnalysisAgent,
    source_text: str,
    should_be_target: bool,
) -> None:
    """target_model K77 ต้อง match เป็น token ไม่ใช่ substring."""
    annotation = {"url": f"https://example.com/{source_text.replace(' ', '-')}", "title": source_text, "content": ""}
    result = agent._assess_source_relevance(annotation)
    if should_be_target:
        assert result["relevant"] is True
        assert result["relevance_type"] == "target"
    else:
        assert result["relevance_type"] != "target"
