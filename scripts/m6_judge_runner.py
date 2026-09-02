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

JUDGE_MODEL = "openai/gpt-5.6-sol"
APPROVED_CAP = 0.15
ABSOLUTE_STOP_CAP = 0.20
MAX_TOKENS = 4000
PROMPT_PRICE = 0.000001
COMPLETION_PRICE = 0.000005

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
        "quick_brief": "",
        "resource_context": "",
        "include_images": True,
    },
    {
        "id": "S2",
        "product_ids": ["Lagenio K2"],
        "user_request": "กรุณาวิเคราะห์คู่แข่งของสินค้าต่อไปนี้",
        "quick_brief": "สรุปแบบ bullet executive brief ห้ามใช้ตาราง",
        "resource_context": "Agent settings: competitor_types=[direct], analysis_depth=deep, importance=[positioning].",
        "include_images": False,
    },
    {
        "id": "S3",
        "product_ids": ["Lagenio K2"],
        "user_request": "กรุณาวางแผนแคมเปญสำหรับสินค้าต่อไปนี้",
        "quick_brief": "executive brief",
        "resource_context": "Agent settings: budget_max=5000, discount_max=0, forbid_tactics=[heavy_discount,flash,bogo].",
        "include_images": False,
    },
    {
        "id": "S4",
        "product_ids": ["Lagenio K2"],
        "user_request": "กรุณาสร้างโพสต์ TikTok 1 โพสต์สำหรับสินค้าต่อไปนี้",
        "quick_brief": "เด็กเดินทางคนเดียวปลอดภัย",
        "resource_context": "",
        "include_images": False,
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


class M6JudgeGuard:
    """Hard guard for judge: model lock, approved cap, absolute stop cap, no retry."""

    def __init__(self, approved_cap: float, absolute_cap: float) -> None:
        self.approved_cap = approved_cap
        self.absolute_cap = absolute_cap
        self.cumulative = 0.0
        self.calls = 0
        self._original_post = httpx.Client.post

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
        return round(prompt_tokens * PROMPT_PRICE + max_completion * COMPLETION_PRICE, 6)

    def _resolve_reserve(self, payload: dict) -> float:
        if payload.get("model") != JUDGE_MODEL:
            raise RuntimeError(f"model mismatch: expected {JUDGE_MODEL}, got {payload.get('model')}")
        reserve = self._compute_reserve(payload)
        if self.cumulative + reserve > self.approved_cap:
            raise RuntimeError(
                f"Judge approved cap exceeded: {self.cumulative:.6f} + {reserve:.6f} > {self.approved_cap:.6f}"
            )
        return reserve

    def _parse_actual(self, resp: httpx.Response) -> dict[str, Any]:
        data = resp.json()
        usage = data.get("usage") or {}
        cost = data.get("cost") or usage.get("cost")
        model = data.get("model", "unknown")
        if JUDGE_MODEL not in model:
            raise RuntimeError(f"model lock breach: expected {JUDGE_MODEL}, got {model}")
        return {
            "cost": float(cost) if cost is not None else None,
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "data": data,
        }

    def __enter__(self) -> "M6JudgeGuard":
        guard = self

        def _guarded_post(client: httpx.Client, url: str, **kwargs: Any) -> Any:
            resolved = str(client.base_url).rstrip("/") + "/" + url.lstrip("/") if not url.startswith("http") else url
            if "openrouter.ai" not in resolved or "/chat/completions" not in resolved:
                return guard._original_post(client, url, **kwargs)
            payload = kwargs.get("json") or {}
            guard._resolve_reserve(payload)
            result = guard._original_post(client, url, **kwargs)
            actual = guard._parse_actual(result)
            cost = actual["cost"]
            if cost is None:
                cost = (actual["prompt_tokens"] * PROMPT_PRICE) + (actual["completion_tokens"] * COMPLETION_PRICE)
                cost = round(float(cost), 6)
            guard.cumulative += cost
            guard.calls += 1
            client._m6_last_actual = actual
            client._m6_last_raw = actual["data"]
            if guard.cumulative > guard.absolute_cap:
                raise RuntimeError(
                    f"Judge absolute stop cap exceeded: {guard.cumulative:.6f} > {guard.absolute_cap:.6f}"
                )
            return result

        httpx.Client.post = _guarded_post
        return self

    def __exit__(self, *exc: object) -> None:
        httpx.Client.post = self._original_post


def _get_source_pack(scenario: dict) -> str:
    product_text = product_db.get_scoped_context_text(scenario["product_ids"])
    lines = [
        "=== Product / source pack ===",
        product_text,
        "",
        f"=== User request ===\n{scenario['user_request']}",
    ]
    if scenario["quick_brief"]:
        lines.append(f"=== Quick brief ===\n{scenario['quick_brief']}")
    if scenario["resource_context"]:
        lines.append(f"=== Agent settings ===\n{scenario['resource_context']}")
    return "\n".join(lines)


def _get_image_paths(scenario: dict, product_id: str) -> list[str]:
    if not scenario["include_images"]:
        return []
    from scripts.m6_frontier_uat import _get_product_image_paths
    return list(_get_product_image_paths(product_id))[:2]


def _build_messages(source_pack: str, output_x: str, output_y: str) -> list[dict[str, Any]]:
    system = (
        "You are an impartial, independent judge evaluating two anonymous AI-generated outputs "
        "(X and Y) for a Thai marketing task. You must not guess which system produced which output. "
        "Score each output independently on a 1–5 scale for every dimension. "
        "Return ONLY a strict JSON object matching the requested schema. No markdown, no explanation outside JSON."
    )
    rubric_lines = "\n".join(f"{k}: {v}" for k, v in RUBRIC.items())
    user = (
        f"--- Source pack ---\n{source_pack}\n\n"
        f"--- Output X ---\n{output_x}\n\n"
        f"--- Output Y ---\n{output_y}\n\n"
        f"--- Rubric (1–5) ---\n{rubric_lines}\n\n"
        "--- Rules ---\n"
        "- Do not identify or mention which output is from which source.\n"
        "- Flag any factual claim that contradicts the source pack.\n"
        "- If the source pack does not contain enough evidence to score a dimension, set insufficient_evidence to true and explain why.\n"
        "- Return valid JSON only.\n\n"
        "--- Required JSON fields ---\n"
        "scores: object with keys Usefulness, Factuality, Instruction following, Brand / asset fit, Evidence quality, User effort.\n"
        "Each dimension must have: X, Y, winner (X, Y, or null), tie (boolean), reason (string).\n"
        "Factuality may have factual_errors array.\n"
        "overall must have: X_mean_score, Y_mean_score, winner, tie, decisive_reasons array, confidence (0-1), insufficient_evidence (boolean), insufficient_evidence_reasons array.\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
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


def _call_judge(messages: list[dict], api_key: str) -> dict:
    client = httpx.Client(base_url="https://openrouter.ai/api/v1")
    payload = {
        "model": JUDGE_MODEL,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://mktapp.local",
        "X-Title": "M6 Judge",
    }
    r = client.post("/chat/completions", json=payload, headers=headers, timeout=180)
    r.raise_for_status()
    raw = getattr(client, "_m6_last_raw", {}) or r.json()
    actual = getattr(client, "_m6_last_actual", {}) or {}
    choices = raw.get("choices", [])
    if not choices:
        raise RuntimeError("no choices in judge response")
    content = choices[0].get("message", {}).get("content", "")
    parsed = json.loads(content)
    parsed["_judge_model"] = actual.get("model", JUDGE_MODEL)
    parsed["_prompt_tokens"] = actual.get("prompt_tokens", 0)
    parsed["_completion_tokens"] = actual.get("completion_tokens", 0)
    parsed["_cost"] = actual.get("cost")
    return _validate_judge_json(parsed)


def run_judge(scenario: dict, run_dir: Path, api_key: str) -> JudgeResult:
    try:
        source_pack = _get_source_pack(scenario)
        x, y = _read_blind_outputs(run_dir, scenario["id"])
        messages = _build_messages(source_pack, x, y)
        raw = _call_judge(messages, api_key)
        actual = getattr(httpx.Client, "_m6_last_actual", {}) or {}
        if not actual:
            # fallback: cost not available because guard not active
            actual = {
                "model": raw.get("_judge_model", JUDGE_MODEL),
                "prompt_tokens": raw.get("_prompt_tokens", 0),
                "completion_tokens": raw.get("_completion_tokens", 0),
                "cost": raw.get("_cost") or 0.0,
            }
        return JudgeResult(
            scenario_id=scenario["id"],
            raw_judge_json=raw,
            actual_model=actual.get("model", JUDGE_MODEL),
            prompt_tokens=actual.get("prompt_tokens", 0),
            completion_tokens=actual.get("completion_tokens", 0),
            cost_usd=round(float(actual.get("cost") or 0.0), 6),
        )
    except Exception as e:
        return JudgeResult(
            scenario_id=scenario["id"],
            raw_judge_json={},
            actual_model="unknown",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            error=str(e),
            stopped=True,
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
        # map X/Y to MKTApp/Frontier
        scores = r.raw_judge_json.get("scores", {})
        mktapp: dict[str, float] = {}
        frontier: dict[str, float] = {}
        for dim, d in scores.items():
            x_score = float(d["X"])
            y_score = float(d["Y"])
            if x_label == "MKTApp":
                mktapp[dim] = x_score
                frontier[dim] = y_score
            else:
                mktapp[dim] = y_score
                frontier[dim] = x_score
        delta = {dim: round(mktapp[dim] - frontier[dim], 2) for dim in mktapp}
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
        scenarios.append({
            "scenario_id": sid,
            "mktapp_score": mktapp,
            "frontier_score": frontier,
            "delta_mktapp_minus_frontier": delta,
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

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge_model": JUDGE_MODEL,
        "scenarios": scenarios,
        "overall": {
            "overall_mean_delta_mktapp_minus_frontier": overall_mean,
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
        "results": [
            {
                "scenario_id": r.scenario_id,
                "raw_judge_json": r.raw_judge_json,
                "actual_model": r.actual_model,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "cost_usd": r.cost_usd,
                "error": r.error,
            }
            for r in raw_results
        ],
    }
    (judge_dir / "m6_judge_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (judge_dir / "m6_judge_scores.json").write_text(json.dumps(revealed, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# M6 Judge Revealed Report", ""]
    lines.append(f"**Judge model:** {JUDGE_MODEL}")
    lines.append(f"**Timestamp:** {revealed['timestamp']}")
    lines.append("")
    o = revealed["overall"]
    lines.append("## Overall verdict")
    lines.append(f"- **M6.1 pass:** {o['m6_1_pass']}")
    lines.append(f"- **Non-inferiority pass:** {o['non_inferiority_pass']}")
    lines.append(f"- **Hard gate pass:** {o['hard_gate_pass']} ({o['hard_gate_failures']})")
    lines.append(f"- **Brand / asset gate pass:** {o['brand_asset_gate_pass']} ({o['brand_asset_passing_scenarios']}/{o['brand_asset_attempted_scenarios']})")
    lines.append(f"- **Overall mean delta (MKTApp - Frontier):** {o['overall_mean_delta_mktapp_minus_frontier']}")
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
        for dim, d in s["delta_mktapp_minus_frontier"].items():
            lines.append(f"- {dim}: delta={d:+.2f}")
        lines.append("")
    (judge_dir / "m6_judge_revealed_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="M6 automated blind judge")
    parser.add_argument("--run-dir", type=Path, default=PROJECT_ROOT / "data" / "m6_frontier_uat" / "20260902_050655")
    parser.add_argument("--no-judge", action="store_true", help="Only run reveal from existing raw judge JSON")
    args = parser.parse_args()

    dotenv.load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
    api_key = os.environ.get("OPENROUTER_API_KEY")
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
        raw_results = [JudgeResult(**r) for r in raw_data["results"]]
    else:
        raw_results: list[JudgeResult] = []
        with M6JudgeGuard(approved_cap=APPROVED_CAP, absolute_cap=ABSOLUTE_STOP_CAP):
            for scenario in SCENARIOS:
                r = run_judge(scenario, args.run_dir, api_key)
                raw_results.append(r)
                if r.error:
                    print(f"Judge stopped at {r.scenario_id}: {r.error}", file=sys.stderr)
                    break
                print(f"Judge {r.scenario_id}: {r.actual_model}, cost=${r.cost_usd:.6f}")

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
