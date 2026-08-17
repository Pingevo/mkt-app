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
