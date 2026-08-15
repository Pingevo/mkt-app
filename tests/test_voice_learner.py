"""Tests for voice_learner — วิเคราะห์ตัวอย่างโพสต์ → voice profile.

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)

Seam ที่ทดสอบ:
  1. analyze_voice(examples: list[str], llm) -> dict
     — ส่งตัวอย่างโพสต์ให้ LLM วิเคราะห์ → คืน voice profile dict
  2. fetch_url_content(url: str) -> str
     — ดึง text จาก URL (httpx + regex strip HTML)
  3. extract_file_text(filepath: Path) -> str
     — อ่าน text จากไฟล์ (ใช้ file_loader.load_file)

กรณีทดสอบ:
  - analyze_voice: ส่ง examples → LLM คืน voice profile (mock LLM)
  - fetch_url_content: URL ปกติ → ดึง text ได้ (mock httpx)
  - fetch_url_content: URL ผิด → คืน error message ไม่ crash
  - fetch_url_content: เว็บ block (403) → คืน error message
  - extract_file_text: ไฟล์ .txt → อ่านได้
  - extract_file_text: ไฟล์ .pdf → อ่านได้ (ถ้ามี PyPDF2)
  - extract_file_text: ไฟล์ไม่มี → คืน error message ไม่ crash
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# analyze_voice — ส่ง examples ให้ LLM → คืน voice profile
# ---------------------------------------------------------------------------

def test_analyze_voice_returns_profile():
    """ส่ง examples ให้ LLM → คืน voice profile dict ที่มี fields ครบ."""
    from src.voice_learner import analyze_voice

    # Mock LLM — จำลองว่า LLM คืน JSON voice profile
    mock_llm = MagicMock()
    mock_llm.chat.return_value = '{"personality": "เป็นมิตร", "tone_description": "อบอุ่น", "formality_level": 3, "language": "ไทย", "banned_phrases": ["คำห้าม"], "examples": ["ตัวอย่าง"]}'

    examples = ["โพสต์ตัวอย่างที่ 1", "โพสต์ตัวอย่างที่ 2"]
    result = analyze_voice(examples, mock_llm)

    assert isinstance(result, dict)
    assert "personality" in result
    assert "tone_description" in result
    assert "formality_level" in result
    assert result["personality"] == "เป็นมิตร"
    assert result["formality_level"] == 3


def test_analyze_voice_empty_examples():
    """ส่ง examples ว่าง → คืน empty dict ไม่เรียก LLM."""
    from src.voice_learner import analyze_voice

    mock_llm = MagicMock()
    result = analyze_voice([], mock_llm)

    assert result == {}
    mock_llm.chat.assert_not_called()


def test_analyze_voice_llm_returns_invalid_json():
    """LLM คืน JSON ไม่ valid → คืน empty dict ไม่ crash."""
    from src.voice_learner import analyze_voice

    mock_llm = MagicMock()
    mock_llm.chat.return_value = 'not valid json {{{'

    result = analyze_voice(["example"], mock_llm)

    assert result == {}


def test_analyze_voice_passes_examples_in_prompt():
    """ตัวอย่างต้องถูกส่งใน prompt ให้ LLM เห็น."""
    from src.voice_learner import analyze_voice

    mock_llm = MagicMock()
    mock_llm.chat.return_value = '{"personality": "test"}'

    analyze_voice(["โพสต์ของแบรนด์ A"], mock_llm)

    # ตรวจว่า prompt ที่ส่งให้ LLM มีตัวอย่าง
    call_args = mock_llm.chat.call_args
    messages = call_args[0][0] if call_args[0] else call_args[1].get("messages", [])
    prompt_text = " ".join(m.get("content", "") for m in messages)
    assert "โพสต์ของแบรนด์ A" in prompt_text


# ---------------------------------------------------------------------------
# fetch_url_content — ดึง text จาก URL
# ---------------------------------------------------------------------------

def test_fetch_url_content_success():
    """URL ปกติ → ดึง text ได้ (strip HTML tags)."""
    from src.voice_learner import fetch_url_content

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "<html><body><p>สวัสดีครับ นี่คือเนื้อหา</p></body></html>"
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_response):
        result = fetch_url_content("https://example.com/post")

    assert "สวัสดีครับ" in result
    assert "นี่คือเนื้อหา" in result
    assert "<html>" not in result  # HTML tags ถูก strip
    assert "<p>" not in result


def test_fetch_url_content_403():
    """เว็บ block (403) → คืน error message ไม่ crash."""
    from src.voice_learner import fetch_url_content

    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.raise_for_status.side_effect = Exception("403 Forbidden")

    with patch("httpx.get", return_value=mock_response):
        result = fetch_url_content("https://example.com/blocked")

    # คืน string ที่บอก error (ไม่ crash)
    assert isinstance(result, str)
    assert "403" in result or "error" in result.lower() or "ไม่" in result


def test_fetch_url_content_network_error():
    """Network error → คืน error message ไม่ crash."""
    from src.voice_learner import fetch_url_content

    with patch("httpx.get", side_effect=Exception("Connection refused")):
        result = fetch_url_content("https://nonexistent.example.com")

    assert isinstance(result, str)
    assert "error" in result.lower() or "ไม่" in result or "Connection" in result


def test_fetch_url_content_truncates_long_content():
    """เนื้อหายาวเกิน → ตัดให้สั้นลง (max 5000 ตัวอักษร)."""
    from src.voice_learner import fetch_url_content

    long_text = "<p>" + "A" * 10000 + "</p>"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = long_text
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_response):
        result = fetch_url_content("https://example.com/long")

    assert len(result) <= 5100  # ให้ buffer เล็กน้อย


# ---------------------------------------------------------------------------
# extract_file_text — อ่าน text จากไฟล์
# ---------------------------------------------------------------------------

def test_extract_file_text_txt():
    """ไฟล์ .txt → อ่านได้."""
    from src.voice_learner import extract_file_text

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "example.txt"
        path.write_text("นี่คือตัวอย่างโพสต์ของแบรนด์", encoding="utf-8")

        result = extract_file_text(path)

        assert "ตัวอย่างโพสต์ของแบรนด์" in result


def test_extract_file_text_md():
    """ไฟล์ .md → อ่านได้."""
    from src.voice_learner import extract_file_text

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "example.md"
        path.write_text("# หัวข้อ\n\nเนื้อหาโพสต์", encoding="utf-8")

        result = extract_file_text(path)

        assert "เนื้อหาโพสต์" in result


def test_extract_file_text_nonexistent():
    """ไฟล์ไม่มี → คืน error message ไม่ crash."""
    from src.voice_learner import extract_file_text

    result = extract_file_text(Path("/nonexistent/file.txt"))

    assert isinstance(result, str)
    assert "ไม่" in result or "error" in result.lower() or "not" in result.lower()


def test_extract_file_text_empty_file():
    """ไฟล์ว่าง → คืน string ว่าง."""
    from src.voice_learner import extract_file_text

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "empty.txt"
        path.write_text("", encoding="utf-8")

        result = extract_file_text(path)

        assert result == ""


# ---------------------------------------------------------------------------
# collect_examples — รวม examples จากหลายแหล่ง
# ---------------------------------------------------------------------------

def test_collect_examples_mixed_sources():
    """รวม examples จาก text + files + URLs → list ของ strings."""
    from src.voice_learner import collect_examples

    with tempfile.TemporaryDirectory() as tmp:
        file_path = Path(tmp) / "ex.txt"
        file_path.write_text("ตัวอย่างจากไฟล์", encoding="utf-8")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<p>ตัวอย่างจากเว็บ</p>"
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.get", return_value=mock_response):
            result = collect_examples(
                pasted_texts=["ตัวอย่างที่ paste"],
                file_paths=[str(file_path)],
                urls=["https://example.com"],
            )

        assert "ตัวอย่างที่ paste" in result
        assert "ตัวอย่างจากไฟล์" in result
        assert "ตัวอย่างจากเว็บ" in result
        assert len(result) == 3


def test_collect_examples_all_empty():
    """ไม่มีอะไรเลย → คืน list ว่าง."""
    from src.voice_learner import collect_examples

    result = collect_examples(pasted_texts=[], file_paths=[], urls=[])

    assert result == []


# ---------------------------------------------------------------------------
# analyze_terms — ดึงคำที่ใช้บ่อย/คำต้องห้ามจากตัวอย่าง
# ---------------------------------------------------------------------------

def test_analyze_terms_returns_profile():
    """ส่ง examples → LLM คืน terms dict ที่มี approved + restricted + replacements."""
    from src.voice_learner import analyze_terms

    mock_llm = MagicMock()
    mock_llm.chat.return_value = (
        '{"approved": ["นวัตกรรม", "คุณภาพ"], '
        '"restricted": ["ถูกที่สุด", "ของแถม"], '
        '"replacements": {"ถูกมาก": "ราคาคุ้ม"}}'
    )

    result = analyze_terms(["โพสต์ตัวอย่างที่ใช้คำว่า นวัตกรรม"], mock_llm)

    assert isinstance(result, dict)
    assert "approved" in result
    assert "restricted" in result
    assert "replacements" in result
    assert "นวัตกรรม" in result["approved"]
    assert "ถูกที่สุด" in result["restricted"]


def test_analyze_terms_empty_examples():
    """ส่ง examples ว่าง → คืน {} ไม่เรียก LLM."""
    from src.voice_learner import analyze_terms

    mock_llm = MagicMock()
    result = analyze_terms([], mock_llm)

    assert result == {}
    mock_llm.chat.assert_not_called()


def test_analyze_terms_invalid_json():
    """LLM คืน JSON ไม่ valid → คืน {} ไม่ crash."""
    from src.voice_learner import analyze_terms

    mock_llm = MagicMock()
    mock_llm.chat.return_value = 'not json {{{'

    result = analyze_terms(["example"], mock_llm)

    assert result == {}


# ---------------------------------------------------------------------------
# analyze_audience — เดากลุ่มเป้าหมายจากโทนเสียง/ภาษาที่ใช้
# ---------------------------------------------------------------------------

def test_analyze_audience_returns_profile():
    """ส่ง examples → LLM คืน audience dict ที่มี primary + pain_points + channels."""
    from src.voice_learner import analyze_audience

    mock_llm = MagicMock()
    mock_llm.chat.return_value = (
        '{"primary": {"age": "30-45 ปี", "role": "ผู้ปกครอง"}, '
        '"pain_points": ["ไม่สบายใจให้ลูกเล่นมือถือ"], '
        '"channels": ["Facebook", "TikTok"]}'
    )

    result = analyze_audience(["โพสต์สำหรับผู้ปกครอง"], mock_llm)

    assert isinstance(result, dict)
    assert "primary" in result
    assert "pain_points" in result
    assert "channels" in result
    assert result["primary"]["role"] == "ผู้ปกครอง"
    assert "Facebook" in result["channels"]


def test_analyze_audience_empty_examples():
    """ส่ง examples ว่าง → คืน {} ไม่เรียก LLM."""
    from src.voice_learner import analyze_audience

    mock_llm = MagicMock()
    result = analyze_audience([], mock_llm)

    assert result == {}
    mock_llm.chat.assert_not_called()


def test_analyze_audience_invalid_json():
    """LLM คืน JSON ไม่ valid → คืน {} ไม่ crash."""
    from src.voice_learner import analyze_audience

    mock_llm = MagicMock()
    mock_llm.chat.return_value = 'broken'

    result = analyze_audience(["example"], mock_llm)

    assert result == {}


# ---------------------------------------------------------------------------
# analyze_brand — วิเคราะห์ทั้ง 3 ส่วนในครั้งเดียว
# ---------------------------------------------------------------------------

def test_analyze_brand_returns_three_sections():
    """ส่ง examples → คืน dict ที่มี voice + terms + audience ครบ."""
    from src.voice_learner import analyze_brand

    mock_llm = MagicMock()
    # LLM ถูกเรียก 3 ครั้ง (voice, terms, audience) — แต่ละครั้งคืน JSON ต่างกัน
    mock_llm.chat.side_effect = [
        '{"personality": "เป็นมิตร", "tone_description": "อบอุ่น", "formality_level": 3, "language": "ไทย", "banned_phrases": [], "examples": []}',
        '{"approved": ["นวัตกรรม"], "restricted": ["ถูกที่สุด"], "replacements": {}}',
        '{"primary": {"age": "30-45", "role": "ผู้ปกครอง"}, "pain_points": [], "channels": ["Facebook"]}',
    ]

    result = analyze_brand(["ตัวอย่างโพสต์"], mock_llm)

    assert "voice" in result
    assert "terms" in result
    assert "audience" in result
    assert result["voice"]["personality"] == "เป็นมิตร"
    assert "นวัตกรรม" in result["terms"]["approved"]
    assert result["audience"]["primary"]["role"] == "ผู้ปกครอง"


def test_analyze_brand_empty_examples():
    """ส่ง examples ว่าง → คืน {} ไม่เรียก LLM."""
    from src.voice_learner import analyze_brand

    mock_llm = MagicMock()
    result = analyze_brand([], mock_llm)

    assert result == {}
    mock_llm.chat.assert_not_called()


def test_analyze_brand_partial_failure():
    """ถ้า terms วิเคราะห์พัง แต่ voice + audience ผ่าน → ยังคืน voice + audience (ไม่ crash)."""
    from src.voice_learner import analyze_brand

    mock_llm = MagicMock()
    mock_llm.chat.side_effect = [
        '{"personality": "เป็นมิตร", "tone_description": "อบอุ่น", "formality_level": 3, "language": "ไทย", "banned_phrases": [], "examples": []}',
        'broken json {{{',  # terms พัง
        '{"primary": {"age": "30-45", "role": "ผู้ปกครอง"}, "pain_points": [], "channels": ["Facebook"]}',
    ]

    result = analyze_brand(["ตัวอย่าง"], mock_llm)

    assert "voice" in result
    assert result["voice"]["personality"] == "เป็นมิตร"
    # terms พัง → คืน {} ไม่ใช่ crash
    assert result.get("terms") == {}
    assert "audience" in result


# ---------------------------------------------------------------------------
# analyze_video_style — วิเคราะห์สไตล์วิดีโอจากไฟล์/YouTube URL
# ---------------------------------------------------------------------------

def test_analyze_video_style_from_youtube_url():
    """ส่ง YouTube URL → LLM คืน video style profile (pacing, transitions, color, etc)."""
    from src.voice_learner import analyze_video_style

    mock_llm = MagicMock()
    mock_llm.chat.return_value = (
        '{"pacing": "fast", "transitions": ["jump cut", "zoom"], '
        '"color_grading": "warm tones", "sound_design": "upbeat music", '
        '"shot_duration": "2-3 sec", "visual_rhythm": "energetic", '
        '"style_summary": "Fast-paced TikTok style with warm tones"}'
    )

    result = analyze_video_style(
        video_url="https://www.youtube.com/watch?v=abc123",
        llm=mock_llm,
    )

    assert isinstance(result, dict)
    assert "pacing" in result
    assert "transitions" in result
    assert "color_grading" in result
    assert "style_summary" in result
    # ตรวจว่าส่ง video_url content type ให้ LLM
    call_args = mock_llm.chat.call_args
    messages = call_args[0][0]
    user_msg = [m for m in messages if m["role"] == "user"][0]
    # content ต้องเป็น list (multimodal) มี video_url part
    assert isinstance(user_msg["content"], list)
    video_parts = [p for p in user_msg["content"] if p.get("type") == "video_url"]
    assert len(video_parts) == 1
    assert "youtube.com" in video_parts[0]["video_url"]["url"]


def test_analyze_video_style_from_file():
    """ส่งไฟล์วิดีโอ → แปลงเป็น base64 data URL → ส่งให้ LLM."""
    from src.voice_learner import analyze_video_style

    mock_llm = MagicMock()
    mock_llm.chat.return_value = (
        '{"pacing": "medium", "transitions": [], "color_grading": "neutral", '
        '"sound_design": "voiceover", "shot_duration": "3-5 sec", '
        '"visual_rhythm": "calm", "style_summary": "Calm documentary style"}'
    )

    # สร้างไฟล์วิดีโอจำลอง
    import tempfile, base64
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.write(b"fake video content")
    tmp.close()

    try:
        result = analyze_video_style(video_path=tmp.name, llm=mock_llm)

        assert isinstance(result, dict)
        assert "style_summary" in result
        # ตรวจว่าส่งเป็น base64 data URL
        call_args = mock_llm.chat.call_args
        messages = call_args[0][0]
        user_msg = [m for m in messages if m["role"] == "user"][0]
        video_parts = [p for p in user_msg["content"] if p.get("type") == "video_url"]
        assert len(video_parts) == 1
        assert video_parts[0]["video_url"]["url"].startswith("data:video/mp4;base64,")
    finally:
        import os
        os.unlink(tmp.name)


def test_analyze_video_style_no_input():
    """ไม่ส่งอะไรเลย → คืน {} ไม่เรียก LLM."""
    from src.voice_learner import analyze_video_style

    mock_llm = MagicMock()
    result = analyze_video_style(llm=mock_llm)

    assert result == {}
    mock_llm.chat.assert_not_called()


def test_analyze_video_style_invalid_json():
    """LLM คืน JSON ไม่ valid → คืน {} ไม่ crash."""
    from src.voice_learner import analyze_video_style

    mock_llm = MagicMock()
    mock_llm.chat.return_value = 'broken json {{{'

    result = analyze_video_style(
        video_url="https://www.youtube.com/watch?v=abc",
        llm=mock_llm,
    )

    assert result == {}


if __name__ == "__main__":
    tests = [
        test_analyze_voice_returns_profile,
        test_analyze_voice_empty_examples,
        test_analyze_voice_llm_returns_invalid_json,
        test_analyze_voice_passes_examples_in_prompt,
        test_fetch_url_content_success,
        test_fetch_url_content_403,
        test_fetch_url_content_network_error,
        test_fetch_url_content_truncates_long_content,
        test_extract_file_text_txt,
        test_extract_file_text_md,
        test_extract_file_text_nonexistent,
        test_extract_file_text_empty_file,
        test_collect_examples_mixed_sources,
        test_collect_examples_all_empty,
        test_analyze_terms_returns_profile,
        test_analyze_terms_empty_examples,
        test_analyze_terms_invalid_json,
        test_analyze_audience_returns_profile,
        test_analyze_audience_empty_examples,
        test_analyze_audience_invalid_json,
        test_analyze_brand_returns_three_sections,
        test_analyze_brand_empty_examples,
        test_analyze_brand_partial_failure,
        test_analyze_video_style_from_youtube_url,
        test_analyze_video_style_from_file,
        test_analyze_video_style_no_input,
        test_analyze_video_style_invalid_json,
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
