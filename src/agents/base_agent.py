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
from ..output_validators import validate_output as _validate_output, _output_is_blank
from ..brand_priority import BrandRules

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
        brand_rules: BrandRules | None = None,
    ) -> None:
        self.config = agent_config
        self.llm = llm_client
        # brand_context = rules string (legacy — ใช้ถ้า brand_rules ไม่ได้ส่งมา)
        # brand_rules = BrandRules object (ใหม่ — hard/soft split, ใช้ build_priority_prompt)
        self.brand_context = brand_context
        self.brand_reference = brand_reference
        self.brand_rules = brand_rules
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

        # Evidence discipline — กฎหลักฐานสำหรับการอ้างอิง (เฉพาะ agent ทีมี evidence_policy)
        ev = ins.get("evidence_policy")
        if ev:
            parts.append("กฎหลักฐาน (Evidence Policy):")
            for claim_type, rules in ev.items():
                allowed = rules.get("allowed_sources", [])
                conf = rules.get("confidence", "")
                note = rules.get("note", "")
                line = f"  - {claim_type}: แหล่งทียอมรับ = {', '.join(allowed)}"
                if conf:
                    line += f"; confidence = {conf}"
                if note:
                    line += f"; {note}"
                parts.append(line)
            parts.append("  - ถ้าไม่พบข้อมูลที่มีหลักฐานเพียงพอ ให้ระบุ 'ไม่พบข้อมูล' แทนการเติมข้อมูลเอง")
            parts.append("  - ทุก claim ต้องระบุแหล่งที่มา + ประเภทของหลักฐาน")

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

        # Custom instruction — user override (soft style เท่านั้น ห้าม override hard rules)
        custom = ins.get("custom", "")
        if custom:
            parts.append(f"คำสั่งจากผู้ใช้ (ห้ามขัดกับกฎบังคับของแบรนด์ — ถ้าขัด ให้ทำตามกฎแบรนด์):\n{custom}")

        if not parts:
            return ""
        return "--- คำแนะนำการทำงานจากผู้ใช้ ---\n" + "\n".join(parts) + "\n--- สิ้นสุดคำแนะนำการทำงาน ---"

    def _build_system_prompt(self) -> str:
        """Combine the agent's system prompt with brand rules, reference, and user instructions."""
        system_prompt = self.config.get("system_prompt", "")
        sections = [system_prompt]

        use_brand = self.config.get("use_brand_context", True)
        priority_text = ""
        if use_brand:
            # ใช้ brand_rules (BrandRules object) ถ้ามี — hard/soft split พร้อม priority prompt
            if self.brand_rules and (self.brand_rules.hard or self.brand_rules.soft):
                from ..brand_priority import build_priority_prompt
                priority_text = build_priority_prompt(self.brand_rules, self.instructions)
                if priority_text:
                    sections.append(
                        f"{priority_text}\n\n"
                        f"ห้ามนำรายการสินค้าในข้อมูลแบรนด์มาใช้เป็นสินค้าที่จะทำงานด้วย\n"
                        f"สินค้าที่จะทำงานด้วยคือสินค้าที่ส่งมาใน user prompt เท่านั้น\n"
                        f"ถ้าข้อมูลสินค้าใน user prompt ขัดแย้งกับข้อมูลแบรนด์ ให้เชื่อข้อมูลสินค้าใน user prompt"
                    )
            elif self.brand_context:
                # Legacy fallback — brand_context เป็น string แบบเดิม
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

        # Reminder: brand context/guidelines are for internal tone only.
        # They must not be emitted, repeated, or verified in the final answer.
        if priority_text or self.brand_context or self.brand_reference:
            sections.append(
                "หมายเหตุสำหรับการเขียน final output:\n"
                "ข้อมูลแบรนด์ กฎ คำต้องห้าม และแนวทางการใช้คำศัพท์ข้างต้น\n"
                "เป็น context ภายในเท่านั้น ใช้เพื่อกำหนดโทนและคำศัพท์\n"
                "ห้ามนำข้อความเหล่านั้น คำต้องห้าม หรือการตรวจสอบคำแทนที่มาแสดงใน final output\n"
                "ห้ามเขียนประโยคแบบ 'Wait, let's check' หรือ '-> No ...' หรือขั้นตอนตรวจสอบออกมาให้ user เห็น\n"
                "ให้ apply แนวทางแบรนด์โดยไม่ต้องอธิบายหรือ verify"
            )

        return "\n\n".join(sections)

    def run(self, user_prompt: str, quick_brief: str = "", image_paths: list[str] | None = None,
            response_format: dict | None = None) -> str:
        """Generate output then review/refine it.

        Returns the final (possibly refined) text response.

        If web_search is enabled in config, runs an agentic loop with
        openrouter:web_search + openrouter:web_fetch server tools:
          - model ค้นเอง หลายรอบ (สูงสุด max_uses)
          - model อ่านหน้าเต็มเองเมื่อต้องการ
          - คืน final response พร้อม URL citations (annotations)
          - verify URL ด้วย web_fetch ตาม config verify_urls

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
                f"--- คำสั่งเพิ่มเติมจากผู้ใช้สำหรับรอบนี้ ---\n"
                f"{quick_brief}\n"
                f"--- สิ้นสุดคำสั่งเพิ่มเติม ---\n"
                f"หมายเหตุ: คำสั่งข้างต้นเป็นคำขอเพิ่มเติม — สามารถปรับ soft style ได้ "
                f"แต่ถ้าขัดแย้งกับกฎบังคับของแบรนด์ใน system prompt ให้ทำตามกฎแบรนด์เสมอ"
            )

        web_search = self.config.get("web_search")

        if web_search and not response_format:
            # --- Market parity: agentic server tool loop ---
            # ส่ง openrouter:web_search + openrouter:web_fetch ให้ model ในครั้งเดียว
            # OpenRouter จะรัน agentic loop ให้: model ค้น → อ่านผล → คิด → ปรับ query → ค้นต่อเอง
            # จนหมด budget (max_uses / max_total_results) แล้วคืน final response พร้อม citations
            console.print(f"\n[cyan]กำลังสร้างผลงาน (web search agentic)... ({self.display_name})[/cyan]\n")
            user_content = self._build_multimodal_content(user_prompt, image_paths)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            tools = self._build_web_search_tools()
            output, annotations = self.llm.chat(
                messages,
                model=self.config.get("model"),
                temperature=self.config.get("temperature", 0.7),
                max_tokens=self.config.get("max_tokens", 4096),
                max_retry_limit=self.config.get("max_retry_limit", 3),
                tools=tools,
                response_format=response_format,
                source=f"{self.agent_name}.generate",
                return_annotations=True,
            )
            # แปะ URL จริงจาก citations + verify ถ้าเปิด
            output = self._append_citations_and_verify(output, annotations)
        else:
            # --- Non-web-search flow (เช่น content_creator ที่ไม่ค้น) ---
            console.print(f"\n[cyan]กำลังสร้างผลงาน... ({self.display_name})[/cyan]\n")
            user_content = self._build_multimodal_content(user_prompt, image_paths)
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

        # ถ้า model คืน output ว่างตั้งแต่แรก ไม่ต้องซ่อม บอกผู้ใช้ชัดเจนเลย
        if _output_is_blank(output):
            raise ValueError("model คืนคำตอบว่างเปล่า — อาจเกิดจาก model ไม่รองรับ web_search tool, prompt ยาวเกินไป หรือถูกปฏิเสธ")

        # ตรวจ output ตามรูปแบบของ agent แล้วซ่อมถ้าไม่ผ่าน
        max_repair = self.config.get("max_retry_limit", 3)
        ok, error = self.validate_output(output)
        for _ in range(max_repair):
            if ok:
                break
            console.print(f"[yellow]output ไม่ผ่าน validation: {error}[/yellow]")
            repair_error = error
            repaired = self._repair_output(output, error, messages, response_format)
            if _output_is_blank(repaired):
                raise ValueError(
                    f"Agent {self.agent_name} ซ่อม output ไม่สำเร็จ: "
                    f"หลังจาก '{repair_error}' model คืน output ว่างเปล่า"
                )
            output = repaired
            ok, error = self.validate_output(output)
        if not ok:
            raise ValueError(f"Agent {self.agent_name} ตรวจ output ไม่ผ่านหลังซ่อม {max_repair} รอบ: {error}")

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




    def _verify_urls_with_fetch(self, annotations: list[dict[str, Any]]) -> str:
        """Verify URL ด้วย openrouter:web_fetch — ตรวจว่าหน้ามีเนื้อหาจริง.

        คืน string ของ URL ที่ verify ผ่าน (หน้ามีเนื้อหา) ว่างถ้าทุก URL ไม่ผ่าน.
        ถ้า web_fetch ล้มเหลว → ถือว่า URL นั้นไม่ verify (ไม่ throw).
        """
        cfg = _web_search_cfg()
        fetch_tools = [{"type": "openrouter:web_fetch"}]
        verified: list[str] = []
        for ann in annotations[:5]:  # จำกัด 5 URL ต่อ query เพื่อควบคุม cost
            url = ann.get("url")
            if not url:
                continue
            try:
                page_text = self.llm.chat(
                    [{"role": "system", "content": "คุณคือผู้อ่านหน้าเว็บ สรุปเนื้อหาสั้นๆ"},
                     {"role": "user", "content": f"อ่านหน้า: {url}\nบอกว่าหน้านี้เกี่ยวกับอะไร สั้นๆ"}],
                    model=self.config.get("model"),
                    temperature=float(cfg.get("execution_temperature", 0.1)),
                    max_tokens=int(cfg.get("execution_max_tokens", 1500)),
                    max_retry_limit=self.config.get("max_retry_limit", 3),
                    stream=False,
                    tools=fetch_tools,
                    source=f"{self.agent_name}.fetch",
                )
                if page_text and len(page_text.strip()) > 20:
                    verified.append(f"- {url} — {page_text.strip()[:100]}")
            except Exception as e:
                console.print(f"[dim]  verify URL ล้มเหลว {url}: {e}[/dim]")
        return "\n".join(verified)

    def _build_web_search_tools(self) -> list[dict[str, Any]]:
        """สร้าง tools สำหรับ agentic web search loop.

        ส่งทั้ง openrouter:web_search (ให้ model ค้นเองหลายรอบ) และ
        openrouter:web_fetch (ให้ model อ่านหน้าเต็มเอง) ในครั้งเดียว.
        ใช้ config ครบ: engine, allowed_domains, excluded_domains, max_uses,
        max_total_results, search_context_size.
        """
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
            {"type": "openrouter:web_fetch"},
        ]

    def _append_citations_and_verify(self, output: str, annotations: list[dict[str, Any]]) -> str:
        """แปะ URL จริงจาก annotations ต่อท้าย output และ verify ด้วย web_fetch.

        แปะเฉพาะ URL ที่ไม่ซ้ำ. ถ้า verify_urls เปิด → เรียก web_fetch ตรวจทีละ URL
        แล้วแปะเฉพาะ URL ที่ verify ผ่าน (มีเนื้อหาเกี่ยวข้อง).
        """
        if not annotations:
            return output
        # ถ้า model คืนเนื้อหาว่างมา แม้มี annotations ก็ตาม อย่าแปะ citations ทำให้ดูไม่ว่าง
        # ให้คืนว่างเพื่อให้ระบบ detect ว่า model คืนคำตอบว่างเปล่า
        if _output_is_blank(output):
            return output
        cfg = _web_search_cfg()
        # Per-agent override: agent config `verify_urls` takes precedence over
        # the global web_search.verify_urls setting.  This lets agents that
        # don't benefit from URL verification (e.g. campaign_strategy) opt out
        # without affecting other agents.
        verify_urls = self.config.get("verify_urls", cfg.get("verify_urls"))
        unique_urls: list[dict[str, Any]] = []
        seen: set[str] = set()
        for a in annotations:
            url = a.get("url")
            if url and url not in seen:
                seen.add(url)
                unique_urls.append(a)

        citation_section = "\n\n---\n\n**แหล่งอ้างอิงจริงจากการค้นหา:**\n"
        citation_section += "\n".join(
            f"- [{a.get('title') or a.get('url')}]({a.get('url')})"
            for a in unique_urls
        )
        output += citation_section

        if verify_urls:
            verified = self._verify_urls_with_fetch(unique_urls)
            if verified:
                output += f"\n\n**URL ที่ verify ผ่าน (หน้ามีเนื้อหาเกี่ยวข้อง):**\n{verified}"
        return output

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

        # Evidence discipline — ตรวจหลักฐานตาม evidence_policy
        evidence_section = ""
        ev = self.instructions.get("evidence_policy") if self.instructions else None
        if ev:
            evidence_section = (
                f"\n--- EVIDENCE DISCIPLINE: ตรวจหลักฐาน ---\n"
                f"ทุก claim ในผลงานต้องมีหลักฐานที match กับประเภท claim:\n"
            )
            for claim_type, rules in ev.items():
                allowed = rules.get("allowed_sources", [])
                conf = rules.get("confidence", "")
                note = rules.get("note", "")
                line = f"- {claim_type}: ใช้หลักฐานประเภท {', '.join(allowed)}"
                if conf:
                    line += f" (confidence: {conf})"
                if note:
                    line += f" — {note}"
                evidence_section += line + "\n"
            evidence_section += (
                "\nห้ามอ้าง market share / ส่วนแบ่งตลาด จาก product page / retailer โดยไม่มี market report\n"
                "ห้ามอ้าง technical specification จาก blog/review โดยไม่มี official source\n"
                "ถ้า claim ใดไม่มีหลักฐานพอ ให้เปลี่ยนเป้น 'ไม่พบข้อมูล' หรือลบ claim ออก\n"
                "--- สิ้นสุด EVIDENCE DISCIPLINE ---\n"
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
                f"{evidence_section}"
                f"{brief_section}\n"
                f"--- ผลงานที่ต้องตรวจ ---\n"
                f"{output}\n"
                f"--- สิ้นสุดผลงาน ---\n\n"
                f"{json_instruction}"
                f"วิธีตรวจ:\n"
                f"1. อ่าน CHECKLIST + EVIDENCE DISCIPLINE ทุกข้อ\n"
                f"2. ถ้ามี claim ใดไม่มีหลักฐานตาม evidence_policy หรือใช้แหล่งทีผิด ให้แก้หรือลบ\n"
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

    def validate_output(self, output: str) -> tuple[bool, str]:
        """ตรวจ output ของ agent ว่าตรงกับรูปแบบที่กำหนดไหม."""
        required = self.config.get("required_output_sections")
        return _validate_output(self.agent_name, output, required)

    def _repair_output(
        self,
        output: str,
        error: str,
        messages: list,
        response_format: dict | None = None,
    ) -> str:
        """ให้ LLM แก้ output ที่ไม่ผ่าน validation โดยไม่เปลี่ยนเนื้อหา."""
        prompt = self.config.get(
            "repair_prompt",
            "ผลงานข้างต้นไม่ตรงตามรูปแบบที่กำหนด: {error}\nกรุณาแก้ไขให้ตรงรูปแบบโดยไม่เปลี่ยนเนื้อหา ส่งเฉพาะผลงานฉบับสุดท้ายเท่านั้น",
        )
        repair_messages = messages + [
            {"role": "assistant", "content": output},
            {"role": "user", "content": prompt.format(error=error)},
        ]
        return self.llm.chat(
            repair_messages,
            model=self.config.get("model"),
            temperature=self.config.get("temperature", 0.7),
            max_tokens=self.config.get("max_tokens", 4096),
            max_retry_limit=self.config.get("max_retry_limit", 3),
            response_format=response_format,
            source=f"{self.agent_name}.repair",
        )

    def build_prompt(self, *args: Any, **kwargs: Any) -> str:
        """Construct the user prompt for this agent.

        Subclasses must override this.
        """
        raise NotImplementedError
