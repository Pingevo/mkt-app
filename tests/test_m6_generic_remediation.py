"""M6 Generic Remediation — offline tests for source-driven M6 fixes.

Tests the smallest generic M6 remediation on top of G3 baseline:
  P0-A: Product Spec one-page normalization + repairable compactness validation
  P0-B: Content Creator semantic self-review (review_prompt enhancement)
  P0-C: Brand hard-rule deterministic enforcement (terms.json/voice.json)
  P0-D: Repair classification/cost control in BaseAgent

Constraints:
  - No model/web/image/video calls
  - No Lagenio/K2/K3-specific logic, no smartwatch-specific logic
  - No static forbidden-claim vocabularies
  - No CPU/OS/RAM/ROM field lists
  - Must preserve G3 cross-domain behavior

Seams under test:
  1. normalize_one_page_brief(quick_brief) -> str  (output_validators)
  2. validate_one_page_compactness(output, quick_brief) -> (ok, error)  (output_validators)
  3. apply_brand_replacements(output, brand_rules) -> str  (output_validators)
  4. validate_brand_hard(output, brand_rules) -> (ok, error)  (output_validators)
  5. BaseAgent repair loop error classification  (base_agent)
  6. ProductSpecAgent._normalize_quick_brief  (product_spec)
  7. content_creator review_prompt config  (agents.yaml)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Slice 1: normalize_one_page_brief — translate "one-page" to concrete target
# ---------------------------------------------------------------------------

def test_normalize_one_page_brief_adds_concrete_target():
    """quick_brief='one-page' → appended concrete compactness target."""
    from src.output_validators import normalize_one_page_brief
    result = normalize_one_page_brief("one-page")
    assert "one-page" in result
    # Must contain a concrete line target (not just "one-page")
    assert "บรรทัด" in result or "lines" in result.lower()


def test_normalize_one_page_brief_preserves_other_briefs():
    """quick_brief without one-page → unchanged."""
    from src.output_validators import normalize_one_page_brief
    result = normalize_one_page_brief("สั้นกระชับ")
    assert result == "สั้นกระชับ"


def test_normalize_one_page_brief_preserves_empty():
    """Empty quick_brief → unchanged (empty string)."""
    from src.output_validators import normalize_one_page_brief
    result = normalize_one_page_brief("")
    assert result == ""


def test_normalize_one_page_brief_handles_one_page_in_sentence():
    """quick_brief containing 'one-page' as part of a longer instruction → still normalized."""
    from src.output_validators import normalize_one_page_brief
    result = normalize_one_page_brief("ทำ one-page เน้นราคา")
    assert "บรรทัด" in result or "lines" in result.lower()


# ---------------------------------------------------------------------------
# Slice 2: validate_one_page_compactness — line count check
# ---------------------------------------------------------------------------

def _make_lines(n: int) -> str:
    """Generate n lines of text."""
    return "\n".join(f"บรรทัดที่ {i}" for i in range(1, n + 1))


def test_one_page_55_lines_passes():
    """Frontier historical S1 55-line output must pass one-page validation."""
    from src.output_validators import validate_one_page_compactness
    output = _make_lines(55)
    ok, error = validate_one_page_compactness(output, "one-page")
    assert ok, f"55 lines should pass: {error}"


def test_one_page_89_lines_triggers_correction():
    """Historical MKTApp S1 89-line output must trigger correction."""
    from src.output_validators import validate_one_page_compactness
    output = _make_lines(89)
    ok, error = validate_one_page_compactness(output, "one-page")
    assert not ok
    assert "one-page" in error or "บรรทัด" in error


def test_one_page_error_has_one_page_prefix():
    """Error must have 'one-page:' prefix for repair classification."""
    from src.output_validators import validate_one_page_compactness
    output = _make_lines(89)
    ok, error = validate_one_page_compactness(output, "one-page")
    assert not ok
    assert error.startswith("one-page:")


def test_non_one_page_no_line_cap():
    """Non-one-page product_spec output has no line cap."""
    from src.output_validators import validate_one_page_compactness
    output = _make_lines(200)
    ok, error = validate_one_page_compactness(output, "สั้นกระชับ")
    assert ok, f"200 lines without one-page should pass: {error}"


def test_non_one_page_empty_brief_no_cap():
    """Empty quick_brief → no line cap."""
    from src.output_validators import validate_one_page_compactness
    output = _make_lines(300)
    ok, error = validate_one_page_compactness(output, "")
    assert ok


# ---------------------------------------------------------------------------
# Slice 3: apply_brand_replacements — deterministic auto-replace
# ---------------------------------------------------------------------------

def _make_brand_rules(replacements=None, restricted=None, banned=None):
    """Create a BrandRules object for testing."""
    from src.brand_priority import BrandRules
    hard_dict = {}
    if replacements:
        hard_dict["replacements"] = dict(replacements)
    if restricted:
        hard_dict["restricted"] = list(restricted)
    if banned:
        hard_dict["banned_phrases"] = list(banned)
    return BrandRules(hard_dict=hard_dict)


def test_brand_replacement_auto_applied():
    """Brand replacements work from runtime brand data, not hardcoded."""
    from src.output_validators import apply_brand_replacements
    rules = _make_brand_rules(replacements={"ถูก": "คุ้มค่า"})
    output = "สินค้าราคาถูก คุณภาพดี"
    result = apply_brand_replacements(output, rules)
    assert "คุ้มค่า" in result
    assert "ถูก" not in result


def test_brand_replacement_no_rules_unchanged():
    """No brand rules → output unchanged."""
    from src.output_validators import apply_brand_replacements
    from src.brand_priority import BrandRules
    rules = BrandRules()
    output = "สินค้าราคาถูก"
    result = apply_brand_replacements(output, rules)
    assert result == output


def test_brand_replacement_multiple():
    """Multiple replacements applied in one pass."""
    from src.output_validators import apply_brand_replacements
    rules = _make_brand_rules(replacements={"ถูก": "คุ้มค่า", "ของแถม": "ของแถมฟรี"})
    output = "สินค้าถูก มีของแถม"
    result = apply_brand_replacements(output, rules)
    assert "คุ้มค่า" in result
    assert "ของแถมฟรี" in result


def test_brand_replacement_does_not_recurse():
    """Replacement should not recurse into already-replaced text."""
    from src.output_validators import apply_brand_replacements
    # If replacement creates a substring that matches another key, it should NOT re-replace
    rules = _make_brand_rules(replacements={"A": "AB", "B": "BC"})
    output = "A"
    result = apply_brand_replacements(output, rules)
    # A → AB, should NOT then replace B in AB → ABC
    assert result == "AB"


# ---------------------------------------------------------------------------
# Slice 4: validate_brand_hard — restricted/banned phrase check
# ---------------------------------------------------------------------------

def test_brand_restricted_phrase_detected():
    """Brand restricted phrases are enforced using runtime brand data."""
    from src.output_validators import validate_brand_hard
    rules = _make_brand_rules(restricted=["ลดแหลก", "ฟรี"])
    output = "สินค้าลดแหลก มากมาย"
    ok, error = validate_brand_hard(output, rules)
    assert not ok
    assert "brand-hard:" in error
    assert "ลดแหลก" in error


def test_brand_banned_phrase_detected():
    """Brand banned_phrases are enforced using runtime brand data."""
    from src.output_validators import validate_brand_hard
    rules = _make_brand_rules(banned=["ถูกที่สุด"])
    output = "สินค้าถูกที่สุดในโลก"
    ok, error = validate_brand_hard(output, rules)
    assert not ok
    assert "brand-hard:" in error


def test_brand_hard_no_violation_passes():
    """Output without restricted/banned phrases → passes."""
    from src.output_validators import validate_brand_hard
    rules = _make_brand_rules(restricted=["ลดแหลก"], banned=["ถูกที่สุด"])
    output = "สินค้าคุณภาพดี ราคาคุ้มค่า"
    ok, error = validate_brand_hard(output, rules)
    assert ok


def test_brand_hard_no_rules_passes():
    """No brand rules → passes."""
    from src.output_validators import validate_brand_hard
    from src.brand_priority import BrandRules
    rules = BrandRules()
    ok, error = validate_brand_hard("anything", rules)
    assert ok


def test_brand_hard_error_has_prefix():
    """Error must have 'brand-hard:' prefix for repair classification."""
    from src.output_validators import validate_brand_hard
    rules = _make_brand_rules(restricted=["ลดแหลก"])
    ok, error = validate_brand_hard("ลดแหลก", rules)
    assert error.startswith("brand-hard:")


# ---------------------------------------------------------------------------
# Slice 5: Error classification in repair loop (P0-D)
# ---------------------------------------------------------------------------

class FakeLLM:
    """Fake LLM that returns scripted outputs."""

    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = 0
        self.call_sources: list[str] = []
        self.last_truncated = False

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            return '{"grounded": true, "unsupported_claims": []}'
        out = self.outputs[self.calls % len(self.outputs)]
        self.calls += 1
        source = kwargs.get("source", "unknown")
        self.call_sources.append(source)
        return out

    def close(self):
        pass


def _make_dummy_agent_class():
    from src.agents.base_agent import BaseAgent

    class DummyAgent(BaseAgent):
        agent_name = "product_spec"
        display_name = "Test Agent"

        def build_prompt(self, *args, **kwargs):
            return ""

        # Override validate_output to inject test-specific validators
        def validate_output(self, output: str) -> tuple[bool, str]:
            return self._test_validate(output)

    return DummyAgent


def test_one_page_repair_limited_to_one_attempt():
    """One-page correction: maximum one targeted repair, then soft-accept."""
    DummyAgent = _make_dummy_agent_class()

    # 89-line output → one-page error → repair returns 80-line (still >70) → soft-accept
    long_output = _make_lines(89)
    medium_output = _make_lines(80)

    llm = FakeLLM([long_output, medium_output])
    config = {
        "system_prompt": "test",
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "max_retry_limit": 3,
        "model": "x",
        "temperature": 0.7,
        "max_tokens": 100,
    }
    agent = DummyAgent(config, llm)
    agent._test_validate = lambda output: (
        (False, "one-page: output ยาวเกินไป")
        if len(output.split("\n")) > 70 and "one-page" in getattr(agent, "_test_quick_brief", "")
        else (True, "")
    )
    agent._test_quick_brief = "one-page"

    result = agent.run("prompt", quick_brief="one-page")
    # Should soft-accept after 1 repair (not raise, not keep retrying)
    assert result is not None
    # Only 2 LLM calls: 1 generate + 1 repair (not 1 + 3)
    assert llm.calls == 2


def test_format_error_uses_existing_repair_budget():
    """Ordinary format errors still use max_retry_limit (not limited to 1)."""
    DummyAgent = _make_dummy_agent_class()

    llm = FakeLLM(["bad", "bad", "bad", "bad"])  # never passes
    config = {
        "system_prompt": "test",
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "max_retry_limit": 3,
        "model": "x",
        "temperature": 0.7,
        "max_tokens": 100,
    }
    agent = DummyAgent(config, llm)
    agent._test_validate = lambda output: (False, "format: ไม่ผ่าน")

    with pytest.raises(ValueError, match="format"):
        agent.run("prompt")
    # 1 generate + 3 repairs = 4 calls
    assert llm.calls == 4


def test_brand_hard_repair_limited_to_one_attempt():
    """Brand hard violation: targeted repair (1 attempt), then controlled
    failure if still invalid — never soft-accept a hard-rule violation."""
    DummyAgent = _make_dummy_agent_class()

    llm = FakeLLM(["ลดแหลก มาก", "ลดแหลก น้อย"])  # still has restricted word
    config = {
        "system_prompt": "test",
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "max_retry_limit": 3,
        "model": "x",
        "temperature": 0.7,
        "max_tokens": 100,
    }
    agent = DummyAgent(config, llm)
    agent._test_validate = lambda output: (False, "brand-hard: มีคำต้องห้าม")

    # Should raise after 1 repair attempt — never soft-accept brand-hard
    with pytest.raises(ValueError, match="brand-hard"):
        agent.run("prompt")
    assert llm.calls == 2  # 1 generate + 1 repair


# ---------------------------------------------------------------------------
# Slice 6: ProductSpecAgent one-page integration (P0-A)
# ---------------------------------------------------------------------------

def test_product_spec_one_page_normalization_in_prompt():
    """ProductSpecAgent with one-page quick_brief → prompt contains concrete target."""
    from src.agents.product_spec import ProductSpecAgent

    llm = FakeLLM(["สเปคสินค้า"])
    config = {
        "system_prompt": "test",
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "max_retry_limit": 0,
        "model": "x",
        "temperature": 0.7,
        "max_tokens": 100,
    }
    agent = ProductSpecAgent(config, llm)
    # Test the normalization hook
    result = agent._normalize_quick_brief("one-page")
    assert "บรรทัด" in result or "lines" in result.lower()


def test_product_spec_non_one_page_unchanged():
    """ProductSpecAgent without one-page → quick_brief unchanged."""
    from src.agents.product_spec import ProductSpecAgent

    llm = FakeLLM(["สเปคสินค้า"])
    config = {
        "system_prompt": "test",
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "max_retry_limit": 0,
        "model": "x",
        "temperature": 0.7,
        "max_tokens": 100,
    }
    agent = ProductSpecAgent(config, llm)
    result = agent._normalize_quick_brief("สั้นกระชับ")
    assert result == "สั้นกระชับ"


def test_base_agent_normalize_quick_brief_default():
    """BaseAgent._normalize_quick_brief returns quick_brief unchanged by default."""
    from src.agents.base_agent import BaseAgent

    class TestAgent(BaseAgent):
        agent_name = "test"
        display_name = "Test"

        def build_prompt(self, *args, **kwargs):
            return ""

    llm = FakeLLM(["output"])
    agent = TestAgent({}, llm)
    assert agent._normalize_quick_brief("anything") == "anything"


# ---------------------------------------------------------------------------
# Slice 7: Content creator review prompt (P0-B)
# ---------------------------------------------------------------------------

def test_content_creator_review_prompt_has_source_grounding():
    """Content creator review prompt receives source context and explicitly
    performs source-grounding review for caption/script/image/video prompts."""
    from src.config_loader import load_config, get_section
    cfg = load_config()
    cc_cfg = cfg.get("agents", {}).get("content_creator", {})
    review_prompt = cc_cfg.get("review_prompt", "")
    # Must explicitly mention checking claims against source
    assert "caption" in review_prompt.lower() or "แคปชัน" in review_prompt
    assert "script" in review_prompt.lower() or "สคริปต์" in review_prompt
    assert "image" in review_prompt.lower() or "รูป" in review_prompt
    assert "video" in review_prompt.lower() or "วิดีโอ" in review_prompt
    # Must mention source grounding
    assert "source" in review_prompt.lower() or "ข้อมูลต้นทาง" in review_prompt


# ---------------------------------------------------------------------------
# Slice 8: S2 residual semantic-grounding proof
# ---------------------------------------------------------------------------

def test_s2_semantic_grounding_residual_proof():
    """S2 residual semantic-grounding proof.

    Provenance validation (G2) checks that a URL is from a verified source
    and that the competitor identity matches. But it does NOT verify that
    the claim text is semantically entailed by the source content.

    This test demonstrates the residual gap: a claim attached to a valid
    source URL can exceed what the source actually supports.

    If this test shows the exaggerated claim appears in rendered output,
    that proves the residual gap exists and must be documented.
    """
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        CompetitorReportRenderer,
        ResearchResponse,
    )

    # Source content says only "กล้องหน้า 5MP" — nothing about video call
    source_annotation = {
        "url": "https://example.com/k2-specs",
        "title": "K2 Specifications",
        "content": "กล้องหน้า 5MP สำหรับถ่ายภาพ",
        "_relevance": {
            "relevant": True,
            "relevance_type": "competitor",
            "matched_competitor": "imoo Watch Phone Z7",
        },
    }

    # Evidence claim EXCEEDS source: adds "ใช้ video call" which source doesn't say
    evidence = CompetitorEvidence(
        competitor="imoo Watch Phone Z7",
        field="กล้อง",
        claim="กล้องหน้า 5MP ใช้ video call ได้",
        url="https://example.com/k2-specs",
        geography="thailand",
    )

    research = ResearchResponse(
        target_model="K2",
        competitor_names=["imoo Watch Phone Z7"],
        evidence=[evidence],
    )

    renderer = CompetitorReportRenderer(
        research=research,
        relevant_annotations=[source_annotation],
    )

    # Validate evidence — provenance check should PASS (URL is verified, competitor matches)
    errors = renderer.validate()
    assert len(errors) == 0, f"Evidence should pass provenance validation: {errors}"

    # Render the output
    product_spec = "กล้อง: 5MP"
    rendered = renderer._render_brief(product_spec)

    # The exaggerated claim "video call" appears in rendered output
    # This PROVES the residual gap: G2 provenance does not verify semantic entailment
    assert "video call" in rendered, (
        "If 'video call' does NOT appear, the G2 path may have been enhanced. "
        "If it DOES appear, this proves the residual semantic-grounding gap: "
        "provenance validation passes but the claim exceeds what the source supports."
    )


def test_s2_semantic_grounding_residual_gap_documented():
    """Document the S2 residual gap: provenance ≠ semantic entailment.

    This test explicitly asserts that the G2 evidence path does NOT check
    whether a claim is semantically entailed by its source. It only checks
    provenance (URL is verified, competitor identity matches).

    This is NOT a bug to fix — it's a documented residual risk that
    requires a model-level solution (review/refine), not a code fix.
    """
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        CompetitorReportRenderer,
        ResearchResponse,
    )

    # Source says "ราคา 4,990 บาท"
    source_annotation = {
        "url": "https://example.com/xiaomi-specs",
        "title": "Xiaomi Smart Kids Watch Specs",
        "content": "ราคา 4,990 บาท",
        "_relevance": {
            "relevant": True,
            "relevance_type": "competitor",
            "matched_competitor": "Xiaomi Smart Kids Watch",
        },
    }

    # Claim EXCEEDS source: adds "ถูกที่สุดในตลาด" (cheapest in market)
    evidence = CompetitorEvidence(
        competitor="Xiaomi Smart Kids Watch",
        field="ราคา",
        claim="ราคา 4,990 บาท ถูกที่สุดในตลาด",
        url="https://example.com/xiaomi-specs",
        geography="thailand",
    )

    research = ResearchResponse(
        target_model="K2",
        competitor_names=["Xiaomi Smart Kids Watch"],
        evidence=[evidence],
    )

    renderer = CompetitorReportRenderer(
        research=research,
        relevant_annotations=[source_annotation],
    )

    # Provenance validation PASSES — URL is verified, competitor matches
    errors = renderer.validate()
    assert len(errors) == 0, (
        f"Provenance should pass (URL verified, competitor matches): {errors}. "
        "This confirms G2 does NOT check semantic entailment."
    )

    # The exaggerated claim "ถูกที่สุดในตลาด" appears in rendered output
    rendered = renderer._render_brief("ราคา: ไม่มีข้อมูล")
    assert "ถูกที่สุดในตลาด" in rendered, (
        "The exaggerated claim should appear in rendered output, proving that "
        "G2 provenance validation does not prevent semantic overclaims."
    )


# ---------------------------------------------------------------------------
# Slice 9: No static forbidden-claim list exists
# ---------------------------------------------------------------------------

def test_no_static_forbidden_claims_in_code():
    """No static K2/smartwatch forbidden-claim list exists in production code."""
    import ast

    src_dir = Path(__file__).resolve().parent.parent / "src"
    forbidden_patterns = [
        "forbidden_claims",
        "technical_attribute_labels",
    ]
    violations = []

    for py_file in src_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in forbidden_patterns:
                        violations.append(f"{py_file}: {target.id}")
            elif isinstance(node, ast.FunctionDef) and node.name in forbidden_patterns:
                violations.append(f"{py_file}: function {node.name}")
            elif isinstance(node, ast.ClassDef) and node.name in forbidden_patterns:
                violations.append(f"{py_file}: class {node.name}")

    assert not violations, (
        f"Found static forbidden-claim vocabulary in production code: {violations}. "
        "M6 remediation must not use static vocabularies."
    )


def test_no_smartwatch_specific_keywords_in_validators():
    """No smartwatch-specific keywords (CPU/OS/RAM/ROM field lists) in
    the M6 remediation functions of output_validators.

    Pre-existing docstring examples may contain generic Thai words like
    'สมาร์ทวอทช์' — this test only checks the M6 remediation additions
    (functions added after the original file body).
    """
    import ast

    src_file = Path(__file__).resolve().parent.parent / "src" / "output_validators.py"
    tree = ast.parse(src_file.read_text(encoding="utf-8"))

    # M6 remediation functions — these must not contain smartwatch-specific keywords
    m6_functions = {
        "normalize_one_page_brief",
        "validate_one_page_compactness",
        "apply_brand_replacements",
        "validate_brand_hard",
        "_has_one_page_intent",
    }

    smartwatch_keywords = [
        "smartwatch", "smart watch",
        "AMOLED", "GPS", "หน้าปัด",
        "K2", "K3", "Lagenio",
    ]

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in m6_functions:
            func_source = ast.get_source_segment(src_file.read_text(encoding="utf-8"), node)
            found = [kw for kw in smartwatch_keywords if kw in func_source]
            if found:
                violations.append(f"{node.name}: {found}")

    assert not violations, (
        f"Found smartwatch-specific keywords in M6 remediation functions: {violations}. "
        "M6 validators must be generic."
    )


# ---------------------------------------------------------------------------
# Slice 10: G2 competitor evidence path still works
# ---------------------------------------------------------------------------

def test_g2_evidence_path_basic_validation():
    """Current G2 competitor evidence/provenance path still passes basic validation."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        CompetitorReportRenderer,
        ResearchResponse,
    )

    source_annotation = {
        "url": "https://example.com/specs",
        "title": "Specs",
        "content": "ราคา 1,000 บาท",
        "_relevance": {
            "relevant": True,
            "relevance_type": "competitor",
            "matched_competitor": "Competitor A",
        },
    }

    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 1,000 บาท",
        url="https://example.com/specs",
        geography="thailand",
    )

    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )

    renderer = CompetitorReportRenderer(
        research=research,
        relevant_annotations=[source_annotation],
    )

    errors = renderer.validate()
    assert len(errors) == 0, f"Valid evidence should pass: {errors}"


def test_g2_evidence_path_rejects_unverified_source():
    """G2 still rejects unverified (candidate) sources."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        CompetitorReportRenderer,
        ResearchResponse,
    )

    source_annotation = {
        "url": "https://example.com/specs",
        "title": "Specs",
        "content": "ราคา 1,000 บาท",
        "_relevance": {
            "relevant": None,  # candidate, not verified
            "relevance_type": "competitor",
            "matched_competitor": "Competitor A",
        },
    }

    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 1,000 บาท",
        url="https://example.com/specs",
        geography="thailand",
    )

    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )

    renderer = CompetitorReportRenderer(
        research=research,
        relevant_annotations=[source_annotation],
    )

    errors = renderer.validate()
    assert len(errors) > 0, "Candidate (unverified) source should be rejected"


# ===========================================================================
# Blocker 1 — S2 semantic evidence review (before renderer)
#
# Architecture:
#   ResearchResponse / evidence records
#   → existing deterministic G2 provenance validation
#   → semantic evidence review (NEW — model-level, 1 pass, no retry)
#   → CompetitorReportRenderer
#
# The semantic reviewer receives, for each evidence claim:
#   - the claim text
#   - the source material/content already available in relevant_annotations
#   - the competitor identity
# It verifies the claim is semantically entailed by the source and rewrites
# or removes unsupported inferences.
# ===========================================================================


class FakeSemanticLLM:
    """Fake LLM for semantic review tests — returns scripted JSON."""

    def __init__(self, review_output: str):
        self.review_output = review_output
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.review_output

    def close(self):
        pass


def _make_verified_annotation(url: str, content: str, competitor: str,
                               title: str = "Source") -> dict:
    """Create a verified (relevant=True) competitor annotation."""
    return {
        "url": url,
        "title": title,
        "content": content,
        "_relevance": {
            "relevant": True,
            "relevance_type": "competitor",
            "matched_competitor": competitor,
        },
    }


# ---------------------------------------------------------------------------
# Test 1: G2 provenance alone still demonstrates the historical semantic gap
# (already covered by test_s2_semantic_grounding_residual_proof above —
#  this is a duplicate explicit assertion for the required test list)
# ---------------------------------------------------------------------------

def test_s2_provenance_alone_still_has_semantic_gap():
    """Required test 1: G2 provenance alone still demonstrates the gap."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        CompetitorReportRenderer,
        ResearchResponse,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "กล้องหน้า 5MP สำหรับถ่ายภาพ",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="กล้อง",
        claim="กล้องหน้า 5MP ใช้ video call ได้",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    renderer = CompetitorReportRenderer(
        research=research,
        relevant_annotations=[source],
    )
    # Provenance passes
    errors = renderer.validate()
    assert len(errors) == 0
    # But the unsupported claim reaches rendered output
    rendered = renderer._render_brief("กล้อง: 5MP")
    assert "video call" in rendered


# ---------------------------------------------------------------------------
# Test 2: Semantic review rewrites unsupported capability inference
# ---------------------------------------------------------------------------

def test_semantic_review_rewrites_unsupported_capability():
    """Required test 2: semantic review rewrites unsupported capability
    inference (e.g. 'video call' when source only says 'ถ่ายภาพ')."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "กล้องหน้า 5MP สำหรับถ่ายภาพ",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="กล้อง",
        claim="กล้องหน้า 5MP ใช้ video call ได้",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    # Reviewer LLM rewrites the claim to only what source supports
    review_json = json.dumps([{
        "index": 0,
        "action": "rewrite",
        "claim": "กล้องหน้า 5MP สำหรับถ่ายภาพ",
    }], ensure_ascii=False)
    fake_llm = FakeSemanticLLM(review_json)

    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewed = reviewer.review(research, [source])

    assert reviewed.evidence[0].claim == "กล้องหน้า 5MP สำหรับถ่ายภาพ"
    assert "video call" not in reviewed.evidence[0].claim
    # Exactly 1 LLM call
    assert len(fake_llm.calls) == 1


# ---------------------------------------------------------------------------
# Test 3: Semantic review rewrites unsupported ranking/superlative inference
# ---------------------------------------------------------------------------

def test_semantic_review_rewrites_unsupported_superlative():
    """Required test 3: semantic review rewrites unsupported
    ranking/superlative (e.g. 'ถูกที่สุดในตลาด' when source says only price)."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "ราคา 4,990 บาท",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 4,990 บาท ถูกที่สุดในตลาด",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    review_json = json.dumps([{
        "index": 0,
        "action": "rewrite",
        "claim": "ราคา 4,990 บาท",
    }], ensure_ascii=False)
    fake_llm = FakeSemanticLLM(review_json)

    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewed = reviewer.review(research, [source])

    assert "ถูกที่สุด" not in reviewed.evidence[0].claim
    assert "4,990" in reviewed.evidence[0].claim
    assert len(fake_llm.calls) == 1


# ---------------------------------------------------------------------------
# Test 4: Grounded evidence remains unchanged
# ---------------------------------------------------------------------------

def test_semantic_review_preserves_grounded_evidence():
    """Required test 4: grounded evidence (claim fully supported by source)
    remains unchanged after semantic review."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "ราคา 1,000 บาท หน้าจอ 1.43 นิ้ว",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 1,000 บาท",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    # Reviewer says "keep" — no change needed
    review_json = json.dumps([{
        "index": 0,
        "action": "keep",
    }], ensure_ascii=False)
    fake_llm = FakeSemanticLLM(review_json)

    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewed = reviewer.review(research, [source])

    assert reviewed.evidence[0].claim == "ราคา 1,000 บาท"
    assert len(fake_llm.calls) == 1


# ---------------------------------------------------------------------------
# Test 5: Strategic hypotheses remain allowed but clearly separated
# ---------------------------------------------------------------------------

def test_semantic_review_preserves_strategic_hypotheses():
    """Required test 5: strategic hypotheses remain allowed and are NOT
    touched by the semantic evidence reviewer (they are unverified by
    design and labeled as such in the renderer)."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
        StrategicHypothesis,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "ราคา 1,000 บาท",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 1,000 บาท",
        url="https://example.com/specs",
        geography="thailand",
    )
    hypothesis = StrategicHypothesis(
        text="อาจเน้นความคุ้มค่าเป็นจุดขาย",
        rationale="ราคาคู่แข่งสูงกว่า แต่ยังไม่ได้เปรียบเทียบทุกรุ่น",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
        strategic_hypotheses=[hypothesis],
    )
    review_json = json.dumps([{
        "index": 0,
        "action": "keep",
    }], ensure_ascii=False)
    fake_llm = FakeSemanticLLM(review_json)

    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewed = reviewer.review(research, [source])

    # Hypothesis unchanged
    assert len(reviewed.strategic_hypotheses) == 1
    assert reviewed.strategic_hypotheses[0].text == hypothesis.text
    assert reviewed.strategic_hypotheses[0].rationale == hypothesis.rationale


# ---------------------------------------------------------------------------
# Test 6: Semantic verification is bounded to one pass, no retry/web loop
# ---------------------------------------------------------------------------

def test_semantic_review_bounded_to_one_pass():
    """Required test 6: semantic verification is bounded to exactly one
    review pass and cannot create a retry or web-call loop."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "ราคา 1,000 บาท",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 1,000 บาท ถูกที่สุด",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    review_json = json.dumps([{
        "index": 0,
        "action": "rewrite",
        "claim": "ราคา 1,000 บาท",
    }], ensure_ascii=False)
    fake_llm = FakeSemanticLLM(review_json)

    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewed = reviewer.review(research, [source])

    # Exactly 1 LLM call — no retry, no web fetch
    assert len(fake_llm.calls) == 1
    # Verify no web_search or web_fetch tools were passed
    tools = fake_llm.calls[0]["kwargs"].get("tools")
    assert tools is None or len(tools) == 0, (
        f"Semantic review must not use web tools: {tools}"
    )


def test_semantic_review_remove_action_drops_evidence():
    """Semantic review can remove an evidence record entirely when the
    claim has no support in the source at all."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "หน้าจอ 1.43 นิ้ว",
        "Competitor A",
    )
    # Claim about battery — source doesn't mention battery at all
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="แบตเตอรี่",
        claim="แบตเตอรี่ 500mAh ใช้งานได้ 14 วัน",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    review_json = json.dumps([{
        "index": 0,
        "action": "remove",
    }], ensure_ascii=False)
    fake_llm = FakeSemanticLLM(review_json)

    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewed = reviewer.review(research, [source])

    assert len(reviewed.evidence) == 0


def test_semantic_review_no_evidence_no_llm_call():
    """If there are no evidence records, no LLM call is made."""
    from src.agents.competitor_evidence import ResearchResponse, SemanticEvidenceReviewer
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[],
    )
    fake_llm = FakeSemanticLLM("should not be called")
    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewed = reviewer.review(research, [])
    assert len(reviewed.evidence) == 0
    assert len(fake_llm.calls) == 0


def test_semantic_review_llm_failure_returns_none():
    """If the semantic review LLM call fails or returns invalid JSON,
    returns None — fail-closed, signaling unchecked claims must not
    reach the renderer."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "ราคา 1,000 บาท",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 1,000 บาท ถูกที่สุด",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    # LLM returns garbage
    fake_llm = FakeSemanticLLM("not valid json at all")
    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewed = reviewer.review(research, [source])

    # Fail-closed: returns None, NOT original research
    assert reviewed is None
    assert len(fake_llm.calls) == 1  # call was made, but failed closed


def test_semantic_review_exception_returns_none():
    """If the semantic review LLM call raises an exception,
    returns None — fail-closed."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
    )

    class ExceptionLLM:
        def __init__(self):
            self.calls = []

        def chat(self, messages, **kwargs):
            self.calls.append({"kwargs": kwargs})
            raise RuntimeError("LLM unavailable")

        def close(self):
            pass

    source = _make_verified_annotation(
        "https://example.com/specs",
        "ราคา 1,000 บาท",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 1,000 บาท ถูกที่สุด",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    fake_llm = ExceptionLLM()
    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewed = reviewer.review(research, [source])

    assert reviewed is None
    assert len(fake_llm.calls) == 1


def test_semantic_review_failure_no_additional_llm_call():
    """Semantic review failure causes no additional LLM call (no retry)."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "ราคา 1,000 บาท",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 1,000 บาท ถูกที่สุด",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    fake_llm = FakeSemanticLLM("garbage")
    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewed = reviewer.review(research, [source])

    assert reviewed is None
    # Exactly 1 call — no retry
    assert len(fake_llm.calls) == 1


def test_semantic_review_failure_no_web_tools():
    """Semantic review failure causes no web/tool call."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "ราคา 1,000 บาท",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 1,000 บาท ถูกที่สุด",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    fake_llm = FakeSemanticLLM("garbage")
    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewer.review(research, [source])

    # Verify no web tools were passed
    tools = fake_llm.calls[0]["kwargs"].get("tools")
    assert tools is None or len(tools) == 0


def test_semantic_review_failure_unchecked_claim_not_in_limited_output():
    """When semantic review fails, the unchecked claim does NOT appear in
    the limited analysis output rendered by the agent."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        CompetitorReportRenderer,
        ResearchResponse,
        StrategicHypothesis,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "ราคา 1,000 บาท",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="ราคา",
        claim="ราคา 1,000 บาท ถูกที่สุดในตลาด",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
        strategic_hypotheses=[StrategicHypothesis(text="test", rationale="r")],
    )
    # Simulate fail-closed: renderer with empty evidence (limited analysis)
    limited_research = ResearchResponse(
        target_model=research.target_model,
        competitor_names=research.competitor_names,
        evidence=[],
        strategic_hypotheses=research.strategic_hypotheses,
        uncertainty=["semantic evidence review ล้มเหลว"],
    )
    renderer = CompetitorReportRenderer(limited_research, [source])
    output = renderer.render("สินค้าของเรา")

    # The unchecked claim must NOT appear
    assert "ถูกที่สุดในตลาด" not in output
    # But competitor names and hypotheses are preserved (labeled unverified)
    assert "Competitor A" in output
    assert "test" in output


def test_semantic_review_source_content_supplied_to_reviewer():
    """The semantic reviewer must receive source content for each evidence
    claim so it can verify entailment."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence,
        ResearchResponse,
        SemanticEvidenceReviewer,
    )
    source = _make_verified_annotation(
        "https://example.com/specs",
        "กล้องหน้า 5MP สำหรับถ่ายภาพ",
        "Competitor A",
    )
    evidence = CompetitorEvidence(
        competitor="Competitor A",
        field="กล้อง",
        claim="กล้องหน้า 5MP ใช้ video call ได้",
        url="https://example.com/specs",
        geography="thailand",
    )
    research = ResearchResponse(
        target_model="My Product",
        competitor_names=["Competitor A"],
        evidence=[evidence],
    )
    review_json = json.dumps([{"index": 0, "action": "keep"}], ensure_ascii=False)
    fake_llm = FakeSemanticLLM(review_json)

    reviewer = SemanticEvidenceReviewer(llm=fake_llm, config={"model": "x"})
    reviewer.review(research, [source])

    # Verify the LLM received source content in the prompt
    user_msg = fake_llm.calls[0]["messages"][-1]["content"]
    assert "กล้องหน้า 5MP สำหรับถ่ายภาพ" in user_msg, (
        "Source content must be supplied to the semantic reviewer"
    )
    assert "video call" in user_msg, (
        "Claim text must be supplied to the semantic reviewer"
    )


# ---------------------------------------------------------------------------
# Test 7-8: Brand hard-rule — repaired → passes; still invalid → failure
# ---------------------------------------------------------------------------

def test_brand_hard_repaired_successfully_passes():
    """Required test 7: brand hard violation repaired successfully → passes."""
    DummyAgent = _make_dummy_agent_class()

    # First output has restricted word, repair removes it
    llm = FakeLLM(["ลดแหลก มาก", "สินค้าคุณภาพดี"])
    config = {
        "system_prompt": "test",
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "max_retry_limit": 3,
        "model": "x",
        "temperature": 0.7,
        "max_tokens": 100,
    }
    agent = DummyAgent(config, llm)
    call_count = [0]
    def _validate(output):
        call_count[0] += 1
        if "ลดแหลก" in output:
            return False, "brand-hard: พบคำต้องห้าม 'ลดแหลก'"
        return True, ""
    agent._test_validate = _validate

    result = agent.run("prompt")
    assert "ลดแหลก" not in result
    assert llm.calls == 2  # 1 generate + 1 repair


def test_brand_hard_still_present_after_repair_raises():
    """Required test 8: brand hard violation still present after one repair
    → controlled failure (ValueError), never soft-accept."""
    DummyAgent = _make_dummy_agent_class()

    # Both outputs have restricted word — repair doesn't fix it
    llm = FakeLLM(["ลดแหลก มาก", "ลดแหลก น้อย"])
    config = {
        "system_prompt": "test",
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "max_retry_limit": 3,
        "model": "x",
        "temperature": 0.7,
        "max_tokens": 100,
    }
    agent = DummyAgent(config, llm)
    agent._test_validate = lambda output: (
        (False, "brand-hard: พบคำต้องห้าม 'ลดแหลก'")
        if "ลดแหลก" in output else (True, "")
    )

    with pytest.raises(ValueError, match="brand-hard"):
        agent.run("prompt")
    # 1 generate + 1 repair = 2 calls (not 3+)
    assert llm.calls == 2


# ---------------------------------------------------------------------------
# Test 9: One-page behavior remains as currently approved
# ---------------------------------------------------------------------------

def test_one_page_behavior_unchanged_after_blocker_fixes():
    """Required test 9: one-page behavior remains as currently approved
    (55 passes, 89 triggers one correction, then soft-accept)."""
    from src.output_validators import validate_one_page_compactness
    # 55 lines passes
    ok, _ = validate_one_page_compactness(_make_lines(55), "one-page")
    assert ok
    # 89 lines triggers correction
    ok, error = validate_one_page_compactness(_make_lines(89), "one-page")
    assert not ok
    assert error.startswith("one-page:")
    # Non-one-page has no cap
    ok, _ = validate_one_page_compactness(_make_lines(200), "สั้นกระชับ")
    assert ok
