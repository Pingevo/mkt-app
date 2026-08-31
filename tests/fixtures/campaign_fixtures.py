"""Version-controlled campaign strategy fixtures for offline acceptance tests.

These fixtures are deterministic raw outputs paired with the semantic context
and instructions used to validate them.  No API calls are made.
"""

from __future__ import annotations


def _base() -> str:
    return (
        "## ราคาแนะนำ\n"
        "- กลุ่มราคาเป้าหมาย: ระดับ mid-range (ต้องกำหนดหลังมีต้นทุน)\n"
        "- ราคาโปรโมชัน: ไม่สามารถเสนอตัวเลขได้เนื่องจากไม่มีข้อมูลต้นทุน (pending validation)\n"
        "- ราคาส่ง/ตัวแทน: ไม่สามารถระบุได้เนื่องจากขาดข้อมูลต้นทุน\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n\n"
        "## KPI ที่ควรวัดผล\n"
        "- ยอดขาย (target ต้องกำหนดหลังมี baseline)\n\n"
        "## งบประมาณประมาณการ\n"
    )


def _tail(sources: str) -> str:
    return "## แหล่งอ้างอิง\n" + sources + "\n"


def _good_product_only() -> str:
    return _base() + "- ประมาณ 50,000 บาท (estimate)\n\n" + _tail("- ไม่มี external factual claim ที่ต้องอ้างอิง")


def _with_competitor_source() -> str:
    return (
        "## ราคาแนะนำ\n"
        "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
        "- ราคาโปรโมชัน: ฿2,240 (indicative)\n"
        "- ราคาคู่แข่ง imoo Z1: 2,990 บาท [Shopee](https://shopee.co.th/imoo-z1)\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n\n"
        "## KPI ที่ควรวัดผล\n"
        "- ยอดขาย (target ต้องกำหนดหลังมี baseline)\n\n"
        "## งบประมาณประมาณการ\n"
        "- ประมาณ 50,000 บาท (estimate)\n\n"
        "## แหล่งอ้างอิง\n"
        "- [Shopee - imoo Z1](https://shopee.co.th/imoo-z1)\n"
    )


CASES: list[dict] = [
    {
        "case_id": "A_product_only",
        "description": "product only — ราคาปลีก estimate ได้ ไม่มี external claim",
        "context": {"product": "LAGENIO K2\nหน้าจอ AMOLED"},
        "instructions": {},
        "quick_brief": "",
        "output": _good_product_only(),
        "expected_ok": True,
        "expected_rule": "",
    },
    {
        "case_id": "B_verified_competitor",
        "description": "verified competitor artifact — ราคาคู่แข่งมี citation ชัด",
        "context": {
            "product": "LAGENIO K2",
            "competitors": (
                "คู่แข่ง: imoo Z1\n"
                "ราคา: 2,990 บาท\n"
                "แหล่ง: [Shopee](https://shopee.co.th/imoo-z1)"
            ),
        },
        "instructions": {},
        "quick_brief": "",
        "output": _with_competitor_source(),
        "expected_ok": True,
        "expected_rule": "",
    },
    {
        "case_id": "C_missing_financials",
        "description": "missing COGS/margin/budget — ราคาส่งไม่ระบุตัวเลข ยอมรับได้",
        "context": {
            "product": "LAGENIO K2",
            "business": (
                "เป้าหมายแคมเปญ: เปิดตัวสินค้า\n"
                "ช่องทางหลัก: TikTok, Facebook, Shopee, Lazada\n"
                "กลุ่มเป้าหมาย: พ่อแม่ผู้ปกครอง"
            ),
        },
        "instructions": {},
        "quick_brief": "",
        "output": _good_product_only(),
        "expected_ok": True,
        "expected_rule": "",
    },
    {
        "case_id": "D_budget_ceiling_fail",
        "description": "budget และ discount ceiling ถูกละเมิด",
        "context": {"product": "LAGENIO K2"},
        "instructions": {"budget_max": "100000", "discount_max": "30"},
        "quick_brief": "",
        "output": _base() + "- งบประมาณ 150,000 บาท\n\n" + _tail("- ไม่มี external factual claim"),
        "expected_ok": False,
        "expected_rule": "budget_max_enforced",
    },
    {
        "case_id": "E_discount_forbid_fail",
        "description": "discount เกิน ceiling และของแถมยังไม่ validate",
        "context": {"product": "LAGENIO K2"},
        "instructions": {"discount_max": "30"},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- ราคาโปรโมชัน: ฿2,000 (indicative, ลด 40%, pending financial validation)\n\n"
            "## แคมเปญหลัก\n"
            "- แคมเปญ Launch\n\n"
            "## แคมเปญเสริม\n- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n- ยอดขาย (ตาม baseline)\n\n"
            "## งบประมาณประมาณการ\n- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n- ไม่มี external factual claim\n"
        ),
        "expected_ok": False,
        "expected_rule": "discount_max_enforced",
    },
    {
        "case_id": "F_conflict_unacknowledged",
        "description": "context ขัดแย้งแต่ output ไม่ระบุ",
        "context": {
            "product": "LAGENIO K2",
            "business": (
                "ข้อมูลขัดแย้ง: คู่แข่ง imoo Z1 ราคา 2,990 บาท "
                "แต่ market ระบุ 3,500 บาท"
            ),
        },
        "instructions": {},
        "quick_brief": "",
        "output": _good_product_only(),
        "expected_ok": False,
        "expected_rule": "conflict_acknowledged",
    },
    {
        "case_id": "G_quick_brief_guaranteed_sales",
        "description": "quick brief ขอ guaranteed sales target",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "ตั้งเป้ายอดขาย 1,000 ชิ้น",
        "output": _base() + (
            "- ประมาณ 50,000 บาท (estimate)\n\n"
            "## KPI ที่ควรวัดผล\n"
            "- เป้าหมายยอดขาย 1,000 ชิ้น (target)\n\n"
            "## แหล่งอ้างอิง\n- ไม่มี external factual claim\n"
        ),
        "expected_ok": False,
        "expected_rule": "no_guaranteed_numeric_targets_without_baseline",
    },
    {
        "case_id": "H_competitor_price_evidence_required",
        "description": "ขอราคาคู่แข่งปัจจุบัน แต่ไม่มี competitor ในบริบท — อ้าง Z1 ผิด identity",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "",
        "output": _with_competitor_source(),
        "expected_ok": False,
        "expected_rule": "product_identity_intact",
    },
    {
        "case_id": "I_unsupported_benchmark",
        "description": "ROAS 3-4x โดยไม่มี source",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "",
        "output": _base() + (
            "- ประมาณ 50,000 บาท (estimate)\n\n"
            "## KPI ที่ควรวัดผล\n"
            "- ROAS 3-4x (benchmark)\n\n"
            "## แหล่งอ้างอิง\n- ไม่มี external factual claim\n"
        ),
        "expected_ok": False,
        "expected_rule": "benchmarks_cited_or_removed",
    },
    {
        "case_id": "J_wholesale_without_financials",
        "description": "numeric wholesale เมื่อไม่มี COGS",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
            "- ราคาโปรโมชัน: ฿2,240 (indicative)\n"
            "- ราคาส่ง: ฿1,500\n\n"
            "## แคมเปญหลัก\n- Launch\n\n"
            "## แคมเปญเสริม\n- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n- ยอดขาย\n\n"
            "## งบประมาณประมาณการ\n- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n- ไม่มี external factual claim\n"
        ),
        "expected_ok": False,
        "expected_rule": "no_numeric_wholesale_margin_without_financials",
    },
    {
        "case_id": "K_url_dump_and_search_tag",
        "description": "URL dump, marketplace/search-tag source, citation ไม่ผูก claim",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
            "- [Shopee search](https://shopee.co.th/search?q=smartwatch)\n\n"
            "## แคมเปญหลัก\n- Launch\n\n"
            "## แคมเปญเสริม\n- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n- ยอดขาย\n\n"
            "## งบประมาณประมาณการ\n- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n"
            "- [Shopee search](https://shopee.co.th/search?q=smartwatch)\n"
            "- [Lazada tag](https://lazada.co.th/tag/smartwatch)\n"
        ),
        "expected_ok": False,
        "expected_rule": "unauthorized_url_or_source",
    },
    {
        "case_id": "L_complete_financials",
        "description": "context ครบทางการเงิน ระบุ margin ได้",
        "context": {
            "product": "LAGENIO K2",
            "business": (
                "ต้นทุน: 1,500 บาท\n"
                "margin เป้าหมาย: 30%\n"
                "ค่าธรรมเนียม: 5%\n"
                "งบประมาณ: 100,000 บาท"
            ),
        },
        "instructions": {},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- ราคาขายปลีก: ฿2,490\n"
            "- ราคาโปรโมชัน: ฿2,240\n"
            "- ราคาส่ง: ฿1,750, margin 30%\n\n"
            "## แคมเปญหลัก\n"
            "- ชื่อ: Launch\n\n"
            "## แคมเปญเสริม\n"
            "- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n"
            "- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n"
            "- ยอดขาย (target ตาม baseline)\n\n"
            "## งบประมาณประมาณการ\n"
            "- 100,000 บาท\n\n"
            "## แหล่งอ้างอิง\n"
            "- ไม่มี external factual claim\n"
        ),
        "expected_ok": True,
        "expected_rule": "",
    },
    {
        "case_id": "M_unauthorized_arbitrary_url",
        "description": "ROAS มี inline URL แต่ไม่อยู่ใน selected evidence manifest",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
            "- ราคาโปรโมชัน: ฿2,240 (indicative)\n\n"
            "## แคมเปญหลัก\n"
            "- ชื่อ: Launch\n\n"
            "## แคมเปญเสริม\n"
            "- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n"
            "- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n"
            "- ROAS 3-4x [Industry Report](https://example.com/report)\n\n"
            "## งบประมาณประมาณการ\n"
            "- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n"
            "- [Industry Report](https://example.com/report)\n"
        ),
        "expected_ok": False,
        "expected_rule": "unauthorized_url_or_source",
    },
    {
        "case_id": "N_unsupported_benchmark_loose_phrase",
        "description": "ROAS โดยไม่มี URL แค่ข้อความ \"จากข้อมูล\" — ต้อง fail",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
            "- ราคาโปรโมชัน: ฿2,240 (indicative)\n\n"
            "## แคมเปญหลัก\n"
            "- ชื่อ: Launch\n\n"
            "## แคมเปญเสริม\n"
            "- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n"
            "- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n"
            "- ROAS 3-4x จากข้อมูลทั่วไป\n\n"
            "## งบประมาณประมาณการ\n"
            "- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n"
            "- ไม่มี external factual claim\n"
        ),
        "expected_ok": False,
        "expected_rule": "benchmarks_cited_or_removed",
    },
    {
        "case_id": "O_floating_link_and_search_tag",
        "description": "ลิงก์ลอยใน body จาก search-tag URL — ต้อง fail",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
            "- ราคาโปรโมชัน: ฿2,240 (indicative)\n"
            "- [Shopee search](https://shopee.co.th/search?q=smartwatch)\n\n"
            "## แคมเปญหลัก\n"
            "- ชื่อ: Launch\n\n"
            "## แคมเปญเสริม\n"
            "- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n"
            "- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n"
            "- ยอดขาย\n\n"
            "## งบประมาณประมาณการ\n"
            "- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n"
            "- [Shopee search](https://shopee.co.th/search?q=smartwatch)\n"
        ),
        "expected_ok": False,
        "expected_rule": "unauthorized_url_or_source",
    },
    {
        "case_id": "P_raw_url_dump_source_section",
        "description": "raw URL dump ใน source section โดยไม่ผูก claim",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
            "- ราคาโปรโมชัน: ฿2,240 (indicative)\n\n"
            "## แคมเปญหลัก\n- ชื่อ: Launch\n\n"
            "## แคมเปญเสริม\n- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n- ยอดขาย\n\n"
            "## งบประมาณประมาณการ\n"
            "- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n"
            "- https://example.com/one\n"
            "- https://example.com/two\n"
        ),
        "expected_ok": False,
        "expected_rule": "no_bare_urls",
    },
    {
        "case_id": "Q_product_identity_mutation",
        "description": "product K2 แต่ output อ้าง LAGENIO K3",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- กลุ่มราคาเป้าหมาย: ระดับ mid-range (ต้องกำหนดหลังมีต้นทุน)\n"
            "- ราคาโปรโมชัน: ไม่สามารถเสนอตัวเลขได้ (pending validation)\n\n"
            "## แคมเปญหลัก\n"
            "- ชื่อ: Launch สำหรับ LAGENIO K3\n\n"
            "## แคมเปญเสริม\n- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n- ยอดขาย\n\n"
            "## งบประมาณประมาณการ\n"
            "- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n"
            "- ไม่มี external factual claim\n"
        ),
        "expected_ok": False,
        "expected_rule": "product_identity_intact",
    },
    {
        "case_id": "R_budget_wording_bypass",
        "description": "budget wording bypass ด้วย 'ใช้ 150,000 บาท'",
        "context": {"product": "LAGENIO K2"},
        "instructions": {"budget_max": "100000"},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- กลุ่มราคาเป้าหมาย: ระดับ mid-range (ต้องกำหนดหลังมีต้นทุน)\n"
            "- ราคาโปรโมชัน: ไม่สามารถเสนอตัวเลขได้ (pending validation)\n\n"
            "## แคมเปญหลัก\n- ชื่อ: Launch\n\n"
            "## แคมเปญเสริม\n- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n- ยอดขาย\n\n"
            "## งบประมาณประมาณการ\n"
            "- ใช้ 150,000 บาทกับ Meta Ads\n\n"
            "## แหล่งอ้างอิง\n"
            "- ไม่มี external factual claim\n"
        ),
        "expected_ok": False,
        "expected_rule": "budget_max_enforced",
    },
    {
        "case_id": "S_kpi_numeric_target_bypass",
        "description": "ยอดขาย 1,000 ชิ้น โดยไม่มี baseline และไม่ใช้คำ target",
        "context": {"product": "LAGENIO K2"},
        "instructions": {},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- ราคาขายปลีก: ฿2,490-2,790 (estimate)\n"
            "- ราคาโปรโมชัน: ฿2,240 (indicative)\n\n"
            "## แคมเปญหลัก\n- ชื่อ: Launch\n\n"
            "## แคมเปญเสริม\n- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n"
            "- ยอดขาย 1,000 ชิ้น\n\n"
            "## งบประมาณประมาณการ\n"
            "- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n"
            "- ไม่มี external factual claim\n"
        ),
        "expected_ok": False,
        "expected_rule": "no_guaranteed_numeric_targets_without_baseline",
    },
    {
        "case_id": "T_discount_money_bypass",
        "description": "ส่วนลดเงิน ฿1,000 จากราคา ฿2,000 = 50% เกิน ceiling 30%",
        "context": {"product": "LAGENIO K2"},
        "instructions": {"discount_max": "30"},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- ราคาขายปลีก: ฿2,000 (estimate)\n"
            "- ราคาโปรโมชัน: ฿2,240 (indicative)\n"
            "- ลด ฿1,000\n\n"
            "## แคมเปญหลัก\n- ชื่อ: Launch\n\n"
            "## แคมเปญเสริม\n- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n- ยอดขาย\n\n"
            "## งบประมาณประมาณการ\n"
            "- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n"
            "- ไม่มี external factual claim\n"
        ),
        "expected_ok": False,
        "expected_rule": "discount_max_enforced",
    },
    {
        "case_id": "U_selected_evidence_manifest_valid",
        "description": "market context มี report URL ตรงรุ่น → ROAS ผูกกับ URL นั้นผ่าน",
        "context": {
            "product": "LAGENIO K2 smartwatch for kids",
            "market": (
                "ข้อมูลตลาด: [Industry Report](https://example.com/report)"
            ),
        },
        "instructions": {},
        "quick_brief": "",
        "output": (
            "## ราคาแนะนำ\n"
            "- กลุ่มราคาเป้าหมาย: ระดับ mid-range (ต้องกำหนดหลังมีต้นทุน)\n"
            "- ราคาโปรโมชัน: ไม่สามารถเสนอตัวเลขได้ (pending validation)\n\n"
            "## แคมเปญหลัก\n- ชื่อ: Launch\n\n"
            "## แคมเปญเสริม\n- แคมเปญ 1\n\n"
            "## ช่องทางโปรโมท\n- TikTok\n\n"
            "## KPI ที่ควรวัดผล\n"
            "- ROAS 3-4x [Industry Report](https://example.com/report)\n\n"
            "## งบประมาณประมาณการ\n"
            "- ประมาณ 50,000 บาท (estimate)\n\n"
            "## แหล่งอ้างอิง\n"
            "- [Industry Report](https://example.com/report)\n"
        ),
        "expected_ok": True,
        "expected_rule": "",
    },
]
