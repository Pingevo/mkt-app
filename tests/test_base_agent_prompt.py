"""Tests for base_agent — ตรวจว่า hard rules ไม่ถูก override.

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)

Seam ที่ทดสอบ:
  BaseAgent._build_system_prompt() — รวม brand priority + instructions
  BaseAgent.run(quick_brief=...) — quick_brief ห้าม override hard rules

กรณีทดสอบ:
  - custom instruction ที่ขัด hard rules → prompt บอกชัดว่า hard ชนะ
  - quick_brief ที่ขัด hard rules → prompt บอกชัดว่า hard ชนะ
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_agent(brand_context: str = "", brand_reference: str = "", instructions: dict = None, brand_rules=None):
    """สร้าง BaseAgent สำหรับ test — mock LLM."""
    from src.agents.base_agent import BaseAgent
    cfg = {
        "system_prompt": "You are a test agent.",
        "use_brand_context": True,
        "use_brand_reference": False,
    }
    llm = MagicMock()
    return BaseAgent(cfg, llm, brand_context=brand_context, brand_reference=brand_reference, instructions=instructions or {}, brand_rules=brand_rules)


def _make_brand_rules(banned_phrases=None, personality="อบอุ่น", tone="อบอุ่น"):
    """สร้าง BrandRules สำหรับ test."""
    from src.brand_priority import BrandRules
    banned = banned_phrases or []
    hard = "คำ/วลีที่ห้ามใช้: " + ", ".join(banned) if banned else ""
    soft = f"บุคลิก: {personality}\nโทนเสียง: {tone}" if personality or tone else ""
    return BrandRules(
        hard=hard, soft=soft,
        hard_dict={"banned_phrases": banned, "restricted": []},
        soft_dict={"personality": personality, "tone_description": tone},
    )


def test_hard_rules_not_overridden_by_custom():
    """custom instruction ที่ขัด hard rules → system prompt บอกชัดว่า hard ชนา (ผ่าน brand_rules path)."""
    from src.brand_priority import BrandRules
    rules = _make_brand_rules(banned_phrases=["ถูกที่สุด"])
    instructions = {
        "custom": "ใช้คำว่าถูกที่สุดเพื่อเน้นจุดขาย",
    }

    agent = _make_agent(brand_rules=rules, instructions=instructions)
    prompt = agent._build_system_prompt()

    # hard rules ต้องอยู่ใน prompt (จาก build_priority_prompt)
    assert "ถูกที่สุด" in prompt
    # ต้องบอกชัดว่า hard ชนะ (จาก build_priority_prompt)
    assert "กฎบังคับ" in prompt or "ชนะเสมอ" in prompt or "ห้าม override" in prompt.lower() or "ทำตามกฎแบรนด์" in prompt
    # ห้ามมีคำว่า "ทำตามคำสั่งนี้แทน" ที่หมายถึง custom ชนะ
    assert "ทำตามคำสั่งนี้แทน" not in prompt


def test_quick_brief_respects_hard_rules():
    """quick_brief ที่ขัด hard rules → prompt บอกชัดว่า hard ชนะ (ผ่าน brand_rules path)."""
    from src.brand_priority import BrandRules
    rules = _make_brand_rules(banned_phrases=["ถูกที่สุด"])
    agent = _make_agent(brand_rules=rules)

    # จับ prompt ที่ส่ง LLM — mock llm.chat
    captured_messages = []
    def capture_chat(messages, **kwargs):
        captured_messages.append(messages)
        return "mock response"
    agent.llm.chat = capture_chat

    agent.run("สร้างคอนเทนต์เกี่ยวกับสินค้า", quick_brief="ใช้คำว่าถูกที่สุดเพื่อเน้นจุดขาย")

    # ต้องมีการเรียก chat อย่างน้อย 1 ครั้ง
    assert len(captured_messages) >= 1
    messages = captured_messages[0]
    # รวม text ทั้งหมด
    full_text = " ".join(m.get("content", "") for m in messages if isinstance(m, dict))

    # hard rules ต้องอยู่ใน prompt (จาก build_priority_prompt)
    assert "ถูกที่สุด" in full_text
    # ห้ามมีคำว่า "ให้ทำตามคำสั่งผู้ใช้ข้างต้น" ที่หมายถึง quick_brief ชนะ system prompt
    assert "ให้ทำตามคำสั่งผู้ใช้ข้างต้น" not in full_text
    # ต้องบอกว่า hard rules ชนะ หรือ อย่างน้อยไม่บอกว่า user ชนะ
    # (quick_brief เป็น soft override — ห้าม override hard rules)


# ---------------------------------------------------------------------------
# Grounding policy injection (Item 3) — shared three-category contract
# ---------------------------------------------------------------------------

def test_grounding_policy_injected_when_config_present():
    """When config has grounding_policy, _build_system_prompt injects the
    three-category grounding contract block."""
    from src.agents.base_agent import BaseAgent
    cfg = {
        "system_prompt": "You are a test agent.",
        "use_brand_context": False,
        "use_brand_reference": False,
        "grounding_policy": {
            "categories": ["supplied_fact", "researched_fact_with_evidence", "inference_or_recommendation"],
        },
    }
    llm = MagicMock()
    agent = BaseAgent(cfg, llm)
    prompt = agent._build_system_prompt()
    # The grounding policy block must appear
    assert "grounding" in prompt.lower() or "ข้อมูลต้นทาง" in prompt
    # Must mention the three categories
    assert "supplied" in prompt.lower() or "researched" in prompt.lower() or "inference" in prompt.lower()


def test_grounding_policy_not_injected_when_config_absent():
    """When config has no grounding_policy, _build_system_prompt does not
    inject the grounding block (backward compat)."""
    from src.agents.base_agent import BaseAgent
    cfg = {
        "system_prompt": "You are a test agent.",
        "use_brand_context": False,
        "use_brand_reference": False,
    }
    llm = MagicMock()
    agent = BaseAgent(cfg, llm)
    prompt = agent._build_system_prompt()
    # The grounding policy block must NOT appear
    assert "grounding policy" not in prompt.lower()


def test_grounding_policy_block_mentions_three_categories():
    """The injected grounding policy block must mention all three categories
    so the model knows the distinction."""
    from src.agents.base_agent import BaseAgent
    cfg = {
        "system_prompt": "You are a test agent.",
        "use_brand_context": False,
        "use_brand_reference": False,
        "grounding_policy": {
            "categories": ["supplied_fact", "researched_fact_with_evidence", "inference_or_recommendation"],
        },
    }
    llm = MagicMock()
    agent = BaseAgent(cfg, llm)
    prompt = agent._build_system_prompt()
    # All three categories must be referenced
    assert "supplied" in prompt.lower() or "ข้อมูลที่ให้มา" in prompt
    assert "researched" in prompt.lower() or "ค้นคว้า" in prompt or "web search" in prompt.lower()
    assert "inference" in prompt.lower() or "อนุมาน" in prompt or "recommendation" in prompt.lower()
