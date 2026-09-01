"""Phase 6b: Synthetic candidate-contract fixture for Gate 3.

This is a **synthetic, offline** fixture — NOT a real model result.
It demonstrates that the acceptance contract is achievable using the
preserved Gate 3 evidence annotations.

The fixture output:
  - cites observed imoo Z1 ฿3,999 inline from selected evidence;
  - identifies the source as a retailer listing;
  - does not overclaim availability when uncertain (Thisshop = out_of_stock);
  - provides an evidence-backed positioning hypothesis satisfying Basis B:
    * cited observed competitor price ฿3,999 from TG Fone;
    * explicit positioning: below competitor;
    * mathematical consistency: ฿2,990–฿3,490 < ฿3,999;
    * labelled as Indicative Estimate pending financial validation;
    * acknowledges missing COGS, margin, and fees;
  - contains no uncited benchmark;
  - passes all validator and behavioral checks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.campaign_validator import audit_campaign_output, extract_context_flags
from src.config_loader import get_agent_config, load_config

RUN_DIR = Path(__file__).resolve().parent.parent / "data" / "campaign_paid_acceptance" / "run-2026-08-31T05-46-01.25174"

# Synthetic output — clearly labelled, NOT a real model result.
# Uses the preserved Gate 3 evidence annotations.
SYNTHETIC_CANDIDATE_OUTPUT = """\
### 1. ราคาแนะนำ (Recommended Pricing)

* **ราคาส่ง / ตัวแทนจำหน่าย (Wholesale / Agent Price):** ไม่สามารถเสนอตัวเลขได้ เนื่องจากไม่มีข้อมูลต้นทุนสินค้า (COGS), มาร์จิ้นเป้าหมาย และค่าธรรมเนียมช่องทางการจำหน่ายในระบบ

* **ราคาขายปลีกแนะนำ (Retail Price - Indicative Estimate):** 2,990 – 3,490 บาท *(Pending financial validation)* วางตำแหน่งต่ำกว่า [imoo Z1 ฿3,999 จากร้าน TG Fone](https://www.tgfone.com/product/detail/3554/imoo-Watch-Phone-Z1-Grapefruit-Red) COGS และ margin ยังไม่ทราบ ค่าธรรมเนียมช่องทางยังไม่ทราบ

* **เหตุผลประกอบ (Rationale):**
  จากการสำรวจราคาคู่แข่งในตลาด พบว่า [imoo Watch Phone Z1 มีราคาจัดจำหน่ายอยู่ที่ 3,999 บาท](https://www.tgfone.com/product/detail/3554/imoo-Watch-Phone-Z1-Grapefruit-Red) จากร้านค้าปลีกหลายแห่ง การวางตำแหน่งราคาของ LAGENIO K2 ในช่วงต่ำกว่าคู่แข่งหลัก จะช่วยเปิดโอกาสให้กลุ่มคุณแม่และผู้ปกครองเข้าถึงสมาร์ทวอทช์เด็กได้ง่ายขึ้น ทั้งนี้ตัวเลขราคาต้องได้รับการคำนวณร่วมกับโครงสร้างต้นทุนจริงอีกครั้ง

---

### 2. แคมเปญหลัก (Main Campaign)

* **ชื่อแคมเปญ:** "ทุกก้าวของลูก อุ่นใจแม่เสมอ กับ LAGENIO K2"
* **วัตถุประสงค์:** สร้างการรับรู้และกระตุ้นการตัดสินใจซื้อสมาร์ทวอทช์เด็ก LAGENIO K2
* **กลุ่มเป้าหมาย:** คุณแม่และผู้ปกครอง อายุ 25–45 ปี
* **ระยะเวลาแคมเปญ:** 30 วัน
* **กลไกแคมเปญ (Campaign Mechanics):**
  * **Storytelling Campaign:** ถ่ายทอดเรื่องราวความห่วงใยของคุณแม่ยุคใหม่ *(pending financial/operational validation)*
  * **Welcome Voucher:** มอบคูปองส่วนลดสำหรับการสั่งซื้อครั้งแรก *(pending financial/operational validation)*

---

### 3. ช่องทางโปรโมท (Promotion Channels)

* **Facebook & Instagram:** สื่อสารด้วยคอนเทนต์เชิงภาพและวิดีโอสั้น
* **TikTok:** นำเสนอคลิปสั้นสาธิตฟังก์ชันใช้งาน
* **E-Commerce (Shopee & Lazada):** ช่องทางหลักในการปิดการขาย

---

### 4. KPI ที่ควรวัดผล (Success Metrics)

*เนื่องจากยังไม่มีข้อมูลฐานตัวเลขเดิม (Baseline) ในระบบ ตัวเลขเป้าหมายที่แท้จริงต้องกำหนดหลังจากการเก็บข้อมูลแคมเปญรอบแรก*

* **ด้านการรับรู้ (Brand Awareness):** Reach และ Video Views
* **ด้านความสนใจ (Engagement):** CTR จากโพสต์และโฆษณา
* **ด้านยอดขาย (Conversion):** Conversion Rate บน E-Commerce
* **ด้านความพึงพอใจ (Customer Satisfaction):** คะแนนรีวิวสินค้าเฉลี่ย

---

### 5. งบประมาณประมาณการ (Estimated Budget Range)

ไม่สามารถระบุตัวเลขงบประมาณได้ เนื่องจากไม่มีข้อมูลเพดานงบประมาณและเป้าหมายรายได้

**กรอบการจัดสรรงบประมาณเชิงคุณภาพ (Qualitative Budget Framework):**
1. **Conversion & Performance Ads**
2. **Influencer & Content Creation**
3. **Production & Marketing Collaterals**

---

### 6. ข้อมูลตลาดที่สังเกตได้ (Observed Market Data)

* [imoo Z1 ฿3,999 จากร้าน TG Fone](https://www.tgfone.com/product/detail/3554/imoo-Watch-Phone-Z1-Grapefruit-Red) — ร้านค้าปลีก ระบุราคาจำหน่าย สถานะสินค้า: มีการขาย
* [imoo Z1 ฿3,999 จาก Central Online](https://www.central.co.th/th/imoo-kid-watch-phone-z1-bamboo-green-cds91192936) — ร้านค้าปลีก สถานะ: Pre-order
* [imoo Z1 ฿3,650 จาก Thisshop](https://www.thisshop.com/item/detail?itemId=4001701028401) — ร้านค้าปลีก สถานะ: Inventory shortage (อาจไม่พร้อมจัดส่ง ณ ปัจจุบัน)

---

### 7. แหล่งอ้างอิง (Sources)

* [ข้อมูลราคา imoo Z1 จาก TG Fone](https://www.tgfone.com/product/detail/3554/imoo-Watch-Phone-Z1-Grapefruit-Red)
* [ข้อมูลราคา imoo Z1 จาก Central Online](https://www.central.co.th/th/imoo-kid-watch-phone-z1-bamboo-green-cds91192936)
* [ข้อมูลราคา imoo Z1 จาก Thisshop](https://www.thisshop.com/item/detail?itemId=4001701028401)
"""


def _load_gate3_state() -> dict:
    state_path = RUN_DIR / "gate_3_state.json"
    if not state_path.exists():
        pytest.skip(f"Gate 3 state artifact not found: {state_path}")
    return json.loads(state_path.read_text(encoding="utf-8"))


def _rules() -> dict:
    return get_agent_config(load_config(), "campaign_strategy").get("semantic_rules", {})


def _build_context_from_state(state: dict) -> dict[str, str]:
    relevant = state.get("relevant_annotations", [])
    competitor_urls = "\n".join(
        f"- [{a.get('title', a.get('url', ''))}]({a.get('url', '')})" for a in relevant
    )
    return {
        "product": "Product: LAGENIO K2 smartwatch for kids. Official model K2.",
        "business": "",
        "competitors": competitor_urls,
        "market": "Mid-range kids smartwatch market in Thailand.",
        "customers": "Urban parents, working moms aged 25-45.",
    }


def _build_instructions_from_state(state: dict) -> dict:
    relevant = state.get("relevant_annotations", [])
    return {
        "selected_evidence_urls": [a.get("url", "") for a in relevant if a.get("url")],
        "selected_evidence": relevant,
    }


@pytest.fixture
def gate3_state() -> dict:
    return _load_gate3_state()


class TestSyntheticCandidateContract:
    """Synthetic candidate-contract fixture — proves the acceptance contract
    is achievable using the preserved Gate 3 evidence.

    This is NOT a real model result.  It only proves the contract is satisfiable.
    """

    def test_all_validator_rules_pass(self, gate3_state: dict):
        """The synthetic output must pass ALL validator rules."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            SYNTHETIC_CANDIDATE_OUTPUT, flags, instructions, _rules()
        )
        failures = [r for r in results if not r.ok]
        assert not failures, (
            f"Synthetic candidate should pass all rules. Failures: "
            f"{[(r.rule, r.reason) for r in failures]}"
        )

    def test_retail_price_passes_basis_b(self, gate3_state: dict):
        """The LAGENIO price hypothesis passes Basis B (evidence-backed
        positioning hypothesis with all five requirements)."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            SYNTHETIC_CANDIDATE_OUTPUT, flags, instructions, _rules()
        )
        retail = next(r for r in results if r.rule == "indicative_retail_promo_labelled")
        assert retail.ok, f"Basis B hypothesis should pass: {retail.reason}"

    def test_no_uncited_benchmark(self, gate3_state: dict):
        """No uncited benchmark should be present."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            SYNTHETIC_CANDIDATE_OUTPUT, flags, instructions, _rules()
        )
        bench = next(r for r in results if r.rule == "benchmarks_cited_or_removed")
        assert bench.ok, f"No uncited benchmark: {bench.reason}"

    def test_product_identity_intact(self, gate3_state: dict):
        """Product identity should be intact (no false model tokens from URLs)."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            SYNTHETIC_CANDIDATE_OUTPUT, flags, instructions, _rules()
        )
        identity = next(r for r in results if r.rule == "product_identity_intact")
        assert identity.ok, f"Identity intact: {identity.reason}"

    def test_competitor_mentions_grounded(self, gate3_state: dict):
        """All competitor mentions should be grounded with inline citations."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            SYNTHETIC_CANDIDATE_OUTPUT, flags, instructions, _rules()
        )
        competitor = next(r for r in results if r.rule == "competitor_mentions_grounded")
        assert competitor.ok, f"Competitor mentions grounded: {competitor.reason}"

    def test_no_bare_urls(self, gate3_state: dict):
        """No bare URLs should appear."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            SYNTHETIC_CANDIDATE_OUTPUT, flags, instructions, _rules()
        )
        bare = next(r for r in results if r.rule == "no_bare_urls")
        assert bare.ok, f"No bare URLs: {bare.reason}"

    def test_cost_mechanics_pending(self, gate3_state: dict):
        """Cost-bearing mechanics should be pending financial validation."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            SYNTHETIC_CANDIDATE_OUTPUT, flags, instructions, _rules()
        )
        cost = next(r for r in results if r.rule == "cost_mechanics_pending_financial_validation")
        assert cost.ok, f"Cost mechanics pending: {cost.reason}"

    def test_no_guaranteed_targets_without_baseline(self, gate3_state: dict):
        """No guaranteed numeric targets without baseline."""
        context = _build_context_from_state(gate3_state)
        instructions = _build_instructions_from_state(gate3_state)
        flags = extract_context_flags(context)
        results = audit_campaign_output(
            SYNTHETIC_CANDIDATE_OUTPUT, flags, instructions, _rules()
        )
        target = next(r for r in results if r.rule == "no_guaranteed_numeric_targets_without_baseline")
        assert target.ok, f"No guaranteed targets: {target.reason}"

    def test_cited_competitor_price_in_evidence(self, gate3_state: dict):
        """The cited competitor price (฿3,999) must actually appear in the
        preserved evidence snippet for the cited URL."""
        from src.campaign_validator import _evidence_contains_claim, _normalize_url

        relevant = gate3_state.get("relevant_annotations", [])
        evidence_snippets = {}
        for a in relevant:
            url = _normalize_url(a.get("url", ""))
            content = a.get("content", "")
            if url and content:
                evidence_snippets[url] = content

        # The price line cites TG Fone with ฿3,999
        price_line = (
            "* **ราคาขายปลีกแนะนำ (Retail Price - Indicative Estimate):** "
            "2,990 – 3,490 บาท *(Pending financial validation)* "
            "วางตำแหน่งต่ำกว่า [imoo Z1 ฿3,999 จากร้าน TG Fone]"
            "(https://www.tgfone.com/product/detail/3554/imoo-Watch-Phone-Z1-Grapefruit-Red) "
            "COGS และ margin ยังไม่ทราบ ค่าธรรมเนียมช่องทางยังไม่ทราบ"
        )
        cited_urls = [_normalize_url("https://www.tgfone.com/product/detail/3554/imoo-Watch-Phone-Z1-Grapefruit-Red")]
        contains = _evidence_contains_claim(price_line, cited_urls, evidence_snippets)
        assert contains, "Cited ฿3,999 must appear in TG Fone evidence snippet"

    def test_out_of_stock_listing_qualified(self):
        """The synthetic output qualifies the Thisshop listing as
        'Inventory shortage' rather than treating it as unquestionably current."""
        assert "Inventory shortage" in SYNTHETIC_CANDIDATE_OUTPUT
        assert "อาจไม่พร้อมจัดส่ง ณ ปัจจุบัน" in SYNTHETIC_CANDIDATE_OUTPUT
