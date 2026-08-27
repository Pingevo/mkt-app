from __future__ import annotations

from src.agents.base_agent import BaseAgent


class _FakeLLM:
    def __init__(self) -> None:
        self.messages: list[dict] | None = None

    def chat(self, messages, **kwargs) -> str:
        self.messages = messages
        return "ok"

    def close(self) -> None:
        pass


def test_resource_context_reaches_user_prompt_before_quick_brief():
    fake = _FakeLLM()
    agent = BaseAgent(
        {
            "model": "test-model",
            "max_review_iterations": 0,
            "max_retry_limit": 0,
        },
        fake,
    )
    resource_context = "--- User-provided resources ---\n[Resource: note.txt]\ncontent\n--- End user-provided resources ---"
    agent.run(
        "ข้อมูลสินค้า",
        quick_brief="สร้างคอนเทนต์",
        resource_context=resource_context,
    )
    user_content = fake.messages[1]["content"]
    assert "--- User-provided resources ---" in user_content
    assert "สร้างคอนเทนต์" in user_content
    # quick brief ต้องอยู่หลัง resource context
    assert user_content.index("--- User-provided resources ---") < user_content.index("--- คำสั่งเพิ่มเติม")
