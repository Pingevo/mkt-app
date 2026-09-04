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
import subprocess
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


def _git_head() -> str:
    """Return current git HEAD commit hash (short) for evidence recording."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(PROJECT_ROOT),
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


# M6 production remediation baseline — the commit that contains the M6 fixes.
# The actual execution HEAD may be newer (test-only commits), but this is the
# production-code baseline that the M6.1 rerun is measuring.
M6_REMEDIATION_BASELINE = "61b6d93bb0a4389eac1bb9ff936ce6a46004ce23"


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

    @property
    def is_charged_but_invalid(self) -> bool:
        """A response that was received and charged but then invalidated
        (e.g. post-call cap breach). Detected via cost > 0 + error present.
        Such results must never be used for judging."""
        return self.cost_usd > 0 and self.error is not None


class M6FrontierGuard:
    """Hard guard for Frontier side.

    Design (M6.1 corrected):
    - Per-scenario amounts are pre-call estimates/reserves, NOT post-call
      rejection thresholds. A paid response is never discarded solely
      because its actual cost exceeded the estimate.
    - The global frontier budget is the real hard limit. Before each call,
      the guard checks that cumulative + conservative reserve + safety margin
      does not exceed the frontier budget (which excludes the judge reserve).
    - After a response is received, its output, cost, model, tokens, web uses,
      and request ID are always preserved in ``last_response_audit``.
    - If actual cost exceeds the per-scenario estimate, a variance warning is
      recorded but the response is returned normally.
    - If insufficient global budget remains for the NEXT call, the guard
      raises before that next call, not after the current one.
    - Model lock, max_tokens, and web-use limits are still enforced.
    """

    SAFETY_MARGIN = 0.02  # extra dollar margin on top of computed reserve

    def __init__(
        self,
        frontier_budget: float,
        per_scenario_estimates: dict[str, float],
    ) -> None:
        self.frontier_budget = frontier_budget
        self.per_scenario_estimates = per_scenario_estimates
        self.cumulative = 0.0
        self.calls = 0
        self.scenario_spent: dict[str, float] = {}
        self.variance_warnings: list[dict[str, Any]] = []
        self._scenario: str | None = None
        self._original_post = httpx.Client.post
        self._original_stream = httpx.Client.stream
        # Full audit trail of the last response for this scenario
        self.last_response_audit: dict[str, dict[str, Any]] = {}

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
        """Pre-call check: model lock + global budget feasibility.

        Does NOT reject based on per-scenario estimate — that is a planning
        hint, not a hard limit. The hard limit is the global frontier budget.
        """
        if self._scenario is None:
            raise RuntimeError("scenario not set before request")
        if payload.get("model") != FRONTIER_MODEL:
            raise RuntimeError(f"model mismatch: expected {FRONTIER_MODEL}, got {payload.get('model')}")
        reserve = self._compute_reserve(payload)
        required = round(reserve + self.SAFETY_MARGIN, 6)
        if self.cumulative + required > self.frontier_budget:
            raise RuntimeError(
                f"Frontier budget insufficient before call: "
                f"cumulative=${self.cumulative:.6f} + reserve=${reserve:.6f} + "
                f"margin=${self.SAFETY_MARGIN:.6f} = ${self.cumulative + required:.6f} > "
                f"budget=${self.frontier_budget:.6f}"
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
            "request_id": data.get("id"),
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
            reserve = guard._resolve_reserve(payload)  # pre-call check only
            result = guard._original_post(client, url, **kwargs)
            actual = guard._parse_actual(result)
            cost = actual["cost"]
            if cost is None:
                cost = guard._compute_cost_from_tokens(actual)
            guard.cumulative += cost
            guard.calls += 1
            guard.scenario_spent[guard._scenario] = guard.scenario_spent.get(guard._scenario, 0.0) + cost
            client._m6_last_actual = actual

            # Preserve full audit evidence for this response (no secrets)
            output_text = ""
            choices = actual.get("data", {}).get("choices") or []
            if choices:
                output_text = choices[0].get("message", {}).get("content", "") or ""
            guard.last_response_audit[guard._scenario] = {
                "cost": cost,
                "model": actual.get("model", "unknown"),
                "prompt_tokens": actual.get("prompt_tokens", 0),
                "completion_tokens": actual.get("completion_tokens", 0),
                "web_uses": actual.get("web_uses", 0),
                "request_id": actual.get("request_id"),
                "output": output_text,
            }

            # Record variance warning if actual > estimate, but do NOT reject
            estimate = guard.per_scenario_estimates.get(guard._scenario, 0.0)
            if cost > estimate:
                guard.variance_warnings.append({
                    "scenario": guard._scenario,
                    "estimate": estimate,
                    "actual": cost,
                    "variance": round(cost - estimate, 6),
                })

            # Web-use limit is an integrity check, not a cost check
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
        # If the guard recorded audit data for this scenario, the response
        # was received and charged. Preserve it with the error.
        audit = guard.last_response_audit.get(scenario["id"])
        if audit:
            return RunResult(
                scenario_id=scenario["id"],
                side="Frontier",
                output=audit["output"] or "(no output)",
                actual_model=audit["model"],
                cost_usd=audit["cost"],
                prompt_tokens=audit["prompt_tokens"],
                completion_tokens=audit["completion_tokens"],
                web_uses=audit["web_uses"],
                error=f"{type(exc).__name__}: {exc}",
                stopped=True,
                evidence={
                    "messages": messages,
                    "image_paths": image_paths,
                    "tools": tools,
                    "request_id": audit.get("request_id"),
                    "traceback": traceback.format_exc(),
                },
            )
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
    execution_mode: str = "paid",
    valid_for_judging: bool = True,
    authorized: bool = False,
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

    # Write outputs independently (before X/Y pairing) so that
    # successfully completed outputs are never lost on a partial run.
    mktapp_dir = outputs_dir / "mktapp"
    frontier_dir = outputs_dir / "frontier"
    mktapp_dir.mkdir(exist_ok=True)
    frontier_dir.mkdir(exist_ok=True)
    for sid, m in mktapp_map.items():
        if m is not None:
            (mktapp_dir / f"{sid}.txt").write_text(m.output or "(no output)", encoding="utf-8")
    for sid, f in frontier_map.items():
        if f is not None:
            (frontier_dir / f"{sid}.txt").write_text(f.output or "(no output)", encoding="utf-8")

    # Write outputs with X/Y labels ONLY when:
    # 1. execution_mode == "paid" (never for dry-run)
    # 2. valid_for_judging is True
    # 3. Both sides completed
    # 4. Neither side is charged-but-invalid
    mapping: dict[str, dict[str, str]] = {}
    if execution_mode == "paid" and valid_for_judging:
        for s in SCENARIOS:
            sid = s["id"]
            m = mktapp_map.get(sid)
            f = frontier_map.get(sid)
            if m is None or f is None:
                continue
            if f.is_charged_but_invalid:
                continue
            if m.is_charged_but_invalid:
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

    # Evidence summary — includes execution_mode and valid_for_judging
    summary = {
        "run_id": run_dir.name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stopped": stopped,
        "stop_reason": stop_reason,
        "approval_cap": APPROVAL_CAP,
        "execution_head": _git_head(),
        "m6_remediation_baseline": M6_REMEDIATION_BASELINE,
        "execution_mode": execution_mode,
        "valid_for_judging": valid_for_judging and execution_mode == "paid",
        "authorized": authorized,
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
            "frontier_charged_but_invalid": f.is_charged_but_invalid if f else False,
            "is_dry_run_placeholder": execution_mode == "dry_run" and f is not None,
        })
    (run_dir / "m6_evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # Blind review scorecard template (only for paid valid runs)
    if execution_mode == "paid" and valid_for_judging:
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


# ---------------------------------------------------------------------------
# Recovery, resume, and verification — bounded continuation after a
# technical interruption.
#
# Design:
# - Two-commit identity: source_execution_head (where outputs were generated)
#   vs continuation_harness_head (current commit with harness fixes).
# - Production code/config fingerprint must be identical between the two.
# - Only evaluation harness/tests may differ.
# - Reused outputs are verified by SHA-256 against a recovery manifest.
# - Cumulative cost = historical_sunk_cost + continuation_incremental + judge.
#   Reused output costs are provenance only, never double-counted.
# - Judge budget is reserved before any Frontier continuation call.
# ---------------------------------------------------------------------------

import hashlib
import math


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_tree_hash(paths: list[Path]) -> str:
    """Compute a deterministic fingerprint over a set of production files."""
    import hashlib as hl
    h = hl.sha256()
    for p in sorted(paths, key=lambda x: str(x)):
        if p.exists():
            h.update(str(p.relative_to(PROJECT_ROOT)).encode())
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


PRODUCTION_FINGERPRINT_PATHS = [
    PROJECT_ROOT / "src" / "agents" / "base_agent.py",
    PROJECT_ROOT / "src" / "agents" / "product_spec.py",
    PROJECT_ROOT / "src" / "agents" / "competitor_analysis.py",
    PROJECT_ROOT / "src" / "agents" / "competitor_evidence.py",
    PROJECT_ROOT / "src" / "agents" / "campaign_strategy.py",
    PROJECT_ROOT / "src" / "agents" / "content_creator.py",
    PROJECT_ROOT / "src" / "output_validators.py",
    PROJECT_ROOT / "src" / "llm_client.py",
    PROJECT_ROOT / "src" / "orchestrator.py",
    PROJECT_ROOT / "config" / "agents.yaml",
    PROJECT_ROOT / "config" / "qualification.yaml",
]


def _production_fingerprint() -> str:
    """Fingerprint of all production code/config files that M6 measures."""
    return _git_tree_hash(PRODUCTION_FINGERPRINT_PATHS)


def _scenario_fingerprint() -> str:
    """Fingerprint of the COMPLETE scenario contract.

    Includes system prompt, user prefix, quick brief, resource context,
    products, platform/image settings, model, max tokens, web-use limits,
    and the source-grounding rule. These are the essential Frontier prompts.
    """
    return _sha256_bytes(json.dumps({
        "scenarios": SCENARIOS,
        "source_grounded_rule": SOURCE_GROUNDED_RULE,
        "frontier_model": FRONTIER_MODEL,
    }, ensure_ascii=False, sort_keys=True).encode())


def _input_pack_fingerprint() -> str:
    """Fingerprint of the exact evaluated input material.

    Covers product context text, brand guideline files, product image
    content hashes (bytes, not just paths), and agent settings/resource
    context per scenario. Fails if a required image disappears or changes.
    """
    parts: list[str] = []
    for s in SCENARIOS:
        pids = s.get("product_ids") or [s["product_id"]]
        parts.append(f"--- {s['id']} product_context ---")
        parts.append(_get_product_context(pids))
        parts.append(f"--- {s['id']} images ---")
        try:
            image_paths = _get_product_image_paths(pids[0])
            for img_path in image_paths:
                p = Path(img_path)
                if p.exists():
                    img_bytes = p.read_bytes()
                    parts.append(json.dumps({
                        "path": str(p.relative_to(PROJECT_ROOT)) if str(p).startswith(str(PROJECT_ROOT)) else str(p),
                        "size": len(img_bytes),
                        "sha256": _sha256_bytes(img_bytes),
                    }, ensure_ascii=False))
                else:
                    parts.append(json.dumps({"path": str(p), "missing": True}))
        except Exception:
            parts.append("(error reading image paths)")
        parts.append(f"--- {s['id']} resource_context ---")
        parts.append(s.get("resource_context", "") or "")
        parts.append(f"--- {s['id']} quick_brief ---")
        parts.append(s.get("quick_brief", "") or "")
    parts.append("--- brand_guidelines ---")
    parts.append(_get_brand_guidelines())
    return _sha256_bytes("\n===\n".join(parts).encode())


# Expected hashes for the historical run 20260904_070830 recovery
EXPECTED_MKTAPP_HASHES = {
    "S1": "fb16b06e9c586e7147ca6cf3b1a07f667f889d7540ead70a3170746b6466f9d3",
    "S2": "45cf98f9a95d1163b356ec2b65921e690af3913fb250295ce553d436b841ae30",
    "S3": "64c57400e1985e0298c611d1c68b361ae97dc7cdae62e9682717d81b7b8d5e98",
    "S4": "731805b2fe4805d2fb785d07952e7bc2d929553d7a02dd07b4f87ed1a1e6fb7d",
}
EXPECTED_FRONTIER_S1_HASH = "05bf1a33da296439bc85dd2fab32cfc453af4d06b64dc11c16f8faed75b31af0"

# Historical charged-invalid Frontier S2 (content was discarded, not recoverable)
HISTORICAL_FRONTIER_S2_COST = 0.601450

# Total historical sunk cost (includes charged-invalid S2)
HISTORICAL_SUNK_COST = round(
    0.030999 + 0.183729 + 0.028928 + 0.040330  # MKTApp S1-S4
    + 0.219400  # Frontier S1
    + HISTORICAL_FRONTIER_S2_COST,  # Frontier S2 (charged, discarded)
    6,
)

# Default judge reserve — can be overridden via --judge-reserve CLI flag.
# This is NOT an implicit spending ceiling. The cumulative ceiling is
# controlled exclusively by --approval-cap for paid runs.
DEFAULT_JUDGE_RESERVE = 0.20

# Proposed (NOT authorized) planning ceiling — shown in dry-run only.
# The Product Owner must explicitly pass --approval-cap for any paid run.
PROPOSED_CEILING = 2.86


# Files that are allowed to change between source HEAD and continuation HEAD.
# Only evaluation harness and tests may differ. Everything else is blocked.
ALLOWED_CHANGED_PATHS = {
    "scripts/m6_frontier_uat.py",
    "scripts/m6_judge_runner.py",
    "tests/test_m6_frontier_uat.py",
    "tests/test_m6_judge_runner.py",
}


def _verify_clean_evaluation_inputs() -> None:
    """Verify that evaluation-relevant production/input/brand/product-data
    paths have no uncommitted changes. Recovery must bind to a clean source state."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, timeout=5,
        cwd=str(PROJECT_ROOT),
    )
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        # Git porcelain format: "XY PATH" where XY is 2-char status, then space, then path.
        # But the path itself may start with a letter that looks like a status code.
        # Use split with maxsplit=1 on the first space after position 1.
        # Robust approach: strip first 2 chars (status), then strip leading spaces.
        if len(line) < 4:
            continue
        path = line[2:].lstrip()
        # Allow harness/test changes (those will be committed separately)
        if path in ALLOWED_CHANGED_PATHS:
            continue
        # Allow reports and documentation (not evaluation inputs)
        if path.startswith("M6_") or (path.endswith(".md") and "M6" in path):
            continue  # M6 reports
        if path.endswith(".html") or path.endswith(".md"):
            continue  # other reports/docs
        if path.startswith("data/") or path.startswith("cache/"):
            continue  # runtime artifacts
        if path.startswith("evaluation_artifacts/"):
            continue
        # Block any change to production/input/brand/config paths
        raise RuntimeError(
            f"Uncommitted change to evaluation-relevant file: {path}. "
            f"Recovery requires clean production/input state. "
            f"Commit or stash changes to production files before recovery."
        )


def _verify_source_to_continuation_diff(source_head: str, continuation_head: str) -> None:
    """Verify that the diff between source HEAD and continuation HEAD contains
    ONLY approved evaluation harness/test files. Reject any production,
    product-data, brand, prompt, config, or unrelated change."""
    result = subprocess.run(
        ["git", "diff", "--name-only", source_head, continuation_head],
        capture_output=True, text=True, timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    changed = set(line.strip() for line in result.stdout.strip().splitlines() if line.strip())
    unapproved = changed - ALLOWED_CHANGED_PATHS
    if unapproved:
        raise RuntimeError(
            f"Unapproved changes between source HEAD {source_head[:8]} and "
            f"continuation HEAD {continuation_head[:8]}: {sorted(unapproved)}. "
            f"Only {sorted(ALLOWED_CHANGED_PATHS)} may differ."
        )


def recover_historical_run(source_dir: Path) -> dict[str, Any]:
    """Recover reusable outputs from the historical run 20260904_070830.

    This is an offline migration step. It:
    1. Verifies current HEAD equals the source execution HEAD.
    2. Verifies all production/input/brand/product-data paths are clean
       (no uncommitted changes to evaluation-relevant files).
    3. Reads MKTApp S1-S4 from the qualification run_outputs directory.
    4. Reads Frontier S1 from the blind output using the source mapping.
    5. Verifies all SHA-256 hashes against expected values.
    6. Creates outputs/mktapp/, outputs/frontier/, output_hashes.json.
    7. Creates recovery_manifest.json with full provenance.
    8. Records Frontier S2 as charged-but-invalid.
    9. Fails closed if any hash differs or file is missing.

    Does NOT alter any recovered output bytes. Does NOT require an API key.
    """
    source_evidence_path = source_dir / "m6_evidence.json"
    if not source_evidence_path.exists():
        raise RuntimeError(f"Source run has no m6_evidence.json: {source_dir}")
    source_evidence = json.loads(source_evidence_path.read_text(encoding="utf-8"))

    mapping_path = source_dir / "m6_mapping_secret.json"
    if not mapping_path.exists():
        raise RuntimeError(f"Source run has no m6_mapping_secret.json: {source_dir}")
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))

    # --- Bind to source execution state ---
    source_head = source_evidence.get("execution_head", "")
    current_head = _git_head()
    if source_head != current_head:
        raise RuntimeError(
            f"Recovery requires current HEAD to equal source execution HEAD. "
            f"Source: {source_head}, current: {current_head}. "
            f"Checkout the source commit before running recovery."
        )

    # Verify evaluation-relevant production paths are clean (no uncommitted changes)
    _verify_clean_evaluation_inputs()

    # Create recovery directories
    mktapp_dir = source_dir / "outputs" / "mktapp"
    frontier_dir = source_dir / "outputs" / "frontier"
    mktapp_dir.mkdir(parents=True, exist_ok=True)
    frontier_dir.mkdir(parents=True, exist_ok=True)

    # 1. Recover MKTApp S1-S4 from qualification run_outputs
    mktapp_source_base = PROJECT_ROOT / "data" / "all_agents_beta_qualification" / "run_outputs"
    recovered_mktapp: dict[str, dict[str, Any]] = {}
    for sid in ("S1", "S2", "S3", "S4"):
        source_path = mktapp_source_base / f"M6_{sid}_output.txt"
        if not source_path.exists():
            raise RuntimeError(f"Missing MKTApp source file: {source_path}")
        content = source_path.read_bytes()
        actual_hash = _sha256_bytes(content)
        expected_hash = EXPECTED_MKTAPP_HASHES[sid]
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"MKTApp {sid} hash mismatch: expected {expected_hash}, got {actual_hash}"
            )
        dest = mktapp_dir / f"{sid}.txt"
        if not dest.exists() or _sha256_file(dest) != expected_hash:
            dest.write_bytes(content)  # copy without altering bytes
        scenario_ev = next((sc for sc in source_evidence.get("scenarios", []) if sc.get("id") == sid), {})
        recovered_mktapp[sid] = {
            "source_path": str(source_path.relative_to(PROJECT_ROOT)),
            "dest_path": str(dest.relative_to(PROJECT_ROOT)),
            "sha256": actual_hash,
            "original_timestamp": source_path.stat().st_mtime,
            "cost_usd": scenario_ev.get("mktapp_cost"),
            "model": scenario_ev.get("mktapp_model"),
        }

    # 2. Recover Frontier S1 from blind output using mapping
    recovered_frontier: dict[str, dict[str, Any]] = {}
    s1_mapping = mapping.get("S1", {})
    s1_frontier_label = None
    for label, side in [("X", s1_mapping.get("X")), ("Y", s1_mapping.get("Y"))]:
        if side == "Frontier":
            s1_frontier_label = label
            break
    if s1_frontier_label is None:
        raise RuntimeError("Cannot find Frontier label in S1 mapping")
    s1_frontier_path = source_dir / "outputs" / f"S1_{s1_frontier_label}.txt"
    if not s1_frontier_path.exists():
        raise RuntimeError(f"Missing Frontier S1 blind output: {s1_frontier_path}")
    s1_content = s1_frontier_path.read_bytes()
    s1_hash = _sha256_bytes(s1_content)
    if s1_hash != EXPECTED_FRONTIER_S1_HASH:
        raise RuntimeError(
            f"Frontier S1 hash mismatch: expected {EXPECTED_FRONTIER_S1_HASH}, got {s1_hash}"
        )
    s1_dest = frontier_dir / "S1.txt"
    if not s1_dest.exists() or _sha256_file(s1_dest) != EXPECTED_FRONTIER_S1_HASH:
        s1_dest.write_bytes(s1_content)
    s1_scenario_ev = next((sc for sc in source_evidence.get("scenarios", []) if sc.get("id") == "S1"), {})
    recovered_frontier["S1"] = {
        "source_path": str(s1_frontier_path.relative_to(PROJECT_ROOT)),
        "dest_path": str(s1_dest.relative_to(PROJECT_ROOT)),
        "sha256": s1_hash,
        "original_timestamp": s1_frontier_path.stat().st_mtime,
        "cost_usd": s1_scenario_ev.get("frontier_cost"),
        "model": s1_scenario_ev.get("frontier_model"),
        "blind_label": s1_frontier_label,
    }

    # 3. Record Frontier S2 as charged-but-invalid
    charged_invalid: dict[str, dict[str, Any]] = {
        "S2": {
            "cost_usd": HISTORICAL_FRONTIER_S2_COST,
            "model": "anthropic/claude-fable-5.1",
            "status": "charged_but_invalid",
            "reason": "Post-call cap breach discarded the response; content not recoverable.",
            "reusable": False,
        },
    }

    # 4. Write output_hashes.json
    hashes = {
        "mktapp": {sid: recovered_mktapp[sid]["sha256"] for sid in recovered_mktapp},
        "frontier": {sid: recovered_frontier[sid]["sha256"] for sid in recovered_frontier},
    }
    (source_dir / "output_hashes.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")

    # 5. Write recovery_manifest.json
    manifest = {
        "source_run_id": source_dir.name,
        "recovery_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_execution_head": source_evidence.get("execution_head"),
        "m6_remediation_baseline": source_evidence.get("m6_remediation_baseline"),
        "production_fingerprint": _production_fingerprint(),
        "scenario_fingerprint": _scenario_fingerprint(),
        "input_pack_fingerprint": _input_pack_fingerprint(),
        "recovered_mktapp": recovered_mktapp,
        "recovered_frontier": recovered_frontier,
        "charged_invalid_frontier": charged_invalid,
        "historical_sunk_cost": HISTORICAL_SUNK_COST,
        "expected_mktapp_hashes": EXPECTED_MKTAPP_HASHES,
        "expected_frontier_s1_hash": EXPECTED_FRONTIER_S1_HASH,
    }
    (source_dir / "recovery_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return manifest


def _verify_recovery_manifest(source_dir: Path) -> dict[str, Any]:
    """Load and fail-closed verify a recovery manifest.

    Every check must pass. Missing manifest, missing hashes, tampered outputs,
    fingerprint mismatches — all raise RuntimeError.
    """
    manifest_path = source_dir / "recovery_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"No recovery_manifest.json in {source_dir}. Run recovery first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Verify MKTApp outputs exist and match hashes recorded in manifest
    for sid, info in manifest.get("recovered_mktapp", {}).items():
        path = source_dir / "outputs" / "mktapp" / f"{sid}.txt"
        if not path.exists():
            raise RuntimeError(f"Recovered MKTApp {sid} missing: {path}")
        actual = _sha256_file(path)
        expected = info["sha256"]
        if actual != expected:
            raise RuntimeError(f"MKTApp {sid} hash mismatch: expected {expected}, got {actual}")

    # Verify Frontier outputs
    for sid, info in manifest.get("recovered_frontier", {}).items():
        path = source_dir / "outputs" / "frontier" / f"{sid}.txt"
        if not path.exists():
            raise RuntimeError(f"Recovered Frontier {sid} missing: {path}")
        actual = _sha256_file(path)
        expected = info["sha256"]
        if actual != expected:
            raise RuntimeError(f"Frontier {sid} hash mismatch: expected {expected}, got {actual}")

    # Verify production fingerprint is unchanged
    current_pf = _production_fingerprint()
    if current_pf != manifest.get("production_fingerprint"):
        raise RuntimeError(
            f"Production fingerprint mismatch: manifest has "
            f"{manifest.get('production_fingerprint', '?')[:16]}..., "
            f"current is {current_pf[:16]}..."
        )

    # Verify scenario fingerprint
    current_sf = _scenario_fingerprint()
    if current_sf != manifest.get("scenario_fingerprint"):
        raise RuntimeError("Scenario fingerprint mismatch — scenario contract changed")

    # Verify input pack fingerprint
    current_ipf = _input_pack_fingerprint()
    if current_ipf != manifest.get("input_pack_fingerprint"):
        raise RuntimeError("Input pack fingerprint mismatch — source data changed")

    # Verify M6 baseline
    if manifest.get("m6_remediation_baseline") != M6_REMEDIATION_BASELINE:
        raise RuntimeError("M6 remediation baseline mismatch in manifest")

    # Verify source-to-continuation diff contains only approved files
    source_head = manifest.get("source_execution_head", "")
    current_head = _git_head()
    if source_head and current_head and source_head != current_head:
        _verify_source_to_continuation_diff(source_head, current_head)

    return manifest


def _load_reused_outputs(
    source_dir: Path,
    manifest: dict[str, Any],
) -> tuple[dict[str, RunResult], dict[str, RunResult], dict[str, dict[str, Any]]]:
    """Load reusable outputs from a verified recovery manifest.

    Returns (mktapp_map, frontier_map, charged_invalid_map).
    Reused RunResults have cost_usd set to 0.0 (provenance cost is in the
    manifest, not double-counted in continuation incremental cost).
    """
    mktapp_map: dict[str, RunResult] = {}
    frontier_map: dict[str, RunResult] = {}

    for sid, info in manifest.get("recovered_mktapp", {}).items():
        path = source_dir / "outputs" / "mktapp" / f"{sid}.txt"
        mktapp_map[sid] = RunResult(
            scenario_id=sid,
            side="MKTApp",
            output=path.read_text(encoding="utf-8"),
            actual_model=info.get("model", "unknown"),
            cost_usd=0.0,  # provenance cost in manifest, not double-counted
            prompt_tokens=0,
            completion_tokens=0,
            web_uses=0,
            error=None,
            stopped=False,
            evidence={"reused_from": str(source_dir.name), "original_cost": info.get("cost_usd")},
        )

    for sid, info in manifest.get("recovered_frontier", {}).items():
        path = source_dir / "outputs" / "frontier" / f"{sid}.txt"
        frontier_map[sid] = RunResult(
            scenario_id=sid,
            side="Frontier",
            output=path.read_text(encoding="utf-8"),
            actual_model=info.get("model", "unknown"),
            cost_usd=0.0,  # provenance cost in manifest, not double-counted
            prompt_tokens=0,
            completion_tokens=0,
            web_uses=0,
            error=None,
            stopped=False,
            evidence={"reused_from": str(source_dir.name), "original_cost": info.get("cost_usd")},
        )

    charged_invalid = manifest.get("charged_invalid_frontier", {})
    return mktapp_map, frontier_map, charged_invalid


def _write_output_hashes(run_dir: Path, mktapp_map: dict[str, RunResult], frontier_map: dict[str, RunResult]) -> None:
    """Write SHA-256 hashes of all outputs for resume verification."""
    hashes: dict[str, dict[str, str]] = {"mktapp": {}, "frontier": {}}
    for sid, r in mktapp_map.items():
        path = run_dir / "outputs" / "mktapp" / f"{sid}.txt"
        if path.exists():
            hashes["mktapp"][sid] = hashlib.sha256(path.read_bytes()).hexdigest()
    for sid, r in frontier_map.items():
        path = run_dir / "outputs" / "frontier" / f"{sid}.txt"
        if path.exists():
            hashes["frontier"][sid] = hashlib.sha256(path.read_bytes()).hexdigest()
    (run_dir / "output_hashes.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")


def _validate_approval_cap(cap: float | None, historical_sunk: float, judge_reserve: float,
                           is_paid: bool) -> tuple[float, str | None]:
    """Validate an approval cap. Returns (cap, error_message).

    For paid runs: cap must be a finite positive number, and must exceed
    historical_sunk + judge_reserve. For dry-run: cap may be None (uses proposed).
    """
    if not is_paid:
        if cap is None:
            return PROPOSED_CEILING, None
        if not _is_finite_non_negative(cap):
            return PROPOSED_CEILING, f"Invalid approval cap (not finite non-negative): {cap}"
        return float(cap), None

    # Paid run — strict validation
    if cap is None:
        return 0.0, "Paid run requires explicit --approval-cap. None provided."
    if not math.isfinite(cap):
        return 0.0, f"Invalid approval cap (not finite): {cap}"
    if cap <= 0:
        return 0.0, f"Invalid approval cap (must be positive): {cap}"
    minimum = round(historical_sunk + judge_reserve, 6)
    if cap < minimum:
        return 0.0, (
            f"Approval cap ${cap:.6f} below minimum required "
            f"(sunk ${historical_sunk:.6f} + judge ${judge_reserve:.6f} = ${minimum:.6f})"
        )
    return float(cap), None


def _is_finite_non_negative(val: float) -> bool:
    return isinstance(val, (int, float)) and math.isfinite(val) and val >= 0


def _validate_judge_reserve(reserve: float) -> str | None:
    if not _is_finite_non_negative(reserve):
        return f"Invalid judge reserve (must be finite non-negative): {reserve}"
    return None


def _validate_reserve_overrides(overrides_json: str | None) -> tuple[dict[str, float], str | None]:
    """Validate frontier reserve overrides. Returns (overrides, error)."""
    if overrides_json is None:
        return {}, None
    try:
        overrides = json.loads(overrides_json)
    except json.JSONDecodeError as e:
        return {}, f"Invalid --frontier-reserve-override JSON: {e}"
    if not isinstance(overrides, dict):
        return {}, "Reserve overrides must be a JSON object"
    valid_scenario_ids = {s["id"] for s in SCENARIOS}
    validated: dict[str, float] = {}
    for sid, val in overrides.items():
        if sid not in valid_scenario_ids:
            return {}, f"Unknown scenario in reserve override: {sid}"
        if not _is_finite_non_negative(val):
            return {}, f"Invalid reserve override for {sid}: {val}"
        validated[sid] = float(val)
    return validated, None


def _verify_paid_continuation_clean(source_head: str) -> str | None:
    """Verify that paid continuation runs from a committed clean harness,
    distinct from the source execution HEAD.

    Returns error message or None if OK.
    - Current HEAD must differ from source execution HEAD
    - No tracked working-tree changes (untracked data/reports allowed)
    - Source-to-continuation diff contains only approved files
    """
    current_head = _git_head()
    if current_head == source_head:
        return (
            f"Paid continuation cannot run from source execution HEAD ({source_head[:8]}...). "
            f"Commit harness fixes to a new commit first, then run paid resume."
        )

    # Check for tracked working-tree changes
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, timeout=5,
        cwd=str(PROJECT_ROOT),
    )
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        status_code = line[:2]
        path = line[2:].lstrip()
        # Untracked files (??) are allowed
        if status_code == "??":
            continue
        # Tracked modifications are NOT allowed for paid runs
        return (
            f"Paid continuation requires clean working tree. "
            f"Tracked file modified: {path}. Commit or stash before paid resume."
        )

    # Verify source-to-continuation diff
    _verify_source_to_continuation_diff(source_head, current_head)
    return None


def _compute_payload_reserve(scenario: dict[str, Any]) -> float:
    """Compute a payload-derived reserve for a Frontier scenario.

    Uses the scenario's max tokens and an estimated prompt size to compute
    a conservative cost reserve based on the model's pricing.
    """
    # Estimate prompt tokens from system + user_prefix + product context
    pids = scenario.get("product_ids") or [scenario["product_id"]]
    product_text = _get_product_context(pids)
    prompt_text = scenario.get("system", "") + scenario.get("user_prefix", "") + product_text
    estimated_prompt_tokens = max(1, len(prompt_text) // 3)  # conservative char-to-token
    max_completion = int(scenario.get("frontier_output_tokens", 4500))
    # Use Frontier model pricing (conservative: $5/M prompt, $15/M completion)
    prompt_price = 5.0 / 1_000_000
    completion_price = 15.0 / 1_000_000
    return round(estimated_prompt_tokens * prompt_price + max_completion * completion_price, 6)


def _write_audit_evidence(
    run_dir: Path,
    guard: "M6FrontierGuard | None",
    mktapp_map: dict[str, RunResult],
    frontier_map: dict[str, RunResult],
    execution_mode: str = "paid",
    valid_for_judging: bool = True,
    reserve_decisions: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Persist per-scenario audit evidence: request ID, actual model, actual cost,
    tokens, web uses, reserve estimate, variance, error/stopped status, provenance,
    execution mode, and pre-call reserve decisions."""
    audit: dict[str, Any] = {
        "execution_mode": execution_mode,
        "valid_for_judging": valid_for_judging and execution_mode == "paid",
        "scenarios": [],
    }
    for s in SCENARIOS:
        sid = s["id"]
        m = mktapp_map.get(sid)
        f = frontier_map.get(sid)
        entry: dict[str, Any] = {"id": sid}

        if m:
            entry["mktapp"] = {
                "model": m.actual_model,
                "cost_usd": m.cost_usd,
                "prompt_tokens": m.prompt_tokens,
                "completion_tokens": m.completion_tokens,
                "web_uses": m.web_uses,
                "error": m.error,
                "stopped": m.stopped,
                "reused": bool(m.evidence.get("reused_from")),
                "original_cost": m.evidence.get("original_cost"),
            }
        if f:
            entry["frontier"] = {
                "model": f.actual_model,
                "cost_usd": f.cost_usd,
                "prompt_tokens": f.prompt_tokens,
                "completion_tokens": f.completion_tokens,
                "web_uses": f.web_uses,
                "error": f.error,
                "stopped": f.stopped,
                "reused": bool(f.evidence.get("reused_from")),
                "original_cost": f.evidence.get("original_cost"),
                "is_dry_run_placeholder": execution_mode == "dry_run" and not f.evidence.get("reused_from"),
            }
        if guard and sid in guard.last_response_audit:
            a = guard.last_response_audit[sid]
            entry["frontier_audit"] = {
                "request_id": a.get("request_id"),
                "actual_cost": a.get("cost"),
                "actual_model": a.get("model"),
                "prompt_tokens": a.get("prompt_tokens"),
                "completion_tokens": a.get("completion_tokens"),
                "web_uses": a.get("web_uses"),
            }
        if reserve_decisions and sid in reserve_decisions:
            entry["reserve_decision"] = reserve_decisions[sid]
        audit["scenarios"].append(entry)

    if guard and guard.variance_warnings:
        audit["variance_warnings"] = guard.variance_warnings

    (run_dir / "m6_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Build inputs/reserves without paid calls")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "m6_frontier_uat")
    parser.add_argument("--approval-cap", type=float, default=None,
                        help="Explicit cumulative program ceiling for paid runs. "
                             "REQUIRED for paid resume. Not required for dry-run or recovery.")
    parser.add_argument("--judge-reserve", type=float, default=DEFAULT_JUDGE_RESERVE,
                        help=f"Judge budget to protect before Frontier calls (default ${DEFAULT_JUDGE_RESERVE:.2f})")
    parser.add_argument("--resume-from", type=str, default=None,
                        help="Resume from a prior run ID. Reuses completed valid outputs, "
                             "executes only missing/invalid sides. Requires a verified "
                             "recovery_manifest.json in the source run directory.")
    parser.add_argument("--recover", action="store_true",
                        help="Run offline recovery/migration on the source run specified by "
                             "--resume-from. Creates outputs/mktapp/, outputs/frontier/, "
                             "output_hashes.json, and recovery_manifest.json. No paid calls. "
                             "Does not require an API key.")
    parser.add_argument("--frontier-reserve-override", type=str, default=None,
                        help="JSON dict of per-scenario Frontier reserve overrides (e.g. "
                             "'{\"S2\":0.65}'). Only used in resume mode.")
    args = parser.parse_args()

    global APPROVAL_CAP
    if args.approval_cap is not None:
        APPROVAL_CAP = args.approval_cap

    judge_reserve = args.judge_reserve

    # --- Recovery mode (offline, no paid calls, no API key needed) ---
    if args.recover:
        if not args.resume_from:
            print("--recover requires --resume-from <run_id>")
            return 1
        source_dir = args.output_dir / args.resume_from
        if not source_dir.exists():
            print(f"Source run not found: {source_dir}")
            return 1
        manifest = recover_historical_run(source_dir)
        print(f"Recovery complete for: {args.resume_from}")
        print(f"  Recovered MKTApp: {sorted(manifest['recovered_mktapp'].keys())}")
        print(f"  Recovered Frontier: {sorted(manifest['recovered_frontier'].keys())}")
        print(f"  Charged-invalid Frontier: {sorted(manifest['charged_invalid_frontier'].keys())}")
        print(f"  Historical sunk cost: ${manifest['historical_sunk_cost']:.6f}")
        print(f"  Production fingerprint: {manifest['production_fingerprint'][:16]}...")
        print(f"  Scenario fingerprint: {manifest['scenario_fingerprint'][:16]}...")
        print(f"  Input pack fingerprint: {manifest['input_pack_fingerprint'][:16]}...")
        print(f"  Source execution HEAD: {manifest['source_execution_head']}")
        print(f"  No API key required. No paid calls made.")
        return 0

    # --- Validate numeric arguments ---
    is_paid = not args.dry_run
    judge_reserve = args.judge_reserve
    reserve_err = _validate_judge_reserve(judge_reserve)
    if reserve_err:
        print(f"ERROR: {reserve_err}", file=sys.stderr)
        return 1

    overrides, override_err = _validate_reserve_overrides(args.frontier_reserve_override)
    if override_err:
        print(f"ERROR: {override_err}", file=sys.stderr)
        return 1

    # --- For paid resume, require explicit approval cap ---
    if args.resume_from and is_paid and args.approval_cap is None:
        print("ERROR: Paid resume requires explicit --approval-cap.", file=sys.stderr)
        print("  The cumulative program ceiling is NOT hardcoded.", file=sys.stderr)
        print(f"  Proposed (NOT authorized) ceiling: ${PROPOSED_CEILING:.2f}", file=sys.stderr)
        print("  Pass --approval-cap <value> to authorize spending.", file=sys.stderr)
        return 1

    # For non-resume paid runs, use env default if not specified
    if args.approval_cap is None and is_paid and not args.resume_from:
        env_cap = os.environ.get("M6_APPROVAL_CAP")
        if env_cap:
            args.approval_cap = float(env_cap)
        else:
            print("ERROR: Paid non-resume run requires --approval-cap or M6_APPROVAL_CAP env.", file=sys.stderr)
            return 1

    if is_paid:
        _preflight_check()

    # --- Resume mode ---
    source_manifest: dict[str, Any] | None = None
    charged_invalid_map: dict[str, dict[str, Any]] = {}
    historical_sunk = 0.0
    if args.resume_from:
        source_dir = args.output_dir / args.resume_from
        if not source_dir.exists():
            print(f"Source run not found: {source_dir}")
            return 1
        # Fail-closed verification of recovery manifest
        source_manifest = _verify_recovery_manifest(source_dir)
        mktapp_map, frontier_map, charged_invalid_map = _load_reused_outputs(source_dir, source_manifest)
        historical_sunk = source_manifest.get("historical_sunk_cost", 0.0)

        source_head = source_manifest.get("source_execution_head", "")
        current_head = _git_head()
        print(f"Resume from: {args.resume_from}")
        print(f"  Source execution HEAD: {source_head}")
        print(f"  Continuation harness HEAD: {current_head}")
        if source_head != current_head:
            print("  Note: HEAD differs — verified production fingerprint + diff instead.")

        # --- Blocker 3: Paid continuation requires committed clean harness ---
        if is_paid:
            clean_err = _verify_paid_continuation_clean(source_head)
            if clean_err:
                print(f"ERROR: {clean_err}", file=sys.stderr)
                return 1
            print(f"  Verified: clean committed continuation harness.")

        print(f"  Historical sunk cost: ${historical_sunk:.6f}")
        print(f"  Reusing MKTApp: {sorted(mktapp_map.keys())}")
        print(f"  Reusing Frontier: {sorted(frontier_map.keys())}")
        print(f"  Charged-invalid Frontier: {sorted(charged_invalid_map.keys())}")
        missing_mktapp = [s["id"] for s in SCENARIOS if s["id"] not in mktapp_map]
        missing_frontier = [s["id"] for s in SCENARIOS if s["id"] not in frontier_map]
        print(f"  Missing MKTApp: {missing_mktapp}")
        print(f"  Missing Frontier: {missing_frontier}")
    else:
        mktapp_map: dict[str, RunResult] = {}
        frontier_map: dict[str, RunResult] = {}

    # --- Blocker 4: Validate approval cap with full context ---
    validated_cap, cap_err = _validate_approval_cap(
        args.approval_cap, historical_sunk, judge_reserve, is_paid
    )
    if cap_err:
        print(f"ERROR: {cap_err}", file=sys.stderr)
        return 1
    APPROVAL_CAP = validated_cap

    # Determine authorization metadata
    if is_paid:
        authorized = True
        execution_mode = "paid"
        valid_for_judging = True
        ceiling_label = "approved"
        cumulative_ceiling = float(APPROVAL_CAP)
    else:
        authorized = False
        execution_mode = "dry_run"
        valid_for_judging = False
        ceiling_label = "PROPOSED (NOT AUTHORIZED)"
        cumulative_ceiling = float(APPROVAL_CAP)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if args.resume_from:
        run_id = f"{run_id}_resume_from_{args.resume_from}"
    run_dir = args.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    stopped = False
    stop_reason = ""

    # --- Budget gate ---
    if args.resume_from:
        print(f"  Cumulative ceiling ({ceiling_label}): ${cumulative_ceiling:.6f}")
        frontier_budget = round(cumulative_ceiling - historical_sunk - judge_reserve, 6)
        print(f"  Judge reserve (protected): ${judge_reserve:.6f}")
        print(f"  Frontier budget (ceiling - sunk - judge): ${frontier_budget:.6f}")
        if frontier_budget <= 0:
            stopped = True
            stop_reason = (
                f"No Frontier budget after sunk ${historical_sunk:.6f} + "
                f"judge reserve ${judge_reserve:.6f} >= ceiling ${cumulative_ceiling:.6f}"
            )
            _write_evidence(run_dir, list(mktapp_map.values()), list(frontier_map.values()),
                            stopped, stop_reason, execution_mode, valid_for_judging, authorized)
            print(stop_reason)
            return 1
    else:
        if args.dry_run:
            frontier_budget = round(APPROVAL_CAP - sum(s["mktapp_reserve"] for s in SCENARIOS), 6)
        else:
            budget_ok, budget_msg, required, _ = _preflight_budget_check()
            if not budget_ok:
                stopped = True
                stop_reason = budget_msg
                _write_evidence(run_dir, list(mktapp_map.values()), list(frontier_map.values()),
                                stopped, stop_reason, execution_mode, valid_for_judging, authorized)
                print(budget_msg)
                return 1
            frontier_budget = round(APPROVAL_CAP - sum(s["mktapp_reserve"] for s in SCENARIOS), 6)

    # --- Compute missing Frontier estimates with overrides applied ---
    frontier_estimates = {s["id"]: s["frontier_reserve"] for s in SCENARIOS}
    for sid, val in overrides.items():
        frontier_estimates[sid] = val

    missing_frontier_ids = [s["id"] for s in SCENARIOS if s["id"] not in frontier_map]
    missing_estimates = {sid: frontier_estimates[sid] for sid in missing_frontier_ids}
    missing_reserve_total = round(sum(missing_estimates.values()), 6)

    # --- Blocker 2: Compute both estimate-only and safety-inclusive totals ---
    safety_margin = M6FrontierGuard.SAFETY_MARGIN
    num_missing = len(missing_frontier_ids)
    total_safety_margins = round(safety_margin * num_missing, 6)

    # Per-call conservative reserve: max(payload-derived, scenario estimate) + safety margin
    reserve_decisions: dict[str, dict[str, Any]] = {}
    conservative_reserve_total = 0.0
    for sid in missing_frontier_ids:
        scenario = next(s for s in SCENARIOS if s["id"] == sid)
        payload_reserve = _compute_payload_reserve(scenario)
        scenario_estimate = missing_estimates[sid]
        chosen_reserve = round(max(payload_reserve, scenario_estimate) + safety_margin, 6)
        conservative_reserve_total = round(conservative_reserve_total + chosen_reserve, 6)
        reserve_decisions[sid] = {
            "payload_derived_reserve": payload_reserve,
            "scenario_estimate": scenario_estimate,
            "chosen_conservative_reserve": chosen_reserve,
            "safety_margin": safety_margin,
            "max_tokens": scenario.get("frontier_output_tokens"),
            "web_uses": scenario.get("frontier_web_uses"),
        }

    estimate_only_cumulative = round(historical_sunk + missing_reserve_total + judge_reserve, 6)
    safety_inclusive_cumulative = round(historical_sunk + conservative_reserve_total + judge_reserve, 6)

    # Print dry-run plan details
    if args.dry_run and args.resume_from:
        print(f"\n--- DRY-RUN PLAN (no paid calls) ---")
        print(f"  Execution mode: {execution_mode}")
        print(f"  Valid for judging: {valid_for_judging}")
        print(f"  Authorized: {authorized}")
        print(f"  MKTApp calls: 0 (all reused)")
        print(f"  Frontier reused: {sorted(frontier_map.keys())}")
        print(f"  Frontier planned calls: {missing_frontier_ids}")
        print(f"  Judge calls: 0 (during this command)")
        print(f"  Historical sunk cost: ${historical_sunk:.6f}")
        print(f"  Planning ceiling: ${cumulative_ceiling:.6f}")
        print(f"  Protected judge reserve: ${judge_reserve:.6f}")
        print(f"  Remaining Frontier budget: ${frontier_budget:.6f}")
        for sid in missing_frontier_ids:
            rd = reserve_decisions[sid]
            print(f"  {sid}: estimate=${rd['scenario_estimate']:.6f}, "
                  f"payload_reserve=${rd['payload_derived_reserve']:.6f}, "
                  f"chosen=${rd['chosen_conservative_reserve']:.6f}, "
                  f"max_tokens={rd['max_tokens']}, web_uses={rd['web_uses']}")
        print(f"  Estimate-only cumulative: ${estimate_only_cumulative:.6f}")
        print(f"  Safety-inclusive cumulative: ${safety_inclusive_cumulative:.6f}")
        print(f"  Safety margins total: ${total_safety_margins:.6f} ({num_missing} calls x ${safety_margin:.6f})")
        print(f"  Margin under proposed $2.86: ${round(PROPOSED_CEILING - safety_inclusive_cumulative, 6):.6f}")
        print(f"--- END DRY-RUN PLAN ---\n")

    # --- 1) MKTApp side — only run missing scenarios ---
    continuation_incremental = 0.0
    for scenario in SCENARIOS:
        if stopped:
            break
        sid = scenario["id"]
        if sid in mktapp_map:
            continue
        r = _run_mktapp_scenario(scenario, dry_run=args.dry_run)
        mktapp_map[sid] = r
        continuation_incremental = round(continuation_incremental + r.cost_usd, 6)
        if r.error:
            stopped = True
            stop_reason = f"MKTApp {sid} error: {r.error}"
            break

    # --- 2) Frontier side — only run missing scenarios ---
    guard: M6FrontierGuard | None = None
    if not stopped:
        remaining_frontier = round(frontier_budget - continuation_incremental, 6)

        # Pre-call feasibility: use conservative reserve (not just estimate)
        if not args.dry_run and conservative_reserve_total > remaining_frontier:
            stopped = True
            stop_reason = (
                f"Conservative Frontier reserve {conservative_reserve_total:.6f} exceeds remaining "
                f"{remaining_frontier:.6f} (judge reserve ${judge_reserve:.2f} protected, "
                f"safety margins ${total_safety_margins:.6f} included)"
            )
        else:
            guard = M6FrontierGuard(
                frontier_budget=remaining_frontier,
                per_scenario_estimates=missing_estimates,
            )
            with guard:
                for scenario in SCENARIOS:
                    sid = scenario["id"]
                    if sid in frontier_map:
                        continue
                    r = _run_frontier_scenario(scenario, guard, dry_run=args.dry_run)
                    frontier_map[sid] = r
                    continuation_incremental = round(continuation_incremental + r.cost_usd, 6)
                    if r.error or r.stopped:
                        stopped = True
                        stop_reason = f"Frontier {sid} stopped: {r.error}"
                        break
                for w in guard.variance_warnings:
                    print(f"  [VARIANCE] {w['scenario']}: estimate=${w['estimate']:.6f} "
                          f"actual=${w['actual']:.6f} variance=+${w['variance']:.6f}")

    # --- Convert maps to lists for evidence ---
    mktapp_results = [mktapp_map.get(s["id"]) for s in SCENARIOS if mktapp_map.get(s["id"])]
    frontier_results = [frontier_map.get(s["id"]) for s in SCENARIOS if frontier_map.get(s["id"])]

    _write_evidence(run_dir, mktapp_results, frontier_results, stopped, stop_reason,
                    execution_mode, valid_for_judging, authorized)
    _write_output_hashes(run_dir, mktapp_map, frontier_map)
    _write_audit_evidence(run_dir, guard, mktapp_map, frontier_map,
                          execution_mode, valid_for_judging, reserve_decisions)

    # --- Resume link and cumulative accounting ---
    if args.resume_from and source_manifest:
        link = {
            "source_run": args.resume_from,
            "source_execution_head": source_manifest.get("source_execution_head"),
            "m6_remediation_baseline": source_manifest.get("m6_remediation_baseline"),
            "continuation_harness_head": _git_head(),
            "production_fingerprint": _production_fingerprint(),
            "scenario_fingerprint": _scenario_fingerprint(),
            "input_pack_fingerprint": _input_pack_fingerprint(),
            "historical_sunk_cost": historical_sunk,
            "continuation_incremental_cost": continuation_incremental,
            "charged_invalid_frontier": charged_invalid_map,
            "cumulative_program_cost": round(historical_sunk + continuation_incremental, 6),
            "judge_incremental_cost": 0.0,  # filled by judge runner
            "judge_reserve": judge_reserve,
            "execution_mode": execution_mode,
            "valid_for_judging": valid_for_judging,
            "authorized": authorized,
            # Blocker 4: separate planning from authorization
            "planning_ceiling": cumulative_ceiling if not is_paid else None,
            "approved_cumulative_ceiling": float(APPROVAL_CAP) if is_paid else None,
            # Blocker 2: both totals recorded
            "estimate_only_cumulative": estimate_only_cumulative,
            "safety_inclusive_cumulative": safety_inclusive_cumulative,
            "reserve_decisions": reserve_decisions,
        }
        (run_dir / "resume_link.json").write_text(json.dumps(link, indent=2), encoding="utf-8")

    print(f"Run: {run_dir}")
    print(f"Stopped: {stopped} | Reason: {stop_reason}")
    print(f"Execution mode: {execution_mode} | Valid for judging: {valid_for_judging} | Authorized: {authorized}")
    print(f"Continuation incremental: ${continuation_incremental:.6f}")
    frontier_incremental = round(sum(r.cost_usd for r in frontier_results if not r.evidence.get("reused_from")), 6)
    print(f"Frontier incremental: ${frontier_incremental:.6f}")
    if args.resume_from:
        cumulative = round(historical_sunk + continuation_incremental, 6)
        print(f"Historical sunk: ${historical_sunk:.6f}")
        print(f"Cumulative program cost: ${cumulative:.6f}")
        print(f"Judge reserve protected: ${judge_reserve:.2f}")
        print(f"Cumulative ceiling ({ceiling_label}): ${cumulative_ceiling:.6f}")
        print(f"Estimate-only cumulative: ${estimate_only_cumulative:.6f}")
        print(f"Safety-inclusive cumulative: ${safety_inclusive_cumulative:.6f}")
        print(f"Remaining to ceiling after judge: ${round(cumulative_ceiling - cumulative - judge_reserve, 6):.6f}")
    else:
        print(f"Grand total: ${continuation_incremental:.6f}")
    return 0 if not stopped else 1



if __name__ == "__main__":
    sys.exit(main())
