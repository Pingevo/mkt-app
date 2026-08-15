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

import base64
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


def _run_llm_json(
    llm: Any,
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    """ส่ง prompt ให้ LLM พร้อม Structured Outputs → คืน parsed JSON dict.

    ถ้า LLM คืน JSON ไม่ valid → คืน {} (ไม่ crash)
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
    }
    try:
        raw = llm.chat(
            messages,
            temperature=0.3,
            max_tokens=2048,
            stream=False,
            response_format=response_format,
            source=source,
        )
        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        return json.loads(clean)
    except (json.JSONDecodeError, TypeError, Exception):
        return {}


def _examples_to_text(examples: list[str]) -> str:
    """รวมตัวอย่างเป็น text block สำหรับส่งให้ LLM."""
    return "\n\n---\n\n".join(
        f"ตัวอย่างที่ {i+1}:\n{ex}" for i, ex in enumerate(examples)
    )


# ---------------------------------------------------------------------------
# Schemas สำหรับ Structured Outputs
# ---------------------------------------------------------------------------

_VOICE_SCHEMA = {
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
}

_TERMS_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "array", "items": {"type": "string"}},
        "restricted": {"type": "array", "items": {"type": "string"}},
        "replacements": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["approved", "restricted", "replacements"],
    "additionalProperties": False,
}

_AUDIENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "primary": {
            "type": "object",
            "properties": {
                "age": {"type": "string"},
                "role": {"type": "string"},
            },
            "required": ["age", "role"],
            "additionalProperties": True,
        },
        "pain_points": {"type": "array", "items": {"type": "string"}},
        "channels": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["primary", "pain_points", "channels"],
    "additionalProperties": True,
}


def analyze_voice(examples: list[str], llm: Any) -> dict[str, Any]:
    """ส่งตัวอย่างให้ LLM วิเคราะห์ → คืน voice profile dict.

    ใช้ Structured Outputs (response_format) เพื่อให้ LLM คืน JSON ที่ตรง schema
    คืน: {personality, tone_description, formality_level, language, banned_phrases, examples}
    ถ้า examples ว่าง → คืน {} (ไม่เรียก LLM)
    ถ้า LLM คืน JSON ไม่ valid → คืน {} (ไม่ crash)
    """
    if not examples:
        return {}

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
    user_prompt = f"วิเคราะห์โทนเสียงจากตัวอย่างต่อไปนี้:\n\n{_examples_to_text(examples)}"

    return _run_llm_json(
        llm, system_prompt, user_prompt,
        "voice_profile", _VOICE_SCHEMA, "voice_learner.analyze_voice",
    )


def analyze_terms(examples: list[str], llm: Any) -> dict[str, Any]:
    """ส่งตัวอย่างให้ LLM ดึงคำที่ใช้บ่อย/คำต้องห้าม → คืน terms dict.

    คืน: {approved: [str], restricted: [str], replacements: {old: new}}
    ถ้า examples ว่าง → คืน {} (ไม่เรียก LLM)
    ถ้า LLM คืน JSON ไม่ valid → คืน {} (ไม่ crash)
    """
    if not examples:
        return {}

    system_prompt = (
        "คุณเป็นนักวิเคราะห์คำศัพท์แบรนด์ (Brand Terminology Analyst)\n"
        "หน้าที่: อ่านตัวอย่างโพสต์แล้วดึงคำที่ใช้บ่อยและคำที่ควรห้ามใช้\n\n"
        "วิเคราะห์:\n"
        "1. approved — คำ/วลีที่ปรากฏบ่อยในตัวอย่าง และสะท้อนแบรนด์ (เช่น 'นวัตกรรม', 'คุณภาพ')\n"
        "2. restricted — คำที่ไม่ปรากฏในตัวอย่าง และน่าจะขัดโทนเสียง (เช่น 'ถูกที่สุด', 'ของแถม')\n"
        "3. replacements — คำที่ควรเปลี่ยน (เช่น {'ถูกมาก': 'ราคาคุ้ม'})\n\n"
        "คืนเป็น JSON เท่านั้น:\n"
        '{"approved": ["..."], "restricted": ["..."], "replacements": {"คำเก่า": "คำใหม่"}}'
    )
    user_prompt = f"ดึงคำศัพท์จากตัวอย่างต่อไปนี้:\n\n{_examples_to_text(examples)}"

    return _run_llm_json(
        llm, system_prompt, user_prompt,
        "terms_profile", _TERMS_SCHEMA, "voice_learner.analyze_terms",
    )


def analyze_audience(examples: list[str], llm: Any) -> dict[str, Any]:
    """ส่งตัวอย่างให้ LLM เดากลุ่มเป้าหมาย → คืน audience dict.

    คืน: {primary: {age, role}, pain_points: [str], channels: [str]}
    ถ้า examples ว่าง → คืน {} (ไม่เรียก LLM)
    ถ้า LLM คืน JSON ไม่ valid → คืน {} (ไม่ crash)
    """
    if not examples:
        return {}

    system_prompt = (
        "คุณเป็นนักวิเคราะห์กลุ่มเป้าหมาย (Audience Analyst)\n"
        "หน้าที่: อ่านตัวอย่างโพสต์แล้วเดาว่าแบรนด์กำลังพูดกับใคร\n\n"
        "วิเคราะห์:\n"
        "1. primary — กลุ่มเป้าหมายหลัก {age: 'ช่วงอายุ', role: 'บทบาท เช่น ผู้ปกครอง/นักธุรกิจ'}\n"
        "2. pain_points — ปัญหาที่กลุ่มเป้าหมายมี (จากบริบทในตัวอย่าง)\n"
        "3. channels — ช่องทางที่น่าจะใช้ (เช่น Facebook, TikTok, Instagram)\n\n"
        "คืนเป็น JSON เท่านั้น:\n"
        '{"primary": {"age": "...", "role": "..."}, "pain_points": ["..."], "channels": ["..."]}'
    )
    user_prompt = f"เดากลุ่มเป้าหมายจากตัวอย่างต่อไปนี้:\n\n{_examples_to_text(examples)}"

    return _run_llm_json(
        llm, system_prompt, user_prompt,
        "audience_profile", _AUDIENCE_SCHEMA, "voice_learner.analyze_audience",
    )


def analyze_brand(examples: list[str], llm: Any) -> dict[str, dict[str, Any]]:
    """วิเคราะห์ทั้ง 3 ส่วนในครั้งเดียว → คืน {voice, terms, audience}.

    ถ้าส่วนไหนพัง → คืน {} ในส่วนนั้น (ไม่ crash ทั้งระบบ)
    ถ้า examples ว่าง → คืน {} (ไม่เรียก LLM)
    """
    if not examples:
        return {}

    return {
        "voice": analyze_voice(examples, llm),
        "terms": analyze_terms(examples, llm),
        "audience": analyze_audience(examples, llm),
    }


# ---------------------------------------------------------------------------
# Video Style Analysis — วิเคราะห์สไตล์วิดีโอจากไฟล์/YouTube URL
# (Style Reverse-Engineering จาก Notion "GOODBYE CAPCUT" prompt #2)
# ---------------------------------------------------------------------------

_VIDEO_STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "pacing": {"type": "string"},
        "transitions": {"type": "array", "items": {"type": "string"}},
        "color_grading": {"type": "string"},
        "sound_design": {"type": "string"},
        "shot_duration": {"type": "string"},
        "visual_rhythm": {"type": "string"},
        "style_summary": {"type": "string"},
    },
    "required": ["pacing", "transitions", "color_grading",
                 "sound_design", "shot_duration", "visual_rhythm", "style_summary"],
    "additionalProperties": False,
}


def _video_file_to_data_url(filepath: str) -> str:
    """อ่านไฟล์วิดีโอ → แปลงเป็น base64 data URL.

    รองรับ: mp4, mpeg, mov, webm
    """
    path = Path(filepath)
    ext = path.suffix.lower().lstrip(".")
    # map ext → mime type
    mime_map = {"mp4": "video/mp4", "mpeg": "video/mpeg",
                "mov": "video/mov", "webm": "video/webm"}
    mime = mime_map.get(ext, "video/mp4")
    data = path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def analyze_video_style(
    llm: Any,
    *,
    video_url: str | None = None,
    video_path: str | None = None,
) -> dict[str, Any]:
    """วิเคราะห์สไตล์วิดีโอ → คืน video style profile dict.

    รับ input 2 แบบ:
      - video_url: YouTube URL หรือ URL วิดีโอโดยตรง (Gemini รองรับ YouTube)
      - video_path: path ของไฟล์วิดีโอ local (แปลงเป็น base64 data URL)

    คืน: {pacing, transitions, color_grading, sound_design,
           shot_duration, visual_rhythm, style_summary}
    ถ้าไม่ส่งทั้งสองอย่าง → คืน {} (ไม่เรียก LLM)
    ถ้า LLM คืน JSON ไม่ valid → คืน {} (ไม่ crash)
    """
    # หา url ที่จะส่งให้ LLM
    url_to_send: str | None = None
    if video_url:
        url_to_send = video_url
    elif video_path:
        url_to_send = _video_file_to_data_url(video_path)

    if not url_to_send:
        return {}

    system_prompt = (
        "คุณเป็นนักตัดต่อวิดีโอและนักวิเคราะห์สไตล์ภาพ (Master Video Editor & Visual Style Analyst)\n"
        "หน้าที่: วิเคราะห์วิดีโอที่ให้มา แล้วสรุปสไตล์การตัดต่อ\n\n"
        "วิเคราะห์:\n"
        "1. pacing — จังหวะการตัดต่อ (เช่น 'เร็ว', 'ช้า', 'ปานกลาง')\n"
        "2. transitions — เทคนิค transition ที่ใช้ (เช่น ['jump cut', 'zoom', 'fade'])\n"
        "3. color_grading — โทนสี (เช่น 'warm tones', 'cool, high contrast')\n"
        "4. sound_design — ดีไซน์เสียง (เช่น 'upbeat music', 'voiceover only', 'SFX heavy')\n"
        "5. shot_duration — ระยะเวลาเฉลี่ยต่อ shot (เช่น '2-3 sec', '5-8 sec')\n"
        "6. visual_rhythm — จังหวะภาพรวม (เช่น 'energetic', 'calm', 'dramatic')\n"
        "7. style_summary — สรุปสไตล์ทั้งหมดใน 1 ประโยค\n\n"
        "คืนเป็น JSON เท่านั้น ตามรูปแบบนี้:\n"
        '{"pacing": "...", "transitions": ["..."], "color_grading": "...", '
        '"sound_design": "...", "shot_duration": "...", "visual_rhythm": "...", '
        '"style_summary": "..."}'
    )

    # multimodal message: text + video_url content part
    user_content = [
        {"type": "text", "text": "วิเคราะห์สไตล์การตัดต่อของวิดีโอนี้"},
        {"type": "video_url", "video_url": {"url": url_to_send}},
    ]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "video_style_profile",
            "strict": True,
            "schema": _VIDEO_STYLE_SCHEMA,
        },
    }

    try:
        raw = llm.chat(
            messages,
            temperature=0.3,
            max_tokens=2048,
            stream=False,
            response_format=response_format,
            source="voice_learner.analyze_video_style",
        )
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
