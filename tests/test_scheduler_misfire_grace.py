"""Regression: misfire_grace_time=0 ทำให้ add_job ล้มเหลว — job ไม่เข้า APScheduler.

APScheduler ไม่ยอมรับ misfire_grace_time=0 (ต้องเป็น None หรือจำนวนเต็มบวก)
เมื่อ config ตั้งเป็น 0, self._aps.add_job() โยน exception ทุกครั้ง
exception ถูก catch กลืน ทำให้ job อยู่ในไฟล์ JSON แต่ไม่อยู่ใน APScheduler
ผล: scheduled job ไม่ยิงเลย แม้เซิร์ฟเวอร์จะรันอยู่
"""
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.scheduler import JsonJobStore, Scheduler


def _write_config(project_root: Path, misfire_grace_time):
    """เขียน config/system.yaml ที่มีค่า scheduler.misfire_grace_time ตามที่ระบุ."""
    cfg_dir = project_root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "system.yaml").write_text(
        f"""
web_port: 9999
scheduler:
  enabled: true
  misfire_grace_time: {misfire_grace_time}
""",
        encoding="utf-8",
    )


@pytest.fixture
def _store(tmp_path):
    return JsonJobStore(tmp_path / "jobs.json", tmp_path / "runs.json", retention_days=30)


def test_add_job_succeeds_with_misfire_grace_zero(tmp_path, _store):
    """misfire_grace_time=0 ใน config ต้องไม่ทำให้ add_job ล้ม.

    APScheduler ไม่ยอมรับ 0 — ระบบต้องแปลงเป็นค่าที่ valid (1) อัตโนมัติ
    ไม่ใช่โยน exception แล้วกลืนเงียบ
    """
    _write_config(tmp_path, 0)
    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    future = (datetime.now().astimezone() + timedelta(seconds=60)).isoformat()
    job_id = sched.add_job({
        "name": "test-grace-zero",
        "enabled": True,
        "schedule_type": "one_time",
        "schedule": {"type": "date", "value": future},
        "flow": {"is_auto": False, "agents": ["content_creator"]},
        "quick_brief": "test",
    })

    # job ต้องอยู่ใน APScheduler's in-memory jobstore
    assert sched.job_exists(job_id), (
        "job ไม่อยู่ใน APScheduler — add_job ล้มเหลวเงียบ "
        "(ตรวจสอบ misfire_grace_time ใน config)"
    )

    sched.remove_job(job_id)
    sched.stop()


def test_start_loads_existing_jobs_with_misfire_grace_zero(tmp_path, _store):
    """start() ต้องโหลด job ที่มีอยู่เข้า APScheduler ได้ แม้ misfire_grace_time=0."""
    _write_config(tmp_path, 0)

    future = (datetime.now().astimezone() + timedelta(seconds=60)).isoformat()
    job = {
        "id": "job_existing",
        "name": "existing",
        "enabled": True,
        "schedule_type": "one_time",
        "schedule": {"type": "date", "value": future},
        "flow": {"is_auto": False, "agents": ["content_creator"]},
        "quick_brief": "test",
    }
    _store.save_jobs([job])

    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    assert sched.job_exists("job_existing"), "start() ต้องโหลด job ที่มีอยู่เข้า APScheduler ได้"

    sched.stop()


def test_job_actually_fires_when_time_arrives(tmp_path, _store):
    """job ต้องยิงจริงเมื่อถึงเวลา — ไม่ใช่แค่ลงทะเบียน.

    spec ผู้ใช้: "ผ่านมาแล้วแต่ยังไม่มี worker ทำงานแม้เซิร์ฟเวอร์จะเปิดอยู่"
    จึงต้องตรวจว่า _run_job ถูกเรียกจริง ไม่ใช่แค่ job_exists
    """
    _write_config(tmp_path, 0)
    sched = Scheduler(project_root=tmp_path, web_port=9999, job_store=_store)
    sched.start()

    # ตั้งเวลาในอนาคตอันใกล้ (2 วินาที) แล้ว mock _run_job เพื่อดูว่าถูกเรียกไหม
    fired = []
    def _fake_run_job(job_id, trigger="auto"):
        fired.append((job_id, trigger))

    future = (datetime.now().astimezone() + timedelta(seconds=2)).isoformat()
    with patch.object(sched, "_run_job", _fake_run_job):
        job_id = sched.add_job({
            "name": "test-fires",
            "enabled": True,
            "schedule_type": "one_time",
            "schedule": {"type": "date", "value": future},
            "flow": {"is_auto": False, "agents": ["content_creator"]},
            "quick_brief": "test",
        })
        assert sched.job_exists(job_id), "job ต้องลงทะเบียนก่อน"

        # รอให้ถึงเวลา + buffer สำหรับ APScheduler thread
        import time
        time.sleep(4)

    assert fired, (
        "job ไม่ยิงเมื่อถึงเวลา — นี่คืออาการที่ผู้ใช้รายงาน "
        "(worker ไม่ทำงาน ทั้งที่เซิร์ฟเวอร์เปิดอยู่)"
    )
    assert fired[0][0] == job_id, "ต้องยิง job ที่ถูกตั้งไว้"

    sched.stop()
