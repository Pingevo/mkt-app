"""Bug 3 repro: เมื่อ wizard ส่ง media_when='ask' + auto_image=False + auto_video=False
ไปยัง /api/run_flows, backend ต้อง **ไม่** เรียก media_gen.generate_image_with_retry
หรือ media_gen.generate_video_with_retry เลย — เพราะ user เลือก "ถามก่อนสร้างสื่อ"

ถ้า test นี้ fail = bug จริง (backend ยัง auto-generate แม้ user เลือก ask mode)
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def _client():
    """import web_viewer ใหม่ทุกครั้ง — กัน state รั่วระหว่าง tests."""
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    return TestClient(web_viewer.app)


def _content_creator_json():
    """content_creator structured output ที่มี image_prompts + video_prompts."""
    return json.dumps({
        "posts": [{
            "platform": "Facebook",
            "concept": "กันน้ำ",
            "title": "t",
            "caption": "cap",
            "script": "",
            "hashtags": "#t",
            "image_prompts": [{"prompt": "product photo"}],
            "video_prompts": [{"prompt": "product video"}],
        }],
    }, ensure_ascii=False)


def test_run_flows_ask_mode_does_not_auto_generate_media(_client, tmp_path, monkeypatch):
    """media_when='ask' + auto_image=False + auto_video=False → ห้ามเรียก media_gen."""
    import web_viewer

    # --- จัด data folder จริงใน tmp_path สำหรับ _read_folder ---
    product_dir = tmp_path / "data" / "TESTPROD"
    product_dir.mkdir(parents=True)
    (product_dir / "info.txt").write_text("test product info", encoding="utf-8")

    # monkeypatch PROJECT_ROOT + DATA_DIR + CACHE_DIR + OUTPUT_DIR ของ web_viewer
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    # --- mock Orchestrator ทั้งก้อน — เราสนแต่ media gen path ---
    fake_orch = MagicMock()
    fake_orch._make_client.return_value = MagicMock()
    fake_orch._make_client.return_value.close = MagicMock()
    # run_content_creator คืน JSON structured output (มี image/video prompts)
    fake_orch.run_content_creator.return_value = _content_creator_json()
    fake_orch.save_result.return_value = {"content_creator": str(tmp_path / "out.md")}
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake_orch)

    # --- spy บน media_gen — ถ้าถูกเรียน = bug ---
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry",
                        MagicMock(side_effect=AssertionError(
                            "BUG: generate_image_with_retry ถูกเรียกทั้งที่ media_when='ask'")))
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry",
                        MagicMock(side_effect=AssertionError(
                            "BUG: generate_video_with_retry ถูกเรียกทั้งที่ media_when='ask'")))
    monkeypatch.setattr(web_viewer.media_gen, "generate_image",
                        MagicMock(side_effect=AssertionError(
                            "BUG: generate_image ถูกเรียกทั้งที่ media_when='ask'")))
    monkeypatch.setattr(web_viewer.media_gen, "generate_video",
                        MagicMock(side_effect=AssertionError(
                            "BUG: generate_video ถูกเรียกทั้งที่ media_when='ask'")))
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)

    # --- content_history record_entry ไม่ต้องเขียนจริง ---
    monkeypatch.setattr(web_viewer.content_history, "record_entry", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer.content_history, "format_product_history_for_prompt",
                        lambda *a, **k: "")
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file",
                        lambda *a, **k: None)

    # --- เรียก /api/run_flows เหมือน wizard ส่ง ---
    resp = _client.post("/api/run_flows", json={
        "quick_brief": "",
        "flows": [{
            "index": 0,
            "folders": ["TESTPROD"],
            "agents": ["content_creator"],
            "is_auto": False,
            "content_count": 1,
            "platforms": ["facebook"],
            "media_type": "image",
            "media_when": "ask",       # ← user ติ๊ก "ถามก่อนสร้างสื่อ"
            "auto_image": False,        # ← wizard ส่ง false เมื่อ ask
            "auto_video": False,
        }],
    })
    assert resp.status_code == 200, resp.text

    # อ่าน SSE จนจบ — ถ้า media_gen ถูกเรียก side_effect จะ AssertionError ทันที
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            data = json.loads(line[6:])
            if data.get("type") == "error":
                pytest.fail(f"flow error: {data.get('message')}")

    # ถ้ามาถึงตรงนี้ = media_gen ไม่ถูกเรียก = pass


def test_run_auto_ask_mode_does_not_auto_generate_media(_client, tmp_path, monkeypatch):
    """Auto flow + media_when='ask' + auto_image=False + auto_video=False → ห้ามเรียก media_gen."""
    import web_viewer

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    # --- mock Orchestrator.run_content_creator_auto คืน result dict ---
    fake_orch = MagicMock()
    fake_orch._make_client.return_value = MagicMock()
    fake_orch._make_client.return_value.close = MagicMock()
    fake_orch.run_content_creator_auto.return_value = {
        "product_id": "AUTO1",
        "product_ids": ["AUTO1"],
        "pillar": "p",
        "concept": "c",
        "reason": "r",
        "content": _content_creator_json(),
        "markdown": "# md",
        "is_duplicate": False,
        "similarity": 0.0,
    }
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake_orch)

    # --- spy บน media_gen ---
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry",
                        MagicMock(side_effect=AssertionError(
                            "BUG: generate_image_with_retry ถูกเรียกทั้งที่ media_when='ask'")))
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry",
                        MagicMock(side_effect=AssertionError(
                            "BUG: generate_video_with_retry ถูกเรียกทั้งที่ media_when='ask'")))
    monkeypatch.setattr(web_viewer.media_gen, "parse_media_prompts",
                        MagicMock(side_effect=AssertionError(
                            "BUG: parse_media_prompts ถูกเรียกทั้งที่ media_when='ask'")))
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
    })
    assert resp.status_code == 200, resp.text

    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            data = json.loads(line[6:])
            if data.get("type") == "error":
                pytest.fail(f"auto flow error: {data.get('message')}")


def test_run_flows_null_auto_image_does_not_auto_generate_media(_client, tmp_path, monkeypatch):
    """ถ้า auto_image=None (ไม่ได้ส่งมา) backend ต้องถือเป็น False
    ไม่ใช่อ่าน config/media.yaml ที่อาจเป็น true → ห้ามเรียก media_gen

    นี่คือ regression test สำหรับ Bug 3: ลบ config fallback ใน _run_single_agent
    """
    import web_viewer

    product_dir = tmp_path / "data" / "TESTPROD"
    product_dir.mkdir(parents=True)
    (product_dir / "info.txt").write_text("test product info", encoding="utf-8")

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    fake_orch = MagicMock()
    fake_orch._make_client.return_value = MagicMock()
    fake_orch._make_client.return_value.close = MagicMock()
    fake_orch.run_content_creator.return_value = _content_creator_json()
    fake_orch.save_result.return_value = {"content_creator": str(tmp_path / "out.md")}
    monkeypatch.setattr(web_viewer, "Orchestrator", lambda **kw: fake_orch)

    # mock media_gen._load_media_config ให้คืน auto_generate_image: true
    # (เหมือน config เดิมก่อนแก้ — ถ้า fallback ยังอยู่ จะ trigger auto-gen)
    monkeypatch.setattr(web_viewer.media_gen, "_load_media_config",
                        lambda: {"auto_generate_image": True, "auto_generate_video": True})

    # spy บน media_gen — ถ้าถูกเรียก = bug (None ไม่ควร trigger auto-gen)
    # ใช้ spy แทน side_effect เพราะ try/except ใน _run_single_agent จะกลืน AssertionError
    image_retry_called = []
    video_retry_called = []
    image_called = []
    video_called = []
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry",
                        lambda *a, **k: image_retry_called.append(True) or {"ok": True, "path": "x"})
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry",
                        lambda *a, **k: video_retry_called.append(True) or {"ok": True, "path": "x"})
    monkeypatch.setattr(web_viewer.media_gen, "generate_image",
                        lambda *a, **k: image_called.append(True) or {"ok": True, "path": "x"})
    monkeypatch.setattr(web_viewer.media_gen, "generate_video",
                        lambda *a, **k: video_called.append(True) or {"ok": True, "path": "x"})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer.content_history, "record_entry", lambda *a, **k: None)
    monkeypatch.setattr(web_viewer.content_history, "format_product_history_for_prompt",
                        lambda *a, **k: "")
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file",
                        lambda *a, **k: None)

    # ส่งโดยไม่มี auto_image / auto_video → body.get(..., None) → None
    resp = _client.post("/api/run_flows", json={
        "quick_brief": "",
        "flows": [{
            "index": 0,
            "folders": ["TESTPROD"],
            "agents": ["content_creator"],
            "is_auto": False,
            "content_count": 1,
            "platforms": ["facebook"],
            "media_type": "image",
            "media_when": "ask",
            # ไม่ส่ง auto_image / auto_video → None
        }],
    })
    assert resp.status_code == 200, resp.text
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            pass  # consume SSE

    # หลังแก้: None ต้องถือเป็น False → media_gen ไม่ถูกเรียก
    assert not image_retry_called, (
        "BUG: generate_image_with_retry ถูกเรียกทั้งที่ auto_image=None "
        "(ควรถือเป็น False ไม่ใช่อ่าน config)")
    assert not video_retry_called, "BUG: generate_video_with_retry ถูกเรียกทั้งที่ auto_video=None"
    assert not image_called, "BUG: generate_image ถูกเรียกทั้งที่ auto_image=None"
    assert not video_called, "BUG: generate_video ถูกเรียกทั้งที่ auto_video=None"
