"""Script Reviewer — ตรวจ script หาจุดน่าเบื่อ + ให้คะแนน + เสนอ hook ใหม่.

อิงจากตลาด (Opus Clip Viral Score, PrePublish retention prediction, Retensis Script Coach):
  - ให้คะแนน 0-100 + คะแนนย่อย 4 มิติ (hook, pacing, clarity, engagement)
  - หาจุดที่คนจะเลิกดู + เสนอ hook ใหม่ 3 แบบ
  - แก้ script ก่อนส่งให้ video generator (ประหยัดเงิน gen)
  - threshold (default 70) — ถ้า score < threshold → ตรวจใหม่หลังแก้ (max 3 รอบ)

1 ฟังก์ชันหลัก:
  review_script(script, platform, llm) -> dict
  — ส่ง script ให้ LLM วิเคราะห์ → คืน {score, component_scores, issues, suggested_hooks, revised_script}
"""
from __future__ import annotations

import json
import re
from typing import Any


_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "คะแนนรวม 0-100 (สูง = ดี, น่าจะ retention ดี)",
        },
        "component_scores": {
            "type": "object",
            "properties": {
                "hook": {
                    "type": "integer",
                    "description": "คะแนน hook 0-25 (10 วินาทีแรกดึงไหม)",
                },
                "pacing": {
                    "type": "integer",
                    "description": "คะแนนจังหวะ 0-25 (ไม่เร็ว/ช้าเกิน)",
                },
                "clarity": {
                    "type": "integer",
                    "description": "คะแนนความชัดเจน 0-25 (เข้าใจง่ายไหม)",
                },
                "engagement": {
                    "type": "integer",
                    "description": "คะแนนการดูจนจบ 0-25 (มีอะไรดึงตลอดไหม)",
                },
            },
            "required": ["hook", "pacing", "clarity", "engagement"],
            "additionalProperties": False,
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string"},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["timestamp", "problem", "fix"],
                "additionalProperties": False,
            },
        },
        "suggested_hooks": {
            "type": "array",
            "items": {"type": "string"},
        },
        "revised_script": {"type": "string"},
    },
    "required": ["score", "component_scores", "issues", "suggested_hooks", "revised_script"],
    "additionalProperties": False,
}


def review_script(
    script: str,
    platform: str,
    llm: Any,
) -> dict[str, Any]:
    """ตรวจ script หาจุดน่าเบื่อ + ให้คะแนน + เสนอ hook ใหม่ → คืน review dict.

    Args:
        script: script ที่ content_creator เขียน (มี timestamp + scene + voiceover)
        platform: แพลตฟอร์มเป้าหมาย (เช่น "TikTok", "Facebook")
        llm: LLMClient instance

    คืน: {score: int, component_scores: {hook, pacing, clarity, engagement},
           issues: [{timestamp, problem, fix}], suggested_hooks: [str], revised_script: str}
    ถ้า script ว่าง → คืน {} (ไม่เรียก LLM)
    ถ้า LLM คืน JSON ไม่ valid → คืน {} (ไม่ crash)
    """
    if not script or not script.strip():
        return {}

    system_prompt = (
        "คุณเป็นนักวิเคราะห์ retention ของวิดีโอ (Audience Retention Analyst)\n"
        "หน้าที่: ตรวจ script วิดีโอ ให้คะแนน หาจุดที่คนจะเลิกดู แล้วเสนอแก้\n\n"
        "ให้คะแนน:\n"
        "1. score — คะแนนรวม 0-100 (ผลรวมของ 4 มิติข้างล่าง)\n"
        "2. component_scores — คะแนนย่อย 4 มิติ แต่ละมิติ 0-25:\n"
        "   - hook (0-25): 10 วินาทีแรกดึงคนไว้ไหม? มี curiosity gap / pattern interrupt ไหม?\n"
        "   - pacing (0-25): จังหวะเร็ว/ช้าเหมาะกับแพลตฟอร์มไหม? มีช่วงที่ลากเยอะไหม?\n"
        "   - clarity (0-25): เข้าใจง่ายไหม? มีคำซับซ้อนเกินไหม?\n"
        "   - engagement (0-25): มีอะไรดึงคนดูจนจบไหม? มี payoff ที่คนรอดูไหม?\n"
        "3. issues — จุดที่พลังตก คนน่าจะเลิกดู แต่ละจุดมี:\n"
        "   - timestamp: ช่วงเวลาที่มีปัญหา (เช่น '0:03-0:05')\n"
        "   - problem: ปัญหาอะไร (เช่น 'พูดสเปคนานเกิน', 'hook ไม่ดึง')\n"
        "   - fix: แก้ยังไง (เช่น 'ตัดเหลือ 2 วินาที', 'ใส่ pattern interrupt')\n"
        "4. suggested_hooks — hook ใหม่ 3 แบบ สำหรับ 10 วินาทีแรก\n"
        "5. revised_script — script ที่แก้แล้ว (เอา hook ที่ดีที่สุดมาใส่ + แก้จุดที่มีปัญหา)\n\n"
        f"แพลตฟอร์ม: {platform}\n"
        "คำนึงถึงลักษณะของแพลตฟอร์ม (TikTok: สั้น เร็ว, Facebook: ยาวกว่า)\n\n"
        "คืนเป็น JSON เท่านั้น"
    )

    user_prompt = f"ตรวจ script นี้:\n\n{script}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "script_review",
            "strict": True,
            "schema": _REVIEW_SCHEMA,
        },
    }

    try:
        raw = llm.chat(
            messages,
            temperature=0.3,
            max_tokens=2048,
            stream=False,
            response_format=response_format,
            source="script_reviewer.review_script",
        )
        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        return json.loads(clean)
    except (json.JSONDecodeError, TypeError, Exception):
        return {}
