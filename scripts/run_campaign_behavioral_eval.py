#!/usr/bin/env python3
"""CLI to run the Agent 3 behavioral scenario evaluation.

Defaults to dry-run (no API credit, no model call).  `--free-preflight` is
required to call `openrouter/free` explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from datetime import datetime
from pathlib import Path

# Ensure the project root is on the path when running from scripts/.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.evaluation.campaign_behavioral_eval import (
    CampaignBehavioralEvalHarness,
    load_scenarios,
)


class DryRunLLM:
    """A fake LLM that returns a dry-run placeholder and never calls an API."""

    def __init__(self) -> None:
        self._last_raw_response: dict = {}
        self.model = "n/a"

    def chat(self, messages, **kwargs) -> str:
        return "[DRY RUN: real model not called]"

    def close(self) -> None:
        pass


def _new_llm_for_free_preflight() -> Any:
    from src.llm_client import LLMClient
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit(
            "ERROR: OPENROUTER_API_KEY is required for --free-preflight (openrouter/free). "
            "Use --dry-run or set the environment variable."
        )
    return LLMClient(api_key=api_key)


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H%M%S")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run behavioral pressure scenarios for the campaign strategy agent."
    )
    parser.add_argument(
        "--plan",
        type=str,
        default=str(ROOT / "data" / "campaign_behavioral_scenarios.json"),
        help="Path to the behavioral scenario plan JSON.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Default safe mode: no LLM is called, outputs are placeholders.",
    )
    mode_group.add_argument(
        "--free-preflight",
        action="store_true",
        help="Call openrouter/free only. No paid fallback, no web search.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(ROOT / "output" / "eval" / "behavioral"),
        help="Directory for timestamped artifacts and reports.",
    )
    parser.add_argument(
        "--report",
        type=str,
        default=None,
        help="Path for the markdown report. Default is a timestamped file in --output-dir.",
    )
    parser.add_argument(
        "--scenarios",
        type=str,
        default=None,
        help="Comma-separated scenario IDs to run (e.g. S07,S05).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if not args.dry_run and not args.free_preflight:
        print("[DRY RUN] No mode selected; defaulting to dry-run. No API credit will be used.")
        args.dry_run = True

    if args.free_preflight:
        llm = _new_llm_for_free_preflight()
        configured_model = "openrouter/free"
        print("[FREE PREFLIGHT] Using openrouter/free. Web search is disabled. No paid fallback.")
    else:
        llm = DryRunLLM()
        configured_model = "n/a"
        print("[DRY RUN] No LLM will be called; outputs are placeholders.")

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"ERROR: plan not found: {plan_path}", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir) / f"run-{_timestamp()}"
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = Path(args.report) if args.report else output_dir / "behavioral_report.md"
    state_path = output_dir / "run_state.json"

    scenarios = load_scenarios(plan_path)
    if args.scenarios:
        wanted = [s.strip() for s in args.scenarios.split(",")]

        def _matches(w: str, scenario_id: str) -> bool:
            return scenario_id == w or scenario_id.startswith(f"{w}_")

        scenarios = [s for s in scenarios if any(_matches(w, s["scenario_id"]) for w in wanted)]
        missing = set(w for w in wanted if not any(_matches(w, s["scenario_id"]) for s in scenarios))
        if missing:
            print(f"ERROR: scenario IDs not found in plan: {sorted(missing)}", file=sys.stderr)
            return 1
        print(f"Filtered to {len(scenarios)} scenario(s): {sorted(wanted)}")

    harness = CampaignBehavioralEvalHarness(
        scenarios=scenarios,
        llm_client=llm,
        artifact_dir=output_dir / "artifacts",
        stop_on={"critical"},
    )

    print(f"Running {len(scenarios)} scenario(s)...")
    result = harness.run_all()
    report = harness.generate_report(result, fmt="markdown")
    report_header = (
        f"**Configured model:** {configured_model}\n\n"
        f"**Actual model returned:** see per-scenario 'Actual model' field below.\n\n"
        f"**Execution mode:** {'free_preflight' if args.free_preflight else 'dry_run'}\n\n"
    )

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_header + report)

    # Save a small machine-readable state file for resume/traceability.
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "mode": "free_preflight" if args.free_preflight else "dry_run",
                "configured_model": configured_model,
                "completed_at": result.completed_at,
                "model_calls": result.model_calls,
                "total": result.total,
                "passed": result.passed,
                "failed": result.failed,
                "stopped": result.stopped,
                "stop_reason": result.stop_reason,
                "report": str(report_path),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"\nReport written to: {report_path}")
    print(f"Total: {result.total}, passed: {result.passed}, failed: {result.failed}")
    if result.stopped:
        print(f"Stopped: {result.stop_reason}")
    print(
        "\nNOTE: Free preflight / dry-run behavioral results do not certify the configured "
        "production model. They only test whether the free/placeholder model handles the "
        "pressure scenarios before committing to paid evaluation."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
