"""Output validation per agent.

Tests ครอบคลุม heading formats ทุกแบบที่ LLM สร้างจริง:
  - ## Section (markdown heading)
  - ### 1. Section (numbered markdown heading)
  - **1. Section** (bold numbered)
  - **Section** (bold)
  - 1. **Section** (numbered bold)
  - Section: (colon suffix)
"""
import json

import pytest

from src.output_validators import validate_content_output, validate_output


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


def _content_json_with_script_review() -> dict:
    data = _content_json()
    data["posts"][0]["script_review"] = {
        "status": "reviewed",
        "reviewed_at": "2026-08-27T10:00:00",
        "score": 85,
        "iterations": 1,
        "threshold": 70,
        "script_changed": False,
        "issues_count": 2,
        "hooks_count": 3,
        "review": {"score": 85, "issues": []},
    }
    return data


def test_content_creator_valid_json():
    ok, err = validate_output("content_creator", json.dumps(_content_json(), ensure_ascii=False))
    assert ok is True
    assert err == ""


def test_content_with_script_review_validates():
    """Final saved artifact with script_review field must pass strict CONTENT_ARTIFACT_SCHEMA."""
    ok, err = validate_content_output(_content_json_with_script_review())
    assert ok is True, err
    assert err == ""


def test_content_string_with_script_review_validates():
    """validate_content_output accepts the raw JSON string saved to disk."""
    ok, err = validate_content_output(json.dumps(_content_json_with_script_review(), ensure_ascii=False))
    assert ok is True, err
    assert err == ""


def test_content_without_script_review_still_validates():
    """Backward compatibility: content without script_review is still valid."""
    ok, err = validate_content_output(_content_json())
    assert ok is True, err
    assert err == ""


def test_content_with_bad_script_review_type_fails():
    """script_review must be an object — not any other type."""
    data = _content_json()
    data["posts"][0]["script_review"] = "not an object"
    ok, err = validate_content_output(data)
    assert ok is False
    assert "script_review" in err


def test_raw_response_schema_has_no_script_review():
    """โมเดลต้องตอบตาม response schema — ห้ามมี script_review ใน raw response."""
    from src.content_schema import CONTENT_RESPONSE_SCHEMA
    post = CONTENT_RESPONSE_SCHEMA["schema"]["properties"]["posts"]["items"]
    assert "script_review" not in post["properties"]
    assert "additionalProperties" in post
    assert post["additionalProperties"] is False


def test_artifact_schema_allows_script_review():
    """ไฟล์ save หลัง review อนุญาต script_review เป็น optional derived field."""
    from src.content_schema import CONTENT_ARTIFACT_SCHEMA
    post = CONTENT_ARTIFACT_SCHEMA["schema"]["properties"]["posts"]["items"]
    assert "script_review" in post["properties"]
    assert "script_review" not in post["required"]
    assert post["additionalProperties"] is False


def test_validate_output_rejects_script_review_in_raw_response():
    """validate_output สำหรับ content_creator raw output ต้องปฏิเสธ script_review."""
    data = _content_json()
    data["posts"][0]["script_review"] = {
        "status": "reviewed", "reviewed_at": "2026-08-27T10:00:00",
        "score": 80, "iterations": 1, "threshold": 70,
        "script_changed": False, "issues_count": 0, "hooks_count": 0,
        "review": {},
    }
    ok, err = validate_output("content_creator", json.dumps(data, ensure_ascii=False))
    assert ok is False
    assert "script_review" in err


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


# ---------------------------------------------------------------------------
# Markdown heading format coverage — รองรับทุก format ที่ LLM สร้างจริง
# ---------------------------------------------------------------------------

_REQUIRED = ["ภาพรวมตลาด", "ตารางเปรียบเทียบ", "จุดแข็งของเรา vs คู่แข่ง"]


def test_markdown_output_empty():
    ok, err = validate_output("product_spec", "   ", required_sections=["ชื่อสินค้า"])
    assert ok is False
    assert "ว่าง" in err


def test_heading_format_markdown_h2():
    """## Section"""
    text = (
        "## ภาพรวมตลาด\nbody\n"
        "## ตารางเปรียบเทียบ\nbody\n"
        "## จุดแข็งของเรา vs คู่แข่ง\nbody\n"
    )
    ok, err = validate_output("competitor_analysis", text, required_sections=_REQUIRED)
    assert ok is True, err


def test_heading_format_markdown_h3_numbered():
    """### 1. Section (Gemini 3.5 Flash สร้างแบบนี้จริง)"""
    text = (
        "### 1. ภาพรวมตลาด (Market Overview)\nbody\n"
        "### 2. ตารางเปรียบเทียบ (Comparison Table)\nbody\n"
        "### 3. จุดแข็งของเรา vs คู่แข่ง (Our Strengths)\nbody\n"
    )
    ok, err = validate_output("competitor_analysis", text, required_sections=_REQUIRED)
    assert ok is True, err


def test_heading_format_bold_numbered():
    """**1. Section** (Gemini 3.7 Flash สร้างแบบนี้จริง)"""
    text = (
        "**1. ภาพรวมตลาด (Market Overview)**\nbody\n"
        "**2. ตารางเปรียบเทียบ (Comparison Table)**\nbody\n"
        "**3. จุดแข็งของเรา vs คู่แข่ง**\nbody\n"
    )
    ok, err = validate_output("competitor_analysis", text, required_sections=_REQUIRED)
    assert ok is True, err


def test_heading_format_bold_only():
    """**Section**"""
    text = (
        "**ภาพรวมตลาด**\nbody\n"
        "**ตารางเปรียบเทียบ**\nbody\n"
        "**จุดแข็งของเรา vs คู่แข่ง**\nbody\n"
    )
    ok, err = validate_output("competitor_analysis", text, required_sections=_REQUIRED)
    assert ok is True, err


def test_heading_format_numbered_bold():
    """1. **Section**"""
    text = (
        "1. **ภาพรวมตลาด (Product Overview)**\nbody\n"
        "2. **ตารางเปรียบเทียบ**\nbody\n"
        "3. **จุดแข็งของเรา vs คู่แข่ง**\nbody\n"
    )
    ok, err = validate_output("competitor_analysis", text, required_sections=_REQUIRED)
    assert ok is True, err


def test_heading_format_colon_suffix():
    """Section:"""
    text = (
        "ภาพรวมตลาด:\nbody\n"
        "ตารางเปรียบเทียบ:\nbody\n"
        "จุดแข็งของเรา vs คู่แข่ง:\nbody\n"
    )
    ok, err = validate_output("competitor_analysis", text, required_sections=_REQUIRED)
    assert ok is True, err


def test_heading_format_bilingual_label():
    """1. **ชื่อสินค้า (Product Name)** — ..."""
    text = (
        "1. **ชื่อสินค้า (Product Name)** — Lagenio K3\n"
        "2. **หมวดหมู่ (Category)** — สมาร์ทวอทช์เด็ก\n"
        "3. **USP (Unique Selling Point)** — แบตอึด 14 วัน"
    )
    ok, err = validate_output(
        "product_spec", text, required_sections=["ชื่อสินค้า", "หมวดหมู่", "USP"]
    )
    assert ok is True, err


def test_heading_format_plain_text_bilingual():
    """ภาพรวมตลาด (Market Overview) — plain text, ไม่มี # ไม่มี **

    กรณีจริงที่ gemini-3.5-flash สร้าง: section name + (English label) บนบรรทัดเดียว
    ไม่มี markdown decoration ใดๆ — validator เดิมมองไม่เห็น ทำให้ validation fail
    แล้ว repair คืนว่าง เผาเครดิต 3 รอบ
    """
    text = (
        "ภาพรวมตลาด (Market Overview)\n"
        "ตลาดสมาร์ทวอทช์เด็กในปัจจุบันมุ่งเน้นไปที่ความปลอดภัย\n\n"
        "ตารางเปรียบเทียบ (Comparison Table)\n"
        "คุณสมบัติ | สินค้าของเรา | คู่แข่ง A\n\n"
        "จุดแข็งของเรา vs คู่แข่ง (Our Strengths vs Competitors)\n"
        "1. จอใหญ่\n\n"
        "จุดอ่อนของเรา vs คู่แข่ง (Our Weaknesses vs Competitors)\n"
        "1. ไม่มี Bluetooth\n\n"
        "ช่องว่างในตลาด (Market Gaps)\n"
        "1. ตลาดสายสุขภาพ\n\n"
        "ภัยคุกคาม (Threats)\n"
        "1. แบรนด์ใหญ่\n\n"
        "คำแนะนำเชิงกลยุทธ์ (Strategic Recommendations)\n"
        "1. วางตำแหน่งพรีเมียม\n\n"
        "แหล่งอ้างอิง (Sources)\n"
        "- [imoo](https://imoo.com)"
    )
    ok, err = validate_output("competitor_analysis", text, required_sections=_REQUIRED)
    assert ok is True, err


def test_missing_section_still_fails():
    """ถ้าขาด section จริง ต้อง fail ไม่ใช่ผ่านหมด"""
    text = (
        "## ภาพรวมตลาด\nbody\n"
        "## ตารางเปรียบเทียบ\nbody\n"
        # ขาด จุดแข็งของเรา vs คู่แข่ง
    )
    ok, err = validate_output("competitor_analysis", text, required_sections=_REQUIRED)
    assert ok is False
    assert "จุดแข็งของเรา vs คู่แข่ง" in err


def test_no_false_positive_on_inline_text():
    """ประโยคธรรมดาที่บังเอิญมีคำว่า section ต้องไม่นับเป็น heading"""
    text = (
        "วันนี้เราจะมาพูดถึงภาพรวมตลาดสมาร์ทวอทช์เด็กกันครับ "
        "ซึ่งตารางเปรียบเทียบจะแสดงให้เห็นว่าจุดแข็งของเรา vs คู่แข่งนั้นชัดเจน"
    )
    ok, err = validate_output("competitor_analysis", text, required_sections=_REQUIRED)
    assert ok is False
    # ต้องบอกว่าขาด section ไม่ใช่ผ่าน
    assert "ไม่พบ section" in err


def test_no_false_positive_on_parenthetical_prose():
    """วงเล็บในประโยคที่ขึ้นต้นด้วยชื่อ section ต้องไม่นับเป็น heading."""
    text = "ภาพรวมตลาด (เฉพาะในประเทศไทย) กำลังเติบโตอย่างรวดเร็ว"
    ok, err = validate_output(
        "competitor_analysis", text, required_sections=["ภาพรวมตลาด"]
    )
    assert ok is False
    assert "ไม่พบ section: ภาพรวมตลาด" in err


def test_campaign_strategy_valid_markdown():
    text = "ราคาแนะนำ: 1,000\nแคมเปญหลัก: ลดราคา\nKPI: ยอดขาย"
    ok, err = validate_output("campaign_strategy", text, required_sections=["ราคาแนะนำ", "แคมเปญหลัก", "KPI"])
    assert ok is True
    assert err == ""


def test_unknown_agent_passes():
    ok, err = validate_output("unknown", "whatever")
    assert ok is True
    assert err == ""
