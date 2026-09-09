"""Scheduler — ระบบทำงานอัตโนมัติตามเวลาที่ตั้ง.

Deep module: interface เล็ก (start/stop/add_job/remove_job/toggle/list/get_run_log)
ซ่อน APScheduler + JobStore + run log ข้างใน

JobStore seam: วันนี้ใช้ JsonJobStore (ไฟล์ JSON), อนาคตเปลี่ยนเป็น MongoJobStore ได้
โดย Scheduler ไม่ต้องแก้ — แค่สลับ adapter ตอน construct

Execution model (สำหรับ wizard UI ใหม่):
  - แต่ละ job เก็บ 1 flow ไว้
  - เมื่อถึงเวลา scheduler เรียก /api/run_flows ผ่าน httpx บน localhost
  - อ่าน SSE events ได้ files + error + done

Interface:
  Scheduler(project_root, web_port=8778).start() / .stop()
  .add_job(job_spec) → job_id
  .remove_job(job_id) → bool
  .toggle_job(job_id, enabled) → bool
  .list_jobs() → list[dict]
  .get_run_log(job_id=None, limit=50) → list[dict]
  .run_now(job_id) → bool
"""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger


# ============================================================
# JobStore seam
# ============================================================

def _parse_ts(ts: str) -> datetime | None:
    """Parse ISO timestamp — คืน None ถ้า parse ไม่ได้."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except (ValueError, TypeError):
        return None


class JobStore(Protocol):
    def load_jobs(self) -> list[dict]: ...
    def save_jobs(self, jobs: list[dict]) -> None: ...
    def load_runs(self, job_id: str | None = None, limit: int = 50) -> list[dict]: ...
    def append_run(self, run: dict) -> None: ...


class JsonJobStore:
    def __init__(self, jobs_path: Path, runs_path: Path, max_runs: int = 200, retention_days: int | None = None):
        self._jobs_path = jobs_path
        self._runs_path = runs_path
        self._max_runs = max_runs
        self._retention_days = retention_days
        self._lock = threading.Lock()

    def load_jobs(self) -> list[dict]:
        if not self._jobs_path.exists():
            return []
        try:
            data = json.loads(self._jobs_path.read_text(encoding="utf-8"))
            return data.get("jobs", [])
        except (json.JSONDecodeError, OSError):
            return []

    def save_jobs(self, jobs: list[dict]) -> None:
        with self._lock:
            self._jobs_path.parent.mkdir(parents=True, exist_ok=True)
            self._jobs_path.write_text(
                json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def load_runs(self, job_id: str | None = None, limit: int = 50) -> list[dict]:
        if not self._runs_path.exists():
            return []
        try:
            data = json.loads(self._runs_path.read_text(encoding="utf-8"))
            runs = data.get("runs", [])
        except (json.JSONDecodeError, OSError):
            return []
        if job_id:
            runs = [r for r in runs if r.get("job_id") == job_id]
        runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return runs[:limit]

    def append_run(self, run: dict) -> None:
        with self._lock:
            self._runs_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(self._runs_path.read_text(encoding="utf-8")) if self._runs_path.exists() else {"runs": []}
            except (json.JSONDecodeError, OSError):
                data = {"runs": []}
            runs = data.get("runs", [])
            runs.append(run)
            # ล้างตามอายุ — เก็บเฉพาะ run จาก N วันล่าสุด (เทียบระดับวัน ไม่ใช่วินาที)
            # ถ้า retention_days=30 → เก็บ run ที่มีวันที่ >= วันนี้ - 30 วัน
            if self._retention_days is not None:
                cutoff_date = (datetime.now().astimezone() - timedelta(days=self._retention_days)).date()
                runs = [
                    r for r in runs
                    if _parse_ts(r.get("started_at", "")) is None
                    or _parse_ts(r.get("started_at", "")).date() >= cutoff_date
                ]
            # ล้างตามจำนวน (fallback ถ้าไม่ได้ตั้ง retention_days)
            if len(runs) > self._max_runs:
                runs = runs[-self._max_runs:]
            data["runs"] = runs
            self._runs_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def update_run(self, job_id: str, started_at: str, updates: dict) -> bool:
        """Update run record ที่ระบุด้วย (job_id, started_at) — คืน True ถ้าเจอ."""
        with self._lock:
            try:
                data = json.loads(self._runs_path.read_text(encoding="utf-8")) if self._runs_path.exists() else {"runs": []}
            except (json.JSONDecodeError, OSError):
                return False
            runs = data.get("runs", [])
            found = False
            for r in runs:
                if r.get("job_id") == job_id and r.get("started_at") == started_at:
                    r.update(updates)
                    found = True
                    break
            if found:
                self._runs_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            return found


# ============================================================
# Scheduler
# ============================================================

def _load_scheduler_config(project_root: Path) -> dict:
    try:
        from src.config_loader import load_config, get_section
        cfg = load_config(project_root)
        # scheduler config อยู่ใต้ system section (ใน config/system.yaml)
        system_cfg = get_section(cfg, "system", {})
        return system_cfg.get("scheduler", {})
    except Exception:
        return {}


def _make_trigger(schedule: dict):
    trig_type = schedule.get("type", "date")
    value = schedule.get("value", "")

    if trig_type == "date":
        run_time = datetime.fromisoformat(value)
        # ถ้า naive (ไม่มี timezone) → ตีความเป็น local time ของเครื่อง
        if run_time.tzinfo is None:
            run_time = run_time.astimezone()
        return DateTrigger(run_date=run_time)
    elif trig_type == "cron":
        parts = value.split()
        if len(parts) == 5:
            return CronTrigger(minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4])
        elif len(parts) == 2:
            return CronTrigger(hour=parts[0], minute=parts[1])
        else:
            return CronTrigger(hour=0, minute=0)
    else:
        raise ValueError(f"ไม่รู้จัก schedule type: {trig_type}")


class Scheduler:
    def __init__(
        self,
        project_root: Path,
        web_port: int = 8778,
        job_store: JobStore | None = None,
        resource_store: Any = None,
    ):
        self._project_root = project_root
        self._web_port = web_port
        self._cfg = _load_scheduler_config(project_root)

        if job_store is None:
            jobs_path = project_root / self._cfg.get("job_store_path", "cache/scheduled_jobs.json")
            runs_path = project_root / self._cfg.get("run_log_path", "cache/scheduled_runs.json")
            max_runs = int(self._cfg.get("max_run_log_entries", 200))
            retention_days = self._cfg.get("run_log_retention_days")
            retention_days = int(retention_days) if retention_days is not None else None
            job_store = JsonJobStore(jobs_path, runs_path, max_runs, retention_days=retention_days)
        self._store = job_store

        # RunResourceStore for durable scheduled attachments — reuses existing
        # file-storage primitives.  Resources cloned at save time get no expires_at
        # so they survive until the job is deleted/changed.
        if resource_store is None:
            try:
                from src.run_resources import RunResourceStore
                from src.config_loader import load_config, get_section
                _rr_cfg = get_section(load_config(project_root), "run_resources", {})
                resource_store = RunResourceStore(project_root, config=_rr_cfg)
            except Exception:
                resource_store = None
        self._resource_store = resource_store

        max_concurrent = int(self._cfg.get("max_concurrent_jobs", 1))
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self._job_timeout = float(self._cfg.get("job_timeout", 600))

        misfire_grace = int(self._cfg.get("misfire_grace_time", 300))
        # APScheduler ไม่ยอมรับ 0 หรือค่าลบ — ต้องเป็น None หรือจำนวนเต็มบวก
        # 0 ใน config หมายถึง "ไม่ยิงชดเชย" → แปลงเป็น 1 (ค่า valid ที่เล็กที่สุด)
        if misfire_grace < 1:
            misfire_grace = 1
        coalesce = bool(self._cfg.get("coalesce", True))
        self._aps = BackgroundScheduler(
            job_defaults={
                "misfire_grace_time": misfire_grace,
                "coalesce": coalesce,
                "max_instances": 1,
            },
        )

        self._job_specs: dict[str, dict] = {}
        self._lock = threading.Lock()
        # live status per running job: job_id → { "status": str, "message": str, "agent": str, "started_at": str }
        self._running_status: dict[str, dict] = {}

        # ฟัง EVENT_JOB_MISSED — บันทึก "missed" ลงประวัติ แทนการยิงชดเชย
        from apscheduler.events import EVENT_JOB_MISSED
        self._aps.add_listener(self._on_job_missed, EVENT_JOB_MISSED)

    def _on_job_missed(self, event) -> None:
        """เมื่อ job พลาด (server ดับตอนถึงเวลา) — บันทึก 'missed' ลงประวัติ.

        ไม่ยิง job ใหม่ — แค่เก็บประวัติให้ user เห็นและกดรันใหม่ได้
        """
        job_id = getattr(event, "job_id", "")
        if not job_id:
            return
        jobs = self._store.load_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if not job:
            return
        self._record_missed(job)

    def start(self) -> None:
        self._aps.start()

        # ตรวจ run record ที่ค้างเป็น 'running' — เกิดจาก server ดับตอนกำลังรัน
        # mark เป็น 'interrupted' เพื่อให้ user เห็นและกดรันใหม่ได้
        self._cleanup_stuck_running()
        self._cleanup_orphaned_durable_sessions()

        jobs = self._store.load_jobs()
        now = datetime.now().astimezone()
        changed = False
        for job in jobs:
            if not job.get("enabled", True):
                continue
            try:
                # ตรวจ one_time job ที่เวลาผ่านไปแล้ว — APScheduler ไม่ส่ง EVENT_JOB_MISSED
                # กรณีนี้ ต้องบันทึก "missed" เองแล้วลบออกจากรายการ
                sched = job.get("schedule", {})
                if job.get("schedule_type") == "one_time" and sched.get("type") == "date":
                    run_ts = _parse_ts(sched.get("value", ""))
                    if run_ts and run_ts < now:
                        self._record_missed(job)
                        self._cleanup_durable_session(job)
                        job["enabled"] = False
                        job["next_run"] = ""
                        changed = True
                        continue

                trigger = _make_trigger(job.get("schedule", {}))
                if trigger is None:
                    continue
                ap_job = self._aps.add_job(
                    self._run_job,
                    trigger=trigger,
                    args=[job["id"]],
                    id=job["id"],
                    replace_existing=True,
                )
                with self._lock:
                    self._job_specs[job["id"]] = job
                    job["next_run"] = ap_job.next_run_time.isoformat() if ap_job.next_run_time else ""
            except Exception as e:
                print(f"[Scheduler] โหลด job {job.get('id', '?')} ไม่ได้: {e}", flush=True)

        # ลบ one_time job ที่ missed ออกจากรายการ (เหลือแค่ในประวัติ)
        if changed:
            jobs = [j for j in jobs if not (j.get("schedule_type") == "one_time" and not j.get("enabled"))]
            self._store.save_jobs(jobs)

    def _record_missed(self, job: dict) -> None:
        """บันทึก 'error' ลงประวัติ — ใช้เมื่อโหลด job ที่เวลาผ่านไปแล้ว."""
        now_iso = datetime.now().astimezone().isoformat()
        run_record = {
            "job_id": job.get("id", ""),
            "job_name": job.get("name", ""),
            "started_at": now_iso,
            "finished_at": now_iso,
            "status": "error",
            "session_ts": "",
            "output_files": [],
            "error": "พลาดเวลาที่กำหนด (server ไม่ได้รันตอนถึงเวลา)",
            "trigger": "auto",
            "flow": dict(job.get("flow", {})),
            "quick_brief": str(job.get("quick_brief", "")),
        }
        self._store.append_run(run_record)

    # ------------------------------------------------------------------
    # Durable scheduled attachments — clone ephemeral resources at save time
    # ------------------------------------------------------------------

    def _clone_attachments(self, flow: dict) -> dict:
        """Clone ephemeral run-resources into a durable (no-expiry) session.

        Scheduled jobs may fire days/weeks later.  The original upload_session
        has a 24h TTL, so resource_refs would be gone by fire time.  This method
        re-uploads each referenced resource into a new session with no
        expires_at, so the attachment survives until the job is deleted.

        Returns a new flow dict with upload_session_id + resource_refs pointing
        to the durable session.  Raises ValueError if any referenced resource
        is already missing/expired — the caller must surface this visibly.
        """
        refs = flow.get("resource_refs", [])
        session_id = flow.get("upload_session_id", "")
        if not refs or not session_id or self._resource_store is None:
            return flow

        # Verify all resources are resolvable before cloning
        missing = []
        for ref in refs:
            if not ref.startswith("resource:"):
                continue
            rec = self._resource_store.resolve_input_ref(ref, session_id)
            if not rec:
                missing.append(ref)
        if missing:
            raise ValueError(
                f"missing or expired scheduled attachments: {', '.join(missing)}"
            )

        # Clone each resource into a new durable session
        durable_session = self._resource_store.create_upload_session()
        new_refs: list[str] = []
        for ref in refs:
            if not ref.startswith("resource:"):
                new_refs.append(ref)
                continue
            old_id = ref.split(":", 1)[1]
            rec = self._resource_store.get_resource(old_id, session_id)
            if not rec:
                continue
            original_path = Path(rec["representations"]["original_path"])
            content = original_path.read_bytes()
            new_rec = self._resource_store.upload(
                filename=rec.get("name", "attachment"),
                content=content,
                media_type=rec.get("media_type"),
                session_id=durable_session,
                expires_at="",  # no expiry — durable
            )
            new_refs.append(f"resource:{new_rec['resource_id']}")

        new_flow = dict(flow)
        new_flow["upload_session_id"] = durable_session
        new_flow["resource_refs"] = new_refs
        return new_flow

    def _cleanup_durable_session(self, job: dict) -> None:
        """Delete the durable attachment session for a job (on delete/fire)."""
        if self._resource_store is None:
            return
        session_id = job.get("durable_session_id", "")
        if not session_id:
            return
        import shutil
        try:
            p = (self._resource_store.storage_dir / session_id).resolve()
            if p.is_relative_to(self._resource_store.storage_dir.resolve()) and p.exists():
                shutil.rmtree(p)
        except Exception:
            pass

    def _cleanup_orphaned_durable_sessions(self) -> None:
        """Delete durable sessions no longer referenced by any job or run history.

        The authoritative lifecycle boundary is run-history retention: when
        ``append_run`` evicts old runs (by ``retention_days`` or ``max_runs``),
        the session IDs they referenced are no longer in the referenced set.
        This method scans the resource store for durable sessions (resources
        with no ``expires_at``) that are not referenced by any active job or
        any remaining run-history record, and deletes them.

        Called at scheduler start and after each ``append_run`` so orphans are
        cleaned up promptly without waiting for a restart.
        """
        if self._resource_store is None:
            return
        import shutil

        # Collect all session IDs still referenced by active jobs or run history
        referenced: set[str] = set()
        for job in self._store.load_jobs():
            referenced.add(job.get("durable_session_id", ""))
            referenced.add(job.get("flow", {}).get("upload_session_id", ""))
        for run in self._store.load_runs(limit=10000):
            referenced.add(run.get("flow", {}).get("upload_session_id", ""))
        referenced.discard("")

        storage_dir = self._resource_store.storage_dir.resolve()
        for session_dir in storage_dir.iterdir():
            if not session_dir.is_dir():
                continue
            if session_dir.name in referenced:
                continue
            # Only delete durable sessions (resources with no expires_at).
            # Ephemeral sessions are handled by cleanup_expired().
            is_durable = False
            for res_dir in session_dir.iterdir():
                if not res_dir.is_dir():
                    continue
                record_path = res_dir / "resource.json"
                if not record_path.exists():
                    continue
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    if not record.get("expires_at"):
                        is_durable = True
                        break
                except (json.JSONDecodeError, OSError):
                    continue
            if is_durable:
                try:
                    shutil.rmtree(session_dir)
                except OSError:
                    pass

    def _cleanup_stuck_running(self) -> None:
        """ตรวจ run record ที่ค้าง — mark เป็น 'error'.

        2 กรณี:
        - 'running' ที่ค้าง: server ดับตอนกำลังรัน
        - 'missed'/'interrupted' เก่า: status เดิมก่อนรวมเป็น 'error'
        ทั้งหมด = error + message บอกสาเหตุ
        """
        runs = self._store.load_runs(limit=500)
        now_iso = datetime.now().astimezone().isoformat()
        for r in runs:
            status = r.get("status", "")
            if status == "running":
                self._store.update_run(
                    r.get("job_id", ""),
                    r.get("started_at", ""),
                    {
                        "status": "error",
                        "finished_at": now_iso,
                        "error": "ถูกหยุดกลางคัน (server ดับตอนกำลังรัน)",
                    },
                )
            elif status in ("missed", "interrupted"):
                # migrate status เก่า → error
                self._store.update_run(
                    r.get("job_id", ""),
                    r.get("started_at", ""),
                    {"status": "error"},
                )

    def stop(self) -> None:
        try:
            self._aps.shutdown(wait=False)
        except Exception:
            pass
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass

    def add_job(self, job_spec: dict) -> str:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        flow = job_spec.get("flow", {})

        # Clone ephemeral attachments to a durable session so they survive
        # until the scheduled fire time (the original 24h TTL would expire).
        durable_session_id = ""
        if flow.get("resource_refs") and flow.get("upload_session_id"):
            flow = self._clone_attachments(flow)
            durable_session_id = flow.get("upload_session_id", "")

        job = {
            "id": job_id,
            "name": job_spec.get("name", "unnamed"),
            "enabled": job_spec.get("enabled", True),
            "schedule_type": job_spec.get("schedule_type", "one_time"),
            "schedule": job_spec.get("schedule", {}),
            "flow": flow,
            "quick_brief": job_spec.get("quick_brief", ""),
            "durable_session_id": durable_session_id,
            "created_at": datetime.now().astimezone().isoformat(),
            "last_run": "",
            "next_run": "",
            "run_count": 0,
        }

        jobs = self._store.load_jobs()
        jobs.append(job)
        self._store.save_jobs(jobs)

        if job["enabled"]:
            try:
                trigger = _make_trigger(job["schedule"])
                if trigger:
                    ap_job = self._aps.add_job(
                        self._run_job,
                        trigger=trigger,
                        args=[job_id],
                        id=job_id,
                        replace_existing=True,
                    )
                    job["next_run"] = ap_job.next_run_time.isoformat() if ap_job.next_run_time else ""
            except Exception as e:
                print(f"[Scheduler] add_job {job_id} ลง APScheduler ไม่ได้: {e}", flush=True)

        with self._lock:
            self._job_specs[job_id] = job

        return job_id

    def remove_job(self, job_id: str) -> bool:
        jobs = self._store.load_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if not job:
            return False

        new_jobs = [j for j in jobs if j["id"] != job_id]
        self._store.save_jobs(new_jobs)
        try:
            self._aps.remove_job(job_id)
        except Exception:
            pass
        with self._lock:
            self._job_specs.pop(job_id, None)

        # Durable session is NOT unconditionally deleted here — retained run
        # history may still reference it for the existing rerun feature.
        # Orphan cleanup deletes it only when no active job AND no run-history
        # record references it.
        self._cleanup_orphaned_durable_sessions()
        return True

    def toggle_job(self, job_id: str, enabled: bool) -> bool:
        jobs = self._store.load_jobs()
        found = False
        for j in jobs:
            if j["id"] == job_id:
                j["enabled"] = enabled
                found = True
                break
        if not found:
            return False

        self._store.save_jobs(jobs)

        if enabled:
            job = next(j for j in jobs if j["id"] == job_id)
            try:
                trigger = _make_trigger(job["schedule"])
                if trigger:
                    ap_job = self._aps.add_job(
                        self._run_job,
                        trigger=trigger,
                        args=[job_id],
                        id=job_id,
                        replace_existing=True,
                    )
                    job["next_run"] = ap_job.next_run_time.isoformat() if ap_job.next_run_time else ""
            except Exception as e:
                print(f"[Scheduler] toggle enable {job_id} ไม่ได้: {e}", flush=True)
        else:
            try:
                self._aps.remove_job(job_id)
            except Exception:
                pass

        with self._lock:
            if job_id in self._job_specs:
                self._job_specs[job_id]["enabled"] = enabled

        return True

    def list_jobs(self) -> list[dict]:
        jobs = self._store.load_jobs()
        for j in jobs:
            try:
                ap_job = self._aps.get_job(j["id"])
                if ap_job and ap_job.next_run_time:
                    j["next_run"] = ap_job.next_run_time.isoformat()
            except Exception:
                pass
        return jobs

    def job_exists(self, job_id: str) -> bool:
        """ตรวจว่า job อยู่ใน APScheduler's in-memory jobstore หรือไม่.

        คืน True ถ้า job ลงทะเบียนแล้วและพร้อมยิง, False ถ้ายังไม่ได้ลงทะเบียน
        (เช่น add_job ล้มเหลวเงียบ) หรือ scheduler ไม่ได้รัน
        """
        try:
            return self._aps.get_job(job_id) is not None
        except Exception:
            return False

    def get_run_log(self, job_id: str | None = None, limit: int = 50) -> list[dict]:
        return self._store.load_runs(job_id=job_id, limit=limit)

    def get_running_status(self) -> dict[str, dict]:
        """คืนสถานะล่าสุดของ job ที่กำลังรันอยู่ { job_id: { status, message, agent, started_at } }"""
        with self._lock:
            return dict(self._running_status)

    def run_now(self, job_id: str) -> bool:
        jobs = self._store.load_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if not job:
            return False
        self._executor.submit(self._run_job, job_id, "manual")
        return True

    def rerun_run(self, job_id: str, started_at: str) -> bool:
        """กดรันใหม่จากประวัติ — โหลด flow + quick_brief จาก run record เดิม.

        ใช้ (job_id, started_at) เป็น key เพื่อระบุ run record ที่ต้องการ rerun
        เพราะ job เดียวกันอาจรันหลายครั้ง (recurring) แต่ละครั้งมี started_at ต่างกัน

        คืน True ถ้าเจอ run record และยิงสำเร็จ, False ถ้าไม่เจอ
        """
        runs = self._store.load_runs(job_id=job_id, limit=200)
        run = next((r for r in runs if r.get("started_at") == started_at), None)
        if not run:
            return False
        self._executor.submit(self._rerun_from_record, dict(run))
        return True

    def _rerun_from_record(self, source_run: dict) -> None:
        """ยิง job ใหม่จาก run record เดิม — เหมือน _run_job แต่อ่าน flow จาก record.

        ถ้า source เป็น error ที่ไม่มี output (missed/interrupted) → update original
        ถ้า source เป็น success หรือ error ที่มี output → append record ใหม่ (เก็บประวัติ)
        """
        source_status = source_run.get("status", "")
        source_job_id = source_run.get("job_id", "")
        source_started_at = source_run.get("started_at", "")
        # error ที่ไม่มี output = placeholder (พลาดเวลา/ถูกตัด) → update แทน append
        should_update_original = source_status == "error" and not source_run.get("output_files")

        started_at = datetime.now().astimezone().isoformat()
        run_record = {
            "job_id": source_job_id,
            "job_name": source_run.get("job_name", ""),
            "started_at": started_at,
            "finished_at": "",
            "status": "running",
            "session_ts": "",
            "output_files": [],
            "error": "",
            "trigger": "rerun",
            "flow": dict(source_run.get("flow", {})),
            "quick_brief": str(source_run.get("quick_brief", "")),
        }
        with self._lock:
            self._running_status[source_job_id] = {
                "status": "running",
                "message": "กำลังรันใหม่...",
                "agent": "",
                "started_at": started_at,
            }

        # ถ้า placeholder (error ไม่มี output) → update original record เป็น "running" ทันที
        # ใช้ source_started_at เป็น key คงที่ ไม่เปลี่ยน started_at จนกว่าจะรันเสร็จ
        if should_update_original:
            self._store.update_run(source_job_id, source_started_at, {
                "status": "running",
                "trigger": "rerun",
                "finished_at": "",
                "error": "",
                "output_files": [],
            })

        try:
            flow = dict(source_run.get("flow", {}))
            quick_brief = str(source_run.get("quick_brief", ""))
            output_files, session_ts, error = self._execute_flow(flow, quick_brief, source_job_id)

            run_record["finished_at"] = datetime.now().astimezone().isoformat()
            run_record["status"] = "error" if error else "success"
            run_record["session_ts"] = session_ts
            run_record["output_files"] = output_files
            run_record["error"] = error

        except Exception as e:
            run_record["finished_at"] = datetime.now().astimezone().isoformat()
            run_record["status"] = "error"
            run_record["error"] = str(e)

        with self._lock:
            self._running_status.pop(source_job_id, None)

        # placeholder → update original (ใช้ source_started_at เป็น key เดิม)
        # success/error ที่มี output → append ใหม่
        if should_update_original:
            self._store.update_run(source_job_id, source_started_at, {
                "status": run_record["status"],
                "started_at": started_at,  # อัปเดตเป็นเวลาที่รันใหม่
                "finished_at": run_record["finished_at"],
                "session_ts": run_record["session_ts"],
                "output_files": run_record["output_files"],
                "error": run_record["error"],
            })
        else:
            self._store.append_run(run_record)
            # Retention may have evicted old runs — clean up orphaned durable sessions.
            self._cleanup_orphaned_durable_sessions()

    def _execute_flow(self, flow: dict, quick_brief: str, job_id_for_status: str) -> tuple[list[str], str, str]:
        """ยิง flow ผ่าน HTTP SSE แล้ว parse events — ใช้ร่วมโดย _run_job และ _rerun_from_record.

        คืน (output_files, session_ts, error)
        """
        is_auto = bool(flow.get("is_auto", False))
        if is_auto:
            url = f"http://localhost:{self._web_port}/api/run_auto"
            payload = {
                "quick_brief": quick_brief,
                "agents": flow.get("agents", []),
                "platforms": flow.get("platforms", ["facebook", "tiktok"]),
                "media_type": flow.get("media_type", "image"),
                "media_when": flow.get("media_when", "ask"),
                "auto_image": flow.get("auto_image", False),
                "auto_video": flow.get("auto_video", False),
                "content_count": flow.get("content_count", 1),
                "product_count": flow.get("auto_count", 1) if flow.get("auto_combined", False) else 1,
                "combined": flow.get("auto_combined", False),
                "upload_session_id": flow.get("upload_session_id", ""),
                "resource_refs": flow.get("resource_refs", []),
            }
        else:
            url = f"http://localhost:{self._web_port}/api/run_flows"
            payload = {"quick_brief": quick_brief, "flows": [flow]}

        output_files: list[str] = []
        session_ts = ""
        error = ""

        with httpx.Client(timeout=httpx.Timeout(self._job_timeout)) as client:
            with client.stream("POST", url, json=payload, headers={"Accept": "text/event-stream"}) as res:
                if res.status_code != 200:
                    body = res.read() or b""
                    raise RuntimeError(f"{url} returned {res.status_code}: {body.decode('utf-8', 'replace')}")

                ct = res.headers.get("content-type", "")
                if "text/event-stream" not in ct:
                    body = res.read() or b""
                    raise RuntimeError(f"{url} ไม่ใช่ SSE (content-type: {ct}): {body.decode('utf-8', 'replace')[:500]}")

                received_any = False
                for line in res.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    received_any = True
                    data = json.loads(line[6:])
                    ev_type = data.get("type", "")

                    if ev_type in ("agent_start", "status", "agent_done", "selection", "error"):
                        msg = data.get("message") or data.get("text") or ""
                        agent = data.get("agent", "")
                        with self._lock:
                            if job_id_for_status in self._running_status:
                                self._running_status[job_id_for_status]["message"] = msg
                                if agent:
                                    self._running_status[job_id_for_status]["agent"] = agent

                    if ev_type == "agent_done":
                        fpath = data.get("file", "")
                        if fpath and fpath not in output_files:
                            output_files.append(fpath)
                        st = data.get("session_ts", "")
                        if st:
                            session_ts = st
                    elif ev_type == "error":
                        error = data.get("text", "unknown error")
                    elif ev_type == "done":
                        break

                if not received_any and not error:
                    error = "ไม่ได้รับ SSE event ใดจาก server"

        return output_files, session_ts, error

    def _run_job(self, job_id: str, trigger: str = "auto") -> None:
        jobs = self._store.load_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if not job:
            print(f"[Scheduler] job {job_id} ไม่พบ", flush=True)
            return

        started_at = datetime.now().astimezone().isoformat()
        run_record = {
            "job_id": job_id,
            "job_name": job.get("name", ""),
            "started_at": started_at,
            "finished_at": "",
            "status": "running",
            "session_ts": "",
            "output_files": [],
            "error": "",
            "trigger": trigger,
            "flow": dict(job.get("flow", {})),
            "quick_brief": str(job.get("quick_brief", "")),
        }
        # mark as running in live status
        with self._lock:
            self._running_status[job_id] = {
                "status": "running",
                "message": "เริ่มรัน...",
                "agent": "",
                "started_at": started_at,
            }

        try:
            flow = dict(job.get("flow", {}))
            quick_brief = str(job.get("quick_brief", ""))
            output_files, session_ts, error = self._execute_flow(flow, quick_brief, job_id)

            run_record["finished_at"] = datetime.now().astimezone().isoformat()
            run_record["status"] = "error" if error else "success"
            run_record["session_ts"] = session_ts
            run_record["output_files"] = output_files
            run_record["error"] = error

        except Exception as e:
            run_record["finished_at"] = datetime.now().astimezone().isoformat()
            run_record["status"] = "error"
            run_record["error"] = str(e)

        # clear live status
        with self._lock:
            self._running_status.pop(job_id, None)

        self._store.append_run(run_record)
        # Retention may have evicted old runs — clean up orphaned durable sessions.
        self._cleanup_orphaned_durable_sessions()

        with self._lock:
            jobs = self._store.load_jobs()
            job_type = ""
            for j in jobs:
                if j["id"] == job_id:
                    j["last_run"] = started_at
                    j["run_count"] = int(j.get("run_count", 0)) + 1
                    job_type = j.get("schedule_type", "")
                    break

            # one-time job ที่รันแล้ว → ลบออกจากรายการ (เหลือแค่ในประวัติ)
            # NOTE: durable attachment session is NOT deleted here — run history
            # retains the flow with upload_session_id/resource_refs so the
            # existing rerun feature can reconstruct the same run.  Cleanup is
            # owned by run-history retention eviction (see _cleanup_orphaned_durable_sessions).
            if job_type == "one_time":
                jobs = [j for j in jobs if j["id"] != job_id]
                try:
                    self._aps.remove_job(job_id)
                except Exception:
                    pass

            self._store.save_jobs(jobs)
