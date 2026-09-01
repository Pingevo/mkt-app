"""Evaluation-only: Rerun Agent 4 only after brand visual type mismatch fix."""
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
QUAL_DIR = PROJECT_ROOT / "data" / "all_agents_beta_qualification" / f"beta_rerun_a4_{TIMESTAMP}"
OUTPUT_DIR = QUAL_DIR / "run_outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PRODUCT = "Lagenio K2"
LEDGER: list[dict] = []


def main():
    print("=" * 70)
    print("MKTApp Beta Qualification RERUN — Agent 4 only")
    print(f"Product fixture: {PRODUCT}")
    print("Fix: orchestrator handles image_style/keywords as string or dict/list")
    print(f"Qualification dir: {QUAL_DIR}")
    print("=" * 70)

    _init_session_baseline()

    print("\n--- C1: Ask-before media (Facebook, 1 post, image) ---")
    if budget_guard("A", 0.02, "A4_rerun"):
        ev = run_case(
            case_id="C1_content_ask_before_facebook_rerun",
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
        print("A4 RERUN BLOCKED by budget")

    ledger_path = QUAL_DIR / "paid_request_ledger.json"
    ledger_path.write_text(json.dumps(LEDGER, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    final_spend = current_spend()
    print(f"\nTotal session spend: ${final_spend['cumulative']:.6f}")
    print(f"Total paid requests: {len(LEDGER)}")

    _restore_yaml_overrides()


if __name__ == "__main__":
    main()
