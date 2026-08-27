"""Agent 3: Campaign Strategy Agent — คิดแคมเปญ + ราคาแนะนำ."""

from __future__ import annotations

from .base_agent import BaseAgent


class CampaignStrategyAgent(BaseAgent):
    agent_name = "campaign_strategy"
    display_name = "นักวางกลยุทธ์แคมเปญ"

    def __init__(self, config: dict, llm_client=None, **kwargs):
        super().__init__(config, llm_client, **kwargs)

    def build_prompt(self, context: dict) -> str:
        """Build the user prompt from a semantic context dict.

        `context` may come from any upstream source — user, competitor agent,
        market agent, etc.  Only `product` is required; everything else is
        enrichment.  This method formats data; it does not reason or decide.
        All policy rules live in the system prompt (agents.yaml) — this method
        does NOT duplicate them.
        """
        product = context.get("product", "")
        if not product.strip():
            raise ValueError("CampaignStrategyAgent ต้องการ product context จึงจะทำงานได้")

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

        return "\n".join(sections)
