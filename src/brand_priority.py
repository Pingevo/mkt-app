"""Brand priority — แยก brand rules เป็น hard (brand ชนะเสมอ) / soft (user ชนะ ณ ตอนรัน).

หลักการ (ตามตลาด — OpenAI instruction hierarchy, Anthropic constitution, Jasper):
  - Hard rules (banned phrases, restricted terms, replacements) → brand ชนะเสมอ
  - Soft style (personality, tone, formality, language) → user ชนะ ณ ตอนรัน
  - ถ้า conflict → บอก user ใน UI ไม่ใช่ให้ LLM ตัดสินใจเงียบๆ

Interface (เล็ก 3 ฟังก์ชัน + 2 dataclass):
  load_brand_priority(brand_dir) -> BrandRules
  detect_conflicts(brand_rules, instructions) -> list[Conflict]
  build_priority_prompt(brand_rules, instructions) -> str
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Field ที่เป็น hard rules (brand ชนะเสมอ)
_HARD_VOICE_FIELDS = ("banned_phrases",)
_HARD_TERMS_FIELDS = ("restricted", "replacements")

# Field ที่เป็น soft style (user ชนะ ณ ตอนรัน)
_SOFT_VOICE_FIELDS = ("personality", "tone_description", "formality_level", "language")
_SOFT_TERMS_FIELDS = ("approved",)  # approved = คำแนะนำ ไม่ใช่ guardrail


@dataclass
class BrandRules:
    """ข้อมูลแบรนด์แยกเป็น 2 ชั้น.

    hard: string สำหรับใส่ใน prompt (banned + restricted + replacements)
    soft: string สำหรับใส่ใน prompt (personality + tone + formality + language)
    hard_dict / soft_dict: ข้อมูลดิบสำหรับ detect_conflicts
    """
    hard: str = ""
    soft: str = ""
    hard_dict: dict[str, Any] = field(default_factory=dict)
    soft_dict: dict[str, Any] = field(default_factory=dict)


@dataclass
class Conflict:
    """conflict 1 ตัวระหว่าง brand และ user instructions.

    severity: "hard" = brand ชนะ (ห้าม override), "soft" = user เลือกได้
    """
    field: str
    brand_value: Any
    user_value: Any
    severity: str  # "hard" หรือ "soft"


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


def _resolve_brand_dir(brand_dir: str | Path | None) -> Path | None:
    """Resolve brand_dir — คืน None ถ้าไม่มี directory."""
    if brand_dir is None:
        project_root = Path(__file__).resolve().parent.parent
        brand_dir = project_root / "brand"
    brand_dir = Path(brand_dir)
    if not brand_dir.exists() or not brand_dir.is_dir():
        return None
    return brand_dir


def _format_hard(voice: dict, terms: dict) -> tuple[str, dict]:
    """รวม hard fields เป็น string + สร้าง hard_dict."""
    parts: list[str] = []
    hard_dict: dict[str, Any] = {}

    banned = voice.get("banned_phrases", [])
    if banned:
        parts.append("คำ/วลีที่ห้ามใช้: " + ", ".join(banned))
        hard_dict["banned_phrases"] = list(banned)

    restricted = terms.get("restricted", [])
    if restricted:
        parts.append("คำต้องห้าม: " + ", ".join(restricted))
        hard_dict["restricted"] = list(restricted)

    replacements = terms.get("replacements", {})
    if replacements:
        rep_text = "\n".join(f"  {old} → {new}" for old, new in replacements.items())
        parts.append(f"คำที่ควรใช้แทน:\n{rep_text}")
        hard_dict["replacements"] = dict(replacements)

    return "\n".join(parts), hard_dict


def _format_soft(voice: dict, terms: dict = None) -> tuple[str, dict]:
    """รวม soft fields เป็น string + สร้าง soft_dict."""
    parts: list[str] = []
    soft_dict: dict[str, Any] = {}
    terms = terms or {}

    personality = voice.get("personality", "")
    if personality:
        parts.append(f"บุคลิก: {personality}")
        soft_dict["personality"] = personality

    tone = voice.get("tone_description", "")
    if tone:
        parts.append(f"โทนเสียง: {tone}")
        soft_dict["tone_description"] = tone

    formality = voice.get("formality_level")
    if formality is not None:
        parts.append(f"ระดับความเป็นทางการ: {formality}/5")
        soft_dict["formality_level"] = formality

    language = voice.get("language", "")
    if language:
        parts.append(f"ภาษา: {language}")
        soft_dict["language"] = language

    approved = terms.get("approved", [])
    if approved:
        parts.append("คำที่แนะนำ: " + ", ".join(approved))
        soft_dict["approved"] = list(approved)

    return "\n".join(parts), soft_dict


def load_brand_priority(brand_dir: str | Path | None = None, *, product_id: str | None = None) -> BrandRules:
    """อ่าน voice.json + terms.json → แยกเป็น hard/soft BrandRules.

    ถ้าไม่มีไฟล์ → คืน BrandRules ว่าง (agent ยังทำงานได้ แค่ไม่มี brand rules)
    ถ้ามี product_id และ product_profile.tone_adjustment → แป๊ะท้าย soft rules
    (เหมือน load_brand_rules เพื่อให้ tone_adjustment ไปถึง prompt แม้ใช้ BrandRules)
    """
    brand_dir = _resolve_brand_dir(brand_dir)
    if not brand_dir:
        return BrandRules()

    voice = _read_json(brand_dir / "voice.json")
    terms = _read_json(brand_dir / "terms.json")

    hard_text, hard_dict = _format_hard(voice, terms)
    soft_text, soft_dict = _format_soft(voice, terms)

    # Product profile — tone_adjustment แป๊ะท้าย soft (ปรับโทนภายใน voice เดิม)
    if product_id:
        from .brand_loader import load_product_profile
        profile = load_product_profile(product_id)
        tone_adj = (profile.get("tone_adjustment") or "").strip()
        if tone_adj:
            label = f"\n--- ปรับโทนสำหรับสินค้านี้ ({product_id}) ---\n{tone_adj}"
            soft_text = (soft_text + "\n" + label) if soft_text else label

    return BrandRules(hard=hard_text, soft=soft_text, hard_dict=hard_dict, soft_dict=soft_dict)


# ---------------------------------------------------------------------------
# detect_conflicts — เปรียบเทียบ brand กับ instructions
# ---------------------------------------------------------------------------

# Map คำที่บ่งบอก "aggressive/bold/controversial" ที่มัก conflict กับ brand อบอุ่น
_AGGRESSIVE_TONES = {"aggressive", "bold", "controversial", "edgy", "provocative"}

# คำที่บ่งบอก brand "อบอุ่น/เป็นมิตร" — ใช้ในหลาย conflict detector
_SOFT_BRAND_KEYWORDS = ("อบอุ่น", "เป็นมิตร", "สุภาพ", "นุ่ม", "เรียบ", "เกรงใจ", "friendly", "warm", "polite")


def _brand_is_soft(brand_text: str) -> bool:
    """ตรวจว่า brand text บ่งบอก personality/tone แบบอบอุ่น/เป็นมิตร หรือไม่."""
    brand_lower = brand_text.lower()
    return any(k in brand_lower for k in _SOFT_BRAND_KEYWORDS)


def _tone_conflicts(brand_tone: str, user_tones: list) -> bool:
    """ตรวจว่า user tone ขัดกับ brand tone หรือไม่ (อย่างง่าย)."""
    if not brand_tone or not user_tones:
        return False
    if not _brand_is_soft(brand_tone):
        return False
    for t in user_tones:
        if str(t).lower() in _AGGRESSIVE_TONES:
            return True
    return False


def _language_conflicts(brand_lang: str, user_lang: str) -> bool:
    """ตรวจว่า user language ขัดกับ brand language หรือไม่."""
    if not brand_lang or not user_lang:
        return False
    brand_lower = brand_lang.lower()
    user_lower = str(user_lang).lower()
    # brand ระบุภาษาชัด (เช่น "ไทย") แล้ว user เลือกภาษาอื่น
    if "ไทย" in brand_lower or "thai" in brand_lower:
        if user_lower in ("english", "expert") and "thai" not in user_lower:
            return True
    return False


def _sell_style_conflicts(brand_personality: str, user_sell: str) -> bool:
    """ตรวจว่า sell_style ขัดกับ brand personality หรือไม่."""
    if not brand_personality or not user_sell:
        return False
    if not _brand_is_soft(brand_personality):
        return False
    # brand อบอุ่น แต่ user เลือก hard sell → conflict
    return str(user_sell).lower() == "hard"


def _hook_style_conflicts(brand_personality: str, user_hook: str) -> bool:
    """ตรวจว่า hook_style ขัดกับ brand personality หรือไม่."""
    if not brand_personality or not user_hook:
        return False
    if not _brand_is_soft(brand_personality):
        return False
    # brand อบอุ่น แต่ user เลือก controversial → conflict
    return str(user_hook).lower() == "controversial"


def detect_conflicts(brand_rules: BrandRules, instructions: dict) -> list[Conflict]:
    """หา conflict ระหว่าง brand และ user instructions.

    คืน list[Conflict] — แต่ละตัวบอก field, brand_value, user_value, severity
    severity "hard" = brand ชนะ (banned/restricted ใน custom text)
    severity "soft" = user เลือกได้ (tone ขัดกับ brand personality)
    """
    conflicts: list[Conflict] = []
    if not instructions:
        return conflicts

    # --- Hard conflicts: banned/restricted phrases ใน custom text ---
    custom_text = instructions.get("custom", "") or ""
    if custom_text:
        seen_phrases: set[str] = set()
        banned = brand_rules.hard_dict.get("banned_phrases", [])
        for phrase in banned:
            if phrase in custom_text and phrase not in seen_phrases:
                conflicts.append(Conflict(
                    field="custom_banned_phrase",
                    brand_value=phrase,
                    user_value=custom_text,
                    severity="hard",
                ))
                seen_phrases.add(phrase)
        restricted = brand_rules.hard_dict.get("restricted", [])
        for phrase in restricted:
            if phrase in custom_text and phrase not in seen_phrases:
                conflicts.append(Conflict(
                    field="custom_restricted_term",
                    brand_value=phrase,
                    user_value=custom_text,
                    severity="hard",
                ))
                seen_phrases.add(phrase)

    # --- Soft conflicts: tone ขัดกับ brand tone_description ---
    user_tones = instructions.get("tone", [])
    if user_tones:
        brand_tone = brand_rules.soft_dict.get("tone_description", "")
        if _tone_conflicts(brand_tone, user_tones):
            conflicts.append(Conflict(
                field="tone",
                brand_value=brand_tone,
                user_value=user_tones,
                severity="soft",
            ))

    # --- Soft conflicts: language ขัดกับ brand language ---
    user_lang = instructions.get("language", "")
    if user_lang:
        brand_lang = brand_rules.soft_dict.get("language", "")
        if brand_lang and _language_conflicts(brand_lang, user_lang):
            conflicts.append(Conflict(
                field="language",
                brand_value=brand_lang,
                user_value=user_lang,
                severity="soft",
            ))

    # --- Soft conflicts: sell_style ขัดกับ brand personality ---
    user_sell = instructions.get("sell_style", "")
    if user_sell:
        brand_personality = brand_rules.soft_dict.get("personality", "")
        if _sell_style_conflicts(brand_personality, user_sell):
            conflicts.append(Conflict(
                field="sell_style",
                brand_value=brand_personality,
                user_value=user_sell,
                severity="soft",
            ))

    # --- Soft conflicts: hook_style ขัดกับ brand personality ---
    user_hook = instructions.get("hook_style", "")
    if user_hook:
        brand_personality = brand_rules.soft_dict.get("personality", "")
        if _hook_style_conflicts(brand_personality, user_hook):
            conflicts.append(Conflict(
                field="hook_style",
                brand_value=brand_personality,
                user_value=user_hook,
                severity="soft",
            ))

    return conflicts


# ---------------------------------------------------------------------------
# build_priority_prompt — สร้าง prompt ที่บอก LLM ลำดับชัดเจน
# ---------------------------------------------------------------------------

def build_priority_prompt(brand_rules: BrandRules, instructions: dict) -> str:
    """สร้าง prompt ที่รวม hard + soft rules พร้อมบอกลำดับความสำคัญ.

    ลำดับ:
      1. Hard rules (brand ชนะเสมอ — ห้าม override แม้ user จะขอ)
      2. Soft style (brand เป็นค่าเริ่มต้น — user สามารถ override ได้ ณ ตอนรัน)

    ถ้าไม่มี brand rules เลย → คืน string ว่าง
    """
    if not brand_rules.hard and not brand_rules.soft:
        return ""

    sections: list[str] = []

    if brand_rules.hard:
        sections.append(
            f"--- กฎบังคับของแบรนด์ (Hard Rules — ชนะเสมอ ห้าม override) ---\n"
            f"{brand_rules.hard}\n"
            f"--- สิ้นสุดกฎบังคับ ---\n"
            f"กฎเหล่านี้เป็นกฎบังคับ — ถ้าคำสั่งของผู้ใช้หรือการตั้งค่าใดๆ ขัดแย้งกับกฎนี้ ให้ทำตามกฎแบรนด์เสมอ"
        )

    if brand_rules.soft:
        user_override_note = ""
        instr = instructions or {}
        overrides = []
        # tone
        user_tones = instr.get("tone", [])
        if user_tones:
            overrides.append(f"tone {', '.join(user_tones)}")
        # language
        user_lang = instr.get("language")
        if user_lang:
            overrides.append(f"ภาษา {user_lang}")
        # sell_style
        user_sell = instr.get("sell_style")
        if user_sell:
            overrides.append(f"การขาย {user_sell}")
        # hook_style
        user_hook = instr.get("hook_style")
        if user_hook:
            overrides.append(f"hook {user_hook}")
        if overrides:
            user_override_note = (
                f"\nหมายเหตุ: ผู้ใช้เลือก {' / '.join(overrides)} สำหรับรอบนี้ "
                f"— สามารถ override สไตล์แบรนด์ได้ (soft style) "
                f"แต่ห้ามใช้คำที่อยู่ในกฎบังคับด้านบน"
            )
        sections.append(
            f"--- สไตล์แบรนด์ (Soft — ค่าเริ่มต้น, ผู้ใช้สามารถ override ได้) ---\n"
            f"{brand_rules.soft}\n"
            f"--- สิ้นสุดสไตล์แบรนด์ ---"
            f"{user_override_note}"
        )

    return "\n\n".join(sections)
