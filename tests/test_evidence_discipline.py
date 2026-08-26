"""Evidence discipline tests — ตรวจว่า agent รู้ว่าหลักฐานแบบไหนใช้พิสูจน์ claim แบบไหน

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)

Seam ที่ทดสอบ:
  1. BaseAgent._format_instructions() ต้องแสดง evidence_policy ของ competitor_analysis
  2. BaseAgent._review_and_refine() ต้องตรวจ evidence discipline
  3. ถ้า output มี market_share claim โดยไม่มี market report source → ต้องถูก reject หรือขึ้น warning
  4. ถ้า output มี spec claim จาก official page → ต้องผ่าน
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_agent(instructions: dict):
    from src.agents.base_agent import BaseAgent
    cfg = {
        "system_prompt": "You are a competitor analyst.",
        "model": "test-model",
        "max_retry_limit": 1,
        "max_review_iterations": 1,
        "review_temperature": 0.2,
        "temperature": 0.7,
        "max_tokens": 4096,
    }
    llm = MagicMock()
    return BaseAgent(cfg, llm, instructions=instructions)


def test_format_instructions_includes_evidence_policy():
    """_format_instructions ต้องแสดง evidence_policy ถ้า instructions มี."""
    instructions = {
        "evidence_policy": {
            "market_share": {
                "allowed_sources": ["market_report", "financial_report"],
                "confidence": "HIGH",
            },
            "specification": {
                "allowed_sources": ["official_product_page", "datasheet"],
                "confidence": "HIGH",
            },
        }
    }
    agent = _make_agent(instructions)
    block = agent._format_instructions()

    assert "กฎหลักฐาน (Evidence Policy)" in block
    assert "market_share" in block
    assert "market_report" in block
    assert "official_product_page" in block


def test_review_flags_market_share_without_market_report():
    """_review_and_refine ต้องตรวจจับ market share claim ทีไม่มี market report."""
    instructions = {
        "evidence_policy": {
            "market_share": {
                "allowed_sources": ["market_report", "financial_report"],
                "confidence": "HIGH",
            }
        }
    }
    agent = _make_agent(instructions)

    # mock review model ตอบว่าพบปัญหา
    def fake_review(messages, **kwargs):
        # ถ้า prompt ถามถึง evidence discipline ให้ตอบว่าไม่ผ่าน
        full = " ".join(m.get("content", "") for m in messages)
        if "market share" in full.lower() and "evidence" in full.lower():
            return 'FAIL: "imoo ครองส่วนแบ่งหลัก" ไม่มี market report รองรับ'
        return "PASS"
    agent.llm.chat = fake_review

    output = "imoo และ Xiaomi ครองส่วนแบ่งหลักในตลาด"
    result = agent._review_and_refine(
        output,
        "คุณคือ analyst",
        instruction_block="ตรวจ evidence discipline",
    )

    assert "FAIL" in result or "ไม่มี market report" in result


def test_run_raises_clear_error_when_model_returns_empty():
    """ถ้า model คืน output ว่างตั้งแต่ generate ต้อง raise ชัด ไม่ต้องซ่อม."""
    agent = _make_agent({})
    agent.config["required_output_sections"] = ["ภาพรวมตลาด", "ตารางเปรียบเทียบ"]
    calls = {"count": 0}

    def fake_chat(messages, **kwargs):
        calls["count"] += 1
        if kwargs.get("return_annotations"):
            return "", []
        return ""

    agent.llm.chat = fake_chat
    agent.config["web_search"] = True
    agent.config["max_review_iterations"] = 0

    with pytest.raises(ValueError) as exc:
        agent.run("test prompt")

    # ต้องบอกทันทีว่า model คืนคำตอบว่างเปล่า ไม่ใช่ซ่อมไป 3 รอบ
    assert "model คืนคำตอบว่างเปล่า" in str(exc.value)


def test_review_passes_spec_with_official_source():
    """_review_and_refine ต้องผ่าน spec claim ทีมี official source."""
    instructions = {
        "evidence_policy": {
            "specification": {
                "allowed_sources": ["official_product_page"],
                "confidence": "HIGH",
            }
        }
    }
    agent = _make_agent(instructions)

    def fake_review(messages, **kwargs):
        return "PASS"
    agent.llm.chat = fake_review

    output = "K2 มีกล้อง 5MP [source: official product page]"
    result = agent._review_and_refine(
        output,
        "คุณคือ analyst",
        instruction_block="ตรวจ evidence discipline",
    )

    assert result == "PASS"
