from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.ai_usage import (
    clear_hub_post_callback,
    flush_usage_log,
    get_hub_post_callback,
    HubReceiptCollector,
    record_ai_usage,
    reconcile_hub_receipts,
    set_hub_post_callback,
    USAGE_LOG_PATH,
)


@pytest.fixture
def _temp_usage_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    # Drain any pending hub threads from previous tests so they don't
    # deliver into this test's callback.
    flush_usage_log(timeout=2.0)
    log_path = tmp_path / "llm_usage.jsonl"
    monkeypatch.setattr("src.ai_usage.USAGE_LOG_PATH", log_path)
    yield log_path


@pytest.fixture
def _enable_hub_post(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.ai_usage._read_hub_credentials", lambda: ("http://hub.test", "token"))


@pytest.fixture
def _fake_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: _Resp())


def _record_local(log_path: Any, request_id: str, reference: str = "run-1") -> None:
    import json
    from datetime import datetime, timezone
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "provider": "openrouter",
            "reference": reference,
            "request_id": request_id,
            "source": "campaign_strategy.generate",
            "cost_usd": 0.01,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False) + "\n")


def test_hub_callback_reports_success(_temp_usage_log: Any, _enable_hub_post: None, _fake_success: None) -> None:
    results: list[dict[str, Any]] = []
    set_hub_post_callback(results.append)
    record_ai_usage({
        "provider": "openrouter",
        "request_id": "req-ok",
        "cost_usd": 0.01,
    })
    assert flush_usage_log(timeout=2.0)
    clear_hub_post_callback()

    assert len(results) == 1
    assert results[0]["hub_status"] == "hub_delivered"
    assert results[0]["status_code"] == 202
    assert results[0]["request_id"] == "req-ok"


def test_hub_callback_reports_http_error(_temp_usage_log: Any, _enable_hub_post: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def _failing_post(*a, **k) -> Any:
        req = httpx.Request("POST", "http://hub.test")
        resp = httpx.Response(500)
        raise httpx.HTTPStatusError("server error", request=req, response=resp)

    monkeypatch.setattr(httpx.Client, "post", _failing_post)

    results: list[dict[str, Any]] = []
    set_hub_post_callback(results.append)
    record_ai_usage({
        "provider": "openrouter",
        "request_id": "req-500",
        "cost_usd": 0.01,
    })
    assert flush_usage_log(timeout=2.0)
    clear_hub_post_callback()

    assert len(results) == 1
    assert results[0]["hub_status"] == "hub_http_error"
    assert results[0]["status_code"] == 500
    assert results[0]["request_id"] == "req-500"
    assert "server error" in results[0]["error"]


def test_hub_callback_reports_transport_error(_temp_usage_log: Any, _enable_hub_post: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("no route")))

    results: list[dict[str, Any]] = []
    set_hub_post_callback(results.append)
    record_ai_usage({
        "provider": "openrouter",
        "request_id": "req-net",
        "cost_usd": 0.01,
    })
    assert flush_usage_log(timeout=2.0)
    clear_hub_post_callback()

    assert len(results) == 1
    assert results[0]["hub_status"] == "hub_transport_error"
    assert results[0]["request_id"] == "req-net"
    assert "no route" in results[0]["error"]


def test_hub_callback_restores_previous_callback() -> None:
    original = get_hub_post_callback()
    set_hub_post_callback(lambda x: x)
    before_collector = get_hub_post_callback()

    with HubReceiptCollector() as collector:
        record = [1]
        collector.results.append(record)

    assert get_hub_post_callback() is before_collector
    set_hub_post_callback(original)
    assert get_hub_post_callback() is original


def test_reconcile_delivered(_temp_usage_log: Any, _fake_success: None) -> None:
    _record_local(_temp_usage_log, "req-a")
    _record_local(_temp_usage_log, "req-b")

    receipts = [
        {"request_id": "req-a", "hub_status": "hub_delivered", "status_code": 202},
        {"request_id": "req-b", "hub_status": "hub_delivered", "status_code": 202},
    ]

    import json
    local: list[dict[str, Any]] = []
    with _temp_usage_log.open("r") as f:
        for line in f:
            local.append(json.loads(line))

    recon = reconcile_hub_receipts(local, receipts, flush_completed=True)
    assert recon["overall"] == "COMPLETE"
    assert recon["expected"] == 2
    assert recon["delivered_count"] == 2
    assert recon["missing_receipt"] == []
    assert recon["per_request"]["req-a"] == "delivered"
    assert recon["per_request"]["req-b"] == "delivered"


def test_reconcile_missing_receipt() -> None:
    local = [
        {"request_id": "req-missing", "provider": "openrouter", "cost_usd": 0.01},
    ]
    receipts: list[dict[str, Any]] = []

    recon = reconcile_hub_receipts(local, receipts, flush_completed=True)
    assert recon["overall"] == "INCOMPLETE"
    assert recon["missing_receipt"] == ["req-missing"]
    assert recon["per_request"]["req-missing"] == "missing_receipt"


def test_reconcile_duplicate_receipt() -> None:
    local = [
        {"request_id": "req-dup", "provider": "openrouter", "cost_usd": 0.01},
    ]
    receipts = [
        {"request_id": "req-dup", "hub_status": "hub_delivered"},
        {"request_id": "req-dup", "hub_status": "hub_delivered"},
    ]

    recon = reconcile_hub_receipts(local, receipts, flush_completed=True)
    assert recon["overall"] == "INCOMPLETE"
    assert recon["duplicate_receipt"] == ["req-dup"]


def test_reconcile_unrelated_receipt() -> None:
    local = [
        {"request_id": "req-local", "provider": "openrouter", "cost_usd": 0.01},
    ]
    receipts = [
        {"request_id": "req-local", "hub_status": "hub_delivered"},
        {"request_id": "req-other", "hub_status": "hub_delivered"},
    ]

    recon = reconcile_hub_receipts(local, receipts, flush_completed=True)
    assert recon["overall"] == "INCOMPLETE"
    assert recon["unrelated_receipt"] == ["req-other"]
    assert recon["per_request"]["req-other"] == "unrelated_receipt"


def test_reconcile_flush_timeout() -> None:
    local = [
        {"request_id": "req-to", "provider": "openrouter", "cost_usd": 0.01},
    ]
    receipts: list[dict[str, Any]] = []

    recon = reconcile_hub_receipts(local, receipts, flush_completed=False)
    assert recon["overall"] == "INCOMPLETE"
    assert recon["flush_timeout"] == ["req-to"]
    assert recon["per_request"]["req-to"] == "flush_timeout"


def test_no_secret_in_callback_result(_temp_usage_log: Any, _enable_hub_post: None, _fake_success: None) -> None:
    results: list[dict[str, Any]] = []
    set_hub_post_callback(results.append)
    record_ai_usage({
        "provider": "openrouter",
        "request_id": "req-safe",
        "cost_usd": 0.01,
    })
    assert flush_usage_log(timeout=2.0)
    clear_hub_post_callback()

    result = str(results)
    assert "x-service-token" not in result
    assert "token" not in result


def test_every_local_request_has_exactly_one_delivered_receipt() -> None:
    local = [
        {"request_id": f"req-{i}", "provider": "openrouter", "cost_usd": 0.01}
        for i in range(5)
    ]
    receipts = [
        {"request_id": f"req-{i}", "hub_status": "hub_delivered"}
        for i in range(5)
    ]

    recon = reconcile_hub_receipts(local, receipts, flush_completed=True)
    assert recon["overall"] == "COMPLETE"
    assert len(recon["per_request"]) == 5
    assert all(status == "delivered" for status in recon["per_request"].values())
