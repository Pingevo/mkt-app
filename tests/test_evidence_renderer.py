"""Offline tests for the two-stage evidence renderer prototype."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.agents.competitor_evidence import (
    CompetitorEvidence,
    CompetitorReportRenderer,
    EvidenceBasedRecommendation,
    ResearchResponse,
    StrategicHypothesis,
)
from src.agents.competitor_analysis import CompetitorAnalysisAgent
from tests.test_competitor_analysis import FakeLLM


def _load_fixture(name: str) -> dict:
    p = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / f"{name}.json"
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _make_agent(fixture: dict) -> CompetitorAnalysisAgent:
    cfg = {
        "web_search": True,
        "web_search_mode": "required",
        "max_retry_limit": 0,
    }
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    agent._last_relevant_annotations = fixture["relevant_annotations"]
    agent._last_raw_annotations_count = len(fixture["relevant_annotations"])
    agent._last_tool_use = {"web_search_requests": 1, "tool_calls_executed": 1}
    return agent


def _check_outcome_for_rendered(output: str, fixture: dict) -> dict:
    from tests.acceptance_competitor_runner import _check_outcome
    return _check_outcome({
        "case_name": "k77_thailand_web_search",
        "output": output,
        "validation_ok": True,
        "validation_error": "",
        "runtime_error": None,
        "relevant_annotations": fixture["relevant_annotations"],
        "product_spec": fixture["product_spec"],
        "competitor_data": fixture["competitor_data"],
        "web_search_mode": "required",
    })


class TestStageAEvidenceContract:
    def test_evidence_without_selected_url_is_dropped(self):
        """Model ไม่สามารถ invent URL เป็น source ได้ — invented URL is not
        rendered; limited analysis is shown instead of the claim."""
        research = ResearchResponse(
            target_model="CACGO K77",
            competitor_names=["Xiaomi Watch S3"],
            evidence=[
                CompetitorEvidence(
                    competitor="Xiaomi Watch S3",
                    field="display",
                    claim="1.43\" AMOLED",
                    url="https://example.com/invented",
                    title="Invented",
                    geography="thailand",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=[])
        output = renderer.render("---\nรหัสสินค้า: K77\nCACGO K77")
        # invented URL must not appear in output
        assert "1.43\" AMOLED" not in output
        assert "https://example.com/invented" not in output
        # limited analysis is shown instead of empty stub
        assert "limited_analysis" in output

    def test_competitor_outside_scope_is_dropped(self):
        """Evidence citing a competitor not in competitor_names is dropped."""
        research = ResearchResponse(
            target_model="CACGO K77",
            competitor_names=["Xiaomi Watch S3"],
            evidence=[
                CompetitorEvidence(
                    competitor="Galaxy Watch9",
                    field="display",
                    claim="1.46\" AMOLED",
                    url="https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    title="Siamphone",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=[{
            "url": "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
            "title": "Siamphone",
            "content": "Xiaomi Watch S3",
            "_relevance": {"relevance_type": "competitor", "geography": "thailand"},
        }])
        output = renderer.render("---\nรหัสสินค้า: K77")
        # out-of-scope competitor's claim must not appear
        assert "1.46\" AMOLED" not in output
        assert "limited_analysis" in output

    def test_thai_claim_with_global_source_is_warning_not_block(self):
        """Thai geography claim with non-Thai source is now a warning,
        not a hard block — model-decided geography is accepted."""
        research = ResearchResponse(
            target_model="CACGO K77",
            competitor_names=["Xiaomi Watch S3"],
            evidence=[
                CompetitorEvidence(
                    competitor="Xiaomi Watch S3",
                    field="price_availability",
                    claim="6,990 บาท",
                    url="https://www.gsmarena.com/xiaomi-watch-s3",
                    title="GSMarena",
                    geography="thailand",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=[{
            "url": "https://www.gsmarena.com/xiaomi-watch-s3",
            "title": "GSMarena",
            "content": "Xiaomi Watch S3 global",
            "_relevance": {"relevance_type": "competitor", "geography": "global"},
        }])
        output = renderer.render("---\nรหัสสินค้า: K77")
        # Geography is model-decided — evidence is rendered despite mismatch
        assert "6,990 บาท" in output
        assert "## ตารางเปรียบเทียบ" in output


class TestStageBRenderer:
    def test_rendered_table_has_inline_citations(self):
        fixture = _load_fixture("agent2_quality_good")
        research = ResearchResponse(
            target_model="CACGO K77",
            competitor_names=["Xiaomi Watch S3", "Kieslect"],
            evidence=[
                CompetitorEvidence(
                    competitor="Xiaomi Watch S3",
                    field="display",
                    claim='1.43" AMOLED (466×466 px)',
                    url="https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    title="Siamphone",
                    geography="thailand",
                ),
                CompetitorEvidence(
                    competitor="Kieslect",
                    field="battery",
                    claim="510mAh",
                    url="https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition",
                    title="Kieslect Thailand",
                    geography="thailand",
                ),
            ],
            evidence_based_recommendations=[
                EvidenceBasedRecommendation(
                    text="เน้นจุดขายหน้าจอใหญ่และแบตอึด",
                    supporting_evidence_urls=[
                        "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                        "https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition",
                    ],
                ),
            ],
            strategic_hypotheses=[
                StrategicHypothesis(
                    text="ระบุช่องทางจำหน่าย official Thailand",
                    rationale="คู่แข่งมีช่องทาง official เราควรชูจุดนี้",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=fixture["relevant_annotations"])
        output = renderer.render(fixture["product_spec"])

        # no fallback dump
        assert "แหล่งอ้างอิง (fallback)" not in output
        # inline citations in table cells — title must come from the selected annotation
        assert "[Xiaomi Watch S3](https://www.siamphone.com/smartwatch/xiaomi/watch-s3)" in output
        assert "[Kieslect AI Smartwatch Elite2 Noir Edition](https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition)" in output
        # target model
        assert "CACGO K77" in output
        # recommendations separated
        assert "ข้อเสนอแนะที่มีหลักฐานรองรับ" in output

    def test_rendered_output_passes_competitor_validation(self):
        fixture = _load_fixture("agent2_quality_good")
        research = ResearchResponse(
            target_model="CACGO K77",
            competitor_names=["Xiaomi Watch S3", "Kieslect"],
            evidence=[
                CompetitorEvidence(
                    competitor="Xiaomi Watch S3",
                    field="display",
                    claim='1.43" AMOLED (466×466 px)',
                    url="https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    title="Siamphone",
                    geography="thailand",
                ),
                CompetitorEvidence(
                    competitor="Kieslect",
                    field="battery",
                    claim="510mAh",
                    url="https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition",
                    title="Kieslect Thailand",
                    geography="thailand",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=fixture["relevant_annotations"])
        output = renderer.render(fixture["product_spec"])

        agent = _make_agent(fixture)
        ok, err = agent.validate_output(output)
        assert ok, err

    def test_rendered_output_has_enough_facts_for_full_analysis(self):
        fixture = _load_fixture("agent2_quality_good")
        research = ResearchResponse(
            target_model="CACGO K77",
            competitor_names=["Xiaomi Watch S3", "Kieslect"],
            evidence=[
                CompetitorEvidence(
                    competitor="Xiaomi Watch S3",
                    field="display",
                    claim='1.43" AMOLED (466×466 px)',
                    url="https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    title="Siamphone",
                    geography="thailand",
                ),
                CompetitorEvidence(
                    competitor="Kieslect",
                    field="battery",
                    claim="510mAh",
                    url="https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition",
                    title="Kieslect Thailand",
                    geography="thailand",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=fixture["relevant_annotations"])
        output = renderer.render(fixture["product_spec"])

        agent = _make_agent(fixture)
        q = agent._full_analysis_quality(output)
        assert q["competitor_fact_cells"] >= 2
        assert q["cited_fact_cells"] >= 2
        assert q["thai_fact_cells"] >= 1
        assert q["competitor_cells_total"]
        assert q["no_evidence_cells"] / q["competitor_cells_total"] <= 0.5

    def test_rendered_output_passes_acceptance_outcome(self):
        fixture = _load_fixture("agent2_quality_good")
        research = ResearchResponse(
            target_model="CACGO K77",
            competitor_names=["Xiaomi Watch S3", "Kieslect"],
            evidence=[
                CompetitorEvidence(
                    competitor="Xiaomi Watch S3",
                    field="display",
                    claim='1.43" AMOLED (466×466 px)',
                    url="https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    title="Siamphone",
                    geography="thailand",
                ),
                CompetitorEvidence(
                    competitor="Kieslect",
                    field="battery",
                    claim="510mAh",
                    url="https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition",
                    title="Kieslect Thailand",
                    geography="thailand",
                ),
            ],
            evidence_based_recommendations=[
                EvidenceBasedRecommendation(
                    text="เน้นจุดขายหน้าจอใหญ่",
                    supporting_evidence_urls=[
                        "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    ],
                ),
            ],
            strategic_hypotheses=[
                StrategicHypothesis(
                    text="ใช้แบตอึดเป้าหมาย",
                    rationale="K77 มีแบตใหญ่ตามสเปก",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=fixture["relevant_annotations"])
        output = renderer.render(fixture["product_spec"])

        outcome = _check_outcome_for_rendered(output, fixture)
        assert outcome["passed"], outcome


class TestBadEvidenceCannotRender:
    def test_tag_or_homepage_source_is_not_rendered(self):
        """Homepage/tag sources are structurally blocked — claim is not rendered."""
        fixture = _load_fixture("agent2_quality_bad_source")
        research = ResearchResponse(
            target_model="CACGO K77",
            competitor_names=["Xiaomi Watch S3", "Kieslect"],
            evidence=[
                CompetitorEvidence(
                    competitor="Kieslect",
                    field="battery",
                    claim="510mAh",
                    url="https://www.kieslectthailand.com/en/tag/Elite2",
                    title="Tag",
                    geography="thailand",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=fixture["relevant_annotations"])
        output = renderer.render(fixture["product_spec"])

        # bad source is dropped
        assert "510mAh" not in output
        # limited analysis is shown instead of empty stub
        assert "limited_analysis" in output

    def test_model_messy_evidence_renders_safely(self):
        """ถ้า model คืน evidence มากเกิน/ซ้ำ/ผิด ให้ renderer เลือกอันทีผ่าน validation เท่านั้น."""
        fixture = _load_fixture("agent2_quality_good")
        research = ResearchResponse(
            target_model="CACGO K77",
            competitor_names=["Xiaomi Watch S3"],
            evidence=[
                # good one
                CompetitorEvidence(
                    competitor="Xiaomi Watch S3",
                    field="display",
                    claim='1.43" AMOLED',
                    url="https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    title="Siamphone",
                    geography="thailand",
                ),
                # invented URL
                CompetitorEvidence(
                    competitor="Xiaomi Watch S3",
                    field="display",
                    claim="1.5\" LCD",
                    url="https://example.com/invented",
                    title="Fake",
                    geography="thailand",
                ),
            ],
        )
        renderer = CompetitorReportRenderer(research, relevant_annotations=fixture["relevant_annotations"])
        output = renderer.render(fixture["product_spec"])

        # มีแค่ good evidence (title comes from the selected annotation)
        assert "[Xiaomi Watch S3]" in output
        assert "1.5\" LCD" not in output


def test_pipe_in_source_title_does_not_break_table():
    """Renderer must escape `|` in source titles so Markdown table does not split."""
    research = ResearchResponse(
        target_model="CACGO K77",
        competitor_names=["Xiaomi Watch S3"],
        evidence=[
            CompetitorEvidence(
                competitor="Xiaomi Watch S3",
                field="display",
                claim='1.43" AMOLED',
                url="https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                title="",
                geography="global",
            ),
        ],
    )
    renderer = CompetitorReportRenderer(research, relevant_annotations=[{
        "url": "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
        "title": "Xiaomi Watch S3 | Siamphone",
        "content": "Xiaomi Watch S3",
        "_relevance": {"relevance_type": "competitor", "geography": "global"},
    }])
    output = renderer.render("---\nรหัสสินค้า: K77")

    # table still has the right number of columns; pipe escaped in title
    rows = [l for l in output.splitlines() if l.startswith("|")]
    data_row = [r for r in rows if '1.43" AMOLED' in r][0]
    cells = [c.strip() for c in data_row.strip().strip("|").split("|")]
    assert len(cells) == 3
    assert "&#124;" in cells[2]
    assert "https://www.siamphone.com/smartwatch/xiaomi/watch-s3" in cells[2]


def test_pipe_and_newline_in_claim_are_escaped():
    """Renderer must escape `|` and newlines inside the factual claim itself."""
    research = ResearchResponse(
        target_model="CACGO K77",
        competitor_names=["Xiaomi Watch S3"],
        evidence=[
            CompetitorEvidence(
                competitor="Xiaomi Watch S3",
                field="display",
                claim='1.43" AMOLED\n466x466\n| 326ppi',
                url="https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                title="",
                geography="global",
            ),
        ],
    )
    renderer = CompetitorReportRenderer(research, relevant_annotations=[{
        "url": "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
        "title": "Siamphone",
        "content": "Xiaomi Watch S3",
        "_relevance": {"relevance_type": "competitor", "geography": "global"},
    }])
    output = renderer.render("---\nรหัสสินค้า: K77")

    data_row = [l for l in output.splitlines() if '1.43" AMOLED' in l][0]
    cells = [c.strip() for c in data_row.strip().strip("|").split("|")]
    assert len(cells) == 3
    assert "<br>" in cells[2]
    assert "&#124;" in cells[2]
    assert "https://www.siamphone.com/smartwatch/xiaomi/watch-s3" in cells[2]


def test_replay_unmodified_validated_artifact_passes_beta_gate():
    """Re-render the latest production .research.json without mutation and verify it passes the BETA gate."""
    from tests.acceptance_competitor_runner import _check_outcome

    base = Path(__file__).resolve().parent.parent / "output" / "acceptance"
    research_paths = sorted(base.glob("case_*.research.json"), reverse=True)
    if not research_paths:
        pytest.skip("no validated_research_response artifact from a real qualification run to replay")

    research_path = research_paths[0]
    meta_path = research_path.with_name(research_path.name.replace(".research.json", ".json"))
    if not meta_path.exists():
        pytest.skip("metadata artifact not found for the research artifact")

    with open(research_path, encoding="utf-8") as f:
        research_json = f.read()
    research = ResearchResponse.from_json(research_json)
    assert research.evidence, "validated artifact must contain evidence"

    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    renderer = CompetitorReportRenderer(
        research,
        relevant_annotations=meta.get("relevant_annotations", []),
    )
    output = renderer.render(meta["product_spec"])

    assert "## ตารางเปรียบเทียบ" in output
    assert "**structural_output_failed**" not in output

    outcome = _check_outcome({
        "case_name": meta.get("case_name", "k77_thailand_web_search"),
        "output": output,
        "validation_ok": True,
        "validation_error": "",
        "runtime_error": None,
        "relevant_annotations": meta.get("relevant_annotations", []),
        "product_spec": meta["product_spec"],
        "competitor_data": meta.get("competitor_data", ""),
        "web_search_mode": "required",
    })
    assert outcome["competitor_fact_cells"] >= 2
    assert outcome["thai_fact_cells"] >= 1
    assert outcome["no_evidence_ratio"] <= 0.5
    assert outcome["passed"]


def test_six_evidence_bounded_response_renders_usable_report():
    """A bounded ResearchResponse with up to 6 evidence must produce a readable Markdown report."""
    from tests.acceptance_competitor_runner import _check_outcome

    fixture = _load_fixture("agent2_quality_good")
    evidence = [
        CompetitorEvidence(
            competitor="Xiaomi Watch S3",
            field="display",
            claim='1.43" AMOLED (466×466 px)',
            url="https://a.example.com/xiaomi-display",
            geography="global",
        ),
        CompetitorEvidence(
            competitor="Xiaomi Watch S3",
            field="battery",
            claim="486mAh, 15-day typical use",
            url="https://a.example.com/xiaomi-battery",
            geography="global",
        ),
        CompetitorEvidence(
            competitor="Xiaomi Watch S3",
            field="price_availability",
            claim="6,990 บาท ผ่านช่องทาง official Thailand",
            url="https://a.example.com/xiaomi-price",
            geography="thailand",
        ),
        CompetitorEvidence(
            competitor="Kieslect",
            field="display",
            claim='1.46" LTPO AMOLED 480×480',
            url="https://a.example.com/kieslect-display",
            geography="global",
        ),
        CompetitorEvidence(
            competitor="Kieslect",
            field="battery",
            claim="510mAh, 14-day typical use",
            url="https://a.example.com/kieslect-battery",
            geography="global",
        ),
        CompetitorEvidence(
            competitor="Kieslect",
            field="features",
            claim="ChatGPT AI assistant, Bluetooth calling",
            url="https://a.example.com/kieslect-features",
            geography="global",
        ),
    ]
    relevant = [
        {
            "url": ev.url,
            "title": f"{ev.competitor} - {ev.field}",
            "content": ev.competitor,
            "_relevance": {"relevance_type": "competitor", "geography": ev.geography},
        }
        for ev in evidence
    ]
    research = ResearchResponse(
        target_model="CACGO K77",
        competitor_names=["Xiaomi Watch S3", "Kieslect"],
        evidence=evidence,
        evidence_based_recommendations=[
            EvidenceBasedRecommendation(
                text="เน้นหน้าจอใหญ่",
                supporting_evidence_urls=[evidence[0].url],
            ),
        ],
        strategic_hypotheses=[
            StrategicHypothesis(
                text="แบตอึด",
                rationale="ตามสเปก",
            ),
        ],
        uncertainty=["ยังไม่พบราคา Kieslect"],
    )
    renderer = CompetitorReportRenderer(research, relevant_annotations=relevant)
    output = renderer.render(fixture["product_spec"])

    # fits within token budget when converted to single-line output
    assert len("\n".join(output.splitlines())) < 20000
    assert "## ตารางเปรียบเทียบ" in output
    assert "ข้อเสนอแนะที่มีหลักฐานรองรับ" in output

    outcome = _check_outcome({
        "case_name": "k77_thailand_web_search",
        "output": output,
        "validation_ok": True,
        "validation_error": "",
        "runtime_error": None,
        "relevant_annotations": relevant,
        "product_spec": fixture["product_spec"],
        "competitor_data": fixture["competitor_data"],
        "web_search_mode": "required",
    })
    assert outcome["competitor_fact_cells"] >= 2
    assert outcome["thai_fact_cells"] >= 1
