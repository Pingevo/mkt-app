from __future__ import annotations

from src.agents.base_agent import BaseAgent


class _FakeLLM:
    def __init__(self) -> None:
        self.messages: list[dict] | None = None

    def chat(self, messages, **kwargs) -> str:
        self.messages = messages
        return (
            "## ราคาแนะนำ\n- ราคา pending financial validation\n"
            "## แคมเปญหลัก\n- ชื่อ: Launch Campaign\n- วัตถุประสงค์: สร้างการรับรู้\n"
            "## แคมเปญเสริม\n- Influencer Review\n"
            "## ช่องทางโปรโมท\n- Facebook Ads และ TikTok\n"
            "## KPI ที่ควรวัดผล\n- Reach และ CTR ต้องกำหนดหลังมี baseline\n"
            "## งบประมาณประมาณการ\n- ต้องอนุมัติทางการเงินก่อน\n"
            "## แหล่งอ้างอิง\n- ไม่มี URL ภายนอกใน context นี้\n"
        )

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
    assert user_content.index("--- User-provided resources ---") < user_content.index("--- คำสั่งเฉพาะรอบนี้จากผู้ใช้")
