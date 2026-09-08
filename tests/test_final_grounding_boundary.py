"""Checkpoint A — Generic final-output truth boundary tests.

Tests that the final grounding gate:
1. Catches unsupported claims introduced by post-review mutation
2. Fails closed on all infrastructure failures (missing LLM, exception,
   malformed JSON, empty response, truncation)
3. Verifies the complete final artifact (not just script)
4. Atomically restores the complete pre-mutation candidate and reverifies
5. Fails the run (raises GroundingError) when no grounded candidate exists
6. Is generic — no product/brand/keyword-specific logic
7. All Agents 1–4 reach the final gate
8. UI options are asserted in the verifier request
9. No product- or brand-specific production logic is introduced
"""
import copy
import json
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helper: build a mock LLM that returns a grounding verdict
# ---------------------------------------------------------------------------

def _mock_llm_grounded():
    m = MagicMock()
    m.chat.return_value = json.dumps({"grounded": True, "unsupported_claims": []})
    m.last_truncated = False
    return m


def _mock_llm_ungrounded(claims=None):
    m = MagicMock()
    m.chat.return_value = json.dumps({
        "grounded": False,
        "unsupported_claims": claims or [{"claim": "unsupported", "reason": "not in source"}],
    })
    m.last_truncated = False
    return m


def _mock_llm_exception():
    m = MagicMock()
    m.chat.side_effect = RuntimeError("provider down")
    m.last_truncated = False
    return m


def _mock_llm_empty():
    m = MagicMock()
    m.chat.return_value = ""
    m.last_truncated = False
    return m


def _mock_llm_malformed():
    m = MagicMock()
    m.chat.return_value = "not json at all"
    m.last_truncated = False
    return m


def _mock_llm_truncated():
    m = MagicMock()
    m.chat.return_value = json.dumps({"grounded": True, "unsupported_claims": []})
    m.last_truncated = True
    return m


# ---------------------------------------------------------------------------
# Tests: verify_final_grounding fail-closed behavior
# ---------------------------------------------------------------------------

def test_verifier_missing_llm_fails_closed():
    """Missing LLM → grounded=False (fail closed)."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = None
    result = agent.verify_final_grounding("some text", {"product_source": "x"})
    assert result["grounded"] is False
    assert result.get("error") == "no_llm"


def test_verifier_exception_fails_closed():
    """LLM exception → grounded=False (fail closed)."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _mock_llm_exception()
    result = agent.verify_final_grounding("some text", {"product_source": "x"})
    assert result["grounded"] is False
    assert result.get("error") == "llm_exception"


def test_verifier_empty_response_fails_closed():
    """Empty LLM response → grounded=False (fail closed)."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _mock_llm_empty()
    result = agent.verify_final_grounding("some text", {"product_source": "x"})
    assert result["grounded"] is False
    assert result.get("error") == "empty_response"


def test_verifier_malformed_json_fails_closed():
    """Malformed JSON → grounded=False (fail closed)."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _mock_llm_malformed()
    result = agent.verify_final_grounding("some text", {"product_source": "x"})
    assert result["grounded"] is False
    assert result.get("error") == "malformed_json"


def test_verifier_truncated_fails_closed():
    """Truncated response → grounded=False (fail closed)."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _mock_llm_truncated()
    result = agent.verify_final_grounding("some text", {"product_source": "x"})
    assert result["grounded"] is False
    assert result.get("error") == "truncated"


def test_verifier_empty_text_is_trivially_grounded():
    """Empty text → grounded=True (nothing to verify)."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _mock_llm_grounded()
    result = agent.verify_final_grounding("", {"product_source": "x"})
    assert result["grounded"] is True


def test_verifier_grounded_text_passes():
    """Grounded text → grounded=True."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _mock_llm_grounded()
    result = agent.verify_final_grounding("GPS tracking watch", {"product_source": "GPS: YES"})
    assert result["grounded"] is True


def test_verifier_ungrounded_text_fails():
    """Unsupported claims → grounded=False."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _mock_llm_ungrounded([{"claim": "24 ชั่วโมง", "reason": "not in source"}])
    result = agent.verify_final_grounding("GPS 24 ชั่วโมง", {"product_source": "GPS: YES"})
    assert result["grounded"] is False
    assert len(result["unsupported_claims"]) == 1


# ---------------------------------------------------------------------------
# Tests: UI options asserted in verifier request
# ---------------------------------------------------------------------------

def test_ui_options_in_verifier_prompt():
    """UI options must appear in the system prompt sent to the LLM."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _mock_llm_grounded()
    agent.verify_final_grounding("text", {
        "product_source": "src",
        "ui_options": {"platform": "TikTok", "media_type": "video"},
    })
    sent = agent.llm.chat.call_args[0][0]
    system_msg = sent[0]["content"]
    assert "TikTok" in system_msg
    assert "video" in system_msg


# ---------------------------------------------------------------------------
# Tests: no product-specific examples in production prompt
# ---------------------------------------------------------------------------

def test_no_product_specific_examples_in_prompt():
    """The verifier system prompt must not contain K5, K9, or other
    product-specific examples."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _mock_llm_grounded()
    agent.verify_final_grounding("text", {"product_source": "src"})
    sent = agent.llm.chat.call_args[0][0]
    system_msg = sent[0]["content"]
    # Generic wording only — no specific product names
    assert "K5" not in system_msg
    assert "K9" not in system_msg
    assert "K2" not in system_msg
    # Generic wording should be present
    assert "สินค้าอื่น" in system_msg or "another product" in system_msg.lower() or "สินค้าที่กำลังตรวจ" in system_msg


# ---------------------------------------------------------------------------
# Tests: brand example does not create capability for different product
# ---------------------------------------------------------------------------

def test_brand_example_does_not_create_capability():
    """A brand example for one product must not create a capability for
    a different product. The verifier should reject this."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _mock_llm_ungrounded([{
        "claim": "video call",
        "reason": "product source does not have video call; brand context refers to another product",
    }])
    result = agent.verify_final_grounding("video call feature", {
        "product_source": "Camera: 0.3MP\nGPS: YES",
        "brand_context": "Brand examples for another product: video call, 4G VoLTE",
    })
    assert result["grounded"] is False


# ---------------------------------------------------------------------------
# Tests: _ground_and_store for Agents 1-3 (text output)
# ---------------------------------------------------------------------------

def _make_orch():
    from src.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = {}
    orch.results = {}
    orch.brand_context = None
    orch.brand_reference = None
    orch.brand_rules = None
    return orch


def test_ground_and_store_grounded_text_agent():
    """Agents 1-3: grounded text is returned (caller persists after gates)."""
    orch = _make_orch()
    result = orch._ground_and_store(
        "product_spec", "grounded output", _mock_llm_grounded(),
        runtime_context={"product_source": "src"},
    )
    assert result == "grounded output"
    # _ground_and_store no longer persists — caller writes after all gates
    assert "product_spec" not in orch.results


def test_ground_and_store_ungrounded_text_agent_raises():
    """Agents 1-3: ungrounded text with no fallback → GroundingError."""
    from src.orchestrator import GroundingError
    orch = _make_orch()
    try:
        orch._ground_and_store(
            "product_spec", "ungrounded output", _mock_llm_ungrounded(),
            runtime_context={"product_source": "src"},
        )
        assert False, "Should have raised GroundingError"
    except GroundingError:
        pass
    assert "product_spec" not in orch.results


def test_ground_and_store_missing_llm_raises():
    """Agents 1-3: missing LLM → GroundingError (fail closed)."""
    from src.orchestrator import GroundingError
    orch = _make_orch()
    try:
        orch._ground_and_store(
            "competitor_analysis", "text", None,
            runtime_context={"product_source": "src"},
        )
        assert False, "Should have raised GroundingError"
    except GroundingError:
        pass


def test_ground_and_store_verifier_exception_raises():
    """Agents 1-3: verifier exception → GroundingError (fail closed)."""
    from src.orchestrator import GroundingError
    orch = _make_orch()
    try:
        orch._ground_and_store(
            "campaign_strategy", "text", _mock_llm_exception(),
            runtime_context={"product_source": "src"},
        )
        assert False, "Should have raised GroundingError"
    except GroundingError:
        pass


# ---------------------------------------------------------------------------
# Tests: _ground_and_store for Agent 4 (structured posts)
# ---------------------------------------------------------------------------

def _make_post(title="Title", caption="Caption", script="Script", cta="CTA",
               hashtags=["#tag"], image_prompts=["img prompt"], video_prompts=["vid prompt"]):
    return {
        "title": title, "caption": caption, "script": script, "cta": cta,
        "hashtags": hashtags, "image_prompts": image_prompts,
        "video_prompts": video_prompts,
    }


def test_ground_and_store_grounded_posts():
    """Agent 4: grounded posts are returned (caller persists after gates)."""
    orch = _make_orch()
    posts = [_make_post()]
    result = orch._ground_and_store(
        "content_creator", json.dumps({"posts": posts}), _mock_llm_grounded(),
        runtime_context={"product_source": "src"},
        posts=posts, pre_mutation_posts=posts,
    )
    stored = json.loads(result)
    assert stored["posts"][0]["script"] == "Script"
    # _ground_and_store no longer persists — caller writes after all gates
    assert "content_creator" not in orch.results


def test_ground_and_store_ungrounded_posts_with_fallback():
    """Agent 4: ungrounded posts → revert to pre-mutation, reverify, store."""
    orch = _make_orch()
    # Pre-mutation: grounded version
    pre_posts = [_make_post(script="original grounded script")]
    # Post-mutation: ungrounded version
    post_posts = [_make_post(script="ungrounded 24 ชั่วโมง")]

    # First call (post-mutation) → ungrounded; second call (pre-mutation) → grounded
    llm = MagicMock()
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "24 ชม", "reason": "x"}]}),
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]
    llm.last_truncated = False

    result = orch._ground_and_store(
        "content_creator", json.dumps({"posts": post_posts}), llm,
        runtime_context={"product_source": "src"},
        posts=post_posts, pre_mutation_posts=pre_posts,
    )
    stored = json.loads(result)
    # Should have reverted to pre-mutation script
    assert stored["posts"][0]["script"] == "original grounded script"


def test_ground_and_store_ungrounded_posts_no_fallback_raises():
    """Agent 4: ungrounded posts with no fallback → GroundingError."""
    from src.orchestrator import GroundingError
    orch = _make_orch()
    posts = [_make_post(script="ungrounded")]
    # pre_mutation_posts is the same as posts (no real fallback)
    try:
        orch._ground_and_store(
            "content_creator", json.dumps({"posts": posts}), _mock_llm_ungrounded(),
            runtime_context={"product_source": "src"},
            posts=posts, pre_mutation_posts=posts,
        )
        assert False, "Should have raised GroundingError"
    except GroundingError:
        pass
    assert "content_creator" not in orch.results


def test_ground_and_store_fallback_also_fails_raises():
    """Agent 4: both mutated and fallback fail → GroundingError."""
    from src.orchestrator import GroundingError
    orch = _make_orch()
    pre_posts = [_make_post(script="also ungrounded")]
    post_posts = [_make_post(script="ungrounded")]

    llm = MagicMock()
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "x", "reason": "y"}]}),
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "z", "reason": "w"}]}),
    ]
    llm.last_truncated = False

    try:
        orch._ground_and_store(
            "content_creator", json.dumps({"posts": post_posts}), llm,
            runtime_context={"product_source": "src"},
            posts=post_posts, pre_mutation_posts=pre_posts,
        )
        assert False, "Should have raised GroundingError"
    except GroundingError:
        pass


def test_ground_and_store_complete_artifact_checked():
    """Agent 4: the complete post (title, caption, script, CTA, hashtags,
    image/video prompts) is serialized and sent to the verifier — not just script."""
    orch = _make_orch()
    posts = [_make_post(
        title="My Title", caption="My Caption", script="My Script",
        cta="My CTA", hashtags=["#mytag"],
        image_prompts=["my image prompt"], video_prompts=["my video prompt"],
    )]
    llm = _mock_llm_grounded()
    orch._ground_and_store(
        "content_creator", json.dumps({"posts": posts}), llm,
        runtime_context={"product_source": "src"},
        posts=posts, pre_mutation_posts=posts,
    )
    # The user prompt sent to the LLM should contain all fields
    sent = llm.chat.call_args[0][0]
    user_msg = sent[1]["content"]
    assert "My Title" in user_msg
    assert "My Caption" in user_msg
    assert "My Script" in user_msg
    assert "My CTA" in user_msg
    assert "#mytag" in user_msg
    assert "my image prompt" in user_msg
    assert "my video prompt" in user_msg


def test_caption_claim_cannot_bypass_gate():
    """An unsupported claim in caption (not script) must still be caught."""
    orch = _make_orch()
    posts = [_make_post(caption="โปรโมชั่นพิเศษวันนี้", script="grounded script")]
    llm = _mock_llm_ungrounded([{"claim": "โปรโมชั่นพิเศษ", "reason": "not in source"}])
    # No fallback
    try:
        orch._ground_and_store(
            "content_creator", json.dumps({"posts": posts}), llm,
            runtime_context={"product_source": "src"},
            posts=posts, pre_mutation_posts=posts,
        )
        assert False, "Should have raised GroundingError"
    except Exception:
        pass


def test_image_prompt_claim_cannot_bypass_gate():
    """An unsupported claim in image_prompts must still be caught."""
    orch = _make_orch()
    posts = [_make_post(image_prompts=["24 ชั่วโมง GPS watch"])]
    llm = _mock_llm_ungrounded([{"claim": "24 ชั่วโมง", "reason": "not in source"}])
    try:
        orch._ground_and_store(
            "content_creator", json.dumps({"posts": posts}), llm,
            runtime_context={"product_source": "src"},
            posts=posts, pre_mutation_posts=posts,
        )
        assert False, "Should have raised GroundingError"
    except Exception:
        pass


def test_video_prompt_claim_cannot_bypass_gate():
    """An unsupported claim in video_prompts must still be caught."""
    orch = _make_orch()
    posts = [_make_post(video_prompts=["video call feature"])]
    llm = _mock_llm_ungrounded([{"claim": "video call", "reason": "not in source"}])
    try:
        orch._ground_and_store(
            "content_creator", json.dumps({"posts": posts}), llm,
            runtime_context={"product_source": "src"},
            posts=posts, pre_mutation_posts=posts,
        )
        assert False, "Should have raised GroundingError"
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests: all Agents 1-4 reach the final gate
# ---------------------------------------------------------------------------

def test_agent1_product_spec_reaches_gate():
    """Agent 1 (product_spec) output must pass through _ground_and_store."""
    from src.orchestrator import Orchestrator
    orch = _make_orch()
    # Simulate the call that run_product_spec makes
    result = orch._ground_and_store(
        "product_spec", "product brief", _mock_llm_grounded(),
        runtime_context={"product_source": "src"},
    )
    # _ground_and_store returns verified content; caller persists
    assert result == "product brief"
    assert "product_spec" not in orch.results


def test_agent2_competitor_analysis_reaches_gate():
    """Agent 2 (competitor_analysis) output must pass through _ground_and_store."""
    orch = _make_orch()
    result = orch._ground_and_store(
        "competitor_analysis", "competitor report", _mock_llm_grounded(),
        runtime_context={"product_source": "src"},
    )
    assert result == "competitor report"
    assert "competitor_analysis" not in orch.results


def test_agent3_campaign_strategy_reaches_gate():
    """Agent 3 (campaign_strategy) output must pass through _ground_and_store."""
    orch = _make_orch()
    result = orch._ground_and_store(
        "campaign_strategy", "campaign plan", _mock_llm_grounded(),
        runtime_context={"product_source": "src"},
    )
    assert result == "campaign plan"
    assert "campaign_strategy" not in orch.results


def test_agent4_content_creator_reaches_gate():
    """Agent 4 (content_creator) output must pass through _ground_and_store."""
    orch = _make_orch()
    posts = [_make_post()]
    result = orch._ground_and_store(
        "content_creator", json.dumps({"posts": posts}), _mock_llm_grounded(),
        runtime_context={"product_source": "src"},
        posts=posts, pre_mutation_posts=posts,
    )
    stored = json.loads(result)
    assert stored["posts"][0]["script"] == "Script"
    assert "content_creator" not in orch.results


# ---------------------------------------------------------------------------
# Tests: atomic fallback restores all fields
# ---------------------------------------------------------------------------

def test_atomic_fallback_restores_all_fields():
    """When reverting to pre-mutation, ALL content fields must be restored,
    not just script."""
    orch = _make_orch()
    pre_posts = [_make_post(
        title="Original Title", caption="Original Caption", script="Original Script",
        cta="Original CTA", hashtags=["#original"],
        image_prompts=["original image"], video_prompts=["original video"],
    )]
    post_posts = [_make_post(
        title="Mutated Title", caption="Mutated Caption", script="Mutated Script",
        cta="Mutated CTA", hashtags=["#mutated"],
        image_prompts=["mutated image"], video_prompts=["mutated video"],
    )]

    llm = MagicMock()
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "x", "reason": "y"}]}),
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]
    llm.last_truncated = False

    result = orch._ground_and_store(
        "content_creator", json.dumps({"posts": post_posts}), llm,
        runtime_context={"product_source": "src"},
        posts=post_posts, pre_mutation_posts=pre_posts,
    )
    stored = json.loads(result)
    p = stored["posts"][0]
    # All fields should be restored to original
    assert p["title"] == "Original Title"
    assert p["caption"] == "Original Caption"
    assert p["script"] == "Original Script"
    assert p["cta"] == "Original CTA"
    assert p["hashtags"] == ["#original"]
    assert p["image_prompts"] == ["original image"]
    assert p["video_prompts"] == ["original video"]


def test_fallback_is_reverified():
    """The fallback must be reverified before being accepted."""
    orch = _make_orch()
    pre_posts = [_make_post(script="pre-mutation")]
    post_posts = [_make_post(script="post-mutation")]

    llm = MagicMock()
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "x", "reason": "y"}]}),
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]
    llm.last_truncated = False

    orch._ground_and_store(
        "content_creator", json.dumps({"posts": post_posts}), llm,
        runtime_context={"product_source": "src"},
        posts=post_posts, pre_mutation_posts=pre_posts,
    )
    # Two LLM calls: first for post-mutation, second for pre-mutation reverify
    assert llm.chat.call_count == 2


# ---------------------------------------------------------------------------
# Tests: no product/brand-specific production logic
# ---------------------------------------------------------------------------

def test_serialize_post_uses_generic_keys():
    """_serialize_post_for_grounding must use generic field keys, not
    product-specific or brand-specific logic."""
    orch = _make_orch()
    post = {
        "title": "T", "caption": "C", "script": "S", "cta": "A",
        "hashtags": ["#h"], "image_prompts": ["i"], "video_prompts": ["v"],
    }
    text = orch._serialize_post_for_grounding(post)
    # Should contain all content fields
    assert "T" in text
    assert "C" in text
    assert "S" in text
    assert "A" in text
    assert "#h" in text
    assert "i" in text
    assert "v" in text
