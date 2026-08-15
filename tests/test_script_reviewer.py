"""Tests for script_reviewer — ตรวจ script หาจุดน่าเบื่อ + ให้คะแนน + เสนอ hook ใหม่.

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)

Seam ที่ทดสอบ:
  review_script(script: str, platform: str, llm) -> dict
  — ส่ง script ให้ LLM วิเคราะห์ → คืน {score, component_scores, issues, suggested_hooks, revised_script}

กรณีทดสอบ:
  - ส่ง script ปกติ → LLM คืน review (score + components + issues + hooks + revised_script)
  - script ว่าง → คืน {} ไม่เรียก LLM
  - LLM คืน JSON ไม่ valid → คืน {} ไม่ crash
  - ตรวจว่าส่ง platform ใน prompt ด้วย
"""
from unittest.mock import MagicMock


_REVIEW_MOCK = (
    '{"score": 72, '
    '"component_scores": {"hook": 18, "pacing": 20, "clarity": 17, "engagement": 17}, '
    '"issues": [{"timestamp": "0:03-0:05", "problem": "พูดสเปคนานเกิน", '
    '"fix": "ตัดเหลือ 2 วินาที"}], '
    '"suggested_hooks": ["hook 1", "hook 2", "hook 3"], '
    '"revised_script": "0:00-0:03 HOOK: hook 1\\n0:03-0:05 BODY: ..."}'
)


def test_review_script_returns_review():
    """ส่ง script → LLM คืน score + component_scores + issues + hooks + revised_script."""
    from src.script_reviewer import review_script

    mock_llm = MagicMock()
    mock_llm.chat.return_value = _REVIEW_MOCK

    result = review_script(
        script="0:00-0:03 HOOK: เจอปัญหานี้ไหม?\n0:03-0:10 BODY: สเปคสินค้า...",
        platform="TikTok",
        llm=mock_llm,
    )

    assert isinstance(result, dict)
    assert "score" in result
    assert "component_scores" in result
    assert "issues" in result
    assert "suggested_hooks" in result
    assert "revised_script" in result
    assert 0 <= result["score"] <= 100
    cs = result["component_scores"]
    assert all(0 <= cs[k] <= 25 for k in ["hook", "pacing", "clarity", "engagement"])
    assert len(result["suggested_hooks"]) == 3
    assert result["issues"][0]["timestamp"] == "0:03-0:05"


def test_review_script_empty_script():
    """script ว่าง → คืน {} ไม่เรียก LLM."""
    from src.script_reviewer import review_script

    mock_llm = MagicMock()
    result = review_script(script="", platform="TikTok", llm=mock_llm)

    assert result == {}
    mock_llm.chat.assert_not_called()


def test_review_script_invalid_json():
    """LLM คืน JSON ไม่ valid → คืน {} ไม่ crash."""
    from src.script_reviewer import review_script

    mock_llm = MagicMock()
    mock_llm.chat.return_value = 'broken {{{'

    result = review_script(script="some script", platform="TikTok", llm=mock_llm)

    assert result == {}


def test_review_script_passes_platform_in_prompt():
    """ตรวจว่า platform ถูกส่งใน prompt ให้ LLM."""
    from src.script_reviewer import review_script

    mock_llm = MagicMock()
    mock_llm.chat.return_value = (
        '{"score": 80, "component_scores": {"hook": 20, "pacing": 20, '
        '"clarity": 20, "engagement": 20}, '
        '"issues": [], "suggested_hooks": [], "revised_script": ""}'
    )

    review_script(script="some script", platform="Facebook", llm=mock_llm)

    call_args = mock_llm.chat.call_args
    messages = call_args[0][0]
    # หาเท่าไหร่ก็ตามที่มี Facebook
    all_text = ""
    for m in messages:
        if isinstance(m["content"], str):
            all_text += m["content"]
        elif isinstance(m["content"], list):
            for part in m["content"]:
                if isinstance(part, dict) and part.get("type") == "text":
                    all_text += part["text"]
    assert "Facebook" in all_text


if __name__ == "__main__":
    tests = [
        test_review_script_returns_review,
        test_review_script_empty_script,
        test_review_script_invalid_json,
        test_review_script_passes_platform_in_prompt,
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
