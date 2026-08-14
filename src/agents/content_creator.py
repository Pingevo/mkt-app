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
        media_capabilities: str = "",
    ) -> str:
        parts = ["กรุณาสร้าง **1 โพสต์** สำหรับโปรโมทสินค้า ตามรูปแบบใน system prompt"]
        # ตรวจว่าเป็นโหมดรวมหลายสินค้าไหม
        is_multi = "=== สินค้า:" in product_spec and product_spec.count("=== สินค้า:") > 1
        if is_multi:
            parts.append("--- สเปคสินค้า (โหมดรวม: มีหลายรุ่น ให้เลือกรุ่นที่เหมาะกับแคมเปญ/มุมมองที่สุด และโฟกัสที่รุ่นนั้น) ---")
        else:
            parts.append("--- สเปคสินค้า (สินค้าที่จะโปรโมท ใช้รุ่นนี้เท่านั้น) ---")
        parts.append(product_spec)
        if competitor_analysis and competitor_analysis.strip():
            parts.append("--- ผลวิเคราะห์คู่แข่ง ---")
            parts.append(competitor_analysis)
        if campaign_strategy and campaign_strategy.strip():
            parts.append("--- แคมเปญที่วางไว้ ---")
            parts.append(campaign_strategy)

        # Media capabilities grounding — บอก agent ว่า model ที่จะใช้สร้างรูป/วิดีโอทำได้อะไร
        # ถ้าไม่มี (ดึงจาก API ไม่ได้) → ข้าม ไม่บังคับ
        if media_capabilities:
            parts.append(
                f"--- ความสามารถของ model สร้างสื่อ (ต้องเขียน prompt อยู่ในกรอบนี้) ---\n"
                f"{media_capabilities}\n"
                f"--- สิ้นสุดความสามารถ ---\n"
                f"สำคัญ: ตอนเขียน prompt สำหรับ Gen Image หรือ Gen Video "
                f"ให้ระบุ duration, aspect ratio, resolution เฉพาะค่าที่ model รองรับเท่านั้น "
                f"ถ้าต้องการค่าที่ model ทำไม่ได้ ให้เลือกค่าใกล้สุดที่ทำได้"
            )

        parts.append("สร้าง 1 โพสต์ตามรูปแบบที่กำหนดใน system prompt")
        if is_multi:
            parts.append("สำคัญ: เลือก 1 รุ่นจากสเปคข้างต้นมาทำโพสต์ ระบุชัดว่าเป็นรุ่นไหน ห้ามสับสนกับรุ่นอื่นนอกจากที่ให้มา")
        else:
            parts.append("สำคัญ: ใช้สินค้าที่ให้มาในสเปคข้างต้นเท่านั้น ห้ามสับสนกับรุ่นอื่นในแบรนด์เดียวกัน")
        return "\n\n".join(parts)
