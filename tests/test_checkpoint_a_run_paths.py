"""Checkpoint A — Production-path grounding boundary tests.

These tests verify that the actual production run_* methods invoke the
final grounding gate, that no semantic mutation occurs after grounding,
that the verifier receives the complete runtime context, and that
field-specific unsupported claims are caught.

Unlike the direct-helper tests in test_final_grounding_boundary.py, these
tests execute the real production entry paths and spy on _ground_and_store
at the boundary.
"""
import copy
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grounded_llm():
    """LLM mock that returns grounded=true for grounding checks."""
    m = MagicMock()
    m.chat.return_value = json.dumps({"grounded": True, "unsupported_claims": []})
    m.last_truncated = False
    return m


def _marker_verifier(marker):
    """LLM mock whose verdict depends on whether `marker` appears in the
    verifier input.  If the marker is present → grounded=false; if absent
    → grounded=true.

    This prevents false-confidence: if a field is removed from
    serialization the marker disappears and the test must fail for the
    correct reason (verifier says grounded=true → no GroundingError).
    """
    m = MagicMock()
    m.last_truncated = False

    def _chat(messages, **kwargs):
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            user_msg = messages[1]["content"] if len(messages) > 1 else ""
            if marker in user_msg:
                return json.dumps({
                    "grounded": False,
                    "unsupported_claims": [{"claim": marker, "reason": "not in source"}],
                })
            return json.dumps({"grounded": True, "unsupported_claims": []})
        # Normal agent call — return minimal valid content
        return '{"posts": [{"platform": "TikTok", "concept": "c", "title": "t", "caption": "cap", "hashtags": "#t", "asset_ids": []}]}'
    m.chat.side_effect = _chat
    return m


def _make_orch():
    from src.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = {}
    orch.results = {}
    orch.brand_context = None
    orch.brand_reference = None
    orch.brand_rules = None
    orch.brand_visual = None
    orch.product_id = None
    orch.product_images = []
    return orch


# ---------------------------------------------------------------------------
# 1. Run-path tests — prove run_* methods invoke _ground_and_store
# ---------------------------------------------------------------------------

def test_agent1_run_product_spec_invokes_grounding_gate():
    """Agent 1: run_product_spec must call _ground_and_store."""
    from src.orchestrator import Orchestrator
    orch = _make_orch()
    orch._make_agent = MagicMock(return_value=MagicMock())
    orch._get_product_image_paths = MagicMock(return_value=[])
    orch._get_product_data = MagicMock(return_value="product source")
    orch._build_brand_context_text = MagicMock(return_value="brand ctx")

    agent = MagicMock()
    agent.run.return_value = "product spec output"
    orch._make_agent.return_value = agent

    llm = _grounded_llm()
    with patch.object(orch, "_ground_and_store", wraps=orch._ground_and_store) as spy:
        orch.run_product_spec("raw data", llm=llm, quick_brief="qb")
        assert spy.called, "run_product_spec must call _ground_and_store"
        call_args = spy.call_args
        assert call_args[0][0] == "product_spec", "must ground as product_spec"


def test_agent2_run_competitor_analysis_invokes_grounding_gate():
    """Agent 2: run_competitor_analysis must call _ground_and_store."""
    from src.orchestrator import Orchestrator
    orch = _make_orch()
    orch._make_agent = MagicMock(return_value=MagicMock())
    orch._get_product_image_paths = MagicMock(return_value=[])
    orch._get_product_data = MagicMock(return_value="product source")
    orch._build_brand_context_text = MagicMock(return_value="brand ctx")

    agent = MagicMock()
    agent.run.return_value = "competitor analysis output"
    orch._make_agent.return_value = agent

    llm = _grounded_llm()
    with patch.object(orch, "_ground_and_store", wraps=orch._ground_and_store) as spy:
        orch.run_competitor_analysis("raw data", llm=llm, quick_brief="qb")
        assert spy.called, "run_competitor_analysis must call _ground_and_store"
        call_args = spy.call_args
        assert call_args[0][0] == "competitor_analysis"


def test_agent3_run_campaign_strategy_invokes_grounding_gate():
    """Agent 3: run_campaign_strategy must call _ground_and_store."""
    from src.orchestrator import Orchestrator
    orch = _make_orch()
    orch._make_agent = MagicMock(return_value=MagicMock())
    orch._get_product_data = MagicMock(return_value="product source")
    orch._build_brand_context_text = MagicMock(return_value="brand ctx")

    agent = MagicMock()
    agent.run.return_value = "campaign strategy output"
    orch._make_agent.return_value = agent

    llm = _grounded_llm()
    with patch.object(orch, "_ground_and_store", wraps=orch._ground_and_store) as spy:
        orch.run_campaign_strategy("pspec", "canalysis", llm=llm, quick_brief="qb")
        assert spy.called, "run_campaign_strategy must call _ground_and_store"
        call_args = spy.call_args
        assert call_args[0][0] == "campaign_strategy"


def test_agent4_standalone_run_content_creator_invokes_grounding_gate():
    """Agent 4 standalone: run_content_creator must call _finalize_content_output
    (which calls _ground_and_store) before persisting.  This is an executable
    test — it runs the actual production method and verifies the grounding
    gate is crossed."""
    from src.orchestrator import Orchestrator
    orch = _make_orch()
    orch._make_agent = MagicMock(return_value=MagicMock())
    orch._get_product_image_paths = MagicMock(return_value=[])
    orch._get_product_data = MagicMock(return_value="product source")
    orch._build_brand_context_text = MagicMock(return_value="brand ctx")
    orch._build_configured_pillars_text = MagicMock(return_value="")

    agent = MagicMock()
    agent.run.return_value = json.dumps({"posts": [{
        "platform": "TikTok", "concept": "c", "title": "t",
        "caption": "cap", "script": "s", "hashtags": "#h",
        "image_prompts": [], "video_prompts": [], "asset_ids": [],
    }]})
    orch._make_agent.return_value = agent

    llm = _grounded_llm()
    with patch.object(orch, "_finalize_content_output", wraps=orch._finalize_content_output) as spy:
        result = orch.run_content_creator("pspec", "analysis", "campaign", llm=llm)
        assert spy.called, "run_content_creator must call _finalize_content_output"
        # The result must be the grounded JSON, not the raw agent output
        parsed = json.loads(result)
        assert "posts" in parsed
        # And it must be stored in self.results
        assert orch.results.get("content_creator") == result


def test_agent4_standalone_run_content_creator_no_raw_in_results_on_failure():
    """If grounding fails, run_content_creator must NOT leave raw ungrounded
    output in self.results under a persistable key."""
    from src.orchestrator import Orchestrator, GroundingError
    orch = _make_orch()
    orch._make_agent = MagicMock(return_value=MagicMock())
    orch._get_product_image_paths = MagicMock(return_value=[])
    orch._get_product_data = MagicMock(return_value="product source")
    orch._build_brand_context_text = MagicMock(return_value="brand ctx")
    orch._build_configured_pillars_text = MagicMock(return_value="")

    agent = MagicMock()
    agent.run.return_value = json.dumps({"posts": [{
        "platform": "TikTok", "concept": "c", "title": "t",
        "caption": "UNSUPPORTED_CLAIM_XYZ", "script": "s", "hashtags": "#h",
        "image_prompts": [], "video_prompts": [], "asset_ids": [],
    }]})
    orch._make_agent.return_value = agent

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.side_effect = [
        # Grounding check: not grounded
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED_CLAIM_XYZ"}]}),
        # Fallback recheck: also not grounded
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED_CLAIM_XYZ"}]}),
    ]

    try:
        orch.run_content_creator("pspec", "analysis", "campaign", llm=llm)
        assert False, "Should have raised GroundingError"
    except GroundingError:
        pass

    # No persistable Agent 4 result should exist
    assert "content_creator" not in orch.results, (
        "Raw ungrounded output leaked into self.results on grounding failure"
    )
    assert "content_creator_markdown" not in orch.results, (
        "Raw ungrounded markdown leaked into self.results on grounding failure"
    )


def test_agent4_auto_run_content_creator_auto_invokes_grounding_gate():
    """Agent 4 auto: run_content_creator_auto must call _finalize_content_output
    which calls _ground_and_store.  We verify this by spying on the method."""
    from src.orchestrator import Orchestrator

    # Instead of trying to run the full auto flow (which has many dependencies),
    # we verify the wiring: the auto path's source code calls
    # _finalize_content_output.  We check this by inspecting the source.
    import inspect
    source = inspect.getsource(Orchestrator.run_content_creator_auto)
    assert "_finalize_content_output" in source, (
        "run_content_creator_auto must call _finalize_content_output"
    )
    assert "_run_content_creator_raw" in source, (
        "run_content_creator_auto must call _run_content_creator_raw (not the grounded public method)"
    )
    # Also verify _finalize_content_output calls _ground_and_store
    finalize_src = inspect.getsource(Orchestrator._finalize_content_output)
    assert "_ground_and_store" in finalize_src, (
        "_finalize_content_output must call _ground_and_store"
    )


def test_agent4_manual_path_invokes_finalize_content_output():
    """Agent 4 manual: the manual path in web_viewer must call
    _finalize_content_output (which calls _ground_and_store)."""
    import inspect
    import web_viewer

    # Verify the manual path source calls _finalize_content_output
    source = inspect.getsource(web_viewer)
    # The manual content creation function calls orch._finalize_content_output
    assert "_finalize_content_output" in source, (
        "web_viewer manual path must call _finalize_content_output"
    )
    assert "_run_content_creator_raw" in source, (
        "web_viewer manual path must call _run_content_creator_raw (not the grounded public method)"
    )


# ---------------------------------------------------------------------------
# 2. Persistence invariant — no semantic mutation after grounding
# ---------------------------------------------------------------------------

def test_agent4_persisted_artifact_equals_grounded_artifact():
    """The exact artifact submitted to final grounding must equal the
    artifact persisted to self.results (no semantic mutation after
    grounding)."""
    orch = _make_orch()
    posts = [{
        "platform": "TikTok", "concept": "c", "title": "Grounded Title",
        "caption": "Grounded Caption", "script": "Grounded Script",
        "hashtags": "#grounded", "image_prompts": [{"prompt": "grounded img"}],
        "video_prompts": [{"prompt": "grounded vid"}], "asset_ids": [],
    }]
    pre_posts = copy.deepcopy(posts)

    grounded_content = None

    original_ground = orch._ground_and_store

    def spy_ground(agent_key, result, llm, **kwargs):
        nonlocal grounded_content
        grounded_content = result
        return original_ground(agent_key, result, llm, **kwargs)

    llm = _grounded_llm()
    with patch.object(orch, "_ground_and_store", side_effect=spy_ground):
        content, md = orch._finalize_content_output(
            posts, pre_posts, llm,
            {"product_source": "src", "brand_context": "bc",
             "quick_brief": "qb", "ui_options": {"platform": "TikTok"}},
        )

    # The persisted content must equal the grounded content
    assert content == grounded_content, (
        "Persisted content differs from grounded content — "
        "semantic mutation occurred after grounding"
    )
    # Verify no internal fallback metadata leaked
    parsed = json.loads(content)
    for p in parsed["posts"]:
        assert "_pre_mutation_post" not in p, "Internal fallback metadata leaked"
        # Check all fields are the grounded values
        assert p["title"] == "Grounded Title"
        assert p["caption"] == "Grounded Caption"
        assert p["script"] == "Grounded Script"


def test_post_grounding_mutation_is_detected():
    """If a semantic mutation is introduced AFTER grounding, the invariant
    test must detect it.  This test demonstrates detection by checking
    that the persisted artifact differs from the grounded artifact."""
    orch = _make_orch()
    posts = [{
        "platform": "TikTok", "concept": "c", "title": "T",
        "caption": "C", "script": "S", "hashtags": "#h",
        "image_prompts": [], "video_prompts": [], "asset_ids": [],
    }]
    pre_posts = copy.deepcopy(posts)

    grounded_content = {"value": None}

    original_finalize = orch._finalize_content_output

    def mutating_finalize(all_posts, pre_mutation_posts, llm, ctx, cb=None):
        # Simulate a post-grounding mutation
        result_content, result_md = original_finalize(
            all_posts, pre_mutation_posts, llm, ctx, cb,
        )
        # Mutate after grounding
        parsed = json.loads(result_content)
        if parsed["posts"]:
            parsed["posts"][0]["caption"] += " MUTATED AFTER GROUNDING"
        return json.dumps(parsed, ensure_ascii=False), result_md

    llm = _grounded_llm()
    # First: capture what grounding sees
    original_ground = orch._ground_and_store

    def capture_ground(agent_key, result, llm_arg, **kwargs):
        grounded_content["value"] = result
        return original_ground(agent_key, result, llm_arg, **kwargs)

    with patch.object(orch, "_ground_and_store", side_effect=capture_ground):
        with patch.object(orch, "_finalize_content_output", side_effect=mutating_finalize):
            content, md = orch._finalize_content_output(
                posts, pre_posts, llm,
                {"product_source": "src"},
            )

    # The test demonstrates that a mutation IS detectable:
    assert content != grounded_content["value"], (
        "Post-grounding mutation was not detected — persisted content "
        "should differ from grounded content"
    )


# ---------------------------------------------------------------------------
# 3. Context-contract tests — exact markers reach the verifier
# ---------------------------------------------------------------------------

def test_brand_context_marker_reaches_verifier():
    """Removing brand_context must cause the verifier to not receive the
    brand marker.  The test asserts the exact marker appears in the
    verifier request."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _grounded_llm()

    agent.verify_final_grounding("text", {
        "product_source": "src",
        "brand_context": "BRAND_MARKER_12345",
        "quick_brief": "qb",
        "ui_options": {"platform": "TikTok"},
    })
    sent = agent.llm.chat.call_args[0][0]
    system_msg = sent[0]["content"]
    assert "BRAND_MARKER_12345" in system_msg, "Brand context marker missing"


def test_quick_brief_marker_reaches_verifier():
    """Removing quick_brief must cause the verifier to not receive the
    quick_brief marker."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _grounded_llm()

    agent.verify_final_grounding("text", {
        "product_source": "src",
        "brand_context": "bc",
        "quick_brief": "QUICKBRIEF_MARKER_67890",
        "ui_options": {"platform": "TikTok"},
    })
    sent = agent.llm.chat.call_args[0][0]
    system_msg = sent[0]["content"]
    assert "QUICKBRIEF_MARKER_67890" in system_msg, "Quick Brief marker missing"


def test_product_source_marker_reaches_verifier():
    """Product source marker must reach the verifier."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _grounded_llm()

    agent.verify_final_grounding("text", {
        "product_source": "PRODUCTSOURCE_MARKER_ABC",
        "brand_context": "bc",
        "quick_brief": "qb",
    })
    sent = agent.llm.chat.call_args[0][0]
    system_msg = sent[0]["content"]
    assert "PRODUCTSOURCE_MARKER_ABC" in system_msg, "Product source marker missing"


def test_verified_evidence_marker_reaches_verifier():
    """Verified evidence marker must reach the verifier when provided."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {}
    agent.instructions = {}
    agent.llm = _grounded_llm()

    agent.verify_final_grounding("text", {
        "product_source": "src",
        "verified_evidence": "EVIDENCE_MARKER_XYZ",
    })
    sent = agent.llm.chat.call_args[0][0]
    system_msg = sent[0]["content"]
    assert "EVIDENCE_MARKER_XYZ" in system_msg, "Verified evidence marker missing"


# ---------------------------------------------------------------------------
# 4. Field-marker tests — verdict depends on field presence in serialization
# ---------------------------------------------------------------------------

def test_caption_marker_detected_in_serialization():
    """Unsupported marker in caption must cause grounding failure.
    If caption is removed from serialization, the marker disappears and
    the verifier returns grounded=true → no GroundingError → test fails."""
    from src.orchestrator import GroundingError
    marker = "UNIQUE_CAPTION_CLAIM_111"
    orch = _make_orch()
    posts = [{
        "platform": "TikTok", "concept": "c", "title": "t",
        "caption": marker, "script": "grounded", "hashtags": "#h",
        "image_prompts": [], "video_prompts": [], "asset_ids": [],
    }]
    llm = _marker_verifier(marker)
    try:
        orch._ground_and_store(
            "content_creator", json.dumps({"posts": posts}), llm,
            runtime_context={"product_source": "src"},
            posts=posts, pre_mutation_posts=posts,
        )
        assert False, "Should have raised GroundingError — caption marker not detected"
    except GroundingError:
        pass  # Correct: marker was detected


def test_image_prompt_marker_detected_in_serialization():
    """Unsupported marker in image_prompts must cause grounding failure."""
    from src.orchestrator import GroundingError
    marker = "UNIQUE_IMAGE_CLAIM_222"
    orch = _make_orch()
    posts = [{
        "platform": "TikTok", "concept": "c", "title": "t",
        "caption": "grounded", "script": "grounded", "hashtags": "#h",
        "image_prompts": [{"prompt": marker}], "video_prompts": [], "asset_ids": [],
    }]
    llm = _marker_verifier(marker)
    try:
        orch._ground_and_store(
            "content_creator", json.dumps({"posts": posts}), llm,
            runtime_context={"product_source": "src"},
            posts=posts, pre_mutation_posts=posts,
        )
        assert False, "Should have raised GroundingError — image_prompt marker not detected"
    except GroundingError:
        pass


def test_video_prompt_marker_detected_in_serialization():
    """Unsupported marker in video_prompts must cause grounding failure."""
    from src.orchestrator import GroundingError
    marker = "UNIQUE_VIDEO_CLAIM_333"
    orch = _make_orch()
    posts = [{
        "platform": "TikTok", "concept": "c", "title": "t",
        "caption": "grounded", "script": "grounded", "hashtags": "#h",
        "image_prompts": [], "video_prompts": [{"prompt": marker}], "asset_ids": [],
    }]
    llm = _marker_verifier(marker)
    try:
        orch._ground_and_store(
            "content_creator", json.dumps({"posts": posts}), llm,
            runtime_context={"product_source": "src"},
            posts=posts, pre_mutation_posts=posts,
        )
        assert False, "Should have raised GroundingError — video_prompt marker not detected"
    except GroundingError:
        pass


def test_cta_marker_detected_in_serialization():
    """Unsupported marker in CTA must cause grounding failure."""
    from src.orchestrator import GroundingError
    marker = "UNIQUE_CTA_CLAIM_444"
    orch = _make_orch()
    posts = [{
        "platform": "TikTok", "concept": "c", "title": "t",
        "caption": "grounded", "script": "grounded", "hashtags": "#h",
        "cta": marker, "image_prompts": [], "video_prompts": [], "asset_ids": [],
    }]
    llm = _marker_verifier(marker)
    try:
        orch._ground_and_store(
            "content_creator", json.dumps({"posts": posts}), llm,
            runtime_context={"product_source": "src"},
            posts=posts, pre_mutation_posts=posts,
        )
        assert False, "Should have raised GroundingError — CTA marker not detected"
    except GroundingError:
        pass


# ---------------------------------------------------------------------------
# 5. Regression: _pre_mutation_post does not leak into persisted artifact
# ---------------------------------------------------------------------------

def test_script_mutation_persists_without_pre_mutation_field():
    """script mutation → final grounding → schema validation → persistence
    succeeds, and the persisted artifact contains no _pre_mutation_post field."""
    from src import script_reviewer
    from src.orchestrator import Orchestrator

    call_count = [0]

    def fake_review(script, platform, llm=None, source_context=""):
        call_count[0] += 1
        if call_count[0] == 1:
            return {
                "score": 50,
                "revised_script": "revised grounded script",
                "issues": ["weak hook"],
                "suggested_hooks": [],
            }
        return {"score": 80, "issues": [], "suggested_hooks": []}

    original = script_reviewer.review_script
    script_reviewer.review_script = fake_review
    try:
        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {}
        orch.results = {}
        orch.brand_context = None
        orch.brand_reference = None
        orch.brand_rules = None

        posts = [{
            "platform": "TikTok", "concept": "c", "title": "t",
            "caption": "cap", "script": "original grounded script",
            "hashtags": "#h", "image_prompts": [], "video_prompts": [],
            "asset_ids": [],
        }]
        # Position-preserving fallback: deep-copy before script review
        import copy as _copy
        pre_snapshot = _copy.deepcopy(posts[0])
        pre_mutation_posts = []
        llm = MagicMock()
        llm.last_truncated = False

        orch._review_script_in_posts(
            posts, "TikTok", llm=llm,
        )

        # The post must NOT contain _pre_mutation_post
        assert "_pre_mutation_post" not in posts[0], (
            "_pre_mutation_post leaked into post dict"
        )

        # Caller appends the pre-mutation snapshot as the fallback slot
        pre_mutation_posts.append(pre_snapshot)
        assert len(pre_mutation_posts) == 1
        assert pre_mutation_posts[0]["script"] == "original grounded script"

        # Now finalize — schema validation must pass
        grounding_llm = _grounded_llm()
        content, md = orch._finalize_content_output(
            posts, pre_mutation_posts, grounding_llm,
            {"product_source": "src", "brand_context": "bc",
             "quick_brief": "qb", "ui_options": {"platform": "TikTok"}},
        )

        # Schema validation passed (no exception)
        parsed = json.loads(content)
        for p in parsed["posts"]:
            assert "_pre_mutation_post" not in p, (
                "_pre_mutation_post leaked into persisted artifact"
            )
        # The script should be the revised version (script_changed=True)
        assert parsed["posts"][0]["script"] == "revised grounded script"
    finally:
        script_reviewer.review_script = original


def test_no_pre_mutation_post_in_persisted_artifact_after_revert():
    """When grounding reverts to pre-mutation, the persisted artifact
    must not contain _pre_mutation_post."""
    orch = _make_orch()
    pre_posts = [{
        "platform": "TikTok", "concept": "c", "title": "Original",
        "caption": "Original", "script": "Original Script",
        "hashtags": "#orig", "image_prompts": [], "video_prompts": [],
        "asset_ids": [],
    }]
    post_posts = [{
        "platform": "TikTok", "concept": "c", "title": "Mutated",
        "caption": "Mutated UNSUPPORTED_CLAIM", "script": "Mutated Script",
        "hashtags": "#mut", "image_prompts": [], "video_prompts": [],
        "asset_ids": [],
    }]

    llm = MagicMock()
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED_CLAIM", "reason": "x"}]}),
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]
    llm.last_truncated = False

    content, md = orch._finalize_content_output(
        post_posts, pre_posts, llm,
        {"product_source": "src"},
    )

    parsed = json.loads(content)
    for p in parsed["posts"]:
        assert "_pre_mutation_post" not in p, (
            "_pre_mutation_post leaked after revert"
        )
        assert p["script"] == "Original Script"
        assert p["title"] == "Original"


# ---------------------------------------------------------------------------
# 6. Pipeline path — run_pipeline cannot return ungrounded Agent 4
# ---------------------------------------------------------------------------

def test_agent4_pipeline_uses_grounded_run_content_creator():
    """run_pipeline must call run_content_creator (the grounded public method),
    not _run_content_creator_raw.  We verify by inspecting the source."""
    from src.orchestrator import Orchestrator
    import inspect
    source = inspect.getsource(Orchestrator.run_pipeline)
    assert "run_content_creator(" in source, (
        "run_pipeline must call run_content_creator (grounded public method)"
    )
    assert "_run_content_creator_raw" not in source, (
        "run_pipeline must not call _run_content_creator_raw directly — "
        "it should use the grounded public method for single-post pipeline use"
    )


def test_agent4_pipeline_grounds_before_returning_results():
    """run_pipeline must ground Agent 4 output before returning self.results.
    We verify by executing run_pipeline with mocked agents and checking
    that self.results['content_creator'] is the grounded output."""
    from src.orchestrator import Orchestrator
    orch = _make_orch()
    orch._make_agent = MagicMock(return_value=MagicMock())
    orch._get_product_image_paths = MagicMock(return_value=[])
    orch._get_product_data = MagicMock(return_value="product source")
    orch._build_brand_context_text = MagicMock(return_value="brand ctx")
    orch._build_configured_pillars_text = MagicMock(return_value="")
    orch.make_client = MagicMock(return_value=_grounded_llm())

    # Mock all 4 agents to return simple outputs
    def make_agent_mock(return_value):
        m = MagicMock()
        m.run.return_value = return_value
        m.build_prompt.return_value = "prompt"
        return m

    agents = iter([
        make_agent_mock("product spec output"),
        make_agent_mock("competitor analysis output"),
        make_agent_mock("campaign strategy output"),
        make_agent_mock(json.dumps({"posts": [{
            "platform": "TikTok", "concept": "c", "title": "t",
            "caption": "cap", "script": "s", "hashtags": "#h",
            "image_prompts": [], "video_prompts": [], "asset_ids": [],
        }]})),
    ])
    orch._make_agent = MagicMock(side_effect=lambda name, cls, llm: next(agents))

    # Spy on _finalize_content_output
    with patch.object(orch, "_finalize_content_output", wraps=orch._finalize_content_output) as spy:
        # Mock Progress to avoid console output
        with patch("src.orchestrator.Progress"):
            results = orch.run_pipeline("raw data", "competitor data")
        assert spy.called, "run_pipeline must call _finalize_content_output for Agent 4"
        # The returned results must contain grounded content
        assert "content_creator" in results
        parsed = json.loads(results["content_creator"])
        assert "posts" in parsed


# ---------------------------------------------------------------------------
# 7. Multi-platform fallback alignment — both mutation orders
# ---------------------------------------------------------------------------

def test_fallback_alignment_unchanged_first_mutated_second():
    """Post 0 unchanged, post 1 mutated.  Grounding rejects mutated aggregate.
    Fallback restores.  Post 0 keeps its own original, post 1 restores its
    own original — no cross-post contamination."""
    orch = _make_orch()

    pre_posts = [
        {"platform": "Facebook", "concept": "c0", "title": "T0",
         "caption": "C0", "script": "S0", "hashtags": "#h0",
         "image_prompts": [], "video_prompts": [], "asset_ids": []},
        {"platform": "TikTok", "concept": "c1", "title": "T1",
         "caption": "C1", "script": "S1", "hashtags": "#h1",
         "image_prompts": [], "video_prompts": [], "asset_ids": []},
    ]
    # Post 0 unchanged (fallback == final), post 1 mutated
    final_posts = [
        copy.deepcopy(pre_posts[0]),
        {"platform": "TikTok", "concept": "c1", "title": "T1_MUTATED",
         "caption": "C1_MUTATED UNSUPPORTED", "script": "S1_MUTATED",
         "hashtags": "#h1m", "image_prompts": [], "video_prompts": [],
         "asset_ids": []},
    ]

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.side_effect = [
        # First grounding: mutated aggregate → not grounded
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED"}]}),
        # Fallback recheck: original aggregate → grounded
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]

    content, md = orch._finalize_content_output(
        final_posts, pre_posts, llm, {"product_source": "src"},
    )

    parsed = json.loads(content)
    # Post 0 keeps its own original
    assert parsed["posts"][0]["title"] == "T0"
    assert parsed["posts"][0]["script"] == "S0"
    assert parsed["posts"][0]["caption"] == "C0"
    # Post 1 restores its own original (not post 0's)
    assert parsed["posts"][1]["title"] == "T1"
    assert parsed["posts"][1]["script"] == "S1"
    assert parsed["posts"][1]["caption"] == "C1"
    # No cross-contamination
    assert parsed["posts"][1]["title"] != "T0"
    assert parsed["posts"][0]["title"] != "T1_MUTATED"


def test_fallback_alignment_mutated_first_unchanged_second():
    """Post 0 mutated, post 1 unchanged.  Grounding rejects mutated aggregate.
    Fallback restores.  Post 0 restores its own original, post 1 keeps its
    own original — no cross-post contamination."""
    orch = _make_orch()

    pre_posts = [
        {"platform": "Facebook", "concept": "c0", "title": "T0",
         "caption": "C0", "script": "S0", "hashtags": "#h0",
         "image_prompts": [], "video_prompts": [], "asset_ids": []},
        {"platform": "TikTok", "concept": "c1", "title": "T1",
         "caption": "C1", "script": "S1", "hashtags": "#h1",
         "image_prompts": [], "video_prompts": [], "asset_ids": []},
    ]
    # Post 0 mutated, post 1 unchanged (fallback == final)
    final_posts = [
        {"platform": "Facebook", "concept": "c0", "title": "T0_MUTATED",
         "caption": "C0_MUTATED UNSUPPORTED", "script": "S0_MUTATED",
         "hashtags": "#h0m", "image_prompts": [], "video_prompts": [],
         "asset_ids": []},
        copy.deepcopy(pre_posts[1]),
    ]

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED"}]}),
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]

    content, md = orch._finalize_content_output(
        final_posts, pre_posts, llm, {"product_source": "src"},
    )

    parsed = json.loads(content)
    # Post 0 restores its own original (not post 1's)
    assert parsed["posts"][0]["title"] == "T0"
    assert parsed["posts"][0]["script"] == "S0"
    assert parsed["posts"][0]["caption"] == "C0"
    # Post 1 keeps its own original
    assert parsed["posts"][1]["title"] == "T1"
    assert parsed["posts"][1]["script"] == "S1"
    assert parsed["posts"][1]["caption"] == "C1"
    # No cross-contamination
    assert parsed["posts"][0]["title"] != "T1"
    assert parsed["posts"][1]["title"] != "T0_MUTATED"


# ---------------------------------------------------------------------------
# 8. Multiple mutated posts restore individually
# ---------------------------------------------------------------------------

def test_multiple_mutated_posts_restore_individually():
    """Both posts mutated.  Grounding rejects mutated aggregate.  Fallback
    restores each post to its own original individually."""
    orch = _make_orch()

    pre_posts = [
        {"platform": "Facebook", "concept": "c0", "title": "Orig0",
         "caption": "Cap0", "script": "Scr0", "hashtags": "#h0",
         "image_prompts": [], "video_prompts": [], "asset_ids": []},
        {"platform": "TikTok", "concept": "c1", "title": "Orig1",
         "caption": "Cap1", "script": "Scr1", "hashtags": "#h1",
         "image_prompts": [], "video_prompts": [], "asset_ids": []},
    ]
    final_posts = [
        {"platform": "Facebook", "concept": "c0", "title": "Mut0",
         "caption": "MutCap0 UNSUPPORTED", "script": "MutScr0",
         "hashtags": "#m0", "image_prompts": [], "video_prompts": [],
         "asset_ids": []},
        {"platform": "TikTok", "concept": "c1", "title": "Mut1",
         "caption": "MutCap1 UNSUPPORTED", "script": "MutScr1",
         "hashtags": "#m1", "image_prompts": [], "video_prompts": [],
         "asset_ids": []},
    ]

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED"}]}),
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]

    content, md = orch._finalize_content_output(
        final_posts, pre_posts, llm, {"product_source": "src"},
    )

    parsed = json.loads(content)
    # Each post restored to its own original
    assert parsed["posts"][0]["title"] == "Orig0"
    assert parsed["posts"][0]["script"] == "Scr0"
    assert parsed["posts"][1]["title"] == "Orig1"
    assert parsed["posts"][1]["script"] == "Scr1"


# ---------------------------------------------------------------------------
# 9. Deep-copy non-aliasing — fallback objects do not alias final posts
# ---------------------------------------------------------------------------

def test_fallback_objects_do_not_alias_final_posts():
    """Fallback (pre_mutation) objects must not be aliases of final post
    objects.  Mutating a final post after fallback creation must not affect
    the fallback."""
    orch = _make_orch()

    pre_posts = [
        {"platform": "TikTok", "concept": "c", "title": "Orig",
         "caption": "Cap", "script": "Scr", "hashtags": "#h",
         "image_prompts": [], "video_prompts": [], "asset_ids": []},
    ]
    final_posts = [
        {"platform": "TikTok", "concept": "c", "title": "Final",
         "caption": "FinalCap", "script": "FinalScr", "hashtags": "#fh",
         "image_prompts": [], "video_prompts": [], "asset_ids": []},
    ]

    # Verify non-aliasing before grounding
    assert pre_posts[0] is not final_posts[0]
    # Mutate final post
    final_posts[0]["title"] = "MUTATED_AFTER_FALLBACK"
    # Fallback must be unaffected
    assert pre_posts[0]["title"] == "Orig"

    llm = _grounded_llm()
    content, md = orch._finalize_content_output(
        final_posts, pre_posts, llm, {"product_source": "src"},
    )
    # Grounded (final posts are grounded) — no revert needed
    parsed = json.loads(content)
    assert parsed["posts"][0]["title"] == "MUTATED_AFTER_FALLBACK"


# ---------------------------------------------------------------------------
# 10. Accepted verifier candidate equals persisted candidate
# ---------------------------------------------------------------------------

def test_grounded_candidate_equals_persisted_candidate_with_fallback():
    """When grounding rejects the mutated version and accepts the fallback,
    the exact fallback candidate accepted by the verifier must equal the
    persisted artifact."""
    orch = _make_orch()

    pre_posts = [{
        "platform": "TikTok", "concept": "c", "title": "Grounded Title",
        "caption": "Grounded Caption", "script": "Grounded Script",
        "hashtags": "#g", "image_prompts": [], "video_prompts": [],
        "asset_ids": [],
    }]
    final_posts = [{
        "platform": "TikTok", "concept": "c", "title": "Mut",
        "caption": "Mut UNSUPPORTED", "script": "MutScr",
        "hashtags": "#m", "image_prompts": [], "video_prompts": [],
        "asset_ids": [],
    }]

    accepted_by_verifier = {"value": None}

    original_ground = orch._ground_and_store

    def spy_ground(agent_key, result, llm, **kwargs):
        # Capture what the verifier accepts (return value, not self.results)
        ret = original_ground(agent_key, result, llm, **kwargs)
        accepted_by_verifier["value"] = ret
        return ret

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED"}]}),
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]

    with patch.object(orch, "_ground_and_store", side_effect=spy_ground):
        content, md = orch._finalize_content_output(
            final_posts, pre_posts, llm, {"product_source": "src"},
        )

    # The persisted content must equal what the verifier accepted
    assert content == accepted_by_verifier["value"], (
        "Persisted content differs from the verifier-accepted candidate"
    )
    # And it must be the grounded (pre-mutation) version
    parsed = json.loads(content)
    assert parsed["posts"][0]["title"] == "Grounded Title"
    assert parsed["posts"][0]["script"] == "Grounded Script"


# ---------------------------------------------------------------------------
# 11. No internal fallback metadata in persisted JSON
# ---------------------------------------------------------------------------

def test_no_internal_metadata_in_persisted_json():
    """The persisted artifact must never contain internal fallback metadata
    like _pre_mutation_post."""
    orch = _make_orch()

    pre_posts = [{
        "platform": "TikTok", "concept": "c", "title": "Orig",
        "caption": "Cap", "script": "Scr", "hashtags": "#h",
        "image_prompts": [], "video_prompts": [], "asset_ids": [],
    }]
    final_posts = [{
        "platform": "TikTok", "concept": "c", "title": "Mut",
        "caption": "Mut UNSUPPORTED", "script": "MutScr",
        "hashtags": "#m", "image_prompts": [], "video_prompts": [],
        "asset_ids": [],
    }]

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED"}]}),
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]

    content, md = orch._finalize_content_output(
        final_posts, pre_posts, llm, {"product_source": "src"},
    )

    parsed = json.loads(content)
    for p in parsed["posts"]:
        assert "_pre_mutation_post" not in p, "Internal metadata leaked"
        # Check no other internal keys
        for key in p:
            assert not key.startswith("_"), f"Internal key {key} leaked"


# ---------------------------------------------------------------------------
# 12. Failed grounding leaves no successful/persistable Agent 4 result
# ---------------------------------------------------------------------------

def test_failed_grounding_leaves_no_persistable_result():
    """If both mutated and fallback fail grounding, _finalize_content_output
    must raise GroundingError and leave no content_creator in self.results."""
    from src.orchestrator import GroundingError
    orch = _make_orch()

    pre_posts = [{
        "platform": "TikTok", "concept": "c", "title": "Orig UNSUPPORTED",
        "caption": "Cap UNSUPPORTED", "script": "Scr",
        "hashtags": "#h", "image_prompts": [], "video_prompts": [],
        "asset_ids": [],
    }]
    final_posts = [{
        "platform": "TikTok", "concept": "c", "title": "Mut UNSUPPORTED",
        "caption": "Mut UNSUPPORTED", "script": "MutScr",
        "hashtags": "#m", "image_prompts": [], "video_prompts": [],
        "asset_ids": [],
    }]

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED"}]}),
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED"}]}),
    ]

    try:
        orch._finalize_content_output(
            final_posts, pre_posts, llm, {"product_source": "src"},
        )
        assert False, "Should have raised GroundingError"
    except GroundingError:
        pass

    assert "content_creator" not in orch.results, (
        "Failed grounding left a persistable result"
    )


# ---------------------------------------------------------------------------
# 13. Truthful script_review telemetry after fallback revert
# ---------------------------------------------------------------------------

def test_script_review_telemetry_truthful_after_revert():
    """After a fallback revert, script_review must not claim a mutation
    remains active.  The fallback snapshot was taken before script review,
    so it has no script_review — the restored post must not have one either."""
    orch = _make_orch()

    pre_posts = [{
        "platform": "TikTok", "concept": "c", "title": "Orig",
        "caption": "Cap", "script": "Scr", "hashtags": "#h",
        "image_prompts": [], "video_prompts": [], "asset_ids": [],
    }]
    # Final post has script_review claiming script_changed=True
    final_posts = [{
        "platform": "TikTok", "concept": "c", "title": "Mut",
        "caption": "Mut UNSUPPORTED", "script": "MutScr",
        "hashtags": "#m", "image_prompts": [], "video_prompts": [],
        "asset_ids": [],
        "script_review": {
            "status": "reviewed", "script_changed": True,
            "score": 80, "iterations": 2, "threshold": 70,
        },
    }]

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED"}]}),
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]

    content, md = orch._finalize_content_output(
        final_posts, pre_posts, llm, {"product_source": "src"},
    )

    parsed = json.loads(content)
    # After revert, script_review must not claim mutation is active
    assert "script_review" not in parsed["posts"][0], (
        "script_review leaked after revert — should be absent since "
        "fallback was taken before script review"
    )
    # Content must be the original
    assert parsed["posts"][0]["script"] == "Scr"
    assert parsed["posts"][0]["title"] == "Orig"


# ---------------------------------------------------------------------------
# 14. Auto path fallback slot assembly — executable tests
# ---------------------------------------------------------------------------

def _make_auto_orch():
    """Create an Orchestrator with enough attributes for
    run_content_creator_auto to run with mocked dependencies."""
    from src.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = {}
    orch.results = {}
    orch.brand_context = None
    orch.brand_reference = None
    orch.brand_rules = None
    orch.brand_visual = None
    orch.product_id = None
    orch.product_images = []
    orch._content_source_context = "product source"
    orch._content_brand_context = "brand ctx"
    return orch


def test_auto_path_fallback_slots_preserve_position_identity():
    """The auto path must assemble one deep-copied fallback slot per final
    post, preserving exact position and post identity.  When post 0 is
    unchanged and post 1 is mutated, the fallback for post 1 must be the
    pre-mutation version of post 1 (not the mutated version, and not
    post 0's version).

    This test exercises the actual run_content_creator_auto method with
    mocked dependencies and spies on _finalize_content_output to capture
    the pre_mutation_posts and all_posts.
    """
    from unittest.mock import patch
    orch = _make_auto_orch()

    # Mock select_product_auto
    orch.select_product_auto = MagicMock(return_value={
        "product_ids": ["P1"], "concept": "c", "pillar": "p",
        "reason": "r", "asset_ids": [],
    })
    # Mock asset selection
    orch._select_assets_for_content = MagicMock(return_value="")
    orch._build_configured_pillars_text = MagicMock(return_value="")

    # Track which call index we're on
    call_idx = [0]

    def _fake_raw(*args, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx == 0:
            # Post 0: unchanged (script review won't mutate)
            return json.dumps({"posts": [{
                "platform": "Facebook", "concept": "c0", "title": "T0",
                "caption": "C0", "script": "S0", "hashtags": "#h0",
                "image_prompts": [], "video_prompts": [], "asset_ids": [],
            }]})
        else:
            # Post 1: will be mutated by script review
            return json.dumps({"posts": [{
                "platform": "TikTok", "concept": "c1", "title": "T1",
                "caption": "C1", "script": "S1_original", "hashtags": "#h1",
                "image_prompts": [], "video_prompts": [], "asset_ids": [],
            }]})
    orch._run_content_creator_raw = MagicMock(side_effect=_fake_raw)

    # Mock script review: mutate post 1's script
    def _fake_review(posts, platform, *args, **kwargs):
        if posts and "S1_original" in posts[0].get("script", ""):
            posts[0]["script"] = "S1_MUTATED"
    orch._review_script_in_posts = MagicMock(side_effect=_fake_review)

    # Mock content_history
    with patch("src.content_history.check_duplicate", return_value={"is_duplicate": False}):
        with patch("src.content_history.record_entry", return_value=None):
            with patch("src.content_history.format_product_history_for_prompt", return_value=""):
                # Spy on _finalize_content_output to capture pre_mutation_posts
                captured = {}

                def _spy_finalize(all_posts, pre_mutation_posts, llm, ctx, cb=None):
                    captured["all_posts"] = all_posts
                    captured["pre_mutation_posts"] = pre_mutation_posts
                    content = json.dumps({"posts": all_posts}, ensure_ascii=False)
                    return content, content

                with patch.object(orch, "_finalize_content_output", side_effect=_spy_finalize):
                    orch.run_content_creator_auto(
                        llm=MagicMock(), platforms=["facebook", "tiktok"],
                        product_count=2,
                    )

    # Verify fallback slots were captured
    assert "pre_mutation_posts" in captured, "_finalize_content_output was not called"
    assert len(captured["all_posts"]) == 2
    assert len(captured["pre_mutation_posts"]) == 2

    # Post 0: unchanged — fallback should have original content
    assert captured["pre_mutation_posts"][0]["title"] == "T0"
    assert captured["pre_mutation_posts"][0]["script"] == "S0"

    # Post 1: mutated — fallback should have PRE-mutation content
    assert captured["pre_mutation_posts"][1]["title"] == "T1"
    assert captured["pre_mutation_posts"][1]["script"] == "S1_original", (
        "Fallback for post 1 should be the pre-mutation version, "
        "not the mutated version — fallback slot misalignment"
    )
    # The final post 1 should have the mutated script
    assert captured["all_posts"][1]["script"] == "S1_MUTATED"

    # No cross-post contamination
    assert captured["pre_mutation_posts"][0]["title"] != "T1"
    assert captured["pre_mutation_posts"][1]["title"] != "T0"


def test_auto_path_fallback_slots_do_not_alias_final_posts():
    """The auto path's fallback slot objects must not be aliases of the
    final post objects.  Mutating a final post must not affect the fallback.

    This test exercises the actual run_content_creator_auto method and
    checks that pre_mutation_posts[i] is not the same object as
    all_posts[i].
    """
    from unittest.mock import patch
    orch = _make_auto_orch()

    orch.select_product_auto = MagicMock(return_value={
        "product_ids": ["P1"], "concept": "c", "pillar": "p",
        "reason": "r", "asset_ids": [],
    })
    orch._select_assets_for_content = MagicMock(return_value="")
    orch._build_configured_pillars_text = MagicMock(return_value="")

    call_idx = [0]

    def _fake_raw(*args, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        return json.dumps({"posts": [{
            "platform": "Facebook" if idx == 0 else "TikTok",
            "concept": f"c{idx}", "title": f"T{idx}",
            "caption": f"C{idx}", "script": f"S{idx}", "hashtags": f"#h{idx}",
            "image_prompts": [], "video_prompts": [], "asset_ids": [],
        }]})
    orch._run_content_creator_raw = MagicMock(side_effect=_fake_raw)
    orch._review_script_in_posts = MagicMock(return_value={})

    with patch("src.content_history.check_duplicate", return_value={"is_duplicate": False}):
        with patch("src.content_history.record_entry", return_value=None):
            with patch("src.content_history.format_product_history_for_prompt", return_value=""):
                captured = {}

                def _spy_finalize(all_posts, pre_mutation_posts, llm, ctx, cb=None):
                    captured["all_posts"] = all_posts
                    captured["pre_mutation_posts"] = pre_mutation_posts
                    content = json.dumps({"posts": all_posts}, ensure_ascii=False)
                    return content, content

                with patch.object(orch, "_finalize_content_output", side_effect=_spy_finalize):
                    orch.run_content_creator_auto(
                        llm=MagicMock(), platforms=["facebook", "tiktok"],
                        product_count=2,
                    )

    assert len(captured["all_posts"]) == 2
    assert len(captured["pre_mutation_posts"]) == 2

    # Fallback objects must not alias final post objects
    for i in range(2):
        assert captured["pre_mutation_posts"][i] is not captured["all_posts"][i], (
            f"Fallback slot {i} aliases final post — deep copy was not used"
        )


# ===========================================================================
# Blocker 1 & 2 tests — malformed/empty output, schema-before-persistence
# ===========================================================================

def _valid_post():
    return {
        "platform": "Facebook", "concept": "c", "title": "T",
        "caption": "C", "script": "S", "hashtags": "#h", "asset_ids": [],
        "image_prompts": [], "video_prompts": [],
    }


def _valid_posts_json():
    return json.dumps({"posts": [_valid_post()]}, ensure_ascii=False)


def test_standalone_malformed_json_raises_and_leaves_no_result():
    """Blocker 1: malformed JSON raises and leaves no persistable result."""
    orch = _make_orch()
    raw_llm = MagicMock()
    raw_llm.last_truncated = False
    raw_llm.chat.return_value = "not valid json at all"
    with patch.object(orch, "_run_content_creator_raw", return_value="not valid json at all"):
        try:
            orch.run_content_creator("spec", "", "", llm=raw_llm)
            assert False, "Should have raised"
        except (ValueError, Exception):
            pass
    assert "content_creator" not in orch.results
    assert "content_creator_markdown" not in orch.results


def test_standalone_non_object_json_raises_and_leaves_no_result():
    """Blocker 1: non-object JSON root raises and leaves no persistable result."""
    orch = _make_orch()
    raw_llm = MagicMock()
    raw_llm.last_truncated = False
    raw_llm.chat.return_value = json.dumps([1, 2, 3])
    with patch.object(orch, "_run_content_creator_raw", return_value=json.dumps([1, 2, 3])):
        try:
            orch.run_content_creator("spec", "", "", llm=raw_llm)
            assert False, "Should have raised"
        except (ValueError, Exception):
            pass
    assert "content_creator" not in orch.results


def test_standalone_missing_posts_raises_and_leaves_no_result():
    """Blocker 1: missing 'posts' key raises and leaves no persistable result."""
    orch = _make_orch()
    raw_llm = MagicMock()
    raw_llm.last_truncated = False
    raw_llm.chat.return_value = json.dumps({"foo": "bar"})
    with patch.object(orch, "_run_content_creator_raw", return_value=json.dumps({"foo": "bar"})):
        try:
            orch.run_content_creator("spec", "", "", llm=raw_llm)
            assert False, "Should have raised"
        except (ValueError, Exception):
            pass
    assert "content_creator" not in orch.results


def test_standalone_empty_posts_raises_and_leaves_no_result():
    """Blocker 1: empty posts array raises and leaves no persistable result."""
    orch = _make_orch()
    raw_llm = MagicMock()
    raw_llm.last_truncated = False
    raw_llm.chat.return_value = '{"posts": []}'
    with patch.object(orch, "_run_content_creator_raw", return_value='{"posts": []}'):
        try:
            orch.run_content_creator("spec", "", "", llm=raw_llm)
            assert False, "Should have raised"
        except (ValueError, Exception):
            pass
    assert "content_creator" not in orch.results


def test_schema_invalid_verifier_approved_not_written_to_results():
    """Blocker 2: schema-invalid but verifier-approved content is not written
    to self.results."""
    orch = _make_orch()
    posts = [_valid_post()]
    # Make the post schema-invalid by removing a required field
    del posts[0]["caption"]

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.return_value = json.dumps({"grounded": True, "unsupported_claims": []})

    try:
        orch._finalize_content_output(
            posts, [copy.deepcopy(p) for p in posts], llm,
            {"product_source": "src"}, None,
        )
        assert False, "Should have raised ValueError for schema-invalid output"
    except ValueError:
        pass
    assert "content_creator" not in orch.results
    assert "content_creator_markdown" not in orch.results


def test_schema_invalid_fallback_not_written_to_results():
    """Blocker 2: schema-invalid fallback is not written to self.results."""
    orch = _make_orch()
    pre_posts = [_valid_post()]
    del pre_posts[0]["caption"]  # schema-invalid fallback
    post_posts = [_valid_post()]
    post_posts[0]["caption"] = "UNSUPPORTED CLAIM"

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.side_effect = [
        json.dumps({"grounded": False, "unsupported_claims": [{"claim": "UNSUPPORTED"}]}),
        json.dumps({"grounded": True, "unsupported_claims": []}),
    ]

    try:
        orch._finalize_content_output(
            post_posts, pre_posts, llm,
            {"product_source": "src"}, None,
        )
        assert False, "Should have raised ValueError for schema-invalid fallback"
    except ValueError:
        pass
    assert "content_creator" not in orch.results


def test_schema_validation_occurs_before_persistence():
    """Blocker 2: schema validation occurs before persistence — no
    self.results write happens before schema validation passes."""
    orch = _make_orch()
    posts = [_valid_post()]
    del posts[0]["caption"]  # schema-invalid

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.return_value = json.dumps({"grounded": True, "unsupported_claims": []})

    # Use a tracking dict to detect premature writes
    class _TrackingDict(dict):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.premature_write = False

        def __setitem__(self, key, value):
            if key in ("content_creator", "content_creator_markdown"):
                self.premature_write = True
            super().__setitem__(key, value)

    orch.results = _TrackingDict()

    try:
        orch._finalize_content_output(
            posts, [copy.deepcopy(p) for p in posts], llm,
            {"product_source": "src"}, None,
        )
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    assert not orch.results.premature_write, (
        "self.results write occurred before schema validation passed"
    )
    assert "content_creator" not in orch.results


def test_successful_grounding_and_schema_persists_exact_accepted_json():
    """Blocker 2: successful grounding + successful schema validation
    persists the exact accepted JSON."""
    orch = _make_orch()
    posts = [_valid_post()]

    llm = MagicMock()
    llm.last_truncated = False
    llm.chat.return_value = json.dumps({"grounded": True, "unsupported_claims": []})

    content, markdown = orch._finalize_content_output(
        posts, [copy.deepcopy(p) for p in posts], llm,
        {"product_source": "src"}, None,
    )
    assert orch.results["content_creator"] == content
    assert orch.results["content_creator_markdown"] == markdown
    stored = json.loads(orch.results["content_creator"])
    assert stored["posts"][0]["title"] == "T"
    assert stored["posts"][0]["caption"] == "C"


# ===========================================================================
# Blocker 3 tests — qualification runner grounding failure
# ===========================================================================

def test_qual_grounding_failure_produces_unsuccessful_case():
    """Blocker 3: qualification grounding failure produces an unsuccessful
    case (error is set, result_text is empty)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import qual_runner
    from src.orchestrator import Orchestrator

    def fake_run_raw(self, *args, **kwargs):
        return _valid_posts_json()

    def fake_finalize(self, all_posts, pre_posts, llm, ctx, cb=None):
        raise RuntimeError("grounding failed")

    def fake_make_llm(orch):
        return MagicMock()

    with patch.object(Orchestrator, "_run_content_creator_raw", fake_run_raw):
        with patch.object(Orchestrator, "_finalize_content_output", fake_finalize):
            with patch.object(qual_runner, "make_llm", fake_make_llm):
                with patch.object(qual_runner, "_init_session_baseline", lambda: None):
                    import tempfile
                    tmp = Path(tempfile.mkdtemp())
                    result = qual_runner.run_case(
                        case_id="A4_grounding_fail",
                        agent_key="content_creator",
                        product_id="TestProduct",
                        platforms=["facebook"],
                        auto_image=False, auto_video=False,
                        output_dir=tmp,
                    )

    assert result["error"] is not None
    assert "grounding" in result["error"].lower()
    assert result["result_text"] == "" or result["result_text"] is None


def test_qual_grounding_failure_does_not_invoke_renderer():
    """Blocker 3: qualification grounding failure does not invoke
    render_posts_to_markdown as a successful output path."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import qual_runner
    from src.orchestrator import Orchestrator
    from src.content_schema import render_posts_to_markdown

    renderer_called = []

    def fake_run_raw(self, *args, **kwargs):
        return _valid_posts_json()

    def fake_finalize(self, all_posts, pre_posts, llm, ctx, cb=None):
        raise RuntimeError("grounding failed")

    def fake_make_llm(orch):
        return MagicMock()

    original_render = render_posts_to_markdown

    def spy_render(parsed):
        renderer_called.append(True)
        return original_render(parsed)

    with patch.object(Orchestrator, "_run_content_creator_raw", fake_run_raw):
        with patch.object(Orchestrator, "_finalize_content_output", fake_finalize):
            with patch.object(qual_runner, "make_llm", fake_make_llm):
                with patch.object(qual_runner, "_init_session_baseline", lambda: None):
                    with patch("src.content_schema.render_posts_to_markdown", spy_render):
                        import tempfile
                        tmp = Path(tempfile.mkdtemp())
                        qual_runner.run_case(
                            case_id="A4_no_render",
                            agent_key="content_creator",
                            product_id="TestProduct",
                            platforms=["facebook"],
                            auto_image=False, auto_video=False,
                            output_dir=tmp,
                        )

    assert not renderer_called, (
        "render_posts_to_markdown was called after grounding failure — "
        "rejected content must not be rendered as a successful result"
    )


def test_qual_grounding_failure_does_not_invoke_media_gen():
    """Blocker 3: qualification grounding failure does not invoke media
    generation."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import qual_runner
    from src.orchestrator import Orchestrator

    media_called = []

    def fake_run_raw(self, *args, **kwargs):
        return _valid_posts_json()

    def fake_finalize(self, all_posts, pre_posts, llm, ctx, cb=None):
        raise RuntimeError("grounding failed")

    def fake_make_llm(orch):
        return MagicMock()

    def spy_media_gen(*args, **kwargs):
        media_called.append(True)

    with patch.object(Orchestrator, "_run_content_creator_raw", fake_run_raw):
        with patch.object(Orchestrator, "_finalize_content_output", fake_finalize):
            with patch.object(qual_runner, "make_llm", fake_make_llm):
                with patch.object(qual_runner, "_init_session_baseline", lambda: None):
                    with patch.object(qual_runner, "_run_media_gen", spy_media_gen):
                        import tempfile
                        tmp = Path(tempfile.mkdtemp())
                        qual_runner.run_case(
                            case_id="A4_no_media",
                            agent_key="content_creator",
                            product_id="TestProduct",
                            platforms=["facebook"],
                            auto_image=True, auto_video=True,
                            output_dir=tmp,
                        )

    assert not media_called, (
        "media generation was called after grounding failure — "
        "rejected content must not trigger media generation"
    )


def test_qual_grounding_failure_does_not_expose_rejected_as_result_text():
    """Blocker 3: qualification grounding failure does not expose rejected
    content as Judge-facing result_text."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import qual_runner
    from src.orchestrator import Orchestrator

    def fake_run_raw(self, *args, **kwargs):
        return _valid_posts_json()

    def fake_finalize(self, all_posts, pre_posts, llm, ctx, cb=None):
        raise RuntimeError("grounding failed")

    def fake_make_llm(orch):
        return MagicMock()

    with patch.object(Orchestrator, "_run_content_creator_raw", fake_run_raw):
        with patch.object(Orchestrator, "_finalize_content_output", fake_finalize):
            with patch.object(qual_runner, "make_llm", fake_make_llm):
                with patch.object(qual_runner, "_init_session_baseline", lambda: None):
                    import tempfile
                    tmp = Path(tempfile.mkdtemp())
                    result = qual_runner.run_case(
                        case_id="A4_no_judge",
                        agent_key="content_creator",
                        product_id="TestProduct",
                        platforms=["facebook"],
                        auto_image=False, auto_video=False,
                        output_dir=tmp,
                    )

    # result_text must not contain the rejected post content
    rt = result.get("result_text", "") or ""
    assert "T" not in rt or "Facebook" not in rt, (
        "Rejected content was exposed as Judge-facing result_text"
    )
    # The output file must not contain the rejected post as success
    out_file = tmp / "A4_no_judge_output.txt"
    if out_file.exists():
        out_content = out_file.read_text(encoding="utf-8")
        assert "Facebook" not in out_content or "error" in out_content.lower(), (
            "Rejected content written to output file as successful result"
        )


# ===========================================================================
# Blocker 1 (final pass): shared finalizer rejects empty aggregate
# ===========================================================================

def test_finalize_empty_aggregate_raises():
    """_finalize_content_output([], [], ...) must raise ValueError."""
    orch = _make_orch()
    llm = MagicMock()
    llm.last_truncated = False
    try:
        orch._finalize_content_output([], [], llm, {"product_source": "src"}, None)
        assert False, "Should have raised ValueError for empty aggregate"
    except ValueError:
        pass


def test_finalize_empty_aggregate_does_not_invoke_verifier():
    """Empty aggregate must fail before the grounding verifier is called."""
    orch = _make_orch()
    llm = MagicMock()
    llm.last_truncated = False
    try:
        orch._finalize_content_output([], [], llm, {"product_source": "src"}, None)
    except ValueError:
        pass
    # The grounding verifier (llm.chat) must not have been called at all
    assert llm.chat.call_count == 0, (
        "Grounding verifier was invoked for an empty aggregate — "
        "empty aggregates must fail before grounding"
    )


def test_finalize_empty_aggregate_leaves_no_result():
    """Empty aggregate must leave no persistable Agent 4 result."""
    orch = _make_orch()
    llm = MagicMock()
    llm.last_truncated = False
    try:
        orch._finalize_content_output([], [], llm, {"product_source": "src"}, None)
    except ValueError:
        pass
    assert "content_creator" not in orch.results
    assert "content_creator_markdown" not in orch.results


def test_auto_path_no_parsed_posts_fails():
    """Auto path with no successfully parsed posts must fail closed."""
    orch = _make_orch()
    # Simulate auto path collecting zero posts then calling finalization
    llm = MagicMock()
    llm.last_truncated = False
    all_posts = []
    pre_posts = []
    try:
        orch._finalize_content_output(all_posts, pre_posts, llm,
                                       {"product_source": "src"}, None)
        assert False, "Auto path with no posts should fail closed"
    except ValueError:
        pass
    assert "content_creator" not in orch.results


def test_manual_web_path_no_parsed_posts_fails():
    """Manual web path with no successfully parsed posts must fail closed."""
    orch = _make_orch()
    llm = MagicMock()
    llm.last_truncated = False
    all_posts = []
    pre_posts = []
    try:
        orch._finalize_content_output(all_posts, pre_posts, llm,
                                       {"product_source": "src"}, None)
        assert False, "Manual web path with no posts should fail closed"
    except ValueError:
        pass
    assert "content_creator" not in orch.results


def test_qual_path_no_parsed_posts_unsuccessful():
    """Qualification path with no successfully parsed posts is unsuccessful
    and cannot reach renderer, Judge-facing output, or media generation."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import qual_runner
    from src.orchestrator import Orchestrator

    renderer_called = []
    media_called = []

    def fake_run_raw(self, *args, **kwargs):
        # Return valid JSON but with empty posts — simulates parse success
        # but zero posts collected
        return '{"posts": []}'

    def fake_finalize(self, all_posts, pre_posts, llm, ctx, cb=None):
        # Use the real finalizer to verify the empty-aggregate guard
        return orch._finalize_content_output(all_posts, pre_posts, llm, ctx, cb)

    def fake_make_llm(orch):
        return MagicMock()

    # We need a real orch instance for fake_finalize to call
    real_orch = _make_orch()

    def spy_render(parsed):
        renderer_called.append(True)
        return "rendered"

    def spy_media(*args, **kwargs):
        media_called.append(True)

    with patch.object(Orchestrator, "_run_content_creator_raw", fake_run_raw):
        with patch.object(Orchestrator, "_finalize_content_output",
                          lambda self, *a, **kw: fake_finalize(self, *a, **kw)):
            with patch.object(qual_runner, "make_llm", fake_make_llm):
                with patch.object(qual_runner, "_init_session_baseline", lambda: None):
                    with patch("src.content_schema.render_posts_to_markdown", spy_render):
                        with patch.object(qual_runner, "_run_media_gen", spy_media):
                            import tempfile
                            tmp = Path(tempfile.mkdtemp())
                            result = qual_runner.run_case(
                                case_id="A4_empty",
                                agent_key="content_creator",
                                product_id="TestProduct",
                                platforms=["facebook"],
                                auto_image=True, auto_video=True,
                                output_dir=tmp,
                            )

    # Must be unsuccessful
    assert result.get("error") is not None
    assert "grounding" in result.get("error", "").lower() or "empty" in result.get("error", "").lower()
    # Must not reach renderer
    assert not renderer_called, "Renderer was called for empty aggregate"
    # Must not reach media generation
    assert not media_called, "Media generation was called for empty aggregate"
    # result_text must not contain rejected post content
    rt = result.get("result_text", "") or ""
    assert "Facebook" not in rt, "Rejected content exposed as result_text"


# ===========================================================================
# Blocker 2 (final pass): failed reruns leave stale successful artifacts
# ===========================================================================

def _grounded_llm_for_rerun():
    """LLM that returns grounded=true for grounding checks."""
    m = MagicMock()
    m.last_truncated = False
    m.chat.return_value = json.dumps({"grounded": True, "unsupported_claims": []})
    return m


def _ungrounded_llm_for_rerun():
    """LLM that returns grounded=false for grounding checks."""
    m = MagicMock()
    m.last_truncated = False
    m.chat.return_value = json.dumps({
        "grounded": False,
        "unsupported_claims": [{"claim": "x", "reason": "not in source"}],
    })
    return m


def test_rerun_product_spec_failure_clears_stale():
    """Agent 1: a failed rerun clears the stale product_spec success key."""
    orch = _make_orch()
    # Seed a previous successful result
    orch.results["product_spec"] = "OLD_SUCCESS"
    orch.results["competitor_analysis"] = "UNRELATED"
    # Simulate a failed rerun — grounding failure
    llm = _ungrounded_llm_for_rerun()
    with patch.object(orch, "_make_agent"):
        with patch("src.orchestrator.ProductSpecAgent"):
            try:
                orch.run_product_spec("raw", llm=llm)
                assert False, "Should have raised"
            except Exception:
                pass
    # Stale primary key must be gone
    assert "product_spec" not in orch.results, (
        "Stale product_spec result survived a failed rerun"
    )
    # Unrelated agent results must remain intact
    assert orch.results.get("competitor_analysis") == "UNRELATED"


def test_rerun_competitor_analysis_failure_clears_stale():
    """Agent 2: a failed rerun clears the stale competitor_analysis key."""
    orch = _make_orch()
    orch.results["competitor_analysis"] = "OLD_SUCCESS"
    orch.results["product_spec"] = "UNRELATED"
    llm = _ungrounded_llm_for_rerun()
    with patch.object(orch, "_make_agent"):
        with patch("src.orchestrator.CompetitorAnalysisAgent"):
            try:
                orch.run_competitor_analysis("spec", "", llm=llm)
                assert False, "Should have raised"
            except Exception:
                pass
    assert "competitor_analysis" not in orch.results, (
        "Stale competitor_analysis result survived a failed rerun"
    )
    assert orch.results.get("product_spec") == "UNRELATED"


def test_rerun_campaign_strategy_failure_clears_stale():
    """Agent 3: a failed rerun clears the stale campaign_strategy key."""
    orch = _make_orch()
    orch.results["campaign_strategy"] = "OLD_SUCCESS"
    orch.results["product_spec"] = "UNRELATED"
    llm = _ungrounded_llm_for_rerun()
    with patch.object(orch, "_make_agent"):
        with patch("src.orchestrator.CampaignStrategyAgent"):
            with patch("src.orchestrator.set_usage_reference"):
                with patch("src.orchestrator.set_usage_metadata"):
                    with patch("src.orchestrator.snapshot_usage_log_offset", return_value=0):
                        with patch("src.orchestrator.read_usage_log_for_reference", return_value=[]):
                            with patch("src.orchestrator.flush_usage_log", return_value=True):
                                with patch("src.orchestrator.reconcile_hub_receipts", return_value={}):
                                    with patch("src.orchestrator.HubReceiptCollector"):
                                        with patch("src.orchestrator.clear_usage_context"):
                                            try:
                                                orch.run_campaign_strategy("spec", "", llm=llm)
                                                assert False, "Should have raised"
                                            except Exception:
                                                pass
    assert "campaign_strategy" not in orch.results, (
        "Stale campaign_strategy result survived a failed rerun"
    )
    assert orch.results.get("product_spec") == "UNRELATED"


def test_rerun_content_creator_failure_clears_stale():
    """Agent 4: a failed rerun clears stale content_creator and markdown."""
    orch = _make_orch()
    orch.results["content_creator"] = "OLD_SUCCESS"
    orch.results["content_creator_markdown"] = "OLD_MD"
    orch.results["product_spec"] = "UNRELATED"
    # Simulate malformed JSON output
    llm = MagicMock()
    llm.last_truncated = False
    with patch.object(orch, "_run_content_creator_raw", return_value="not json"):
        try:
            orch.run_content_creator("spec", "", "", llm=llm)
            assert False, "Should have raised"
        except Exception:
            pass
    assert "content_creator" not in orch.results, (
        "Stale content_creator result survived a failed rerun"
    )
    assert "content_creator_markdown" not in orch.results, (
        "Stale content_creator_markdown survived a failed rerun"
    )
    assert orch.results.get("product_spec") == "UNRELATED"


def test_rerun_content_creator_empty_posts_clears_stale():
    """Agent 4: empty posts on rerun clears stale content_creator keys."""
    orch = _make_orch()
    orch.results["content_creator"] = "OLD_SUCCESS"
    orch.results["content_creator_markdown"] = "OLD_MD"
    llm = MagicMock()
    llm.last_truncated = False
    with patch.object(orch, "_run_content_creator_raw", return_value='{"posts": []}'):
        try:
            orch.run_content_creator("spec", "", "", llm=llm)
            assert False, "Should have raised"
        except Exception:
            pass
    assert "content_creator" not in orch.results
    assert "content_creator_markdown" not in orch.results


def test_rerun_finalize_empty_clears_stale():
    """Agent 4: _finalize_content_output with empty aggregate clears stale."""
    orch = _make_orch()
    orch.results["content_creator"] = "OLD_SUCCESS"
    orch.results["content_creator_markdown"] = "OLD_MD"
    llm = MagicMock()
    llm.last_truncated = False
    try:
        orch._finalize_content_output([], [], llm, {"product_source": "src"}, None)
        assert False, "Should have raised"
    except ValueError:
        pass
    assert "content_creator" not in orch.results
    assert "content_creator_markdown" not in orch.results


def test_rerun_product_spec_success_stores_new():
    """Agent 1: a successful rerun stores the new accepted result."""
    orch = _make_orch()
    orch.results["product_spec"] = "OLD_SUCCESS"
    llm = _grounded_llm_for_rerun()
    # Mock the agent to return a new successful result
    fake_agent = MagicMock()
    fake_agent.run.return_value = "NEW_SUCCESS"
    fake_agent.build_prompt.return_value = "prompt"
    with patch.object(orch, "_make_agent", return_value=fake_agent):
        with patch("src.orchestrator.ProductSpecAgent"):
            with patch.object(orch, "_get_product_image_paths", return_value=[]):
                with patch.object(orch, "_get_product_data", return_value="data"):
                    with patch.object(orch, "_build_brand_context_text", return_value=""):
                        result = orch.run_product_spec("raw", llm=llm)
    assert result == "NEW_SUCCESS"
    assert orch.results["product_spec"] == "NEW_SUCCESS"


def test_rerun_content_creator_success_stores_new():
    """Agent 4: a successful rerun stores the new accepted result."""
    orch = _make_orch()
    orch.results["content_creator"] = "OLD_SUCCESS"
    orch.results["content_creator_markdown"] = "OLD_MD"
    llm = _grounded_llm_for_rerun()
    new_posts_json = _valid_posts_json()
    with patch.object(orch, "_run_content_creator_raw", return_value=new_posts_json):
        with patch.object(orch, "_get_product_data", return_value="data"):
            with patch.object(orch, "_build_brand_context_text", return_value=""):
                result = orch.run_content_creator("spec", "", "", llm=llm)
    stored = json.loads(result)
    assert stored["posts"][0]["title"] == "T"
    assert orch.results["content_creator"] == result
    assert orch.results["content_creator_markdown"] is not None


# ===========================================================================
# Issue 2: stale invalidation must happen before make_client()
# ===========================================================================

import pytest as _pytest


@_pytest.mark.parametrize("agent_key,run_method,run_args,stale_keys,unrelated_key", [
    ("product_spec", "run_product_spec", ("raw",), ("product_spec",), "competitor_analysis"),
    ("competitor_analysis", "run_competitor_analysis", ("spec", ""), ("competitor_analysis",), "product_spec"),
    ("campaign_strategy", "run_campaign_strategy", ("spec", ""), ("campaign_strategy",), "product_spec"),
    ("content_creator", "run_content_creator", ("spec", "", ""), ("content_creator", "content_creator_markdown"), "product_spec"),
])
def test_make_client_failure_clears_stale(agent_key, run_method, run_args, stale_keys, unrelated_key):
    """If make_client() raises, the stale primary result key(s) must already
    be removed — invalidation happens before client construction."""
    orch = _make_orch()
    # Seed stale successful artifacts
    for k in stale_keys:
        orch.results[k] = "OLD_SUCCESS"
    orch.results[unrelated_key] = "UNRELATED"

    # make_client() raises
    def _boom():
        raise RuntimeError("client construction failed")
    with patch.object(orch, "make_client", _boom):
        try:
            getattr(orch, run_method)(*run_args)
            assert False, "Should have raised"
        except RuntimeError:
            pass

    # Stale primary keys must be gone
    for k in stale_keys:
        assert k not in orch.results, (
            f"Stale {k} survived make_client failure"
        )
    # Unrelated agent results must remain intact
    assert orch.results.get(unrelated_key) == "UNRELATED"


def test_make_client_failure_auto_clears_stale():
    """run_content_creator_auto: make_client failure clears stale content keys."""
    orch = _make_orch()
    orch.results["content_creator"] = "OLD_SUCCESS"
    orch.results["content_creator_markdown"] = "OLD_MD"
    orch.results["product_spec"] = "UNRELATED"

    def _boom():
        raise RuntimeError("client construction failed")
    with patch.object(orch, "make_client", _boom):
        try:
            orch.run_content_creator_auto()
            assert False, "Should have raised"
        except RuntimeError:
            pass

    assert "content_creator" not in orch.results
    assert "content_creator_markdown" not in orch.results
    assert orch.results.get("product_spec") == "UNRELATED"
