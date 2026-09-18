"""Agent reasoning-budget propagation + finish_reason=length truncation handling.

Live defect (2026-09-17): ``product_spec.review`` burned 3928/4096 tokens on
reasoning → ``finish_reason=length`` — and the truncated review output was
silently adopted. The ingestion path already had ``reasoning_max_tokens_summary``;
agent calls had no equivalent bound.

Contract pinned here:
- every Agent LLM call (generate / review / repair / final_grounding) carries
  a bounded ``reasoning`` payload derived from config ``reasoning_max_tokens``;
- the bound is clamped so reasoning can never consume more than half the
  completion budget (output reserve is mechanical, not advisory);
- a truncated review/repair/grounding result can never count as success.
"""
import json
from unittest.mock import MagicMock

import pytest

from src.agents.base_agent import BaseAgent


class DummyAgent(BaseAgent):
    agent_name = "product_spec"

    def build_prompt(self, *args, **kwargs):
        return "prompt"


class RecordingLLM:
    """Records kwargs of every chat call; per-call truncation flags."""

    def __init__(self, outputs, truncated=None):
        self.outputs = outputs
        self.truncated = truncated or set()  # call indexes that return finish_reason=length
        self.calls = 0
        self.kwargs_log = []
        self.last_truncated = False

    def chat(self, messages, **kwargs):
        idx = self.calls
        self.calls += 1
        self.kwargs_log.append(kwargs)
        self.last_truncated = idx in self.truncated
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            return '{"grounded": true, "unsupported_claims": []}'
        out = self.outputs[idx % len(self.outputs)]
        if kwargs.get("return_annotations"):
            return out, []
        return out

    def close(self):
        pass

    def calls_for(self, source_suffix):
        return [kw for kw in self.kwargs_log if kw.get("source", "").endswith(source_suffix)]


def _config(**over):
    cfg = {
        "system_prompt": "คุณคือ agent",
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "max_retry_limit": 3,
        "strict_output_sections": True,
        "required_output_sections": ["ชื่อสินค้า"],
        "model": "x",
        "temperature": 0.7,
        "max_tokens": 4096,
        "reasoning_max_tokens": 700,
    }
    cfg.update(over)
    return cfg


def _review_config(**over):
    cfg = _config(**over)
    cfg["max_review_iterations"] = 1
    cfg["review_temperature"] = 0.2
    cfg["review_prompt"] = "คุณคือ reviewer"
    return cfg


# ---------------------------------------------------------------------------
# Reasoning propagation — every Agent LLM call path
# ---------------------------------------------------------------------------

def test_generate_passes_reasoning_budget():
    """generate call carries reasoning={'max_tokens': configured} on the wire."""
    llm = RecordingLLM(["ชื่อสินค้า: K5"])
    agent = DummyAgent(_config(), llm)
    agent.run("prompt")

    gen = llm.calls_for(".generate")
    assert gen, "generate call not recorded"
    assert gen[0]["reasoning"] == {"max_tokens": 700}


def test_generate_reasoning_clamped_to_half_of_max_tokens():
    """reasoning can never consume more than half the completion budget —
    output reserve for structured/final responses is mechanical."""
    llm = RecordingLLM(["ชื่อสินค้า: K5"])
    cfg = _config(reasoning_max_tokens=3000, max_tokens=4096)
    agent = DummyAgent(cfg, llm)
    agent.run("prompt")

    gen = llm.calls_for(".generate")
    assert gen[0]["reasoning"] == {"max_tokens": 2048}  # 4096 // 2


def test_review_passes_reasoning_budget():
    """review call carries the same bounded reasoning contract."""
    llm = RecordingLLM(["ชื่อสินค้า: K5 draft", "ชื่อสินค้า: K5 refined"])
    agent = DummyAgent(_review_config(), llm)
    agent.run("prompt")

    rev = llm.calls_for(".review")
    assert rev, "review call not recorded"
    assert rev[0]["reasoning"] == {"max_tokens": 700}


def test_repair_passes_reasoning_budget():
    """repair call carries the same bounded reasoning contract."""
    llm = RecordingLLM(["ไม่มี section เลย", "ชื่อสินค้า: K5"])
    agent = DummyAgent(_config(), llm)
    agent.run("prompt")

    rep = llm.calls_for(".repair")
    assert rep, "repair call not recorded"
    assert rep[0]["reasoning"] == {"max_tokens": 700}


def test_final_grounding_passes_reasoning_budget():
    """final_grounding_check call carries reasoning bounded against its own
    2048 completion budget (→ ≤1024)."""
    llm = RecordingLLM(["ชื่อสินค้า: K5"])
    agent = DummyAgent(_config(), llm)
    result = agent.verify_final_grounding("ชื่อสินค้า: K5", {"product_source": "x"})

    chk = llm.calls_for("final_grounding_check")
    assert chk, "grounding call not recorded"
    assert chk[0]["reasoning"] == {"max_tokens": 700}
    assert result["grounded"] is True


def test_grounding_reasoning_clamped_against_its_own_budget():
    """grounding uses max_tokens=2048 → reasoning bound must not exceed 1024."""
    llm = RecordingLLM(["ชื่อสินค้า: K5"])
    cfg = _config(reasoning_max_tokens=3000)
    agent = DummyAgent(cfg, llm)
    agent.verify_final_grounding("ชื่อสินค้า: K5", {"product_source": "x"})

    chk = llm.calls_for("final_grounding_check")
    assert chk[0]["reasoning"] == {"max_tokens": 1024}  # 2048 // 2


def test_unconfigured_resolves_canonical_default():
    """A call site whose config omits the key resolves agents.yaml
    ``defaults.reasoning_max_tokens`` — the canonical contract means no
    reasoning-capable call can be silently unbounded."""
    llm = RecordingLLM(["ชื่อสินค้า: K5"])
    cfg = _config()
    cfg.pop("reasoning_max_tokens")
    agent = DummyAgent(cfg, llm)
    agent.run("prompt")

    gen = llm.calls_for(".generate")
    # repo agents.yaml → defaults.reasoning_max_tokens = 1024
    assert gen[0].get("reasoning") == {"max_tokens": 1024}


def test_explicit_zero_disables_reasoning():
    """``reasoning_max_tokens: 0`` is the mechanical 'intentionally
    non-reasoning' switch — provable, not accidental."""
    llm = RecordingLLM(["ชื่อสินค้า: K5"])
    agent = DummyAgent(_config(reasoning_max_tokens=0), llm)
    agent.run("prompt")

    gen = llm.calls_for(".generate")
    assert gen[0].get("reasoning") in (None, {})


# ---------------------------------------------------------------------------
# Non-BaseAgent call paths — same canonical contract everywhere
# ---------------------------------------------------------------------------

def test_ground_and_store_real_path_is_bounded():
    """The real Orchestrator._ground_and_store builds an ad-hoc
    ``{"model": "grounding-check"}`` agent — the canonical-defaults fallback
    must bound it anyway (this exact gap was measured unbounded)."""
    from src.orchestrator import Orchestrator
    llm = RecordingLLM(["ชื่อสินค้า: K5"])
    orch = Orchestrator()
    orch._ground_and_store("product_spec", "ชื่อสินค้า: K5\nสเปคครบถ้วน", llm=llm)

    chk = llm.calls_for("final_grounding_check")
    assert chk, "grounding call did not fire"
    assert chk[0]["reasoning"] == {"max_tokens": 1024}


def _research_with_evidence():
    from src.agents.competitor_evidence import ResearchResponse
    return ResearchResponse.from_dict({
        "target_model": "K5",
        "competitor_names": ["Rival A"],
        "evidence": [{"competitor": "Rival A", "field": "price",
                      "claim": "Rival A ขาย 1,500 บาท",
                      "url": "https://example.com/a", "geography": "thailand"}],
        "evidence_based_recommendations": [],
        "strategic_hypotheses": [],
        "uncertainty": [],
    })


def test_semantic_review_constructor_path_is_bounded():
    """SemanticEvidenceReviewer's inline {model, max_tokens} config — the
    exact shape competitor_analysis constructs — resolves the canonical
    default via bounded_reasoning."""
    from src.agents.competitor_evidence import SemanticEvidenceReviewer
    llm = RecordingLLM(['[{"index": 0, "action": "keep"}]'])
    reviewer = SemanticEvidenceReviewer(
        llm=llm, config={"model": "x", "max_tokens": 8192})
    reviewer.review(_research_with_evidence(), [{"url": "https://example.com/a"}])

    calls = llm.calls_for("semantic_review")
    assert calls and calls[0]["reasoning"] == {"max_tokens": 1024}


def test_brand_interpretation_constructor_path_is_bounded():
    """BrandInterpretationPass — same inline config shape, same contract."""
    from src.agents.competitor_evidence import BrandInterpretationPass
    llm = RecordingLLM(['[{"evidence_index": 0, "implication": "x"}]'])
    interp = BrandInterpretationPass(
        llm=llm, config={"model": "x", "max_tokens": 8192})
    interp.interpret(_research_with_evidence(), brand_reference="ref")

    calls = llm.calls_for("brand_interpretation")
    assert calls and calls[0]["reasoning"] == {"max_tokens": 1024}


def test_voice_learner_every_attempt_is_bounded():
    """_run_llm_json retries JSON parse up to 3 times — every application-
    level attempt must carry the bound (these are parse attempts, not
    provider transport retries)."""
    from src.voice_learner import _run_llm_json
    llm = RecordingLLM(["not json", "still not json", "nope"])
    try:
        _run_llm_json(llm, "sys", "usr", "s", {}, "voice_learner.test",
                      strict=True)
    except Exception:
        pass  # strict → raises after 3 attempts; we only assert the wire

    assert llm.calls == 3
    assert all(k["reasoning"] == {"max_tokens": 1024}
               for k in llm.kwargs_log)


def test_script_reviewer_is_bounded():
    """script_reviewer.review_script — measured unbounded in the preflight
    ledger; now resolves the canonical default."""
    from src.script_reviewer import review_script
    llm = RecordingLLM([json.dumps({
        "score": 80,
        "component_scores": {"hook": 20, "pacing": 20, "clarity": 20,
                             "engagement": 20},
        "issues": [], "suggested_hooks": [], "revised_script": "",
    })])
    review_script("script text", "TikTok", llm)

    calls = llm.calls_for("review_script")
    assert calls and calls[0]["reasoning"] == {"max_tokens": 1024}


# ---------------------------------------------------------------------------
# Truncation handling — finish_reason=length must never silently pass
# ---------------------------------------------------------------------------

def test_truncated_review_returns_draft_with_warning():
    """Truncated review output is NOT adopted — draft survives with a warning.

    Live defect: review burned 3928/4096 reasoning tokens → length → the
    truncated body silently replaced the good generated output.
    """
    draft = "ชื่อสินค้า: CACGO K66\nคำอธิบาย: ครบ"
    llm = RecordingLLM([draft, "ชื่อสินค้า: CACGO K66\nคำอธิบาย: ตัดกลางประโยค"], truncated={1})
    agent = DummyAgent(_review_config(), llm)
    result = agent.run("prompt")

    assert llm.calls == 2  # generate + review only
    assert "ตัดกลางประโยค" not in result  # truncated body not adopted
    assert "CACGO K66" in result  # draft kept
    assert "ตรวจทาน" in result or "ล้มเหลว" in result  # visible warning


def test_truncated_repair_raises_controlled_error():
    """Truncated repair output is NOT adopted — the run fails with a
    controlled truncation error naming the original validation error."""
    llm = RecordingLLM(
        ["ไม่มี section เลย", "ชื่อสินค้า: ตัดครึ่ง"],
        truncated={1},
    )
    agent = DummyAgent(_config(), llm)
    with pytest.raises(ValueError) as exc:
        agent.run("prompt")
    msg = str(exc.value)
    assert "ไม่ครบ" in msg or "truncated" in msg or "length" in msg
    assert "ชื่อสินค้า" in msg  # original validation error preserved


def test_truncated_generate_enters_bounded_repair():
    """Truncated generate output is never shipped as a final artifact even
    when it would pass validators — it enters the bounded repair flow."""
    truncated_draft = "ชื่อสินค้า: K5\nราคา: ฿2,990"  # structurally valid-looking
    repaired = "ชื่อสินค้า: K5\nราคา: ฿2,990\nสเปคครบถ้วน"
    llm = RecordingLLM([truncated_draft, repaired], truncated={0})
    agent = DummyAgent(_config(), llm)
    result = agent.run("prompt")

    assert result == repaired  # repaired (complete) output shipped, not the truncated draft
    assert llm.calls_for(".repair"), "truncated generate must enter repair flow"


def test_truncated_generate_with_failed_repair_raises():
    """Truncated generate + repairs that never validate → controlled failure."""
    llm = RecordingLLM(["ไม่มี section", "ยังไม่มี", "ยังไม่มี", "ยังไม่มี"], truncated={0})
    agent = DummyAgent(_config(), llm)
    with pytest.raises(ValueError):
        agent.run("prompt")


def test_truncated_grounding_fails_closed():
    """Truncated grounding response → grounded=False, error='truncated'."""
    m = MagicMock()
    m.chat.return_value = json.dumps({"grounded": True, "unsupported_claims": []})
    m.last_truncated = True
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.config = {"reasoning_max_tokens": 512}
    agent.instructions = {}
    agent.llm = m
    result = agent.verify_final_grounding("some text", {"product_source": "x"})
    assert result["grounded"] is False
    assert result.get("error") == "truncated"


# ---------------------------------------------------------------------------
# Remaining call paths — web-search generate, fetch, competitor_evidence
# ---------------------------------------------------------------------------

def test_web_search_generate_passes_reasoning_budget():
    """The web-search (tools) generate branch carries the same bound."""
    llm = RecordingLLM(["ชื่อสินค้า: K5"])
    cfg = _config(web_search=True)
    agent = DummyAgent(cfg, llm)
    agent.run("prompt")

    gen = llm.calls_for(".generate")
    assert gen, "web-search generate call not recorded"
    assert gen[0]["reasoning"] == {"max_tokens": 700}
    assert gen[0].get("tools"), "expected web-search tools on this path"


def test_fetch_verification_passes_reasoning_budget():
    """openrouter:web_fetch verification calls are bounded against their own
    1500-token cap (→ ≤750)."""
    llm = RecordingLLM(["หน้าสินค้า"])
    cfg = _config(reasoning_max_tokens=900)
    agent = DummyAgent(cfg, llm)
    agent._verify_urls_with_fetch([{"url": "https://x.example/product/1"}])

    fetches = llm.calls_for(".fetch")
    assert fetches, "fetch call not recorded"
    assert fetches[0]["reasoning"] == {"max_tokens": 750}  # 1500 // 2


def test_competitor_semantic_review_passes_reasoning_budget():
    """competitor_evidence structured-JSON calls carry the bound too."""
    from src.agents.competitor_evidence import (
        CompetitorEvidence, ResearchResponse, SemanticEvidenceReviewer,
    )
    llm = RecordingLLM(["[]"])
    reviewer = SemanticEvidenceReviewer(
        llm, {"model": "x", "max_tokens": 4096, "reasoning_max_tokens": 700}
    )
    research = ResearchResponse(
        target_model="K5",
        evidence=[CompetitorEvidence(competitor="c", field="price", claim="x", url="https://u")],
    )
    reviewer.review(research, [])

    calls = llm.calls_for("semantic_review")
    assert calls, "semantic_review call not recorded"
    assert calls[0]["reasoning"] == {"max_tokens": 700}


def test_competitor_brand_interpretation_passes_reasoning_budget():
    """brand_interpretation (max_tokens 2048) clamps reasoning to ≤1024."""
    from src.agents.competitor_evidence import (
        BrandInterpretationPass, CompetitorEvidence, ResearchResponse,
    )
    llm = RecordingLLM(["[]"])
    interp = BrandInterpretationPass(
        llm, {"model": "x", "max_tokens": 2048, "reasoning_max_tokens": 3000}
    )
    research = ResearchResponse(
        target_model="K5",
        evidence=[CompetitorEvidence(competitor="c", field="price", claim="x", url="https://u")],
    )
    interp.interpret(research, brand_reference="ref")

    calls = llm.calls_for("brand_interpretation")
    assert calls, "brand_interpretation call not recorded"
    assert calls[0]["reasoning"] == {"max_tokens": 1024}  # 2048 // 2


def test_bounded_reasoning_helper_contract():
    """The shared helper resolution order: per-site key wins → canonical
    defaults fallback → explicit 0 disables → clamped to half the budget."""
    from src.llm_client import bounded_reasoning
    # absent key → canonical agents.yaml defaults (1024 in repo config)
    assert bounded_reasoning({}, 4096) == {"max_tokens": 1024}
    assert bounded_reasoning(None, 4096) == {"max_tokens": 1024}
    # per-site override wins over the default
    assert bounded_reasoning({"reasoning_max_tokens": 700}, 4096) == {"max_tokens": 700}
    # explicit 0 → mechanically disabled (intentionally non-reasoning)
    assert bounded_reasoning({"reasoning_max_tokens": 0}, 4096) is None
    # clamped so output always keeps ≥ half the call budget
    assert bounded_reasoning({"reasoning_max_tokens": 9000}, 4096) == {"max_tokens": 2048}


# ---------------------------------------------------------------------------
# chat_with_tools — the tool-calling path carries the same bounded contract
# ---------------------------------------------------------------------------
# Live defect: orchestrator passed reasoning= into chat_with_tools(), which had
# no such parameter → TypeError with the real LLMClient.  Browser FakeLLM
# missed it because its chat_with_tools(**kwargs) swallows anything.  These
# tests pin the contract at the REAL client and gateway seams — a permissive
# FakeLLM is not acceptable proof here.

from types import SimpleNamespace

from src.llm_client import LLMClient


def _real_client() -> LLMClient:
    """A real LLMClient — never sent to the network; the gate seam is stubbed."""
    return LLMClient(api_key="dummy-test-key")


def _sdk_response(content=None, tool_calls=None, finish_reason="stop"):
    """Minimal stand-in for an OpenAI SDK chat.completions response object."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(id="req_test", choices=[choice], usage=None)


def _gate_stub(captured: list, responses: list):
    def _fake_gate(**kwargs):
        captured.append(kwargs)
        return responses[min(len(captured) - 1, len(responses) - 1)]
    return _fake_gate


def test_chat_with_tools_accepts_reasoning_and_forwards_to_gateway(monkeypatch):
    """chat_with_tools(reasoning=...) must not TypeError on the real client,
    and the bound must reach the gateway call unchanged."""
    captured = []
    monkeypatch.setattr(
        "src.llm_client._gate_chat_completions_create",
        _gate_stub(captured, [_sdk_response(content='{"ok": true}')]),
    )
    out = _real_client().chat_with_tools(
        [{"role": "user", "content": "x"}],
        tools=[], tool_handlers={},
        reasoning={"max_tokens": 512},
    )
    assert out == '{"ok": true}'
    assert captured and captured[0]["reasoning"] == {"max_tokens": 512}


def test_chat_with_tools_emits_no_reasoning_when_absent(monkeypatch):
    """reasoning=None (disabled) → no reasoning field crosses the gate."""
    captured = []
    monkeypatch.setattr(
        "src.llm_client._gate_chat_completions_create",
        _gate_stub(captured, [_sdk_response(content="done")]),
    )
    _real_client().chat_with_tools(
        [{"role": "user", "content": "x"}], tools=[], tool_handlers={})
    assert captured and captured[0].get("reasoning") is None


def test_chat_with_tools_tool_loop_unchanged(monkeypatch):
    """Normal tool-call behavior is untouched: a tool_calls response executes
    the handler, appends the result, and the loop ends on the final text."""
    tool_call = SimpleNamespace(
        id="call_1", type="function",
        function=SimpleNamespace(name="get_product_detail",
                                 arguments='{"product_id": "P1"}'),
    )
    responses = [
        _sdk_response(content=None, tool_calls=[tool_call],
                      finish_reason="tool_calls"),
        _sdk_response(content="final answer"),
    ]
    captured = []
    monkeypatch.setattr(
        "src.llm_client._gate_chat_completions_create",
        _gate_stub(captured, responses),
    )
    ran = []
    out = _real_client().chat_with_tools(
        [{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "get_product_detail"}}],
        tool_handlers={"get_product_detail": lambda product_id: ran.append(product_id) or {"name": "K5"}},
        reasoning={"max_tokens": 256},
    )
    assert out == "final answer"
    assert ran == ["P1"]
    assert len(captured) == 2  # tool turn + final turn
    assert all(c["reasoning"] == {"max_tokens": 256} for c in captured)


def test_gateway_emits_reasoning_via_extra_body(monkeypatch):
    """Wire contract: reasoning must reach the provider body at top level —
    through the SDK that is ``extra_body``, matching what chat() puts in the
    raw payload (``payload["reasoning"]``)."""
    created = {}

    class _Completions:
        def create(self, **kwargs):
            created.update(kwargs)
            return _sdk_response(content="ok")

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=_Completions())

        def close(self):
            pass

    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    monkeypatch.setattr("src.openrouter_gateway.account", lambda **kw: None)
    from src.openrouter_gateway import chat_completions_create
    chat_completions_create(
        model="m", messages=[{"role": "user", "content": "x"}],
        api_key="dummy", reasoning={"max_tokens": 512})
    assert created["extra_body"] == {"reasoning": {"max_tokens": 512}}


def test_gateway_emits_no_extra_body_without_reasoning(monkeypatch):
    """reasoning=None → the provider body carries no reasoning field at all."""
    created = {}

    class _Completions:
        def create(self, **kwargs):
            created.update(kwargs)
            return _sdk_response(content="ok")

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=_Completions())

        def close(self):
            pass

    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    monkeypatch.setattr("src.openrouter_gateway.account", lambda **kw: None)
    from src.openrouter_gateway import chat_completions_create
    chat_completions_create(
        model="m", messages=[{"role": "user", "content": "x"}],
        api_key="dummy")
    assert "extra_body" not in created


def test_select_product_auto_crosses_reasoning_boundary_with_real_client(monkeypatch):
    """The exact production failure: select_product_auto passes reasoning=
    to chat_with_tools → TypeError with a real LLMClient.  Regression must
    prove the boundary is crossed and the bound is on the wire."""
    from src.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.config = {}
    monkeypatch.setattr("src.product_db.get_all_products",
                        lambda: [{"product_id": "P1"}, {"product_id": "P2"}])
    monkeypatch.setattr("src.product_db.is_ready", lambda pid: True)
    captured = []
    monkeypatch.setattr(
        "src.llm_client._gate_chat_completions_create",
        _gate_stub(captured, [_sdk_response(
            content='{"product_ids": ["P1", "P2"], "concept": "compare", '
                    '"reason": "r", "pillar": "p"}')]),
    )
    result = orch.select_product_auto(llm=_real_client(), product_count=2)
    assert result.get("product_ids") == ["P1", "P2"]
    # canonical default: auto_mode has no reasoning_max_tokens → agents.yaml 1024
    assert captured and captured[0]["reasoning"] == {"max_tokens": 1024}


def test_select_assets_for_content_crosses_reasoning_boundary(monkeypatch):
    """Same boundary on the second call site.  Its try/except swallows the
    TypeError and silently disables asset selection — the regression must
    prove the real call succeeds, not merely that it does not raise."""
    from src.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.config = {}
    monkeypatch.setattr("src.asset_library.list_all",
                        lambda: [{"id": "a_1"}])
    monkeypatch.setattr("src.asset_library.tool_definitions", lambda: [])
    monkeypatch.setattr("src.asset_library.tool_handlers", lambda: {})
    monkeypatch.setattr("src.asset_library.get_asset", lambda aid: {
        "id": aid, "file": "f.png", "type": "image", "subject": "s",
        "tags": [], "description": "d"})
    captured = []
    monkeypatch.setattr(
        "src.llm_client._gate_chat_completions_create",
        _gate_stub(captured, [_sdk_response(
            content='{"selected_asset_ids": ["a_1"], "reason": "r", '
                    '"usage_hints": {}}')]),
    )
    result = orch._select_assets_for_content(_real_client())
    assert result and "a_1" in result  # TypeError → swallowed → "" (silent skip)
    assert captured and captured[0]["reasoning"] == {"max_tokens": 1024}
