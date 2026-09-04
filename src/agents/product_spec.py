"""Agent 1: Product Spec Agent — สร้างสเปคสินค้าจากข้อมูลดิบ."""

from __future__ import annotations

from pathlib import Path

from ..file_loader import load_file
from ..output_validators import normalize_one_page_brief
from .base_agent import BaseAgent


class ProductSpecAgent(BaseAgent):
    agent_name = "product_spec"
    display_name = "นักวิเคราะห์สินค้า"

    def _normalize_quick_brief(self, quick_brief: str) -> str:
        """Translate 'one-page' intent into a concrete compactness target.

        Only activates when the quick_brief explicitly contains one-page
        intent. Normal product_spec requests are unchanged.
        """
        return normalize_one_page_brief(quick_brief)

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
        else:
            # ไม่มีรูป → บอกชัดเพื่อป้องกัน LLM แต่งภาพขึ้นเอง
            prompt += (
                f"--- รูปภาพสินค้า ---\n"
                f"ไม่มีรูปภาพสินค้าส่งมาในรอบนี้ — ห้ามอ้างว่าเห็นรูปภาพจริง "
                f"และห้ามบรรยายลักษณะที่ไม่ได้ระบุในข้อมูล\n"
                f"ถ้า custom instruction ให้ยืนยันเรื่องรูป ให้ระบุว่า 'ไม่เห็นรูปภาพจริง'\n"
                f"--- สิ้นสุดรูปภาพสินค้า ---\n\n"
            )

        # ตรวจ multi-product scopes — ถ้ามีหลาย scope headers ให้สั่งแยกสเปค
        scope_count = raw_data.count("--- ขอบเขตสินค้า ---")
        if scope_count > 1:
            prompt += (
                f"--- หมายเหตุ: หลายสินค้าในข้อมูลดิบ ---\n"
                f"ข้อมูลดิบมี {scope_count} สินค้าแยกกันด้วย scope headers\n"
                f"สร้างสเปคแยกตามแต่ละสินค้า ห้ามปนข้อมูลข้ามรุ่น\n"
                f"แต่ละสเปคต้องใช้เฉพาะข้อมูลใน scope ของสินค้านั้นเท่านั้น\n"
                f"--- สิ้นสุดหมายเหตุ ---\n\n"
            )

        prompt += "สร้างสเปคสินค้าเป็นภาษาไทยตามรูปแบบที่กำหนดใน system prompt"
        return prompt
