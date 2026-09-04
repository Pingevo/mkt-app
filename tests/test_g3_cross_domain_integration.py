"""G3 — Minimal Cross-Domain Offline Integration Acceptance.

Proves that the source-driven engine (G1 runtime contract + G2 open evidence
schema) handles cross-domain data through production-equivalent paths.
No new architecture, validators, schema, or domain rules are added.

Production seams exercised (real construction, not bypassed):
  - Orchestrator(brand_dir=...) — real constructor, real brand_loader
  - build_step_run_context — real resource resolution
  - Orchestrator.run_product_spec / run_competitor_analysis / run_campaign_strategy
  - CompetitorAnalysisAgent.run in evidence_mode (production config kept ON)
  - CompetitorReportRenderer — real validation + provenance
  - /api/run_agent and /api/run_agents HTTP routes via FastAPI TestClient

Mock boundary (only external boundaries):
  - FakeLLM: replaces LLMClient at the LLM/network boundary
  - Orchestrator.make_client: patched to return FakeLLM (route tests only)
  - filesystem root: tmp_path for test isolation
  - web tool response: FakeLLM returns scripted annotations

Production behavior NOT disabled:
  - evidence_mode stays True (production config)
  - web_search stays True (production config) — FakeLLM simulates the response
  - output_quality / semantic_rules / validators stay ON
  - brand_loader reads real voice.json from tmp_path brand dirs
  - product_db reads real records from tmp_path
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from src import product_db
from src.agents.competitor_evidence import CompetitorReportRenderer, ResearchResponse
from src.orchestrator import Orchestrator
from src.run_context import build_step_run_context
from src.run_resources import RunResourceStore


# ---------------------------------------------------------------------------
# Fixtures: real production construction with tmp_path isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path):
    return RunResourceStore(tmp_path, storage_dir=tmp_path / "run_resources")


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point product_db and cwd at tmp_path so tests don't pollute the real cache."""
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _make_brand_dir(tmp_path: Path, name: str, personality: str) -> Path:
    """Create a real brand directory with voice.json that brand_loader reads."""
    brand = tmp_path / name
    brand.mkdir(parents=True, exist_ok=True)
    (brand / "voice.json").write_text(
        json.dumps({"personality": personality}, ensure_ascii=False), encoding="utf-8"
    )
    return brand


def _write_product(project_root: Path, product_id: str, raw_text: str, category: str = "") -> None:
    """Write a READY product DB record with domain-specific raw_text."""
    record = product_db._empty_record(product_id)
    record["status"] = product_db.STATUS_READY
    record["raw_text"] = raw_text
    record["metadata"] = {
        "summary": product_id,
        "category": category,
        "file_count": 1,
        "has_images": False,
        "image_count": 0,
    }
    product_db.save(product_id, record)


class _FakeLLM:
    """Deterministic LLM double at the network boundary.

    Returns (text, annotations) when return_annotations=True, matching the
    real LLMClient.chat contract.  Captures all calls for assertion.
    """

    def __init__(self, output: str = "", annotations: list | None = None,
                 revision_output: str = ""):
        self.output = output
        self.annotations = annotations or []
        self.revision_output = revision_output or output
        self.calls: list[dict[str, Any]] = []
        self._last_raw_response = {
            "usage": {"server_tool_use_details": {"web_search_requests": 1, "tool_calls_executed": 1}}
        }
        self._last_raw_annotations_count = 0

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        self._last_raw_annotations_count = len(self.annotations)
        source = kwargs.get("source", "")
        if ".semantic_review" in source:
            # Semantic review: keep all evidence (no changes)
            import json as _json
            return _json.dumps([{"index": i, "action": "keep"} for i in range(10)], ensure_ascii=False)
        if ".revise" in source:
            return self.revision_output
        if ".repair" in source:
            return self.output
        if ".review" in source:
            # Review returns the same output → early termination (no change)
            return self.output
        if kwargs.get("return_annotations"):
            return self.output, self.annotations
        return self.output

    def close(self):
        pass


def _build_ctx(store, product_refs: list[str], quick_brief: str = "", brand_dir: str = "brand"):
    return build_step_run_context(
        store,
        workflow_id="g3_wf",
        step_id="g3_step_0",
        agent_key="product_spec",
        quick_brief=quick_brief,
        product_refs=product_refs,
        resource_refs=[],
        upload_session_id="",
        brand_dir=brand_dir,
    )


# ---------------------------------------------------------------------------
# A. Cross-domain product/context plumbing (parametrized)
# ---------------------------------------------------------------------------


_DOMAIN_SOURCES = {
    "restaurant": (
        "ร้าน Sushi Bar A\nเมนู: ซูชิ, ซาชิมิ, ราเมน\n"
        "เมนูเด็ด: Tonkotsu ramen with 18-hour broth\nช่วงราคา: 120-180 บาท"
    ),
    "apparel": (
        "Brand X T-Shirt\nmaterial: 100% organic cotton\n"
        "size: S, M, L, XL\ncare: machine wash cold\nprice: $29.99"
    ),
    "saas": (
        "CloudApp Pro\nplan tier: Business $49/month\n"
        "integration: Slack, Zapier, REST API\nAPI rate limit: 1000 requests/minute"
    ),
    "unknown": "Mystery Service Q\nproperty: custom XYZ\nvalue: 42",
}


@pytest.mark.parametrize("domain,source_text", list(_DOMAIN_SOURCES.items()))
def test_cross_domain_source_reaches_agent_prompt(
    domain, source_text, store, isolated_project, tmp_path
):
    """Source data for each domain reaches the agent prompt through the real
    production path: build_step_run_context → Orchestrator.run_product_spec
    → agent.run.  No smartwatch/electronics fields are forced; unknown data
    is not routed to any template/category behavior."""
    product_id = f"G3-{domain}"
    _write_product(isolated_project, product_id, source_text, domain if domain != "unknown" else "")

    brand = _make_brand_dir(tmp_path, "brand_g3", "neutral")
    ctx = _build_ctx(store, [f"product:{product_id}"], brand_dir=str(brand))
    assert ctx.warnings == ()

    orch = Orchestrator(brand_dir=str(brand), product_id=product_id)
    fake = _FakeLLM(output=f"## สเปคสินค้า\n\n{source_text[:40]}")
    result = orch.run_product_spec(source_text, llm=fake, step_context=ctx)

    # Source data reached the prompt sent to the LLM
    prompt_text = str(fake.calls[0]["messages"])
    for key_fragment in source_text.split("\n")[:2]:
        assert key_fragment.strip() in prompt_text, (
            f"[{domain}] source fragment '{key_fragment.strip()}' must reach agent prompt"
        )

    # No smartwatch/electronics fields are injected by the engine
    system_prompt = str(fake.calls[0]["messages"])
    assert "battery" not in system_prompt.lower() or "battery" in source_text.lower()
    assert "gps" not in system_prompt.lower() or "gps" in source_text.lower()

    # Result is non-empty (production validators passed)
    assert result.strip()


# ---------------------------------------------------------------------------
# B. Competitor evidence path — real evidence_mode through orchestrator
# ---------------------------------------------------------------------------


def _saas_research_response_json() -> str:
    """A valid ResearchResponse for a SaaS product with non-smartwatch fields."""
    return json.dumps({
        "target_model": "CloudApp Pro",
        "competitor_names": ["SaaS Competitor Z"],
        "evidence": [
            {
                "competitor": "SaaS Competitor Z",
                "field": "plan_tier",
                "claim": "Business plan at $49/month",
                "url": "https://example.com/saas-competitor-z-pricing",
                "geography": "global",
            },
            {
                "competitor": "SaaS Competitor Z",
                "field": "api_rate_limit",
                "claim": "1000 requests/minute on Business plan",
                "url": "https://example.com/saas-competitor-z-pricing",
                "geography": "global",
            },
        ],
        "evidence_based_recommendations": [
            {
                "text": "เน้น plan tier ที่ยืดหยุ่นของ CloudApp Pro เปรียบเทียบกับ SaaS Competitor Z",
                "supporting_evidence_urls": ["https://example.com/saas-competitor-z-pricing"],
            },
        ],
        "strategic_hypotheses": [
            {
                "text": "SaaS Competitor Z อาจลดราคาในไตรมาสหน้า",
                "rationale": "แนวโน้มตลาด SaaS มีการแข่งขันราคา",
            },
        ],
        "uncertainty": ["ยังไม่พบข้อมูล integration ของ SaaS Competitor Z"],
    }, ensure_ascii=False)


def _saas_annotations() -> list[dict]:
    """Annotations whose identity matches the competitor name in the research
    response, so _assess_source_relevance returns relevant=True after
    _reassess_for_default_discovery."""
    return [
        {
            "url": "https://example.com/saas-competitor-z-pricing",
            "title": "SaaS Competitor Z - Pricing and Plans",
            "content": "SaaS Competitor Z business plan pricing and API rate limits",
        },
    ]


def test_saas_evidence_mode_through_orchestrator(store, isolated_project, tmp_path):
    """SaaS competitor evidence flows through the real evidence_mode pipeline:
    Orchestrator.run_competitor_analysis → CompetitorAnalysisAgent.run
    (evidence_mode=True) → validation → CompetitorReportRenderer.

    Production config is kept ON (evidence_mode, web_search, output_quality).
    FakeLLM simulates the web search response with structured evidence +
    annotations whose identity matches the competitor."""
    raw = _DOMAIN_SOURCES["saas"]
    _write_product(isolated_project, "G3-saas", raw, "saas")

    brand = _make_brand_dir(tmp_path, "brand_g3", "neutral")
    ctx = _build_ctx(store, ["product:G3-saas"], brand_dir=str(brand))

    orch = Orchestrator(brand_dir=str(brand), product_id="G3-saas")
    fake = _FakeLLM(
        output=_saas_research_response_json(),
        annotations=_saas_annotations(),
    )
    result = orch.run_competitor_analysis("", None, llm=fake, step_context=ctx)

    # Evidence-mode produced a rendered Markdown report (not a structural failure)
    assert "**structural_output_failed**" not in result
    assert "**required_search_failed**" not in result

    # Open schema accepted SaaS-specific field IDs (not smartwatch fields)
    assert "plan_tier" in result
    assert "api_rate_limit" in result

    # Evidence provenance is preserved (URL appears in rendered output)
    assert "https://example.com/saas-competitor-z-pricing" in result

    # Competitor attribution worked (competitor name appears)
    assert "SaaS Competitor Z" in result

    # No electronics/watch assumptions injected
    assert "battery" not in result.lower()
    assert "gps" not in result.lower()
    assert "display" not in result.lower() or "display" in raw.lower()


# ---------------------------------------------------------------------------
# C. Actual HTTP routes via FastAPI TestClient
# ---------------------------------------------------------------------------


def _setup_route_isolation(monkeypatch, tmp_path: Path):
    """Patch filesystem roots so routes use tmp_path. Returns (data_dir, cache_dir)."""
    import web_viewer

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    return tmp_path / "data", tmp_path / "cache"


def _patch_llm_boundary(monkeypatch, fake_llm: _FakeLLM):
    """Patch Orchestrator.make_client to return FakeLLM — the LLM/network
    boundary.  Production route handling, context building, and orchestrator
    construction remain real."""
    import web_viewer

    def _fake_make_client(self):
        return fake_llm

    monkeypatch.setattr(web_viewer.Orchestrator, "make_client", _fake_make_client)


def _write_data_product(data_dir: Path, product_id: str, text: str):
    """Write a real product folder in data/ that _read_folder picks up."""
    pdir = data_dir / product_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "info.txt").write_text(text, encoding="utf-8")


def test_single_agent_http_route(monkeypatch, tmp_path):
    """Single-agent request through the real /api/run_agent HTTP route.
    Verifies: request parsing → route handling → build_step_run_context
    → _run_single_agent → orchestrator → agent.run → SSE response contract."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    # _current_llm is a module-level global used by the route; ensure it
    # exists after reload so the worker can read/write it.
    monkeypatch.setattr(web_viewer, "_current_llm", None, raising=False)

    data_dir, cache_dir = _setup_route_isolation(monkeypatch, tmp_path)
    _write_data_product(data_dir, "G3-Route-A", "Restaurant data: menu, price 120-180 baht")

    # Write a READY product DB record so _get_product_data finds it
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    _write_product(tmp_path, "G3-Route-A", "Restaurant data: menu, price 120-180 baht", "restaurant")

    brand = _make_brand_dir(tmp_path, "brand_route", "BrandRouteVoice")
    fake = _FakeLLM(output="## สเปคสินค้า\n\nร้านอาหาร G3-Route-A เมนูซูชิ ราคา 120-180 บาท")
    _patch_llm_boundary(monkeypatch, fake)

    # NOTE: _get_orchestrator is NOT patched — production factory
    # Orchestrator(brand_dir=brand_dir or "brand") is used with the
    # brand_dir from the HTTP body.  This proves the brand directory
    # sent in the request is loaded through the real production path.

    client = TestClient(web_viewer.app)
    resp = client.post("/api/run_agent", json={
        "agent": "product_spec",
        "folder": "G3-Route-A",
        "brand_dir": str(brand),
        "context": {"use_competitor": False, "use_campaign": False},
        "content_count": 1,
        "quick_brief": "",
    })
    assert resp.status_code == 200, resp.text

    # Consume SSE — collect agent_done and error events
    events = []
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            data = json.loads(line[6:])
            events.append(data)
            if data.get("type") == "error":
                pytest.fail(f"route error: {data.get('text')}")
            if data.get("type") == "done":
                break

    # Route contract: must have agent_start + agent_done + done
    types = [e["type"] for e in events]
    assert "agent_start" in types, f"missing agent_start: {types}"
    assert "agent_done" in types, f"missing agent_done: {types}"
    assert "done" in types, f"missing done: {types}"

    # Source data reached the LLM prompt
    assert len(fake.calls) > 0
    prompt_text = str(fake.calls[0]["messages"])
    assert "G3-Route-A" in prompt_text or "Restaurant" in prompt_text

    # Brand directory from HTTP body was loaded through the real
    # production _get_orchestrator → Orchestrator(brand_dir=...) →
    # load_brand_rules(voice.json).  The brand personality must appear
    # in the captured prompt, proving the production factory path.
    assert "BrandRouteVoice" in prompt_text, (
        "brand_dir from HTTP body was not loaded through production "
        "_get_orchestrator — brand voice missing from prompt"
    )


def test_multi_product_http_route(monkeypatch, tmp_path):
    """Multi-product request through the real /api/run_agents HTTP route
    (combined mode).  Verifies: both product refs are resolved, source blocks
    have separate identity, prompt does not contain data from a third product
    or a previous run, output is returned in the route SSE contract."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    monkeypatch.setattr(web_viewer, "_current_llm", None, raising=False)

    data_dir, cache_dir = _setup_route_isolation(monkeypatch, tmp_path)
    _write_data_product(data_dir, "G3-Multi-A", "Restaurant: ramen, price 120 baht")
    _write_data_product(data_dir, "G3-Multi-B", "SaaS: CloudApp, plan $49/mo, API 1000 req/min")

    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    _write_product(tmp_path, "G3-Multi-A", "Restaurant: ramen, price 120 baht", "restaurant")
    _write_product(tmp_path, "G3-Multi-B", "SaaS: CloudApp, plan $49/mo, API 1000 req/min", "saas")

    brand = _make_brand_dir(tmp_path, "brand_multi", "BrandMultiVoice")
    fake = _FakeLLM(output="## สเปคสินค้า\n\nร้านอาหาร + SaaS รวม")
    _patch_llm_boundary(monkeypatch, fake)

    # NOTE: /api/run_agents creates Orchestrator(brand_dir=brand_dir)
    # directly (not via _get_orchestrator).  brand_dir from the HTTP
    # body flows through the production constructor → load_brand_rules.

    client = TestClient(web_viewer.app)
    resp = client.post("/api/run_agents", json={
        "agents": ["product_spec"],
        "folders": ["G3-Multi-A", "G3-Multi-B"],
        "mode": "combined",
        "brand_dir": str(brand),
        "context": {"use_competitor": False, "use_campaign": False},
        "content_count": 1,
        "quick_brief": "",
    })
    assert resp.status_code == 200, resp.text

    events = []
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            data = json.loads(line[6:])
            events.append(data)
            if data.get("type") == "error":
                pytest.fail(f"route error: {data.get('text')}")
            if data.get("type") == "done":
                break

    # SSE contract: agent_start + agent_done + done, no error
    types = [e["type"] for e in events]
    assert "agent_start" in types, f"missing agent_start: {types}"
    assert "agent_done" in types, f"missing agent_done: {types}"
    assert "done" in types, f"missing done: {types}"
    assert "error" not in types, f"unexpected error event: {types}"

    # Both products' source data reached the prompt
    assert len(fake.calls) > 0
    prompt_text = str(fake.calls[0]["messages"])
    assert "ramen" in prompt_text, f"restaurant source missing from prompt"
    assert "CloudApp" in prompt_text, f"SaaS source missing from prompt"

    # Identity separation: production get_scoped_context_text wraps each
    # product's context in an identity envelope that names the product ID.
    # Assert each product's boundary is present and its source is inside.
    a_start = prompt_text.find("เริ่มข้อมูลสินค้า: G3-Multi-A")
    a_end = prompt_text.find("สิ้นสุดข้อมูลสินค้า: G3-Multi-A")
    b_start = prompt_text.find("เริ่มข้อมูลสินค้า: G3-Multi-B")
    b_end = prompt_text.find("สิ้นสุดข้อมูลสินค้า: G3-Multi-B")
    assert a_start != -1, "G3-Multi-A identity boundary missing from prompt"
    assert a_end != -1, "G3-Multi-A identity end boundary missing from prompt"
    assert b_start != -1, "G3-Multi-B identity boundary missing from prompt"
    assert b_end != -1, "G3-Multi-B identity end boundary missing from prompt"

    # Restaurant source is inside G3-Multi-A's boundary, not B's
    restaurant_pos = prompt_text.find("ramen")
    assert a_start < restaurant_pos < a_end, (
        "restaurant source not inside G3-Multi-A identity boundary"
    )
    assert not (b_start < restaurant_pos < b_end), (
        "restaurant source leaked into G3-Multi-B identity boundary"
    )

    # SaaS source is inside G3-Multi-B's boundary, not A's
    saas_pos = prompt_text.find("CloudApp")
    assert b_start < saas_pos < b_end, (
        "SaaS source not inside G3-Multi-B identity boundary"
    )
    assert not (a_start < saas_pos < a_end), (
        "SaaS source leaked into G3-Multi-A identity boundary"
    )

    # No third-product or previous-run leakage
    assert "G3-Route-A" not in prompt_text
    assert "G3-saas" not in prompt_text
    assert "G3-Brand-A" not in prompt_text
    assert "G3-Brand-B" not in prompt_text

    # Brand directory from HTTP body was loaded through the production
    # Orchestrator(brand_dir=...) constructor → brand voice in prompt
    assert "BrandMultiVoice" in prompt_text, (
        "brand_dir from HTTP body was not loaded through production "
        "Orchestrator constructor — brand voice missing from prompt"
    )


# ---------------------------------------------------------------------------
# D. Brand/resource isolation — real brand_loader, captured prompts
# ---------------------------------------------------------------------------


def test_brand_and_resource_isolation_through_real_loader(store, isolated_project, tmp_path):
    """Two brand directories with distinct voice.json pass through the real
    brand_loader (Orchestrator constructor).  Two run-scoped resources pass
    through the real RunResourceStore.  Captured prompts must contain only
    the brand and resource that own the run — not just different strings."""
    _write_product(isolated_project, "G3-Brand-A", "Product A data", "")
    _write_product(isolated_project, "G3-Brand-B", "Product B data", "")

    brand_a = _make_brand_dir(tmp_path, "brand_a", "Brand A personality voice")
    brand_b = _make_brand_dir(tmp_path, "brand_b", "Brand B personality voice")

    # Create two run-scoped resources through the real RunResourceStore
    session_a = store.create_upload_session()
    session_b = store.create_upload_session()
    res_a = store.upload(
        filename="resource_a.txt",
        content="Resource A content - confidential brief for brand A".encode("utf-8"),
        media_type="text/plain",
        session_id=session_a,
    )
    res_b = store.upload(
        filename="resource_b.txt",
        content="Resource B content - confidential brief for brand B".encode("utf-8"),
        media_type="text/plain",
        session_id=session_b,
    )
    assert res_a["status"] == "ready", f"resource A upload failed: {res_a}"
    assert res_b["status"] == "ready", f"resource B upload failed: {res_b}"
    ref_a = f"resource:{res_a['resource_id']}"
    ref_b = f"resource:{res_b['resource_id']}"

    # Build contexts with resource refs — each context references only its
    # own resource, scoped to its own upload session.
    ctx_a = build_step_run_context(
        store,
        workflow_id="g3_wf_a",
        step_id="g3_step_a",
        agent_key="product_spec",
        quick_brief="",
        product_refs=["product:G3-Brand-A"],
        resource_refs=[ref_a],
        upload_session_id=session_a,
        brand_dir=str(brand_a),
    )
    ctx_b = build_step_run_context(
        store,
        workflow_id="g3_wf_b",
        step_id="g3_step_b",
        agent_key="product_spec",
        quick_brief="",
        product_refs=["product:G3-Brand-B"],
        resource_refs=[ref_b],
        upload_session_id=session_b,
        brand_dir=str(brand_b),
    )
    assert ctx_a.warnings == (), f"ctx_a warnings: {ctx_a.warnings}"
    assert ctx_b.warnings == (), f"ctx_b warnings: {ctx_b.warnings}"

    # Real Orchestrator constructor — brand_loader reads voice.json
    orch_a = Orchestrator(brand_dir=str(brand_a), product_id="G3-Brand-A")
    orch_b = Orchestrator(brand_dir=str(brand_b), product_id="G3-Brand-B")

    assert "Brand A personality" in orch_a.brand_context
    assert "Brand B personality" in orch_b.brand_context
    assert orch_a.brand_context != orch_b.brand_context

    fake_a = _FakeLLM(output="## สเปค\n\nProduct A output")
    fake_b = _FakeLLM(output="## สเปค\n\nProduct B output")
    orch_a.run_product_spec("Product A data", llm=fake_a, step_context=ctx_a)
    orch_b.run_product_spec("Product B data", llm=fake_b, step_context=ctx_b)

    # Each prompt contains its own brand voice and product — not the other's
    prompt_a = str(fake_a.calls[0]["messages"])
    prompt_b = str(fake_b.calls[0]["messages"])

    assert "Brand A personality" in prompt_a
    assert "Brand B personality" not in prompt_a
    assert "Product A data" in prompt_a
    assert "Product B data" not in prompt_a

    assert "Brand B personality" in prompt_b
    assert "Brand A personality" not in prompt_b
    assert "Product B data" in prompt_b
    assert "Product A data" not in prompt_b

    # Resource isolation: each prompt contains only its own resource content
    assert "Resource A content" in prompt_a, "resource A missing from prompt A"
    assert "Resource B content" not in prompt_a, "resource B leaked into prompt A"
    assert "Resource B content" in prompt_b, "resource B missing from prompt B"
    assert "Resource A content" not in prompt_b, "resource A leaked into prompt B"

    # Product/resource refs do not cross runs
    assert "G3-Brand-A" in ctx_a.product_refs[0]
    assert "G3-Brand-B" in ctx_b.product_refs[0]
    assert "G3-Brand-B" not in ctx_a.product_refs
    assert "G3-Brand-A" not in ctx_b.product_refs
    assert ref_a in ctx_a.resource_refs
    assert ref_b in ctx_b.resource_refs
    assert ref_b not in ctx_a.resource_refs
    assert ref_a not in ctx_b.resource_refs


# ---------------------------------------------------------------------------
# E. Quick Brief — reaches production prompt
#
# Agent Settings (preset, custom instructions, rules_must) propagation is
# covered by existing regression tests in tests/test_ui_to_agent_flow.py:
#   - test_save_instructions_then_orchestrator_reads_new_value
#   - test_quick_brief_from_ui_reaches_agent_run
#   - test_preset_change_in_ui_affects_agent_output
# Those tests prove: settings are read into agent config/instruction via
# _load_agent_instructions → _make_agent → _format_instructions, captured
# model prompt contains user-set values, and config is restored after test.
# G3 does not duplicate that coverage.  This test covers Quick Brief only,
# which is the per-run instruction that flows through build_step_run_context.
# ---------------------------------------------------------------------------


def test_quick_brief_reaches_production_prompt(store, isolated_project, tmp_path):
    """Quick Brief flows through build_step_run_context into the real agent
    prompt at the model boundary.  This test does NOT claim model quality —
    it proves the production path delivers the per-run instruction to the
    model boundary and preserves the output contract."""
    raw = "Widget Pro\ntype: gadget\nprice: $50"
    _write_product(isolated_project, "G3-Brief", raw, "")

    brand = _make_brand_dir(tmp_path, "brand_brief", "brief test")
    brief = "เขียนแค่ 1 ย่อหน้า เน้นราคา ไม่เกิน 3 บรรทัด"
    ctx = _build_ctx(store, ["product:G3-Brief"], quick_brief=brief, brand_dir=str(brand))
    assert ctx.quick_brief == brief

    orch = Orchestrator(brand_dir=str(brand), product_id="G3-Brief")
    fake = _FakeLLM(output="## สเปค\n\nWidget Pro ราคา $50 — สรุปสั้น")
    result = orch.run_product_spec(raw, llm=fake, step_context=ctx)

    # Quick Brief reached the prompt sent to the model boundary
    prompt_text = str(fake.calls[0]["messages"])
    assert brief in prompt_text
    assert "คำสั่งเฉพาะรอบนี้จากผู้ใช้" in prompt_text

    # Output contract preserved (non-empty result returned)
    assert result.strip()
