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
