"""Tests for the redesigned relevance gate: model-decided evidence with
provenance-only hard gates.

These tests verify the design principles:
1. URL spelling/slug can differ from competitor name — model's choice passes
2. URL that model selected from annotations passes provenance check
3. URL that model invented or outside annotations is blocked
4. No evidence → limited analysis instead of empty report
5. Homepage/category_mismatch sources are still hard-blocked
6. Geography mismatch is still a hard gate

No test is tied to specific competitor names (imoo, hyphen, etc.) —
all use generic placeholder names.
"""
from __future__ import annotations

from src.agents.competitor_evidence import (
    CompetitorEvidence,
    CompetitorReportRenderer,
    EvidenceBasedRecommendation,
    ResearchResponse,
    StrategicHypothesis,
)
from src.agents.competitor_analysis import CompetitorAnalysisAgent
from tests.test_competitor_analysis import FakeLLM


def _make_agent_with_annotations(
    annotations: list[dict],
    competitor_names: list[str] | None = None,
) -> CompetitorAnalysisAgent:
    """Create an evidence-mode agent with pre-populated annotations."""
    cfg = {
        "web_search": True,
        "evidence_mode": True,
        "web_search_mode": "required",
        "max_retry_limit": 0,
    }
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent._competitor_names = competitor_names or []
    agent._target_model = "TestProduct X1"
    agent._relevance_context = {
        "target_model": "TestProduct X1",
        "target_category": "smartwatch",
        "competitor_names": competitor_names or [],
        "target_text": "testproduct x1 smartwatch",
    }
    agent._last_annotations = annotations
    agent._reassess_all_annotations()
    return agent


class TestProvenanceGate:
    """Principle: code checks provenance (URL in annotations), not semantics."""

    def test_url_slug_differs_from_competitor_name_still_passes(self):
        """URL slug can differ from competitor name — model's choice passes
        as long as the URL came from real search results (provenance).

        G2 contract: unmatched sources (no identity match) go to
        _last_candidate_annotations, NOT _last_relevant_annotations.
        Only verified sources (relevant=True) enter _last_relevant_annotations.
        """
        # Competitor name has spaces; URL slug has hyphens and extra words
        annotations = [{
            "url": "https://store.example.com/brand-kid-watch-model-x1",
            "title": "Brand Kids Watch Model X1",
            "content": "Smartwatch for kids with GPS tracking",
        }]
        agent = _make_agent_with_annotations(annotations, competitor_names=["Brand Watch X1"])

        # Unmatched source goes to candidates (not rejected, not verified)
        assert len(agent._last_rejected_annotations) == 0
        assert len(agent._last_candidate_annotations) == 1
        # _last_relevant_annotations only has verified (relevant=True) sources
        assert len(agent._last_relevant_annotations) == 0

    def test_url_model_selected_from_candidate_annotations_is_rejected(self):
        """Renderer REJECTS evidence when URL is only in candidate annotations
        (relevant=None). The model's choice of a candidate URL does NOT
        constitute identity verification — candidate sources cannot be
        validated evidence.
        """
        annotations = [{
            "url": "https://store.example.com/product-page-with-different-slug",
            "title": "Competitor Product Page",
            "content": "Some product info here",
            "_relevance": {"relevant": None, "relevance_type": "unknown", "geography": "global"},
        }]
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[
                CompetitorEvidence(
                    competitor="Competitor Y2",
                    field="display",
                    claim="1.5 inch AMOLED",
                    url="https://store.example.com/product-page-with-different-slug",
                    title="Competitor Product Page",
                    geography="global",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        errors = renderer.validate()

        # P0-2: candidate URL must NOT pass evidence validation
        assert len(errors) == 1
        assert "not verified" in errors[0]

    def test_url_model_selected_from_verified_annotations_passes_renderer(self):
        """Renderer accepts evidence when URL is in verified annotations
        (relevant=True) — explicit identity match confirmed the source."""
        annotations = [{
            "url": "https://store.example.com/competitor-y2-product",
            "title": "Competitor Y2 Product Page",
            "content": "Competitor Y2 product info here",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "global", "matched_competitor": "Competitor Y2"},
        }]
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[
                CompetitorEvidence(
                    competitor="Competitor Y2",
                    field="display",
                    claim="1.5 inch AMOLED",
                    url="https://store.example.com/competitor-y2-product",
                    title="Competitor Y2 Product Page",
                    geography="global",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1\nTestProduct X1")

        # Evidence should be rendered (verified provenance passed)
        assert "1.5 inch AMOLED" in output
        assert "## ตารางเปรียบเทียบ" in output

    def test_invented_url_not_in_annotations_is_blocked(self):
        """URL that model invented (not in annotations) is blocked by
        provenance check — this is a hard gate."""
        annotations = [{
            "url": "https://store.example.com/real-product-page",
            "title": "Real Product",
            "content": "Real info",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "global", "matched_competitor": "Competitor Y2"},
        }]
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[
                CompetitorEvidence(
                    competitor="Competitor Y2",
                    field="display",
                    claim="1.5 inch AMOLED",
                    url="https://evil.example.com/invented-url",
                    title="Fake",
                    geography="global",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1")

        # Invented URL must not appear
        assert "1.5 inch AMOLED" not in output
        assert "https://evil.example.com/invented-url" not in output
        # Limited analysis shown instead
        assert "limited_analysis" in output


class TestStructuralBlocks:
    """Principle: homepage and category_mismatch are still hard-blocked."""

    def test_homepage_source_is_blocked(self):
        """Homepage URLs (no product-specific path) are hard-blocked."""
        annotations = [{
            "url": "https://store.example.com/",
            "title": "Store Homepage",
            "content": "Welcome to our store",
            "_relevance": {"relevance_type": "homepage", "geography": "global"},
        }]
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[
                CompetitorEvidence(
                    competitor="Competitor Y2",
                    field="display",
                    claim="1.5 inch AMOLED",
                    url="https://store.example.com/",
                    title="Store Homepage",
                    geography="global",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1")

        assert "1.5 inch AMOLED" not in output
        assert "limited_analysis" in output

    def test_category_mismatch_source_is_blocked(self):
        """Sources about a different category (e.g., phones when target is
        smartwatch) are hard-blocked."""
        annotations = [{
            "url": "https://store.example.com/smartphone-model-z",
            "title": "Smartphone Model Z",
            "content": "Latest smartphone with 5G",
            "_relevance": {"relevance_type": "category_mismatch", "geography": "global"},
        }]
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[
                CompetitorEvidence(
                    competitor="Competitor Y2",
                    field="display",
                    claim="1.5 inch AMOLED",
                    url="https://store.example.com/smartphone-model-z",
                    title="Smartphone Model Z",
                    geography="global",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1")

        assert "1.5 inch AMOLED" not in output
        assert "limited_analysis" in output

    def test_geography_mismatch_is_warning_not_block(self):
        """Geography mismatch between model-declared and code-detected is
        now a warning, not a hard block.  See TestGeographyGate for
        detailed coverage of the new geography policy."""
        # This test is kept as a marker — the detailed test is in
        # TestGeographyGate.test_geography_mismatch_is_warning_not_block
        pass


class TestLimitedAnalysisFallback:
    """Principle: no evidence → limited analysis, not empty report."""

    def test_no_validated_evidence_produces_limited_analysis(self):
        """When no evidence passes validation, renderer returns a limited
        analysis report with competitor names, product spec, and uncertainty
        — not an empty stub."""
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2", "Competitor Z3"],
            evidence=[],  # no evidence at all
            evidence_based_recommendations=[],
            strategic_hypotheses=[
                StrategicHypothesis(text="ขอข้อมูลเพิ่มเติม", rationale="ยังไม่ครบ"),
            ],
            uncertainty=["ยังไม่พบข้อมูลราคา"],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=[])
        output = renderer.render("---\nรหัสสินค้า: X1\nTestProduct X1\nจอ AMOLED")

        assert "limited_analysis" in output
        assert "TestProduct X1" in output
        assert "Competitor Y2" in output
        assert "Competitor Z3" in output
        # Product spec is shown
        assert "จอ AMOLED" in output
        # Recommendations and uncertainty are preserved
        assert "ขอข้อมูลเพิ่มเติม" in output
        assert "ยังไม่พบข้อมูลราคา" in output

    def test_limited_analysis_is_not_empty(self):
        """Limited analysis must have enough content to pass basic
        structural validation (not be a 100-char stub)."""
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[],
            evidence_based_recommendations=[],
            strategic_hypotheses=[],
            uncertainty=["ไม่มีข้อมูล"],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=[])
        output = renderer.render("---\nรหัสสินค้า: X1\nTestProduct X1\nจอ AMOLED 1.78 นิ้ว\nแบตเตอรี่ 680mAh")

        # Must be substantial (not a 100-char stub)
        assert len(output) > 300
        assert "limited_analysis" in output


class TestReassessAllAnnotations:
    """Principle: _reassess_all_annotations splits into three buckets:
    verified (relevant=True) → _last_relevant_annotations
    unverified (relevant=None) → _last_candidate_annotations
    rejected (relevant=False) → _last_rejected_annotations

    Both verified and unverified are available to the model via the
    evidence manifest, but only verified sources appear in user-facing
    fallback citations (legacy mode).
    """

    def test_market_unverified_annotations_go_to_candidates(self):
        """Annotations with relevant=None (unverified) go to
        _last_candidate_annotations, NOT _last_relevant_annotations.
        Only relevant=False (homepage) are rejected."""
        annotations = [
            {
                "url": "https://store.example.com/product-a",
                "title": "Product A",
                "content": "smartwatch market overview",
            },
            {
                "url": "https://store.example.com/",
                "title": "Store Home",
                "content": "welcome to our store",
            },
        ]
        agent = _make_agent_with_annotations(annotations, competitor_names=["Competitor Y2"])

        # First annotation (unverified) → candidates
        # Second annotation (homepage — empty path) → rejected
        assert len(agent._last_relevant_annotations) == 0
        assert len(agent._last_candidate_annotations) == 1
        assert len(agent._last_rejected_annotations) == 1
        assert agent._last_candidate_annotations[0]["url"] == "https://store.example.com/product-a"
        assert agent._last_rejected_annotations[0]["url"] == "https://store.example.com/"

    def test_all_non_blocked_annotations_available_without_competitor_match(self):
        """Even when no competitor name matches (default discovery before
        model returns names), annotations are still available as candidates
        — the model will decide which to cite."""
        annotations = [
            {
                "url": "https://store.example.com/some-product",
                "title": "Some Product",
                "content": "some smartwatch product info",
            },
            {
                "url": "https://store.example.com/another-product",
                "title": "Another Product",
                "content": "another smartwatch product info",
            },
        ]
        # No competitor names — default discovery mode
        agent = _make_agent_with_annotations(annotations, competitor_names=[])

        # Both annotations should be candidates (not blocked, not verified)
        assert len(agent._last_relevant_annotations) == 0
        assert len(agent._last_candidate_annotations) == 2
        assert len(agent._last_rejected_annotations) == 0


class TestTargetProductGuard:
    """Principle: target product must not change — model must analyze the
    product specified in the input, not a different one."""

    def test_target_model_mismatch_is_blocked(self):
        """If model returns a different target_model than the input product,
        validation fails."""
        import json
        agent = _make_agent_with_annotations([], competitor_names=["Competitor Y2"])
        # Model returned a different target
        bad_json = json.dumps({
            "target_model": "DifferentProduct Z9",
            "competitor_names": ["Competitor Y2"],
            "evidence": [],
            "evidence_based_recommendations": [], "strategic_hypotheses": [],
            "uncertainty": [],
        }, ensure_ascii=False)
        ok, err, _ = agent._validate_research_json(bad_json)
        assert not ok
        assert "target_model changed" in err

    def test_target_model_match_passes(self):
        """If model returns the same target_model (or a superset), validation passes."""
        import json
        agent = _make_agent_with_annotations([], competitor_names=["Competitor Y2"])
        good_json = json.dumps({
            "target_model": "TestProduct X1",
            "competitor_names": ["Competitor Y2"],
            "evidence": [],
            "evidence_based_recommendations": [], "strategic_hypotheses": [],
            "uncertainty": [],
        }, ensure_ascii=False)
        ok, err, _ = agent._validate_research_json(good_json)
        # evidence is empty so renderer.validate() will fail, but target check passes
        assert "target_model changed" not in err


class TestGeographyGate:
    """Principle: geography is model-decided.  Code does NOT override the
    model's geography judgment with TLD/substring detection.  Mismatch
    between model-declared and code-detected geography is a warning, not
    a hard block.

    Hard-block scope: foreign source presented as Thai price without
    qualification (e.g., USD price labeled as Thai market price).
    """

    def test_thai_store_with_com_tld_passes(self):
        """Thai store using .com TLD (e.g. vteccomputer.com) with price in
        baht — model declares geography=thailand, code accepts it."""
        annotations = [{
            "url": "https://www.vteccomputer.com/product/27105/imoo-watch-phone-z7",
            "title": "imoo Watch Phone Z7 - Vtec Computer",
            "content": "ราคา 7,999 บาท รับประกันศูนย์ไทย",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "global", "matched_competitor": "Competitor Y2"},  # code detects global
        }]
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[
                CompetitorEvidence(
                    competitor="Competitor Y2",
                    field="price_availability",
                    claim="7,999 บาท",
                    url="https://www.vteccomputer.com/product/27105/imoo-watch-phone-z7",
                    title="Vtec Computer",
                    geography="thailand",  # model declares thailand
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1")

        # Evidence should be rendered — model's geography declaration accepted
        assert "7,999 บาท" in output
        assert "## ตารางเปรียบเทียบ" in output

    def test_co_th_tld_passes(self):
        """Source with .co.th TLD passes geography check."""
        annotations = [{
            "url": "https://www.central.co.th/th/product-y2",
            "title": "Product Y2 - Central",
            "content": "Product info",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand", "matched_competitor": "Competitor Y2"},
        }]
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[
                CompetitorEvidence(
                    competitor="Competitor Y2",
                    field="price_availability",
                    claim="3,990 บาท",
                    url="https://www.central.co.th/th/product-y2",
                    title="Central",
                    geography="thailand",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1")

        assert "3,990 บาท" in output
        assert "## ตารางเปรียบเทียบ" in output

    def test_global_source_with_global_geography_passes(self):
        """Foreign source with geography=global passes."""
        annotations = [{
            "url": "https://www.gsmarena.com/product-y2",
            "title": "Product Y2 - GSMarena",
            "content": "Global product info",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "global", "matched_competitor": "Competitor Y2"},
        }]
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[
                CompetitorEvidence(
                    competitor="Competitor Y2",
                    field="display",
                    claim="1.5 inch AMOLED",
                    url="https://www.gsmarena.com/product-y2",
                    title="GSMarena",
                    geography="global",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1")

        assert "1.5 inch AMOLED" in output
        assert "## ตารางเปรียบเทียบ" in output

    def test_geography_mismatch_is_warning_not_block(self):
        """When model declares geography=thailand but code detects global,
        evidence is still rendered (warning, not hard block)."""
        annotations = [{
            "url": "https://store.example.com/product-y2",
            "title": "Product Y2",
            "content": "Some product info",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "global", "matched_competitor": "Competitor Y2"},
        }]
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[
                CompetitorEvidence(
                    competitor="Competitor Y2",
                    field="price_availability",
                    claim="3,990 บาท",
                    url="https://store.example.com/product-y2",
                    title="Store",
                    geography="thailand",  # model declares thailand, code detects global
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1")

        # Evidence should be rendered despite geography mismatch
        assert "3,990 บาท" in output
        assert "## ตารางเปรียบเทียบ" in output

    def test_url_outside_annotations_still_blocked(self):
        """URL provenance check still blocks invented URLs even with
        geography gate relaxed."""
        annotations = [{
            "url": "https://store.example.com/real-product",
            "title": "Real Product",
            "content": "Real info",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand", "matched_competitor": "Competitor Y2"},
        }]
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[
                CompetitorEvidence(
                    competitor="Competitor Y2",
                    field="price_availability",
                    claim="3,990 บาท",
                    url="https://evil.example.com/invented",
                    title="Fake",
                    geography="thailand",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1")

        # Invented URL must not appear
        assert "3,990 บาท" not in output
        assert "https://evil.example.com/invented" not in output
        assert "limited_analysis" in output
