"""Tests for brand_priority — แยก brand rules เป็น hard (brand ชนะ) / soft (user ชนะ).

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)

Seams ที่ทดสอบ:
  1. load_brand_priority(brand_dir) -> BrandRules
     — แยก voice.json + terms.json เป็น hard/soft
  2. detect_conflicts(brand_rules, instructions) -> list[Conflict]
     — หา conflict ระหว่าง brand และ user instructions
  3. build_priority_prompt(brand_rules, instructions) -> str
     — สร้าง prompt ที่บอก LLM ลำดับชัดเจน (hard ชนะ, soft ใช้ user ถ้ามี)

กรณีทดสอบ:
  - load_brand_priority: มี voice + terms → แยก hard (banned/restricted/replacements) / soft (personality/tone/formality/language)
  - load_brand_priority: ไม่มีไฟล์ → คืน BrandRules ว่าง
  - detect_conflicts: tone ขัด → soft conflict
  - detect_conflicts: banned phrase อยู่ใน custom → hard conflict
  - detect_conflicts: ไม่มีขัด → คืน list ว่าง
  - build_priority_prompt: hard rules อยู่ก่อน soft + บอกลำดับชัด
"""
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# load_brand_priority — แยก voice.json + terms.json เป็น hard/soft
# ---------------------------------------------------------------------------

def test_load_brand_priority_splits_hard_soft():
    """มี voice.json + terms.json → แยก field ได้ถูกต้อง."""
    from src.brand_priority import load_brand_priority

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "voice.json").write_text(json.dumps({
            "personality": "เป็นมิตร อบอุ่น",
            "tone_description": "กลาง-เป็นทางการเล็กน้อย",
            "formality_level": 3,
            "language": "ไทย",
            "banned_phrases": ["ถูกที่สุด", "ของแถม"],
            "examples": ["ตัวอย่างโพสต์"],
        }), encoding="utf-8")
        (brand_dir / "terms.json").write_text(json.dumps({
            "approved": ["คุณภาพ", "ความคุ้มค่า"],
            "restricted": ["ลดแหลก", "ฟรี"],
            "replacements": {"ถูก": "คุ้มค่า"},
        }), encoding="utf-8")

        rules = load_brand_priority(brand_dir)

        # hard: banned_phrases + restricted + replacements
        assert "ถูกที่สุด" in rules.hard
        assert "ของแถม" in rules.hard
        assert "ลดแหลก" in rules.hard
        assert "ฟรี" in rules.hard
        assert "ถูก" in rules.hard  # from replacements key
        assert "คุ้มค่า" in rules.hard  # replacement value

        # soft: personality + tone + formality + language + approved (คำแนะนำ ไม่ใช่ guardrail)
        assert "เป็นมิตร" in rules.soft
        assert "อบอุ่น" in rules.soft
        assert "กลาง-เป็นทางการ" in rules.soft
        assert "ไทย" in rules.soft
        assert "คุณภาพ" in rules.soft  # approved moved to soft
        assert "ความคุ้มค่า" in rules.soft

        # hard_dict / soft_dict ต้องมีข้อมูลดิบ
        assert "ถูกที่สุด" in rules.hard_dict.get("banned_phrases", [])
        assert "ลดแหลก" in rules.hard_dict.get("restricted", [])
        assert rules.soft_dict.get("personality") == "เป็นมิตร อบอุ่น"
        assert "คุณภาพ" in rules.soft_dict.get("approved", [])


def test_load_brand_priority_empty_when_no_files():
    """ไม่มีไฟล์เลย → คืน BrandRules ทุก field ว่าง."""
    from src.brand_priority import load_brand_priority, BrandRules

    with tempfile.TemporaryDirectory() as tmp:
        rules = load_brand_priority(Path(tmp))
        assert isinstance(rules, BrandRules)
        assert rules.hard == ""
        assert rules.soft == ""
        assert rules.hard_dict == {}
        assert rules.soft_dict == {}


def test_load_brand_priority_only_voice():
    """มีแค่ voice.json → hard มี banned_phrases, soft มี personality/tone."""
    from src.brand_priority import load_brand_priority

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "voice.json").write_text(json.dumps({
            "personality": "เพื่อน",
            "banned_phrases": ["คำห้าม"],
        }), encoding="utf-8")

        rules = load_brand_priority(brand_dir)
        assert "คำห้าม" in rules.hard
        assert "เพื่อน" in rules.soft
        # ไม่มี terms.json → ไม่มี restricted
        assert "restricted" not in rules.hard_dict


# ---------------------------------------------------------------------------
# detect_conflicts — หา conflict ระหว่าง brand และ instructions
# ---------------------------------------------------------------------------

def test_detect_conflicts_tone_mismatch_soft():
    """brand tone 'อบอุ่น' vs user tone ['aggressive'] → soft conflict."""
    from src.brand_priority import detect_conflicts, BrandRules

    rules = BrandRules(
        hard="คำต้องห้าม: ถูกที่สุด",
        soft="บุคลิก: อบอุ่น\nโทนเสียง: อบอุ่น เป็นมิตร",
        hard_dict={"banned_phrases": ["ถูกที่สุด"], "restricted": []},
        soft_dict={"personality": "อบอุ่น", "tone_description": "อบอุ่น เป็นมิตร"},
    )
    instructions = {"tone": ["aggressive", "bold"]}

    conflicts = detect_conflicts(rules, instructions)
    assert len(conflicts) >= 1
    tone_conflict = [c for c in conflicts if c.field == "tone"]
    assert len(tone_conflict) == 1
    assert tone_conflict[0].severity == "soft"
    assert "อบอุน" in str(tone_conflict[0].brand_value) or "อบอุ่น" in str(tone_conflict[0].brand_value)


def test_detect_conflicts_banned_in_custom_hard():
    """banned phrase 'ถูกที่สุด' อยู่ใน custom instruction → hard conflict."""
    from src.brand_priority import detect_conflicts, BrandRules

    rules = BrandRules(
        hard="คำต้องห้าม: ถูกที่สุด",
        soft="",
        hard_dict={"banned_phrases": ["ถูกที่สุด"], "restricted": []},
        soft_dict={},
    )
    instructions = {"custom": "ใช้คำว่าถูกที่สุดเพื่อเน้นจุดขาย"}

    conflicts = detect_conflicts(rules, instructions)
    hard_conflicts = [c for c in conflicts if c.severity == "hard"]
    assert len(hard_conflicts) >= 1
    assert any("ถูกที่สุด" in str(c.brand_value) for c in hard_conflicts)


def test_detect_conflicts_restricted_in_custom_hard():
    """restricted term 'ลดแหลก' อยู่ใน custom → hard conflict."""
    from src.brand_priority import detect_conflicts, BrandRules

    rules = BrandRules(
        hard="คำต้องห้าม: ลดแหลก",
        soft="",
        hard_dict={"banned_phrases": [], "restricted": ["ลดแหลก"]},
        soft_dict={},
    )
    instructions = {"custom": "เน้นว่าลดแหลกวันนี้"}

    conflicts = detect_conflicts(rules, instructions)
    hard_conflicts = [c for c in conflicts if c.severity == "hard"]
    assert len(hard_conflicts) >= 1


def test_detect_conflicts_no_conflict():
    """brand กับ instructions ไม่ขัด → คืน list ว่าง."""
    from src.brand_priority import detect_conflicts, BrandRules

    rules = BrandRules(
        hard="คำต้องห้าม: ถูกที่สุด",
        soft="บุคลิก: อบอุ่น",
        hard_dict={"banned_phrases": ["ถูกที่สุด"], "restricted": []},
        soft_dict={"personality": "อบอุ่น", "tone_description": "อบอุ่น"},
    )
    instructions = {"tone": ["friendly"], "custom": "เน้นคุณภาพ"}

    conflicts = detect_conflicts(rules, instructions)
    assert conflicts == []


def test_detect_conflicts_language_mismatch():
    """brand language 'ไทย' vs user language 'english' → soft conflict."""
    from src.brand_priority import detect_conflicts, BrandRules

    rules = BrandRules(
        hard="", soft="ภาษา: ไทย",
        hard_dict={}, soft_dict={"language": "ไทย"},
    )
    instructions = {"language": "english"}
    conflicts = detect_conflicts(rules, instructions)
    lang_conflicts = [c for c in conflicts if c.field == "language"]
    assert len(lang_conflicts) == 1
    assert lang_conflicts[0].severity == "soft"


def test_detect_conflicts_sell_style_hard_with_soft_brand():
    """brand personality 'อบอุ่น' vs user sell_style 'hard' → soft conflict."""
    from src.brand_priority import detect_conflicts, BrandRules

    rules = BrandRules(
        hard="", soft="บุคลิก: อบอุ่น เป็นมิตร",
        hard_dict={}, soft_dict={"personality": "อบอุ่น เป็นมิตร"},
    )
    instructions = {"sell_style": "hard"}
    conflicts = detect_conflicts(rules, instructions)
    sell_conflicts = [c for c in conflicts if c.field == "sell_style"]
    assert len(sell_conflicts) == 1
    assert sell_conflicts[0].severity == "soft"


def test_detect_conflicts_hook_style_controversial_with_soft_brand():
    """brand personality 'อบอุ่น' vs user hook_style 'controversial' → soft conflict."""
    from src.brand_priority import detect_conflicts, BrandRules

    rules = BrandRules(
        hard="", soft="บุคลิก: อบอุ่น",
        hard_dict={}, soft_dict={"personality": "อบอุ่น"},
    )
    instructions = {"hook_style": "controversial"}
    conflicts = detect_conflicts(rules, instructions)
    hook_conflicts = [c for c in conflicts if c.field == "hook_style"]
    assert len(hook_conflicts) == 1
    assert hook_conflicts[0].severity == "soft"


# ---------------------------------------------------------------------------
# build_priority_prompt — สร้าง prompt ที่บอก LLM ลำดับชัดเจน
# ---------------------------------------------------------------------------

def test_build_priority_prompt_hard_before_soft():
    """prompt ต้องมี hard rules ก่อน soft และบอกลำดับชัด."""
    from src.brand_priority import build_priority_prompt, BrandRules

    rules = BrandRules(
        hard="คำต้องห้าม: ถูกที่สุด",
        soft="บุคลิก: อบอุ่น",
        hard_dict={"banned_phrases": ["ถูกที่สุด"]},
        soft_dict={"personality": "อบอุ่น"},
    )
    prompt = build_priority_prompt(rules, instructions={})

    # hard อยู่ก่อน soft
    assert prompt.index("ถูกที่สุด") < prompt.index("อบอุ่น")
    # บอกลำดับชัด
    assert "กฎบังคับ" in prompt or "ห้าม override" in prompt.lower() or "ชนะเสมอ" in prompt


def test_build_priority_prompt_empty_when_no_rules():
    """ไม่มี brand rules → คืน string ว่าง."""
    from src.brand_priority import build_priority_prompt, BrandRules

    rules = BrandRules(hard="", soft="", hard_dict={}, soft_dict={})
    prompt = build_priority_prompt(rules, instructions={})
    assert prompt == ""


def test_build_priority_prompt_soft_uses_user_when_set():
    """มี user tone → prompt บอกใช้ user tone แทน brand tone (soft)."""
    from src.brand_priority import build_priority_prompt, BrandRules

    rules = BrandRules(
        hard="",
        soft="บุคลิก: อบอุ่น\nโทนเสียง: อบอุ่น",
        hard_dict={},
        soft_dict={"personality": "อบอุ่น", "tone_description": "อบอุ่น"},
    )
    instructions = {"tone": ["aggressive"]}
    prompt = build_priority_prompt(rules, instructions)

    # บอกว่า user สามารถ override soft ได้
    assert "override" in prompt.lower() or "แทน" in prompt or "user" in prompt.lower() or "ผู้ใช้" in prompt
