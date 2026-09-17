"""FREE-IMPORT-AI-PROPOSAL-01 — HARD cost-boundary qualification.

Provider/model invocation count must be exactly ZERO for every
import/edit/view path.  The FIRST allowed invocation may only come from
the explicit เริ่มใช้ AI confirmation → POST /api/product_ai/{folder}/enrich.

Instrumentation:
  * provider choke point — every paid call funnels through
    src.openrouter_gateway.{chat_post, chat_stream_open, image_post,
    video_generate, embeddings_post}; each is wrapped with a counter.
  * construction seam — src.ingestion._make_llm is the single LLM-client
    construction point the app uses; free paths must never call it.  For
    the AI-action test it returns a deterministic FakeLLM (no network).
"""
from __future__ import annotations

import io
import json
import time

import pytest

import src.ingestion as ingestion
import src.openrouter_gateway as gateway
import src.url_import as url_import
import web_viewer
from tests.conftest import make_brand_client


_PROVIDER_FUNCS = (
    "chat_post", "chat_stream_open", "image_post",
    "video_generate", "embeddings_post",
)


@pytest.fixture
def cost(monkeypatch, tmp_path):
    """Count every provider invocation + every LLM construction."""
    calls = {"provider": 0, "make_llm": 0, "llm_chat": []}

    def _count(name):
        def _wrapped(*a, **kw):
            calls["provider"] += 1
            raise RuntimeError(f"provider call blocked by cost boundary ({name})")
        return _wrapped

    for fn in _PROVIDER_FUNCS:
        monkeypatch.setattr(gateway, fn, _count(fn))

    class _CountingFakeLLM:
        def chat(self, messages, *, source="unknown", **kw):
            calls["llm_chat"].append(source)
            if source == "ingestion.metadata_summary":
                return json.dumps({
                    "summary": "AI summary",
                    "category": "AI Cat",
                    "derived_facts": {"battery": {"label": "แบตเตอรี่", "value": "750mAh"}},
                })
            if source == "voice_learner.analyze_product_positioning":
                return json.dumps({"price_tier": "mid"})
            return "{}"

        def close(self):
            pass

    monkeypatch.setattr(ingestion, "_make_llm", lambda: (
        calls.__setitem__("make_llm", calls["make_llm"] + 1) or _CountingFakeLLM()
    ))

    client, uid, bid, brand_root = make_brand_client(
        web_viewer.app, tmp_path, monkeypatch)
    calls.update({
        "client": client, "brand_root": brand_root,
        "user_id": uid, "brand_id": bid,
    })
    return calls


def _create_product_via_staging(cost, name="Widget"):
    """Deterministic staged commit — the only free product-creation path."""
    client = cost["client"]
    files = [("files", ("spec.txt", io.BytesIO(b"Battery 770mAh spec text")))]
    r = client.post("/api/upload_stage", files=files)
    assert r.status_code == 200, r.text
    batch_id = r.json()["batch_id"]
    r = client.post(f"/api/stage/{batch_id}/commit", json={
        "choices": [{"segment_index": 0, "action": "create", "name": name}],
    })
    assert r.status_code == 200, r.text
    return name


def _url_result(page_class="single", **kw):
    base = {
        "original_url": "https://shop.example/p/k1",
        "final_url": "https://shop.example/p/k1",
        "canonical_url": "",
        "fetched_via": "static",
        "page_title": "K1 Watch",
        "og_title": "K1 Watch",
        "text": "K1 kids watch Battery 770mAh " + "x" * 300,
        "images": [],
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "page_class": page_class,
        "page_signals": {"product_count": 1, "multi_signals": []},
    }
    base.update(kw)
    return base


class TestFreePaths:
    def test_upload_stage_and_commit_zero_calls(self, cost):
        name = _create_product_via_staging(cost)
        assert cost["provider"] == 0 and cost["make_llm"] == 0
        # Product materialized + ready (deterministic, no enrichment needed)
        r = cost["client"].get(f"/api/product_info/{name}")
        assert r.status_code == 200
        rec = json.loads(
            (cost["brand_root"] / "cache" / name / "product.json").read_text())
        assert rec["status"] == "ready", rec["status"]

    def test_url_import_single_zero_calls(self, cost, monkeypatch):
        monkeypatch.setattr(url_import, "fetch_product_page",
                            lambda url: _url_result("single"))
        r = cost["client"].post("/api/product_from_url",
                                json={"url": "https://shop.example/p/k1"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["staged"] is True
        assert cost["provider"] == 0 and cost["make_llm"] == 0
        # Commit the staged single product — still zero calls
        r = cost["client"].post(f"/api/stage/{data['batch_id']}/commit", json={
            "choices": [{"segment_index": 0, "action": "create", "name": "K1 URL"}],
        })
        assert r.status_code == 200
        assert cost["provider"] == 0 and cost["make_llm"] == 0

    def test_url_import_multi_rejected_zero_calls_zero_products(self, cost, monkeypatch):
        monkeypatch.setattr(url_import, "fetch_product_page",
                            lambda url: _url_result("multi"))
        r = cost["client"].post("/api/product_from_url",
                                json={"url": "https://shop.example/catalog"})
        assert r.status_code == 400
        assert r.json().get("multi_product") is True
        assert cost["provider"] == 0 and cost["make_llm"] == 0
        # Zero products created — no staging residue materialized
        created = [p.name for p in (cost["brand_root"] / "data").iterdir()
                   if p.is_dir() and not p.name.startswith(".")]
        assert created == []

    def test_url_import_ambiguous_rejected_zero_calls(self, cost, monkeypatch):
        monkeypatch.setattr(url_import, "fetch_product_page",
                            lambda url: _url_result("ambiguous"))
        r = cost["client"].post("/api/product_from_url",
                                json={"url": "https://shop.example/maybe"})
        assert r.status_code == 400
        assert r.json().get("ambiguous_product") is True
        assert cost["provider"] == 0 and cost["make_llm"] == 0

    def test_existing_product_source_upload_zero_calls(self, cost):
        name = _create_product_via_staging(cost)
        files = [("files", ("extra.txt", io.BytesIO(b"more spec text")))]
        r = cost["client"].post("/api/upload",
                                data={"product_name": name}, files=files)
        assert r.status_code == 200, r.text
        # Background ingest is deterministic — wait for it to settle
        for _ in range(50):
            r = cost["client"].get(f"/api/ingest_status/{name}")
            if r.json().get("status") != "processing":
                break
            time.sleep(0.1)
        assert cost["provider"] == 0 and cost["make_llm"] == 0

    def test_detail_view_and_manual_edit_zero_calls(self, cost):
        name = _create_product_via_staging(cost)
        for path in (f"/api/product_info/{name}",
                     f"/api/product_profile/{name}",
                     f"/api/folder_files/{name}",
                     f"/api/product_ai/{name}/proposal"):
            r = cost["client"].get(path)
            assert r.status_code == 200, path
        # Manual Product Information save (facts → canonical manual layer)
        r = cost["client"].post(f"/api/product_profile_save/{name}", json={
            "facts": {"battery": "770mAh"},
        })
        assert r.status_code == 200
        # Manual Marketing save
        r = cost["client"].post(f"/api/product_profile_save/{name}", json={
            "price_tier": "entry", "tone_adjustment": "สดใส",
        })
        assert r.status_code == 200
        assert cost["provider"] == 0 and cost["make_llm"] == 0

    def test_proposal_view_zero_calls(self, cost):
        name = _create_product_via_staging(cost)
        r = cost["client"].get(f"/api/product_ai/{name}/proposal")
        assert r.status_code == 200
        assert r.json()["proposal"] is None
        assert cost["provider"] == 0 and cost["make_llm"] == 0


class TestExplicitAIBoundary:
    def test_enrich_is_first_allowed_invocation(self, cost):
        """Everything before เริ่มใช้ AI must be free; the confirm POST is
        the first and only model invocation."""
        name = _create_product_via_staging(cost)
        assert cost["provider"] == 0 and cost["make_llm"] == 0

        r = cost["client"].post(f"/api/product_ai/{name}/enrich",
                                json={"scopes": ["facts"]})
        assert r.status_code == 200, r.text
        # THE boundary: construction happened here, not before
        assert cost["make_llm"] == 1
        assert "ingestion.metadata_summary" in cost["llm_chat"]
        # Fake LLM means no real provider call — but the seam fired exactly once
        assert cost["provider"] == 0

        proposal = r.json()["proposal"]
        assert proposal["proposal_id"]

        # Resolving is canonical write only — no new model invocation
        r = cost["client"].post(f"/api/product_ai/{name}/resolve", json={
            "proposal_id": proposal["proposal_id"],
            "resolutions": [{"kind": "fact", "key": "battery", "action": "accept"}],
        })
        assert r.status_code == 200
        assert cost["make_llm"] == 1  # unchanged

    def test_enrich_pending_conflict_requires_replace(self, cost):
        name = _create_product_via_staging(cost)
        r = cost["client"].post(f"/api/product_ai/{name}/enrich",
                                json={"scopes": ["facts"]})
        assert r.status_code == 200
        # Second run while pending → 409 has_pending (no silent merge)
        r = cost["client"].post(f"/api/product_ai/{name}/enrich",
                                json={"scopes": ["facts"]})
        assert r.status_code == 409
        assert r.json()["has_pending"] is True
        # Explicit replace → new proposal
        r = cost["client"].post(f"/api/product_ai/{name}/enrich",
                                json={"scopes": ["facts"], "replace": True})
        assert r.status_code == 200

    def test_enrich_requires_ownership(self, cost):
        r = cost["client"].post("/api/product_ai/NotAProduct/enrich",
                                json={"scopes": ["facts"]})
        assert r.status_code == 404
        r = cost["client"].post("/api/product_ai/..%2Fescape/enrich",
                                json={"scopes": ["facts"]})
        assert r.status_code in (400, 404)
        assert cost["make_llm"] == 0

    def test_enrich_empty_scopes_is_400_never_spend(self, cost):
        """An explicit empty scope selection must NOT default into a paid
        call — the user's choice of 'nothing' is honored as 400."""
        name = _create_product_via_staging(cost)
        r = cost["client"].post(f"/api/product_ai/{name}/enrich",
                                json={"scopes": []})
        assert r.status_code == 400, r.text
        assert cost["make_llm"] == 0 and cost["provider"] == 0

    def test_discard_wrong_proposal_id_is_409(self, cost):
        name = _create_product_via_staging(cost)
        r = cost["client"].post(f"/api/product_ai/{name}/enrich",
                                json={"scopes": ["facts"]})
        assert r.status_code == 200
        r = cost["client"].post(f"/api/product_ai/{name}/resolve", json={
            "proposal_id": "deadbeef", "discard": True})
        assert r.status_code == 409, r.text
        # the real proposal survives the stale discard
        r = cost["client"].get(f"/api/product_ai/{name}/proposal")
        assert r.json()["proposal"] is not None

    def test_profile_suggest_endpoint_removed(self, cost):
        """The old direct-AI-into-form path bypassed the proposal boundary —
        it is gone; AI for product fields is proposal-only now."""
        name = _create_product_via_staging(cost)
        r = cost["client"].post(f"/api/product_profile_suggest/{name}")
        assert r.status_code == 404
        assert cost["make_llm"] == 0 and cost["provider"] == 0
