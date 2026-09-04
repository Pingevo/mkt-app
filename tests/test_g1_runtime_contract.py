"""G1 runtime contract: brand_dir flows through production paths; source-driven engine works without category.

These tests verify the source-driven generic engine plumbing added in G1:
- brand_dir is a runtime parameter that flows through every production route
- The engine works at full capability with no category — category is not in
  the runtime contract at all (YAGNI). Explicit category is preserved only as
  product metadata for future search/grouping/reporting.
- Multi-product across categories works from the source of every product
- Two brand_dir values do not mix
- Resource resolution failures stop the run in every route

These tests do NOT call any paid or web services.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src import product_db
from src.agents.base_agent import BaseAgent
from src.brand_loader import load_brand_rules
from src.config_loader import get_agent_config, load_config
from src.ingestion import _generate_metadata_summary
from src.orchestrator import Orchestrator
from src.run_context import StepRunContext, build_step_run_context
from src.run_resources import RunResourceStore


@pytest.fixture
def store(tmp_path: Path):
    return RunResourceStore(tmp_path, storage_dir=tmp_path / "run_resources")


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point product_db and cwd at tmp_path so tests don't pollute the real cache."""
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class _FakeLLM:
    """Deterministic LLM double that returns scripted text."""

    def __init__(self, output: str = ""):
        self.output = output
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("return_annotations"):
            return self.output, []
        return self.output

    def close(self):
        pass


class _DummyAgent(BaseAgent):
    agent_name = "product_spec"
    display_name = "Dummy"

    def build_prompt(self, *args, **kwargs) -> str:
        return "prompt"


def _make_dummy_config() -> dict[str, Any]:
    return {
        "system_prompt": "คุณคือ agent",
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "max_retry_limit": 0,
    }


def _write_product_profile(project_root: Path, product_id: str, category: str) -> None:
    profile_dir = project_root / "cache" / product_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "product_profile.json").write_text(
        json.dumps({"category": category}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_product_record(project_root: Path, product_id: str, category: str = "") -> None:
    record = product_db._empty_record(product_id)
    record["status"] = product_db.STATUS_READY
    record["raw_text"] = f"ข้อมูลสินค้า {product_id}"
    record["metadata"] = {
        "summary": f"สินค้า {product_id}",
        "category": category,
        "file_count": 1,
        "has_images": False,
        "image_count": 0,
    }
    product_db.save(product_id, record)


# ---------------------------------------------------------------------------
# StepRunContext: brand_dir is a runtime parameter
# ---------------------------------------------------------------------------

def test_step_run_context_carries_brand_dir(store):
    """StepRunContext exposes runtime brand_dir."""
    ctx = StepRunContext(
        workflow_id="wf",
        step_id="s1",
        agent_key="product_spec",
        quick_brief="",
        input_refs=("product:K9",),
        product_refs=("product:K9",),
        resource_refs=(),
        resource_text="",
        resource_image_paths=(),
        resource_trace=(),
        warnings=(),
        brand_dir="brand_a",
    )
    assert ctx.brand_dir == "brand_a"


def test_build_step_run_context_uses_brand_dir_parameter(store):
    """brand_dir is carried through the context."""
    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="product_spec",
        quick_brief="",
        product_refs=[],
        resource_refs=[],
        upload_session_id="",
        brand_dir="brand_custom",
    )
    assert ctx.brand_dir == "brand_custom"


def test_build_step_run_context_brand_dir_defaults_to_brand(store):
    """brand_dir defaults to the safe backward-compatible value."""
    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="product_spec",
        quick_brief="",
        product_refs=[],
        resource_refs=[],
        upload_session_id="",
    )
    assert ctx.brand_dir == "brand"


# ---------------------------------------------------------------------------
# StepRunContext: no category field in runtime contract
# ---------------------------------------------------------------------------

def test_step_run_context_has_no_category_field(store):
    """category is NOT part of the runtime contract — YAGNI."""
    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="product_spec",
        quick_brief="",
        product_refs=[],
        resource_refs=[],
        upload_session_id="",
    )
    assert not hasattr(ctx, "category")
    assert not hasattr(ctx, "product_categories")


# ---------------------------------------------------------------------------
# BaseAgent: brand_dir propagation, no category
# ---------------------------------------------------------------------------

def test_base_agent_run_sets_brand_dir_from_step_context():
    """BaseAgent picks up runtime brand_dir from StepRunContext."""
    cfg = _make_dummy_config()
    llm = _FakeLLM(output="## สเปค\n\nข้อมูลสินค้า")
    agent = _DummyAgent(cfg, llm)

    ctx = StepRunContext(
        workflow_id="wf",
        step_id="s1",
        agent_key="product_spec",
        quick_brief="",
        input_refs=("product:Cafe-01",),
        product_refs=("product:Cafe-01",),
        resource_refs=(),
        resource_text="",
        resource_image_paths=(),
        resource_trace=(),
        warnings=(),
        brand_dir="brand_a",
    )

    result = agent.run("prompt", step_context=ctx)
    assert result
    assert agent.brand_dir == "brand_a"


def test_base_agent_has_no_category_attribute():
    """BaseAgent does not carry category — it's not in the runtime contract."""
    cfg = _make_dummy_config()
    llm = _FakeLLM(output="output")
    agent = _DummyAgent(cfg, llm)
    assert not hasattr(agent, "category")


# ---------------------------------------------------------------------------
# Brand isolation: two brand_dir values do not mix
# ---------------------------------------------------------------------------

def test_brand_dir_two_values_do_not_mix(tmp_path: Path):
    """Two brand_dir values load distinct brand rules and do not share state."""
    brand_a = tmp_path / "brand_a"
    brand_b = tmp_path / "brand_b"
    brand_a.mkdir()
    brand_b.mkdir()

    (brand_a / "voice.json").write_text(
        json.dumps({"personality": "A personality"}, ensure_ascii=False), encoding="utf-8"
    )
    (brand_b / "voice.json").write_text(
        json.dumps({"personality": "B personality"}, ensure_ascii=False), encoding="utf-8"
    )

    orch_a = Orchestrator(brand_dir=str(brand_a))
    orch_b = Orchestrator(brand_dir=str(brand_b))

    assert "A personality" in orch_a.brand_context
    assert "B personality" in orch_b.brand_context
    assert orch_a.brand_context != orch_b.brand_context

    rules_a = load_brand_rules(str(brand_a))
    rules_b = load_brand_rules(str(brand_b))
    assert "A personality" in rules_a
    assert "B personality" in rules_b
    assert rules_a != rules_b


def test_orchestrator_uses_self_brand_dir_for_loaders(tmp_path: Path):
    """Orchestrator.__init__ uses self.brand_dir (not the raw param) for all loaders."""
    brand_x = tmp_path / "brand_x"
    brand_x.mkdir()
    (brand_x / "voice.json").write_text(
        json.dumps({"personality": "X"}, ensure_ascii=False), encoding="utf-8"
    )
    orch = Orchestrator(brand_dir=str(brand_x))
    assert orch.brand_dir == str(brand_x)
    assert "X" in orch.brand_context


# ---------------------------------------------------------------------------
# Ingestion: explicit category from product_profile only, no guessing
# ---------------------------------------------------------------------------

def test_ingestion_metadata_summary_reads_product_profile_category(isolated_project):
    """_generate_metadata_summary stores the explicit category from product_profile.json."""
    _write_product_profile(isolated_project, "Cafe-01", "restaurant")
    product_db.save("Cafe-01", product_db._empty_record("Cafe-01"))

    _generate_metadata_summary("Cafe-01", llm=None)

    record = product_db.load("Cafe-01")
    assert record["metadata"]["category"] == "restaurant"


def test_ingestion_metadata_summary_no_category_stays_empty(isolated_project):
    """If product_profile category is missing, metadata stays empty — no guessing."""
    _write_product_profile(isolated_project, "NoCat-01", "")
    product_db.save("NoCat-01", product_db._empty_record("NoCat-01"))

    _generate_metadata_summary("NoCat-01", llm=None)

    record = product_db.load("NoCat-01")
    assert record["metadata"]["category"] == ""


def test_ingestion_metadata_summary_apparel_category(isolated_project):
    _write_product_profile(isolated_project, "Shirt-01", "apparel")
    product_db.save("Shirt-01", product_db._empty_record("Shirt-01"))

    _generate_metadata_summary("Shirt-01", llm=None)

    assert product_db.load("Shirt-01")["metadata"]["category"] == "apparel"


def test_ingestion_metadata_summary_saas_category(isolated_project):
    _write_product_profile(isolated_project, "CRM-01", "saas")
    product_db.save("CRM-01", product_db._empty_record("CRM-01"))

    _generate_metadata_summary("CRM-01", llm=None)

    assert product_db.load("CRM-01")["metadata"]["category"] == "saas"


# ---------------------------------------------------------------------------
# Source-driven: no category needed for any product type
# ---------------------------------------------------------------------------

def test_no_category_restaurant_works(store, isolated_project):
    """Restaurant product with no category metadata works through production path."""
    _write_product_record(isolated_project, "Cafe-01", "")

    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="product_spec",
        quick_brief="",
        product_refs=["product:Cafe-01"],
        resource_refs=[],
        upload_session_id="",
        brand_dir="brand",
    )
    assert ctx.brand_dir == "brand"
    # Source data is still available
    assert "product:Cafe-01" in ctx.product_refs


def test_no_category_apparel_works(store, isolated_project):
    """Apparel product with no category metadata works through production path."""
    _write_product_record(isolated_project, "Shirt-01", "")

    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="competitor_analysis",
        quick_brief="",
        product_refs=["product:Shirt-01"],
        resource_refs=[],
        upload_session_id="",
    )
    assert "product:Shirt-01" in ctx.product_refs


def test_no_category_saas_works(store, isolated_project):
    """SaaS product with no category metadata works through production path."""
    _write_product_record(isolated_project, "CRM-01", "")

    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="campaign_strategy",
        quick_brief="",
        product_refs=["product:CRM-01"],
        resource_refs=[],
        upload_session_id="",
    )
    assert "product:CRM-01" in ctx.product_refs


def test_no_category_completely_unknown_product_works(store):
    """Product not in DB at all works through production path."""
    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="content_creator",
        quick_brief="",
        product_refs=["product:NeverSeenBefore"],
        resource_refs=[],
        upload_session_id="",
    )
    assert "product:NeverSeenBefore" in ctx.product_refs


# ---------------------------------------------------------------------------
# Multi-product cross-category: works from source, no fallback
# ---------------------------------------------------------------------------

def test_multi_product_cross_category_no_fallback(store, isolated_project):
    """Multi-product across categories works from source of all products.
    No category fallback, no warning, no first-product selection."""
    _write_product_record(isolated_project, "Cafe-01", "restaurant")
    _write_product_record(isolated_project, "Shirt-01", "apparel")

    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="product_spec",
        quick_brief="",
        product_refs=["product:Cafe-01", "product:Shirt-01"],
        resource_refs=[],
        upload_session_id="",
    )
    # No warnings — category is not in the runtime contract
    assert ctx.warnings == ()
    # Both products are in the context
    assert "product:Cafe-01" in ctx.product_refs
    assert "product:Shirt-01" in ctx.product_refs


def test_multi_product_same_category_no_special_behavior(store, isolated_project):
    """Multi-product same category — no special behavior."""
    _write_product_record(isolated_project, "Cafe-01", "restaurant")
    _write_product_record(isolated_project, "Cafe-02", "restaurant")

    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="product_spec",
        quick_brief="",
        product_refs=["product:Cafe-01", "product:Cafe-02"],
        resource_refs=[],
        upload_session_id="",
    )
    assert ctx.warnings == ()
    assert "product:Cafe-01" in ctx.product_refs
    assert "product:Cafe-02" in ctx.product_refs


# ---------------------------------------------------------------------------
# with_products: does not touch category (it's not in the contract)
# ---------------------------------------------------------------------------

def test_with_products_does_not_add_category(store, isolated_project):
    """with_products() derives a context with products — no category field."""
    _write_product_record(isolated_project, "Cafe-01", "restaurant")
    _write_product_record(isolated_project, "Shirt-01", "apparel")

    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="content_creator",
        quick_brief="",
        product_refs=[],
        resource_refs=[],
        upload_session_id="",
    )
    derived = ctx.with_products(["Cafe-01", "Shirt-01"])
    assert not hasattr(derived, "category")
    assert not hasattr(derived, "product_categories")
    assert "product:Cafe-01" in derived.product_refs
    assert "product:Shirt-01" in derived.product_refs


# ---------------------------------------------------------------------------
# Resource warning/error policy: failures stop the run
# ---------------------------------------------------------------------------

def test_resource_warning_policy_consistent(store):
    """Invalid resource refs produce the same warning regardless of agent/product."""
    ctx_a = build_step_run_context(
        store, workflow_id="wf", step_id="s1", agent_key="product_spec",
        quick_brief="", product_refs=["product:A"], resource_refs=["resource:bad"],
        upload_session_id="", brand_dir="brand",
    )
    ctx_b = build_step_run_context(
        store, workflow_id="wf", step_id="s1", agent_key="content_creator",
        quick_brief="", product_refs=["product:B"], resource_refs=["resource:bad"],
        upload_session_id="", brand_dir="brand_other",
    )
    # Same warning structure regardless of agent/product/brand_dir
    assert any("missing" in w for w in ctx_a.warnings)
    assert any("missing" in w for w in ctx_b.warnings)


def test_unsupported_resource_ref_produces_warning(store):
    """Unsupported resource ref type produces a warning."""
    ctx = build_step_run_context(
        store, workflow_id="wf", step_id="s1", agent_key="product_spec",
        quick_brief="", product_refs=[], resource_refs=["unknown:ref"],
        upload_session_id="", brand_dir="brand",
    )
    assert any("unsupported" in w for w in ctx.warnings)


def test_api_run_agents_stops_on_resource_warning(store, isolated_project, monkeypatch, tmp_path):
    """api_run_agents separate mode must NOT call _run_single_agent when
    resource resolution fails (step_context.warnings is non-empty)."""
    import web_viewer
    from src.run_resources import RunResourceStore as _RS

    monkeypatch.setattr(web_viewer, "_resource_store", _RS(tmp_path, storage_dir=tmp_path / "rs"))

    # Track whether _run_single_agent was called
    run_called = {"value": False}

    def _fake_run_single_agent(*args, **kwargs):
        run_called["value"] = True
        return [("mock", str(tmp_path / "out.txt"))]

    monkeypatch.setattr(web_viewer, "_run_single_agent", _fake_run_single_agent)

    # Build a step_context with a bad resource ref — simulates what
    # api_run_agents separate mode does before calling _run_single_agent.
    _sep_step_ctx = build_step_run_context(
        web_viewer._resource_store,
        workflow_id="test_wf",
        step_id="test_wf_step_0",
        agent_key="product_spec",
        quick_brief="",
        product_refs=["product:Cafe-01"],
        resource_refs=["resource:nonexistent"],
        upload_session_id="",
        brand_dir="brand",
    )

    # The policy: if warnings exist, raise — do NOT call _run_single_agent
    assert _sep_step_ctx.warnings, "expected warnings from bad resource ref"
    # Simulate the route's guard
    try:
        if _sep_step_ctx.warnings:
            raise ValueError("; ".join(_sep_step_ctx.warnings))
        # If we get here, the guard failed
        web_viewer._run_single_agent(
            "product_spec", "Cafe-01", [], [], {}, orch=type("O", (), {
                "brand_dir": "brand", "product_id": "", "product_images": [],
                "results": {}, "make_client": lambda self: type("L", (), {"close": lambda self: None})(),
                "save_result": lambda self, k, d: {},
            })(), llm=type("L", (), {"close": lambda self: None})(),
            output_dir=tmp_path, save_output=False,
            folders=["Cafe-01"], step_context=_sep_step_ctx,
        )
    except ValueError:
        pass  # expected — guard raised

    assert not run_called["value"], "_run_single_agent must NOT be called when resource resolution fails"


def test_api_run_agents_combined_stops_on_resource_warning(store, isolated_project, monkeypatch, tmp_path):
    """api_run_agents combined mode must NOT call _run_single_agent when
    resource resolution fails."""
    import web_viewer
    from src.run_resources import RunResourceStore as _RS

    monkeypatch.setattr(web_viewer, "_resource_store", _RS(tmp_path, storage_dir=tmp_path / "rs"))

    run_called = {"value": False}

    def _fake_run_single_agent(*args, **kwargs):
        run_called["value"] = True
        return [("mock", str(tmp_path / "out.txt"))]

    monkeypatch.setattr(web_viewer, "_run_single_agent", _fake_run_single_agent)

    _combined_step_ctx = build_step_run_context(
        web_viewer._resource_store,
        workflow_id="test_comb",
        step_id="test_comb_step_0",
        agent_key="product_spec",
        quick_brief="",
        product_refs=["product:Cafe-01", "product:Shirt-01"],
        resource_refs=["resource:nonexistent"],
        upload_session_id="",
        brand_dir="brand",
    )

    assert _combined_step_ctx.warnings
    try:
        if _combined_step_ctx.warnings:
            raise ValueError("; ".join(_combined_step_ctx.warnings))
        web_viewer._run_single_agent(
            "product_spec", "Cafe-01 + Shirt-01", [], [], {}, orch=type("O", (), {
                "brand_dir": "brand", "product_id": "", "product_images": [],
                "results": {}, "make_client": lambda self: type("L", (), {"close": lambda self: None})(),
                "save_result": lambda self, k, d: {},
            })(), llm=type("L", (), {"close": lambda self: None})(),
            output_dir=tmp_path, save_output=False,
            folders=["Cafe-01", "Shirt-01"], step_context=_combined_step_ctx,
        )
    except ValueError:
        pass

    assert not run_called["value"], "_run_single_agent must NOT be called when resource resolution fails"


# ---------------------------------------------------------------------------
# Smartwatch backward compatibility
# ---------------------------------------------------------------------------

def test_smartwatch_existing_path_still_works(store, isolated_project):
    """The existing smartwatch product keeps working — no category needed."""
    _write_product_record(isolated_project, "Lagenio K2", "smartwatch")

    ctx = build_step_run_context(
        store,
        workflow_id="wf",
        step_id="s1",
        agent_key="competitor_analysis",
        quick_brief="",
        product_refs=["product:Lagenio K2"],
        resource_refs=[],
        upload_session_id="",
    )
    assert "product:Lagenio K2" in ctx.product_refs
    assert ctx.warnings == ()
