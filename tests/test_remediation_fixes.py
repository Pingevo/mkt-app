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
