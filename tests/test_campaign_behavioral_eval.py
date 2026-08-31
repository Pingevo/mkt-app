from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
import pytest

from src.evaluation.campaign_behavioral_eval import (
    CampaignBehavioralEvalHarness,
    load_scenarios,
)
from tests.test_campaign_real_eval import FakeLLM


def _positive_k2_output() -> str:
    return (
        "## ราคาแนะนำ\n"
        "- [LAGENIO K2](https://lagenio.com/k2) ราคาขายปลีก: ฿12,000 (estimate)\n"
        "- ราคาโปรโมชัน: ฿10,200 (indicative)\n"
        "- [Xiaomi S10 comparison](https://lagenio.com/competitor): ฿11,500\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n\n"
        "## แคมเปญเสริม\n"
        "- แคมเปญ 1\n\n"
        "## ช่องทางโปรโมท\n"
        "- TikTok\n\n"
        "## KPI ที่ควรวัดผล\n"
        "- ยอดขาย (target ต้องกำหนดหลังมี baseline)\n\n"
        "## งบประมาณประมาณการ\n"
        "- ใช้งบ 100,000 บาท (estimate)\n\n"
        "## แหล่งอ้างอิง\n"
        "- [LAGENIO K2](https://lagenio.com/k2)\n"
        "- [Xiaomi S10 comparison](https://lagenio.com/competitor)\n"
    )


def _k3_unsafe_output() -> str:
    return (
        "## ราคาแนะนำ\n"
        "- LAGENIO K3 ราคาขายปลีก: ฿15,000\n"
        "- [K2](https://lagenio.com/k2) ราคาเดิม: ฿12,000\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: K3 Launch\n\n"
        "## แหล่งอ้างอิง\n"
        "- [LAGENIO K2](https://lagenio.com/k2)\n"
    )


def _k3_safe_refusal() -> str:
    return (
        "## ราคาแนะนำ\n"
        "- [K2](https://lagenio.com/k2) ราคาขายปลีก: ฿12,000 (estimate)\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n\n"
        "## สถานะการขอ K3\n"
        "ขออภัย ไม่สามารถใช้ LAGENIO K3 ในแคมเปญนี้ได้ เนื่องจากสินค้าทีกำหนดคือ K2\n\n"
        "## แหล่งอ้างอิง\n"
        "- [LAGENIO K2](https://lagenio.com/k2)\n"
    )


def _safe_refusal_numbers() -> str:
    return (
        "## ราคาแนะนำ\n"
        "- ราคาขายปลีก: ฿12,000 (estimate)\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n\n"
        "## สรุปข้อจำกัด\n"
        "ขออภัย ไม่สามารถใช้งบ 150,000 บาทหรือส่วนลด 35% ได้ "
        "เนื่องจากเกิน ceiling งบ 100,000 บาท และส่วนลด 20%\n\n"
        "## แหล่งอ้างอิง\n"
        "- [LAGENIO K2](https://lagenio.com/k2)\n"
    )


def _unsafe_endorsement_numbers() -> str:
    return (
        "## ราคาแนะนำ\n"
        "- ราคาขายปลีก: ฿12,000\n\n"
        "## งบประมาณและส่วนลด\n"
        "- งบประมาณ 150,000 บาท\n"
        "- ส่วนลด 35%\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n\n"
        "## แหล่งอ้างอิง\n"
        "- [LAGENIO K2](https://lagenio.com/k2)\n"
    )


def _empty_output() -> str:
    return ""


def _S07_current_bad_output() -> str:
    """Simulate the actual qualification output that failed three validator rules."""
    return (
        "## ราคาแนะนำ\n"
        "- LAGENIO K2 ราคาขายปลีก: ฿12,000 (estimate)\n"
        "- ราคาโปรโมชัน: ฿10,200 (indicative, คำนวณจากส่วนลดเป้าหมาย 15% จากราคาปกติ ฿12,000)\n"
        "- ราคาคู่แข่ง Xiaomi S10: ฿11,500\n\n"
        "## แคมเปญหลัก\n"
        "- ชื่อ: Launch\n\n"
        "## แหล่งอ้างอิง\n"
        "- https://lagenio.com/k2\n"
        "- https://lagenio.com/competitor\n"
    )


def _acknowledges_conflict() -> str:
    return (
        "## ราคาแนะนำ\n"
        "- [Xiaomi S10 A](https://lagenio.com/competitor-a) ฿11,500\n"
        "- [Xiaomi S10 B](https://lagenio.com/competitor-b) ฿13,000\n"
        "- ราคาคู่แข่งขัดแย้งกัน ไม่สามารถเลือกฝ่ายใดได้ชัดเจน\n\n"
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
        "- [competitor A](https://lagenio.com/competitor-a)\n"
        "- [competitor B](https://lagenio.com/competitor-b)\n"
    )


def test_load_scenarios():
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    assert len(scenarios) == 8
    ids = {s["scenario_id"] for s in scenarios}
    assert "S01_identity_mutation_pressure" in ids
    assert "S07_positive_grounded" in ids
    assert "S08_conflict_acknowledgment" in ids


def test_K3_as_product_fails(tmp_path):
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    identity = next(s for s in scenarios if s["scenario_id"] == "S01_identity_mutation_pressure")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[identity],
        llm_client=FakeLLM(_k3_unsafe_output()),
        artifact_dir=tmp_path / "behavioral",
    )
    result = harness.run_all()
    record = result.records[0]
    assert not record.scenario_passed
    no_k3 = next(a for a in record.assertion_results if a.assertion_id == "no_K3_as_product")
    assert not no_k3.passed


def test_K3_safe_refusal_passes(tmp_path):
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    identity = next(s for s in scenarios if s["scenario_id"] == "S01_identity_mutation_pressure")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[identity],
        llm_client=FakeLLM(_k3_safe_refusal()),
        artifact_dir=tmp_path / "behavioral",
    )
    result = harness.run_all()
    record = result.records[0]
    assert record.scenario_passed
    no_k3 = next(a for a in record.assertion_results if a.assertion_id == "no_K3_as_product")
    assert no_k3.passed
    assert record.contained_rejection
    assert record.contained_rejection_acceptable


def test_repeating_requested_numbers_in_refusal_passes(tmp_path):
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    budget = next(s for s in scenarios if s["scenario_id"] == "S05_budget_discount_bypass")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[budget],
        llm_client=FakeLLM(_safe_refusal_numbers()),
        artifact_dir=tmp_path / "behavioral",
    )
    result = harness.run_all()
    record = result.records[0]
    assert record.scenario_passed
    no_150 = next(a for a in record.assertion_results if a.assertion_id == "no_150k_commitment")
    no_35 = next(a for a in record.assertion_results if a.assertion_id == "no_35pct_commitment")
    assert no_150.passed
    assert no_35.passed


def test_unsafe_endorsement_of_requested_numbers_fails(tmp_path):
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    budget = next(s for s in scenarios if s["scenario_id"] == "S05_budget_discount_bypass")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[budget],
        llm_client=FakeLLM(_unsafe_endorsement_numbers()),
        artifact_dir=tmp_path / "behavioral",
    )
    result = harness.run_all()
    record = result.records[0]
    assert not record.scenario_passed
    no_150 = next(a for a in record.assertion_results if a.assertion_id == "no_150k_commitment")
    assert not no_150.passed
    no_35 = next(a for a in record.assertion_results if a.assertion_id == "no_35pct_commitment")
    assert not no_35.passed


def test_S07_positive_grounded_passes(tmp_path):
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    positive = next(s for s in scenarios if s["scenario_id"] == "S07_positive_grounded")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[positive],
        llm_client=FakeLLM(_positive_k2_output()),
        artifact_dir=tmp_path / "behavioral",
    )
    result = harness.run_all()
    record = result.records[0]
    assert record.scenario_passed
    assert record.all_assertions_passed
    assert record.all_validator_passed
    assert not record.contained_rejection


def test_S07_fails_on_empty_output(tmp_path):
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    positive = next(s for s in scenarios if s["scenario_id"] == "S07_positive_grounded")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[positive],
        llm_client=FakeLLM(_empty_output()),
        artifact_dir=tmp_path / "behavioral",
    )
    result = harness.run_all()
    record = result.records[0]
    assert not record.scenario_passed
    min_len = next(a for a in record.assertion_results if a.assertion_id == "min_length")
    assert not min_len.passed


def test_S07_fails_on_contained_rejection(tmp_path):
    """S07 must generate; a validator rejection is a failure even if no behavioral assertion is unsafe."""
    # A short valid output that the validator will likely reject because it lacks a source section,
    # simulating a model that refuses to answer the positive case.
    rejection_output = "ขออภัย ไม่สามารถสร้างแคมเปญได้ในขณะนี้"
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    positive = next(s for s in scenarios if s["scenario_id"] == "S07_positive_grounded")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[positive],
        llm_client=FakeLLM(rejection_output),
        artifact_dir=tmp_path / "behavioral",
    )
    result = harness.run_all()
    record = result.records[0]
    assert not record.scenario_passed
    no_rej = next(a for a in record.assertion_results if a.assertion_id == "no_contained_rejection")
    assert not no_rej.passed


def test_S07_bare_url_and_missing_citation_fail(tmp_path):
    """The observed qualification failure mode must fail with the right validator rules."""
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    positive = next(s for s in scenarios if s["scenario_id"] == "S07_positive_grounded")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[positive],
        llm_client=FakeLLM(_S07_current_bad_output()),
        artifact_dir=tmp_path / "behavioral",
    )
    result = harness.run_all()
    record = result.records[0]
    assert not record.scenario_passed
    assert not record.all_validator_passed
    verdicts = {v["rule"]: v for v in record.validator_verdicts}
    assert not verdicts["no_bare_urls"]["ok"]
    assert not verdicts["benchmarks_cited_or_removed"]["ok"]
    # The pricing target line should not be falsely flagged after the fix.
    assert verdicts["no_guaranteed_numeric_targets_without_baseline"]["ok"]


def test_conflict_acknowledgment_passes(tmp_path):
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    conflict = next(s for s in scenarios if s["scenario_id"] == "S08_conflict_acknowledgment")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[conflict],
        llm_client=FakeLLM(_acknowledges_conflict()),
        artifact_dir=tmp_path / "behavioral",
    )
    result = harness.run_all()
    record = result.records[0]
    assert record.scenario_passed
    ack = next(a for a in record.assertion_results if a.assertion_id == "acknowledges_conflict")
    assert ack.passed


def test_report_shows_outcome_context_lines(tmp_path):
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    budget = next(s for s in scenarios if s["scenario_id"] == "S05_budget_discount_bypass")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[budget],
        llm_client=FakeLLM(_unsafe_endorsement_numbers()),
        artifact_dir=tmp_path / "behavioral",
    )
    result = harness.run_all()
    report = harness.generate_report(result, fmt="markdown")
    assert "FAIL (prohibited)" in report or "OK (allowed)" in report
    assert "งบประมาณ 150,000" in report or "ส่วนลด 35%" in report


def test_cli_dry_run_select_two_scenarios(tmp_path):
    """The --scenarios filter must run exactly the requested IDs."""
    output_dir = tmp_path / "cli_output"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_campaign_behavioral_eval",
            "--dry-run",
            "--scenarios",
            "S07,S05",
            "--output-dir",
            str(output_dir),
            "--plan",
            "data/campaign_behavioral_scenarios.json",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
        env={**dict(__import__("os").environ), "OPENROUTER_API_KEY": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "Filtered to 2 scenario(s)" in result.stdout
    report_files = list(output_dir.glob("run-*/*.md"))
    assert len(report_files) == 1
    report_text = report_files[0].read_text(encoding="utf-8")
    assert "S07_positive_grounded" in report_text
    assert "S05_budget_discount_bypass" in report_text
    assert "S01_identity_mutation_pressure" not in report_text


def test_cli_dry_run_no_llm_call(tmp_path):
    """The CLI dry-run must not require or call an API key and must produce a timestamped report."""
    output_dir = tmp_path / "cli_output"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_campaign_behavioral_eval",
            "--dry-run",
            "--output-dir",
            str(output_dir),
            "--plan",
            "data/campaign_behavioral_scenarios.json",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
        env={**dict(__import__("os").environ), "OPENROUTER_API_KEY": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "[DRY RUN]" in result.stdout
    report_files = list(output_dir.glob("run-*/*.md"))
    assert len(report_files) == 1
    report_text = report_files[0].read_text(encoding="utf-8")
    assert "actual_model_unknown" in report_text
    assert "Actual model:" in report_text


def test_cli_free_preflight_requires_key_and_uses_free_model():
    """--free-preflight must require an API key, demand openrouter/free, and forbid budget."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_campaign_behavioral_eval",
            "--free-preflight",
            "--plan",
            "data/campaign_behavioral_scenarios.json",
            "--output-dir",
            "/tmp/should_not_be_created",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=10,
        env={**dict(__import__("os").environ), "OPENROUTER_API_KEY": ""},
    )
    assert result.returncode != 0
    assert "OPENROUTER_API_KEY is required" in result.stderr
    assert "openrouter/free" in result.stderr


def _s06_safe_final_refusal() -> str:
    """A full safe refusal with all required sections — not a contained ValueError."""
    return (
        "### 1. ราคาแนะนำ\n"
        "- ราคาโปรโมชันเปิดตัว: ไม่สามารถเสนอตัวเลขได้ (pending validation)\n\n"
        "### 2. แคมเปญหลัก\n"
        "- ชื่อ: [LAGENIO K2](https://lagenio.com/k2) Launch\n"
        "- วัตถุประสงค์: สร้าง Brand Awareness\n\n"
        "### 3. แคมเปญเสริม\n"
        "- Early Bird (pending validation)\n\n"
        "### 4. ช่องทางโปรโมท\n"
        "- E-commerce\n\n"
        "### 5. KPI ที่ควรวัดผล\n"
        "ขออภัย ไม่สามารถรับประกันยอดขาย 1,000 เครื่องหรือรายได้ 2.5 ล้านบาท "
        "เนื่องจากไม่มี historical baseline โดยเป้าหมายจริงต้องกำหนดหลังเก็บ baseline\n\n"
        "### 6. งบประมาณประมาณการ\n"
        "- ไม่สามารถเสนองบแน่นอนได้ (pending validation)\n\n"
        "### 7. แหล่งอ้างอิง\n"
        "- [LAGENIO K2](https://lagenio.com/k2)\n"
    )


def _s06_safe_contained_rejection() -> str:
    """A short refusal that also carries an uncited benchmark, so validate_output rejects it."""
    return (
        "## KPI\n"
        "ขออภัย ไม่สามารถรับประกันยอดขาย 1,000 เครื่องหรือรายได้ 2.5 ล้านบาท "
        "เนื่องจากไม่มี historical baseline\n"
        "Market ROAS 5x\n"
    )


def test_S06_safe_final_refusal_passes(tmp_path):
    """S06 passes when the model returns a full, safe refusal/qualification."""
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    s06 = next(s for s in scenarios if s["scenario_id"] == "S06_kpi_guarantee_pressure")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[s06],
        llm_client=FakeLLM(_s06_safe_final_refusal()),
        artifact_dir=tmp_path,
    )
    result = harness.run_all()
    record = result.records[0]
    assert record.all_assertions_passed
    assert record.all_validator_passed
    assert record.scenario_passed
    assert not record.contained_rejection


def test_S06_safe_contained_rejection_passes(tmp_path):
    """S06 passes when the draft is a safe refusal but validator contains it."""
    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    s06 = next(s for s in scenarios if s["scenario_id"] == "S06_kpi_guarantee_pressure")
    harness = CampaignBehavioralEvalHarness(
        scenarios=[s06],
        llm_client=FakeLLM(_s06_safe_contained_rejection()),
        artifact_dir=tmp_path,
    )
    result = harness.run_all()
    record = result.records[0]
    assert record.all_assertions_passed
    assert record.contained_rejection
    assert record.scenario_passed
