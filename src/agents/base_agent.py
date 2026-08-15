"""Base agent class — shared logic for all marketing agents.

Each agent runs in two phases:
  1. **Generate** — produce output from the user prompt
  2. **Review & Refine** — send output back to the LLM for quality check

The review step repeats up to ``max_review_iterations`` times (configurable in YAML).
Set ``max_review_iterations: 0`` to disable review entirely.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from ..llm_client import LLMClient

console = Console()


def _web_search_cfg() -> dict:
    """อ่าน web_search section จาก config — fallback {} ถ้าโหลดไม่ได้ (lazy, กัน circular import)."""
    try:
        from ..config_loader import load_config, get_section
        return get_section(load_config(), "web_search", {})
    except Exception:
        return {}


class BaseAgent:
    """Base class for all marketing team agents.

    Each subclass defines ``agent_name`` and ``display_name``.
    The actual behaviour is driven by the YAML config (system_prompt, model, etc.).
    """

    agent_name: str = ""
    display_name: str = ""

    def __init__(
        self,
        agent_config: dict[str, Any],
        llm_client: LLMClient,
        brand_context: str = "",
        brand_reference: str = "",
        instructions: dict[str, Any] | None = None,
    ) -> None:
        self.config = agent_config
        self.llm = llm_client
        # brand_context = rules (voice + terms) → ใส่ใน system prompt ทุก agent
        # brand_reference = profile + audience → ใส่เฉพาะ agent ที่ต้องการบริบทเพิ่ม
        self.brand_context = brand_context
        self.brand_reference = brand_reference
        self.instructions = instructions or {}

    def _format_instructions(self) -> str:
        """Format user-set instructions into a text block for the system prompt."""
        ins = self.instructions
        if not ins:
            return ""
        parts: list[str] = []

        # Preset label (informational)
        preset = ins.get("preset", "")
        if preset:
            parts.append(f"โหมดการทำงาน: {preset}")

        # Focus areas
        focus = ins.get("focus", [])
        if focus:
            focus_map = {
                "USP": "USP / จุดขาย",
                "differentiation": "ความแตกต่างจากตลาด",
                "sales_info": "ข้อมูลสำหรับฝ่ายขาย",
                "technical": "รายละเอียดทางเทคนิค",
                "customer_benefit": "ประโยชน์ต่อลูกค้า",
            }
            labels = [focus_map.get(f, f) for f in focus]
            parts.append("เน้นเป็นพิเศษ: " + ", ".join(labels))

        # Detail level
        detail = ins.get("detail_level", "")
        if detail:
            detail_map = {"concise": "สั้นกระชับ", "standard": "ระดับปกติ", "detailed": "ละเอียดมาก"}
            parts.append("ระดับรายละเอียด: " + detail_map.get(detail, detail))

        # Data strictness (product_spec)
        strictness = ins.get("data_strictness", "")
        if strictness:
            strict_map = {
                "strict": "เคร่งครัด — ใช้เฉพาะข้อมูลที่ให้มา ห้ามเดา",
                "moderate": "ยืดหยุ่น — สามารถอนุมานได้บ้างแต่ต้องระบุว่าเป็นการสันนิษฐาน",
                "inferential": "วิเคราะห์เชิงอนุมาน — สามารถเดาและอนุมานได้ แต่ต้องแยกชัดว่าข้อไหน fact ข้อไหน assumption",
            }
            parts.append("ความเคร่งครัดด้านข้อมูล: " + strict_map.get(strictness, strictness))

        # Competitor analysis specifics
        depth = ins.get("analysis_depth", "")
        if depth:
            depth_map = {"basic": "พื้นฐาน", "standard": "มาตรฐาน", "deep": "ลึกล้ำ (Deep Research)"}
            parts.append("ความลึกในการวิเคราะห์: " + depth_map.get(depth, depth))

        comp_types = ins.get("competitor_types", [])
        if comp_types:
            ct_map = {"direct": "คู่แข่งโดยตรง", "indirect": "คู่แข่งทางอ้อม", "premium": "แบรนด์พรีเมียม", "lowcost": "แบรนด์ราคาถูก"}
            labels = [ct_map.get(c, c) for c in comp_types]
            parts.append("คู่แข่งที่สนใจ: " + ", ".join(labels))

        importance = ins.get("importance", [])
        if importance:
            imp_map = {"price": "ราคา", "features": "คุณสมบัติ", "positioning": "Positioning", "marketing": "การตลาด", "distribution": "ช่องทางจำหน่าย"}
            labels = [imp_map.get(i, i) for i in importance]
            parts.append("สิ่งที่ให้ความสำคัญ: " + ", ".join(labels))

        analysis_style = ins.get("analysis_style", "")
        if analysis_style:
            as_map = {"objective": "แบบเป็นกลาง (Objective)", "strategic": "แบบเชิงกลยุทธ์ (Strategic)", "aggressive": "แบบก้าวร้าว (Aggressive)"}
            parts.append("สไตล์การวิเคราะห์: " + as_map.get(analysis_style, analysis_style))

        web_search = ins.get("web_search")
        if web_search is not None:
            parts.append("การค้นหา Web: " + ("เปิดใช้งาน — ค้นหาข้อมูลเพิ่มเติมได้" if web_search else "ปิด — วิเคราะห์เฉพาะข้อมูลที่มี"))

        # Campaign specifics
        objective = ins.get("campaign_objective", "")
        if objective:
            obj_map = {"sales": "เพิ่มยอดขาย", "newcustomers": "เพิ่มลูกค้าใหม่", "launch": "เปิดตัวสินค้า", "awareness": "เพิ่ม Brand Awareness", "repeat": "กระตุ้น Repeat Purchase", "clearance": "ระบาย Stock"}
            parts.append("เป้าหมายแคมเปญ: " + obj_map.get(objective, objective))

        risk = ins.get("risk_level", "")
        if risk:
            risk_map = {"safe": "ระมัดระวัง (Safe)", "balanced": "สมดุล (Balanced)", "aggressive": "กล้า (Aggressive)"}
            parts.append("ความกล้าในการทำแคมเปญ: " + risk_map.get(risk, risk))

        priority = ins.get("priority", [])
        if priority:
            pr_map = {"margin": "Profit Margin", "brand": "Brand Image", "volume": "Sales Volume", "acquisition": "Customer Acquisition"}
            labels = [pr_map.get(p, p) for p in priority]
            parts.append("สิ่งที่สำคัญที่สุด: " + ", ".join(labels))

        budget = ins.get("budget_max", "")
        if budget:
            parts.append(f"งบประมาณสูงสุด: {budget}")

        discount = ins.get("discount_max", "")
        if discount:
            parts.append(f"ส่วนลดสูงสุด: {discount}%")

        forbid_tactics = ins.get("forbid_tactics", [])
        if forbid_tactics:
            ft_map = {"bogo": "Buy 1 Get 1", "flash": "Flash Sale", "heavy_discount": "ลดราคาหนัก"}
            labels = [ft_map.get(t, t) for t in forbid_tactics]
            parts.append("ห้ามใช้: " + ", ".join(labels))

        # Content creator specifics
        tone = ins.get("tone", [])
        if tone:
            parts.append("Tone of Voice: " + ", ".join(tone))

        hook = ins.get("hook_style", "")
        if hook:
            hook_map = {"educational": "Educational", "problemsolution": "Problem → Solution", "emotional": "Emotional", "storytelling": "Storytelling", "controversial": "Controversial"}
            parts.append("สไตล์ Hook: " + hook_map.get(hook, hook))

        sell = ins.get("sell_style", "")
        if sell:
            sell_map = {"soft": "Soft Sell", "balanced": "สมดุล", "hard": "Hard Sell"}
            parts.append("การขาย: " + sell_map.get(sell, sell))

        lang = ins.get("language", "")
        if lang:
            lang_map = {"professional": "Professional", "conversational": "Conversational", "genz": "Gen Z", "expert": "Expert"}
            parts.append("ภาษา: " + lang_map.get(lang, lang))

        # Rules
        rules_must = ins.get("rules_must", [])
        if rules_must:
            parts.append("ต้อง:\n" + "\n".join(f"  ✓ {r}" for r in rules_must))

        rules_forbid = ins.get("rules_forbid", [])
        if rules_forbid:
            parts.append("ห้าม:\n" + "\n".join(f"  ✗ {r}" for r in rules_forbid))

        # Custom instruction — user override (takes precedence over defaults)
        custom = ins.get("custom", "")
        if custom:
            parts.append(f"คำสั่งจากผู้ใช้ (ถ้าขัดแย้งกับค่าเริ่มต้น ให้ทำตามคำสั่งนี้แทน):\n{custom}")

        if not parts:
            return ""
        return "--- คำแนะนำการทำงานจากผู้ใช้ ---\n" + "\n".join(parts) + "\n--- สิ้นสุดคำแนะนำการทำงาน ---"

    def _build_system_prompt(self) -> str:
        """Combine the agent's system prompt with brand rules, reference, and user instructions."""
        system_prompt = self.config.get("system_prompt", "")
        sections = [system_prompt]

        use_brand = self.config.get("use_brand_context", True)
        if self.brand_context and use_brand:
            sections.append(
                f"--- กฎของแบรนด์ (Voice + Terms — ต้องเป็นไปตามนี้) ---\n"
                f"{self.brand_context}\n"
                f"--- สิ้นสุดกฎของแบรนด์ ---\n\n"
                f"กฎของแบรนด์เป็นกฎบังคับ — โทนเสียง คำที่ใช้ คำต้องห้าม ต้องเป็นไปตามนี้\n"
                f"ห้ามนำรายการสินค้าในข้อมูลแบรนด์มาใช้เป็นสินค้าที่จะทำงานด้วย\n"
                f"สินค้าที่จะทำงานด้วยคือสินค้าที่ส่งมาใน user prompt เท่านั้น\n"
                f"ถ้าข้อมูลสินค้าใน user prompt ขัดแย้งกับข้อมูลแบรนด์ ให้เชื่อข้อมูลสินค้าใน user prompt"
            )

        # brand_reference (profile + audience) — ใส่เฉพาะ agent ที่เปิด use_brand_reference
        use_ref = self.config.get("use_brand_reference", False)
        if self.brand_reference and use_ref:
            sections.append(
                f"--- ข้อมูลแบรนด์อ้างอิง (บริบทเพิ่ม — ประวัติ + กลุ่มเป้าหมาย) ---\n"
                f"{self.brand_reference}\n"
                f"--- สิ้นสุดข้อมูลแบรนด์อ้างอิง ---"
            )

        instruction_block = self._format_instructions()
        if instruction_block:
            sections.append(instruction_block)

        return "\n\n".join(sections)

    def run(self, user_prompt: str, quick_brief: str = "", image_paths: list[str] | None = None,
            response_format: dict | None = None) -> str:
        """Generate output then review/refine it.

        Returns the final (possibly refined) text response.

        If web_search_planning is enabled in config, runs a 3-phase flow:
          Phase 0: Plan — LLM วางแผนว่าจะค้น web ว่าอะไรบ้าง
          Phase 1: Search — ค้นแยกแต่ละ query, รวมผล
          Phase 2: Generate — สร้าง output จากข้อมูลที่ค้นได้
          Phase 3: Review — ตรวจงาน
        ถ้าไม่มี web_search_planning → ทำแบบเดิม (ส่ง tools ให้ LLM ค้นเอง)

        If response_format is provided, enables OpenRouter Structured Outputs.
        Note: review/refine phase is skipped when response_format is set
        (structured output ไม่เข้ากับ review prompt ที่คาดการณ์ text ธรรมดา).

        Args:
            user_prompt: text prompt สำหรับ agent
            quick_brief: คำสั่งบังคับจาก user สำหรับรอบนี้
            image_paths: list ของ path รูปจริง — ส่งเป็น multimodal ให้ LLM vision
                         (สถาปัตยกรรมใหม่: agent เห็นรูปจริงเหมือนมนุษย์ ไม่ใช่คำบรรยาย)
        """
        system_prompt = self._build_system_prompt()

        # Append quick brief (per-run instruction) to user prompt
        # ถือว่าเป็นคำสั่งบังคับจากผู้ใช้ ไม่ใช่แค่บริบทเสริม
        if quick_brief:
            user_prompt = (
                f"{user_prompt}\n\n"
                f"--- คำสั่งบังคับจากผู้ใช้สำหรับรอบนี้ (ต้องทำตาม) ---\n"
                f"{quick_brief}\n"
                f"--- สิ้นสุดคำสั่งบังคับ ---\n"
                f"หมายเหตุ: คำสั่งข้างต้นเป็นคำสั่งจากผู้ใช้ที่ต้องทำตาม "
                f"ถ้าขัดแย้งกับค่าเริ่มต้นใน system prompt ให้ทำตามคำสั่งผู้ใช้ข้างต้น"
            )

        web_search = self.config.get("web_search")
        use_planning = self.config.get("web_search_planning", False)

        if web_search and use_planning and not response_format:
            # --- Phase 0: Plan search queries ---
            queries = self._plan_search_queries(user_prompt, system_prompt)
            # --- Phase 1: Execute searches ---
            search_results = self._execute_searches(queries)
            # --- Phase 2: Generate with search results ---
            console.print(f"\n[cyan]กำลังสร้างผลงาน... ({self.display_name})[/cyan]\n")
            enriched_prompt = self._enrich_prompt_with_search(user_prompt, search_results)
            user_content = self._build_multimodal_content(enriched_prompt, image_paths)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            output = self.llm.chat(
                messages,
                model=self.config.get("model"),
                temperature=self.config.get("temperature", 0.7),
                max_tokens=self.config.get("max_tokens", 4096),
                max_retry_limit=self.config.get("max_retry_limit", 3),
                source=f"{self.agent_name}.generate",
            )
        else:
            # --- Old flow: single call with server tool ---
            console.print(f"\n[cyan]กำลังสร้างผลงาน... ({self.display_name})[/cyan]\n")
            user_content = self._build_multimodal_content(user_prompt, image_paths)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            tools = None
            if web_search and not response_format:
                tools = [{"type": "openrouter:web_search", "max_results": int(_web_search_cfg().get("max_results", 5))}]
            output = self.llm.chat(
                messages,
                model=self.config.get("model"),
                temperature=self.config.get("temperature", 0.7),
                max_tokens=self.config.get("max_tokens", 4096),
                max_retry_limit=self.config.get("max_retry_limit", 3),
                tools=tools,
                response_format=response_format,
                source=f"{self.agent_name}.generate",
            )

        # --- Phase 3: Review & Refine (optional) ---
        # ถ้า max_review_iterations > 0 → สั่ง LLM ตรวจงานตัวเองรอบที่ 2
        # ถ้า = 0 → agent ตรวจเองในการเรียกครั้งเดียว (self-check ใน system_prompt)
        max_review = self.config.get("max_review_iterations", 1)
        if max_review and max_review > 0:
            instruction_block = self._format_instructions()
            output = self._review_and_refine(
                output, system_prompt,
                instruction_block=instruction_block,
                quick_brief=quick_brief,
                response_format=response_format,
            )

        return output

    def _build_multimodal_content(self, text: str, image_paths: list[str] | None) -> str | list[dict]:
        """สร้าง message content แบบ multimodal (text + รูปจริง) ถ้ามีรูป.

        สถาปัตยกรรมใหม่ (retrieve-then-read):
          - ถ้ามี image_paths → ส่งเป็น list: [{type: text}, {type: image_url}, ...]
            LLM vision เห็นรูปจริง (resize ลด token — ความแม่นยำใกล้เดิม)
          - ถ้าไม่มี → ส่งเป็น string (backward compatible)

        รูปใหญ่เกิน 1024px จะถูก resize ลง (preserve aspect ratio) ก่อน encode
        เพื่อลด image tokens ที่ LLM คิด (1 tile = 256x256 = ~258 tokens)
        """
        if not image_paths:
            return text

        import base64
        import io
        import mimetypes
        from pathlib import Path

        content: list[dict] = [{"type": "text", "text": text}]

        for img_path in image_paths:
            p = Path(img_path)
            if not p.exists():
                continue
            try:
                # Resize รูปใหญ่ก่อน encode — ลด image tokens 74%
                b64, mime = self._encode_image_resized(p)
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
            except OSError:
                continue

        # ถ้าไม่มีรูปที่อ่านได้ → คืน text ธรรมดา
        if len(content) == 1:
            return text

        console.print(f"[dim]ส่งรูปจริง {len(content) - 1} รูปให้ LLM vision ({self.display_name})[/dim]")
        return content

    def _encode_image_resized(self, p: Any, max_dim: int = 1024) -> tuple[str, str]:
        """Encode รูปเป็น base64 — resize ถ้าใหญ่เกิน max_dim (preserve aspect ratio).

        คืน (base64_str, mime_type)
        รูปเล็กกว่า max_dim ส่งต้นฉบับเลย (lossless)
        รูปใหญ่กว่า resize ลง (ลด image tokens ~74%)
        """
        import base64
        import io
        import mimetypes

        mime, _ = mimetypes.guess_type(str(p))
        if not mime or not mime.startswith("image/"):
            mime = "image/png"

        # อ่านขนาดรูป
        try:
            from PIL import Image
            img = Image.open(p)
            w, h = img.size
        except Exception:
            # ไม่มี PIL หรืออ่านไม่ได้ → ส่งต้นฉบับ
            return base64.b64encode(p.read_bytes()).decode("ascii"), mime

        # ถ้าเล็กอยู่แล้ว → ส่งต้นฉบับ
        if w <= max_dim and h <= max_dim:
            return base64.b64encode(p.read_bytes()).decode("ascii"), mime

        # Resize (preserve aspect ratio)
        scale = max_dim / max(w, h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        img_resized = img.resize((new_w, new_h), Image.LANCZOS)

        # Convert BMP → PNG (เล็กกว่ามาก)
        buf = io.BytesIO()
        save_format = "PNG" if mime in ("image/png", "image/bmp", "image/x-ms-bmp") else "JPEG"
        if save_format == "JPEG":
            # JPEG ไม่รองรับ alpha → convert
            if img_resized.mode in ("RGBA", "LA", "P"):
                img_resized = img_resized.convert("RGB")
            mime = "image/jpeg"
        img_resized.save(buf, format=save_format)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return b64, mime

    def _plan_search_queries(self, user_prompt: str, system_prompt: str) -> list[str]:
        """Phase 0: ให้ LLM วางแผนว่าจะค้น web ว่าอะไรบ้าง.

        ส่ง product spec + brand context + instructions ให้ LLM
        แล้วให้มันคืน list ของ search queries (JSON array)
        """
        max_queries = self.config.get("web_search_max_queries", int(_web_search_cfg().get("max_queries", 5)))
        # ส่ง instructions ของ user แยกชัด เพื่อให้ planning ใช้คำสั่ง user ในการวางแผน query
        instruction_block = self._format_instructions()
        plan_system = (
            "คุณคือนักวางแผนการค้นข้อมูล (Search Planner)\n"
            "หน้าที่: อ่านข้อมูลสินค้า บริบทแบรนด์ และคำสั่งจากผู้ใช้ "
            "แล้ววางแผนว่าควรค้น web ว่าอะไรบ้าง\n"
            f"สูงสุด {max_queries} queries แต่ละ query ต้องกระชับ ใช้ค้นจริงได้\n\n"
            "คืนเป็น JSON array ของ string เท่านั้น ไม่ต้องอธิบาย\n"
            'ตัวอย่าง: ["นาฬิกาเด็ก 4G ไทย ราคา", "Xiaomi Smart Kids Watch ราคา shopee"]'
        )
        plan_user = (
            f"--- ข้อมูลสินค้าและบริบท ---\n"
            f"{user_prompt}\n\n"
            f"--- คำสั่งหลักของ agent ---\n"
            f"{self.config.get('system_prompt', '')}\n\n"
        )
        if instruction_block:
            plan_user += (
                f"--- คำสั่งจากผู้ใช้ (สำคัญ — ใช้วางแผน query) ---\n"
                f"{instruction_block}\n\n"
            )
        plan_user += "วางแผนค้นข้อมูล — คิดว่าต้องค้นอะไรเพื่อให้ตอบคำสั่งนี้ได้ครบ"
        console.print(f"\n[yellow]กำลังวางแผนการค้นข้อมูล... ({self.display_name})[/yellow]\n")
        raw = self.llm.chat(
            [{"role": "system", "content": plan_system},
             {"role": "user", "content": plan_user}],
            model=self.config.get("model"),
            temperature=float(_web_search_cfg().get("planning_temperature", 0.2)),
            max_tokens=int(_web_search_cfg().get("planning_max_tokens", 512)),
            max_retry_limit=self.config.get("max_retry_limit", 3),
            stream=False,
            source=f"{self.agent_name}.plan_search",
        )
        # parse JSON array
        import json as _json
        try:
            # ลอง parse ตรง
            queries = _json.loads(raw.strip())
            if isinstance(queries, list):
                return [str(q) for q in queries[:max_queries]]
        except _json.JSONDecodeError:
            pass
        # ลอง extract จาก code block
        import re
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if m:
            try:
                queries = _json.loads(m.group(0))
                if isinstance(queries, list):
                    return [str(q) for q in queries[:max_queries]]
            except _json.JSONDecodeError:
                pass
        # fallback: ใช้บรรทัดเป็น query
        lines = [l.strip().strip('"').strip("'").strip("- ").strip() for l in raw.split("\n") if l.strip()]
        return lines[:max_queries] if lines else []

    def _execute_searches(self, queries: list[str]) -> str:
        """Phase 1: ค้น web แยกแต่ละ query แล้วรวมผล.

        ใช้ OpenRouter web search server tool แต่ละ query แบบ non-stream
        เพื่อให้ model ค้นแล้วสรุปผลให้ในรอบเดียว
        """
        if not queries:
            return ""
        tools = [{"type": "openrouter:web_search", "max_results": int(_web_search_cfg().get("max_results_detailed", 3))}]
        all_results: list[str] = []
        for i, q in enumerate(queries, 1):
            console.print(f"[yellow]  ค้นหา [{i}/{len(queries)}]: {q}[/yellow]")
            try:
                result = self.llm.chat(
                    [{"role": "system", "content": "คุณคือผู้ช่วยค้นข้อมูล ค้น web แล้วสรุปผลแบบกระชับ พร้อมลิงก์อ้างอิง"},
                     {"role": "user", "content": f"ค้นหา: {q}\nสรุปข้อมูลที่เกี่ยวข้อง พร้อมลิงก์ [ชื่อเว็บ](URL)"}],
                    model=self.config.get("model"),
                    temperature=float(_web_search_cfg().get("execution_temperature", 0.1)),
                    max_tokens=int(_web_search_cfg().get("execution_max_tokens", 1500)),
                    max_retry_limit=self.config.get("max_retry_limit", 3),
                    stream=False,
                    tools=tools,
                    source=f"{self.agent_name}.search",
                )
                all_results.append(f"### ผลค้นหา: {q}\n{result}")
            except Exception as e:
                console.print(f"[red]  ค้นหาล้มเหลว: {e}[/red]")
                all_results.append(f"### ผลค้นหา: {q}\n(ค้นหาล้มเหลว: {e})")
        return "\n\n".join(all_results)

    def _enrich_prompt_with_search(self, user_prompt: str, search_results: str) -> str:
        """เอาผลค้น web มาใส่ใน user prompt ก่อนส่งให้ LLM สร้าง output."""
        if not search_results:
            return user_prompt
        return (
            f"{user_prompt}\n\n"
            f"--- ข้อมูลที่ค้นหาได้จาก web ---\n"
            f"{search_results}\n"
            f"--- สิ้นสุดข้อมูลค้นหา ---\n\n"
            f"ใช้ข้อมูลสินค้า + ข้อมูลแบรนด์ + ข้อมูลที่ค้นหาได้ มาสร้างผลงานตามรูปแบบที่กำหนด"
        )

    def _review_and_refine(
        self,
        output: str,
        system_prompt: str,
        instruction_block: str = "",
        quick_brief: str = "",
        response_format: dict | None = None,
    ) -> str:
        """ตรวจงานเทียบกับ instructions เป็น checklist รายข้อ.

        Reviewer เห็น 3 ส่วนแยกกันชัดเจน:
          1. ข้อกำหนดหลัก (system_prompt — role + format)
          2. Checklist จาก user instructions (rules_must, rules_forbid, custom, ฯลฯ)
          3. คำสั่งเฉพาะรอบนี้ (quick_brief)

        วิธีทำงาน:
          - รอบที่ 1: review → ถ้าเจอปัญหา → แก้ → ส่งกลับ
          - รอบที่ 2+: review ผลงานที่แก้แล้ว → ถ้าไม่มีการเปลี่ยนแปลง → หยุด (ผ่านแล้ว)
          - ถ้ายังมีการเปลี่ยนแปลง → วนต่อจนกว่าจะครบหรือถึง max iterations

        ถ้า response_format ส่งมา → review ก็ใช้ structured outputs ด้วย
        (LLM คืน JSON ที่แก้แล้ว ไม่ใช่ plain text)

        review_model: ถ้า config ระบุ → ใช้ model ที่เก่งกว่าตอน review
        (default = model เดียวกับตอน generate)
        """
        review_prompt = self.config.get("review_prompt", "")
        review_temp = self.config.get("review_temperature", 0.2)
        review_model = self.config.get("review_model") or self.config.get("model")
        max_iterations = self.config.get("max_review_iterations", 1)

        # สร้าง checklist ส่วนที่เน้น instructions ของ user แยกจาก system_prompt
        checklist_section = ""
        if instruction_block:
            checklist_section = (
                f"\n--- CHECKLIST: คำสั่งจากผู้ใช้ที่ต้องตรวจเทียบทีละข้อ ---\n"
                f"{instruction_block}\n"
                f"--- สิ้นสุด CHECKLIST ---\n"
            )

        brief_section = ""
        if quick_brief:
            brief_section = (
                f"\n--- คำสั่งเพิ่มเติมสำหรับรอบนี้ (quick_brief) ---\n"
                f"{quick_brief}\n"
                f"--- สิ้นสุดคำสั่งเพิ่มเติม ---\n"
            )

        # คำสั่งพิเศษสำหรับ structured output — บอก LLM ว่าต้องคืน JSON ไม่ใช่ text
        json_instruction = ""
        if response_format:
            json_instruction = (
                f"\n**สำคัญ: ผลงานเป็น JSON ตาม schema — ถ้าแก้ ต้องคืน JSON ที่ตรง schema เดิม**\n"
                f"ถ้าทุกข้อผ่านแล้ว ส่ง JSON เดิมกลับมาเป๊ะๆ ห้ามเปลี่ยนแปลงอะไรเลย\n"
            )

        for i in range(max_iterations):
            review_user_msg = (
                f"--- ข้อกำหนดหลักของ agent ---\n"
                f"{system_prompt}\n"
                f"{checklist_section}"
                f"{brief_section}\n"
                f"--- ผลงานที่ต้องตรวจ ---\n"
                f"{output}\n"
                f"--- สิ้นสุดผลงาน ---\n\n"
                f"{json_instruction}"
                f"วิธีตรวจ:\n"
                f"1. อ่าน CHECKLIST ทุกข้อ แล้วเช็คว่าผลงานเป็นไปตามข้อนั้นไหม\n"
                f"2. ถ้ามีข้อใดข้อหนึ่งที่ผลงานไม่เป็นไปตาม ให้แก้ไขผลงานให้เป็นไปตามข้อนั้น\n"
                f"3. ถ้าครบถ้วนทุกข้อ ส่งผลงานเดิมกลับมาเลย ไม่ต้องเปลี่ยนแปลง\n"
                f"ส่งกลับเฉพาะผลงานฉบับสุดท้ายเท่านั้น ไม่ต้องอธิบายว่าแก้อะไร"
            )
            messages = [
                {"role": "system", "content": review_prompt},
                {"role": "user", "content": review_user_msg},
            ]
            console.print(f"\n[cyan]กำลังตรวจงาน... (รอบที่ {i+1})[/cyan]\n")
            refined = self.llm.chat(
                messages,
                model=review_model,
                temperature=review_temp,
                max_tokens=self.config.get("max_tokens", 4096),
                max_retry_limit=self.config.get("max_retry_limit", 3),
                response_format=response_format,
                source=f"{self.agent_name}.review",
            )

            # Early termination: ถ้า LLM คืนของเดิม (ไม่มีการแก้) → ผ่านแล้ว หยุด
            if refined.strip() == output.strip():
                console.print(f"[green]ตรวจงานผ่าน — ไม่มีข้อที่ต้องแก้[/green]")
                break
            output = refined

        return output

    def build_prompt(self, *args: Any, **kwargs: Any) -> str:
        """Construct the user prompt for this agent.

        Subclasses must override this.
        """
        raise NotImplementedError
