"""Agent 2: Competitor Analysis Agent — วิเคราะห์เปรียบเทียบคู่แข่ง."""

from __future__ import annotations

import json
import re
from typing import Any

from ..output_validators import _strip_citations_and_urls
from .base_agent import BaseAgent
from .competitor_evidence import (
    ALLOWED_FIELD_IDS,
    CompetitorReportRenderer,
    EVIDENCE_SYSTEM_PROMPT,
    ResearchResponse,
    RESEARCH_RESPONSE_SCHEMA,
)


class CompetitorAnalysisAgent(BaseAgent):
    agent_name = "competitor_analysis"
    display_name = "นักวิเคราะห์คู่แข่ง"

    _FABRICATABLE_SPEC_RE = re.compile(r"\b(mah|ghz|mhz|mp|gb|mb|mm|cm|w|v|ip\d+|amoled|tft|ips|gps|nfc|ecg|spo2|cpu|gpu|ram|rom|bluetooth|wifi|camera|heart rate|blood oxygen)\b", re.I)
    _CURRENCY_RE = re.compile(r"(฿|\$|usd|baht|บาท|euro|€)", re.I)
    _NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
    _DISTRIBUTION_RE = re.compile(r"\b(ขาย|จำหน่าย|วางจำหน่าย|ช่องทาง|ตัวแทน|ทั่วไป|shopee|lazada)\b", re.I)

    # ------------------------------------------------------------------
    # System prompt split: legacy report vs evidence-mode Stage A
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Return the evidence-only Stage A prompt in evidence mode, otherwise legacy."""
        evidence = getattr(self, "_evidence_mode", False) or self.config.get("evidence_mode", False)
        if evidence:
            return EVIDENCE_SYSTEM_PROMPT
        return super()._build_system_prompt()

    def build_prompt(self, product_spec: str, competitor_data: str) -> str:
        self._relevance_context = self._build_relevance_context(product_spec, competitor_data)
        self._product_spec = product_spec or ""
        self._competitor_data = competitor_data or ""
        self._competitor_names = [line.strip() for line in self._competitor_data.splitlines() if line.strip()]
        self._target_model = self._extract_target_model(self._product_spec)
        self._web_search_enabled = bool(self.config.get("web_search"))
        self._thin_competitor_context = self._is_thin_competitor_context(self._competitor_data)

        prompt = (
            "กรุณาวิเคราะห์เปรียบเทียบสินค้าของเรากับคู่แข่ง "
            "โดยใช้ข้อมูลดังต่อไปนี้:\n\n"
            "--- สเปคสินค้าของเรา (สินค้าที่จะวิเคราะห์ ใช้รุ่นนี้เท่านั้น) ---\n"
            f"{product_spec}\n\n"
            "--- ข้อมูลคู่แข่ง ---\n"
            f"{competitor_data}\n"
            "--- สิ้นสุดข้อมูลคู่แข่ง ---\n\n"
            "วิเคราะห์เปรียบเทียบเป็นภาษาไทยตามรูปแบบที่กำหนดใน system prompt\n"
            "สำคัญ: ใช้สินค้าที่ให้มาในสเปคข้างต้นเท่านั้น "
            "ห้ามสับสนกับรุ่นอื่นในแบรนด์เดียวกัน"
        )

        # Default competitor discovery: when no competitor data is provided
        # and web search is enabled, instruct the model to discover competitors
        # itself from web search and populate competitor_names + evidence in the output.
        if not self._competitor_names and self._web_search_enabled:
            prompt += (
                "\n\n"
                "**โหมดค้นหาคู่แข่งอัตโนมัติ (Default Competitor Discovery)**\n"
                "ไม่มีข้อมูลคู่แข่งที่ระบุไว้ล่วงหน้า — ให้คุณค้นหาคู่แข่งเองจาก web search\n"
                "ขั้นตอน:\n"
                "1. วิเคราะห์สินค้าเป้าหมายจากสเปคข้างต้น (หมวดหมู่ ราคา ฟีเจอร์หลัก)\n"
                "2. ค้นเว็บหาสินค้าคู่แข่งในหมวดเดียวกัน ในตลาดเดียวกัน (ไทย ถ้าระบุ)\n"
                "3. เลือก 2-3 คู่แข่งหลักที่ใกล้เคียงที่สุด\n"
                "4. ใส่ชื่อคู่แข่งที่ค้นพบใน field `competitor_names` ของ JSON output\n"
                "5. ค้นข้อมูลเฉพาะรุ่นของคู่แข่งแต่ละรุ่น แล้วสร้าง evidence records\n\n"
                "สำคัญ: competitor_names ต้องไม่ว่าง — ใส่ชื่อคู่แข่งที่ค้นพบจาก web search\n"
                "evidence ต้องไม่ว่าง — ใส่อย่างน้อย 1 evidence record ต่อคู่แข่ง "
                "โดยอ้างอิง URL จริงจาก search results\n"
                "แต่ละ evidence record ต้องมี: competitor (ชื่อตรงกับ competitor_names), "
                "field (เช่น price_availability, display, battery, gps_tracking), "
                "claim (ข้อเท็จจริงที่พบ), url (URL จริงจาก search), geography (thailand หรือ global)\n"
                "ถ้าค้นไม่พบข้อมูลเฉพาะรุ่น ให้ใส่ evidence ที่พบใกล้เคียงที่สุดและอธิบายใน uncertainty"
            )

        if self._thin_competitor_context and not self._web_search_enabled:
            prompt += (
                "\n\n"
                "คำเตือน: ข้อมูลคู่แข่งที่ให้มามีเฉพาะชื่อรุ่นหรือข้อมูลเปล่า "
                "ห้ามเขียน technical spec, ราคา, ฟีเจอร, หรือ comparison fact ของคู่แข่ง "
                "เว้นแต่ข้อมูลดังกล่าวจะระบุไว้อย่างชัดเจนในข้อมูลคู่แข่งข้างต้นเท่านั้น "
                "ถ้าไม่มีข้อมูลพอ ให้คืนรายงานแบบ limited analysis ที่ระบุสิ่งที่รู้และข้อมูลที่ต้องขอเพิ่ม"
            )
        return prompt

    def run(
        self,
        user_prompt: str,
        quick_brief: str = "",
        image_paths: list[str] | None = None,
        extra_image_paths: list[str] | None = None,
        resource_context: str = "",
        response_format: dict | None = None,
        step_context: Any = None,
    ) -> str:
        """Generate output. In evidence mode, use structured self-review for ResearchResponse."""
        self._evidence_mode = self.config.get("evidence_mode", False)
        if not self._evidence_mode:
            return super().run(
                user_prompt,
                quick_brief=quick_brief,
                image_paths=image_paths,
                extra_image_paths=extra_image_paths,
                resource_context=resource_context,
                response_format=response_format,
                step_context=step_context,
            )

        # Evidence mode always uses the canonical schema so prompt+schema stay in sync.
        response_format = RESEARCH_RESPONSE_SCHEMA
        if not self.config.get("web_search"):
            return self._structural_output_failure("evidence mode requires web_search")

        # Stage 1: generate ResearchResponse (BaseAgent handles web search + tools)
        self._skip_agent_validation = True
        try:
            json_output = super().run(
                user_prompt,
                quick_brief=quick_brief,
                image_paths=image_paths,
                extra_image_paths=extra_image_paths,
                resource_context=resource_context,
                response_format=response_format,
                step_context=step_context,
                provider={"require_parameters": True},
            )
        finally:
            self._skip_agent_validation = False

        # Stage 2: deterministic validation + exactly one self-review round
        self._last_generate_raw = json_output

        # Default discovery mode: the model discovered competitor names via web
        # search, but _assess_source_relevance ran inside super().run() BEFORE
        # those names were known — so _last_relevant_annotations is empty.
        # Extract competitor_names from the raw JSON and re-assess all
        # annotations BEFORE validation, so _validate_research_json and the
        # renderer can match evidence URLs against relevant annotations.
        self._reassess_for_default_discovery(json_output)

        # Keep quick_brief for the final renderer so it can choose presentation.
        self._quick_brief = quick_brief

        ok, err, research = self._validate_research_json(json_output)
        final_json = json_output
        if not ok:
            final_json = self._revise_research(user_prompt, json_output, err, response_format)
            self._last_revise_raw = final_json if final_json != json_output else None
            # Re-assess again in case the revision changed competitor_names
            self._reassess_for_default_discovery(final_json)
            ok, err, research = self._validate_research_json(final_json)
            if not ok or research is None:
                if err and "evidence_validation" in err:
                    return self._required_search_failure(err)
                return self._structural_output_failure(err or "research response validation failed after one revision")

        self._last_validated_research_json = final_json

        relevant = getattr(self, "_last_relevant_annotations", []) or []
        renderer = CompetitorReportRenderer(
            research,
            relevant_annotations=relevant,
            quick_brief=getattr(self, "_quick_brief", ""),
        )

        # Stage 3: render Markdown from validated evidence only
        markdown = renderer.render(self._product_spec)
        ok, err = self.validate_output(markdown)
        if not ok:
            return self._structural_output_failure(err)

        self._last_draft_output = final_json
        return markdown

    # ------------------------------------------------------------------
    # Two-stage evidence contract
    # ------------------------------------------------------------------

    _FENCE_RE = re.compile(r"^```(?:json)?\s*\n?([\s\S]*?)\n?```\s*$")

    def _reassess_for_default_discovery(self, json_output: str) -> None:
        """In default discovery mode, re-assess annotations with model-discovered
        competitor names.  Called before _validate_research_json so the renderer
        can match evidence URLs against relevant annotations.

        In default discovery mode (no competitor_data), _assess_source_relevance
        ran inside super().run() with an empty competitor_names list — so no
        annotation got relevant=True.  The model then discovers competitors via
        web search and returns their names in the JSON.  This method extracts
        those names, updates the relevance context, and re-runs the assessment.
        """
        if self._competitor_names:
            return  # not default discovery mode — competitor names were provided

        # Extract competitor_names from the raw JSON without full validation
        try:
            data = json.loads(self._strip_json_fence(json_output))
        except (json.JSONDecodeError, AttributeError):
            return

        names = data.get("competitor_names") if isinstance(data, dict) else None
        if not isinstance(names, list) or not names:
            return

        self._relevance_context["competitor_names"] = [str(n) for n in names if n]
        self._reassess_all_annotations()

    def _reassess_all_annotations(self) -> None:
        """Re-run _assess_source_relevance on all stored annotations and split
        them into _last_relevant_annotations (available for citation) and
        _last_rejected_annotations (blocked).

        Design: _assess_source_relevance is diagnostic only — its substring
        matching cannot reliably match competitor names to URL slugs (e.g.
        "imoo watch phone z1" vs "imoo-kid-watch-phone-z1").  Only
        structurally-blocked sources (relevant=False: homepage,
        category_mismatch) are excluded.  Everything else (relevant=True,
        None) is available for the model to cite, with the diagnostic
        _relevance metadata attached for warnings.
        """
        all_annotations = getattr(self, "_last_annotations", []) or []
        self._last_relevant_annotations = []
        self._last_rejected_annotations = []
        for a in all_annotations:
            r = self._assess_source_relevance(a)
            if r.get("relevant") is False:
                # Structurally blocked: homepage, category mismatch
                self._last_rejected_annotations.append({**a, "_relevance": r})
            else:
                # Available for citation (relevant=True or None)
                self._last_relevant_annotations.append({**a, "_relevance": r})

    def _strip_json_fence(self, text: str) -> str:
        text = text.strip()
        m = self._FENCE_RE.match(text)
        if m:
            return m.group(1).strip()
        return text

    def _validate_research_json(self, text: str) -> tuple[bool, str, ResearchResponse | None]:
        """Deterministic validation: JSON transport hygiene, schema, and evidence contract."""
        text = self._strip_json_fence(text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return False, f"structural_output_failed: invalid JSON: {e}", None

        required = ("target_model", "competitor_names", "evidence",
                    "evidence_based_recommendations", "strategic_hypotheses", "uncertainty")
        for key in required:
            if key not in data:
                return False, f"structural_output_failed: missing field {key}", None

        if not isinstance(data.get("competitor_names"), list):
            return False, "structural_output_failed: competitor_names must be a list", None
        if not isinstance(data.get("evidence"), list):
            return False, "structural_output_failed: evidence must be a list", None

        for i, ev in enumerate(data.get("evidence", [])):
            if not isinstance(ev, dict):
                return False, f"structural_output_failed: evidence[{i}] is not an object", None
            for k in ("competitor", "field", "claim", "url", "geography"):
                if k not in ev:
                    return False, f"structural_output_failed: evidence[{i}] missing {k}", None
            if ev.get("field") not in ALLOWED_FIELD_IDS:
                return False, f"structural_output_failed: evidence[{i}] field {ev.get('field')!r} not allowed", None
            if ev.get("geography") not in ("thailand", "global"):
                return False, f"structural_output_failed: evidence[{i}] geography invalid", None
            if len(ev.get("competitor", "")) > 120:
                return False, f"structural_output_failed: evidence[{i}] competitor too long", None
            if len(ev.get("claim", "")) > 400:
                return False, f"structural_output_failed: evidence[{i}] claim too long", None
            if len(ev.get("url", "")) > 500:
                return False, f"structural_output_failed: evidence[{i}] url too long", None

        if len(data.get("evidence", [])) > 6:
            return False, "structural_output_failed: evidence has more than 6 items", None
        if any(len(c) > 120 for c in data.get("competitor_names", [])):
            return False, "structural_output_failed: competitor_name too long", None

        # Validate evidence_based_recommendations structure
        ebr = data.get("evidence_based_recommendations", [])
        if len(ebr) > 3:
            return False, "structural_output_failed: evidence_based_recommendations has more than 3 items", None
        for i, rec in enumerate(ebr):
            if not isinstance(rec, dict):
                return False, f"structural_output_failed: evidence_based_recommendations[{i}] is not an object", None
            for k in ("text", "supporting_evidence_urls"):
                if k not in rec:
                    return False, f"structural_output_failed: evidence_based_recommendations[{i}] missing {k}", None
            if len(rec.get("text", "")) > 400:
                return False, f"structural_output_failed: evidence_based_recommendations[{i}] text too long", None
            if not isinstance(rec.get("supporting_evidence_urls"), list):
                return False, f"structural_output_failed: evidence_based_recommendations[{i}] supporting_evidence_urls is not an array", None
            if len(rec.get("supporting_evidence_urls", [])) > 6:
                return False, f"structural_output_failed: evidence_based_recommendations[{i}] too many URLs", None

        # Validate strategic_hypotheses structure
        sh = data.get("strategic_hypotheses", [])
        if len(sh) > 3:
            return False, "structural_output_failed: strategic_hypotheses has more than 3 items", None
        for i, h in enumerate(sh):
            if not isinstance(h, dict):
                return False, f"structural_output_failed: strategic_hypotheses[{i}] is not an object", None
            for k in ("text", "rationale"):
                if k not in h:
                    return False, f"structural_output_failed: strategic_hypotheses[{i}] missing {k}", None
            if len(h.get("text", "")) > 400:
                return False, f"structural_output_failed: strategic_hypotheses[{i}] text too long", None
            if len(h.get("rationale", "")) > 400:
                return False, f"structural_output_failed: strategic_hypotheses[{i}] rationale too long", None

        if len(data.get("uncertainty", [])) > 3:
            return False, "structural_output_failed: uncertainty has more than 3 items", None
        if any(len(u) > 400 for u in data.get("uncertainty", [])):
            return False, "structural_output_failed: uncertainty too long", None
        if len(data.get("target_model", "")) > 120:
            return False, "structural_output_failed: target_model too long", None

        research = ResearchResponse.from_dict(data)
        if not research.target_model or not research.target_model.strip():
            return False, "structural_output_failed: target_model is empty", None
        if not research.competitor_names:
            return False, "structural_output_failed: competitor_names is empty", None

        # Target product must not change — model must analyze the product
        # specified in the input, not a different one.
        expected_target = (getattr(self, "_target_model", "") or "").strip().lower()
        actual_target = (research.target_model or "").strip().lower()
        if expected_target and actual_target and expected_target not in actual_target and actual_target not in expected_target:
            return False, f"structural_output_failed: target_model changed from {expected_target!r} to {actual_target!r}", None

        relevant = getattr(self, "_last_relevant_annotations", []) or []
        errors = CompetitorReportRenderer(research, relevant_annotations=relevant).validate()
        if errors:
            return False, "evidence_validation: " + "; ".join(errors), research

        return True, "", research

    def _build_evidence_manifest(self) -> str:
        """Canonical manifest of all non-rejected annotations for revise call.

        Market parity: include ALL non-rejected search results (relevant=True
        and relevant=None) with relevance labels, so the model can decide
        which sources to cite — like Claude/ChatGPT do.
        """
        relevant = getattr(self, "_last_relevant_annotations", []) or []
        target = getattr(self, "_target_model", "") or ""
        competitors = getattr(self, "_competitor_names", []) or []
        lines = [
            f"สินค้าเป้าหมาย (target_model): {target}",
            f"คู่แข่งใน scope: {', '.join(competitors) if competitors else '(ไม่ระบุ)'}",
            f"หมายเหตุ: competitor_names ต้องมาจากรายชื่อคู่แข่งข้างต้นเสมอ แม้จะไม่มี evidence",
        ]
        if not relevant:
            lines.append("ไม่พบ URL จากการค้นหา")
        else:
            lines.append("--- รายการ URL จากการค้นหา (เลือกใช้ตามความเกี่ยวข้อง) ---")
            for i, a in enumerate(relevant, 1):
                rel = a.get("_relevance", {})
                title = a.get("title", "").strip()
                url = a.get("url", "").strip()
                geo = rel.get("geography", "global")
                rel_type = rel.get("relevance_type", "unknown")
                rel_label = "verified" if rel.get("relevant") is True else "unverified"
                lines.append(f"\n[{i}] URL: {url}")
                lines.append(f"    title: {title}")
                lines.append(f"    geography: {geo}")
                lines.append(f"    relevance: {rel_label} ({rel_type})")
                content = (a.get("content") or "").strip()
                if content:
                    lines.append(f"    snippet: {content[:400]}")
        lines.append(
            "\nคุณสามารถใช้ URL จากรายการข้างต้นเท่านั้น "
            "ห้ามค้นหาเว็บเพิ่ม ห้าม fetch ห้ามสร้าง factual claim ใหม่ "
            "ถ้าไม่มี evidence พอ ให้คืน evidence: [] และอธิบายใน uncertainty"
        )
        return "\n".join(lines)

    def _revise_research(self, user_prompt: str, draft: str, error: str, response_format: dict) -> str:
        """One self-review/revision call. No tools, no new search."""
        system_prompt = self._build_system_prompt()
        manifest = self._build_evidence_manifest()
        revision_prompt = (
            f"{manifest}\n\n"
            "ผลงานก่อนหน้าไม่ผ่านการตรวจสอบ กรุณาแก้ไขและคืนคำตอบใหม่"
            "เป็น JSON ตาม schema 'competitor_research' เท่านั้น\n\n"
            "ข้อผิดพลาดทีพบ:\n"
            f"{error}\n\n"
            "กฎ:\n"
            "- ห้ามใช้ Markdown code fence (```json) หรือคำอธิบายนอก JSON\n"
            "- ห้ามเปลี่ยนชื่อ key หรือโครงสร้าง schema\n"
            "- ห้ามใช้ URL นอก manifest ข้างต้น\n"
            "- ห้ามสร้าง factual claim ทีไม่มี URL รองรับ\n"
            "- หากไม่มีหลักฐานพอ ให้คืน evidence: [] และอธิบายใน uncertainty"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": draft},
            {"role": "user", "content": revision_prompt},
        ]
        output = self.llm.chat(
            messages,
            model=self.config.get("model"),
            temperature=self.config.get("temperature", 0.4),
            max_tokens=self.config.get("max_tokens", 4096),
            max_retry_limit=1,
            response_format=response_format,
            provider={"require_parameters": True},
            source=f"{self.agent_name}.revise",
        )
        return output

    # ------------------------------------------------------------------
    # Per-agent source relevance — offline, deterministic
    # ------------------------------------------------------------------

    def _build_relevance_context(self, product_spec: str, competitor_data: str) -> dict[str, Any]:
        """สร้าง context สำหรับตัดสิน relevance จาก product_spec + competitor_data."""
        spec = product_spec or ""
        comp = competitor_data or ""
        ctx_text = (spec + "\n" + comp).lower()

        target_model = self._extract_target_model(spec)
        target_category = self._derive_target_category(ctx_text)
        competitor_names = [line.strip() for line in comp.splitlines() if line.strip()]

        return {
            "target_model": target_model,
            "target_category": target_category,
            "competitor_names": competitor_names,
            "target_text": ctx_text,
        }

    def _extract_target_model(self, product_spec: str) -> str:
        m = re.search(r"รหัสสินค้า[:=]\s*([^\s]+)", product_spec, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        # fallback: รุ่นทั่วไป เช่น K77, Lagenio K5
        m = re.search(r"\b([A-Za-z]+\d[\w]{0,4})\b", product_spec)
        return m.group(1) if m else ""

    def _derive_target_category(self, ctx_text: str) -> str:
        policy = self.config.get("relevance_policy", {})
        keywords = policy.get("target_category_keywords", [])
        ctx = ctx_text.lower()
        for kw in keywords:
            if kw.lower() in ctx:
                return "smartwatch"
        return "unknown"

    def _assess_source_relevance(self, annotation: dict[str, Any]) -> dict[str, Any]:
        """ตัดสิน offline วว่า source นี้เกี่ยวข้องกับงาน competitor analysis หรือไม่.

        คืน dict:
            relevant: True | False | None
            relevance_type: "target" | "competitor" | "market" | "category_mismatch" | "unknown"
            reason: str
            geography: "thailand" | "global" | "unknown"
        """
        ctx = getattr(self, "_relevance_context", None) or {}
        target_model = ctx.get("target_model", "")
        target_category = ctx.get("target_category", "unknown")
        target_text = ctx.get("target_text", "")
        competitor_names = ctx.get("competitor_names", [])
        policy = self.config.get("relevance_policy", {})

        url = (annotation.get("url") or "").lower()
        title = (annotation.get("title") or "").lower()
        content = (annotation.get("content") or "").lower()
        source_text = f"{url} {title} {content}"
        geography = self._detect_geography(source_text, url)

        # 1) obvious category mismatch
        if target_category != "unknown":
            for kw in policy.get("mismatch_keywords", []):
                if re.search(re.escape(kw.lower()), source_text):
                    return {
                        "relevant": False,
                        "relevance_type": "category_mismatch",
                        "reason": f"source mentions '{kw}' which conflicts with target category {target_category}",
                        "geography": geography,
                    }

        # 2) subcategory mismatch (kids, children, elderly) ถ้า target ไม่มี
        for kw in policy.get("subcategory_mismatch_keywords", []):
            if kw in source_text and kw not in target_text:
                return {
                    "relevant": None,
                    "relevance_type": "unknown",
                    "reason": f"source mentions '{kw}' which is absent from target context",
                    "geography": geography,
                }

        # 2.5) homepage / marketplace root ทีไม่ระบุรุ่นเฉพาะ
        if self._is_homepage_or_marketplace_root(url) and not self._source_mentions_product(source_text):
            return {
                "relevant": False,
                "relevance_type": "homepage",
                "reason": "source is a homepage or marketplace root without product-specific evidence",
                "geography": geography,
            }

        # 3) target product match
        # ใช้ word boundary เพื่อกัน false positive จากรุ่นใกล้เคียง เช่น K77 vs K771/K77A
        if target_model and re.search(
            r"\b" + re.escape(target_model.lower()) + r"\b", source_text
        ):
            return {
                "relevant": True,
                "relevance_type": "target",
                "reason": f"source matches target model {target_model}",
                "geography": geography,
            }

        # 4) competitor match — diagnostic only, not a hard gate.
        # Substring matching cannot reliably match competitor names to URL
        # slugs (e.g. "imoo watch phone z1" vs "imoo-kid-watch-phone-z1").
        # This assessment is stored as _relevance metadata for diagnostics,
        # but _reassess_all_annotations no longer excludes annotations that
        # don't match — the model decides which sources to cite.
        for name in competitor_names:
            if not name:
                continue
            pattern = re.escape(name.lower())
            if re.search(pattern, source_text):
                return {
                    "relevant": True,
                    "relevance_type": "competitor",
                    "reason": f"source matches competitor {name}",
                    "geography": geography,
                }

        # 5) market / category match: แค่บ่งบอกว่าเป็นหมวดเดียวกัน แต่ยังไม่ specific พอ
        # ให้ relevant=None เพื่อรอ verify ด้วย web_fetch หรือ model จะตัดสินเอง
        # ไม่ให้ relevant=True ทันทีเพราะอาจเป็น homepage หรือรายงานทั่วไป
        for kw in policy.get("market_keywords", []):
            if re.search(re.escape(kw.lower()), source_text):
                return {
                    "relevant": None,
                    "relevance_type": "market_unverified",
                    "reason": f"source is about {target_category} market/category but not product-specific",
                    "geography": geography,
                }

        # 6) unknown
        return {
            "relevant": None,
            "relevance_type": "unknown",
            "reason": "cannot confidently determine relevance from title/URL",
            "geography": geography,
        }

    # ------------------------------------------------------------------
    # Deterministic guards — ฝั่ง implementation ไม่ใช่ prompt อย่างเดียว
    # ------------------------------------------------------------------

    def _is_thin_competitor_context(self, competitor_data: str) -> bool:
        """ตัดสินว่า competitor_data มี factual evidence หรือมีแค่ชื่อ/คำบรรยายทั่วไป."""
        if not competitor_data or not competitor_data.strip():
            return True
        text = competitor_data.strip()
        if self._CURRENCY_RE.search(text):
            return False
        if self._FABRICATABLE_SPEC_RE.search(text):
            return False
        if re.search(r"https?://", text):
            return False
        # ถ้ามีตัวเลข 3 หลักขึ้นไปพร้อม unit หรือคำบรรยายเทคนิค จึงจะนับว่ามี spec
        numbers = self._NUMBER_RE.findall(text)
        for n in numbers:
            if len(n) >= 3:
                return False
        return True

    def _has_fabricated_competitor_specs(self, output: str) -> str:
        """สแกนว่า output มีการเขียนสเปค/ราคา/ตัวเลขของคู่แข่งทีไม่มีในข้อมูลต้นทางหรือไม่.

        คืน string บอกรายละเอียดหากพบ, หรือสตริงว่างถ้าสะอาด
        """
        names = getattr(self, "_competitor_names", [])
        thin = getattr(self, "_thin_competitor_context", False)
        comp_data = getattr(self, "_competitor_data", "")
        if not names or not thin:
            return ""

        comp_lower = comp_data.lower()
        for name in names:
            if not name:
                continue
            name_lower = name.lower()
            for line in output.splitlines():
                idx = line.lower().find(name_lower)
                if idx == -1:
                    continue
                # เอาเฉพาะส่วนทีเริ่มจากชื่อคู่แข่งไปจนถึงท้ายบรรทัด
                # แล้วค่อยตัดชื่อคู่แข่งออก ทำให้ตัวเลขก่อนหน้า (เช่น K77 ทีสินค้า) ไม่โดนนับ
                segment = line[idx:]
                rest = re.sub(re.escape(name), "", segment, count=1, flags=re.I)
                # หาตัวเลข/ราคาในบรรทัดที่ไม่อยู่ในข้อมูลต้นทาง
                numbers = self._NUMBER_RE.findall(rest)
                for num in numbers:
                    if num not in comp_lower:
                        return f"line mentions '{name}' with number '{num}' not in source data"
                # หาสัญลักษณ์สกุลเงิน
                if self._CURRENCY_RE.search(rest) and not self._CURRENCY_RE.search(comp_lower):
                    return f"line mentions '{name}' with currency not in source data"
                # หา unit/spec token ทั่วไปถ้าไม่มีในข้อมูลต้นทาง
                for spec in self._FABRICATABLE_SPEC_RE.findall(rest):
                    if spec.lower() not in comp_lower:
                        return f"line mentions '{name}' with spec token '{spec}' not in source data"
        return ""

    def _has_homepage_citations(self, output: str) -> str:
        """ตรวจ inline citation URL ทีเป็น homepage ไม่ใช่ product page."""
        from urllib.parse import urlparse

        allowed = {a.get("url", "").lower().rstrip("/") for a in getattr(self, "_last_relevant_annotations", [])}
        for match in re.finditer(r"\[([^\]]+)\]\((https?://[^\)]+)\)", output):
            url = match.group(2)
            key = url.lower().rstrip("/")
            if key in allowed:
                continue
            parsed = urlparse(url)
            path = (parsed.path or "").lower()
            # ถ้า path เปล่าหรือแค่ /th /en ถือว่า homepage
            if path in ("", "/") or re.match(r"^/(th|en|zh|ko|ja)/?$", path):
                return f"citation points to homepage: {url}"
            # ถ้า URL ไม่มี target model หรือ competitor name ใน path หรือ query ให้ระมัดระวัง
            target = getattr(self, "_target_model", "")
            comp_names = getattr(self, "_competitor_names", [])
            # แยกชื่อรุ่นออกเป็นคำ เพื่อรองรับเช่น "imoo Z1" → imoo
            all_keys = [target.lower()]
            for n in comp_names:
                all_keys.append(n.lower())
                all_keys.extend([p for p in n.lower().split() if len(p) >= 2])
            all_keys = [k for k in all_keys if k]
            if all_keys and not any(k in path or k in (parsed.query or "").lower() for k in all_keys):
                return f"citation URL does not contain product/competitor slug: {url}"
        return ""

    def _has_factual_signal(self, line: str) -> bool:
        """บรรทัดมี factual signal (ราคา, ตัวเลข, สเปค, ช่องทางจำหน่าย) หรือไม่."""
        # ตัวเลขต้องมีอย่างน้อย 3 หลัก เพื่อกัน false positive จากชื่อรุ่น เช่น S3, K77
        has_number = any(len(n) >= 3 for n in self._NUMBER_RE.findall(line))
        return bool(
            self._CURRENCY_RE.search(line)
            or has_number
            or self._FABRICATABLE_SPEC_RE.search(line)
            or self._DISTRIBUTION_RE.search(line)
        )

    def _is_scope_or_recommendation(self, line: str) -> bool:
        """บรรทัดเป็น scope, process, recommendation หรือ limitation statement."""
        line_lower = line.lower()
        scope_words = ("วิเคราะห์", "เปรียบเทียบ", "รายงานนี้", "รายงานฉบับนี้", "บทสรุป", "สรุป")
        process_words = ("ขั้นตอน", "วิธี", "กระบวนการ", "หมายเหตุ", "ข้อจำกัด", "ข้อสังเกต")
        recommendation_words = ("แนะนำ", "ควร", "อาจ", "สมมติฐาน", "ข้อเสนอแนะ")
        return (
            any(w in line_lower for w in scope_words)
            or any(w in line_lower for w in process_words)
            or any(w in line_lower for w in recommendation_words)
        )

    _NO_EVIDENCE_MARKERS = (
        "ไม่มีข้อมูล",
        "ไม่พบหลักฐาน",
        "ไม่มียืนยัน",
        "ไม่มีข้อมูลยืนยัน",
        "ไม่พบข้อมูล",
        "ไม่มีหลักฐานยืนยัน",
        "ไม่มีข้อมูลอย่างเป็นทางการ",
    )

    def _is_no_evidence_marker(self, text: str) -> bool:
        """เซลล์ระบุว่าไม่มีหลักฐาน ไม่ต้องการ citation."""
        t = text.lower()
        return any(m in t for m in self._NO_EVIDENCE_MARKERS)

    def _split_table_row(self, line: str) -> list[str]:
        """แยกเซลล์จากบรรทัดตาราง markdown โดยคง pipe ข้างนอกไม่เปลี่ยน."""
        # ตัด | นำท้ายและ split
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [c.strip() for c in stripped.split("|")]

    def _validate_table_claims(self, output: str) -> str:
        """ตรวจ factual claim ในตารางแต่ละเซลล์ต้องมี inline citation ทีถูกต้อง."""
        relevant = getattr(self, "_last_relevant_annotations", []) or []
        if not relevant:
            return "no selected evidence"

        rel_map: dict[str, dict[str, Any]] = {}
        for a in relevant:
            key = (a.get("url") or "").lower().rstrip("/")
            if key:
                rel_map[key] = a

        ctx = getattr(self, "_relevance_context", None) or {}
        competitor_names = [n.lower() for n in ctx.get("competitor_names", []) if n]
        thai_markers = ("บาท", "฿", "thb", "thailand", "ไทย", "ประเทศไทย", "shopee", "lazada")
        citation_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

        lines = output.splitlines()
        in_table = False
        table_lines: list[tuple[int, str]] = []
        for i, line in enumerate(lines):
            if "|" in line and not line.startswith("#"):
                in_table = True
                table_lines.append((i, line))
            else:
                if in_table and not line.strip():
                    break
                in_table = False

        # หา header ของแต่ละตาราง
        for start, (idx, _line) in enumerate(table_lines):
            # ข้าม separator เช่น |:---|:
            if re.match(r"^\s*\|[-:\s|]+\|\s*$", _line) or re.match(r"^\s*\|[-:\s]+\|", _line):
                continue
            cells = self._split_table_row(_line)
            # ตรวจว่า header มีชื่อคู่แข่งหรือไม่
            comp_col_indices: list[int] = []
            for j, cell in enumerate(cells):
                cell_lower = cell.lower()
                # คอลัมน์แรกเป็นคุณสมบัติ, คอลัมน์ทีสองเป็นสินค้าเรา
                if j >= 2 and any(c in cell_lower for c in competitor_names):
                    comp_col_indices.append(j)
            if not comp_col_indices:
                continue
            # ตรวจ data rows ต่อจาก header
            for ridx, rline in table_lines[start + 1:]:
                if re.match(r"^\s*\|[-:\s|]+\|\s*$", rline):
                    continue
                rcells = self._split_table_row(rline)
                for cidx in comp_col_indices:
                    if cidx >= len(rcells):
                        continue
                    cell = rcells[cidx]
                    if self._is_no_evidence_marker(cell):
                        continue
                    cell_lower = cell.lower()
                    # ถ้าเป็น scope/intro หรือ text ธรรมดาภายในเซลล์ ไม่ต้อง citation
                    if not self._has_factual_signal(cell):
                        continue
                    # factual cell ต้องมี inline citation ในเซลล์เดียวกัน
                    urls = [u.lower().rstrip("/") for _, u in citation_pattern.findall(cell)]
                    if not urls:
                        return f"table cell claim without citation: row {ridx + 1} col {cidx + 1}: {cell[:80]}"
                    for u in urls:
                        a = rel_map.get(u)
                        if not a:
                            return f"table cell unverified citation: {u}"
                        if a.get("_relevance", {}).get("relevance_type") != "competitor":
                            return f"table cell cites non-competitor source: {u}"
                        src_text = f"{a.get('url','')} {a.get('title','')} {a.get('content','')}".lower()
                        header_name = cells[cidx].lower() if cidx < len(cells) else ""
                        if not any(c in src_text for c in competitor_names if c in header_name):
                            return f"table cell source does not mention competitor: {u}"
                        if any(m in cell_lower for m in thai_markers):
                            if a.get("_relevance", {}).get("geography") != "thailand":
                                return f"table cell thai claim cites non-thai source: {u}"

        # ตรวจ fallback URL dump
        for i, line in enumerate(lines):
            if "แหล่งอ้างอิง" in line.lower() and "fallback" in line.lower():
                return f"fallback citation dump at line {i + 1}: {line.strip()[:80]}"

        return ""

    def _repair_table_claims(self, output: str) -> str:
        """แปลงเซลล์ตารางทีไม่มีหลักฐาน เป็น no-evidence marker."""
        relevant = getattr(self, "_last_relevant_annotations", []) or []
        rel_map: dict[str, dict[str, Any]] = {}
        for a in relevant:
            key = (a.get("url") or "").lower().rstrip("/")
            if key:
                rel_map[key] = a

        ctx = getattr(self, "_relevance_context", None) or {}
        competitor_names = [n.lower() for n in ctx.get("competitor_names", []) if n]
        thai_markers = ("บาท", "฿", "thb", "thailand", "ไทย", "ประเทศไทย", "shopee", "lazada")
        citation_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
        new_lines = output.splitlines()

        i = 0
        while i < len(new_lines):
            line = new_lines[i]
            if "|" in line and not line.startswith("#"):
                # หา header row ของตารางนี้
                header_cells = self._split_table_row(line)
                comp_col_indices = [
                    j for j, c in enumerate(header_cells)
                    if j >= 2 and any(name in c.lower() for name in competitor_names)
                ]
                if comp_col_indices:
                    i += 1
                    # ข้าม separator
                    while i < len(new_lines) and re.match(r"^\s*\|[-:\s|]+\|\s*$", new_lines[i]):
                        i += 1
                    while i < len(new_lines) and "|" in new_lines[i] and not new_lines[i].startswith("#"):
                        rcells = self._split_table_row(new_lines[i])
                        modified = False
                        for cidx in comp_col_indices:
                            if cidx >= len(rcells):
                                continue
                            cell = rcells[cidx]
                            if self._is_no_evidence_marker(cell):
                                continue
                            if not self._has_factual_signal(cell):
                                continue
                            urls = [u.lower().rstrip("/") for _, u in citation_pattern.findall(cell)]
                            valid = False
                            for u in urls:
                                a = rel_map.get(u)
                                if not a:
                                    continue
                                if a.get("_relevance", {}).get("relevance_type") != "competitor":
                                    continue
                                src_text = f"{a.get('url','')} {a.get('title','')} {a.get('content','')}".lower()
                                header_name = header_cells[cidx].lower() if cidx < len(header_cells) else ""
                                if not any(c in src_text for c in competitor_names if c in header_name):
                                    continue
                                if any(m in cell.lower() for m in thai_markers):
                                    if a.get("_relevance", {}).get("geography") != "thailand":
                                        continue
                                valid = True
                                break
                            if not valid:
                                rcells[cidx] = "*ไม่มีหลักฐานยืนยัน*"
                                modified = True
                        if modified:
                            new_lines[i] = "| " + " | ".join(rcells) + " |"
                        i += 1
                    continue
            i += 1

        return "\n".join(new_lines)

    def _full_analysis_quality(self, output: str) -> dict[str, int]:
        """นับ coverage ของ competitor facts ทีมี inline citation ในตาราง."""
        relevant = getattr(self, "_last_relevant_annotations", []) or []
        rel_map: dict[str, dict[str, Any]] = {}
        for a in relevant:
            key = (a.get("url") or "").lower().rstrip("/")
            if key:
                rel_map[key] = a

        ctx = getattr(self, "_relevance_context", None) or {}
        competitor_names = [n.lower() for n in ctx.get("competitor_names", []) if n]
        thai_markers = ("บาท", "฿", "thb", "thailand", "ไทย", "ประเทศไทย", "shopee", "lazada")
        citation_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

        quality = {
            "competitor_cells_total": 0,
            "no_evidence_cells": 0,
            "competitor_fact_cells": 0,
            "cited_fact_cells": 0,
            "thai_fact_cells": 0,
            "uncited_fact_cells": 0,
            "has_fallback_dump": False,
            "competitor_columns": 0,
        }

        if "**แหล่งอ้างอิง (fallback):**" in output:
            quality["has_fallback_dump"] = True

        lines = output.splitlines()
        idx = 0
        while idx < len(lines):
            line = lines[idx]
            idx += 1
            if not ("|" in line and not line.startswith("#")):
                continue
            if re.match(r"^\s*\|[-:\s|]+\|\s*$", line):
                continue
            cells = self._split_table_row(line)
            comp_col_indices = [
                j for j, c in enumerate(cells)
                if j >= 2 and any(name in c.lower() for name in competitor_names)
            ]
            if not comp_col_indices:
                continue
            quality["competitor_columns"] = max(quality["competitor_columns"], len(comp_col_indices))
            # data rows follow
            while idx < len(lines) and "|" in lines[idx] and not lines[idx].startswith("#"):
                if re.match(r"^\s*\|[-:\s|]+\|\s*$", lines[idx]):
                    idx += 1
                    continue
                rcells = self._split_table_row(lines[idx])
                for cidx in comp_col_indices:
                    if cidx >= len(rcells):
                        continue
                    quality["competitor_cells_total"] += 1
                    cell = rcells[cidx]
                    if self._is_no_evidence_marker(cell):
                        quality["no_evidence_cells"] += 1
                        continue
                    if not self._has_factual_signal(cell):
                        continue
                    quality["competitor_fact_cells"] += 1
                    urls = [u.lower().rstrip("/") for _, u in citation_pattern.findall(cell)]
                    valid = False
                    thai_valid = False
                    for u in urls:
                        a = rel_map.get(u)
                        if not a:
                            continue
                        if a.get("_relevance", {}).get("relevance_type") != "competitor":
                            continue
                        src_text = f"{a.get('url','')} {a.get('title','')} {a.get('content','')}".lower()
                        header_name = cells[cidx].lower() if cidx < len(cells) else ""
                        if not any(c in src_text for c in competitor_names if c in header_name):
                            continue
                        if any(m in cell.lower() for m in thai_markers):
                            if a.get("_relevance", {}).get("geography") != "thailand":
                                continue
                            thai_valid = True
                        valid = True
                    if valid:
                        quality["cited_fact_cells"] += 1
                        if thai_valid:
                            quality["thai_fact_cells"] += 1
                    else:
                        quality["uncited_fact_cells"] += 1
                idx += 1

        return quality

    def _remove_fallback_citation_dump(self, output: str) -> str:
        """ตัด section แหล่งอ้างอิง (fallback) ท้ายรายงาน."""
        lines = output.splitlines()
        cutoff = None
        for i, line in enumerate(lines):
            if "แหล่งอ้างอิง" in line.lower() and "fallback" in line.lower():
                cutoff = i
                break
        if cutoff is None:
            return output
        # ถ้าข้างบนเป็น `---` ให้ตัดทิ้งด้วย
        start = cutoff
        if cutoff > 0 and lines[cutoff - 1].strip() == "---":
            start = cutoff - 1
        return "\n".join(lines[:start])

    def _validate_claim_to_source(self, output: str) -> str:
        """ตรวจ claim-to-source contract: ทุก external factual claim ต้องมี selected evidence ทีรองรับ."""
        if "**limited_analysis: true**" in output:
            return ""

        relevant = getattr(self, "_last_relevant_annotations", []) or []
        if not relevant:
            return "no selected evidence"

        # ตรวจตารางก่อน (per-cell citation)
        table_err = self._validate_table_claims(output)
        if table_err:
            return table_err

        # สร้าง map URL -> annotation สำหรับ citation ทีผ่าน relevance
        rel_map: dict[str, dict[str, Any]] = {}
        for a in relevant:
            key = (a.get("url") or "").lower().rstrip("/")
            if key:
                rel_map[key] = a

        ctx = getattr(self, "_relevance_context", None) or {}
        target_model = ctx.get("target_model", "")
        competitor_names = [n.lower() for n in ctx.get("competitor_names", []) if n]
        thai_markers = ("บาท", "฿", "thb", "thailand", "ไทย", "ประเทศไทย", "shopee", "lazada")

        citation_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

        for line in output.splitlines():
            line_lower = line.lower()
            # ข้าม headings, ตาราง, section ข้อมูลต้นทาง, และรายการ spec ของสินค้าเรา
            if re.match(r"^#{1,3}\s", line) or "|" in line or line.startswith("- สินค้า:") or line.startswith("- รหัสสินค้า:"):
                continue

            urls = [u.lower().rstrip("/") for _, u in citation_pattern.findall(line)]
            # หา competitor ทีปรากฏในบรรทัด
            line_competitors = [n for n in competitor_names if n in line_lower]
            # ถ้ามี competitor ในบรรทัดต้องมี competitor-specific source (เฉพาะ factual claim)
            if line_competitors and self._has_factual_signal(line):
                if not urls:
                    return f"competitor claim without citation: {line.strip()[:80]}"
                for u in urls:
                    a = rel_map.get(u)
                    if not a:
                        return f"unverified citation for competitor claim: {u}"
                    if a.get("_relevance", {}).get("relevance_type") != "competitor":
                        return f"competitor claim cites non-competitor source: {u}"
                    # ตรวจว่า source ระบุ competitor จริง
                    src_text = f"{a.get('url','')} {a.get('title','')} {a.get('content','')}".lower()
                    if not any(c in src_text for c in line_competitors):
                        return f"competitor source does not mention the same competitor: {u}"

            # Thai price / distribution / availability claim ต้องมี Thailand-specific source
            # ห้ามบังคับ citation กับ scope/intro/recommendation ทีมีคำว่า ไทย แต่ไม่มี factual signal
            if any(m in line_lower for m in thai_markers) and self._has_factual_signal(line):
                if self._is_scope_or_recommendation(line) and not urls:
                    # ถ้าเป็น scope/recommendation ไม่มี citation ก็ไม่ถือเป็น factual claim
                    continue
                if not urls:
                    return f"thai market claim without citation: {line.strip()[:80]}"
                for u in urls:
                    a = rel_map.get(u)
                    if not a:
                        return f"unverified citation for thai claim: {u}"
                    if a.get("_relevance", {}).get("geography") != "thailand":
                        return f"thai market claim cites non-thai source: {u}"

        return ""

    def _web_search_tool_status(self) -> str:
        """ระบุสถานะ web-search tool ครั้งล่าสุด."""
        raw_count = getattr(self, "_last_raw_annotations_count", 0)
        relevant = getattr(self, "_last_relevant_annotations", []) or []
        tool_use = getattr(self, "_last_tool_use", {}) or {}
        executed = tool_use.get("tool_calls_executed") or tool_use.get("web_search_requests") or 0

        if not executed:
            return "tool_not_invoked"
        if raw_count == 0:
            return "tool_invoked_no_results"
        if not relevant:
            return "evidence_rejected_by_relevance_gate"
        return "tool_invoked_with_relevant_annotations"

    def validate_output(self, output: str) -> tuple[bool, str]:
        # Evidence-mode: generated output is JSON ResearchResponse (skip when BaseAgent validation is disabled)
        if getattr(self, "_skip_agent_validation", False):
            return True, ""

        # Evidence-mode final Markdown: the CompetitorReportRenderer already
        # validated evidence provenance, structure, and geography.  Skip the
        # legacy web search guards (which use substring matching that conflicts
        # with the renderer's provenance-based validation).  Only run basic
        # structural validation (length, format).
        if getattr(self, "_evidence_mode", False):
            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                pass
            else:
                for key in ("target_model", "competitor_names", "evidence",
                            "evidence_based_recommendations", "strategic_hypotheses", "uncertainty"):
                    if key not in data:
                        return False, f"structural_output_failed: missing field {key}"
                for ev in data.get("evidence", []):
                    for k in ("competitor", "field", "claim", "url", "geography"):
                        if k not in ev:
                            return False, f"structural_output_failed: evidence missing {k}"
                for rec in data.get("evidence_based_recommendations", []):
                    for k in ("text", "supporting_evidence_urls"):
                        if k not in rec:
                            return False, f"structural_output_failed: evidence_based_recommendations missing {k}"
                for h in data.get("strategic_hypotheses", []):
                    for k in ("text", "rationale"):
                        if k not in h:
                            return False, f"structural_output_failed: strategic_hypotheses missing {k}"
                return True, ""

            # Rendered Markdown from CompetitorReportRenderer — the renderer
            # already controls structure (table, recommendations, uncertainty).
            # Skip BaseAgent's structural quality checks (min chars, citation
            # ratio) which conflict with the renderer's deterministic output.
            # Only accept failure markers from the agent itself.
            if "**required_search_failed: true**" in output or "**structural_output_failed: true**" in output:
                return True, ""

            return True, ""

        ok, err = super().validate_output(output)
        if not ok:
            return ok, err

        web_search = getattr(self, "_web_search_enabled", False)
        thin = getattr(self, "_thin_competitor_context", False)

        # ถ้าเป็น failure marker ทีสร้างโดย agent ผ่านเลย
        if "**required_search_failed: true**" in output or "**structural_output_failed: true**" in output:
            return True, ""

        # Guard 1: ไม่แต่ง spec คู่แข่งเมื่อไม่มีข้อมูลและไม่ค้น
        if not web_search and thin:
            fabricate = self._has_fabricated_competitor_specs(output)
            if fabricate:
                return False, f"fabricated competitor spec: {fabricate}"

        if web_search:
            # Guard 2a: ยังตรวจ citation อ่อนก่อน (เช่น homepage / ไม่อยู่ใน relevant set)
            homepage = self._has_homepage_citations(output)
            if homepage:
                return False, f"weak citation: {homepage}"

            # Guard 2b: ถ้า web search แล้วไม่มี selected evidence ต้องกลายเป็น limited
            relevant = getattr(self, "_last_relevant_annotations", []) or []
            is_limited = "**limited_analysis: true**" in output
            if not relevant and not is_limited:
                return False, "no selected evidence: web search returned no product-specific sources"

            # Guard 2c: claim-to-source contract
            claim_err = self._validate_claim_to_source(output)
            if claim_err:
                return False, f"claim-to-source mismatch: {claim_err}"

            # Guard 2d: สำหรับ required mode ถ้าไม่มี evidence ต้องระบุเหตุผล
            mode = self.config.get("web_search_mode", "optional")
            if mode == "required" and not is_limited:
                tool_status = self._web_search_tool_status()
                if tool_status != "tool_invoked_with_relevant_annotations":
                    return False, f"required web search failed: {tool_status}"

        return True, ""

    def _remove_uncited_claim_line(self, output: str, error: str) -> str:
        """ตัด claim ทีไม่มีหลักฐานออกโดยไม่ทำลาย output ทั้งฉบับ."""
        # token คือบรรทัด หรือ URL ทีอยู่หลัง `: ` สุดท้ายใน error message
        parts = error.rsplit(": ", 1)
        mode = self.config.get("web_search_mode", "optional")
        if len(parts) < 2:
            return self._required_search_failure(error) if mode == "required" else self._limited_analysis_fallback()
        token = parts[-1].strip().lower()
        if not token:
            return self._required_search_failure(error) if mode == "required" else self._limited_analysis_fallback()

        lines = output.splitlines()
        for i, line in enumerate(lines):
            line_stripped = line.strip().lower()
            if line_stripped.startswith(token) or token in line_stripped:
                # ตัดบรรทัดนั้นออก รักษาช่วงว่างบรรทัดอื่นไว้
                return "\n".join(lines[:i] + lines[i + 1 :])

        # หาไม่เจอบรรทัดทีตรงกัน → ไม่สามารถซ่อมแบบ targeted ได้
        return self._required_search_failure(error) if mode == "required" else self._limited_analysis_fallback()

    def _repair_output(
        self,
        output: str,
        error: str,
        messages: list,
        response_format: dict | None = None,
    ) -> str:
        thin = getattr(self, "_thin_competitor_context", False)
        web_search = getattr(self, "_web_search_enabled", False)

        if "fabricated competitor spec" in error and thin and not web_search:
            return self._limited_analysis_fallback()

        # ถ้า claim-to-source หรือ weak citation ล้มเหลว ตัด/แปลงก่อน
        if web_search:
            if "table cell" in error:
                return self._repair_table_claims(output)
            if "fallback citation dump" in error:
                return self._remove_fallback_citation_dump(output)
            if "claim-to-source mismatch:" in error or "weak citation:" in error:
                return self._remove_uncited_claim_line(output, error)

        if web_search:
            # structural/quality error แยกจาก search/evidence error
            if self._is_structural_quality_error(error):
                if self._has_enough_content(output):
                    return self._format_output_structure(output)
                return self._structural_output_failure(error)

            # สำหรับ web search ถ้าไม่ใช่ optional ให้คืน required-failure ชัดเจน
            mode = self.config.get("web_search_mode", "optional")
            if mode == "required":
                return self._required_search_failure(error)
            # optional → คืน limited ตามเดิม
            return self._limited_analysis_fallback()

        return super()._repair_output(output, error, messages, response_format)

    def _limited_analysis_fallback(self) -> str:
        """คืนรายงาน limited analysis แบบ deterministic เมื่อข้อมูลไม่พอ."""
        spec = getattr(self, "_product_spec", "")
        model = getattr(self, "_target_model", "สินค้า") or "สินค้า"
        competitors = getattr(self, "_competitor_names", [])
        comp_section = ", ".join(competitors) if competitors else "ยังไม่ระบุ"

        lines = [
            f"# รายงานวิเคราะห์จำกัด: {model}",
            "",
            "**limited_analysis: true**",
            "",
            "## สรุปสินค้าของเรา",
            "",
            "ข้อมูลต้นทางที่มีชัดเจน:",
            "",
        ]
        for s in spec.splitlines():
            if s.strip():
                lines.append(f"- {s.strip()}")
        lines.extend([
            "",
            "## ข้อจำกัดของรายงานนี้",
            "",
            f"- ข้อมูลคู่แข่งที่ได้รับ: {comp_section}",
            "- ข้อมูลคู่แข่งยังไม่มีรายละเอียดทางเทคนิค, ราคา, หรือ feature ที่เชื่อถือได้",
            "- จึงไม่สามารถทำการเปรียบเทียบเชิงปริมาณหรือสรุปจุดแข็ง/จุดอ่อนสัมพัทธ์ได้",
            "",
            "## สิ่งที่ต้องขอเพิ่มเพื่อทำวิเคราะห์เต็มรูปแบบ",
            "",
            "- สเปคเฉพาะของแต่ละคู่แข่ง (หน้าจอ, CPU, เซนเซอร, แบต, กันน้ำ)",
            "- ราคาขายปลีก/ต้นทุนที่มีหลักฐาน",
            "- ช่องทางจำหน่ายในประเทศเป้าหมาย",
            "- รีวิวหรือข้อมูลผู้ใช้งานจริง",
            "",
            "## ตำแหน่งเบื้องต้นทีอธิบายได้จากข้อมูลทีมี",
            "",
            f"- {model} มีสเปคตามทีระบุข้างต้น",
            "- การเปรียบเทียบกับคู่แข่งจะทำได้เมื่อได้รับข้อมูลคู่แข่งเพิ่มเติมครบถ้วน",
            "",
            "*หมายเหตุ: รายงานนี้ไม่มีการสร้างหรืออนุมานข้อมูลคู่แข่งเอง*",
        ])
        return "\n".join(lines)

    def _is_structural_quality_error(self, error: str) -> bool:
        """ structural/quality error ไม่ใช่ evidence/search error."""
        return any(
            k in error
            for k in (
                "structural analysis blocks",
                "output สั้นเกินไป",
                "เนื้อหาทีไม่ใช่ citations",
                "output มากกว่าครึ่วเป็น citations/URLs",
            )
        )

    def _has_enough_content(self, output: str) -> bool:
        """ตรวจว่า output มีเนื้อหาเนื้อแท้พอจะจัด section ได้หรือไม่."""
        quality = self.config.get("output_quality") or {}
        min_non = quality.get("min_non_citation_chars", 0)
        stripped = _strip_citations_and_urls(output)
        non_citation = len(stripped.replace(" ", "").replace("\n", ""))
        return non_citation >= min_non

    def _format_output_structure(self, output: str) -> str:
        """จัด output ทีมีเนื้อหาอยู่แล้วให้มี section ครบ โดยไม่แต่ง factual claim."""
        relevant = getattr(self, "_last_relevant_annotations", []) or []
        competitor_names = [n.lower() for n in (getattr(self, "_competitor_names", []) or [])]
        target_model = (getattr(self, "_target_model", "") or "สินค้า").upper()

        # สรุปชื่อคู่แข่งทีมีหลักฐาน ไม่แต่ง fact
        thai_matched: set[str] = set()
        global_matched: set[str] = set()
        for a in relevant:
            src = f"{a.get('url', '')} {a.get('title', '')} {a.get('content', '')}".lower()
            for name in competitor_names:
                if name in src:
                    geo = a.get("_relevance", {}).get("geography", "global")
                    if geo == "thailand":
                        thai_matched.add(name)
                    else:
                        global_matched.add(name)

        thai_list = ", ".join(sorted(thai_matched)) if thai_matched else "-"
        global_list = ", ".join(sorted(global_matched)) if global_matched else "-"

        extra = [
            "",
            "## ภาพรวมคู่แข่งและหลักฐาน",
            "",
            "หลักฐานทีผ่าน relevance gate แบ่งตามภูมิภาคดังนี้:",
            f"- ประเทศไทย: {thai_list}",
            f"- ตลาดโลก: {global_list}",
            "",
            "## ข้อเสนอแนะสำหรับการวิเคราะห์ต่อ",
            "",
            f"- ใช้ข้อมูลจาก {target_model} เท่านั้น",
            "- สร้างสเปค ราคา หรือช่องทางจำหน่ายจาก official/authorized source",
            "- หากไม่พบข้อมูลของรุ่นใด ให้ระบุว่าไม่พบแทนการแต่งขึ้น",
        ]
        return output.rstrip() + "\n" + "\n".join(extra)

    def _structural_output_failure(self, error: str) -> str:
        """คืนรายงานทีบอกว่า evidence ผ่านแต่ output structure ไม่ตรง contract."""
        model = (getattr(self, "_target_model", "") or "สินค้า").upper()
        tool_status = self._web_search_tool_status()
        return "\n".join([
            f"# รายงานวิเคราะห์ไม่สำเร็จ: {model}",
            "",
            "**structural_output_failed: true**",
            "",
            "## สถานะการค้นหา",
            "",
            f"- tool_status: {tool_status}",
            f"- สาเหตุ: {error}",
            "",
            "## สาเหตุเบื้องต้น",
            "",
            "- web search เรียกสำเร็จและมี selected evidence",
            "- แต่ model สร้าง output ทีไม่ครบ structural contract",
            "",
            "## ข้อแนะนำ",
            "",
            "- ตรวจ prompt ให้บังคับ section วิเคราะห์อย่างน้อย 2 blocks",
            "- หรือใช้ deterministic formatter ก่อนส่ง",
            "- ตรวจสอบว่า output มีเนื้อหาเพียงพอก่อนส่ง",
            "",
            "*หมายเหตุ: รายงานนี้ไม่สามารถสร้างได้เนื่องจากรูปแบบ output ไม่ตรงเงื่อนไข*",
        ])

    def _required_search_failure(self, error: str) -> str:
        """คืนรายงานทีบอกว่า required web search ล้มเหลว และระบุสาเหตุ."""
        model = getattr(self, "_target_model", "สินค้า") or "สินค้า"
        # ดึง tool status จากสถานะจริงของ web search ครั้งล่าสุด
        tool_status = self._web_search_tool_status()

        return "\n".join([
            f"# รายงานวิเคราะห์ไม่สำเร็จ: {model}",
            "",
            "**required_search_failed: true**",
            "",
            "## สถานะการค้นหา",
            "",
            f"- tool_status: {tool_status}",
            f"- สาเหตุ: {error}",
            "",
            "## สาเหตุเบื้องต้น",
            "",
            "- การค้นหาข้อมูลบนเว็บไม่สามารถดำเนินการได้ตามทีต้องการ",
            "- อาจเกิดจาก model ไม่ได้เรียก search tool, search ไม่พบผล, หรือผลทีได้ไม่ผ่าน relevance gate",
            "",
            "## ข้อแนะนำ",
            "",
            "- ลองเปลี่ยน model หรือ search mechanism",
            "- ตรวจสอบว่า model รองรับและเลือกใช้ openrouter:web_search tool จริง",
            "",
            "*หมายเหตุ: รายงานนี้ไม่สามารถสร้างได้เนื่องจากต้องการข้อมูลจากเว็บแบบ required*",
        ])

    def _append_citations_and_verify(self, output: str, annotations: list[dict[str, Any]]) -> str:
        """แปะ citation และทำความสะอาด inline citations ทีไม่ผ่าน relevance."""
        # บันทึก evidence ทั้งหมดเพื่อ diagnostic
        self._last_annotations = list(annotations)
        self._reassess_all_annotations()

        # เรียก base เพื่อแปะ fallback citations ตาม policy (ถ้ามี inline แล้ว base จะไม่แปะ)
        if not getattr(self, "_evidence_mode", False):
            output = super()._append_citations_and_verify(output, annotations)
            # ลบ inline citations ทีไม่อยู่ใน relevant set ออกก่อนส่งต่อ
            output = self._sanitize_citations(output)
        # เก็บ raw tool state สำหรับ validate ครังหลัง
        self._last_raw_annotations_count = getattr(self.llm, "_last_raw_annotations_count", 0)
        raw = getattr(self.llm, "_last_raw_response", {}) or {}
        self._last_tool_use = (raw.get("usage") or {}).get("server_tool_use_details", {})
        # ถ้า output กลายเป็นว่างหลัง sanitize ให้ fallback ตาม mode
        if self.config.get("web_search") and not output.strip():
            if self.config.get("web_search_mode") == "required":
                return self._required_search_failure("required web search failed: tool_not_invoked")
            return self._limited_analysis_fallback()
        return output

    def _sanitize_citations(self, output: str) -> str:
        """ลบ markdown link ทีอ้างอิง source ทีไม่ผ่าน relevance ออก พร้อมลบ claim ทีไม่มีหลักฐาน."""
        allowed = {a.get("url", "").lower().rstrip("/") for a in self._last_relevant_annotations}
        pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

        lines: list[str] = []
        for line in output.splitlines():
            urls = pattern.findall(line)
            if not urls:
                lines.append(line)
                continue
            has_allowed = any(url.lower().rstrip("/") in allowed for _, url in urls)
            if not has_allowed:
                # บรรทัดนี้มีแต่ citation ไม่ผ่าน → ลบทั้งบรรทัด (claim ไม่มีหลักฐาน)
                continue
            # ถ้ามี citation ผ่านปนอยู่ ลบแค่ link ทีไม่ผ่าน โดยไม่ทิ้ง placeholder
            def _clean(match):
                url = match.group(2)
                if url.lower().rstrip("/") in allowed:
                    return match.group(0)
                return ""
            cleaned = pattern.sub(_clean, line).strip()
            if cleaned:
                lines.append(cleaned)
        return "\n".join(lines)

    def _detect_geography(self, source_text: str, url: str) -> str:
        """ตรวจภูมิศาสตร์ของ source จาก URL และเนื้อหา."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        netloc = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
        if ".co.th" in netloc:
            return "thailand"
        if "thailand" in source_text or "thai" in source_text or "ไทย" in source_text or "ประเทศไทย" in source_text:
            return "thailand"
        if re.search(r"\bth\b", f"{netloc} {path}"):
            return "thailand"
        return "global"

    def _is_homepage_or_marketplace_root(self, url: str) -> bool:
        """ตรวจว่า URL เป็น homepage หรือ language root (เช่น /, /th, /th/)."""
        try:
            from urllib.parse import urlparse
        except ImportError:
            return False
        parsed = urlparse(url)
        path = (parsed.path or "").lower().rstrip("/")
        # path วว่าง/หลัก หรือรหัสภาษา 2 ตัว
        if not path or path in ("", "/"):
            return True
        if re.fullmatch(r"/[a-z]{2}", path) or re.fullmatch(r"[a-z]{2}", path):
            return True
        return False

    def _source_mentions_product(self, source_text: str) -> bool:
        """ตรวจว่า title/URL/content มีชื่อ target model หรือคู่แข่งหรือไม่."""
        ctx = getattr(self, "_relevance_context", None) or {}
        target_model = ctx.get("target_model", "")
        competitor_names = ctx.get("competitor_names", [])
        if target_model and re.search(r"\b" + re.escape(target_model.lower()) + r"\b", source_text):
            return True
        for name in competitor_names:
            if name and re.search(re.escape(name.lower()), source_text):
                return True
        return False
