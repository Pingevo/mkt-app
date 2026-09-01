"""Evaluation-only: Execute all Beta qualification cases in order.

Run with: python3 scripts/run_qualification.py

This script runs the paid real-model cases for Agents 1-4 in sequence,
enforcing the hard budget ceiling. It saves all evidence to the
qualification directory.
"""

from __future__ import annotations

import json
import sys
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
)

# Qualification directory
QUAL_DIR = PROJECT_ROOT / "data" / "all_agents_beta_qualification" / "20260831_160709"
OUTPUT_DIR = QUAL_DIR / "run_outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PRODUCT = "Lagenio K2"

# Ledger of all paid requests
LEDGER: list[dict] = []


def _record_ledger(evidence: dict) -> None:
    """Record paid requests from a case into the global ledger."""
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
    print("MKTApp Beta Qualification — Real-Model Evaluation")
    print(f"Product fixture: {PRODUCT}")
    print(f"Qualification dir: {QUAL_DIR}")
    print("=" * 70)

    _init_session_baseline()

    results: dict[str, dict] = {}

    # ===================================================================
    # Agent 1 — Product Analyst
    # ===================================================================
    print("\n" + "=" * 70)
    print("AGENT 1 — Product Analyst (product_spec)")
    print("=" * 70)

    # A1: Zero-prompt default
    print("\n--- A1: Zero-prompt default ---")
    if budget_guard("A", 0.02, "A1"):
        ev_a1 = run_case(
            case_id="A1_product_spec_default",
            agent_key="product_spec",
            product_id=PRODUCT,
            quick_brief="",
            stage="A",
            estimated_cost=0.02,
            output_dir=OUTPUT_DIR,
        )
        save_evidence(ev_a1, QUAL_DIR)
        _record_ledger(ev_a1)
        _print_case_summary(ev_a1)
        results["A1"] = ev_a1
    else:
        results["A1"] = {"blocked_by_budget": True}
        print("A1 BLOCKED by budget")

    # A2: Quick Brief + Settings
    print("\n--- A2: Quick Brief + Agent Settings ---")
    if budget_guard("A", 0.02, "A2"):
        ev_a2 = run_case(
            case_id="A2_product_spec_brief_settings",
            agent_key="product_spec",
            product_id=PRODUCT,
            quick_brief="เน้นมุมมองสำหรับผู้ปกครองที่กังวลเรื่องความปลอดภัยของเด็ก — เน้น GPS tracking และ Geo-Fence เป็นจุดขายหลัก",
            stage="A",
            estimated_cost=0.02,
            agent_settings_override={"detail_level": "detailed", "focus": ["USP", "customer_benefit", "technical"]},
            output_dir=OUTPUT_DIR,
        )
        save_evidence(ev_a2, QUAL_DIR)
        _record_ledger(ev_a2)
        _print_case_summary(ev_a2)
        results["A2"] = ev_a2
    else:
        results["A2"] = {"blocked_by_budget": True}
        print("A2 BLOCKED by budget")

    # ===================================================================
    # Agent 2 — Competitor Analyst
    # ===================================================================
    print("\n" + "=" * 70)
    print("AGENT 2 — Competitor Analyst (competitor_analysis)")
    print("=" * 70)

    print("\n--- A2-comp: Bounded evidence case ---")
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

    # ===================================================================
    # Agent 3 — Campaign Strategist (Gate 3 live-web)
    # ===================================================================
    print("\n" + "=" * 70)
    print("AGENT 3 — Campaign Strategist (campaign_strategy) — Gate 3 live-web")
    print("=" * 70)

    print("\n--- A3: Gate 3 live-web case ---")
    if budget_guard("A", 0.05, "A3"):
        ev_a3 = run_case(
            case_id="A3_campaign_gate3",
            agent_key="campaign_strategy",
            product_id=PRODUCT,
            quick_brief="",
            stage="A",
            estimated_cost=0.05,
            output_dir=OUTPUT_DIR,
        )
        save_evidence(ev_a3, QUAL_DIR)
        _record_ledger(ev_a3)
        _print_case_summary(ev_a3)
        results["A3"] = ev_a3
    else:
        results["A3"] = {"blocked_by_budget": True}
        print("A3 BLOCKED by budget")

    # ===================================================================
    # Agent 4 — Content Creator (C1, C2, C3)
    # ===================================================================
    print("\n" + "=" * 70)
    print("AGENT 4 — Content Creator (content_creator)")
    print("=" * 70)

    # C1: Ask-before media (Facebook, 1 post, image, ask-before)
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

    # C2: Platform/count/steering (TikTok, 2 posts, video, ask-before)
    print("\n--- C2: TikTok, 2 posts, video, ask-before ---")
    if budget_guard("A", 0.03, "C2"):
        ev_c2 = run_case(
            case_id="C2_content_tiktok_2posts_video",
            agent_key="content_creator",
            product_id=PRODUCT,
            quick_brief="เน้นความปลอดภัยของเด็ก ใช้มุมมองผู้ปกครองกังวล",
            stage="A",
            estimated_cost=0.03,
            content_count=2,
            platforms=["tiktok"],
            media_type="video",
            auto_image=False,
            auto_video=False,
            agent_settings_override={"hook_style": "emotional", "tone": ["friendly", "professional"]},
            output_dir=OUTPUT_DIR,
        )
        save_evidence(ev_c2, QUAL_DIR)
        _record_ledger(ev_c2)
        _print_case_summary(ev_c2)
        results["C2"] = ev_c2
    else:
        results["C2"] = {"blocked_by_budget": True}
        print("C2 BLOCKED by budget")

    # C3: Real image (1 post, 1 platform, image, auto-generate)
    print("\n--- C3: Real image generation (Facebook, 1 post, auto-generate) ---")
    if budget_guard("A", 0.08, "C3"):
        ev_c3 = run_case(
            case_id="C3_content_real_image",
            agent_key="content_creator",
            product_id=PRODUCT,
            quick_brief="",
            stage="A",
            estimated_cost=0.08,
            content_count=1,
            platforms=["facebook"],
            media_type="image",
            auto_image=True,
            auto_video=False,
            output_dir=OUTPUT_DIR,
        )
        save_evidence(ev_c3, QUAL_DIR)
        _record_ledger(ev_c3)
        _print_case_summary(ev_c3)
        results["C3"] = ev_c3
    else:
        results["C3"] = {"blocked_by_budget": True}
        print("C3 BLOCKED by budget")

    # ===================================================================
    # Stage B — Agent 4 C4: Real video
    # ===================================================================
    print("\n" + "=" * 70)
    print("STAGE B — Agent 4 C4: Real video generation")
    print("=" * 70)

    # Only proceed if all Stage A non-video cases completed without critical failure
    stage_a_ok = all(
        not r.get("blocked_by_budget") and not r.get("error")
        for k, r in results.items()
        if k in ("A1", "A2", "A2comp", "A3", "C1", "C2", "C3")
    )

    if not stage_a_ok:
        print("Stage A had failures or blocks — Stage B (video) NOT started.")
        print("Stage A status:")
        for k in ("A1", "A2", "A2comp", "A3", "C1", "C2", "C3"):
            r = results.get(k, {})
            print(f"  {k}: blocked={r.get('blocked_by_budget', False)} error={r.get('error', 'none')}")
    else:
        print("\n--- C4: Real video (TikTok, 1 post, auto-generate) ---")
        if budget_guard("B", 0.50, "C4"):
            ev_c4 = run_case(
                case_id="C4_content_real_video",
                agent_key="content_creator",
                product_id=PRODUCT,
                quick_brief="",
                stage="B",
                estimated_cost=0.50,
                content_count=1,
                platforms=["tiktok"],
                media_type="video",
                auto_image=False,
                auto_video=True,
                output_dir=OUTPUT_DIR,
            )
            save_evidence(ev_c4, QUAL_DIR)
            _record_ledger(ev_c4)
            _print_case_summary(ev_c4)
            results["C4"] = ev_c4
        else:
            results["C4"] = {"blocked_by_budget": True}
            print("C4 BLOCKED by budget")

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
        "final_spend": final_spend,
        "total_paid_requests": len(LEDGER),
        "cases": {k: {
            "blocked_by_budget": v.get("blocked_by_budget", False),
            "error": v.get("error"),
            "run_cost_usd": v.get("run_cost_usd", 0),
            "num_paid_requests": v.get("num_paid_requests", 0),
        } for k, v in results.items()},
    }
    summary_path = QUAL_DIR / "qualification_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Summary saved: {summary_path}")
    print(f"\nFinal session spend: ${final_spend['cumulative']:.6f}")
    print(f"Budget remaining: ${final_spend['total_ceiling'] - final_spend['cumulative']:.6f}")


if __name__ == "__main__":
    main()
