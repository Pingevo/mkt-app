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
    from src.content_schema import CONTENT_SCHEMA
    post_schema = CONTENT_SCHEMA["schema"]["properties"]["posts"]["items"]
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
