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
        "strict_output_sections": True,
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


# ------------------------------------------------------------------
#  Pipeline reorder: blank generate must skip review, not be masked
# ------------------------------------------------------------------

def _config_with_review():
    """Config that enables review (max_review_iterations=1)."""
    cfg = _config()
    cfg["max_review_iterations"] = 1
    cfg["review_temperature"] = 0.2
    cfg["review_prompt"] = "คุณคือ reviewer"
    return cfg


class CapturingLLM:
    """FakeLLM that captures messages passed to each chat call."""

    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = 0
        self.captured_messages = []

    def chat(self, messages, **kwargs):
        self.captured_messages.append(messages)
        out = self.outputs[self.calls % len(self.outputs)]
        self.calls += 1
        return out

    def close(self):
        pass


def test_generate_blank_skips_review_and_raises():
    """generate คืนว่าง → ต้อง raise ทันที ไม่เรียก reviewer เลย.

    สถานการณ์จริง: Flow 2 (K69+K72) — generate ว่าง แต่ reviewer ถูกเรียก
    และสร้างแบบฟอร์ม "ไม่มีข้อมูล" ปลอม ทำให้ validator ปล่อยผ่าน
    """
    llm = CapturingLLM(["", "ไม่มีข้อมูลปลอมจาก reviewer"])
    agent = DummyAgent(_config_with_review(), llm)
    with pytest.raises(ValueError) as exc:
        agent.run("prompt ที่มีข้อมูลดิบของสินค้า")
    assert "model คืนคำตอบว่างเปล่า" in str(exc.value)
    # reviewer ต้องไม่ถูกเรียกเลย — มีแค่ generate call
    assert llm.calls == 1


def test_review_blank_returns_draft_with_warning():
    """generate สำเร็จ → reviewer คืนว่าง → ต้องคืน draft พร้อม warning ไม่ใช่ว่าง.

    สถานการณ์จริง: Flow 1 (K66) — generate สร้างสเปค K66 ครบ แต่ reviewer
    คืนว่าง (timeout) ระบบเขียนทับงานดี้ด้วยค่าว่าง → งานหายหมด
    """
    draft = "ชื่อสินค้า: CACGO K66\nคำอธิบาย: สมาร์ทวอทช์จอ 1.85 นิ้ว"
    llm = CapturingLLM([draft, ""])  # generate สำเร็จ, review ว่าง
    agent = DummyAgent(_config_with_review(), llm)
    result = agent.run("prompt ที่มีข้อมูลดิบของ K66")

    # ต้องได้ draft กลับมา ไม่ใช่ว่าง
    assert "CACGO K66" in result
    # ต้องมี warning บอกว่า review ล้มเหลว
    assert "ตรวจทาน" in result or "review" in result.lower() or "ล้มเหลว" in result
    assert llm.calls == 2  # generate + review


def test_review_exception_returns_draft_with_warning():
    """generate สำเร็จ → reviewer เกิด exception → ต้องคืน draft พร้อม warning."""
    draft = "ชื่อสินค้า: CACGO K66\nคำอธิบาย: สมาร์ทวอทช์"

    class ReviewCrashingLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return draft
            raise RuntimeError("review timeout from OpenRouter")

        def close(self):
            pass

    llm = ReviewCrashingLLM()
    agent = DummyAgent(_config_with_review(), llm)
    result = agent.run("prompt ที่มีข้อมูลดิบของ K66")

    assert "CACGO K66" in result
    assert "ตรวจทาน" in result or "review" in result.lower() or "ล้มเหลว" in result
    assert llm.calls == 2


def test_reviewer_receives_original_source_prompt():
    """reviewer ต้องเห็น user_prompt เดิม (ที่มีข้อมูลดิบ) ไม่ใช่แค่ output ของ generator.

    สถานการณ์จริง: reviewer ไม่เห็น source → ตรวจข้อเท็จจริงไม่ได้
    จึงไม่สามารถบอกได้ว่า K72 "สู้แสงกลางแจ้งได้ดี" เกิน source หรือไม่
    """
    draft = "ชื่อสินค้า: K72\nจุดแข็ง: สู้แสงกลางแจ้งได้ดี"
    source_prompt = "ข้อมูลดิบ: CPU ATS3085L จอ 2.13 นิ้ว ไม่มี brightness"
    llm = CapturingLLM([draft, draft])  # generate + review คืนอย่างเดียวกัน
    agent = DummyAgent(_config_with_review(), llm)
    agent.run(source_prompt)

    # review call คือ call ที่ 2
    review_messages = llm.captured_messages[1]
    review_text = " ".join(
        m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content", ""))
        for m in review_messages
    )
    # reviewer ต้องเห็น source prompt เดิม (มี "ข้อมูลดิบ" หรือ "CPU ATS3085L")
    assert "ข้อมูลดิบ" in review_text or "ATS3085L" in review_text


def test_reviewer_receives_image_paths():
    """reviewer ต้องได้ image_paths ชุดเดียวกับ generator เพื่อตรวจข้อเท็จจริงของรูปได้."""
    draft = "ชื่อสินค้า: K2\nยืนยัน: เห็นรูปภาพจริง"
    llm = CapturingLLM([draft, draft])
    agent = DummyAgent(_config_with_review(), llm)

    # สร้างไฟล์รูปจำลอง
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        # Minimal PNG header
        f.write(b'\x89PNG\r\n\x1a\n' + b'\x00' * 100)
        tmp_img = f.name
    try:
        agent.run("prompt", image_paths=[tmp_img])
        # review call คือ call ที่ 2 — ต้องเป็น multimodal content (list) ไม่ใช่ string
        review_messages = llm.captured_messages[1]
        user_msg = review_messages[1]  # [system, user]
        assert isinstance(user_msg["content"], list), \
            "reviewer ต้องได้ multimodal content (text + image) เหมือน generator"
    finally:
        os.unlink(tmp_img)


class FakeLLMWithAnnotations:
    """Fake LLM สำหรับ web_search path คืน (text, annotations) tuple."""

    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = 0
        self.captured_messages = []

    def chat(self, messages, **kwargs):
        self.captured_messages.append(messages)
        out, ann = self.outputs[self.calls % len(self.outputs)]
        self.calls += 1
        if kwargs.get("return_annotations"):
            return out, ann
        return out

    def close(self):
        pass


def _config_research():
    cfg = _config()
    cfg["strict_output_sections"] = False
    cfg["web_search"] = True
    cfg["verify_urls"] = False
    cfg["citation_policy"] = {"mode": "default"}
    return cfg


def test_research_required_injects_web_search_instruction():
    """research_required=True ต้องสอดแทรกคำสั่งบังคับค้นหาใน user prompt."""
    llm = FakeLLMWithAnnotations([("output", [{"url": "http://example.com", "title": "x"}])])
    agent = DummyAgent(_config_research(), llm)
    agent.run("prompt", research_required=True)
    user_msg = llm.captured_messages[0][1]["content"]
    assert "ต้องใช้ web search" in user_msg


def test_research_required_fails_when_no_annotations():
    """research_required=True แต่ LLM ไม่ค้น (ไม่มี annotations) ต้อง fail."""
    llm = FakeLLMWithAnnotations([("output", [])])
    agent = DummyAgent(_config_research(), llm)
    with pytest.raises(ValueError) as exc:
        agent.run("prompt", research_required=True)
    assert "research_required" in str(exc.value)


def test_research_required_resets_annotations_between_runs():
    """_last_annotations ต้อง reset ทีต้น run — ข้อมูลเก่าห้ามทำให้ run ถัดไปผ่าน."""
    llm = FakeLLMWithAnnotations([
        ("first", [{"url": "http://old", "title": "old"}]),
        ("second", []),
    ])
    agent = DummyAgent(_config_research(), llm)
    agent.run("prompt", research_required=True)
    with pytest.raises(ValueError) as exc:
        agent.run("prompt", research_required=True)
    assert "research_required" in str(exc.value)
