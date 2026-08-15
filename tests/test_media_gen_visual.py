"""Tests for media_gen visual injection — แป๊ะ visual keywords ต่อท้าย prompt.

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)

Seam ที่ทดสอบ:
  build_visual_suffix(visual: dict) -> str
    — รับ visual.json dict → สร้าง suffix string แป๊ะต่อท้าย image/video prompt

กรณีทดสอบ:
  - มี keywords + avoid → suffix มีทั้งคู่
  - มีแค่ keywords → suffix มีแค่ keywords
  - visual ว่าง → คืน string ว่าง (ไม่แป๊ะ)
  - มี colors → suffix มี color hints
  - มี image_style.tone → suffix มี tone hint
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_build_visual_suffix_with_keywords_and_avoid():
    """มี keywords + avoid → suffix มีทั้งคู่."""
    from src.media_gen import build_visual_suffix

    visual = {
        "keywords": ["soft light", "warm tone", "family"],
        "avoid": ["dark", "gloomy"],
    }
    suffix = build_visual_suffix(visual)

    assert "soft light" in suffix, f"keywords missing: {suffix}"
    assert "warm tone" in suffix
    assert "family" in suffix
    assert "dark" in suffix, f"avoid missing: {suffix}"
    assert "gloomy" in suffix


def test_build_visual_suffix_only_keywords():
    """มีแค่ keywords (ไม่มี avoid) → suffix มีแค่ keywords ไม่ crash."""
    from src.media_gen import build_visual_suffix

    visual = {"keywords": ["bright", "clean"]}
    suffix = build_visual_suffix(visual)

    assert "bright" in suffix
    assert "clean" in suffix


def test_build_visual_suffix_empty_dict():
    """visual ว่าง → คืน string ว่าง (ไม่แป๊ะอะไร)."""
    from src.media_gen import build_visual_suffix

    suffix = build_visual_suffix({})
    assert suffix == "", f"Expected empty, got: {suffix!r}"


def test_build_visual_suffix_with_colors():
    """มี colors → suffix มี color hints."""
    from src.media_gen import build_visual_suffix

    visual = {
        "colors": {"primary": "#1a73e8", "secondary": "#34a853"},
        "keywords": [],
    }
    suffix = build_visual_suffix(visual)

    assert "#1a73e8" in suffix, f"primary color missing: {suffix}"
    assert "#34a853" in suffix


def test_build_visual_suffix_with_image_style_tone():
    """มี image_style.tone → suffix มี tone hint."""
    from src.media_gen import build_visual_suffix

    visual = {
        "image_style": {"tone": "อบอุ่น"},
        "keywords": [],
    }
    suffix = build_visual_suffix(visual)

    assert "อบอุ่น" in suffix, f"tone missing: {suffix}"


def test_build_visual_suffix_no_keywords_no_colors():
    """มีแค่ avoid (ไม่มี keywords, colors) → suffix มีแค่ avoid."""
    from src.media_gen import build_visual_suffix

    visual = {"avoid": ["scary", "cheap"]}
    suffix = build_visual_suffix(visual)

    assert "scary" in suffix
    assert "cheap" in suffix


if __name__ == "__main__":
    tests = [
        test_build_visual_suffix_with_keywords_and_avoid,
        test_build_visual_suffix_only_keywords,
        test_build_visual_suffix_empty_dict,
        test_build_visual_suffix_with_colors,
        test_build_visual_suffix_with_image_style_tone,
        test_build_visual_suffix_no_keywords_no_colors,
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
