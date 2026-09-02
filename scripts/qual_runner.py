"""Evaluation-only: UI-equivalent runner for Beta qualification.

This file is NOT production code. It calls the exact same Orchestrator methods
that web_viewer.py's _run_single_agent() calls, with the same parameters the
UI sends. It enforces a hard outbound budget guard that blocks before any paid
network request exceeds the ceiling.

Budget ceilings (hard, enforced before network):
  Stage A (text + 1 real image): $0.40 cumulative
  Stage B (1 real video):        $0.95 additional
  Total:                         $1.35

Every paid LLM and media call is recorded in the local usage log
(logs/llm_usage.jsonl) by the production ai_usage module. This runner reads
that log to compute cumulative spend and blocks before the next call if the
budget would be exceeded.
"""

from __future__ import annotations

import httpx
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# --- Silent env load (never prints values) ---
_env = PROJECT_ROOT / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from src.orchestrator import Orchestrator
from src.llm_client import LLMClient
from src.ai_usage import USAGE_LOG_PATH, flush_usage_log, HubReceiptCollector, reconcile_hub_receipts
from src.evaluation.campaign_qualification import (
    QualificationBudgetExhausted,
    read_usage_log_for_reference,
    snapshot_usage_log_offset,
)
from src.flow_context import set_usage_reference, set_usage_metadata, clear_usage_context
from src import product_db
from src.config_loader import load_config, get_agent_config


# ---------------------------------------------------------------------------
# Budget guard
# ---------------------------------------------------------------------------

STAGE_A_CEILING = 0.40
STAGE_B_CEILING = 0.95
TOTAL_CEILING = 1.35

# Per-run call budget (logical-run: generation + review + repair + web + media).
# _RUN_MAX_CALLS is set from the dry-run plan before each run_case.
_RUN_CALL_COUNT = 0
_RUN_MAX_CALLS = 0

# Cached qualification config (cost per call, max-call defaults, ceilings).
_QUAL_CONFIG: dict[str, Any] | None = None


def _load_qualification_config() -> dict[str, Any]:
    """Load qualification tuning from config/qualification.yaml (config, not hardcode)."""
    global _QUAL_CONFIG
    if _QUAL_CONFIG is not None:
        return _QUAL_CONFIG
    import yaml
    path = PROJECT_ROOT / "config" / "qualification.yaml"
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            _QUAL_CONFIG = data.get("qualification", {})
        except Exception:
            _QUAL_CONFIG = {}
    else:
        _QUAL_CONFIG = {}
    return _QUAL_CONFIG


def _qual_ceilings() -> dict[str, float]:
    """Return stage/total ceilings from config or fallback constants."""
    cfg = _load_qualification_config().get("ceilings", {})
    return {
        "stage_a": float(cfg.get("stage_a", STAGE_A_CEILING)),
        "stage_b": float(cfg.get("stage_b", STAGE_B_CEILING)),
        "total": float(cfg.get("total", TOTAL_CEILING)),
    }


# Session baseline: spend that existed BEFORE this qualification started.
# The budget guard tracks only spend incurred DURING this qualification.
_SESSION_BASELINE: float | None = None


def _read_total_cost() -> float:
    """Read total cost from local usage log (all entries with cost_usd)."""
    if not USAGE_LOG_PATH.exists():
        return 0.0
    total = 0.0
    try:
        for line in USAGE_LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    continue
                total += float(entry.get("cost_usd", 0) or 0)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
    except Exception:
        pass
    return round(total, 6)


def _init_session_baseline() -> None:
    """Capture the usage log total at the start of this qualification session."""
    global _SESSION_BASELINE
    _SESSION_BASELINE = _read_total_cost()
    print(f"[BUDGET GUARD] Session baseline set: ${_SESSION_BASELINE:.6f}", flush=True)


def _read_cumulative_cost() -> float:
    """Read spend incurred DURING this qualification session (total - baseline)."""
    total = _read_total_cost()
    baseline = _SESSION_BASELINE if _SESSION_BASELINE is not None else 0.0
    return round(total - baseline, 6)


def _read_entries_since(offset: int) -> list[dict[str, Any]]:
    """Read usage log entries appended after the given byte offset."""
    if not USAGE_LOG_PATH.exists():
        return []
    entries = []
    try:
        with open(USAGE_LOG_PATH, "r", encoding="utf-8") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        continue
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return entries


def _per_call_ceiling(call_kind: str) -> float:
    """Return the conservative USD reserve to set aside for one call of this kind."""
    cfg = _load_qualification_config().get("cost_per_call_ceiling", {})
    return float(cfg.get(call_kind, cfg.get("text", 0.05)))


def _resolved_url(client: httpx.Client, url: str) -> str:
    """Resolve a possibly relative request path against the httpx client base_url."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    base = str(client.base_url)
    if not base:
        return url
    return base.rstrip("/") + "/" + url.lstrip("/")


def _init_call_budget(max_calls: int) -> None:
    """Reset the logical-run call counter and set the hard call cap."""
    global _RUN_CALL_COUNT, _RUN_MAX_CALLS
    _RUN_CALL_COUNT = 0
    _RUN_MAX_CALLS = max(max_calls, 0)


def _call_budget_state() -> dict[str, int]:
    return {"used": _RUN_CALL_COUNT, "max": _RUN_MAX_CALLS}


def budget_guard(stage: str, estimated_cost: float = 0.0, label: str = "", call_kind: str = "") -> bool:
    """Return True if the call is allowed, False if it must be blocked.

    Reserves a conservative per-call ceiling before allowing any paid network
    request.  This is the single gate for call-count and USD budgets.
    """
    global _RUN_CALL_COUNT
    if _RUN_MAX_CALLS > 0 and _RUN_CALL_COUNT >= _RUN_MAX_CALLS:
        print(f"[BUDGET GUARD] BLOCKED by call budget: call {_RUN_CALL_COUNT + 1}/{_RUN_MAX_CALLS} "
              f"stage={stage} label={label}", flush=True)
        return False

    per_call = _per_call_ceiling(call_kind)
    reserve = max(estimated_cost, per_call)
    cumulative = _read_cumulative_cost()
    ceiling = _qual_ceilings()["stage_a"] if stage == "A" else _qual_ceilings()["total"]
    projected = round(cumulative + reserve, 6)
    if projected > ceiling:
        print(f"[BUDGET GUARD] BLOCKED by USD: stage={stage} cumulative=${cumulative:.6f} "
              f"reserve=${reserve:.6f} projected=${projected:.6f} ceiling=${ceiling:.6f} "
              f"label={label}", flush=True)
        return False
    _RUN_CALL_COUNT += 1
    print(f"[BUDGET GUARD] ALLOW: call={_RUN_CALL_COUNT}/{_RUN_MAX_CALLS or '?'} "
          f"stage={stage} cumulative=${cumulative:.6f} "
          f"reserve=${reserve:.6f} projected=${projected:.6f} ceiling=${ceiling:.6f} "
          f"label={label}", flush=True)
    return True


def _is_paid_request(url: str) -> bool:
    """Return True for outbound paid requests; exclude the AI-usage Hub."""
    from src.ai_usage import _read_hub_credentials
    hub_url = _read_hub_credentials()[0]
    if hub_url and url.startswith(hub_url):
        return False
    # Qualification only counts known paid endpoints (OpenRouter LLM + media).
    return "openrouter.ai" in url and any(p in url for p in ("/chat/completions", "/images", "/videos"))


def _has_web_search_tool(kwargs: dict[str, Any]) -> bool:
    """Return True if the chat payload carries an OpenRouter web tool."""
    payload = kwargs.get("json") or {}
    if not isinstance(payload, dict):
        return False
    tools = payload.get("tools") or []
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = (tool.get("type") or "").lower()
        if tool_type in {"openrouter:web_search", "openrouter:web_fetch"}:
            return True
    return False


def _detect_call_kind(url: str, kwargs: dict[str, Any] | None = None) -> str:
    """Classify the paid request URL + payload into a call kind for cost reservation."""
    low = url.lower()
    if "/images" in low:
        return "image"
    if "/videos" in low:
        return "video"
    if "/chat/completions" in low:
        if kwargs and _has_web_search_tool(kwargs):
            return "web_search"
        return "text"
    return "text"  # default reserve for any other paid endpoint


class PaidCallGuard:
    """Context manager that wraps every paid httpx call with budget_guard.

    This is the single hard guard for the qualification run.  It patches
    ``httpx.Client.post`` and ``httpx.Client.stream`` so that each outbound
    paid request is counted and a conservative per-call USD reserve is set
    aside before the request is actually sent.
    """

    def __init__(self, stage: str, max_calls: int) -> None:
        self.stage = stage
        self.max_calls = max_calls
        self._original_post = httpx.Client.post
        self._original_stream = httpx.Client.stream
        self._call_log: list[dict[str, Any]] = []

    def __enter__(self) -> "PaidCallGuard":
        _init_call_budget(self.max_calls)
        guard = self

        def _guarded_post(client: httpx.Client, url: str, **kwargs: Any) -> Any:
            resolved = _resolved_url(client, url)
            if not _is_paid_request(resolved):
                return guard._original_post(client, url, **kwargs)
            kind = _detect_call_kind(resolved, kwargs)
            if not budget_guard(self.stage, label=resolved, call_kind=kind):
                raise QualificationBudgetExhausted(
                    _RUN_CALL_COUNT, resolved, self.max_calls
                )
            guard._call_log.append({"url": resolved, "kind": kind, "allowed": True})
            return guard._original_post(client, url, **kwargs)

        def _guarded_stream(
            client: httpx.Client, method: str, url: str, **kwargs: Any
        ) -> Any:
            resolved = _resolved_url(client, url)
            if not _is_paid_request(resolved):
                return guard._original_stream(client, method, url, **kwargs)
            kind = _detect_call_kind(resolved, kwargs)
            if not budget_guard(self.stage, label=resolved, call_kind=kind):
                raise QualificationBudgetExhausted(
                    _RUN_CALL_COUNT, resolved, self.max_calls
                )
            guard._call_log.append({"url": resolved, "kind": kind, "allowed": True})
            return guard._original_stream(client, method, url, **kwargs)

        httpx.Client.post = _guarded_post
        httpx.Client.stream = _guarded_stream
        return self

    def __exit__(self, *exc: object) -> None:
        httpx.Client.post = self._original_post
        httpx.Client.stream = self._original_stream

    def actual_calls(self) -> list[dict[str, Any]]:
        """Return the paid calls that were allowed through the guard."""
        return list(self._call_log)


def current_spend() -> dict[str, float]:
    """Return cumulative spend snapshot."""
    return {
        "cumulative": _read_cumulative_cost(),
        "stage_a_ceiling": STAGE_A_CEILING,
        "stage_b_ceiling": STAGE_B_CEILING,
        "total_ceiling": TOTAL_CEILING,
    }


# ---------------------------------------------------------------------------
# UI-equivalent runner
# ---------------------------------------------------------------------------

def make_orchestrator(product_id: str) -> Orchestrator:
    """Create orchestrator exactly like web_viewer _get_orchestrator + _run_single_agent."""
    orch = Orchestrator(brand_dir="brand", product_id=product_id)
    return orch


def make_llm(orch: Orchestrator) -> LLMClient:
    """Create LLM client exactly like the UI does."""
    return orch.make_client()


def run_case(
    case_id: str,
    agent_key: str,
    product_id: str,
    quick_brief: str = "",
    stage: str = "A",
    estimated_cost: float = 0.01,
    content_count: int = 1,
    platforms: list[str] | None = None,
    media_type: str = "",
    auto_image: bool | None = None,
    auto_video: bool | None = None,
    context: dict | None = None,
    agent_settings_override: dict | None = None,
    output_dir: Path | None = None,
    product_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run one UI-equivalent case and capture all evidence.

    Mirrors web_viewer._run_single_agent exactly:
    - sets orch.product_id = folder
    - calls the same orch.run_* method
    - captures usage log entries + Hub receipts
    - product_ids: optional list for multi-product runs; joined with " + " to
      match production multi-product flow in orchestrator.py
    """
    # Pre-flight dry-run plan: no model calls, just expected calls + upper-bound cost
    plan = dry_run_cost_plan(
        agent_key=agent_key,
        product_id=product_id,
        quick_brief=quick_brief,
        content_count=content_count,
        platforms=platforms,
        auto_image=auto_image,
        auto_video=auto_video,
    )
    _print_dry_run_plan(plan)

    if output_dir is None:
        output_dir = PROJECT_ROOT / "data" / "all_agents_beta_qualification" / "run_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    if context is None:
        context = {"use_competitor": True, "use_campaign": True}
    if platforms is None:
        platforms = ["facebook", "tiktok"]

    # Apply agent settings override (evaluation-only — same as user saving in UI)
    if agent_settings_override:
        _apply_agent_settings(agent_key, agent_settings_override)

    # Ensure session baseline is captured for this qualification run.
    if _SESSION_BASELINE is None:
        _init_session_baseline()

    # Multi-product: join with " + " exactly like the production UI/orchestrator path.
    # Only fall back to single product when product_ids is None; an empty list
    # would be a programming error, not a silent single-product fallback.
    folder_list = product_ids if product_ids is not None else [product_id]
    effective_product_id = " + ".join(folder_list)

    orch = make_orchestrator(effective_product_id)
    llm = make_llm(orch)
    orch.product_id = effective_product_id

    # Snapshot usage log before run
    log_offset = snapshot_usage_log_offset(USAGE_LOG_PATH)
    run_ref = f"qual:{case_id}:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    set_usage_reference(run_ref)
    set_usage_metadata({
        "product_id": product_id,
        "product_ids": product_ids,
        "effective_product_id": effective_product_id,
        "agent": agent_key,
        "run_ref": run_ref,
        "case_id": case_id,
    })

    result_text = ""
    error = None
    hub_results: list[dict] = []
    hub_ack = False
    call_guard: PaidCallGuard | None = None
    start_time = time.time()

    try:
        with PaidCallGuard(stage, plan["max_calls"]) as call_guard:
            with HubReceiptCollector() as hub_collector:
                # Fix #4: UI parity — send product images like web_viewer does
                image_paths = orch._get_product_image_paths()

                if agent_key == "product_spec":
                    raw_data = product_db.get_scoped_context_text(folder_list)
                    if not raw_data.strip():
                        raw_data = ""
                    result_text = orch.run_product_spec(
                        raw_data, image_paths, llm=llm, quick_brief=quick_brief,
                    )
                elif agent_key == "competitor_analysis":
                    result_text = orch.run_competitor_analysis(
                        "", None, llm=llm, quick_brief=quick_brief,
                    )
                elif agent_key == "campaign_strategy":
                    # Fix #3: pass competitor analysis text if provided in context
                    competitor_text = ""
                    if context and isinstance(context, dict):
                        competitor_text = context.get("competitor_analysis", "")
                    result_text = orch.run_campaign_strategy(
                        "", competitor_text, llm=llm, quick_brief=quick_brief,
                    )
                elif agent_key == "content_creator":
                    # UI path: loops per platform, count_per_platform posts each
                    platform_names = {"facebook": "Facebook", "tiktok": "TikTok"}
                    target_platforms = platforms if platforms else [""]
                    _n_plat = max(1, len(target_platforms))
                    count_per_platform = max(1, (content_count + _n_plat - 1) // _n_plat)
                    all_posts = []

                    # UI-equivalent media_type: the web UI always passes a media_type;
                    # the harness derives it from auto_image/auto_video when not supplied.
                    if not media_type:
                        if auto_image and auto_video:
                            media_type = "both"
                        elif auto_image:
                            media_type = "image"
                        elif auto_video:
                            media_type = "video"
                        else:
                            media_type = ""

                    # Fix #4: UI parity — add content history like web_viewer does
                    try:
                        from src import content_history
                        _product_history_text = content_history.format_product_history_for_prompt(
                            PROJECT_ROOT, effective_product_id,
                        )
                    except Exception:
                        _product_history_text = ""

                    # Fix #4: UI parity — use competitor/campaign context if provided
                    analysis_text = ""
                    campaign_text = ""
                    if context and isinstance(context, dict):
                        analysis_text = context.get("competitor_analysis", "")
                        campaign_text = context.get("campaign_strategy", "")

                    for platform in target_platforms:
                        platform_label = platform_names.get(platform, platform) if platform else ""
                        for post_idx in range(count_per_platform):
                            multi_brief = quick_brief
                            if count_per_platform > 1:
                                multi_brief = f"โพสต์ที่ {post_idx+1} จาก {count_per_platform} โพสต์ — สร้างคอนเทนต์ที่แตกต่างจากโพสต์ก่อนหน้า"
                                if all_posts:
                                    multi_brief += "\n\n--- คอนเทนต์ที่สร้างไปแล้ว (ห้ามซ้ำ) ---\n"
                                    for j, p in enumerate(all_posts):
                                        multi_brief += f"\nโพสต์ที่ {j+1}:\n{json.dumps(p, ensure_ascii=False)[:800]}\n"
                                    multi_brief += "--- สิ้นสุด ---\n"
                                    multi_brief += "สร้างโพสต์ใหม่ที่มีมุมมอง/concept ต่างจากโพสต์ก่อนหน้า"
                                if quick_brief:
                                    multi_brief += f"\n\nคำขอเพิ่มเติมจาก user: {quick_brief}"
                            # Fix #4: UI parity — add content history
                            if _product_history_text:
                                multi_brief = (multi_brief or "") + "\n\n" + _product_history_text + "\n"
                                multi_brief += "วิเคราะห์สินค้านี้แล้วเลือกมุมมองใหม่ที่ต่างจากที่เคยใช้ แล้วสร้างโพสต์จากมุมมองนั้น"
                            else:
                                multi_brief = (multi_brief or "") + "\n\nวิเคราะห์สินค้านี้แล้วเลือกมุมมองที่เหมาะสมที่สุด แล้วสร้างโพสต์จากมุมมองนั้น"
                            if platform_label:
                                multi_brief = (multi_brief or "") + f"\nแพลตฟอร์มที่ต้องสร้างสำหรับโพสต์นี้: {platform_label} เท่านั้น"
                            result = orch.run_content_creator(
                                "", analysis_text, campaign_text,
                                llm=llm, quick_brief=multi_brief,
                                media_type=media_type,
                            )
                            try:
                                parsed = json.loads(result)
                            except (json.JSONDecodeError, TypeError) as exc:
                                raise ValueError(f"content creator output is not valid JSON: {exc}") from exc
                            if not isinstance(parsed, dict):
                                raise ValueError(
                                    f"content creator output was not a JSON object; got {type(parsed).__name__}"
                                )
                            posts = parsed.get("posts", [])
                            if posts:
                                if platform_label:
                                    posts[0]["platform"] = platform_label
                                all_posts.append(posts[0])
                    combined = {"posts": all_posts}
                    result_text = json.dumps(combined, ensure_ascii=False, indent=2)

                    # Auto media generation (mirrors UI)
                    if (auto_image or auto_video) and all_posts:
                        _run_media_gen(
                            orch, llm, result_text, output_dir,
                            auto_image=auto_image or False,
                            auto_video=auto_video or False,
                            product_id=product_id,
                            case_id=case_id,
                            stage=stage,
                        )
                else:
                    raise ValueError(f"Unknown agent: {agent_key}")

                # Fix #5: Hub receipt race — flush and read results INSIDE the
                # `with` block, before __exit__ uninstalls the callback.  This
                # ensures any background POST threads can still append receipts
                # while we wait for flush to complete.
                hub_ack = flush_usage_log(timeout=5.0)
                hub_results = list(hub_collector.results)
    except Exception as exc:
        error = str(exc)
    finally:
        clear_usage_context()
        llm.close()

    elapsed = round(time.time() - start_time, 2)

    # Read usage entries for this run
    run_entries = _read_entries_since(log_offset)
    run_cost = round(sum(float(e.get("cost_usd", 0) or 0) for e in run_entries), 6)
    request_ids = [e.get("request_id") for e in run_entries if e.get("request_id")]
    call_sources = [e.get("source") for e in run_entries]
    models_used = [e.get("model") for e in run_entries if e.get("model")]

    # Reconcile Hub receipts (hub_results already captured inside `with`)
    hub_reconciliation = reconcile_hub_receipts(run_entries, hub_results, flush_completed=hub_ack)

    # Save output
    out_file = output_dir / f"{case_id}_output.txt"
    out_file.write_text(result_text or f"(error: {error})", encoding="utf-8")

    # Save full evidence
    evidence = {
        "case_id": case_id,
        "agent_key": agent_key,
        "product_id": product_id,
        "product_ids": product_ids,
        "effective_product_id": effective_product_id,
        "quick_brief": quick_brief,
        "ui_options": {
            "content_count": content_count,
            "platforms": platforms,
            "media_type": media_type,
            "auto_image": auto_image,
            "auto_video": auto_video,
            "context": context,
        },
        "agent_settings_override": agent_settings_override,
        "run_ref": run_ref,
        "elapsed_seconds": elapsed,
        "error": error,
        "result_text": result_text,
        "result_preview": (result_text or "")[:2000],
        "usage_entries": run_entries,
        "run_cost_usd": run_cost,
        "request_ids": request_ids,
        "call_sources": call_sources,
        "models_used": models_used,
        "num_paid_requests": len(run_entries),
        "hub_results": hub_results,
        "hub_reconciliation": hub_reconciliation,
        "dry_run_plan": plan,
        "actual_call_classifications": (call_guard.actual_calls() if call_guard is not None else []),
        "cumulative_spend_after": _read_cumulative_cost(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Also capture orchestrator results dict for campaign_strategy
    if agent_key == "campaign_strategy":
        for k in ["campaign_strategy_run_ref", "campaign_strategy_output",
                  "campaign_strategy_draft", "campaign_strategy_repaired",
                  "campaign_strategy_selected_evidence_urls",
                  "campaign_strategy_requested_competitor_models",
                  "campaign_strategy_evidence_confirmed_competitor_models",
                  "campaign_strategy_run_cost_usd",
                  "campaign_strategy_actual_model",
                  "campaign_strategy_request_ids",
                  "campaign_strategy_call_classifications",
                  "campaign_strategy_hub_reconciliation",
                  "campaign_strategy_validator_results",
                  "campaign_strategy_validator_ok"]:
            if k in orch.results:
                evidence[k] = orch.results[k]

    return evidence


def _apply_agent_settings(agent_key: str, settings: dict) -> None:
    """Apply agent settings override to config (evaluation-only).

    Mirrors what happens when user saves settings in the UI.
    Also handles YAML-only fields like max_retry_limit that live in agents.yaml
    rather than agent_instructions.json.
    """
    # 1. JSON settings (UI-saved fields like detail_level, focus, tone, hook_style)
    path = PROJECT_ROOT / "config" / "agent_instructions.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if agent_key not in data:
                data[agent_key] = {}
            # Only apply JSON-suitable settings; YAML-only fields go to agents.yaml
            json_settings = {k: v for k, v in settings.items()
                             if k not in ("max_retry_limit", "max_review_iterations",
                                          "max_search_calls", "web_search")}
            data[agent_key].update(json_settings)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[qual] agent settings override (json) failed: {e}", flush=True)

    # 2. YAML settings (structural fields like max_retry_limit)
    yaml_keys = {k: v for k, v in settings.items()
                 if k in ("max_retry_limit", "max_review_iterations",
                          "max_search_calls", "web_search")}
    if yaml_keys:
        _apply_yaml_overrides(agent_key, yaml_keys)


def _apply_yaml_overrides(agent_key: str, overrides: dict) -> None:
    """Apply overrides to config/agents.yaml for the given agent (evaluation-only).

    Saves the original values so they can be restored after the run via
    _restore_yaml_overrides.  This avoids permanently mutating production config.
    """
    import yaml
    yaml_path = PROJECT_ROOT / "config" / "agents.yaml"
    if not yaml_path.exists():
        return
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            original_text = f.read()
            data = yaml.safe_load(f)
        agents = data.get("agents", {})
        if agent_key not in agents:
            return
        # Save original values for restore
        _YAML_RESTORE[agent_key] = {
            k: agents[agent_key].get(k) for k in overrides
        }
        agents[agent_key].update(overrides)
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"[qual] YAML override for {agent_key}: {overrides}", flush=True)
    except Exception as e:
        print(f"[qual] agent settings override (yaml) failed: {e}", flush=True)


# Track original YAML values for restore after run
_YAML_RESTORE: dict[str, dict] = {}


def _restore_yaml_overrides() -> None:
    """Restore config/agents.yaml to original values after qualification run."""
    if not _YAML_RESTORE:
        return
    import yaml
    yaml_path = PROJECT_ROOT / "config" / "agents.yaml"
    if not yaml_path.exists():
        return
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        agents = data.get("agents", {})
        for agent_key, originals in _YAML_RESTORE.items():
            if agent_key in agents:
                agents[agent_key].update(originals)
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"[qual] YAML overrides restored: {list(_YAML_RESTORE.keys())}", flush=True)
        _YAML_RESTORE.clear()
    except Exception as e:
        print(f"[qual] YAML restore failed: {e}", flush=True)


def _run_media_gen(
    orch: Orchestrator, llm, content_json: str, output_dir: Path,
    auto_image: bool, auto_video: bool, product_id: str, case_id: str, stage: str,
) -> None:
    """Run media generation exactly like web_viewer _run_single_agent content_creator path."""
    from src import media_gen
    from src import asset_library as _al

    image_paths = orch._get_product_image_paths()
    parsed = media_gen.parse_media_prompts(content_json)

    if auto_image:
        for j, img in enumerate(parsed.get("images", [])):
            img_path = output_dir / f"{case_id}_image_{j+1}.png"
            img_kwargs: dict = {}
            if img.get("aspect_ratio"):
                img_kwargs["aspect_ratio"] = img["aspect_ratio"]
            _refs = _al.build_input_references(
                image_paths, img.get("asset_ids", []),
            )
            if _refs:
                img_kwargs["input_references"] = _refs
            visual = orch.brand_visual
            if visual:
                img_kwargs["visual"] = visual
            print(f"[qual] generating image {j+1}: {img_path.name}", flush=True)
            r = media_gen.generate_image_with_retry(
                img["prompt"], img_path, llm=llm, **img_kwargs,
            )
            media_gen.save_retry_history(output_dir, "image", img_path.name, r)
            print(f"[qual] image {j+1} result: ok={r.get('ok')} error={r.get('error','')[:200]}", flush=True)

    if auto_video:
        for j, vid in enumerate(parsed.get("videos", [])):
            vid_path = output_dir / f"{case_id}_video_{j+1}.mp4"
            vid_kwargs: dict = {}
            if vid.get("duration"):
                vid_kwargs["duration"] = int(vid["duration"])
            if vid.get("aspect_ratio"):
                vid_kwargs["aspect_ratio"] = vid["aspect_ratio"]
            if vid.get("resolution"):
                vid_kwargs["resolution"] = vid["resolution"]
            _refs = _al.build_input_references(
                image_paths, vid.get("asset_ids", []),
            )
            if _refs:
                vid_kwargs["input_references"] = _refs
            visual = orch.brand_visual
            if visual:
                vid_kwargs["visual"] = visual
            print(f"[qual] generating video {j+1}: {vid_path.name} "
                  f"duration={vid_kwargs.get('duration')} aspect={vid_kwargs.get('aspect_ratio')} "
                  f"resolution={vid_kwargs.get('resolution')}", flush=True)
            r = media_gen.generate_video_with_retry(
                vid["prompt"], vid_path, llm=llm, **vid_kwargs,
            )
            media_gen.save_retry_history(output_dir, "video", vid_path.name, r)
            print(f"[qual] video {j+1} result: ok={r.get('ok')} error={r.get('error','')[:200]}", flush=True)


def save_evidence(evidence: dict, qual_dir: Path) -> Path:
    """Save evidence JSON to the qualification directory."""
    case_id = evidence.get("case_id", "unknown")
    path = qual_dir / f"{case_id}_evidence.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def dry_run_cost_plan(
    agent_key: str,
    product_id: str = "Lagenio K2",
    quick_brief: str = "",
    content_count: int = 1,
    platforms: list[str] | None = None,
    auto_image: bool | None = None,
    auto_video: bool | None = None,
) -> dict[str, Any]:
    """Return a pre-flight cost plan for one qualification case without calling the model.

    The plan counts generation, review/repair, web-search and media calls as one
    logical-run budget, and computes an upper-bound cost from
    config/qualification.yaml (no hardcoded prices in the runner).
    """
    import re

    cfg = _load_qualification_config()
    cost_per = cfg.get("cost_per_call_ceiling", {})
    defaults = cfg.get("default_max_calls", {})
    ceilings = _qual_ceilings()

    agent_cfg = get_agent_config(load_config(), agent_key) or {}
    max_review = int(agent_cfg.get("max_review_iterations", 0))
    web_search_enabled = bool(agent_cfg.get("web_search", False))

    calls: list[dict[str, Any]] = []
    upper = 0.0
    web_calls = 0
    media_calls = 0

    # text generation for the agent's main output
    calls.append({"kind": "text", "label": f"{agent_key}:generate"})
    upper += float(cost_per.get("text", 0.05))

    # review/repair per agent config
    for i in range(max_review):
        calls.append({"kind": "text", "label": f"{agent_key}:review_{i + 1}"})
        upper += float(cost_per.get("text", 0.05))
        calls.append({"kind": "text", "label": f"{agent_key}:repair_{i + 1}"})
        upper += float(cost_per.get("text", 0.05))

    # web-search calls: limited by competitor names in quick_brief + 1 discovery guard
    if web_search_enabled:
        if agent_key == "competitor_analysis":
            # Count likely competitor model tokens in the brief (e.g. imoo Z1)
            toks = re.findall(r"[A-Za-z]+\d[\w]*", quick_brief)
            web_calls = max(1, len(toks)) + 1  # discovery + one per named competitor
        elif agent_key == "campaign_strategy":
            web_calls = 1
        for i in range(web_calls):
            calls.append({"kind": "web_search", "label": f"{agent_key}:web_search_{i + 1}"})
            upper += float(cost_per.get("web_search", 0.15))

    # content creator media calls
    if agent_key == "content_creator":
        _platforms = platforms or ["facebook"]
        _n_plat = max(1, len(_platforms))
        count_per = max(1, (content_count + _n_plat - 1) // _n_plat)
        if auto_image:
            for p in _platforms:
                for i in range(count_per):
                    calls.append({"kind": "image", "label": f"{p}:image_{i + 1}"})
                    upper += float(cost_per.get("image", 0.08))
                    media_calls += 1
        if auto_video:
            for p in _platforms:
                for i in range(count_per):
                    calls.append({"kind": "video", "label": f"{p}:video_{i + 1}"})
                    upper += float(cost_per.get("video", 0.60))
                    media_calls += 1

    configured_cap = int(defaults.get(agent_key, 0))
    max_calls = configured_cap if configured_cap > 0 else len(calls)
    conservative = round(
        sum(float(cost_per.get(c["kind"], 0.05)) for c in calls[:max_calls]), 6
    )

    if len(calls) > max_calls:
        status = f"exceeds call cap (blocks after call {max_calls})"
    elif conservative > ceilings["stage_a"]:
        status = "exceeds cost ceiling"
    else:
        status = "under ceiling"

    return {
        "agent_key": agent_key,
        "product_id": product_id,
        "quick_brief_summary": quick_brief[:120] if quick_brief else "(default job)",
        "expected_calls": calls,
        "max_calls": max_calls,
        "text_calls": 1 + 2 * max_review,
        "web_search_calls": web_calls,
        "media_calls": media_calls,
        "conservative_estimate": conservative,
        "ceiling_stage_a": ceilings["stage_a"],
        "ceiling_total": ceilings["total"],
        "status": status,
    }


def _print_dry_run_plan(plan: dict[str, Any]) -> None:
    print("[DRY-RUN COST PLAN]", flush=True)
    print(f"  agent: {plan['agent_key']} | product: {plan['product_id']}", flush=True)
    print(f"  quick_brief: {plan['quick_brief_summary']!r}", flush=True)
    print(f"  max calls: {plan['max_calls']}  "
          f"(text={plan['text_calls']}, web={plan['web_search_calls']}, media={plan['media_calls']})", flush=True)
    print(f"  conservative estimate: ${plan['conservative_estimate']:.6f} USD", flush=True)
    print(f"  status: {plan['status']}", flush=True)
    print(f"  ceiling (stage A): ${plan['ceiling_stage_a']:.2f} USD", flush=True)
    for c in plan["expected_calls"]:
        print(f"    - {c['kind']:15s} {c['label']}", flush=True)


if __name__ == "__main__":
    _init_session_baseline()
    print("This is an evaluation-only module. Use run_qualification.py to execute cases.")
    print("Current session spend:", current_spend())
