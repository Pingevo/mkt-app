"""BaseAgent output validation + repair integration."""
import pytest

from src.agents.base_agent import BaseAgent


class DummyAgent(BaseAgent):
    agent_name = "product_spec"

    def build_prompt(self, *args, **kwargs):
        return ""


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = 0

    def chat(self, messages, **kwargs):
        out = self.outputs[self.calls % len(self.outputs)]
        self.calls += 1
        return out

    def close(self):
        pass


def _config():
    return {
        "system_prompt": "คุณคือ agent",
        "use_brand_context": False,
        "use_brand_reference": False,
        "max_review_iterations": 0,
        "max_retry_limit": 3,
        "required_output_sections": ["ชื่อสินค้า"],
        "model": "x",
        "temperature": 0.7,
        "max_tokens": 100,
    }


def test_base_agent_repairs_invalid_output():
    llm = FakeLLM(["output ไม่มี section", "ชื่อสินค้า: Lagenio\nอื่นๆ"])
    agent = DummyAgent(_config(), llm)
    result = agent.run("prompt")
    assert "ชื่อสินค้า" in result
    assert llm.calls == 2


def test_base_agent_valid_output_skips_repair():
    llm = FakeLLM(["ชื่อสินค้า: Lagenio"])
    agent = DummyAgent(_config(), llm)
    result = agent.run("prompt")
    assert result == "ชื่อสินค้า: Lagenio"
    assert llm.calls == 1


def test_base_agent_raises_after_max_repairs():
    llm = FakeLLM(["ไม่ถูก", "ไม่ถูก", "ไม่ถูก", "ไม่ถูก"])
    agent = DummyAgent(_config(), llm)
    with pytest.raises(ValueError) as exc:
        agent.run("prompt")
    assert "ตรวจ output ไม่ผ่าน" in str(exc.value)
    assert llm.calls == 4  # 1 initial + 3 repair attempts


def test_base_agent_raises_when_repair_returns_blank():
    """ถ้า repair คืน output ว่าง ต้อง raise ทันที ไม่ loop ไป 3 รอบเผาเครดิต."""
    llm = FakeLLM(["output ไม่มี section", "", "third", "fourth"])
    agent = DummyAgent(_config(), llm)
    with pytest.raises(ValueError) as exc:
        agent.run("prompt")
    assert "ซ่อม output ไม่สำเร็จ" in str(exc.value)
    assert "output ว่างเปล่า" in str(exc.value)
    assert llm.calls == 2  # 1 initial + 1 repair
