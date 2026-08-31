"""Bug: brand_visual.image_style และ keywords อาจเป็น string หรือ dict/list.

product_profile.visual_override ใน cache หลายสินค้าเก็บ image_style เป็น string
(เช่น "สปอร์ตเอาต์ดอร์ ลุย ทนทาน") และ keywords เป็น string คั่นด้วยจุลภาค
(เช่น "outdoor rugged, bluetooth call") แต่ orchestrator คาดหวัง dict และ list
ทำให้ run_content_creator crash ก่อนเรียก LLM (AttributeError / join ทีละตัวอักษร).

test นี้ยืนยันว่า orchestrator รองรับทั้งสองรูปแบบโดยไม่ crash และส่ง visual_style
hint ไปยัง prompt ของ LLM ได้ถูกต้อง
"""
from __future__ import annotations

import pytest

from src.orchestrator import Orchestrator


class FakeLLM:
    """Deterministic LLM double that captures all chat calls."""

    def __init__(self, output: str = ""):
        self.output = output
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("return_annotations"):
            return self.output, []
        return self.output

    def close(self):
        pass


def _make_orchestrator(brand_visual: dict) -> Orchestrator:
    """Create an Orchestrator without requiring brand files or API key."""
    from src.config_loader import load_config
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = load_config()
    orch.brand_context = ""
    orch.brand_reference = ""
    orch.brand_visual = brand_visual
    orch.brand_rules = {}
    orch.product_images = []
    orch.product_id = None
    orch.results = {}
    return orch


# ---------------------------------------------------------------------------
# Seam: Orchestrator.run_content_creator — ต้องไม่ crash เมื่อ brand_visual
# มี image_style เป็น string หรือ keywords เป็น string
# ---------------------------------------------------------------------------


def test_run_content_creator_string_image_style_does_not_crash():
    """image_style เป็น string (เช่นจาก product_profile) → ต้องไม่ crash."""
    orch = _make_orchestrator({
        "image_style": "สดใส ปลอดภัย เหมาะกับเด็ก",
    })
    fake_llm = FakeLLM(output='{"posts": []}')

    # ก่อนแก้: บรรทัด style.get("tone", "") จะ raise AttributeError
    # เพราะ style เป็น string ไม่ใช่ dict
    result = orch.run_content_creator(
        product_spec="Lagenio K2 smartwatch",
        competitor_analysis="",
        campaign_strategy="",
        llm=fake_llm,
    )

    assert result == '{"posts": []}'
    # visual_style hint ต้องปรากฏใน prompt ที่ส่งให้ LLM
    all_prompt_text = " ".join(
        str(c["messages"]) for c in fake_llm.calls
    )
    assert "สดใส" in all_prompt_text, (
        f"tone จาก string image_style ไม่ถึง prompt: {all_prompt_text}"
    )


def test_run_content_creator_string_keywords_does_not_crash():
    """keywords เป็น string คั่นจุลภาค → ต้องไม่ crash และไม่ join ทีละตัวอักษร."""
    orch = _make_orchestrator({
        "keywords": "outdoor rugged, bluetooth call, long battery",
    })
    fake_llm = FakeLLM(output='{"posts": []}')

    # ก่อนแก้: ", ".join(keywords) จะ join ทีละตัวอักษร → "o, u, t, d, o, o, r..."
    result = orch.run_content_creator(
        product_spec="Lagenio K2 smartwatch",
        competitor_analysis="",
        campaign_strategy="",
        llm=fake_llm,
    )

    assert result == '{"posts": []}'
    all_prompt_text = " ".join(
        str(c["messages"]) for c in fake_llm.calls
    )
    assert "outdoor rugged" in all_prompt_text, (
        f"keywords string ไม่ถูก split แล้ว join ใหม่: {all_prompt_text}"
    )


def test_run_content_creator_dict_image_style_still_works():
    """image_style เป็น dict (จาก UI) → ยังทำงานเหมือนเดิม (regression guard)."""
    orch = _make_orchestrator({
        "image_style": {"tone": "อบอุ่น สดใส", "product_shot": "สะอาด พื้นขาว"},
        "keywords": ["soft light", "warm tone", "family"],
    })
    fake_llm = FakeLLM(output='{"posts": []}')

    result = orch.run_content_creator(
        product_spec="Lagenio K2 smartwatch",
        competitor_analysis="",
        campaign_strategy="",
        llm=fake_llm,
    )

    assert result == '{"posts": []}'
    all_prompt_text = " ".join(
        str(c["messages"]) for c in fake_llm.calls
    )
    assert "อบอุ่น" in all_prompt_text, (
        f"tone จาก dict image_style หายไป: {all_prompt_text}"
    )
    assert "soft light" in all_prompt_text, (
        f"keywords list หายไป: {all_prompt_text}"
    )


def test_run_content_creator_string_image_style_and_string_keywords_together():
    """ทั้ง image_style และ keywords เป็น string พร้อมกัน (กรณีจริงจาก cache)."""
    orch = _make_orchestrator({
        "image_style": "สปอร์ตเอาต์ดอร์ ลุย ทนทาน",
        "keywords": "outdoor rugged, bluetooth call, long battery life",
    })
    fake_llm = FakeLLM(output='{"posts": []}')

    result = orch.run_content_creator(
        product_spec="Lagenio K2 smartwatch",
        competitor_analysis="",
        campaign_strategy="",
        llm=fake_llm,
    )

    assert result == '{"posts": []}'
    all_prompt_text = " ".join(
        str(c["messages"]) for c in fake_llm.calls
    )
    assert "สปอร์ตเอาต์ดอร์" in all_prompt_text
    assert "outdoor rugged" in all_prompt_text


if __name__ == "__main__":
    tests = [
        test_run_content_creator_string_image_style_does_not_crash,
        test_run_content_creator_string_keywords_does_not_crash,
        test_run_content_creator_dict_image_style_still_works,
        test_run_content_creator_string_image_style_and_string_keywords_together,
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
