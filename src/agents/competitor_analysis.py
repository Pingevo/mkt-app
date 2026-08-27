"""Agent 2: Competitor Analysis Agent — วิเคราะห์เปรียบเทียบคู่แข่ง."""

from __future__ import annotations

import re
from typing import Any

from .base_agent import BaseAgent


class CompetitorAnalysisAgent(BaseAgent):
    agent_name = "competitor_analysis"
    display_name = "นักวิเคราะห์คู่แข่ง"

    def build_prompt(self, product_spec: str, competitor_data: str) -> str:
        self._relevance_context = self._build_relevance_context(product_spec, competitor_data)
        return (
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

        # 1) obvious category mismatch
        if target_category != "unknown":
            for kw in policy.get("mismatch_keywords", []):
                if re.search(re.escape(kw.lower()), source_text):
                    return {
                        "relevant": False,
                        "relevance_type": "category_mismatch",
                        "reason": f"source mentions '{kw}' which conflicts with target category {target_category}",
                    }

        # 2) subcategory mismatch (kids, children, elderly) ถ้า target ไม่มี
        for kw in policy.get("subcategory_mismatch_keywords", []):
            if kw in source_text and kw not in target_text:
                return {
                    "relevant": None,
                    "relevance_type": "unknown",
                    "reason": f"source mentions '{kw}' which is absent from target context",
                }

        # 3) target product match
        if target_model and re.search(re.escape(target_model.lower()), source_text):
            return {
                "relevant": True,
                "relevance_type": "target",
                "reason": f"source matches target model {target_model}",
            }

        # 4) competitor match
        for name in competitor_names:
            if not name:
                continue
            pattern = re.escape(name.lower())
            if re.search(pattern, source_text):
                return {
                    "relevant": True,
                    "relevance_type": "competitor",
                    "reason": f"source matches competitor {name}",
                }

        # 5) market / category match
        for kw in policy.get("market_keywords", []):
            if re.search(re.escape(kw.lower()), source_text):
                return {
                    "relevant": True,
                    "relevance_type": "market",
                    "reason": f"source is about {target_category} market/category",
                }

        # 6) unknown
        return {
            "relevant": None,
            "relevance_type": "unknown",
            "reason": "cannot confidently determine relevance from title/URL",
        }
