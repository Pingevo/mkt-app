"""Output display — title จาก content ของโพสต์ + ดาวน์โหลดสื่อ"""
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def _client():
    import importlib
    import web_viewer
    importlib.reload(web_viewer)
    return TestClient(web_viewer.app)


def _content_json(title="รีวิว Lagenio K3"):
    return json.dumps({
        "posts": [{
            "platform": "Facebook",
            "concept": "รีวิว",
            "title": title,
            "caption": "caption text",
            "script": "",
            "hashtags": "#tag",
            "image_prompts": [],
            "video_prompts": [],
        }],
    }, ensure_ascii=False)


def test_scan_sessions_include_title_from_content_json(tmp_path, monkeypatch):
    """_scan_sessions ต้องคืน title ของโพสต์ โดยอ่านจาก .json คู่ของ .md"""
    import web_viewer
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    out = tmp_path / "output"
    session = out / "19_ส.ค._2569_12.53"
    session.mkdir(parents=True)
    (session / "04_content_creator_K2_AUTO_โพสต์ที่1_123456.md").write_text("# markdown", encoding="utf-8")
    (session / "04_content_creator_K2_AUTO_โพสต์ที่1_123456.json").write_text(
        _content_json("รีวิว Lagenio K3 สมาร์ทวอทช์"), encoding="utf-8")
    (session / "image_1.png").write_bytes(b"\x89PNG")

    sessions = web_viewer._scan_sessions()
    assert len(sessions) == 1
    assert sessions[0]["title"] == "รีวิว Lagenio K3 สมาร์ทวอทช์"
    assert [f["name"] for f in sessions[0]["files"]] == ["04_content_creator_K2_AUTO_โพสต์ที่1_123456.md"]
    assert sessions[0]["files"][0]["title"] == "รีวิว Lagenio K3 สมาร์ทวอทช์"


def test_api_sessions_returns_title(_client, tmp_path, monkeypatch):
    """/api/sessions ต้องคืน title ของ session กับ file"""
    import web_viewer
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    out = tmp_path / "output"
    session = out / "20_ส.ค._2569_09.00"
    session.mkdir(parents=True)
    (session / "04_content_creator_K2_โพสต์ที่1_123456.md").write_text("# markdown", encoding="utf-8")
    (session / "04_content_creator_K2_โพสต์ที่1_123456.json").write_text(
        _content_json("รีวิว Retro Watch"), encoding="utf-8")

    res = _client.get("/api/sessions")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["title"] == "รีวิว Retro Watch"
    md = next(f for f in data[0]["files"] if f["name"].endswith(".md"))
    assert md["title"] == "รีวิว Retro Watch"


def test_api_file_download_sets_content_disposition(_client, tmp_path, monkeypatch):
    """/api/file/...?download=1 ต้องตั้ง Content-Disposition: attachment"""
    import web_viewer
    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", tmp_path / "output")

    out = tmp_path / "output"
    session = out / "session1"
    session.mkdir(parents=True)
    (session / "image_1.png").write_bytes(b"\x89PNG")

    res = _client.get("/api/file/session1/image_1.png?download=1")
    assert res.status_code == 200
    assert "content-disposition" in res.headers
    assert "attachment" in res.headers["content-disposition"]
