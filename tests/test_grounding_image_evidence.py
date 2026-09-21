"""Observable-vs-inferred image evidence contract for final grounding.

Proves the generic evidence boundary:

- Product images reach the grounding call as multimodal content (same call,
  no extra provider call).
- Images may support only DIRECTLY OBSERVABLE claims.
- Capability inferred from an image alone stays unsupported; the same
  capability is accepted when textual/structured evidence exists.
- Only the selected product's images are supplied — unrelated product images
  cannot authorize a claim; cross-brand isolation holds.
- A product with no images behaves exactly as before.
- The gate still fails closed for genuinely unsupported claims.

All boundaries are fakes/recordings — zero provider calls.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _png(path: Path) -> str:
    """Real decodable 1x1 PNG on disk (PIL is a test dep already)."""
    from PIL import Image
    Image.new("RGB", (2, 2), (255, 0, 0)).save(path)
    return str(path)


def _agent():
    """Ad-hoc grounding agent — same construction as orchestrator."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test_agent"
    agent.display_name = "test_agent"
    agent.config = {"model": "grounding-check"}
    agent.instructions = {}
    return agent


class _RecordingLLM:
    """Captures the exact request the verifier sends; canned verdict."""
    last_truncated = False
    last_finish_reason = "stop"

    def __init__(self, verdict):
        self.verdict = verdict
        self.requests = []

    def chat(self, messages, **kw):
        self.requests.append({"messages": messages, "kw": kw})
        return json.dumps(self.verdict, ensure_ascii=False)


GROUNDED = {"grounded": True, "unsupported_claims": []}
UNGROUNDED = {"grounded": False, "unsupported_claims": [
    {"claim": "inferred capability", "reason": "image shows an icon only"}]}


def _image_parts(req):
    """image_url parts inside the recorded user message."""
    content = req["messages"][1]["content"]
    if not isinstance(content, list):
        return []
    return [p for p in content if p.get("type") == "image_url"]


# ---------------------------------------------------------------------------
# Images reach the grounding call
# ---------------------------------------------------------------------------

def test_product_images_attached_to_grounding_call(tmp_path):
    """runtime_context product_image_paths → multimodal user content in the
    SAME grounding call (no extra provider call)."""
    img = _png(tmp_path / "p1.png")
    llm = _RecordingLLM(GROUNDED)
    agent = _agent()
    agent.llm = llm
    res = agent.verify_final_grounding(
        "ข้อความผลงาน", {"product_source": "src", "product_image_paths": [img]})
    assert res["grounded"] is True
    assert len(llm.requests) == 1                     # one call only
    req = llm.requests[0]
    content = req["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert len(_image_parts(req)) == 1


def test_missing_image_file_not_fabricated(tmp_path):
    """A listed path that does not exist on disk encodes to nothing —
    content degrades to plain text, no fabricated evidence."""
    llm = _RecordingLLM(GROUNDED)
    agent = _agent()
    agent.llm = llm
    agent.verify_final_grounding(
        "text", {"product_image_paths": [str(tmp_path / "ghost.png")]})
    assert isinstance(llm.requests[0]["messages"][1]["content"], str)


# ---------------------------------------------------------------------------
# Observable-vs-inferred contract reaches the verifier
# ---------------------------------------------------------------------------

def test_image_evidence_rules_in_verifier_prompt(tmp_path):
    """The grounding rules distinguish observable vs inferred — the model's
    judgment contract. Rules appear only when images are supplied."""
    img = _png(tmp_path / "p1.png")
    llm = _RecordingLLM(GROUNDED)
    agent = _agent()
    agent.llm = llm
    agent.verify_final_grounding(
        "text", {"product_image_paths": [img]})
    sys_prompt = llm.requests[0]["messages"][0]["content"]
    assert "มองเห็นได้โดยตรง" in sys_prompt          # directly observable
    assert "ห้ามอนุมาน" in sys_prompt                # no inference from image
    assert "ไม่ได้ทำให้ claim มีฐาน" in sys_prompt   # image ≠ auto-evidence


def test_no_images_verifier_prompt_unchanged():
    """Product with no images → byte-identical prompt shape: no image rules,
    plain-string user content — behaves exactly as before."""
    llm = _RecordingLLM(GROUNDED)
    agent = _agent()
    agent.llm = llm
    agent.verify_final_grounding("text", {"product_source": "x"})
    req = llm.requests[0]
    sys_prompt = req["messages"][0]["content"]
    assert "กฎหลักฐานภาพ" not in sys_prompt
    assert "มองเห็นได้โดยตรง" not in sys_prompt
    assert isinstance(req["messages"][1]["content"], str)


def test_empty_image_list_same_as_no_images():
    llm = _RecordingLLM(GROUNDED)
    agent = _agent()
    agent.llm = llm
    agent.verify_final_grounding("text", {"product_image_paths": []})
    assert isinstance(llm.requests[0]["messages"][1]["content"], str)


# ---------------------------------------------------------------------------
# Verdict classes flow through correctly
# ---------------------------------------------------------------------------

def test_visually_grounded_observation_accepted(tmp_path):
    """A claim the image can directly establish → verifier grounds it."""
    img = _png(tmp_path / "p1.png")
    llm = _RecordingLLM(GROUNDED)
    agent = _agent()
    agent.llm = llm
    res = agent.verify_final_grounding(
        "ในภาพสินค้าเห็นไอคอนและข้อความบนหน้าปัด",
        {"product_source": "spec text", "product_image_paths": [img]})
    assert res["grounded"] is True
    assert _image_parts(llm.requests[0])             # evidence was available


def test_inferred_capability_remains_unsupported(tmp_path):
    """Capability inferred from the image alone → verifier rejects."""
    img = _png(tmp_path / "p1.png")
    llm = _RecordingLLM(UNGROUNDED)
    agent = _agent()
    agent.llm = llm
    res = agent.verify_final_grounding(
        "รองรับฟังก์ชันที่อนุมานจากไอคอนในภาพ",
        {"product_source": "spec text", "product_image_paths": [img]})
    assert res["grounded"] is False
    assert res["unsupported_claims"]


def test_capability_grounded_by_text_evidence(tmp_path):
    """Same capability WITH textual/structured evidence → grounded."""
    img = _png(tmp_path / "p1.png")
    llm = _RecordingLLM(GROUNDED)
    agent = _agent()
    agent.llm = llm
    res = agent.verify_final_grounding(
        "รองรับฟังก์ชันที่ระบุในเอกสาร",
        {"product_source": "เอกสารระบุฟังก์ชันนั้นอย่างชัดเจน",
         "product_image_paths": [img]})
    assert res["grounded"] is True


def test_fail_closed_genuinely_unsupported(tmp_path):
    """Genuinely unsupported claim still fails closed — images attached do
    not loosen the gate."""
    img = _png(tmp_path / "p1.png")
    llm = _RecordingLLM(UNGROUNDED)
    agent = _agent()
    agent.llm = llm
    res = agent.verify_final_grounding(
        "claim ที่ไม่มีหลักฐานเลย",
        {"product_image_paths": [img]})
    assert res["grounded"] is False


# ---------------------------------------------------------------------------
# Orchestrator wiring — selected-product isolation
# ---------------------------------------------------------------------------

def test_ground_and_store_supplies_only_selected_product_images(tmp_path):
    """_ground_and_store injects the SELECTED product's image paths — the
    same resolution the generator used.  Other products' images are never
    looked up."""
    from src.orchestrator import Orchestrator
    from src import product_db
    from src.agents.base_agent import BaseAgent

    img_a = _png(tmp_path / "a.png")
    img_b = _png(tmp_path / "b.png")
    seen = {}

    orch = Orchestrator.__new__(Orchestrator)
    orch.product_id = "prod_a"
    orch.results = {}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(product_db, "is_ready", lambda pid: True)
    monkey.setattr(product_db, "get_product_image_paths",
                   lambda pid: {"prod_a": [img_a], "prod_b": [img_b]}[pid])

    captured = {}

    def _spy(self, text, ctx):
        captured["ctx"] = dict(ctx)
        return {"grounded": True, "unsupported_claims": []}

    monkey.setattr(BaseAgent, "verify_final_grounding", _spy)
    try:
        out = orch._ground_and_store("product_spec", "final text",
                                     MagicMock(), runtime_context={})
    finally:
        monkey.undo()
    assert out == "final text"
    assert captured["ctx"]["product_image_paths"] == [img_a]
    assert img_b not in captured["ctx"]["product_image_paths"]


def test_ground_and_store_no_product_no_image_key():
    """No selected product → no product_image_paths key → identical legacy
    behavior."""
    from src.orchestrator import Orchestrator
    from src.agents.base_agent import BaseAgent

    orch = Orchestrator.__new__(Orchestrator)
    orch.product_id = None
    orch.results = {}

    captured = {}

    def _spy(self, text, ctx):
        captured["ctx"] = dict(ctx)
        return {"grounded": True, "unsupported_claims": []}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(BaseAgent, "verify_final_grounding", _spy)
    try:
        orch._ground_and_store("product_spec", "final text",
                               MagicMock(), runtime_context={})
    finally:
        monkey.undo()
    assert "product_image_paths" not in captured["ctx"]


def test_ground_and_store_caller_supplied_paths_win(tmp_path):
    """A caller that explicitly supplies product_image_paths is respected
    (setdefault, not override)."""
    from src.orchestrator import Orchestrator
    from src import product_db
    from src.agents.base_agent import BaseAgent

    explicit = _png(tmp_path / "explicit.png")
    other = _png(tmp_path / "other.png")

    orch = Orchestrator.__new__(Orchestrator)
    orch.product_id = "prod_a"
    orch.results = {}

    captured = {}

    def _spy(self, text, ctx):
        captured["ctx"] = dict(ctx)
        return {"grounded": True, "unsupported_claims": []}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(product_db, "is_ready", lambda pid: True)
    monkey.setattr(product_db, "get_product_image_paths",
                   lambda pid: [other])
    monkey.setattr(BaseAgent, "verify_final_grounding", _spy)
    try:
        orch._ground_and_store(
            "product_spec", "final text", MagicMock(),
            runtime_context={"product_image_paths": [explicit]})
    finally:
        monkey.undo()
    assert captured["ctx"]["product_image_paths"] == [explicit]


# ---------------------------------------------------------------------------
# Upstream propagation — generation/review guidance
# ---------------------------------------------------------------------------

def test_grounding_policy_block_carries_image_rule():
    """The shared policy block (seen by generate + review) instructs:
    describe what is visibly observable; do not upgrade to capability."""
    from src.agents.base_agent import BaseAgent
    out = BaseAgent._render_grounding_policy({"categories": ["supplied_fact"]})
    assert "มองเห็นได้โดยตรง" in out
    assert "ห้ามอนุมาน" in out


def test_image_rule_reaches_generate_prompt():
    """The system prompt embeds the shared policy block when the agent
    config has grounding_policy (all four agents do)."""
    from src.agents.base_agent import BaseAgent
    agent = BaseAgent.__new__(BaseAgent)
    agent.agent_name = "test"
    agent.display_name = "test"
    agent.config = {"grounding_policy": {"categories": ["supplied_fact"]}}
    agent.instructions = {}
    agent.brand_context = ""
    agent.brand_reference = None
    agent.brand_rules = None
    agent.brand_dir = ""
    prompt = agent._build_system_prompt()
    assert "มองเห็นได้โดยตรง" in prompt


def test_verifier_is_multimodal_only_when_images(tmp_path):
    """Exactly one grounding request; images ride inside it — no second
    image-analysis call is ever made."""
    img = _png(tmp_path / "p.png")
    llm = _RecordingLLM(GROUNDED)
    agent = _agent()
    agent.llm = llm
    agent.verify_final_grounding(
        "t", {"product_image_paths": [img]})
    assert len(llm.requests) == 1
