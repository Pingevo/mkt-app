"""Voice Learning — วิเคราะห์ตัวอย่างโพสต์ → voice profile.

ตามที่ตลาดทำ (Jasper Brand Voice, Writer Voice Profile):
  user upload ตัวอย่าง (text/files/URLs) → LLM วิเคราะห์ tone → สร้าง voice profile

3 ฟังก์ชันหลัก:
  collect_examples(pasted_texts, file_paths, urls) -> list[str]
    — รวมตัวอย่างจากทุกแหล่ง (paste + upload + URL scrape)
  analyze_voice(examples, llm) -> dict
    — ส่งตัวอย่างให้ LLM วิเคราะห์ → คืน voice profile dict
  fetch_url_content(url) -> str
    — ดึง text จาก URL (httpx + regex strip HTML)

Voice profile ที่ได้จะบันทึกลง brand/voice.json ผ่าน /api/brand_json_save
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx


# จำกัดขนาด content จาก URL (ป้องกัน abuse + token เยอะ)
MAX_URL_CONTENT_LENGTH = 5000


def _is_error_content(content: str) -> bool:
    """เช็คว่า content เป็น error message จาก fetch/extract (ไม่ใช่ตัวอย่างจริง)."""
    if not content:
        return True
    prefix = content.lower()[:30]
    return "error" in prefix or "ไม่" in content[:10]


def collect_examples(
    pasted_texts: list[str] | None = None,
    file_paths: list[str] | None = None,
    urls: list[str] | None = None,
) -> list[str]:
    """รวมตัวอย่างจากทุกแหล่ง → list ของ strings.

    - pasted_texts: text ที่ user paste โดยตรง
    - file_paths: path ของไฟล์ที่ upload (.txt, .md, .pdf, .docx)
    - urls: URL ที่จะ scrape เอา text
    """
    examples: list[str] = []

    for text in (pasted_texts or []):
        text = text.strip()
        if text:
            examples.append(text)

    for fp in (file_paths or []):
        content = extract_file_text(Path(fp))
        if not _is_error_content(content):
            examples.append(content)

    for url in (urls or []):
        content = fetch_url_content(url)
        if not _is_error_content(content):
            examples.append(content)

    return examples


def analyze_voice(examples: list[str], llm: Any) -> dict[str, Any]:
    """ส่งตัวอย่างให้ LLM วิเคราะห์ → คืน voice profile dict.

    ใช้ Structured Outputs (response_format) เพื่อให้ LLM คืน JSON ที่ตรง schema
    คืน: {personality, tone_description, formality_level, language, banned_phrases, examples}
    ถ้า examples ว่าง → คืน {} (ไม่เรียก LLM)
    ถ้า LLM คืน JSON ไม่ valid → คืน {} (ไม่ crash)
    """
    if not examples:
        return {}

    # รวมตัวอย่างเป็น text block
    examples_text = "\n\n---\n\n".join(
        f"ตัวอย่างที่ {i+1}:\n{ex}" for i, ex in enumerate(examples)
    )

    system_prompt = (
        "คุณเป็นนักวิเคราะห์โทนเสียงแบรนด์ (Brand Voice Analyst)\n"
        "หน้าที่: วิเคราะห์ตัวอย่างโพสต์ที่ให้มา แล้วสรุปเป็น voice profile\n\n"
        "วิเคราะห์:\n"
        "1. บุคลิกของแบรนด์ (personality) — เช่น 'เป็นมิตร เข้าใจง่าย' หรือ 'ผู้เชี่ยวชาญ เป็นทางการ'\n"
        "2. คำอธิบายโทนเสียง (tone_description) — เช่น 'อบอุ่น เป็นกันเอง แต่น่าเชื่อถือ'\n"
        "3. ระดับความเป็นทางการ (formality_level) — 1-5 (1=ไม่เป็นทางการ, 5=เป็นทางการมาก)\n"
        "4. ภาษาที่ใช้ (language) — เช่น 'ไทยเป็นหลัก'\n"
        "5. คำ/วลีที่ห้ามใช้ (banned_phrases) — คำที่ไม่ปรากฏในตัวอย่างและน่าจะขัดโทนเสียง\n"
        "6. ตัวอย่างที่สะท้อนโทนเสียงได้ดี (examples) — เลือก 1-3 ตัวอย่างที่ดีที่สุดจากที่ให้มา\n\n"
        "คืนเป็น JSON เท่านั้น ตามรูปแบบนี้:\n"
        '{"personality": "...", "tone_description": "...", "formality_level": 3, '
        '"language": "...", "banned_phrases": ["..."], "examples": ["..."]}'
    )

    user_prompt = f"วิเคราะห์โทนเสียงจากตัวอย่างต่อไปนี้:\n\n{examples_text}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Structured Outputs schema — บังคับให้ LLM คืน JSON ที่ตรงรูปแบบ
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "voice_profile",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "personality": {"type": "string"},
                    "tone_description": {"type": "string"},
                    "formality_level": {"type": "integer"},
                    "language": {"type": "string"},
                    "banned_phrases": {"type": "array", "items": {"type": "string"}},
                    "examples": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["personality", "tone_description", "formality_level",
                             "language", "banned_phrases", "examples"],
                "additionalProperties": False,
            },
        },
    }

    try:
        raw = llm.chat(
            messages,
            temperature=0.3,
            max_tokens=2048,
            stream=False,
            response_format=response_format,
            source="voice_learner.analyze_voice",
        )
        # ลอง parse JSON — อาจมี ```json wrapper (บาง model ไม่รองรับ strict schema)
        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        return json.loads(clean)
    except (json.JSONDecodeError, TypeError, Exception):
        return {}


def fetch_url_content(url: str) -> str:
    """ดึง text จาก URL — httpx + regex strip HTML tags.

    จำกัดขนาด (max 5000 ตัวอักษร) เพื่อป้องกัน abuse + token เยอะ
    ถ้าดึงไม่ได้ (403, JS-render, network error) → คืน error message
    """
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; VoiceLearner/1.0)"
        })
        resp.raise_for_status()
        text = resp.text

        # Strip HTML tags — regex แบบง่าย (ไม่ติดตั้ง bs4)
        # 1. ลบ script/style content
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # 2. ลบ tags ทั้งหมด
        text = re.sub(r"<[^>]+>", " ", text)
        # 3. ลบ HTML entities ทั่วไป
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"&quot;", '"', text)
        text = re.sub(r"&#\d+;", " ", text)
        # 4. บีบ whitespace
        text = re.sub(r"\s+", " ", text).strip()

        # ตัดให้สั้น
        if len(text) > MAX_URL_CONTENT_LENGTH:
            text = text[:MAX_URL_CONTENT_LENGTH]

        return text

    except Exception as e:
        return f"[error] ดึงเนื้อหาจาก URL ไม่ได้: {e}"


def extract_file_text(filepath: Path) -> str:
    """อ่าน text จากไฟล์ — ใช้ file_loader.load_file() ที่มีอยู่แล้ว.

    รองรับ: .txt, .md, .pdf (PyPDF2), .docx, .xlsx
    ไม่รองรับ .png OCR (tesseract ไม่ได้ติดตั้ง) → บอก user
    """
    try:
        from .file_loader import load_file
        return load_file(filepath)
    except FileNotFoundError:
        return "[error] ไม่พบไฟล์"
    except ValueError as e:
        return f"[error] {e}"
    except Exception as e:
        return f"[error] อ่านไฟล์ไม่ได้: {e}"
