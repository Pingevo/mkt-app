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
        media_type: str = "",
        visual_style: str = "",
        asset_summary: str = "",
        selected_pillar: str = "",
        content_pillars: str = "",
        reference_catalog: list[dict] | None = None,
        brand_context: str = "",
    ) -> str:
        parts = ["กรุณาสร้าง **1 โพสต์** สำหรับโปรโมทสินค้า ตามรูปแบบใน system prompt"]

        # media_type directive — override system prompt ในการตัดสินใจว่าจะสร้าง image/video
        if media_type == "image":
            parts.append("สำคัญ: สร้าง **Prompt สำหรับ Gen Image เท่านั้น** — ห้ามสร้าง Gen Video prompt แม้ว่าแพลตฟอร์มจะเป็น TikTok")
        elif media_type == "video":
            parts.append("สำคัญ: สร้าง **Prompt สำหรับ Gen Video เท่านั้น** — ห้ามสร้าง Gen Image prompt")
        elif media_type == "both":
            parts.append("สำคัญ: ต้องสร้างทั้ง **Prompt สำหรับ Gen Image** และ **Prompt สำหรับ Gen Video**")

        # Visual style hint — high-level style จาก visual.json (keywords เฉพาะแป๊ะที่ media_gen)
        if visual_style:
            parts.append(
                f"--- แนวทางภาพของแบรนด์ (ใช้เป็นแนวทางตอนเขียน image/video prompts) ---\n"
                f"{visual_style}\n"
                f"--- สิ้นสุดแนวทางภาพ ---"
            )
        # Brand context — บริบทแบรนด์/ผู้อ่าน จาก brand_context (audience, tone, positioning)
        if brand_context:
            parts.append(
                f"--- บริบทแบรนด์และผู้อ่าน ---\n"
                f"{brand_context}\n"
                f"--- สิ้นสุดบริบทแบรนด์และผู้อ่าน ---"
            )
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
                f"ถ้าต้องการค่าที่ model ทำไม่ได้ ให้เลือกค่าใกล้สุดที่ทำได้ "
                f"พิจารณาค่าใช้จ่าย: เลือก duration ที่สั้นที่สุดที่ส่งความหมายของ brief ได้เพียงพอ "
                f"ถ้า user ระบุ duration ชัดเจนและ model รองรับ ให้ใช้ค่านั้น"
            )

        # Asset Library — วัตถุดิบแบรนด์ที่ agent เลือกมาใช้ประกอบคอนเทนต์
        if asset_summary:
            parts.append(
                f"--- วัตถุดิบแบรนด์ที่เลือกมา ---\n"
                f"{asset_summary}\n"
                f"--- สิ้นสุดวัตถุดิบแบรนด์ ---\n"
                f"ระบุ asset_ids ที่ใช้ในแต่ละโพสต์"
            )

        # Ordered reference catalog — รูปอ้างอิงทั้งหมดที่จะส่งให้ provider สร้างสื่อ
        # แต่ละรูปมีเลข Reference N (เรียงตามลำดับที่จะส่ง), provenance, filename, และ
        # metadata ของ asset (ถ้ามี) — ไม่มีการตีความหมวดหมู่ของรูปในโค้ด
        if reference_catalog:
            lines = []
            for ref in reference_catalog:
                line = f"- {ref.get('label', '')}: source={ref.get('provenance', '')}"
                if ref.get("filename"):
                    line += f" | file={ref['filename']}"
                if ref.get("asset_id"):
                    line += f" | asset_id={ref['asset_id']}"
                meta = ref.get("metadata") or {}
                if meta:
                    meta_parts = [f"{k}={v}" for k, v in meta.items()]
                    line += f" | {' '.join(meta_parts)}"
                lines.append(line)
            parts.append(
                f"--- รูปอ้างอิงที่จะส่งให้ผู้สร้างสื่อ (ตามลำดับเลข Reference) ---\n"
                f"{chr(10).join(lines)}\n"
                f"--- สิ้นสุดรูปอ้างอิง ---"
            )
            # Generic instruction เดียว — ให้โมเดลเป็นคนตัดสินใจเรื่องความหมายของแต่ละรูป
            parts.append(
                "พิจารณา Quick Brief, บริบทสินค้า, บริบทแบรนด์, และรูปอ้างอิงทุกรูปข้างต้น "
                "ตัดสินใจว่ารูปไหนเกี่ยวข้องและจะใช้แต่ละรูปอย่างไรสำหรับสื่อชิ้นนี้ "
                "เขียน prompt สำหรับผู้สร้างสื่อโดยระบุรูปอ้างอิงด้วยเลข Reference ตามลำดับข้างต้น "
                "เมื่อความตั้งใจของ user และบริบทแสดงว่าต้องรักษาเอกลักษณ์/รายละเอียดภาพของรูปใด "
                "ให้รักษาไว้ ห้ามแทนที่ ออกแบบใหม่ หรือตัดรูปที่ user ขอใช้โดยไม่บอก "
                "ถ้าคำขอไม่พอใส่ในขีดจำกัดของผู้สร้างสื่อโดยเปลี่ยนความตั้งใจของ user "
                "ให้คืนข้อจำกัดที่ชัดเจนแทนการเดา"
            )

        # Content Pillars — optional content-strategy guidance (not mandatory keywords)
        if selected_pillar:
            parts.append(
                f"--- Content Pillar ที่เลือก (ใช้เป็นแนวทางสร้างคอนเทนต์) ---\n"
                f"{selected_pillar}\n"
                f"--- สิ้นสุด Content Pillar ที่เลือก ---"
            )
        if content_pillars:
            parts.append(
                f"--- Content Pillars ทั้งหมด (ใช้เป็นบริบทยุทธศาสตร์เนื้อหาเมื่อเกี่ยวข้อง) ---\n"
                f"{content_pillars}\n"
                f"--- สิ้นสุด Content Pillars ---"
            )
        if selected_pillar or content_pillars:
            parts.append(
                "ใช้ Content Pillar เป็นแนวทางสร้างคอนเทนต์เมื่อเกี่ยวข้อง — "
                "ไม่บังคับให้กล่าวถึงชื่อ Pillar ในผลลัพธ์"
            )

        parts.append("สร้าง 1 โพสต์ตามรูปแบบที่กำหนดใน system prompt")
        if is_multi:
            parts.append("สำคัญ: เลือก 1 รุ่นจากสเปคข้างต้นมาทำโพสต์ ระบุชัดว่าเป็นรุ่นไหน ห้ามสับสนกับรุ่นอื่นนอกจากที่ให้มา")
        else:
            parts.append("สำคัญ: ใช้สินค้าที่ให้มาในสเปคข้างต้นเท่านั้น ห้ามสับสนกับรุ่นอื่นในแบรนด์เดียวกัน")
        return "\n\n".join(parts)
