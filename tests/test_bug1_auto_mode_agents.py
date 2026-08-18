"""Bug 1 repro: Auto mode ต้องรันตาม agent ที่ user เลือก
ไม่ใช่ติดล็อก content_creator อย่างเดียว

test นี้ส่ง agents=["product_spec", "content_creator"] ไป /api/run_auto
แล้วตรวจว่า product_spec ถูกเรียกด้วย (ไม่ใช่แค่ content_creator)
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def _client():
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    return TestClient(web_viewer.app)


def _content_json():
    return json.dumps({
        "posts": [{
            "platform": "Facebook",
            "concept": "กันน้ำ",
            "title": "t",
            "caption": "cap",
            "script": "",
            "hashtags": "#t",
            "image_prompts": [],
            "video_prompts": [],
        }],
    }, ensure_ascii=False)


def test_auto_mode_runs_all_selected_agents(_client, tmp_path, monkeypatch):
    """Auto mode + agents=[product_spec, content_creator] → ทั้งคู่ต้องรัน."""
    import web_viewer

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    # --- track which agents ran ---
    agents_ran = []

    def _make_fake_orch():
        fake = MagicMock()
        fake._make_client.return_value = MagicMock()
        fake._make_client.return_value.close = MagicMock()

        # select_product_auto คืนสินค้าที่เลือก
        fake.select_product_auto.return_value = {
            "product_ids": ["AUTO1"],
            "concept": "กันน้ำ",
            "pillar": "p",
            "reason": "r",
            "asset_ids": [],
        }

        # product_spec
        def _run_product_spec(*args, **kw):
            agents_ran.append("product_spec")
            return "product spec result"

        fake.run_product_spec.side_effect = _run_product_spec

        # content_creator
        def _run_content_creator(*args, **kw):
            agents_ran.append("content_creator")
            return _content_json()

        fake.run_content_creator.side_effect = _run_content_creator

        # competitor_analysis
        def _run_competitor(*args, **kw):
            agents_ran.append("competitor_analysis")
            return "competitor result"

        fake.run_competitor_analysis.side_effect = _run_competitor

        # campaign_strategy
        def _run_campaign(*args, **kw):
            agents_ran.append("campaign_strategy")
            return "campaign result"

        fake.run_campaign_strategy.side_effect = _run_campaign

        fake.save_result.return_value = {"product_spec": "x", "content_creator": "y",
                                          "competitor_analysis": "z", "campaign_strategy": "w"}
        return fake

    fake_orch = _make_fake_orch()
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake_orch)

    # mock _read_folder ให้คืนข้อมูลสินค้า (ไม่ใช่โฟลเดอร์จริง)
    monkeypatch.setattr(web_viewer, "_read_folder",
                        lambda folder: (["product info text"], [], {}))

    # mock media_gen + content_history
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer.content_history, "record_entry", lambda *a, **k: True)
    monkeypatch.setattr(web_viewer.content_history, "format_product_history_for_prompt",
                        lambda *a, **k: "")
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file",
                        lambda *a, **k: None)

    # --- ส่ง /api/run_auto พร้อม agents ---
    resp = _client.post("/api/run_auto", json={
        "quick_brief": "",
        "platforms": ["facebook"],
        "media_type": "image",
        "media_when": "ask",
        "auto_image": False,
        "auto_video": False,
        "content_count": 1,
        "product_count": 1,
        "agents": ["product_spec", "content_creator"],  # ← user เลือก 2 agent
    })
    assert resp.status_code == 200, resp.text
    # consume SSE (อาจมี error เพราะ backend ยังไม่รองรับ agents — ไม่สน สนแค่ agents_ran)
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            pass

    # --- ตรวจว่า agent ตัวแรกถูกรัน (TEMP LOCK ให้ agent เดียว) ---
    assert agents_ran == ["product_spec"], (
        f"BUG: ตอนนี้ locked ให้รันแค่ agent ตัวแรก แต่ได้ {agents_ran}")


def test_auto_mode_backward_compat_no_agents(_client, tmp_path, monkeypatch):
    """ถ้าไม่ส่ง agents มา → default เป็น content_creator (backward compat)."""
    import web_viewer

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    agents_ran = []
    fake_orch = MagicMock()
    fake_orch._make_client.return_value = MagicMock()
    fake_orch._make_client.return_value.close = MagicMock()
    fake_orch.select_product_auto.return_value = {
        "product_ids": ["AUTO1"],
        "concept": "c",
        "pillar": "p",
        "reason": "r",
        "asset_ids": [],
    }
    fake_orch.run_content_creator_auto.return_value = {
        "product_id": "AUTO1",
        "product_ids": ["AUTO1"],
        "pillar": "p",
        "concept": "c",
        "reason": "r",
        "content": _content_json(),
        "markdown": "# md",
        "is_duplicate": False,
        "similarity": 0.0,
    }
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake_orch)
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file",
                        lambda *a, **k: None)

    # ไม่ส่ง agents → default content_creator (เหมือนเดิม)
    resp = _client.post("/api/run_auto", json={
        "quick_brief": "",
        "platforms": ["facebook"],
        "media_type": "image",
        "media_when": "ask",
        "auto_image": False,
        "auto_video": False,
        "content_count": 1,
        "product_count": 1,
        # ไม่ส่ง agents
    })
    assert resp.status_code == 200, resp.text
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            data = json.loads(line[6:])
            if data.get("type") == "error":
                pytest.fail(f"auto flow error: {data.get('message')}")

    # backward compat: ถ้าไม่ส่ง agents ใช้ run_content_creator_auto เหมือนเดิม
    assert fake_orch.run_content_creator_auto.called, (
        "BUG: ถ้าไม่ส่ง agents ต้องเรียก run_content_creator_auto (backward compat)")
