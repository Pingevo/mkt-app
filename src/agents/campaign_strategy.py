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
        target_category = self._derive_target_category(target_text)

        self._relevance_context = {
            "target_model": canonical,
            "product_aliases": product_aliases,
            "competitor_names": [m for m in competitor_models if m],
            "target_text": target_text,
            "target_category": target_category,
        }
        self._selected_evidence_urls = context_urls | explicit_urls

    def _derive_target_category(self, ctx_text: str) -> str:
        for kw in ["smartwatch", "นาฬิกา", "watch", "smart watch"]:
            if kw in ctx_text:
                return "smartwatch"
        for kw in ["kids", "เด็ก", "children"]:
            if kw in ctx_text:
                return "kids"
        return "unknown"

    def _assess_source_relevance(self, annotation: dict[str, Any]) -> dict[str, Any]:
        """Offline relevance gate for web-search annotations.

        Mirrors the Agent 2 evidence contract in spirit: a URL/title/content
        must clearly relate to the product, a named competitor, or the
        product/category before it can be used as a selected citation.
        """
        ctx = self._relevance_context or {}
        target_model = ctx.get("target_model", "")
        product_aliases = {a.lower() for a in (ctx.get("product_aliases") or [])}
        competitor_names = [n.lower() for n in (ctx.get("competitor_names") or [])]
        target_text = ctx.get("target_text", "")
        target_category = ctx.get("target_category", "unknown")

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

        # 4) category / market match
        if target_category == "smartwatch":
            for kw in ["smartwatch", "smart watch", "นาฬิกา", "watch"]:
                if kw in source_text:
                    return {
                        "relevant": True,
                        "relevance_type": "market",
                        "reason": f"source is about {target_category}",
                    }
        if target_category == "kids":
            for kw in ["kids", "children", "เด็ก"]:
                if kw in source_text:
                    return {
                        "relevant": True,
                        "relevance_type": "market",
                        "reason": f"source is about {target_category}",
                    }

        # 5) fallback: if the source text just repeats the user context, consider it relevant
        if target_text and target_text != "unknown" and any(tok in source_text for tok in target_text.split() if len(tok) > 2):
            return {
                "relevant": True,
                "relevance_type": "context_match",
                "reason": "source text matches provided context",
            }

        return {
            "relevant": False,
            "relevance_type": "unknown",
            "reason": "cannot determine relevance from title/URL",
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

    def run(self, *args, **kwargs) -> str:
        """Capture the effective quick_brief (StepRunContext wins) for validation."""
        step = kwargs.get("step_context")
        quick = step.quick_brief if step is not None else kwargs.get("quick_brief")
        if quick is None and len(args) > 1:
            quick = args[1]
        self._last_quick_brief = quick or ""
        return super().run(*args, **kwargs)

    def _validator_instructions(self) -> dict[str, Any]:
        instructions = dict(self.instructions or {})
        instructions["quick_brief"] = self._last_quick_brief

        # Add runtime selected evidence from web-search annotations.
        relevant = list(getattr(self, "_last_relevant_annotations", []) or [])
        annotation_urls = {u.lower().rstrip("/") for u in (a.get("url") for a in relevant) if u}
        # Also keep any selected evidence URLs the caller explicitly passed in.
        explicit_urls = {u.lower().rstrip("/") for u in (instructions.get("selected_evidence_urls") or [])}
        selected = self._selected_evidence_urls | explicit_urls | annotation_urls
        instructions["selected_evidence_urls"] = sorted(selected)

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
