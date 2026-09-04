"""Validate agent outputs before they are saved or shown in the UI."""

from __future__ import annotations

import json
import re
from typing import Any

from .config_loader import load_config
from .content_schema import CONTENT_ARTIFACT_SCHEMA, CONTENT_RESPONSE_SCHEMA

from .brand_priority import BrandRules


def _output_is_blank(output: str | None) -> bool:
    return not output or not output.strip()


# ---------------------------------------------------------------------------
# Section detection — กฎเดียวครอบคลุมทุก format ที่ LLM สร้างจริง
#
# แทนการเพิ่ม pattern ทีละอัน (##, **, 1., colon, plain text, ...) ใช้กฎเดียว:
#   บรรทัดเป็น heading ของ section ถ้าหลังตัด markdown decoration (#, *, เลข, จุด)
#   บรรทัดขึ้นต้นด้วยชื่อ section แล้วตามด้วยตัวคั่น (จบบรรทัด, วงเล็บ, ทวิภาค, ขีด)
#   ไม่ใช่ตามด้วยข้อความต่อเนื่อง — กัน false positive จากประโยคธรรมดา
# ---------------------------------------------------------------------------

# ตัด decorator ด้านหน้า: #, *, ตัวเลข, จุด, space
_HEADING_PREFIX_RE = re.compile(r'^[#*\d.\s]+')
# ตัวคั่บหลังชื่อ section ที่บอกว่า "จบ heading แล้ว" — ไม่ใช่ข้อความต่อ
_SEPARATOR_CHARS = set(':：*—-\u2014')


def _line_is_heading_for(required: str, line: str) -> bool:
    """บรรทัดนี้เป็น heading ของ section `required` หรือไม่.

    หลักการ: ตัด markdown decoration ด้านหน้าออก แล้วเช็คว่าบรรทัดขึ้นต้นด้วย
    ชื่อ section และตามด้วยตัวคั่น (ไม่ใช่ข้อความต่อเนื่อง) ครอบคลุมทุก format:
      ## ภาพรวมตลาด / **ภาพรวมตลาด** / 1. ภาพรวมตลาด / ภาพรวมตลาด: /
      ภาพรวมตลาด (Market Overview) / ### 1. ภาพรวมตลาด — สถานการณ์
    แต่ไม่ match ประโยคธรรมดา เช่น "ภาพรวมตลาดสมาร์ทวอทช์เด็กในปัจจุบัน..."
    """
    cleaned = _HEADING_PREFIX_RE.sub('', line).strip()
    # ตัด ** ที่อาจตกค้างด้านหน้า (กรณี **1. Section**)
    cleaned = cleaned.lstrip('*').strip()
    if not cleaned.startswith(required):
        return False
    rest = cleaned[len(required):]
    if not rest:
        return True
    rest = rest.strip()
    if not rest:
        return True
    if rest.startswith("("):
        closing = rest.find(")")
        if closing == -1:
            return False
        rest = rest[closing + 1:].strip()
        if not rest:
            return True
    return rest[0] in _SEPARATOR_CHARS


def _section_present_in_output(required: str, output: str) -> bool:
    """required section ปรากฏเป็น heading ใน output บางบรรทัดหรือไม่."""
    for line in output.split("\n"):
        if _line_is_heading_for(required, line):
            return True
    return False


def _validate_json_against_schema(data: Any, schema: dict, path: str = "") -> tuple[bool, str]:
    """Validate a parsed JSON value against a subset of JSON Schema.

    Supports the constructs used by our agent schemas: object, array, string,
    number, boolean, required fields, additionalProperties, and nested items.
    """
    stype = schema.get("type")
    if stype == "object":
        if not isinstance(data, dict):
            return False, f"{path or 'root'} ต้องเป็น object"
        required = schema.get("required", [])
        for key in required:
            if key not in data:
                return False, f"{path} ขาดฟิลด์: {key}"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in properties:
                    return False, f"{path} มีฟิลด์เกิน: {key}"
        for key, value in data.items():
            if key in properties:
                child_path = f"{path}.{key}" if path else key
                ok, err = _validate_json_against_schema(value, properties[key], child_path)
                if not ok:
                    return False, err
        return True, ""
    if stype == "array":
        if not isinstance(data, list):
            return False, f"{path} ต้องเป็น array"
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(data):
                ok, err = _validate_json_against_schema(item, items_schema, f"{path}[{i}]")
                if not ok:
                    return False, err
        return True, ""
    if stype == "string":
        if not isinstance(data, str):
            return False, f"{path} ต้องเป็น string"
        return True, ""
    if stype == "number":
        if not isinstance(data, (int, float)) or isinstance(data, bool):
            return False, f"{path} ต้องเป็น number"
        return True, ""
    if stype == "boolean":
        if not isinstance(data, bool):
            return False, f"{path} ต้องเป็น boolean"
        return True, ""
    return True, ""


def _validate_json(output: str) -> tuple[bool, str]:
    if _output_is_blank(output):
        return False, "output ว่างเปล่า"
    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        return False, f"ไม่ใช่ JSON: {e}"
    return _validate_json_against_schema(data, CONTENT_RESPONSE_SCHEMA["schema"])


def _validate_markdown(output: str, required_sections: list[str] | None = None) -> tuple[bool, str]:
    if _output_is_blank(output):
        return False, "output ว่างเปล่า"
    for s in required_sections or []:
        if not _section_present_in_output(s, output):
            return False, f"ไม่พบ section: {s}"
    return True, ""


_CITATION_KEYWORDS = [
    "แหล่งอ้างอิง",
    "อ้างอิง",
    "url",
    "verify",
    "citation",
    "source",
    "reference",
    "แหล่งที่มา",
    "เครดิต",
    "link",
]


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BARE_URL_RE = re.compile(r"https?://\S+")


def _is_citation_label(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _CITATION_KEYWORDS)


def _strip_citations_and_urls(output: str) -> str:
    """ลบ markdown links, bare URLs ออกเพื่อวัดเนื้อหาจริง."""
    text = _MD_LINK_RE.sub("", output)
    text = _BARE_URL_RE.sub("", text)
    return text


def _count_analysis_blocks(output: str) -> int:
    """นับ structural analysis signals หลายรูปแบบ (heading, bold label, table)."""
    lines = output.split("\n")
    blocks = 0
    in_table = False

    for line in lines:
        # Markdown heading: ## หรือ ###
        if re.match(r"^#{1,3}\s+\S", line):
            blocks += 1
            continue

        # Bold section label: **หัวข้อ:** หรือ **หัวข้อ**
        m = re.match(r"^\s*\*\*(.+?)\*\*\s*$", line)
        if m and not _is_citation_label(m.group(1)):
            blocks += 1
            continue

        # Table block: นับต่อเนื่องเป็นกลุ่มเดียว
        if re.match(r"^\s*\|.*\|.*\|", line):
            if not in_table:
                in_table = True
                blocks += 1
            continue
        in_table = False

    return blocks


def _validate_quality(output: str, quality: dict[str, Any]) -> tuple[bool, str]:
    """Validate against an output_quality contract (minimum quality gate)."""
    min_total = quality.get("min_total_chars", 0)
    if min_total and len(output) < min_total:
        return False, f"output สั้นเกินไป ({len(output)} < {min_total})"

    stripped = _strip_citations_and_urls(output)
    non_citation_len = len(stripped.replace(" ", "").replace("\n", ""))

    if quality.get("forbid_citation_only", False) and non_citation_len == 0:
        return False, "output มากกว่าครึ่งเป็น citations/URLs ไม่มีเนื้อหาวิเคราะหา"

    min_non = quality.get("min_non_citation_chars", 0)
    if min_non and non_citation_len < min_non:
        return False, f"เนื้อหาทีไม่ใช่ citations น้อยเกินไป ({non_citation_len} < {min_non})"

    min_blocks = quality.get("min_analysis_blocks", 0)
    if min_blocks:
        block_count = _count_analysis_blocks(output)
        if block_count < min_blocks:
            return False, f"output ขาด structural analysis blocks ({block_count} < {min_blocks})"

    return True, ""


def validate_output(
    agent_name: str,
    output: str,
    required_sections: list[str] | None = None,
    output_quality: dict[str, Any] | None = None,
    context_flags: dict | None = None,
) -> tuple[bool, str]:
    """Return (ok, error_message) for an agent's raw text output.

    ``context_flags`` is accepted for backward compatibility but no longer
    used — deterministic Markdown/regex guardrails were removed in favour of
    keeping all policy rules in the agent's system prompt.
    """
    cfg = load_config()
    agent_cfg = cfg.get("agents", {}).get(agent_name, {})
    required = required_sections if required_sections is not None else agent_cfg.get("required_output_sections")
    if required:
        ok, err = _validate_markdown(output, required)
        if not ok:
            return ok, err
    elif agent_cfg.get("output_format") == "json":
        ok, err = _validate_json(output)
        if not ok:
            return ok, err

    if output_quality:
        ok, err = _validate_quality(output, output_quality)
        if not ok:
            return ok, err

    return True, ""


def validate_content_output(output: str | dict) -> tuple[bool, str]:
    """Validate a content_creator JSON artifact (raw string or parsed dict) against CONTENT_ARTIFACT_SCHEMA.

    This is the strict gate for the final saved content artifact — including any
    optional fields added after generation (e.g. script_review).
    """
    data: Any
    if isinstance(output, str):
        if _output_is_blank(output):
            return False, "output ว่างเปล่า"
        try:
            data = json.loads(output)
        except json.JSONDecodeError as e:
            return False, f"ไม่ใช่ JSON: {e}"
    else:
        data = output
    return _validate_json_against_schema(data, CONTENT_ARTIFACT_SCHEMA["schema"])


# ---------------------------------------------------------------------------
# M6 Generic Remediation — source-driven validators (no hardcoded vocabulary)
#
# Design: these validators read from runtime source data (quick_brief, brand
# rules from terms.json/voice.json) — never from hardcoded product/category
# lists. Error prefixes encode repair policy for BaseAgent classification.
# ---------------------------------------------------------------------------

# One-page compactness target (lines). Chosen from the empirical Frontier
# benchmark: the Frontier S1 output at 55 lines was accepted as "good one-page"
# by the M6.1 judge. The upper bound 70 gives the model room while still being
# meaningfully "one page". Over 70 triggers a single compression repair.
_ONE_PAGE_TARGET_LINES = 70
_ONE_PAGE_KEYWORDS = ("one-page", "one page", "หน้าเดียว")


def _has_one_page_intent(quick_brief: str) -> bool:
    """Detect explicit one-page intent in quick_brief."""
    if not quick_brief:
        return False
    lowered = quick_brief.lower()
    return any(kw in lowered for kw in _ONE_PAGE_KEYWORDS)


def normalize_one_page_brief(quick_brief: str) -> str:
    """Translate 'one-page' intent into a concrete compactness target.

    If the quick_brief contains explicit one-page intent, append a concrete
    line target so the model has a measurable instruction instead of a vague
    "one-page". If no one-page intent, return unchanged.

    This is a prompt-level steer — it does NOT enforce a hard line count.
    The deterministic check is in validate_one_page_compactness.
    """
    if not _has_one_page_intent(quick_brief):
        return quick_brief
    target = _ONE_PAGE_TARGET_LINES
    return (
        f"{quick_brief}\n"
        f"หมายเหตุ: 'one-page' หมายถึงสเปคหนึ่งหน้า A4 กระชับ "
        f"ประมาณ {target} บรรทัด ไม่ใช่เอกสารหลายหน้า "
        f"— ใช้เฉพาะข้อมูลที่สำคัญที่สุด"
    )


def validate_one_page_compactness(output: str, quick_brief: str) -> tuple[bool, str]:
    """Check line count when one-page intent is active.

    Returns (ok, error). If one-page intent is NOT active, always passes.
    If one-page intent IS active and line count exceeds the target, returns
    an error with 'one-page:' prefix for repair classification.

    This is a REPAIRABLE instruction-following issue — not a hard rejection.
    The repair loop in BaseAgent limits this to one targeted repair.
    """
    if not _has_one_page_intent(quick_brief):
        return True, ""
    if _output_is_blank(output):
        return True, ""  # blank check is handled elsewhere
    line_count = len(output.split("\n"))
    if line_count > _ONE_PAGE_TARGET_LINES:
        return False, (
            f"one-page: output ยาว {line_count} บรรทัด เกินเป้าหมาย one-page "
            f"({_ONE_PAGE_TARGET_LINES} บรรทัด) — กรุณาย่อให้กระชับ ใช้เฉพาะข้อมูลที่สำคัญที่สุด"
        )
    return True, ""


# ---------------------------------------------------------------------------
# Brand hard-rule deterministic enforcement
#
# Reads from runtime brand data (terms.json restricted/replacements,
# voice.json banned_phrases) — never from hardcoded vocabulary.
# ---------------------------------------------------------------------------


def apply_brand_replacements(output: str, brand_rules: BrandRules) -> str:
    """Auto-apply brand term replacements (old → new) deterministically.

    Zero LLM cost. Reads replacements from brand_rules.hard_dict['replacements'].
    Uses a single-pass regex replacement to prevent cascade effects where
    a replacement value contains a substring matching another replacement key.

    Returns the output with replacements applied. If no brand rules or
    no replacements, returns output unchanged.
    """
    if not brand_rules or not brand_rules.hard_dict:
        return output
    replacements = brand_rules.hard_dict.get("replacements")
    if not replacements:
        return output
    # Single-pass: build a regex that matches any old key, replace with
    # the corresponding new value. Each position in the string is matched
    # at most once, so replacement values cannot be re-processed.
    pattern = re.compile("|".join(re.escape(old) for old in replacements if old))
    return pattern.sub(lambda m: replacements[m.group(0)], output)


def validate_brand_hard(output: str, brand_rules: BrandRules) -> tuple[bool, str]:
    """Check output for restricted/banned phrases from runtime brand data.

    Returns (ok, error). If a restricted or banned phrase is found in the
    output, returns an error with 'brand-hard:' prefix for repair
    classification. Replacements are handled separately by
    apply_brand_replacements (auto-applied before validation).

    This is a REPAIRABLE issue — the repair loop in BaseAgent limits this
    to one targeted repair. After that, the error is soft-accepted.
    """
    if not brand_rules or not brand_rules.hard_dict:
        return True, ""
    violations: list[str] = []
    restricted = brand_rules.hard_dict.get("restricted", [])
    for phrase in restricted:
        if phrase and phrase in output:
            violations.append(f"คำต้องห้าม '{phrase}'")
    banned = brand_rules.hard_dict.get("banned_phrases", [])
    for phrase in banned:
        if phrase and phrase in output:
            violations.append(f"วลีต้องห้าม '{phrase}'")
    if violations:
        return False, (
            f"brand-hard: พบคำ/วลีต้องห้ามใน output — {'; '.join(violations)} — "
            f"กรุณาลบหรือเปลี่ยนคำที่ต้องห้ามออกจาก output"
        )
    return True, ""

