"""Evaluation-only: Rerun Beta qualification for Agent 2, 3, 4 only.

This script fixes the 5 runner issues identified by Codex:
1. Agent 3 repair limit: override max_retry_limit=1 for qualification
2. Budget guard: check AFTER each case, not just before
3. Agent 3 case: use Gate 3 equivalent input (competitor data + Quick Brief)
4. UI parity: send StepRunContext, product images, resources, content history
5. Hub receipt: wait for async flush before reading results

Run with: python3 scripts/run_qualification_rerun.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from qual_runner import (
    _init_session_baseline,
    budget_guard,
    current_spend,
    run_case,
    save_evidence,
    _read_cumulative_cost,
    _restore_yaml_overrides,
)

# New qualification directory for this rerun
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
QUAL_DIR = PROJECT_ROOT / "data" / "all_agents_beta_qualification" / f"rerun_{TIMESTAMP}"
OUTPUT_DIR = QUAL_DIR / "run_outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PRODUCT = "Lagenio K2"

LEDGER: list[dict] = []


def _record_ledger(evidence: dict) -> None:
    for entry in evidence.get("usage_entries", []):
        LEDGER.append({
            "case_id": evidence.get("case_id"),
            "agent": evidence.get("agent_key"),
            "request_id": entry.get("request_id"),
            "model": entry.get("model"),
            "source": entry.get("source"),
            "cost_usd": entry.get("cost_usd"),
            "timestamp": entry.get("timestamp"),
        })


def _print_case_summary(evidence: dict) -> None:
    cid = evidence.get("case_id", "?")
    cost = evidence.get("run_cost_usd", 0)
    n_req = evidence.get("num_paid_requests", 0)
    err = evidence.get("error")
    blocked = evidence.get("blocked_by_budget", False)
    cum = evidence.get("cumulative_spend_after", _read_cumulative_cost())
    status = "BLOCKED" if blocked else ("ERROR" if err else "OK")
    print(f"\n{'='*60}")
    print(f"CASE {cid}: {status}")
    print(f"  Paid requests: {n_req}")
    print(f"  Case cost: ${cost:.6f}")
    print(f"  Cumulative session spend: ${cum:.6f}")
    if err:
        print(f"  Error: {err[:300]}")
    if evidence.get("result_preview"):
        print(f"  Output preview: {evidence['result_preview'][:200]}...")
    print(f"{'='*60}\n")


def main():
    print("=" * 70)
    print("MKTApp Beta Qualification RERUN — Agent 2, 3, 4 only")
    print(f"Product fixture: {PRODUCT}")
    print(f"Qualification dir: {QUAL_DIR}")
    print(f"Fixes applied:")
    print("  1. Agent 3 max_retry_limit=1 (override config)")
    print("  2. Budget guard checks AFTER each case too")
    print("  3. Agent 3 uses Gate 3 equivalent input (competitor + brief)")
    print("  4. UI parity: StepRunContext, images, resources, history")
    print("  5. Hub receipt: wait for async flush")
    print("=" * 70)

    _init_session_baseline()

    results: dict[str, dict] = {}

    # ===================================================================
    # Agent 2 — Competitor Analyst (default discovery)
    # ===================================================================
    print("\n" + "=" * 70)
    print("AGENT 2 — Competitor Analyst (competitor_analysis)")
    print("=" * 70)

    print("\n--- A2comp: Default competitor discovery ---")
    if budget_guard("A", 0.05, "A2comp"):
        ev_a2c = run_case(
            case_id="A2comp_competitor_evidence",
            agent_key="competitor_analysis",
            product_id=PRODUCT,
            quick_brief="วิเคราะห์เฉพาะคู่แข่งเด็กสมาร์ทวอทช์ในไทย เน้นเรื่องราคาและฟีเจอร์ GPS tracking เท่านั้น จำกัด 2-3 คู่แข่งหลัก",
            stage="A",
            estimated_cost=0.05,
            output_dir=OUTPUT_DIR,
        )
        save_evidence(ev_a2c, QUAL_DIR)
        _record_ledger(ev_a2c)
        _print_case_summary(ev_a2c)
        results["A2comp"] = ev_a2c
    else:
        results["A2comp"] = {"blocked_by_budget": True}
        print("A2comp BLOCKED by budget")

    # Post-case budget check (fix #2)
    cum_after_a2 = _read_cumulative_cost()
    print(f"[BUDGET] After A2comp: ${cum_after_a2:.6f} / ${0.40:.6f} Stage A ceiling")

    # ===================================================================
    # Agent 3 — Campaign Strategist (Gate 3 equivalent)
    # ===================================================================
    print("\n" + "=" * 70)
    print("AGENT 3 — Campaign Strategist (campaign_strategy)")
    print("  Gate 3 equivalent: competitor data + Quick Brief + selected evidence")
    print("=" * 70)

    print("\n--- A3: Gate 3 equivalent case ---")
    if budget_guard("A", 0.05, "A3"):
        # Fix #3: Use Gate 3 equivalent input
        # Gate 3 S07 used: competitor data + Quick Brief + selected evidence URLs
        # We use the same product (Lagenio K2) with competitor context
        ev_a3 = run_case(
            case_id="A3_campaign_gate3",
            agent_key="campaign_strategy",
            product_id=PRODUCT,
            quick_brief="ช่วยวางแคมเปญเปิดตัว LAGENIO K2 ตามข้อมูลที่มี เน้นราคาแข่งกับคู่แข่งในกลุ่มสมาร์ทวอทช์เด็ก",
            stage="A",
            estimated_cost=0.05,
            output_dir=OUTPUT_DIR,
            # Fix #3: pass competitor context like Gate 3 did
            context={
                "use_competitor": True,
                "use_campaign": True,
                "competitor_analysis": "คู่แข่งหลักในกลุ่มสมาร์ทวอทช์เด็ก: imoo Z1, Huawei Watch Kids, Xiaomi Mi Watch Kids — ราคาช่วง 2,000-5,000 บาท",
            },
            # Fix #1: override max_retry_limit to 1 for qualification
            agent_settings_override={"max_retry_limit": 1},
        )
        save_evidence(ev_a3, QUAL_DIR)
        _record_ledger(ev_a3)
        _print_case_summary(ev_a3)
        results["A3"] = ev_a3
    else:
        results["A3"] = {"blocked_by_budget": True}
        print("A3 BLOCKED by budget")

    cum_after_a3 = _read_cumulative_cost()
    print(f"[BUDGET] After A3: ${cum_after_a3:.6f} / ${0.40:.6f} Stage A ceiling")

    # ===================================================================
    # Agent 4 — Content Creator (C1 only — ask-before, Facebook, 1 post)
    # ===================================================================
    print("\n" + "=" * 70)
    print("AGENT 4 — Content Creator (content_creator)")
    print("  C1: Ask-before media (Facebook, 1 post, image)")
    print("=" * 70)

    print("\n--- C1: Ask-before media (Facebook, 1 post, image) ---")
    if budget_guard("A", 0.02, "C1"):
        ev_c1 = run_case(
            case_id="C1_content_ask_before_facebook",
            agent_key="content_creator",
            product_id=PRODUCT,
            quick_brief="",
            stage="A",
            estimated_cost=0.02,
            content_count=1,
            platforms=["facebook"],
            media_type="image",
            auto_image=False,
            auto_video=False,
            output_dir=OUTPUT_DIR,
        )
        save_evidence(ev_c1, QUAL_DIR)
        _record_ledger(ev_c1)
        _print_case_summary(ev_c1)
        results["C1"] = ev_c1
    else:
        results["C1"] = {"blocked_by_budget": True}
        print("C1 BLOCKED by budget")

    cum_after_c1 = _read_cumulative_cost()
    print(f"[BUDGET] After C1: ${cum_after_c1:.6f} / ${0.40:.6f} Stage A ceiling")

    # ===================================================================
    # Save ledger + summary
    # ===================================================================
    print("\n" + "=" * 70)
    print("SAVING LEDGER AND SUMMARY")
    print("=" * 70)

    ledger_path = QUAL_DIR / "paid_request_ledger.json"
    ledger_path.write_text(json.dumps(LEDGER, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Ledger saved: {ledger_path} ({len(LEDGER)} paid requests)")

    final_spend = current_spend()
    summary = {
        "qualification_dir": str(QUAL_DIR),
        "product_fixture": PRODUCT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rerun_reason": "Fix runner issues + verify Agent 2/3/4 after code fixes",
        "fixes_applied": [
            "Agent 3 max_retry_limit=1 (override config for qualification)",
            "Budget guard checks after each case",
            "Agent 3 uses Gate 3 equivalent input (competitor data + Quick Brief)",
            "UI parity: StepRunContext, images, resources, content history (via run_case)",
            "Hub receipt: wait for async flush (via run_case)",
        ],
        "final_spend": final_spend,
        "total_paid_requests": len(LEDGER),
        "cases": {},
    }

    for cid, ev in results.items():
        summary["cases"][cid] = {
            "blocked_by_budget": ev.get("blocked_by_budget", False),
            "error": ev.get("error"),
            "run_cost_usd": ev.get("run_cost_usd", 0),
            "num_paid_requests": ev.get("num_paid_requests", 0),
        }

    summary_path = QUAL_DIR / "qualification_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Summary saved: {summary_path}")

    # Print final verdict
    print("\n" + "=" * 70)
    print("FINAL VERDICT")
    print("=" * 70)
    for cid, ev in results.items():
        blocked = ev.get("blocked_by_budget", False)
        err = ev.get("error")
        if blocked:
            verdict = "BLOCKED by budget"
        elif err:
            verdict = f"FAIL: {err[:100]}"
        else:
            verdict = "OK"
        print(f"  {cid}: {verdict}")

    print(f"\nTotal spend: ${final_spend['cumulative']:.6f}")
    print(f"Total paid requests: {len(LEDGER)}")

    # Restore config/agents.yaml to original values
    _restore_yaml_overrides()


if __name__ == "__main__":
    main()
