#!/usr/bin/env python3
"""Production Qualification #1 for campaign_strategy with a hard API budget guard.

This runner intentionally does not modify config/agents.yaml or Agent 3 behavior.
It only wraps the production execution with an evaluation-only BudgetGuard that
limits outbound paid LLM attempts to 2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Load .env so OPENROUTER_API_KEY and AI Usage Hub credentials are available.
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

from src.agents.campaign_strategy import CampaignStrategyAgent
from src.config_loader import get_agent_config, load_config
from src.evaluation.campaign_behavioral_eval import load_scenarios
from src.evaluation.campaign_qualification import (
    BudgetGuard,
    QualificationBudgetExhausted,
    aggregate_run_usage,
    make_run_reference,
    read_usage_log_for_reference,
    snapshot_usage_log_offset,
)
from src.flow_context import set_usage_metadata, set_usage_reference, clear_usage_context
from src.llm_client import LLMClient
from src.openrouter_gateway import get_api_key as _gate_get_api_key
from src.ai_usage import (
    _read_hub_credentials,
    flush_usage_log,
    HubReceiptCollector,
    reconcile_hub_receipts,
    USAGE_LOG_PATH,
)


def _provider_metadata(llm: LLMClient) -> dict[str, object]:
    raw = getattr(llm, "_last_raw_response", {}) or {}
    usage = raw.get("usage") or {}
    cost = raw.get("cost") or usage.get("cost")
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "provider_cost_usd": cost if cost is not None else "unknown",
        "finish_reason": None,
        "request_id": raw.get("id"),
    }


def _actual_model(llm: LLMClient) -> str:
    raw = getattr(llm, "_last_raw_response", {}) or {}
    return raw.get("model", "unknown")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Production qualification for a single campaign_strategy scenario with a hard API budget guard."
    )
    parser.add_argument(
        "--scenario",
        default="S07_positive_grounded",
        help="Scenario ID from data/campaign_behavioral_scenarios.json to qualify",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=2,
        help="Hard cap on outbound paid LLM attempts",
    )
    args = parser.parse_args()

    api_key = _gate_get_api_key()
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 1

    output_dir = Path("data/campaign_qualification_attempt")
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir / f"run-{_now().replace(':', '-')[:-7]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    config = load_config()
    agent_config = get_agent_config(config, "campaign_strategy")

    # Qualification overrides: lock the run to one scenario with the selected evidence manifest.
    agent_config = dict(agent_config)
    agent_config["web_search"] = False
    agent_config["verify_urls"] = False
    agent_config["max_retry_limit"] = 1
    agent_config["max_review_iterations"] = 0
    agent_config["stream"] = False
    agent_config["citation_policy"] = {"mode": "inline_first", "fallback_annotations_when_no_inline": False}

    scenarios = load_scenarios(Path("data/campaign_behavioral_scenarios.json"))
    scenario = next((s for s in scenarios if s["scenario_id"] == args.scenario), None)
    if scenario is None:
        print(f"ERROR: scenario {args.scenario} not found", file=sys.stderr)
        return 1

    product_context = scenario["product_context"]
    instructions = scenario["instructions"]
    quick_brief = scenario["quick_brief"]

    # AI Usage Hub preflight check (must be active before any paid call)
    hub_url, hub_token = _read_hub_credentials()
    if not (hub_url and hub_token):
        print("ERROR: AI Usage Hub URL or token not configured", file=sys.stderr)
        return 1

    # Preflight checklist
    print("=== Qualification Preflight ===")
    print(f"Scenario: {scenario['scenario_id']}")
    print(f"Configured model: {agent_config.get('model')}")
    print(f"Web search: {agent_config.get('web_search')}")
    print(f"Verify URLs: {agent_config.get('verify_urls')}")
    print(f"Max retry/limit: {agent_config.get('max_retry_limit')}")
    print(f"Budget: {args.budget} outbound paid LLM attempts (1 generation + 1 repair)")
    print(f"Selected evidence: {instructions.get('selected_evidence_urls', [])}")
    print("================================\n")

    llm = LLMClient(api_key=api_key)
    agent = CampaignStrategyAgent(agent_config, llm, instructions=instructions)
    user_prompt = agent.build_prompt(product_context)

    output = ""
    exception_type = ""
    exception_message = ""
    validator_results: list[dict[str, object]] = []
    audit_output: list[dict[str, object]] = []
    budget_exhausted = False
    budget_exhausted_request = ""

    # Run-scoped usage accounting: unique reference, byte-offset snapshot.
    run_id = run_dir.name.replace("run-", "")
    run_ref = make_run_reference(run_id, scenario["scenario_id"], kind="qualification")
    set_usage_reference(run_ref)
    set_usage_metadata({
        "scenario_id": scenario["scenario_id"],
        "run_id": run_id,
        "runner": "run_campaign_qualification",
        "budget": args.budget,
    })
    usage_log_offset = snapshot_usage_log_offset(USAGE_LOG_PATH)

    # Capture per-request Hub delivery status scoped to this run.
    with HubReceiptCollector() as hub_collector:
        with BudgetGuard(budget=args.budget) as guard:
            try:
                output = agent.run(user_prompt, quick_brief=quick_brief)
            except QualificationBudgetExhausted as exc:
                budget_exhausted = True
                budget_exhausted_request = exc.next_request
                exception_type = type(exc).__name__
                exception_message = str(exc)
                # If BaseAgent got as far as producing a draft, capture it.
                draft = getattr(agent, "_last_draft_output", "")
                if draft:
                    output = draft
            except Exception as exc:
                exception_type = type(exc).__name__
                exception_message = str(exc)
                traceback.print_exc(file=sys.stderr)
                draft = getattr(agent, "_last_draft_output", "")
                if draft:
                    output = draft

        # Audit any output we have (draft, final, or repaired).
        if output and output.strip():
            try:
                audit = agent.audit_output(output)
                audit_output = [{"rule": r.rule, "ok": r.ok, "reason": r.reason} for r in audit]
                validator_results = [r for r in audit_output if not r["ok"]]
            except Exception:
                traceback.print_exc(file=sys.stderr)

        # Generation/repair accounting
        repair_requested = getattr(agent, "_last_repair_count", 0)
        repair_blocked_by_budget = budget_exhausted and repair_requested > 0
        repair_completed = (
            repair_requested - 1
            if budget_exhausted and repair_requested > 0
            else repair_requested
        )
        http_attempts_used = guard.used
        generation_attempts = 1 if http_attempts_used > 0 else 0

        # Write artifact.
        artifact_path = artifacts_dir / f"{scenario['scenario_id']}.md"
        artifact_path.write_text(output or "(no output)", encoding="utf-8")

        # Run cost accounting by reference + byte offset (not time window)
        run_entries = read_usage_log_for_reference(USAGE_LOG_PATH, run_ref, usage_log_offset)
        run_summary = aggregate_run_usage(
            run_entries,
            expected_count=guard.used,
            reference=run_ref,
        )

        # Hub flush is required before reporting.  flush == True only means daemon
        # threads ended; the captured per-request callbacks are the real receipt.
        hub_ack = flush_usage_log(timeout=5.0)

    hub_results: list[dict[str, Any]] = hub_collector.results
    hub_reconciliation = reconcile_hub_receipts(
        run_entries,
        hub_results,
        flush_completed=hub_ack,
    )

    report_lines = [
        "# Campaign Strategy Production Qualification #1",
        "",
        "## Execution",
        "",
        f"- **Scenario:** {scenario['scenario_id']}",
        f"- **Configured model:** {agent_config.get('model')}",
        f"- **Actual returned model:** {_actual_model(llm)}",
        f"- **Web search:** {agent_config.get('web_search')}",
        f"- **Verify URLs:** {agent_config.get('verify_urls')}",
        f"- **Max retry limit:** {agent_config.get('max_retry_limit')}",
        f"- **Budget guard:** {guard.budget} outbound paid LLM attempts",
        f"- **HTTP attempts used:** {http_attempts_used}",
        f"- **Budget exhausted:** {budget_exhausted}",
        f"- **Exception type:** {exception_type or 'none'}",
        f"- **Exception message:** {exception_message or '-'}",
        f"- **Run directory:** {run_dir}",
        f"- **Completed at:** {_now()}",
        "",
        "## Attempt log",
        "",
        "| # | method | url | status | error |",
        "|---|--------|-----|--------|-------|",
    ]
    for i, entry in enumerate(guard.attempt_log, 1):
        report_lines.append(
            f"| {i} | {entry.get('method')} | {entry.get('url')} | {entry.get('status')} | {entry.get('error') or '-'} |"
        )
    report_lines.extend([
        "",
        "## Provider metadata",
        "",
    ])
    meta = _provider_metadata(llm)
    for k, v in meta.items():
        report_lines.append(f"- **{k}:** {v if v is not None else '-'}")

    report_lines.extend([
        "",
        "## Draft/repair accounting",
        "",
        f"- **Generation attempts:** {generation_attempts}",
        f"- **Repair requested:** {repair_requested}",
        f"- **Repair completed:** {repair_completed}",
        f"- **Repair blocked by budget:** {repair_blocked_by_budget}",
        f"- **HTTP attempts used:** {http_attempts_used} / {guard.budget}",
        f"- **Draft output chars:** {len(getattr(agent, '_last_draft_output', '') or '')}",
        f"- **First validation error:** {getattr(agent, '_last_first_validation_error', '') or '-'}",
        "",
        "## Validator results",
        "",
    ])
    if audit_output:
        report_lines.append("| rule | ok | reason |")
        report_lines.append("|------|----|--------|")
        for r in audit_output:
            ok = "PASS" if r["ok"] else "FAIL"
            report_lines.append(f"| {r['rule']} | {ok} | {r['reason'] or '-'} |")
    else:
        report_lines.append("No validator results available.")

    report_lines.extend([
        "",
        "## Run cost accounting",
        "",
        f"- **Usage reference:** `{run_ref}`",
        f"- **Total run cost (local log):** {run_summary.total_cost:.6f} USD",
        f"- **Final response cost (provider metadata):** {meta.get('provider_cost_usd') if meta.get('provider_cost_usd') is not None else '-'} USD",
        f"- **Run calls recorded:** {run_summary.call_count}",
        f"- **Expected calls:** {run_summary.expected_count}",
        f"- **Accounting status:** {run_summary.accounting_status}",
        f"- **Total prompt tokens:** {run_summary.total_prompt_tokens}",
        f"- **Total completion tokens:** {run_summary.total_completion_tokens}",
        f"- **Total tokens:** {run_summary.total_tokens}",
    ])

    if run_summary.per_call:
        report_lines.extend([
            "",
            "### Per-call local usage",
            "",
            "| # | request_id | source | classification | status | cost (USD) | prompt | completion | total |",
            "|---|------------|--------|----------------|--------|------------|--------|------------|-------|",
        ])
        for i, call in enumerate(run_summary.per_call, 1):
            report_lines.append(
                f"| {i} | {call['request_id'] or '-'} | {call['source'] or '-'} | "
                f"{call['classification']} | {call['status'] or '-'} | "
                f"{call['cost_usd']:.6f} | {call['prompt_tokens']} | "
                f"{call['completion_tokens']} | {call['total_tokens']} |"
            )

    report_lines.extend([
        "",
        "## Hub delivery reconciliation",
        "",
        f"- **Hub flush acknowledged:** {hub_ack}",
        f"- **Hub reconciliation status:** {hub_reconciliation['overall']}",
        f"- **Expected receipts:** {hub_reconciliation['expected']}",
        f"- **Delivered:** {hub_reconciliation['delivered_count']}",
        f"- **Missing:** {hub_reconciliation['missing_receipt'] or 'none'}",
        f"- **Duplicate:** {hub_reconciliation['duplicate_receipt'] or 'none'}",
        f"- **Unrelated:** {hub_reconciliation['unrelated_receipt'] or 'none'}",
        f"- **HTTP errors:** {hub_reconciliation['http_error'] or 'none'}",
        f"- **Transport errors:** {hub_reconciliation['transport_error'] or 'none'}",
        f"- **Flush timeout:** {hub_reconciliation['flush_timeout'] or 'none'}",
    ])

    if hub_results:
        report_lines.extend([
            "",
            "### Per-request Hub delivery",
            "",
            "| # | request_id | hub_status | status_code | error |",
            "|---|------------|------------|-------------|-------|",
        ])
        for i, r in enumerate(hub_results, 1):
            status = r.get("status_code", "-")
            error = (r.get("error") or "-")[:60]
            report_lines.append(
                f"| {i} | {r.get('request_id') or '-'} | {r.get('hub_status')} | {status} | {error} |"
            )
    else:
        report_lines.extend(["", "- **Per-request Hub delivery:** no receipts captured"])

    report_lines.extend([
        "",
        "## Output preview (first 2000 chars)",
        "",
        "```text",
        (output or "(no output)")[:2000],
        "```",
    ])

    report_path = run_dir / "qualification_report.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    # Decision helpers — the human picks one after review.
    decision = "PENDING"
    if exception_type == "QualificationBudgetExhausted":
        decision = "INCONCLUSIVE — API BUDGET EXHAUSTED"
    elif output and output.strip() and not validator_results:
        decision = "PASS"
    elif output and output.strip() and validator_results:
        decision = "FAIL — VALIDATOR VIOLATIONS"

    state = {
        "mode": "production_qualification",
        "configured_model": agent_config.get("model"),
        "actual_model": _actual_model(llm),
        "scenario_id": scenario["scenario_id"],
        "web_search": agent_config.get("web_search"),
        "verify_urls": agent_config.get("verify_urls"),
        "max_retry_limit": agent_config.get("max_retry_limit"),
        "budget": guard.budget,
        "generation_attempts": generation_attempts,
        "repair_requested": repair_requested,
        "repair_completed": repair_completed,
        "repair_blocked_by_budget": repair_blocked_by_budget,
        "http_attempts_used": http_attempts_used,
        "budget_exhausted": budget_exhausted,
        "exception_type": exception_type,
        "exception_message": exception_message,
        "validator_fails": len(validator_results),
        "decision_suggestion": decision,
        "attempt_log": guard.attempt_log,
        "provider_metadata": meta,
        "usage_reference": run_ref,
        "run_cost_total_usd": round(run_summary.total_cost, 6),
        "run_cost_per_call": run_summary.per_call,
        "run_tokens_total": run_summary.total_tokens,
        "run_accounting_status": run_summary.accounting_status,
        "run_expected_calls": run_summary.expected_count,
        "run_calls_recorded": run_summary.call_count,
        "hub_ack": hub_ack,
        "hub_results": hub_results,
        "hub_reconciliation": hub_reconciliation,
        "report": str(report_path),
        "artifact": str(artifact_path),
        "completed_at": _now(),
    }
    (run_dir / "run_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    # Release the usage context for this run so the next run gets a clean reference.
    clear_usage_context()

    # Report AI Usage logging status for this run (local log + Hub flush).
    if run_summary.call_count > 0:
        print(f"AI Usage local log: {run_summary.call_count} calls recorded.")
    else:
        print("WARNING: No matching AI Usage log entries found.", file=sys.stderr)

    print(f"Report written to: {report_path}")
    print(f"Artifact: {artifact_path}")
    print(f"Actual outbound LLM attempts: {guard.used} / {guard.budget}")
    print(f"Hub reconciliation: {hub_reconciliation['overall']}")
    print(f"Decision suggestion: {decision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
