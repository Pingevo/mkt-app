"""จำลอง flow จริงจาก UI → save → reload → agent ได้รับค่า.

ตรวจว่า:
1. user save ผ่าน /api/agent_instructions/{key} → ไฟล์เปลี่ยนจริง
2. orchestrator._load_agent_instructions() อ่านค่าใหม่ได้
3. agent ที่สร้างจาก orchestrator ได้รับ instructions ที่ user ตั้ง
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_config
from src.orchestrator import Orchestrator

pytestmark = pytest.mark.usefixtures("brand_ws")


class FakeLLM:
    def __init__(self):
        self.calls: list[dict] = []
        self.last_truncated = False

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        # Grounding gate calls use a JSON schema response format and a
        # source ending in .final_grounding_check — return a valid
        # grounded verdict so wiring tests are not blocked by the gate.
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            return '{"grounded": true, "unsupported_claims": []}'
        out = (
            "## ราคาแนะนำ\n- ราคา pending financial validation\n"
            "## แคมเปญหลัก\n- ชื่อ: Launch Campaign เปิดตัวสินค้ารุ่นใหม่\n"
            "- วัตถุประสงค์: สร้างการรับรู้และกระตุ้นยอดขาย\n"
            "## แคมเปญเสริม\n- Influencer Review ใช้บล็อกเกอร์ทดลองสินค้า\n"
            "## ช่องทางโปรโมท\n- Facebook Ads และ TikTok สำหรับ Gen Z\n"
            "## KPI ที่ควรวัดผล\n- Reach และ CTR ต้องกำหนดหลังมี baseline\n"
            "## งบประมาณประมาณการ\n- ต้องอนุมัติทางการเงินก่อนกำหนดสัดส่วนงบ\n"
            "## แหล่งอ้างอิง\n- ไม่มี URL ภายนอกใน context นี้\n"
        )
        if kwargs.get("return_annotations"):
            return out, []
        return out

    def close(self):
        pass


INSTR_PATH = Path(__file__).resolve().parent.parent / "config" / "agent_instructions.json"


def _backup_instructions() -> str:
    return INSTR_PATH.read_text(encoding="utf-8")


def _restore_instructions(content: str) -> None:
    INSTR_PATH.write_text(content, encoding="utf-8")


def _make_orchestrator() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = load_config()
    orch.brand_context = ""
    orch.brand_reference = ""
    orch.brand_visual = ""
    orch.brand_rules = None
    orch.product_images = []
    orch.product_id = None
    orch.results = {}
    return orch


def test_save_instructions_then_orchestrator_reads_new_value():
    """user save ผ่าน local workspace → orchestrator อ่านค่าใหม่ได้."""
    from src.local_workspace import save_agent_instructions_local, reset_local_workspace
    reset_local_workspace()
    try:
        # 1. Save via local workspace (as the API endpoint does)
        save_agent_instructions_local({
            "campaign_strategy": {
                "custom": "เน้นราคาเฉพาะเจาะจง ไม่ต้องเป็น range กว้าง",
                "rules_must": ["ราคาต้องจบด้วยเลข 9"],
            }
        })

        # 2. orchestrator อ่านค่าใหม่
        orch = _make_orchestrator()
        instructions = orch._load_agent_instructions("campaign_strategy")

        assert instructions.get("custom") == "เน้นราคาเฉพาะเจาะจง ไม่ต้องเป็น range กว้าง"
        assert "ราคาต้องจบด้วยเลข 9" in instructions.get("rules_must", [])

        # 3. agent ที่สร้างจาก orchestrator ได้รับค่าจริง
        from src.agents.campaign_strategy import CampaignStrategyAgent
        agent = orch._make_agent("campaign_strategy", CampaignStrategyAgent, FakeLLM())
        block = agent._format_instructions()
        assert "เน้นราคาเฉพาะเจาะจง" in block
        assert "ราคาต้องจบด้วยเลข 9" in block
    finally:
        reset_local_workspace()


def test_quick_brief_from_ui_reaches_agent_run():
    """user พิมพ์ quick_brief ตอนกดปุ่ม → ส่งถึง agent.run จริง."""
    from src.local_workspace import save_agent_instructions_local, reset_local_workspace
    reset_local_workspace()
    try:
        # เคลียร์ custom ให้เทสสะอาด
        save_agent_instructions_local({"campaign_strategy": {"custom": ""}})

        orch = _make_orchestrator()
        llm = FakeLLM()
        result = orch.run_campaign_strategy(
            product_spec="Lagenio K2 smartwatch",
            competitor_analysis="",
            llm=llm,
            quick_brief="อยากได้ราคาเฉพาะเจาะจง และเน้น TikTok",
        )

        # quick_brief ต้องส่งถึง LLM
        user_content = llm.calls[0]["messages"][1]["content"]
        assert "อยากได้ราคาเฉพาะเจาะจง" in user_content
        assert "เน้น TikTok" in user_content
    finally:
        reset_local_workspace()


def test_preset_change_in_ui_affects_agent_output():
    """user เปลี่ยน preset ใน UI → save → agent ได้รับ preset ใหม่."""
    from src.local_workspace import save_agent_instructions_local, reset_local_workspace
    reset_local_workspace()
    try:
        # เปลี่ยนเป็น sales_focus
        save_agent_instructions_local({
            "campaign_strategy": {
                "preset": "sales_focus",
                "campaign_objective": "sales",
                "risk_level": "aggressive",
                "priority": ["volume", "acquisition"],
            }
        })

        orch = _make_orchestrator()
        from src.agents.campaign_strategy import CampaignStrategyAgent
        agent = orch._make_agent("campaign_strategy", CampaignStrategyAgent, FakeLLM())
        block = agent._format_instructions()

        assert "sales_focus" in block
        assert "กล้า" in block  # aggressive
        assert "Sales Volume" in block
    finally:
        reset_local_workspace()


def test_orchestrator_wires_product_images_to_all_agents(monkeypatch, tmp_path):
    """product_db.get_product_image_paths ต้องถูกส่งถึงทุก Agent ผ่าน agent.run(image_paths=...)
    และ product_spec prompt ต้องไม่บอกว่าไม่มีรูปเมื่อสินค้ามีรูปจริง."""
    import os
    from src import product_db
    from src.agents import base_agent

    os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

    # สร้างรูปจริงสำหรับสินค้า
    prod_img = tmp_path / "product.png"
    prod_img.write_bytes(b"png")

    # จำลองสินค้าพร้อมรูป — ไม่พึ่ง DB จริง
    monkeypatch.setattr(product_db, "is_ready", lambda pid: True)
    monkeypatch.setattr(product_db, "get_product_image_paths", lambda pid: [str(prod_img)])

    captured_images: dict[str, list[str]] = {}
    captured_prompts: dict[str, str] = {}

    orig_run = base_agent.BaseAgent.run

    def _capture_run(self, user_prompt, **kwargs):
        captured_images[self.agent_name] = list(kwargs.get("image_paths") or [])
        captured_prompts[self.agent_name] = user_prompt
        if self.agent_name == "content_creator":
            return json.dumps({"posts": [{
                "platform": "Facebook", "concept": "test", "title": "T",
                "caption": "C", "hashtags": "#h", "asset_ids": [],
                "image_prompts": [], "video_prompts": [],
            }]})
        return f"[{self.agent_name} result]"

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator.__new__(Orchestrator)
    orch.config = load_config()
    orch.brand_context = ""
    orch.brand_reference = ""
    orch.brand_visual = ""
    orch.brand_rules = None
    orch.product_images = []
    orch.product_id = "TEST-PRODUCT"
    orch.results = {}

    llm = FakeLLM()

    orch.run_product_spec("raw data", llm=llm)
    orch.run_competitor_analysis("spec", "", llm=llm)
    orch.run_campaign_strategy("spec", "", llm=llm)
    orch.run_content_creator("spec", "", "", llm=llm)

    expected = [str(prod_img)]
    for key in ("product_spec", "competitor_analysis", "campaign_strategy", "content_creator"):
        assert captured_images.get(key) == expected, f"{key} did not receive product image paths"

    # product_spec prompt ต้องไม่สร้างข้อความ "ไม่มีรูป" เมื่อมีรูปจริง
    product_prompt = captured_prompts.get("product_spec", "")
    assert "ไม่มีรูปภาพสินค้าส่งมาในรอบนี้" not in product_prompt


def test_orchestrator_resolves_stale_product_data_and_images(tmp_path, monkeypatch):
    """Orchestrator ต้องโหลดข้อมูลและรูปภาพของสินค้าที่สถานะ stale ได้ (stale ใช้ของเก่าได้)."""
    import src.product_db as product_db

    prod_id = "StaleLagenio"
    prod_dir = tmp_path / "data" / prod_id
    prod_dir.mkdir(parents=True)
    img_file = prod_dir / "watch.jpg"
    img_file.write_bytes(b"image")

    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)

    rec = product_db.load(prod_id)
    rec["status"] = product_db.STATUS_STALE
    rec["raw_text"] = "Lagenio K5 Child Smartwatch Specs"
    rec["files"] = [{"name": "watch.jpg", "path": str(img_file), "type": "image"}]
    rec["image_descriptions"] = [{"path": str(img_file), "description": "Front view"}]
    product_db.save(prod_id, rec)

    orch = Orchestrator.__new__(Orchestrator)
    orch.config = load_config()
    orch.product_id = prod_id
    orch.product_images = []

    data = orch._get_product_data("FALLBACK")
    assert "Lagenio K5 Child Smartwatch Specs" in data
    assert data != "FALLBACK"

    images = orch._get_product_image_paths()
    assert str(img_file) in images


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


def test_orchestrator_wires_selected_product_source_urls(brand_ws):
    """_make_agent authorizes the selected product's source_import URLs —
    the grounding contract for URL-imported products."""
    import src.product_db as product_db
    from src.agents.product_spec import ProductSpecAgent

    rec = product_db.load("prod_a")
    rec["source_import"] = {"original_url": "https://a.example/p/1"}
    product_db.save("prod_a", rec)
    rec_b = product_db.load("prod_b")
    rec_b["source_import"] = {"original_url": "https://b.example/p/9"}
    product_db.save("prod_b", rec_b)

    orch = Orchestrator.__new__(Orchestrator)
    orch.config = load_config()
    orch.brand_dir = "brand"
    orch.brand_context = None
    orch.brand_reference = None
    orch.brand_visual = None
    orch.brand_rules = None
    orch.product_id = "prod_a"

    agent = orch._make_agent("product_spec", ProductSpecAgent, FakeLLM())
    assert "https://a.example/p/1" in agent._product_source_urls
    # Cross-product isolation: prod_b's source URL is not authorized here
    assert "https://b.example/p/9" not in agent._product_source_urls


def test_orchestrator_multi_product_source_urls_union(brand_ws):
    """Multi-product run unions each selected product's provenance — still
    scoped to the run's products only."""
    import src.product_db as product_db
    from src.agents.product_spec import ProductSpecAgent

    for pid, url in (("pa", "https://a.example/1"), ("pb", "https://b.example/2"), ("pc", "https://c.example/3")):
        rec = product_db.load(pid)
        rec["source_import"] = {"original_url": url}
        product_db.save(pid, rec)

    orch = Orchestrator.__new__(Orchestrator)
    orch.config = load_config()
    orch.brand_dir = "brand"
    orch.brand_context = None
    orch.brand_reference = None
    orch.brand_visual = None
    orch.brand_rules = None
    orch.product_id = "pa + pb"

    agent = orch._make_agent("product_spec", ProductSpecAgent, FakeLLM())
    assert agent._product_source_urls == {"https://a.example/1", "https://b.example/2"}
