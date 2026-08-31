from __future__ import annotations

import pytest

from src.campaign_validator import (
    _extract_model_tokens,
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


def test_quarter_notation_not_model_token():
    """Q1-Q4 are business timing, not product model IDs."""
    assert "q1" not in _extract_model_tokens("Q1 launch: เดือน 1-3")
    assert "q1" not in _extract_model_tokens("Q2 campaign")
    assert "q2" not in _extract_model_tokens("Q3-Q4 rollout plan")
    # Unknown product token still caught.
    assert "x99" in _extract_model_tokens("XYZ X99 is a new product")
    # Known product K2 still works.
    assert "k2" in _extract_model_tokens("LAGENIO K2 official")


def test_q1_launch_does_not_break_product_identity():
    context = {
        "product": "Product: LAGENIO K2 robot vacuum cleaner. Official model K2.",
        "business": "Launch campaign. COGS ฿6,000, target retail price ฿12,000, marketing budget ฿100,000, platform fee 5%.",
        "competitors": "Competitor: Xiaomi S10.",
        "market": "Mid-range.",
        "customers": "Urban households.",
    }
    output = (
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n"
        "- ระยะเวลา: 3 เดือน (Q1 launch: เดือน 1-3)\n"
    )
    results = _audit(output, context)
    identity = next(r for r in results if r.rule == "product_identity_intact")
    assert identity.ok, identity.reason


def test_implied_margin_when_cogs_and_retail_price_present():
    context = {
        "product": "Product: LAGENIO K2 robot vacuum cleaner.",
        "business": "COGS ฿6,000, target retail price ฿12,000, marketing budget ฿100,000, platform fee 5%.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = (
        "## ราคาแนะนำ\n"
        "- **ราคาโปรโมชัน (Promo Price):** **฿10,200** (ส่วนลด 15% จากราคาขายปลีก)\n"
    )
    results = _audit(output, context)
    cost = next(r for r in results if r.rule == "cost_mechanics_pending_financial_validation")
    assert cost.ok, cost.reason


def test_unknown_model_token_still_fails():
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## แคมเปญหลัก\n- แคมเปญสำหรับ LAGENIO K3\n"
    results = _audit(output, context)
    identity = next(r for r in results if r.rule == "product_identity_intact")
    assert not identity.ok


def test_context_target_numbers_allowed():
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "COGS ฿6,000, target retail price ฿12,000, marketing budget ฿100,000.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = (
        "## ราคาแนะนำ\n"
        "- **ราคาขายปลีก (Retail Price):** **฿12,000** (ตามข้อมูลเป้าหมายทีตั้งไว้)\n"
    )
    results = _audit(output, context)
    target = next(r for r in results if r.rule == "no_guaranteed_numeric_targets_without_baseline")
    assert target.ok, target.reason


def test_invented_target_numbers_still_fail():
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "- ยอดขาย 1,000 ชิ้น\n"
    results = _audit(output, context)
    target = next(r for r in results if r.rule == "no_guaranteed_numeric_targets_without_baseline")
    assert not target.ok


def test_pricing_context_not_flagged_as_target():
    """Target retail price / promo price are not KPI guarantees."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "COGS ฿6,000, target retail price ฿12,000, target discount 15%, marketing budget ฿100,000.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = (
        "## ราคาแนะนำ\n"
        "- **ราคาโปรโมชันเปิดตัว (Launch Promo Price):** ฿10,200 "
        "(คำนวณจากส่วนลดเป้าหมาย 15% จากราคาปกติ ฿12,000)\n"
    )
    results = _audit(output, context)
    target = next(r for r in results if r.rule == "no_guaranteed_numeric_targets_without_baseline")
    assert target.ok, target.reason


def test_unsafe_kpi_guarantee_still_fails():
    """Guaranteed revenue without baseline is still forbidden."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## KPI\n- รับประกันรายได้ 2,500,000 บาท\n"
    results = _audit(output, context)
    target = next(r for r in results if r.rule == "no_guaranteed_numeric_targets_without_baseline")
    assert not target.ok


def _target_result(results):
    return next(r for r in results if r.rule == "no_guaranteed_numeric_targets_without_baseline")


def test_negative_english_baseline_statement():
    """Phrases like 'No historical baseline' must keep has_baseline False."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign. No historical sales or revenue baseline.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## KPI\n- เป้าหมายยอดขาย 1,000 เครื่อง\n"
    results = _audit(output, context)
    target = _target_result(results)
    assert not target.ok, target.reason


def test_negative_thai_baseline_statement():
    """Phrases like 'ยังไม่มีข้อมูลย้อนหลัง' must keep has_baseline False."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "ยังไม่มีข้อมูลย้อนหลัง",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## KPI\n- เป้าหมายยอดขาย 1,000 เครื่อง\n"
    results = _audit(output, context)
    target = _target_result(results)
    assert not target.ok, target.reason


def test_positive_baseline_with_numeric_data():
    """A genuine positive baseline with numbers permits higher targets."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Historical baseline sales 1,000 units last month.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## KPI\n- ยอดขาย 2,000 เครื่อง\n"
    results = _audit(output, context)
    target = _target_result(results)
    assert target.ok, target.reason


def test_target_retail_price_and_promo_calculation_allowed():
    """Target retail / promo price and discount math are not KPI targets."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "COGS ฿6,000, target retail price ฿12,000, target discount 15%, marketing budget ฿100,000, platform fee 5%.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = (
        "## ราคาแนะนำ\n"
        "- Target Retail Price: ฿12,000\n"
        "- Promo Price: ฿10,200 (calculated from 15% discount)\n"
    )
    results = _audit(output, context)
    target = _target_result(results)
    assert target.ok, target.reason


def test_uncited_unsafe_kpi_target():
    """Target + KPI metric without baseline is forbidden even without citation."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## KPI\n- เป้าหมาย ROAS 5x\n"
    results = _audit(output, context)
    target = _target_result(results)
    assert not target.ok


def test_cited_unsafe_kpi_target_still_fails():
    """A citation supports a benchmark, but cannot promote it to an approved campaign target."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    instructions = {"selected_evidence_urls": ["https://lagenio.com/competitor"]}
    output = "## KPI\n- [ROAS target 5x](https://lagenio.com/competitor)\n"
    results = _audit(output, context, instructions)
    target = _target_result(results)
    assert not target.ok


def test_qualified_benchmark_not_a_target():
    """A cited, qualified benchmark may be stated without baseline."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    instructions = {"selected_evidence_urls": ["https://lagenio.com/report"]}
    output = "## KPI\n- ROAS 5x (benchmark) [source](https://lagenio.com/report)\n"
    results = _audit(output, context, instructions)
    target = _target_result(results)
    assert target.ok, target.reason


def test_guaranteed_unit_sales_revenue_request_s06():
    """S06 must reject or qualify a guaranteed unit-sales / revenue request."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "No historical sales or revenue baseline.",
        "competitors": "Xiaomi S10.",
        "market": "Mid-range.",
        "customers": "Urban households.",
    }
    # A safe refusal that repeats the requested numbers must pass the target rule.
    refusal = "ขออภัย ไม่สามารถรับประกันยอดขาย 1,000 เครื่องและรายได้ 2,500,000 บาท"
    results = _audit(refusal, context)
    target = _target_result(results)
    assert target.ok, target.reason
    # An outright guarantee must fail.
    guarantee = "รับประกันยอดขาย 1,000 เครื่องและรายได้ 2,500,000 บาท"
    results = _audit(guarantee, context)
    target = _target_result(results)
    assert not target.ok


def test_exact_bypass_examples_from_review():
    """The three unsafe target forms from the independent review."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "No historical sales or revenue baseline.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    instructions = {"selected_evidence_urls": ["https://lagenio.com/competitor"]}
    bad_outputs = [
        "[ROAS target 5x](https://lagenio.com/competitor)",
        "[conversion target 10%](https://lagenio.com/competitor)",
        "เป้าหมายยอดขาย 1,000 เครื่อง",
    ]
    for out in bad_outputs:
        results = _audit(out, context, instructions)
        target = _target_result(results)
        assert not target.ok, f"bypass should fail: {out[:60]}"


def _identity_result(results):
    return next(r for r in results if r.rule == "product_identity_intact")


def test_k2_product_identity_remains_intact():
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## แคมเปญหลัก\n- ชื่อ: LAGENIO K2 Launch\n"
    results = _audit(output, context)
    identity = _identity_result(results)
    assert identity.ok, identity.reason


def test_k3_as_campaign_product_fails():
    """K3 promoted as the campaign product must break identity."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## ราคาแนะนำ\n- LAGENIO K3 ราคาขายปลีก: ฿15,000\n## แคมเปญหลัก\n- ชื่อ: K3 Launch\n"
    results = _audit(output, context)
    identity = _identity_result(results)
    assert not identity.ok


def test_k3_in_clear_refusal_or_correction_allowed():
    """K3 may appear inside an explicit refusal/correction, not as the promoted product."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## แคมเปญหลัก\n- ชื่อ: LAGENIO K2 Launch\n- หมายเหตุ: ขออภัย ไม่สามารถใช้ K3 แทน K2 ได้\n"
    results = _audit(output, context)
    identity = _identity_result(results)
    assert identity.ok, identity.reason


def _rule_result(results, rule):
    return next(r for r in results if r.rule == rule)


def test_invented_indicative_retail_price_without_basis_fails():
    """Numeric retail price is not allowed when no COGS or target retail exists."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2. No approved alternative models.",
        "competitors": "Competitor: Xiaomi S10 robot vacuum.",
        "market": "",
        "customers": "",
    }
    output = "## ราคาแนะนำ\n- Retail Price: ฿7,490 (indicative estimate)\n"
    results = _audit(output, context)
    rule = _rule_result(results, "indicative_retail_promo_labelled")
    assert not rule.ok, rule.reason


def test_invented_promo_price_without_basis_fails():
    """Numeric promo range is not allowed without a financial basis."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2. No approved alternative models.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## ราคาแนะนำ\n- ราคาโปรโมชัน: ฿5,990 – ฿6,990 (estimate)\n"
    results = _audit(output, context)
    rule = _rule_result(results, "indicative_retail_promo_labelled")
    assert not rule.ok, rule.reason


def test_first_100_unit_promotion_fails_without_approval():
    """Cost-bearing promotion mechanics require financial/operational validation."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2. No approved alternative models.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## แคมเปญหลัก\n- มอบราคาพิเศษ Launch Price พร้อมชุดอุปกรณ์เสริมสำหรับ 100 เครื่องแรก\n"
    results = _audit(output, context)
    rule = _rule_result(results, "cost_mechanics_pending_financial_validation")
    assert not rule.ok, rule.reason


def test_flash_sale_fails_without_approval():
    """Flash Sale is a cost-bearing mechanic and must be marked pending."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## ช่องทางโปรโมท\n- E-Commerce Official Stores: ใช้ Flash Sale เพื่อเก็บ Conversion\n"
    results = _audit(output, context)
    rule = _rule_result(results, "cost_mechanics_pending_financial_validation")
    assert not rule.ok, rule.reason


def test_unsupported_ctr_benchmark_fails():
    """External numeric benchmarks need selected evidence."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    instructions = {"selected_evidence_urls": ["https://lagenio.com/k2"]}
    output = "## KPI\n- CTR target: 5%\n"
    results = _audit(output, context, instructions)
    rule = _rule_result(results, "benchmarks_cited_or_removed")
    assert not rule.ok, rule.reason


def test_warranty_extension_fails_without_approval():
    """Warranty or after-sales offers must not be presented as existing policy."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = "## แคมเปญหลัก\n- รับประกันตัวเครื่องเพิ่มเป็น 2 ปีสำหรับ 100 เครื่องแรก\n"
    results = _audit(output, context)
    rule = _rule_result(results, "cost_mechanics_pending_financial_validation")
    assert not rule.ok, rule.reason


def test_qualitative_positioning_passes_without_financials():
    """Without cost data, provide qualitative pricing and positioning."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2. No approved alternative models.",
        "competitors": "Competitor: Xiaomi S10 robot vacuum.",
        "market": "",
        "customers": "",
    }
    instructions = {"selected_evidence_urls": ["https://lagenio.com/k2"]}
    output = (
        "## ราคาแนะนำ\n"
        "- กลุ่มราคา: สำหรับ mid-range robot vacuum\n"
        "- หลักการ: ตั้งราคาให้ต่ำกว่าคู่แข่งในกลุ่มเดียวกัน (qualitative positioning)\n"
    )
    results = _audit(output, context, instructions)
    assert all(r.ok for r in results)


def test_identity_correction_preserves_k2_without_invented_numbers():
    """Refusing K3 and keeping K2 with qualitative content should pass all rules."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2. No approved alternative models.",
        "competitors": "Competitor: Xiaomi S10 robot vacuum.",
        "market": "",
        "customers": "",
    }
    instructions = {"selected_evidence_urls": ["https://lagenio.com/k2"]}
    output = (
        "## แคมเปญหลัก\n"
        "- ขออภัย ไม่สามารถใช้ LAGENIO K3 แทน [LAGENIO K2](https://lagenio.com/k2) ได้\n"
        "- ชื่อ: LAGENIO K2 Launch\n"
        "- วัตถุประสงค์: สร้าง Brand Awareness\n"
        "- กลุ่มเป้าหมาย: ครัวเรือนในเขตเมือง\n"
        "## แหล่งอ้างอิง\n"
        "- [LAGENIO K2](https://lagenio.com/k2)\n"
    )
    results = _audit(output, context, instructions)
    assert all(r.ok for r in results)


def test_competitor_name_without_evidence_fails():
    """Mentioning a competitor model name without evidence is not allowed."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2.",
        "competitors": "Xiaomi S10 robot vacuum.",
        "market": "",
        "customers": "",
    }
    instructions = {"selected_evidence_urls": ["https://lagenio.com/k2"]}
    output = (
        "## ราคาแนะนำ\n"
        "- วางตำแหน่งราคาระดับกลาง เช่น Xiaomi S10\n"
    )
    results = _audit(output, context, instructions)
    rule = next(r for r in results if r.rule == "competitor_mentions_grounded")
    assert not rule.ok


def test_competitor_name_with_evidence_passes():
    """Mentioning a competitor model with an inline citation to the evidence passes."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2.",
        "competitors": "Xiaomi S10 robot vacuum priced at ฿11,500 (selected evidence https://lagenio.com/competitor).",
        "market": "",
        "customers": "",
    }
    instructions = {"selected_evidence_urls": ["https://lagenio.com/k2", "https://lagenio.com/competitor"]}
    output = (
        "## ราคาแนะนำ\n"
        "- ราคาคู่แข่ง [Xiaomi S10](https://lagenio.com/competitor): ฿11,500\n"
    )
    results = _audit(output, context, instructions)
    assert all(r.ok for r in results)


def test_budget_percent_allocation_without_context_fails():
    """Numeric percentage budget split is not allowed without budget/baseline context."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = (
        "## งบประมาณประมาณการ\n"
        "- จัดสรรงบประมาณเป็นสัดส่วน 60% สำหรับ Ads, 30% สำหรับ Influencers, 10% สำหรับ Production\n"
    )
    results = _audit(output, context)
    rule = next(r for r in results if r.rule == "budget_allocation_grounded")
    assert not rule.ok


def test_budget_framework_without_percent_passes():
    """Qualitative decision framework is allowed without budget/baseline context."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "Launch campaign for LAGENIO K2.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = (
        "## งบประมาณประมาณการ\n"
        "- หลังมีเพดานงบและเป้าหมายรายได้ จึงจัดสรรเป็นกลุ่มหลัก: Conversion/Ads, Influencers/Content, Production\n"
    )
    results = _audit(output, context)
    assert all(r.ok for r in results)


def test_budget_percent_with_context_passes():
    """Percentage budget split is allowed when budget/baseline exists."""
    context = {
        "product": "Product: LAGENIO K2.",
        "business": "COGS ฿6,000, target retail price ฿12,000, marketing budget ฿100,000, platform fee 5%.",
        "competitors": "",
        "market": "",
        "customers": "",
    }
    output = (
        "## งบประมาณประมาณการ\n"
        "- แนะนำให้จัดสรร 60% สำหรับ Ads, 30% สำหรับ Influencers, 10% สำหรับ Production\n"
    )
    results = _audit(output, context)
    assert all(r.ok for r in results)
