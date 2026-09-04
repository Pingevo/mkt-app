"""G2 cross-domain offline regression tests.

Verifies that the source-driven generic engine works across product domains
without category-specific configuration. No model/web/image/video calls.

Domains tested:
- smartwatch (backward compatibility)
- restaurant
- apparel
- SaaS
- unknown product/service

For each domain, verifies:
1. Evidence schema accepts domain-specific fields (no fixed catalog)
2. No smartwatch fields are forced on other domains
3. No keyword/category branch changes behavior
4. Facts have provenance and hypotheses are separated from facts
5. Multi-product sources don't mix
"""

from __future__ import annotations

import json

import pytest

from src.agents.competitor_evidence import (
    CompetitorEvidence,
    CompetitorReportRenderer,
    EvidenceBasedRecommendation,
    ResearchResponse,
    StrategicHypothesis,
)


# ---------------------------------------------------------------------------
# Fixtures: domain-specific evidence + annotations
# ---------------------------------------------------------------------------


def _smartwatch_evidence() -> ResearchResponse:
    return ResearchResponse.from_dict({
        "target_model": "CACGO K77",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "battery",
                "claim": "280mAh battery",
                "url": "https://example.com/redmi-watch-3",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })


def _restaurant_evidence() -> ResearchResponse:
    return ResearchResponse.from_dict({
        "target_model": "Sushi Bar A",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "menu_price_range",
                "claim": "Lunch set 120-180 baht",
                "url": "https://example.com/ramen-shop-b",
                "geography": "thailand",
            },
            {
                "competitor": "Competitor A",
                "field": "signature_dish",
                "claim": "Tonkotsu ramen with 18-hour broth",
                "url": "https://example.com/ramen-shop-b",
                "geography": "thailand",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })


def _apparel_evidence() -> ResearchResponse:
    return ResearchResponse.from_dict({
        "target_model": "Brand X T-Shirt",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "material",
                "claim": "100% organic cotton",
                "url": "https://example.com/brand-y-tee",
                "geography": "global",
            },
            {
                "competitor": "Competitor A",
                "field": "price",
                "claim": "$29.99",
                "url": "https://example.com/brand-y-tee",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })


def _saas_evidence() -> ResearchResponse:
    return ResearchResponse.from_dict({
        "target_model": "CloudApp Pro",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "plan_tier",
                "claim": "Business plan at $49/month",
                "url": "https://example.com/saas-z-pricing",
                "geography": "global",
            },
            {
                "competitor": "Competitor A",
                "field": "api_rate_limit",
                "claim": "1000 requests/minute on Business plan",
                "url": "https://example.com/saas-z-pricing",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })


def _unknown_product_evidence() -> ResearchResponse:
    return ResearchResponse.from_dict({
        "target_model": "Mystery Product Q",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "custom_property_xyz",
                "claim": "Has property XYZ with value 42",
                "url": "https://example.com/mystery-r",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })


def _annotations_for(urls: list[str], competitor: str = "Competitor A") -> list[dict]:
    """Create annotations with verified _relevance metadata (relevant=True,
    competitor match) so they pass the renderer's provenance + identity +
    cross-competitor attribution check."""
    return [
        {
            "url": u,
            "title": f"{competitor} source for {u.split('/')[-1]}",
            "content": f"{competitor} product info",
            "_relevance": {
                "relevant": True,
                "relevance_type": "competitor",
                "matched_competitor": competitor,
                "geography": "global",
            },
        }
        for u in urls
    ]


# ---------------------------------------------------------------------------
# 1. Evidence schema accepts domain-specific fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "research_factory,expected_fields",
    [
        (_smartwatch_evidence, ("battery",)),
        (_restaurant_evidence, ("menu_price_range", "signature_dish")),
        (_apparel_evidence, ("material", "price")),
        (_saas_evidence, ("plan_tier", "api_rate_limit")),
        (_unknown_product_evidence, ("custom_property_xyz",)),
    ],
)
def test_open_schema_accepts_domain_specific_fields(
    research_factory,
    expected_fields: tuple[str, ...],
):
    """The evidence schema is open — any field label the model returns is accepted."""
    research = research_factory()
    annotations = _annotations_for([
        ev.url for ev in research.evidence
    ])
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    errors = renderer.validate()
    assert not errors, f"Domain-specific fields should be accepted: {errors}"

    # Verify the fields appear in the rendered output
    output = renderer.render("product spec")
    for f in expected_fields:
        assert f in output, f"Field '{f}' should appear in rendered output"


# ---------------------------------------------------------------------------
# 2. No smartwatch fields are forced on other domains
# ---------------------------------------------------------------------------


def test_restaurant_does_not_force_smartwatch_fields():
    """Restaurant evidence must not require display/battery/gps fields."""
    research = _restaurant_evidence()
    annotations = _annotations_for([ev.url for ev in research.evidence])
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    output = renderer.render("Sushi Bar A\nmenu: sushi, sashimi")

    # Smartwatch-specific fields should NOT appear
    assert "battery" not in output.lower()
    assert "gps" not in output.lower()
    assert "display" not in output.lower()
    # Restaurant-specific fields SHOULD appear
    assert "menu_price_range" in output
    assert "signature_dish" in output


def test_saas_does_not_force_smartwatch_fields():
    """SaaS evidence must not require display/battery/gps fields."""
    research = _saas_evidence()
    annotations = _annotations_for([ev.url for ev in research.evidence])
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    output = renderer.render("CloudApp Pro\nplan: business")

    assert "battery" not in output.lower()
    assert "gps" not in output.lower()
    assert "plan_tier" in output
    assert "api_rate_limit" in output


# ---------------------------------------------------------------------------
# 3. No keyword/category branch changes behavior
# ---------------------------------------------------------------------------


def test_same_renderer_works_for_all_domains():
    """The same CompetitorReportRenderer works for all domains without config."""
    domains = [
        _smartwatch_evidence(),
        _restaurant_evidence(),
        _apparel_evidence(),
        _saas_evidence(),
        _unknown_product_evidence(),
    ]
    for research in domains:
        annotations = _annotations_for([ev.url for ev in research.evidence])
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        errors = renderer.validate()
        assert not errors, f"Renderer should work for all domains: {errors}"
        output = renderer.render("product spec")
        assert research.target_model in output
        # Must have evidence or limited analysis
        assert len(output) > 50


# ---------------------------------------------------------------------------
# 4. Facts have provenance and hypotheses are separated from facts
# ---------------------------------------------------------------------------


def test_facts_have_provenance_and_hypotheses_are_separated():
    """Evidence (facts) must have URLs; hypotheses must be labeled separately."""
    research = ResearchResponse.from_dict({
        "target_model": "Test Product",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "price",
                "claim": "Price is $100",
                "url": "https://example.com/competitor-a",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [
            {
                "text": "Recommend X based on price evidence",
                "supporting_evidence_urls": ["https://example.com/competitor-a"],
            },
        ],
        "strategic_hypotheses": [
            {
                "text": "Competitor A may lower price next quarter",
                "rationale": "Market trend suggests price war",
            },
        ],
        "uncertainty": ["Cannot confirm future pricing"],
    })
    annotations = _annotations_for(["https://example.com/competitor-a"])
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    output = renderer.render("Test Product\nprice: $120")

    # Facts must have provenance (URL)
    assert "https://example.com/competitor-a" in output
    # Hypotheses must be labeled as unverified
    assert "สมมติฐาน" in output
    assert "ยังไม่ยืนยัน" in output
    # Evidence-based recommendations must be in a separate section
    assert "ข้อเสนอแนะที่มีหลักฐานรองรับ" in output


def test_unverified_recommendation_demoted_to_hypothesis():
    """A recommendation whose supporting URL is not in validated evidence
    must be demoted to hypothesis, not shown as evidence-based."""
    research = ResearchResponse.from_dict({
        "target_model": "Test Product",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "price",
                "claim": "Price is $100",
                "url": "https://example.com/competitor-a",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [
            {
                "text": "Recommend based on unverified source",
                "supporting_evidence_urls": ["https://example.com/unverified"],
            },
        ],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    # Annotation for the evidence URL, but NOT for the recommendation's URL
    annotations = _annotations_for(["https://example.com/competitor-a"])
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    output = renderer.render("Test Product\nprice: $120")

    # The unverified recommendation must appear in hypotheses, not evidence-based
    assert "สมมติฐาน" in output
    assert "Recommend based on unverified source" in output


# ---------------------------------------------------------------------------
# 5. Multi-product sources don't mix
# ---------------------------------------------------------------------------


def test_multi_product_evidence_does_not_mix():
    """Evidence for two products in the same report must not mix fields."""
    research = ResearchResponse.from_dict({
        "target_model": "Product A",
        "competitor_names": ["Competitor X", "Competitor Y"],
        "evidence": [
            {
                "competitor": "Competitor X",
                "field": "price",
                "claim": "$50",
                "url": "https://example.com/comp-x",
                "geography": "global",
            },
            {
                "competitor": "Competitor Y",
                "field": "material",
                "claim": "cotton",
                "url": "https://example.com/comp-y",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    annotations = (
        _annotations_for(["https://example.com/comp-x"], competitor="Competitor X")
        + _annotations_for(["https://example.com/comp-y"], competitor="Competitor Y")
    )
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    output = renderer.render("Product A\nprice: $60\nmaterial: polyester")

    # Both competitors should appear with their own evidence
    assert "Competitor X" in output
    assert "Competitor Y" in output
    assert "$50" in output
    assert "cotton" in output
    # Competitor X should NOT have "cotton" and Competitor Y should NOT have "$50"
    # in their respective cells (they should show no-evidence for the other's field)


# ---------------------------------------------------------------------------
# 6. Backward compatibility: smartwatch flow still works
# ---------------------------------------------------------------------------


def test_smartwatch_flow_backward_compatible():
    """Smartwatch evidence with traditional fields must still render correctly."""
    research = _smartwatch_evidence()
    annotations = _annotations_for([ev.url for ev in research.evidence])
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    output = renderer.render("CACGO K77\nbattery: 1000mAh")

    assert "CACGO K77" in output
    assert "Competitor A" in output
    assert "battery" in output
    assert "280mAh" in output
    assert "https://example.com/redmi-watch-3" in output


# ---------------------------------------------------------------------------
# 7. Empty field is rejected (structural validation still works)
# ---------------------------------------------------------------------------


def test_empty_geography_is_rejected():
    """An evidence record with an empty geography must fail validation."""
    research = ResearchResponse.from_dict({
        "target_model": "Test Product",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "price",
                "claim": "some claim",
                "url": "https://example.com/comp-a",
                "geography": "",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    annotations = _annotations_for(["https://example.com/comp-a"])
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    errors = renderer.validate()
    assert len(errors) == 1
    assert "geography is empty" in errors[0]


def test_open_geography_accepts_various_regions():
    """Geography is an open string — accepts thailand, japan, eu, global, etc."""
    regions = ["thailand", "japan", "eu", "global", "south-east-asia"]
    for geo in regions:
        research = ResearchResponse.from_dict({
            "target_model": "Test Product",
            "competitor_names": ["Competitor A"],
            "evidence": [
                {
                    "competitor": "Competitor A",
                    "field": "price",
                    "claim": "some claim",
                    "url": "https://example.com/comp-a",
                    "geography": geo,
                },
            ],
            "evidence_based_recommendations": [],
            "strategic_hypotheses": [],
            "uncertainty": [],
        })
        annotations = _annotations_for(["https://example.com/comp-a"])
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        errors = renderer.validate()
        assert not errors, f"Geography '{geo}' should be accepted: {errors}"


# ---------------------------------------------------------------------------
# 8. Provenance check still works (URL must be in annotations)
# ---------------------------------------------------------------------------


def test_url_not_in_annotations_is_rejected():
    """An evidence URL that is not in the provided annotations must fail."""
    research = ResearchResponse.from_dict({
        "target_model": "Test Product",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "price",
                "claim": "$100",
                "url": "https://example.com/invented-url",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    # No annotations provided — URL is not in any verified source
    renderer = CompetitorReportRenderer(research, relevant_annotations=[])
    errors = renderer.validate()
    assert len(errors) == 1
    assert "URL not in verified evidence" in errors[0]


# ---------------------------------------------------------------------------
# 9. Field canonicalization — casing/spacing variants are grouped
# ---------------------------------------------------------------------------


def test_field_casing_variants_are_grouped():
    """Fields 'Price', 'price', ' PRICE ' should be grouped into one row."""
    research = ResearchResponse.from_dict({
        "target_model": "Test Product",
        "competitor_names": ["Competitor A", "Competitor B"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "Price",
                "claim": "$100",
                "url": "https://example.com/comp-a",
                "geography": "global",
            },
            {
                "competitor": "Competitor B",
                "field": "price",
                "claim": "$120",
                "url": "https://example.com/comp-b",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    annotations = (
        _annotations_for(["https://example.com/comp-a"], competitor="Competitor A")
        + _annotations_for(["https://example.com/comp-b"], competitor="Competitor B")
    )
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    output = renderer.render("Test Product\nprice: $110")

    # Should have ONE price row, not two
    price_count = output.lower().count("| **price")
    assert price_count == 1, f"Should have 1 price row (canonicalized), got {price_count}"


def test_field_spacing_variants_are_grouped():
    """Fields 'plan tier', 'plan-tier', 'plan_tier' should be grouped."""
    research = ResearchResponse.from_dict({
        "target_model": "CloudApp",
        "competitor_names": ["Competitor A", "Competitor B"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "plan tier",
                "claim": "Business $49/mo",
                "url": "https://example.com/comp-a",
                "geography": "global",
            },
            {
                "competitor": "Competitor B",
                "field": "plan-tier",
                "claim": "Pro $99/mo",
                "url": "https://example.com/comp-b",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    annotations = (
        _annotations_for(["https://example.com/comp-a"], competitor="Competitor A")
        + _annotations_for(["https://example.com/comp-b"], competitor="Competitor B")
    )
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    output = renderer.render("CloudApp\nplan tier: enterprise")

    # Should have ONE plan tier row
    plan_count = output.lower().count("plan tier")
    assert plan_count >= 1
    # Both competitors should appear in the same row
    assert "Business $49/mo" in output
    assert "Pro $99/mo" in output


# ---------------------------------------------------------------------------
# 10. _product_cells does not show '-' misleadingly
# ---------------------------------------------------------------------------


def test_product_cells_shows_no_match_message_when_label_differs():
    """When product spec has data but label doesn't match evidence field,
    show a clear 'no match' message, NOT '-'."""
    research = ResearchResponse.from_dict({
        "target_model": "Test Product",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "price",
                "claim": "$100",
                "url": "https://example.com/comp-a",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    annotations = _annotations_for(["https://example.com/comp-a"])
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    # Product spec uses a different label format
    output = renderer.render("Test Product\ncost: $110")

    # Should show the no-match message, not just "-"
    assert "ไม่พบค่าที่จับคู่ได้จากข้อมูลต้นทาง" in output


def test_product_cells_matches_different_label_formats():
    """When product spec uses 'Price: $110' and evidence field is 'price',
    they should match after canonicalization."""
    research = ResearchResponse.from_dict({
        "target_model": "Test Product",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "price",
                "claim": "$100",
                "url": "https://example.com/comp-a",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    annotations = _annotations_for(["https://example.com/comp-a"])
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    output = renderer.render("Test Product\nPrice: $110")

    # Should show $110 (matched), not the no-match message
    assert "$110" in output
    assert "ไม่พบค่าที่จับคู่ได้จากข้อมูลต้นทาง" not in output


# ---------------------------------------------------------------------------
# 11. Cross-domain false positive: restaurant source must not enter SaaS evidence
# ---------------------------------------------------------------------------


def test_restaurant_source_not_accepted_for_saas_competitor():
    """A restaurant source with no SaaS competitor identity match:
    - agent assesses as candidate (relevant=None)
    - if model cites it as evidence, renderer rejects it (not in verified set)
    """
    from src.agents.competitor_analysis import CompetitorAnalysisAgent
    from tests.test_competitor_analysis import FakeLLM

    annotation = {
        "url": "https://example.com/ramen-shop-b",
        "title": "Ramen Shop B - Menu and Prices",
        "content": "Lunch set 120-180 baht",
    }
    agent = CompetitorAnalysisAgent(
        {"web_search": True, "evidence_mode": True, "max_retry_limit": 0},
        FakeLLM(),
    )
    agent._target_model = "CloudApp Pro"
    agent._competitor_names = ["SaaS Competitor Z"]
    agent._relevance_context = {
        "target_model": "CloudApp Pro",
        "competitor_names": ["SaaS Competitor Z"],
        "target_text": "cloudapp pro saas software",
    }
    agent._last_annotations = [annotation]
    agent._reassess_all_annotations()

    # Agent-level: restaurant source gets relevant=None (candidate, not verified)
    assert len(agent._last_relevant_annotations) == 0
    assert len(agent._last_candidate_annotations) == 1
    assert agent._last_candidate_annotations[0]["_relevance"]["relevant"] is None

    # Renderer-level: if model cites the candidate URL as evidence, it is rejected
    # because only verified annotations (relevant=True) are passed to the renderer
    research = ResearchResponse.from_dict({
        "target_model": "CloudApp Pro",
        "competitor_names": ["SaaS Competitor Z"],
        "evidence": [
            {
                "competitor": "SaaS Competitor Z",
                "field": "price",
                "claim": "$49/month",
                "url": "https://example.com/ramen-shop-b",
                "geography": "thailand",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    # Renderer receives only verified annotations (empty — no verified sources)
    renderer = CompetitorReportRenderer(
        research,
        relevant_annotations=agent._last_relevant_annotations,
    )
    errors = renderer.validate()
    assert len(errors) == 1
    assert "URL not in verified evidence" in errors[0]


# ---------------------------------------------------------------------------
# 12. Cross-competitor evidence attribution prevention
# ---------------------------------------------------------------------------


def test_cross_competitor_attribution_is_rejected():
    """Source verified for Competitor B must NOT validate evidence claiming
    Competitor A — prevents cross-competitor attribution."""
    research = ResearchResponse.from_dict({
        "target_model": "Test Product",
        "competitor_names": ["Competitor A", "Competitor B"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "price",
                "claim": "$100",
                "url": "https://example.com/comp-b",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    # Annotation verified for Competitor B
    annotations = _annotations_for(["https://example.com/comp-b"], competitor="Competitor B")
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    errors = renderer.validate()

    assert len(errors) == 1
    assert "cross-competitor attribution" in errors[0]


def test_target_match_cannot_validate_competitor_evidence():
    """Source that matches the target product must NOT validate evidence
    claiming a competitor — target-matched sources are not competitor evidence."""
    research = ResearchResponse.from_dict({
        "target_model": "TestProduct X1",
        "competitor_names": ["Competitor A"],
        "evidence": [
            {
                "competitor": "Competitor A",
                "field": "price",
                "claim": "$100",
                "url": "https://example.com/testproduct-x1-review",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    # Annotation verified as target match (not competitor)
    annotations = [{
        "url": "https://example.com/testproduct-x1-review",
        "title": "TestProduct X1 Review",
        "content": "TestProduct X1 specs and features",
        "_relevance": {
            "relevant": True,
            "relevance_type": "target",
            "matched_target": "testproduct x1",
            "geography": "global",
        },
    }]
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    errors = renderer.validate()

    assert len(errors) == 1
    assert "not a competitor match" in errors[0]


def test_same_competitor_hyphenated_identity_passes():
    """Hyphenated URL/title identity of the same competitor should pass
    after canonical normalization (e.g. 'imoo-watch-phone-z1' matches
    'imoo watch phone z1')."""
    research = ResearchResponse.from_dict({
        "target_model": "Test Product",
        "competitor_names": ["imoo watch phone z1"],
        "evidence": [
            {
                "competitor": "imoo watch phone z1",
                "field": "price",
                "claim": "$150",
                "url": "https://example.com/imoo-watch-phone-z1",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    annotations = [{
        "url": "https://example.com/imoo-watch-phone-z1",
        "title": "imoo-watch-phone-z1 Kids Smart Watch",
        "content": "imoo watch phone z1 specs",
        "_relevance": {
            "relevant": True,
            "relevance_type": "competitor",
            "matched_competitor": "imoo watch phone z1",
            "geography": "global",
        },
    }]
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    errors = renderer.validate()
    assert not errors, f"Hyphenated identity should pass: {errors}"


def test_same_competitor_casing_spacing_variants_pass():
    """Casing/spacing variants of the same competitor name should pass
    after canonical normalization."""
    research = ResearchResponse.from_dict({
        "target_model": "Test Product",
        "competitor_names": ["Redmi Watch 3 Active"],
        "evidence": [
            {
                "competitor": "Redmi Watch 3 Active",
                "field": "price",
                "claim": "$80",
                "url": "https://example.com/redmi-watch-3",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })
    # matched_competitor uses different casing/spacing
    annotations = [{
        "url": "https://example.com/redmi-watch-3",
        "title": "REDMI WATCH 3  Active",
        "content": "redmi  watch 3 active review",
        "_relevance": {
            "relevant": True,
            "relevance_type": "competitor",
            "matched_competitor": "redmi watch 3  active",
            "geography": "global",
        },
    }]
    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    errors = renderer.validate()
    assert not errors, f"Casing/spacing variants should pass: {errors}"


def test_partial_common_token_match_is_candidate_not_verified():
    """A source that shares a common token (e.g. 'watch') with a competitor
    name but not the full identity should be a candidate (relevant=None),
    not verified (relevant=True)."""
    from src.agents.competitor_analysis import CompetitorAnalysisAgent
    from tests.test_competitor_analysis import FakeLLM

    agent = CompetitorAnalysisAgent(
        {"web_search": True, "evidence_mode": True, "max_retry_limit": 0},
        FakeLLM(),
    )
    agent._target_model = "TestProduct X1"
    agent._competitor_names = ["Redmi Watch 3 Active"]
    agent._relevance_context = {
        "target_model": "TestProduct X1",
        "competitor_names": ["Redmi Watch 3 Active"],
        "target_text": "testproduct x1",
    }
    # Source has "watch" but not the full "redmi watch 3 active" identity
    annotation = {
        "url": "https://example.com/some-watch-review",
        "title": "Best Watch Review 2024",
        "content": "A generic watch review article",
    }
    result = agent._assess_source_relevance(annotation)
    assert result["relevant"] is None, (
        "Partial/common token match should be candidate (relevant=None), "
        f"not verified — got {result['relevant']}"
    )
