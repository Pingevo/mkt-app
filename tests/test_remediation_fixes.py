"""Targeted tests for the 3 generic production fixes:
1. Streaming cost extraction in _chat_stream
2. Truncation guard in BrandInterpretationPass
3. Grounding policy in review phase
"""
import json
from unittest.mock import MagicMock, patch
from pathlib import Path

import sys
sys.path.insert(0, "/Users/its-dev2/MKTApp")


# --- Fix 1: Streaming cost extraction ---
def test_streaming_cost_extracted_from_usage():
    """_chat_stream should set _last_cost_usd from usage.cost in the final chunk.

    Tests the actual _chat_stream method by mocking _stream_attempt to emit
    a usage chunk with a cost field, then verifies the observable client
    state (_last_cost_usd) is set — not by reproducing the assignment logic.
    """
    from src.llm_client import LLMClient

    client = LLMClient(api_key="test-key", default_model="test-model")
    client._last_cost_usd = None  # ensure clean state

    # Mock _stream_attempt to emit content + a usage chunk with cost
    def fake_stream_attempt(payload, attempt_timeout, progress_timeout):
        yield ("content", "hello")
        yield ("usage", {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.00234})
        yield ("finish", "stop")
        yield ("request_id", "req-123")

    client._stream_attempt = fake_stream_attempt

    # Call the actual _chat_stream method
    text, usage, req_id = client._chat_stream({"model": "test", "messages": []})

    # Verify observable client state was set by _chat_stream itself
    assert client._last_cost_usd == 0.00234, (
        f"Expected _last_cost_usd=0.00234, got {client._last_cost_usd}"
    )
    assert client._last_finish_reason == "stop"
    assert text == "hello"


def test_streaming_cost_none_when_no_cost_in_usage():
    """_chat_stream should leave _last_cost_usd unchanged when usage has no cost.

    Tests the actual _chat_stream method — verifies _last_cost_usd stays None
    when the usage chunk omits the cost field.
    """
    from src.llm_client import LLMClient

    client = LLMClient(api_key="test-key", default_model="test-model")
    client._last_cost_usd = None

    def fake_stream_attempt(payload, attempt_timeout, progress_timeout):
        yield ("content", "hello")
        yield ("usage", {"prompt_tokens": 100, "completion_tokens": 50})  # no cost
        yield ("finish", "stop")

    client._stream_attempt = fake_stream_attempt

    text, usage, req_id = client._chat_stream({"model": "test", "messages": []})

    assert client._last_cost_usd is None, (
        f"Expected None, got {client._last_cost_usd}"
    )


# --- Fix 2: Truncation guard in BrandInterpretationPass ---
def test_brand_interpretation_returns_empty_on_truncation():
    """BrandInterpretationPass should return [] when finish_reason=length."""
    from src.agents.competitor_evidence import BrandInterpretationPass, ResearchResponse, CompetitorEvidence

    # Create a mock LLM that returns a truncated response
    mock_llm = MagicMock()
    mock_llm.chat.return_value = '[{"evidence_ref": 0, "implication": "truncated'
    mock_llm.last_truncated = True

    research = ResearchResponse(
        target_model="K5",
        competitor_names=["CompA"],
        evidence=[CompetitorEvidence(competitor="CompA", field="price", claim="100 THB", url="http://x")],
    )

    brand_interp = BrandInterpretationPass(llm=mock_llm, config={"model": "test"})
    result = brand_interp.interpret(research, brand_reference="premium brand")

    assert result == [], f"Expected [] for truncated response, got {result}"


def test_brand_interpretation_parses_non_truncated_response():
    """BrandInterpretationPass should parse valid JSON when not truncated."""
    from src.agents.competitor_evidence import BrandInterpretationPass, ResearchResponse, CompetitorEvidence

    mock_llm = MagicMock()
    mock_llm.chat.return_value = json.dumps([
        {"evidence_ref": 0, "implication": "test implication", "category": "recommendation"}
    ])
    mock_llm.last_truncated = False

    research = ResearchResponse(
        target_model="K5",
        competitor_names=["CompA"],
        evidence=[CompetitorEvidence(competitor="CompA", field="price", claim="100 THB", url="http://x")],
    )

    brand_interp = BrandInterpretationPass(llm=mock_llm, config={"model": "test"})
    result = brand_interp.interpret(research, brand_reference="premium brand")

    assert len(result) == 1, f"Expected 1 implication, got {len(result)}"
    assert result[0].implication == "test implication"


# --- Fix 3: Grounding policy in review phase ---
def test_grounding_policy_included_in_review_message():
    """_review_and_refine should include grounding_policy block when configured."""
    from src.agents.base_agent import BaseAgent

    # Create a minimal agent with grounding_policy
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test_agent"
    agent.config = {
        "grounding_policy": {
            "categories": ["supplied_fact", "inference_or_recommendation"],
        },
        "review_prompt": "test review prompt",
        "review_temperature": 0.2,
        "model": "test-model",
        "max_review_iterations": 1,
        "max_tokens": 4096,
        "max_retry_limit": 3,
        "stream": False,
    }
    agent.instructions = {}

    # Mock the LLM to capture the review message
    captured_messages = []

    def mock_chat(messages, **kwargs):
        captured_messages.append(messages)
        return "reviewed output"

    agent.llm = MagicMock()
    agent.llm.chat = mock_chat
    agent.llm.last_truncated = False
    agent._build_multimodal_content = lambda text, imgs: text

    # Call _review_and_refine
    agent._review_and_refine(
        output="test output",
        system_prompt="test system prompt",
        user_prompt="test user prompt with source data",
    )

    # Check that the review message includes grounding policy
    review_msg = captured_messages[0][1]["content"]  # user message
    assert "GROUNDING POLICY" in review_msg, f"GROUNDING POLICY not found in review message"
    assert "supplied facts" in review_msg, f"supplied facts not found"
    assert "inference/recommendation" in review_msg, f"inference/recommendation not found"


def test_no_grounding_section_when_no_policy():
    """_review_and_refine should not include grounding section when no grounding_policy."""
    from src.agents.base_agent import BaseAgent

    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test_agent"
    agent.config = {
        "review_prompt": "test review prompt",
        "review_temperature": 0.2,
        "model": "test-model",
        "max_review_iterations": 1,
        "max_tokens": 4096,
        "max_retry_limit": 3,
        "stream": False,
    }
    agent.instructions = {}

    captured_messages = []

    def mock_chat(messages, **kwargs):
        captured_messages.append(messages)
        return "reviewed output"

    agent.llm = MagicMock()
    agent.llm.chat = mock_chat
    agent.llm.last_truncated = False
    agent._build_multimodal_content = lambda text, imgs: text

    agent._review_and_refine(
        output="test output",
        system_prompt="test system prompt",
        user_prompt="test user prompt",
    )

    review_msg = captured_messages[0][1]["content"]
    # The grounding section block should not appear when no grounding_policy
    assert "--- GROUNDING POLICY (ใช้ตรวจข้อเท็จจริง) ---" not in review_msg
    assert "นโยบายข้อมูลต้นทาง (Grounding Policy)" not in review_msg


# --- Fix 4: Post-review mutation seam (script_reviewer grounding) ---
def test_script_reviewer_includes_source_context_when_provided():
    """review_script should include source context in the system prompt when provided."""
    from src.script_reviewer import review_script

    mock_llm = MagicMock()
    mock_llm.chat.return_value = json.dumps({
        "score": 80,
        "component_scores": {"hook": 20, "pacing": 20, "clarity": 20, "engagement": 20},
        "issues": [],
        "suggested_hooks": [],
        "revised_script": "test",
    })
    mock_llm.last_truncated = False

    review_script(
        "0:00-0:05 Scene: test",
        "TikTok",
        mock_llm,
        source_context="สเปคสินค้า: ราคา 99 บาท กล้อง 0.3MP",
    )

    # The system prompt sent to the LLM should contain the source context
    sent_messages = mock_llm.chat.call_args[0][0]
    system_msg = sent_messages[0]["content"]
    assert "ข้อมูลต้นทาง" in system_msg, "source context section not found in system prompt"
    assert "ราคา 99 บาท" in system_msg, "source facts not passed through"
    assert "ห้ามเพิ่มข้อเท็จจริง" in system_msg, "grounding rule not found"


def test_script_reviewer_no_source_section_when_empty():
    """review_script should not include source section when source_context is empty."""
    from src.script_reviewer import review_script

    mock_llm = MagicMock()
    mock_llm.chat.return_value = json.dumps({
        "score": 80,
        "component_scores": {"hook": 20, "pacing": 20, "clarity": 20, "engagement": 20},
        "issues": [],
        "suggested_hooks": [],
        "revised_script": "test",
    })
    mock_llm.last_truncated = False

    review_script("0:00-0:05 Scene: test", "TikTok", mock_llm)

    sent_messages = mock_llm.chat.call_args[0][0]
    system_msg = sent_messages[0]["content"]
    assert "ข้อมูลต้นทาง" not in system_msg, "source section should not appear when empty"


# --- Fix 5: Agent 2 fail-closed drop of unverified evidence_based recommendations ---
def test_failed_evidence_recommendations_dropped_not_promoted():
    """CompetitorReportRenderer should drop (not promote) recommendations
    whose supporting URLs fail validation.  Promoting them verbatim to
    strategic_hypotheses leaks unsupported factual premises (prices, specs)."""
    from src.agents.competitor_evidence import (
        CompetitorReportRenderer,
        ResearchResponse,
        CompetitorEvidence,
        EvidenceBasedRecommendation,
        StrategicHypothesis,
    )

    research = ResearchResponse(
        target_model="K5",
        competitor_names=["CompA"],
        evidence=[
            CompetitorEvidence(competitor="CompA", field="price", claim="100 THB",
                              url="http://valid.example.com"),
        ],
        evidence_based_recommendations=[
            EvidenceBasedRecommendation(
                text="ตั้งราคา K5 ที่ 99 ยูโร (3,600 บาท) เพราะคู่แข่งอยู่ที่ 7,099 บาท",
                supporting_evidence_urls=["http://invalid.example.com"],  # URL fails validation
            ),
        ],
        strategic_hypotheses=[
            StrategicHypothesis(text="ควรเน้นความคุ้มค่า", rationale="ราคาต่ำกว่าคู่แข่ง"),
        ],
    )

    renderer = CompetitorReportRenderer(research, relevant_annotations=[])
    evidence_based, all_hypotheses = renderer._classify_recommendations()

    # The failed-evidence recommendation should NOT appear in hypotheses
    assert len(evidence_based) == 0, "no recommendation should survive (URL invalid)"
    assert len(all_hypotheses) == 1, "only the original model-authored hypothesis should remain"
    assert "99 ยูโร" not in all_hypotheses[0].text, (
        "unsupported factual premise leaked into hypotheses"
    )


# --- Fix 6: Fail-closed when second script review cannot verify revision ---
def test_review_script_in_posts_fails_closed_on_empty_second_review(monkeypatch):
    """If the second review_script returns empty (cannot verify), the orchestrator
    should retain the original grounded script rather than persist unchecked mutation."""
    from src.orchestrator import Orchestrator
    from src import script_reviewer

    call_count = [0]

    def fake_review(script, platform, llm=None, source_context=""):
        call_count[0] += 1
        if call_count[0] == 1:
            # First review: low score, suggests a revision
            return {
                "score": 50,
                "revised_script": "revised with unsupported claim",
                "issues": ["weak hook"],
                "suggested_hooks": [],
            }
        else:
            # Second review: returns empty (cannot verify)
            return {}

    original = script_reviewer.review_script
    script_reviewer.review_script = fake_review
    try:
        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {}
        posts = [{"script": "original grounded script", "video_prompts": [], "platform": "TikTok"}]
        llm = MagicMock()
        llm.last_truncated = False

        result = orch._review_script_in_posts(posts, "TikTok", llm=llm)

        # The original script should be retained, not the revised one
        assert posts[0]["script"] == "original grounded script", (
            f"Expected original script retained, got: {posts[0]['script']}"
        )
        assert "unsupported claim" not in posts[0]["script"]
    finally:
        script_reviewer.review_script = original


def test_review_script_in_posts_persists_accepted_revision(monkeypatch):
    """When round-1 review is below threshold and round-2 verifies the revision
    higher, the ACCEPTED revision (round-1 revised_script) must land in the post —
    the original must not survive, and the round-2 reviewer's further suggestion
    (unverified, loop ended) must not be applied either."""
    from src.orchestrator import Orchestrator
    from src import script_reviewer

    call_count = [0]

    def fake_review(script, platform, llm=None, source_context=""):
        call_count[0] += 1
        if call_count[0] == 1:
            # Round 1: below threshold (68 < 70), proposes revision v2
            return {
                "score": 68,
                "revised_script": "REVISED_V2",
                "issues": [{"timestamp": "0:03", "problem": "p", "fix": "f"}],
                "suggested_hooks": [],
            }
        # Round 2: verifies v2 higher (78), suggests further v3
        return {
            "score": 78,
            "revised_script": "SUGGESTED_V3",
            "issues": [],
            "suggested_hooks": [],
        }

    monkeypatch.setattr(script_reviewer, "review_script", fake_review)
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = {}
    posts = [{"script": "ORIGINAL", "video_prompts": [], "platform": "TikTok"}]
    llm = MagicMock()
    llm.last_truncated = False

    orch._review_script_in_posts(posts, "TikTok", llm=llm)

    assert posts[0]["script"] == "REVISED_V2", (
        f"accepted revision must be the final output, got: {posts[0]['script']!r}"
    )
    sr = posts[0]["script_review"]
    assert sr["script_changed"] is True
    assert sr["score"] == 78
    assert sr["review"]["revised_script"] == "SUGGESTED_V3"


# ---------------------------------------------------------------------------
# AGENT2-EVIDENCE-01 — flattened spec-table rows must not leak or starve
# the comparison table.  The real K2 record reached Agent 2 with flattened
# pipe lines only (no positioned tables): _product_cells prefix-matched a
# raw line and dumped it whole into the cell ("Display | Type | AMOLED"),
# picked the first "camera"-prefixed row (Back Camera | No) while the front
# camera row went unseen, and missed "Other | Waterproof level | IP68"
# because its canon starts with "other".
# ---------------------------------------------------------------------------

_K2_SHAPED_SPEC = """\
MODEL NAME | K2
APP | Lagenio
Display | Type | AMOLED
 | Size | 1.78inch
 | Resolution | 368 x 448 pixels
 | Glass manufacturer | Truly
Camera | Back Camera | No
 | Front Camera | YES | 5MP
Battery | Capacity | 680mAh
 | Standby time | 3-5 days
 | Charging | Magnetic
Other | Waterproof level | IP68
 | Material | Silicone strap
Software | OS | Android 8.1
 | Language | Multi-language
 | Requirement | Customized
"""


def _renderer():
    from src.agents.competitor_evidence import CompetitorReportRenderer
    return CompetitorReportRenderer.__new__(CompetitorReportRenderer)


def test_product_cells_never_leak_raw_pipe_rows():
    """Flattened 'section | feature | value' rows must render as the VALUE,
    never the raw source line — the K2 run leaked 'Display | Type | AMOLED'
    verbatim into the user-facing comparison table."""
    r = _renderer()
    fields = (
        ("display", "Display"),
        ("waterproof", "Waterproof"),
        ("battery", "Battery"),
    )
    cells = r._product_cells(_K2_SHAPED_SPEC, fields)
    for canon in ("display", "waterproof", "battery"):
        assert "|" not in cells[canon], f"{canon} leaked raw pipe syntax: {cells[canon]!r}"
    assert "AMOLED" in cells["display"]
    assert "IP68" in cells["waterproof"]
    assert "680mAh" in cells["battery"]


def test_product_cells_match_label_inside_section_path():
    """Field canon may sit inside a section-prefixed label path:
    'Other | Waterproof level | IP68' must match field 'waterproof'."""
    r = _renderer()
    cells = r._product_cells(_K2_SHAPED_SPEC, (("waterproof", "Waterproof"),))
    assert cells["waterproof"] == "IP68"


def test_product_cells_ambiguous_sub_keys_are_merged_not_first_match():
    """K2's camera rows are 'Camera | Back Camera | No' and
    '| Front Camera | YES | 5MP'.  Picking the first prefix match compares
    a rear camera against competitors' front cameras — every matching row
    must be surfaced with its sub-label instead."""
    r = _renderer()
    cells = r._product_cells(_K2_SHAPED_SPEC, (("camera", "Camera"),))
    cell = cells["camera"]
    assert "No" in cell and "5MP" in cell, \
        f"both camera rows must surface, got: {cell!r}"
    assert "Back Camera" in cell and "Front Camera" in cell, \
        f"sub-labels must disambiguate which camera each value is: {cell!r}"


def test_extract_source_facts_sparse_hierarchy_block():
    """Flattened pipe text without positioned tables: a block mixing
    two-cell continuations with wider rows is a section/feature/value
    hierarchy — wide rows extract as label-path = last-cell value."""
    from src.ingestion import extract_source_facts
    facts = extract_source_facts(_K2_SHAPED_SPEC, None, "spec.txt")
    labels = {f["label"]: f["value"] for f in facts.values()}
    assert labels.get("Display Type") == "AMOLED"
    assert labels.get("Other Waterproof level") == "IP68"
    assert labels.get("Battery Capacity") == "680mAh"
    assert labels.get("Camera Back Camera") == "No"
    assert labels.get("Size") == "1.78inch"


def test_extract_source_facts_dense_wide_block_fails_closed():
    """A pipe block of uniformly wide rows has no hierarchy evidence — it
    is a dense comparison grid and must not be guessed into facts."""
    from src.ingestion import extract_source_facts
    text = (
        "Spec | Model A | Model B\n"
        "Battery | 680mAh | 800mAh\n"
        "Camera | 5MP | 13MP\n"
        "OS | Android | Android\n"
    )
    facts = extract_source_facts(text, None, "cmp.txt")
    assert not facts, f"dense comparison rows must stay evidence, not facts: {facts}"


def test_staged_commit_preserves_positioned_tables(brand_ws):
    """The staging/segmentation path must attach positioned ``tables`` to
    text_extracts exactly like ingest_file does — without it, a sparse
    spec sheet loses every category-header row from derived_facts (the
    real K2 record came through this path with ``tables`` absent)."""
    import io
    import openpyxl
    from src import staging, product_db

    wb = openpyxl.Workbook()
    ws = wb.active
    for r in (["Display", "Type", "AMOLED"], ["", "Size", "1.78inch"],
              ["Other", "Waterproof level", "IP68"], ["", "Material", "Silicone"],
              ["Battery", "680mAh", ""]):
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)

    batch_id = staging.create_batch([("spec.xlsx", buf.getvalue())])
    staging.run_segmentation(batch_id)
    out = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "StagedTables"}])
    assert out["created"] == ["StagedTables"]

    rec = product_db.load("StagedTables")
    extracts = rec.get("text_extracts") or []
    assert any(t.get("tables") for t in extracts), \
        "staged text_extracts must carry positioned tables like ingest does"
    labels = {f["label"]: f["value"]
              for f in (rec.get("derived_facts") or {}).values()}
    assert labels.get("Display Type") == "AMOLED"
    assert labels.get("Other Waterproof level") == "IP68"


def test_grounding_contract_includes_claim_strength_rule():
    """Shared grounding gate (agents 1–4) must reject claims stated MORE
    strongly than their evidence — the K2 review found conclusions and
    publishable claims that outran the product facts.  The rule lives in
    the shared verifier prompt; this pins the contract."""
    from src.agents.base_agent import BaseAgent

    agent = BaseAgent.__new__(BaseAgent)
    agent.config = {}
    agent.agent_name = "test"
    captured = {}

    class _LLM:
        def chat(self, messages, **kw):
            captured["system"] = messages[0]["content"]
            return '{"grounded": true, "unsupported_claims": []}'

    agent.llm = _LLM()
    agent.verify_final_grounding("some output text", {"product_source": "spec"})

    sys_prompt = captured["system"]
    assert "ไม่แรงกว่าหลักฐาน" in sys_prompt, \
        "claim-strength rule missing from shared grounding contract"


def test_product_cells_dedupe_identical_fact_representations():
    """The same fact reachable via two source shapes (derived-fact colon
    line + flattened spec-table pipe row) must not duplicate in the cell —
    distinct rows (front vs back camera) still merge once each."""
    r = _renderer()
    spec = _K2_SHAPED_SPEC + (
        "Battery Capacity: 680mAh\nWaterproof level: IP68\n"
        "Camera Back Camera: No\nFront Camera YES: 5MP\n")
    cells = r._product_cells(
        spec, (("battery", "Battery"), ("waterproof", "Waterproof"), ("camera", "Camera")))
    assert cells["battery"] == "680mAh", f"duplicate pair must collapse: {cells['battery']!r}"
    assert cells["waterproof"] == "IP68"
    assert cells["camera"].count("Back Camera") == 1, \
        f"identical merged pairs must not repeat: {cells['camera']!r}"
    assert "5MP" in cells["camera"] and "No" in cells["camera"]


# ---------------------------------------------------------------------------
# Confirmed capability record must reach the agent-facing product context
# (Evo launch defect: a user-confirmed capability lived only in the accepted
# enrichment profile — never in spec-table facts — so it never reached Agent 1
# and the model filled the gap from mistranslated marketplace text.)
# ---------------------------------------------------------------------------

def _ctx_product(brand_ws, pid, raw_text="", profile=None):
    """Persist a minimal product record (+ optional confirmed profile) and
    return the composed agent context text."""
    from src import product_db
    cache_dir = brand_ws["brand_root"] / "cache" / pid
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "product.json").write_text(json.dumps(
        {"product_id": pid, "raw_text": raw_text}, ensure_ascii=False),
        encoding="utf-8")
    if profile is not None:
        (cache_dir / "product_profile.json").write_text(json.dumps(
            profile, ensure_ascii=False), encoding="utf-8")
    return product_db.get_agent_context_text(pid)


def test_confirmed_profile_fields_reach_agent_context(brand_ws):
    """User-confirmed capability record (summary + capability-bearing
    differentiators/use_cases from the accepted enrichment profile) must be
    part of the product context every agent sees — not only spec-table
    facts."""
    ctx = _ctx_product(brand_ws, "P1", raw_text="raw specs here", profile={
        "summary": "CONFIRMED_SUMMARY_SENTINEL",
        "differentiators": ["CONFIRMED_CAPABILITY_SENTINEL"],
        "use_cases": ["CONFIRMED_USECASE_SENTINEL"],
    })
    assert "CONFIRMED_SUMMARY_SENTINEL" in ctx
    assert "CONFIRMED_CAPABILITY_SENTINEL" in ctx
    assert "CONFIRMED_USECASE_SENTINEL" in ctx


def test_confirmed_capability_survives_verbatim_with_same_meaning(brand_ws):
    """A confirmed capability string must reach the context exactly as
    confirmed — the composition seam must not paraphrase, substitute, or
    drop it."""
    capability = "ติดต่อผ่านการโทรวิดีโอโดยตรงบนตัวนาฬิกา"
    ctx = _ctx_product(brand_ws, "P1", profile={
        "summary": "Kids smartwatch",
        "differentiators": [capability],
    })
    assert capability in ctx, \
        "confirmed capability text must reach the context unchanged"


def test_confirmed_record_labeled_and_precedes_raw_data(brand_ws):
    """The confirmed record must be labeled as user-confirmed (not raw
    extraction) and positioned before the raw data block — same trust tier
    as verified facts, so it overrides noisy/mistranslated raw phrasing."""
    ctx = _ctx_product(brand_ws, "P1", raw_text="RAW_DATA_SENTINEL", profile={
        "summary": "CONFIRMED_SUMMARY_SENTINEL",
        "differentiators": ["CONFIRMED_CAPABILITY_SENTINEL"],
    })
    assert "ยืนยัน" in ctx, "confirmed record must be labeled as confirmed"
    assert ctx.index("CONFIRMED_SUMMARY_SENTINEL") < ctx.index("RAW_DATA_SENTINEL"), \
        "confirmed record must precede raw data (higher precedence)"


def test_confirmed_record_carries_preservation_contract(brand_ws):
    """The confirmed block must carry the generic semantic contract:
    preserve confirmed capability meaning, do not substitute another
    capability, do not drop confirmed capabilities."""
    ctx = _ctx_product(brand_ws, "P1", profile={
        "summary": "S", "differentiators": ["C"]})
    assert "คงความหมาย" in ctx or "ความหมาย" in ctx, \
        "confirmed record must instruct meaning preservation"
    assert "ห้ามแทนที่" in ctx, \
        "confirmed record must forbid capability substitution"


def test_no_confirmed_block_when_profile_absent(brand_ws):
    """Products without an accepted profile (or an empty one) must render
    exactly as before — no phantom confirmed block."""
    ctx = _ctx_product(brand_ws, "P1", raw_text="raw only")
    assert "ยืนยัน" not in ctx
    ctx2 = _ctx_product(brand_ws, "P2", raw_text="raw only", profile={
        "target_audience": "parents", "price_tier": "budget"})
    assert "ยืนยัน" not in ctx2, \
        "profile without capability-bearing fields must not render a confirmed block"


def test_positioning_fields_stay_out_of_product_context(brand_ws):
    """Positioning fields (audience, competitors, price_tier, tone, visual)
    are marketing positioning delivered via the brand-reference channel —
    they must not be dumped into the product-facts context."""
    ctx = _ctx_product(brand_ws, "P1", raw_text="raw", profile={
        "summary": "S", "differentiators": ["C"],
        "target_audience": "POSITIONING_AUDIENCE_SENTINEL",
        "competitors": ["POSITIONING_COMPETITOR_SENTINEL"],
        "price_tier": "POSITIONING_PRICE_SENTINEL",
        "tone_adjustment": "POSITIONING_TONE_SENTINEL",
        "visual_override": "POSITIONING_VISUAL_SENTINEL",
    })
    for sentinel in ("POSITIONING_AUDIENCE_SENTINEL", "POSITIONING_COMPETITOR_SENTINEL",
                     "POSITIONING_PRICE_SENTINEL", "POSITIONING_TONE_SENTINEL",
                     "POSITIONING_VISUAL_SENTINEL"):
        assert sentinel not in ctx


def test_multimodal_context_includes_confirmed_record(brand_ws):
    """The multimodal context path (get_agent_context) must carry the same
    confirmed record — image-bearing products take this path."""
    from src import product_db
    cache_dir = brand_ws["brand_root"] / "cache" / "P1"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "product.json").write_text(json.dumps(
        {"product_id": "P1", "raw_text": "raw"}, ensure_ascii=False), encoding="utf-8")
    (cache_dir / "product_profile.json").write_text(json.dumps(
        {"summary": "S", "differentiators": ["CONFIRMED_CAPABILITY_SENTINEL"]},
        ensure_ascii=False), encoding="utf-8")
    ctx = product_db.get_agent_context("P1")
    assert "CONFIRMED_CAPABILITY_SENTINEL" in ctx["text"]


def test_raw_capability_not_promoted_into_confirmed_block(brand_ws):
    """A capability that exists only in raw text must NOT appear inside the
    confirmed-record block — raw noise must never be promoted to the
    confirmed tier.  It may still appear in the raw section below."""
    ctx = _ctx_product(brand_ws, "P1",
                       raw_text="RAW_ONLY_CAPABILITY_SENTINEL in the listing",
                       profile={"summary": "S", "differentiators": ["CONFIRMED_CAPABILITY_SENTINEL"]})
    confirmed_section = ctx.split("สิ้นสุดข้อมูลที่ยืนยัน")[0]
    assert "CONFIRMED_CAPABILITY_SENTINEL" in confirmed_section
    assert "RAW_ONLY_CAPABILITY_SENTINEL" not in confirmed_section, \
        "raw-only capability must not be promoted into the confirmed record"


def test_explicit_limitation_renders_verbatim(brand_ws):
    """An explicit limitation the user confirmed (e.g. a missing capability)
    must reach the context verbatim — confirmed limitations are facts too."""
    ctx = _ctx_product(brand_ws, "P1", profile={
        "summary": "S",
        "differentiators": ["LIMITATION_SENTINEL: ไม่มีผู้ช่วยเสียงในตัว"],
    })
    assert "LIMITATION_SENTINEL: ไม่มีผู้ช่วยเสียงในตัว" in ctx


def test_profile_list_fields_coerce_bare_strings(brand_ws):
    """Profile JSON is user-writable — a bare string differentiators field
    must become one item, not explode into per-character joins."""
    ctx = _ctx_product(brand_ws, "P1", profile={
        "summary": "S", "differentiators": "SINGLE_CAPABILITY_STRING"})
    assert "SINGLE_CAPABILITY_STRING" in ctx
    assert "S, I, N" not in ctx, \
        "bare string must be one item, not joined per-character"
