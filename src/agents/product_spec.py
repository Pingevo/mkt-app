"""Agent 1: Product Spec Agent — สร้างสเปคสินค้าจากข้อมูลดิบ."""

from __future__ import annotations

from pathlib import Path

from ..file_loader import load_file
from .base_agent import BaseAgent


class ProductSpecAgent(BaseAgent):
    agent_name = "product_spec"
    display_name = "นักวิเคราะห์สินค้า"

    def build_prompt(self, raw_data: str, product_images: list[str] | None = None) -> str:
        prompt = (
            "กรุณาวิเคราะห์ข้อมูลดิบของสินค้าต่อไปนี้ "
            "แล้วสร้างสเปคสินค้าตามรูปแบบที่กำหนด:\n\n"
            "--- ข้อมูลดิบ ---\n"
            f"{raw_data}\n"
            "--- สิ้นสุดข้อมูลดิบ ---\n\n"
        )
        
        if product_images:
            for i, image_path in enumerate(product_images, 1):
                try:
                    image_text = load_file(image_path)
                    prompt += (
                        f"--- ข้อมูลจากรูปภาพสินค้าภาพที่ {i} ---\n"
                        f"{image_text}\n"
                        f"--- สิ้นสุดข้อมูลจากรูปภาพภาพที่ {i} ---\n\n"
                    )
                except Exception as e:
                    prompt += f"[หมายเหตุ: ไม่สามารถอ่านรูปภาพภาพที่ {i} ({image_path}) ได้: {e}]\n\n"
        
        prompt += "สร้างสเปคสินค้าเป็นภาษาไทยตามรูปแบบที่กำหนดใน system prompt"
        return prompt
