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
from ..output_validators import (
    validate_output as _validate_output,
    _output_is_blank,
    apply_brand_replacements,
)
from ..brand_priority import BrandRules
from ..run_context import StepRunContext, build_multimodal_content

console = Console()


# ---------------------------------------------------------------------------
# M6 repair classification — single mechanism, not per-case flags.
#
# Error categories are extracted from the error prefix (e.g. "one-page:..."
# → category "one-page"). Each category has a repair budget and a
# soft-accept policy. When budget is exhausted:
#   - soft-accept categories (one-page) → break the loop (accept output)
#   - hard-rule categories (brand-hard) → raise ValueError (never soft-accept)
#   - other categories → raise ValueError (existing behavior)
#
# one-page is an instruction-following/compactness condition with tolerance.
# brand-hard is an explicit deterministic contract from terms.json/voice.json
# — a remaining violation after the single repair attempt must NOT be
# delivered to the user.
# ---------------------------------------------------------------------------

_SOFT_ACCEPT_CATEGORIES = frozenset({"one-page", "grounding"})


def _error_category(error: str) -> str:
    """Extract category from error prefix (before ':')."""
    if ":" in error:
        return error.split(":", 1)[0].strip()
    return "format"


def _repair_budget(category: str, max_repair: int) -> int:
    """Repair budget per error category.

    one-page: 1 repair then soft-accept (instruction-following tolerance).
    brand-hard: 1 repair then raise (deterministic hard contract, no tolerance).
    format/other: max_repair then raise (existing behavior).
    """
    if category in ("one-page", "brand-hard", "grounding"):
        return 1
    return max_repair


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
        # Runtime multi-brand contract (brand_dir from StepRunContext in run()).
        self.brand_dir = ""

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

    def _core_system_prompt(self) -> str:
        """Return the agent's core system prompt.

        Subclasses may override this to provide an alternative core prompt
        (e.g. evidence mode) while still receiving all shared additions
        (brand priority, brand reference, instructions, grounding policy)
        from ``_build_system_prompt``.
        """
        return self.config.get("system_prompt", "")

    def _include_brand_reference_in_prompt(self) -> bool:
        """Whether ``_build_system_prompt`` should inject ``brand_reference``.

        Default: True (injected when ``use_brand_reference`` config is true).
        Evidence mode overrides this to return False so that Stage A
        evidence research stays brand-objective — brand interpretation
        is applied in a separate ``BrandInterpretationPass`` that runs
        AFTER evidence is finalized, never during research.
        """
        return True

    def _build_system_prompt(self) -> str:
        """Combine the agent's system prompt with brand rules, reference, and user instructions."""
        sections = [self._core_system_prompt()]

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
        # AND the agent has not opted out of brand_reference injection
        # (evidence mode opts out — brand interpretation happens in a
        # separate BrandInterpretationPass after evidence is finalized).
        # use_brand_differentiator (Item 4): reframe as task-decision context
        # (active) instead of passive background. The flag is a modifier on
        # use_brand_reference — it only takes effect when use_brand_reference
        # is also true. Hard restrictions/replacements stay deterministic via
        # BrandRules; positioning is NOT merged into BrandRules.
        use_ref = self.config.get("use_brand_reference", False)
        use_diff = self.config.get("use_brand_differentiator", False)
        if self.brand_reference and use_ref and self._include_brand_reference_in_prompt():
            if use_diff:
                sections.append(
                    f"--- ข้อมูลแบรนด์อ้างอิง (ใช้เป็นบริบทตัดสินใจ — audience + positioning) ---\n"
                    f"{self.brand_reference}\n"
                    f"--- สิ้นสุดข้อมูลแบรนด์อ้างอิง ---\n"
                    f"ใช้ข้อมูลกลุ่มเป้าหมายและตำแหน่งข้างต้นเพื่อตัดสินใจเรื่อง "
                    f"audience targeting, positioning, และ differentiators "
                    f"อย่างมีนัยสำคัญ — ไม่ใช่แค่บริบทเบาๆ\n"
                    f"ปรับ message tone, channel selection, และ strategic emphasis "
                    f"ตามข้อมูลนี้ แต่ห้ามเดาหรือสร้างข้อเท็จจริงที่ไม่มีในข้อมูลดิบ\n"
                    f"ห้ามนำคำศัพท์เฉพาะแบรนด์มาเป็น required vocabulary — "
                    f"ใช้ข้อมูล semantically เพื่อตัดสินใจ ไม่ใช่เป็น checklist คำ"
                )
            else:
                sections.append(
                    f"--- ข้อมูลแบรนด์อ้างอิง (บริบทเพิ่ม — ประวัติ + กลุ่มเป้าหมาย) ---\n"
                    f"{self.brand_reference}\n"
                    f"--- สิ้นสุดข้อมูลแบรนด์อ้างอิง ---"
                )

        instruction_block = self._format_instructions()
        if instruction_block:
            sections.append(instruction_block)

        # Grounding policy (Item 3) — shared three-category contract injected
        # from config. The model uses this to distinguish supplied facts,
        # researched facts with evidence, and inference/recommendation.
        # Code enforces only mechanically-knowable citation provenance.
        grounding_policy = self.config.get("grounding_policy")
        if grounding_policy:
            sections.append(self._render_grounding_policy(grounding_policy))

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

    @staticmethod
    def _render_grounding_policy(policy: dict[str, Any]) -> str:
        """Render the shared three-category grounding policy block.

        The block tells the model to distinguish three categories of
        information and to label inference/recommendation accordingly.
        The code enforces only mechanically-knowable citation provenance
        (URL membership in the allowed set); semantic grounding review
        remains the model's responsibility.

        Config shape (agents.yaml):
            grounding_policy:
              categories: [supplied_fact, researched_fact_with_evidence, inference_or_recommendation]

        The categories list is the source of truth — the block is rendered
        from it so no hardcoded category vocabulary lives in code.
        """
        categories = policy.get("categories") or []
        if not categories:
            return ""
        lines = [
            "--- นโยบายข้อมูลต้นทาง (Grounding Policy) ---",
            "ข้อมูลที่ใช้ใน output แบ่งเป็นสามประเภท:",
        ]
        # Map config category names to descriptive Thai labels generically.
        # No hardcoded vocabulary beyond the three accepted categories.
        category_labels = {
            "supplied_fact": "ข้อมูลที่ให้มา (supplied facts) — จาก user prompt หรือข้อมูลดิบ",
            "researched_fact_with_evidence": "ข้อมูลที่ค้นคว้าพร้อมหลักฐาน (researched facts with evidence) — ต้องมี citation URL ที่ได้จาก web search",
            "inference_or_recommendation": "การอนุมานหรือข้อเสนอแนะ (inference/recommendation) — ต้องติดป้ายชัดเจน ห้ามแสดงเป็นข้อเท็จจริง",
        }
        for cat in categories:
            label = category_labels.get(cat, cat)
            lines.append(f"- {label}")
        lines.extend([
            "ห้ามสร้างข้อเท็จจริงที่ไม่มีในข้อมูลที่ให้มาหรือไม่ได้ค้นคว้าพร้อมหลักฐาน",
            "ถ้าไม่มีข้อมูล ให้ระบุว่า 'ไม่มีข้อมูลระบุ'",
            "--- สิ้นสุดนโยบายข้อมูลต้นทาง ---",
        ])
        return "\n".join(lines)

    def _normalize_quick_brief(self, quick_brief: str) -> str:
        """Hook for agent-specific quick_brief normalization.

        Base implementation returns quick_brief unchanged.
        Subclasses (e.g. ProductSpecAgent) can override to translate
        format instructions like 'one-page' into concrete targets.
        """
        return quick_brief

    def run(self, user_prompt: str, quick_brief: str = "", image_paths: list[str] | None = None,
            extra_image_paths: list[str] | None = None,
            resource_context: str = "", response_format: dict | None = None,
            step_context: "StepRunContext" | None = None,
            provider: dict | None = None,
            research_required: bool = False) -> str:
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

        # Reset per-run web evidence so stale annotations from a previous run cannot
        # pass a research_required gate on a new run.
        self._last_annotations = []
        if hasattr(self, "_last_relevant_annotations"):
            self._last_relevant_annotations = []
        if hasattr(self, "_selected_evidence"):
            self._selected_evidence = []
        if hasattr(self, "_selected_evidence_urls"):
            self._selected_evidence_urls = set()

        if extra_image_paths is None:
            extra_image_paths = []
        if image_paths is None:
            image_paths = []

        # Use StepRunContext as the single source of truth when provided.
        if step_context is not None:
            # Fatal resource preflight — if the user explicitly referenced an
            # attachment that cannot provide usable context (rejected, parser
            # error, missing, etc.), halt before any prompt construction or
            # model call. This is the single shared seam that guarantees no
            # agent execution can silently proceed without a required resource.
            if step_context.warnings:
                raise ValueError(
                    f"resource preflight failed: {'; '.join(step_context.warnings)}"
                )
            quick_brief = step_context.quick_brief
            resource_context = step_context.resource_text
            extra_image_paths = list(step_context.resource_image_paths)
            # Runtime multi-brand contract
            self.brand_dir = step_context.brand_dir

        all_image_paths = image_paths + extra_image_paths

        # Append resource context as untrusted user-provided documents
        if resource_context:
            user_prompt = f"{user_prompt}\n\n{resource_context}"

        # Normalize quick_brief (agent-specific, e.g. one-page → concrete target)
        quick_brief = self._normalize_quick_brief(quick_brief)

        # Append quick brief (per-run instruction) to user prompt
        # ถือว่าเป็นคำสั่งบังคับจากผู้ใช้ ไม่ใช่แค่บริบทเสริม
        if quick_brief:
            user_prompt = (
                f"{user_prompt}\n\n"
                f"--- คำสั่งเฉพาะรอบนี้จากผู้ใช้ (quick_brief) ---\n"
                f"{quick_brief}\n"
                f"--- สิ้นสุดคำสั่งเฉพาะรอบนี้ ---\n"
                f"หมายเหตุ: คำสั่งข้างต้นใช้เพื่อ steer รูปแบบ ระดับรายละเอียด กลุ่มผู้อ่าน และหัวข้อที่เน้นเท่านั้น\n"
                f"ห้ามสร้างหรืออนุมานข้อเท็จจริงนอก \"ข้อมูลต้นทางที่ส่งมาใน user prompt\"\n"
                f"ห้ามขัดแย้งกับ system guardrails, hard brand rules และข้อมูลต้นทาง\n"
                f"quick_brief สามารถขอรูปแบบ output / deliverable format อื่นได้ โดยไม่ขัด guardrails และข้อมูลต้นทาง\n"
                f"ถ้าขัดแย้ง ให้ system guardrails และข้อมูลต้นทางใน user prompt ชนะเสมอ"
            )

        if research_required:
            user_prompt += (
                "\n\n--- คำสั่นเฉพาะรอบนี้: ต้องใช้ web search ---\n"
                "รอบนี้ต้องการข้อมูลปัจจุบัน กรุณาใช้ web_search tool ก่อนสร้าง final answer "
                "และอ้างอิงแหล่งทางการใน output\n"
                "ถ้าไม่ค้นหาเว็บจริง ระบบจะปฏิเสธคำตอบ"
            )

        web_search = self.config.get("web_search")

        if web_search:
            # --- Market parity: agentic server tool loop ---
            # ส่ง openrouter:web_search + openrouter:web_fetch ให้ model ในครั้งเดียว
            # OpenRouter จะรัน agentic loop ให้: model ค้น → อ่านผล → คิด → ปรับ query → ค้นต่อเอง
            # จนหมด budget (max_uses / max_total_results) แล้วคืน final response พร้อม citations
            console.print(f"\n[cyan]กำลังสร้างผลงาน (web search agentic)... ({self.display_name})[/cyan]\n")
            user_content = self._build_multimodal_content(user_prompt, all_image_paths)
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
                provider=provider,
                source=f"{self.agent_name}.generate",
                return_annotations=True,
            )
            # Preserve raw web-search evidence for downstream review.
            self._last_annotations = list(annotations or [])
            # แปะ URL จริงจาก citations + verify ถ้าเปิด
            output = self._append_citations_and_verify(output, annotations)
        else:
            # --- Non-web-search flow (เช่น content_creator ที่ไม่ค้น) ---
            console.print(f"\n[cyan]กำลังสร้างผลงาน... ({self.display_name})[/cyan]\n")
            user_content = self._build_multimodal_content(user_prompt, all_image_paths)
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
                stream=self.config.get("stream", True),
                response_format=response_format,
                provider=provider,
                source=f"{self.agent_name}.generate",
            )

        # --- Phase 2: Blank check — ตรวจ generate ว่างก่อนเรียก reviewer ---
        # ถ้า generate คืนว่าง → ไม่เรียก reviewer เลย เพราะ reviewer ไม่มี source
        # จะสร้างแบบฟอร์ม "ไม่มีข้อมูล" ปลอมแทน (เห็นใน Flow 2: K69+K72)
        if _output_is_blank(output):
            raise ValueError("model คืนคำตอบว่างเปล่า — อาจเกิดจาก model ไม่รองรับ web_search tool, prompt ยาวเกินไป หรือถูกปฏิเสธ")

        # เก็บ draft ก่อน review — ถ้า reviewer ล้มเหลว จะได้คืน draft ไม่ใช่ว่าง
        draft = output

        # Research-required run: ต้องมีหลักฐาน web search จริงจากการค้นหา
        if research_required:
            if not self.config.get("web_search"):
                raise ValueError(f"Agent {self.agent_name} ถูกเรียกด้วย research_required แต่ไม่ได้เปิด web_search ใน config")
            if not getattr(self, "_last_annotations", []):
                raise ValueError("research_required: ต้องมีการค้นหาเว็บจริงและพบแหล่งอ้างอิง")

        # --- Phase 3: Review & Refine (optional) ---
        # ถ้า max_review_iterations > 0 → สั่ง LLM ตรวจงานตัวเองรอบที่ 2
        # ถ้า = 0 → agent ตรวจเองในการเรียกครั้งเดียว (self-check ใน system_prompt)
        max_review = self.config.get("max_review_iterations", 1)
        if max_review and max_review > 0:
            instruction_block = self._format_instructions()
            try:
                reviewed = self._review_and_refine(
                    output, system_prompt,
                    instruction_block=instruction_block,
                    quick_brief=quick_brief,
                    response_format=response_format,
                    user_prompt=user_prompt,
                    image_paths=all_image_paths,
                )
            except Exception as review_exc:
                # reviewer ล้มเหลว (timeout, exception) → คืน draft พร้อม warning
                console.print(f"[yellow]reviewer ล้มเหลว: {review_exc} — คืน draft พร้อม warning[/yellow]")
                output = (
                    f"⚠️ **การตรวจทานอัตโนมัติล้มเหลว** — {review_exc}\n"
                    f"ผลงานด้านล่างเป็น draft จาก generator กรุณาตรวจสอบด้วยตนเองก่อนใช้งาน\n"
                    f"---\n\n"
                    f"{draft}"
                )
            else:
                # reviewer คืนว่าง → คืน draft พร้อม warning (ไม่ใช่ว่าง)
                if _output_is_blank(reviewed):
                    console.print("[yellow]reviewer คืนคำตอบว่าง — คืน draft พร้อม warning[/yellow]")
                    output = (
                        f"⚠️ **การตรวจทานอัตโนมัติล้มเหลว** — reviewer คืนคำตอบว่างเปล่า "
                        f"(อาจเป็น timeout, rate limit หรือ model ปฏิเสธ)\n"
                        f"ผลงานด้านล่างเป็น draft จาก generator กรุณาตรวจสอบด้วยตนเองก่อนใช้งาน\n"
                        f"---\n\n"
                        f"{draft}"
                    )
                else:
                    output = reviewed

        # --- M6: Brand hard-rule deterministic auto-replacement (zero LLM cost) ---
        # Apply before validation so replacements don't trigger repair calls.
        if self.brand_rules:
            output = apply_brand_replacements(output, self.brand_rules)

        # Store quick_brief for validate_output to access one-page compactness check
        self._last_quick_brief = quick_brief

        # ตรวจ output ตามรูปแบบของ agent แล้วซ่อมถ้าไม่ผ่าน
        # M6: error classification — soft-accept categories (one-page, brand-hard)
        # get 1 repair then accept; format errors keep existing max_retry_limit.
        max_repair = self.config.get("max_retry_limit", 3)
        ok, error = self.validate_output(output)
        # บันทึก draft ก่อน repair เพื่อ acceptance diagnostics
        self._last_draft_output = output
        self._last_first_validation_error = error if not ok else ""
        self._last_repair_count = 0
        repair_attempts: dict[str, int] = {}
        for _ in range(max_repair):
            if ok:
                break
            category = _error_category(error)
            budget = _repair_budget(category, max_repair)
            if repair_attempts.get(category, 0) >= budget:
                if category in _SOFT_ACCEPT_CATEGORIES:
                    # Soft-accept: instruction-following / brand issue exhausted.
                    # Don't raise — accept output despite the soft error.
                    console.print(f"[yellow]soft-accept: {category} ซ่อมครบ budget แล้ว — ยอมรับ output[/yellow]")
                    ok = True
                    break
                raise ValueError(
                    f"Agent {self.agent_name} ตรวจ output ไม่ผ่านหลังซ่อม {budget} รอบ: {error}"
                )
            self._last_repair_count += 1
            repair_attempts[category] = repair_attempts.get(category, 0) + 1
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
        self._last_repaired_output = output
        if not ok:
            raise ValueError(f"Agent {self.agent_name} ตรวจ output ไม่ผ่านหลังซ่อม {max_repair} รอบ: {error}")

        return output

    def _build_multimodal_content(self, text: str, image_paths: list[str] | None) -> str | list[dict]:
        """สร้าง message content แบบ multimodal (text + รูปจริง) ถ้ามีรูป."""
        content = build_multimodal_content(text, tuple(image_paths or []))
        if isinstance(content, list):
            console.print(f"[dim]ส่งรูปจริง {len(content) - 1} รูปให้ LLM vision ({self.display_name})[/dim]")
        return content



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

        แปะเฉพาะ URL ที่ไม่ซ้ำ. ถ้า agent มี _assess_source_relevance จะกรอง topic
        relevance ก่อน: relevant=False ทิ้ง, relevant=True เก็บไม่ fetch,
        relevant=None ส่งเข้า web_fetch ตรวจแบบเดิม.

        ถ้า agent ตั้ง citation_policy.mode == "inline_first" จะไม่แปะ dump
        ซ้ำกับ inline citations ที model ใส่ไว้แล้ว ถ้าไม่มี inline เลยจึงค่อย
        fallback ด้วย relevant annotations.
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

        relevant_annotations: list[dict[str, Any]] = []
        candidate_annotations: list[dict[str, Any]] = []
        to_verify: list[dict[str, Any]] = []

        assess = getattr(self, "_assess_source_relevance", None)
        if callable(assess):
            for a in unique_urls:
                result = assess(a)
                rel = result.get("relevant")
                if rel is False:
                    continue
                if rel is True:
                    # Verified identity match — can be used as fallback citation
                    relevant_annotations.append({**a, "_relevance": result})
                else:
                    # Unverified (relevant=None) — diagnostic only, NOT fallback.
                    # Still send to web_fetch for verification.
                    candidate_annotations.append({**a, "_relevance": result})
                    to_verify.append(a)
        else:
            to_verify = unique_urls

        # _last_relevant_annotations = verified only (for renderer provenance + fallback)
        self._last_relevant_annotations = relevant_annotations
        # _last_candidate_annotations = unverified (for manifest/diagnostic only)
        if hasattr(self, "_last_candidate_annotations"):
            self._last_candidate_annotations = candidate_annotations
        # Let subclasses update derived state (selected evidence, competitor models, etc.)
        # before the first validation/repair.
        on_ready = getattr(self, "_on_annotations_ready", None)
        if callable(on_ready):
            on_ready()
        citation_policy = self.config.get("citation_policy", {})
        if citation_policy.get("mode") == "inline_first":
            # Inline-first: ถ้า output มี URL จาก annotations อยู่แล้ว ไม่ append dump ซ้ำ
            unique_url_set = {a.get("url", "") for a in unique_urls}
            has_inline = any(url and (url in output) for url in unique_url_set)
            if has_inline:
                return output
            # ถ้าไม่มี inline เลย → fallback ด้วย relevant annotations ถ้าเปิดไว้
            if not citation_policy.get("fallback_annotations_when_no_inline", False):
                return output
            if relevant_annotations:
                output += (
                    "\n\n---\n\n**แหล่งอ้างอิง (fallback):**\n"
                    + "\n".join(
                        f"- [{a.get('title') or a.get('url')}]({a.get('url')})"
                        for a in relevant_annotations
                    )
                )
            return output

        # Default: แสดงเฉพาะ verified sources (relevant=True) ใน section หลัก
        # unverified candidates (relevant=None) ไม่แสดงเป็น citation
        # ถ้าไม่มี assess function (else branch) relevant_annotations ว่าง ใช้ to_verify แทน
        citation_annotations = relevant_annotations if relevant_annotations else to_verify

        if citation_annotations:
            output += (
                "\n\n---\n\n**แหล่งอ้างอิงจริงจากการค้นหา:**\n"
                + "\n".join(
                    f"- [{a.get('title') or a.get('url')}]({a.get('url')})"
                    for a in citation_annotations
                )
            )

        if verify_urls and to_verify:
            verified = self._verify_urls_with_fetch(to_verify)
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
        user_prompt: str = "",
        image_paths: list[str] | None = None,
    ) -> str:
        """ตรวจงานเทียบกับ instructions เป็น checklist รายข้อ.

        Reviewer เห็น 4 ส่วนแยกกันชัดเจน:
          1. ข้อกำหนดหลัก (system_prompt — role + format)
          2. ข้อมูลต้นทาง (user_prompt — มี raw data ของสินค้า) เพื่อตรวจข้อเท็จจริง
          3. Checklist จาก user instructions (rules_must, rules_forbid, custom, ฯลฯ)
          4. คำสั่งเฉพาะรอบนี้ (quick_brief)

        ถ้ามี image_paths → reviewer ได้รูปชุดเดียวกับ generator (multimodal)
        เพื่อตรวจข้อเท็จจริงของรูปได้ ไม่ใช่ตรวจแค่จาก text output

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

        # Grounding policy — same block the generator sees, so the reviewer
        # can distinguish supplied facts from inferences using the same
        # three-category contract.  Without this, agents that have
        # grounding_policy but no evidence_policy (e.g. content_creator)
        # get no grounding guidance during review.
        grounding_section = ""
        gp = self.config.get("grounding_policy")
        if gp:
            grounding_section = (
                f"\n--- GROUNDING POLICY (ใช้ตรวจข้อเท็จจริง) ---\n"
                + self._render_grounding_policy(gp)
                + "\n--- สิ้นสุด GROUNDING POLICY ---\n"
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

        # ส่วนข้อมูลต้นทาง — reviewer ต้องเห็น source เดียวกับ generator
        # เพื่อตรวจข้อเท็จจริงได้ (เช่น ตัด claim ที่เกิน source)
        source_section = ""
        if user_prompt:
            source_section = (
                f"\n--- ข้อมูลต้นทาง (ใช้ตรวจข้อเท็จจริงของผลงาน) ---\n"
                f"{user_prompt}\n"
                f"--- สิ้นสุดข้อมูลต้นทาง ---\n"
            )

        for i in range(max_iterations):
            review_user_msg = (
                f"--- ข้อกำหนดหลักของ agent ---\n"
                f"{system_prompt}\n"
                f"{source_section}"
                f"{checklist_section}"
                f"{evidence_section}"
                f"{grounding_section}"
                f"{brief_section}\n"
                f"--- ผลงานที่ต้องตรวจ ---\n"
                f"{output}\n"
                f"--- สิ้นสุดผลงาน ---\n\n"
                f"{json_instruction}"
                f"วิธีตรวจ:\n"
                f"1. อ่าน CHECKLIST + EVIDENCE DISCIPLINE + GROUNDING POLICY ทุกข้อ\n"
                f"2. เทียบทุก claim ในผลงานกับข้อมูลต้นทาง — ถ้าเกิน source ให้ลบหรือแก้\n"
                f"3. ถ้ามี claim ใดไม่มีหลักฐานตาม evidence_policy หรือใช้แหล่งทีผิด ให้แก้หรือลบ\n"
                f"4. ถ้าครบถ้วนทุกข้อ ส่งผลงานเดิมกลับมาเลย ไม่ต้องเปลี่ยนแปลง\n"
                f"ส่งกลับเฉพาะผลงานฉบับสุดท้ายเท่านั้น ไม่ต้องอธิบายว่าแก้อะไร"
            )
            # ส่งรูปชุดเดียวกับ generator ให้ reviewer (multimodal) เพื่อตรวจข้อเท็จจริงของรูป
            review_content = self._build_multimodal_content(review_user_msg, image_paths)
            messages = [
                {"role": "system", "content": review_prompt},
                {"role": "user", "content": review_content},
            ]
            console.print(f"\n[cyan]กำลังตรวจงาน... (รอบที่ {i+1})[/cyan]\n")
            refined = self.llm.chat(
                messages,
                model=review_model,
                temperature=review_temp,
                max_tokens=self.config.get("max_tokens", 4096),
                max_retry_limit=self.config.get("max_retry_limit", 3),
                stream=self.config.get("stream", True),
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
        """ตรวจ output ของ agent ว่าตรงกับรูปแบบที่กำหนดไหม.

        required_output_sections ที่กำหนดใน config จะกลายเป็น "คำแนะนำ/โครงสร้าง default"
        เมื่อ user ไม่ระบุรูปแบบอื่น แต่จะถูกบังคับจริงก็ต่อเมื่อเปิด strict_output_sections เท่านั้น

        M6 additions (source-driven, no hardcoded vocabulary):
        - one-page compactness check (only when one-page intent is active)
        - brand hard-rule check (restricted/banned from terms.json/voice.json)
        """
        if self.config.get("strict_output_sections"):
            required = self.config.get("required_output_sections")
        else:
            # ปล่อยให้โมเดลยืดหยุ่นตาม quick_brief — ไม่ fallback ไปอ่าน config ส่วนกลาง
            required = []
        quality = self.config.get("output_quality")
        ok, error = _validate_output(self.agent_name, output, required, output_quality=quality)
        if not ok:
            return ok, error

        # M6: one-page compactness (only when one-page intent is active)
        from ..output_validators import validate_one_page_compactness, validate_brand_hard
        quick_brief = getattr(self, "_last_quick_brief", "")
        ok, error = validate_one_page_compactness(output, quick_brief)
        if not ok:
            return ok, error

        # M6: brand hard-rule (restricted/banned from runtime brand data)
        if self.brand_rules:
            ok, error = validate_brand_hard(output, self.brand_rules)
            if not ok:
                return ok, error

        # M6: citation provenance (Item 3) — mechanically-knowable URL check.
        # Only runs when web_search is active. The allowed URL set is
        # constructed from ALL mechanically-known evidence paths:
        # _last_annotations, _last_relevant_annotations, and
        # _selected_evidence_urls. If a provenance path cannot be
        # mechanically determined, no heuristic is invented for it.
        if self.config.get("web_search"):
            from ..output_validators import validate_citation_provenance
            allowed_urls: set[str] = set()
            for ann in getattr(self, "_last_annotations", []) or []:
                url = ann.get("url") if isinstance(ann, dict) else None
                if url:
                    allowed_urls.add(url)
            for ann in getattr(self, "_last_relevant_annotations", []) or []:
                url = ann.get("url") if isinstance(ann, dict) else None
                if url:
                    allowed_urls.add(url)
            allowed_urls |= getattr(self, "_selected_evidence_urls", set()) or set()
            ok, error = validate_citation_provenance(output, allowed_urls)
            if not ok:
                return ok, error

        return True, ""

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
            "ผลงานข้างต้นไม่ตรงตามรูปแบบที่กำหนด: {error}\n"
            "กรุณาแก้ไขให้ตรงรูปแบบโดยไม่เปลี่ยนเนื้อหา ส่งเฉพาะผลงานฉบับสุดท้ายเท่านั้น",
        )
        evidence_text = ""
        selected = list(getattr(self, "_selected_evidence", []) or [])
        if selected:
            lines = ["ข้อมูลหลักฐานทีเลือกใช้ (selected evidence) สำหรับแก้ไข:"]
            for a in selected:
                title = a.get("title") or "แหล่งอ้างอิง"
                url = a.get("url") or ""
                content = (a.get("content") or "").replace("\n", " ").strip()
                # Compact excerpt: keep enough to be useful, not a full-page dump.
                snippet = content[:300]
                if len(content) > 300:
                    snippet += " ..."
                lines.append(f"- {title} ({url}): {snippet}")
            lines.append(
                "คำแนะนำ: แก้ไขข้อผิดพลาดโดยอ้างอิงข้อมูลด้านบนเมื่อเหมาะสม, "
                "เก็บข้อเท็จจริงทีมีหลักฐานสนับสนุน พร้อม inline citation [claim](URL), "
                "ลบเฉพาะข้อมูลทีไม่มีหลักฐาน ไม่สร้างตัวเลขเอง ไม่แปลงสกุลเงิน ไม่บอกว่าไม่มีหลักฐานเมื่อมี"""
            )
            evidence_text = "\n".join(lines) + "\n\n"
        repair_messages = messages + [
            {"role": "assistant", "content": output},
            {"role": "user", "content": evidence_text + prompt.format(error=error)},
        ]
        return self.llm.chat(
            repair_messages,
            model=self.config.get("model"),
            temperature=self.config.get("temperature", 0.7),
            max_tokens=self.config.get("max_tokens", 4096),
            max_retry_limit=self.config.get("max_retry_limit", 3),
            stream=self.config.get("stream", True),
            response_format=response_format,
            source=f"{self.agent_name}.repair",
        )

    def build_prompt(self, *args: Any, **kwargs: Any) -> str:
        """Construct the user prompt for this agent.

        Subclasses must override this.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Final grounding gate — shared truth boundary for all agents
    # ------------------------------------------------------------------

    _GROUNDING_CHECK_SCHEMA = {
        "type": "object",
        "properties": {
            "grounded": {
                "type": "boolean",
                "description": "true ถ้าทุก claim ใน final_text มีฐานจาก context ที่ให้",
            },
            "unsupported_claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string", "description": "ข้อความที่ไม่มีฐาน"},
                        "reason": {"type": "string", "description": "เหตุผลว่าทำไมไม่มีฐาน"},
                    },
                    "required": ["claim", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["grounded", "unsupported_claims"],
        "additionalProperties": False,
    }

    def verify_final_grounding(
        self,
        final_text: str,
        runtime_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Final grounding verification before persistence.

        Uses model reasoning to check if final_text contains claims not
        supported by the authorized runtime context.  This is a semantic
        entailment check — no regex/keyword lists.

        **Fails closed**: if the verifier cannot produce a valid, non-truncated
        result (missing LLM, exception, malformed JSON, empty response,
        truncation), the result is ``{grounded: False}`` so the caller
        does NOT persist the artifact as a successful run.

        Args:
            final_text: the final candidate output (after all mutations)
            runtime_context: dict with keys:
                - product_source: selected product source text
                - brand_context: brand/audience settings (positioning, tone, vocabulary)
                - quick_brief: user instructions for this run
                - ui_options: typed UI selections (platform, media_type, etc.)
                - verified_evidence: externally verified facts with provenance (optional)

        Returns:
            {grounded: bool, unsupported_claims: [{claim, reason}]}
        """
        # Empty text is trivially grounded (nothing to verify).
        if not final_text or not final_text.strip():
            return {"grounded": True, "unsupported_claims": []}

        # Missing LLM → cannot verify → fail closed.
        if not self.llm:
            return {"grounded": False, "unsupported_claims": [], "error": "no_llm"}

        # Build the context sections for the model
        sections = []

        product_source = runtime_context.get("product_source", "")
        if product_source:
            sections.append(
                f"--- ข้อมูลสินค้า (product source — ข้อเท็จจริงเท่านั้น) ---\n"
                f"{product_source}\n"
                f"--- สิ้นสุดข้อมูลสินค้า ---"
            )

        verified_evidence = runtime_context.get("verified_evidence", "")
        if verified_evidence:
            sections.append(
                f"--- หลักฐานที่ผ่านการตรวจสอบ (verified evidence) ---\n"
                f"{verified_evidence}\n"
                f"--- สิ้นสุดหลักฐาน ---"
            )

        brand_context = runtime_context.get("brand_context", "")
        if brand_context:
            sections.append(
                f"--- Brand/Audience (positioning, tone, vocabulary — ไม่ใช่ product capability) ---\n"
                f"{brand_context}\n"
                f"--- สิ้นสุด Brand/Audience ---"
            )

        quick_brief = runtime_context.get("quick_brief", "")
        if quick_brief:
            sections.append(
                f"--- Quick Brief (คำสั่งของ user — ไม่สามารถสร้าง fact ได้) ---\n"
                f"{quick_brief}\n"
                f"--- สิ้นสุด Quick Brief ---"
            )

        ui_options = runtime_context.get("ui_options", {})
        if ui_options:
            import json as _json_opts
            sections.append(
                f"--- UI Options ---\n"
                f"{_json_opts.dumps(ui_options, ensure_ascii=False)}\n"
                f"--- สิ้นสุด UI Options ---"
            )

        context_block = "\n\n".join(sections)

        system_prompt = (
            "คุณเป็นผู้ตรวจสอบข้อเท็จจริง (Grounding Verifier)\n"
            "หน้าที่: ตรวจว่างานสุดท้ายมี claim ใดที่ไม่มีฐานจาก context ที่ให้หรือไม่\n\n"
            "กฎการตรวจ:\n"
            "1. product source เป็นข้อเท็จจริงเท่านั้น — claim ต้องมีฐานจากนี่\n"
            "2. verified evidence เป็นข้อเท็จจริงที่ตรวจสอบแล้ว — ใช้ได้\n"
            "3. Brand/Audience ควบคุม positioning, tone, vocabulary, channels — "
            "ไม่ใช่ product capability\n"
            "   ถ้า brand context อ้างถึงสินค้าอื่น ห้ามนำ capability ของ "
            "สินค้านั้นมาใส่ในสินค้าที่กำลังตรวจ\n"
            "4. Quick Brief ควบคุมงานที่ขอ — ไม่สามารถสร้าง fact หรือ offer ได้\n"
            "5. ถ้าไม่แน่ใจว่า claim มีฐานหรือไม่ → ถือว่าไม่มีฐาน\n"
            "6. CTA ที่อ้างถึง offer/โปรโมชั่นที่ user ไม่ได้ระบุ = unsupported claim\n"
            "7. inference/recommendation ที่ไม่อ้างเป็น fact = ผ่าน\n\n"
            f"{context_block}\n\n"
            "คืน JSON ตาม schema"
        )

        user_prompt = f"ตรวจงานสุดท้ายนี้:\n\n{final_text}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "grounding_check",
                "strict": True,
                "schema": self._GROUNDING_CHECK_SCHEMA,
            },
        }

        import json as _json
        import re as _re

        try:
            raw = self.llm.chat(
                messages,
                temperature=0.1,
                max_tokens=2048,
                stream=False,
                response_format=response_format,
                source=f"{self.agent_name}.final_grounding_check",
            )
        except Exception:
            # Infrastructure failure → fail closed.
            return {"grounded": False, "unsupported_claims": [], "error": "llm_exception"}

        if isinstance(raw, tuple):
            raw = raw[0]

        # Empty or None response → fail closed.
        if not raw or not raw.strip():
            return {"grounded": False, "unsupported_claims": [], "error": "empty_response"}

        # Truncated response → fail closed.
        if getattr(self.llm, "last_truncated", False):
            return {"grounded": False, "unsupported_claims": [], "error": "truncated"}

        # Strip code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = _re.sub(r"^```(?:json)?\s*", "", raw)
            raw = _re.sub(r"\s*```$", "", raw)

        try:
            result = _json.loads(raw)
        except (ValueError, TypeError):
            # Malformed JSON → fail closed.
            return {"grounded": False, "unsupported_claims": [], "error": "malformed_json"}

        if not isinstance(result, dict) or "grounded" not in result:
            return {"grounded": False, "unsupported_claims": [], "error": "invalid_schema"}

        return {
            "grounded": bool(result.get("grounded")),
            "unsupported_claims": result.get("unsupported_claims", []),
        }
