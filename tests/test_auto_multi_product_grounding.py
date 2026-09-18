"""Auto-mode multi-product grounding contract.

Runtime contract for 2+ selected products in ``run_content_creator_auto``:

- every selected product identity reaches the agent runtime (canonical
  ``"A + B"`` combined binding — the same seam the manual flow uses via
  ``_run_single_agent → orch.bind_product("A + B")``);
- every selected product's images reach the multimodal request;
- provenance/source URLs stay associated with the correct products;
- the grounding scope (``_content_source_context``) covers every selected
  product, not only the first;
- no stale product data survives a subsequent selection;
- Manual and Auto multi-product expose equivalent grounding inputs.

The test exercises the real ``run_content_creator_auto`` +
``_run_content_creator_raw`` path with real product_db records under an
isolated brand workspace — only the LLM boundary and persistence side
effects are stubbed.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from src import product_db
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Capture agent — real _make_agent constructs it, real run() kwargs recorded
# ---------------------------------------------------------------------------

class _CaptureAgent:
    """Stand-in for ContentCreatorAgent — records the real inputs the
    production seam hands to the agent (build_prompt product_data and the
    run() image_paths / quick_brief kwargs)."""

    instances: list = []

    def __init__(self, cfg, llm, **kwargs):
        self.config = cfg
        self.llm = llm
        self.captured: dict = {}
        _CaptureAgent.instances.append(self)

    def build_prompt(self, product_data, *args, **kwargs):
        self.captured["product_data"] = product_data
        return "PROMPT"

    def run(self, prompt, **kwargs):
        self.captured["run_kwargs"] = kwargs
        return json.dumps({"posts": [{
            "platform": "facebook", "concept": "c", "title": "T",
            "caption": "cap", "script": "s", "hashtags": "#h",
            "image_prompts": [], "video_prompts": [], "asset_ids": [],
        }]})


def _seed_product(pid: str, marker: str, img_path=None, source_url: str = ""):
    """Create a real ready product record in the active brand workspace."""
    rec = product_db.load(pid)
    rec["status"] = product_db.STATUS_READY
    rec["raw_text"] = f"PRODUCT-DATA::{marker}:: unique product payload"
    if img_path is not None:
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + marker.encode())
        rec["image_descriptions"] = [
            {"file": img_path.name, "path": str(img_path), "description": f"img {marker}"}
        ]
    if source_url:
        rec["source_import"] = {
            "original_url": source_url,
            "final_url": source_url,
            "canonical_url": source_url,
        }
    product_db.save(pid, rec)


def _make_orch():
    orch = Orchestrator()
    orch.brand_dir = "brand"
    return orch


def _stub_auto(orch, pids):
    orch.select_product_auto = MagicMock(return_value={
        "product_ids": list(pids), "concept": "c", "pillar": "p",
        "reason": "r", "asset_ids": [],
    })
    orch._select_assets_for_content = MagicMock(return_value="")
    orch._build_configured_pillars_text = MagicMock(return_value="")
    orch._review_script_in_posts = MagicMock(return_value={})
    orch._finalize_content_output = MagicMock(
        side_effect=lambda posts, pre, llm, ctx, cb=None: (
            json.dumps({"posts": posts}), "md"))


def _run_auto(orch, pids, llm):
    _stub_auto(orch, pids)
    with patch("src.orchestrator.ContentCreatorAgent", _CaptureAgent), \
         patch("src.content_history.check_duplicate",
               return_value={"is_duplicate": False}), \
         patch("src.content_history.record_entry", return_value=None):
        return orch.run_content_creator_auto(
            llm=llm, product_count=len(pids), platforms=["facebook"])


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------

def test_auto_multi_product_binds_every_selected_product(brand_ws):
    """2+ selected products → canonical "A + B" binding: identities, data,
    images, provenance and grounding scope all cover BOTH products —
    the same grounding inputs the manual multi-product flow produces."""
    img_a = brand_ws["brand_root"] / "cache" / "ProdA" / "a.png"
    img_b = brand_ws["brand_root"] / "cache" / "ProdB" / "b.png"
    img_a.parent.mkdir(parents=True, exist_ok=True)
    img_b.parent.mkdir(parents=True, exist_ok=True)
    url_a = "https://shop.example/products/aaa"
    url_b = "https://other.example/items/bbb"
    _seed_product("ProdA", "AAA", img_path=img_a, source_url=url_a)
    _seed_product("ProdB", "BBB", img_path=img_b, source_url=url_b)

    _CaptureAgent.instances.clear()
    orch = _make_orch()
    result = _run_auto(orch, ["ProdA", "ProdB"], llm=MagicMock())

    assert "error" not in result, f"auto run failed: {result.get('error')}"
    agent = _CaptureAgent.instances[-1]

    # 1. Canonical combined binding — identical to manual "A + B".
    assert orch.product_id == "ProdA + ProdB"

    # 2. Every product's data reaches the generation input.
    product_data = agent.captured["product_data"]
    assert "PRODUCT-DATA::AAA::" in product_data
    assert "PRODUCT-DATA::BBB::" in product_data

    # 3. Every product's images reach the multimodal request.
    image_paths = agent.captured["run_kwargs"].get("image_paths") or []
    assert str(img_a) in image_paths
    assert str(img_b) in image_paths

    # 4. Provenance stays associated with the correct products — the agent's
    #    authorized citation set covers both products' source URLs.
    assert url_a in agent._product_source_urls
    assert url_b in agent._product_source_urls

    # 5. Grounding scope covers every selected product.
    assert "PRODUCT-DATA::AAA::" in orch._content_source_context
    assert "PRODUCT-DATA::BBB::" in orch._content_source_context


def test_auto_multi_product_matches_manual_grounding_inputs(brand_ws):
    """Manual (bind_product "A + B") and Auto must expose equivalent
    grounding inputs — same product data, image paths, and provenance set."""
    img_a = brand_ws["brand_root"] / "cache" / "ProdA" / "a.png"
    img_b = brand_ws["brand_root"] / "cache" / "ProdB" / "b.png"
    img_a.parent.mkdir(parents=True, exist_ok=True)
    img_b.parent.mkdir(parents=True, exist_ok=True)
    url_a = "https://a.example/x"
    url_b = "https://b.example/y"
    _seed_product("ProdA", "AAA", img_path=img_a, source_url=url_a)
    _seed_product("ProdB", "BBB", img_path=img_b, source_url=url_b)

    # Manual seam: the exact call _run_single_agent makes for multi-product.
    manual = _make_orch()
    manual.bind_product("ProdA + ProdB")
    manual_data = manual._get_product_data("")
    manual_images = manual._get_product_image_paths()
    manual_urls = manual._selected_product_source_urls()

    _CaptureAgent.instances.clear()
    auto = _make_orch()
    _run_auto(auto, ["ProdA", "ProdB"], llm=MagicMock())

    assert auto._get_product_data("") == manual_data
    assert auto._get_product_image_paths() == manual_images
    assert auto._selected_product_source_urls() == manual_urls


def test_auto_reselection_leaves_no_stale_product_data(brand_ws):
    """A second auto run selecting a different set must not leak the previous
    selection's data, images, or provenance."""
    img_a = brand_ws["brand_root"] / "cache" / "ProdA" / "a.png"
    img_b = brand_ws["brand_root"] / "cache" / "ProdB" / "b.png"
    img_c = brand_ws["brand_root"] / "cache" / "ProdC" / "c.png"
    for p in (img_a, img_b, img_c):
        p.parent.mkdir(parents=True, exist_ok=True)
    _seed_product("ProdA", "AAA", img_path=img_a, source_url="https://a.example/1")
    _seed_product("ProdB", "BBB", img_path=img_b, source_url="https://b.example/2")
    _seed_product("ProdC", "CCC", img_path=img_c, source_url="https://c.example/3")

    _CaptureAgent.instances.clear()
    orch = _make_orch()
    _run_auto(orch, ["ProdA", "ProdB"], llm=MagicMock())
    _run_auto(orch, ["ProdC"], llm=MagicMock())

    agent = _CaptureAgent.instances[-1]
    product_data = agent.captured["product_data"]
    assert "PRODUCT-DATA::CCC::" in product_data
    assert "PRODUCT-DATA::AAA::" not in product_data
    assert "PRODUCT-DATA::BBB::" not in product_data

    image_paths = agent.captured["run_kwargs"].get("image_paths") or []
    assert str(img_c) in image_paths
    assert str(img_a) not in image_paths
    assert str(img_b) not in image_paths

    assert agent._product_source_urls == {"https://c.example/3"}


def test_auto_single_product_uses_canonical_binding(brand_ws):
    """Single-product auto must bind the same way as manual single-product —
    product identity (not a combined label) with product profile refresh."""
    _seed_product("ProdA", "AAA")
    orch = _make_orch()
    _run_auto(orch, ["ProdA"], llm=MagicMock())
    assert orch.product_id == "ProdA"
