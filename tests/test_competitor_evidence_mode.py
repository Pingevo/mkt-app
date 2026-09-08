"""Tests for CompetitorAnalysisAgent two-stage evidence mode.

Covers: schema validation, JSON transport hygiene (fenced JSON),
self-review/revision, evidence-to-annotation contract, and failure state.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.competitor_analysis import CompetitorAnalysisAgent
from src.agents.competitor_evidence import (
    CompetitorReportRenderer,
    EVIDENCE_SYSTEM_PROMPT,
    RESEARCH_RESPONSE_SCHEMA,
    ResearchResponse,
)
from src.config_loader import load_config, get_agent_config

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "agent2_quality_good.json"


def _load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _evidence_config():
    cfg = dict(get_agent_config(load_config(), "competitor_analysis"))
    cfg["evidence_mode"] = True
    cfg["web_search"] = True
    cfg["web_search_mode"] = "required"
    cfg["max_retry_limit"] = 0  # 1 LLM attempt, no BaseAgent repair
    return cfg


def _good_research_response():
    return json.dumps(
        {
            "target_model": "CACGO K77",
            "competitor_names": ["Xiaomi Watch S3", "Kieslect"],
            "evidence": [
                {
                    "competitor": "Xiaomi Watch S3",
                    "field": "display",
                    "claim": '1.43" AMOLED (466×466 px)',
                    "url": "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    "geography": "global",
                },
                {
                    "competitor": "Kieslect",
                    "field": "battery",
                    "claim": "510mAh",
                    "url": "https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition",
                    "geography": "thailand",
                },
            ],
            "evidence_based_recommendations": [
                {
                    "text": "เน้นหน้าจอใหญ่ของ K77 เปรียบเทียบกับ Xiaomi Watch S3",
                    "supporting_evidence_urls": [
                        "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    ],
                },
            ],
            "strategic_hypotheses": [
                {
                    "text": "ชูแบตอึดเป็นจุดขาย",
                    "rationale": "K77 มีแบตใหญ่กว่า Kieslect ตามสเปก แต่ยังไม่ได้เปรียบเทียบกับทุกรุ่น",
                },
            ],
            "uncertainty": ["ยังไม่พบราคา Kieslect"],
        },
        ensure_ascii=False,
    )


class FakeLLM:
    """Deterministic LLM double with distinct generate and revision outputs."""

    def __init__(self, generate_output: str = "", annotations: list | None = None, revision_output: str = "",
                 semantic_review_output: str = ""):
        self.generate_output = generate_output
        self.annotations = annotations or []
        self.revision_output = revision_output
        # Default: keep all evidence (no semantic changes)
        self.semantic_review_output = semantic_review_output or json.dumps(
            [{"index": i, "action": "keep"} for i in range(10)], ensure_ascii=False
        )
        self.calls: list[dict] = []
        self._last_raw_response = {"usage": {"server_tool_use_details": {"web_search_requests": 1, "tool_calls_executed": 1}}}
        self._last_raw_annotations_count = 0

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        self._last_raw_annotations_count = len(self.annotations)
        source = kwargs.get("source", "")
        if ".semantic_review" in source:
            return self.semantic_review_output
        if ".revise" in source:
            return self.revision_output
        if kwargs.get("return_annotations"):
            return self.generate_output, self.annotations
        return self.generate_output

    def close(self):
        pass


def test_valid_research_response_renders_markdown():
    """Valid ResearchResponse -> 1 API call, deterministic Markdown."""
    fixture = _load_fixture()
    fake = FakeLLM(generate_output=_good_research_response(), annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt)

    assert len(fake.calls) == 2  # 1 generate + 1 semantic review
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" in result
    assert "**structural_output_failed**" not in result
    assert "**required_search_failed**" not in result
    assert agent._last_draft_output == _good_research_response()


def test_fenced_json_with_valid_schema_renders():
    """Transport hygiene: strip ```json fence and validate."""
    fixture = _load_fixture()
    fenced = f"```json\n{_good_research_response()}\n```"
    fake = FakeLLM(generate_output=fenced, annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt)

    assert len(fake.calls) == 2  # 1 generate + 1 semantic review
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" in result
    assert "**structural_output_failed**" not in result


def test_wrong_key_triggers_one_revision():
    """Model returns wrong key -> one self-review -> valid second response."""
    fixture = _load_fixture()
    first = json.dumps({
        "research_metadata": {"scope": "competitor_research"},
        "competitors": ["Xiaomi Watch S3"],
    })
    fake = FakeLLM(generate_output=first, revision_output=_good_research_response(), annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt)

    assert len(fake.calls) == 3  # 1 generate + 1 revise + 1 semantic review
    assert fake.calls[1]["kwargs"].get("source") == "competitor_analysis.revise"
    assert fake.calls[1]["kwargs"].get("response_format") == RESEARCH_RESPONSE_SCHEMA
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" in result
    assert "**structural_output_failed**" not in result


def test_untrusted_url_triggers_one_revision():
    """Evidence URL not in selected annotations -> one self-review."""
    fixture = _load_fixture()
    bad = json.dumps(
        {
            "target_model": "CACGO K77",
            "competitor_names": ["Xiaomi Watch S3", "Kieslect"],
            "evidence": [
                {
                    "competitor": "Xiaomi Watch S3",
                    "field": "display",
                    "claim": "1.43\" AMOLED",
                    "url": "https://example.com/invented",
                    "geography": "thailand",
                },
                {
                    "competitor": "Kieslect",
                    "field": "battery",
                    "claim": "510mAh",
                    "url": "https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition",
                    "geography": "thailand",
                },
            ],
            "evidence_based_recommendations": [], "strategic_hypotheses": [],
            "uncertainty": ["ยังไม่พบราคา"],
        },
        ensure_ascii=False,
    )
    fake = FakeLLM(generate_output=bad, revision_output=_good_research_response(), annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt)

    assert len(fake.calls) == 3  # 1 generate + 1 revise + 1 semantic review
    assert "**required_search_failed**" not in result
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" in result


def test_revision_still_invalid_fails_structural():
    """First JSON invalid, revision still invalid -> clear structural failure."""
    fixture = _load_fixture()
    fake = FakeLLM(generate_output="not json", revision_output="still not json", annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt)

    assert len(fake.calls) == 2
    assert "**structural_output_failed: true**" in result


def test_untrusted_url_not_fixable_fails_required_search():
    """URL not in selected annotations and revision cannot fix -> required_search_failed."""
    fixture = _load_fixture()
    bad = json.dumps(
        {
            "target_model": "CACGO K77",
            "competitor_names": ["Xiaomi Watch S3", "Kieslect"],
            "evidence": [
                {
                    "competitor": "Xiaomi Watch S3",
                    "field": "display",
                    "claim": "1.43\" AMOLED",
                    "url": "https://example.com/invented",
                    "geography": "thailand",
                },
            ],
            "evidence_based_recommendations": [], "strategic_hypotheses": [],
            "uncertainty": ["ยังไม่พบราคา"],
        },
        ensure_ascii=False,
    )
    fake = FakeLLM(generate_output=bad, revision_output=bad, annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt)

    assert len(fake.calls) == 2
    assert "**required_search_failed: true**" in result


def test_thai_evidence_with_global_source_is_accepted():
    """evidence.geography=thailand but code detects global source — model-decided
    geography is accepted (warning, not hard block).  Evidence is rendered."""
    fixture = _load_fixture()
    bad = json.dumps(
        {
            "target_model": "CACGO K77",
            "competitor_names": ["Xiaomi Watch S3", "Kieslect"],
            "evidence": [
                {
                    "competitor": "Xiaomi Watch S3",
                    "field": "display",
                    "claim": '1.43" AMOLED',
                    "url": "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    "geography": "thailand",
                },
                {
                    "competitor": "Kieslect",
                    "field": "battery",
                    "claim": "510mAh",
                    "url": "https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition",
                    "geography": "thailand",
                },
            ],
            "evidence_based_recommendations": [], "strategic_hypotheses": [],
            "uncertainty": ["ยังไม่พบราคา"],
        },
        ensure_ascii=False,
    )
    fake = FakeLLM(generate_output=bad, revision_output=_good_research_response(), annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt)

    # Geography is model-decided — evidence is rendered without revision
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" in result


def test_evidence_prompt_is_stage_a_not_legacy():
    """Evidence mode uses the dedicated Stage A JSON-only core prompt while
    still receiving shared additions (brand reference, instructions,
    grounding policy) from BaseAgent composition."""
    cfg = _evidence_config()
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent._evidence_mode = True
    prompt = agent._build_system_prompt()

    # The evidence core must be present
    assert EVIDENCE_SYSTEM_PROMPT in prompt
    assert "competitor_research" in prompt
    assert "Stage B" in prompt
    # The legacy competitor core must NOT be present
    assert "Final output ต้องเป็นรายงานวิเคราะห์มืออาชีพ" not in prompt
    assert "เขียนเป็นรายงานวิเคราะห์มืออาชีพ" not in prompt
    assert "ตารางเปรียบเทียบ" not in prompt
    # Shared additions from BaseAgent must also be present
    # Grounding policy is configured for competitor_analysis
    assert "Grounding Policy" in prompt or "นโยบายข้อมูลต้นทาง" in prompt


def test_revise_receives_canonical_manifest():
    """Self-review call must include a canonical manifest of selected/relevant URLs."""
    fixture = _load_fixture()
    bad = json.dumps({"wrong_key": "value"})
    fake = FakeLLM(generate_output=bad, revision_output=_good_research_response(), annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    agent.run(prompt)

    assert len(fake.calls) == 3  # 1 generate + 1 revise + 1 semantic review
    revise_message = fake.calls[1]["messages"][-1]["content"]
    # G2: manifest now separates verified vs unverified sources
    assert "รายการ URL ที่ยืนยันแล้ว" in revise_message
    assert "https://www.siamphone.com/smartwatch/xiaomi/watch-s3" in revise_message
    assert "https://www.kieslectthailand.com" in revise_message
    assert "ห้ามใช้ URL นอก manifest" in revise_message


def test_revise_url_outside_manifest_rejected():
    """Revise output using a URL not in the canonical manifest must fail."""
    fixture = _load_fixture()
    bad = json.dumps({"wrong_key": "value"})
    outside = json.dumps(
        {
            "target_model": "CACGO K77",
            "competitor_names": ["Xiaomi Watch S3", "Kieslect"],
            "evidence": [
                {
                    "competitor": "Xiaomi Watch S3",
                    "field": "display",
                    "claim": "1.43\" AMOLED",
                    "url": "https://example.com/outside-manifest",
                    "geography": "thailand",
                },
            ],
            "evidence_based_recommendations": [], "strategic_hypotheses": [],
            "uncertainty": ["ยังไม่พบราคา"],
        },
        ensure_ascii=False,
    )
    fake = FakeLLM(generate_output=bad, revision_output=outside, annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt)

    assert len(fake.calls) == 2
    assert "**required_search_failed: true**" in result


def test_weird_key_persists_after_revision():
    """If the model still returns a non-schema key after self-review, fail structural."""
    fixture = _load_fixture()
    weird = json.dumps({"research_metadata": {"scope": "competitor_research"}, "competitors": ["Xiaomi"]})
    fake = FakeLLM(generate_output=weird, revision_output=weird, annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt)

    assert len(fake.calls) == 2
    assert "**structural_output_failed: true**" in result


def test_evidence_generate_sends_provider_require_parameters():
    """Evidence-mode generate call must send provider.require_parameters=true."""
    fixture = _load_fixture()
    fake = FakeLLM(generate_output=_good_research_response(), annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    agent.run(prompt)

    assert fake.calls[0]["kwargs"].get("provider") == {"require_parameters": True}
    assert fake.calls[0]["kwargs"].get("response_format") == RESEARCH_RESPONSE_SCHEMA


def test_evidence_revise_sends_provider_require_parameters():
    """Evidence-mode revise call must send provider.require_parameters=true."""
    fixture = _load_fixture()
    bad = json.dumps({"wrong_key": "value"})
    fake = FakeLLM(generate_output=bad, revision_output=_good_research_response(), annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    agent.run(prompt)

    assert len(fake.calls) == 3  # 1 generate + 1 revise + 1 semantic review
    assert fake.calls[1]["kwargs"].get("provider") == {"require_parameters": True}
    assert fake.calls[1]["kwargs"].get("response_format") == RESEARCH_RESPONSE_SCHEMA


def test_free_field_accepted_in_open_schema():
    """G2: free-form field like 'design_and_features' is accepted — the
    evidence schema is open and accepts any field label the model returns."""
    fixture = _load_fixture()
    good = json.dumps(
        {
            "target_model": "CACGO K77",
            "competitor_names": ["Xiaomi Watch S3"],
            "evidence": [
                {
                    "competitor": "Xiaomi Watch S3",
                    "field": "design_and_features",
                    "claim": "some claim",
                    "url": "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                    "geography": "thailand",
                },
            ],
            "evidence_based_recommendations": [], "strategic_hypotheses": [],
            "uncertainty": ["ยังไม่พบ"],
        },
        ensure_ascii=False,
    )
    fake = FakeLLM(generate_output=good, annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt)

    # G2: open schema — free-form field is accepted on first try (no revision)
    assert len(fake.calls) == 2  # 1 generate + 1 semantic review
    assert "**structural_output_failed: true**" not in result


def test_canonical_fields_render_two_competitor_facts_and_one_thai():
    """Valid canonical fields must render at least 2 competitor facts and 1 Thai fact."""
    from tests.acceptance_competitor_runner import _check_outcome

    fixture = _load_fixture()
    fake = FakeLLM(generate_output=_good_research_response(), annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    output = agent.run(prompt)

    outcome = _check_outcome({
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
    assert outcome["competitor_fact_cells"] >= 2
    assert outcome["thai_fact_cells"] >= 1
    assert outcome["cited_fact_cells"] >= 2
    assert outcome["no_evidence_ratio"] < 1.0


def test_default_renderer_uses_table():
    """CompetitorReportRenderer defaults to table when no quick_brief requested."""
    fixture = _load_fixture()
    research = ResearchResponse.from_dict(json.loads(_good_research_response()))
    renderer = CompetitorReportRenderer(research, relevant_annotations=fixture["relevant_annotations"])
    output = renderer.render(fixture["product_spec"])
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" in output
    assert "| คุณสมบัติ |" in output


def test_quick_brief_without_non_table_request_stays_table():
    """A generic summary brief must not switch renderer to bullet mode."""
    fixture = _load_fixture()
    research = ResearchResponse.from_dict(json.loads(_good_research_response()))
    renderer = CompetitorReportRenderer(
        research,
        relevant_annotations=fixture["relevant_annotations"],
        quick_brief="สรุปคู่แข่ง",
    )
    output = renderer.render(fixture["product_spec"])
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" in output
    assert "| คุณสมบัติ |" in output


def test_brief_renderer_outputs_bullets_no_table():
    """Quick Brief asking for bullet executive brief renders bullet list with evidence URLs."""
    fixture = _load_fixture()
    research = ResearchResponse.from_dict(json.loads(_good_research_response()))
    renderer = CompetitorReportRenderer(
        research,
        relevant_annotations=fixture["relevant_annotations"],
        quick_brief="สรุปแบบ bullet executive brief ห้ามใช้ตาราง",
    )
    output = renderer.render(fixture["product_spec"])
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" not in output
    assert "| คุณสมบัติ |" not in output
    assert "https://www.siamphone.com/smartwatch/xiaomi/watch-s3" in output
    assert "## สมมติฐานเชิงกลยุทธ์ (ยังไม่ยืนยัน)" in output
    assert "## ข้อจำกัด" in output


def test_brief_renderer_drops_unverified_recommendation_fail_closed():
    """Unvalidated evidence_based_recommendation is dropped fail-closed,
    not promoted to hypothesis — promoting verbatim leaks unsupported factual
    premises (prices, specs, offers) as if they were strategic reasoning."""
    fixture = _load_fixture()
    data = json.loads(_good_research_response())
    data["evidence_based_recommendations"] = [
        {
            "text": "เน้นหน้าจอใหญ่ของ K77",
            "supporting_evidence_urls": ["https://untrusted.example.com/spec"],  # not in annotations
        }
    ]
    research = ResearchResponse.from_dict(data)
    renderer = CompetitorReportRenderer(
        research,
        relevant_annotations=fixture["relevant_annotations"],
        quick_brief="สรุปแบบ bullet executive brief",
    )
    output = renderer.render(fixture["product_spec"])
    assert "## ข้อเสนอแนะที่มีหลักฐานรองรับ" not in output
    # The unverified recommendation must NOT appear anywhere — not as evidence,
    # not as hypothesis.  Promoting it verbatim leaks factual premises.
    assert "เน้นหน้าจอใหญ่ของ K77" not in output


def test_agent_run_respects_quick_brief_and_routes_to_brief():
    """CompetitorAnalysisAgent passes quick_brief to renderer and returns bullet output."""
    fixture = _load_fixture()
    fake = FakeLLM(generate_output=_good_research_response(), annotations=fixture["relevant_annotations"])
    agent = CompetitorAnalysisAgent(_evidence_config(), fake)
    prompt = agent.build_prompt(fixture["product_spec"], fixture["competitor_data"])
    result = agent.run(prompt, quick_brief="สรุปแบบ bullet executive brief ห้ามใช้ตาราง")
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" not in result
    assert "| คุณสมบัติ |" not in result
    assert len(fake.calls) == 2  # 1 generate + 1 semantic review


def _evidence_template(**overrides):
    base = {
        "competitor": "Xiaomi Watch S3",
        "field": "display",
        "claim": '1.43" AMOLED',
        "url": "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
        "geography": "global",
    }
    base.update(overrides)
    return base


def _base_payload(**overrides):
    base = {
        "target_model": "CACGO K77",
        "competitor_names": ["Xiaomi Watch S3"],
        "evidence": [_evidence_template()],
        "evidence_based_recommendations": [
            {
                "text": "เน้นหน้าจอ",
                "supporting_evidence_urls": [_evidence_template()["url"]],
            },
        ],
        "strategic_hypotheses": [],
        "uncertainty": ["ยังไม่พบราคา"],
    }
    for k, v in overrides.items():
        if k == "evidence_append" and k not in base:
            pass
        elif k in base:
            base[k] = v
    return json.dumps(base, ensure_ascii=False)


def test_rejects_more_than_six_evidence():
    agent = CompetitorAnalysisAgent(_evidence_config(), FakeLLM())
    payload = _base_payload(evidence=[_evidence_template() for _ in range(7)])
    ok, err, _ = agent._validate_research_json(payload)
    assert not ok and "more than 6" in err


def test_rejects_too_long_claim():
    agent = CompetitorAnalysisAgent(_evidence_config(), FakeLLM())
    payload = _base_payload(evidence=[_evidence_template(claim="x" * 401)])
    ok, err, _ = agent._validate_research_json(payload)
    assert not ok and "claim too long" in err


def test_rejects_too_many_recommendations():
    agent = CompetitorAnalysisAgent(_evidence_config(), FakeLLM())
    payload = _base_payload(evidence_based_recommendations=[
        {"text": "a", "supporting_evidence_urls": []},
        {"text": "b", "supporting_evidence_urls": []},
        {"text": "c", "supporting_evidence_urls": []},
        {"text": "d", "supporting_evidence_urls": []},
    ])
    ok, err, _ = agent._validate_research_json(payload)
    assert not ok and "evidence_based_recommendations has more than 3" in err


def test_rejects_too_long_uncertainty():
    agent = CompetitorAnalysisAgent(_evidence_config(), FakeLLM())
    payload = _base_payload(uncertainty=["x" * 401])
    ok, err, _ = agent._validate_research_json(payload)
    assert not ok and "uncertainty too long" in err
