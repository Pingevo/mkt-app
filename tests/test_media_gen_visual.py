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
import base64
import httpx
import sys
import tempfile
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


def test_build_visual_suffix_with_image_style_product_shot():
    """มี image_style.product_shot → suffix มี product shot hint.

    Bug เดิม: ฟอร์ม Visual มีช่อง "Product shot" เซฟลง visual.json
    แต่ build_visual_suffix ไม่อ่านค่านี้ → ผู้ใช้กรอกแล้วไม่มีผล.
    """
    from src.media_gen import build_visual_suffix

    visual = {
        "image_style": {"product_shot": "สะอาด พื้นขาว โฟกัสที่ตัวสินค้า"},
        "keywords": [],
    }
    suffix = build_visual_suffix(visual)

    assert "สะอาด" in suffix, f"product_shot missing: {suffix}"
    assert "พื้นขาว" in suffix, f"product_shot value missing: {suffix}"


def test_build_visual_suffix_with_tone_and_product_shot():
    """มีทั้ง tone และ product_shot → suffix มีทั้งคู่ (ไม่ทับกัน)."""
    from src.media_gen import build_visual_suffix

    visual = {
        "image_style": {"tone": "อบอุ่น สดใส", "product_shot": "สะอาด พื้นขาว"},
        "keywords": [],
    }
    suffix = build_visual_suffix(visual)

    assert "อบอุ่น" in suffix, f"tone missing: {suffix}"
    assert "สะอาด" in suffix, f"product_shot missing: {suffix}"


def test_build_visual_suffix_no_keywords_no_colors():
    """มีแค่ avoid (ไม่มี keywords, colors) → suffix มีแค่ avoid."""
    from src.media_gen import build_visual_suffix

    visual = {"avoid": ["scary", "cheap"]}
    suffix = build_visual_suffix(visual)

    assert "scary" in suffix
    assert "cheap" in suffix


def test_build_visual_suffix_lagenio_k2_legacy_string_visual():
    """รองรับ visual_override ของ Lagenio K2 จริงที่ image_style และ keywords เป็น string.

    Bug ต้นเหตุ: `image_style` จาก `product_profile.json` ส่งเป็น string
    `build_visual_suffix` เดิมเรียก `.get()` จึงพังทันทีก่อน image API.
    """
    from src.media_gen import build_visual_suffix
    from src.brand_loader import load_brand_visual

    visual = load_brand_visual("brand", product_id="Lagenio K2")
    assert visual.get("image_style")  # ต้องมี image_style จาก visual_override
    assert isinstance(visual["image_style"], str)

    suffix = build_visual_suffix(visual)

    assert suffix
    assert "สดใส" in suffix, f"image_style string missing: {suffix!r}"
    assert "kids smartwatch" in suffix, f"keywords string not split: {suffix!r}"
    assert "Brand colors:" in suffix, f"colors missing: {suffix!r}"


def test_generate_image_reaches_api_with_lagenio_visual(monkeypatch):
    """สร้างภาพด้วย visual ของ Lagenio K2 ไปถึง image API request โดยไม่เสียเงิน."""
    from src import media_gen
    from src.brand_loader import load_brand_visual

    visual = load_brand_visual("brand", product_id="Lagenio K2")

    monkeypatch.setattr(media_gen, "_get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr(media_gen, "_log_media_usage", lambda *a, **k: None)
    monkeypatch.setattr(media_gen, "get_model_capabilities", lambda *a, **k: {})

    calls = []
    original_post = httpx.Client.post

    fake_png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()

    def fake_post(client, url, **kwargs):
        calls.append((url, kwargs))
        class FakeResp:
            def raise_for_status(self): pass
            def json(self):
                return {"data": [{"b64_json": fake_png}]}
        return FakeResp()

    try:
        httpx.Client.post = fake_post
        with tempfile.TemporaryDirectory() as td:
            output_path = Path(td) / "test_image.png"
            result = media_gen.generate_image(
                "a kids smartwatch on a wrist",
                output_path,
                visual=visual,
            )

            assert result.get("ok") is True, result
            assert output_path.exists()

        assert len(calls) == 1
        assert calls[0][0] == "https://openrouter.ai/api/v1/images"
        assert calls[0][1].get("json", {}).get("model")
    finally:
        httpx.Client.post = original_post


if __name__ == "__main__":
    tests = [
        test_build_visual_suffix_with_keywords_and_avoid,
        test_build_visual_suffix_only_keywords,
        test_build_visual_suffix_empty_dict,
        test_build_visual_suffix_with_colors,
        test_build_visual_suffix_with_image_style_tone,
        test_build_visual_suffix_with_image_style_product_shot,
        test_build_visual_suffix_with_tone_and_product_shot,
        test_build_visual_suffix_no_keywords_no_colors,
        test_build_visual_suffix_lagenio_k2_legacy_string_visual,
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
