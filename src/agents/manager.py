"""Manager Agent — รับคำสั่งจาก user วิเคราะห์ วางแผน แล้วสั่ง agent ทำงาน."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console

from ..llm_client import LLMClient
from .base_agent import BaseAgent

console = Console()


class ManagerAgent(BaseAgent):
    agent_name = "manager"
    display_name = "Manager"

    def build_prompt(
        self,
        user_message: str,
        products: list[dict[str, Any]],
        conversation_history: list[dict[str, str]] | None = None,
    ) -> str:
        """Build prompt with current data state for the manager to analyze."""
        products_summary = []
        for p in products:
            status_parts = []
            if p.get("raw"):
                status_parts.append(f"ข้อมูลดิบ: {p['raw']}")
            else:
                status_parts.append("ข้อมูลดิบ: ไม่มี")
            if p.get("images"):
                status_parts.append(f"รูปภาพ: {len(p['images'])} รูป")
            if p.get("product_spec"):
                status_parts.append("สเปคสินค้า (ready): มี")
            else:
                status_parts.append("สเปคสินค้า (ready): ยังไม่มี")
            if p.get("competitor"):
                status_parts.append("วิเคราะห์คู่แข่ง (ready): มี")
            else:
                status_parts.append("วิเคราะห์คู่แข่ง (ready): ยังไม่มี")

            products_summary.append(f"  - {p['name']}: {', '.join(status_parts)}")

        products_text = "\n".join(products_summary) if products_summary else "  (ยังไม่มีสินค้า)"

        history_text = ""
        if conversation_history and len(conversation_history) > 1:
            history_lines = []
            for msg in conversation_history[-10:-1]:
                role = "User" if msg["role"] == "user" else "Manager"
                history_lines.append(f"  {role}: {msg['content']}")
            history_text = (
                "\n\n--- ประวัติการสนทนา (ใช้บริบทนี้เพื่อเข้าใจคำสั่งปัจจุบัน) ---\n"
                + "\n".join(history_lines)
                + "\n--- สิ้นสุดประวัติ ---"
            )

        return (
            f"คำสั่งล่าสุดจาก user: {user_message}\n\n"
            f"สินค้าที่มีในระบบ:\n{products_text}"
            f"{history_text}\n\n"
            f"วิเคราะห์คำสั่งล่าสุดของ user โดยใช้บริบทจากประวัติการสนทนาด้วย "
            f"ถ้า user พูดถึงสิ่งที่เคยคุยก่อนหน้านี้ ให้เข้าใจจากบริบท "
            f"แล้วส่งคืนเป็น JSON"
        )

    def parse_response(self, response: str) -> dict[str, Any]:
        """Parse JSON response from manager."""
        try:
            text = response.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)
            return json.loads(text)
        except (json.JSONDecodeError, KeyError) as e:
            return {
                "reply": response,
                "action": "ask",
                "agents": [],
                "product_id": None,
                "missing": [],
                "_parse_error": str(e),
            }
