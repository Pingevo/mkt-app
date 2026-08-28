"""Web-search capability probe for Agent 2.

Usage (real API, after approval):
    python tests/competitor_search_probe.py --model minimax/minimax-m3:free --mechanism server_tool
    python tests/competitor_search_probe.py --model minimax/minimax-m3:free --mechanism plugin

The probe is not an acceptance test. It must pass before the full case-10
acceptance run is approved.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

import src.ai_usage as _ai_usage
import src.flow_context as _flow_context
from src.agents.competitor_analysis import CompetitorAnalysisAgent
from src.config_loader import get_agent_config, get_env, load_config
from src.llm_client import LLMClient


# Product spec used to set the relevance gate for the probe.
PROBE_PRODUCT_SPEC = """
สินค้า: CACGO K77
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
ราคาต้นทุน FOB US$15.00
""".strip()

DEFAULT_COMPETITOR_DATA = (
    "Xiaomi\nMibro\nHuawei\nAmazfit\nGarmin\nSamsung\nApple\nFitbit"
)


def _build_probe_prompt(model: str = "CACGO K77") -> str:
    """Short Thai prompt with a clear, bounded scope."""
    return (
        f"ค้นหาคู่แข่งสมาร์ทวอทช์ {model} ในประเทศไทย จากเว็บไซต์\n"
        "- ต้องค้นข้อมูลบนเว็บจริง\n"
        "- ต้องตอบด้วยชื่อคู่แข่งอย่างน้อย 1 รุ่นและ URL ไทยทีรองรับ\n"
        "- URL ต้องเป็นหน้าสินค้าหรือร้านค้าไทย (ไม่ใช่ homepage ทั่วไป)\n"
        "- ถ้าหาไม่ได้ ให้ตอบว่าไม่พบหลักฐาน"
    )


def _web_search_cfg() -> dict[str, Any]:
    import yaml
    path = Path(PROJECT_ROOT) / "config" / "web_search.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_server_tools() -> list[dict[str, Any]]:
    """Payload for openrouter:web_search server tool."""
    cfg = _web_search_cfg()
    tool_params: dict[str, Any] = {
        "max_results": int(cfg.get("max_results_detailed", 5)),
    }
    for key in ("engine", "allowed_domains", "excluded_domains",
                "max_uses", "max_total_results", "search_context_size"):
        val = cfg.get(key)
        if val:
            tool_params[key] = val
    return [
        {"type": "openrouter:web_search", "parameters": tool_params},
    ]


def _build_plugins() -> list[dict[str, Any]]:
    """Payload for OpenRouter web plugin (deprecated; for capability probe only)."""
    # NOTE: this is a deprecated plugin-style contract. The production
    # implementation uses the openrouter:web_search server tool.
    return [{"id": "web"}]


def _classify_tool_status(
    mechanism: str,
    tool_use: dict[str, Any] | None,
    raw_annotations_count: int,
    annotations_count: int,
) -> str:
    if mechanism == "plugin":
        # สำหรับ plugin ไม่มี server_tool_use_details; ใช้ raw annotations เป็นหลักฐัน
        if raw_annotations_count == 0:
            return "plugin_no_annotations"
        if annotations_count == 0:
            return "plugin_response_parse_failure"
        return "plugin_invoked_with_annotations"

    # server_tool ใช้ server_tool_use_details เป็นหลัก
    executed = (tool_use or {}).get("tool_calls_executed") or (tool_use or {}).get("web_search_requests") or 0
    if not executed:
        return "tool_not_invoked"
    if raw_annotations_count == 0:
        return "tool_invoked_no_results"
    if annotations_count == 0:
        return "response_parse_failure"
    return "tool_invoked_with_annotations"


def _run_relevance_gate(
    agent: CompetitorAnalysisAgent,
    annotations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run Agent 2 relevance gate over the probe's raw annotations."""
    relevant: list[dict[str, Any]] = []
    for a in annotations:
        r = agent._assess_source_relevance(a)
        if r.get("relevant") is True:
            relevant.append({**a, "_relevance": r})
    return relevant


def _has_required_evidence(relevant: list[dict[str, Any]]) -> bool:
    """Probe passes only if there is Thai product or Thai competitor evidence."""
    for a in relevant:
        rel = a.get("_relevance", {})
        if rel.get("geography") != "thailand":
            continue
        if rel.get("relevance_type") in ("target", "competitor"):
            return True
    return False


def _has_hub_delivered(hub_results: list[dict[str, Any]]) -> bool:
    return any(h.get("hub_status") == "hub_delivered" for h in hub_results)


def _classify_hub_status(
    hub_results: list[dict[str, Any]],
    hub_flushed: bool,
) -> tuple[str, dict[str, Any] | None]:
    """คืนสถานะ Hub delivery ทีชัดเจน ไม่ตีความ timeout เป็น failure ทันที."""
    if not hub_results:
        if not hub_flushed:
            return "hub_timeout_unknown", None
        return "hub_not_delivered", None
    # สำหรับ probe มักมี 1 event ต่อ run
    r = hub_results[0]
    return r.get("hub_status", "hub_unknown"), r


def _read_local_log_event(request_id: str) -> dict[str, Any] | None:
    path = Path(_ai_usage.USAGE_LOG_PATH)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            for line in reversed(f.readlines()):
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("request_id") == request_id:
                    return entry
    except Exception:
        pass
    return None


def run_probe(
    llm: LLMClient,
    model: str,
    mechanism: str = "server_tool",
    prompt: str | None = None,
    product_spec: str | None = None,
    competitor_data: str | None = None,
    probe_id: str = "",
) -> dict[str, Any]:
    """Run a single web-search probe and return a report.

    The probe is fully testable offline by passing a fake LLMClient.
    """
    product_spec = (product_spec or PROBE_PRODUCT_SPEC).strip()
    competitor_data = (competitor_data or DEFAULT_COMPETITOR_DATA).strip()
    prompt = (prompt or _build_probe_prompt()).strip()

    # Build an Agent 2 relevance gate with the same context as the full case.
    cfg = load_config()
    agent_cfg = dict(get_agent_config(cfg, "competitor_analysis"))
    agent_cfg["web_search"] = True
    agent_cfg["web_search_mode"] = "optional"  # probe itself should not force required
    agent = CompetitorAnalysisAgent(agent_cfg, llm)
    agent.build_prompt(product_spec, competitor_data)

    _flow_context.set_usage_actor("probe:competitor_analysis")
    _flow_context.set_usage_reference(f"probe:{mechanism}:{probe_id or '1'}")
    _flow_context.set_usage_metadata({"mechanism": mechanism, "probe_id": probe_id, "model": model})

    hub_results: list[dict[str, Any]] = []
    previous_callback = _ai_usage.HUB_POST_CALLBACK
    _ai_usage.HUB_POST_CALLBACK = lambda r: hub_results.append(r)

    t0 = time.monotonic()
    output: str | None = None
    annotations: list[dict[str, Any]] = []
    messages = [
        {"role": "system", "content": "คุณคือผู้ช่วยค้นหาข้อมูลบนเว็บ ตอบพร้อมอ้างอิง URL"},
        {"role": "user", "content": prompt},
    ]

    hub_flushed = False
    try:
        if mechanism == "server_tool":
            output, annotations = llm.chat(
                messages,
                model=model,
                temperature=0.7,
                max_tokens=2048,
                tools=_build_server_tools(),
                return_annotations=True,
                source="web_search.probe",
            )
        elif mechanism == "plugin":
            output, annotations = llm.chat(
                messages,
                model=model,
                temperature=0.7,
                max_tokens=2048,
                plugins=_build_plugins(),
                return_annotations=True,
                source="web_search.probe",
            )
        else:
            raise ValueError(f"unknown mechanism: {mechanism}")
    finally:
        # รอให้ daemon POST ทำงานเสร็จก่อนนำ result ไปประเมิน แล้วคืนค่า callback
        hub_flushed = _ai_usage.flush_usage_log(timeout=20.0)
        _ai_usage.HUB_POST_CALLBACK = previous_callback
        _flow_context.clear_usage_context()

    duration_ms = int((time.monotonic() - t0) * 1000)

    raw = getattr(llm, "_last_raw_response", {}) or {}
    request_id = raw.get("id")
    usage = raw.get("usage") or {}
    tool_use = usage.get("server_tool_use_details")
    raw_annotations_count = getattr(llm, "_last_raw_annotations_count", 0)
    annotations_count = getattr(llm, "_last_annotations_count", 0)
    tool_status = _classify_tool_status(mechanism, tool_use, raw_annotations_count, annotations_count)

    # Run the same relevance gate as the full Agent 2.
    relevant = _run_relevance_gate(agent, annotations or [])

    # Probe pass criteria.
    hub_status, hub_detail = _classify_hub_status(hub_results, hub_flushed)
    reasons: list[str] = []
    if tool_status not in ("tool_invoked_with_annotations", "plugin_invoked_with_annotations"):
        reasons.append(tool_status)
    if not _has_required_evidence(relevant):
        reasons.append("no_thai_competitor_or_product_evidence")
    if hub_status != "hub_delivered":
        reasons.append(hub_status)

    local_event = _read_local_log_event(request_id) if request_id else None
    if not local_event:
        reasons.append("local_log_missing")

    probe_passed = not reasons

    return {
        "model": model,
        "mechanism": mechanism,
        "prompt": prompt,
        "product_spec": product_spec,
        "competitor_data": competitor_data,
        "probe_id": probe_id,
        "request_id": request_id,
        "output": output,
        "duration_ms": duration_ms,
        "tool_status": tool_status,
        "raw_annotations_count": raw_annotations_count,
        "annotations_count": annotations_count,
        "relevant_annotations_count": len(relevant),
        "relevant_annotations": relevant,
        "tool_use": tool_use,
        "usage_cost": usage.get("cost"),
        "hub_flushed": hub_flushed,
        "hub_status": hub_status,
        "hub_detail": hub_detail,
        "hub_results": hub_results,
        "local_event": local_event,
        "probe_passed": probe_passed,
        "probe_fail_reasons": reasons,
        "timestamp": datetime.now().isoformat(),
    }


def _save_artifact(report: dict[str, Any]) -> Path:
    out_dir = Path(PROJECT_ROOT) / "output" / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_model = report["model"].replace("/", "_").replace(":", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"probe_{report['mechanism']}_{safe_model}_{ts}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main() -> None:
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Web-search capability probe for Agent 2")
    parser.add_argument("--model", default="minimax/minimax-m3:free")
    parser.add_argument("--mechanism", choices=["server_tool", "plugin"], default="server_tool")
    parser.add_argument("--probe-id", default="manual")
    args = parser.parse_args()

    cfg = load_config()
    defaults = cfg.get("defaults", {})
    llm = LLMClient(
        api_key=get_env("OPENROUTER_API_KEY"),
        base_url=defaults.get("base_url", "https://openrouter.ai/api/v1"),
        default_model=args.model,
        timeout=defaults.get("timeout_seconds", 120),
    )
    try:
        report = run_probe(llm, args.model, args.mechanism, probe_id=args.probe_id)
        path = _save_artifact(report)
        print(json.dumps({
            "artifact": str(path),
            "probe_passed": report["probe_passed"],
            "tool_status": report["tool_status"],
            "cost_usd": report["usage_cost"],
            "fail_reasons": report["probe_fail_reasons"],
        }, ensure_ascii=False, indent=2))
    finally:
        llm.close()


if __name__ == "__main__":
    main()
