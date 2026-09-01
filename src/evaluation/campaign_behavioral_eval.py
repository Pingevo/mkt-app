"""Behavioral scenario evaluation for the campaign strategy agent.

This harness tests whether the *model's actual output* satisfies behavioral
assertions under adversarial user pressure.  Deterministic validator verdicts
are recorded alongside, but passing the validator is not the same as passing
the behavioral scenario.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents.campaign_strategy import CampaignStrategyAgent
from ..config_loader import get_agent_config, load_config


@dataclass
class BehavioralAssertionResult:
    assertion_id: str
    kind: str
    passed: bool
    description: str
    severity: str
    detail: str = ""


@dataclass
class BehavioralRunRecord:
    scenario_id: str
    name: str
    description: str = ""
    quick_brief: str = ""
    product_context: dict[str, str] = field(default_factory=dict)
    instructions: dict[str, Any] = field(default_factory=dict)
    web_search: bool = False
    output: str = ""
    output_length: int = 0
    output_artifact_path: str = ""
    actual_model: str = ""
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    exception_type: str = ""
    contained_rejection: bool = False
    validator_verdicts: list[dict[str, Any]] = field(default_factory=list)
    all_validator_passed: bool = True
    assertion_results: list[BehavioralAssertionResult] = field(default_factory=list)
    all_assertions_passed: bool = True
    scenario_passed: bool = True
    non_goals: list[str] = field(default_factory=list)
    severity_if_lets_through: str = "release_blocking"
    contained_rejection_acceptable: bool = False
    stopped: bool = False
    stop_reason: str = ""
    latency_ms: int = 0


@dataclass
class BehavioralEvalResult:
    records: list[BehavioralRunRecord] = field(default_factory=list)
    total: int = 0
    passed: int = 0
    failed: int = 0
    model_calls: int = 0
    stopped: bool = False
    stop_reason: str = ""
    completed_at: str = ""


def load_scenarios(path: str | Path) -> list[dict[str, Any]]:
    """Load behavioral scenarios from a JSON plan and flatten the stage list."""
    with open(Path(path), "r", encoding="utf-8") as f:
        data = json.load(f)
    scenarios: list[dict[str, Any]] = []
    for stage in data.get("stages", []):
        for s in stage.get("scenarios", []):
            s["_stage_id"] = stage.get("stage_id", "1")
            scenarios.append(s)
    return scenarios


def _safe_filename(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", text)


def _provider_metadata(llm_client: Any) -> dict[str, Any]:
    raw = getattr(llm_client, "_last_raw_response", {}) or {}
    usage = raw.get("usage", {}) or {}
    return {
        "model": raw.get("model"),
        "finish_reason": raw.get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens") if isinstance(usage, dict) else None,
        "completion_tokens": usage.get("completion_tokens") if isinstance(usage, dict) else None,
        "total_tokens": usage.get("total_tokens") if isinstance(usage, dict) else None,
    }


def _actual_model(llm_client: Any) -> str:
    raw = getattr(llm_client, "_last_raw_response", {}) or {}
    return raw.get("model") or "actual_model_unknown"


class CampaignBehavioralEvalHarness:
    """Run behavioral scenarios and evaluate both validator and behavioral verdicts."""

    def __init__(
        self,
        scenarios: list[dict[str, Any]],
        llm_client: Any,
        config: dict[str, Any] | None = None,
        artifact_dir: str | Path | None = None,
        stop_on: set[str] | None = None,
        max_output_chars: int = 5000,
    ) -> None:
        self.scenarios = scenarios
        self.llm = llm_client
        self.config = config or load_config()
        self.stop_on = stop_on or {"critical"}
        self.max_output_chars = max_output_chars
        root = Path(__file__).resolve().parent.parent.parent
        self.artifact_dir = Path(artifact_dir) if artifact_dir else root / "output" / "eval" / "behavioral"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def _agent_for_scenario(self, scenario: dict[str, Any]) -> CampaignStrategyAgent:
        agent_config = dict(get_agent_config(self.config, "campaign_strategy"))
        agent_config["model"] = "openrouter/free"
        agent_config["web_search"] = bool(scenario.get("web_search", False))
        agent_config["max_retry_limit"] = 0  # generation-only for behavioral diagnosis
        instructions = dict(scenario.get("instructions", {}))
        return CampaignStrategyAgent(agent_config, self.llm, instructions=instructions)

    def _evaluate_assertion(
        self,
        assertion: dict[str, Any],
        output: str,
        validator_verdicts: list[dict[str, Any]],
        record: BehavioralRunRecord,
    ) -> BehavioralAssertionResult:
        kind = assertion.get("kind", "includes")
        assertion_id = assertion.get("assertion_id", "unknown")
        description = assertion.get("description", "")
        severity = assertion.get("severity", "release_blocking")

        if kind == "includes":
            pattern = assertion.get("pattern", "")
            found = bool(re.search(pattern, output, re.IGNORECASE))
            return BehavioralAssertionResult(
                assertion_id=assertion_id,
                kind=kind,
                passed=found,
                description=description,
                severity=severity,
                detail=f"pattern={pattern!r}, found={found}",
            )

        if kind == "excludes":
            pattern = assertion.get("pattern", "")
            found = bool(re.search(pattern, output, re.IGNORECASE))
            return BehavioralAssertionResult(
                assertion_id=assertion_id,
                kind=kind,
                passed=not found,
                description=description,
                severity=severity,
                detail=f"pattern={pattern!r}, found={found}",
            )

        if kind == "outcome":
            return self._evaluate_outcome_assertion(assertion, output)

        if kind == "validator_rule":
            rule = assertion.get("rule", "")
            expected = bool(assertion.get("expected", True))
            verdict = next((v for v in validator_verdicts if v.get("rule") == rule), None)
            if verdict is None:
                ok = not expected  # missing rule is a fail when we expect it to pass
                detail = f"rule={rule!r} not found in validator audit; expected={expected}"
            else:
                ok = bool(verdict.get("ok")) == expected
                detail = f"rule={rule!r}, ok={verdict.get('ok')}, expected={expected}"
            return BehavioralAssertionResult(
                assertion_id=assertion_id,
                kind=kind,
                passed=ok,
                description=description,
                severity=severity,
                detail=detail,
            )

        if kind == "contained_rejection":
            expected = bool(assertion.get("expected", False))
            passed = record.contained_rejection == expected
            return BehavioralAssertionResult(
                assertion_id=assertion_id,
                kind=kind,
                passed=passed,
                description=description,
                severity=severity,
                detail=f"contained_rejection={record.contained_rejection}, expected={expected}",
            )

        if kind == "min_length":
            value = int(assertion.get("value", 0))
            passed = len(output) >= value
            return BehavioralAssertionResult(
                assertion_id=assertion_id,
                kind=kind,
                passed=passed,
                description=description,
                severity=severity,
                detail=f"min={value}, actual={len(output)}",
            )

        if kind == "max_length":
            value = int(assertion.get("value", 0))
            passed = len(output) <= value
            return BehavioralAssertionResult(
                assertion_id=assertion_id,
                kind=kind,
                passed=passed,
                description=description,
                severity=severity,
                detail=f"max={value}, actual={len(output)}",
            )

        return BehavioralAssertionResult(
            assertion_id=assertion_id,
            kind=kind,
            passed=False,
            description=description,
            severity=severity,
            detail=f"unknown assertion kind: {kind}",
        )

    def _evaluate_outcome_assertion(
        self,
        assertion: dict[str, Any],
        output: str,
    ) -> BehavioralAssertionResult:
        """Outcome-aware assertion: the unsafe token may appear, but only outside a prohibited commitment context."""
        assertion_id = assertion.get("assertion_id", "unknown")
        description = assertion.get("description", "")
        severity = assertion.get("severity", "release_blocking")
        value_pattern = assertion.get("value_pattern", "")
        prohibited = assertion.get("prohibited_context_pattern", "")
        allowed = assertion.get("allowed_context_pattern", "")

        matches = list(re.finditer(value_pattern, output, re.IGNORECASE))
        if not matches:
            return BehavioralAssertionResult(
                assertion_id=assertion_id,
                kind="outcome",
                passed=True,
                description=description,
                severity=severity,
                detail=f"value_pattern={value_pattern!r} not found in output",
            )

        verdicts: list[str] = []
        failed = False
        for m in matches:
            start = max(0, m.start() - 80)
            end = min(len(output), m.end() + 80)
            window = output[start:end].replace("\n", " ")
            if allowed and re.search(allowed, window, re.IGNORECASE):
                verdicts.append(f"OK (allowed): ...{window}...")
            elif prohibited and re.search(prohibited, window, re.IGNORECASE):
                failed = True
                verdicts.append(f"FAIL (prohibited): ...{window}...")
            else:
                verdicts.append(f"OK (neutral): ...{window}...")

        return BehavioralAssertionResult(
            assertion_id=assertion_id,
            kind="outcome",
            passed=not failed,
            description=description,
            severity=severity,
            detail=" | ".join(verdicts)[:300],
        )

    def _run_one(self, scenario: dict[str, Any]) -> BehavioralRunRecord:
        import time

        scenario_id = scenario["scenario_id"]
        name = scenario.get("name", scenario_id)
        description = scenario.get("description", "")
        quick_brief = scenario.get("quick_brief", "")
        product_context = scenario.get("product_context", {})
        instructions = scenario.get("instructions", {})
        web_search = bool(scenario.get("web_search", False))
        non_goals = scenario.get("non_goals", [])
        severity_if_lets = scenario.get("severity_if_model_or_validator_lets_through", "release_blocking")
        assertions = scenario.get("expected_behavioral_assertions", [])

        contained_rejection_acceptable = bool(scenario.get("contained_rejection_acceptable", False))

        record = BehavioralRunRecord(
            scenario_id=scenario_id,
            name=name,
            description=description,
            quick_brief=quick_brief,
            product_context=product_context,
            instructions=instructions,
            web_search=web_search,
            non_goals=non_goals,
            severity_if_lets_through=severity_if_lets,
            contained_rejection_acceptable=contained_rejection_acceptable,
        )

        # Ensure the selected evidence manifest is visible in the prompt so the
        # agent's own validator can compare URLs against it.
        product_context_for_prompt = dict(product_context)
        selected = list(instructions.get("selected_evidence_urls", []))
        if selected:
            product_context_for_prompt["competitors"] = (
                product_context_for_prompt.get("competitors", "") + "\nSelected evidence manifest: " + " ".join(selected)
            )

        try:
            start = time.perf_counter()
            agent = self._agent_for_scenario(scenario)
            prompt = agent.build_prompt(product_context_for_prompt)
            output = agent.run(prompt, quick_brief=quick_brief)
            latency_ms = int((time.perf_counter() - start) * 1000)
            exception_type = ""
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            exception_type = type(exc).__name__
            # A ValueError from the validator is a contained refusal when the draft
            # itself is otherwise safe; other exceptions are provider/infra problems.
            if exception_type == "ValueError":
                record.contained_rejection = True
            # The draft output is what the model actually generated before validation rejected it.
            output = getattr(agent, "_last_draft_output", "") or ""

        # Re-audit the final or draft output to get validator verdicts.
        validator_verdicts: list[dict[str, Any]] = []
        if output and output.strip():
            try:
                audit = agent.audit_output(output)
                validator_verdicts = [{"rule": a.rule, "ok": a.ok, "reason": a.reason} for a in audit]
            except Exception:
                pass

        record.output = output[: self.max_output_chars]
        record.output_length = len(output)
        record.exception_type = exception_type
        record.latency_ms = latency_ms
        record.actual_model = _actual_model(self.llm)
        record.provider_metadata = _provider_metadata(self.llm)
        record.validator_verdicts = validator_verdicts
        record.all_validator_passed = all(v.get("ok", False) for v in validator_verdicts) if validator_verdicts else True

        assertion_results = [
            self._evaluate_assertion(a, output, validator_verdicts, record) for a in assertions
        ]
        record.assertion_results = assertion_results
        record.all_assertions_passed = all(a.passed for a in assertion_results)

        # A scenario passes when it produced non-empty output, all behavioral assertions pass,
        # and either the final output passes validators or the rejection was contained and allowed.
        # Provider/infrastructure exceptions are never contained rejections.
        has_allowed_contained = record.contained_rejection and record.contained_rejection_acceptable
        record.scenario_passed = (
            bool(output)
            and record.all_assertions_passed
            and (record.all_validator_passed or has_allowed_contained)
            and (not record.exception_type or has_allowed_contained)
        )

        # Save the full output artifact.
        if output:
            safe = _safe_filename(scenario_id)
            artifact_file = self.artifact_dir / f"{safe}.md"
            try:
                with open(artifact_file, "w", encoding="utf-8") as f:
                    f.write(output)
                try:
                    record.output_artifact_path = str(artifact_file.relative_to(Path(__file__).resolve().parent.parent.parent))
                except ValueError:
                    record.output_artifact_path = str(artifact_file)
            except Exception as exc:
                record.output_artifact_path = f"(failed to write {artifact_file}: {exc})"

        # Stop the batch if a critical scenario is not fully contained.
        if not record.scenario_passed and severity_if_lets in self.stop_on:
            record.stopped = True
            record.stop_reason = f"{scenario_id}: behavioral assertion failed with severity {severity_if_lets}"

        return record

    def run_all(self) -> BehavioralEvalResult:
        """Run every scenario sequentially."""
        result = BehavioralEvalResult(total=len(self.scenarios))
        for scenario in self.scenarios:
            if result.stopped:
                break
            record = self._run_one(scenario)
            result.records.append(record)
            if record.scenario_passed:
                result.passed += 1
            else:
                result.failed += 1
            if record.stopped:
                result.stopped = True
                result.stop_reason = record.stop_reason
        result.completed_at = datetime.now().isoformat()
        result.model_calls = len(result.records)
        return result

    def generate_report(self, result: BehavioralEvalResult, fmt: str = "markdown") -> str:
        """Return a human-readable report separating validator and behavioral verdicts."""
        if fmt == "markdown":
            return self._report_markdown(result)
        if fmt == "json":
            return json.dumps(
                {k: v for k, v in result.__dict__.items() if k != "records"},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        raise ValueError(f"unsupported report format: {fmt}")

    def _report_markdown(self, result: BehavioralEvalResult) -> str:
        lines: list[str] = []
        lines.append("# Campaign Strategy Behavioral Evaluation Report")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append(f"- Total scenarios: {result.total}")
        lines.append(f"- Behavioral pass: {result.passed}")
        lines.append(f"- Behavioral fail: {result.failed}")
        lines.append(f"- Model calls: {result.model_calls}")
        lines.append(f"- Stopped: {'yes' if result.stopped else 'no'} ({result.stop_reason or '-'})")
        lines.append(f"- Completed at: {result.completed_at}")
        lines.append("")
        lines.append(
            "**Note:** Validator verdicts and behavioral assertion verdicts are reported separately. "
            "Passing the deterministic validator does not imply a scenario passes its behavioral assertions, "
            "because the validator only checks guardrail rules while the assertions check what the model actually produced."
        )
        lines.append("")

        for record in result.records:
            lines.append(f"## {record.scenario_id}: {record.name}")
            lines.append("")
            lines.append(record.description)
            lines.append("")
            lines.append(f"- **Severity if let through:** {record.severity_if_lets_through}")
            lines.append(f"- **Web search:** {record.web_search}")
            lines.append(f"- **Quick brief:** {record.quick_brief}")
            lines.append(f"- **Actual model:** `{record.actual_model}`")
            meta = record.provider_metadata or {}
            if meta.get("prompt_tokens") is not None:
                lines.append(f"- **Tokens:** prompt={meta.get('prompt_tokens')}, completion={meta.get('completion_tokens')}, total={meta.get('total_tokens')}, finish_reason={meta.get('finish_reason') or '-'}")
            lines.append(f"- **Exception type:** {record.exception_type or 'none'}")
            lines.append(f"- **Contained rejection:** {record.contained_rejection} (acceptable: {record.contained_rejection_acceptable})")
            lines.append(f"- **Output length:** {record.output_length} chars")
            if record.output_artifact_path:
                lines.append(f"- **Output artifact:** `{record.output_artifact_path}`")
            lines.append(f"- **Scenario passed:** {record.scenario_passed}")
            lines.append(f"- **All behavioral assertions passed:** {record.all_assertions_passed}")
            lines.append(f"- **All validator rules passed:** {record.all_validator_passed}")
            lines.append("")

            if record.non_goals:
                lines.append("### Non-goals")
                lines.append("")
                for ng in record.non_goals:
                    lines.append(f"- {ng}")
                lines.append("")

            lines.append("### Behavioral Assertion Verdicts")
            lines.append("")
            lines.append("| assertion | kind | passed | severity | description | detail |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for a in record.assertion_results:
                passed = "yes" if a.passed else "no"
                desc = (a.description or "")[:80]
                detail = (a.detail or "")[:200]
                lines.append(f"| {a.assertion_id} | {a.kind} | {passed} | {a.severity} | {desc} | {detail} |")
            lines.append("")

            if record.validator_verdicts:
                lines.append("### Validator Verdicts")
                lines.append("")
                lines.append("| rule | ok | reason |")
                lines.append("| --- | --- | --- |")
                for v in record.validator_verdicts:
                    ok = "pass" if v.get("ok") else "fail"
                    reason = (v.get("reason") or "")[:120]
                    lines.append(f"| {v.get('rule')} | {ok} | {reason} |")
                lines.append("")

            if record.output:
                lines.append("### Output Preview (truncated)")
                lines.append("")
                lines.append(f"```text\n{record.output[:500]}\n```")
                lines.append("")

        return "\n".join(lines)
