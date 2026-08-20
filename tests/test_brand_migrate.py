"""Tests for brand_migrate — แปลง .md เดิม → .json อัตโนมัติ.

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)

Seam ที่ทดสอบ:
  migrate_brand(brand_dir) -> dict  — อ่าน .md เดิม → เขียน .json → คืน status

กรณีทดสอบ:
  - มี tone_of_voice.md → สร้าง voice.json + terms.json
  - มี visual_guidelines.md → สร้าง visual.json
  - มี target_audience.md → สร้าง audience.json
  - มี brand_profile.md → เก็บเป็น .md ไว้ (ไม่ convert)
  - ไม่มี .md เลย → ไม่ทำอะไร (คืน empty status)
  - มี .json อยู่แล้ว → ไม่ overwrite (เว้นแต่ force=True)
  - หลัง migrate → .md เดิมเก็บเป็น .md.bak (ไม่ลบทิ้ง)
"""
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ข้อมูลตัวอย่างเหมือนไฟล์จริง (จาก .example.md)
_SAMPLE_TONE = """# โทนเสียงแบรนด์ (Tone of Voice)

## บุคลิกของแบรนด์ (Brand Personality)
เหมือนพ่อแม่ที่เข้าใจเทคโนโลยี

## ภาษาที่ใช้
- ใช้ภาษาไทยเป็นหลัก สำหรับตลาดไทย
- ใช้คำว่า "คุณพ่อคุณแม่" กับลูกค้า

## คำที่ควรใช้ (Do Say)
- ปลอดภัย, นวัตกรรม, ครอบครัว
- สมาร์ท, เชื่อมต่อ

## คำที่ห้ามใช้ (Don't Say)
- ไม่ใช้คำที่สร้างความกลัว เช่น "อันตราย"
- ไม่เรียกสินค้าว่า "กำไล" — ใช้ "นาฬิกา" แทน

## ระดับความเป็นทางการ
3 — กลาง-เป็นทางการเล็กน้อย เป็นมิตร อบอุ่น

## ตัวอย่างโพสต์ที่ใช่
"ลูกน้อยเดินเล่นในสวน คุณพ่อคุณแม่เชื่อมต่อผ่านนาฬิกาสมาร์ทได้ตลอดเวลา"
"""

_SAMPLE_VISUAL = """# แนวทางภาพลักษณ์ (Visual Guidelines)

## สีหลักของแบรนด์ (Brand Colors)
- Primary: **#1a73e8** — สีโลโก้, ปุ่ม CTA
- Secondary: **#34a853** — สีรอง
- Accent: **#fbbc04** — สีเน้น
- พื้นหลัง: #ffffff
- โทนภาพรวม: สดใส อบอุ่น

## สไตล์ภาพ (Image Style)
- Product shot สะอาด พื้นขาว
- Lifestyle: ครอบครัวในบ้าน แสงธรรมชาติ
- โทนภาพ: อบอุ่น
- มุมกล้อง: ระดับสายตาเด็ก

## แนวทางสำหรับ AI Image/Video Prompt
- ใช้ keyword: soft light, warm tone, family, natural, bright
- โทนสี: อบอุ่น สดใส
- หลีกเลี่ยง: dark, gloomy, aggressive
"""

_SAMPLE_AUDIENCE = """# กลุ่มเป้าหมายของแบรนด์ (Target Audience)

## กลุ่มเป้าหมายหลัก
- อายุ: 30-45 (ผู้ปกครอง)
- เพศ: หญิง 70% / ชาย 30%
- อาชีพ: มืออาชีพ
- รายได้: กลาง-บน
- ที่อยู่: กรุงเทพฯ และปริมณฑล

## ผู้ใช้ปลายทาง (End User)
- อายุ: 5-12
- เด็กวัยเรียน

## ไลฟ์สไตล์
- ใส่ใจสุขภาพลูก
- ใช้สมาร์ทโฟนทุกวัน
- ติดตามข่าวเทคโนโลยี

## พฤติกรรมการซื้อ
- ตัดสินใจซื้อจาก: ความปลอดภัย > คุณสมบัติ > ราคา
- งบประมาณต่อครั้ง: 2,000-5,000 บาท
- ซื้อผ่าน: ออนไลน์

## ปัญหา/ความต้องการ (Pain Points)
- ความปลอดภัย
- การติดต่อ
- ตำแหน่งที่อยู่

## ช่องทางที่ใช้บ่อย
- Social Media: Facebook, TikTok
- ช้อปออนไลน์: Shopee, Lazada
- ค้นหาข้อมูล: Google, YouTube review
"""

_SAMPLE_PROFILE = """# ประวัติแบรนด์ (Brand Profile)

## ชื่อแบรนด์
Lagenio

## แท็กไลน์
"Smartwatch for Kids"

## ประวัติ
บริษัทก่อตั้งปี 2018
"""


# ---------------------------------------------------------------------------
# migrate_brand — แปลง .md → .json
# ---------------------------------------------------------------------------

def test_migrate_tone_to_voice_and_terms():
    """มี tone_of_voice.md → สร้าง voice.json + terms.json."""
    from src.brand_migrate import migrate_brand

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "tone_of_voice.md").write_text(_SAMPLE_TONE, encoding="utf-8")

        status = migrate_brand(brand_dir)

        # ตรวจ voice.json
        voice_path = brand_dir / "voice.json"
        assert voice_path.exists(), "voice.json not created"
        voice = json.loads(voice_path.read_text(encoding="utf-8"))
        assert "เหมือนพ่อแม่" in voice.get("personality", ""), f"personality wrong: {voice}"
        assert voice.get("formality_level") == 3, f"formality wrong: {voice}"
        assert "ไทย" in voice.get("language", ""), f"language wrong: {voice}"

        # ตรวจ terms.json
        terms_path = brand_dir / "terms.json"
        assert terms_path.exists(), "terms.json not created"
        terms = json.loads(terms_path.read_text(encoding="utf-8"))
        assert "ปลอดภัย" in terms.get("approved", []), f"approved wrong: {terms}"
        assert "อันตราย" in terms.get("restricted", []), f"restricted wrong: {terms}"

        # ตรวจ status
        assert "voice.json" in status.get("created", []), f"status wrong: {status}"
        assert "terms.json" in status.get("created", [])


def test_migrate_visual_to_visual_json():
    """มี visual_guidelines.md → สร้าง visual.json."""
    from src.brand_migrate import migrate_brand

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "visual_guidelines.md").write_text(_SAMPLE_VISUAL, encoding="utf-8")

        status = migrate_brand(brand_dir)

        visual_path = brand_dir / "visual.json"
        assert visual_path.exists(), "visual.json not created"
        visual = json.loads(visual_path.read_text(encoding="utf-8"))
        # colors มี primary hex
        colors = visual.get("colors", {})
        assert "#1a73e8" in str(colors.get("primary", "")), f"colors wrong: {colors}"
        # keywords มี soft light
        keywords = visual.get("keywords", [])
        assert "soft light" in keywords, f"keywords wrong: {keywords}"
        # avoid มี dark
        avoid = visual.get("avoid", [])
        assert "dark" in avoid, f"avoid wrong: {avoid}"

        assert "visual.json" in status.get("created", [])


def test_migrate_audience_to_audience_json():
    """มี target_audience.md → สร้าง audience.json."""
    from src.brand_migrate import migrate_brand

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "target_audience.md").write_text(_SAMPLE_AUDIENCE, encoding="utf-8")

        status = migrate_brand(brand_dir)

        audience_path = brand_dir / "audience.json"
        assert audience_path.exists(), "audience.json not created"
        audience = json.loads(audience_path.read_text(encoding="utf-8"))
        primary = audience.get("primary", {})
        assert "30-45" in str(primary.get("age", "")), f"primary age wrong: {primary}"
        pain = audience.get("pain_points", [])
        assert "ความปลอดภัย" in pain, f"pain_points wrong: {pain}"

        assert "audience.json" in status.get("created", [])


def test_migrate_audience_extracts_lifestyle_and_buying_behavior():
    """migrate audience ต้องดึงฟิลด์ที่เคยหาย: lifestyle, buying_behavior, search_channels, เพศ, ที่อยู่.

    Bug เดิม: _parse_target_audience ดึงแค่ primary/end_user/pain_points/channels
    ทิ้งฟิลด์: เพศ, ที่อยู่, ไลฟ์สไตล์, พฤติกรรมการซื้อ, ช่องค้นหาข้อมูล.
    """
    from src.brand_migrate import migrate_brand

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "target_audience.md").write_text(_SAMPLE_AUDIENCE, encoding="utf-8")

        migrate_brand(brand_dir)

        audience = json.loads((brand_dir / "audience.json").read_text(encoding="utf-8"))

        # primary มีเพศ + ที่อยู่
        primary = audience.get("primary", {})
        assert "หญิง" in str(primary.get("เพศ", "") or primary.get("gender", "")), \
            f"gender missing: {primary}"
        assert "กรุงเทพ" in str(primary.get("ที่อยู่", "") or primary.get("location", "")), \
            f"location missing: {primary}"

        # lifestyle เป็น list
        lifestyle = audience.get("lifestyle", [])
        assert isinstance(lifestyle, list) and len(lifestyle) > 0, \
            f"lifestyle missing or not list: {audience}"
        assert any("สุขภาพ" in s for s in lifestyle), \
            f"lifestyle content wrong: {lifestyle}"

        # buying_behavior เป็น dict
        bb = audience.get("buying_behavior", {})
        assert isinstance(bb, dict), f"buying_behavior not dict: {audience}"
        assert "ความปลอดภัย" in str(bb.get("decision_factors", "")), \
            f"decision_factors missing: {bb}"
        assert "2,000" in str(bb.get("budget_per_purchase", "")), \
            f"budget missing: {bb}"

        # search_channels แยกจาก channels
        search = audience.get("search_channels", [])
        assert isinstance(search, list), f"search_channels not list: {audience}"
        assert any("Google" in s for s in search) or any("YouTube" in s for s in search), \
            f"search_channels content wrong: {search}"


def test_migrate_keeps_profile_md():
    """มี brand_profile.md → เก็บเป็น .md ไว้ (ไม่ convert เป็น .json)."""
    from src.brand_migrate import migrate_brand

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "brand_profile.md").write_text(_SAMPLE_PROFILE, encoding="utf-8")

        status = migrate_brand(brand_dir)

        # profile.md ยังอยู่
        assert (brand_dir / "brand_profile.md").exists(), "profile.md should remain"
        # ไม่สร้าง profile.json
        assert not (brand_dir / "profile.json").exists(), "profile.json should NOT be created"


def test_migrate_no_md_does_nothing():
    """ไม่มี .md เลย → ไม่ทำอะไร คืน empty status."""
    from src.brand_migrate import migrate_brand

    with tempfile.TemporaryDirectory() as tmp:
        status = migrate_brand(Path(tmp))
        assert status.get("created", []) == [], f"Expected no files created, got: {status}"


def test_migrate_backs_up_md():
    """หลัง migrate → .md เดิมเก็บเป็น .md.bak (ไม่ลบทิ้ง)."""
    from src.brand_migrate import migrate_brand

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "tone_of_voice.md").write_text(_SAMPLE_TONE, encoding="utf-8")

        migrate_brand(brand_dir)

        # .md.bak ต้องมี (เก็บข้อมูลเดิมไว้)
        bak_path = brand_dir / "tone_of_voice.md.bak"
        assert bak_path.exists(), f".md.bak not created: {bak_path}"
        # เนื้อหา .bak ต้องตรงของเดิม
        assert "เหมือนพ่อแม่" in bak_path.read_text(encoding="utf-8")


def test_migrate_does_not_overwrite_existing_json():
    """มี .json อยู่แล้ว → ไม่ overwrite (เว้นแต่ force=True)."""
    from src.brand_migrate import migrate_brand

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "tone_of_voice.md").write_text(_SAMPLE_TONE, encoding="utf-8")
        # สร้าง voice.json เดิมไว้ก่อน (มีข้อมูลต่างจาก .md)
        existing_voice = {"personality": "EXISTING — ห้ามทับ"}
        (brand_dir / "voice.json").write_text(json.dumps(existing_voice), encoding="utf-8")

        status = migrate_brand(brand_dir)  # force=False (default)

        voice = json.loads((brand_dir / "voice.json").read_text(encoding="utf-8"))
        assert voice["personality"] == "EXISTING — ห้ามทับ", f"JSON was overwritten: {voice}"
        assert "voice.json" not in status.get("created", []), f"Should not be in created: {status}"


def test_migrate_force_overwrites_existing_json():
    """force=True → overwrite .json ที่มีอยู่แล้ว."""
    from src.brand_migrate import migrate_brand

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "tone_of_voice.md").write_text(_SAMPLE_TONE, encoding="utf-8")
        existing_voice = {"personality": "OLD"}
        (brand_dir / "voice.json").write_text(json.dumps(existing_voice), encoding="utf-8")

        status = migrate_brand(brand_dir, force=True)

        voice = json.loads((brand_dir / "voice.json").read_text(encoding="utf-8"))
        assert "เหมือนพ่อแม่" in voice["personality"], f"JSON not overwritten: {voice}"


def test_migrate_all_files_together():
    """มี .md ครบทั้ง 4 ไฟล์ → migrate ทั้งหมดในครั้งเดียว."""
    from src.brand_migrate import migrate_brand

    with tempfile.TemporaryDirectory() as tmp:
        brand_dir = Path(tmp)
        (brand_dir / "tone_of_voice.md").write_text(_SAMPLE_TONE, encoding="utf-8")
        (brand_dir / "visual_guidelines.md").write_text(_SAMPLE_VISUAL, encoding="utf-8")
        (brand_dir / "target_audience.md").write_text(_SAMPLE_AUDIENCE, encoding="utf-8")
        (brand_dir / "brand_profile.md").write_text(_SAMPLE_PROFILE, encoding="utf-8")

        status = migrate_brand(brand_dir)

        created = status.get("created", [])
        assert "voice.json" in created
        assert "terms.json" in created
        assert "visual.json" in created
        assert "audience.json" in created
        # profile.md ยังอยู่
        assert (brand_dir / "brand_profile.md").exists()


if __name__ == "__main__":
    tests = [
        test_migrate_tone_to_voice_and_terms,
        test_migrate_visual_to_visual_json,
        test_migrate_audience_to_audience_json,
        test_migrate_audience_extracts_lifestyle_and_buying_behavior,
        test_migrate_keeps_profile_md,
        test_migrate_no_md_does_nothing,
        test_migrate_backs_up_md,
        test_migrate_does_not_overwrite_existing_json,
        test_migrate_force_overwrites_existing_json,
        test_migrate_all_files_together,
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
