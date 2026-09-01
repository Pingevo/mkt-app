"""Evaluation-only tooling for a capped production qualification run.

This module does NOT change production agent behavior. It wraps
``httpx.Client.post`` and ``httpx.Client.stream`` during the qualification
context so that a single S07 production run cannot consume more than the
configured number of outbound paid LLM API attempts.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx


class QualificationBudgetExhausted(Exception):
    """Raised before an outbound API attempt that would exceed the qualification budget."""

    def __init__(self, attempts: int, next_request: str, budget: int) -> None:
        self.attempts = attempts
        self.next_request = next_request
        self.budget = budget
        super().__init__(
            f"Qualification API budget exhausted: {attempts}/{budget} attempts used, "
            f"blocking {next_request}"
        )


class BudgetGuard:
    """Context manager that hard-caps outbound httpx ``post``/``stream`` attempts.

    Use as an evaluation-only wrapper around ``CampaignStrategyAgent.run``:

    .. code-block:: python

        with BudgetGuard(budget=2) as guard:
            output = agent.run(user_prompt, quick_brief=quick_brief)

    The original ``httpx.Client.post`` and ``httpx.Client.stream`` are restored
    on exit (even when an exception is raised).
    """

    def __init__(self, budget: int = 2) -> None:
        if budget < 1:
            raise ValueError("qualification budget must be >= 1")
        self.budget = budget
        self.used = 0
        self.attempt_log: list[dict[str, Any]] = []
        self._original_post = httpx.Client.post
        self._original_stream = httpx.Client.stream

    def __enter__(self) -> "BudgetGuard":
        guard = self

        def _guarded_post(client: httpx.Client, url: str, **kwargs: Any) -> Any:
            if not guard._is_llm_request(url):
                return guard._original_post(client, url, **kwargs)
            guard._consume(url, "POST")
            try:
                result = guard._original_post(client, url, **kwargs)
                guard._record_result("sent")
                return result
            except Exception as exc:
                guard._record_result("error", f"{type(exc).__name__}: {exc}")
                raise

        def _guarded_stream(client: httpx.Client, method: str, url: str, **kwargs: Any) -> Any:
            if not guard._is_llm_request(url):
                return guard._original_stream(client, method, url, **kwargs)
            guard._consume(url, method)
            try:
                result = guard._original_stream(client, method, url, **kwargs)
                guard._record_result("sent")
                return result
            except Exception as exc:
                guard._record_result("error", f"{type(exc).__name__}: {exc}")
                raise

        httpx.Client.post = _guarded_post
        httpx.Client.stream = _guarded_stream
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        httpx.Client.post = self._original_post
        httpx.Client.stream = self._original_stream

    def _is_llm_request(self, url: str) -> bool:
        return url.endswith("/chat/completions") or "/chat/completions" in url

    def _consume(self, url: str, method: str = "POST") -> int:
        entry = {
            "timestamp": time.time(),
            "method": method,
            "url": url,
            "status": "pending",
            "error": None,
        }
        if self.used >= self.budget:
            entry["status"] = "blocked"
            self.attempt_log.append(entry)
            raise QualificationBudgetExhausted(self.used, url, self.budget)
        self.attempt_log.append(entry)
        self.used += 1
        return self.used

    def _record_result(self, status: str, error: str | None = None) -> None:
        if self.attempt_log:
            self.attempt_log[-1]["status"] = status
            self.attempt_log[-1]["error"] = error


@dataclass
class RunUsageSummary:
    """Aggregated cost/tokens for a qualification run."""

    per_call: list[dict[str, Any]] = field(default_factory=list)
    total_cost: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0
    expected_count: int = 0
    accounting_status: str = "INCOMPLETE"
    reference: str = ""

def _parse_timestamp(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def make_run_reference(run_id: str, scenario_id: str, *, kind: str = "qualification") -> str:
    """Return a stable, run-scoped reference for usage log correlation."""
    return f"{kind}:{run_id}:{scenario_id}"


def snapshot_usage_log_offset(log_path: Path) -> int:
    """Return the current byte size of the usage log (0 if not yet created)."""
    if not log_path.exists():
        return 0
    return log_path.stat().st_size


def read_usage_log_for_reference(
    log_path: Path,
    reference: str,
    start_offset: int,
) -> list[dict[str, Any]]:
    """Return local usage entries for a run reference starting from a byte offset.

    Reads only lines appended after the snapshot offset.  This avoids
    timezone drift and stale correlation from earlier runs.
    """
    entries: list[dict[str, Any]] = []
    if not log_path.exists():
        return entries
    with log_path.open("r", encoding="utf-8") as f:
        f.seek(start_offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("reference") == reference:
                entries.append(entry)
    return entries


def read_usage_log_for_run(
    log_path: Path,
    start: datetime,
    end: datetime,
    source_prefix: str,
) -> list[dict[str, Any]]:
    """Return local usage entries owned by a qualification run.

    Filters by time window and source prefix.  Unrelated or out-of-window
    entries are excluded.
    """
    entries: list[dict[str, Any]] = []
    if not log_path.exists():
        return entries
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_timestamp(entry.get("timestamp", ""))
            if ts is None or ts < start or ts > end:
                continue
            source = entry.get("source", "")
            if not isinstance(source, str) or not source.startswith(source_prefix):
                continue
            entries.append(entry)
    return entries


def classify_source(source: str) -> str:
    """Classify a usage source as one of generation/repair/retry/other."""
    if not isinstance(source, str):
        return "other"
    if ".repair" in source:
        return "repair"
    if ".review" in source:
        return "review"
    if ".generate" in source:
        return "generation"
    if ".retry" in source:
        return "retry"
    return "other"


def aggregate_run_usage(
    entries: list[dict[str, Any]],
    *,
    expected_count: int = 0,
    reference: str = "",
) -> RunUsageSummary:
    """Aggregate a list of run-owned usage entries into a summary.

    If ``expected_count`` is provided, the accounting is marked COMPLETE only
    when the number of local entries equals the expected number of outbound
    attempts.  Otherwise it remains INCOMPLETE.
    """
    per_call: list[dict[str, Any]] = []
    total_cost = 0.0
    total_prompt = 0
    total_completion = 0
    total = 0
    for e in entries:
        prompt = int(e.get("prompt_tokens") or 0)
        completion = int(e.get("completion_tokens") or 0)
        call_total = prompt + completion
        cost = float(e.get("cost_usd") or 0.0)
        call = {
            "request_id": e.get("request_id"),
            "source": e.get("source"),
            "classification": classify_source(e.get("source", "")),
            "status": e.get("status"),
            "cost_usd": cost,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": call_total,
        }
        per_call.append(call)
        total_cost += cost
        total_prompt += prompt
        total_completion += completion
        total += call_total

    call_count = len(entries)
    accounting_status = "COMPLETE" if (expected_count > 0 and call_count == expected_count) else "INCOMPLETE"
    if expected_count <= 0:
        accounting_status = "INCOMPLETE"

    return RunUsageSummary(
        per_call=per_call,
        total_cost=total_cost,
        total_prompt_tokens=total_prompt,
        total_completion_tokens=total_completion,
        total_tokens=total,
        call_count=call_count,
        expected_count=expected_count,
        accounting_status=accounting_status,
        reference=reference,
    )


def _extract_cited_urls(output: str, raw_annotations: list[dict], selected_evidence_urls: list[str]) -> list[str]:
    """Return the URLs that are actually cited in the final output and
    were either returned by the live search or pre-selected in the prompt."""
    allowed = {a.get("url", "").lower().rstrip("/") for a in raw_annotations if a.get("url")}
    allowed |= {u.lower().rstrip("/") for u in selected_evidence_urls}
    found: set[str] = set()
    for m in re.finditer(r"\[([^\]]+)\]\(([^ )]+)\)", output):
        found.add(m.group(2).lower().rstrip("/"))
    for m in re.finditer(r"https?://[^\s\)\]\]\[]+", output):
        found.add(m.group(0).lower().rstrip("/"))
    return sorted(u for u in found if u in allowed)


def evaluate_gate3_behavior(
    output: str,
    raw_annotations: list[dict],
    selected_evidence_urls: list[str],
    quick_brief: str,
) -> tuple[bool, str]:
    """Behavioral check: when the user asks for competitor pricing and live
    search returns price evidence, the final answer must cite it.
    """
    if not quick_brief or ("ราคา" not in quick_brief and "price" not in quick_brief.lower()):
        return True, ""
    if not raw_annotations:
        return True, "No web evidence returned"
    price_re = re.compile(r"\d{1,3}(?:,\d{3})+\s*[฿$€£¥]|\d+\s*[฿$€£¥]|฿\s*\d{1,3}(?:,\d{3})+")
    has_price = any(price_re.search(a.get("content", "") + " " + (a.get("title") or "")) for a in raw_annotations)
    if not has_price:
        return True, "Returned web evidence contains no price"
    if "ไม่พบข้อมูลแหล่งอ้างอิง" in output or "no external evidence" in output.lower():
        return False, "Final answer denies evidence that was actually returned"
    cited = _extract_cited_urls(output, raw_annotations, selected_evidence_urls)
    if not cited:
        return False, "Final answer did not cite any selected evidence URL"
    return True, ""
