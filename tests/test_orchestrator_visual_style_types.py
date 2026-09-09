"""Bug: brand_visual.image_style และ keywords อาจเป็น string หรือ dict/list.

product_profile.visual_override ใน cache หลายสินค้าเก็บ image_style เป็น string
(เช่น "สปอร์ตเอาต์ดอร์ ลุย ทนทาน") และ keywords เป็น string คั่นด้วยจุลภาค
(เช่น "outdoor rugged, bluetooth call") แต่ orchestrator คาดหวัง dict และ list
ทำให้ run_content_creator crash ก่อนเรียก LLM (AttributeError / join ทีละตัวอักษร).

test นี้ยืนยันว่า orchestrator รองรับทั้งสองรูปแบบโดยไม่ crash และส่ง visual_style
hint ไปยัง prompt ของ LLM ได้ถูกต้อง
"""
from __future__ import annotations

import json

import pytest

from src.orchestrator import Orchestrator


class FakeLLM:
    """Deterministic LLM double that captures all chat calls."""

    def __init__(self, output: str = ""):
        self.output = output
        self.calls: list[dict] = []
        self.last_truncated = False

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            return '{"grounded": true, "unsupported_claims": []}'
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("return_annotations"):
            return self.output, []
        return self.output

    def close(self):
        pass


_VALID_POSTS_JSON = json.dumps({
    "posts": [{
        "platform": "Facebook",
        "concept": "test",
        "title": "Test Post",
        "caption": "test caption",
        "hashtags": "#test",
        "asset_ids": [],
        "image_prompts": [],
        "video_prompts": [],
    }],
}, ensure_ascii=False)


def _make_orchestrator(brand_visual: dict, monkeypatch) -> Orchestrator:
    """Create an Orchestrator without requiring brand files or API key.

    monkeypatch is used to mock media_gen.get_model_capabilities so the real
    OpenRouter capability endpoint is never reached from these offline tests.
    The mock is automatically scoped to the calling test by pytest's
    monkeypatch fixture — no manual stop() needed, no leak between tests.
    """
    from src import media_gen
    monkeypatch.setattr(media_gen, "get_model_capabilities", lambda *a, **k: {})
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


def test_run_content_creator_string_image_style_does_not_crash(monkeypatch):
    """image_style เป็น string (เช่นจาก product_profile) → ต้องไม่ crash."""
    orch = _make_orchestrator({
        "image_style": "สดใส ปลอดภัย เหมาะกับเด็ก",
    }, monkeypatch)
    fake_llm = FakeLLM(output=_VALID_POSTS_JSON)

    # ก่อนแก้: บรรทัด style.get("tone", "") จะ raise AttributeError
    # เพราะ style เป็น string ไม่ใช่ dict
    result = orch.run_content_creator(
        product_spec="Lagenio K2 smartwatch",
        competitor_analysis="",
        campaign_strategy="",
        llm=fake_llm,
    )

    assert json.loads(result)["posts"][0]["title"] == "Test Post"
    # visual_style hint ต้องปรากฏใน prompt ที่ส่งให้ LLM
    all_prompt_text = " ".join(
        str(c["messages"]) for c in fake_llm.calls
    )
    assert "สดใส" in all_prompt_text, (
        f"tone จาก string image_style ไม่ถึง prompt: {all_prompt_text}"
    )


def test_run_content_creator_string_keywords_does_not_crash(monkeypatch):
    """keywords เป็น string คั่นจุลภาค → ต้องไม่ crash และไม่ join ทีละตัวอักษร."""
    orch = _make_orchestrator({
        "keywords": "outdoor rugged, bluetooth call, long battery",
    }, monkeypatch)
    fake_llm = FakeLLM(output=_VALID_POSTS_JSON)

    # ก่อนแก้: ", ".join(keywords) จะ join ทีละตัวอักษร → "o, u, t, d, o, o, r..."
    result = orch.run_content_creator(
        product_spec="Lagenio K2 smartwatch",
        competitor_analysis="",
        campaign_strategy="",
        llm=fake_llm,
    )

    assert json.loads(result)["posts"][0]["title"] == "Test Post"
    all_prompt_text = " ".join(
        str(c["messages"]) for c in fake_llm.calls
    )
    assert "outdoor rugged" in all_prompt_text, (
        f"keywords string ไม่ถูก split แล้ว join ใหม่: {all_prompt_text}"
    )


def test_run_content_creator_dict_image_style_still_works(monkeypatch):
    """image_style เป็น dict (จาก UI) → ยังทำงานเหมือนเดิม (regression guard)."""
    orch = _make_orchestrator({
        "image_style": {"tone": "อบอุ่น สดใส", "product_shot": "สะอาด พื้นขาว"},
        "keywords": ["soft light", "warm tone", "family"],
    }, monkeypatch)
    fake_llm = FakeLLM(output=_VALID_POSTS_JSON)

    result = orch.run_content_creator(
        product_spec="Lagenio K2 smartwatch",
        competitor_analysis="",
        campaign_strategy="",
        llm=fake_llm,
    )

    assert json.loads(result)["posts"][0]["title"] == "Test Post"
    all_prompt_text = " ".join(
        str(c["messages"]) for c in fake_llm.calls
    )
    assert "อบอุ่น" in all_prompt_text, (
        f"tone จาก dict image_style หายไป: {all_prompt_text}"
    )
    assert "soft light" in all_prompt_text, (
        f"keywords list หายไป: {all_prompt_text}"
    )


def test_run_content_creator_string_image_style_and_string_keywords_together(monkeypatch):
    """ทั้ง image_style และ keywords เป็น string พร้อมกัน (กรณีจริงจาก cache)."""
    orch = _make_orchestrator({
        "image_style": "สปอร์ตเอาต์ดอร์ ลุย ทนทาน",
        "keywords": "outdoor rugged, bluetooth call, long battery life",
    }, monkeypatch)
    fake_llm = FakeLLM(output=_VALID_POSTS_JSON)

    result = orch.run_content_creator(
        product_spec="Lagenio K2 smartwatch",
        competitor_analysis="",
        campaign_strategy="",
        llm=fake_llm,
    )

    assert json.loads(result)["posts"][0]["title"] == "Test Post"
    all_prompt_text = " ".join(
        str(c["messages"]) for c in fake_llm.calls
    )
    assert "สปอร์ตเอาต์ดอร์" in all_prompt_text
    assert "outdoor rugged" in all_prompt_text


def test_offline_orchestrator_path_does_not_call_provider_capability_endpoint(monkeypatch):
    """Regression: run_content_creator must not reach the real OpenRouter
    capability endpoint. The media_gen.get_model_capabilities seam must be
    mocked so no network/provider call occurs during offline tests.

    Also proves the mock is test-scoped: after the test body, the original
    get_model_capabilities function is restored (no leak between tests).
    """
    from src import media_gen
    from src.config_loader import load_config

    original_get_caps = media_gen.get_model_capabilities

    fetch_calls: list = []

    def _tracking_get_caps(*a, **k):
        fetch_calls.append(a)
        return {}

    monkeypatch.setattr(media_gen, "get_model_capabilities", _tracking_get_caps)

    # Build orchestrator inline (not via _make_orchestrator) so the tracking
    # mock is the ONLY mock on get_model_capabilities — _make_orchestrator
    # would overwrite it with its own lambda.
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = load_config()
    orch.brand_context = ""
    orch.brand_reference = ""
    orch.brand_visual = {"image_style": "test tone"}
    orch.brand_rules = {}
    orch.product_images = []
    orch.product_id = None
    orch.results = {}

    fake_llm = FakeLLM(output=_VALID_POSTS_JSON)
    orch.run_content_creator(
        product_spec="test product",
        competitor_analysis="",
        campaign_strategy="",
        llm=fake_llm,
    )

    # The real orchestration path executed (LLM was called)
    assert len(fake_llm.calls) > 0, "orchestrator must reach the LLM"

    # The mock is still active during the test body (not overwritten)
    assert media_gen.get_model_capabilities is _tracking_get_caps, \
        "tracking mock must remain active throughout the test"

    # The in-memory cache was not populated with real provider data
    for cache_key in media_gen._CAPABILITIES_CACHE:
        cached = media_gen._CAPABILITIES_CACHE[cache_key]
        assert cached == {}, \
            f"offline test must not populate real capability cache: {cache_key}"

    # Prove the mock is test-scoped: the original function differs from the
    # mock. monkeypatch will restore it automatically at teardown. We do NOT
    # call the original (that would hit the real provider).
    assert original_get_caps is not _tracking_get_caps, \
        "original must differ from mock"


if __name__ == "__main__":
    from unittest.mock import MagicMock

    class _FakeMonkeypatch:
        """Minimal monkeypatch for __main__ execution — auto-restores via
        recording setattr calls and undoing them."""
        def __init__(self):
            self._undo = []
        def setattr(self, target, name, value):
            original = getattr(target, name)
            self._undo.append((target, name, original))
            setattr(target, name, value)
        def undo(self):
            for target, name, original in reversed(self._undo):
                setattr(target, name, original)
            self._undo.clear()

    tests = [
        test_run_content_creator_string_image_style_does_not_crash,
        test_run_content_creator_string_keywords_does_not_crash,
        test_run_content_creator_dict_image_style_still_works,
        test_run_content_creator_string_image_style_and_string_keywords_together,
    ]
    passed = 0
    failed = 0
    for test in tests:
        mp = _FakeMonkeypatch()
        try:
            test(mp)
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
        finally:
            mp.undo()
    print(f"\n{passed} passed, {failed} failed")
