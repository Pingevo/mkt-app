"""Offline provider-flow tests for media_gen — mocked HTTP, no paid calls.

Seam under test:
    generate_image(prompt, output_path, ...) -> {"ok": True, "path": ...}
    generate_video(prompt, output_path, ...) -> {"ok": True, "path": ...}

These prove the full flow from endpoint request to saved file completes for
both image and video generation, using httpx.MockTransport so that real URL
validation still runs (a relative polling URL raises UnsupportedProtocol, the
exact production failure mode).

Root cause covered:
    OpenRouter's video submit response returns ``polling_url`` as a RELATIVE
    path (``/api/v1/videos/<jobId>``). The code polled ``client.get(polling_url)``
    on an httpx Client with no base_url → ``UnsupportedProtocol`` → video gen
    always returned ``ok: False``.
"""
import base64
import json
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _patch_http(monkeypatch, handler):
    """Inject a MockTransport into every httpx.Client so real URL validation
    still runs (relative URLs raise UnsupportedProtocol, matching production),
    while responses come from ``handler``."""
    transport = httpx.MockTransport(handler)
    original_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    return transport


def _stub_media_env(monkeypatch):
    """Stub API key, usage logging, and capabilities so no real network call
    is made and clamping is skipped."""
    from src import media_gen
    from src import openrouter_gateway
    monkeypatch.setattr(media_gen, "_get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr(openrouter_gateway, "get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr(media_gen, "_log_media_usage", lambda *a, **k: None)
    # Gateway owns accounting for image+video — stub the Hub seam too
    monkeypatch.setattr(openrouter_gateway, "record_ai_usage", lambda entry: None)
    monkeypatch.setattr(media_gen, "get_model_capabilities", lambda *a, **k: {})


# ---------------------------------------------------------------------------
# Image generation — endpoint → saved file
# ---------------------------------------------------------------------------

def test_image_generation_flow_saves_file(monkeypatch, tmp_path):
    """Image gen: POST /api/v1/images → b64_json → file written → ok."""
    from src import media_gen

    _stub_media_env(monkeypatch)
    fake_png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-png-bytes").decode()

    def handler(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/images"
        assert request.method == "POST"
        body = request.read()
        import json
        payload = json.loads(body)
        assert payload["model"] == "google/gemini-3.1-flash-image"
        assert payload["prompt"]
        return httpx.Response(200, json={
            "data": [{"b64_json": fake_png, "media_type": "image/png"}],
            "usage": {"cost": 0.04, "prompt_tokens": 0, "completion_tokens": 4175},
        })

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "image_1.png"
    result = media_gen.generate_image("a product photo on a clean background", out_path)

    assert result.get("ok") is True, result
    assert result["path"] == str(out_path)
    assert out_path.exists()
    assert out_path.read_bytes().startswith(b"\x89PNG")


# ---------------------------------------------------------------------------
# Video generation — the relative polling_url root cause
# ---------------------------------------------------------------------------

def test_video_generation_flow_handles_relative_polling_url(monkeypatch, tmp_path):
    """Video gen: submit returns RELATIVE polling_url (per OpenRouter docs) →
    poll → download → file written → ok.

    RED before fix: client.get('/api/v1/videos/job-abc') raises
    UnsupportedProtocol because httpx needs an absolute URL.
    GREEN after fix: polling_url is made absolute before polling.
    """
    from src import media_gen

    _stub_media_env(monkeypatch)

    polled_urls: list[str] = []

    def handler(request):
        url = str(request.url)
        # submit
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            return httpx.Response(200, json={
                "id": "job-abc123",
                # OpenRouter returns a RELATIVE polling_url per the docs.
                "polling_url": "/api/v1/videos/job-abc123",
                "status": "pending",
            })
        # poll — must be the absolute URL built from the relative polling_url
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-abc123":
            polled_urls.append(url)
            return httpx.Response(200, json={
                "id": "job-abc123",
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/video.mp4"],
                "usage": {"cost": 0.16},
            })
        # download the finished video (provider requires auth header on unsigned_urls)
        if request.method == "GET" and url == "https://cdn.example.com/video.mp4":
            assert request.headers.get("authorization") == "Bearer fake-api-key"
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42VIDEOBYTES")
        return httpx.Response(404, text=f"unexpected {request.method} {url}")

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_1.mp4"
    result = media_gen.generate_video(
        "a cinematic 5s product shot", out_path,
        poll_interval=0.0, max_wait=10.0,
    )

    assert result.get("ok") is True, result
    assert result["path"] == str(out_path)
    assert out_path.exists()
    assert out_path.read_bytes().startswith(b"\x00\x00\x00\x18ftypmp42")
    # the poll must have hit the absolute URL, not the relative one
    assert polled_urls, "polling never reached the absolute polling URL"
    assert all(u.startswith("http") for u in polled_urls), polled_urls


def test_video_generation_submit_rejects_missing_job_id(monkeypatch, tmp_path):
    """If the submit response lacks job_id/polling_url, return ok: False with a
    clear error instead of proceeding to poll."""
    from src import media_gen

    _stub_media_env(monkeypatch)

    def handler(request):
        return httpx.Response(200, json={"id": None, "status": "pending"})

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_1.mp4"
    result = media_gen.generate_video("prompt", out_path, poll_interval=0.0, max_wait=5.0)

    assert result.get("ok") is False
    assert "job_id" in result.get("error", "").lower() or "polling_url" in result.get("error", "").lower()


def test_video_generation_propagates_failed_status(monkeypatch, tmp_path):
    """A 'failed' poll status returns ok: False with the provider error."""
    from src import media_gen

    _stub_media_env(monkeypatch)

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            return httpx.Response(200, json={
                "id": "job-fail", "polling_url": "/api/v1/videos/job-fail", "status": "pending",
            })
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-fail":
            return httpx.Response(200, json={"status": "failed", "error": "content policy"})
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_1.mp4"
    result = media_gen.generate_video("prompt", out_path, poll_interval=0.0, max_wait=5.0)

    assert result.get("ok") is False
    assert "content policy" in result.get("error", "")


# ---------------------------------------------------------------------------
# All 3 reference sources + visual suffix → payload["input_references"]
# These tests capture the ACTUAL payload sent to OpenRouter and assert that
# product images, user resources, and brand assets are all merged, converted
# to base64 data URLs, and formatted correctly — for both image and video.
# ---------------------------------------------------------------------------

def _make_real_png(path: Path):
    """Write a minimal valid PNG so _image_to_data_url produces a real data URL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)


def _captured_payload_handler(captured: dict, response_body: dict, url: str):
    """Build a handler that captures the request payload then returns a
    canned response."""
    def handler(request):
        captured["url"] = str(request.url)
        captured["method"] = request.method
        import json
        captured["payload"] = json.loads(request.read())
        return httpx.Response(200, json=response_body)
    return handler


def test_image_payload_merges_all_3_ref_sources_as_data_urls(monkeypatch, tmp_path):
    """Image: product image + user resource + brand asset all appear in
    payload["input_references"] as base64 data URLs."""
    from src import media_gen

    _stub_media_env(monkeypatch)

    # 3 distinct reference sources — real PNG files on disk
    prod_img = tmp_path / "product.png"
    res_img = tmp_path / "resource.png"
    asset_img = tmp_path / "brand_logo.png"
    for p in (prod_img, res_img, asset_img):
        _make_real_png(p)

    # build_input_references merges product + resource + asset paths
    from src import asset_library
    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(asset_img)} if aid == "a_0001" else None)
    refs = asset_library.build_input_references(
        [str(prod_img)], ["a_0001"], resource_paths=[str(res_img)],
    )
    assert str(prod_img) in refs
    assert str(res_img) in refs
    assert str(asset_img) in refs

    fake_png = base64.b64encode(b"\x89PNG\r\n\x1a\nresult").decode()
    captured: dict = {}
    handler = _captured_payload_handler(
        captured, {"data": [{"b64_json": fake_png}]},
        "https://openrouter.ai/api/v1/images",
    )
    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "image_1.png"
    result = media_gen.generate_image(
        "a product photo", out_path, input_references=refs,
    )
    assert result.get("ok") is True, result

    payload = captured["payload"]
    assert payload["model"] == "google/gemini-3.1-flash-image"
    ir = payload.get("input_references")
    assert ir is not None and len(ir) == 3, f"expected 3 refs, got {len(ir) if ir else 0}"
    # Each must be the API format with a base64 data URL
    for ref in ir:
        assert ref["type"] == "image_url"
        url = ref["image_url"]["url"]
        assert url.startswith("data:image/"), f"not a data URL: {url[:30]}"
        assert "base64," in url


def test_image_payload_appends_visual_suffix_to_prompt(monkeypatch, tmp_path):
    """Image: brand visual suffix (keywords/colors/tone) is appended to the
    prompt that reaches the API payload."""
    from src import media_gen

    _stub_media_env(monkeypatch)

    visual = {
        "keywords": ["clean", "minimal", "warm lighting"],
        "colors": {"primary": "#1a73e8"},
        "image_style": {"tone": "bright and airy"},
    }
    fake_png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    captured: dict = {}
    handler = _captured_payload_handler(
        captured, {"data": [{"b64_json": fake_png}]},
        "https://openrouter.ai/api/v1/images",
    )
    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "image_1.png"
    result = media_gen.generate_image("a product photo", out_path, visual=visual)
    assert result.get("ok") is True, result

    sent_prompt = captured["payload"]["prompt"]
    assert "a product photo" in sent_prompt
    assert "clean" in sent_prompt, "visual keywords missing from prompt"
    assert "minimal" in sent_prompt
    assert "#1a73e8" in sent_prompt, "brand color missing from prompt"
    assert "bright and airy" in sent_prompt, "tone missing from prompt"


def test_video_payload_merges_all_3_ref_sources_as_data_urls(monkeypatch, tmp_path):
    """Video: product image + user resource + brand asset all appear in
    payload["input_references"] as base64 data URLs."""
    from src import media_gen

    _stub_media_env(monkeypatch)

    prod_img = tmp_path / "product.png"
    res_img = tmp_path / "resource.png"
    asset_img = tmp_path / "brand_logo.png"
    for p in (prod_img, res_img, asset_img):
        _make_real_png(p)

    from src import asset_library
    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(asset_img)} if aid == "a_0001" else None)
    refs = asset_library.build_input_references(
        [str(prod_img)], ["a_0001"], resource_paths=[str(res_img)],
    )

    captured: dict = {}

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            import json
            captured["payload"] = json.loads(request.read())
            return httpx.Response(200, json={
                "id": "job-abc", "polling_url": "/api/v1/videos/job-abc", "status": "pending",
            })
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-abc":
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
            })
        if request.method == "GET" and url == "https://cdn.example.com/v.mp4":
            assert request.headers.get("authorization") == "Bearer fake-api-key"
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_1.mp4"
    result = media_gen.generate_video(
        "a cinematic product shot", out_path,
        input_references=refs, poll_interval=0.0, max_wait=10.0,
    )
    assert result.get("ok") is True, result

    payload = captured["payload"]
    assert payload["model"] == "bytedance/seedance-2.0-fast"
    ir = payload.get("input_references")
    assert ir is not None and len(ir) == 3, f"expected 3 refs, got {len(ir) if ir else 0}"
    for ref in ir:
        assert ref["type"] == "image_url"
        url = ref["image_url"]["url"]
        assert url.startswith("data:image/"), f"not a data URL: {url[:30]}"
        assert "base64," in url


def test_video_payload_appends_visual_suffix_to_prompt(monkeypatch, tmp_path):
    """Video: brand visual suffix is appended to the prompt that reaches the
    API payload."""
    from src import media_gen

    _stub_media_env(monkeypatch)

    visual = {"keywords": ["cinematic", "soft focus"], "image_style": {"tone": "warm"}}
    captured: dict = {}

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            import json
            captured["payload"] = json.loads(request.read())
            return httpx.Response(200, json={
                "id": "job-v", "polling_url": "/api/v1/videos/job-v", "status": "pending",
            })
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-v":
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
            })
        if request.method == "GET" and url == "https://cdn.example.com/v.mp4":
            assert request.headers.get("authorization") == "Bearer fake-api-key"
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_1.mp4"
    result = media_gen.generate_video(
        "a product shot", out_path, visual=visual,
        poll_interval=0.0, max_wait=10.0,
    )
    assert result.get("ok") is True, result

    sent_prompt = captured["payload"]["prompt"]
    assert "a product shot" in sent_prompt
    assert "cinematic" in sent_prompt, "visual keywords missing"
    assert "soft focus" in sent_prompt
    assert "warm" in sent_prompt, "tone missing"


def test_multi_product_input_references_merge(monkeypatch, tmp_path):
    """Multi-product: product_image_paths from 2 products merge into
    input_references together (the web_viewer splits on ' + ')."""
    from src import media_gen

    _stub_media_env(monkeypatch)

    prod1 = tmp_path / "p1.png"
    prod2 = tmp_path / "p2.png"
    _make_real_png(prod1)
    _make_real_png(prod2)

    # Simulate what web_viewer does for multi-product: extend from both
    product_image_paths = [str(prod1), str(prod2)]
    fake_png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    captured: dict = {}
    handler = _captured_payload_handler(
        captured, {"data": [{"b64_json": fake_png}]},
        "https://openrouter.ai/api/v1/images",
    )
    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "image_1.png"
    result = media_gen.generate_image(
        "multi-product photo", out_path, input_references=product_image_paths,
    )
    assert result.get("ok") is True, result

    ir = captured["payload"]["input_references"]
    assert len(ir) == 2, f"expected 2 multi-product refs, got {len(ir)}"
    for ref in ir:
        assert ref["image_url"]["url"].startswith("data:image/")


# ---------------------------------------------------------------------------
# Multi-reference pipeline: product image (product_db) + person/lifestyle
# asset (asset_ids) + user resource (resource_paths) — through the REAL seams.
# These tests prove that when a user attaches a person/lifestyle photo via
# Quick Brief (resource_paths) or Asset Library (asset_ids), BOTH the product
# image AND the person image are delivered as input_references to the provider.
# ---------------------------------------------------------------------------

def test_build_input_references_combines_product_and_person_asset(monkeypatch, tmp_path):
    """build_input_references combines:
      - product image from product_db.get_product_image_paths (real product)
      - person/lifestyle asset from asset_ids (Asset Library)
      - user resource from resource_paths (Quick Brief attachment)
    All 3 must appear in the returned list, product first.
    """
    from src import asset_library

    prod_img = tmp_path / "product.png"
    person_asset = tmp_path / "person_asset.jpeg"
    user_resource = tmp_path / "user_resource.png"
    for p in (prod_img, person_asset, user_resource):
        _make_real_png(p)

    # Mock get_asset to return the person asset record (path + metadata)
    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(person_asset),
                                     "subject": "person", "tags": ["lifestyle", "child"]}
                        if aid == "a_0006" else None)
    monkeypatch.setattr(asset_library, "_load_config",
                        lambda: {"media": {"max_refs_per_post": 10}})

    refs = asset_library.build_input_references(
        product_paths=[str(prod_img)],
        asset_ids=["a_0006"],  # person asset from Asset Library
        resource_paths=[str(user_resource)],  # Quick Brief attachment
    )

    # Product image must come first (most important — must match the product)
    assert str(prod_img) in refs, "product image missing from input_references"
    assert str(person_asset) in refs, "person/lifestyle asset missing"
    assert str(user_resource) in refs, "user resource missing"
    assert refs[0] == str(prod_img), "product image must be first"


def test_image_payload_delivers_both_product_and_person_to_provider(monkeypatch, tmp_path):
    """End-to-end: product image + person asset → build_input_references →
    generate_image → payload["input_references"] contains BOTH as base64
    data URLs. The person must NOT be discarded."""
    from src import media_gen, asset_library

    _stub_media_env(monkeypatch)

    prod_img = tmp_path / "product.png"
    person_asset = tmp_path / "person_lifestyle.jpeg"
    _make_real_png(prod_img)
    _make_real_png(person_asset)

    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(person_asset)}
                        if aid == "a_0006" else None)
    monkeypatch.setattr(asset_library, "_load_config",
                        lambda: {"media": {"max_refs_per_post": 10}})

    # Simulate the real flow: product_db provides product image,
    # agent selects asset_ids=["a_0006"] (person), user attached a resource
    refs = asset_library.build_input_references(
        product_paths=[str(prod_img)],
        asset_ids=["a_0006"],
        resource_paths=[],
    )
    assert len(refs) == 2, f"expected product + person, got {refs}"

    fake_png = base64.b64encode(b"\x89PNG\r\n\x1a\nresult").decode()
    captured: dict = {}
    handler = _captured_payload_handler(
        captured, {"data": [{"b64_json": fake_png}]},
        "https://openrouter.ai/api/v1/images",
    )
    _patch_http(monkeypatch, handler)

    # The agent's prompt should describe using BOTH the product and the person
    prompt = ("a lifestyle photo of a child wearing a kids smartwatch, "
              "natural lighting, the child is smiling")
    out_path = tmp_path / "image_1.png"
    result = media_gen.generate_image(prompt, out_path, input_references=refs)
    assert result.get("ok") is True, result

    ir = captured["payload"]["input_references"]
    assert len(ir) == 2, f"provider received {len(ir)} refs, expected 2"
    # Both must be base64 data URLs (not file paths)
    for ref in ir:
        assert ref["type"] == "image_url"
        url = ref["image_url"]["url"]
        assert url.startswith("data:image/"), f"not a data URL: {url[:40]}"
    # The prompt must mention both the product (smartwatch) and the person (child)
    sent_prompt = captured["payload"]["prompt"]
    assert "smartwatch" in sent_prompt.lower()
    assert "child" in sent_prompt.lower()


def test_video_payload_delivers_both_product_and_person_to_provider(monkeypatch, tmp_path):
    """End-to-end: product image + person asset → build_input_references →
    generate_video → payload["input_references"] contains BOTH as base64
    data URLs."""
    from src import media_gen, asset_library

    _stub_media_env(monkeypatch)

    prod_img = tmp_path / "product.png"
    person_asset = tmp_path / "person_lifestyle.jpeg"
    _make_real_png(prod_img)
    _make_real_png(person_asset)

    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(person_asset)}
                        if aid == "a_0006" else None)
    monkeypatch.setattr(asset_library, "_load_config",
                        lambda: {"media": {"max_refs_per_post": 10}})

    refs = asset_library.build_input_references(
        product_paths=[str(prod_img)],
        asset_ids=["a_0006"],
        resource_paths=[],
    )
    assert len(refs) == 2

    captured: dict = {}

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            import json
            captured["payload"] = json.loads(request.read())
            return httpx.Response(200, json={
                "id": "job-mp", "polling_url": "/api/v1/videos/job-mp", "status": "pending",
            })
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-mp":
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
            })
        if request.method == "GET" and url == "https://cdn.example.com/v.mp4":
            assert request.headers.get("authorization") == "Bearer fake-api-key"
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    prompt = ("a cinematic 4-second lifestyle video of a child wearing "
              "a kids smartwatch, smiling, natural daylight")
    out_path = tmp_path / "video_1.mp4"
    result = media_gen.generate_video(
        prompt, out_path, input_references=refs,
        duration=4, poll_interval=0.0, max_wait=10.0,
    )
    assert result.get("ok") is True, result

    ir = captured["payload"]["input_references"]
    assert len(ir) == 2, f"provider received {len(ir)} refs, expected 2"
    for ref in ir:
        assert ref["type"] == "image_url"
        assert ref["image_url"]["url"].startswith("data:image/")
    sent_prompt = captured["payload"]["prompt"]
    assert "smartwatch" in sent_prompt.lower()
    assert "child" in sent_prompt.lower()


def test_agent4_prompt_includes_asset_summary_with_person_subject(monkeypatch):
    """Agent 4's prompt receives asset_summary that includes the person
    asset's subject and description — so the agent knows to write prompts
    that incorporate the person, not discard them."""
    from src.agents.content_creator import ContentCreatorAgent

    agent = ContentCreatorAgent.__new__(ContentCreatorAgent)

    asset_summary = (
        "เหตุผลที่เลือก: lifestyle shot ของเด็กสวมนาฬิกา\n"
        "- ID: a_0006 | images-2.jpeg | type: image | subject: person | "
        "tags: lifestyle, child | คำบรรยาย: เด็กสวมนาฬิกาอยู่ในชีวิตจริง"
    )

    prompt = agent.build_prompt(
        product_spec="สินค้า: Lagenio K5 สมาร์ทวอทช์สำหรับเด็ก",
        competitor_analysis="",
        campaign_strategy="",
        asset_summary=asset_summary,
    )

    # The agent must see the person asset's subject and description
    assert "a_0006" in prompt, "asset ID missing from agent prompt"
    assert "person" in prompt, "person subject missing — agent won't know to use the person"
    assert "เด็กสวมนาฬิกา" in prompt, "person description missing"
    assert "ระบุ asset_ids" in prompt, "agent not instructed to specify asset_ids"


def test_agent4_prompt_receives_numbered_reference_catalog_and_generic_instruction(tmp_path):
    """Agent 4's prompt must receive the ordered reference catalog (numbered
    Reference 1/2/3) plus one generic instruction to reason about which refs
    matter and write the provider prompt identifying refs by number.

    No category hardcoding — the catalog carries only mechanical fields and
    metadata pass-through; the model decides meaning.
    """
    from src.agents.content_creator import ContentCreatorAgent

    prod = tmp_path / "prod.png"
    res = tmp_path / "res.png"
    asset = tmp_path / "asset.png"
    for p in (prod, res, asset):
        _make_real_png(p)

    catalog = [
        {"ordinal": 1, "label": "Reference 1", "provenance": "product_db",
         "path": str(prod), "asset_id": "", "filename": "prod.png", "metadata": {}},
        {"ordinal": 2, "label": "Reference 2", "provenance": "user_resource",
         "path": str(res), "asset_id": "", "filename": "res.png", "metadata": {}},
        {"ordinal": 3, "label": "Reference 3", "provenance": "asset_library",
         "path": str(asset), "asset_id": "a_0001", "filename": "asset.png",
         "metadata": {"subject": "logo", "tags": ["brand"]}},
    ]

    agent = ContentCreatorAgent.__new__(ContentCreatorAgent)
    prompt = agent.build_prompt(
        product_spec="สินค้า: K5 สมาร์ทวอทช์",
        competitor_analysis="", campaign_strategy="",
        reference_catalog=catalog,
    )

    # Agent 4 sees the numbered references in order
    assert "Reference 1" in prompt
    assert "Reference 2" in prompt
    assert "Reference 3" in prompt
    # Provenance + filename visible for traceability
    assert "product_db" in prompt
    assert "user_resource" in prompt
    assert "asset_library" in prompt
    assert "prod.png" in prompt and "res.png" in prompt and "asset.png" in prompt
    # Asset metadata passed through (not interpreted by code)
    assert "logo" in prompt and "brand" in prompt
    # Generic instruction present (model reasons, code doesn't branch on category)
    assert "Reference" in prompt  # instruction tells agent to identify refs by number
    # No semantic fidelity/required/composition labels leaked into the prompt
    for forbidden in ("identity_preservation", "semantic_guidance", "style_only",
                      "[required]", "fidelity"):
        assert forbidden not in prompt, \
            f"semantic field '{forbidden}' must not leak into agent prompt"


def test_provider_receives_references_in_same_order_as_catalog(tmp_path):
    """The order shown to Agent 4 (catalog) must exactly match the order sent
    to the provider (input_references). build_input_references must preserve
    the catalog order — product → user_resource → asset_library."""
    from src import asset_library

    prod = tmp_path / "prod.png"
    res = tmp_path / "res.png"
    asset = tmp_path / "asset.png"
    for p in (prod, res, asset):
        _make_real_png(p)

    import unittest.mock as _mock
    with _mock.patch.object(asset_library, "get_asset",
                            lambda aid: {"id": aid, "type": "image", "path": str(asset)}
                            if aid == "a_0001" else None), \
         _mock.patch.object(asset_library, "_load_config",
                            lambda: {"media": {"max_refs_per_post": 10}}):
        catalog = asset_library.build_reference_catalog(
            [str(prod)], ["a_0001"], [str(res)],
        )
        refs = asset_library.build_input_references(
            [str(prod)], ["a_0001"], [str(res)],
        )

    # Same order, same count
    assert len(refs) == len(catalog) == 3
    for i, ref_path in enumerate(refs):
        assert ref_path == catalog[i]["path"], \
            f"order mismatch at {i}: provider got {ref_path}, catalog has {catalog[i]['path']}"
    assert refs[0] == str(prod)
    assert refs[1] == str(res)
    assert refs[2] == str(asset)


def test_max_refs_per_post_overflow_errors_without_truncation(monkeypatch, tmp_path):
    """When total references exceed max_refs_per_post, preflight_references must
    return a visible error. build_input_references must NOT silently truncate
    based on provenance or category — the user decides what to keep.

    This replaces the old provenance-based truncation behavior (drop optional
    assets first) which let code decide reference importance.
    """
    from src import asset_library

    prod1 = tmp_path / "p1.png"
    prod2 = tmp_path / "p2.png"
    res = tmp_path / "res.png"
    asset = tmp_path / "asset.png"
    for p in (prod1, prod2, res, asset):
        _make_real_png(p)

    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(asset)}
                        if aid == "a_0006" else None)
    # max_refs_per_post=2 → 4 refs (2 product + 1 resource + 1 asset) won't fit
    monkeypatch.setattr(asset_library, "_load_config",
                        lambda: {"media": {"max_refs_per_post": 2}})

    catalog = asset_library.build_reference_catalog(
        product_paths=[str(prod1), str(prod2)],
        asset_ids=["a_0006"],
        resource_paths=[str(res)],
    )
    # Catalog preserves ALL refs in order — no truncation at catalog build time
    assert len(catalog) == 4, f"catalog must keep all 4 refs, got {len(catalog)}"

    # Preflight catches the overflow visibly
    err = asset_library.preflight_references(catalog)
    assert err, "expected preflight error for overflow"
    assert "exceed" in err.lower() or "max_refs_per_post" in err, \
        f"error should mention the limit conflict: {err}"

    # build_input_references stays list[str] — preflight is the caller's job
    refs = asset_library.build_input_references(
        product_paths=[str(prod1), str(prod2)],
        asset_ids=["a_0006"],
        resource_paths=[str(res)],
    )
    assert isinstance(refs, list), "build_input_references must return list[str]"
    assert len(refs) == 4, "no silent truncation in build_input_references"


def test_max_refs_per_post_fits_when_under_limit(monkeypatch, tmp_path):
    """When refs fit under max_refs_per_post, preflight passes and all refs survive."""
    from src import asset_library

    prod1 = tmp_path / "p1.png"
    prod2 = tmp_path / "p2.png"
    asset = tmp_path / "asset.png"
    for p in (prod1, prod2, asset):
        _make_real_png(p)

    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(asset)}
                        if aid == "a_0006" else None)
    monkeypatch.setattr(asset_library, "_load_config",
                        lambda: {"media": {"max_refs_per_post": 5}})

    catalog = asset_library.build_reference_catalog(
        product_paths=[str(prod1), str(prod2)],
        asset_ids=["a_0006"],
        resource_paths=[],
    )
    assert asset_library.preflight_references(catalog) is None
    refs = asset_library.build_input_references(
        product_paths=[str(prod1), str(prod2)],
        asset_ids=["a_0006"],
        resource_paths=[],
    )
    assert len(refs) == 3
    assert str(prod1) in refs and str(prod2) in refs and str(asset) in refs


# ---------------------------------------------------------------------------
# Mechanical ordered reference catalog tests (replaces semantic manifest tests)
# ---------------------------------------------------------------------------

def test_reference_catalog_is_mechanical_no_semantic_fields(monkeypatch, tmp_path):
    """build_reference_catalog must produce mechanical fields only — ordinal,
    label, provenance, path, asset_id, filename, metadata. No required/fidelity/
    intent/composition (those were semantic decisions that belong to Agent 4)."""
    from src import asset_library

    prod = tmp_path / "prod.png"
    res = tmp_path / "res.png"
    asset = tmp_path / "asset.png"
    for p in (prod, res, asset):
        _make_real_png(p)

    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(asset),
                                     "subject": "logo", "tags": ["brand"],
                                     "description": "brand logo"}
                        if aid == "a_0001" else None)
    monkeypatch.setattr(asset_library, "_load_config",
                        lambda: {"media": {"max_refs_per_post": 10}})

    catalog = asset_library.build_reference_catalog(
        [str(prod)], ["a_0001"], [str(res)],
    )
    assert len(catalog) == 3
    forbidden = {"required", "fidelity", "intent", "composition", "user_ref_intents"}
    for ref in catalog:
        for f in forbidden:
            assert f not in ref, f"catalog entry must not carry semantic field '{f}': {ref}"
        # Mechanical fields present
        assert "ordinal" in ref and "label" in ref
        assert "provenance" in ref and "path" in ref
        assert "asset_id" in ref and "filename" in ref and "metadata" in ref

    # Order: product → user_resource → asset_library (stable)
    assert catalog[0]["provenance"] == "product_db"
    assert catalog[0]["ordinal"] == 1
    assert catalog[0]["label"] == "Reference 1"
    assert catalog[1]["provenance"] == "user_resource"
    assert catalog[1]["ordinal"] == 2
    assert catalog[2]["provenance"] == "asset_library"
    assert catalog[2]["ordinal"] == 3
    # Asset metadata is passed through (not interpreted)
    assert catalog[2]["metadata"].get("subject") == "logo"
    assert catalog[2]["asset_id"] == "a_0001"


def test_preflight_references_missing_file_errors_with_ordinal(monkeypatch, tmp_path):
    """Missing/unreadable reference must produce a visible preflight error that
    names the Reference number and path. Numbering must NOT silently shift."""
    from src import asset_library

    prod = tmp_path / "prod.png"
    res = tmp_path / "res.png"  # does NOT exist on disk
    _make_real_png(prod)

    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(res)}
                        if aid == "a_0001" else None)
    monkeypatch.setattr(asset_library, "_load_config",
                        lambda: {"media": {"max_refs_per_post": 10}})

    catalog = asset_library.build_reference_catalog(
        [str(prod)], ["a_0001"], [str(res)],
    )
    err = asset_library.preflight_references(catalog)
    assert err, "expected preflight error for missing file"
    # Error names the Reference number (not just the path) and does not renumber
    assert "Reference" in err, f"error should name the Reference number: {err}"
    assert str(res) in err, f"error should name the missing path: {err}"


def test_preflight_missing_asset_file_does_not_renumber(monkeypatch, tmp_path):
    """A missing Asset Library file must stay in the catalog with its ordinal
    so preflight can report it by Reference number. It must NOT be silently
    dropped (which would shift the numbering of later references)."""
    from src import asset_library

    prod = tmp_path / "prod.png"
    asset_missing = tmp_path / "missing_asset.png"  # does NOT exist
    res = tmp_path / "res.png"
    _make_real_png(prod)
    _make_real_png(res)

    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(asset_missing)}
                        if aid == "a_0001" else None)
    monkeypatch.setattr(asset_library, "_load_config",
                        lambda: {"media": {"max_refs_per_post": 10}})

    catalog = asset_library.build_reference_catalog(
        [str(prod)], ["a_0001"], [str(res)],
    )
    # The missing asset must still be in the catalog (not silently dropped)
    assert len(catalog) == 3, \
        f"missing asset must stay in catalog for preflight, got {len(catalog)} entries"
    # Order preserved: product=1, asset=2, resource=3 (asset inserted before resource)
    # Actually order is product → user_resource → asset_library
    assert catalog[0]["provenance"] == "product_db"
    assert catalog[1]["provenance"] == "user_resource"
    assert catalog[2]["provenance"] == "asset_library"
    assert catalog[2]["path"] == str(asset_missing)
    # Preflight catches the missing asset by its Reference number
    err = asset_library.preflight_references(catalog)
    assert err, "expected preflight error for missing asset file"
    assert "Reference 3" in err, \
        f"error must name Reference 3 (the missing asset), got: {err}"


def test_build_input_references_returns_list_str_only(monkeypatch, tmp_path):
    """build_input_references must return list[str] — never a dict error.
    Preflight is the caller's responsibility, not smuggled through this helper."""
    from src import asset_library

    prod = tmp_path / "prod.png"
    _make_real_png(prod)
    monkeypatch.setattr(asset_library, "_load_config",
                        lambda: {"media": {"max_refs_per_post": 1}})
    # Even when refs exceed limit, helper returns list — preflight catches it
    refs = asset_library.build_input_references(
        product_paths=[str(prod)], asset_ids=[], resource_paths=[],
    )
    assert isinstance(refs, list), "build_input_references must always return list[str]"
    assert all(isinstance(r, str) for r in refs)


# ---------------------------------------------------------------------------
# Privacy / input-image rejection tests
# ---------------------------------------------------------------------------

def test_input_image_rejected_classified_correctly():
    """input_image_rejected must be mechanically parsed from provider error codes."""
    from src.media_gen import _classify_media_error
    # ByteDance privacy rejection
    assert _classify_media_error(
        "InputImageSensitiveContentDetected", None, None,
    ) == "input_image_rejected"
    assert _classify_media_error(
        None, "InputImagePolicyViolation", None,
    ) == "input_image_rejected"
    # Regular content policy stays separate
    assert _classify_media_error("content_policy_violation", None, None) == "content_policy"
    # Transient stays separate
    assert _classify_media_error(None, None, 429) == "transient"
    assert _classify_media_error(None, None, 503) == "transient"


def test_image_retry_does_not_resend_same_refs_for_input_image_rejected(
    monkeypatch, tmp_path,
):
    """When provider returns input_image_rejected, must NOT resend the same reference set.
    Must return an actionable error, not loop with same refs."""
    from src import media_gen

    out = tmp_path / "out.png"
    call_count = {"n": 0}

    def _fake_generate_image(prompt, output_path, **kwargs):
        call_count["n"] += 1
        return {
            "ok": False,
            "error": "InputImageSensitiveContentDetected.PrivacyInformation",
            "error_type": "InputImageSensitiveContentDetected",
            "path": str(output_path),
        }

    monkeypatch.setattr(media_gen, "generate_image", _fake_generate_image)
    monkeypatch.setattr(media_gen, "_load_media_config", lambda: {})
    monkeypatch.setattr(media_gen, "_media_cfg", lambda: {"media_retry_model": "test/model"})

    result = media_gen.generate_image_with_retry(
        "test prompt", out,
        input_references=["/fake/ref.png"],
    )
    # Must make exactly 1 provider call — no retry with same rejected refs
    assert call_count["n"] == 1, \
        f"input_image_rejected must not resend same refs: got {call_count['n']} calls"
    assert result["ok"] is False
    assert "input_image_rejected" in result.get("error_category", "") or \
           result.get("error_type") == "InputImageSensitiveContentDetected"


def test_video_retry_does_not_resend_same_refs_for_input_image_rejected(
    monkeypatch, tmp_path,
):
    """Same invariant for video: input_image_rejected must not resend same refs."""
    from src import media_gen

    out = tmp_path / "out.mp4"
    call_count = {"n": 0}

    def _fake_generate_video(prompt, output_path, **kwargs):
        call_count["n"] += 1
        return {
            "ok": False,
            "error": "InputImagePolicyViolation",
            "error_code": "InputImagePolicyViolation",
            "path": str(output_path),
        }

    monkeypatch.setattr(media_gen, "generate_video", _fake_generate_video)
    monkeypatch.setattr(media_gen, "_load_media_config", lambda: {})
    monkeypatch.setattr(media_gen, "_media_cfg", lambda: {"media_retry_model": "test/model"})

    result = media_gen.generate_video_with_retry(
        "test prompt", out,
        input_references=["/fake/ref.png"],
    )
    assert call_count["n"] == 1, \
        f"input_image_rejected must not resend same refs: got {call_count['n']} calls"
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# Multi-product resolution tests
# ---------------------------------------------------------------------------

def test_multi_product_resolves_each_independently(monkeypatch, tmp_path):
    """Multi-product label "K5 + K2" must query each product separately."""
    import web_viewer

    k5_img = tmp_path / "k5.png"
    k2_img = tmp_path / "k2.png"
    _make_real_png(k5_img)
    _make_real_png(k2_img)

    from src import product_db
    call_log: list[str] = []

    def _fake_get_paths(pid):
        call_log.append(pid)
        if pid == "K5":
            return [str(k5_img)]
        elif pid == "K2":
            return [str(k2_img)]
        return []

    monkeypatch.setattr(product_db, "get_product_image_paths", _fake_get_paths)

    paths = web_viewer._resolve_product_image_paths("K5 + K2")
    assert "K5" in call_log, "K5 must be queried independently"
    assert "K2" in call_log, "K2 must be queried independently"
    assert "K5 + K2" not in call_log, "combined label must NOT be passed to product_db"
    assert str(k5_img) in paths, "K5 image must be in result"
    assert str(k2_img) in paths, "K2 image must be in result"


# ---------------------------------------------------------------------------
# Product visual override tests
# ---------------------------------------------------------------------------

def test_get_brand_visual_passes_product_id_for_override(monkeypatch):
    """_get_brand_visual(product_id) must call load_brand_visual with product_id."""
    import web_viewer

    call_log: list = []

    def _fake_load_brand_visual(brand_dir, product_id=None):
        call_log.append(product_id)
        if product_id == "K5":
            return {"colors": ["red"], "keywords": ["sporty"], "product_override": True}
        return {"colors": ["blue"], "keywords": ["general"]}

    monkeypatch.setattr(web_viewer, "load_brand_visual", _fake_load_brand_visual)
    monkeypatch.setattr(web_viewer, "_BRAND_VISUAL_CACHE", None)

    # Without product_id → base visual
    v1 = web_viewer._get_brand_visual()
    assert v1.get("product_override") is None

    # With product_id → override merged
    v2 = web_viewer._get_brand_visual("K5")
    assert v2.get("product_override") is True, "product visual override must reach caller"
    assert "K5" in call_log, "load_brand_visual must receive product_id"


# ---------------------------------------------------------------------------
# Reference remapping — Agent 4 catalog → per-item provider array
# ---------------------------------------------------------------------------

def test_reference_remap_across_agent_output_to_provider_boundary(monkeypatch, tmp_path):
    """Agent 4 sees a full catalog with multiple Asset references.
    One media item selects only a subset.
    The provider must receive that subset, and every 'Reference N' in the
    remapped prompt must map to the exact corresponding provider input file.

    This test crosses the actual Agent-output → compose_media_input boundary,
    not just the catalog builder twice with identical inputs.
    """
    from src import asset_library
    import web_viewer

    # 3 product images, 1 user resource, 3 asset library images
    prod1 = tmp_path / "prod1.png"; _make_real_png(prod1)
    prod2 = tmp_path / "prod2.png"; _make_real_png(prod2)
    prod3 = tmp_path / "prod3.png"; _make_real_png(prod3)
    res1 = tmp_path / "res1.png"; _make_real_png(res1)
    asset1 = tmp_path / "asset1.png"; _make_real_png(asset1)
    asset2 = tmp_path / "asset2.png"; _make_real_png(asset2)
    asset3 = tmp_path / "asset3.png"; _make_real_png(asset3)

    # Mock asset_library.get_asset for 3 assets
    _asset_recs = {
        "a_001": {"id": "a_001", "type": "image", "path": str(asset1), "subject": "logo"},
        "a_002": {"id": "a_002", "type": "image", "path": str(asset2), "subject": "lifestyle"},
        "a_003": {"id": "a_003", "type": "image", "path": str(asset3), "subject": "product"},
    }
    monkeypatch.setattr(asset_library, "get_asset", lambda aid: _asset_recs.get(aid))
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    # Full catalog: Agent 4 sees 7 references
    # Reference 1-3: product images
    # Reference 4: user resource
    # Reference 5-7: asset library images
    full_catalog_asset_ids = ["a_001", "a_002", "a_003"]
    full_catalog = asset_library.build_reference_catalog(
        [str(prod1), str(prod2), str(prod3)],
        full_catalog_asset_ids,
        [str(res1)],
    )
    assert len(full_catalog) == 7
    # Verify ordinals
    assert full_catalog[0]["label"] == "Reference 1"  # prod1
    assert full_catalog[4]["label"] == "Reference 5"  # asset1
    assert full_catalog[6]["label"] == "Reference 7"  # asset3

    # Agent 4 writes a prompt mentioning Reference 5 (asset1) and Reference 7 (asset3)
    agent4_prompt = "Use Reference 5 for the logo overlay and Reference 7 for the product shot"

    # Per-item: media item selects only [a_001, a_003] (asset1, asset3)
    item_asset_ids = ["a_001", "a_003"]

    # Compose through the shared seam with catalog_asset_ids
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda *a, **k: {})
    monkeypatch.setattr(web_viewer, "_resolve_product_image_paths",
                        lambda pid: [str(prod1), str(prod2), str(prod3)])

    composed = web_viewer.compose_media_input(
        prompt=agent4_prompt,
        product_id="TEST",
        asset_ids=item_asset_ids,
        resource_paths=[str(res1)],
        catalog_asset_ids=full_catalog_asset_ids,
    )

    # No preflight error
    assert not composed.get("preflight_error"), composed.get("preflight_error")

    # Provider receives 6 files: 3 product + 1 resource + 2 assets (subset)
    input_refs = composed["input_references"]
    assert len(input_refs) == 6, f"expected 6 refs, got {len(input_refs)}: {input_refs}"

    # The remapped prompt: per-item catalog is:
    # prod1=1, prod2=2, prod3=3, res1=4, asset1=5, asset3=6
    # Reference 5 (asset1 in full) → Reference 5 (same position, first asset)
    # Reference 7 (asset3 in full) → Reference 6 (shifted because asset2 was dropped)
    remapped_prompt = composed["prompt"]
    assert "Reference 6" in remapped_prompt, \
        f"Reference 7 should be remapped to Reference 6: {remapped_prompt}"
    assert "Reference 5" in remapped_prompt, \
        f"Reference 5 should remain Reference 5: {remapped_prompt}"
    # Reference 7 must NOT appear in the remapped prompt
    assert "Reference 7" not in remapped_prompt, \
        f"Reference 7 should have been remapped: {remapped_prompt}"

    # Verify: Reference 5 in the remapped prompt maps to input_refs[4] (asset1)
    # and Reference 6 maps to input_refs[5] (asset3)
    item_catalog = composed["reference_catalog"]
    ref5_entry = [r for r in item_catalog if r["ordinal"] == 5][0]
    ref6_entry = [r for r in item_catalog if r["ordinal"] == 6][0]
    assert ref5_entry["path"] == str(asset1), \
        f"Reference 5 must be asset1, got {ref5_entry['path']}"
    assert ref6_entry["path"] == str(asset3), \
        f"Reference 6 must be asset3, got {ref6_entry['path']}"
    # And these match the provider input array
    assert input_refs[4] == str(asset1)
    assert input_refs[5] == str(asset3)


def test_reference_remap_noop_when_subset_equals_full(monkeypatch, tmp_path):
    """When per-item asset_ids == catalog_asset_ids, no remapping occurs
    and the prompt is unchanged."""
    from src import asset_library
    import web_viewer

    prod1 = tmp_path / "prod1.png"; _make_real_png(prod1)
    asset1 = tmp_path / "asset1.png"; _make_real_png(asset1)

    monkeypatch.setattr(asset_library, "get_asset",
                        lambda aid: {"id": aid, "type": "image", "path": str(asset1)})
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda *a, **k: {})
    monkeypatch.setattr(web_viewer, "_resolve_product_image_paths", lambda pid: [str(prod1)])

    prompt = "Use Reference 2 for the brand logo"
    composed = web_viewer.compose_media_input(
        prompt=prompt, product_id="TEST",
        asset_ids=["a_001"], resource_paths=[],
        catalog_asset_ids=["a_001"],
    )
    # No remapping needed — prompt unchanged
    assert composed["prompt"] == prompt
    assert composed["reference_remap"] is None


# ---------------------------------------------------------------------------
# Brand context reaches Agent 4
# ---------------------------------------------------------------------------

def test_agent4_prompt_receives_brand_context():
    """Agent 4's build_prompt must include brand_context when provided."""
    from src.agents.content_creator import ContentCreatorAgent

    agent = ContentCreatorAgent.__new__(ContentCreatorAgent)
    prompt = agent.build_prompt(
        product_spec="Product K5 spec",
        competitor_analysis="",
        campaign_strategy="",
        media_capabilities="Image: 1024x1024",
        visual_style="tone: modern",
        asset_summary="",
        reference_catalog=None,
        brand_context="target_audience: young professionals\ntone: premium",
    )
    assert "บริบทแบรนด์และผู้อ่าน" in prompt, "brand context section must appear"
    assert "young professionals" in prompt
    assert "premium" in prompt
    # Also verify other context is present
    assert "Product K5 spec" in prompt, "product context must appear"
    assert "modern" in prompt, "visual style must appear"
    assert "1024x1024" in prompt, "media capabilities must appear"


# ---------------------------------------------------------------------------
# Completed-video download retry — single generation submission
# ---------------------------------------------------------------------------

def test_video_download_retry_succeeds_without_resubmitting(monkeypatch, tmp_path):
    """Video generation POST succeeds exactly once.
    Job reaches completed state.
    First artifact download FAILS.
    Bounded download retry SUCCEEDS.
    Total generation POST submissions remain exactly 1.
    """
    from src import media_gen

    _stub_media_env(monkeypatch)

    post_count: list[str] = []
    download_count: list[str] = []

    def handler(request):
        url = str(request.url)
        # submit — count POSTs to /api/v1/videos
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            post_count.append(url)
            return httpx.Response(200, json={
                "id": "job-dl-retry",
                "polling_url": "/api/v1/videos/job-dl-retry",
                "status": "pending",
            })
        # poll
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-dl-retry":
            return httpx.Response(200, json={
                "id": "job-dl-retry",
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/video.mp4"],
                "usage": {"cost": 0.16},
            })
        # download — fail first time, succeed second time
        if request.method == "GET" and url == "https://cdn.example.com/video.mp4":
            download_count.append(url)
            if len(download_count) == 1:
                # First download attempt fails
                return httpx.Response(503, text="Service Unavailable")
            # Retry succeeds
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42VIDEOBYTES")
        return httpx.Response(404, text=f"unexpected {request.method} {url}")

    _patch_http(monkeypatch, handler)

    # Set short retry delay so test runs fast
    monkeypatch.setattr(media_gen, "_system_cfg", lambda: {
        "api_timeout_video": 60,
        "api_timeout_video_download": 5,
        "video_download_retries": 3,
        "video_download_retry_delay_seconds": 0.0,
    })

    out_path = tmp_path / "video_dl_retry.mp4"
    result = media_gen.generate_video(
        "a cinematic product shot", out_path,
        poll_interval=0.0, max_wait=10.0,
    )

    assert result.get("ok") is True, f"expected ok, got: {result}"
    assert result["path"] == str(out_path)
    assert out_path.exists()
    # Exactly 1 generation POST
    assert len(post_count) == 1, \
        f"exactly 1 generation POST expected, got {len(post_count)}"
    # Exactly 2 download attempts (1 fail + 1 success)
    assert len(download_count) == 2, \
        f"exactly 2 download attempts expected (1 fail + 1 success), got {len(download_count)}"


# ---------------------------------------------------------------------------
# Dangling reference preflight — reject prompts referencing excluded refs
# ---------------------------------------------------------------------------

def _setup_remap_env(monkeypatch, tmp_path):
    """Shared setup: 1 product image, 3 asset images, mocked asset_library."""
    prod1 = tmp_path / "prod1.png"; _make_real_png(prod1)
    asset1 = tmp_path / "asset1.png"; _make_real_png(asset1)
    asset2 = tmp_path / "asset2.png"; _make_real_png(asset2)
    asset3 = tmp_path / "asset3.png"; _make_real_png(asset3)

    _asset_recs = {
        "a_001": {"id": "a_001", "type": "image", "path": str(asset1), "subject": "logo"},
        "a_002": {"id": "a_002", "type": "image", "path": str(asset2), "subject": "lifestyle"},
        "a_003": {"id": "a_003", "type": "image", "path": str(asset3), "subject": "product"},
    }
    from src import asset_library
    monkeypatch.setattr(asset_library, "get_asset", lambda aid: _asset_recs.get(aid))
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    import web_viewer
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda *a, **k: {})
    monkeypatch.setattr(web_viewer, "_resolve_product_image_paths", lambda pid: [str(prod1)])

    return prod1, asset1, asset2, asset3


def test_dangling_reference_remap_succeeds_for_included_refs(monkeypatch, tmp_path):
    """Test A: Full catalog has refs 5,6,7 (assets). Per-item selects 5 and 7.
    Prompt references 5 and 7. Remap succeeds: 5→5, 7→6. Provider order matches."""
    prod1, asset1, asset2, asset3 = _setup_remap_env(monkeypatch, tmp_path)
    import web_viewer

    full_catalog_asset_ids = ["a_001", "a_002", "a_003"]
    item_asset_ids = ["a_001", "a_003"]  # subset: a_001 and a_003

    # Full catalog: prod=1, a_001=2, a_002=3, a_003=4
    # Per-item catalog: prod=1, a_001=2, a_003=3
    # Prompt references "Reference 2" (a_001) and "Reference 4" (a_003)
    # Remap: 2→2 (same), 4→3 (shifted)
    prompt = "Use Reference 2 for the logo and Reference 4 for the product"
    composed = web_viewer.compose_media_input(
        prompt=prompt, product_id="TEST",
        asset_ids=item_asset_ids, resource_paths=[],
        catalog_asset_ids=full_catalog_asset_ids,
    )
    assert not composed.get("preflight_error"), composed.get("preflight_error")
    remapped = composed["prompt"]
    assert "Reference 2" in remapped, f"Reference 2 should remain: {remapped}"
    assert "Reference 3" in remapped, f"Reference 4 should remap to 3: {remapped}"
    assert "Reference 4" not in remapped, f"Reference 4 should be remapped: {remapped}"
    # Provider receives: prod + a_001 + a_003 = 3 files
    refs = composed["input_references"]
    assert len(refs) == 3
    assert refs[1] == str(asset1), f"Reference 2 must be asset1: {refs}"
    assert refs[2] == str(asset3), f"Reference 3 must be asset3: {refs}"


def test_dangling_reference_preflight_rejects_excluded_ref(monkeypatch, tmp_path):
    """Test B: Same catalog/subset. Prompt references 5,6,7 (all 3 assets).
    Per-item selects only 5 and 7 (a_001, a_003). Reference 6 (a_002) was excluded.
    Preflight FAILS. Provider call count = 0."""
    prod1, asset1, asset2, asset3 = _setup_remap_env(monkeypatch, tmp_path)
    import web_viewer

    full_catalog_asset_ids = ["a_001", "a_002", "a_003"]
    item_asset_ids = ["a_001", "a_003"]  # a_002 excluded

    # Full catalog: prod=1, a_001=2, a_002=3, a_003=4
    # Prompt references Reference 2 (a_001), Reference 3 (a_002), Reference 4 (a_003)
    # Reference 3 (a_002) was excluded → dangling → preflight error
    prompt = "Use Reference 2, Reference 3, and Reference 4 for the composition"
    composed = web_viewer.compose_media_input(
        prompt=prompt, product_id="TEST",
        asset_ids=item_asset_ids, resource_paths=[],
        catalog_asset_ids=full_catalog_asset_ids,
    )
    err = composed.get("preflight_error")
    assert err is not None, "Preflight should reject dangling Reference 3"
    assert "Reference 3" in err, f"Error should name the missing Reference: {err}"
    # Provider receives NO input references
    assert composed["input_references"] == [], \
        "Provider should not receive any refs when preflight fails"
    # Verify provider would NOT be called by checking the composed result
    assert composed["preflight_error"] is not None


def test_dangling_reference_no_silent_collision(monkeypatch, tmp_path):
    """Test C: No duplicate/colliding Reference number can cause two semantically
    different original references to become the same provider position silently.

    Full catalog: prod=1, a_001=2, a_002=3, a_003=4
    Per-item selects [a_001, a_003] → per-item catalog: prod=1, a_001=2, a_003=3
    If prompt references BOTH Reference 3 (a_002, excluded) and Reference 3 (per-item a_003),
    the preflight must catch that original Reference 3 was excluded.
    The remap must NOT silently map both to the same position.
    """
    prod1, asset1, asset2, asset3 = _setup_remap_env(monkeypatch, tmp_path)
    from src import asset_library

    full_catalog_asset_ids = ["a_001", "a_002", "a_003"]
    item_asset_ids = ["a_001", "a_003"]  # a_002 excluded

    full_catalog = asset_library.build_reference_catalog(
        [str(prod1)], full_catalog_asset_ids, [],
    )
    item_catalog, remap = asset_library.build_reference_remap(
        full_catalog, item_asset_ids, [str(prod1)], [],
    )
    # Full catalog: prod=1, a_001=2, a_002=3, a_003=4
    # Per-item: prod=1, a_001=2, a_003=3
    # remap: {1→1, 2→2, 4→3}
    # Reference 3 (a_002) is NOT in remap → dangling
    assert 3 not in remap, "Reference 3 (a_002) should be absent from remap"
    assert remap[4] == 3, "Reference 4 (a_003) should remap to 3"

    # Prompt mentions Reference 3 (excluded a_002) → preflight must catch it
    prompt = "Use Reference 3 for the background"
    err = asset_library.preflight_reference_mentions(prompt, full_catalog, remap)
    assert err is not None, "Should reject Reference 3 (excluded)"
    assert "Reference 3" in err

    # Prompt mentions Reference 4 (included a_003, remaps to 3) → should pass
    prompt2 = "Use Reference 4 for the product shot"
    err2 = asset_library.preflight_reference_mentions(prompt2, full_catalog, remap)
    assert err2 is None, "Reference 4 is included, should pass"

    # After remap, "Reference 4" becomes "Reference 3" — but this is correct
    # because it maps to a_003 at per-item position 3, NOT to a_002
    remapped = asset_library.remap_reference_numbers(prompt2, remap)
    assert "Reference 3" in remapped
    # The per-item catalog position 3 is a_003, not a_002
    pos3_entry = [r for r in item_catalog if r["ordinal"] == 3][0]
    assert pos3_entry["path"] == str(asset3), \
        f"Per-item Reference 3 must be asset3, not asset2: {pos3_entry}"


# ---------------------------------------------------------------------------
# Per-item product-reference selection — Agent 4 chooses a subset of BOTH
# product and asset references per media item. Python transports that decision
# mechanically by extracting "Reference N" ordinals from the prompt.
# ---------------------------------------------------------------------------

def _setup_product_subset_env(monkeypatch, tmp_path):
    """3 product images + 1 asset library image.
    Full catalog: Reference 1=prod1, 2=prod2, 3=prod3, 4=asset1(logo).
    """
    prod1 = tmp_path / "prod1.png"; _make_real_png(prod1)
    prod2 = tmp_path / "prod2.png"; _make_real_png(prod2)
    prod3 = tmp_path / "prod3.png"; _make_real_png(prod3)
    asset1 = tmp_path / "asset1.png"; _make_real_png(asset1)

    _asset_recs = {
        "a_001": {"id": "a_001", "type": "image", "path": str(asset1), "subject": "logo"},
    }
    from src import asset_library
    monkeypatch.setattr(asset_library, "get_asset", lambda aid: _asset_recs.get(aid))
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    import web_viewer
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda *a, **k: {})
    monkeypatch.setattr(web_viewer, "_resolve_product_image_paths",
                        lambda pid: [str(prod1), str(prod2), str(prod3)])

    return prod1, prod2, prod3, asset1


def test_extract_reference_ordinals():
    """extract_reference_ordinals parses 'Reference N' from prompt text."""
    from src import asset_library
    assert asset_library.extract_reference_ordinals("Use Reference 2 and Reference 4") == [2, 4]
    assert asset_library.extract_reference_ordinals("No references here") == []
    assert asset_library.extract_reference_ordinals("Reference 1 Reference 1 Reference 3") == [1, 3]
    assert asset_library.extract_reference_ordinals("") == []


def test_filter_catalog_by_ordinals():
    """filter_catalog_by_ordinals filters catalog and builds remap."""
    from src import asset_library
    catalog = [
        {"ordinal": 1, "label": "Reference 1", "path": "/a.png", "provenance": "product_db"},
        {"ordinal": 2, "label": "Reference 2", "path": "/b.png", "provenance": "product_db"},
        {"ordinal": 3, "label": "Reference 3", "path": "/c.png", "provenance": "product_db"},
        {"ordinal": 4, "label": "Reference 4", "path": "/d.png", "provenance": "asset_library"},
    ]
    filtered, remap = asset_library.filter_catalog_by_ordinals(catalog, [2, 4])
    assert len(filtered) == 2
    assert filtered[0]["ordinal"] == 1
    assert filtered[0]["path"] == "/b.png"
    assert filtered[1]["ordinal"] == 2
    assert filtered[1]["path"] == "/d.png"
    assert remap == {2: 1, 4: 2}
    # Original catalog must not be mutated
    assert catalog[0]["ordinal"] == 1
    assert catalog[1]["ordinal"] == 2


def test_per_item_product_subset_excludes_unselected(monkeypatch, tmp_path):
    """Test A: Full catalog: prod1=1, prod2=2, prod3=3, asset1=4.
    Media item selects Reference 2 + Reference 4.
    Provider receives exactly 2 files (prod2, asset1).
    References 1 and 3 are NOT sent."""
    prod1, prod2, prod3, asset1 = _setup_product_subset_env(monkeypatch, tmp_path)
    import web_viewer

    prompt = "Use Reference 2 for the product and Reference 4 for the logo"
    composed = web_viewer.compose_media_input(
        prompt=prompt,
        product_id="TEST",
        asset_ids=["a_001"],
        resource_paths=[],
        catalog_asset_ids=["a_001"],
        selected_reference_ordinals=[2, 4],
    )

    assert not composed.get("preflight_error"), composed.get("preflight_error")
    refs = composed["input_references"]
    assert len(refs) == 2, f"expected 2 refs, got {len(refs)}: {refs}"
    assert refs[0] == str(prod2), f"Reference 1 should be prod2: {refs}"
    assert refs[1] == str(asset1), f"Reference 2 should be asset1: {refs}"
    assert str(prod1) not in refs, "prod1 (Reference 1) must NOT be sent"
    assert str(prod3) not in refs, "prod3 (Reference 3) must NOT be sent"


def test_per_item_prompt_remapped_to_filtered_positions(monkeypatch, tmp_path):
    """Test B: Prompt references Reference 2 and Reference 4.
    After filtering to [2, 4], prompt is remapped to Reference 1 and Reference 2."""
    prod1, prod2, prod3, asset1 = _setup_product_subset_env(monkeypatch, tmp_path)
    import web_viewer

    prompt = "Use Reference 2 for the product and Reference 4 for the logo"
    composed = web_viewer.compose_media_input(
        prompt=prompt,
        product_id="TEST",
        asset_ids=["a_001"],
        resource_paths=[],
        catalog_asset_ids=["a_001"],
        selected_reference_ordinals=[2, 4],
    )

    assert not composed.get("preflight_error")
    remapped = composed["prompt"]
    assert "Reference 1" in remapped, f"Reference 2 should remap to 1: {remapped}"
    assert "Reference 2" in remapped, f"Reference 4 should remap to 2: {remapped}"
    assert "Reference 4" not in remapped, f"Reference 4 should be remapped: {remapped}"


def test_per_item_dangling_reference_preflight_fail(monkeypatch, tmp_path):
    """Test C: Media item selects 2+4 but prompt also references 3.
    Preflight FAILS. Provider receives 0 refs."""
    prod1, prod2, prod3, asset1 = _setup_product_subset_env(monkeypatch, tmp_path)
    import web_viewer

    prompt = "Use Reference 2, Reference 3, and Reference 4"
    composed = web_viewer.compose_media_input(
        prompt=prompt,
        product_id="TEST",
        asset_ids=["a_001"],
        resource_paths=[],
        catalog_asset_ids=["a_001"],
        selected_reference_ordinals=[2, 4],
    )

    err = composed.get("preflight_error")
    assert err is not None, "Preflight should reject dangling Reference 3"
    assert "Reference 3" in err, f"Error should name Reference 3: {err}"
    assert composed["input_references"] == [], "Provider should receive 0 refs"


def test_per_item_different_subsets_for_image_and_video(monkeypatch, tmp_path):
    """Test D: Image selects 2+3+4, video selects 2+4.
    Each provider call gets only its own subset."""
    prod1, prod2, prod3, asset1 = _setup_product_subset_env(monkeypatch, tmp_path)
    import web_viewer

    # Image: selects 2, 3, 4
    img_prompt = "Use Reference 2, Reference 3, and Reference 4"
    composed_img = web_viewer.compose_media_input(
        prompt=img_prompt,
        product_id="TEST",
        asset_ids=["a_001"],
        resource_paths=[],
        catalog_asset_ids=["a_001"],
        selected_reference_ordinals=[2, 3, 4],
    )
    assert not composed_img.get("preflight_error")
    assert len(composed_img["input_references"]) == 3
    assert composed_img["input_references"] == [str(prod2), str(prod3), str(asset1)]

    # Video: selects 2, 4
    vid_prompt = "Use Reference 2 and Reference 4"
    composed_vid = web_viewer.compose_media_input(
        prompt=vid_prompt,
        product_id="TEST",
        asset_ids=["a_001"],
        resource_paths=[],
        catalog_asset_ids=["a_001"],
        selected_reference_ordinals=[2, 4],
    )
    assert not composed_vid.get("preflight_error")
    assert len(composed_vid["input_references"]) == 2
    assert composed_vid["input_references"] == [str(prod2), str(asset1)]


def test_per_item_refresh_regenerate_restores_same_subset(monkeypatch, tmp_path):
    """Test E: Persist a prompt that references 2+4.
    After 'session reconstruction' (re-reading the persisted prompt),
    regenerate without frontend selection state.
    Assert exactly refs 2+4 are restored — no other product reference is sent."""
    prod1, prod2, prod3, asset1 = _setup_product_subset_env(monkeypatch, tmp_path)
    import web_viewer
    from src import asset_library

    # Simulate: Agent 4 produced a prompt that references 2 and 4
    original_prompt = "Use Reference 2 for the product and Reference 4 for the logo"

    # Simulate persistence: the prompt is saved in a JSON artifact
    artifact = {"posts": [{"image_prompts": [{"prompt": original_prompt}], "asset_ids": ["a_001"]}]}
    artifact_path = tmp_path / "agent4_output.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    # Simulate session reconstruction: read the artifact, extract the prompt
    loaded = json.loads(artifact_path.read_text(encoding="utf-8"))
    loaded_prompt = loaded["posts"][0]["image_prompts"][0]["prompt"]

    # Extract ordinals from the persisted prompt (no frontend state needed)
    ordinals = asset_library.extract_reference_ordinals(loaded_prompt)
    assert ordinals == [2, 4], f"expected [2, 4], got {ordinals}"

    # Regenerate using the extracted ordinals
    composed = web_viewer.compose_media_input(
        prompt=loaded_prompt,
        product_id="TEST",
        asset_ids=["a_001"],
        resource_paths=[],
        catalog_asset_ids=["a_001"],
        selected_reference_ordinals=ordinals,
    )

    assert not composed.get("preflight_error")
    refs = composed["input_references"]
    assert len(refs) == 2, f"expected 2 refs, got {len(refs)}"
    assert refs == [str(prod2), str(asset1)]
    assert str(prod1) not in refs
    assert str(prod3) not in refs


def test_uat_regression_unrelated_product_ref_not_sent(monkeypatch, tmp_path):
    """Test H: Real-UAT finding regression fixture.

    Reference 1 = product DB image (e.g., an app screenshot — NO category logic)
    Reference 2 = product DB image (actual product imagery)
    Reference 3 = product DB image (actual product imagery)
    Reference 4 = asset library image (logo)

    Agent media item requests only Reference 2 + Reference 4.
    Assert Reference 1 never reaches the provider.

    No brand/category/screenshot logic in production code — test uses generic files.
    """
    prod1, prod2, prod3, asset1 = _setup_product_subset_env(monkeypatch, tmp_path)
    import web_viewer

    # Agent 4's prompt mentions only Reference 2 and Reference 4
    # (Reference 1 is an unrelated product-context image Agent 4 chose NOT to use)
    prompt = "Show the product from Reference 2 with the logo from Reference 4"
    composed = web_viewer.compose_media_input(
        prompt=prompt,
        product_id="TEST",
        asset_ids=["a_001"],
        resource_paths=[],
        catalog_asset_ids=["a_001"],
        selected_reference_ordinals=[2, 4],
    )

    assert not composed.get("preflight_error"), composed.get("preflight_error")
    refs = composed["input_references"]
    # Reference 1 (prod1) must NEVER reach the provider
    assert str(prod1) not in refs, \
        "Reference 1 (unrelated product image) must NOT be sent to provider"
    assert str(prod3) not in refs, \
        "Reference 3 (unselected product image) must NOT be sent to provider"
    assert len(refs) == 2, f"expected exactly 2 refs, got {len(refs)}"
    assert refs == [str(prod2), str(asset1)]


# ---------------------------------------------------------------------------
# Out-of-range / unknown reference ordinals — preflight must reject any
# mentioned "Reference N" that does not exist in the full catalog, and must
# not return early when the full catalog is empty. These exercise the real
# production sequence: extract_reference_ordinals(prompt) → compose_media_input.
# ---------------------------------------------------------------------------

def test_unknown_reference_ordinal_rejected_via_real_sequence(monkeypatch, tmp_path):
    """Regression for Codex review finding: prompt mentions Reference 99 while
    the full catalog only contains Reference 1. Production derives
    selected_reference_ordinals from the same prompt via
    extract_reference_ordinals, then calls compose_media_input.

    Expected:
      - visible preflight_error;
      - error names 'Reference 99';
      - input_references == [];
      - provider is not called.
    """
    prod1, prod2, prod3, asset1 = _setup_product_subset_env(monkeypatch, tmp_path)
    import web_viewer
    from src import asset_library

    prompt = "Use Reference 99 for the hero shot"
    # Real production sequence: caller extracts ordinals from the same prompt
    ordinals = asset_library.extract_reference_ordinals(prompt)
    assert ordinals == [99]

    provider_called: list = []
    def _fake_gen(*a, **k):
        provider_called.append(True)
        return {"ok": True, "path": "x", "model": "test"}
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry", _fake_gen)

    composed = web_viewer.compose_media_input(
        prompt=prompt,
        product_id="TEST",
        asset_ids=["a_001"],
        resource_paths=[],
        catalog_asset_ids=["a_001"],
        selected_reference_ordinals=ordinals,
    )

    err = composed.get("preflight_error")
    assert err is not None, "Preflight must reject unknown Reference 99"
    assert "Reference 99" in err, f"Error must name Reference 99: {err}"
    assert composed["input_references"] == [], \
        f"Provider must receive 0 refs, got: {composed['input_references']}"
    assert provider_called == [], "Provider must not be called on preflight failure"


def test_reference_mention_with_empty_full_catalog_rejected(monkeypatch, tmp_path):
    """Prompt mentions Reference 1 but the full catalog is empty (no product
    images, no assets, no resources). Preflight must reject — not return early.

    Expected:
      - visible preflight_error;
      - provider is not called.
    """
    import web_viewer
    from src import asset_library

    # Empty catalog: no product images, no assets, no resources
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda *a, **k: {})
    monkeypatch.setattr(web_viewer, "_resolve_product_image_paths", lambda pid: [])
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    provider_called: list = []
    def _fake_gen(*a, **k):
        provider_called.append(True)
        return {"ok": True, "path": "x", "model": "test"}
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry", _fake_gen)

    prompt = "Use Reference 1 for the product"
    ordinals = asset_library.extract_reference_ordinals(prompt)
    assert ordinals == [1]

    composed = web_viewer.compose_media_input(
        prompt=prompt,
        product_id="TEST",
        asset_ids=[],
        resource_paths=[],
        catalog_asset_ids=[],
        selected_reference_ordinals=ordinals,
    )

    err = composed.get("preflight_error")
    assert err is not None, "Preflight must reject Reference 1 against empty catalog"
    assert "Reference 1" in err, f"Error must name Reference 1: {err}"
    assert provider_called == [], "Provider must not be called on preflight failure"


# ---------------------------------------------------------------------------
# Provider-advertised pricing metadata — cost-aware duration seam (C2)
# ---------------------------------------------------------------------------

def test_video_capability_parsing_preserves_pricing_skus(monkeypatch, tmp_path):
    """get_model_capabilities must preserve pricing_skus from the video-models
    API response so Agent 4 can reason about cost vs. duration."""
    from src import media_gen
    from src import openrouter_gateway

    monkeypatch.setattr(media_gen, "_get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr(openrouter_gateway, "get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr(media_gen, "_CAPABILITIES_CACHE", {})
    monkeypatch.setattr(media_gen, "_CAPABILITIES_CACHE_DIR", tmp_path)

    def handler(request):
        return httpx.Response(200, json={
            "data": [{
                "id": "bytedance/seedance-2.0-fast",
                "name": "Seedance 2.0 Fast",
                "supported_durations": [4, 5, 6, 7, 8],
                "supported_aspect_ratios": ["16:9"],
                "supported_resolutions": ["720p"],
                "supported_sizes": None,
                "generate_audio": True,
                "supported_frame_images": ["first_frame", "last_frame"],
                "pricing_skus": {"per-video-second": "0.1028"},
            }]
        })

    _patch_http(monkeypatch, handler)

    caps = media_gen.get_model_capabilities("bytedance/seedance-2.0-fast", kind="video")
    assert caps.get("pricing_skus") == {"per-video-second": "0.1028"}, \
        "pricing_skus must be preserved from API response"


def test_format_capabilities_includes_per_second_cost_when_present(monkeypatch, tmp_path):
    """format_capabilities_for_prompt must surface provider-advertised per-second
    cost as approximate pricing so Agent 4 can weigh duration vs. cost."""
    from src import media_gen

    monkeypatch.setattr(media_gen, "get_model_capabilities", lambda *a, **k: {
        "id": "bytedance/seedance-2.0-fast",
        "durations": [4, 5, 6, 7, 8],
        "aspect_ratios": ["16:9"],
        "resolutions": ["720p"],
        "generate_audio": True,
        "pricing_skus": {"duration_seconds": "0.1028"},
    })

    text = media_gen.format_capabilities_for_prompt("bytedance/seedance-2.0-fast", kind="video")
    assert "0.1028" in text, f"per-second price must appear: {text}"
    assert "วินาที" in text, f"duration capability must still appear: {text}"
    # Must be framed as advertised/approximate, not a guaranteed charge
    assert "โดยประมาณ" in text or "advertised" in text.lower() or "approx" in text.lower(), \
        f"must be framed as approximate: {text}"


def test_format_capabilities_without_pricing_remains_valid(monkeypatch, tmp_path):
    """Missing pricing metadata must not break formatting — image models and
    providers that omit pricing_skus must still produce usable capability text."""
    from src import media_gen

    monkeypatch.setattr(media_gen, "get_model_capabilities", lambda *a, **k: {
        "id": "google/gemini-3.1-flash-image",
        "durations": [],
        "aspect_ratios": [],
        "resolutions": [],
        "generate_audio": None,
        "pricing_skus": {},
    })

    text = media_gen.format_capabilities_for_prompt("google/gemini-3.1-flash-image", kind="image")
    # No caps fields present → empty string (existing behavior preserved)
    assert text == "", f"empty caps must yield empty text: {text!r}"

    monkeypatch.setattr(media_gen, "get_model_capabilities", lambda *a, **k: {
        "id": "some/video-model",
        "durations": [4, 5],
        "aspect_ratios": ["16:9"],
        "resolutions": ["720p"],
        "generate_audio": False,
        "pricing_skus": {},
    })
    text = media_gen.format_capabilities_for_prompt("some/video-model", kind="video")
    assert "วินาที" in text, f"duration must still appear without pricing: {text}"
    assert "cost" not in text.lower() and "$" not in text and "โดยประมาณ" not in text, \
        f"no pricing line when pricing_skus empty: {text}"


def test_stale_cache_without_pricing_refreshes_to_include_pricing(monkeypatch, tmp_path):
    """A cached capability record written before pricing_skus was preserved must
    not permanently block pricing metadata from reaching the Agent. The cache
    refresh path must re-fetch and include the new field."""
    from src import media_gen
    from src import openrouter_gateway

    monkeypatch.setattr(media_gen, "_get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr(openrouter_gateway, "get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr(media_gen, "_CAPABILITIES_CACHE", {})
    monkeypatch.setattr(media_gen, "_CAPABILITIES_CACHE_DIR", tmp_path)

    # Simulate a stale pre-change cache file (no pricing_skus field)
    stale_caps = {
        "id": "bytedance/seedance-2.0-fast",
        "name": "Seedance 2.0 Fast",
        "durations": [4, 5, 6, 7, 8],
        "aspect_ratios": ["16:9"],
        "resolutions": ["720p"],
        "sizes": [],
        "generate_audio": True,
        "frame_images": ["first_frame", "last_frame"],
    }
    cache_file = tmp_path / "video_bytedance_seedance-2.0-fast.json"
    cache_file.write_text(json.dumps(stale_caps, ensure_ascii=False), encoding="utf-8")

    def handler(request):
        return httpx.Response(200, json={
            "data": [{
                "id": "bytedance/seedance-2.0-fast",
                "name": "Seedance 2.0 Fast",
                "supported_durations": [4, 5, 6, 7, 8],
                "supported_aspect_ratios": ["16:9"],
                "supported_resolutions": ["720p"],
                "supported_sizes": None,
                "generate_audio": True,
                "supported_frame_images": ["first_frame", "last_frame"],
                "pricing_skus": {"per-video-second": "0.1028"},
            }]
        })

    _patch_http(monkeypatch, handler)

    caps = media_gen.get_model_capabilities("bytedance/seedance-2.0-fast", kind="video")
    assert caps.get("pricing_skus") == {"per-video-second": "0.1028"}, \
        "stale cache without pricing_skus must refresh to include it"


def test_agent4_prompt_receives_cost_conscious_duration_instruction():
    """Agent 4 build_prompt with video media_capabilities must include a generic
    instruction to choose the shortest sufficient duration considering cost,
    without imposing a hard maximum."""
    from src.agents.content_creator import ContentCreatorAgent

    agent = ContentCreatorAgent.__new__(ContentCreatorAgent)
    prompt = agent.build_prompt(
        product_spec="Product K5 spec",
        competitor_analysis="",
        campaign_strategy="",
        media_capabilities="duration: 4-8 วินาที | cost โดยประมาณ: ~$0.10/วินาที",
        media_type="video",
        visual_style="",
        asset_summary="",
        reference_catalog=None,
        brand_context="",
    )
    assert "duration" in prompt.lower(), "duration capability must appear"
    # Generic cost-conscious instruction present
    assert "สั้นที่สุด" in prompt or "shortest" in prompt.lower(), \
        "must instruct to choose shortest sufficient duration"
    assert "cost" in prompt.lower() or "ค่าใช้จ่าย" in prompt, \
        "must reference cost in the instruction"


def test_no_hard_duration_clamp_introduced(monkeypatch, tmp_path):
    """clamp_to_capabilities must NOT clamp duration to a new hard maximum as a
    substitute for agent reasoning. A user-requested 10s within provider
    supported range must remain 10s (no artificial 5s/6s/8s cap)."""
    from src import media_gen

    caps = {
        "durations": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        "aspect_ratios": ["16:9"],
        "resolutions": ["720p"],
        "pricing_skus": {"per-video-second": "0.1028"},
    }
    # User explicitly requests 10s, provider supports 10s → must stay 10s
    val, warn = media_gen.clamp_to_capabilities(10, caps, "durations", 5)
    assert val == 10, f"explicit 10s within range must not be clamped: {val}"
    assert warn is None, f"no warning for in-range value: {warn}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
