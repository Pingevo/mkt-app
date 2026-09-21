"""AGENT2-WEB-STRUCTURAL-01 — competitor identity must be derived from the
canonical structured contract, not from a single fragile field.

Real browser failure: model emitted ``competitor_names: []`` while
``evidence[].competitor`` carried discovered names → validator returned
``structural_output_failed: competitor_names is empty`` → the meta error
report reached final grounding → semantic_failure → user saw no report.

Contract:
- identity set = declared input scope ∪ competitor_names ∪ evidence[].competitor
- validated evidence with named competitors produces a non-empty identity set
- multiple named competitors survive parsing
- empty-of-all-identity remains fail-closed
- malformed structure remains fail-closed
- declared (user-provided) scope still constrains nothing extra when empty
"""
from __future__ import annotations

import json

from src.agents.competitor_analysis import CompetitorAnalysisAgent
from src.agents.competitor_evidence import (
    CompetitorEvidence,
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


def _make_agent(product_spec: str, competitor_data: str) -> CompetitorAnalysisAgent:
    cfg = {"web_search": True, "evidence_mode": True, "max_retry_limit": 0}
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(product_spec, competitor_data)
    return agent


def _research_json(competitor_names, evidence, target="Evo"):
    return json.dumps({
        "target_model": target,
        "competitor_names": competitor_names,
        "evidence": evidence,
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    }, ensure_ascii=False)


def _ev(competitor, url, field="price", claim="found claim", geo="thailand"):
    return {"competitor": competitor, "field": field, "claim": claim,
            "url": url, "geography": geo}


def _relevant_annotation(url, matched_name):
    return {
        "url": url,
        "title": f"{matched_name} product page",
        "content": f"{matched_name} details",
        "_relevance": {
            "relevant": True,
            "relevance_type": "competitor",
            "matched_competitor": matched_name,
            "reason": "test",
            "geography": "thailand",
        },
    }


# ---------------------------------------------------------------------------
# 1. Evidence-carried identity survives an empty competitor_names field
# ---------------------------------------------------------------------------

def test_empty_names_derived_from_named_evidence():
    """competitor_names: [] + evidence[].competitor = ['imoo Z1'] must produce
    a non-empty identity set — the canonical contract carries the name."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo kids smartwatch", "")
    agent._last_relevant_annotations = [
        _relevant_annotation("https://imoo.com/z1", "imoo Z1")
    ]
    payload = _research_json([], [_ev("imoo Z1", "https://imoo.com/z1")])
    ok, err, research = agent._validate_research_json(payload)
    assert ok, f"named evidence must satisfy the identity requirement: {err}"
    assert research.competitor_names == ["imoo Z1"]


def test_multiple_named_competitors_derived_from_evidence():
    """Every distinct evidence[].competitor survives — order preserved."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo kids smartwatch", "")
    agent._last_relevant_annotations = [
        _relevant_annotation("https://imoo.com/z1", "imoo Z1"),
        _relevant_annotation("https://wonlex.com/kt42", "KT42"),
    ]
    payload = _research_json([], [
        _ev("imoo Z1", "https://imoo.com/z1"),
        _ev("KT42", "https://wonlex.com/kt42"),
        _ev("imoo Z1", "https://imoo.com/z1", field="display"),  # dup
    ])
    ok, err, research = agent._validate_research_json(payload)
    assert ok, f"multi-name derivation must pass: {err}"
    assert research.competitor_names == ["imoo Z1", "KT42"]


def test_derived_names_are_canonical_deduped():
    """Case/whitespace variants ('KT42' vs 'kt42', 'imoo  Z1' vs 'imoo Z1')
    collapse via canonical identity — the same identity never appears twice."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    agent._last_relevant_annotations = [
        _relevant_annotation("https://wonlex.com/kt42", "KT42"),
    ]
    payload = _research_json(["KT42", "  kt42  "], [
        _ev("KT42", "https://wonlex.com/kt42"),
        _ev("KT42", "https://wonlex.com/kt42", field="display"),
    ])
    ok, err, research = agent._validate_research_json(payload)
    assert ok, f"canonical dedup must pass: {err}"
    assert research.competitor_names == ["KT42"]


def test_union_declared_plus_evidence_names():
    """Declared scope and evidence-carried names union — declared order first."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    agent._last_relevant_annotations = [
        _relevant_annotation("https://wonlex.com/kt42", "KT42"),
    ]
    payload = _research_json(["imoo Z1"], [_ev("KT42", "https://wonlex.com/kt42")])
    ok, err, research = agent._validate_research_json(payload)
    assert ok, f"union of declared + evidence names must pass: {err}"
    assert set(research.competitor_names) == {"imoo Z1", "KT42"}


def test_non_string_name_items_do_not_crash_and_do_not_count():
    """competitor_names containing non-string items contributes no identity;
    identity must still come from evidence or fail honestly."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    agent._last_relevant_annotations = []
    payload = _research_json([{"name": "imoo Z1"}], [])
    ok, err, research = agent._validate_research_json(payload)
    # no string identity anywhere → honest failure, not a crash
    assert not ok
    assert "competitor_names is empty" in err


# ---------------------------------------------------------------------------
# 2. Fail-closed behavior preserved
# ---------------------------------------------------------------------------

def test_no_identity_anywhere_fails_closed():
    """names=[] + evidence=[] → honest structural failure (nothing to derive)."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    agent._last_relevant_annotations = []
    ok, err, research = agent._validate_research_json(_research_json([], []))
    assert not ok
    assert "competitor_names is empty" in err


def test_malformed_json_still_fails_closed():
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    ok, err, _ = agent._validate_research_json("not json at all")
    assert not ok
    assert "invalid JSON" in err


def test_missing_required_field_still_fails():
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    bad = json.dumps({"target_model": "Evo", "evidence": []})
    ok, err, _ = agent._validate_research_json(bad)
    assert not ok
    assert "missing field" in err


def test_derived_names_do_not_bypass_evidence_provenance():
    """Derived identity does not relax evidence validation — an evidence
    record citing an unverified URL must still fail evidence_validation."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    agent._last_relevant_annotations = []  # nothing verified
    payload = _research_json([], [_ev("imoo Z1", "https://imoo.com/z1")])
    ok, err, research = agent._validate_research_json(payload)
    assert not ok, "unverified URL must still fail even though names derive"
    assert "evidence_validation" in err


# ---------------------------------------------------------------------------
# 3. Reassessment picks up evidence-carried names (annotation relevance)
# ---------------------------------------------------------------------------

def test_reassess_marks_annotations_for_evidence_only_names():
    """Default discovery: names living only in evidence[].competitor must
    still drive annotation re-assessment — otherwise their URLs can never
    be verified provenance."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo kids smartwatch", "")
    agent._evidence_mode = True
    agent._last_annotations = [{
        "url": "https://imoo.com/z1",
        "title": "imoo Z1 kids smartwatch",
        "content": "imoo Z1 with GPS",
    }]
    agent._last_relevant_annotations = []
    agent._last_candidate_annotations = []
    agent._last_rejected_annotations = []

    payload = _research_json([], [_ev("imoo Z1", "https://imoo.com/z1")])
    agent._reassess_for_default_discovery(payload)

    assert agent._relevance_context["competitor_names"] == ["imoo Z1"]
    assert len(agent._last_relevant_annotations) == 1, (
        "annotation matching an evidence-carried name must become relevant"
    )


def test_reassess_string_names_field_does_not_decompose_to_chars():
    """competitor_names emitted as a bare string (contract-invalid) must not
    decompose into per-character identities during reassessment — the field
    is only a name source when it is actually a list."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    agent._last_annotations = [{
        "url": "https://imoo.com/z1",
        "title": "imoo Z1 kids smartwatch",
        "content": "imoo Z1 with GPS",
    }]
    payload = json.dumps({
        "target_model": "Evo",
        "competitor_names": "imoo Z1",  # string, not list
        "evidence": [_ev("imoo Z1", "https://imoo.com/z1")],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    }, ensure_ascii=False)
    agent._reassess_for_default_discovery(payload)
    scope = agent._relevance_context["competitor_names"]
    assert scope == ["imoo Z1"], f"must derive from evidence, not chars: {scope}"


def test_reassess_respects_declared_scope_no_extraction():
    """When the user declared competitor names, reassessment must not run
    (not discovery mode) and must not touch the declared scope."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "imoo Z1")
    agent._last_annotations = []
    payload = _research_json(["OTHER"], [_ev("OTHER", "https://x.com/o")])
    agent._reassess_for_default_discovery(payload)
    assert agent._relevance_context["competitor_names"] == ["imoo Z1"]


# ---------------------------------------------------------------------------
# 4. Revision manifest must show the canonical scope, not "(ไม่ระบุ)"
# ---------------------------------------------------------------------------

def test_manifest_scope_shows_discovered_names():
    """After reassessment, the revision manifest must present the discovered
    names as the scope — telling the model 'names must come from the list'
    while showing an empty list is a self-contradicting instruction."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    agent._relevance_context["competitor_names"] = ["imoo Z1", "KT42"]
    agent._last_relevant_annotations = []
    agent._last_candidate_annotations = []
    manifest = agent._build_evidence_manifest()
    assert "imoo Z1" in manifest
    assert "KT42" in manifest
    assert "(ไม่ระบุ)" not in manifest


def test_manifest_empty_scope_does_not_demand_names_from_empty_list():
    """When nothing has been discovered yet, the manifest must not claim
    names 'must come from the list above' — the list is empty.  It should
    instead tie identity to evidence-carried competitor names."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    agent._last_relevant_annotations = []
    agent._last_candidate_annotations = []
    manifest = agent._build_evidence_manifest()
    assert "competitor_names ต้องมาจากรายชื่อคู่แข่งข้างต้นเสมอ" not in manifest


# ---------------------------------------------------------------------------
# 5. Grounding still rejects meta/error reports (boundary intact)
# ---------------------------------------------------------------------------

def test_error_report_is_still_rejected_by_grounding_semantics():
    """The structural failure report text must remain a rejectable candidate
    at the grounding boundary — this fix must not make meta reports pass."""
    agent = _make_agent("รหัสสินค้า: Evo\nLAGENIO Evo", "")
    report = agent._structural_output_failure("competitor_names is empty")
    assert "structural_output_failed" in report
    assert "รายงานวิเคราะห์ไม่สำเร็จ" in report


# ---------------------------------------------------------------------------
# AGENT2-PRODUCT-IDENTITY-CLOSEOUT-01 — runtime product identity binding
#
# our_product = runtime-selected product (product_id / folder name).
# target_model = model-emitted research field — must never override it.
# ---------------------------------------------------------------------------


def _render_research(target="KT31"):
    """Minimal validated research: model named the target KT31 while the
    runtime-selected product is something else (e.g. Lagenio Evo)."""
    return ResearchResponse(
        target_model=target,
        competitor_names=["imoo Z1"],
        evidence=[
            CompetitorEvidence(
                competitor="imoo Z1", field="price",
                claim="imoo Z1 ราคา 2,999 บาท",
                url="https://imoo.com/z1", geography="thailand",
            )
        ],
        evidence_based_recommendations=[],
        strategic_hypotheses=[],
        uncertainty=[],
    )


def _imoo_annotation():
    return [{
        "url": "https://imoo.com/z1",
        "title": "imoo Z1 official",
        "content": "imoo Z1 price",
        "_relevance": {
            "relevant": True, "relevance_type": "competitor",
            "matched_competitor": "imoo Z1", "reason": "t", "geography": "thailand",
        },
    }]


def test_renderer_title_and_column_use_runtime_identity():
    """Runtime product 'Lagenio Evo' + model target_model 'KT31' → report
    must display Lagenio Evo as ours; KT31 must not head the report."""
    renderer = CompetitorReportRenderer(
        _render_research("KT31"), relevant_annotations=_imoo_annotation(),
        our_product="Lagenio Evo",
    )
    out = renderer.render("รหัสสินค้า: KT31\nLAGENIO Evo 4G")
    title = out.splitlines()[0]
    assert "Lagenio Evo" in title
    assert "KT31" not in title
    header = next(l for l in out.splitlines() if l.startswith("| คุณสมบัติ"))
    assert "Lagenio Evo (สินค้าของเรา)" in header
    assert "| KT31 |" not in header and "| KT31 (" not in header


def test_model_named_target_may_still_appear_as_competitor_column():
    """A model-emitted name that conflicts with runtime identity may still
    appear as a competitor when evidence supports it — competitors keep
    separate columns."""
    research = _render_research("KT31")
    research.competitor_names = ["KT31", "imoo Z1"]
    research.evidence.append(CompetitorEvidence(
        competitor="KT31", field="price", claim="KT31 ราคา 3,273 บาท",
        url="https://wonlex.com/kt31", geography="global",
    ))
    anns = _imoo_annotation() + [{
        "url": "https://wonlex.com/kt31",
        "title": "Wonlex KT31", "content": "KT31 details",
        "_relevance": {
            "relevant": True, "relevance_type": "competitor",
            "matched_competitor": "KT31", "reason": "t", "geography": "global",
        },
    }]
    renderer = CompetitorReportRenderer(research, relevant_annotations=anns,
                                      our_product="Lagenio Evo")
    out = renderer.render("รหัสสินค้า: KT31\nLAGENIO Evo 4G")
    header = next(l for l in out.splitlines() if l.startswith("| คุณสมบัติ"))
    assert "Lagenio Evo (สินค้าของเรา)" in header
    assert "| KT31 |" in header  # competitor column preserved
    assert "imoo Z1" in header


def test_missing_runtime_identity_falls_back_without_competitor_adoption():
    """No runtime identity → fallback must never pick a competitor name as
    'our product'.  Model target_model remains the standalone fallback."""
    renderer = CompetitorReportRenderer(
        _render_research("KT31"), relevant_annotations=_imoo_annotation(),
    )
    out = renderer.render("รหัสสินค้า: KT31")
    header = next(l for l in out.splitlines() if l.startswith("| คุณสมบัติ"))
    assert "(สินค้าของเรา)" in header
    # the our-product column is never 'imoo Z1'
    assert "imoo Z1 (สินค้าของเรา)" not in header


def test_missing_all_identity_uses_safe_label_not_competitor():
    """No runtime identity AND empty target_model → safe generic label,
    never a competitor name."""
    research = _render_research("")
    renderer = CompetitorReportRenderer(
        research, relevant_annotations=_imoo_annotation(), our_product="",
    )
    out = renderer.render("")
    header = next(l for l in out.splitlines() if l.startswith("| คุณสมบัติ"))
    assert "imoo Z1 (สินค้าของเรา)" not in header
    assert "(สินค้าของเรา)" in header


def test_validator_accepts_runtime_name_as_target_identity():
    """When the model emits the runtime product name (Lagenio Evo) instead of
    the spec-derived code (KT31), the target-unchanged check must accept it —
    both refer to our product."""
    agent = _make_agent("รหัสสินค้า: KT31\nLAGENIO Evo kids smartwatch", "")
    agent._our_product = "Lagenio Evo"
    agent._last_relevant_annotations = [_imoo_annotation()[0]]
    payload = _research_json(["imoo Z1"],
                             [_ev("imoo Z1", "https://imoo.com/z1")],
                             target="Lagenio Evo")
    ok, err, research = agent._validate_research_json(payload)
    assert ok, f"runtime product name must be accepted as target: {err}"


def test_validator_still_rejects_unrelated_target():
    """Model drifting to an unrelated product still fails — runtime binding
    must not weaken the wrong-product guard."""
    agent = _make_agent("รหัสสินค้า: KT31\nLAGENIO Evo kids smartwatch", "")
    agent._our_product = "Lagenio Evo"
    agent._last_relevant_annotations = [_imoo_annotation()[0]]
    payload = _research_json(["imoo Z1"],
                             [_ev("imoo Z1", "https://imoo.com/z1")],
                             target="Samsung Galaxy Watch")
    ok, err, _ = agent._validate_research_json(payload)
    assert not ok
    assert "target_model changed" in err
