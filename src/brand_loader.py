"""Load brand configuration from a directory — แยก 3 ชั้น (rules / reference / visual).

โครงไฟล์ใหม่:
  brand/voice.json     — โทนเสียง + บุคลิก + ตัวอย่าง (rules → system prompt)
  brand/terms.json     — คำอนุมัติ / คำต้องห้าม (hard rules → system prompt)
  brand/profile.md     — ประวัติแบรนด์ (reference — ดึงตาม relevance)
  brand/audience.json  — กลุ่มเป้าหมาย (reference — structured)
  brand/visual.json    — สี สไตล์ keyword สำหรับ image prompt (structured → media_gen)

3 ฟังก์ชันแยกตามชั้น:
  load_brand_rules(brand_dir, product_id=None) -> str
      — voice.json + terms.json → rules string
      — ถ้ามี product_profile.tone_adjustment → แป๊ะท้าย
  load_brand_reference(brand_dir, product_id=None) -> str
      — profile.md + audience.json → reference string
      — ถ้ามี product_profile.audience → ทับของแบรนด์
      — ถ้ามี competitors/differentiators/use_cases/price_tier → เพิ่ม section
  load_brand_visual(brand_dir, product_id=None) -> dict
      — visual.json → dict สำหรับ media_gen
      — ถ้ามี product_profile.visual_override → merge (ทับทั้งก้อนฟิลด์ย่อย)

กฎรวม: สินค้ามีฟิลด์ไหน → ใช้ของสินค้า (ทับทั้งก้อน); ไม่มี → ใช้ของแบรนด์
Backward compat: product_id=None → ทำงานเหมือนเดิมทุกประการ
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
    lifestyle = audience.get("lifestyle", [])
    if lifestyle:
        parts.append("ไลฟ์สไตล์: " + ", ".join(lifestyle))
    bb = audience.get("buying_behavior", {})
    if bb:
        bb_lines = [f"  {k}: {v}" for k, v in bb.items()]
        parts.append("พฤติกรรมการซื้อ:\n" + "\n".join(bb_lines))
    pain = audience.get("pain_points", [])
    if pain:
        parts.append("ปัญหา/ความต้องการ: " + ", ".join(pain))
    channels = audience.get("channels", [])
    if channels:
        parts.append("ช่องทาง: " + ", ".join(channels))
    search = audience.get("search_channels", [])
    if search:
        parts.append("ช่องทางค้นหาข้อมูล: " + ", ".join(search))
    return "\n".join(parts)


def load_brand_rules(brand_dir: str | Path | None = None, *, product_id: str | None = None) -> str:
    """อ่าน voice.json + terms.json → รวมเป็น rules string ใส่ system prompt.

    ถ้าไม่มีไฟล์เลย → คืน string ว่าง (agent ยังทำงานได้ แค่ไม่มี brand rules)
    Fallback: ถ้ามี .md เดิมแต่ไม่มี .json → เรียก migrate_brand อัตโนมัติ
    ถ้ามี product_id และ product_profile.tone_adjustment → แป๊ะท้าย rules string
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

    # Product profile — tone_adjustment แป๊ะท้าย rules (ปรับโทนภายใน voice เดิม)
    if product_id:
        profile = load_product_profile(product_id)
        tone_adj = profile.get("tone_adjustment", "")
        if tone_adj:
            sections.append(f"--- ปรับโทนสำหรับสินค้านี้ ({product_id}) ---\n{tone_adj}")

    if not sections:
        return ""

    return "--- กฎของแบรนด์ (Brand Rules) ---\n\n" + "\n\n".join(sections) + "\n\n--- สิ้นสุดกฎของแบรนด์ ---"


def load_brand_reference(brand_dir: str | Path | None = None, *, product_id: str | None = None) -> str:
    """อ่าน profile.md + audience.json → รวมเป็น reference string.

    reference ดึงตาม relevance — ไม่ได้ใส่ใน system prompt ทุก agent
    ใส่เฉพาะ agent ที่ต้องการบริบทเพิ่ม (content_creator, campaign_strategy)
    Fallback: ถ้ามี .md เดิมแต่ไม่มี .json → เรียก migrate_brand อัตโนมัติ
    ถ้ามี product_id และ product_profile.audience → ทับของแบรนด์ (ทั้งก้อน)
    ถ้ามี competitors/differentiators/use_cases/price_tier → เพิ่มเป็น section ใหม่
    """
    brand_dir = _resolve_brand_dir(brand_dir)
    if not brand_dir:
        return ""

    _auto_migrate_if_needed(brand_dir)

    profile = _read_text(brand_dir / "brand_profile.md")
    brand_audience = _read_json(brand_dir / "audience.json")

    # ถ้ามี product_profile.audience → ทับของแบรนด์ทั้งก้อน
    product_profile = load_product_profile(product_id) if product_id else {}
    audience = product_profile.get("audience") or brand_audience

    sections: list[str] = []
    if profile:
        sections.append(f"### Brand Profile\n\n{profile}")
    audience_text = _format_audience(audience)
    if audience_text:
        sections.append(f"### Target Audience\n\n{audience_text}")

    # Positioning section — เฉพาะสินค้าที่มี product_profile
    positioning = _format_positioning(product_profile, product_id or "")
    if positioning:
        sections.append(positioning)

    if not sections:
        return ""

    return "--- ข้อมูลแบรนด์อ้างอิง ---\n\n" + "\n\n---\n\n".join(sections) + "\n\n--- สิ้นสุดข้อมูลแบรนด์อ้างอิง ---"


def load_brand_visual(brand_dir: str | Path | None = None, *, product_id: str | None = None) -> dict[str, Any]:
    """อ่าน visual.json → dict สำหรับ media_gen.

    คืน {} ถ้าไม่มี visual.json — media_gen จะไม่แป๊ะ visual keywords
    Fallback: ถ้ามี .md เดิมแต่ไม่มี .json → เรียก migrate_brand อัตโนมัติ
    ถ้ามี product_profile.visual_override → merge dict (ทับทั้งก้อนฟิลด์ย่อยที่ระบุ)
    """
    brand_dir = _resolve_brand_dir(brand_dir)
    if not brand_dir:
        return {}

    _auto_migrate_if_needed(brand_dir)

    visual = _read_json(brand_dir / "visual.json")

    # Merge product visual_override — ทับทั้งก้อนฟิลด์ย่อยที่ระบุ
    if product_id:
        profile = load_product_profile(product_id)
        override = profile.get("visual_override", {})
        if override:
            visual = {**visual, **override}

    return visual


def load_brand_context(brand_dir: str | Path | None = None, *, product_id: str | None = None) -> str:
    """Backward compat — delegate ไป load_brand_rules.

    call sites เดิม (orchestrator.py, base_agent.py) ยังเรียกฟังก์ชันนี้
    หลัง migration เสร็จ จะเปลี่ยน call sites ไปใช้ load_brand_rules โดยตรง
    """
    return load_brand_rules(brand_dir, product_id=product_id)


def _resolve_brand_dir(brand_dir: str | Path | None) -> Path | None:
    """Resolve brand_dir — brand-scoped workspace first, then product brand/.

    MB-02 security: when a verified brand context is active, brand state is
    server-derived from ``brand_state_root()`` and a client-supplied
    ``brand_dir`` is IGNORED — the browser never chooses a filesystem path.
    An explicit ``brand_dir`` is honored only when no brand context is active
    (CLI / test backward compat).
    """
    from .workspace_context import get_workspace
    ws = get_workspace()
    if ws is not None and ws.brand_id is not None:
        # Normal multi-brand execution — ignore any client-supplied path.
        brand_dir = None
    if brand_dir is None or brand_dir == "brand":
        from .local_workspace import local_brand_dir, product_brand_dir
        local = local_brand_dir()
        if local.exists() and any(local.iterdir()):
            return local
        prod = product_brand_dir()
        if prod.exists() and prod.is_dir():
            return prod
        return None
    # CLI / test backward compat (no brand context): explicit path honored.
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


# ---------------------------------------------------------------------------
# Product Profile — ตำแหน่งสินค้า (positioning) แยกจากแบรนด์
# ---------------------------------------------------------------------------

def load_product_profile(product_id: str) -> dict[str, Any]:
    """อ่าน cache/{product_id}/product_profile.json — คืน {} ถ้าไม่มี.

    Public API สำหรับดึง product profile ดิบ (ก่อน merge กับแบรนด์).
    ใช้เมื่อต้องการเห็น profile เฉพาะสินค้าโดยไม่รวมของแบรนด์
    (เช่น orchestrator auto mode ส่ง price_tier/differentiators ให้ LLM เลือก).

    เก็บเฉพาะสิ่งที่ต่างจากแบรนด์ (audience, competitors, differentiators,
    use_cases, price_tier, tone_adjustment, visual_override).
    กฎรวม: สินค้ามีฟิลด์ไหน → ใช้ของสินค้า; ไม่มี → ใช้ของแบรนด์

    ค้นหาจาก workspace root (per-user หรือ project root) — ไม่ fallback ไป Path.cwd()
    รวมถึง data/ เดิมเพื่อ backward compat ภายใต้ workspace root เท่านั้น
    """
    if not product_id:
        return {}
    # MB-02: product profiles are brand-specific.  Resolve through the brand
    # root when a brand context is active.  An authenticated user-only context
    # (workspace set, no brand_id) must fail closed — never fall back to user
    # root.  Only a true no-workspace CLI context (get_workspace() is None)
    # retains the user/project-root fallback for CLI/test backward compat.
    from .workspace_context import get_workspace, brand_state_root, user_state_root, contain_path
    ws = get_workspace()
    if ws is not None and ws.brand_id is not None:
        _ws_root = brand_state_root(Path(__file__).resolve().parent.parent)
    elif ws is None:
        _ws_root = user_state_root(Path(__file__).resolve().parent.parent)
    else:
        raise ValueError("load_product_profile requires an active brand context")
    candidates = [
        contain_path(product_id, _ws_root / "cache") / "product_profile.json",
        contain_path(product_id, _ws_root / "data") / "product_profile.json",
    ]
    for path in candidates:
        data = _read_json(path)
        if data:
            return data
    return {}




_POSITIONING_LIST_FIELDS = [
    ("คู่แข่งหลัก", "competitors"),
    ("จุดขายหลัก", "differentiators"),
    ("Use cases", "use_cases"),
]


def build_multi_product_profile_context(product_ids: list[str]) -> str:
    """Build labeled per-product profile context for multi-product runs.

    Each product's profile (audience, positioning, tone_adjustment,
    visual_override) is rendered as a separately labeled identity envelope.
    The model composes an appropriate combined presentation from all labeled
    constraints — no deterministic merge, no last-write-wins, no precedence
    by load order.

    Visual overrides are rendered as text constraints for the model, not
    applied as dict merges to brand_visual.  Media generation continues to
    use brand-level visual config for multi-product runs.

    Returns "" if no product has a profile.
    """
    parts: list[str] = []
    for pid in product_ids:
        profile = load_product_profile(pid)
        # Effective facts may exist in product.json even without a profile
        from .product_db import get_effective_facts
        effective = get_effective_facts(pid)
        if not profile and not effective:
            continue
        sections: list[str] = []
        # Effective product facts (derived + manual merged) — สูงสุด
        if effective:
            fact_lines = [f"  {info['label']}: {info['value']}" for info in effective.values()]
            if fact_lines:
                sections.append(
                    "ข้อมูลสินค้า (Product Information):\n"
                    + "\n".join(fact_lines)
                )
        audience = profile.get("audience")
        if audience:
            audience_text = _format_audience(audience)
            if audience_text:
                sections.append(f"กลุ่มเป้าหมาย:\n{audience_text}")
        positioning = _format_positioning(profile, pid)
        if positioning:
            sections.append(positioning)
        tone_adj = (profile.get("tone_adjustment") or "").strip()
        if tone_adj:
            sections.append(f"ปรับโทน: {tone_adj}")
        visual_override = profile.get("visual_override") or {}
        if isinstance(visual_override, dict) and visual_override:
            visual_lines = []
            for k, v in visual_override.items():
                if isinstance(v, dict):
                    sub = ", ".join(f"{sk}: {sv}" for sk, sv in v.items() if sv)
                    if sub:
                        visual_lines.append(f"  {k}: {sub}")
                elif v:
                    visual_lines.append(f"  {k}: {v}")
            if visual_lines:
                sections.append("แนวทางภาพ:\n" + "\n".join(visual_lines))
        if not sections:
            continue
        label = (pid or "").strip().replace("\n", " ").replace("\r", " ")
        parts.append(
            f"--- โปรไฟล์สินค้า: {label} ---\n"
            f"{chr(10).join(sections)}\n"
            f"--- สิ้นสุดโปรไฟล์สินค้า: {label} ---"
        )
    if not parts:
        return ""
    return (
        "--- โปรไฟล์สินค้าแยกตามรุ่น (แต่ละรุ่นเป็นอิสระต่อกัน) ---\n"
        "ข้อมูลนี้เป็นข้อจำกัดเฉพาะของแต่ละสินค้า ใช้เฉพาะกับสินค้าที่ระบุ\n"
        "ถ้าสินค้าหลายตัวมี profile ขัดแย้งกัน ให้ compose presentation ที่เหมาะสม\n"
        "โดยเคารพข้อจำกัดของแต่ละตัว ไม่ละเลยสินค้าใดสินค้าหนึ่ง\n\n"
        + "\n\n".join(parts)
        + "\n\n--- สิ้นสุดโปรไฟล์สินค้าแยกตามรุ่น ---"
    )


def _format_positioning(profile: dict[str, Any], product_id: str) -> str:
    """แปลง product_profile → positioning section string สำหรับ reference.

    รวม: competitors, differentiators, use_cases, price_tier (ไม่รวม audience/tone/visual
    เพราะจัดการในจุดอื่น — audience ทับของแบรนด์, tone แป๊ะใน rules, visual merge ใน visual)
    """
    parts: list[str] = []
    for label, key in _POSITIONING_LIST_FIELDS:
        values = profile.get(key, [])
        if values:
            parts.append(f"{label}: " + ", ".join(values))
    price_tier = profile.get("price_tier", "")
    if price_tier:
        parts.append(f"ระดับราคา: {price_tier}")
    if not parts:
        return ""
    header = f"### Product Positioning ({product_id})"
    return header + "\n\n" + "\n".join(parts)
