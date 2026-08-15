"""Content Pillar Manager — ระบบหมุนเวียนเสาหลักคอนเทนต์.

Content Pillars คือหมวดใหญ่ 3-5 หมวดที่แบรนด์พูดเสมอ
ระบบนับว่าแต่ละ pillar ใช้ไปกี่ครั้งแล้ว แล้วบอก LLM ให้เลือก pillar ที่ใช้น้อย

ดู CONTEXT.md สำหรับนิยามเต็มของ Content Pillar vs Concept vs Instruction
"""

from __future__ import annotations


def get_pillar_usage(history: dict, pillars: list[str]) -> dict[str, int]:
    """นับว่าแต่ละ pillar ใช้ไปกี่ครั้งแล้ว จาก content history.

    Args:
        history: content_history dict มี "entries" list
        pillars: list ของ pillar names ที่ต้องการนับ

    Returns:
        dict {pillar_name: count} — pillar ที่ไม่มีใน history ก็นับ 0
    """
    usage = {p: 0 for p in pillars}
    for entry in history.get("entries", []):
        pillar = entry.get("pillar")
        if pillar in usage:
            usage[pillar] += 1
    return usage


def build_pillar_context(pillars: list[str], usage: dict[str, int]) -> str:
    """สร้างข้อความบอก LLM ว่ามี pillar อะไรบ้าง ใช้ไปกี่ครั้ง แนะนำอะไร.

    Args:
        pillars: list ของ pillar names
        usage: dict จาก get_pillar_usage()

    Returns:
        ข้อความสำหรับใส่ใน prompt ของ auto mode
        ถ้า pillars ว่าง → คืน string ว่าง (ไม่บังคับ)
    """
    if not pillars:
        return ""

    lines = ["Content Pillars (เสาหลักคอนเทนต์) — เลือก pillar ที่ใช้น้อยเพื่อหมุนเวียน:"]
    for pillar in pillars:
        count = usage.get(pillar, 0)
        lines.append(f"  - {pillar}: ใช้ {count} ครั้ง")

    # หา pillar ที่ใช้น้อยสุด
    min_count = min(usage.get(p, 0) for p in pillars)
    least_used = [p for p in pillars if usage.get(p, 0) == min_count]
    if len(least_used) < len(pillars):
        lines.append(f"  แนะนำ: ลอง {', '.join(least_used)} (ยังไม่ค่อยได้ใช้)")
    else:
        lines.append("  ทุก pillar ยังไม่เคยใช้ — เลือกอะไรก็ได้")

    return "\n".join(lines)


def infer_pillar(concept: str, pillars: list[str], keywords_map: dict[str, list[str]]) -> str:
    """Infer pillar จาก concept text โดยใช้ keyword matching.

    ใช้เมื่อ LLM ไม่ส่ง pillar กลับมา — ระบบเดาจากคำใน concept

    Args:
        concept: concept text ที่ LLM ส่งกลับ
        pillars: list ของ pillar names ที่รองรับ
        keywords_map: dict {pillar: [keywords]} จาก config pillar_keywords

    Returns:
        pillar name ที่ match ได้ หรือ "" ถ้าไม่ match
    """
    if not concept or not pillars:
        return ""

    concept_lower = concept.lower()

    # Check 1: pillar name อยู่ใน concept ไหม
    for p in pillars:
        if p.lower() in concept_lower:
            return p

    # Check 2: keyword matching
    for p in pillars:
        keywords = keywords_map.get(p, [])
        if any(k in concept_lower for k in keywords):
            return p

    return ""
