"""Agent 3: Campaign Strategy Agent — คิดแคมเปญ + ราคาแนะนำ."""

from __future__ import annotations

from .base_agent import BaseAgent


class CampaignStrategyAgent(BaseAgent):
    agent_name = "campaign_strategy"
    display_name = "นักวางกลยุทธ์แคมเปญ"

    def build_prompt(self, product_spec: str, competitor_analysis: str) -> str:
        return (
            "กรุณาออกแบบแคมเปญโปรโมชันและกำหนดราคาแนะนำ "
            "โดยใช้ข้อมูลดังต่อไปนี้:\n\n"
            "--- สเปคสินค้า ---\n"
            f"{product_spec}\n\n"
            "--- ผลวิเคราะห์คู่แข่ง ---\n"
            f"{competitor_analysis}\n\n"
            "ออกแบบแคมเปญและราคาแนะนำเป็นภาษาไทยตามรูปแบบที่กำหนดใน system prompt"
        )
