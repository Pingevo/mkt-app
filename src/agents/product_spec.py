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

        # สถาปัตยกรรมใหม่: รูปจริงถูกส่งเป็น multimodal ใน agent.run(image_paths=...)
        # ไม่ต้อง OCR รูปเป็น text แล้ว — บอกแค่ว่ามีรูปประกอบให้ดูประกอบการวิเคราะห์
        if product_images:
            prompt += (
                f"--- รูปภาพสินค้า ---\n"
                f"มีรูปภาพสินค้า {len(product_images)} รูป ประกอบการวิเคราะห์ (ส่งเป็น image input)\n"
                f"--- สิ้นสุดรูปภาพสินค้า ---\n\n"
            )

        prompt += "สร้างสเปคสินค้าเป็นภาษาไทยตามรูปแบบที่กำหนดใน system prompt"
        return prompt
