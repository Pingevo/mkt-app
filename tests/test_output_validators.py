"""Output validation per agent."""
import json

import pytest

from src.output_validators import validate_output


def _content_json() -> dict:
    return {
        "posts": [{
            "platform": "Facebook",
            "concept": "รีวิว",
            "title": "รีวิว Lagenio K3",
            "caption": "caption text",
            "script": "",
            "hashtags": "#tag",
            "image_prompts": [],
            "video_prompts": [],
            "asset_ids": [],
        }],
    }


def test_content_creator_valid_json():
    ok, err = validate_output("content_creator", json.dumps(_content_json(), ensure_ascii=False))
    assert ok is True
    assert err == ""


def test_content_creator_missing_posts():
    ok, err = validate_output("content_creator", json.dumps({"other": 1}))
    assert ok is False
    assert "posts" in err


def test_content_creator_missing_title_field():
    data = _content_json()
    del data["posts"][0]["title"]
    ok, err = validate_output("content_creator", json.dumps(data, ensure_ascii=False))
    assert ok is False
    assert "title" in err


def test_content_creator_title_wrong_type():
    data = _content_json()
    data["posts"][0]["title"] = ["first", "second"]
    ok, err = validate_output("content_creator", json.dumps(data, ensure_ascii=False))
    assert ok is False
    assert "title" in err


def test_product_spec_valid_markdown():
    text = "ชื่อสินค้า: Lagenio\nหมวดหมู่: สมาร์ทวอทช์\nUSP: แบตอึด"
    ok, err = validate_output("product_spec", text, required_sections=["ชื่อสินค้า", "หมวดหมู่", "USP"])
    assert ok is True
    assert err == ""


def test_product_spec_missing_section():
    text = "ชื่อสินค้า: Lagenio\nหมวดหมู่: สมาร์ทวอทช์"
    ok, err = validate_output("product_spec", text, required_sections=["ชื่อสินค้า", "หมวดหมู่", "USP"])
    assert ok is False
    assert "USP" in err


def test_markdown_output_empty():
    ok, err = validate_output("product_spec", "   ", required_sections=["ชื่อสินค้า"])
    assert ok is False
    assert "ว่าง" in err


def test_product_spec_bilingual_label_markdown():
    # รูปแบบที่ system_prompt ใน agents.yaml สั่งให้ LLM สร้างจริง:
    #   1. **ชื่อสินค้า (Product Name)** — ...
    # ชื่อ section ตามด้วย " (English)" ก่อนถึง ** ตัวปิด
    text = (
        "1. **ชื่อสินค้า (Product Name)** — Lagenio K3\n"
        "2. **หมวดหมู่ (Category)** — สมาร์ทวอทช์เด็ก\n"
        "3. **USP (Unique Selling Point)** — แบตอึด 14 วัน"
    )
    ok, err = validate_output(
        "product_spec", text, required_sections=["ชื่อสินค้า", "หมวดหมู่", "USP"]
    )
    assert ok is True
    assert err == ""


def test_campaign_strategy_valid_markdown():
    text = "ราคาแนะนำ: 1,000\nแคมเปญหลัก: ลดราคา\nKPI: ยอดขาย"
    ok, err = validate_output("campaign_strategy", text, required_sections=["ราคาแนะนำ", "แคมเปญหลัก", "KPI"])
    assert ok is True
    assert err == ""


def test_unknown_agent_passes():
    ok, err = validate_output("unknown", "whatever")
    assert ok is True
    assert err == ""
