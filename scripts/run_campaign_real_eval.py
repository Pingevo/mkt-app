"""CLI for running the Agent 3 real-model / free-preflight evaluation harness.

Default behaviour is safe: it only shows the plan and cost estimate.
No credit is charged unless ``--execute-paid`` is used.

Examples
--------
Show the paid plan without spending anything:
  python scripts/run_campaign_real_eval.py

Show the free preflight plan without spending anything:
  python scripts/run_campaign_real_eval.py --show-free-plan

Dry-run the harness mechanics (no model calls):
  python scripts/run_campaign_real_eval.py --dry-run --stage 1

Run a free preflight stage (openrouter/free, no web search):
  python scripts/run_campaign_real_eval.py --free-preflight --stage 1

Run a paid production validation stage with a hard budget:
  python scripts/run_campaign_real_eval.py --execute-paid --budget-cap 0.10 --stage 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Add project root to path so we can import src.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.evaluation.campaign_real_eval import CampaignRealEvalHarness
from src.llm_client import LLMClient


REQUIRED_ENV_VAR = "OPENROUTER_API_KEY"
DEFAULT_PAID_PLAN = ROOT / "data" / "campaign_real_eval_plan.json"
DEFAULT_FREE_PLAN = ROOT / "data" / "campaign_free_preflight_plan.json"
DEFAULT_PAID_STATE = ROOT / "data" / "campaign_real_eval_state.json"
DEFAULT_PAID_REPORT = ROOT / "data" / "campaign_real_eval_report.md"
DEFAULT_FREE_STATE = ROOT / "data" / "campaign_free_preflight_state.json"
DEFAULT_FREE_REPORT = ROOT / "data" / "campaign_free_preflight_report.md"


def _load_plan(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_client() -> LLMClient:
    from src.openrouter_gateway import get_api_key as _gate_get_api_key
    api_key = _gate_get_api_key()
    if not api_key:
        raise RuntimeError(
            f"{REQUIRED_ENV_VAR} is not set. "
            "Set it in the environment or .env file before any real execution."
        )
    # Use defaults from config, not the plan, to avoid hard-coding secrets.
    from src.config_loader import load_config
    defaults = load_config().get("defaults", {})
    return LLMClient(
        api_key=api_key,
        base_url=defaults.get("base_url", "https://openrouter.ai/api/v1"),
        default_model=defaults.get("model", "anthropic/claude-sonnet-4"),
        timeout=float(defaults.get("timeout_seconds", 120)),
    )


def _print_plan_summary(plan: dict, is_free: bool = False) -> None:
    print("=" * 60)
    print(f"Plan: {plan.get('name', 'n/a')}")
    print(f"Model family: {plan.get('model_family', 'n/a')}")
    if is_free:
        print("Cost: $0.00 USD (free preflight)")
    else:
        print(f"Recommended budget cap: ${plan.get('recommended_budget_cap_usd', 0):.4f} USD")
    print("")
    total_est = 0.0
    for stage in plan.get("stages", []):
        sid = stage.get("stage_id")
        desc = stage.get("description", "")
        cases = stage.get("cases", [])
        stage_est = sum(float(c.get("estimated_cost_usd", 0)) * int(c.get("n_runs", 1)) for c in cases)
        total_est += stage_est
        print(f"Stage {sid}: {desc}")
        print(f"  cases: {len(cases)} | estimated cost: ${stage_est:.4f} USD")
        for c in cases:
            print(
                f"    - {c.get('case_id')}: n_runs={c.get('n_runs')} "
                f"web_search={c.get('web_search')} "
                f"est=${float(c.get('estimated_cost_usd', 0)):.4f} "
                f"| {c.get('why', '')}"
            )
    print("")
    print(f"Total estimated cost for all stages: ${total_est:.4f} USD")
    if len(plan.get("stages", [])) > 1:
        print(f"Minimum meaningful run (Stage 1 only): "
              f"${sum(float(c.get('estimated_cost_usd', 0)) * int(c.get('n_runs', 1)) for c in plan.get('stages', [])[1].get('cases', [])):.4f} USD")
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=None,
                        help="Path to a plan JSON. Defaults depend on execution mode.")
    parser.add_argument("--state", type=Path, default=None,
                        help="Path to state JSON. Defaults depend on execution mode.")
    parser.add_argument("--report", type=Path, default=None,
                        help="Path to report Markdown. Defaults depend on execution mode.")
    parser.add_argument("--stage", default=None,
                        help="Stage index or stage_id to execute. Required for all execution modes.")
    parser.add_argument("--budget-cap", type=float, default=None,
                        help="Hard spending cap in USD. Required for --execute-paid; rejected for --free-preflight.")
    parser.add_argument("--execute-paid", action="store_true",
                        help="Run with the production model on real credit.")
    parser.add_argument("--free-preflight", action="store_true",
                        help="Run with openrouter/free. No budget cap, no web search, no production certification.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the harness mechanics without calling any model.")
    parser.add_argument("--show-free-plan", action="store_true",
                        help="Show the free preflight plan and exit.")
    args = parser.parse_args(argv)

    # Mutually exclusive execution mode flags.
    exec_modes = [args.execute_paid, args.free_preflight, args.dry_run, args.show_free_plan]
    if sum(bool(m) for m in exec_modes) > 1:
        print("Only one of --execute-paid, --free-preflight, --dry-run, --show-free-plan may be used.", file=sys.stderr)
        return 1

    # Safe default: show the paid plan.
    if not any(exec_modes):
        plan = _load_plan(args.plan or DEFAULT_PAID_PLAN)
        _print_plan_summary(plan, is_free=False)
        print("\nNo model will be called. Use --free-preflight or --execute-paid to run.")
        return 0

    if args.show_free_plan:
        plan = _load_plan(args.plan or DEFAULT_FREE_PLAN)
        _print_plan_summary(plan, is_free=True)
        print("\nNo model will be called. Use --free-preflight --stage <n> to execute with openrouter/free.")
        return 0

    # All actual run modes require a stage.
    if args.stage is None:
        print("--stage is required when running the harness.", file=sys.stderr)
        return 1

    if args.free_preflight:
        if args.budget_cap is not None:
            print("--budget-cap is not accepted with --free-preflight. Free preflight is zero-cost.", file=sys.stderr)
            return 1
        plan = _load_plan(args.plan or DEFAULT_FREE_PLAN)
        state_path = args.state or DEFAULT_FREE_STATE
        report_path = args.report or DEFAULT_FREE_REPORT
        budget_cap = 0.0
        dry_run = False
        mode = "free_preflight"
        print("[FREE PREFLIGHT] Using openrouter/free. Web search is disabled. No credit will be charged.")
    elif args.execute_paid:
        if args.budget_cap is None:
            print("--budget-cap is required for --execute-paid.", file=sys.stderr)
            return 1
        plan = _load_plan(args.plan or DEFAULT_PAID_PLAN)
        state_path = args.state or DEFAULT_PAID_STATE
        report_path = args.report or DEFAULT_PAID_REPORT
        budget_cap = args.budget_cap
        dry_run = False
        mode = "paid"
        print(f"[PAID EXECUTION] Spending will be capped at ${budget_cap:.4f} USD.")
    elif args.dry_run:
        plan = _load_plan(args.plan or DEFAULT_PAID_PLAN)
        state_path = args.state or DEFAULT_PAID_STATE
        report_path = args.report or DEFAULT_PAID_REPORT
        budget_cap = args.budget_cap or 0.0
        dry_run = True
        mode = "paid"
        print("[DRY RUN] No API credit will be used. Outputs are placeholders.")
    else:
        # Defensive fallback.
        plan = _load_plan(args.plan or DEFAULT_PAID_PLAN)
        _print_plan_summary(plan, is_free=False)
        return 0

    llm = None
    if not dry_run:
        llm = _make_client()

    harness = CampaignRealEvalHarness(
        plan=plan,
        llm_client=llm,
        state_path=state_path,
        report_path=report_path,
        budget_cap=budget_cap,
        dry_run=dry_run,
        mode=mode,
    )

    result = harness.run_plan(stage=args.stage)
    report = harness.generate_report(result, fmt="markdown")
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\nReport written to: {report_path}")
    print(f"Total cost: ${result.total_cost_usd:.4f} USD")
    print(f"Remaining budget: ${result.remaining_budget_usd:.4f} USD")
    print(f"Stopped: {result.stopped} ({result.stop_reason or 'not stopped'})")
    return 0 if not result.stopped else 2


if __name__ == "__main__":
    sys.exit(main())
