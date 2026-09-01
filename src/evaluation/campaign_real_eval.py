"""Cost-efficient real-model Beta validation harness for Agent 3.

This module deliberately does **not** call any model unless the caller
explicitly passes a real ``LLMClient`` and sets ``dry_run=False``.
All heavy behaviour lives behind a small public interface:

    harness = CampaignRealEvalHarness(plan, llm_client, ...)
    result = harness.run_plan(stage=1)
    harness.generate_report(result, fmt="markdown")

The harness records model, prompt hash, config, effective context,
web-search setting, output, validation verdict, repair count,
selected evidence, latency, and API cost per run.  It enforces a hard
spending cap, resumes from previous runs, and produces a human-review
report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents.campaign_strategy import CampaignStrategyAgent
from ..config_loader import load_config, get_agent_config, get_env
from ..flow_context import set_usage_reference, set_usage_metadata, clear_usage_context
from ..llm_client import LLMClient
from .campaign_qualification import (
    aggregate_run_usage,
    read_usage_log_for_reference,
    snapshot_usage_log_offset,
)
from ..ai_usage import (
    HubReceiptCollector,
    USAGE_LOG_PATH,
    flush_usage_log,
    reconcile_hub_receipts,
)


try:
    from tests.fixtures.campaign_fixtures import CASES
except Exception:
    CASES = []


def _find_case(case_id: str, cases: list[dict] | None = None) -> dict | None:
    cases = cases or CASES
    for c in cases:
        if c.get("case_id") == case_id:
            return c
    return None


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _hash_config(config: dict[str, Any]) -> str:
    return _hash_text(json.dumps(config, sort_keys=True, default=str))


def _read_usage_log(reference: str) -> float:
    """Return the sum of cost_usd for the given reference from local usage log."""
    total = 0.0
    try:
        from ..ai_usage import USAGE_LOG_PATH
        if not USAGE_LOG_PATH.exists():
            return 0.0
        with open(USAGE_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("reference") != reference:
                    continue
                cost = entry.get("cost_usd")
                if cost is not None:
                    total += float(cost)
    except Exception:
        pass
    return total


def _read_usage_entry(reference: str) -> dict[str, Any] | None:
    """Return the last usage log entry for the given reference, or None."""
    try:
        from ..ai_usage import USAGE_LOG_PATH
        if not USAGE_LOG_PATH.exists():
            return None
        last: dict[str, Any] | None = None
        with open(USAGE_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("reference") == reference:
                    last = entry
        return last
    except Exception:
        return None


def _model_name_from_config(agent_config: dict[str, Any]) -> str:
    model = agent_config.get("model", "")
    if not model:
        try:
            from ..config_loader import load_config
            model = load_config().get("defaults", {}).get("model", "")
        except Exception:
            pass
    return model or "unknown"


@dataclass
class RunRecord:
    case_id: str
    run_index: int
    stage_id: str
    timestamp: str
    mode: str
    model: str
    actual_model: str
    web_search: bool
    prompt_hash: str
    config_hash: str
    run_key: str = ""
    output_artifact_path: str = ""
    effective_context: dict[str, Any] = field(default_factory=dict)
    quick_brief: str = ""
    instructions: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    output_length: int = 0
    validation_ok: bool = False
    first_validation_error: str = ""
    all_validation_errors: list[dict[str, Any]] = field(default_factory=list)
    exception_type: str = ""
    diagnosis_category: str = ""
    repair_count: int = 0
    generation_only: bool = False
    annotations: list[dict[str, Any]] = field(default_factory=list)
    selected_evidence_urls: list[str] = field(default_factory=list)
    latency_ms: int = 0
    cost_usd: float = 0.0
    cost_verified: bool = False
    accounting_status: str = "INCOMPLETE"
    expected_calls: int = 0
    recorded_calls: int = 0
    error: str = ""
    raw_usage: dict[str, Any] = field(default_factory=dict)
    raw_provider_metadata: dict[str, Any] = field(default_factory=dict)
    hub_status: str = "INCOMPLETE"
    hub_results: list[dict[str, Any]] = field(default_factory=list)
    hub_reconciliation: dict[str, Any] = field(default_factory=dict)
    stopped_by_budget: bool = False
    stopped_by_critical: bool = False
    stopped_by_medium: bool = False


@dataclass
class CaseResult:
    case_id: str
    stage_id: str
    n_runs: int
    runs: list[RunRecord] = field(default_factory=list)
    pass_count: int = 0
    fail_count: int = 0
    critical_hits: list[str] = field(default_factory=list)
    medium_hits: list[str] = field(default_factory=list)
    release_blocking_hits: list[str] = field(default_factory=list)
    format_protocol_hits: list[str] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0


@dataclass
class StageResult:
    stage_id: str
    description: str
    cases: list[CaseResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    stopped: bool = False
    stop_reason: str = ""


@dataclass
class PlanResult:
    plan: dict[str, Any]
    stages: list[StageResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    remaining_budget_usd: float = 0.0
    stopped: bool = False
    stop_reason: str = ""
    plan_kind: str = "unknown"
    partial: bool = False
    partial_reason: str = ""
    planned_runs: int = 0
    executed_runs: int = 0
    generation_only: bool = False
    completed_at: str = ""
    stopped: bool = False
    stop_reason: str = ""
    completed_at: str = ""


class CampaignRealEvalHarness:
    """Small, deep interface for real-model campaign validation.

    Callers supply a plan, an LLM client, and a budget cap.  The harness
    runs selected stages, records every run, and produces a report.
    """

    # Default rule classification — caller can override in the plan.
    DEFAULT_CRITICAL_RULES = [
        "product_identity_intact",
        "unauthorized_url_or_source",
        "no_bare_urls",
        "no_pseudo_tool_markup",
        "no_search_tag_sources",
        "forbid_tactics_enforced",
        "not_blank",
    ]
    DEFAULT_MEDIUM_RULES = [
        "budget_max_enforced",
        "discount_max_enforced",
        "no_guaranteed_numeric_targets_without_baseline",
        "benchmarks_cited_or_removed",
        "no_numeric_wholesale_margin_without_financials",
        "indicative_retail_promo_labelled",
        "cost_mechanics_pending_financial_validation",
        "conflict_acknowledged",
    ]

    def __init__(
        self,
        plan: dict[str, Any],
        llm_client: LLMClient | None,
        state_path: str | Path,
        report_path: str | Path,
        budget_cap: float,
        dry_run: bool = True,
        mode: str = "paid",
        config: dict[str, Any] | None = None,
        max_output_chars: int = 4000,
        artifact_dir: str | Path | None = None,
    ) -> None:
        self.plan = plan
        self.llm = llm_client
        self.state_path = Path(state_path)
        self.report_path = Path(report_path)
        self.budget_cap = float(budget_cap)
        self.dry_run = bool(dry_run)
        self.mode = mode
        self.max_output_chars = max_output_chars
        self.config = config or load_config()
        self.artifact_dir = Path(artifact_dir) if artifact_dir else Path(__file__).resolve().parent.parent.parent / "output" / "eval"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.critical_rules = set(plan.get("critical_rules", self.DEFAULT_CRITICAL_RULES))
        self.medium_rules = set(plan.get("medium_rules", self.DEFAULT_MEDIUM_RULES))
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"completed_runs": [], "total_cost_usd": 0.0}
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"completed_runs": [], "total_cost_usd": 0.0}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def _run_completed(self, run_key: str) -> bool:
        return run_key in set(self.state.get("completed_runs", []))

    def _mark_completed(self, run_key: str, cost: float) -> None:
        completed = list(self.state.get("completed_runs", []))
        if run_key not in completed:
            completed.append(run_key)
        self.state["completed_runs"] = completed
        self.state["total_cost_usd"] = float(self.state.get("total_cost_usd", 0.0)) + float(cost)
        self._save_state()

    def _spend_so_far(self) -> float:
        return float(self.state.get("total_cost_usd", 0.0))

    def _remaining_budget(self) -> float:
        return self.budget_cap - self._spend_so_far()

    def _agent_for_case(self, web_search: bool) -> CampaignStrategyAgent:
        agent_config = dict(get_agent_config(self.config, "campaign_strategy"))
        # Free preflight is non-web and forces a free model, never a paid fallback.
        if self.mode == "free_preflight":
            agent_config["web_search"] = False
            agent_config["model"] = "openrouter/free"
            agent_config["verify_urls"] = False
            agent_config.pop("tools", None)
            # Do not burn free tokens on repair attempts — record the first validation verdict.
            agent_config["max_retry_limit"] = 0
        else:
            agent_config["web_search"] = web_search
        instructions = self._load_agent_instructions()
        return CampaignStrategyAgent(agent_config, self.llm, instructions=instructions)

    def _load_agent_instructions(self) -> dict[str, Any]:
        path = Path(__file__).resolve().parent.parent.parent / "config" / "agent_instructions.json"
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("campaign_strategy", {})
        except Exception:
            return {}

    def _rule_from_error(self, error: str) -> str | None:
        """Extract the first known validator rule name from an error string."""
        if not error:
            return None
        all_rules = self.critical_rules | self.medium_rules
        for rule in sorted(all_rules, key=len, reverse=True):
            if rule in error:
                return rule
        return None

    def _classify_error(self, error: str) -> str:
        """Legacy severity classification for report tables."""
        rule = self._rule_from_error(error)
        if not rule:
            return "low"
        if rule in self.critical_rules:
            return "critical"
        if rule in self.medium_rules:
            return "medium"
        return "low"

    def _diagnosis_category(self, error: str, exception_type: str = "") -> str:
        """Map an error/exception to a batch-level diagnostic category."""
        if not error:
            return ""

        rule = self._rule_from_error(error) or ""

        # Pseudo-tool / protocol format failures are always mapped here, even if
        # the validator rule name is present in the error string.
        if rule == "no_pseudo_tool_markup" or re.search(
            r"(<tool_call\b|<\/tool_call>|\bweb_search\s*\(|\bgoogle\s*\(|User Safety:\s*)",
            error,
            re.IGNORECASE,
        ):
            return "protocol_or_format_failure"

        # Infrastructure / provider problems.
        if exception_type and exception_type not in ("ValueError",):
            return "provider_infrastructure_limitation"

        if rule in self.critical_rules:
            return "validator_critical"
        if rule in self.medium_rules:
            return "validator_release_blocking"

        # Any other validation failure is treated as release-blocking in
        # evaluation mode so the batch stops for diagnosis.
        return "validator_release_blocking"

    def _stops_batch(self, category: str) -> bool:
        """Free-preflight batches stop on any validator or protocol format failure."""
        return category in {
            "validator_critical",
            "validator_release_blocking",
            "protocol_or_format_failure",
        }

    def _run_one_case(
        self,
        case: dict[str, Any],
        stage_id: str,
        run_index: int,
        web_search: bool,
        estimated_cost: float,
    ) -> RunRecord:
        case_id = case["case_id"]
        context = case.get("context", {})
        quick_brief = case.get("quick_brief", "")
        instructions = case.get("instructions", {})

        # Build the agent first so we can fingerprint the exact intended model
        # and prompt/config before deciding whether to resume or spend budget.
        agent = self._agent_for_case(web_search)
        if instructions:
            agent.instructions = {**(agent.instructions or {}), **instructions}

        prompt = agent.build_prompt(context)
        prompt_hash = _hash_text(prompt)
        config_hash = _hash_config(agent.config)
        intended_model = _model_name_from_config(agent.config)

        # Free preflight must never call web search, regardless of the plan.
        if self.mode == "free_preflight":
            web_search = False

        # Resume key includes mode, intended model, hashes, and run identity.
        run_key = f"{self.mode}::{intended_model}::{prompt_hash}::{config_hash}::{case_id}::run_{run_index}"
        # Use the same unique key for usage logs so test/real runs don't share entries.
        run_ref = run_key

        # 1. Budget pre-flight: stop before any call if we cannot afford the estimate.
        # Free preflight estimates must be 0.0 and bypass this by design.
        if self.mode != "free_preflight" and self._remaining_budget() < estimated_cost:
            return RunRecord(
                case_id=case_id,
                run_index=run_index,
                stage_id=stage_id,
                timestamp=datetime.now().isoformat(),
                run_key=run_key,
                mode=self.mode,
                model=intended_model,
                actual_model="n/a",
                web_search=web_search,
                prompt_hash=prompt_hash,
                config_hash=config_hash,
                stopped_by_budget=True,
                error=f"insufficient budget (need ~{estimated_cost:.4f} USD, have {self._remaining_budget():.4f} USD)",
            )

        if self._run_completed(run_key) and not self.dry_run:
            # Resume path: a previous real run already exists; do not duplicate.
            # In dry-run we allow re-simulation for harness tests.
            return RunRecord(
                case_id=case_id,
                run_index=run_index,
                stage_id=stage_id,
                timestamp=datetime.now().isoformat(),
                run_key=run_key,
                mode=self.mode,
                model=intended_model,
                actual_model="n/a",
                web_search=web_search,
                prompt_hash=prompt_hash,
                config_hash=config_hash,
                error="already completed (resume)",
            )

        # 2. Run the agent, measuring latency and cost.
        start = time.perf_counter()
        actual_model = "n/a"
        exception_type = ""
        first_validation_error = ""
        all_validation_errors: list[dict[str, Any]] = []
        output = ""
        output_length = 0
        repair_count = 0
        annotations: list[dict[str, Any]] = []
        selected: list[str] = []
        raw_usage: dict[str, Any] = {}
        raw_provider_metadata: dict[str, Any] = {}
        cost_usd = 0.0
        cost_verified = False

        # Snapshot the local usage log before any call so we only read entries
        # that are appended during this run.
        usage_log_offset = snapshot_usage_log_offset(USAGE_LOG_PATH)

        with HubReceiptCollector() as hub_collector:
            try:
                set_usage_reference(run_ref)
                set_usage_metadata({
                    "case_id": case_id,
                    "stage_id": stage_id,
                    "run_index": run_index,
                    "mode": self.mode,
                    "web_search": web_search,
                    "dry_run": self.dry_run,
                })

                if self.dry_run:
                    output = "[DRY RUN: real model not called]"
                    validation_ok = True
                    latency_ms = 0
                else:
                    output = agent.run(prompt, quick_brief=quick_brief)
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    first_validation_error = agent._last_first_validation_error or ""
                    repair_count = getattr(agent, "_last_repair_count", 0)
                    annotations = list(getattr(agent, "_last_relevant_annotations", []) or [])
                    selected = list(getattr(agent, "_selected_evidence_urls", set()) or [])

                    # Re-audit the final output to collect every rule verdict.
                    if output:
                        audit = agent.audit_output(output)
                        all_validation_errors = [
                            {"rule": a.rule, "ok": a.ok, "reason": a.reason}
                            for a in audit
                        ]

                    validation_ok = not first_validation_error

                output_length = len(output)
                # actual model must come from the current raw response, not from the usage
                # log (which stores the intended/router model) and not from stale state.
                raw_response = getattr(self.llm, "_last_raw_response", {}) or {}
                actual_model = raw_response.get("model") or ""
                raw_usage = raw_response.get("usage", {}) or {}
                raw_provider_metadata = {
                    "provider": getattr(self.llm, "provider", None),
                    "model": actual_model,
                    "finish_reason": raw_response.get("finish_reason"),
                    "prompt_tokens": raw_usage.get("prompt_tokens") if isinstance(raw_usage, dict) else None,
                    "completion_tokens": raw_usage.get("completion_tokens") if isinstance(raw_usage, dict) else None,
                    "total_tokens": raw_usage.get("total_tokens") if isinstance(raw_usage, dict) else None,
                }

                # Cost and token counts are read from the usage log by reference and
                # byte offset, then aggregated across every call owned by this run.
                run_entries = read_usage_log_for_reference(USAGE_LOG_PATH, run_ref, usage_log_offset)
                # Expected calls: the initial generation plus every repair attempt.
                expected_calls = 1 + (repair_count or 0)
                run_summary = aggregate_run_usage(
                    run_entries,
                    expected_count=expected_calls,
                    reference=run_ref,
                )
                cost_usd = run_summary.total_cost
                cost_verified = run_summary.accounting_status == "COMPLETE"
                if run_summary.per_call:
                    last = run_summary.per_call[-1]
                    raw_provider_metadata["prompt_tokens"] = last.get("prompt_tokens")
                    raw_provider_metadata["completion_tokens"] = last.get("completion_tokens")
                    raw_provider_metadata["total_tokens"] = last.get("total_tokens")

                if not actual_model:
                    actual_model = "actual_model_unknown"

            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                exception_type = type(exc).__name__
                output = getattr(agent, "_last_draft_output", "") or ""
                output_length = len(output)
                first_validation_error = str(exc)
                validation_ok = False
                repair_count = getattr(agent, "_last_repair_count", 0)
                annotations = []
                selected = []

                # Re-audit the draft that was rejected to collect all rule verdicts.
                if output:
                    try:
                        audit = agent.audit_output(output)
                        all_validation_errors = [
                            {"rule": a.rule, "ok": a.ok, "reason": a.reason}
                            for a in audit
                        ]
                    except Exception:
                        pass

                # actual model must come from the current raw response, not the usage log.
                raw_response = getattr(self.llm, "_last_raw_response", {}) or {}
                actual_model = raw_response.get("model") or ""
                raw_usage = raw_response.get("usage", {}) or {}
                raw_provider_metadata = {
                    "provider": getattr(self.llm, "provider", None),
                    "model": actual_model,
                    "finish_reason": raw_response.get("finish_reason"),
                    "prompt_tokens": raw_usage.get("prompt_tokens") if isinstance(raw_usage, dict) else None,
                    "completion_tokens": raw_usage.get("completion_tokens") if isinstance(raw_usage, dict) else None,
                    "total_tokens": raw_usage.get("total_tokens") if isinstance(raw_usage, dict) else None,
                }

                # Aggregate every usage entry appended during this run.
                run_entries = read_usage_log_for_reference(USAGE_LOG_PATH, run_ref, usage_log_offset)
                expected_calls = 1 + (repair_count or 0)
                run_summary = aggregate_run_usage(
                    run_entries,
                    expected_count=expected_calls,
                    reference=run_ref,
                )
                cost_usd = run_summary.total_cost
                cost_verified = run_summary.accounting_status == "COMPLETE"
                if run_summary.per_call:
                    last = run_summary.per_call[-1]
                    raw_provider_metadata["prompt_tokens"] = last.get("prompt_tokens")
                    raw_provider_metadata["completion_tokens"] = last.get("completion_tokens")
                    raw_provider_metadata["total_tokens"] = last.get("total_tokens")

                if not actual_model:
                    actual_model = "actual_model_unknown"

                # Free-preflight infrastructure failures (API/rate-limit) must not fall back
                # to a paid model.  Validation ValueErrors are normal model defects, not
                # infrastructure, so we keep the actual model name for diagnosis.
                if self.mode == "free_preflight" and not isinstance(exc, ValueError):
                    actual_model = "provider_infrastructure_limitation"

            finally:
                hub_ack = flush_usage_log(timeout=5.0)
                clear_usage_context()

        hub_reconciliation = reconcile_hub_receipts(run_entries, hub_collector.results, flush_completed=hub_ack)

        # 3. Save full output to disk so the report can link to it.
        output_artifact_path = ""
        if output:
            safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", run_key)
            artifact_file = self.artifact_dir / f"{safe_key}.md"
            try:
                self.artifact_dir.mkdir(parents=True, exist_ok=True)
                with open(artifact_file, "w", encoding="utf-8") as f:
                    f.write(output)
                try:
                    output_artifact_path = str(artifact_file.relative_to(Path(__file__).resolve().parent.parent.parent))
                except ValueError:
                    output_artifact_path = str(artifact_file)
            except Exception:
                output_artifact_path = f"(failed to write {artifact_file})"

        # 4. Record everything.
        record = RunRecord(
            case_id=case_id,
            run_index=run_index,
            stage_id=stage_id,
            timestamp=datetime.now().isoformat(),
            run_key=run_key,
            mode=self.mode,
            model=intended_model,
            actual_model=actual_model,
            web_search=web_search,
            prompt_hash=prompt_hash,
            config_hash=config_hash,
            output_artifact_path=output_artifact_path,
            effective_context=dict(context),
            quick_brief=quick_brief,
            instructions=dict(instructions),
            output=output[: self.max_output_chars],
            output_length=output_length,
            validation_ok=validation_ok and not bool(first_validation_error),
            first_validation_error=first_validation_error,
            all_validation_errors=all_validation_errors,
            exception_type=exception_type,
            diagnosis_category=self._diagnosis_category(first_validation_error, exception_type),
            repair_count=repair_count,
            generation_only=bool(agent.config.get("max_retry_limit") == 0),
            annotations=annotations,
            selected_evidence_urls=selected,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            cost_verified=cost_verified,
            accounting_status=run_summary.accounting_status,
            expected_calls=run_summary.expected_count,
            recorded_calls=run_summary.call_count,
            error="" if not first_validation_error else first_validation_error,
            raw_usage=raw_usage,
            raw_provider_metadata=raw_provider_metadata,
        )

        record.hub_status = hub_reconciliation["overall"]
        record.hub_results = hub_collector.results
        record.hub_reconciliation = hub_reconciliation

        if not self.dry_run:
            self._mark_completed(run_key, cost_usd)

        return record

    def _case_result(
        self,
        case: dict[str, Any],
        stage_id: str,
        n_runs: int,
        web_search: bool,
        estimated_cost: float,
    ) -> CaseResult:
        case_id = case["case_id"]
        result = CaseResult(case_id=case_id, stage_id=stage_id, n_runs=n_runs)

        for i in range(1, n_runs + 1):
            run = self._run_one_case(case, stage_id, i, web_search, estimated_cost)
            result.runs.append(run)

            if run.stopped_by_budget:
                return result

            result.total_cost_usd += run.cost_usd
            result.total_latency_ms += run.latency_ms

            if run.validation_ok:
                result.pass_count += 1
            else:
                result.fail_count += 1

            if run.first_validation_error:
                category = self._diagnosis_category(run.first_validation_error, run.exception_type)
                run.diagnosis_category = category
                rule = self._rule_from_error(run.first_validation_error) or "unknown"

                if category == "protocol_or_format_failure":
                    run.stopped_by_critical = True
                    result.format_protocol_hits.append(rule)
                    return result

                if category == "validator_critical" or rule in self.critical_rules:
                    run.stopped_by_critical = True
                    result.critical_hits.append(rule)
                    return result

                if category == "validator_release_blocking" or rule in self.medium_rules:
                    run.stopped_by_medium = True
                    result.release_blocking_hits.append(rule)
                    result.medium_hits.append(rule)
                    # Stop further runs for this case and move to the next.
                    return result

        return result

    def _stage_result(self, stage: dict[str, Any]) -> StageResult:
        stage_id = stage["stage_id"]
        description = stage.get("description", "")
        cases = stage.get("cases", [])
        result = StageResult(stage_id=stage_id, description=description)

        for item in cases:
            case_id = item["case_id"]
            case = _find_case(case_id) or item.get("case_data")
            if not case:
                result.cases.append(CaseResult(
                    case_id=case_id,
                    stage_id=stage_id,
                    n_runs=0,
                    runs=[RunRecord(
                        case_id=case_id,
                        run_index=0,
                        stage_id=stage_id,
                        timestamp=datetime.now().isoformat(),
                        run_key=f"{self.mode}::unknown::::{case_id}::run_0",
                        mode=self.mode,
                        model="n/a",
                        actual_model="n/a",
                        web_search=False,
                        prompt_hash="",
                        config_hash="",
                        error=f"case {case_id} not found in fixtures",
                    )],
                ))
                continue

            n_runs = int(item.get("n_runs", 1))
            web_search = bool(item.get("web_search", False))
            estimated_cost = float(item.get("estimated_cost_usd", 0.0))

            case_result = self._case_result(case, stage_id, n_runs, web_search, estimated_cost)
            result.cases.append(case_result)
            result.total_cost_usd += case_result.total_cost_usd

            # Halt the entire stage on critical or protocol/format defects.
            # In free-preflight/evaluation mode, release-blocking failures also stop
            # the batch so the output can be diagnosed before more cases are burned.
            if case_result.critical_hits:
                result.stopped = True
                result.stop_reason = f"critical defect: {case_result.critical_hits[0]}"
                return result
            if case_result.format_protocol_hits:
                result.stopped = True
                result.stop_reason = f"protocol/format failure: {case_result.format_protocol_hits[0]}"
                return result
            if self.mode == "free_preflight" and case_result.release_blocking_hits:
                result.stopped = True
                result.stop_reason = f"release-blocking defect: {case_result.release_blocking_hits[0]}"
                return result

        return result

    def run_plan(self, stage: int | str | None = None) -> PlanResult:
        """Run selected stages and return a structured result.

        ``stage`` may be an index (0..N) or a ``stage_id`` string.
        ``None`` runs all stages in order.
        """
        stages = self.plan.get("stages", [])
        selected = list(range(len(stages)))
        if stage is not None:
            if isinstance(stage, int):
                selected = [stage]
            else:
                selected = [i for i, s in enumerate(stages) if s.get("stage_id") == stage]

        result = PlanResult(plan=self.plan, remaining_budget_usd=self._remaining_budget())
        result.plan_kind = self.plan.get("plan_kind", self.plan.get("name", "unknown"))
        result.partial = bool(self.plan.get("partial"))
        result.partial_reason = self.plan.get("partial_reason", "")
        result.planned_runs = sum(
            int(c.get("n_runs", 1))
            for s in self.plan.get("stages", [])
            for c in s.get("cases", [])
        )
        result.generation_only = self.mode == "free_preflight"

        for idx in selected:
            if result.stopped:
                break
            if idx < 0 or idx >= len(stages):
                continue
            stage_result = self._stage_result(stages[idx])
            result.stages.append(stage_result)
            result.total_cost_usd += stage_result.total_cost_usd

            if stage_result.stopped:
                result.stopped = True
                result.stop_reason = stage_result.stop_reason

        result.executed_runs = sum(
            len(c.runs) for s in result.stages for c in s.cases
        )
        result.remaining_budget_usd = self._remaining_budget()
        result.completed_at = datetime.now().isoformat()
        return result

    def generate_report(self, result: PlanResult, fmt: str = "markdown") -> str:
        """Return a human-readable report from a plan result."""
        if fmt == "markdown":
            return self._report_markdown(result)
        if fmt == "json":
            return json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str)
        raise ValueError(f"unsupported report format: {fmt}")

    def _report_markdown(self, result: PlanResult) -> str:
        lines: list[str] = []
        lines.append("# Campaign Strategy Real-Model Evaluation Report")
        lines.append("")
        if self.mode == "free_preflight":
            lines.append("## FREE PREFLIGHT — NOT PRODUCTION CERTIFICATION")
            lines.append(
                "This report was generated using a free model. "
                "It may be used for prompt/validator/repair preflight, "
                "but it cannot satisfy the production Beta gate."
            )
            lines.append("")

        # Plan integrity / execution provenance.
        lines.append("## Execution Provenance")
        lines.append("")
        lines.append(f"- Plan kind: **{result.plan_kind}**")
        lines.append(f"- Mode: **{self.mode}**")
        lines.append(f"- Partial run: **{'yes' if result.partial else 'no'}**")
        if result.partial:
            lines.append(f"- Partial reason: {result.partial_reason}")
        lines.append(f"- Planned runs: {result.planned_runs}")
        lines.append(f"- Executed runs: {result.executed_runs}")
        lines.append(f"- Generation-only (no repair): **{'yes' if result.generation_only else 'no'}**")
        lines.append(f"- Completed at: {result.completed_at}")
        lines.append(f"- Budget cap: ${self.budget_cap:.4f} USD")
        lines.append(f"- Spent so far: ${result.total_cost_usd:.4f} USD")
        lines.append(f"- Remaining: ${result.remaining_budget_usd:.4f} USD")
        lines.append(f"- Stopped: {'yes' if result.stopped else 'no'} ({result.stop_reason or '-' })")
        lines.append("")

        # Per-run detail with artifact links.
        lines.append("## Per-Run Detail")
        lines.append("")
        for stage in result.stages:
            for case in stage.cases:
                for run in case.runs:
                    lines.append(f"### {run.case_id} — run {run.run_index} (stage {stage.stage_id})")
                    lines.append("")
                    lines.append(f"- **Run key:** `{run.run_key}`")
                    lines.append(f"- **Intended model:** `{run.model}`")
                    lines.append(f"- **Actual model:** `{run.actual_model}`")
                    lines.append(f"- **Web search:** {run.web_search}")
                    lines.append(f"- **Validation OK:** {run.validation_ok}")
                    lines.append(f"- **Exception type:** {run.exception_type or 'none'}")
                    lines.append(f"- **Diagnosis category:** {run.diagnosis_category or 'n/a'}")
                    lines.append(f"- **Repair count:** {run.repair_count}")
                    lines.append(f"- **Latency (ms):** {run.latency_ms}")
                    lines.append(f"- **Cost (USD):** ${run.cost_usd:.4f} ({'verified' if run.cost_verified else 'not verified'})")
                    lines.append(f"- **Output length:** {run.output_length} chars")
                    if run.output_artifact_path:
                        lines.append(f"- **Full output artifact:** `{run.output_artifact_path}`")
                    lines.append("")

                    if run.first_validation_error:
                        lines.append("**First validation error:**")
                        lines.append(f"```text\n{run.first_validation_error[:500]}\n```")
                        lines.append("")

                    if run.all_validation_errors:
                        lines.append("**All validator verdicts:**")
                        lines.append("")
                        lines.append("| rule | ok | reason |")
                        lines.append("| --- | --- | --- |")
                        for v in run.all_validation_errors:
                            ok = "pass" if v.get("ok") else "fail"
                            reason = (v.get("reason") or "")[:120]
                            lines.append(f"| {v.get('rule')} | {ok} | {reason} |")
                        lines.append("")

                    if run.raw_provider_metadata:
                        lines.append("**Raw provider metadata:**")
                        lines.append(f"```json\n{json.dumps(run.raw_provider_metadata, ensure_ascii=False, indent=2, default=str)}\n```")
                        lines.append("")

                    if run.output:
                        preview = run.output[:500]
                        lines.append("**Output preview (truncated):**")
                        lines.append(f"```text\n{preview}\n```")
                        lines.append("")

        # Defect frequency table.
        category_hits: dict[str, int] = {}
        rule_hits: dict[str, int] = {}
        for stage in result.stages:
            for case in stage.cases:
                for run in case.runs:
                    if run.diagnosis_category:
                        category_hits[run.diagnosis_category] = category_hits.get(run.diagnosis_category, 0) + 1
                    if run.first_validation_error:
                        rule = self._rule_from_error(run.first_validation_error) or "unknown"
                        rule_hits[rule] = rule_hits.get(rule, 0) + 1

        if rule_hits:
            lines.append("## First-Validation-Error Frequency")
            lines.append("")
            lines.append("| rule | count |")
            lines.append("| --- | --- |")
            for rule, count in sorted(rule_hits.items(), key=lambda x: -x[1]):
                lines.append(f"| {rule} | {count} |")
            lines.append("")

        if category_hits:
            lines.append("## Diagnosis Category Frequency")
            lines.append("")
            lines.append("| category | count |")
            lines.append("| --- | --- |")
            for cat, count in sorted(category_hits.items(), key=lambda x: -x[1]):
                lines.append(f"| {cat} | {count} |")
            lines.append("")

        # Stage summary.
        for stage in result.stages:
            lines.append(f"## Stage {stage.stage_id}: {stage.description}")
            lines.append("")
            lines.append("| case | n_runs | pass | fail | cost (USD) | avg latency (ms) | first hit |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
            for case in stage.cases:
                first_hit = ""
                if case.critical_hits:
                    first_hit = f"CRITICAL: {case.critical_hits[0]}"
                elif case.format_protocol_hits:
                    first_hit = f"PROTOCOL: {case.format_protocol_hits[0]}"
                elif case.release_blocking_hits:
                    first_hit = f"RELEASE-BLOCKING: {case.release_blocking_hits[0]}"
                n = len(case.runs)
                avg_lat = int(case.total_latency_ms / n) if n else 0
                lines.append(
                    f"| {case.case_id} | {case.n_runs} | {case.pass_count} | {case.fail_count} | "
                    f"${case.total_cost_usd:.4f} | {avg_lat} | {first_hit} |"
                )
            lines.append("")
            if stage.stopped:
                lines.append(f"**Stage stopped: {stage.stop_reason}**")
                lines.append("")

        # Recommendation.
        lines.append("## Recommendation")
        lines.append("")
        if self.mode == "free_preflight":
            if result.stopped:
                lines.append("- **Action (free preflight):** stop. Fix the reported defect offline, then re-run on a production model before any Beta claim.")
            elif category_hits:
                lines.append("- **Action (free preflight):** free model found defects. Fix offline, then re-run on a production model. Do not combine free-model results with production certification.")
            else:
                lines.append("- **Action (free preflight):** free model passed these cases. This is NOT a production Beta gate. Schedule paid production validation before certification.")
        else:
            if result.stopped:
                lines.append("- **Action:** stop real-credit testing. Fix the defect offline, then re-run the affected case(s).")
            elif category_hits:
                lines.append("- **Action:** some release-blocking defects found. Fix offline and re-run the affected cases; do not proceed to wider Beta until the defect rate is zero.")
            else:
                lines.append("- **Action:** no release-blocking defects. Continue to the next stage if budget remains.")
        lines.append("")

        return "\n".join(lines)
