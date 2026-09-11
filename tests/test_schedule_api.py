"""Schedule API round-trip — prove persisted job matches saved values.

POST /api/schedule/save → GET /api/schedule/jobs → assert all material fields
round-trip unchanged. No real Agent/model call.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def _client(tmp_path, monkeypatch):
    """FastAPI TestClient with isolated filesystem and mocked scheduler init."""
    import importlib
    import web_viewer

    importlib.reload(web_viewer)

    monkeypatch.setattr(web_viewer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", lambda: tmp_path / "output")
    monkeypatch.setattr(web_viewer, "DATA_DIR", lambda: tmp_path / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", lambda: tmp_path / "cache")

    (tmp_path / "data" / "TestProduct").mkdir(parents=True)
    (tmp_path / "data" / "TestProduct" / "info.txt").write_text("info", encoding="utf-8")
    (tmp_path / "cache" / "TestProduct").mkdir(parents=True)
    (tmp_path / "brand").mkdir(exist_ok=True)
    (tmp_path / "output").mkdir(exist_ok=True)

    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_instructions.json").write_text(
        json.dumps({"_presets": {}}, ensure_ascii=False), encoding="utf-8",
    )
    (config_dir / "system.yaml").write_text(
        "web_port: 9999\nscheduler:\n  enabled: true\n", encoding="utf-8",
    )
    (config_dir / "run_resources.yaml").write_text(
        "enabled: true\nttl_hours: 24\n", encoding="utf-8",
    )

    monkeypatch.setattr(web_viewer, "_current_llm", None, raising=False)
    monkeypatch.setattr(web_viewer, "_session_ts", "", raising=False)
    monkeypatch.setattr(web_viewer, "_cancel_requested", False, raising=False)

    # Redirect _resource_store to per-user workspace (no explicit storage_dir
    # so it resolves via user_state_root, same as the scheduler's store)
    from src.run_resources import RunResourceStore
    monkeypatch.setattr(web_viewer, "_resource_store", RunResourceStore(
        tmp_path,
        config={"enabled": True, "ttl_hours": 24, "max_files_per_flow": 5,
                "max_file_size_mb": 15, "max_total_size_mb": 30,
                "max_extracted_chars_total": 120000,
                "max_extracted_chars_per_file": 50000,
                "allowed_extensions": [".txt", ".md", ".csv", ".pdf", ".png", ".jpg"]},
    ))

    # Patch scheduler start to avoid APScheduler threads
    _orig_get = web_viewer._get_scheduler

    def _safe_get():
        try:
            return _orig_get()
        except Exception:
            return None

    monkeypatch.setattr(web_viewer, "_get_scheduler", _safe_get)

    # Authenticate the client
    from tests.conftest import make_authed_client
    client, user_id, ws_root = make_authed_client(web_viewer.app, tmp_path, monkeypatch)
    # Patch path functions to per-user workspace
    monkeypatch.setattr(web_viewer, "OUTPUT_DIR", lambda: ws_root / "output")
    monkeypatch.setattr(web_viewer, "DATA_DIR", lambda: ws_root / "data")
    monkeypatch.setattr(web_viewer, "CACHE_DIR", lambda: ws_root / "cache")
    # Create per-user dirs
    (ws_root / "data" / "TestProduct").mkdir(parents=True, exist_ok=True)
    (ws_root / "data" / "TestProduct" / "info.txt").write_text("info", encoding="utf-8")
    (ws_root / "cache" / "TestProduct").mkdir(parents=True, exist_ok=True)
    (ws_root / "output").mkdir(parents=True, exist_ok=True)
    (ws_root / "brand").mkdir(parents=True, exist_ok=True)
    return client


class TestScheduleRoundTrip:
    """POST /api/schedule/save → GET /api/schedule/jobs — all fields survive."""

    def test_round_trip_preserves_all_material_fields(self, _client):
        # Upload a real attachment so the clone has something to work with
        upload_resp = _client.post(
            "/api/run-resources/upload",
            files={"files": ("brief.txt", b"test attachment", "text/plain")},
        )
        assert upload_resp.status_code == 200
        upload_data = upload_resp.json()
        session_id = upload_data["upload_session_id"]
        resource_id = upload_data["resources"][0]["resource_id"]

        flow = {
            "is_auto": False,
            "agents": ["product_spec", "content_creator"],
            "folders": ["TestProduct"],
            "platforms": ["facebook", "tiktok"],
            "content_count": 2,
            "media_type": "image",
            "media_when": "ask",
            "auto_image": False,
            "auto_video": False,
            "upload_session_id": session_id,
            "resource_refs": [f"resource:{resource_id}"],
        }
        payload = {
            "name": "round-trip test",
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
            "flow": flow,
            "quick_brief": "test brief for round-trip",
        }

        resp = _client.post("/api/schedule/save", json=payload)
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        assert job_id

        resp = _client.get("/api/schedule/jobs")
        assert resp.status_code == 200
        jobs = resp.json()
        assert len(jobs) >= 1
        job = next(j for j in jobs if j["id"] == job_id)

        assert job["name"] == "round-trip test"
        assert job["schedule_type"] == "one_time"
        assert job["schedule"] == {"type": "date", "value": "2099-01-01T09:00:00"}
        assert job["quick_brief"] == "test brief for round-trip"

        # Flow fields
        f = job["flow"]
        assert f["agents"] == ["product_spec", "content_creator"]
        assert f["folders"] == ["TestProduct"]
        assert f["platforms"] == ["facebook", "tiktok"]
        assert f["content_count"] == 2
        assert f["media_type"] == "image"
        assert f["media_when"] == "ask"
        assert f["auto_image"] is False
        assert f["auto_video"] is False

        # Attachments: the session_id and resource_refs are cloned to a durable
        # session — they must be present but different from the original ephemeral ones
        assert f["upload_session_id"], "job must have a durable upload_session_id"
        assert f["upload_session_id"] != session_id, \
            "job must use a durable session, not the ephemeral one"
        assert len(f["resource_refs"]) == 1
        assert f["resource_refs"][0].startswith("resource:")

    def test_round_trip_auto_flow(self, _client):
        """Auto flow fields must also round-trip."""
        flow = {
            "is_auto": True,
            "agents": ["content_creator"],
            "platforms": ["facebook"],
            "content_count": 3,
            "media_type": "video",
            "media_when": "auto",
            "auto_image": True,
            "auto_video": True,
            "auto_combined": True,
            "auto_count": 2,
        }
        payload = {
            "name": "auto round-trip",
            "schedule_type": "recurring",
            "schedule": {"type": "cron", "value": "0 9 * * *"},
            "flow": flow,
            "quick_brief": "auto brief",
        }

        resp = _client.post("/api/schedule/save", json=payload)
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        resp = _client.get("/api/schedule/jobs")
        job = next(j for j in resp.json() if j["id"] == job_id)

        assert job["schedule_type"] == "recurring"
        assert job["schedule"] == {"type": "cron", "value": "0 9 * * *"}
        f = job["flow"]
        assert f["is_auto"] is True
        assert f["agents"] == ["content_creator"]
        assert f["platforms"] == ["facebook"]
        assert f["content_count"] == 3
        assert f["media_type"] == "video"
        assert f["media_when"] == "auto"
        assert f["auto_image"] is True
        assert f["auto_video"] is True

    def test_delete_removes_job(self, _client):
        payload = {
            "name": "to delete",
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": "2099-01-01T09:00:00"},
            "flow": {"agents": ["content_creator"]},
            "quick_brief": "",
        }
        resp = _client.post("/api/schedule/save", json=payload)
        job_id = resp.json()["job_id"]

        resp = _client.post("/api/schedule/delete", json={"job_id": job_id})
        assert resp.json()["ok"] is True

        resp = _client.get("/api/schedule/jobs")
        assert all(j["id"] != job_id for j in resp.json())
