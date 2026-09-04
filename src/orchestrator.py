"""Orchestrator — coordinates the 4-agent pipeline.

Pipeline flow:
  1. Product Spec      (raw data → product spec)
  2. Competitor Analysis (product spec + competitor data → analysis)
  3. Campaign Strategy   (product spec + analysis → campaign)
  4. Content Creator     (product spec + analysis + campaign → content)

Each step can also run independently.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from .agents import (
    CampaignStrategyAgent,
    CompetitorAnalysisAgent,
    ContentCreatorAgent,
    ManagerAgent,
    ProductSpecAgent,
)
from .brand_loader import load_brand_rules, load_brand_reference, load_brand_visual, load_product_profile
from .brand_priority import load_brand_priority
from .config_loader import get_agent_config, load_config
from .data_loader import detect_data_files, get_agent_data
from .llm_client import LLMClient
from .run_context import StepRunContext, build_multimodal_content
from .flow_context import set_usage_reference, set_usage_metadata, clear_usage_context
from .ai_usage import HubReceiptCollector, flush_usage_log, reconcile_hub_receipts, USAGE_LOG_PATH
from .evaluation.campaign_qualification import (
    read_usage_log_for_reference,
    snapshot_usage_log_offset,
)

# Platform display names — ใช้ในหลายที่ นิยามครั้งเดียว
_PLATFORM_NAMES = {"facebook": "Facebook", "tiktok": "TikTok"}

# ชื่อไฟล์ output ของแต่ละ agent — นิยามครั้งเดียว
_AGENT_OUTPUT_NAMES = {
    "product_spec": "01_product_spec",
    "competitor_analysis": "02_competitor_analysis",
    "campaign_strategy": "03_campaign_strategy",
    "content_creator": "04_content_creator",
}


def _make_run_id() -> str:
    """สร้างรหัสเฉพาะรอบ สำหรับตั้งชื่อไฟล์ output."""
    return f"{datetime.now().strftime('%H%M%S_%f')}_{uuid.uuid4().hex}"


from . import product_db


def _strip_code_fence(text: str) -> str:
    """Strip markdown code fences (```json ... ```) จาก LLM response.

    ใช้ร่วมกันทุกที่ที่ parse JSON จาก LLM — กัน duplicated code.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text

console = Console()

PIPELINE_STEPS = [
    ("product_spec", ProductSpecAgent, "นักวิเคราะห์สินค้า"),
    ("competitor_analysis", CompetitorAnalysisAgent, "นักวิเคราะห์คู่แข่ง"),
    ("campaign_strategy", CampaignStrategyAgent, "นักวางกลยุทธ์แคมเปญ"),
    ("content_creator", ContentCreatorAgent, "นักสร้างคอนเทนต์"),
]


class Orchestrator:
    """Run the full marketing pipeline or individual agents."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        brand_dir: str | Path | None = None,
        product_images: list[str] | None = None,
        product_id: str | None = None,
    ) -> None:
        self.config = load_config(config_path)
        self.brand_dir = brand_dir or "brand"
        self.brand_context = load_brand_rules(self.brand_dir, product_id=product_id)
        self.brand_reference = load_brand_reference(self.brand_dir, product_id=product_id)
        self.brand_visual = load_brand_visual(self.brand_dir, product_id=product_id)
        self.brand_rules = load_brand_priority(self.brand_dir)
        self.product_images = product_images or []
        self.product_id = product_id
        self.results: dict[str, str] = {}

    def make_client(self) -> LLMClient:
        defaults = self.config.get("defaults", {})
        from .config_loader import get_env

        api_key = get_env("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY not found. "
                "Set it in .env file or environment variable."
            )
        return LLMClient(
            api_key=api_key,
            base_url=defaults.get("base_url", "https://openrouter.ai/api/v1"),
            default_model=defaults.get("model", "anthropic/claude-3.5-sonnet"),
            timeout=defaults.get("timeout_seconds", 120),
        )

    def _make_agent(self, agent_name: str, agent_cls: type, llm: LLMClient):
        cfg = get_agent_config(self.config, agent_name)
        instructions = self._load_agent_instructions(agent_name)
        agent = agent_cls(cfg, llm, brand_context=self.brand_context,
                          brand_reference=self.brand_reference, instructions=instructions,
                          brand_rules=self.brand_rules)
        # Runtime multi-brand contract.
        # StepRunContext (when provided) overrides brand_dir in agent.run().
        agent.brand_dir = getattr(self, "brand_dir", "brand")
        return agent

    def _load_agent_instructions(self, agent_name: str) -> dict:
        """Load user-set instructions for an agent from config/agent_instructions.json."""
        import json as _json
        from pathlib import Path as _Path
        path = _Path(__file__).resolve().parent.parent / "config" / "agent_instructions.json"
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return data.get(agent_name, {})
        except Exception:
            return {}

    def get_products_state(self) -> list[dict[str, Any]]:
        """Get current state of all products for manager to analyze.

        ดึงสถานะจาก product DB ไม่ใช่จาก cache/ อีกต่อไป
        แยกชัด: product_spec/competitor ใน cache/ = deliverable สำหรับ user (ไม่ใช่ data source)
        """
        products = product_db.get_all_products()
        state = []
        for p in products:
            pid = p.get("product_id", "")
            if not pid or pid.startswith("."):
                continue
            detected = detect_data_files(product_id=pid)
            state.append({
                "name": pid,
                "status": p.get("status", product_db.STATUS_EMPTY),
                "raw": detected["raw"],
                "images": detected["images"],
                "videos": detected["videos"],
                "audios": detected["audios"],
                "has_product_spec": detected["product_spec"] is not None,  # deliverable มีไหม
                "has_competitor": detected["competitor"] is not None,      # deliverable มีไหม
                "raw_text_preview": (p.get("raw_text", "") or "")[:500],   # ส่ง preview ให้ manager เห็น
            })
        return state

    def run_manager(
        self,
        user_message: str,
        conversation_history: list[dict[str, str]] | None = None,
        llm: LLMClient | None = None,
    ) -> dict[str, Any]:
        """Run manager agent to analyze user intent and plan execution."""
        own = llm is None
        if own:
            llm = self.make_client()
        try:
            agent = self._make_agent("manager", ManagerAgent, llm)
            products = self.get_products_state()
            prompt = agent.build_prompt(user_message, products, conversation_history)
            system_prompt = agent._build_system_prompt()
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
            response = self.llm_chat_raw(llm, messages, agent.config)
            return agent.parse_response(response)
        finally:
            if own:
                llm.close()

    def llm_chat_raw(self, llm: LLMClient, messages: list[dict[str, str]], config: dict) -> str:
        """Call LLM without streaming (for manager)."""
        return llm.chat(
            messages,
            model=config.get("model"),
            temperature=config.get("temperature", 0.7),
            max_tokens=config.get("max_tokens", 4096),
            max_retry_limit=config.get("max_retry_limit", 3),
            stream=False,
            source="orchestrator.manager",
        )

    # ------------------------------------------------------------------
    #  Individual agent runners
    # ------------------------------------------------------------------

    def run_product_spec(
        self, raw_data: str, product_images: list[str] | None = None, llm: LLMClient | None = None,
        quick_brief: str = "", resource_context: str = "",
        extra_image_paths: list[str] | None = None,
        step_context: StepRunContext | None = None,
    ) -> str:
        """Run product_spec agent — สร้างเอกสารสเปคสินค้าเป็น deliverable สำหรับ user.

        สิ่งนี้ไม่ใช่ data source ของ agent ตัวอื่น — agent การตลาดดึงข้อมูลจาก product DB
        product_spec agent ทำหน้าที่แปลงข้อมูลดิบ → เอกสารสเปคภาษาไทยให้ user เท่านั้น

        raw_data ที่ส่งเข้าควรมาจาก product_db.get_scoped_context_text() ไม่ใช่ไฟล์ดิบ
        เพราะไฟล์ดิบอาจเป็น catalog หลายรุ่น → LLM จะเขียนสเปคทั้งซีรีส์แทนเฉพาะรุ่นที่เลือก
        """
        own = llm is None
        if own:
            llm = self.make_client()
        try:
            agent = self._make_agent("product_spec", ProductSpecAgent, llm)
            prompt = agent.build_prompt(raw_data, product_images or self.product_images)
            # ส่งรูปจริงให้ agent (retrieve-then-read — agent เห็นรูปเหมือนมนุษย์)
            image_paths = self._get_product_image_paths() if self.product_id else (product_images or [])
            result = agent.run(
                prompt, quick_brief=quick_brief, image_paths=image_paths,
                resource_context=resource_context, extra_image_paths=extra_image_paths,
                step_context=step_context,
            )
            self.results["product_spec"] = result

            return result
        finally:
            if own:
                llm.close()

    def _get_product_data(self, fallback: str = "") -> str:
        """ดึงข้อมูลสินค้าสำหรับ agent การตลาด จาก product DB.

        ถ้ามี product_id ตั้งอยู่และ DB พร้อม → ดึงจาก DB
        ถ้าไม่มี → ใช้ fallback (parameter ที่ส่งมา สำหรับ backward compatible)

        กรณี multi-product (product_id = "K5 + K2"): ดึงแต่ละสินค้าจาก DB มารวมกัน
        เพราะ DB เก็บแยก per-product ไม่มี combined ID
        """
        if self.product_id:
            # กรณี multi-product: product_id = "Lagenio K5 + Lagenio K2"
            if " + " in self.product_id:
                parts = self.product_id.split(" + ")
                combined = []
                for pid in parts:
                    pid = pid.strip()
                    if product_db.is_ready(pid):
                        data = get_agent_data(pid)
                        if data:
                            combined.append(f"=== สินค้า: {pid} ===\n{data}")
                if combined:
                    return "\n\n".join(combined)
            # กรณี single product
            elif product_db.is_ready(self.product_id):
                db_data = get_agent_data(self.product_id)
                if db_data:
                    return db_data
        return fallback

    def _get_product_image_paths(self) -> list[str]:
        """ดึง path รูปจริงของสินค้า — สำหรับส่งเป็น multimodal ให้ agent (retrieve-then-read).

        สถาปัตยกรรมใหม่: agent เห็นรูปจริง (lossless) ไม่ใช่คำบรรยาย (lossy)
        กรณี multi-product: รวมรูปจากทุกสินค้า
        """
        if not self.product_id:
            return []
        paths: list[str] = []
        if " + " in self.product_id:
            for pid in self.product_id.split(" + "):
                pid = pid.strip()
                if product_db.is_ready(pid):
                    paths.extend(product_db.get_product_image_paths(pid))
        elif product_db.is_ready(self.product_id):
            paths = product_db.get_product_image_paths(self.product_id)
        return paths

    def run_competitor_analysis(
        self, product_spec: str, competitor_data: str | None = None, llm: LLMClient | None = None,
        quick_brief: str = "", resource_context: str = "",
        extra_image_paths: list[str] | None = None,
        step_context: StepRunContext | None = None,
    ) -> str:
        own = llm is None
        if own:
            llm = self.make_client()
        try:
            agent = self._make_agent("competitor_analysis", CompetitorAnalysisAgent, llm)
            # ดึงข้อมูลสินค้าจาก DB ถ้ามี ไม่งั้นใช้ parameter (backward compatible)
            product_data = self._get_product_data(product_spec)
            # If competitor_data is None or empty, agent will search web itself
            prompt = agent.build_prompt(product_data, competitor_data or "")
            image_paths = self._get_product_image_paths()
            result = agent.run(
                prompt, quick_brief=quick_brief, image_paths=image_paths,
                resource_context=resource_context, extra_image_paths=extra_image_paths,
                step_context=step_context,
            )
            self.results["competitor_analysis"] = result

            return result
        finally:
            if own:
                llm.close()

    def run_campaign_strategy(
        self, product_spec: str, competitor_analysis: str, llm: LLMClient | None = None,
        quick_brief: str = "", resource_context: str = "",
        extra_image_paths: list[str] | None = None,
        step_context: StepRunContext | None = None,
        web_search: bool | None = None,
    ) -> str:
        own = llm is None
        if own:
            llm = self.make_client()
        run_exception: Exception | None = None
        run_ref = ""
        try:
            agent = self._make_agent("campaign_strategy", CampaignStrategyAgent, llm)
            if web_search is not None:
                if agent.config is None:
                    agent.config = {}
                agent.config["web_search"] = web_search
            # ดึงข้อมูลสินค้าจาก DB ถ้ามี ไม่งั้นใช้ parameter (backward compatible)
            product_data = self._get_product_data(product_spec)
            # CampaignStrategyAgent.build_prompt accepts a single context dict.
            # All policy rules live in the agent's system prompt (agents.yaml);
            # the orchestrator only passes data, not validation flags.
            context = {
                "product": product_data,
                "competitors": competitor_analysis or "",
            }
            prompt = agent.build_prompt(context)
            image_paths = self._get_product_image_paths()
            run_ref = f"orchestrator:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
            set_usage_reference(run_ref)
            set_usage_metadata({
                "product_id": self.product_id or "",
                "agent": "campaign_strategy",
                "run_ref": run_ref,
            })
            usage_log_offset = snapshot_usage_log_offset(USAGE_LOG_PATH)
            result: str = ""
            with HubReceiptCollector() as hub_collector:
                try:
                    result = agent.run(
                        prompt, quick_brief=quick_brief, image_paths=image_paths,
                        resource_context=resource_context, extra_image_paths=extra_image_paths,
                        step_context=step_context,
                    )
                except Exception as exc:
                    run_exception = exc
                finally:
                    # Preserve the latest generated/repaired text even on exception.
                    last_draft = getattr(agent, "_last_draft_output", "") or ""
                    last_repaired = getattr(agent, "_last_repaired_output", "") or ""
                    final_text = result or last_repaired or last_draft

                    # Read local usage and flush Hub receipts even on exception.
                    run_entries = read_usage_log_for_reference(USAGE_LOG_PATH, run_ref, usage_log_offset)
                    run_cost_usd = round(sum(float(e.get("cost_usd", 0) or 0) for e in run_entries), 6)
                    actual_model = run_entries[-1].get("model") if run_entries else "unknown"
                    request_ids = [e.get("request_id") for e in run_entries if e.get("request_id")]
                    call_classifications = [e.get("source") for e in run_entries]

                    hub_ack = flush_usage_log(timeout=5.0)
                    hub_reconciliation = reconcile_hub_receipts(run_entries, hub_collector.results, flush_completed=hub_ack)

                    self.results["campaign_strategy_run_ref"] = run_ref
                    self.results["campaign_strategy_output"] = final_text
                    self.results["campaign_strategy_draft"] = last_draft
                    self.results["campaign_strategy_repaired"] = last_repaired
                    self.results["campaign_strategy_raw_annotations"] = list(getattr(agent, "_last_annotations", []) or [])
                    self.results["campaign_strategy_relevant_annotations"] = list(getattr(agent, "_last_relevant_annotations", []) or [])
                    self.results["campaign_strategy_selected_evidence"] = list(getattr(agent, "_selected_evidence", []) or [])
                    self.results["campaign_strategy_selected_evidence_urls"] = sorted(getattr(agent, "_selected_evidence_urls", set()))
                    self.results["campaign_strategy_requested_competitor_models"] = sorted(getattr(agent, "_requested_competitor_models", set()))
                    self.results["campaign_strategy_evidence_confirmed_competitor_models"] = sorted(getattr(agent, "_evidence_confirmed_competitor_models", set()))
                    self.results["campaign_strategy_run_entries"] = run_entries
                    self.results["campaign_strategy_run_cost_usd"] = run_cost_usd
                    self.results["campaign_strategy_actual_model"] = actual_model
                    self.results["campaign_strategy_request_ids"] = request_ids
                    self.results["campaign_strategy_call_classifications"] = call_classifications
                    self.results["campaign_strategy_hub_results"] = hub_collector.results
                    self.results["campaign_strategy_hub_reconciliation"] = hub_reconciliation

            # Audit the preserved final text (draft, repaired, or final) for diagnostics.
            try:
                if final_text:
                    audit = agent.audit_output(final_text)
                    self.results["campaign_strategy_validator_results"] = [{"rule": a.rule, "ok": a.ok, "reason": a.reason} for a in audit]
                    self.results["campaign_strategy_validator_ok"] = all(a.ok for a in audit)
                else:
                    self.results["campaign_strategy_validator_results"] = []
                    self.results["campaign_strategy_validator_ok"] = False
            except Exception as audit_exc:
                self.results["campaign_strategy_validator_results"] = [{"rule": "audit_error", "ok": False, "reason": str(audit_exc)}]
                self.results["campaign_strategy_validator_ok"] = False

            if run_exception:
                raise run_exception
            return final_text
        finally:
            clear_usage_context()
            if own:
                llm.close()

    def run_content_creator(
        self,
        product_spec: str,
        competitor_analysis: str,
        campaign_strategy: str,
        llm: LLMClient | None = None,
        quick_brief: str = "",
        media_type: str = "",
        asset_summary: str = "",
        resource_context: str = "",
        extra_image_paths: list[str] | None = None,
        step_context: StepRunContext | None = None,
    ) -> str:
        own = llm is None
        if own:
            llm = self.make_client()
        try:
            agent = self._make_agent("content_creator", ContentCreatorAgent, llm)
            # ดึงข้อมูลสินค้าจาก DB ถ้ามี ไม่งั้นใช้ parameter (backward compatible)
            product_data = self._get_product_data(product_spec)

            # ดึง media model capabilities เพื่อบอก agent ว่า model ทำได้อะไร (grounding)
            media_caps_text = ""
            try:
                from . import media_gen
                cfg = media_gen._load_media_config()
                video_model = cfg.get("video_model", media_gen.DEFAULT_VIDEO_MODEL)
                image_model = cfg.get("image_model", media_gen.DEFAULT_IMAGE_MODEL)
                video_caps = media_gen.format_capabilities_for_prompt(video_model, kind="video")
                image_caps = media_gen.format_capabilities_for_prompt(image_model, kind="image")
                caps_parts = []
                if video_caps:
                    caps_parts.append(f"[Video] {video_caps}")
                if image_caps:
                    caps_parts.append(f"[Image] {image_caps}")
                if caps_parts:
                    media_caps_text = "\n".join(caps_parts)
            except Exception:
                pass  # ดึงไม่ได้ → ไม่บังคับ ใช้ default

            # Visual style hint — high-level style จาก visual.json (ส่งให้ LLM)
            # image_style/keywords อาจเป็น string (จาก product_profile รุ่นเก่า)
            # หรือ dict/list (จาก UI) — ต้องรองรับทั้งสองรูปแบบ
            visual_style = ""
            if self.brand_visual:
                style = self.brand_visual.get("image_style", {})
                if isinstance(style, str):
                    tone = style
                else:
                    tone = style.get("tone", "") if isinstance(style, dict) else ""
                keywords_raw = self.brand_visual.get("keywords", [])
                if isinstance(keywords_raw, str):
                    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
                else:
                    keywords = keywords_raw or []
                if tone or keywords:
                    visual_parts = []
                    if tone:
                        visual_parts.append(f"โทนภาพ: {tone}")
                    if keywords:
                        visual_parts.append("คำสำคัญ: " + ", ".join(keywords))
                    visual_style = "\n".join(visual_parts)
                # Video style profile (จาก analyze_video_style — Style Reverse-Engineering)
                video_style = self.brand_visual.get("video_style", {})
                if video_style:
                    vs_parts = []
                    summary = video_style.get("style_summary", "")
                    if summary:
                        vs_parts.append(f"สไตล์วิดีโอ: {summary}")
                    pacing = video_style.get("pacing", "")
                    if pacing:
                        vs_parts.append(f"จังหวะ: {pacing}")
                    transitions = video_style.get("transitions", [])
                    if transitions:
                        vs_parts.append("transitions: " + ", ".join(transitions))
                    color = video_style.get("color_grading", "")
                    if color:
                        vs_parts.append(f"โทนสี: {color}")
                    if vs_parts:
                        visual_style = (visual_style + "\n" if visual_style else "") + "\n".join(vs_parts)

            prompt = agent.build_prompt(
                product_data, competitor_analysis, campaign_strategy,
                media_capabilities=media_caps_text,
                media_type=media_type,
                visual_style=visual_style,
                asset_summary=asset_summary,
            )
            image_paths = self._get_product_image_paths()
            # ใช้ Structured Outputs — LLM คืน JSON ที่ตรง schema
            # แทนการ parse markdown ด้วย regex (ที่พังทุกครั้งที่ format เปลี่ยน)
            # LLM คืนแค่ posts (structured data) — เรา generate markdown เอง
            from .content_schema import CONTENT_RESPONSE_FORMAT, render_posts_to_markdown
            import json as _json_cc
            raw_result = agent.run(
                prompt, quick_brief=quick_brief, image_paths=image_paths,
                resource_context=resource_context, extra_image_paths=extra_image_paths,
                response_format=CONTENT_RESPONSE_FORMAT,
                step_context=step_context,
            )
            # แปลง JSON → markdown สำหรับ display + เก็บ JSON ดิบไว้สำหรับ parse_media_prompts
            try:
                parsed = _json_cc.loads(raw_result)
                markdown = render_posts_to_markdown(parsed)
                # เก็บทั้ง JSON ดิบ (สำหรับ media gen) และ markdown (สำหรับ display)
                # save_result จะแยกเซฟ .json + .md
                self.results["content_creator"] = raw_result
                self.results["content_creator_markdown"] = markdown
            except (_json_cc.JSONDecodeError, TypeError):
                self.results["content_creator"] = raw_result
                self.results["content_creator_markdown"] = raw_result
            return raw_result
        finally:
            if own:
                llm.close()

    def _select_assets_for_content(
        self,
        llm: LLMClient,
        quick_brief: str = "",
        concept: str = "",
        product_ids: list[str] | None = None,
        preselected_asset_ids: list[str] | None = None,
    ) -> str:
        """Asset selection phase — เลือกวัตถุดิบแบรนด์ที่เหมาะกับแคมเปญ.

        เรียก LLM 1 รอบ ให้เลือก asset จาก library ผ่าน tool calling
        รับ concept + product_ids จาก select_product_auto เพื่อเลือก asset ที่เหมาะกับแนวคิด.

        ถ้า preselected_asset_ids มีค่าแล้ว (เลือกจาก select_product_auto) → ใช้เลย ไม่เรียก LLM.
        ถ้า asset library ว่าง → คืน "" (ข้ามทั้ง phase ไม่เสีย LLM call).
        คืน: string สรุป asset ที่เลือก (id + description + วิธีใช้) สำหรับแปะเข้า prompt.
        """
        from . import asset_library
        all_assets = asset_library.list_all()
        if not all_assets:
            return ""

        import json as _json
        from .config_loader import get_section, get_agent_config

        auto_cfg = get_section(self.config, "auto_mode")
        manager_cfg = get_agent_config(self.config, "manager")

        # ถ้า select_product_auto เลือก asset มาแล้ว → ใช้เลย ไม่เรียก LLM ซ้ำ
        if preselected_asset_ids:
            selected_ids = preselected_asset_ids
            reason = "เลือกจากจังหวะเลือกสินค้า"
            usage_hints = {}
        else:
            tools = asset_library.tool_definitions()
            tool_handlers = asset_library.tool_handlers()

            # สร้างบริบท — concept + product + quick_brief
            context_parts = []
            if concept:
                context_parts.append(f"แนวคิดที่เลือก: {concept}")
            if product_ids:
                context_parts.append(f"สินค้า: {', '.join(product_ids)}")
            if quick_brief:
                context_parts.append(f"คำขอเพิ่มเติมจาก user: {quick_brief}")
            context = "\n".join(context_parts) if context_parts else "ไม่มีบริบทเพิ่มเติม"

            messages = [
                {"role": "system", "content": (
                    "คุณเป็น assistant เลือกวัตถุดิบแบรนด์ (asset) ที่เหมาะสมกับคอนเทนต์ที่จะสร้าง "
                    "เรียก list_assets() เพื่อดูว่ามี asset อะไรบ้าง "
                    "แล้วเลือก asset ที่เกี่ยวข้องกับแนวคิดและสินค้า "
                    "ตอบเป็น JSON: {\"selected_asset_ids\": [\"a_0001\", ...], \"reason\": \"เหตุผล\", \"usage_hints\": {\"a_0001\": \"วิธีใช้ asset นี้\"}} "
                    "ถ้าไม่มี asset ที่เหมาะ ตอบ {\"selected_asset_ids\": [], \"reason\": \"...\", \"usage_hints\": {}}"
                )},
                {"role": "user", "content": f"เลือกวัตถุดิบแบรนด์ที่จะใช้ประกอบคอนเทนต์\n{context}"},
            ]

            try:
                response = llm.chat_with_tools(
                    messages,
                    tools=tools,
                    tool_handlers=tool_handlers,
                    model=manager_cfg.get("model"),
                    temperature=manager_cfg.get("temperature", 0.7),
                    max_tokens=auto_cfg.get("max_tokens", 4096),
                    max_retry_limit=manager_cfg.get("max_retry_limit", 3),
                    max_iterations=auto_cfg.get("max_iterations", 10),
                    source="orchestrator.select_assets",
                )
                text = _strip_code_fence(response)
                parsed = _json.loads(text)
                selected_ids = parsed.get("selected_asset_ids", [])
                reason = parsed.get("reason", "")
                usage_hints = parsed.get("usage_hints", {})
            except Exception as e:
                print(f"[Orchestrator] asset selection failed: {e}", flush=True)
                return ""

        if not selected_ids:
            return ""

        # สร้างสรุป asset ที่เลือก (id + description + วิธีใช้)
        summaries = []
        for aid in selected_ids:
            rec = asset_library.get_asset(aid)
            if rec:
                tags_str = ", ".join(rec.get("tags", [])) if rec.get("tags") else "-"
                hint = usage_hints.get(aid, "")
                hint_section = f" | วิธีใช้: {hint}" if hint else ""
                summaries.append(
                    f"- ID: {rec['id']} | {rec.get('file', '')} | "
                    f"type: {rec.get('type', '')} | subject: {rec.get('subject', '')} | "
                    f"tags: {tags_str} | คำบรรยาย: {rec.get('description', '')}{hint_section}"
                )
        if not summaries:
            return ""
        return f"เหตุผลที่เลือก: {reason}\n" + "\n".join(summaries)

    # ------------------------------------------------------------------
    #  Script Review — self-review อัตโนมัติ (ใช้ร่วม auto + regular flow)
    # ------------------------------------------------------------------

    def _review_script_in_posts(
        self,
        posts: list[dict[str, Any]],
        platform: str,
        llm: LLMClient | None = None,
        status_callback=None,
        ch_cfg: dict | None = None,
    ) -> dict[str, Any]:
        """ตรวจ script ใน posts — แก้ script ถ้า score ต่ำ + สร้าง video_prompts ใหม่.

        แก้ posts ใน place (อัปเดต script + video_prompts + เพิ่ม script_review field).
        คืน script_review_result dict (สำหรับเก็บใน all_script_reviews).
        """
        import json as _json
        from datetime import datetime as _dt

        ch_cfg = ch_cfg or {}
        script_review_result: dict[str, Any] = {}

        try:
            # หา post ที่มี script
            script_post_idx = -1
            original_script = ""
            for i, p in enumerate(posts):
                if p.get("script", "").strip():
                    script_post_idx = i
                    original_script = p["script"]
                    break

            if script_post_idx >= 0 and original_script:
                from .script_reviewer import review_script
                threshold = int(ch_cfg.get("script_review_threshold", 70))
                max_iter = min(int(ch_cfg.get("script_review_max_iterations", 2)), 2)
                iterations_done = 0

                # รอบ 1: ตรวจ script เดิม
                if status_callback:
                    status_callback(f"กำลังตรวจ script (รอบที่ 1/{max_iter})...")
                script_review_result = review_script(
                    original_script,
                    platform or "TikTok",
                    llm,
                )
                iterations_done = 1

                first_score = script_review_result.get("score", 0) if script_review_result else 0
                if status_callback and script_review_result:
                    status_callback(
                        f"Script review รอบที่ 1: score {first_score}/100, "
                        f"{len(script_review_result.get('issues', []))} จุดน่าเบื่อ"
                    )

                # ถ้ารอบ 1 ผ่าน → ใช้ script เดิม จบ
                if first_score >= threshold or not script_review_result:
                    final_script = original_script
                    final_score = first_score
                else:
                    # รอบ 2: ใช้ revised_script แล้วตรวจใหม่
                    revised = script_review_result.get("revised_script", "")
                    if not revised.strip():
                        final_script = original_script
                        final_score = first_score
                    else:
                        if status_callback:
                            status_callback(f"กำลังตรวจ script ที่แก้ (รอบที่ 2/{max_iter})...")
                        re_review = review_script(
                            revised,
                            platform or "TikTok",
                            llm,
                        )
                        iterations_done = 2
                        re_score = re_review.get("score", 0) if re_review else 0

                        if status_callback:
                            status_callback(
                                f"Script review รอบที่ 2: score {re_score}/100"
                            )

                        # Loop safety: ถ้า re-check ต่ำกว่า original → revert
                        if re_score < first_score:
                            if status_callback:
                                status_callback(
                                    f"⚠ Script ที่แก้ ({re_score}/100) แย่กว่าต้นฉบับ "
                                    f"({first_score}/100) — ใช้ต้นฉบับ"
                                )
                            final_script = original_script
                            final_score = first_score
                            script_review_result["re_review"] = re_review
                        else:
                            final_script = revised
                            final_score = re_score
                            script_review_result = re_review
                            script_review_result["original_score"] = first_score

                # --- Apply: อัปเดต script ใน post ---
                script_changed = final_script.strip() != original_script.strip()

                if script_changed and final_script.strip():
                    posts[script_post_idx]["script"] = final_script
                    # สร้าง video_prompts ใหม่จาก script สุดท้าย
                    if status_callback:
                        status_callback("กำลังสร้าง video_prompts ใหม่จาก script ที่แก้...")
                    try:
                        from .config_loader import get_agent_config
                        cc_cfg = get_agent_config(self.config, "content_creator")
                        vp_system = cc_cfg.get("system_prompt", "")
                        vp_user = (
                            f"เขียน video_prompts ใหม่จาก script นี้ (platform: {platform or 'TikTok'}):\n\n"
                            f"{final_script}\n\n"
                            f"คืน JSON ตาม schema ใน system prompt"
                        )
                        from .content_schema import CONTENT_RESPONSE_SCHEMA
                        vp_response = llm.chat(
                            [{"role": "system", "content": vp_system},
                             {"role": "user", "content": vp_user}],
                            temperature=cc_cfg.get("temperature", 0.9),
                            max_tokens=cc_cfg.get("max_tokens", 4096),
                            stream=False,
                            response_format={
                                "type": "json_schema",
                                "json_schema": {
                                    "name": "content_output",
                                    "strict": True,
                                    "schema": CONTENT_RESPONSE_SCHEMA["schema"],
                                },
                            },
                            source="script_review.regenerate_prompts",
                        )
                        vp_clean = _strip_code_fence(vp_response)
                        vp_data = _json.loads(vp_clean)
                        vp_posts = vp_data.get("posts", [])
                        if vp_posts and vp_posts[0].get("video_prompts"):
                            posts[script_post_idx]["video_prompts"] = vp_posts[0]["video_prompts"]
                    except Exception:
                        pass  # สร้าง video_prompts ไม่ได้ → ใช้ของเดิม

                # บันทึก review state ลงใน post
                posts[script_post_idx]["script_review"] = {
                    "status": "reviewed",
                    "reviewed_at": _dt.now().isoformat(),
                    "score": final_score,
                    "iterations": iterations_done,
                    "threshold": threshold,
                    "script_changed": script_changed,
                    "issues_count": len(script_review_result.get("issues", [])),
                    "hooks_count": len(script_review_result.get("suggested_hooks", [])),
                    "review": script_review_result,
                }

                if status_callback:
                    if final_score >= threshold:
                        status_callback(
                            f"✓ Script review ผ่าน: {final_score}/100 "
                            f"({iterations_done} รอบ, {'แก้แล้ว' if script_changed else 'ไม่ต้องแก้'})"
                        )
                    else:
                        status_callback(
                            f"⚠ Script score {final_score}/100 ยังต่ำกว่า {threshold} "
                            f"หลัง {iterations_done} รอบ — ใช้ script ล่าสุด"
                        )
        except Exception:
            pass  # review พังไม่ต้อง crash pipeline

        return script_review_result

    # ------------------------------------------------------------------
    #  Auto mode — agent เลือกสินค้าเอง + คอนเทนต์ไม่ซ้ำ
    # ------------------------------------------------------------------

    def select_product_auto(
        self,
        llm: LLMClient | None = None,
        quick_brief: str = "",
        platforms: list[str] | None = None,
        product_count: int = 1,
        step_context: StepRunContext | None = None,
    ) -> dict[str, Any]:
        """Phase 1 ของ auto mode — ให้ LLM เลือกสินค้า + แนวคิดที่ยังไม่ซ้ำ.

        ใช้ tool calling — LLM เรียก function เอง:
          - list_products() → ดูสินค้าทั้งหมด (metadata สั้น)
          - get_product_detail(product_id) → ดูสเปคเต็มของสินค้าที่สนใจ
          - get_content_history() → ดูประวัติคอนเทนต์ที่เคยทำ

        Open-ended: LLM สร้างสรรค์ได้อิสระ — เลือก 1 ชิ้น, 2 ชิ้นมาเปรียบเทียบ, หรือหลายชิ้นมารวม
        user กำหนดทิศทางได้ผ่าน quick_brief (คำสั่งเฉพาะรอบ ไม่ใช่ instruction ถาวร)

        คืน JSON: {product_ids: [...], concept, reason}
        """
        import json as _json
        from . import asset_library
        from . import content_history
        from . import pillar_manager
        from .config_loader import get_section

        own = llm is None
        if own:
            llm = self.make_client()
        try:
            project_root = Path(__file__).resolve().parent.parent

            # อ่าน config
            auto_cfg = get_section(self.config, "auto_mode")
            ch_cfg = get_section(self.config, "content_history")
            pillars = self.config.get("pillars", [])

            # ตรวจว่ามีสินค้า ready อย่างน้อย 1 ชิ้น
            all_products = product_db.get_all_products()
            ready_count = sum(
                1 for p in all_products
                if p.get("product_id") and not p.get("product_id", "").startswith(".")
                and p.get("status") == product_db.STATUS_READY
            )
            if ready_count == 0:
                return {"error": "ไม่มีสินค้าที่พร้อมในระบบ — กรุณาอัปโหลดและ ingest สินค้าก่อน", "step_context": step_context}

            # ปรับ product_count ตามจำนวนสินค้าที่พร้อมจริงในฐานข้อมูล
            if product_count > ready_count:
                product_count = ready_count

            # ค่าจาก config (ไม่ใช่ hardcode)
            summary_len = auto_cfg.get("list_summary_length", 200)
            summary_fallback_len = auto_cfg.get("list_summary_fallback", 300)
            detail_text_len = auto_cfg.get("detail_text_length", 3000)
            history_limit = ch_cfg.get("default_limit", 50)

            # --- tool definitions (OpenAI schema) ---
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "list_products",
                        "description": "ดูรายการสินค้าทั้งหมดที่พร้อมใช้งาน (metadata สั้น) — เรียกครั้งแรกเพื่อดูตัวเลือก",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "category": {
                                    "type": "string",
                                    "description": "กรองตามหมวดหมู่ (optional) — เช่น 'สมาร์ทวอทช์', 'เครื่องดื่ม'",
                                },
                            },
                            "required": [],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "get_product_detail",
                        "description": "ดูสเปคสินค้าเต็มของสินค้าที่สนใจ — เรียกหลังจาก list_products แล้วเลือกสินค้าที่อยากดูละเอียด",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "product_id": {
                                    "type": "string",
                                    "description": "ชื่อสินค้า (product_id) ที่ได้จาก list_products",
                                },
                            },
                            "required": ["product_id"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "get_content_history",
                        "description": "ดูประวัติคอนเทนต์ที่เคยสร้างไปแล้ว — เพื่อหลีกเลี่ยงการทำซ้ำ",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "product_id": {
                                    "type": "string",
                                    "description": "ดูประวัติเฉพาะสินค้านี้ (optional) — ถ้าไม่ใส่จะดูทั้งหมด",
                                },
                            },
                            "required": [],
                        },
                    },
                },
            ] + asset_library.tool_definitions()

            # --- tool handlers (function จริง) ---
            def _list_products(category: str = "") -> list[dict]:
                products = product_db.get_all_products()
                result = []
                for p in products:
                    pid = p.get("product_id", "")
                    if not pid or pid.startswith("."):
                        continue
                    if p.get("status") != product_db.STATUS_READY:
                        continue
                    meta = product_db.get_product_metadata(pid)
                    if category and category.lower() not in (meta.get("category", "") or "").lower():
                        continue
                    # ถ้า summary ว่าง → ดึงส่วนแรกของ text context มาแทน
                    summary = meta.get("summary", "")
                    if not summary:
                        ctx = product_db.get_agent_context(pid)
                        text = ctx.get("text", "")
                        summary = text[:summary_fallback_len].replace("\n", " ").strip()
                    # ดึง product_profile positioning — ช่วย LLM เลือกสินค้าฉลาดขึ้น
                    profile = load_product_profile(pid)
                    result.append({
                        "product_id": pid,
                        "summary": summary[:summary_len],
                        "category": meta.get("category", ""),
                        "image_count": meta.get("image_count", 0),
                        "price_tier": profile.get("price_tier", ""),
                        "differentiators": profile.get("differentiators", []),
                    })
                return result

            def _get_product_detail(product_id: str) -> dict:
                if not product_db.is_ready(product_id):
                    return {"error": f"สินค้า {product_id} ไม่พร้อมหรือไม่มีในระบบ"}
                ctx = product_db.get_agent_context(product_id)
                profile = load_product_profile(product_id)
                return {
                    "product_id": product_id,
                    "text_context": ctx.get("text", "")[:detail_text_len],
                    "image_count": len(ctx.get("image_paths", [])),
                    "product_profile": profile,
                }

            def _get_content_history(product_id: str = "") -> list[dict]:
                if product_id:
                    entries = content_history.get_entries_for_product(
                        project_root, product_id, config=ch_cfg,
                    )
                else:
                    entries = content_history.get_recent_entries(
                        project_root, config=ch_cfg,
                    )
                # strip embedding — LLM ไม่ได้ใช้ vector ทำอะไร
                # แต่ละ entry มี embedding 1536 floats = ~31K chars
                # ถ้าส่งไป LLM จะกิน tokens มหาศาล (340K+ tokens ต่อ call)
                return [
                    {k: v for k, v in e.items() if k != "embedding"}
                    for e in entries
                ]

            # asset tools — ใช้ shared helpers จาก asset_library (กัน duplicated code)
            asset_handlers = asset_library.tool_handlers()
            tool_handlers = {
                "list_products": _list_products,
                "get_product_detail": _get_product_detail,
                "get_content_history": _get_content_history,
                "list_assets": asset_handlers["list_assets"],
                "get_asset_detail": asset_handlers["get_asset_detail"],
            }

            # --- prompt สำหรับ LLM (อ่านจาก config) ---
            platform_str = ""
            if platforms:
                platform_names = _PLATFORM_NAMES
                selected = [platform_names.get(p, p) for p in platforms]
                platform_str = f"\nแพลตฟอร์มที่ต้องสร้าง: {' หรือ '.join(selected)}"

            effective_brief = quick_brief
            resource_section = ""
            if step_context is not None:
                effective_brief = step_context.quick_brief
                selection_view = step_context.for_phase("selection")
                if selection_view.instruction_text:
                    resource_section = f"\n\n--- เอกสารประกอบจากผู้ใช้ ---\n{selection_view.instruction_text}"

            brief_section = ""
            if effective_brief:
                brief_section = f"\n\nคำขอเพิ่มเติมจาก user: {effective_brief}"

            # Output schema — ใช้ product_ids เสมอ (รองรับทั้ง 1 และหลายชิ้น)
            count_hint = ""
            if product_count > 1:
                count_hint = f"\n\nUser ต้องการให้เลือก {product_count} สินค้ามาทำคอนเทนต์รวมกันใน 1 โพสต์"
            else:
                count_hint = "\n\nUser เลือกโหมดแยก — เลือกสินค้า 1 ชิ้นเท่านั้น"
            output_schema = '{"product_ids": ["สินค้า1", ...], "pillar": "หมวดคอนเทนต์", "concept": "แนวคิด", "reason": "เหตุผล", "asset_ids": ["a_0001", ...]}'

            # ใช้ prompt จาก config (open-ended) + pillar context
            base_prompt = auto_cfg.get("selection_prompt", "")

            # เพิ่ม Content Pillars context — บอก LLM ว่ามีหมวดอะไร ใช้ไปกี่ครั้ง
            pillar_ctx = ""
            if pillars:
                history = content_history.load_history(project_root)
                usage = pillar_manager.get_pillar_usage(history, pillars)
                pillar_ctx = pillar_manager.build_pillar_context(pillars, usage)
                pillar_ctx = f"\n\n{pillar_ctx}\n"

            system_prompt = (
                f"{base_prompt}\n\n"
                f"{pillar_ctx}"
                f"รูปแบบคำตอบ JSON:\n   {output_schema}\n"
                f"{count_hint}"
            )

            user_prompt = (
                f"เลือกสินค้าและแนวคิดเพื่อสร้างคอนเทนต์{platform_str}{brief_section}{resource_section}\n\n"
                f"ขั้นตอน: เรียก list_products() → เรียก get_content_history() → "
                f"เรียก get_product_detail() สำหรับสินค้าที่สนใจ → ตอบ JSON\n"
                f"ถ้ามีวัตถุดิบแบรนด์ (โลโก้ พรีเซนเตอร์ เพลง) ให้เรียก list_assets() "
                f"เพื่อดูว่ามีอะไรใช้ประกอบคอนเทนต์ได้\n\n"
                f"สำคัญ: ต้องเลือก Content Pillar จาก list ใน system prompt "
                f"และส่ง field \"pillar\" ใน JSON ด้วย\n"
                f"สำคัญ: เลือก pillar ที่เหมาะสมกับจำนวนสินค้าที่เลือก "
                f"(เช่น ถ้าเลือกสินค้า 1 ชิ้น ห้ามเลือก pillar ที่ต้องเปรียบเทียบหลายชิ้น)"
            )

            if step_context is not None:
                selection_view = step_context.for_phase("selection")
                user_content = build_multimodal_content(user_prompt, selection_view.image_paths)
            else:
                user_content = user_prompt

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

            # --- tool calling loop (ค่าจาก config) ---
            manager_cfg = get_agent_config(self.config, "manager")
            response = llm.chat_with_tools(
                messages,
                tools=tools,
                tool_handlers=tool_handlers,
                model=manager_cfg.get("model"),
                temperature=manager_cfg.get("temperature", 0.7),
                max_tokens=auto_cfg.get("max_tokens", 4096),
                max_retry_limit=manager_cfg.get("max_retry_limit", 3),
                max_iterations=auto_cfg.get("max_iterations", 10),
                source="orchestrator.select_product_auto",
            )

            # parse JSON จากคำตอบสุดท้าย
            text = _strip_code_fence(response)
            try:
                result = _json.loads(text)
            except _json.JSONDecodeError:
                err_len = auto_cfg.get("raw_text_preview_length", 500)
                return {"error": f"LLM ไม่คืน JSON ที่ถูกต้อง: {response[:err_len]}", "step_context": step_context}

            # รองรับทั้ง product_id (1 ชิ้น) และ product_ids (หลายชิ้น)
            if "product_id" in result and "product_ids" not in result:
                result["product_ids"] = [result["product_id"]]
            product_ids = result.get("product_ids", [])
            if not product_ids:
                return {"error": "LLM ไม่ได้เลือกสินค้า", "step_context": step_context}

            # ตรวจทุกสินค้าที่เลือก — ต้องมีจริงในระบบ
            all_ready = [
                p["product_id"] for p in product_db.get_all_products()
                if p.get("status") == product_db.STATUS_READY
                and not p.get("product_id", "").startswith(".")
            ]
            validated = []
            for pid in product_ids:
                pid = pid.strip() if isinstance(pid, str) else str(pid)
                if product_db.is_ready(pid):
                    validated.append(pid)
                else:
                    for vpid in all_ready:
                        if pid.lower() in vpid.lower() or vpid.lower() in pid.lower():
                            validated.append(vpid)
                            break
                    else:
                        return {"error": f"LLM เลือกสินค้าที่ไม่มีในระบบ: {pid}", "step_context": step_context}
            result["product_ids"] = validated
            result["product_id"] = validated[0]

            # validate asset_ids ถ้า LLM เลือกมาแล้ว (เก็บจากจังหวะนี้ ส่งต่อไป content_creator)
            selected_assets = result.get("asset_ids", [])
            if selected_assets:
                valid_assets = []
                all_assets = asset_library.list_all()
                valid_ids = {a.get("id") for a in all_assets}
                for aid in selected_assets:
                    if isinstance(aid, str) and aid in valid_ids:
                        valid_assets.append(aid)
                result["asset_ids"] = valid_assets

            # ถ้า LLM ไม่ส่ง pillar กลับมา → infer จาก concept
            if not result.get("pillar") and pillars:
                pillar_keywords = self.config.get("pillar_keywords", {})
                result["pillar"] = pillar_manager.infer_pillar(
                    result.get("concept", ""), pillars, pillar_keywords,
                )

            result["step_context"] = step_context.with_phase("selection") if step_context is not None else None
            return result
        finally:
            if own:
                llm.close()

    def run_content_creator_auto(
        self,
        llm: LLMClient | None = None,
        quick_brief: str = "",
        media_type: str = "",
        platforms: list[str] | None = None,
        product_count: int = 1,
        status_callback=None,
        resource_context: str = "",
        extra_image_paths: list[str] | None = None,
        step_context: StepRunContext | None = None,
    ) -> dict[str, Any]:
        """Auto mode — agent เลือกสินค้าเอง + สร้างคอนเทนต์ที่ไม่ซ้ำ.

        2 phase:
          1. select_product_auto() — LLM เลือกสินค้า + แนวคิด
          2. run_content_creator() — สร้างคอนเทนต์จากสินค้าที่เลือก

        Dedup ใช้ embeddings + cosine similarity (ตามมาตรฐานตลาด)

        Returns:
            dict ที่มี:
            - product_ids: สินค้าที่เลือก (array)
            - product_id: สินค้าแรก (backward compat)
            - concept: แนวคิดที่เลือก
            - reason: เหตุผลที่เลือก
            - content: ผลลัพธ์ JSON จาก content_creator
            - markdown: markdown สำหรับ display
            - is_duplicate: ถ้าคอนเทนต์ซ้ำกับที่เคยทำ
            - error: ถ้ามีปัญหา
        """
        import json as _json
        from . import content_history
        from .config_loader import get_section

        own = llm is None
        if own:
            llm = self.make_client()
        try:
            # อ่าน config
            auto_cfg = get_section(self.config, "auto_mode")
            ch_cfg = get_section(self.config, "content_history")

            # --- Phase 1: เลือกสินค้า ---
            if status_callback:
                status_callback("กำลังเลือกสินค้าและแนวคิด...")
            selection = self.select_product_auto(
                llm=llm, quick_brief=quick_brief, platforms=platforms,
                product_count=product_count,
                step_context=step_context,
            )
            if "error" in selection:
                return selection

            step_context = selection.get("step_context") or step_context
            chosen_pids = selection.get("product_ids", [])
            chosen_concept = selection.get("concept") or selection.get("angle", "")
            chosen_pillar = selection.get("pillar", "")
            chosen_asset_ids = selection.get("asset_ids", [])
            reason = selection.get("reason", "")

            if status_callback:
                status_callback(f"เลือก: {', '.join(chosen_pids)} — {chosen_concept}")

            # --- Phase 1.5: เลือก asset (ครั้งเดียว ก่อน retry loop) ---
            # ถ้า select_product_auto เลือก asset มาแล้ว → ใช้เลย ไม่เรียก LLM ซ้ำ
            # ถ้ายัง → เรียก LLM 1 รอบ (มี concept + product_ids ส่งต่อ)
            asset_summary = self._select_assets_for_content(
                llm, quick_brief,
                concept=chosen_concept, product_ids=chosen_pids,
                preselected_asset_ids=chosen_asset_ids or None,
            )

            # --- Phase 2: สร้างคอนเทนต์ ---
            multi_text_len = auto_cfg.get("multi_product_text_length", 2000)
            if len(chosen_pids) == 1:
                self.product_id = chosen_pids[0]
            else:
                self.product_id = chosen_pids[0]
                multi_context = "\n\n--- สินค้าเพิ่มเติมสำหรับทำคอนเทนต์รวม ---\n"
                for i, pid in enumerate(chosen_pids[1:], start=2):
                    ctx = product_db.get_agent_context(pid)
                    multi_context += f"\n=== สินค้าที่ {i}: {pid} ===\n"
                    multi_context += ctx.get("text", "")[:multi_text_len]
                    multi_context += f"\n--- สิ้นสุดสินค้าที่ {i} ---\n"
                quick_brief = (quick_brief or "") + multi_context

            # ส่งแนวคิดที่เลือกเป็น quick_brief เพิ่ม
            base_auto_brief = f"แนวคิดที่ต้องใช้: {chosen_concept}"
            if quick_brief:
                base_auto_brief += f"\n\nคำขอเพิ่มเติมจาก user: {quick_brief}"

            if step_context is not None:
                step_context = (
                    step_context
                    .with_products(chosen_pids)
                    .with_quick_brief(quick_brief)
                    .with_phase("generation")
                )

            project_root = Path(__file__).resolve().parent.parent
            max_dedup_retries = int(ch_cfg.get("dedup_max_retries", 3))
            platform_names = _PLATFORM_NAMES
            target_platforms = platforms if platforms else [""]

            all_posts: list[dict[str, Any]] = []
            all_script_reviews: list[dict[str, Any]] = []
            overall_duplicate = False
            max_similarity = 0.0
            last_retry_count = 0

            for platform in target_platforms:
                platform_label = platform_names.get(platform, platform) if platform else ""
                if status_callback:
                    if platform_label:
                        status_callback(f"กำลังสร้างคอนเทนต์สำหรับ {', '.join(chosen_pids)} ({platform_label})...")
                    else:
                        status_callback(f"กำลังสร้างคอนเทนต์สำหรับ {', '.join(chosen_pids)}...")

                platform_brief = base_auto_brief
                if platform_label:
                    platform_brief += f"\nแพลตฟอร์มที่ต้องสร้าง: {platform_label} เท่านั้น"

                # --- Phase 2: สร้างคอนเทนต์ + ตรวจซ้ำ (retry ถ้าซ้ำ) ---
                content = ""
                caption_summary = ""
                platform_used = platform_label
                dup_result = {"is_duplicate": False, "similarity": 0.0, "matched_entry": None}
                retry_count = 0

                for attempt in range(max_dedup_retries + 1):
                    attempt_brief = platform_brief
                    if retry_count > 0 and dup_result.get("is_duplicate"):
                        matched = dup_result.get("matched_entry") or {}
                        matched_caption = matched.get("caption_summary", "")[:200]
                        attempt_brief = (
                            platform_brief + "\n\n"
                            f"--- คอนเทนต์ที่สร้างครั้งก่อนซ้ำกับที่เคยทำ (similarity {dup_result.get('similarity', 0):.2f}) ---\n"
                            f"คอนเทนต์เดิมที่ซ้ำ: {matched_caption}\n"
                            f"--- สิ้นสุด ---\n"
                            f"สร้างคอนเทนต์ใหม่ที่แตกต่างจากด้านบนอย่างชัดเจน — เปลี่ยนมุมมอง/concept/เนื้อหา"
                        )
                        if status_callback:
                            status_callback(f"คอนเทนต์ซ้ำ (ครั้งที่ {retry_count}) — กำลังสร้างใหม่...")

                    content_kwargs = {
                        "llm": llm,
                        "quick_brief": attempt_brief,
                        "media_type": media_type,
                        "asset_summary": asset_summary,
                        "resource_context": resource_context,
                        "extra_image_paths": extra_image_paths,
                    }
                    if step_context is not None:
                        content_kwargs["step_context"] = step_context.with_quick_brief(attempt_brief)
                    content = self.run_content_creator("", "", "", **content_kwargs)

                    # ดึง caption เพื่อตรวจซ้ำ
                    caption_summary = ""
                    platform_used = platform_label
                    try:
                        parsed = _json.loads(content)
                        posts = parsed.get("posts", [])
                        if posts:
                            first = posts[0]
                            caption_summary = first.get("caption", "")
                            if not platform_used and first.get("platform"):
                                platform_used = first.get("platform")
                    except (_json.JSONDecodeError, TypeError):
                        pass

                    # ตรวจซ้ำ
                    dup_result = content_history.check_duplicate(
                        project_root, caption_summary, config=ch_cfg,
                    )

                    if not dup_result.get("is_duplicate"):
                        break

                    if attempt < max_dedup_retries:
                        retry_count += 1

                # แจ้ง user ถ้ายังซ้ำหลัง retry หมด
                if dup_result.get("is_duplicate") and retry_count >= max_dedup_retries:
                    if status_callback:
                        status_callback(
                            f"⚠ ยังซ้ำหลังลอง {max_dedup_retries} ครั้ง — ใช้ผลงานล่าสุด (similarity {dup_result.get('similarity', 0):.2f})"
                        )

                overall_duplicate = overall_duplicate or dup_result.get("is_duplicate", False)
                max_similarity = max(max_similarity, dup_result.get("similarity", 0.0))
                last_retry_count = retry_count

                # --- Phase 3: Script Review — self-review อัตโนมัติ ---
                script_review_result: dict[str, Any] = {}
                try:
                    parsed_content = _json.loads(content)
                    posts = parsed_content.get("posts", [])
                    if posts and platform_label:
                        posts[0]["platform"] = platform_label

                    script_review_result = self._review_script_in_posts(
                        posts, platform_used, llm, status_callback, ch_cfg,
                    )

                    if posts:
                        all_posts.append(posts[0])
                    all_script_reviews.append(script_review_result)
                except Exception:
                    pass  # review พังไม่ต้อง crash pipeline

            # รวมผลลัพธ์ทุกแพลตฟอรืมเป็น JSON เดียว
            combined = {"posts": all_posts}

            # Strict JSON contract: final saved artifact must always pass CONTENT_ARTIFACT_SCHEMA
            from .output_validators import validate_content_output
            ok, err = validate_content_output(combined)
            if not ok:
                if status_callback:
                    status_callback(f"⚠ ไฟล์ content ไม่ผ่าน schema: {err}")
                raise ValueError(f"Content output validation failed: {err}")

            content = _json.dumps(combined, ensure_ascii=False, indent=2)
            try:
                from .content_schema import render_posts_to_markdown
                markdown = render_posts_to_markdown(combined)
            except Exception:
                markdown = content
            self.results["content_creator"] = content
            self.results["content_creator_markdown"] = markdown

            # บันทึก history (1 entry ต่อการสร้าง — เก็บ product_ids ทั้งหมด + pillar)
            record_platform = ", ".join(
                platform_names.get(p, p) for p in target_platforms if p
            ) or platform_used
            record_caption = "\n".join(
                p.get("caption", "") for p in all_posts
            )
            content_history.record_entry(
                project_root,
                product_ids=chosen_pids,
                concept=chosen_concept,
                pillar=chosen_pillar,
                platform=record_platform,
                caption_summary=record_caption,
                config=ch_cfg,
            )

            result = {
                "product_ids": chosen_pids,
                "product_id": chosen_pids[0],
                "pillar": chosen_pillar,
                "concept": chosen_concept,
                "reason": reason,
                "content": content,
                "markdown": markdown,
                "is_duplicate": overall_duplicate,
                "similarity": max_similarity,
                "dedup_retries": last_retry_count,
                "script_review": all_script_reviews,
            }
            if step_context is not None:
                result["step_context"] = step_context
            return result
        finally:
            if own:
                llm.close()

    # ------------------------------------------------------------------
    #  Full pipeline
    # ------------------------------------------------------------------

    def run_pipeline(self, raw_data: str, competitor_data: str | None = None) -> dict[str, str]:
        """Run all 4 agents in sequence, piping outputs forward.
        
        If competitor_data is None or empty, CompetitorAnalysisAgent will search web itself.
        """
        llm = self.make_client()
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:

                task = progress.add_task("Agent 1/4: นักวิเคราะห์สินค้า — กำลังสร้าง...", total=1)
                progress.update(task, description="Agent 1/4: นักวิเคราะห์สินค้า — กำลังตรวจงาน...")
                product_spec = self.run_product_spec(raw_data, self.product_images, llm)
                progress.update(task, description="[green]✓[/green] Agent 1/4: สเปคสินค้า + ตรวจงานเสร็จ")

                task = progress.add_task("Agent 2/4: นักวิเคราะห์คู่แข่ง — กำลังสร้าง...", total=1)
                progress.update(task, description="Agent 2/4: นักวิเคราะห์คู่แข่ง — กำลังตรวจงาน...")
                competitor_analysis = self.run_competitor_analysis(
                    product_spec, competitor_data, llm
                )
                progress.update(
                    task, description="[green]✓[/green] Agent 2/4: วิเคราะห์คู่แข่ง + ตรวจงานเสร็จ"
                )

                task = progress.add_task("Agent 3/4: นักวางกลยุทธ์แคมเปญ — กำลังสร้าง...", total=1)
                progress.update(task, description="Agent 3/4: นักวางกลยุทธ์แคมเปญ — กำลังตรวจงาน...")
                campaign_strategy = self.run_campaign_strategy(
                    product_spec, competitor_analysis, llm
                )
                progress.update(
                    task, description="[green]✓[/green] Agent 3/4: แคมเปญ + ตรวจงานเสร็จ"
                )

                task = progress.add_task("Agent 4/4: นักสร้างคอนเทนต์ — กำลังสร้าง...", total=1)
                progress.update(task, description="Agent 4/4: นักสร้างคอนเทนต์ — กำลังตรวจงาน...")
                content = self.run_content_creator(
                    product_spec, competitor_analysis, campaign_strategy, llm
                )
                progress.update(
                    task, description="[green]✓[/green] Agent 4/4: คอนเทนต์ + ตรวจงานเสร็จ"
                )

            return self.results
        finally:
            llm.close()

    # ------------------------------------------------------------------
    #  Output helpers
    # ------------------------------------------------------------------

    def save_result(self, agent_key: str, output_dir: str | Path | None = None) -> dict[str, Path]:
        """Save a single agent result to a markdown file.

        สำหรับ content_creator: ถ้า result เป็น JSON (structured output)
        → เซฟ .json (raw) + .md (markdown field สำหรับ user ดู)
        """
        if output_dir is None:
            output_dir = Path("output") / "latest"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        run_id = _make_run_id()
        pid = self.product_id or "product"

        if agent_key not in self.results:
            return {}

        fname = _AGENT_OUTPUT_NAMES.get(agent_key, agent_key)
        raw = self.results[agent_key]

        # content_creator ใช้ Structured Outputs → result เป็น JSON string
        # แยกเซฟ: .json (raw) + .md (markdown ที่เรา generate จาก posts)
        if agent_key == "content_creator":
            import json as _json
            try:
                # เซฟ .json (raw structured output — สำหรับ parse_media_prompts)
                json_path = output_dir / f"{fname}_{pid}_{run_id}.json"
                json_path.write_text(raw, encoding="utf-8")
                # เซฟ .md (markdown ที่ render_posts_to_markdown สร้าง — สำหรับ user ดู)
                md_content = self.results.get("content_creator_markdown", raw)
                md_path = output_dir / f"{fname}_{pid}_{run_id}.md"
                md_path.write_text(md_content, encoding="utf-8")
                return {agent_key: md_path, f"{agent_key}_json": json_path}
            except (_json.JSONDecodeError, TypeError):
                # fallback: ถ้า LLM ไม่คืน JSON (model ไม่รองรับ structured outputs)
                # เซฟเป็น .md ธรรมดาเหมือนเดิม
                pass

        filepath = output_dir / f"{fname}_{pid}_{run_id}.md"
        filepath.write_text(raw, encoding="utf-8")
        return {agent_key: filepath}

    def save_results(self, output_dir: str | Path | None = None) -> dict[str, Path]:
        """Save each agent result to a separate markdown file.
        
        output_dir is expected to be session-specific: output/{timestamp}/
        Filenames include product_id to support multiple products per session.
        """
        if output_dir is None:
            output_dir = Path("output") / "latest"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        run_id = _make_run_id()
        pid = self.product_id or "product"
        saved: dict[str, Path] = {}

        for key, result in self.results.items():
            fname = _AGENT_OUTPUT_NAMES.get(key, key)
            filepath = output_dir / f"{fname}_{pid}_{run_id}.md"
            filepath.write_text(result, encoding="utf-8")
            saved[key] = filepath

        return saved
