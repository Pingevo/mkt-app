"""Load brand configuration from a directory — แยก 3 ชั้น (rules / reference / visual).

โครงไฟล์ใหม่:
  brand/voice.json     — โทนเสียง + บุคลิก + ตัวอย่าง (rules → system prompt)
  brand/terms.json     — คำอนุมัติ / คำต้องห้าม (hard rules → system prompt)
  brand/profile.md     — ประวัติแบรนด์ (reference — ดึงตาม relevance)
  brand/audience.json  — กลุ่มเป้าหมาย (reference — structured)
  brand/visual.json    — สี สไตล์ keyword สำหรับ image prompt (structured → media_gen)

3 ฟังก์ชันแยกตามชั้น:
  load_brand_rules(brand_dir) -> str     — voice.json + terms.json → rules string
  load_brand_reference(brand_dir) -> str — profile.md + audience.json → reference string
  load_brand_visual(brand_dir) -> dict   — visual.json → dict สำหรับ media_gen

Backward compat: load_brand_context() ยังทำงาน — delegate ไป load_brand_rules
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    """อ่าน JSON file — คืน {} ถ้าไม่มีไฟล์หรือ parse ไม่ได้."""
    if not path.exists() or not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _read_text(path: Path) -> str:
    """อ่าน text file — คืน '' ถ้าไม่มีไฟล์."""
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _format_voice(voice: dict[str, Any]) -> str:
    """แปลง voice.json dict → string สำหรับใส่ใน system prompt."""
    parts: list[str] = []
    personality = voice.get("personality", "")
    if personality:
        parts.append(f"บุคลิก: {personality}")
    tone = voice.get("tone_description", "")
    if tone:
        parts.append(f"โทนเสียง: {tone}")
    formality = voice.get("formality_level")
    if formality is not None:
        parts.append(f"ระดับความเป็นทางการ: {formality}/5")
    language = voice.get("language", "")
    if language:
        parts.append(f"ภาษา: {language}")
    banned = voice.get("banned_phrases", [])
    if banned:
        parts.append("คำ/วลีที่ห้ามใช้: " + ", ".join(banned))
    examples = voice.get("examples", [])
    if examples:
        ex_text = "\n".join(f"  - {ex}" for ex in examples)
        parts.append(f"ตัวอย่างโพสต์ที่ใช่:\n{ex_text}")
    return "\n".join(parts)


def _format_terms(terms: dict[str, Any]) -> str:
    """แปลง terms.json dict → string สำหรับใส่ใน system prompt."""
    parts: list[str] = []
    approved = terms.get("approved", [])
    if approved:
        parts.append("คำที่อนุมัติ: " + ", ".join(approved))
    restricted = terms.get("restricted", [])
    if restricted:
        parts.append("คำต้องห้าม: " + ", ".join(restricted))
    replacements = terms.get("replacements", {})
    if replacements:
        rep_text = "\n".join(f"  {old} → {new}" for old, new in replacements.items())
        parts.append(f"คำที่ควรใช้แทน:\n{rep_text}")
    return "\n".join(parts)


def _format_audience(audience: dict[str, Any]) -> str:
    """แปลง audience.json dict → string สำหรับใส่ใน reference."""
    parts: list[str] = []
    primary = audience.get("primary", {})
    if primary:
        prim_lines = [f"  {k}: {v}" for k, v in primary.items()]
        parts.append("กลุ่มเป้าหมายหลัก:\n" + "\n".join(prim_lines))
    end_user = audience.get("end_user", {})
    if end_user:
        eu_lines = [f"  {k}: {v}" for k, v in end_user.items()]
        parts.append("ผู้ใช้ปลายทาง:\n" + "\n".join(eu_lines))
    pain = audience.get("pain_points", [])
    if pain:
        parts.append("ปัญหา/ความต้องการ: " + ", ".join(pain))
    channels = audience.get("channels", [])
    if channels:
        parts.append("ช่องทาง: " + ", ".join(channels))
    return "\n".join(parts)


def load_brand_rules(brand_dir: str | Path | None = None) -> str:
    """อ่าน voice.json + terms.json → รวมเป็น rules string ใส่ system prompt.

    ถ้าไม่มีไฟล์เลย → คืน string ว่าง (agent ยังทำงานได้ แค่ไม่มี brand rules)
    Fallback: ถ้ามี .md เดิมแต่ไม่มี .json → เรียก migrate_brand อัตโนมัติ
    """
    brand_dir = _resolve_brand_dir(brand_dir)
    if not brand_dir:
        return ""

    _auto_migrate_if_needed(brand_dir)

    voice = _read_json(brand_dir / "voice.json")
    terms = _read_json(brand_dir / "terms.json")

    sections: list[str] = []
    voice_text = _format_voice(voice)
    if voice_text:
        sections.append(f"--- โทนเสียงแบรนด์ (Voice) ---\n{voice_text}")
    terms_text = _format_terms(terms)
    if terms_text:
        sections.append(f"--- คำที่ใช้/ห้ามใช้ (Terms) ---\n{terms_text}")

    if not sections:
        return ""

    return "--- กฎของแบรนด์ (Brand Rules) ---\n\n" + "\n\n".join(sections) + "\n\n--- สิ้นสุดกฎของแบรนด์ ---"


def load_brand_reference(brand_dir: str | Path | None = None) -> str:
    """อ่าน profile.md + audience.json → รวมเป็น reference string.

    reference ดึงตาม relevance — ไม่ได้ใส่ใน system prompt ทุก agent
    ใส่เฉพาะ agent ที่ต้องการบริบทเพิ่ม (content_creator, campaign_strategy)
    Fallback: ถ้ามี .md เดิมแต่ไม่มี .json → เรียก migrate_brand อัตโนมัติ
    """
    brand_dir = _resolve_brand_dir(brand_dir)
    if not brand_dir:
        return ""

    _auto_migrate_if_needed(brand_dir)

    profile = _read_text(brand_dir / "brand_profile.md")
    audience = _read_json(brand_dir / "audience.json")

    sections: list[str] = []
    if profile:
        sections.append(f"### Brand Profile\n\n{profile}")
    audience_text = _format_audience(audience)
    if audience_text:
        sections.append(f"### Target Audience\n\n{audience_text}")

    if not sections:
        return ""

    return "--- ข้อมูลแบรนด์อ้างอิง ---\n\n" + "\n\n---\n\n".join(sections) + "\n\n--- สิ้นสุดข้อมูลแบรนด์อ้างอิง ---"


def load_brand_visual(brand_dir: str | Path | None = None) -> dict[str, Any]:
    """อ่าน visual.json → dict สำหรับ media_gen.

    คืน {} ถ้าไม่มี visual.json — media_gen จะไม่แป๊ะ visual keywords
    Fallback: ถ้ามี .md เดิมแต่ไม่มี .json → เรียก migrate_brand อัตโนมัติ
    """
    brand_dir = _resolve_brand_dir(brand_dir)
    if not brand_dir:
        return {}

    _auto_migrate_if_needed(brand_dir)

    return _read_json(brand_dir / "visual.json")


def load_brand_context(brand_dir: str | Path | None = None) -> str:
    """Backward compat — delegate ไป load_brand_rules.

    call sites เดิม (orchestrator.py, base_agent.py) ยังเรียกฟังก์ชันนี้
    หลัง migration เสร็จ จะเปลี่ยน call sites ไปใช้ load_brand_rules โดยตรง
    """
    return load_brand_rules(brand_dir)


def _resolve_brand_dir(brand_dir: str | Path | None) -> Path | None:
    """Resolve brand_dir — คืน None ถ้าไม่มี directory."""
    if brand_dir is None:
        project_root = Path(__file__).resolve().parent.parent
        brand_dir = project_root / "brand"
    brand_dir = Path(brand_dir)
    if not brand_dir.exists() or not brand_dir.is_dir():
        return None
    return brand_dir


def _auto_migrate_if_needed(brand_dir: Path) -> None:
    """Fallback: ถ้ามี .md เดิมแต่ยังไม่มี .json → เรียก migrate_brand อัตโนมัติ.

    ทำงานครั้งเดียวต่อ directory — หลัง migrate แล้ว .json จะมีอยู่ จะไม่ migrate ซ้ำ
    ปลอดภัยเพราะ migrate_brand ไม่ overwrite .json ที่มีอยู่แล้ว (force=False default)
    """
    has_md = any((brand_dir / f).exists() for f in
                 ("tone_of_voice.md", "visual_guidelines.md", "target_audience.md"))
    has_json = any((brand_dir / f).exists() for f in
                   ("voice.json", "visual.json", "audience.json"))
    if has_md and not has_json:
        try:
            from .brand_migrate import migrate_brand
            migrate_brand(brand_dir)
        except Exception:
            pass  # migrate พัง → ไม่ crash loader, คืน empty (agent ยังทำงานได้)
