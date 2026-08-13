"""Agent 4: Content Creator Agent — สร้าง 1 โพสต์ + prompt รูป/วิดีโอ (optional) + hashtag."""

from __future__ import annotations

from .base_agent import BaseAgent


class ContentCreatorAgent(BaseAgent):
    agent_name = "content_creator"
    display_name = "นักสร้างคอนเทนต์"

    def build_prompt(
        self,
        product_spec: str,
        competitor_analysis: str,
        campaign_strategy: str,
    ) -> str:
        parts = ["กรุณาสร้าง **1 โพสต์** สำหรับโปรโมทสินค้า ตามรูปแบบใน system prompt"]
        parts.append("--- สเปคสินค้า (สินค้าที่จะโปรโมท ใช้รุ่นนี้เท่านั้น) ---")
        parts.append(product_spec)
        if competitor_analysis and competitor_analysis.strip():
            parts.append("--- ผลวิเคราะห์คู่แข่ง ---")
            parts.append(competitor_analysis)
        if campaign_strategy and campaign_strategy.strip():
            parts.append("--- แคมเปญที่วางไว้ ---")
            parts.append(campaign_strategy)
        parts.append("สร้าง 1 โพสต์ตามรูปแบบที่กำหนดใน system prompt")
        parts.append("สำคัญ: ใช้สินค้าที่ให้มาในสเปคข้างต้นเท่านั้น ห้ามสับสนกับรุ่นอื่นในแบรนด์เดียวกัน")
        return "\n\n".join(parts)
