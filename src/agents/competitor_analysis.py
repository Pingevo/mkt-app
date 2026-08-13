"""Agent 2: Competitor Analysis Agent — วิเคราะห์เปรียบเทียบคู่แข่ง."""

from __future__ import annotations

from .base_agent import BaseAgent


class CompetitorAnalysisAgent(BaseAgent):
    agent_name = "competitor_analysis"
    display_name = "นักวิเคราะห์คู่แข่ง"

    def build_prompt(self, product_spec: str, competitor_data: str) -> str:
        return (
            "กรุณาวิเคราะห์เปรียบเทียบสินค้าของเรากับคู่แข่ง "
            "โดยใช้ข้อมูลดังต่อไปนี้:\n\n"
            "--- สเปคสินค้าของเรา ---\n"
            f"{product_spec}\n\n"
            "--- ข้อมูลคู่แข่ง ---\n"
            f"{competitor_data}\n"
            "--- สิ้นสุดข้อมูลคู่แข่ง ---\n\n"
            "วิเคราะห์เปรียบเทียบเป็นภาษาไทยตามรูปแบบที่กำหนดใน system prompt"
        )
