"""Pytest wrapper for the offline Agent 3 acceptance runner."""

from __future__ import annotations

from tests.offline_campaign_acceptance import run_acceptance


def test_offline_acceptance_suite_matches_expected():
    summary = run_acceptance()
    mismatched = [
        c for c in summary["cases"] if c["expected_ok"] != c["actual_ok"]
    ]
    assert summary["all_match"], f"mismatched cases: {mismatched}"
