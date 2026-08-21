"""Bug 4: regular flow (non-auto) ต้องเหมือน auto mode
- เลือก 2 แพลตฟอร์ม → 1 ไฟล์ที่มี 2 โพสต์ (1 ต่อแพลตฟอร์ม)
- ต้องมี script review
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


def _content_json(platform="Facebook", concept="c"):
    return json.dumps({
        "posts": [{
            "platform": platform,
            "concept": concept,
            "title": "t",
            "caption": "cap",
            "script": "script text here",
            "hashtags": "#t",
            "image_prompts": [],
            "video_prompts": [],
        }],
    }, ensure_ascii=False)


def test_regular_flow_multi_platform_one_file_with_script_review(_client, tmp_path, monkeypatch):
    """2 แพลตฟอร์ม → 1 ไฟล์ 2 โพสต์ + script review ถูกเรียก."""
    import web_viewer

    product_dir = tmp_path / "data" / "K2"
    product_dir.mkdir(parents=True)
    (product_dir / "info.txt").write_text("product info", encoding="utf-8")

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    # --- track calls ---
    cc_call_count = [0]
    script_review_called = []

    def _make_fake_orch():
        fake = MagicMock()
        fake._make_client.return_value = MagicMock()
        fake._make_client.return_value.close = MagicMock()
        fake.save_result.return_value = {"content_creator": str(tmp_path / "out.md")}

        # run_content_creator — คืน 1 โพสต์ต่อ call
        def _run_cc(*args, **kw):
            idx = cc_call_count[0]
            cc_call_count[0] += 1
            platform = "Facebook" if idx == 0 else "TikTok"
            return _content_json(platform=platform, concept=f"concept_{platform}")
        fake.run_content_creator.side_effect = _run_cc

        # script review method — track ว่าถูกเรียก
        def _review_script(*args, **kw):
            platform = args[1] if len(args) > 1 else kw.get("platform", "")
            script_review_called.append(platform)
            return {}
        fake._review_script_in_posts = MagicMock(side_effect=_review_script)

        return fake

    fake_orch = _make_fake_orch()
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake_orch)

    # mock content_history + media_gen
    monkeypatch.setattr(web_viewer.content_history, "record_entry", lambda *a, **k: True)
    monkeypatch.setattr(web_viewer.content_history, "format_product_history_for_prompt",
                        lambda *a, **k: "")
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file",
                        lambda *a, **k: None)
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)

    # --- ส่ง /api/run_flows: 2 แพลตฟอร์ม, 1 โพสต์/แพลตฟอร์ม ---
    resp = _client.post("/api/run_flows", json={
        "quick_brief": "",
        "flows": [{
            "index": 0,
            "folders": ["K2"],
            "agents": ["content_creator"],
            "is_auto": False,
            "content_count": 2,  # 1 โพสต์/แพลตฟอร์ม × 2 แพลตฟอร์ม
            "platforms": ["facebook", "tiktok"],
            "media_type": "both",
            "media_when": "ask",
            "auto_image": False,
            "auto_video": False,
        }],
    })
    assert resp.status_code == 200, resp.text

    # consume SSE
    saved_files = []
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            data = json.loads(line[6:])
            if data.get("type") == "agent_done" and data.get("file"):
                saved_files.append(data["file"])
            if data.get("type") == "error":
                pytest.fail(f"flow error: {data.get('message')}")

    # --- ตรวจว่ามี 1 ไฟล์ (ไม่ใช่ 2 ไฟล์แยก) ---
    assert len(saved_files) == 1, (
        f"BUG: ควรมี 1 ไฟล์ที่มี 2 โพสต์ แต่ได้ {len(saved_files)} ไฟล์: {saved_files}")

    # --- ตรวจว่าไฟล์มี 2 โพสต์ ---
    if saved_files:
        md_path = Path(saved_files[0])
        json_path = md_path.with_suffix(".json")
        if json_path.exists():
            content = json.loads(json_path.read_text(encoding="utf-8"))
            posts = content.get("posts", [])
            assert len(posts) == 2, (
                f"BUG: ไฟล์ควรมี 2 โพสต์ (1 ต่อแพลตฟอร์ม) แต่มี {len(posts)} โพสต์: {posts}")
            platforms_in_posts = [p.get("platform", "") for p in posts]
            assert "Facebook" in platforms_in_posts, (
                f"BUG: ไม่มีโพสต์สำหรับ Facebook: {platforms_in_posts}")
            assert "TikTok" in platforms_in_posts, (
                f"BUG: ไม่มีโพสต์สำหรับ TikTok: {platforms_in_posts}")

    # --- ตรวจว่า script review ถูกเรียก ---
    assert len(script_review_called) > 0, (
        "BUG: script review ไม่ถูกเรียกใน regular flow")


def test_run_flows_per_flow_quick_brief(_client, tmp_path, monkeypatch):
    """แต่ละ flow ต้องใช้ quick_brief ของตัวเอง ไม่ใช้ค่ารวม."""
    import web_viewer

    product_dir = tmp_path / "data" / "K2"
    product_dir.mkdir(parents=True)
    (product_dir / "info.txt").write_text("product info", encoding="utf-8")

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    def _content_json_brief(platform="Facebook"):
        return json.dumps({
            "posts": [{
                "platform": platform,
                "concept": "c",
                "title": "t",
                "caption": "cap",
                "script": "",
                "hashtags": "#t",
                "image_prompts": [],
                "video_prompts": [],
            }],
        }, ensure_ascii=False)

    briefs = []

    def _run_cc(*args, **kw):
        briefs.append(kw.get("quick_brief", ""))
        return _content_json_brief()

    fake_orch = MagicMock()
    fake_orch._make_client.return_value = MagicMock()
    fake_orch._make_client.return_value.close = MagicMock()
    fake_orch.run_content_creator.side_effect = _run_cc
    fake_orch.save_result.return_value = {"content_creator": str(tmp_path / "out.md")}
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake_orch)

    monkeypatch.setattr(web_viewer.content_history, "record_entry", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer.content_history, "format_product_history_for_prompt",
                        lambda *a, **k: "")
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file",
                        lambda *a, **k: None)

    resp = _client.post("/api/run_flows", json={
        "quick_brief": "global",
        "flows": [
            {
                "index": 0,
                "folders": ["K2"],
                "agents": ["content_creator"],
                "is_auto": False,
                "content_count": 1,
                "platforms": ["facebook"],
                "media_type": "image",
                "media_when": "ask",
                "auto_image": False,
                "auto_video": False,
                "quick_brief": "flow1",
            },
            {
                "index": 1,
                "folders": ["K2"],
                "agents": ["content_creator"],
                "is_auto": False,
                "content_count": 1,
                "platforms": ["facebook"],
                "media_type": "image",
                "media_when": "ask",
                "auto_image": False,
                "auto_video": False,
                "quick_brief": "flow2",
            },
        ],
    })
    assert resp.status_code == 200, resp.text

    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            data = json.loads(line[6:])
            if data.get("type") == "error":
                pytest.fail(f"flow error: {data.get('message')}")

    first_lines = {b.split("\n")[0] for b in briefs}
    assert first_lines == {"flow1", "flow2"}, (
        f"quick_brief ต่อ flow ไม่ถูกต้อง: {briefs}")
