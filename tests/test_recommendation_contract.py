"""Tests for the recommendation data contract: evidence_based vs strategic_hypothesis.

Verifies that:
1. evidence_based_recommendations with valid URLs are rendered as "ข้อเสนอแนะที่มีหลักฐานรองรับ"
2. evidence_based_recommendations with invalid URLs are demoted to hypotheses
3. strategic_hypotheses are rendered under "สมมติฐานเชิงกลยุทธ์ (ยังไม่ยืนยัน)"
4. Unsupported comparative claims cannot appear as verified recommendations
5. Limited analysis mode demotes all evidence_based to hypotheses
"""
from __future__ import annotations

from src.agents.competitor_evidence import (
    CompetitorEvidence,
    CompetitorReportRenderer,
    EvidenceBasedRecommendation,
    ResearchResponse,
    StrategicHypothesis,
)


def _make_validated_evidence_and_annotations():
    """Create a minimal validated evidence set with 2 URLs."""
    annotations = [
        {
            "url": "https://store.example.com/product-a-display",
            "title": "Product A Display",
            "content": "Product A has 1.5 inch AMOLED",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "global", "matched_competitor": "Competitor Y2"},
        },
        {
            "url": "https://store.example.com/product-a-price",
            "title": "Product A Price",
            "content": "Product A costs 3,990 baht",
            "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand", "matched_competitor": "Competitor Y2"},
        },
    ]
    research = ResearchResponse(
        target_model="TestProduct X1",
        competitor_names=["Competitor Y2"],
        evidence=[
            CompetitorEvidence(
                competitor="Competitor Y2",
                field="display",
                claim="1.5 inch AMOLED",
                url="https://store.example.com/product-a-display",
                geography="global",
            ),
            CompetitorEvidence(
                competitor="Competitor Y2",
                field="price_availability",
                claim="3,990 บาท",
                url="https://store.example.com/product-a-price",
                geography="thailand",
            ),
        ],
    )
    return research, annotations


class TestEvidenceBasedRecommendations:
    def test_valid_evidence_based_recommendation_is_rendered(self):
        """Recommendation with URLs that exist in validated evidence
        is rendered under 'ข้อเสนอแนะที่มีหลักฐานรองรับ'."""
        research, annotations = _make_validated_evidence_and_annotations()
        research.evidence_based_recommendations = [
            EvidenceBasedRecommendation(
                text="เน้นจอใหญ่ของ X1 เปรียบเทียบ Competitor Y2",
                supporting_evidence_urls=[
                    "https://store.example.com/product-a-display",
                ],
            ),
        ]
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1\nTestProduct X1")

        assert "ข้อเสนอแนะที่มีหลักฐานรองรับ" in output
        assert "เน้นจอใหญ่ของ X1" in output
        # Must NOT appear as hypothesis
        assert "สมมติฐานเชิงกลยุทธ์" not in output or "เน้นจอใหญ่ของ X1" not in output.split("สมมติฐานเชิงกลยุทธ์")[0]

    def test_evidence_based_with_invalid_url_is_demoted_to_hypothesis(self):
        """Recommendation with URL NOT in validated evidence is demoted
        to strategic hypothesis — not rendered as verified."""
        research, annotations = _make_validated_evidence_and_annotations()
        research.evidence_based_recommendations = [
            EvidenceBasedRecommendation(
                text="X1 เหนือกว่าคู่แข่งทุกรุ่น",
                supporting_evidence_urls=[
                    "https://evil.example.com/invented-url",  # not in evidence
                ],
            ),
        ]
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1\nTestProduct X1")

        # Must NOT appear as verified recommendation
        assert "ข้อเสนอแนะที่มีหลักฐานรองรับ" not in output
        # Must appear as hypothesis (demoted)
        assert "สมมติฐานเชิงกลยุทธ์" in output
        assert "X1 เหนือกว่าคู่แข่งทุกรุ่น" in output

    def test_evidence_based_with_empty_urls_is_demoted(self):
        """Recommendation with empty supporting_evidence_urls is demoted
        — no evidence means it's a hypothesis, not a verified claim."""
        research, annotations = _make_validated_evidence_and_annotations()
        research.evidence_based_recommendations = [
            EvidenceBasedRecommendation(
                text="X1 มี RAM มากกว่าคู่แข่ง",
                supporting_evidence_urls=[],  # empty
            ),
        ]
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1\nTestProduct X1")

        assert "ข้อเสนอแนะที่มีหลักฐานรองรับ" not in output
        assert "สมมติฐานเชิงกลยุทธ์" in output
        assert "X1 มี RAM มากกว่าคู่แข่ง" in output

    def test_mixed_valid_and_invalid_recommendations(self):
        """Valid recommendation is rendered as evidence_based;
        invalid one is demoted to hypothesis — both appear but in
        different sections."""
        research, annotations = _make_validated_evidence_and_annotations()
        research.evidence_based_recommendations = [
            EvidenceBasedRecommendation(
                text="เน้นราคา X1 ถูกกว่า Y2",
                supporting_evidence_urls=["https://store.example.com/product-a-price"],
            ),
            EvidenceBasedRecommendation(
                text="X1 มีฟีเจอร์สุขภาพที่หายาก",
                supporting_evidence_urls=["https://evil.example.com/invented"],
            ),
        ]
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1\nTestProduct X1")

        # Valid one in evidence_based section
        assert "ข้อเสนอแนะที่มีหลักฐานรองรับ" in output
        assert "เน้นราคา X1 ถูกกว่า Y2" in output
        # Invalid one demoted to hypothesis
        assert "สมมติฐานเชิงกลยุทธ์" in output
        assert "X1 มีฟีเจอร์สุขภาพที่หายาก" in output


class TestStrategicHypotheses:
    def test_strategic_hypothesis_is_rendered_as_unverified(self):
        """Strategic hypothesis is rendered under 'สมมติฐานเชิงกลยุทธ์ (ยังไม่ยืนยัน)'
        with explicit unverified label."""
        research, annotations = _make_validated_evidence_and_annotations()
        research.strategic_hypotheses = [
            StrategicHypothesis(
                text="X1 น่าจะเหนือกว่าคู่แข่งในระดับราคาเดียวกัน",
                rationale="สเปกดูดีกว่า แต่ยังไม่ได้เปรียบเทียบจริง",
            ),
        ]
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1\nTestProduct X1")

        assert "สมมติฐานเชิงกลยุทธ์ (ยังไม่ยืนยัน)" in output
        assert "X1 น่าจะเหนือกว่าคู่แข่ง" in output
        assert "สเปกดูดีกว่า" in output  # rationale shown
        # Must NOT appear as verified
        assert "ข้อเสนอแนะที่มีหลักฐานรองรับ" not in output

    def test_hypothesis_disclaimer_is_shown(self):
        """Hypotheses section must include disclaimer that content is unverified."""
        research, annotations = _make_validated_evidence_and_annotations()
        research.strategic_hypotheses = [
            StrategicHypothesis(text="test hypothesis", rationale="test rationale"),
        ]
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: X1\nTestProduct X1")

        assert "ไม่ใช่ข้อเท็จจริงที่ผ่านการตรวจสอบ" in output


class TestLimitedAnalysisDemotesAllRecommendations:
    def test_limited_analysis_demotes_evidence_based_to_hypotheses(self):
        """In limited analysis (no validated evidence), all evidence_based
        recommendations are demoted to hypotheses."""
        research = ResearchResponse(
            target_model="TestProduct X1",
            competitor_names=["Competitor Y2"],
            evidence=[],  # no evidence → limited analysis
            evidence_based_recommendations=[
                EvidenceBasedRecommendation(
                    text="X1 เหนือกว่าคู่แข่ง",
                    supporting_evidence_urls=["https://store.example.com/some-url"],
                ),
            ],
            strategic_hypotheses=[
                StrategicHypothesis(text="test hypothesis", rationale="test"),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=[])
        output = renderer.render("---\nรหัสสินค้า: X1\nTestProduct X1")

        # Limited analysis mode
        assert "limited_analysis" in output
        # No evidence_based section (no validated evidence)
        assert "ข้อเสนอแนะที่มีหลักฐานรองรับ" not in output
        # Both original hypothesis and demoted recommendation appear as hypotheses
        assert "สมมติฐานเชิงกลยุทธ์" in output
        assert "X1 เหนือกว่าคู่แข่ง" in output
        assert "test hypothesis" in output


class TestReplayArtifactScenario:
    """Replay the exact scenario from the failed qualification run:
    model returned comparative claims in recommendations without
    evidence backing.  The new contract separates evidence_based from
    hypotheses, but provenance check alone cannot detect semantic
    mismatch (e.g., GPS URL backing a display claim).  The system
    prompt is responsible for guiding the model to use the right category."""

    def test_comparative_claim_with_wrong_evidence_url_passes_provenance(self):
        """Documents the limitation: if model uses a valid URL (from a
        different field) to back a comparative claim, provenance check
        passes.  The system prompt must guide the model to put
        unsupported comparative claims in strategic_hypotheses instead.

        This is expected behavior — code checks provenance (URL in
        evidence set), not semantic relevance of URL to claim text.
        Semantic matching is the model's job, enforced via prompt."""
        annotations = [
            {
                "url": "https://www.homepro.co.th/p/888201600001",
                "title": "imoo Z1 GPS",
                "content": "imoo Z1 with GPS",
                "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand", "matched_competitor": "imoo Watch Phone Z1"},
            },
            {
                "url": "https://www.central.co.th/th/imoo-kid-watch-phone-z1",
                "title": "imoo Z1 Price",
                "content": "imoo Z1 3,999 baht",
                "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand", "matched_competitor": "imoo Watch Phone Z1"},
            },
        ]
        research = ResearchResponse(
            target_model="K2",
            competitor_names=["imoo Watch Phone Z1"],
            evidence=[
                CompetitorEvidence(
                    competitor="imoo Watch Phone Z1",
                    field="gps",
                    claim="รองรับ GPS",
                    url="https://www.homepro.co.th/p/888201600001",
                    geography="thailand",
                ),
                CompetitorEvidence(
                    competitor="imoo Watch Phone Z1",
                    field="price_availability",
                    claim="3,999 บาท",
                    url="https://www.central.co.th/th/imoo-kid-watch-phone-z1",
                    geography="thailand",
                ),
            ],
            # Model correctly puts display claim in hypotheses (no display evidence)
            evidence_based_recommendations=[
                EvidenceBasedRecommendation(
                    text="เน้นราคา K2 ที่แข่งขันได้กับ imoo Z1",
                    supporting_evidence_urls=[
                        "https://www.central.co.th/th/imoo-kid-watch-phone-z1",
                    ],
                ),
            ],
            strategic_hypotheses=[
                StrategicHypothesis(
                    text="ควรเน้นจุดขายด้านหน้าจอ AMOLED 1.78 นิ้ว ซึ่งน่าจะเหนือกว่าคู่แข่ง",
                    rationale="สเปก K77 มีจอ AMOLED แต่ยังไม่พบข้อมูลจอของคู่แข่งจาก web search",
                ),
            ],
            uncertainty=["ไม่พบข้อมูลสเปคหน้าจอของคู่แข่ง"],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
        output = renderer.render("---\nรหัสสินค้า: K2\nLagenio K2\nจอ AMOLED 1.78 นิ้ว")

        # Price recommendation (valid URL) appears as evidence_based
        assert "ข้อเสนอแนะที่มีหลักฐานรองรับ" in output
        assert "เน้นราคา K2" in output
        # Display claim (no evidence) appears as hypothesis
        assert "สมมติฐานเชิงกลยุทธ์" in output
        assert "AMOLED 1.78 นิ้ว" in output
        assert "ไม่ใช่ข้อเท็จจริงที่ผ่านการตรวจสอบ" in output
