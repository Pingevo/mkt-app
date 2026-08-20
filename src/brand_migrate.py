"""Migrate brand files from .md → .json (one-time conversion).

อ่านไฟล์ .md เดิม (tone_of_voice.md, visual_guidelines.md, target_audience.md)
แปลงเป็น .json ตามโครงใหม่:
  tone_of_voice.md     → voice.json + terms.json
  visual_guidelines.md → visual.json
  target_audience.md   → audience.json
  brand_profile.md     → เก็บเป็น .md ไว้ (ไม่ convert)

หลัง migrate:
  - .md เดิมเก็บเป็น .md.bak (ไม่ลบทิ้ง — กันข้อมูลหาย)
  - ถ้า .json มีอยู่แล้ว → ไม่ overwrite (เว้นแต่ force=True)

Usage:
  from src.brand_migrate import migrate_brand
  status = migrate_brand("brand/")  # → {"created": ["voice.json", ...], "backed_up": [...]}
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def migrate_brand(brand_dir: str | Path, *, force: bool = False) -> dict[str, Any]:
    """Migrate .md brand files → .json.

    Args:
        brand_dir: path to brand/ directory
        force: ถ้า True → overwrite .json ที่มีอยู่แล้ว

    Returns:
        {"created": ["voice.json", ...], "backed_up": ["tone_of_voice.md.bak", ...]}
    """
    brand_dir = Path(brand_dir)
    if not brand_dir.exists() or not brand_dir.is_dir():
        return {"created": [], "backed_up": []}

    created: list[str] = []
    backed_up: list[str] = []

    # tone_of_voice.md → voice.json + terms.json
    tone_path = brand_dir / "tone_of_voice.md"
    if tone_path.exists():
        content = tone_path.read_text(encoding="utf-8")
        voice, terms = _parse_tone_of_voice(content)

        if _write_json_if_needed(brand_dir / "voice.json", voice, force):
            created.append("voice.json")
        if _write_json_if_needed(brand_dir / "terms.json", terms, force):
            created.append("terms.json")
        _backup_md(tone_path, backed_up)

    # visual_guidelines.md → visual.json
    visual_md = brand_dir / "visual_guidelines.md"
    if visual_md.exists():
        content = visual_md.read_text(encoding="utf-8")
        visual = _parse_visual_guidelines(content)
        if _write_json_if_needed(brand_dir / "visual.json", visual, force):
            created.append("visual.json")
        _backup_md(visual_md, backed_up)

    # target_audience.md → audience.json
    audience_md = brand_dir / "target_audience.md"
    if audience_md.exists():
        content = audience_md.read_text(encoding="utf-8")
        audience = _parse_target_audience(content)
        if _write_json_if_needed(brand_dir / "audience.json", audience, force):
            created.append("audience.json")
        _backup_md(audience_md, backed_up)

    # brand_profile.md → เก็บเป็น .md ไว้ (ไม่ convert)

    return {"created": created, "backed_up": backed_up}


# ---------------------------------------------------------------------------
# Helpers — parse .md sections
# ---------------------------------------------------------------------------

def _split_sections(md: str) -> dict[str, str]:
    """แยก markdown ตาม ## headers → dict {header: content}."""
    sections: dict[str, str] = {}
    current_header = ""
    current_lines: list[str] = []

    for line in md.split("\n"):
        if line.startswith("## "):
            if current_header:
                sections[current_header] = "\n".join(current_lines).strip()
            current_header = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_header:
        sections[current_header] = "\n".join(current_lines).strip()

    return sections


def _parse_list_items(content: str) -> list[str]:
    """แยก bullet list (- item) → list ของ strings."""
    items: list[str] = []
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("- "):
            item = line[2:].strip()
            # ถ้ามี comma → split เพิ่ม (เช่น "คำ1, คำ2, คำ3")
            if "," in item and not item.startswith("ไม่"):
                items.extend(x.strip() for x in item.split(",") if x.strip())
            else:
                items.append(item)
    return items


def _extract_after_colon(line: str) -> str:
    """ดึงค่าหลังเครื่องหมาย ':' (เช่น 'อายุ: 30-45' → '30-45')."""
    if ":" in line:
        return line.split(":", 1)[1].strip()
    return line.strip()


def _parse_tone_of_voice(content: str) -> tuple[dict, dict]:
    """แยก tone_of_voice.md → (voice_dict, terms_dict)."""
    sections = _split_sections(content)

    voice: dict[str, Any] = {}
    terms: dict[str, Any] = {}

    # Personality
    pers_text = sections.get("บุคลิกของแบรนด์ (Brand Personality)", "")
    if pers_text:
        voice["personality"] = pers_text.split("\n")[0].strip()

    # Language
    lang_text = sections.get("ภาษาที่ใช้", "")
    if lang_text:
        # ดึงบรรทัดแรกที่มี "ภาษา"
        first_line = lang_text.split("\n")[0].strip()
        if first_line.startswith("- "):
            first_line = first_line[2:].strip()
        voice["language"] = first_line

    # Formality
    form_text = sections.get("ระดับความเป็นทางการ", "")
    if form_text:
        # ดึงตัวเลข 1-5 จากต้นบรรทัด
        match = re.match(r"(\d)", form_text.strip())
        if match:
            voice["formality_level"] = int(match.group(1))
        # เก็บคำอธิบายด้วย
        desc = re.sub(r"^\d+\s*—\s*", "", form_text.strip())
        if desc:
            voice["tone_description"] = desc

    # Examples
    ex_text = sections.get("ตัวอย่างโพสต์ที่ใช่", "")
    if ex_text:
        # แยกตัวอย่างแต่ละอัน (อยู่ใน "..." หรือบรรทัดแยก)
        examples = re.findall(r'"([^"]+)"', ex_text)
        if not examples:
            # ถ้าไม่มี quote → แยกตามบรรทัด
            examples = [l.strip().strip('"') for l in ex_text.split("\n") if l.strip()]
        voice["examples"] = examples

    # Do Say → terms.approved
    do_text = sections.get("คำที่ควรใช้ (Do Say)", "")
    if do_text:
        approved = _parse_list_items(do_text)
        # flatten comma-separated
        flat: list[str] = []
        for item in approved:
            flat.extend(x.strip() for x in item.split(",") if x.strip())
        terms["approved"] = flat

    # Don't Say → terms.restricted + replacements
    dont_text = sections.get("คำที่ห้ามใช้ (Don't Say)", "")
    if dont_text:
        restricted: list[str] = []
        replacements: dict[str, str] = {}
        for line in dont_text.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            # ตรวจรูปแบบ "ไม่เรียกสินค้าว่า X — ใช้ Y แทน"
            rep_match = re.search(r'"([^"]+)"\s*—\s*ใช้\s*"([^"]+)"', item)
            if rep_match:
                old_word = rep_match.group(1)
                new_word = rep_match.group(2)
                replacements[old_word] = new_word
                restricted.append(old_word)
            else:
                # ดึงคำใน quote
                quoted = re.findall(r'"([^"]+)"', item)
                if quoted:
                    restricted.extend(quoted)
                else:
                    # ถ้าไม่มี quote → เอาคำหลัง "เช่น" หรือคำสุดท้าย
                    clean = re.sub(r"^ไม่ใช้คำที่สร้างความกลัว\s*เช่น\s*", "", item)
                    if clean != item:
                        quoted_clean = re.findall(r'"([^"]+)"', clean)
                        if quoted_clean:
                            restricted.extend(quoted_clean)
        terms["restricted"] = restricted
        if replacements:
            terms["replacements"] = replacements

    return voice, terms


def _parse_visual_guidelines(content: str) -> dict[str, Any]:
    """แยก visual_guidelines.md → visual_dict."""
    sections = _split_sections(content)
    visual: dict[str, Any] = {}

    # Colors
    color_text = sections.get("สีหลักของแบรนด์ (Brand Colors)", "")
    if color_text:
        colors: dict[str, str] = {}
        for line in color_text.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            # Primary: **#hex** — usage
            color_match = re.match(r"(Primary|Secondary|Accent|พื้นหลัง):\s*\**([^*]+)\**", item)
            if color_match:
                key = color_match.group(1).lower()
                if key == "พื้นหลัง":
                    key = "background"
                colors[key] = color_match.group(2).strip()
        if colors:
            visual["colors"] = colors

    # Image Style
    style_text = sections.get("สไตล์ภาพ (Image Style)", "")
    if style_text:
        style: dict[str, str] = {}
        for line in style_text.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            # Product shot: ...
            if ":" in item:
                key, val = item.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                style[key] = val.strip()
        if style:
            visual["image_style"] = style

    # AI Prompt keywords
    prompt_text = sections.get("แนวทางสำหรับ AI Image/Video Prompt", "")
    if prompt_text:
        keywords: list[str] = []
        avoid: list[str] = []
        for line in prompt_text.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            if item.startswith("ใช้ keyword:"):
                kw_str = _extract_after_colon(item.replace("ใช้ keyword:", "keyword:"))
                # ลบ quotes รอบแต่ละ keyword (เช่น "clean" → clean)
                for x in kw_str.split(","):
                    clean = x.strip().strip('"').strip()
                    if clean:
                        keywords.append(clean)
            elif item.startswith("หลีกเลี่ยง:"):
                av_str = _extract_after_colon(item.replace("หลีกเลี่ยง:", "avoid:"))
                for x in av_str.split(","):
                    clean = x.strip().strip('"').strip()
                    if clean:
                        avoid.append(clean)
        if keywords:
            visual["keywords"] = keywords
        if avoid:
            visual["avoid"] = avoid

    return visual


# Mapping คำไทยใน .md → key มาตรฐานใน audience.json
# (เพิ่ม/แก้ได้โดยไม่ต้องแก้ logic — เป็น data ไม่ใช่ code)
_BUYING_BEHAVIOR_KEY_MAP = {
    "ตัดสินใจ": "decision_factors",
    "งบ": "budget_per_purchase",
    "budget": "budget_per_purchase",
    "ซื้อผ่าน": "channels_purchase",
    "channel": "channels_purchase",
}


def _parse_target_audience(content: str) -> dict[str, Any]:
    """แยก target_audience.md → audience_dict.

    ดึงครบทุก section ตาม target_audience.example.md:
      - primary (age, role, เพศ, อาชีพ, รายได้, ที่อยู่)
      - end_user
      - lifestyle (list)
      - buying_behavior (dict: decision_factors, budget_per_purchase, channels_purchase)
      - pain_points (list)
      - channels (list — social + shop)
      - search_channels (list — ค้นหาข้อมูล)
    """
    sections = _split_sections(content)
    audience: dict[str, Any] = {}

    # Primary
    prim_text = sections.get("กลุ่มเป้าหมายหลัก", "")
    if prim_text:
        primary: dict[str, str] = {}
        for line in prim_text.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            if ":" in item:
                key, val = item.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                # ดึงค่าในวงเล็บออก (เช่น "30-45 (ผู้ปกครอง)" → age=30-45, role=ผู้ปกครอง)
                val = val.strip()
                paren_match = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", val)
                if paren_match and key == "อายุ":
                    primary["age"] = paren_match.group(1).strip()
                    primary["role"] = paren_match.group(2).strip()
                else:
                    primary[key] = val
        if primary:
            audience["primary"] = primary

    # End User
    eu_text = sections.get("ผู้ใช้ปลายทาง (End User)", "")
    if eu_text:
        end_user: dict[str, str] = {}
        for line in eu_text.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            if ":" in item:
                key, val = item.split(":", 1)
                end_user[key.strip().lower().replace(" ", "_")] = val.strip()
            else:
                if "desc" not in end_user:
                    end_user["desc"] = item
        if end_user:
            audience["end_user"] = end_user

    # Lifestyle — bullet list
    life_text = sections.get("ไลฟ์สไตล์", "")
    if life_text:
        lifestyle = _parse_list_items(life_text)
        if lifestyle:
            audience["lifestyle"] = lifestyle

    # Buying Behavior — key: value pairs
    bb_text = sections.get("พฤติกรรมการซื้อ", "")
    if bb_text:
        bb: dict[str, str] = {}
        for line in bb_text.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            if ":" in item:
                key, val = item.split(":", 1)
                key_lower = key.strip().lower()
                # map คำไทย → key มาตรฐาน (ดู _BUYING_BEHAVIOR_KEY_MAP)
                mapped = next((std for thai, std in _BUYING_BEHAVIOR_KEY_MAP.items() if thai in key_lower), None)
                key = mapped or key_lower.replace(" ", "_")
                bb[key] = val.strip()
        if bb:
            audience["buying_behavior"] = bb

    # Pain Points
    pain_text = sections.get("ปัญหา/ความต้องการ (Pain Points)", "")
    if pain_text:
        audience["pain_points"] = _parse_list_items(pain_text)

    # Channels — แยก search_channels ออกจาก channels ปกติ
    chan_text = sections.get("ช่องทางที่ใช้บ่อย", "")
    if chan_text:
        channels: list[str] = []
        search_channels: list[str] = []
        for line in chan_text.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            if ":" in item:
                key, val = item.split(":", 1)
                key_lower = key.strip().lower()
                vals = [x.strip() for x in val.split(",") if x.strip()]
                if "ค้นหา" in key_lower or "search" in key_lower:
                    search_channels.extend(vals)
                else:
                    channels.extend(vals)
        if channels:
            audience["channels"] = channels
        if search_channels:
            audience["search_channels"] = search_channels

    return audience


# ---------------------------------------------------------------------------
# Helpers — write/backup
# ---------------------------------------------------------------------------

def _write_json_if_needed(path: Path, data: dict, force: bool) -> bool:
    """เขียน .json ถ้าไม่มีอยู่ หรือ force=True. คืน True ถ้าเขียนแล้ว."""
    if path.exists() and not force:
        return False
    if not data:
        return False  # ไม่สร้างไฟล์ว่าง
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _backup_md(md_path: Path, backed_up: list[str]):
    """เก็บ .md เดิมเป็น .md.bak."""
    bak_path = md_path.with_suffix(".md.bak")
    if not bak_path.exists():
        bak_path.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
        backed_up.append(bak_path.name)
