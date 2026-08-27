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
