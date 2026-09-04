"""TDD tests for CompetitorAnalysisAgent source relevance hook.

Offline — no OpenRouter calls.

G2 contract: relevance is based on product/competitor identity match and
structural URL checks — NOT on category keyword lists. Category mismatch
and market keyword branches were removed in G2.
"""

from __future__ import annotations

import pytest

from src.agents.competitor_analysis import CompetitorAnalysisAgent


def _make_agent() -> CompetitorAnalysisAgent:
    return CompetitorAnalysisAgent(agent_config={}, llm_client=None)


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
        # ambiguous — no target/competitor match → unknown (model decides)
        (
            "https://www.casio.com/g-shock",
            "Casio G-Shock",
            None,
            "unknown",
        ),
        # G2: category mismatch is no longer a hard block — sources without
        # target/competitor identity match fall through to "unknown" and the
        # model decides whether to cite them.
        (
            "https://www.amazon.com/Redragon-Wireless-Mechanical-Keyboard/dp/B0GZYT25QJ",
            "Redragon Wireless Mechanical Gaming Keyboard",
            None,
            "unknown",
        ),
        # G2: market article without product-specific identity → unknown
        (
            "https://www.techradar.com/best-cheap-smartwatches",
            "Best cheap smartwatches 2024",
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


def test_homepage_without_product_is_not_relevant(agent: CompetitorAnalysisAgent) -> None:
    """homepage / marketplace root ทีไม่ระบุรุ่นเฉพาะต้องไม่ผ่าน."""
    annotation = {"url": "https://shopee.co.th", "title": "Shopee Thailand", "content": "ซื้อขายออนไลน์"}
    result = agent._assess_source_relevance(annotation)
    assert result["relevant"] is False
    assert result["relevance_type"] == "homepage"


def test_brand_language_root_without_product_is_not_relevant(agent: CompetitorAnalysisAgent) -> None:
    """mi.com/th ที title ไม่มีรุ่นเฉพาะต้องถูกปฏิเสธ."""
    annotation = {"url": "https://www.mi.com/th", "title": "Xiaomi Thailand", "content": "สมาร์ทโฟนและสมาร์ทวอทช์"}
    result = agent._assess_source_relevance(annotation)
    assert result["relevant"] is False
    assert result["relevance_type"] == "homepage"


def test_product_page_without_slug_but_title_confirms_is_relevant(agent: CompetitorAnalysisAgent) -> None:
    """path ใช้ SKU/ID แต่ title ยืนยันรุ่น ต้องผ่าน."""
    annotation = {
        "url": "https://example.com/p/5227839",
        "title": "CACGO K77 Smart Watch 1.7\" display",
        "content": "รีวิว CACGO K77 smartwatch",
    }
    result = agent._assess_source_relevance(annotation)
    assert result["relevant"] is True
    assert result["relevance_type"] == "target"


def test_homepage_with_product_title_is_relevant(agent: CompetitorAnalysisAgent) -> None:
    """homepage ของ mi.com แต่ title ระบุรุ่นชัดเจน ต้องผ่าน."""
    annotation = {
        "url": "https://www.mi.com/th",
        "title": "Redmi Watch 3 Active — Xiaomi Thailand",
        "content": "",
    }
    result = agent._assess_source_relevance(annotation)
    assert result["relevant"] is True
    assert result["relevance_type"] == "competitor"


@pytest.fixture
def thai_agent() -> CompetitorAnalysisAgent:
    """Agent with the same named-competitor scope as acceptance case 10."""
    a = _make_agent()
    product_spec = (
        "--- ขอบเขตสินค้า ---\n"
        "รหัสสินค้า: K77\n"
        "--- สิ้นสุดขอบเขตสินค้า ---\n\n"
        "--- ข้อมูลดิบ (text) ---\n"
        "CACGO K77 smart watch with Bluetooth calling, 1000mAh battery\n"
        "--- สิ้นสุดข้อมูลดิบ ---\n"
    )
    competitor_data = (
        "Xiaomi Watch S3\n"
        "Kieslect AI Smartwatch Elite2 Lumina Edition\n"
        "Kieslect AI Smartwatch Elite2 Noir Edition\n"
        "Galaxy Watch9"
    )
    a.build_prompt(product_spec, competitor_data)
    return a


@pytest.mark.parametrize(
    "url,title,content,expected_relevant,expected_type,expected_geo",
    [
        (
            "https://www.mi.com/th/product/xiaomi-watch-s3/",
            "Xiaomi Watch S3 | Xiaomi ประเทศไทย | สเปคและฟีเจอรทั้งหมด",
            "",
            True,
            "competitor",
            "thailand",
        ),
        (
            "https://www.kieslectthailand.com/product/72127/%E0%B8%AA%E0%B8%A1%E0%B8%B2%E0%B8%A3%E0%B9%8C%E0%B8%97%E0%B8%A7%E0%B8%AD%E0%B8%97%E0%B8%8A%E0%B9%8C-kieslect-ai-smartwatch-elite2-lumina-edition",
            "สมาร์ทวอทช์ Kieslect AI Smartwatch Elite2 Lumina Edition",
            "",
            True,
            "competitor",
            "thailand",
        ),
        (
            "https://www.samsung.com/th/watches/galaxy-watch/galaxy-watch9-40mm-cream-bluetooth-sm-l340nzeaasa/",
            "Galaxy Watch9 (Bluetooth, 40 mm) Cream | ซัมซุงประเทศไทย",
            "",
            True,
            "competitor",
            "thailand",
        ),
        (
            "https://www.thaisuperphone.com/product/49656/%E0%B8%AA%E0%B8%A1%E0%B8%B2%E0%B8%A3%E0%B9%8C%E0%B8%97%E0%B8%A7%E0%B8%AD%E0%B8%97%E0%B8%8A%E0%B9%8C-kieslect-ai-smartwatch-elite2-noir-edition",
            "สมาร์ทวอทช์ Kieslect AI Smartwatch Elite2 Noir Edition มีGPS",
            "",
            True,
            "competitor",
            "thailand",
        ),
        # brand or marketplace page without the exact model should not be competitor-specific
        (
            "https://shopee.co.th/",
            "Shopee Thailand",
            "ซื้อขายออนไลน์",
            False,
            "homepage",
            "thailand",
        ),
        # G2: generic smartwatch listing without named competitor → unknown
        (
            "https://www.alibaba.co.th/product-detail/AK87-Sport-Smart-Watch-FitcloudPro-BT5_1601856346889.html",
            "นาฬิกาอัจฉริยะ AK87 Sport Smart Watch",
            "",
            None,
            "unknown",
            "thailand",
        ),
    ],
)
def test_thai_competitor_relevance(
    thai_agent: CompetitorAnalysisAgent,
    url: str,
    title: str,
    content: str,
    expected_relevant: bool | None,
    expected_type: str,
    expected_geo: str,
) -> None:
    """Named Thai competitors must be selected; generic marketplace/brand pages must not."""
    annotation = {"url": url, "title": title, "content": content}
    result = thai_agent._assess_source_relevance(annotation)
    assert result["relevant"] is expected_relevant
    assert result.get("relevance_type") == expected_type
    assert result.get("geography") == expected_geo
