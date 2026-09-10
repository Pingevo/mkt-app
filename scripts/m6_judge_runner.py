#!/usr/bin/env python3
"""Standalone M6 automated blind judge runner.

This script does NOT touch production code/config. It only reads:
- blind outputs X/Y from a prior M6.1 run
- user-visible source packs built from product_db
- rubric and JSON schema from this file

It explicitly does NOT read or send `m6_mapping_secret.json` to the judge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dotenv
import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from src import product_db
from src.llm_client import LLMClient
from src.openrouter_gateway import get_api_key as _gate_get_api_key
from src.run_context import build_multimodal_content

JUDGE_MODEL = "openai/gpt-5.6-sol"
APPROVED_CAP = 0.18
ABSOLUTE_STOP_CAP = 0.20
MAX_TOKENS = 4000
PROMPT_PRICE = 0.000001
COMPLETION_PRICE = 0.000005

# Pinned remediation baseline — both resume_link and recovery_manifest
# must equal this exact value. Not just compared to each other.
EXPECTED_M6_REMEDIATION_BASELINE = "61b6d93bb0a4389eac1bb9ff936ce6a46004ce23"

SOURCE_GROUNDED_RULE = (
    "ห้ามอ้างคุณสมบัติหรือความสามารถใด (เช่น video call) หากไม่มีข้อมูลรองรับโดยตรงใน source pack; "
    "หากไม่พบข้อมูลให้ระบุว่า 'ไม่มีข้อมูลระบุ' เท่านั้น"
)

RUBRIC = {
    "Usefulness": "1=no value, 5=highly useful",
    "Factuality": "1=factual errors, 5=fully supported by source pack",
    "Instruction following": "1=missed key requirements, 5=perfect compliance",
    "Brand / asset fit": "1=misaligned, 5=well aligned with brand and product",
    "Evidence quality": "1=no evidence, 5=strong cited/grounded evidence",
    "User effort": "1=needs heavy editing, 5=ready to use",
}
DIMENSIONS = list(RUBRIC.keys())
HARD_DIMENSIONS = ["Factuality", "Instruction following"]
BRAND_DIMENSION = "Brand / asset fit"

SCENARIOS = [
    {
        "id": "S1",
        "product_ids": ["Lagenio K2", "Lagenio K3"],
        "user_request": "กรุณาสร้างสเปคสินค้าแบบ one-page จากข้อมูลสินค้าและรูปภาพต่อไปนี้",
        "quick_brief": "one-page",
        "resource_context": "",
        "include_images": True,
        "agent_responsibility": "สร้างสเปคสินค้าแบบ one-page จากข้อมูลและรูปภาพที่ให้เท่านั้น ไม่ใช่การวิเคราะห์คู่แข่งหรือวางแผนแคมเปญ",
    },
    {
        "id": "S2",
        "product_ids": ["Lagenio K2"],
        "user_request": "กรุณาวิเคราะห์คู่แข่งของสินค้าต่อไปนี้",
        "quick_brief": "สรุปแบบ bullet executive brief ห้ามใช้ตาราง",
        "resource_context": "Agent settings: competitor_types=[direct], analysis_depth=deep, importance=[positioning].",
        "include_images": False,
        "agent_responsibility": "วิเคราะห์คู่แข่งโดยใช้หลักฐานจากเว็บ ไม่ใช่การสร้างสเปคสินค้าหรือวางแผนแคมเปญ",
    },
    {
        "id": "S3",
        "product_ids": ["Lagenio K2"],
        "user_request": "กรุณาวางแผนแคมเปญสำหรับสินค้าต่อไปนี้",
        "quick_brief": "executive brief",
        "resource_context": "Agent settings: budget_max=5000, discount_max=0, forbid_tactics=[heavy_discount,flash,bogo].",
        "include_images": False,
        "agent_responsibility": "วางแผนแคมเปญตามงบและข้อจำกัดที่กำหนด ไม่ใช่การสร้างสเปคสินค้าหรือสร้างคอนเทนต์",
    },
    {
        "id": "S4",
        "product_ids": ["Lagenio K2"],
        "user_request": "กรุณาสร้างโพสต์ TikTok 1 โพสต์สำหรับสินค้าต่อไปนี้",
        "quick_brief": "เด็กเดินทางคนเดียวปลอดภัย",
        "resource_context": "",
        "include_images": False,
        "agent_responsibility": "สร้างคอนเทนต์ TikTok (caption, script, hashtags) ไม่ใช่การวางแผนแคมเปญ งบประมาณแคมเปญ/KPI ไม่ใช่หน้าที่ของ agent นี้",
    },
]


@dataclass
class JudgeResult:
    scenario_id: str
    raw_judge_json: dict[str, Any]
    actual_model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    error: str | None = None
    stopped: bool = False
    # Fix 1: preserve charged response audit even on post-call failure
    request_id: str | None = None
    raw_provider_response: dict[str, Any] | None = None
    charged: bool = False  # True if a paid API response was received
    audit_artifact_path: str | None = None  # path to persisted provider audit
    cost_source: str | None = None  # provider_reported | token_computed | pre_call_reserve
    pre_call_reserve: float = 0.0  # reserve amount used as conservative fallback


class M6JudgeGuard:
    """Hard guard for judge: model lock, conservative pre-call reserve, no retry.

    Design (corrected — never discards a paid response):
    - Before each call: compute conservative reserve from payload.
      If cumulative + reserve > approved_cap: stop BEFORE the call (no charge).
    - After each call: ALWAYS retain content, cost, model, tokens, request ID.
      Never raise after a paid response. Never convert a charged response to zero cost.
    - If total authorization is exhausted, stop before the NEXT judge call.
    - No official verdict unless all four valid judge results complete.
    """

    SAFETY_MARGIN = 0.005  # per-call safety margin for judge

    def __init__(self, approved_cap: float, absolute_cap: float) -> None:
        self.approved_cap = approved_cap
        self.absolute_cap = absolute_cap
        self.cumulative = 0.0
        self.calls = 0
        self.last_response_audit: dict[str, dict[str, Any]] = {}
        self._original_post: Any = None  # captured in __enter__
        self._scenario_id: str | None = None
        self._last_reserve: float = 0.0  # retained for conservative fallback

    def set_scenario(self, sid: str) -> None:
        self._scenario_id = sid

    def _estimate_prompt_tokens(self, payload: dict) -> int:
        messages = payload.get("messages") or []
        texts: list[str] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(str(part.get("text", "")))
        return sum(max(1, len(t) // 2) for t in texts)

    def _compute_reserve(self, payload: dict) -> float:
        prompt_tokens = self._estimate_prompt_tokens(payload)
        max_completion = int(payload.get("max_tokens") or MAX_TOKENS)
        base = round(prompt_tokens * PROMPT_PRICE + max_completion * COMPLETION_PRICE, 6)
        return round(base + self.SAFETY_MARGIN, 6)

    def _resolve_reserve(self, payload: dict) -> float:
        """Pre-call check. Raises if insufficient budget — BEFORE any API call."""
        if payload.get("model") != JUDGE_MODEL:
            raise RuntimeError(f"model mismatch: expected {JUDGE_MODEL}, got {payload.get('model')}")
        reserve = self._compute_reserve(payload)
        if self.cumulative + reserve > self.approved_cap:
            raise RuntimeError(
                f"Judge approved cap exceeded BEFORE call: "
                f"{self.cumulative:.6f} + {reserve:.6f} > {self.approved_cap:.6f}. "
                f"Stopping before paid call. No charge incurred."
            )
        return reserve

    def _best_effort_extract(self, resp: httpx.Response) -> dict[str, Any]:
        """Best-effort extraction of audit fields from an HTTP response.

        NEVER raises. If anything is wrong (non-JSON, missing fields, model
        mismatch, malformed envelope), records the error as an audit field
        and returns a partial audit dict. The caller always gets a result.
        """
        audit: dict[str, Any] = {
            "data": None,
            "raw_text": None,
            "model": "unknown",
            "request_id": "unknown",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost": None,
            "http_status": resp.status_code,
            "extraction_errors": [],
        }

        # Try to get the raw text first (always available)
        try:
            audit["raw_text"] = resp.text
        except Exception:
            audit["raw_text"] = None

        # Try to parse JSON
        try:
            data = resp.json()
            audit["data"] = data
        except Exception as e:
            audit["extraction_errors"].append(f"json_parse: {e}")
            # Can't extract anything else from non-JSON
            return audit

        # Best-effort field extraction
        if not isinstance(data, dict):
            audit["extraction_errors"].append(f"envelope not dict: {type(data).__name__}")
            return audit

        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        audit["model"] = data.get("model", "unknown") or "unknown"
        audit["request_id"] = data.get("id", "unknown") or "unknown"
        audit["prompt_tokens"] = usage.get("prompt_tokens", 0) or 0
        audit["completion_tokens"] = usage.get("completion_tokens", 0) or 0

        cost = data.get("cost") or usage.get("cost")
        if cost is not None:
            try:
                audit["cost"] = round(float(cost), 6)
            except (TypeError, ValueError):
                audit["extraction_errors"].append(f"cost_not_numeric: {cost}")

        # Model-lock check — record as audit error, do NOT raise
        if audit["model"] != "unknown" and JUDGE_MODEL not in audit["model"]:
            audit["extraction_errors"].append(
                f"model_lock_mismatch: expected {JUDGE_MODEL}, got {audit['model']}"
            )

        return audit

    def __enter__(self) -> "M6JudgeGuard":
        guard = self
        guard._original_post = httpx.Client.post  # capture at enter time

        def _guarded_post(client: httpx.Client, url: str, **kwargs: Any) -> Any:
            resolved = str(client.base_url).rstrip("/") + "/" + url.lstrip("/") if not url.startswith("http") else url
            if "openrouter.ai" not in resolved or "/chat/completions" not in resolved:
                return guard._original_post(client, url, **kwargs)
            payload = kwargs.get("json") or {}
            # Pre-call reserve check — raises BEFORE any API call if insufficient
            # Retain the reserve for conservative fallback cost
            guard._last_reserve = guard._resolve_reserve(payload)
            # Make the call — this is the point of no return (charged)
            result = guard._original_post(client, url, **kwargs)
            # Best-effort extraction — NEVER raises
            audit = guard._best_effort_extract(result)

            # Compute cost with correct cost_source labeling:
            # 1. provider cost present → provider_reported
            # 2. token usage available → token_computed
            # 3. no cost/tokens → pre_call_reserve (conservative, never zero)
            cost = audit["cost"]
            if cost is not None:
                cost_source = "provider_reported"
            else:
                pt = audit["prompt_tokens"] or 0
                ct = audit["completion_tokens"] or 0
                if pt > 0 or ct > 0:
                    cost = round(pt * PROMPT_PRICE + ct * COMPLETION_PRICE, 6)
                    cost_source = "token_computed"
                else:
                    # No usable cost or token data — use pre-call reserve
                    cost = guard._last_reserve
                    cost_source = "pre_call_reserve"
            audit["cost"] = cost
            audit["cost_source"] = cost_source
            audit["pre_call_reserve"] = guard._last_reserve
            guard.cumulative = round(guard.cumulative + cost, 6)
            guard.calls += 1

            # Store audit on the client for the caller to pick up
            client._m6_last_actual = audit
            client._m6_last_raw = audit["data"] or {}
            client._m6_last_audit = audit

            # Record audit for this scenario
            sid = guard._scenario_id or f"call_{guard.calls}"
            guard.last_response_audit[sid] = {
                "request_id": audit["request_id"],
                "cost": cost,
                "cost_source": cost_source,
                "pre_call_reserve": guard._last_reserve,
                "model": audit["model"],
                "prompt_tokens": audit["prompt_tokens"],
                "completion_tokens": audit["completion_tokens"],
                "http_status": audit["http_status"],
                "extraction_errors": audit["extraction_errors"],
            }

            # NEVER raise after a paid response — always return the result
            return result

        httpx.Client.post = _guarded_post
        return self

    def __exit__(self, *exc: object) -> None:
        httpx.Client.post = self._original_post


def _get_brand_guidelines() -> str:
    brand_dir = PROJECT_ROOT / "brand"
    parts: list[str] = []
    for name in ["brand_profile.md", "tone_of_voice.md", "visual_guidelines.md", "terms.json"]:
        p = brand_dir / name
        if p.exists():
            parts.append(f"--- {name} ---\n{p.read_text(encoding='utf-8')}")
    return "\n\n".join(parts) if parts else ""


def _get_source_pack(scenario: dict) -> str:
    product_text = product_db.get_scoped_context_text(scenario["product_ids"])
    brand = _get_brand_guidelines()
    lines = [
        "=== Product / source pack ===",
        product_text,
        "",
        f"=== Source-grounded rule ===\n{SOURCE_GROUNDED_RULE}",
    ]
    if brand:
        lines.extend(["", f"=== Brand guidelines ===\n{brand}"])
    lines.extend(["", f"=== User request ===\n{scenario['user_request']}"])
    if scenario["quick_brief"]:
        lines.append(f"=== Quick brief ===\n{scenario['quick_brief']}")
    if scenario["resource_context"]:
        lines.append(f"=== Agent settings ===\n{scenario['resource_context']}")
    return "\n".join(lines)


def _get_image_paths_for_judge(scenario: dict) -> tuple[str, ...]:
    if not scenario.get("include_images"):
        return ()
    return tuple(_get_image_paths(scenario, scenario["product_ids"][0])[:2])


def _get_image_paths(scenario: dict, product_id: str) -> list[str]:
    if not scenario["include_images"]:
        return []
    from scripts.m6_frontier_uat import _get_product_image_paths
    return list(_get_product_image_paths(product_id))[:2]


def _build_messages(source_pack: str, output_x: str, output_y: str, scenario: dict, image_paths: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    system = (
        "You are an impartial, independent judge evaluating two anonymous AI-generated outputs "
        "(X and Y) for a Thai marketing task. You must not guess which system produced which output. "
        "Score each output independently on a 1–5 scale for every dimension. "
        "Return ONLY a strict JSON object matching the requested schema. No markdown, no explanation outside JSON."
        "\n\nSource-grounded rule: " + SOURCE_GROUNDED_RULE
        + "\n\nScenario-scoped evaluation rule: "
        "Score Instruction Following ONLY against the requirements of THIS scenario — "
        "the user request, Quick Brief, UI selections, Agent Settings, source pack, and the agent's responsibility. "
        "Do NOT invent requirements from other scenarios or other agents. "
        "Do NOT penalize an output for omitting information the user did not request and the agent is not responsible for. "
        "Do NOT reward either X or Y for irrelevant extra material that was not requested. "
        "For S4 content_creator specifically: campaign budget, KPI, ROI, CPA, ROAS, and conversion targets "
        "are NOT requirements of this scenario and their absence must not reduce any score."
    )
    rubric_lines = "\n".join(f"{k}: {v}" for k, v in RUBRIC.items())
    scenario_scope = (
        f"--- Scenario scope ---\n"
        f"User request: {scenario['user_request']}\n"
        f"Quick brief: {scenario['quick_brief']}\n"
        f"Agent settings: {scenario.get('resource_context', '(none)')}\n"
        f"Agent responsibility: {scenario.get('agent_responsibility', '(not specified)')}\n"
        f"Important: Only the above define the requirements for this scenario. "
        f"Do not add requirements from other scenarios or agents.\n"
    )
    user_text = (
        f"{scenario_scope}\n"
        f"--- Source pack ---\n{source_pack}\n\n"
        f"--- Output X ---\n{output_x}\n\n"
        f"--- Output Y ---\n{output_y}\n\n"
        f"--- Rubric (1–5) ---\n{rubric_lines}\n\n"
        "--- Rules ---\n"
        "- Do not identify or mention which output is from which source.\n"
        "- Score Instruction Following ONLY against the scenario scope above. "
        "Do NOT penalize for missing items that were not requested or are not this agent's responsibility.\n"
        "- Flag any factual claim that contradicts the source pack.\n"
        "- If the source pack does not contain enough evidence to score a dimension, set insufficient_evidence to true and explain why.\n"
        "- Return valid JSON only.\n\n"
        "--- Required JSON fields ---\n"
        "scores: object with keys Usefulness, Factuality, Instruction following, Brand / asset fit, Evidence quality, User effort.\n"
        "Each dimension must have: X, Y, winner (X, Y, or null), tie (boolean), reason (string).\n"
        "Factuality may have factual_errors array.\n"
        "overall must have: X_mean_score, Y_mean_score, winner, tie, decisive_reasons array, confidence (0-1), insufficient_evidence (boolean), insufficient_evidence_reasons array.\n"
    )
    user_content = build_multimodal_content(user_text, image_paths)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def _read_blind_outputs(run_dir: Path, scenario_id: str) -> tuple[str, str]:
    outputs_dir = run_dir / "outputs"
    x = (outputs_dir / f"{scenario_id}_X.txt").read_text(encoding="utf-8")
    y = (outputs_dir / f"{scenario_id}_Y.txt").read_text(encoding="utf-8")
    return x, y


def _validate_judge_json(data: dict) -> dict:
    scores = data.get("scores") or {}
    for dim in DIMENSIONS:
        if dim not in scores:
            raise ValueError(f"missing dimension: {dim}")
        d = scores[dim]
        for key in ("X", "Y", "tie", "reason"):
            if key not in d:
                raise ValueError(f"missing key {key} in {dim}")
        for key in ("X", "Y"):
            if not isinstance(d[key], (int, float)):
                raise ValueError(f"score {key} in {dim} is not numeric")
    overall = data.get("overall") or {}
    for key in ("X_mean_score", "Y_mean_score", "winner", "tie", "decisive_reasons", "confidence", "insufficient_evidence"):
        if key not in overall:
            raise ValueError(f"missing overall key: {key}")
    return data


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to a temp file then atomically rename to the target path."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _persist_response_audit(
    run_dir: Path, sid: str, audit: dict[str, Any]
) -> Path:
    """Persist a per-scenario provider-response audit artifact to disk.

    This MUST be called immediately after an HTTP response is received,
    BEFORE any HTTP-status, model-lock, content-JSON, or Judge-schema
    validation can fail. The artifact is the durable evidence of the
    charged call.
    """
    judge_dir = run_dir / "judge" / "provider_audits"
    artifact_path = judge_dir / f"{sid}_provider_audit.json"

    # Use cost_source from the audit if present, otherwise determine it
    cost = audit.get("cost")
    cost_source = audit.get("cost_source")
    pre_call_reserve = audit.get("pre_call_reserve", 0.0)

    if cost_source is None:
        # Determine cost_source based on available data
        if cost is not None:
            cost_source = "provider_reported"
        else:
            pt = audit.get("prompt_tokens", 0) or 0
            ct = audit.get("completion_tokens", 0) or 0
            if pt > 0 or ct > 0:
                cost = round(pt * PROMPT_PRICE + ct * COMPLETION_PRICE, 6)
                cost_source = "token_computed"
            else:
                # Conservative fallback: use pre-call reserve, never zero
                cost = pre_call_reserve
                cost_source = "pre_call_reserve"

    artifact = {
        "scenario_id": sid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "http_status": audit.get("http_status"),
        "request_id": audit.get("request_id", "unknown"),
        "reported_model": audit.get("model", "unknown"),
        "prompt_tokens": audit.get("prompt_tokens", 0),
        "completion_tokens": audit.get("completion_tokens", 0),
        "cost": cost,
        "cost_source": cost_source,
        "pre_call_reserve": pre_call_reserve,
        "charged": True,  # we received an HTTP response
        "extraction_errors": audit.get("extraction_errors", []),
        "raw_json": audit.get("data"),  # None if non-JSON
        "raw_text": audit.get("raw_text"),  # fallback when JSON unavailable
    }

    _atomic_write_json(artifact_path, artifact)
    return artifact_path


def _call_judge_raw(
    messages: list[dict],
    api_key: str,
    budget_guard_fn: Any | None = None,
) -> dict[str, Any]:
    """Make the paid API call via the canonical LLMClient and return audit.

    Routes through LLMClient.chat() so AI Usage Hub accounting is
    automatic (accounting-by-construction).  The M6JudgeGuard still
    patches httpx.Client.post at the class level, so it intercepts
    LLMClient's internal call for budget enforcement and audit extraction.

    This function ONLY makes the call and extracts audit fields via
    best-effort. It does NOT validate the schema — all of that happens
    in run_judge AFTER the audit is captured and persisted, so a
    post-call failure can never discard a charged response.

    If ``budget_guard_fn`` is provided, it is called BEFORE the paid
    call.  It may raise (e.g. BudgetExceededError) to prevent the call.
    This is the real Judge budget interception seam.
    """
    # Budget preflight — raises if denied, preventing the paid call
    if budget_guard_fn is not None:
        budget_guard_fn()

    llm = LLMClient(api_key=api_key, timeout=180)
    text: str | None = None
    exc: Exception | None = None
    try:
        text = llm.chat(
            messages,
            model=JUDGE_MODEL,
            max_tokens=MAX_TOKENS,
            temperature=0.3,
            response_format={"type": "json_object"},
            max_retry_limit=1,
            source="m6_judge",
        )
    except Exception as e:
        # LLMClient already logged the error to AI Usage Hub.
        exc = e
    finally:
        llm.close()

    # The guard stores audit on the gate's internal httpx.Client BEFORE
    # raise_for_status, so it is available even when LLMClient raised.  The
    # gate extracts the audit and returns it via ChatPostResult; LLMClient
    # stores it as self._m6_last_audit.  If the guard raised pre-call (no
    # audit stored), re-raise so run_judge knows no call was made
    # (uncharged).  If the guard made the call (audit stored), the
    # response is charged — preserve it.
    guard_audit = getattr(llm, "_m6_last_audit", None) or {}
    guard_raw = getattr(llm, "_m6_last_raw", None) or {}

    # Also check exception-attached audit (gate attaches _m6_audit to errors)
    if not guard_audit and exc is not None:
        guard_audit = getattr(exc, "_m6_audit", None) or {}
        guard_raw = getattr(exc, "_m6_raw", None) or {}

    if exc is not None and not guard_audit:
        # Pre-call rejection (guard raised before any HTTP call) — propagate
        raise exc

    if guard_audit:
        # Guard was active — use its audit directly
        cost = guard_audit.get("cost")
        cost_source = guard_audit.get("cost_source")
        pre_call_reserve = guard_audit.get("pre_call_reserve", 0.0)
        model = guard_audit.get("model", "unknown")
        request_id = guard_audit.get("request_id", "unknown")
        prompt_tokens = guard_audit.get("prompt_tokens", 0)
        completion_tokens = guard_audit.get("completion_tokens", 0)
        http_status = guard_audit.get("http_status")
        extraction_errors = guard_audit.get("extraction_errors", [])
        if exc is not None:
            extraction_errors = list(extraction_errors) + [str(exc)]
        raw_provider_response = guard_raw if isinstance(guard_raw, dict) else {}
        raw_text = guard_audit.get("raw_text") if exc is not None else text
    else:
        # Guard was not active — extract from LLMClient's per-call metadata
        raw = llm._last_raw_response or {}
        usage = raw.get("usage") or {}
        model = raw.get("model", "unknown") or "unknown"
        request_id = raw.get("id", "unknown") or "unknown"
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0
        pre_call_reserve = 0.0
        http_status = 200 if exc is None else None
        extraction_errors = [str(exc)] if exc is not None else []
        raw_provider_response = raw
        raw_text = text

        cost = llm._last_cost_usd
        if cost is None:
            cost = raw.get("cost")
            if cost is not None:
                cost = round(float(cost), 6)

        if cost is not None:
            cost_source = "provider_reported"
        elif prompt_tokens > 0 or completion_tokens > 0:
            cost = round(prompt_tokens * PROMPT_PRICE + completion_tokens * COMPLETION_PRICE, 6)
            cost_source = "token_computed"
        else:
            cost = pre_call_reserve
            cost_source = "pre_call_reserve"

    return {
        "raw_provider_response": raw_provider_response,
        "raw_text": raw_text,
        "model": model,
        "request_id": request_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost": cost,
        "cost_source": cost_source,
        "pre_call_reserve": pre_call_reserve,
        "http_status": http_status,
        "extraction_errors": extraction_errors,
        "http_response": None,  # LLMClient already called raise_for_status
        "audit": guard_audit or {
            "data": raw_provider_response,
            "model": model,
            "request_id": request_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost": cost,
            "cost_source": cost_source,
            "pre_call_reserve": pre_call_reserve,
            "http_status": http_status,
            "extraction_errors": extraction_errors,
        },
    }


def run_judge(
    scenario: dict,
    run_dir: Path,
    api_key: str,
    budget_guard_fn: Any | None = None,
    budget_commit_fn: Any | None = None,
) -> JudgeResult:
    """Run the Judge for one scenario.

    If ``budget_guard_fn`` is provided, it is called before the paid
    Judge LLM call.  It may raise to prevent the call.

    If ``budget_commit_fn`` is provided, it is called AFTER the successful
    Judge call with the actual cost, to commit spend to the budget hierarchy.
    """
    sid = scenario["id"]
    try:
        source_pack = _get_source_pack(scenario)
        x, y = _read_blind_outputs(run_dir, sid)
        image_paths = _get_image_paths_for_judge(scenario)
        messages = _build_messages(source_pack, x, y, scenario, image_paths)

        # --- Make the paid call and capture audit BEFORE any parsing ---
        call_result = _call_judge_raw(messages, api_key, budget_guard_fn=budget_guard_fn)
        raw_response = call_result["raw_provider_response"]
        raw_text = call_result.get("raw_text")
        actual_model = call_result["model"]
        request_id = call_result["request_id"]
        prompt_tokens = call_result["prompt_tokens"]
        completion_tokens = call_result["completion_tokens"]
        cost_usd = call_result["cost"]
        cost_source = call_result.get("cost_source", "pre_call_reserve")
        pre_call_reserve = call_result.get("pre_call_reserve", 0.0)
        http_status = call_result.get("http_status")
        extraction_errors = call_result.get("extraction_errors", [])
        http_response = call_result.get("http_response")

        # At this point the response is charged. Any failure below MUST
        # preserve the cost and raw response.
        charged = True

        # Commit actual Judge spend to budget hierarchy
        if budget_commit_fn is not None and cost_usd is not None:
            try:
                budget_commit_fn(float(cost_usd))
            except Exception:
                pass  # don't let budget commit failure discard the charged response

        # --- Fix 2: Persist the audit to disk BEFORE any validation ---
        audit_for_persist = {
            "data": raw_response if isinstance(raw_response, dict) else None,
            "raw_text": raw_text,
            "model": actual_model,
            "request_id": request_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost": cost_usd,
            "cost_source": cost_source,
            "pre_call_reserve": pre_call_reserve,
            "http_status": http_status,
            "extraction_errors": extraction_errors,
        }
        audit_artifact_path = _persist_response_audit(run_dir, sid, audit_for_persist)

        # --- Post-call validation (AFTER audit is persisted) ---
        # These may raise, but the audit is already on disk and the
        # cost is already in local variables.

        # HTTP status check
        if http_response is not None:
            http_response.raise_for_status()

        # Model-lock validation — reject missing, empty, "unknown", or mismatched
        if not actual_model or actual_model == "unknown" or JUDGE_MODEL not in actual_model:
            raise RuntimeError(
                f"model lock breach: expected {JUDGE_MODEL}, got '{actual_model}'"
            )

        # Check for extraction errors from the guard
        if extraction_errors:
            # If there were extraction errors, the response is malformed
            raise RuntimeError(
                f"provider response extraction errors: {'; '.join(extraction_errors)}"
            )

        # --- Parse content (may fail — but audit is already persisted) ---
        choices = raw_response.get("choices", []) if isinstance(raw_response, dict) else []
        if not choices:
            raise RuntimeError("no choices in judge response")
        content = choices[0].get("message", {}).get("content", "")
        parsed = json.loads(content)  # may raise JSONDecodeError
        parsed["_judge_model"] = actual_model
        parsed["_prompt_tokens"] = prompt_tokens
        parsed["_completion_tokens"] = completion_tokens
        parsed["_cost"] = cost_usd
        validated = _validate_judge_json(parsed)  # may raise ValueError

        return JudgeResult(
            scenario_id=sid,
            raw_judge_json=validated,
            actual_model=actual_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            request_id=request_id,
            raw_provider_response=raw_response,
            charged=charged,
            audit_artifact_path=str(audit_artifact_path),
            cost_source=cost_source,
            pre_call_reserve=pre_call_reserve,
        )
    except Exception as e:
        # Fix 1: NEVER discard a charged response. If we got far enough to
        # have a provider response, preserve its cost and audit.
        # The variables may be unbound if the failure was pre-call (e.g.
        # _get_source_pack failed), so use locals().get() defensively.
        loc = locals()
        raw_resp = loc.get("raw_response")
        actual_mod = loc.get("actual_model", "unknown")
        req_id = loc.get("request_id")
        pt = loc.get("prompt_tokens", 0)
        ct = loc.get("completion_tokens", 0)
        cost = loc.get("cost_usd", 0.0)
        was_charged = loc.get("charged", False)
        audit_path = loc.get("audit_artifact_path")
        csrc = loc.get("cost_source")
        pcr = loc.get("pre_call_reserve", 0.0)

        return JudgeResult(
            scenario_id=sid,
            raw_judge_json={},
            actual_model=actual_mod,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_usd=cost if was_charged else 0.0,
            error=str(e),
            stopped=True,
            request_id=req_id,
            raw_provider_response=raw_resp,
            charged=was_charged,
            audit_artifact_path=str(audit_path) if audit_path else None,
            cost_source=csrc,
            pre_call_reserve=pcr,
        )


def _reveal(mapping_path: Path, raw_results: list[JudgeResult]) -> dict[str, Any]:
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    scenarios: list[dict[str, Any]] = []
    brand_passes = []
    brand_attempted = []
    hard_failures: list[str] = []
    all_deltas: list[float] = []
    for r in raw_results:
        if r.error:
            continue
        sid = r.scenario_id
        m = mapping.get(sid, {})
        x_label = m.get("X", "X")
        y_label = m.get("Y", "Y")
        # Auto-detect comparison side label from mapping
        comp_label = "Baseline" if y_label == "Baseline" or x_label == "Baseline" else "Frontier"
        # map X/Y to MKTApp/comparison
        scores = r.raw_judge_json.get("scores", {})
        mktapp: dict[str, float] = {}
        comparison: dict[str, float] = {}
        for dim, d in scores.items():
            x_score = float(d["X"])
            y_score = float(d["Y"])
            if x_label == "MKTApp":
                mktapp[dim] = x_score
                comparison[dim] = y_score
            else:
                mktapp[dim] = y_score
                comparison[dim] = x_score
        delta = {dim: round(mktapp[dim] - comparison[dim], 2) for dim in mktapp}
        hard_pass = all(delta.get(d, 0.0) >= -0.5 for d in HARD_DIMENSIONS)
        if not hard_pass:
            hard_failures.append(sid)
        brand_d = delta.get(BRAND_DIMENSION, 0.0)
        brand_insufficient = bool(scores.get(BRAND_DIMENSION, {}).get("insufficient_evidence"))
        brand_attempted.append(not brand_insufficient)
        brand_pass = not brand_insufficient and brand_d >= 0.5
        brand_passes.append(brand_pass)
        mean_delta = round(sum(delta.values()) / len(delta), 3) if delta else 0.0
        all_deltas.extend(delta.values())
        # Use generic key names with the detected comparison label
        comp_score_key = f"{comp_label.lower()}_score"
        delta_key = f"delta_mktapp_minus_{comp_label.lower()}"
        scenarios.append({
            "scenario_id": sid,
            "comparison_label": comp_label,
            "mktapp_score": mktapp,
            comp_score_key: comparison,
            delta_key: delta,
            "overall_mean_delta": mean_delta,
            "hard_gate_pass": hard_pass,
            "brand_asset_advantage_pass": brand_pass,
            "insufficient_evidence_brand": brand_insufficient,
            "raw_judge_json": r.raw_judge_json,
            "cost_usd": r.cost_usd,
            "actual_model": r.actual_model,
        })

    overall_mean = round(sum(all_deltas) / len(all_deltas), 3) if all_deltas else 0.0
    non_inferior = all(d >= -0.5 for d in all_deltas) and overall_mean >= -0.5
    attempted_brand = sum(brand_attempted)
    brand_gate = attempted_brand > 0 and (sum(brand_passes) / attempted_brand) >= 0.5
    m6_pass = non_inferior and len(hard_failures) == 0 and brand_gate

    # Detect comparison label from first scenario
    comp_label = scenarios[0]["comparison_label"] if scenarios else "Frontier"
    comp_key = comp_label.lower()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge_model": JUDGE_MODEL,
        "comparison_label": comp_label,
        "scenarios": scenarios,
        "overall": {
            f"overall_mean_delta_mktapp_minus_{comp_key}": overall_mean,
            "non_inferiority_pass": non_inferior,
            "hard_gate_failures": hard_failures,
            "hard_gate_pass": len(hard_failures) == 0,
            "brand_asset_gate_pass": brand_gate,
            "brand_asset_attempted_scenarios": attempted_brand,
            "brand_asset_passing_scenarios": sum(brand_passes),
            "m6_1_pass": m6_pass,
        },
    }


def _write_artifacts(run_dir: Path, raw_results: list[JudgeResult], revealed: dict) -> None:
    judge_dir = run_dir / "judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge_model": JUDGE_MODEL,
        "incomplete": False,
        "completed": sum(1 for r in raw_results if r.error is None and not r.stopped),
        "expected": len(SCENARIOS),
        "results": [
            {
                "scenario_id": r.scenario_id,
                "raw_judge_json": r.raw_judge_json,
                "actual_model": r.actual_model,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "cost_usd": r.cost_usd,
                "error": r.error,
                "stopped": r.stopped,
                "request_id": r.request_id,
                "charged": r.charged,
                "has_raw_provider_response": r.raw_provider_response is not None,
                "audit_artifact_path": r.audit_artifact_path,
                "cost_source": r.cost_source,
                "pre_call_reserve": r.pre_call_reserve,
                "raw_provider_response": r.raw_provider_response,
            }
            for r in raw_results
        ],
    }
    (judge_dir / "m6_judge_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (judge_dir / "m6_judge_scores.json").write_text(json.dumps(revealed, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# M6 Judge Revealed Report", ""]
    lines.append(f"**Judge model:** {JUDGE_MODEL}")
    lines.append(f"**Timestamp:** {revealed['timestamp']}")
    comp_label = revealed.get("comparison_label", "Frontier")
    comp_key = comp_label.lower()
    lines.append("")
    o = revealed["overall"]
    lines.append("## Overall verdict")
    lines.append(f"- **M6.1 pass:** {o['m6_1_pass']}")
    lines.append(f"- **Non-inferiority pass:** {o['non_inferiority_pass']}")
    lines.append(f"- **Hard gate pass:** {o['hard_gate_pass']} ({o['hard_gate_failures']})")
    lines.append(f"- **Brand / asset gate pass:** {o['brand_asset_gate_pass']} ({o['brand_asset_passing_scenarios']}/{o['brand_asset_attempted_scenarios']})")
    lines.append(f"- **Overall mean delta (MKTApp - {comp_label}):** {o[f'overall_mean_delta_mktapp_minus_{comp_key}']}")
    lines.append("")
    lines.append("## Per-scenario results")
    for s in revealed["scenarios"]:
        lines.append(f"### {s['scenario_id']}")
        lines.append(f"- **Mean delta:** {s['overall_mean_delta']}")
        lines.append(f"- **Hard gate pass:** {s['hard_gate_pass']}")
        lines.append(f"- **Brand advantage pass:** {s['brand_asset_advantage_pass']}")
        lines.append(f"- **Cost:** ${s['cost_usd']:.6f}")
        lines.append(f"- **Actual model:** {s['actual_model']}")
        lines.append("")
        for dim, d in s[f"delta_mktapp_minus_{comp_key}"].items():
            lines.append(f"- {dim}: delta={d:+.2f}")
        lines.append("")
    (judge_dir / "m6_judge_revealed_report.md").write_text("\n".join(lines), encoding="utf-8")


def judge_preflight(run_dir: Path) -> tuple[bool, str]:
    """Genuinely fail-closed preflight check before ANY paid judge call.

    Returns (ok, reason). If not ok, the judge must exit without making
    even the S1 call.

    REQUIRED files (all must exist — missing = fail, not skip):
    - m6_evidence.json
    - m6_mapping_secret.json
    - output_hashes.json
    - resume_link.json (for continuation runs)

    For every S1–S4:
    1. Independent MKTApp and comparison (Frontier/Baseline) outputs must exist
    2. Their hashes must match output_hashes.json
    3. X and Y files must exist and be non-empty
    4. Mapping must be an exact permutation of {MKTApp, Frontier} or {MKTApp, Baseline}
    5. X/Y bytes must correspond to the mapped independent outputs
    6. No stopped/errored/charged-invalid/unknown-model results

    Before the first judge call, verify cumulative budget availability:
    historical_sunk + continuation_actual + judge_absolute_reserve <= approved_ceiling

    Supports both historical Frontier naming and new Baseline naming.
    The comparison side name is auto-detected from the mapping.
    """
    import hashlib

    expected_sids = ["S1", "S2", "S3", "S4"]
    expected_sids_set = set(expected_sids)
    outputs_dir = run_dir / "outputs"

    # --- REQUIRED files (missing = fail, not skip) ---
    evidence_path = run_dir / "m6_evidence.json"
    if not evidence_path.exists():
        return False, "m6_evidence.json missing — required for judge preflight"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    mapping_path = run_dir / "m6_mapping_secret.json"
    if not mapping_path.exists():
        return False, "m6_mapping_secret.json missing — required for judge preflight"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))

    hash_path = run_dir / "output_hashes.json"
    if not hash_path.exists():
        return False, "output_hashes.json missing — required for judge preflight"
    hashes = json.loads(hash_path.read_text(encoding="utf-8"))

    # resume_link.json is REQUIRED for continuation runs — not optional
    resume_link_path = run_dir / "resume_link.json"
    if not resume_link_path.exists():
        return False, "resume_link.json missing — required for judge preflight"
    resume_link = json.loads(resume_link_path.read_text(encoding="utf-8"))

    # --- Verify execution mode and authorization (both must be true) ---
    if resume_link.get("execution_mode") != "paid":
        return False, f"resume_link execution_mode must be 'paid', got '{resume_link.get('execution_mode')}'"
    if not resume_link.get("valid_for_judging"):
        return False, "resume_link valid_for_judging must be true"
    if not resume_link.get("authorized"):
        return False, "resume_link authorized must be true — dry-run artifacts cannot be judged"
    approved_ceiling = resume_link.get("approved_cumulative_ceiling")
    if approved_ceiling is None:
        return False, "resume_link approved_cumulative_ceiling is null — not authorized for paid judge calls"

    # --- Require evidence.authorized == true (not just resume_link) ---
    if not evidence.get("authorized"):
        return False, "evidence authorized must be true — unauthorized run cannot be judged"
    if evidence.get("execution_mode") != "paid":
        return False, f"evidence execution_mode must be 'paid', got '{evidence.get('execution_mode')}'"
    if not evidence.get("valid_for_judging"):
        return False, "evidence valid_for_judging must be true"

    # --- Verify resume provenance fields exist and are non-null ---
    required_fields = [
        "source_run", "source_execution_head", "continuation_harness_head",
        "production_fingerprint", "scenario_fingerprint", "input_pack_fingerprint",
        "historical_sunk_cost", "continuation_incremental_cost", "judge_reserve",
    ]
    for field in required_fields:
        if resume_link.get(field) is None:
            return False, f"resume_link field '{field}' is null or missing"

    # --- Require source recovery_manifest.json to exist (not optional) ---
    source_run_id = resume_link.get("source_run")
    if not source_run_id:
        return False, "resume_link source_run is empty — cannot locate recovery manifest"
    source_manifest_path = run_dir.parent / source_run_id / "recovery_manifest.json"
    if not source_manifest_path.exists():
        return False, f"source recovery_manifest.json missing at {source_manifest_path} — required for judge preflight"
    try:
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return False, f"source recovery_manifest.json malformed: {e}"

    # --- Cross-check ALL provenance fields against recovery manifest ---
    manifest_checks = [
        ("source_execution_head", "source_execution_head"),
        ("production_fingerprint", "production_fingerprint"),
        ("scenario_fingerprint", "scenario_fingerprint"),
        ("input_pack_fingerprint", "input_pack_fingerprint"),
    ]
    for link_field, manifest_field in manifest_checks:
        link_val = resume_link.get(link_field)
        manifest_val = source_manifest.get(manifest_field)
        if manifest_val is None:
            return False, f"recovery_manifest missing field '{manifest_field}'"
        if link_val != manifest_val:
            return False, (
                f"{link_field} mismatch: resume_link='{link_val}' "
                f"vs recovery_manifest='{manifest_val}'"
            )

    # --- Cross-check source run ID ---
    if source_manifest.get("source_run_id") and source_manifest["source_run_id"] != source_run_id:
        return False, f"source_run mismatch: resume_link='{source_run_id}' vs recovery_manifest='{source_manifest['source_run_id']}'"

    # --- Cross-check M6 remediation baseline (REQUIRED, pinned to expected value) ---
    link_baseline = resume_link.get("m6_remediation_baseline")
    manifest_baseline = source_manifest.get("m6_remediation_baseline")
    if link_baseline is None:
        return False, "resume_link m6_remediation_baseline is null or missing — required for judge preflight"
    if manifest_baseline is None:
        return False, "recovery_manifest m6_remediation_baseline is null or missing — required for judge preflight"
    if link_baseline != manifest_baseline:
        return False, (
            f"m6_remediation_baseline mismatch: "
            f"resume_link='{link_baseline}' vs recovery_manifest='{manifest_baseline}'"
        )
    if link_baseline != EXPECTED_M6_REMEDIATION_BASELINE:
        return False, (
            f"m6_remediation_baseline does not match expected value: "
            f"got '{link_baseline}', expected '{EXPECTED_M6_REMEDIATION_BASELINE}'"
        )

    # --- Validate all budget values as finite and non-negative ---
    import math
    budget_fields = {
        "historical_sunk_cost": resume_link.get("historical_sunk_cost"),
        "continuation_incremental_cost": resume_link.get("continuation_incremental_cost"),
        "judge_reserve": resume_link.get("judge_reserve"),
        "approved_cumulative_ceiling": approved_ceiling,
    }
    for fname, fval in budget_fields.items():
        if fval is None:
            return False, f"budget field '{fname}' is null"
        if not isinstance(fval, (int, float)) or not math.isfinite(fval):
            return False, f"budget field '{fname}' is not finite: {fval}"
        if fval < 0:
            return False, f"budget field '{fname}' is negative: {fval}"

    # --- Verify judge reserve is sufficient for the guard's effective authorization ---
    judge_reserve = resume_link.get("judge_reserve", 0.0)
    if judge_reserve < ABSOLUTE_STOP_CAP:
        return False, (
            f"judge reserve ${judge_reserve:.6f} is less than judge absolute stop cap "
            f"${ABSOLUTE_STOP_CAP:.6f} — insufficient for guard authorization"
        )

    # --- Check all 4 scenarios in mapping ---
    mapped_sids = set(mapping.keys())
    missing = expected_sids_set - mapped_sids
    if missing:
        return False, f"mapping missing scenarios: {sorted(missing)}"

    # --- Check evidence not stopped ---
    if evidence.get("stopped"):
        return False, f"source run was stopped: {evidence.get('stop_reason', '?')}"

    # --- Check no partial judge artifacts ---
    judge_dir = run_dir / "judge"
    if judge_dir.exists():
        for artifact in ("m6_judge_raw.json", "m6_judge_scores.json"):
            if (judge_dir / artifact).exists():
                return False, f"partial judge artifact exists: {artifact}."

    # --- Per-scenario checks ---
    # Detect comparison side: prefer explicit qualification_mode from evidence,
    # fall back to auto-detection from mapping for historical compatibility.
    qualification_mode = evidence.get("qualification_mode", "")
    if qualification_mode == "same_model_uplift":
        comparison_label = "Baseline"
        comparison_dir = "baseline"
    else:
        # Historical fallback: auto-detect from mapping
        first_mapping = next(iter(mapping.values()), {})
        first_y = first_mapping.get("Y", "")
        if first_y == "Baseline":
            comparison_label = "Baseline"
            comparison_dir = "baseline"
        else:
            comparison_label = "Frontier"
            comparison_dir = "frontier"

    for sid in expected_sids:
        m_entry = mapping.get(sid, {})

        # 4. Mapping must be exact permutation of {MKTApp, comparison}
        x_side = m_entry.get("X")
        y_side = m_entry.get("Y")
        if not x_side or not y_side:
            return False, f"{sid} mapping incomplete: X={x_side}, Y={y_side}"
        sides = {x_side, y_side}
        if sides != {"MKTApp", comparison_label}:
            return False, f"{sid} mapping must be {{MKTApp, {comparison_label}}}, got {sides}"

        # 1. Independent MKTApp and comparison outputs must exist
        for side in ("mktapp", comparison_dir):
            side_path = outputs_dir / side / f"{sid}.txt"
            if not side_path.exists():
                return False, f"missing independent output: {side}/{sid}.txt"
            content = side_path.read_text(encoding="utf-8").strip()
            if not content:
                return False, f"empty independent output: {side}/{sid}.txt"

        # 2. Hashes must match
        for side in ("mktapp", comparison_dir):
            if sid not in hashes.get(side, {}):
                return False, f"no hash recorded for {side}/{sid}"
            side_path = outputs_dir / side / f"{sid}.txt"
            actual = hashlib.sha256(side_path.read_bytes()).hexdigest()
            if actual != hashes[side][sid]:
                return False, f"hash mismatch for {side}/{sid}: expected {hashes[side][sid][:16]}..., got {actual[:16]}..."

        # 3. X and Y files must exist and be non-empty
        x_path = outputs_dir / f"{sid}_X.txt"
        y_path = outputs_dir / f"{sid}_Y.txt"
        if not x_path.exists():
            return False, f"missing X output: {sid}_X.txt"
        if not y_path.exists():
            return False, f"missing Y output: {sid}_Y.txt"
        x_content = x_path.read_bytes()
        y_content = y_path.read_bytes()
        if not x_content.strip():
            return False, f"empty X output: {sid}_X.txt"
        if not y_content.strip():
            return False, f"empty Y output: {sid}_Y.txt"

        # 5. X/Y bytes must correspond to mapped independent outputs
        mktapp_bytes = (outputs_dir / "mktapp" / f"{sid}.txt").read_bytes()
        comparison_bytes = (outputs_dir / comparison_dir / f"{sid}.txt").read_bytes()
        if x_side == "MKTApp":
            expected_x, expected_y = mktapp_bytes, comparison_bytes
        else:
            expected_x, expected_y = comparison_bytes, mktapp_bytes
        if x_content != expected_x:
            return False, f"{sid}_X.txt does not match mapped {x_side} independent output"
        if y_content != expected_y:
            return False, f"{sid}_Y.txt does not match mapped {y_side} independent output"

        # 6. No stopped/errored/charged-invalid/unknown-model
        scenario_ev = next((sc for sc in evidence.get("scenarios", []) if sc.get("id") == sid), {})
        # Check comparison side (Frontier or Baseline naming)
        comp_charged_invalid = (
            scenario_ev.get("frontier_charged_but_invalid") or
            scenario_ev.get("baseline_charged_but_invalid")
        )
        if comp_charged_invalid:
            return False, f"{sid} has charged-but-invalid {comparison_label} result"
        comp_model = (
            scenario_ev.get("baseline_model") or
            scenario_ev.get("frontier_model")
        )
        if comp_model in (None, "unknown"):
            return False, f"{sid} has unknown {comparison_label} model"
        if scenario_ev.get("mktapp_model") in (None, "unknown"):
            return False, f"{sid} has unknown MKTApp model"

        # 7. Candidate completeness — both sides must be COMPLETE
        # UNKNOWN or INCOMPLETE candidates cannot be judged.
        # Do NOT score as a loss or win — the scenario is INVALID / NOT JUDGED.
        from src.candidate_completeness import CompletenessStatus, status_from_evidence_dict, evaluate_single_call_completeness

        # Check comparison side completeness (Frontier or Baseline naming)
        comp_completeness = (
            scenario_ev.get("baseline_completeness") or
            scenario_ev.get("frontier_completeness")
        )
        if comp_completeness is None:
            # Fallback: evaluate from finish metadata if available
            comp_fr = (
                scenario_ev.get("baseline_finish_reason") or
                scenario_ev.get("frontier_finish_reason")
            )
            comp_trunc = (
                scenario_ev.get("baseline_truncated") if "baseline_truncated" in scenario_ev
                else scenario_ev.get("frontier_truncated")
            )
            if comp_fr is None and comp_trunc is None:
                comp_completeness = CompletenessStatus.UNKNOWN.value
            else:
                comp_completeness = evaluate_single_call_completeness(comp_fr, comp_trunc).value

        if comp_completeness != CompletenessStatus.COMPLETE.value:
            return False, (
                f"{sid} {comparison_label} candidate completeness is {comp_completeness} — "
                f"only COMPLETE candidates can be judged. Scenario is INVALID / NOT JUDGED."
            )

        # Check MKTApp completeness
        mktapp_completeness = scenario_ev.get("mktapp_completeness")
        if mktapp_completeness is None:
            # Fallback: evaluate from finish records if available
            mktapp_ev_dict = {
                "scenario_id": sid,
                "side": "mktapp",
                "finish_records": scenario_ev.get("mktapp_finish_records", []),
                "final_finish_reason": scenario_ev.get("mktapp_final_finish_reason"),
                "final_truncated": scenario_ev.get("mktapp_final_truncated"),
            }
            mktapp_completeness = status_from_evidence_dict(mktapp_ev_dict).value

        if mktapp_completeness != CompletenessStatus.COMPLETE.value:
            return False, (
                f"{sid} MKTApp candidate completeness is {mktapp_completeness} — "
                f"only COMPLETE candidates can be judged. Scenario is INVALID / NOT JUDGED."
            )

    # --- Cumulative budget verification ---
    historical_sunk = resume_link.get("historical_sunk_cost", 0.0)
    continuation_inc = resume_link.get("continuation_incremental_cost", 0.0)
    cumulative = round(historical_sunk + continuation_inc + ABSOLUTE_STOP_CAP, 6)
    if cumulative > approved_ceiling:
        return False, (
            f"Cumulative budget exceeded: sunk ${historical_sunk:.6f} + "
            f"continuation ${continuation_inc:.6f} + judge absolute ${ABSOLUTE_STOP_CAP:.6f} "
            f"= ${cumulative:.6f} > approved ceiling ${approved_ceiling:.6f}"
        )

    return True, "ok"


def _update_judge_accounting(
    run_dir: Path,
    raw_results: list[JudgeResult],
    incomplete: bool,
) -> None:
    """Write immutable judge_accounting.json and update resume_link.json.

    Distinguishes attempted, successfully completed, and failed scenarios.
    Includes the per-scenario response audit and the actual charged total.
    Incomplete execution updates cumulative actual cost WITHOUT publishing
    a verdict.
    """
    attempted = len(raw_results)
    completed = sum(1 for r in raw_results if r.error is None and not r.stopped)
    failed = sum(1 for r in raw_results if r.error is not None or r.stopped)
    judge_actual = round(sum(r.cost_usd for r in raw_results), 6)

    # Per-scenario response audit
    response_audit: dict[str, dict[str, Any]] = {}
    for r in raw_results:
        response_audit[r.scenario_id] = {
            "actual_model": r.actual_model,
            "request_id": r.request_id,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "cost_usd": r.cost_usd,
            "charged": r.charged,
            "error": r.error,
            "stopped": r.stopped,
            "has_raw_provider_response": r.raw_provider_response is not None,
            "audit_artifact_path": r.audit_artifact_path,
            "cost_source": r.cost_source,
            "pre_call_reserve": r.pre_call_reserve,
        }

    accounting = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge_model": JUDGE_MODEL,
        "judge_incremental_actual": judge_actual,
        "incomplete": incomplete,
        "scenarios_attempted": attempted,
        "scenarios_completed": completed,
        "scenarios_failed": failed,
        "scenarios_expected": len(SCENARIOS),
        "response_audit": response_audit,
    }
    accounting_path = run_dir / "judge_accounting.json"
    accounting_path.write_text(json.dumps(accounting, indent=2, ensure_ascii=False), encoding="utf-8")

    # Update resume_link.json if it exists
    link_path = run_dir / "resume_link.json"
    if link_path.exists():
        link = json.loads(link_path.read_text(encoding="utf-8"))
        link["judge_incremental_cost"] = judge_actual
        historical = link.get("historical_sunk_cost", 0.0)
        continuation = link.get("continuation_incremental_cost", 0.0)
        link["cumulative_program_actual"] = round(historical + continuation + judge_actual, 6)
        approved = link.get("approved_cumulative_ceiling")
        if approved is not None:
            link["remaining_authorized"] = round(approved - link["cumulative_program_actual"], 6)
        link["judge_complete"] = not incomplete
        link["judge_scenarios_attempted"] = attempted
        link["judge_scenarios_completed"] = completed
        link["judge_scenarios_failed"] = failed
        link_path.write_text(json.dumps(link, indent=2), encoding="utf-8")


def _validate_raw_for_reveal(raw_data: dict) -> tuple[bool, str, list[JudgeResult]]:
    """Validate that a raw judge artifact is complete and safe to reveal.

    Returns (ok, reason, results). If not ok, the artifact is incomplete
    and must NEVER produce an official verdict.

    Requirements:
    - exactly S1–S4 present once each
    - all four results are successful (no error, not stopped)
    - every result has a valid model, response, and cost accounting
    - the raw artifact explicitly records a complete Judge run
    """
    expected_sids = {"S1", "S2", "S3", "S4"}

    # The raw artifact must explicitly record a complete run
    if raw_data.get("incomplete") is not False:
        return False, "raw artifact does not record a complete run (incomplete != false)", []
    if raw_data.get("completed") != len(SCENARIOS):
        return False, f"raw artifact completed={raw_data.get('completed')}, expected {len(SCENARIOS)}", []
    if raw_data.get("expected") != len(SCENARIOS):
        return False, f"raw artifact expected={raw_data.get('expected')}, expected {len(SCENARIOS)}", []

    results_data = raw_data.get("results", [])
    if not isinstance(results_data, list):
        return False, "raw artifact results is not a list", []

    # Reconstruct JudgeResult objects
    try:
        results = [JudgeResult(**r) for r in results_data]
    except Exception as e:
        return False, f"cannot reconstruct JudgeResult: {e}", []

    # Check exactly S1–S4 present once each
    sids = [r.scenario_id for r in results]
    if len(sids) != len(SCENARIOS):
        return False, f"expected {len(SCENARIOS)} results, got {len(sids)}", []
    if set(sids) != expected_sids:
        return False, f"scenario IDs {sids} != expected {sorted(expected_sids)}", []
    if len(set(sids)) != len(sids):
        return False, f"duplicate scenario IDs in results: {sids}", []

    # Check all four results are successful
    for r in results:
        if r.error is not None:
            return False, f"{r.scenario_id} has error: {r.error}", []
        if r.stopped:
            return False, f"{r.scenario_id} is stopped", []

    # Check every result has a valid model, response, and cost accounting
    for r in results:
        if not r.actual_model or r.actual_model == "unknown":
            return False, f"{r.scenario_id} has unknown/empty model", []
        if not r.raw_judge_json:
            return False, f"{r.scenario_id} has empty raw_judge_json", []
        if not isinstance(r.cost_usd, (int, float)) or r.cost_usd < 0:
            return False, f"{r.scenario_id} has invalid cost_usd: {r.cost_usd}", []
        if not isinstance(r.prompt_tokens, int) or r.prompt_tokens < 0:
            return False, f"{r.scenario_id} has invalid prompt_tokens: {r.prompt_tokens}", []
        if not isinstance(r.completion_tokens, int) or r.completion_tokens < 0:
            return False, f"{r.scenario_id} has invalid completion_tokens: {r.completion_tokens}", []

    return True, "ok", results


def main() -> int:
    parser = argparse.ArgumentParser(description="M6 automated blind judge")
    parser.add_argument("--run-dir", type=Path, default=PROJECT_ROOT / "data" / "m6_frontier_uat" / "20260902_050655")
    parser.add_argument("--no-judge", action="store_true", help="Only run reveal from existing raw judge JSON")
    args = parser.parse_args()

    dotenv.load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
    api_key = _gate_get_api_key()
    if not api_key and not args.no_judge:
        print("OPENROUTER_API_KEY not set", file=sys.stderr)
        return 1

    mapping_path = args.run_dir / "m6_mapping_secret.json"
    if not mapping_path.exists():
        print(f"mapping not found: {mapping_path}", file=sys.stderr)
        return 1

    if args.no_judge:
        raw_path = args.run_dir / "judge" / "m6_judge_raw.json"
        if not raw_path.exists():
            print(f"raw judge file not found: {raw_path}", file=sys.stderr)
            return 1
        raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
        # Fix 4: Prevent partial reveal absolutely
        ok, reason, raw_results = _validate_raw_for_reveal(raw_data)
        if not ok:
            print(f"Cannot reveal from raw artifact: {reason}", file=sys.stderr)
            print("Partial raw artifacts cannot produce an official verdict.", file=sys.stderr)
            return 1
    else:
        # Fail-closed preflight before any paid judge call
        ok, reason = judge_preflight(args.run_dir)
        if not ok:
            print(f"Judge preflight FAILED: {reason}", file=sys.stderr)
            print("No paid judge calls made.", file=sys.stderr)
            return 1
        print(f"Judge preflight passed: {reason}")

        raw_results: list[JudgeResult] = []
        judge_stopped = False
        guard = M6JudgeGuard(approved_cap=APPROVED_CAP, absolute_cap=ABSOLUTE_STOP_CAP)
        with guard:
            for scenario in SCENARIOS:
                guard.set_scenario(scenario["id"])
                r = run_judge(scenario, args.run_dir, api_key)
                raw_results.append(r)
                if r.error:
                    print(f"Judge stopped at {r.scenario_id}: {r.error}", file=sys.stderr)
                    judge_stopped = True
                    break
                print(f"Judge {r.scenario_id}: {r.actual_model}, cost=${r.cost_usd:.6f}")

        # If judge did not complete all 4 scenarios, do NOT publish a verdict
        if judge_stopped or len(raw_results) < len(SCENARIOS):
            _completed = sum(1 for r in raw_results if r.error is None and not r.stopped)
            _failed = sum(1 for r in raw_results if r.error is not None or r.stopped)
            print(f"Judge INCOMPLETE: {_completed}/{len(SCENARIOS)} scenarios completed, {_failed} failed (attempted={len(raw_results)}).", file=sys.stderr)
            print("No official PASS/FAIL verdict from partial result.", file=sys.stderr)
            # Write only raw results, no verdict
            judge_dir = args.run_dir / "judge"
            judge_dir.mkdir(parents=True, exist_ok=True)
            judge_actual_total = round(sum(r.cost_usd for r in raw_results), 6)
            raw = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "judge_model": JUDGE_MODEL,
                "incomplete": True,
                "completed": sum(1 for r in raw_results if r.error is None and not r.stopped),
                "expected": len(SCENARIOS),
                "judge_incremental_actual": judge_actual_total,
                "results": [
                    {
                        "scenario_id": r.scenario_id,
                        "raw_judge_json": r.raw_judge_json,
                        "actual_model": r.actual_model,
                        "prompt_tokens": r.prompt_tokens,
                        "completion_tokens": r.completion_tokens,
                        "cost_usd": r.cost_usd,
                        "error": r.error,
                        "stopped": r.stopped,
                        "request_id": r.request_id,
                        "charged": r.charged,
                        "has_raw_provider_response": r.raw_provider_response is not None,
                        "audit_artifact_path": r.audit_artifact_path,
                        "cost_source": r.cost_source,
                        "pre_call_reserve": r.pre_call_reserve,
                        "raw_provider_response": r.raw_provider_response,
                    }
                    for r in raw_results
                ],
            }
            (judge_dir / "m6_judge_raw.json").write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            (judge_dir / "m6_judge_scores.json").write_text(
                json.dumps({"incomplete": True, "completed": sum(1 for r in raw_results if r.error is None and not r.stopped),
                            "expected": len(SCENARIOS),
                            "judge_incremental_actual": judge_actual_total},
                           indent=2), encoding="utf-8")
            # Update accounting with real attempted/completed/failed counts
            _update_judge_accounting(args.run_dir, raw_results, incomplete=True)
            return 1

    # Complete judge execution — update accounting and produce verdict
    _update_judge_accounting(args.run_dir, raw_results, incomplete=False)

    revealed = _reveal(mapping_path, raw_results)
    _write_artifacts(args.run_dir, raw_results, revealed)

    o = revealed["overall"]
    print(f"Total judge cost: ${sum(r.cost_usd for r in raw_results):.6f}")
    print(f"M6.1 pass: {o['m6_1_pass']}")
    print(f"Non-inferiority: {o['non_inferiority_pass']}")
    print(f"Hard gate: {o['hard_gate_pass']} {o['hard_gate_failures']}")
    print(f"Brand gate: {o['brand_asset_gate_pass']}")
    return 0 if o["m6_1_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
