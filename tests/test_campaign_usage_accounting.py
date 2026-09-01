from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.ai_usage import record_ai_usage, USAGE_LOG_PATH
from src.evaluation.campaign_qualification import (
    aggregate_run_usage,
    make_run_reference,
    read_usage_log_for_reference,
    snapshot_usage_log_offset,
)


@pytest.fixture
def _temp_usage_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Route local usage logging to a temp file and disable Hub POST."""
    log_path = tmp_path / "llm_usage.jsonl"
    monkeypatch.setattr("src.ai_usage.USAGE_LOG_PATH", log_path)
    monkeypatch.setattr("src.ai_usage._read_hub_credentials", lambda: (None, None))
    return log_path


def _write_entry(log_path: Path, entry: dict[str, Any]) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**entry, "timestamp": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")


def test_make_run_reference_is_stable_and_run_scoped() -> None:
    ref = make_run_reference("run-2025", "A_product_only", kind="qualification")
    assert ref == "qualification:run-2025:A_product_only"
    assert "A_product_only" in ref
    assert "run-2025" in ref


def test_snapshot_offset_is_zero_for_missing_log(tmp_path: Path) -> None:
    missing = tmp_path / "missing.jsonl"
    assert snapshot_usage_log_offset(missing) == 0


def test_snapshot_offset_matches_existing_size(_temp_usage_log: Path) -> None:
    _write_entry(_temp_usage_log, {"provider": "x"})
    _write_entry(_temp_usage_log, {"provider": "x"})
    assert snapshot_usage_log_offset(_temp_usage_log) == _temp_usage_log.stat().st_size


def test_one_generation_call_aggregates(_temp_usage_log: Path) -> None:
    ref = make_run_reference("r1", "A")
    _write_entry(_temp_usage_log, {
        "provider": "openrouter",
        "reference": ref,
        "source": "campaign_strategy.generate",
        "cost_usd": 0.0123,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "request_id": "req-1",
    })
    offset = 0
    entries = read_usage_log_for_reference(_temp_usage_log, ref, offset)
    summary = aggregate_run_usage(entries, expected_count=1, reference=ref)

    assert summary.accounting_status == "COMPLETE"
    assert summary.call_count == 1
    assert round(summary.total_cost, 6) == 0.0123
    assert summary.total_prompt_tokens == 100
    assert summary.total_completion_tokens == 50
    assert summary.total_tokens == 150
    assert summary.per_call[0]["classification"] == "generation"
    assert summary.per_call[0]["request_id"] == "req-1"


def test_generation_plus_repair_aggregates(_temp_usage_log: Path) -> None:
    ref = make_run_reference("r2", "B")
    _write_entry(_temp_usage_log, {
        "provider": "openrouter",
        "reference": ref,
        "source": "campaign_strategy.generate",
        "cost_usd": 0.01,
        "prompt_tokens": 80,
        "completion_tokens": 40,
    })
    _write_entry(_temp_usage_log, {
        "provider": "openrouter",
        "reference": ref,
        "source": "campaign_strategy.repair",
        "cost_usd": 0.005,
        "prompt_tokens": 70,
        "completion_tokens": 30,
    })

    summary = aggregate_run_usage(
        read_usage_log_for_reference(_temp_usage_log, ref, 0),
        expected_count=2,
        reference=ref,
    )

    assert summary.accounting_status == "COMPLETE"
    assert summary.call_count == 2
    assert round(summary.total_cost, 6) == 0.015
    assert summary.total_prompt_tokens == 150
    assert summary.total_completion_tokens == 70
    assert summary.total_tokens == 220
    assert summary.per_call[0]["classification"] == "generation"
    assert summary.per_call[1]["classification"] == "repair"


def test_incomplete_when_count_mismatches(_temp_usage_log: Path) -> None:
    ref = make_run_reference("r3", "C")
    _write_entry(_temp_usage_log, {
        "provider": "openrouter",
        "reference": ref,
        "source": "campaign_strategy.generate",
        "cost_usd": 0.01,
        "prompt_tokens": 10,
        "completion_tokens": 5,
    })

    # Expect 2 calls but only 1 is in the log.
    summary = aggregate_run_usage(
        read_usage_log_for_reference(_temp_usage_log, ref, 0),
        expected_count=2,
        reference=ref,
    )

    assert summary.accounting_status == "INCOMPLETE"
    assert summary.call_count == 1
    assert summary.expected_count == 2


def test_byte_offset_isolates_runs(_temp_usage_log: Path) -> None:
    ref_old = make_run_reference("r-old", "X")
    _write_entry(_temp_usage_log, {
        "provider": "openrouter",
        "reference": ref_old,
        "source": "campaign_strategy.generate",
        "cost_usd": 0.1,
        "prompt_tokens": 1,
        "completion_tokens": 1,
    })
    offset = snapshot_usage_log_offset(_temp_usage_log)

    ref_new = make_run_reference("r-new", "Y")
    _write_entry(_temp_usage_log, {
        "provider": "openrouter",
        "reference": ref_new,
        "source": "campaign_strategy.generate",
        "cost_usd": 0.02,
        "prompt_tokens": 20,
        "completion_tokens": 10,
    })

    # Even if we ask for the old reference with the new offset, no entries should match.
    old_entries = read_usage_log_for_reference(_temp_usage_log, ref_old, offset)
    assert old_entries == []

    new_entries = read_usage_log_for_reference(_temp_usage_log, ref_new, offset)
    summary = aggregate_run_usage(new_entries, expected_count=1, reference=ref_new)
    assert summary.accounting_status == "COMPLETE"
    assert summary.total_cost == 0.02
    assert summary.total_tokens == 30


def test_record_ai_usage_writes_reference(_temp_usage_log: Path) -> None:
    ref = make_run_reference("r4", "D")
    record_ai_usage({
        "provider": "openrouter",
        "reference": ref,
        "source": "campaign_strategy.generate",
        "cost_usd": 0.007,
        "prompt_tokens": 10,
        "completion_tokens": 5,
    })

    entries = read_usage_log_for_reference(_temp_usage_log, ref, 0)
    assert len(entries) == 1
    assert entries[0]["reference"] == ref
    assert float(entries[0]["cost_usd"]) == 0.007
