"""AGENT2-SOURCE-SCOPE-CLOSEOUT-01 — our-product cells must draw only from
product-owned sources, never from unscoped page text that embeds other
products' listings.

Real browser failure: grounding rejected an our-product cell
``หน้าจอ: วอนเล็กซ์ KT31 ... AMOLED HD ... แบตเตอรี่ 900mAh ... Whatsapp``
— text lifted from a related-product card inside the scraped marketplace
page, not from the selected product.

Contract (authority order):
- confirmed structured Product facts/profile — highest
- selected-product structured/scoped source facts
- selected-product-scoped raw evidence (proven segments only)
- explicit no-evidence marker — preferred over any borrowed value
"""
from __future__ import annotations

import json

import pytest

from src.agents.competitor_analysis import CompetitorAnalysisAgent
from src.agents.competitor_evidence import (
    CompetitorReportRenderer,
    ResearchResponse,
)


class FakeLLM:
    def __init__(self):
        self.last_truncated = False

    def chat(self, *args, **kwargs):
        return ""

    def close(self):
        pass


def _make_agent() -> CompetitorAnalysisAgent:
    cfg = {"web_search": True, "evidence_mode": True, "max_retry_limit": 0}
    return CompetitorAnalysisAgent(cfg, FakeLLM())


# A composed spec blob mixing trusted envelopes with an unscoped raw page
# that embeds a DIFFERENT product's recommendation card — the shape that
# produced the KT31 contamination.
RELATED_CARD = (
    "วอนเล็กซ์ KT31 สมาร์ทวอทช์สำหรับเด็ก 4G SOS WIFI GPS ติดตามตัว "
    "หน้าจอ AMOLED HD วิดีโอคอล แบตเตอรี่ 900mAh รองรับ Whatsapp"
)
SPEC_BLOB = (
    "--- ข้อมูลสินค้า (Product Information) ---\n"
    "  ราคา: ฿3,273.32\n"
    "--- สิ้นสุดข้อมูลสินค้า ---\n"
    "--- ข้อมูลดิบ (text) ---\n"
    "สินค้าที่เกี่ยวข้อง\n"
    f"{RELATED_CARD}\n"
    "--- สิ้นสุดข้อมูลดิบ ---\n"
)
OUR_PRODUCT_FACTS = "  ราคา: ฿3,273.32\n  ระบบระบุตำแหน่ง: GPS, WiFi, LBS\n"


def _research_and_annotations(fields):
    """Research with one verified competitor evidence per field, plus the
    verified annotations that make those URLs admissible."""
    evidence = []
    annotations = []
    for i, field in enumerate(fields):
        url = f"https://example.com/{i}"
        evidence.append({
            "competitor": "Comp A",
            "field": field,
            "claim": f"Comp A claim for {field}",
            "url": url,
            "geography": "thailand",
        })
        annotations.append({
            "url": url,
            "title": "Comp A source",
            "content": f"Comp A claim for {field}",
            "_relevance": {
                "relevant": True,
                "relevance_type": "competitor",
                "geography": "thailand",
                "matched_competitor": "Comp A",
            },
        })
    research = ResearchResponse.from_dict({
        "target_model": "Comp B",
        "competitor_names": ["Comp A"],
        "evidence": evidence,
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    return research, annotations


def _cell(output: str, field: str) -> str:
    row = next(l for l in output.splitlines() if f"**{field}**" in l)
    return row.split("|")[2].strip()


def test_confirmed_fact_wins_over_related_product_text():
    """Confirmed product facts are the source of truth — a related-product
    card carrying the same attribute must not override or contaminate it."""
    research, annotations = _research_and_annotations(["ราคา"])
    renderer = CompetitorReportRenderer(
        research, relevant_annotations=annotations,
        our_product="Lagenio Evo", our_product_spec=OUR_PRODUCT_FACTS)
    out = renderer.render(SPEC_BLOB)
    assert _cell(out, "ราคา") == "฿3,273.32"


def test_related_product_attribute_never_fills_our_product_cell():
    """An attribute the selected product lacks but a related card carries
    must render the no-evidence marker — never the borrowed card text."""
    research, annotations = _research_and_annotations(["หน้าจอ"])
    renderer = CompetitorReportRenderer(
        research, relevant_annotations=annotations,
        our_product="Lagenio Evo", our_product_spec=OUR_PRODUCT_FACTS)
    out = renderer.render(SPEC_BLOB)
    cell = _cell(out, "หน้าจอ")
    assert "KT31" not in cell
    assert "AMOLED" not in cell
    assert "ไม่พบค่าที่จับคู่ได้" in cell


def test_scoped_product_source_fills_cell():
    """Product-owned source text (facts/confirmed/scoped segment) may fill a
    cell even when it is absent from the raw page blob."""
    research, annotations = _research_and_annotations(["ระบบระบุตำแหน่ง"])
    renderer = CompetitorReportRenderer(
        research, relevant_annotations=annotations,
        our_product="Lagenio Evo", our_product_spec=OUR_PRODUCT_FACTS)
    out = renderer.render(SPEC_BLOB)
    assert _cell(out, "ระบบระบุตำแหน่ง") == "GPS, WiFi, LBS"


def test_empty_product_source_fails_closed_not_to_blob():
    """Runtime identity set but no product-owned facts → explicit no-evidence
    even though the blob contains a matching related-product line."""
    research, annotations = _research_and_annotations(["หน้าจอ"])
    renderer = CompetitorReportRenderer(
        research, relevant_annotations=annotations,
        our_product="Lagenio Evo", our_product_spec="")
    out = renderer.render(SPEC_BLOB)
    cell = _cell(out, "หน้าจอ")
    assert "KT31" not in cell
    assert "ไม่พบค่าที่จับคู่ได้" in cell


def test_bare_prose_line_is_not_a_fact_source():
    """A bare line (no label:value / table shape) has no extractable value —
    it must not echo itself into a cell. This was the direct mechanism that
    copied the whole KT31 card into the our-product column."""
    research, annotations = _research_and_annotations(["หน้าจอ"])
    renderer = CompetitorReportRenderer(
        research, relevant_annotations=annotations,
        our_product_spec="จอแสดงผลหน้าจอ คือส่วนที่สำคัญมาก")
    out = renderer.render("anything")
    assert "ไม่พบค่าที่จับคู่ได้" in _cell(out, "หน้าจอ")


def test_standalone_without_runtime_identity_scans_given_spec():
    """No runtime identity → the caller-provided spec is caller-asserted
    product data; legacy scan behavior is preserved."""
    research, annotations = _research_and_annotations(["ราคา"])
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    out = renderer.render("ราคา: ฿999")
    assert _cell(out, "ราคา") == "฿999"


def test_agent_loads_product_owned_facts_via_product_db(monkeypatch):
    """With a runtime product identity, the agent resolves the product-owned
    fact source from product_db — scoped raw only when the record proves a
    segment, never the unscoped page blob."""
    import src.product_db as product_db

    monkeypatch.setattr(product_db, "is_ready", lambda pid: True)
    monkeypatch.setattr(
        product_db, "get_agent_facts_text", lambda pid: OUR_PRODUCT_FACTS)

    agent = _make_agent()
    agent.build_prompt(SPEC_BLOB, "", product_name="Lagenio Evo")
    assert agent._our_product_facts == OUR_PRODUCT_FACTS

    # No runtime identity → no fact source lookup, standalone preserved.
    agent2 = _make_agent()
    agent2.build_prompt(SPEC_BLOB, "")
    assert agent2._our_product_facts == ""
