"""TDD tests for output_quality contract.

Offline — no OpenRouter calls.
"""

from __future__ import annotations

import pytest

from src import output_validators


OUTPUT_QUALITY = {
    "min_total_chars": 500,
    "min_non_citation_chars": 300,
    "min_analysis_blocks": 2,
    "forbid_citation_only": True,
}


def test_citation_dump_only_fails() -> None:
    """citation dump อย่างเดียว → fail."""
    output = (
        "**แหล่งอ้างอิง:**\n"
        "- [One](https://example.com/1)\n"
        "- [Two](https://example.com/2)\n"
        "- [Three](https://example.com/3)\n"
    ) * 10

    ok, err = output_validators.validate_output(
        "competitor_analysis",
        output,
        output_quality=OUTPUT_QUALITY,
    )

    assert not ok
    assert "forbid" in err or "citation" in err or "non-citation" in err or err


def test_short_intro_plus_citations_fails() -> None:
    """บทนำสั้น + citation dump → fail."""
    output = (
        "นี่คือรายงานสั้น ๆ\n\n"
        "**แหล่งอ้างอิง:**\n"
        "- [One](https://example.com/1)\n"
        "- [Two](https://example.com/2)\n"
    ) * 5

    ok, err = output_validators.validate_output(
        "competitor_analysis",
        output,
        output_quality=OUTPUT_QUALITY,
    )

    assert not ok


def test_good_report_with_headings_passes() -> None:
    """รายงานดีแบบ heading ปกติ → pass."""
    output = (
        "## ภาพรวมตลาด\n\n"
        "ตลาดสมาร์ทวอทช์เด็กเติบโตขึ้นอย่างต่อเนื่องจากการใช้งานของผู้ปกครอง "
        "ทีต้องการติดต่อลูกได้ตลอดเวลา รวมถึงความกังวลเรื่องความปลอดภัย "
        "ทำให้สมาร์ทวอทช์เด็กมีการเติบโตในกลุ่มประเทศเอเชียตะวันออกเฉียงใต้ "
        "โดยเฉพาะในเมืองใหญ่ทีผู้ปกครองมีงานประจำและต้องการติดตามลูกน้อย\n\n"
        "## ตารางเปรียบเทียบ\n\n"
        "| คุณสมบัติ | เรา | คู่แข่ง |\n"
        "|---|---|---|\n"
        "| แบต | 680mAh | 580mAh |\n"
        "| กล้อง | 5MP | 2MP |\n"
        "| หน้าจอ | 1.78\" AMOLED | 1.3\" TFT |\n\n"
        "## คำแนะนำ\n\n"
        "ควรเน้นจุดขายด้านแบตเตอรี่และกล้องหน้าคมชัดเพื่อแข่งขันกับ imoo "
        "และ Xiaomi ในตลาดนี้ โดยเฉพาะการสื่อสารวาสินค้าปลอดภัยและใช้งานง่าย "
        "ซึ่งจะช่วยเพิ่มความน่าเชื่ือถือและตัดสินใจซื้าอย่างรวดเร็ว\n\n"
        "[Source](https://example.com/source)"
    )

    ok, err = output_validators.validate_output(
        "competitor_analysis",
        output,
        output_quality=OUTPUT_QUALITY,
    )

    assert ok, err


def test_good_report_with_bold_sections_passes() -> None:
    """รายงานดีแบบ bold sections ไม่มี `##` → pass."""
    output = (
        "**ภาพรวมตลาด:**\n\n"
        "ตลาดสมาร์ทวอทช์เด็กเติบโตขึ้นอย่างต่อเนื่องจากการใช้งานของผู้ปกครอง "
        "ทีต้องการติดต่อลูกได้ตลอดเวลา รวมถึงความกังวลเรื่องความปลอดภัย "
        "ทำให้สมาร์ทวอทช์เด็กมีการเติบโตในกลุ่มประเทศเอเชียตะวันออกเฉียงใต้ "
        "โดยเฉพาะในเมืองใหญ่ทีผู้ปกครองมีงานประจำและต้องการติดตามลูกน้อย\n\n"
        "**คำแนะนำเชิงกลยุทธ์:**\n\n"
        "ควรเน้นจุดขายด้านแบตเตอรี่และกล้องหน้าคมชัดเพื่อแข่งขันกับ imoo "
        "และ Xiaomi ในตลาดนี้ โดยเฉพาะการสื่อสารวาสินค้าปลอดภัยและใช้งานง่าย "
        "ซึ่งจะช่วยเพิ่มความน่าเชื่ือถือและตัดสินใจซื้าอย่างรวดเร็ว\n\n"
        "[Source](https://example.com/source)"
    )

    ok, err = output_validators.validate_output(
        "competitor_analysis",
        output,
        output_quality=OUTPUT_QUALITY,
    )

    assert ok, err


def test_long_unstructured_fails() -> None:
    """รายงานยาวแต่ไม่มี analysis structure → fail."""
    output = (
        "นี่คือข้อความยาว ๆ ทีอธิบายถึงสถานการณ์ทั่วไปในตลาด "
        "แต่ไม่มีหัวข้อหรือโครงสร้างใด ๆ ช่วยให้ผู้อ่านเข้าใจว่าสินค้าไหนเหนือกว่า "
        "หรือควรทำอย่างไร ตลาดเติบโตแต่ไม่มีการวิเคราะห์เชิงลึก "
        "ทำให้เอกสารฉบับนี้ไม่สามารถนำไปใช้ตัดสินใจได้โดยตรง "
        "ถ้าอ่านดูจะพบวาไม่มีตาราง ไม่มีรายการจุดแข็ง ไม่มีคำแนะนำ "
        "แค่พูดถึงความเป็นไปได้และ trend ทั่วไปเท่านั้น "
        "ซึ่งไม่เพียงพอสำหรับรายงาน competitor analysis "
        "ผู้อ่านต้องการข้อมูลเชิงเปรียบเทียบและคำแนะนำทีชัดเจน "
        "ไม่ใช่แค่ข้อความยาว ๆ ทีไม่มีโครงสร้าง "
        "นี่คือการทดสอบ validator วาจะ reject output ทีไม่มี analysis blocks ได้จริง "
        "โดยไม่ให้ความยาวของข้อความหลอก validator ว่าผ่าน "
    )

    ok, err = output_validators.validate_output(
        "competitor_analysis",
        output,
        output_quality=OUTPUT_QUALITY,
    )

    assert not ok
    assert "analysis" in err or "structure" in err or err


def test_short_with_substantive_structure_passes() -> None:
    """รายงานสั้นแต่มี comparison + recommendation จริง → pass."""
    output = (
        "**เปรียบเทียบสั้น ๆ:**\n\n"
        "| คุณสมบัติ | เรา | คู่แข่ง |\n"
        "|---|---|---|\n"
        "| แบต | 680mAh | 580mAh |\n"
        "| กล้อง | 5MP | 2MP |\n"
        "| หน้าจอ | 1.78\" AMOLED | 1.3\" TFT |\n"
        "| กันน้ำ | IP68 | IPX8 |\n\n"
        "**คำแนะนำ:**\n\n"
        "เน้นขายแบตเตอรี่และกล้อง เป้าหมายพ่อแม่ทีกังวลความปลอดภัยและไม่อยากชาร์จบ่อย "
        "โดยยกระดับจุดขายด้านความปลอดภัยและใช้งานง่ายให้เด่นชัดในสื่อสารการตลาด "
        "พร้อมทั้งสร้างข้อความทีเน้นความโปร่งใสของข้อมูลและสื่อสารกลุ่มเป้าหมายให้ชัดเจน "
        "นี่คือข้อสรุปหลักสำหรับทีมการตลาดลงมือทันที\n\n"
        "[Source](https://example.com/source)"
    )

    ok, err = output_validators.validate_output(
        "competitor_analysis",
        output,
        output_quality=OUTPUT_QUALITY,
    )

    assert ok, err
