"""Acceptance runner for competitor_analysis BETA evaluation.

Usage:
    python tests/acceptance_competitor_runner.py --case 1 --repeats 2 --model openrouter/free
    python tests/acceptance_competitor_runner.py --case 10 --repeats 1 --model openrouter/free

Artifacts are saved to `output/acceptance/` (gitignored) with no secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow `python tests/acceptance_competitor_runner.py` from repo root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from src.agents.competitor_analysis import CompetitorAnalysisAgent


class _DummyLLM:
    """LLM stand-in for offline acceptance checks."""
    def chat(self, *args, **kwargs):
        return ""
    def close(self):
        pass
from src.config_loader import get_agent_config, get_env, load_config
from src import flow_context as _flow_context
import src.ai_usage as _ai_usage
from src.llm_client import LLMClient
from src.output_validators import validate_output

load_dotenv()

OUTPUT_DIR = Path("output/acceptance")

PRODUCT_SPEC_K77 = """สินค้า: CACGO K77
รหัสสินค้า: K77
หมวดหมู่: สมาร์ทวอทช์ (smartwatch)
หน้าจอ 1.7" full round IPS ความละเอียด 360x360
CPU Realtek 8773EWE-VP
Bluetooth 5.0
เซนเซอร์ Heart Rate + SpO2
กันน้ำ IP68
แบตเตอรี่ 1000mAh
โหมดกีฬา 100+ โหมด
ฟังก์ชัน Flashlight, BT calling, BT music
ตัวเรือนสี Black/Silver
สายซิลิโคน 22mm ถอดได้
ราคาต้นทุน FOB US$15.00"""

CASES: dict[int, dict[str, Any]] = {
    1: {
        "name": "k77_vs_k771",
        "description": "K77 หลัก vs K771 คู่แข่งใกล้เคียง — ต้องไม่สับสนรุ่น/หมวด",
        "product_spec": PRODUCT_SPEC_K77,
        "competitor_data": "K771",
        "user_prompt": "วิเคราะห์คู่แข่ง K77",
        "quick_brief": "เปรียบเทียบกับรุ่น K771 โดยเฉพาะ อย่าสับสนระหว่าง K77 กับ K771",
        "web_search": False,
    },
    2: {
        "name": "k77_identity_explicit_competitors",
        "description": "product identity ชัดเจน + คู่แข่งที่ผู้ใช้ระบุเอง",
        "product_spec": PRODUCT_SPEC_K77,
        "competitor_data": "Redmi Watch 3 Active\nHaylou Solar Plus RT3\nMibro Watch A2",
        "user_prompt": "วิเคราะห์คู่แข่ง CACGO K77",
        "quick_brief": "เน้นสินค้า CACGO K77 เป็นหลัก อย่าสับสนกับรุ่นอื่น",
        "web_search": False,
    },
    3: {
        "name": "k77_insufficient_data",
        "description": "ข้อมูลไม่พอ — ต้องคืน limited analysis โดยไม่แต่งข้อมูล",
        "product_spec": PRODUCT_SPEC_K77,
        "competitor_data": "",
        "user_prompt": "วิเคราะห์ตลาดสมาร์ทวอทช์ K77 ในประเทศไทย เดือนมีนาคม 2027",
        "quick_brief": "ข้อมูลไม่พอให้ระบุอย่างตรงไปตรงมา ไม่ต้องเติมข้อมูล",
        "web_search": False,
    },
    10: {
        "name": "k77_thailand_web_search",
        "description": "web search จำกัด: K77 ราคา/คู่แข่งในประเทศไทย",
        "product_spec": PRODUCT_SPEC_K77,
        "competitor_data": (
            "# comparison scope (user-provided)\n"
            "Xiaomi Watch S3\n"
            "Kieslect AI Smartwatch Elite2 Lumina Edition\n"
            "Kieslect AI Smartwatch Elite2 Noir Edition\n"
            "Galaxy Watch9"
        ),
        "user_prompt": (
            "วิเคราะห์คู่แข่ง CACGO K77 ในประเทศไทย\n"
            "ค้นหา official Thailand page หรือ authorized Thai retailer ของแต่ละรุ่นทีระบุ\n"
            "ถ้าหาหลักฐานของรุ่นใดไม่พบ ให้บอกว่าไม่พบ ห้ามแทนที่ด้วยรุ่นอื่นหรือแหล่งทั่วไป\n"
            "ห้ามสร้างราคา สเปค หรือช่องทางจำหน่ายจากความจำ"
        ),
        "quick_brief": "เฉพาะประเทศไทย ค้นหา official/authorized Thai source ของรุ่นทีระบุ",
        "web_search": True,
    },
    11: {
        "name": "k77_k771_thailand_web_search",
        "description": "web search จำกัด: เปรียบเทียบ K77 กับ K771 ในประเทศไทย",
        "product_spec": PRODUCT_SPEC_K77,
        "competitor_data": "K771",
        "user_prompt": "วิเคราะห์คู่แข่ง CACGO K77",
        "quick_brief": "เฉพาะประเทศไทย เปรียบเทียบ K77 กับ K771",
        "web_search": True,
    },
}


def _setup_agent(agent_cfg: dict[str, Any], model: str) -> tuple[CompetitorAnalysisAgent, LLMClient]:
    cfg = load_config()
    defaults = cfg.get("defaults", {})
    agent_cfg = dict(agent_cfg)
    agent_cfg["max_review_iterations"] = 0
    agent_cfg.setdefault("max_retry_limit", 3)
    agent_cfg.setdefault("max_tokens", 4096)
    agent_cfg.setdefault("temperature", 0.4)
    agent_cfg["model"] = model
    # URL verification is tested separately; disable by default to keep cost minimal.
    agent_cfg["verify_urls"] = False
    # Evidence mode needs web_search; turn off when cases do not search.
    if not agent_cfg.get("web_search"):
        agent_cfg["evidence_mode"] = False
        agent_cfg.pop("response_format", None)

    llm = LLMClient(
        api_key=get_env("OPENROUTER_API_KEY"),
        base_url=defaults.get("base_url", "https://openrouter.ai/api/v1"),
        default_model=model,
        timeout=defaults.get("timeout_seconds", 120),
    )

    agent = CompetitorAnalysisAgent(agent_cfg, llm)
    return agent, llm


def _run_one(case_id: int, run: int, model: str, agent_cfg: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    prompt = ""  # filled after build_prompt
    call_log: list[dict[str, Any]] = []
    usage_log: list[dict[str, Any]] = []

    original_chat = LLMClient.chat
    original_log = LLMClient._log_usage

    # เก็บผล Hub POST สำหรับตรวจ receipt
    hub_results: list[dict[str, Any]] = []
    previous_callback = _ai_usage.HUB_POST_CALLBACK
    _ai_usage.HUB_POST_CALLBACK = lambda r: hub_results.append(r)

    # บังคับให้ทุก AI call ในครั้งนี้ถูกส่งเข้า local log + AI Usage Hub
    _flow_context.set_usage_actor("acceptance:competitor_analysis")
    _flow_context.set_usage_reference(f"acceptance:{case_id}:{run}")
    _flow_context.set_usage_metadata({"case_id": case_id, "case_name": case["name"], "run": run})

    def logged_chat(self, messages, *args, **kwargs):
        # Runner is non-interactive; non-streaming lets us capture usage and cost.
        if not kwargs.get("return_annotations"):
            kwargs.setdefault("stream", False)
        source = kwargs.get("source", "")
        t0 = time.monotonic()
        result = original_chat(self, messages, *args, **kwargs)
        duration_ms = int((time.monotonic() - t0) * 1000)
        text = result[0] if isinstance(result, tuple) else result
        annotations = result[1] if isinstance(result, tuple) else []
        raw = getattr(self, "_last_raw_response", None) or {}
        raw_msg = raw.get("choices", [{}])[0].get("message", {}) if raw else {}
        raw_annotations = raw_msg.get("annotations") or []
        usage = raw.get("usage") or {}
        tool_use = usage.get("server_tool_use_details")
        call_log.append({
            "source": source,
            "model": kwargs.get("model") or self._default_model,
            "tools": bool(kwargs.get("tools")),
            "return_annotations": bool(kwargs.get("return_annotations")),
            "duration_ms": duration_ms,
            "output_len": len(text) if text else 0,
            "annotations_count": len(annotations) if isinstance(annotations, list) else 0,
            "raw_annotations_count": len(raw_annotations) if isinstance(raw_annotations, list) else 0,
            "tool_use": tool_use,
        })
        return result

    def wrap_log(model_name, source, usage, *, duration_ms, attempt=1, status="success",
                 http_status=None, error_message=None, request_id=None):
        # เรียก logger ตัวจริงก่อนเสมอ → record_ai_usage ถูกเรียก
        original_log(
            model_name, source, usage,
            duration_ms=duration_ms, attempt=attempt, status=status,
            http_status=http_status, error_message=error_message, request_id=request_id,
        )
        usage_log.append({
            "model": model_name,
            "source": source,
            "attempt": attempt,
            "status": status,
            "http_status": http_status,
            "error_message": error_message,
            "request_id": request_id,
            "duration_ms": duration_ms,
            "usage": usage,
        })

    agent, llm = _setup_agent(agent_cfg, model)
    prompt = agent.build_prompt(case["product_spec"], case["competitor_data"])

    LLMClient.chat = logged_chat
    LLMClient._log_usage = staticmethod(wrap_log)

    hub_flushed = False
    t0 = time.monotonic()
    try:
        output = agent.run(prompt, quick_brief=case["quick_brief"])
        error = None
    except Exception as exc:
        output = ""
        error = f"{type(exc).__name__}: {exc}"
    finally:
        LLMClient.chat = original_chat
        LLMClient._log_usage = original_log
        _flow_context.clear_usage_context()
        # รอให้ Hub POST daemon threads ทำงานเสร็จก่อน process จบ (bounded)
        if _ai_usage is not None:
            hub_flushed = _ai_usage.flush_usage_log(timeout=20.0)
        _ai_usage.HUB_POST_CALLBACK = previous_callback
        llm.close()

    wall_ms = int((time.monotonic() - t0) * 1000)

    ok, err = validate_output(
        "competitor_analysis",
        output,
        required_sections=None,
        output_quality=agent_cfg.get("output_quality"),
    )

    # เก็บ diagnostic annotations สำหรับตรวจสอบ evidence ในภายหลัง
    relevant = getattr(agent, "_last_relevant_annotations", []) or []
    rejected = getattr(agent, "_last_rejected_annotations", []) or []
    tool_status = _classify_tool_status(call_log, relevant, rejected)

    if not hub_results:
        hub_status = "hub_timeout_unknown" if not hub_flushed else "hub_not_delivered"
    else:
        hub_status = hub_results[0].get("hub_status", "hub_unknown")

    return {
        "case_id": case_id,
        "case_name": case["name"],
        "run": run,
        "model": model,
        "web_search": agent_cfg.get("web_search", False),
        "product_spec": case["product_spec"],
        "competitor_data": case["competitor_data"],
        "actor": "acceptance:competitor_analysis",
        "reference": f"acceptance:{case_id}:{run}",
        "llm_calls": len(call_log),
        "call_log": call_log,
        "usage_log": usage_log,
        "relevant_annotations": relevant,
        "rejected_annotations": rejected,
        "tool_status": tool_status,
        "hub_flushed": hub_flushed,
        "hub_status": hub_status,
        "hub_results": hub_results,
        "total_cost_usd": _sum_cost(usage_log),
        "total_prompt_tokens": _sum_tokens(usage_log, "prompt_tokens"),
        "total_completion_tokens": _sum_tokens(usage_log, "completion_tokens"),
        "wall_ms": wall_ms,
        "output_len": len(output),
        "validation_ok": ok,
        "validation_error": err,
        "runtime_error": error,
        "output": output,
        "draft_output": getattr(agent, "_last_draft_output", ""),
        "generate_raw_response": getattr(agent, "_last_generate_raw", ""),
        "revise_raw_response": getattr(agent, "_last_revise_raw", ""),
        "validated_research_response": getattr(agent, "_last_validated_research_json", ""),
        "first_validation_error": getattr(agent, "_last_first_validation_error", ""),
        "request_id": (usage_log[0].get("request_id") if usage_log else None),
        "web_search_mode": agent_cfg.get("web_search_mode", "optional"),
        "system_prompt_len": len(agent._build_system_prompt()),
        "user_prompt_len": len(prompt),
    }


def _sum_cost(logs: list[dict[str, Any]]) -> float:
    total = 0.0
    for entry in logs:
        u = entry.get("usage") or {}
        if u.get("cost") is not None:
            total += float(u["cost"])
    return round(total, 6)


def _sum_tokens(logs: list[dict[str, Any]], key: str) -> int:
    total = 0
    for entry in logs:
        u = entry.get("usage") or {}
        if u.get(key) is not None:
            total += int(u[key])
    return total


def _classify_tool_status(call_log: list[dict[str, Any]], relevant: list, rejected: list) -> str:
    """ระบุสถานะ web-search tool จาก call log."""
    gen = next((c for c in call_log if c.get("source") == "competitor_analysis.generate"), {})
    tool_use = gen.get("tool_use") or {}
    raw_count = gen.get("raw_annotations_count", 0)
    ann_count = gen.get("annotations_count", 0)
    executed = tool_use.get("tool_calls_executed") or tool_use.get("web_search_requests") or 0

    if not gen.get("tools"):
        return "web_search_disabled"
    if not executed:
        return "tool_not_invoked"
    if raw_count == 0:
        return "tool_invoked_no_results"
    if ann_count == 0:
        return "response_parse_failure"
    if not relevant and not rejected:
        return "tool_invoked_no_annotations"
    if not relevant:
        return "evidence_rejected_by_relevance_gate"
    return "tool_invoked_with_relevant_annotations"


def _check_outcome(result: dict[str, Any]) -> dict[str, Any]:
    output = result.get("output", "")
    outcome: dict[str, Any] = {}

    # A table is only truncated if the final non-empty line starts with `|`
    # but does not end with `|` (i.e., it was cut mid-row).
    non_empty = [ln for ln in output.splitlines() if ln.strip()]
    last = non_empty[-1] if non_empty else ""
    truncated = bool(re.match(r"^\s*\|", last)) and not re.search(r"\|\s*$", last)

    # Basic quality gates from the BETA criteria
    outcome["not_blank"] = bool(output and output.strip())
    outcome["no_truncated_table"] = not truncated
    outcome["no_url_dump"] = not re.search(r"https?://\S+\s+https?://\S+\s+https?://\S+", output)
    outcome["no_verify_dump"] = "**URL ที่ verify ผ่าน" not in output
    outcome["mentions_k77"] = "K77" in output
    outcome["mentions_k771_if_relevant"] = "K771" in output if result["case_name"].startswith("k77_vs") or result["case_name"].endswith("k771_thailand") else None
    outcome["has_section_headings"] = bool(re.search(r"^#{1,3}\s+\S|^\*\*[\u0e00-\u0e7fA-Za-z].*\*\*\s*$", output, flags=re.MULTILINE))
    outcome["limited_analysis"] = bool(re.search(r"\*\*limited_analysis:\s*true\*\*", output))
    relevant = result.get("relevant_annotations") or []
    outcome["has_selected_evidence"] = len(relevant) > 0

    # Identify common defects
    defects: list[str] = []
    if not outcome["not_blank"]:
        defects.append("blank output")
    if not outcome["no_truncated_table"]:
        defects.append("truncated table")
    if not outcome["no_url_dump"]:
        defects.append("URL dump")
    if not outcome["no_verify_dump"]:
        defects.append("verification dump")
    if not outcome["mentions_k77"]:
        defects.append("missing K77 product identity")
    if not outcome["has_section_headings"]:
        defects.append("missing section headings")
    if result["validation_ok"] is False:
        defects.append(f"validation failed: {result['validation_error']}")
    if result["runtime_error"]:
        defects.append(f"runtime error: {result['runtime_error']}")
    # ทุก case ทีคืน failure marker ถือว่าไม่ผ่าน
    if outcome["limited_analysis"]:
        defects.append("limited analysis marker in output")
    if re.search(r"\*\*required_search_failed:\s*true\*\*", output):
        defects.append("required search failed marker in output")
    if re.search(r"\*\*structural_output_failed:\s*true\*\*", output):
        defects.append("structural output failed marker in output")

    # case 10 / 11 คือ web search Thailand ต้องมี selected evidence ทีเป็น Thai competitor จริง
    if result["case_name"].endswith("thailand_web_search"):
        thai_competitor = any(
            a.get("_relevance", {}).get("relevance_type") == "competitor"
            and a.get("_relevance", {}).get("geography") == "thailand"
            for a in relevant
        )
        if not thai_competitor:
            defects.append("thailand web search has no selected thai competitor evidence")
        if not outcome["has_selected_evidence"]:
            defects.append("thailand web search produced no selected evidence")

    # Semantic full-analysis quality gate
    if result["case_name"].endswith("thailand_web_search") and result.get("competitor_data"):
        agent = CompetitorAnalysisAgent({"web_search": True, "web_search_mode": result.get("web_search_mode", "required")}, _DummyLLM())
        agent.build_prompt(result["product_spec"], result["competitor_data"])
        agent._last_relevant_annotations = result.get("relevant_annotations", [])
        quality = agent._full_analysis_quality(output)
        outcome["competitor_fact_cells"] = quality["competitor_fact_cells"]
        outcome["cited_fact_cells"] = quality["cited_fact_cells"]
        outcome["thai_fact_cells"] = quality["thai_fact_cells"]
        outcome["no_evidence_ratio"] = round(quality["no_evidence_cells"] / quality["competitor_cells_total"], 2) if quality["competitor_cells_total"] else 0.0
        if (
            quality["competitor_fact_cells"] < 2
            or quality["cited_fact_cells"] < 2
            or quality["thai_fact_cells"] < 1
            or (quality["competitor_cells_total"] and quality["no_evidence_cells"] / quality["competitor_cells_total"] > 0.5)
            or quality["has_fallback_dump"]
        ):
            defects.append("insufficient competitor evidence")
            outcome["insufficient_competitor_evidence"] = True

    outcome["defects"] = defects
    outcome["passed"] = len(defects) == 0
    return outcome


def _run_case(case_id: int, repeats: int, model: str, web_search_mode: str = "optional") -> list[dict[str, Any]]:
    case = CASES[case_id]
    cfg = load_config()
    agent_cfg = dict(get_agent_config(cfg, "competitor_analysis"))
    agent_cfg["web_search"] = case["web_search"]
    agent_cfg["web_search_mode"] = web_search_mode

    results: list[dict[str, Any]] = []
    for run in range(1, repeats + 1):
        print(f"\n=== Case {case_id}: {case['name']} — run {run}/{repeats} ===")
        result = _run_one(case_id, run, model, agent_cfg, case)
        result["outcome"] = _check_outcome(result)
        results.append(result)
    return results


def _save_results(results: list[dict[str, Any]]) -> list[Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    paths: list[Path] = []
    for result in results:
        base = OUTPUT_DIR / f"case_{result['case_id']}_{result['case_name']}_run{result['run']}_{ts}"
        md_path = base.with_suffix(".md")
        draft_path = base.with_name(f"{base.name}.draft.md")
        raw_gen_path = base.with_name(f"{base.name}.raw.generate.json")
        raw_rev_path = base.with_name(f"{base.name}.raw.revise.json")
        research_path = base.with_name(f"{base.name}.research.json")
        json_path = base.with_suffix(".json")

        md_path.write_text(result["output"], encoding="utf-8")
        draft_path.write_text(result.get("draft_output", "") or "", encoding="utf-8")
        raw_gen_path.write_text(result.get("generate_raw_response", "") or "", encoding="utf-8")
        raw_rev_path.write_text(result.get("revise_raw_response", "") or "", encoding="utf-8")
        research_path.write_text(result.get("validated_research_response", "") or "", encoding="utf-8")

        # Save metadata without duplicating the full raw outputs (already in .md/.json files)
        metadata = {k: v for k, v in result.items() if k not in ("output", "draft_output", "generate_raw_response", "revise_raw_response", "validated_research_response")}
        metadata["output_path"] = str(md_path)
        metadata["draft_output_path"] = str(draft_path)
        metadata["generate_raw_response_path"] = str(raw_gen_path)
        metadata["revise_raw_response_path"] = str(raw_rev_path)
        metadata["validated_research_response_path"] = str(research_path)
        json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        paths.append(json_path)
        print(f"Saved: {json_path}")
        print(f"  output: {md_path}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Run competitor_analysis BETA acceptance cases.")
    parser.add_argument("--case", type=int, required=True, choices=sorted(CASES.keys()),
                        help="Acceptance case ID to run")
    parser.add_argument("--repeats", type=int, default=2,
                        help="Number of repeated runs for this case (default 2)")
    parser.add_argument("--model", type=str, default="openrouter/free",
                        help="OpenRouter model ID to use (default openrouter/free)")
    parser.add_argument("--web-search-mode", type=str, default="optional",
                        choices=["optional", "required"],
                        help="Set web_search_mode for the agent (default optional)")
    args = parser.parse_args()

    case = CASES[args.case]
    print(f"=== Case {args.case}: {case['name']} ===")
    print(f"Description: {case['description']}")
    print(f"Model: {args.model}")
    print(f"Web search: {'on' if case['web_search'] else 'off'}")
    print(f"Repeats: {args.repeats}")

    results = _run_case(args.case, args.repeats, args.model, args.web_search_mode)
    _save_results(results)

    print("\n=== Summary ===")
    for r in results:
        out = r["outcome"]
        print(f"run {r['run']}: ok={out['passed']} cost=${r['total_cost_usd']} "
              f"calls={r['llm_calls']} tokens={r['total_prompt_tokens']}+{r['total_completion_tokens']} "
              f"wall={r['wall_ms']}ms defects={out['defects']}")


if __name__ == "__main__":
    main()
