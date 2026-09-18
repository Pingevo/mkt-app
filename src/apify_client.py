"""Apify actor runner — Python port of sellcenter's ApifyRunService.js.

Runs an actor to completion and returns its dataset items together with the
REAL cost Apify reports for the run (``usageTotalUsd``), so callers can log
accurate accounting instead of estimating.  Flow mirrors the JS helper:

  POST /v2/acts/{id}/runs?waitForFinish=60   (inline wait, Apify's practical cap)
  GET  /v2/actor-runs/{runId}?waitForFinish=60  until the status is terminal
  GET  /v2/datasets/{defaultDatasetId}/items?clean=true

Failed / timed-out runs still carry compute cost — ``ApifyError.cost_usd`` and
``.run`` expose it so the error path can be logged too.

Test seam: ``_make_client()`` — tests inject an httpx.MockTransport client.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

try:
    from .config_loader import get_env
except ImportError:  # pragma: no cover — direct import from src/
    from config_loader import get_env  # type: ignore

APIFY_BASE = "https://api.apify.com/v2"
WAIT_SECS_PER_CALL = 60
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})
DEFAULT_TIMEOUT_S = 180


class ApifyError(Exception):
    """Controlled actor-run failure. ``reason`` is a machine key for logs/tests."""

    def __init__(self, reason: str, message: str, *, run: dict | None = None,
                 cost_usd: float = 0.0, http_status: int | None = None):
        super().__init__(message)
        self.reason = reason
        self.run = run
        self.cost_usd = cost_usd
        self.http_status = http_status


@dataclass
class ApifyRunResult:
    items: list[Any]
    cost_usd: float
    run_id: str
    run: dict = field(default_factory=dict)


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(WAIT_SECS_PER_CALL + 15))


def apify_token() -> str | None:
    return get_env("APIFY_TOKEN") or None


def run_actor_and_get_items(actor_id: str, actor_input: dict,
                            *, timeout_s: int = DEFAULT_TIMEOUT_S) -> ApifyRunResult:
    """Run ``actor_id`` with ``actor_input`` and return its dataset items + real cost."""
    token = apify_token()
    if not token:
        raise ApifyError("no_token", "APIFY_TOKEN is not set")
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + timeout_s
    run: dict = {}
    client = _make_client()
    try:
        try:
            resp = client.post(
                f"{APIFY_BASE}/acts/{actor_id}/runs",
                params={"waitForFinish": WAIT_SECS_PER_CALL},
                json=actor_input, headers=headers,
            )
            resp.raise_for_status()
            run = resp.json()["data"]

            while run.get("status") not in TERMINAL_STATUSES:
                if time.monotonic() > deadline:
                    raise ApifyError(
                        "timeout",
                        f"Apify actor run {run.get('id')} ({actor_id}) did not finish within {timeout_s}s",
                        run=run, cost_usd=float(run.get("usageTotalUsd") or 0.0),
                    )
                resp = client.get(
                    f"{APIFY_BASE}/actor-runs/{run['id']}",
                    params={"waitForFinish": WAIT_SECS_PER_CALL}, headers=headers,
                )
                resp.raise_for_status()
                run = resp.json()["data"]

            if run.get("status") != "SUCCEEDED":
                msg = f"Apify actor run {run.get('id')} ({actor_id}) ended with status {run.get('status')}"
                if run.get("statusMessage"):
                    msg += f" — {run['statusMessage']}"
                raise ApifyError("run_failed", msg, run=run,
                                 cost_usd=float(run.get("usageTotalUsd") or 0.0))

            resp = client.get(
                f"{APIFY_BASE}/datasets/{run['defaultDatasetId']}/items",
                params={"clean": "true"}, headers=headers, timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise ApifyError(
                "http_error", f"Apify API {e.response.status_code} for {actor_id}",
                run=run or None, cost_usd=float(run.get("usageTotalUsd") or 0.0),
                http_status=e.response.status_code,
            ) from e
        except httpx.HTTPError as e:
            raise ApifyError("transport_error", f"{type(e).__name__}: {e}",
                             run=run or None,
                             cost_usd=float(run.get("usageTotalUsd") or 0.0)) from e
    finally:
        try:
            client.close()
        except Exception:
            pass

    return ApifyRunResult(
        items=data if isinstance(data, list) else [],
        cost_usd=float(run.get("usageTotalUsd") or 0.0),
        run_id=str(run.get("id", "")),
        run=run,
    )
