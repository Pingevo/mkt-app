"""TDD tests for citation inline-first + fallback policy.

Offline — no OpenRouter calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.competitor_analysis import CompetitorAnalysisAgent
from src.agents.base_agent import BaseAgent


def _competitor_agent() -> CompetitorAnalysisAgent:
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
            "market_keywords": ["smart watch", "smartwatch", "smartband"],
        },
        "citation_policy": {
            "mode": "inline_first",
            "fallback_annotations_when_no_inline": True,
        },
    }
    return CompetitorAnalysisAgent(agent_config=config, llm_client=None)


def _other_agent() -> BaseAgent:
    """Fake agent ทีไม่มี _assess_source_relevance และไม่ใช่ inline_first."""

    class FakeAgent(BaseAgent):
        agent_name = "fake_agent"
        display_name = "Fake Agent"

        def build_prompt(self, *args, **kwargs) -> str:
            return ""

    return FakeAgent(agent_config={"citation_policy": {}}, llm_client=MagicMock())


def _build_context(agent: CompetitorAnalysisAgent) -> None:
    product_spec = (
        "--- ขอบเขตสินค้า ---\n"
        "รหัสสินค้า: K77\n"
        "--- สิ้นสุดขอบเขตสินค้า ---\n\n"
        "--- ข้อมูลดิบ (text) ---\n"
        "CACGO K77 smart watch\n"
        "--- สิ้นสุดข้อมูลดิบ ---\n"
    )
    competitor_data = "Redmi Watch 3 Active\nHaylou Solar Plus RT3\nMibro Watch A2"
    agent.build_prompt(product_spec, competitor_data)


def test_inline_citation_prevents_dump() -> None:
    """ถ้า output มี inline citation อยู่แล้ว → ไม่ต้อง append dump."""
    agent = _competitor_agent()
    _build_context(agent)

    url = "https://shopee.co.th/CACGO-K77"
    output = f"สินค้า [CACGO K77]({url}) เป็น smartwatch"
    annotations = [{"url": url, "title": "CACGO K77 Smart Watch"}]

    result = agent._append_citations_and_verify(output, annotations)

    assert result == output


def test_multiple_inline_citations_prevent_dump() -> None:
    """หลาย inline citations → ก็ไม่มี dump."""
    agent = _competitor_agent()
    _build_context(agent)

    url1 = "https://shopee.co.th/CACGO-K77"
    url2 = "https://www.mi.com/redmi-watch-3-active"
    output = f"[K77]({url1}) vs [Redmi]({url2})"
    annotations = [
        {"url": url1, "title": "CACGO K77"},
        {"url": url2, "title": "Redmi Watch 3 Active"},
    ]

    result = agent._append_citations_and_verify(output, annotations)

    assert result == output


def test_no_inline_citation_with_relevant_annotations_adds_fallback() -> None:
    """ไม่มี inline → fallback แสดง relevant annotations."""
    agent = _competitor_agent()
    _build_context(agent)

    url = "https://shopee.co.th/CACGO-K77"
    annotations = [{"url": url, "title": "CACGO K77 Smart Watch"}]
    output = "สินค้าเป็น smartwatch"

    result = agent._append_citations_and_verify(output, annotations)

    assert url in result
    assert output in result
    assert result.startswith(output)


def test_no_inline_with_irrelevant_annotations_no_fallback() -> None:
    """ไม่มี inline + source ทีไม่ relevant → ไม่แสดงอะไรทั้งนั้น."""
    agent = _competitor_agent()
    _build_context(agent)

    url = "https://amazon.com/redragon-keyboard"
    annotations = [{"url": url, "title": "Redragon Mechanical Keyboard"}]
    output = "สินค้าเป็น smartwatch"

    result = agent._append_citations_and_verify(output, annotations)

    assert result == output


def test_mixed_relevant_and_irrelevant_fallback_only_relevant() -> None:
    """ผสม relevant/irrelevant แต่ไม่มี inline → fallback แค่ relevant."""
    agent = _competitor_agent()
    _build_context(agent)

    relevant_url = "https://shopee.co.th/CACGO-K77"
    irrelevant_url = "https://amazon.com/redragon-keyboard"
    annotations = [
        {"url": relevant_url, "title": "CACGO K77 Smart Watch"},
        {"url": irrelevant_url, "title": "Redragon Mechanical Keyboard"},
    ]
    output = "สินค้าเป็น smartwatch"

    result = agent._append_citations_and_verify(output, annotations)

    assert relevant_url in result
    assert irrelevant_url not in result


def test_other_agent_keeps_old_dump_behavior() -> None:
    """agent อื่นทีไม่ใช่ inline_first ยังแปะ citation section เดิม."""
    agent = _other_agent()

    url = "https://example.com/any"
    annotations = [{"url": url, "title": "Any Source"}]
    output = "some claim"

    result = agent._append_citations_and_verify(output, annotations)

    assert url in result
    assert "แหล่งอ้างอิงจริงจากการค้นหา" in result


def test_title_mention_without_url_does_not_prevent_fallback() -> None:
    """ชื่อ source ปรากฏใน output แต่ไม่มี URL → ยังถือวาไม่มี inline citation."""
    agent = _competitor_agent()
    _build_context(agent)

    url = "https://shopee.co.th/CACGO-K77"
    title = "CACGO K77 Smart Watch"
    annotations = [{"url": url, "title": title}]
    output = f"สินค้าชื่อ {title} เป็น smartwatch"

    result = agent._append_citations_and_verify(output, annotations)

    assert url in result
    assert output in result
