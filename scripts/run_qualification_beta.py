"""Evaluation-only: Beta qualification for Agent 2, 3, 4 — one case each.

Matches the Product Owner's qualification prompt:
- Agent 2: default discovery (no competitor_data, short Quick Brief)
- Agent 3: non-web scope, short Quick Brief changing campaign emphasis
- Agent 4: Facebook + TikTok, text+image, short Quick Brief, no video

Hard outbound budget guard: $0.40 Stage A ceiling, $1.35 total.
1 generation + max 1 repair per case. Stops after pass.
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

TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
QUAL_DIR = PROJECT_ROOT / "data" / "all_agents_beta_qualification" / f"beta_{TIMESTAMP}"
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
        print(f"  Error: {err[:500]}")
    if evidence.get("result_preview"):
        preview = evidence['result_preview'][:500]
        print(f"  Output preview:\n{preview}")
    print(f"{'='*60}\n")


def main():
    print("=" * 70)
    print("MKTApp Beta Qualification — Agent 2, 3, 4 (one case each)")
    print(f"Product fixture: {PRODUCT}")
    print(f"Qualification dir: {QUAL_DIR}")
    print(f"Budget: $0.40 Stage A ceiling, $1.35 total")
    print(f"Rules: 1 generation + max 1 repair per case, stop after pass")
    print("=" * 70)

    _init_session_baseline()
    results: dict[str, dict] = {}

    # ===================================================================
    # Agent 2 — Competitor Analyst (default discovery)
    # User: select product, select Competitor Analyst, no competitor_data,
    #        short Quick Brief, press run → default discovery
    # ===================================================================
    print("\n" + "=" * 70)
    print("AGENT 2 — Competitor Analyst (default discovery)")
    print("  Scenario: user selects product + agent, no competitor data,")
    print("            short Quick Brief, expects analysis with evidence")
    print("  Model: google/gemini-3.8-flash (production config)")
    print("  Web scope: live web search (production default)")
    print("  Call ceiling: 1 generation + 1 repair max")
    print("=" * 70)

    print("\n--- A2: Default competitor discovery ---")
    if budget_guard("A", 0.05, "A2"):
        ev_a2 = run_case(
            case_id="A2_default_discovery",
            agent_key="competitor_analysis",
            product_id=PRODUCT,
            quick_brief="วิเคราะห์คู่แข่งเด็กสมาร์ทวอทช์ในไทย เน้นราคาและฟีเจอร์ GPS tracking",
            stage="A",
            estimated_cost=0.05,
            output_dir=OUTPUT_DIR,
        )
        save_evidence(ev_a2, QUAL_DIR)
        _record_ledger(ev_a2)
        _print_case_summary(ev_a2)
        results["A2"] = ev_a2
    else:
        results["A2"] = {"blocked_by_budget": True}
        print("A2 BLOCKED by budget")

    cum_after_a2 = _read_cumulative_cost()
    print(f"[BUDGET] After A2: ${cum_after_a2:.6f} / $0.40 Stage A ceiling")

    # ===================================================================
    # Agent 3 — Campaign Strategist (non-web Beta scope)
    # User: select product, select Campaign Strategist, use product/brand
    #        context, short Quick Brief changing emphasis, no live web
    # ===================================================================
    print("\n" + "=" * 70)
    print("AGENT 3 — Campaign Strategist (non-web Beta scope)")
    print("  Scenario: user selects product + agent, product/brand context,")
    print("            short Quick Brief changing campaign emphasis, no live web")
    print("  Model: google/gemini-3.8-flash (production config)")
    print("  Web scope: OFF (non-web Beta scope this round)")
    print("  Call ceiling: 1 generation + 1 repair max")
    print("=" * 70)

    print("\n--- A3: Campaign strategy with Quick Brief ---")
    if budget_guard("A", 0.05, "A3"):
        ev_a3 = run_case(
            case_id="A3_campaign_nonweb",
            agent_key="campaign_strategy",
            product_id=PRODUCT,
            quick_brief="เน้นกลุ่มผู้ปกครองที่ซื้อให้ลูก ไม่ใช่เด็กใช้เอง โทนน่าเชื่อถือ ไม่ใช่โทนขายเร่ง",
            stage="A",
            estimated_cost=0.05,
            output_dir=OUTPUT_DIR,
            context={
                "use_competitor": True,
                "use_campaign": True,
                "competitor_analysis": "คู่แข่งหลักในกลุ่มสมาร์ทวอทช์เด็ก: imoo Z1, Huawei Watch Kids — ราคาช่วง 2,000-5,000 บาท",
            },
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
    print(f"[BUDGET] After A3: ${cum_after_a3:.6f} / $0.40 Stage A ceiling")

    # ===================================================================
    # Agent 4 — Content Creator (text + image, Facebook + TikTok)
    # User: select product, select Content Creator, select Facebook + TikTok,
    #        select post count, select text+image, short Quick Brief, no video
    # ===================================================================
    print("\n" + "=" * 70)
    print("AGENT 4 — Content Creator (text + image, FB + TikTok)")
    print("  Scenario: user selects product + agent, Facebook + TikTok,")
    print("            2 posts total, text+image, short Quick Brief, no video")
    print("  Model: google/gemini-3.8-flash (production config)")
    print("  Media scope: text + image only (no video this round)")
    print("  Call ceiling: 1 generation + 1 repair max")
    print("=" * 70)

    print("\n--- C1: Content Creator FB+TikTok, text+image ---")
    if budget_guard("A", 0.05, "C1"):
        ev_c1 = run_case(
            case_id="C1_content_fb_tiktok_text_image",
            agent_key="content_creator",
            product_id=PRODUCT,
            quick_brief="โทนเป็นกันเอง น่ารัก ดูเป็นครอบครัว ไม่ใช่โทนขายเร่ง",
            stage="A",
            estimated_cost=0.05,
            content_count=2,
            platforms=["facebook", "tiktok"],
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
    print(f"[BUDGET] After C1: ${cum_after_c1:.6f} / $0.40 Stage A ceiling")

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
        "purpose": "Beta qualification Agent 2/3/4 — one UI-equivalent case each",
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
            "result_preview": ev.get("result_preview", "")[:1000],
        }

    summary_path = QUAL_DIR / "qualification_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Summary saved: {summary_path}")

    print("\n" + "=" * 70)
    print("FINAL VERDICT")
    print("=" * 70)
    for cid, ev in results.items():
        blocked = ev.get("blocked_by_budget", False)
        err = ev.get("error")
        if blocked:
            verdict = "BLOCKED by budget"
        elif err:
            verdict = f"FAIL: {err[:200]}"
        else:
            verdict = "OK — output produced"
        print(f"  {cid}: {verdict}")

    print(f"\nTotal session spend: ${final_spend['cumulative']:.6f}")
    print(f"Total paid requests: {len(LEDGER)}")

    _restore_yaml_overrides()


if __name__ == "__main__":
    main()
