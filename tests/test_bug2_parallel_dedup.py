"""Bug 2 repro: เมื่อรัน 2 flow ขนานกัน ทั้งคู่ใช้สินค้าเดียวกัน + content_creator
content_creator ต้องรอคิวทีละ flow (serialize) — ไม่ใช่รันพร้อมกัน
เพราะถ้ารันพร้อมกัน ทั้งคู่จะอ่าน history เดียวกัน → สร้างคอนเทนต์ซ้ำกัน

test นี้ตรวจว่า content_creator ของ 2 flow ไม่ overlap กัน (serialized)
"""
import json
import time
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


def _content_json(concept: str):
    return json.dumps({
        "posts": [{
            "platform": "Facebook",
            "concept": concept,
            "title": "t",
            "caption": f"caption for {concept}",
            "script": "",
            "hashtags": "#t",
            "image_prompts": [],
            "video_prompts": [],
        }],
    }, ensure_ascii=False)


def test_parallel_flows_serialize_content_creator(_client, tmp_path, monkeypatch):
    """2 flow ขนาน + สินค้าเดียวกัน → content_creator ต้องรอคิว ไม่ overlap."""
    import web_viewer

    product_dir = tmp_path / "data" / "SHARED"
    product_dir.mkdir(parents=True)
    (product_dir / "info.txt").write_text("shared product", encoding="utf-8")

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    # --- track content_creator execution timing ---
    execution_log = []  # list of ("start"/"end", flow_index, timestamp)

    def _make_fake_orch(flow_concepts):
        """สร้าง fake Orchestrator ที่บันทึก timing ของ content_creator."""
        fake = MagicMock()
        fake._make_client.return_value = MagicMock()
        fake._make_client.return_value.close = MagicMock()
        fake.save_result.return_value = {"content_creator": str(tmp_path / "out.md")}

        call_count = [0]

        def _run_content_creator(*args, **kw):
            idx = call_count[0]
            call_count[0] += 1
            concept = flow_concepts[idx] if idx < len(flow_concepts) else f"fallback_{idx}"
            execution_log.append(("start", idx, time.monotonic()))
            time.sleep(0.1)  # simulate work
            execution_log.append(("end", idx, time.monotonic()))
            return _content_json(concept)

        fake.run_content_creator.side_effect = _run_content_creator
        return fake

    # แต่ละ flow ใช้ Orchestrator คนละ instance (เหมือนจริง)
    fake_orchs = [_make_fake_orch(["concept_A", "concept_B"])]
    orch_idx = [0]
    monkeypatch.setattr(web_viewer, "Orchestrator",
                        lambda **kw: fake_orchs[0])  # same mock for both flows

    # mock content_history — ไม่เขียนจริง
    monkeypatch.setattr(web_viewer.content_history, "record_entry", lambda *a, **k: True)
    monkeypatch.setattr(web_viewer.content_history, "format_product_history_for_prompt",
                        lambda *a, **k: "")
    monkeypatch.setattr(web_viewer.content_history, "update_last_entry_output_file",
                        lambda *a, **k: None)

    # mock media_gen — ไม่สร้างสื่อ
    monkeypatch.setattr(web_viewer.media_gen, "generate_image_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "generate_video_with_retry",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(web_viewer.media_gen, "save_retry_history", lambda *a, **k: None)

    # --- ส่ง 2 flow ขนาน ทั้งคู่ใช้สินค้า "SHARED" + content_creator ---
    resp = _client.post("/api/run_flows", json={
        "quick_brief": "",
        "flows": [
            {
                "index": 0,
                "folders": ["SHARED"],
                "agents": ["content_creator"],
                "is_auto": False,
                "content_count": 1,
                "platforms": ["facebook"],
                "media_type": "image",
                "media_when": "ask",
                "auto_image": False,
                "auto_video": False,
            },
            {
                "index": 1,
                "folders": ["SHARED"],
                "agents": ["content_creator"],
                "is_auto": False,
                "content_count": 1,
                "platforms": ["facebook"],
                "media_type": "image",
                "media_when": "ask",
                "auto_image": False,
                "auto_video": False,
            },
        ],
    })
    assert resp.status_code == 200, resp.text
    for line in resp.iter_lines():
        if line and line.startswith("data: "):
            pass  # consume

    # --- ตรวจว่า content_creator ของ 2 flow ไม่ overlap ---
    # ถ้า serialized: flow 0 end ก่อน flow 1 start
    # ถ้า parallel (bug): flow 1 start ก่อน flow 0 end
    assert len(execution_log) == 4, f"Expected 4 events, got {execution_log}"

    # แยก start/end ของแต่ละ flow
    events_by_flow = {}
    for event_type, flow_idx, ts in execution_log:
        events_by_flow.setdefault(flow_idx, {})[event_type] = ts

    flow_0_start = events_by_flow[0]["start"]
    flow_0_end = events_by_flow[0]["end"]
    flow_1_start = events_by_flow[1]["start"]
    flow_1_end = events_by_flow[1]["end"]

    # ตรวจ overlap: flow 1 ต้อง start หลัง flow 0 end (หรือ flow 0 start หลัง flow 1 end)
    overlap = (flow_0_start < flow_1_end) and (flow_1_start < flow_0_end)
    assert not overlap, (
        f"BUG: content_creator ของ 2 flow รันพร้อมกัน (overlap)\n"
        f"  flow 0: {flow_0_start:.3f} → {flow_0_end:.3f}\n"
        f"  flow 1: {flow_1_start:.3f} → {flow_1_end:.3f}\n"
        f"  ควร serialize: flow 0 จบก่อน flow 1 เริ่ม"
    )
