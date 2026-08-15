"""Orchestrator — coordinates the 4-agent pipeline.

Pipeline flow:
  1. Product Spec      (raw data → product spec)
  2. Competitor Analysis (product spec + competitor data → analysis)
  3. Campaign Strategy   (product spec + analysis → campaign)
  4. Content Creator     (product spec + analysis + campaign → content)

Each step can also run independently.
"""

from __future__ import annotations

from datetime import datetime
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
from .brand_loader import load_brand_context
from .config_loader import get_agent_config, load_config
from .data_loader import detect_data_files, get_agent_data
from .llm_client import LLMClient

# Platform display names — ใช้ในหลายที่ นิยามครั้งเดียว
_PLATFORM_NAMES = {"facebook": "Facebook", "tiktok": "TikTok"}
from . import product_db

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
        self.brand_context = load_brand_context(brand_dir)
        self.product_images = product_images or []
        self.product_id = product_id
        self.results: dict[str, str] = {}

    def _make_client(self) -> LLMClient:
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
        return agent_cls(cfg, llm, brand_context=self.brand_context, instructions=instructions)

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
            llm = self._make_client()
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
        quick_brief: str = "",
    ) -> str:
        """Run product_spec agent — สร้างเอกสารสเปคสินค้าเป็น deliverable สำหรับ user.

        สิ่งนี้ไม่ใช่ data source ของ agent ตัวอื่น — agent การตลาดดึงข้อมูลจาก product DB
        product_spec agent ทำหน้าที่แปลงข้อมูลดิบ → เอกสารสเปคภาษาไทยให้ user เท่านั้น
        """
        own = llm is None
        if own:
            llm = self._make_client()
        try:
            agent = self._make_agent("product_spec", ProductSpecAgent, llm)
            prompt = agent.build_prompt(raw_data, product_images or self.product_images)
            # ส่งรูปจริงให้ agent (retrieve-then-read — agent เห็นรูปเหมือนมนุษย์)
            image_paths = self._get_product_image_paths() if self.product_id else (product_images or [])
            result = agent.run(prompt, quick_brief=quick_brief, image_paths=image_paths)
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
        quick_brief: str = "",
    ) -> str:
        own = llm is None
        if own:
            llm = self._make_client()
        try:
            agent = self._make_agent("competitor_analysis", CompetitorAnalysisAgent, llm)
            # ดึงข้อมูลสินค้าจาก DB ถ้ามี ไม่งั้นใช้ parameter (backward compatible)
            product_data = self._get_product_data(product_spec)
            # If competitor_data is None or empty, agent will search web itself
            prompt = agent.build_prompt(product_data, competitor_data or "")
            image_paths = self._get_product_image_paths()
            result = agent.run(prompt, quick_brief=quick_brief, image_paths=image_paths)
            self.results["competitor_analysis"] = result

            return result
        finally:
            if own:
                llm.close()

    def run_campaign_strategy(
        self, product_spec: str, competitor_analysis: str, llm: LLMClient | None = None,
        quick_brief: str = "",
    ) -> str:
        own = llm is None
        if own:
            llm = self._make_client()
        try:
            agent = self._make_agent("campaign_strategy", CampaignStrategyAgent, llm)
            # ดึงข้อมูลสินค้าจาก DB ถ้ามี ไม่งั้นใช้ parameter (backward compatible)
            product_data = self._get_product_data(product_spec)
            prompt = agent.build_prompt(product_data, competitor_analysis)
            image_paths = self._get_product_image_paths()
            result = agent.run(prompt, quick_brief=quick_brief, image_paths=image_paths)
            self.results["campaign_strategy"] = result
            return result
        finally:
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
    ) -> str:
        own = llm is None
        if own:
            llm = self._make_client()
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

            prompt = agent.build_prompt(
                product_data, competitor_analysis, campaign_strategy,
                media_capabilities=media_caps_text,
                media_type=media_type,
            )
            image_paths = self._get_product_image_paths()
            # ใช้ Structured Outputs — LLM คืน JSON ที่ตรง schema
            # แทนการ parse markdown ด้วย regex (ที่พังทุกครั้งที่ format เปลี่ยน)
            # LLM คืนแค่ posts (structured data) — เรา generate markdown เอง
            from .content_schema import CONTENT_RESPONSE_FORMAT, render_posts_to_markdown
            import json as _json_cc
            raw_result = agent.run(
                prompt, quick_brief=quick_brief, image_paths=image_paths,
                response_format=CONTENT_RESPONSE_FORMAT,
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

    # ------------------------------------------------------------------
    #  Auto mode — agent เลือกสินค้าเอง + คอนเทนต์ไม่ซ้ำ
    # ------------------------------------------------------------------

    def select_product_auto(
        self,
        llm: LLMClient | None = None,
        quick_brief: str = "",
        platforms: list[str] | None = None,
        product_count: int = 1,
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
        from . import content_history
        from . import pillar_manager
        from .config_loader import get_section

        own = llm is None
        if own:
            llm = self._make_client()
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
                return {"error": "ไม่มีสินค้าที่พร้อมในระบบ — กรุณาอัปโหลดและ ingest สินค้าก่อน"}

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
            ]

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
                    result.append({
                        "product_id": pid,
                        "summary": summary[:summary_len],
                        "category": meta.get("category", ""),
                        "image_count": meta.get("image_count", 0),
                    })
                return result

            def _get_product_detail(product_id: str) -> dict:
                if not product_db.is_ready(product_id):
                    return {"error": f"สินค้า {product_id} ไม่พร้อมหรือไม่มีในระบบ"}
                ctx = product_db.get_agent_context(product_id)
                return {
                    "product_id": product_id,
                    "text_context": ctx.get("text", "")[:detail_text_len],
                    "image_count": len(ctx.get("image_paths", [])),
                }

            def _get_content_history(product_id: str = "") -> list[dict]:
                if product_id:
                    return content_history.get_entries_for_product(
                        project_root, product_id, config=ch_cfg,
                    )
                return content_history.get_recent_entries(
                    project_root, config=ch_cfg,
                )

            tool_handlers = {
                "list_products": _list_products,
                "get_product_detail": _get_product_detail,
                "get_content_history": _get_content_history,
            }

            # --- prompt สำหรับ LLM (อ่านจาก config) ---
            platform_str = ""
            if platforms:
                platform_names = _PLATFORM_NAMES
                selected = [platform_names.get(p, p) for p in platforms]
                platform_str = f"\nแพลตฟอร์มที่ต้องสร้าง: {' หรือ '.join(selected)}"

            brief_section = ""
            if quick_brief:
                brief_section = f"\n\nคำขอเพิ่มเติมจาก user: {quick_brief}"

            # Output schema — ใช้ product_ids เสมอ (รองรับทั้ง 1 และหลายชิ้น)
            count_hint = ""
            if product_count > 1:
                count_hint = f"\n\nUser ต้องการให้เลือก {product_count} สินค้ามาทำคอนเทนต์รวมกันใน 1 โพสต์"
            else:
                count_hint = "\n\nUser เลือกโหมดแยก — เลือกสินค้า 1 ชิ้นเท่านั้น"
            output_schema = '{"product_ids": ["สินค้า1", ...], "pillar": "หมวดคอนเทนต์", "concept": "แนวคิด", "reason": "เหตุผล"}'

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
                f"เลือกสินค้าและแนวคิดเพื่อสร้างคอนเทนต์{platform_str}{brief_section}\n\n"
                f"ขั้นตอน: เรียก list_products() → เรียก get_content_history() → "
                f"เรียก get_product_detail() สำหรับสินค้าที่สนใจ → ตอบ JSON\n\n"
                f"สำคัญ: ต้องเลือก Content Pillar จาก list ใน system prompt "
                f"และส่ง field \"pillar\" ใน JSON ด้วย\n"
                f"สำคัญ: เลือก pillar ที่เหมาะสมกับจำนวนสินค้าที่เลือก "
                f"(เช่น ถ้าเลือกสินค้า 1 ชิ้น ห้ามเลือก pillar ที่ต้องเปรียบเทียบหลายชิ้น)"
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
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
            text = response.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)
            try:
                result = _json.loads(text)
            except _json.JSONDecodeError:
                err_len = auto_cfg.get("raw_text_preview_length", 500)
                return {"error": f"LLM ไม่คืน JSON ที่ถูกต้อง: {response[:err_len]}"}

            # รองรับทั้ง product_id (1 ชิ้น) และ product_ids (หลายชิ้น)
            if "product_id" in result and "product_ids" not in result:
                result["product_ids"] = [result["product_id"]]
            product_ids = result.get("product_ids", [])
            if not product_ids:
                return {"error": "LLM ไม่ได้เลือกสินค้า"}

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
                        return {"error": f"LLM เลือกสินค้าที่ไม่มีในระบบ: {pid}"}
            result["product_ids"] = validated
            result["product_id"] = validated[0]

            # ถ้า LLM ไม่ส่ง pillar กลับมา → infer จาก concept
            if not result.get("pillar") and pillars:
                pillar_keywords = self.config.get("pillar_keywords", {})
                result["pillar"] = pillar_manager.infer_pillar(
                    result.get("concept", ""), pillars, pillar_keywords,
                )

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
            llm = self._make_client()
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
            )
            if "error" in selection:
                return selection

            chosen_pids = selection.get("product_ids", [])
            chosen_concept = selection.get("concept") or selection.get("angle", "")
            chosen_pillar = selection.get("pillar", "")
            reason = selection.get("reason", "")

            if status_callback:
                status_callback(f"เลือก: {', '.join(chosen_pids)} — {chosen_concept}")

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
            auto_brief = f"แนวคิดที่ต้องใช้: {chosen_concept}"
            if quick_brief:
                auto_brief += f"\n\nคำขอเพิ่มเติมจาก user: {quick_brief}"
            if platforms:
                platform_names = _PLATFORM_NAMES
                selected = [platform_names.get(p, p) for p in platforms]
                if len(selected) == 1:
                    auto_brief += f"\nแพลตฟอร์มที่ต้องสร้าง: {selected[0]} เท่านั้น"
                else:
                    auto_brief += f"\nแพลตฟอร์มที่เลือก: {' หรือ '.join(selected)}"

            if status_callback:
                status_callback(f"กำลังสร้างคอนเทนต์สำหรับ {', '.join(chosen_pids)}...")

            # --- Phase 2: สร้างคอนเทนต์ + ตรวจซ้ำ (retry ถ้าซ้ำ) ---
            project_root = Path(__file__).resolve().parent.parent
            max_dedup_retries = int(ch_cfg.get("dedup_max_retries", 3))
            content = ""
            caption_summary = ""
            platform_used = ""
            dup_result = {"is_duplicate": False, "similarity": 0.0, "matched_entry": None}
            retry_count = 0

            for attempt in range(max_dedup_retries + 1):
                # เพิ่ม feedback ให้ LLM รู้ว่าซ้ำ (รอบต่อๆ ไป)
                attempt_brief = auto_brief
                if retry_count > 0 and dup_result.get("is_duplicate"):
                    matched = dup_result.get("matched_entry") or {}
                    matched_caption = matched.get("caption_summary", "")[:200]
                    attempt_brief = (
                        auto_brief + "\n\n"
                        f"--- คอนเทนต์ที่สร้างครั้งก่อนซ้ำกับที่เคยทำ (similarity {dup_result.get('similarity', 0):.2f}) ---\n"
                        f"คอนเทนต์เดิมที่ซ้ำ: {matched_caption}\n"
                        f"--- สิ้นสุด ---\n"
                        f"สร้างคอนเทนต์ใหม่ที่แตกต่างจากด้านบนอย่างชัดเจน — เปลี่ยนมุมมอง/concept/เนื้อหา"
                    )
                    if status_callback:
                        status_callback(f"คอนเทนต์ซ้ำ (ครั้งที่ {retry_count}) — กำลังสร้างใหม่...")

                content = self.run_content_creator(
                    "", "", "",
                    llm=llm, quick_brief=attempt_brief,
                    media_type=media_type,
                )

                # ดึง caption เพื่อตรวจซ้ำ
                caption_summary = ""
                platform_used = ""
                try:
                    parsed = _json.loads(content)
                    posts = parsed.get("posts", [])
                    if posts:
                        first = posts[0]
                        caption_summary = first.get("caption", "")
                        platform_used = first.get("platform", "")
                except (_json.JSONDecodeError, TypeError):
                    pass

                # ตรวจซ้ำ
                dup_result = content_history.check_duplicate(
                    project_root, caption_summary, config=ch_cfg,
                )

                if not dup_result.get("is_duplicate"):
                    break  # ไม่ซ้ำ → ใช้ผลงานนี้

                if attempt < max_dedup_retries:
                    retry_count += 1

            # แจ้ง user ถ้ายังซ้ำหลัง retry หมด
            if dup_result.get("is_duplicate") and retry_count >= max_dedup_retries:
                if status_callback:
                    status_callback(
                        f"⚠ ยังซ้ำหลังลอง {max_dedup_retries} ครั้ง — ใช้ผลงานล่าสุด (similarity {dup_result.get('similarity', 0):.2f})"
                    )

            # บันทึก history (1 entry ต่อการสร้าง — เก็บ product_ids ทั้งหมด + pillar)
            content_history.record_entry(
                project_root,
                product_ids=chosen_pids,
                concept=chosen_concept,
                pillar=chosen_pillar,
                platform=platform_used,
                caption_summary=caption_summary,
                config=ch_cfg,
            )

            markdown = self.results.get("content_creator_markdown", content)

            return {
                "product_ids": chosen_pids,
                "product_id": chosen_pids[0],
                "pillar": chosen_pillar,
                "concept": chosen_concept,
                "reason": reason,
                "content": content,
                "markdown": markdown,
                "is_duplicate": dup_result.get("is_duplicate", False),
                "similarity": dup_result.get("similarity", 0.0),
                "dedup_retries": retry_count,
            }
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
        llm = self._make_client()
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

        timestamp = datetime.now().strftime("%H%M%S")
        pid = self.product_id or "product"

        filenames = {
            "product_spec": "01_product_spec",
            "competitor_analysis": "02_competitor_analysis",
            "campaign_strategy": "03_campaign_strategy",
            "content_creator": "04_content_creator",
        }

        if agent_key not in self.results:
            return {}

        fname = filenames.get(agent_key, agent_key)
        raw = self.results[agent_key]

        # content_creator ใช้ Structured Outputs → result เป็น JSON string
        # แยกเซฟ: .json (raw) + .md (markdown ที่เรา generate จาก posts)
        if agent_key == "content_creator":
            import json as _json
            try:
                # เซฟ .json (raw structured output — สำหรับ parse_media_prompts)
                json_path = output_dir / f"{fname}_{pid}_{timestamp}.json"
                json_path.write_text(raw, encoding="utf-8")
                # เซฟ .md (markdown ที่ render_posts_to_markdown สร้าง — สำหรับ user ดู)
                md_content = self.results.get("content_creator_markdown", raw)
                md_path = output_dir / f"{fname}_{pid}_{timestamp}.md"
                md_path.write_text(md_content, encoding="utf-8")
                return {agent_key: md_path, f"{agent_key}_json": json_path}
            except (_json.JSONDecodeError, TypeError):
                # fallback: ถ้า LLM ไม่คืน JSON (model ไม่รองรับ structured outputs)
                # เซฟเป็น .md ธรรมดาเหมือนเดิม
                pass

        filepath = output_dir / f"{fname}_{pid}_{timestamp}.md"
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

        timestamp = datetime.now().strftime("%H%M%S")
        pid = self.product_id or "product"
        saved: dict[str, Path] = {}

        filenames = {
            "product_spec": "01_product_spec",
            "competitor_analysis": "02_competitor_analysis",
            "campaign_strategy": "03_campaign_strategy",
            "content_creator": "04_content_creator",
        }

        for key, result in self.results.items():
            fname = filenames.get(key, key)
            filepath = output_dir / f"{fname}_{pid}_{timestamp}.md"
            filepath.write_text(result, encoding="utf-8")
            saved[key] = filepath

        return saved
