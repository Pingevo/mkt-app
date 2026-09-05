from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cost_summary import write_flow_meta
from src.run_context import StepRunContext, build_step_run_context
from src.run_resources import RunResourceStore


@pytest.fixture
def store(tmp_path: Path):
    return RunResourceStore(tmp_path, storage_dir=tmp_path / "run_resources")


def test_empty_context_has_no_resources_and_valid_identity(store):
    ctx = build_step_run_context(
        store,
        workflow_id="wf_test",
        step_id="wf_test_step_0",
        agent_key="content_creator",
        quick_brief="สร้างคอนเทนต์",
        product_refs=["product:K9"],
        resource_refs=[],
        upload_session_id="",
    )
    assert ctx.workflow_id == "wf_test"
    assert ctx.step_id == "wf_test_step_0"
    assert ctx.agent_key == "content_creator"
    assert ctx.quick_brief == "สร้างคอนเทนต์"
    assert ctx.input_refs == ("product:K9",)
    assert ctx.resource_text == ""
    assert ctx.resource_image_paths == ()
    assert ctx.resource_trace == ()
    assert ctx.warnings == ()


def test_text_resources_are_built(store):
    text_rec = store.upload("note.txt", "ข้อมูลยอดขาย Q2".encode("utf-8"))
    session_id = text_rec["scope_id"]

    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="campaign_strategy",
        quick_brief="วางแคมเปญ",
        product_refs=["product:K9"],
        resource_refs=[f"resource:{text_rec['resource_id']}"],
        upload_session_id=session_id,
    )
    assert "ข้อมูลยอดขาย Q2" in ctx.resource_text
    assert any(r.name == "note.txt" for r in ctx.resource_trace)
    assert "product:K9" in ctx.input_refs
    assert f"resource:{text_rec['resource_id']}" in ctx.input_refs


def test_invalid_or_cross_scope_ref_is_rejected(store, tmp_path):
    other_store = RunResourceStore(tmp_path, storage_dir=tmp_path / "other")
    rec = other_store.upload("other.txt", b"secret")
    session_id = rec["scope_id"]

    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=[],
        resource_refs=[f"resource:{rec['resource_id']}"],
        upload_session_id=session_id,
    )
    assert ctx.warnings
    assert any("missing" in w for w in ctx.warnings)
    assert ctx.resource_text == ""


def test_context_is_immutable_and_derives_without_mutating(store):
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="original",
        product_refs=[],
        resource_refs=[],
        upload_session_id="",
    )
    derived = ctx.with_quick_brief("changed").with_products(["K2"])
    assert derived.quick_brief == "changed"
    assert "product:K2" in derived.input_refs
    assert ctx.quick_brief == "original"
    assert "product:K2" not in ctx.input_refs


def test_agent_key_can_be_derived(store):
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=[],
        resource_refs=[],
        upload_session_id="",
    )
    derived = ctx.with_agent_key("product_spec")
    assert derived.agent_key == "product_spec"
    assert ctx.agent_key == "content_creator"


def test_phase_view_carries_trace_metadata(store):
    rec = store.upload("note.txt", "ข้อมูล".encode("utf-8"))
    session_id = rec["scope_id"]
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=["product:K9"],
        resource_refs=[f"resource:{rec['resource_id']}"],
        upload_session_id=session_id,
    ).with_phase("selection")
    view = ctx.for_phase("selection")
    assert view.trace_metadata["phase"] == "selection"
    assert view.trace_metadata["workflow_id"] == "wf_1"
    assert view.trace_metadata["agent_key"] == "content_creator"
    assert any(t.phase == "selection" for t in ctx.phase_traces)


def test_phase_view_returns_multimodal_content_with_image(store, tmp_path):
    text_rec = store.upload("note.txt", "brief".encode("utf-8"))
    img_path = tmp_path / "chart.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    session_id = text_rec["scope_id"]

    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=["product:K9"],
        resource_refs=[f"resource:{text_rec['resource_id']}"],
        upload_session_id=session_id,
    )
    view = ctx.for_phase("selection")
    view_with_image = view  # image_paths จริงต้องมาจาก resource store; ทดสอบ as_llm_content ผ่าน fake
    from src.run_context import PhaseContext
    phase_view = PhaseContext(
        instruction_text=view.instruction_text,
        image_paths=(str(img_path),),
        input_refs=view.input_refs,
        trace_metadata=view.trace_metadata,
    )
    content = phase_view.as_llm_content()
    assert isinstance(content, list)
    assert any(item.get("type") == "text" for item in content)
    assert any(item.get("type") == "image_url" for item in content)


def test_write_flow_meta_persists_phase_traces(tmp_path: Path):
    ctx = StepRunContext(
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        input_refs=("product:K9",),
        product_refs=("product:K9",),
        resource_refs=(),
        resource_text="",
        resource_image_paths=(),
        resource_trace=(),
        warnings=(),
        phase_traces=(),
    ).with_phase("selection")
    meta_path = write_flow_meta(
        tmp_path, "flow_1", ["out.json"],
        phase_traces=[t.as_dict() for t in ctx.phase_traces],
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        input_refs=list(ctx.input_refs),
    )
    assert meta_path is not None
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert data["phase_traces"]
    assert data["phase_traces"][0]["phase"] == "selection"
    assert data["phase_traces"][0]["agent_key"] == "content_creator"


def test_with_products_does_not_duplicate_when_reapplied(store):
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=[],
        resource_refs=[],
        upload_session_id="",
    )
    derived = ctx.with_products(["K2"])
    assert derived.product_refs == ("product:K2",)
    assert derived.input_refs.count("product:K2") == 1
    # การเติมสินค้าเดิมซ้ำต้องไม่ทำให้ input_refs ซ้ำ (เกิดได้ใน Auto mode หลายโพสต์)
    rederived = derived.with_products(["K2"])
    assert rederived.product_refs == ("product:K2",)
    assert rederived.input_refs.count("product:K2") == 1


# ---------------------------------------------------------------------------
# Attachment preflight — non-ready referenced resources must NOT be silently
# dropped. The user explicitly attached them; runtime must surface an error.
# ---------------------------------------------------------------------------

def test_ready_attachment_reaches_runtime_context(store):
    """A successfully parsed attachment must reach the runtime context."""
    rec = store.upload("note.txt", "sales data Q2".encode("utf-8"))
    session_id = rec["scope_id"]
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="campaign_strategy",
        quick_brief="",
        product_refs=[],
        resource_refs=[f"resource:{rec['resource_id']}"],
        upload_session_id=session_id,
    )
    assert "sales data Q2" in ctx.resource_text
    assert len(ctx.warnings) == 0


def test_zero_resources_remains_valid(store):
    """Zero attachments must remain valid — no warnings, no errors."""
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=[],
        resource_refs=[],
        upload_session_id="",
    )
    assert ctx.resource_text == ""
    assert len(ctx.warnings) == 0


def test_multiple_resources_remain_distinguishable(store):
    """Multiple attachments must remain distinguishable in the context."""
    rec1 = store.upload("file1.txt", "content one".encode("utf-8"))
    session_id = rec1["scope_id"]
    rec2 = store.upload("file2.txt", "content two".encode("utf-8"), session_id=session_id)
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=[],
        resource_refs=[
            f"resource:{rec1['resource_id']}",
            f"resource:{rec2['resource_id']}",
        ],
        upload_session_id=session_id,
    )
    assert "content one" in ctx.resource_text
    assert "content two" in ctx.resource_text
    assert "file1.txt" in ctx.resource_text
    assert "file2.txt" in ctx.resource_text


def test_rejected_resource_surfaces_preflight_warning(store):
    """A referenced resource with non-ready status (e.g. rejected extension)
    must produce a warning — not be silently dropped."""
    # Upload a file with unsupported extension to get a rejected status
    rec = store.upload("data.xyz", "binary data".encode("utf-8"))
    session_id = rec["scope_id"]
    # The resource should have a non-ready status
    assert rec.get("status") != "ready"
    # If the resource_id exists, try to reference it
    resource_id = rec.get("resource_id", "")
    if resource_id:
        ctx = build_step_run_context(
            store,
            workflow_id="wf_1",
            step_id="wf_1_step_0",
            agent_key="content_creator",
            quick_brief="",
            product_refs=[],
            resource_refs=[f"resource:{resource_id}"],
            upload_session_id=session_id,
        )
        # Must have a warning about the non-ready resource
        assert len(ctx.warnings) > 0
        warning_text = " ".join(ctx.warnings)
        assert "not ready" in warning_text.lower() or "missing" in warning_text.lower()


def test_missing_resource_surfaces_warning(store):
    """A referenced resource that doesn't exist must produce a warning."""
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=[],
        resource_refs=["resource:nonexistent_id"],
        upload_session_id="some_session",
    )
    assert len(ctx.warnings) > 0
    assert "missing" in " ".join(ctx.warnings).lower() or "invalid" in " ".join(ctx.warnings).lower()


def test_attachment_context_treated_as_supplied_info(store):
    """Attachment context must be labeled as user-provided/supplied info,
    not as web research evidence."""
    rec = store.upload("brief.txt", "campaign budget 50000".encode("utf-8"))
    session_id = rec["scope_id"]
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="campaign_strategy",
        quick_brief="",
        product_refs=[],
        resource_refs=[f"resource:{rec['resource_id']}"],
        upload_session_id=session_id,
    )
    # The resource context must label content as user-provided
    assert "User-provided" in ctx.resource_text or "ผู้ใช้" in ctx.resource_text
    # Must mention supplied facts / no web citation needed
    assert "supplied" in ctx.resource_text.lower() or "ไม่ต้องมี web citation" in ctx.resource_text


def test_attachments_and_quick_brief_coexist(store):
    """Attachments and quick_brief must be able to coexist in the same run."""
    rec = store.upload("note.txt", "extra context".encode("utf-8"))
    session_id = rec["scope_id"]
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="สร้างคอนเทนต์แนวใหม่",
        product_refs=[],
        resource_refs=[f"resource:{rec['resource_id']}"],
        upload_session_id=session_id,
    )
    assert ctx.quick_brief == "สร้างคอนเทนต์แนวใหม่"
    assert "extra context" in ctx.resource_text


# ---------------------------------------------------------------------------
# Fatal resource preflight at BaseAgent.run — the single shared seam
# ---------------------------------------------------------------------------


class _RecordingLLM:
    """LLM stub that records whether it was called."""

    def __init__(self, output: str = "## ok\n\nbody") -> None:
        self.output = output
        self.called = False

    def chat(self, *a, **kw):  # noqa: D401
        self.called = True
        return self.output


def _make_agent(store, tmp_path):
    from src.agents.base_agent import BaseAgent

    cfg = {
        "agent_name": "test_agent",
        "display_name": "Test Agent",
        "system_prompt": "You are a test agent.",
        "model": "test-model",
        "provider": {"name": "test"},
        "temperature": 0.0,
    }
    llm = _RecordingLLM()
    return BaseAgent(cfg, llm), llm


def test_base_agent_run_blocks_when_step_context_has_warnings(store, tmp_path):
    """If step_context.warnings is non-empty, BaseAgent.run must raise before
    calling the LLM — the single shared seam guaranteeing no agent execution
    can silently proceed with a non-ready referenced resource."""
    agent, llm = _make_agent(store, tmp_path)
    bad_ctx = StepRunContext(
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        input_refs=(),
        product_refs=(),
        resource_refs=("resource:missing_id",),
        resource_text="",
        resource_image_paths=(),
        resource_trace=(),
        warnings=("missing or invalid resource refs: resource:missing_id",),
    )
    with pytest.raises(ValueError, match="resource preflight failed"):
        agent.run("user prompt", step_context=bad_ctx)
    assert not llm.called, "LLM must not be called after fatal resource preflight"


def test_base_agent_run_proceeds_when_step_context_has_no_warnings(store, tmp_path):
    """A ready resource (warnings empty) must allow agent execution."""
    agent, llm = _make_agent(store, tmp_path)
    rec = store.upload("brief.txt", b"campaign budget 50000")
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=[],
        resource_refs=[f"resource:{rec['resource_id']}"],
        upload_session_id=rec["scope_id"],
    )
    assert ctx.warnings == ()
    result = agent.run("user prompt", step_context=ctx)
    assert llm.called
    assert result == "## ok\n\nbody"


def test_base_agent_run_proceeds_with_zero_resources(store, tmp_path):
    """Zero attachments (empty warnings) must remain a valid execution path."""
    agent, llm = _make_agent(store, tmp_path)
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=[],
        resource_refs=[],
        upload_session_id="",
    )
    assert ctx.warnings == ()
    result = agent.run("user prompt", step_context=ctx)
    assert llm.called


def test_base_agent_run_proceeds_without_step_context(store, tmp_path):
    """When no step_context is supplied (e.g. standalone/script paths), the
    preflight does not apply and execution proceeds normally."""
    agent, llm = _make_agent(store, tmp_path)
    result = agent.run("user prompt")
    assert llm.called


def test_base_agent_run_truncated_ready_resource_remains_valid(store, tmp_path):
    """A ready resource that was truncated is still valid — fatal preflight
    must not block on truncation, only on non-ready/missing/rejected refs."""
    agent, llm = _make_agent(store, tmp_path)
    long_text = "x" * 1000
    rec = store.upload("big.txt", long_text.encode("utf-8"))
    ctx = build_step_run_context(
        store,
        workflow_id="wf_1",
        step_id="wf_1_step_0",
        agent_key="content_creator",
        quick_brief="",
        product_refs=[],
        resource_refs=[f"resource:{rec['resource_id']}"],
        upload_session_id=rec["scope_id"],
    )
    # Truncation does not produce a warning — only non-ready refs do
    assert ctx.warnings == ()
    result = agent.run("user prompt", step_context=ctx)
    assert llm.called
