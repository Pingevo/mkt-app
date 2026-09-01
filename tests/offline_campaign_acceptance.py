"""Offline acceptance runner for Agent 3 (campaign_strategy).

Reads version-controlled fixtures (no API calls) and produces a per-rule
verdict report.  Usage:

    python tests/offline_campaign_acceptance.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.campaign_validator import (
    audit_campaign_output,
    extract_context_flags,
    validate_campaign_output,
)
from src.config_loader import get_agent_config, load_config
from tests.fixtures.campaign_fixtures import CASES


def _load_rules() -> dict[str, Any]:
    cfg = load_config()
    return dict(get_agent_config(cfg, "campaign_strategy")).get("semantic_rules", {})


def _run_case(case: dict, rules: dict[str, Any]) -> dict[str, Any]:
    flags = extract_context_flags(case["context"])
    instructions = {**case.get("instructions", {}), "quick_brief": case.get("quick_brief", "")}
    results = audit_campaign_output(case["output"], flags, instructions, rules)
    ok, first_error = validate_campaign_output(case["output"], flags, instructions, rules)
    return {
        "case_id": case["case_id"],
        "description": case["description"],
        "expected_ok": case["expected_ok"],
        "actual_ok": ok,
        "first_error": first_error,
        "rule_verdicts": [
            {"rule": r.rule, "ok": r.ok, "reason": r.reason}
            for r in results
        ],
    }


def run_acceptance() -> dict[str, Any]:
    """Run the offline acceptance suite and return the full report."""
    rules = _load_rules()
    report: list[dict[str, Any]] = []
    all_match = True
    for case in CASES:
        result = _run_case(case, rules)
        report.append(result)
        if result["expected_ok"] != result["actual_ok"]:
            all_match = False

    return {
        "total": len(report),
        "all_match": all_match,
        "cases": report,
    }


def main() -> None:
    summary = run_acceptance()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["all_match"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
