"""Tests for the metadata_summary structured-output fix.

Proves:
- response_format is actually passed to llm.chat
- schema is strict and contains summary/category/derived_facts
- valid structured response persists derived_facts
- malformed/unexpected response follows controlled fallback path
- failure is observable (not silently swallowed)
- LLM call count remains exactly unchanged (1 call per ingestion)
- existing Product Information/Product Facts tests remain green

Uses FakeLLM/deterministic fixtures — no paid/live calls.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class _FakeLLM:
    """Deterministic LLM double.

    Captures every call's kwargs so tests can assert response_format was
    passed.  Returns either valid JSON (default) or a configurable
    malformed response to exercise the fallback path.
    """

    def __init__(self, *, response: str | None = None,
                 derived_facts: dict | None = None,
                 summary: str = "Test summary",
                 category: str = "Test Category"):
        self._response = response  # if set, return this verbatim (for malformed tests)
        self._derived = derived_facts or {}
        self._summary = summary
        self._category = category
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self._response is not None:
            return self._response
        return json.dumps({
            "summary": self._summary,
            "category": self._category,
            "derived_facts": self._derived,
        }, ensure_ascii=False)

    def close(self):
        pass

    def abort(self):
        pass


@pytest.fixture
def _ws(tmp_path, monkeypatch):
    from src.workspace_context import WorkspaceContext, set_workspace, reset_workspace
    from src.brand_registry import BrandRegistry

    monkeypatch.setattr("src.local_workspace._archive_root",
                        lambda: tmp_path / ".recovery_archive")
    reg = BrandRegistry(user_id="test_user", project_root=tmp_path)
    brand = reg.create("TestBrand")
    ws = WorkspaceContext.for_brand("test_user", brand["brand_id"], tmp_path)
    token = set_workspace(ws)
    brand_root = tmp_path / "users" / "test_user" / "brands" / brand["brand_id"]
    for d in ("data", "cache", "output", "brand"):
        (brand_root / d).mkdir(parents=True, exist_ok=True)
    yield {"root": brand_root, "ws": ws}
    set_workspace(None)
    reset_workspace(token)


def _ingest_txt(_ws, product_id, text, llm=None, filename="spec.txt"):
    import src.openrouter_gateway as gateway
    from src import ingestion

    data_dir = _ws["root"] / "data" / product_id
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / filename).write_text(text, encoding="utf-8")

    old_key = gateway.get_api_key
    old_env = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        if llm is not None:
            gateway.get_api_key = lambda: "fake-key"
            # explicit-llm seam — normal import never constructs a client
            ingestion.ingest_product(product_id, force=True, llm=llm)
        else:
            gateway.get_api_key = lambda: ""
            ingestion.ingest_product(product_id, force=True)
    finally:
        gateway.get_api_key = old_key
        if old_env is not None:
            os.environ["OPENROUTER_API_KEY"] = old_env


# ===========================================================================
# 1. response_format is actually passed to llm.chat
# ===========================================================================

def test_response_format_passed_to_llm_chat(_ws):
    """The metadata_summary LLM call must pass response_format (not prompt-only)."""
    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "RespFmt", "spec text", llm=fake_llm)

    # Find the metadata_summary call (not the positioning call)
    summary_calls = [c for c in fake_llm.calls
                     if c["kwargs"].get("source") == "ingestion.metadata_summary"]
    assert len(summary_calls) == 1, f"Expected 1 metadata_summary call, got {len(summary_calls)}"
    kwargs = summary_calls[0]["kwargs"]
    assert "response_format" in kwargs, "response_format must be passed to llm.chat"
    rf = kwargs["response_format"]
    assert isinstance(rf, dict), "response_format must be a dict"
    assert rf.get("type") == "json_schema", "response_format type must be json_schema"


def test_schema_is_strict_and_contains_required_fields(_ws):
    """The response_format schema must be strict and contain
    summary, category, derived_facts."""
    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "SchemaTest", "spec text", llm=fake_llm)

    summary_calls = [c for c in fake_llm.calls
                     if c["kwargs"].get("source") == "ingestion.metadata_summary"]
    rf = summary_calls[0]["kwargs"]["response_format"]
    js = rf.get("json_schema", {})
    assert js.get("strict") is True, "schema must be strict"
    schema = js.get("schema", {})
    assert schema.get("type") == "object"
    props = schema.get("properties", {})
    assert "summary" in props, "schema must include summary"
    assert "category" in props, "schema must include category"
    assert "derived_facts" in props, "schema must include derived_facts"
    # derived_facts must be an object with label/value per fact
    df_schema = props["derived_facts"]
    assert df_schema.get("type") == "object"
    # additionalProperties must define the per-fact shape
    ap = df_schema.get("additionalProperties")
    assert isinstance(ap, dict), "derived_facts must define additionalProperties"
    ap_props = ap.get("properties", {})
    assert "label" in ap_props, "each derived fact must have label"
    assert "value" in ap_props, "each derived fact must have value"
    # required must include all three
    required = schema.get("required", [])
    assert "summary" in required
    assert "category" in required
    assert "derived_facts" in required


# ===========================================================================
# 2. Valid structured response persists derived_facts
# ===========================================================================

def test_valid_structured_response_persists_derived_facts(_ws):
    """When the LLM returns valid JSON via response_format, derived_facts
    must be persisted into product.json."""
    from src import product_db

    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
        "battery_capacity": {"label": "แบตเตอรี่", "value": "800 mAh"},
    })
    _ingest_txt(_ws, "ValidPersist", "spec text", llm=fake_llm)

    record = product_db.load("ValidPersist")
    assert "derived_facts" in record, "derived_facts must be persisted"
    assert record["derived_facts"]["price"]["value"] == "3,990 บาท"
    assert record["derived_facts"]["battery_capacity"]["value"] == "800 mAh"
    # summary must be the LLM-generated summary, not raw_text fallback
    assert record["metadata"]["summary"] == "Test summary"
    assert record["metadata"]["category"] == "Test Category"


# ===========================================================================
# 3. Malformed/unexpected response follows controlled fallback path
# ===========================================================================

def test_malformed_response_uses_controlled_fallback(_ws):
    """When the LLM returns malformed JSON (even with response_format, some
    models may still fail), the ingestion must not crash — it must use the
    controlled fallback (raw_text preview as summary, no derived_facts)."""
    from src import product_db

    fake_llm = _FakeLLM(response="This is not JSON at all, just plain text.")
    _ingest_txt(_ws, "Malformed", "spec text content here", llm=fake_llm)

    record = product_db.load("Malformed")
    # Must not crash; status should still be ready (positioning may also fail)
    assert record["status"] in ("ready", "no_usable_data")
    # derived_facts must be absent (not partially populated)
    assert "derived_facts" not in record or not record["derived_facts"]
    # summary must be the raw_text fallback, not the malformed text
    assert "spec text" in record["metadata"]["summary"]


def test_empty_derived_facts_in_response_is_safe(_ws):
    """When the LLM returns valid JSON but derived_facts is empty {}."""
    from src import product_db

    fake_llm = _FakeLLM(derived_facts={})
    _ingest_txt(_ws, "EmptyFacts", "spec text", llm=fake_llm)

    record = product_db.load("EmptyFacts")
    # derived_facts should be absent or empty (not stored if empty)
    assert "derived_facts" not in record or not record["derived_facts"]
    # summary should still be the LLM summary
    assert record["metadata"]["summary"] == "Test summary"


# ===========================================================================
# 4. Failure is observable (not silently swallowed)
# ===========================================================================

def test_failure_is_observable_via_log(_ws, caplog):
    """When structured-output parsing fails, a warning must be logged
    with source='ingestion.metadata_summary' so the failure is observable."""
    import logging

    fake_llm = _FakeLLM(response="not json")
    with caplog.at_level(logging.WARNING, logger="ingestion"):
        _ingest_txt(_ws, "ObservableFail", "spec text content", llm=fake_llm)

    # At least one warning record must mention metadata_summary and failure
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) >= 1, "Structured-output failure must be logged as a warning"
    # The warning must mention the source or the failure
    found = False
    for r in warnings:
        msg = r.getMessage()
        if "metadata_summary" in msg or "derived_facts" in msg or "structured" in msg.lower():
            found = True
            break
    assert found, f"Warning must mention metadata_summary/derived_facts/structured, got: {[r.getMessage() for r in warnings]}"


# ===========================================================================
# 5. LLM call count remains exactly unchanged
# ===========================================================================

def test_llm_call_count_unchanged(_ws):
    """The fix must NOT add a new LLM call — still 1 metadata_summary call."""
    fake_llm = _FakeLLM(derived_facts={
        "price": {"label": "ราคา", "value": "3,990 บาท"},
    })
    _ingest_txt(_ws, "CallCount", "spec text", llm=fake_llm)

    summary_calls = [c for c in fake_llm.calls
                     if c["kwargs"].get("source") == "ingestion.metadata_summary"]
    assert len(summary_calls) == 1, \
        f"Must be exactly 1 metadata_summary call, got {len(summary_calls)}"
