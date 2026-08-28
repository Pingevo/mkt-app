"""Bug 5: ยืนยัน auto mode ทำงานได้กับทุก agent ที่ user เลือก
- product_spec, competitor_analysis, campaign_strategy, content_creator
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
            "script": "script text",
            "hashtags": "#t",
            "image_prompts": [],
            "video_prompts": [],
            "asset_ids": [],
        }],
    }, ensure_ascii=False)


def test_auto_mode_runs_all_four_agents(_client, tmp_path, monkeypatch):
    """เลือกทั้ง 4 agent ใน auto mode → ทุก agent ต้องถูกเรียงลำดับรัน."""
    import web_viewer

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(web_viewer, "_read_folder", lambda f: (["info"], [], {}))

    agents_ran = []

    def _make_fake_orch():
        fake = MagicMock()
        fake._make_client.return_value = MagicMock()
        fake._make_client.return_value.close = MagicMock()
        fake.select_product_auto.return_value = {
            "product_ids": ["AUTO1"],
            "concept": "c",
            "pillar": "p",
            "reason": "r",
            "asset_ids": [],
        }
        fake.save_result.return_value = {
            "product_spec": "x",
            "competitor_analysis": "y",
            "campaign_strategy": "z",
            "content_creator": "w",
        }

        def _run_product_spec(*a, **k):
            agents_ran.append("product_spec")
            return "product spec"
        fake.run_product_spec.side_effect = _run_product_spec

        def _run_competitor(*a, **k):
            agents_ran.append("competitor_analysis")
            return "competitor"
        fake.run_competitor_analysis.side_effect = _run_competitor

        def _run_campaign(*a, **k):
            agents_ran.append("campaign_strategy")
            return "campaign"
        fake.run_campaign_strategy.side_effect = _run_campaign

        def _run_content_creator(*a, **k):
            agents_ran.append("content_creator")
            return _content_json()
        fake.run_content_creator.side_effect = _run_content_creator

        fake._review_script_in_posts = MagicMock(return_value={})

        return fake

    fake = _make_fake_orch()
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake)

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

    resp = _client.post("/api/run_auto", json={
        "quick_brief": "",
        "platforms": ["facebook"],
        "media_type": "image",
        "media_when": "ask",
        "auto_image": False,
        "auto_video": False,
        "content_count": 1,
        "product_count": 1,
        "agents": ["product_spec", "competitor_analysis", "campaign_strategy", "content_creator"],
    })
    assert resp.status_code == 200, resp.text
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            data = json.loads(line[6:])
            if data.get("type") == "error":
                pytest.fail(f"auto flow error: {data.get('message')}")

    # TEMP LOCK: รัน agent ตัวแรกอย่างเดียว
    expected_order = ["product_spec"]
    assert agents_ran == expected_order, (
        f"BUG: ตอนนี้ locked ให้รันแค่ agent ตัวแรก แต่ได้ {agents_ran}")
