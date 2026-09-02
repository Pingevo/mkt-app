"""Tests สำหรับ Phase 3 — Agent integration (Asset Library).

ทดสอบ:
- asset_library.tool_definitions() + tool_handlers() ทำงานถูก
- content_schema มี field asset_ids
- content_creator.build_prompt แปะ asset_summary ถูก
- _strip_code_fence ใน orchestrator ทำงานถูก
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# เพิ่ม project root ให้ import ได้
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_strip_code_fence():
    """_strip_code_fence ตัด markdown code fences ออกจาก LLM response."""
    from src.orchestrator import _strip_code_fence

    # กรณีมี ```json ... ```
    assert _strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    # กรณีมี ``` ... ```
    assert _strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'
    # กรณีไม่มี fence
    assert _strip_code_fence('{"a": 1}') == '{"a": 1}'
    # กรณีมี whitespace
    assert _strip_code_fence('  ```json\n{"a": 1}\n```  ') == '{"a": 1}'


def test_asset_tool_definitions():
    """asset_library.tool_definitions() คืน list_assets + get_asset_detail schemas."""
    from src.asset_library import tool_definitions
    tools = tool_definitions()
    assert len(tools) == 2
    names = {t["function"]["name"] for t in tools}
    assert names == {"list_assets", "get_asset_detail"}


def test_asset_tool_handlers_list_assets(tmp_path, monkeypatch):
    """asset_library.tool_handlers().list_assets คืน list ของ asset สั้น."""
    from src import asset_library

    # mock _db_path ให้ชี้ไป tmp_path
    db_path = tmp_path / "db.json"
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {
        "supported_formats": {"image": [".png", ".jpg"]},
        "max_file_size_mb": {"image": 10},
    })

    # สร้าง asset DB จำลอง
    db_path.write_text(json.dumps({
        "assets": [
            {"id": "a_0001", "file": "logo.png", "type": "image", "subject": "logo",
             "style": "modern", "tags": ["logo"], "description": "โลโก้",
             "path": str(tmp_path / "logo.png"), "hash": "abc", "status": "ready",
             "embedding": [0.1, 0.2]},
        ],
        "next_id": 2,
    }))

    handlers = asset_library.tool_handlers()
    result = handlers["list_assets"]()
    assert len(result) == 1
    assert result[0]["id"] == "a_0001"
    assert result[0]["file"] == "logo.png"
    assert "style" in result[0]  # ต้องมี style (consistent output)
    # ไม่ส่ง path/hash ออกไป
    assert "path" not in result[0]
    assert "hash" not in result[0]


def test_asset_tool_handlers_get_asset_detail(tmp_path, monkeypatch):
    """asset_library.tool_handlers().get_asset_detail คืน record เต็ม (ไม่มี path/hash)."""
    from src import asset_library

    db_path = tmp_path / "db.json"
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {})

    db_path.write_text(json.dumps({
        "assets": [
            {"id": "a_0001", "file": "logo.png", "type": "image", "subject": "logo",
             "tags": ["logo"], "description": "โลโก้",
             "path": str(tmp_path / "logo.png"), "hash": "abc", "status": "ready"},
        ],
        "next_id": 2,
    }))

    handlers = asset_library.tool_handlers()
    result = handlers["get_asset_detail"]("a_0001")
    assert result["id"] == "a_0001"
    assert "path" not in result
    assert "hash" not in result

    # ไม่มี asset → คืน error
    result = handlers["get_asset_detail"]("a_9999")
    assert "error" in result


def test_content_schema_has_asset_ids():
    """content_schema มี field asset_ids ใน structured output."""
    from src.content_schema import CONTENT_RESPONSE_SCHEMA
    post_schema = CONTENT_RESPONSE_SCHEMA["schema"]["properties"]["posts"]["items"]
    assert "asset_ids" in post_schema["properties"]
    assert "asset_ids" in post_schema["required"]


def test_render_posts_to_markdown_shows_asset_ids():
    """render_posts_to_markdown แสดง asset_ids ถ้ามี."""
    from src.content_schema import render_posts_to_markdown
    parsed = {
        "posts": [
            {
                "platform": "TikTok",
                "concept": "test",
                "title": "title",
                "caption": "caption",
                "script": "",
                "hashtags": "#tag",
                "image_prompts": [],
                "video_prompts": [],
                "asset_ids": ["a_0001", "a_0002"],
            }
        ]
    }
    md = render_posts_to_markdown(parsed)
    assert "a_0001" in md
    assert "a_0002" in md
    assert "วัตถุดิบแบรนด์" in md


def test_render_posts_to_markdown_no_asset_ids():
    """render_posts_to_markdown ไม่แสดง section asset_ids ถ้า array ว่าง."""
    from src.content_schema import render_posts_to_markdown
    parsed = {
        "posts": [
            {
                "platform": "TikTok", "concept": "test", "title": "t",
                "caption": "c", "script": "", "hashtags": "#t",
                "image_prompts": [], "video_prompts": [],
                "asset_ids": [],
            }
        ]
    }
    md = render_posts_to_markdown(parsed)
    assert "วัตถุดิบแบรนด์" not in md


def test_build_prompt_with_asset_summary():
    """content_creator.build_prompt แปะ asset_summary ถ้ามี."""
    from src.agents.content_creator import ContentCreatorAgent
    agent = ContentCreatorAgent.__new__(ContentCreatorAgent)
    prompt = agent.build_prompt(
        product_spec="สินค้า test",
        competitor_analysis="",
        campaign_strategy="",
        asset_summary="- ID: a_0001 | โลโก้ LAGENIO",
    )
    assert "วัตถุดิบแบรนด์" in prompt
    assert "a_0001" in prompt
    assert "asset_ids" in prompt


def test_build_prompt_without_asset_summary():
    """content_creator.build_prompt ไม่แปะ section asset ถ้าไม่มี."""
    from src.agents.content_creator import ContentCreatorAgent
    agent = ContentCreatorAgent.__new__(ContentCreatorAgent)
    prompt = agent.build_prompt(
        product_spec="สินค้า test",
        competitor_analysis="",
        campaign_strategy="",
        asset_summary="",
    )
    assert "วัตถุดิบแบรนด์" not in prompt


def test_parse_media_prompts_carries_asset_ids():
    """parse_media_prompts ส่ง asset_ids ของโพสต์ติดไปกับแต่ละ image/video item.

    Phase 4: media_gen ใช้ asset_ids ดึงรูป asset เป็น input_references.
    """
    from src.media_gen import parse_media_prompts
    content = json.dumps({
        "posts": [
            {
                "platform": "TikTok", "concept": "c", "title": "t",
                "caption": "cap", "script": "", "hashtags": "#t",
                "image_prompts": [{"prompt": "a product photo", "aspect_ratio": "9:16"}],
                "video_prompts": [{"prompt": "a product video", "duration": 8}],
                "asset_ids": ["a_0001", "a_0004"],
            }
        ]
    })
    parsed = parse_media_prompts(content)
    assert parsed["images"][0]["asset_ids"] == ["a_0001", "a_0004"]
    assert parsed["videos"][0]["asset_ids"] == ["a_0001", "a_0004"]


def test_parse_media_prompts_no_asset_ids():
    """โพสต์ไม่มี asset_ids → item ไม่มี key asset_ids (backward compat)."""
    from src.media_gen import parse_media_prompts
    content = json.dumps({
        "posts": [
            {
                "platform": "TikTok", "concept": "c", "title": "t",
                "caption": "cap", "script": "", "hashtags": "#t",
                "image_prompts": [{"prompt": "a product photo"}],
                "video_prompts": [],
                "asset_ids": [],
            }
        ]
    })
    parsed = parse_media_prompts(content)
    assert "asset_ids" not in parsed["images"][0]


def test_parse_media_prompts_regression_a4_no_crash_on_string_prompts():
    """Regression จาก A4 smoke artifact: image_prompts ที่เป็น string ต้องไม่ทำให้
    parse_media_prompts เกิด AttributeError ตอนเรียก .get() บน string."""
    from src.media_gen import parse_media_prompts
    content = json.dumps({
        "posts": [
            {
                "platform": "Facebook", "concept": "c1", "title": "t1",
                "caption": "cap1", "script": "", "hashtags": "#f",
                "image_prompts": [{"prompt": "valid prompt", "aspect_ratio": "16:9"}],
                "video_prompts": [],
            },
            {
                "platform": "TikTok", "concept": "c2", "title": "t2",
                "caption": "cap2", "script": "", "hashtags": "#t",
                # malformed: image_prompts มี string แทน dict
                "image_prompts": ["malformed string prompt"],
                "video_prompts": [],
            },
        ]
    })
    parsed = parse_media_prompts(content)
    assert len(parsed.get("images", [])) == 1
    assert parsed["images"][0]["prompt"] == "valid prompt"
    assert any("dict" in w.lower() or "expected" in w.lower() for w in parsed.get("warnings", []))


def test_build_input_references_merges_and_caps(tmp_path, monkeypatch):
    """build_input_references รวมรูปสินค้า + รูป asset และจำกัดจำนวนตาม config.

    Phase 4: ทุก call site ใช้ helper นี้ตัวเดียว — ไม่ต่อ list เอง.
    """
    from src import asset_library

    db_path = tmp_path / "db.json"
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {
        "media": {"max_refs_per_post": 3},
    })

    # สร้างรูป asset จริง 2 ไฟล์
    a1 = tmp_path / "logo.png"
    a1.write_bytes(b"png")
    a2 = tmp_path / "person.png"
    a2.write_bytes(b"png")
    db_path.write_text(json.dumps({
        "assets": [
            {"id": "a_0001", "file": "logo.png", "type": "image", "subject": "logo",
             "path": str(a1), "hash": "h1", "status": "ready"},
            {"id": "a_0002", "file": "person.png", "type": "image", "subject": "person",
             "path": str(a2), "hash": "h2", "status": "ready"},
        ],
        "next_id": 3,
    }))

    # product 2 รูป + asset 2 รูป = 4 → cap ที่ 3 (product มาก่อน)
    p1 = tmp_path / "prod1.png"
    p1.write_bytes(b"png")
    p2 = tmp_path / "prod2.png"
    p2.write_bytes(b"png")
    refs = asset_library.build_input_references(
        [str(p1), str(p2)], ["a_0001", "a_0002"],
    )
    assert len(refs) == 3
    assert refs[0] == str(p1)
    assert refs[1] == str(p2)
    assert refs[2] == str(a1)  # asset แรกที่ยังใส่ได้


def test_build_input_references_no_assets(tmp_path, monkeypatch):
    """ไม่มี asset_ids → คืนแค่รูปสินค้า (backward compat)."""
    from src import asset_library
    monkeypatch.setattr(asset_library, "_db_path", lambda: tmp_path / "db.json")
    monkeypatch.setattr(asset_library, "_load_config", lambda: {})
    refs = asset_library.build_input_references(["/tmp/p1.png"], [])
    assert refs == ["/tmp/p1.png"]


def test_build_input_references_routes_run_resource_paths(tmp_path, monkeypatch):
    """รูปแนบจาก quick brief/run context ต้องเดินทางถึง media generator แยกจาก product/asset."""
    from src import asset_library

    db_path = tmp_path / "db.json"
    asset_img = tmp_path / "brand.png"
    asset_img.write_bytes(b"png")
    db_path.write_text(json.dumps({
        "assets": [
            {"id": "a_0001", "file": "brand.png", "type": "image", "subject": "brand",
             "path": str(asset_img), "hash": "h1", "status": "ready"},
        ],
        "next_id": 2,
    }))
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")
    res_img = tmp_path / "resource.png"
    res_img.write_bytes(b"png")

    refs = asset_library.build_input_references(
        [str(prod_img)], ["a_0001"], [str(res_img)],
    )
    assert str(prod_img) in refs
    assert str(res_img) in refs
    assert str(asset_img) in refs


def test_build_input_references_ignores_unknown_res_id(tmp_path, monkeypatch):
    """res_... ไม่ควรถูกส่งเข้า asset-library resolver — ต้องถูกข้าม."""
    from src import asset_library

    db_path = tmp_path / "db.json"
    (db_path).write_text(json.dumps({"assets": [], "next_id": 1}))
    monkeypatch.setattr(asset_library, "_db_path", lambda: db_path)
    monkeypatch.setattr(asset_library, "_load_config", lambda: {"media": {"max_refs_per_post": 10}})

    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")
    res_img = tmp_path / "resource.png"
    res_img.write_bytes(b"png")

    refs = asset_library.build_input_references(
        [str(prod_img)], ["res_12345"], [str(res_img)],
    )
    assert str(prod_img) in refs
    assert str(res_img) in refs
    assert "res_12345" not in refs
    assert all(not r.startswith("res_") for r in refs)
    assert len(refs) == 2
