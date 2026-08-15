"""Tests for brand_loader — ระบบโหลดข้อมูลแบรนด์แยก 3 ชั้น (rules / reference / visual).

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)

Seams ที่ทดสอบ (ตกลงแล้ว):
  1. load_brand_rules(brand_dir) -> str    — voice.json + terms.json → rules string
  2. load_brand_reference(brand_dir) -> str — profile.md + audience.json → reference string
  3. load_brand_visual(brand_dir) -> dict   — visual.json → dict สำหรับ media_gen

กรณีทดสอบ:
  - มีไฟล์ครบ → คืนค่าถูกต้อง
  - มีแค่บางไฟล์ → คืนเฉพาะที่มี (ไม่ crash)
  - ไม่มี brand/ เลย → คืน empty (string ว่าง / dict ว่าง)
  - มี .md เดิมแต่ไม่มี .json → ยังไม่ migrate (คืน empty, ไม่ auto-migrate ใน loader)
"""
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# load_brand_rules — voice.json + terms.json → rules string
# ---------------------------------------------------------------------------

def test_load_brand_rules_with_voice_and_terms():
    """มี voice.json + terms.json → คืน rules string ที่มีเนื้อหาทั้งสองไฟล์."""
    from src.brand_loader import load_brand_rules

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "voice.json").write_text(json.dumps({
            "personality": "เป็นมิตร",
            "tone_description": "อบอุ่น",
            "formality_level": 3,
            "banned_phrases": ["คำต้องห้าม"],
        }), encoding="utf-8")
        (brand_dir / "terms.json").write_text(json.dumps({
            "approved": ["คำดี"],
            "restricted": ["คำไม่ดี"],
        }), encoding="utf-8")

        result = load_brand_rules(brand_dir)

        assert "เป็นมิตร" in result, f"voice personality missing: {result}"
        assert "อบอุ่น" in result, f"tone_description missing: {result}"
        assert "คำดี" in result, f"approved terms missing: {result}"
        assert "คำไม่ดี" in result, f"restricted terms missing: {result}"


def test_load_brand_rules_only_voice():
    """มีแค่ voice.json (ไม่มี terms.json) → คืนเฉพาะ voice ไม่ crash."""
    from src.brand_loader import load_brand_rules

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "voice.json").write_text(json.dumps({
            "personality": "เป็นมิตร",
        }), encoding="utf-8")

        result = load_brand_rules(brand_dir)

        assert "เป็นมิตร" in result
        # ไม่ crash แม้ไม่มี terms.json


def test_load_brand_rules_empty_dir():
    """ไม่มีไฟล์เลย → คืน string ว่าง."""
    from src.brand_loader import load_brand_rules

    with tempfile.TemporaryDirectory() as tmp:
        result = load_brand_rules(Path(tmp))
        assert result == "", f"Expected empty string, got: {result!r}"


def test_load_brand_rules_no_dir():
    """ไม่มี brand/ directory → คืน string ว่าง (ไม่ crash)."""
    from src.brand_loader import load_brand_rules

    result = load_brand_rules(Path("/nonexistent/path/brand_xyz"))
    assert result == ""


# ---------------------------------------------------------------------------
# load_brand_reference — profile.md + audience.json → reference string
# ---------------------------------------------------------------------------

def test_load_brand_reference_with_profile_and_audience():
    """มี profile.md + audience.json → คืน reference string ที่มีทั้งสอง."""
    from src.brand_loader import load_brand_reference

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "brand_profile.md").write_text("# แบรนด์ X\n\nประวัติบริษัท...", encoding="utf-8")
        (brand_dir / "audience.json").write_text(json.dumps({
            "primary": {"age": "30-45", "role": "ผู้ปกครอง"},
            "pain_points": ["ความปลอดภัย"],
        }), encoding="utf-8")

        result = load_brand_reference(brand_dir)

        assert "แบรนด์ X" in result, f"profile missing: {result}"
        assert "ประวัติบริษัท" in result
        assert "ผู้ปกครอง" in result, f"audience missing: {result}"
        assert "ความปลอดภัย" in result


def test_load_brand_reference_only_profile():
    """มีแค่ brand_profile.md → คืนเฉพาะ profile ไม่ crash."""
    from src.brand_loader import load_brand_reference

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "brand_profile.md").write_text("# แบรนด์ Y", encoding="utf-8")

        result = load_brand_reference(brand_dir)

        assert "แบรนด์ Y" in result


def test_load_brand_reference_empty_dir():
    """ไม่มีไฟล์ → คืน string ว่าง."""
    from src.brand_loader import load_brand_reference

    with tempfile.TemporaryDirectory() as tmp:
        result = load_brand_reference(Path(tmp))
        assert result == ""


# ---------------------------------------------------------------------------
# load_brand_visual — visual.json → dict สำหรับ media_gen
# ---------------------------------------------------------------------------

def test_load_brand_visual_with_full_data():
    """มี visual.json ครบ → คืน dict ที่มี colors, image_style, keywords, avoid."""
    from src.brand_loader import load_brand_visual

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "visual.json").write_text(json.dumps({
            "colors": {"primary": "#FF0000", "secondary": "#00FF00"},
            "image_style": {"tone": "อบอุ่น", "product_shot": "clean background"},
            "keywords": ["soft light", "warm tone"],
            "avoid": ["dark", "gloomy"],
        }), encoding="utf-8")

        result = load_brand_visual(brand_dir)

        assert isinstance(result, dict)
        assert result["colors"]["primary"] == "#FF0000"
        assert result["image_style"]["tone"] == "อบอุ่น"
        assert "soft light" in result["keywords"]
        assert "dark" in result["avoid"]


def test_load_brand_visual_empty_dir():
    """ไม่มี visual.json → คืน dict ว่าง (ไม่ crash)."""
    from src.brand_loader import load_brand_visual

    with tempfile.TemporaryDirectory() as tmp:
        result = load_brand_visual(Path(tmp))
        assert result == {}, f"Expected empty dict, got: {result}"


def test_load_brand_visual_no_dir():
    """ไม่มี brand/ directory → คืน dict ว่าง."""
    from src.brand_loader import load_brand_visual

    result = load_brand_visual(Path("/nonexistent/path/brand_xyz"))
    assert result == {}


def test_load_brand_visual_partial_data():
    """visual.json มีแค่ keywords (ไม่ครบ) → คืน dict ที่มีแค่ keywords."""
    from src.brand_loader import load_brand_visual

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "visual.json").write_text(json.dumps({
            "keywords": ["bright", "clean"],
        }), encoding="utf-8")

        result = load_brand_visual(brand_dir)

        assert result.get("keywords") == ["bright", "clean"]
        # ไม่มี colors ก็ไม่ crash


# ---------------------------------------------------------------------------
# Backward compat — load_brand_context ยังต้องทำงาน (delegates to rules)
# ---------------------------------------------------------------------------

def test_load_brand_context_still_works():
    """load_brand_context เดิมยังทำงานได้ — delegate ไป load_brand_rules."""
    from src.brand_loader import load_brand_context

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "voice.json").write_text(json.dumps({
            "personality": "เป็นมิตร",
        }), encoding="utf-8")

        result = load_brand_context(brand_dir)

        # ยังคืน string (ไม่ break call sites เดิม)
        assert isinstance(result, str)
        assert "เป็นมิตร" in result


# ---------------------------------------------------------------------------
# Auto-migration fallback — ถ้ามี .md แต่ไม่มี .json → migrate อัตโนมัติ
# ---------------------------------------------------------------------------

def test_auto_migrate_fallback_creates_json():
    """มี tone_of_voice.md แต่ไม่มี voice.json → loader เรียก migrate อัตโนมัติ."""
    from src.brand_loader import load_brand_rules

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        # มีแค่ .md (เหมือนก่อน migration)
        (brand_dir / "tone_of_voice.md").write_text(
            "# โทนเสียง\n\n## บุคลิกของแบรนด์ (Brand Personality)\nเป็นมิตร\n", encoding="utf-8")

        # เรียก loader — ควร auto-migrate แล้วคืน rules
        result = load_brand_rules(brand_dir)

        # หลัง auto-migrate → voice.json ต้องถูกสร้าง
        assert (brand_dir / "voice.json").exists(), "auto-migrate did not create voice.json"
        # และ loader คืน rules ที่มี personality
        assert "เป็นมิตร" in result, f"rules missing after auto-migrate: {result}"


def test_auto_migrate_fallback_visual():
    """มี visual_guidelines.md แต่ไม่มี visual.json → load_brand_visual auto-migrate."""
    from src.brand_loader import load_brand_visual

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "visual_guidelines.md").write_text(
            "# Visual\n\n## แนวทางสำหรับ AI Image/Video Prompt\n- ใช้ keyword: bright, clean\n",
            encoding="utf-8")

        result = load_brand_visual(brand_dir)

        assert (brand_dir / "visual.json").exists(), "auto-migrate did not create visual.json"
        assert "bright" in result.get("keywords", []), f"visual missing: {result}"


def test_auto_migrate_no_json_no_md_does_nothing():
    """ไม่มีทั้ง .md และ .json → ไม่ migrate ไม่ crash."""
    from src.brand_loader import load_brand_rules

    with tempfile.TemporaryDirectory() as tmp:
        # ไม่ใส่อะไรเลย
        result = load_brand_rules(Path(tmp))
        assert result == ""


def test_auto_migrate_does_not_overwrite_existing_json():
    """มี .json อยู่แล้ว → ไม่ migrate ซ้ำ (even if .md ยังอยู่)."""
    from src.brand_loader import load_brand_rules

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        # มีทั้ง .md และ .json (หลัง migrate แล้ว)
        (brand_dir / "tone_of_voice.md").write_text("# Old\n## บุคลิกของแบรนด์ (Brand Personality)\nOLD\n", encoding="utf-8")
        (brand_dir / "voice.json").write_text(json.dumps({"personality": "EXISTING"}), encoding="utf-8")

        result = load_brand_rules(brand_dir)

        # ใช้ .json ที่มีอยู่ ไม่ overwrite
        assert "EXISTING" in result
        assert "OLD" not in result


if __name__ == "__main__":
    tests = [
        test_load_brand_rules_with_voice_and_terms,
        test_load_brand_rules_only_voice,
        test_load_brand_rules_empty_dir,
        test_load_brand_rules_no_dir,
        test_load_brand_reference_with_profile_and_audience,
        test_load_brand_reference_only_profile,
        test_load_brand_reference_empty_dir,
        test_load_brand_visual_with_full_data,
        test_load_brand_visual_empty_dir,
        test_load_brand_visual_no_dir,
        test_load_brand_visual_partial_data,
        test_load_brand_context_still_works,
        test_auto_migrate_fallback_creates_json,
        test_auto_migrate_fallback_visual,
        test_auto_migrate_no_json_no_md_does_nothing,
        test_auto_migrate_does_not_overwrite_existing_json,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} passed")
    if failed > 0:
        sys.exit(1)
