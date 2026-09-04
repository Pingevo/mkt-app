"""Agent 3: Campaign Strategy Agent — คิดแคมเปญ + ราคาแนะนำ."""

from __future__ import annotations

import re
from typing import Any

from ..campaign_validator import (
    AuditResult,
    audit_campaign_output,
    extract_context_flags,
    validate_campaign_output,
)
from .base_agent import BaseAgent


class CampaignStrategyAgent(BaseAgent):
    agent_name = "campaign_strategy"
    display_name = "นักวางกลยุทธ์แคมเปญ"

    def __init__(self, config: dict, llm_client=None, **kwargs):
        super().__init__(config, llm_client, **kwargs)
        self._context_flags: dict = {}
        self._last_quick_brief: str = ""
        self._relevance_context: dict = {}
        self._selected_evidence_urls: set[str] = set()
        self._selected_evidence: list[dict[str, Any]] = []
        self._requested_competitor_models: set[str] = set()
        self._evidence_confirmed_competitor_models: set[str] = set()

    def build_prompt(self, context: dict) -> str:
        """Build the user prompt from a semantic context dict.

        `context` may come from any upstream source — user, competitor agent,
        market agent, etc.  Only `product` is required; everything else is
        enrichment.  This method formats data; it does not reason or decide.
        All policy rules live in the system prompt (agents.yaml) — this method
        does NOT duplicate them.

        It also builds the runtime selected-evidence manifest and product
        identity contract for deterministic validation.
        """
        product = context.get("product", "")
        if not product.strip():
            raise ValueError("CampaignStrategyAgent ต้องการ product context จึงจะทำงานได้")

        self._context_flags = extract_context_flags(context)
        self._build_relevance_context(context)

        sections = [
            "--- สินค้า (product) ---",
            product,
            "",
            "จากข้อมูลข้างต้น ให้ออกแบบแคมเปญและราคาแนะนำ",
        ]

        for key, label in [
            ("competitors", "ผลวิเคราะห์คู่แข่ง (competitors)"),
            ("market", "ข้อมูลตลาดและเทรนด์ (market)"),
            ("customers", "กลุ่มเป้าหมาย (customers)"),
            ("business", "ข้อมูลทางธุรกิจและงบประมาณ (business)"),
        ]:
            value = context.get(key)
            if value:
                sections.extend([f"--- {label} ---", str(value), ""])

        selected = sorted(self._selected_evidence_urls)
        if selected:
            sections.extend(
                [
                    "--- หลักฐานทีเลือกใช้ (selected evidence URLs) ---",
                    "ให้อ้างอิงเฉพาะ URL ด้านล่างเท่านั้น โดยแปะเป็น [ข้อความ claim](URL) ในบรรทัดเดียวกับ claim",
                ]
                + [f"- {url}" for url in selected]
                + [""]
            )

        return "\n".join(sections)

    def _build_relevance_context(self, context: dict) -> None:
        """Build a product/competitor relevance context and the URL manifest."""
        product = str(context.get("product", ""))
        competitors = str(context.get("competitors", ""))
        market = str(context.get("market", ""))
        customers = str(context.get("customers", ""))

        canonical = str(self._context_flags.get("canonical_product_id", ""))
        product_aliases = set(self._context_flags.get("product_aliases") or [])
        competitor_models = set(self._context_flags.get("competitor_models") or [])
        context_urls = set(self._context_flags.get("context_urls") or [])
        explicit_urls = {
            u.lower().rstrip("/")
            for u in (self.instructions or {}).get("selected_evidence_urls", [])
            if u
        }

        target_text = (product + "\n" + competitors + "\n" + market + "\n" + customers).lower()

        self._relevance_context = {
            "target_model": canonical,
            "product_aliases": product_aliases,
            "competitor_names": [m for m in competitor_models if m],
            "target_text": target_text,
        }
        self._selected_evidence_urls = context_urls | explicit_urls

    def _assess_source_relevance(self, annotation: dict[str, Any]) -> dict[str, Any]:
        """Offline relevance gate for web-search annotations.

        Mirrors the Agent 2 evidence contract in spirit: a URL/title/content
        must clearly relate to the product, a named competitor, or the
        provided context before it can be used as a selected citation.

        Relevance is based on product/competitor identity match and
        structural URL checks — NOT on category keyword lists.
        """
        ctx = self._relevance_context or {}
        target_model = ctx.get("target_model", "")
        product_aliases = {a.lower() for a in (ctx.get("product_aliases") or [])}
        competitor_names = [n.lower() for n in (ctx.get("competitor_names") or [])]

        url = (annotation.get("url") or "").lower()
        title = (annotation.get("title") or "").lower()
        content = (annotation.get("content") or "").lower()
        source_text = f"{url} {title} {content}"

        # 1) obvious non-direct / marketplace root
        if self._is_homepage_or_marketplace_root(url) and not self._source_mentions_product(source_text, product_aliases, target_model):
            return {
                "relevant": False,
                "relevance_type": "homepage",
                "reason": "source is a homepage or marketplace root without product-specific evidence",
            }

        # 2) target product match
        if target_model and re.search(r"\b" + re.escape(target_model.lower()) + r"\b", source_text):
            return {
                "relevant": True,
                "relevance_type": "target",
                "reason": f"source matches target model {target_model}",
            }
        for alias in product_aliases:
            if alias and alias in source_text:
                return {
                    "relevant": True,
                    "relevance_type": "target",
                    "reason": f"source matches product alias {alias}",
                }

        # 3) competitor match
        for name in competitor_names:
            if name and name in source_text:
                return {
                    "relevant": True,
                    "relevance_type": "competitor",
                    "reason": f"source matches competitor {name}",
                }

        # 4) no explicit identity match — let the model decide.
        # Code does NOT build a semantic relevance classifier (no tokenizers,
        # stopword lists, or fuzzy matching). The model sees all non-blocked
        # search results and decides which to cite.
        return {
            "relevant": None,
            "relevance_type": "unknown",
            "reason": "no explicit identity match — model decides",
        }

    def _is_homepage_or_marketplace_root(self, url: str) -> bool:
        """Return True for URLs that look like a homepage or marketplace listing root."""
        if not url:
            return True
        parsed = url.lower().rstrip("/")
        # homepage if no path beyond domain
        if parsed.count("/") <= 2:
            return True
        path = parsed.split("/", 3)[-1]
        if path.startswith("search") or path.startswith("tag") or path.startswith("category") or path.startswith("shop"):
            return True
        if "shopee.co.th" in parsed and "/product/" not in parsed:
            return True
        return False

    def _source_mentions_product(self, source_text: str, product_aliases: set[str], target_model: str) -> bool:
        if target_model and target_model.lower() in source_text:
            return True
        for alias in product_aliases:
            if alias and alias in source_text:
                return True
        return False

    @staticmethod
    def _extract_competitor_tokens(text: str) -> set[str]:
        """Extract model-like competitor tokens a user explicitly asks about.

        Only accepts clear competitor/comparison phrasing (คู่แข่ง / เปรียบเทียบ / competitor).
        """
        if not text:
            return set()
        trigger_phrases = ["คู่แข่ง", "competitor", "เปรียบเทียบ", "compare", "เทียบกับ", "vs", "against"]
        low = text.lower()
        if not any(p in low for p in trigger_phrases):
            return set()
        models: set[str] = set()
        for pat in [r"\b([A-Za-z]{2,}\s+[A-Za-z]*\d[\w]{0,4})\b", r"\b([A-Za-z]+\d[\w]{0,4})\b"]:
            for m in re.finditer(pat, text):
                token = m.group(1).split()[-1].lower().replace(" ", "")
                if token not in {"q1", "q2", "q3", "q4"}:
                    models.add(token)
        return models

    @staticmethod
    def _annotation_confirms_competitor(annotation: dict, models: set[str]) -> bool:
        """Return True if the returned web evidence corroborates a requested competitor."""
        if not models:
            return False
        combined = (annotation.get("title", "") + " " + annotation.get("content", "") + " " + annotation.get("url", "")).lower()
        for m in models:
            # Require at least one full brand-ish token and one model-ish token in evidence.
            if m in combined:
                return True
        return False

    def _on_annotations_ready(self) -> None:
        """Compute effective selected evidence and evidence-confirmed competitor models
        after the live web-search call returns annotations.
        """
        raw = list(getattr(self, "_last_annotations", []) or [])
        # For campaign_strategy, the relevance assessment was already done in _append_citations_and_verify
        # via _last_relevant_annotations; if not, fall back to all raw annotations.
        relevant = list(getattr(self, "_last_relevant_annotations", []) or [])
        if not relevant and raw:
            relevant = raw
        self._selected_evidence = list(relevant)
        self._selected_evidence_urls = {a.get("url", "").lower().rstrip("/") for a in self._selected_evidence if a.get("url")}

        confirmed: set[str] = set()
        for m in self._requested_competitor_models:
            if any(self._annotation_confirms_competitor(a, {m}) for a in self._selected_evidence):
                confirmed.add(m)
        self._evidence_confirmed_competitor_models = confirmed

        self._context_flags["requested_competitor_models"] = sorted(self._requested_competitor_models)
        self._context_flags["evidence_confirmed_competitor_models"] = sorted(self._evidence_confirmed_competitor_models)

    def run(self, *args, **kwargs) -> str:
        """Capture the effective quick_brief (StepRunContext wins) for validation."""
        step = kwargs.get("step_context")
        quick = step.quick_brief if step is not None else kwargs.get("quick_brief")
        if quick is None and len(args) > 1:
            quick = args[1]
        self._last_quick_brief = quick or ""
        self._requested_competitor_models = self._extract_competitor_tokens(self._last_quick_brief)
        # Preserve an explicit caller override; only auto-detect if the caller did not supply one.
        kwargs.setdefault("research_required", self._is_research_required(self._last_quick_brief))
        return super().run(*args, **kwargs)

    def _is_research_required(self, quick_brief: str) -> bool:
        """Return True when the run must fetch current information from the web.

        Triggered by an explicit config flag or by research-related intent in the quick_brief.
        """
        if self.config.get("research_required", False):
            return True
        if not quick_brief:
            return False
        lowered = quick_brief.lower()
        keywords = self.config.get(
            "research_required_keywords",
            [
                "ราคาปัจจุบัน",
                "ตลาดปัจจุบัน",
                "trend ปัจจุบัน",
                "campaign คู่แข่ง",
                "current",
                "now",
                "today",
                "ล่าสุด",
                "ปัจจุบัน",
                "ค้นเว็บ",
                "web search",
            ],
        )
        return any(kw.lower() in lowered for kw in keywords)

    def _validator_instructions(self) -> dict[str, Any]:
        instructions = dict(self.instructions or {})
        instructions["quick_brief"] = self._last_quick_brief

        # Pass the effective selected evidence manifest to validation and repair.
        instructions["selected_evidence"] = list(self._selected_evidence)
        instructions["selected_evidence_urls"] = sorted(self._selected_evidence_urls)
        instructions["requested_competitor_models"] = sorted(self._requested_competitor_models)
        instructions["evidence_confirmed_competitor_models"] = sorted(self._evidence_confirmed_competitor_models)

        return instructions

    def validate_output(self, output: str) -> tuple[bool, str]:
        """Run generic validation then the campaign-specific semantic guardrails."""
        ok, err = super().validate_output(output)
        if not ok:
            return False, err

        rules = self.config.get("semantic_rules", {})
        return validate_campaign_output(output, self._context_flags, self._validator_instructions(), rules)

    def audit_output(self, output: str) -> list[AuditResult]:
        """Return per-rule verdicts for the campaign-specific guardrails."""
        rules = self.config.get("semantic_rules", {})
        return audit_campaign_output(output, self._context_flags, self._validator_instructions(), rules)
