"""Tests for pillar_manager — Content Pillar rotation system.

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_get_pillar_usage_empty_history():
    """History ว่าง → ทุก pillar ใช้ 0 ครั้ง."""
    from src.pillar_manager import get_pillar_usage

    pillars = ["รีวิว", "เปรียบเทียบ", "ทดสอบ"]
    history = {"entries": []}
    usage = get_pillar_usage(history, pillars)
    assert usage == {"รีวิว": 0, "เปรียบเทียบ": 0, "ทดสอบ": 0}, f"Expected all 0, got {usage}"


def test_get_pillar_usage_with_entries():
    """มี entries → นับถูกตาม pillar field."""
    from src.pillar_manager import get_pillar_usage

    pillars = ["รีวิว", "เปรียบเทียบ", "ทดสอบ"]
    history = {
        "entries": [
            {"pillar": "รีวิว", "concept": "K5 รีวิว"},
            {"pillar": "รีวิว", "concept": "K2 รีวิว"},
            {"pillar": "เปรียบเทียบ", "concept": "K2 vs K9"},
            {"concept": "ไม่มี pillar field"},  # entry เก่า ไม่มี pillar
        ]
    }
    usage = get_pillar_usage(history, pillars)
    assert usage == {"รีวิว": 2, "เปรียบเทียบ": 1, "ทดสอบ": 0}, f"Got {usage}"


def test_get_pillar_usage_unknown_pillar():
    """Entry มี pillar นอก list → ไม่นับ (ไม่ crash)."""
    from src.pillar_manager import get_pillar_usage

    pillars = ["รีวิว", "เปรียบเทียบ"]
    history = {"entries": [{"pillar": "unknown", "concept": "x"}]}
    usage = get_pillar_usage(history, pillars)
    assert usage == {"รีวิว": 0, "เปรียบเทียบ": 0}, f"Got {usage}"


def test_build_pillar_context_empty_usage():
    """Usage ทั้งหมด 0 → บอก LLM ว่ายังไม่เคยทำเลย."""
    from src.pillar_manager import build_pillar_context

    pillars = ["รีวิว", "เปรียบเทียบ", "ทดสอบ"]
    usage = {"รีวิว": 0, "เปรียบเทียบ": 0, "ทดสอบ": 0}
    ctx = build_pillar_context(pillars, usage)
    assert "รีวิว" in ctx
    assert "เปรียบเทียบ" in ctx
    assert "ทดสอบ" in ctx
    assert "0" in ctx


def test_build_pillar_context_with_usage():
    """มี usage → บอก LLM ว่า pillar ไหนใช้บ่อย ไหนยังไม่ค่อย."""
    from src.pillar_manager import build_pillar_context

    pillars = ["รีวิว", "เปรียบเทียบ", "ทดสอบ"]
    usage = {"รีวิว": 5, "เปรียบเทียบ": 1, "ทดสอบ": 0}
    ctx = build_pillar_context(pillars, usage)
    assert "5" in ctx
    assert "1" in ctx
    assert "0" in ctx
    assert "น้อย" in ctx or "ยังไม่" in ctx or "แนะนำ" in ctx


def test_build_pillar_context_empty_pillars():
    """ไม่มี pillars → คืน string ว่าง (ไม่บังคับ)."""
    from src.pillar_manager import build_pillar_context

    ctx = build_pillar_context([], {})
    assert ctx == ""


def test_infer_pillar_by_name():
    """Pillar name อยู่ใน concept → คืน pillar นั้น."""
    from src.pillar_manager import infer_pillar

    pillars = ["รีวิวสินค้า", "เปรียบเทียบ", "ทดสอบความทน"]
    keywords = {"รีวิวสินค้า": ["รีวิว"], "เปรียบเทียบ": ["เทียบ"], "ทดสอบความทน": ["ทดสอบ"]}
    result = infer_pillar("คอนเทนต์ เปรียบเทียบ K2 กับ K9", pillars, keywords)
    assert result == "เปรียบเทียบ"


def test_infer_pillar_by_keyword():
    """Pillar name ไม่อยู่ใน concept แต่ keyword อยู่ → คืน pillar ที่ match."""
    from src.pillar_manager import infer_pillar

    pillars = ["รีวิวสินค้า", "เปรียบเทียบ", "ทดสอบความทน"]
    keywords = {"รีวิวสินค้า": ["รีวิว"], "เปรียบเทียบ": ["เทียบ"], "ทดสอบความทน": ["ทดสอบ", "ทิ้งน้ำ"]}
    result = infer_pillar("ทิ้งน้ำ K5 30 นาที", pillars, keywords)
    assert result == "ทดสอบความทน"


def test_infer_pillar_no_match():
    """ไม่ match อะไรเลย → คืน string ว่าง."""
    from src.pillar_manager import infer_pillar

    pillars = ["รีวิวสินค้า", "เปรียบเทียบ"]
    keywords = {"รีวิวสินค้า": ["รีวิว"], "เปรียบเทียบ": ["เทียบ"]}
    result = infer_pillar("สวัสดีครับ", pillars, keywords)
    assert result == ""


def test_infer_pillar_empty_concept():
    """Concept ว่าง → คืน string ว่าง."""
    from src.pillar_manager import infer_pillar

    result = infer_pillar("", ["รีวิว"], {"รีวิว": ["รีวิว"]})
    assert result == ""


if __name__ == "__main__":
    tests = [
        test_get_pillar_usage_empty_history,
        test_get_pillar_usage_with_entries,
        test_get_pillar_usage_unknown_pillar,
        test_build_pillar_context_empty_usage,
        test_build_pillar_context_with_usage,
        test_build_pillar_context_empty_pillars,
        test_infer_pillar_by_name,
        test_infer_pillar_by_keyword,
        test_infer_pillar_no_match,
        test_infer_pillar_empty_concept,
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
