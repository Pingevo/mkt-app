#!/usr/bin/env python3
"""M6.1 Direct-User Frontier UAT runner.

Evaluation-only harness.  Does not modify production code/config.  Enforces:
  - exact Frontier model = anthropic/claude-fable-5.1
  - max 2 web_search tool uses per S2/S3 Frontier request
  - per-scenario reserve caps from M6_OPENROUTER_PREFLIGHT_REPORT.md
  - total approval cap $2.00
  - no rerun/retry; stop on any error or cap breach
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

from src.llm_client import LLMClient
from src.orchestrator import Orchestrator
from src import product_db
from src.run_context import build_multimodal_content
import qual_runner

FRONTIER_MODEL = "anthropic/claude-fable-5.1"
APPROVAL_CAP = 2.00
CONTINGENCY = 0.20

PROMPT_PRICE = 0.000010
COMPLETION_PRICE = 0.000050
REASONING_PRICE = 0.000050
WEB_SEARCH_PRICE = 0.010
IMAGE_TOKEN_ESTIMATE = 1100
WEB_SEARCH_RESULT_TOKEN_ALLOWANCE = 15000

SOURCE_GROUNDED_RULE = (
    "ห้ามอ้างคุณสมบัติหรือความสามารถใด (เช่น video call) หากไม่มีข้อมูลรองรับโดยตรงใน source pack; "
    "หากไม่พบข้อมูลให้ระบุว่า 'ไม่มีข้อมูลระบุ' เท่านั้น"
)

AGENT_INSTRUCTIONS_PATH = PROJECT_ROOT / "config" / "agent_instructions.json"


def _parse_list(value: str) -> list[str]:
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1]
        if not inner.strip():
            return []
        return [v.strip() for v in inner.split(",") if v.strip()]
    return [value.strip()] if value.strip() else []


def _parse_agent_settings_override(resource_context: str, agent_key: str) -> dict[str, Any]:
    """Parse typed UI/Agent Settings from the M6 harness resource_context string.

    Mirrors the fields saved by the production UI in config/agent_instructions.json.
    """
    override: dict[str, Any] = {}
    if not resource_context or "Agent settings:" not in resource_context:
        return override
    try:
        # Extract the "Agent settings: ..." block up to the next section or end.
        match = re.search(r"Agent settings:\s*(.+?)(?:\n\n|\n---|$)", resource_context, re.DOTALL)
        if not match:
            return override
        body = match.group(1).strip().rstrip(".")
        # key=value pairs, where value may be a bracketed list (do not split on commas inside brackets)
        for key, val in re.findall(r"(\w+)\s*=\s*(\[[^\]]*\]|[^\[\],]+)(?:,|$)", body):
            key = key.strip()
            val = val.strip()
            if val.startswith("["):
                override[key] = _parse_list(val)
            elif val.replace(".", "", 1).isdigit():
                override[key] = float(val) if "." in val else int(val)
            elif val.lower() in ("true", "false"):
                override[key] = val.lower() == "true"
            else:
                override[key] = val
    except Exception:
        return override
    return override


def _get_brand_guidelines() -> str:
    brand_dir = PROJECT_ROOT / "brand"
    parts: list[str] = []
    for name in ["brand_profile.md", "tone_of_voice.md", "visual_guidelines.md", "terms.json"]:
        p = brand_dir / name
        if p.exists():
            parts.append(f"--- {name} ---\n{p.read_text(encoding='utf-8')}")
    if not parts:
        return ""
    return "\n\n".join(parts)

SCENARIOS = [
    {
        "id": "S1",
        "agent_key": "product_spec",
        "product_ids": ["Lagenio K2", "Lagenio K3"],
        "product_id": "Lagenio K2",
        "quick_brief": "one-page",
        "resource_context": "",
        "platforms": None,
        "auto_image": False,
        "mktapp_max_calls": 2,
        "frontier_input_tokens": 5000,
        "frontier_output_tokens": 4000,
        "frontier_web_uses": 0,
        "mktapp_reserve": 0.10,
        "frontier_reserve": 0.28,
        "system": "คุณคือนักวิเคราะห์สินค้า รับข้อมูลสินค้าและรูปภาพแล้วสร้างสเปคสินค้าแบบ one-page เป็นภาษาไทย กระชับ ชัดเจน",
        "user_prefix": "กรุณาสร้างสเปคสินค้าแบบ one-page จากข้อมูลสินค้าและรูปภาพต่อไปนี้:\n\n",
    },
    {
        "id": "S2",
        "agent_key": "competitor_analysis",
        "product_ids": None,
        "product_id": "Lagenio K2",
        "quick_brief": "สรุปแบบ bullet executive brief ห้ามใช้ตาราง",
        "resource_context": "Agent settings: competitor_types=[direct], analysis_depth=deep, importance=[positioning].",
        "platforms": None,
        "auto_image": False,
        "mktapp_max_calls": 2,
        "frontier_input_tokens": 5000,
        "frontier_output_tokens": 4500,
        "frontier_web_uses": 2,
        "mktapp_reserve": 0.20,
        "frontier_reserve": 0.60,
        "system": "คุณคือนักวิเคราะห์คู่แข่ง ใช้ข้อมูลจากเว็บ (web_search) เพื่อหาหลักฐาน สร้างรายงานวิเคราะห์คู่แข่งแบบ bullet executive brief เป็นภาษาไทย ห้ามใช้ตาราง ใช้ web_search ไม่เกิน 2 ครั้ง",
        "user_prefix": "กรุณาวิเคราะห์คู่แข่งของสินค้าต่อไปนี้:\n\n",
    },
    {
        "id": "S3",
        "agent_key": "campaign_strategy",
        "product_ids": None,
        "product_id": "Lagenio K2",
        "quick_brief": "executive brief",
        "resource_context": "Agent settings: budget_max=5000, discount_max=0, forbid_tactics=[heavy_discount,flash,bogo].",
        "platforms": None,
        "auto_image": False,
        "mktapp_max_calls": 4,
        "frontier_input_tokens": 5000,
        "frontier_output_tokens": 4500,
        "frontier_web_uses": 2,
        "mktapp_reserve": 0.30,
        "frontier_reserve": 0.60,
        "system": "คุณคือนักวางกลยุทธ์แคมเปญ ใช้ข้อมูลจากเว็บ (web_search) ถ้าขาด context สร้างแคมเปญ executive brief เป็นภาษาไทย งบ 5,000 THB ห้ามเสนอส่วนลด/Flash Sale/BOGO ใช้ web_search ไม่เกิน 2 ครั้ง",
        "user_prefix": "กรุณาวางแผนแคมเปญสำหรับสินค้าต่อไปนี้:\n\n",
    },
    {
        "id": "S4",
        "agent_key": "content_creator",
        "product_ids": None,
        "product_id": "Lagenio K2",
        "quick_brief": "เด็กเดินทางคนเดียวปลอดภัย",
        "resource_context": "",
        "platforms": ["tiktok"],
        "auto_image": False,
        "mktapp_max_calls": 6,
        "frontier_input_tokens": 5000,
        "frontier_output_tokens": 3500,
        "frontier_web_uses": 0,
        "mktapp_reserve": 0.10,
        "frontier_reserve": 0.24,
        "system": "คุณคือนักสร้างคอนเทนต์ สร้างโพสต์ TikTok 1 โพสต์ สำหรับสินค้าต่อไปนี้ ออกแบบ caption, script, hashtags, content plan เป็นภาษาไทย ไม่ต้องสร้างภาพ",
        "user_prefix": "กรุณาสร้างโพสต์ TikTok 1 โพสต์สำหรับสินค้าต่อไปนี้:\n\n",
    },
]


@dataclass
class RunResult:
    scenario_id: str
    side: str
    output: str
    actual_model: str
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    web_uses: int
    error: str | None
    stopped: bool
    evidence: dict


class M6FrontierGuard:
    """Hard guard for Frontier side: model lock, per-scenario reserve, total cap, web uses."""

    def __init__(self, total_cap: float, per_scenario_caps: dict[str, float]) -> None:
        self.total_cap = total_cap
        self.per_scenario_caps = per_scenario_caps
        self.cumulative = 0.0
        self.calls = 0
        self.scenario_spent: dict[str, float] = {}
        self._scenario: str | None = None
        self._original_post = httpx.Client.post
        self._original_stream = httpx.Client.stream

    def set_scenario(self, scenario_id: str) -> None:
        self._scenario = scenario_id

    def _extract_texts(self, messages: list[dict]) -> list[str]:
        texts: list[str] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(str(part.get("text", "")))
        return texts

    def _count_images(self, messages: list[dict]) -> int:
        images = 0
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        images += 1
        return images

    def _count_web_uses(self, payload: dict) -> int:
        tools = payload.get("tools") or []
        web_uses = 0
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "openrouter:web_search":
                params = tool.get("parameters") or {}
                max_uses = params.get("max_uses")
                web_uses += int(max_uses) if isinstance(max_uses, int) else 1
        return web_uses

    def _estimate_prompt_tokens(self, payload: dict) -> int:
        messages = payload.get("messages") or []
        texts = self._extract_texts(messages)
        text_tokens = sum(max(1, len(t) // 2) for t in texts)
        image_tokens = self._count_images(messages) * IMAGE_TOKEN_ESTIMATE
        web_uses = self._count_web_uses(payload)
        web_result_tokens = web_uses * WEB_SEARCH_RESULT_TOKEN_ALLOWANCE
        return text_tokens + image_tokens + web_result_tokens

    def _compute_reserve(self, payload: dict) -> float:
        """Worst-case dollar reserve for this request from payload data."""
        prompt_tokens = self._estimate_prompt_tokens(payload)
        max_completion = int(payload.get("max_tokens") or 4096)
        web_uses = self._count_web_uses(payload)
        return round(
            prompt_tokens * PROMPT_PRICE
            + max_completion * COMPLETION_PRICE
            + web_uses * WEB_SEARCH_PRICE,
            6,
        )

    def _resolve_reserve(self, payload: dict) -> float:
        if self._scenario is None:
            raise RuntimeError("scenario not set before request")
        if payload.get("model") != FRONTIER_MODEL:
            raise RuntimeError(f"model mismatch: expected {FRONTIER_MODEL}, got {payload.get('model')}")
        cap = self.per_scenario_caps.get(self._scenario, 0.0)
        reserve = self._compute_reserve(payload)
        if reserve > cap:
            raise RuntimeError(
                f"per-scenario reserve breach before call: scenario={self._scenario} "
                f"reserve=${reserve:.6f} cap=${cap:.6f}"
            )
        if self.cumulative + reserve > self.total_cap:
            raise RuntimeError(
                f"Frontier cap exceeded: {self.cumulative:.6f} + {reserve:.6f} > {self.total_cap:.6f}"
            )
        return reserve

    def _parse_actual(self, resp: httpx.Response) -> dict[str, Any]:
        data = resp.json()
        usage = data.get("usage") or {}
        cost = data.get("cost") or usage.get("cost")
        model = data.get("model", "unknown")
        tool_details = usage.get("server_tool_use_details") or {}
        web_uses = tool_details.get("web_search_requests") or 0
        return {
            "cost": float(cost) if cost is not None else None,
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "web_uses": web_uses,
            "data": data,
        }

    def _compute_cost_from_tokens(self, usage: dict) -> float:
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        return round(prompt * PROMPT_PRICE + completion * COMPLETION_PRICE, 6)

    def __enter__(self) -> "M6FrontierGuard":
        guard = self

        def _guarded_post(client: httpx.Client, url: str, **kwargs: Any) -> Any:
            resolved = str(client.base_url).rstrip("/") + "/" + url.lstrip("/") if not url.startswith("http") else url
            if "openrouter.ai" not in resolved or "/chat/completions" not in resolved:
                return guard._original_post(client, url, **kwargs)
            payload = kwargs.get("json") or {}
            reserve = guard._resolve_reserve(payload)
            result = guard._original_post(client, url, **kwargs)
            actual = guard._parse_actual(result)
            cost = actual["cost"]
            if cost is None:
                cost = guard._compute_cost_from_tokens(actual)
            guard.cumulative += cost
            guard.calls += 1
            guard.scenario_spent[guard._scenario] = guard.scenario_spent.get(guard._scenario, 0.0) + cost
            client._m6_last_actual = actual

            cap = guard.per_scenario_caps.get(guard._scenario, 0.0)
            if guard.scenario_spent[guard._scenario] > cap:
                raise RuntimeError(
                    f"per-scenario cost breach after call: scenario={guard._scenario} "
                    f"spent=${guard.scenario_spent[guard._scenario]:.6f} cap=${cap:.6f}"
                )
            if actual["web_uses"] > guard._count_web_uses(payload):
                raise RuntimeError(
                    f"web_search uses exceeded: {actual['web_uses']} > {guard._count_web_uses(payload)} for {guard._scenario}"
                )
            return result

        def _guarded_stream(client: httpx.Client, method: str, url: str, **kwargs: Any) -> Any:
            resolved = str(client.base_url).rstrip("/") + "/" + url.lstrip("/") if not url.startswith("http") else url
            if "openrouter.ai" in resolved and "/chat/completions" in resolved:
                raise RuntimeError("streaming not allowed for M6 Frontier; use non-stream tools/annotations")
            return guard._original_stream(client, method, url, **kwargs)

        httpx.Client.post = _guarded_post
        httpx.Client.stream = _guarded_stream
        return self

    def __exit__(self, *exc: object) -> None:
        httpx.Client.post = self._original_post
        httpx.Client.stream = self._original_stream


def _get_product_context(product_ids: list[str]) -> str:
    return product_db.get_scoped_context_text(product_ids)


def _get_product_image_paths(product_id: str) -> list[str]:
    orch = Orchestrator(brand_dir="brand", product_id=product_id)
    return list(orch._get_product_image_paths())


def _build_frontier_messages(scenario: dict) -> tuple[list[dict], list[str]]:
    product_ids = scenario.get("product_ids") or [scenario["product_id"]]
    text = _get_product_context(product_ids)
    image_paths = _get_product_image_paths(product_ids[0])
    brand_guidelines = _get_brand_guidelines()
    resource_context = scenario.get("resource_context", "") or ""
    if brand_guidelines:
        resource_context = f"{brand_guidelines}\n\n{resource_context}" if resource_context else brand_guidelines
    resource_context = (
        f"--- กฎข้อมูลต้นทาง ---\n{SOURCE_GROUNDED_RULE}\n\n"
        + (resource_context or "")
    )
    full_user = scenario["user_prefix"] + text
    if resource_context.strip():
        full_user += f"\n\n{resource_context.strip()}"
    if scenario["quick_brief"]:
        full_user += f"\n\nคำขอเฉพาะ: {scenario['quick_brief']}"
    content = build_multimodal_content(full_user, tuple(image_paths[:2]))
    messages = [
        {"role": "system", "content": scenario["system"] + "\n\n" + SOURCE_GROUNDED_RULE},
        {"role": "user", "content": content},
    ]
    return messages, image_paths


def _run_frontier_scenario(scenario: dict, guard: M6FrontierGuard, dry_run: bool = False) -> RunResult:
    guard.set_scenario(scenario["id"])
    messages, image_paths = _build_frontier_messages(scenario)
    tools = [
        {
            "type": "openrouter:web_search",
            "parameters": {"max_uses": scenario["frontier_web_uses"]},
        }
    ] if scenario["frontier_web_uses"] > 0 else None
    return_annotations = scenario["frontier_web_uses"] > 0

    if dry_run:
        return RunResult(
            scenario_id=scenario["id"],
            side="Frontier",
            output="(dry-run: no API call)",
            actual_model=FRONTIER_MODEL,
            cost_usd=0.0,
            prompt_tokens=scenario["frontier_input_tokens"],
            completion_tokens=scenario["frontier_output_tokens"],
            web_uses=scenario["frontier_web_uses"],
            error=None,
            stopped=False,
            evidence={"messages": messages, "image_paths": image_paths, "tools": tools},
        )

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    llm = LLMClient(
        api_key=api_key,
        default_model=FRONTIER_MODEL,
        timeout=180,
    )
    try:
        if return_annotations:
            text, annotations = llm.chat(
                messages,
                model=FRONTIER_MODEL,
                temperature=0.7,
                max_tokens=scenario["frontier_output_tokens"],
                max_retry_limit=0,
                stream=False,
                tools=tools,
                source="m6_frontier",
                return_annotations=True,
            )
            output = text
            evidence = {"messages": messages, "image_paths": image_paths, "tools": tools, "annotations": annotations}
        else:
            text = llm.chat(
                messages,
                model=FRONTIER_MODEL,
                temperature=0.7,
                max_tokens=scenario["frontier_output_tokens"],
                max_retry_limit=0,
                stream=False,
                tools=tools,
                source="m6_frontier",
            )
            output = text
            evidence = {"messages": messages, "image_paths": image_paths, "tools": tools}

        actual = getattr(llm, "_last_raw_response", {}) or {}
        usage = actual.get("usage") or {}
        tool_details = usage.get("server_tool_use_details") or {}
        web_uses = tool_details.get("web_search_requests") or 0
        cost = actual.get("cost") or usage.get("cost")
        if cost is None:
            cost = (usage.get("prompt_tokens", 0) * PROMPT_PRICE) + (usage.get("completion_tokens", 0) * COMPLETION_PRICE)
            cost = round(float(cost), 6)
        else:
            cost = round(float(cost), 6)
        return RunResult(
            scenario_id=scenario["id"],
            side="Frontier",
            output=output or "(empty output)",
            actual_model=actual.get("model", "unknown"),
            cost_usd=cost,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            web_uses=web_uses,
            error=None,
            stopped=False,
            evidence=evidence,
        )
    except Exception as exc:
        return RunResult(
            scenario_id=scenario["id"],
            side="Frontier",
            output="",
            actual_model="unknown",
            cost_usd=0.0,
            prompt_tokens=0,
            completion_tokens=0,
            web_uses=0,
            error=f"{type(exc).__name__}: {exc}",
            stopped=True,
            evidence={"messages": messages, "image_paths": image_paths, "tools": tools, "traceback": traceback.format_exc()},
        )
    finally:
        llm.close()


def _run_mktapp_scenario(scenario: dict, dry_run: bool = False) -> RunResult:
    brand_guidelines = _get_brand_guidelines()
    raw_resource_context = scenario.get("resource_context", "") or ""
    agent_settings_override = _parse_agent_settings_override(raw_resource_context, scenario["agent_key"])
    # Strip typed agent settings from the prompt resource_context; those travel via
    # the typed UI/Agent Settings contract (agent_settings_override). Keep only
    # source-grounded rule + brand guidelines for the prompt.
    resource_context = re.sub(r"Agent settings:.*?(\n\n|\n---|$)", "", raw_resource_context, flags=re.DOTALL).strip()
    if brand_guidelines:
        resource_context = f"{brand_guidelines}\n\n{resource_context}" if resource_context else brand_guidelines
    resource_context = (
        f"--- กฎข้อมูลต้นทาง ---\n{SOURCE_GROUNDED_RULE}\n\n"
        + (resource_context or "")
    ).strip()
    quick_brief = scenario.get("quick_brief", "") or ""
    if dry_run:
        return RunResult(
            scenario_id=scenario["id"],
            side="MKTApp",
            output="(dry-run: no API call)",
            actual_model="google/gemini-3.7-flash",
            cost_usd=0.0,
            prompt_tokens=0,
            completion_tokens=0,
            web_uses=0,
            error=None,
            stopped=False,
            evidence={
                "resource_context": resource_context,
                "quick_brief": quick_brief,
                "agent_settings_override": agent_settings_override,
            },
        )
    try:
        # Backup UI-saved config so the harness does not leave production config changed.
        original_agent_instructions = ""
        if AGENT_INSTRUCTIONS_PATH.exists():
            original_agent_instructions = AGENT_INSTRUCTIONS_PATH.read_text(encoding="utf-8")
        try:
            evidence = qual_runner.run_case(
                case_id=f"M6_{scenario['id']}",
                agent_key=scenario["agent_key"],
                product_id=scenario["product_id"],
                quick_brief=quick_brief,
                resource_context=resource_context,
                stage="A",
                product_ids=scenario.get("product_ids"),
                platforms=scenario.get("platforms"),
                auto_image=scenario.get("auto_image"),
                context={"use_competitor": scenario["agent_key"] != "product_spec", "use_campaign": scenario["agent_key"] in ("campaign_strategy", "content_creator")},
                agent_settings_override=agent_settings_override if agent_settings_override else None,
            )
        finally:
            if original_agent_instructions:
                AGENT_INSTRUCTIONS_PATH.write_text(original_agent_instructions, encoding="utf-8")
        return RunResult(
            scenario_id=scenario["id"],
            side="MKTApp",
            output=evidence.get("result_text", "") or "(no output)",
            actual_model=evidence.get("models_used", ["unknown"])[-1] if evidence.get("models_used") else "unknown",
            cost_usd=round(float(evidence.get("run_cost_usd", 0) or 0), 6),
            prompt_tokens=sum(int(e.get("prompt_tokens", 0) or 0) for e in evidence.get("usage_entries", [])),
            completion_tokens=sum(int(e.get("completion_tokens", 0) or 0) for e in evidence.get("usage_entries", [])),
            web_uses=sum(1 for e in evidence.get("usage_entries", []) if e.get("source") == "web_search"),
            error=evidence.get("error"),
            stopped=bool(evidence.get("error")),
            evidence=evidence,
        )
    except Exception as exc:
        return RunResult(
            scenario_id=scenario["id"],
            side="MKTApp",
            output="",
            actual_model="unknown",
            cost_usd=0.0,
            prompt_tokens=0,
            completion_tokens=0,
            web_uses=0,
            error=f"{type(exc).__name__}: {exc}",
            stopped=True,
            evidence={"traceback": traceback.format_exc()},
        )


def _write_evidence(
    run_dir: Path,
    mktapp_results: list[RunResult],
    frontier_results: list[RunResult],
    stopped: bool,
    stop_reason: str,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    inputs_dir = run_dir / "inputs"
    outputs_dir = run_dir / "outputs"
    inputs_dir.mkdir(exist_ok=True)
    outputs_dir.mkdir(exist_ok=True)

    mktapp_map: dict[str, RunResult] = {r.scenario_id: r for r in mktapp_results}
    frontier_map: dict[str, RunResult] = {r.scenario_id: r for r in frontier_results}

    # Write exact input packs (redact secrets)
    inputs: dict[str, Any] = {}
    for s in SCENARIOS:
        product_ids = s.get("product_ids") or [s["product_id"]]
        text = _get_product_context(product_ids)
        image_paths = _get_product_image_paths(product_ids[0])
        inputs[s["id"]] = {
            "agent_key": s["agent_key"],
            "product_ids": product_ids,
            "quick_brief": s["quick_brief"],
            "resource_context": s["resource_context"],
            "platforms": s.get("platforms"),
            "auto_image": s.get("auto_image"),
            "product_context_text_length": len(text),
            "product_image_paths": image_paths,
            "frontier_messages_text_length": len(text) + len(s["system"]) + len(s["user_prefix"]),
        }
    (inputs_dir / "m6_inputs.json").write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")

    # Write outputs with X/Y labels
    mapping: dict[str, dict[str, str]] = {}
    for s in SCENARIOS:
        sid = s["id"]
        m = mktapp_map.get(sid)
        f = frontier_map.get(sid)
        if m is None or f is None:
            continue
        if random.random() < 0.5:
            a, b = m, f
            a_label, b_label = "X", "Y"
        else:
            a, b = f, m
            a_label, b_label = "X", "Y"
        (outputs_dir / f"{sid}_X.txt").write_text(a.output or "(no output)", encoding="utf-8")
        (outputs_dir / f"{sid}_Y.txt").write_text(b.output or "(no output)", encoding="utf-8")
        mapping[sid] = {
            a_label: a.side,
            b_label: b.side,
            "mktapp_model": m.actual_model,
            "frontier_model": f.actual_model,
        }
    (run_dir / "m6_mapping_secret.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")

    # Evidence summary
    summary = {
        "run_id": run_dir.name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stopped": stopped,
        "stop_reason": stop_reason,
        "approval_cap": APPROVAL_CAP,
        "scenarios": [],
    }
    for s in SCENARIOS:
        sid = s["id"]
        m = mktapp_map.get(sid)
        f = frontier_map.get(sid)
        summary["scenarios"].append({
            "id": sid,
            "mktapp_cost": m.cost_usd if m else None,
            "mktapp_model": m.actual_model if m else None,
            "frontier_cost": f.cost_usd if f else None,
            "frontier_model": f.actual_model if f else None,
            "frontier_web_uses": f.web_uses if f else None,
        })
    (run_dir / "m6_evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # Blind review scorecard template
    headers = ["scenario", "dimension", "X_score", "Y_score", "reviewer", "timestamp"]
    rows = []
    for dim in ["Usefulness", "Factuality", "Instruction following", "Brand / asset fit", "Evidence quality", "User effort"]:
        for s in SCENARIOS:
            rows.append(f"{s['id']},{dim},,,,")
    (run_dir / "m6_scorecard.csv").write_text("\n".join([",".join(headers)] + rows), encoding="utf-8")


def _preflight_check() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    # Ensure product data exists
    for s in SCENARIOS:
        pids = s.get("product_ids") or [s["product_id"]]
        ctx = _get_product_context(pids)
        if not ctx.strip():
            raise RuntimeError(f"No product context for {pids}")


def _preflight_budget_check() -> tuple[bool, str, float, float]:
    """Return (ok, message, required_cap, actual_cap) for worst-case budget.

    Does not change APPROVAL_CAP. If the combined worst-case plan is unsafe,
    the message gives Product Owner choices.
    """
    mktapp_total_reserve = round(sum(s["mktapp_reserve"] for s in SCENARIOS), 6)
    frontier_total_reserve = round(sum(s["frontier_reserve"] for s in SCENARIOS), 6)
    required = round(mktapp_total_reserve + frontier_total_reserve + CONTINGENCY, 6)
    if required <= APPROVAL_CAP:
        return True, "", required, APPROVAL_CAP

    msg = (
        f"M6.1 worst-case budget plan exceeds approved cap: "
        f"MKTApp reserve ${mktapp_total_reserve:.6f} + "
        f"Frontier reserve ${frontier_total_reserve:.6f} + "
        f"contingency ${CONTINGENCY:.6f} = "
        f"${required:.6f} > approval cap ${APPROVAL_CAP:.6f}.\n"
        "Product Owner choices:\n"
        f"  A) Keep ${APPROVAL_CAP:.2f} cap: harness blocks this M6.1 plan.\n"
        f"  B) Approve new cap: minimum required ${required:.6f} "
        f"(or higher with more contingency).\n"
        "No paid calls made."
    )
    return False, msg, required, APPROVAL_CAP


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Build inputs/reserves without paid calls")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "m6_frontier_uat")
    parser.add_argument("--approval-cap", type=float, default=float(os.environ.get("M6_APPROVAL_CAP", "2.0")), help="Total M6.1 approval cap (default from env M6_APPROVAL_CAP)")
    args = parser.parse_args()

    global APPROVAL_CAP
    APPROVAL_CAP = args.approval_cap

    _preflight_check()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = args.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    mktapp_results: list[RunResult] = []
    frontier_results: list[RunResult] = []
    stopped = False
    stop_reason = ""

    # Combined worst-case budget gate (before any paid call)
    if not args.dry_run:
        budget_ok, budget_msg, required, _ = _preflight_budget_check()
        if not budget_ok:
            stopped = True
            stop_reason = budget_msg
            _write_evidence(run_dir, mktapp_results, frontier_results, stopped, stop_reason)
            print(budget_msg)
            return 1

    # 1) MKTApp side
    for scenario in SCENARIOS:
        if stopped:
            break
        r = _run_mktapp_scenario(scenario, dry_run=args.dry_run)
        mktapp_results.append(r)
        if r.error:
            stopped = True
            stop_reason = f"MKTApp {scenario['id']} error: {r.error}"
            break

    mktapp_total = round(sum(r.cost_usd for r in mktapp_results), 6)
    remaining = round(APPROVAL_CAP - mktapp_total, 6)
    frontier_total_reserve = round(sum(s["frontier_reserve"] for s in SCENARIOS), 6)

    if not stopped and frontier_total_reserve > remaining:
        stopped = True
        stop_reason = f"Frontier total reserve {frontier_total_reserve} exceeds remaining cap {remaining} after MKTApp {mktapp_total}"

    # 2) Frontier side
    if not stopped:
        frontier_reserves = {s["id"]: s["frontier_reserve"] for s in SCENARIOS}
        frontier_guard = M6FrontierGuard(total_cap=remaining, per_scenario_caps=frontier_reserves)
        with frontier_guard:
            for scenario in SCENARIOS:
                r = _run_frontier_scenario(scenario, frontier_guard, dry_run=args.dry_run)
                frontier_results.append(r)
                if r.error or r.stopped:
                    stopped = True
                    stop_reason = f"Frontier {scenario['id']} stopped: {r.error}"
                    break

    _write_evidence(run_dir, mktapp_results, frontier_results, stopped, stop_reason)

    print(f"Run: {run_dir}")
    print(f"Stopped: {stopped} | Reason: {stop_reason}")
    print(f"MKTApp total: ${mktapp_total:.6f}")
    frontier_total = round(sum(r.cost_usd for r in frontier_results), 6)
    print(f"Frontier total: ${frontier_total:.6f}")
    print(f"Grand total: ${round(mktapp_total + frontier_total, 6):.6f}")
    return 0 if not stopped else 1


if __name__ == "__main__":
    sys.exit(main())
