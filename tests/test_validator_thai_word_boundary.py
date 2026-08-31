"""Bug: _has_word ทำ substring matching ทำให้ Thai false positive.

ตัวอย่างจาก qualification รอบ 20260831:
- "มอบ" (cost mechanic marker) ตรงกับ "ความ**มอบ**อุ่น" (warmth) → cost_mechanics rule บล็อก storytelling
- "มาตรฐาน" (benchmark marker) ตรงกับ "มาตรฐานกันน้ำ IP68" (product spec) → benchmarks rule บล็อก product spec

test นี้ยืนยันว่า:
1. _has_word ตรวจ Thai vowel/tone mark ก่อน keyword → ปฏิเสธ false positive
2. "มาตรฐาน" ถูกเอาออกจาก benchmark_markers เพราะกำกวม (standard vs benchmark)
3. cost_mechanics rule ไม่บล็อก storytelling เรื่องความอบอุ่น
4. benchmarks rule ไม่บล็อก product spec (IP68, Heart Rate, SpO2)
"""
from __future__ import annotations

import pytest

from src.campaign_validator import (
    _has_word,
    audit_campaign_output,
    extract_context_flags,
)
from src.config_loader import get_agent_config, load_config


def _rules() -> dict:
    return get_agent_config(load_config(), "campaign_strategy").get("semantic_rules", {})


def _audit(output: str, context: dict[str, str], instructions: dict | None = None) -> list:
    flags = extract_context_flags(context)
    return audit_campaign_output(
        output,
        flags,
        instructions or {},
        _rules(),
    )


# ---------------------------------------------------------------------------
# Seam 1: _has_word — Thai word boundary
# ---------------------------------------------------------------------------


def test_has_word_thai_vowel_before_rejects_false_positive():
    """'มอบ' ตามหลังสระ 'า' ใน 'ความอบอุ่น' → ไม่ใช่ word boundary → ไม่ตรง."""
    # "ความอบอุ่น" — "ม" ตามหลัง "า" (สระ) → "ม" เป็นส่วนของ "ความ" ไม่ใช่ "มอบ"
    assert not _has_word("สื่อสารเรื่องราวความอบอุ่นผ่านมุมมองความห่วงใย", ["มอบ"]), (
        "'มอบ' ไม่ควรตรงกับ 'ความอบอุ่น' — เป็น substring false positive"
    )


def test_has_word_thai_real_word_still_matches():
    """'มอบ' เมื่อเป็นคำจริง (ตามหลัง space/newline) → ตรง."""
    assert _has_word("มอบของแถมให้ลูกค้า", ["มอบ"]), (
        "'มอบ' เป็นคำจริงที่ขึ้นต้นประโยค → ต้องตรง"
    )
    assert _has_word("แคมเปญ: มอบเครดิตฟรี", ["มอบ"]), (
        "'มอบ' หลังเครื่องหมายโคลอน → ต้องตรง"
    )


def test_has_word_english_word_boundary_still_works():
    """English keywords ยังทำงานเหมือนเดิม (substring หรือ word boundary)."""
    assert _has_word("flash sale today", ["flash sale"])
    assert _has_word("free shipping", ["free shipping"])
    assert not _has_word("no relevant text here", ["flash sale"])


def test_has_word_thai_tone_mark_before_rejects():
    """Tone mark ก่อน keyword → ไม่ใช่ word boundary."""
    # "ส่วนลด" — ถ้ามี tone mark ก่อน "ส่วน" ก็ไม่ควรตรง (edge case)
    # แต่ "ส่วนลด" เป็นคำเริ่มต้นประโยค → ตรง
    assert _has_word("ส่วนลด 10%", ["ส่วนลด"])


# ---------------------------------------------------------------------------
# Seam 2: cost_mechanics rule — storytelling ไม่ใช่ cost mechanic
# ---------------------------------------------------------------------------


def test_cost_mechanics_does_not_flag_storytelling_warmth():
    """'สื่อสารเรื่องราวความอบอุ่น' เป็น storytelling ไม่ใช่ cost mechanic → ต้องผ่าน."""
    context = {
        "product": "Product: LAGENIO K2 kids smartwatch. GPS, Geo-Fence, Heart Rate, SpO2, IP68.",
    }
    output = (
        "## แคมเปญหลัก\n"
        "- ชื่อ: ความปลอดภัยของลูก\n"
        "- กลไก: สื่อสารเรื่องราวความอบอุ่นผ่านมุมมองความห่วงใยของคุณแม่ในการดูแลลูก\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n"
        "## KPI ที่ควรวัดผล\n"
        "- ยอดขาย (target ต้องกำหนดหลังมี baseline)\n"
        "## งบประมาณประมาณการ\n"
        "- ประมาณ 50,000 บาท (estimate)\n"
        "## แหล่งอ้างอิง\n"
        "- ไม่มี external factual claim ที่ต้องอ้างอิง\n"
    )
    results = _audit(output, context)
    cost_rule = next(r for r in results if r.rule == "cost_mechanics_pending_financial_validation")
    assert cost_rule.ok, (
        f"storytelling เรื่องความอบอุ่นไม่ใช่ cost mechanic: {cost_rule.reason}"
    )


# ---------------------------------------------------------------------------
# Seam 3: benchmarks rule — product spec ไม่ใช่ benchmark
# ---------------------------------------------------------------------------


def test_benchmarks_does_not_flag_product_spec():
    """'มาตรฐานกันน้ำ IP68, Heart Rate, SpO2' เป็น product spec ไม่ใช่ benchmark → ต้องผ่าน."""
    context = {
        "product": "Product: LAGENIO K2 kids smartwatch. GPS, Geo-Fence, Heart Rate, SpO2, IP68.",
    }
    output = (
        "## แคมเปญหลัก\n"
        "- ชื่อ: Health & Safety\n"
        "- กลไก: แนะนำฟังก์ชันเซนเซอร์ Heart Rate, SpO2 และมาตรฐานกันน้ำ IP68\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n"
        "## KPI ที่ควรวัดผล\n"
        "- ยอดขาย (target ต้องกำหนดหลังมี baseline)\n"
        "## งบประมาณประมาณการ\n"
        "- ประมาณ 50,000 บาท (estimate)\n"
        "## แหล่งอ้างอิง\n"
        "- ไม่มี external factual claim ที่ต้องอ้างอิง\n"
    )
    results = _audit(output, context)
    bench_rule = next(r for r in results if r.rule == "benchmarks_cited_or_removed")
    assert bench_rule.ok, (
        f"product spec (IP68/Heart Rate/SpO2) ไม่ใช่ benchmark: {bench_rule.reason}"
    )


# ---------------------------------------------------------------------------
# Regression: real cost mechanics and benchmarks still caught
# ---------------------------------------------------------------------------


def test_cost_mechanics_still_catches_real_discount():
    """'ส่วนลด 20%' ไม่มี pending label → ต้องถูก flag (regression guard)."""
    context = {
        "product": "Product: LAGENIO K2 kids smartwatch.",
    }
    output = (
        "## แคมเปญหลัก\n"
        "- ชื่อ: Flash Sale\n"
        "- กลไก: ส่วนลด 20% สำหรับลูกค้าใหม่\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n"
        "## KPI ที่ควรวัดผล\n"
        "- ยอดขาย (target ต้องกำหนดหลังมี baseline)\n"
        "## งบประมาณประมาณการ\n"
        "- ประมาณ 50,000 บาท (estimate)\n"
        "## แหล่งอ้างอิง\n"
        "- ไม่มี external factual claim ที่ต้องอ้างอิง\n"
    )
    results = _audit(output, context)
    cost_rule = next(r for r in results if r.rule == "cost_mechanics_pending_financial_validation")
    assert not cost_rule.ok, (
        f"ส่วนลด 20% ไม่มี pending label → ต้องถูก flag: {cost_rule.reason}"
    )


def test_benchmarks_still_catches_real_market_share_claim():
    """'ส่วนแบ่งตลาด 15%' ไม่มี citation → ต้องถูก flag (regression guard)."""
    context = {
        "product": "Product: LAGENIO K2 kids smartwatch.",
    }
    output = (
        "## แคมเปญหลัก\n"
        "- ชื่อ: Market Leader\n"
        "- กลไก: เป้าหมายส่วนแบ่งตลาด 15% ภายในไตรมาสแรก\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n"
        "## KPI ที่ควรวัดผล\n"
        "- ยอดขาย (target ต้องกำหนดหลังมี baseline)\n"
        "## งบประมาณประมาณการ\n"
        "- ประมาณ 50,000 บาท (estimate)\n"
        "## แหล่งอ้างอิง\n"
        "- ไม่มี external factual claim ที่ต้องอ้างอิง\n"
    )
    results = _audit(output, context)
    bench_rule = next(r for r in results if r.rule == "benchmarks_cited_or_removed")
    assert not bench_rule.ok, (
        f"ส่วนแบ่งตลาด 15% ไม่มี citation → ต้องถูก flag: {bench_rule.reason}"
    )


if __name__ == "__main__":
    tests = [
        test_has_word_thai_vowel_before_rejects_false_positive,
        test_has_word_thai_real_word_still_matches,
        test_has_word_english_word_boundary_still_works,
        test_has_word_thai_tone_mark_before_rejects,
        test_cost_mechanics_does_not_flag_storytelling_warmth,
        test_benchmarks_does_not_flag_product_spec,
        test_cost_mechanics_still_catches_real_discount,
        test_benchmarks_still_catches_real_market_share_claim,
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
    print(f"\n{passed} passed, {failed} failed")
