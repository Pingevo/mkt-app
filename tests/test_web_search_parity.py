"""Web search market parity tests — ทำให้ agent 2/3 ค้นเว็บเทียบเท่า Claude/ChatGPT.

TDD: เขียน test ก่อน (red) → implement ให้ผ่าน (green)

Seam ที่ทดสอบ:
  1. BaseAgent.run() กับ web_search=True ส่ง openrouter:web_search + web_fetch tools ในครั้งเดียว
  2. BaseAgent.run() คืน output พร้อม URL จริงจาก OpenRouter annotations
  3. BaseAgent.run() ส่ง quick_brief ใน user prompt (model ใส่ปีใน query เอง)
  4. BaseAgent.run() verify URL ทีละอันด้วย openrouter:web_fetch ถ้า verify_urls เปิด

ปัญหาที่แก้:
  - ลิงก์กดไปไม่เจอสินค้าจริง → ใช้ URL จาก annotations + verify ด้วย web_fetch
  - ข้อมูลเก่าแม้จำกัดช่วงเวลาใน quick_brief → ส่ง quick_brief ให้ model ค้นเอง
  - ค้นไม่หลายรอบ → ใช้ max_uses ให้ model follow-up เอง
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_agent(agent_cfg: dict = None):
    """สร้าง BaseAgent สำหรับ test ด้วย config ทีอยากได้."""
    from src.agents.base_agent import BaseAgent
    cfg = {
        "system_prompt": "You are a marketing analyst.",
        "web_search": True,
        "model": "test-model",
        "max_retry_limit": 1,
        "max_review_iterations": 0,
        "temperature": 0.7,
        "max_tokens": 4096,
        **(agent_cfg or {}),
    }
    llm = MagicMock()
    return BaseAgent(cfg, llm)


def _make_web_search_cfg():
    return {
        "max_results_detailed": 5,
        "max_uses": 8,
        "max_total_results": 20,
        "engine": "exa",
        "search_context_size": "high",
        "execution_temperature": 0.1,
        "execution_max_tokens": 1500,
        "verify_urls": True,
        "excluded_domains": ["reddit.com"],
    }


def _make_empty_web_search_cfg():
    return {
        "max_results_detailed": 3,
        "execution_temperature": 0.1,
        "execution_max_tokens": 1500,
        "verify_urls": False,
    }


# ============================================================
# Seam 1: run() ส่ง web_search + web_fetch tools ในครั้งเดียว
# ============================================================

def test_run_sends_web_search_and_fetch_tools(monkeypatch):
    """เมื่อ web_search=True, run() ต้องส่งทั้ง openrouter:web_search และ
    openrouter:web_fetch ในครั้งเดียว ไม่เรียก _plan/_execute แยก."""
    agent = _make_agent()
    monkeypatch.setattr("src.agents.base_agent._web_search_cfg", _make_web_search_cfg)

    captured = []
    def fake_chat(messages, **kwargs):
        captured.append({"messages": messages, "kwargs": kwargs})
        return "ผลลัพธ์การวิเคราะห์", []
    agent.llm.chat = fake_chat

    agent.run("วิเคราะห์คู่แข่งของ LAGENIO K2")

    assert len(captured) == 1
    tools = captured[0]["kwargs"].get("tools", [])
    assert len(tools) == 2
    assert tools[0]["type"] == "openrouter:web_search"
    assert tools[1]["type"] == "openrouter:web_fetch"


def test_run_uses_config_tool_parameters(monkeypatch):
    """run() ต้องส่ง engine/max_uses/search_context_size จาก config ไป OpenRouter."""
    agent = _make_agent()
    monkeypatch.setattr("src.agents.base_agent._web_search_cfg", _make_web_search_cfg)

    captured = []
    def fake_chat(messages, **kwargs):
        captured.append(kwargs.get("tools"))
        return "ผล", []
    agent.llm.chat = fake_chat

    agent.run("วิเคราะห์คู่แข่ง K2")

    tools = captured[0]
    params = tools[0]["parameters"]
    assert params.get("engine") == "exa"
    assert params.get("max_uses") == 8
    assert params.get("max_total_results") == 20
    assert params.get("search_context_size") == "high"
    assert params.get("excluded_domains") == ["reddit.com"]


def test_run_does_not_call_old_plan_and_execute_flow(monkeypatch):
    """ตรวจว่า _plan_search_queries และ _execute_searches ไม่ถูกเรียก."""
    agent = _make_agent()
    monkeypatch.setattr("src.agents.base_agent._web_search_cfg", _make_web_search_cfg)

    agent.llm.chat = lambda *args, **kwargs: ("ผล", [])
    spy = {"plan": False, "execute": False}
    agent._plan_search_queries = lambda *a, **k: (spy.__setitem__("plan", True) or ["q"])
    agent._execute_searches = lambda *a, **k: (spy.__setitem__("execute", True) or "")

    agent.run("วิเคราะห์คู่แข่ง K2")

    assert spy["plan"] is False
    assert spy["execute"] is False


# ============================================================
# Seam 2: run() คืน output พร้อม URL จริงจาก annotations
# ============================================================

def test_run_appends_real_citations_from_annotations(monkeypatch):
    """หลัง run() เสร็จ output ต้องมี URL จริงจาก OpenRouter annotations."""
    agent = _make_agent()
    monkeypatch.setattr("src.agents.base_agent._web_search_cfg", _make_empty_web_search_cfg)

    def fake_chat(messages, **kwargs):
        return "TCL MT46 ราคา 1,290", [
            {"url": "https://www.tcl.com/asia/en/watches/mt46", "title": "TCL MT46"},
        ]
    agent.llm.chat = fake_chat

    output = agent.run("วิเคราะห์คู่แข่ง K2")

    assert "TCL MT46" in output
    assert "https://www.tcl.com/asia/en/watches/mt46" in output


# ============================================================
# Seam 3: run() ส่ง quick_brief ใน user prompt
# ============================================================

def test_run_passes_quick_brief_in_user_prompt(monkeypatch):
    """quick_brief ต้องอยู่ใน user message ที่ส่งให้ model เพื่อ model
    ใส่ขอบเขตเวลาใน query เอง."""
    agent = _make_agent()
    monkeypatch.setattr("src.agents.base_agent._web_search_cfg", _make_empty_web_search_cfg)

    captured = []
    def fake_chat(messages, **kwargs):
        captured.append(messages)
        return "ผล", []
    agent.llm.chat = fake_chat

    agent.run("วิเคราะห์คู่แข่ง K2", quick_brief="เฉพาะปี 2025")

    last_user = captured[0][-1]["content"]
    assert "เฉพาะปี 2025" in last_user


# ============================================================
# Seam 4: run() verify URL ทีละอัน
# ============================================================

def test_run_verifies_urls_when_verify_urls_enabled(monkeypatch):
    """ถ้า verify_urls=True, run() ต้องเรียก web_fetch ตรวจ URL จาก annotations."""
    agent = _make_agent()
    monkeypatch.setattr("src.agents.base_agent._web_search_cfg", _make_web_search_cfg)

    call_count = {"search": 0, "fetch": 0}
    def fake_chat(messages, **kwargs):
        tools = kwargs.get("tools", [])
        # เรียกแบบ tool: web_search หรือ web_fetch
        tool_type = tools[0].get("type") if tools else None
        if tool_type == "openrouter:web_fetch":
            call_count["fetch"] += 1
            url = messages[-1]["content"]
            if "tcl.com/asia/en/watches/mt46" in url:
                return "หน้านี้คือ TCL MT46 Kids Smartwatch"
            return "หน้าไม่เกี่ยว"
        # generate call (ทีหลัง verify)
        if kwargs.get("return_annotations"):
            call_count["search"] += 1
            return "K2 คู่แข่ง TCL MT46", [
                {"url": "https://www.tcl.com/asia/en/watches/mt46", "title": "TCL MT46"},
                {"url": "https://example.com/", "title": "ไม่เกี่ยว"},
            ]
        return "ผล", []
    agent.llm.chat = fake_chat

    output = agent.run("วิเคราะห์คู่แข่ง K2")

    # ต้องมีการ fetch อย่างน้อย 1 ครั้ง (verify URL จริง)
    assert call_count["fetch"] >= 1
    # URL ที่ verify ผ่านต้องมีในผลลัพธ์
    assert "https://www.tcl.com/asia/en/watches/mt46" in output
