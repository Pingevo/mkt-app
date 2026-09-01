"""Evaluation-only: Rerun Agent 3 only after validator fix (Thai word boundary)."""
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
    _restore_yaml_overrides,
)

TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
QUAL_DIR = PROJECT_ROOT / "data" / "all_agents_beta_qualification" / f"beta_rerun_a3_{TIMESTAMP}"
OUTPUT_DIR = QUAL_DIR / "run_outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PRODUCT = "Lagenio K2"
LEDGER: list[dict] = []


def main():
    print("=" * 70)
    print("MKTApp Beta Qualification RERUN — Agent 3 only")
    print(f"Product fixture: {PRODUCT}")
    print("Fix: validator Thai word boundary + removed ambiguous benchmark markers")
    print(f"Qualification dir: {QUAL_DIR}")
    print("=" * 70)

    _init_session_baseline()

    print("\n--- A3: Gate 3 equivalent (rerun) ---")
    if budget_guard("A", 0.05, "A3_rerun"):
        ev = run_case(
            case_id="A3_campaign_gate3_rerun",
            agent_key="campaign_strategy",
            product_id=PRODUCT,
            quick_brief="ช่วยวางแคมเปญเปิดตัว LAGENIO K2 ตามข้อมูลที่มี เน้นราคาแข่งกับคู่แข่งในกลุ่มสมาร์ทวอทช์เด็ก",
            stage="A",
            estimated_cost=0.05,
            output_dir=OUTPUT_DIR,
            context={
                "use_competitor": True,
                "use_campaign": True,
                "competitor_analysis": "คู่แข่งหลักในกลุ่มสมาร์ทวอทช์เด็ก: imoo Z1, Huawei Watch Kids, Xiaomi Mi Watch Kids — ราคาช่วง 2,000-5,000 บาท",
            },
            agent_settings_override={"max_retry_limit": 1},
        )
        save_evidence(ev, QUAL_DIR)
        for entry in ev.get("usage_entries", []):
            LEDGER.append({
                "case_id": ev.get("case_id"),
                "agent": ev.get("agent_key"),
                "request_id": entry.get("request_id"),
                "model": entry.get("model"),
                "source": entry.get("source"),
                "cost_usd": entry.get("cost_usd"),
            })

        cid = ev.get("case_id", "?")
        cost = ev.get("run_cost_usd", 0)
        n_req = ev.get("num_paid_requests", 0)
        err = ev.get("error")
        blocked = ev.get("blocked_by_budget", False)
        status = "BLOCKED" if blocked else ("ERROR" if err else "OK")
        print(f"\n{'='*60}")
        print(f"CASE {cid}: {status}")
        print(f"  Paid requests: {n_req}")
        print(f"  Case cost: ${cost:.6f}")
        if err:
            print(f"  Error: {err[:500]}")
        if ev.get("result_preview"):
            print(f"  Output preview:\n{ev['result_preview'][:1000]}")
        print(f"{'='*60}")
    else:
        print("A3 RERUN BLOCKED by budget")

    ledger_path = QUAL_DIR / "paid_request_ledger.json"
    ledger_path.write_text(json.dumps(LEDGER, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    final_spend = current_spend()
    print(f"\nTotal session spend: ${final_spend['cumulative']:.6f}")
    print(f"Total paid requests: {len(LEDGER)}")

    _restore_yaml_overrides()


if __name__ == "__main__":
    main()
