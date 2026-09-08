"""Phase 4: media_gen wiring — ตรวจว่า asset_ids ถูกส่งเป็น input_references จริง.

ทดสอบว่า /api/generate_all_media ใช้ build_input_references รวมรูปสินค้า + รูป asset
แล้วส่งเป็น input_references ของ media_gen.generate_image / generate_video.
"""
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def _client():
    """import web_viewer ใหม่ทุกครั้ง — กัน state รั่วระหว่าง tests."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    return TestClient(web_viewer.app)


def _make_content_json(posts):
    """สร้าง content_creator structured output JSON string."""
    return json.dumps({"posts": posts}, ensure_ascii=False)


def test_generate_all_media_wires_asset_refs(_client, tmp_path, monkeypatch):
    """/api/generate_all_media ส่งรูปสินค้า + รูป asset เป็น input_references."""
    # --- สร้างรูปสินค้าจริง ---
    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")

    # --- สร้างรูป asset จริง ---
    asset_img = tmp_path / "logo.png"
    asset_img.write_bytes(b"png")

    # --- asset DB ---
    db_path = tmp_path / "asset_db.json"
    db_path.write_text(json.dumps({
        "assets": [
            {"id": "a_0001", "file": "logo.png", "type": "image",
             "subject": "logo", "path": str(asset_img), "hash": "h1",
             "status": "ready"},
        ],
        "next_id": 2,
    }))

    from src import asset_library
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {
        "media": {"max_refs_per_post": 5},
    })

    # --- content_creator output ที่มี asset_ids ---
    content = _make_content_json([{
        "platform": "Instagram", "concept": "c", "title": "t",
        "caption": "cap", "script": "", "hashtags": "#t",
        "image_prompts": [{"prompt": "a product photo with logo"}],
        "video_prompts": [],
        "asset_ids": ["a_0001"],
    }])
    content_file = tmp_path / "04_content_creator_test.json"
    content_file.write_text(content, encoding="utf-8")

    # --- mock product_db ---
    from src import product_db
    monkeypatch.setattr(
        product_db, "get_product_image_paths",
        lambda pid: [str(prod_img)],
    )

    # --- mock media_gen — capture input_references ---
    captured_kwargs: dict = {}

    def _fake_generate_image(prompt, out_path, **kwargs):
        captured_kwargs.update(kwargs)
        captured_kwargs["_prompt"] = prompt
        return {"ok": True, "path": str(out_path), "model": "test"}

    # web_viewer ใช้ `media_gen.generate_image` โดยตรง — patch ที่นั่น
    import web_viewer
    monkeypatch.setattr(web_viewer.media_gen, "generate_image", _fake_generate_image)
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    # _get_brand_visual อาจดึงจาก config — mock ให้คืน {}
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda: {})

    # --- เรียก endpoint ---
    resp = _client.post("/api/generate_all_media", json={
        "file": str(content_file),
        "auto_image": True,
        "auto_video": False,
        "product_id": "TEST",
    })
    assert resp.status_code == 200

    # --- ตรวจว่า input_references มีทั้งรูปสินค้า + รูป asset ---
    refs = captured_kwargs.get("input_references")
    assert refs is not None, "input_references ไม่ถูกส่ง"
    assert str(prod_img) in refs, "รูปสินค้าไม่อยู่ใน input_references"
    assert str(asset_img) in refs, "รูป asset ไม่อยู่ใน input_references"


def test_run_auto_wires_product_resource_and_asset_images(_client, tmp_path, monkeypatch):
    """auto flow ส่งรูปสินค้า + รูปแนบ quick brief/run + รูป asset ไป media generator แยกกัน."""
    import web_viewer
    from unittest.mock import MagicMock

    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")
    res_img = tmp_path / "resource.png"
    res_img.write_bytes(b"png")
    asset_img = tmp_path / "brand.png"
    asset_img.write_bytes(b"png")

    # asset DB
    db_path = tmp_path / "asset_db.json"
    db_path.write_text(json.dumps({
        "assets": [
            {"id": "a_0001", "file": "brand.png", "type": "image", "subject": "brand",
             "path": str(asset_img), "hash": "h1", "status": "ready"},
        ],
        "next_id": 2,
    }))
    from src import asset_library
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    # product images
    from src import product_db
    monkeypatch.setattr(product_db, "is_ready", lambda pid: True)
    monkeypatch.setattr(product_db, "get_product_image_paths", lambda pid: [str(prod_img)])

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    # fake step context with a run-resource image
    def _fake_step_context(*a, **k):
        ctx = MagicMock()
        ctx.warnings = ()
        ctx.resource_text = ""
        ctx.resource_image_paths = (str(res_img),)
        ctx.resource_trace = ()
        ctx.phase_traces = ()
        ctx.input_refs = ()
        ctx.with_quick_brief.return_value = ctx
        return ctx
    monkeypatch.setattr(web_viewer, "build_step_run_context", _fake_step_context)

    captured: dict = {}
    def _fake_generate_image_with_retry(prompt, out_path, **kwargs):
        captured.update(kwargs)
        captured["_prompt"] = prompt
        return {"ok": True, "path": str(out_path)}
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry", _fake_generate_image_with_retry)
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda: {})
    monkeypatch.setattr(web_viewer.content_history, "record_entry", lambda *a, **k: True)
    monkeypatch.setattr(web_viewer.content_history, "format_product_history_for_prompt", lambda *a, **k: "")
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file", lambda *a, **k: None)

    content = json.dumps({
        "posts": [{
            "platform": "Facebook",
            "concept": "c",
            "title": "t",
            "caption": "cap",
            "script": "",
            "hashtags": "#t",
            "image_prompts": [{"prompt": "a product photo"}],
            "video_prompts": [],
            "asset_ids": ["a_0001"],
        }],
    }, ensure_ascii=False)

    fake_orch = MagicMock()
    fake_orch._make_client.return_value = MagicMock()
    fake_orch._make_client.return_value.close = MagicMock()
    fake_orch.run_content_creator_auto.return_value = {
        "product_ids": ["AUTO1"],
        "product_id": "AUTO1",
        "concept": "c",
        "pillar": "p",
        "reason": "r",
        "content": content,
        "markdown": "md",
    }
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake_orch)

    resp = _client.post("/api/run_auto", json={
        "quick_brief": "",
        "platforms": ["facebook"],
        "media_type": "image",
        "media_when": "ask",
        "auto_image": True,
        "auto_video": False,
        "content_count": 1,
        "product_count": 1,
        "agents": ["content_creator"],
        "resource_refs": ["resource:res_12345"],
        "upload_session_id": "sess",
    })
    assert resp.status_code == 200, resp.text

    refs = captured.get("input_references")
    assert refs is not None, "input_references ไม่ถูกส่ง"
    assert str(prod_img) in refs, "รูปสินค้าไม่อยู่ใน input_references"
    assert str(res_img) in refs, "รูปแนบจาก quick brief/run ไม่อยู่ใน input_references"
    assert str(asset_img) in refs, "รูป asset ไม่อยู่ใน input_references"
    assert all(not r.startswith("res_") for r in refs), "run-resource ID ห้ามปรากฏใน input_references"


def test_generate_all_media_recovers_run_resource_images_from_meta(_client, tmp_path, monkeypatch):
    """generate-later path อ่าน _session_meta.json แล้ว recover รูปแนบไป media generator."""
    import web_viewer

    # สร้าง output session พร้อม content + _session_meta
    output_dir = tmp_path / "session"
    output_dir.mkdir(parents=True)

    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")
    res_img = tmp_path / "resource.png"
    res_img.write_bytes(b"png")
    asset_img = tmp_path / "brand.png"
    asset_img.write_bytes(b"png")

    content = json.dumps({
        "posts": [{
            "platform": "Facebook", "concept": "c", "title": "t",
            "caption": "cap", "script": "", "hashtags": "#t",
            "image_prompts": [{"prompt": "a product photo"}],
            "video_prompts": [],
            "asset_ids": ["a_0001"],
        }],
    }, ensure_ascii=False)
    content_file = output_dir / "04_content_creator_TEST.json"
    content_file.write_text(content, encoding="utf-8")

    # _session_meta.json source of truth = resource_refs + upload_session_id
    (output_dir / "_session_meta.json").write_text(json.dumps({
        "product_id": "TEST",
        "resource_refs": ["resource:res_12345"],
        "upload_session_id": "sess",
    }, ensure_ascii=False), encoding="utf-8")

    # asset DB
    db_path = tmp_path / "asset_db.json"
    db_path.write_text(json.dumps({
        "assets": [
            {"id": "a_0001", "file": "brand.png", "type": "image", "subject": "brand",
             "path": str(asset_img), "hash": "h1", "status": "ready"},
        ],
        "next_id": 2,
    }))
    from src import asset_library
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    # product images
    from src import product_db
    monkeypatch.setattr(product_db, "get_product_image_paths", lambda pid: [str(prod_img)])

    # resolve resource images via build_step_run_context
    from unittest.mock import MagicMock
    def _fake_build_step_run_context(*a, **k):
        ctx = MagicMock()
        ctx.warnings = ()
        ctx.resource_image_paths = (str(res_img),)
        return ctx
    monkeypatch.setattr(web_viewer, "build_step_run_context", _fake_build_step_run_context)

    # capture input_references
    captured: dict = {}
    def _fake_generate_image(prompt, out_path, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "path": str(out_path)}
    monkeypatch.setattr(web_viewer.media_gen, "generate_image", _fake_generate_image)
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda: {})

    resp = _client.post("/api/generate_all_media", json={
        "file": str(content_file),
        "auto_image": True,
        "auto_video": False,
    })
    assert resp.status_code == 200, resp.text

    refs = captured.get("input_references")
    assert refs is not None, "input_references ไม่ถูกส่ง"
    assert str(prod_img) in refs
    assert str(res_img) in refs
    assert str(asset_img) in refs
    assert all(not r.startswith("res_") for r in refs)


def test_generate_all_media_rejects_resource_refs_without_session(_client, tmp_path, monkeypatch):
    """endpoint ต้อง reject ถ้าส่ง resource_refs มาแต่ไม่มี upload_session_id."""
    import web_viewer
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    # ต้องเป็นไฟล์จริง เพราะ endpoint จะ reject หลังหาไฟล์เจอ
    content_file = tmp_path / "04_content_creator.json"
    content_file.write_text("{}", encoding="utf-8")

    resp = _client.post("/api/generate_all_media", json={
        "file": str(content_file),
        "auto_image": True,
        "resource_refs": ["resource:res_12345"],
        "upload_session_id": "",
    })
    assert resp.status_code == 400
    assert "upload_session_id" in resp.text


def test_run_flows_wires_product_resource_and_asset_images(_client, tmp_path, monkeypatch):
    """regular flow (api_run_flows) ส่งรูปสินค้า + รูปแนบ + รูป asset ไป media generator."""
    import web_viewer
    from unittest.mock import MagicMock

    product_dir = tmp_path / "data" / "K2"
    product_dir.mkdir(parents=True)
    (product_dir / "info.txt").write_text("product info", encoding="utf-8")

    # _read_folder สร้าง image_paths จากไฟล์ใน data/{folder}/
    prod_img = product_dir / "product.png"
    prod_img.write_bytes(b"png")
    res_img = tmp_path / "resource.png"
    res_img.write_bytes(b"png")
    asset_img = tmp_path / "brand.png"
    asset_img.write_bytes(b"png")

    # asset DB
    db_path = tmp_path / "asset_db.json"
    db_path.write_text(json.dumps({
        "assets": [
            {"id": "a_0001", "file": "brand.png", "type": "image", "subject": "brand",
             "path": str(asset_img), "hash": "h1", "status": "ready"},
        ],
        "next_id": 2,
    }))
    from src import asset_library
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    # product images
    from src import product_db
    monkeypatch.setattr(product_db, "is_ready", lambda pid: True)
    monkeypatch.setattr(product_db, "get_product_image_paths", lambda pid: [str(prod_img)])

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    # fake step context with a run-resource image
    def _fake_step_context(*a, **k):
        ctx = MagicMock()
        ctx.warnings = ()
        ctx.resource_text = ""
        ctx.resource_image_paths = (str(res_img),)
        ctx.resource_trace = ()
        ctx.phase_traces = (MagicMock(as_dict=lambda: {}),)
        ctx.input_refs = ()
        ctx.with_quick_brief.return_value = ctx
        ctx.with_agent_key.return_value = ctx
        ctx.with_phase.return_value = ctx
        return ctx
    monkeypatch.setattr(web_viewer, "build_step_run_context", _fake_step_context)

    captured: dict = {}
    def _fake_generate_image_with_retry(prompt, out_path, **kwargs):
        captured.update(kwargs)
        captured["_prompt"] = prompt
        return {"ok": True, "path": str(out_path)}
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry", _fake_generate_image_with_retry)
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda: {})
    monkeypatch.setattr(web_viewer.content_history, "record_entry", lambda *a, **k: True)
    monkeypatch.setattr(web_viewer.content_history, "format_product_history_for_prompt", lambda *a, **k: "")
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file", lambda *a, **k: None)

    content = json.dumps({
        "posts": [{
            "platform": "Facebook", "concept": "c", "title": "t",
            "caption": "cap", "script": "", "hashtags": "#t",
            "image_prompts": [{"prompt": "a product photo"}],
            "video_prompts": [],
            "asset_ids": ["a_0001"],
        }],
    }, ensure_ascii=False)

    fake_orch = MagicMock()
    fake_orch._make_client.return_value = MagicMock()
    fake_orch._make_client.return_value.close = MagicMock()
    fake_orch.save_result.return_value = {"content_creator": str(tmp_path / "out.md")}
    fake_orch._run_content_creator_raw.return_value = content
    fake_orch._review_script_in_posts = MagicMock(return_value={})
    fake_orch._finalize_content_output.return_value = (content, content)
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake_orch)

    resp = _client.post("/api/run_flows", json={
        "quick_brief": "",
        "flows": [{
            "index": 0,
            "folders": ["K2"],
            "agents": ["content_creator"],
            "is_auto": False,
            "content_count": 1,
            "platforms": ["facebook"],
            "media_type": "image",
            "media_when": "ask",
            "auto_image": True,
            "auto_video": False,
            "resource_refs": ["resource:res_12345"],
            "upload_session_id": "sess",
        }],
    })
    assert resp.status_code == 200, resp.text

    refs = captured.get("input_references")
    assert refs is not None, "input_references ไม่ถูกส่ง"
    assert str(prod_img) in refs
    assert str(res_img) in refs
    assert str(asset_img) in refs
    assert all(not r.startswith("res_") for r in refs)


def test_generate_all_media_explicit_refs_resolve_resource(_client, tmp_path, monkeypatch):
    """explicit resource_refs + upload_session_id ใน request resolve ได้โดยตรง."""
    import web_viewer
    from unittest.mock import MagicMock

    output_dir = tmp_path / "session"
    output_dir.mkdir(parents=True)

    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")
    res_img = tmp_path / "resource.png"
    res_img.write_bytes(b"png")
    asset_img = tmp_path / "brand.png"
    asset_img.write_bytes(b"png")

    content = json.dumps({
        "posts": [{
            "platform": "Facebook", "concept": "c", "title": "t",
            "caption": "cap", "script": "", "hashtags": "#t",
            "image_prompts": [{"prompt": "a product photo"}],
            "video_prompts": [],
            "asset_ids": ["a_0001"],
        }],
    }, ensure_ascii=False)
    content_file = output_dir / "04_content_creator_TEST.json"
    content_file.write_text(content, encoding="utf-8")

    # asset DB
    db_path = tmp_path / "asset_db.json"
    db_path.write_text(json.dumps({
        "assets": [
            {"id": "a_0001", "file": "brand.png", "type": "image", "subject": "brand",
             "path": str(asset_img), "hash": "h1", "status": "ready"},
        ],
        "next_id": 2,
    }))
    from src import asset_library
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    from src import product_db
    monkeypatch.setattr(product_db, "get_product_image_paths", lambda pid: [str(prod_img)])

    def _fake_build_step_run_context(*a, **k):
        ctx = MagicMock()
        ctx.warnings = ()
        ctx.resource_image_paths = (str(res_img),)
        return ctx
    monkeypatch.setattr(web_viewer, "build_step_run_context", _fake_build_step_run_context)

    captured: dict = {}
    def _fake_generate_image(prompt, out_path, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "path": str(out_path)}
    monkeypatch.setattr(web_viewer.media_gen, "generate_image", _fake_generate_image)
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda: {})

    resp = _client.post("/api/generate_all_media", json={
        "file": str(content_file),
        "auto_image": True,
        "auto_video": False,
        "resource_refs": ["resource:res_12345"],
        "upload_session_id": "sess",
    })
    assert resp.status_code == 200, resp.text

    refs = captured.get("input_references")
    assert refs is not None
    assert str(res_img) in refs


def test_generate_all_media_400_when_session_resource_expired(_client, tmp_path, monkeypatch):
    """generate-later ที่ resource หมดอายุ/ถูกลบต้อง reject 400 และไม่เรียก media generator."""
    import web_viewer
    from unittest.mock import MagicMock

    output_dir = tmp_path / "session"
    output_dir.mkdir(parents=True)

    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")
    asset_img = tmp_path / "brand.png"
    asset_img.write_bytes(b"png")

    content = json.dumps({
        "posts": [{
            "platform": "Facebook", "concept": "c", "title": "t",
            "caption": "cap", "script": "", "hashtags": "#t",
            "image_prompts": [{"prompt": "a product photo"}],
            "video_prompts": [],
            "asset_ids": ["a_0001"],
        }],
    }, ensure_ascii=False)
    content_file = output_dir / "04_content_creator_TEST.json"
    content_file.write_text(content, encoding="utf-8")

    (output_dir / "_session_meta.json").write_text(json.dumps({
        "product_id": "TEST",
        "resource_refs": ["resource:res_expired"],
        "upload_session_id": "expired",
    }, ensure_ascii=False), encoding="utf-8")

    db_path = tmp_path / "asset_db.json"
    db_path.write_text(json.dumps({
        "assets": [
            {"id": "a_0001", "file": "brand.png", "type": "image", "subject": "brand",
             "path": str(asset_img), "hash": "h1", "status": "ready"},
        ],
        "next_id": 2,
    }))
    from src import asset_library
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    from src import product_db
    monkeypatch.setattr(product_db, "get_product_image_paths", lambda pid: [str(prod_img)])

    def _fake_build_step_run_context(*a, **k):
        ctx = MagicMock()
        ctx.warnings = ("missing or invalid resource refs: resource:res_expired",)
        ctx.resource_image_paths = ()
        return ctx
    monkeypatch.setattr(web_viewer, "build_step_run_context", _fake_build_step_run_context)

    media_called = []
    def _fake_generate_image(prompt, out_path, **kwargs):
        media_called.append(True)
        return {"ok": True, "path": str(out_path)}
    monkeypatch.setattr(web_viewer.media_gen, "generate_image", _fake_generate_image)
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda: {})

    resp = _client.post("/api/generate_all_media", json={
        "file": str(content_file),
        "auto_image": True,
        "auto_video": False,
    })
    assert resp.status_code == 400
    assert "resource preflight failed" in resp.text
    assert not media_called, "media generator ต้องไม่ถูกเรียกเมื่อ resource resolve ไม่ได้"


def test_script_review_regenerates_video_prompts_with_inner_schema():
    """script review สร้าง video_prompts ใหม่ต้องส่ง inner schema ไม่ใช่ envelope."""
    from src.orchestrator import Orchestrator
    from src.content_schema import CONTENT_RESPONSE_SCHEMA
    from src import script_reviewer, config_loader
    import json
    from unittest.mock import MagicMock

    def _fake_review(script, platform, llm=None, source_context=""):
        return {
            "score": 50,
            "revised_script": "revised " + script,
            "issues": ["issue"],
            "suggested_hooks": [],
        }

    original_review = script_reviewer.review_script
    original_get_agent = config_loader.get_agent_config
    try:
        script_reviewer.review_script = _fake_review
        config_loader.get_agent_config = lambda cfg, name: {
            "system_prompt": "", "temperature": 0.9, "max_tokens": 4096,
        }

        llm = MagicMock()
        llm.chat.return_value = json.dumps({
            "posts": [{"video_prompts": [{"prompt": "new video prompt"}]}]
        })

        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {}
        posts = [{"script": "original script", "video_prompts": [], "platform": "TikTok"}]
        orch._review_script_in_posts(posts, "TikTok", llm=llm)

        assert llm.chat.called
        _, kwargs = llm.chat.call_args
        sent_schema = kwargs["response_format"]["json_schema"]["schema"]
        assert sent_schema == CONTENT_RESPONSE_SCHEMA["schema"]
        assert "name" not in sent_schema
        assert "strict" not in sent_schema
    finally:
        script_reviewer.review_script = original_review
        config_loader.get_agent_config = original_get_agent


# ---------------------------------------------------------------------------
# Media resource preflight — fatal on non-ready referenced resources
#
# The /api/generate_all_media endpoint must not silently proceed when a
# user-referenced resource is not ready (rejected, parser error, missing).
# This is the same product invariant as the agent execution seam: if the
# user explicitly selects a resource, the system must not silently proceed
# as though that resource were usable.
# ---------------------------------------------------------------------------


def test_media_preflight_accepts_ready_resource(_client, tmp_path, monkeypatch):
    """A ready referenced image resource must be accepted — media generation
    proceeds normally."""
    import web_viewer
    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")
    res_img = tmp_path / "ready.png"
    res_img.write_bytes(b"png")

    content_file = tmp_path / "output" / "content.md"
    content_file.parent.mkdir(parents=True)
    content_file.write_text(json.dumps({"posts": [{"image_prompts": [{"prompt": "test"}]}]}, ensure_ascii=False), encoding="utf-8")

    from src import product_db
    monkeypatch.setattr(product_db, "get_product_image_paths", lambda pid: [str(prod_img)])

    def _fake_build_step_run_context(*a, **k):
        ctx = MagicMock()
        ctx.warnings = ()
        ctx.resource_image_paths = (str(res_img),)
        return ctx
    monkeypatch.setattr(web_viewer, "build_step_run_context", _fake_build_step_run_context)

    media_called = []
    monkeypatch.setattr(web_viewer.media_gen, "generate_image", lambda *a, **k: media_called.append(True) or {"ok": True, "path": str(res_img)})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda: {})

    resp = _client.post("/api/generate_all_media", json={
        "file": str(content_file),
        "auto_image": True,
        "auto_video": False,
        "resource_refs": ["resource:res_ready"],
        "upload_session_id": "session1",
    })
    assert resp.status_code in (200, 202)


def test_media_preflight_blocks_non_ready_resource(_client, tmp_path, monkeypatch):
    """A non-ready referenced resource must block media generation before
    any media call. The media generator mock must not be called."""
    import web_viewer
    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")

    content_file = tmp_path / "output" / "content.md"
    content_file.parent.mkdir(parents=True)
    content_file.write_text(json.dumps({"posts": [{"image_prompts": [{"prompt": "test"}]}]}, ensure_ascii=False), encoding="utf-8")

    from src import product_db
    monkeypatch.setattr(product_db, "get_product_image_paths", lambda pid: [str(prod_img)])

    def _fake_build_step_run_context(*a, **k):
        ctx = MagicMock()
        ctx.warnings = ("referenced resource(s) not ready: resource:res_bad",)
        ctx.resource_image_paths = ()
        return ctx
    monkeypatch.setattr(web_viewer, "build_step_run_context", _fake_build_step_run_context)

    media_called = []
    monkeypatch.setattr(web_viewer.media_gen, "generate_image", lambda *a, **k: media_called.append(True) or {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda: {})

    resp = _client.post("/api/generate_all_media", json={
        "file": str(content_file),
        "auto_image": True,
        "auto_video": False,
        "resource_refs": ["resource:res_bad"],
        "upload_session_id": "session1",
    })
    assert resp.status_code == 400
    assert "resource preflight failed" in resp.text
    assert not media_called, "media generator must not be called after fatal resource preflight"


def test_media_preflight_blocks_non_ready_session_meta_resource(_client, tmp_path, monkeypatch):
    """When recovering resource refs from session metadata, a non-ready
    resource must also block media generation."""
    import web_viewer
    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")

    output_dir = tmp_path / "output"
    content_file = output_dir / "content.md"
    output_dir.mkdir(parents=True)
    content_file.write_text(json.dumps({"posts": [{"image_prompts": [{"prompt": "test"}]}]}, ensure_ascii=False), encoding="utf-8")
    # Session metadata with resource refs
    (output_dir / "_session_meta.json").write_text(json.dumps({
        "resource_refs": ["resource:res_bad"],
        "upload_session_id": "session1",
    }, ensure_ascii=False), encoding="utf-8")

    from src import product_db
    monkeypatch.setattr(product_db, "get_product_image_paths", lambda pid: [str(prod_img)])

    def _fake_build_step_run_context(*a, **k):
        ctx = MagicMock()
        ctx.warnings = ("referenced resource(s) not ready: resource:res_bad",)
        ctx.resource_image_paths = ()
        return ctx
    monkeypatch.setattr(web_viewer, "build_step_run_context", _fake_build_step_run_context)

    media_called = []
    monkeypatch.setattr(web_viewer.media_gen, "generate_image", lambda *a, **k: media_called.append(True) or {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda: {})

    resp = _client.post("/api/generate_all_media", json={
        "file": str(content_file),
        "auto_image": True,
        "auto_video": False,
    })
    assert resp.status_code == 400
    assert "resource preflight failed" in resp.text
    assert not media_called


def test_media_preflight_zero_resources_remains_valid(_client, tmp_path, monkeypatch):
    """Zero referenced resources must remain a valid path — media generation
    proceeds with product images only."""
    import web_viewer
    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")

    content_file = tmp_path / "output" / "content.md"
    content_file.parent.mkdir(parents=True)
    content_file.write_text(json.dumps({"posts": [{"image_prompts": [{"prompt": "test"}]}]}, ensure_ascii=False), encoding="utf-8")

    from src import product_db
    monkeypatch.setattr(product_db, "get_product_image_paths", lambda pid: [str(prod_img)])

    media_called = []
    monkeypatch.setattr(web_viewer.media_gen, "generate_image", lambda *a, **k: media_called.append(True) or {"ok": True, "path": str(prod_img)})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer, "_get_brand_visual", lambda: {})

    resp = _client.post("/api/generate_all_media", json={
        "file": str(content_file),
        "auto_image": True,
        "auto_video": False,
    })
    assert resp.status_code in (200, 202)
