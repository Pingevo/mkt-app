"""Regression: output paths must be unique per run even when timestamps collide."""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _frozen_datetime(fixed: datetime):
    class _Frozen(datetime):
        @classmethod
        def now(cls):
            return fixed
    return _Frozen


def test_save_result_does_not_overwrite_same_second(tmp_path, monkeypatch):
    """สองรอบทีจบในวินาทีเดียวกันต้องไม่ทับไฟล์กัน."""
    from src.orchestrator import Orchestrator

    fixed = datetime(2024, 1, 1, 12, 0, 0)
    monkeypatch.setattr("src.orchestrator.datetime", _frozen_datetime(fixed))

    orch = Orchestrator.__new__(Orchestrator)
    orch.product_id = "prodA"

    out = tmp_path / "out"

    orch.results = {"product_spec": "first"}
    p1 = orch.save_result("product_spec", out)["product_spec"]

    orch.results = {"product_spec": "second"}
    p2 = orch.save_result("product_spec", out)["product_spec"]

    assert p1 != p2, "save_result ต้องสร้างไฟล์คนละชื่อเมื่อเวลาตรงกัน"
    assert p1.read_text(encoding="utf-8") == "first"
    assert p2.read_text(encoding="utf-8") == "second"


def test_session_ts_label_unique_per_call(monkeypatch):
    """ชื่อ session folder ต้องต่างกันแม้เรียกในชื่อเวลาเดียวกัน."""
    import web_viewer

    fixed = datetime(2024, 1, 1, 12, 0, 0, 0)
    monkeypatch.setattr(web_viewer, "datetime", _frozen_datetime(fixed))
    monkeypatch.setattr(web_viewer, "_sys_cfg", lambda: {"filename_max_length": 30})

    labels = [web_viewer._session_ts_label(["prodA"]) for _ in range(20)]
    assert len(set(labels)) == len(labels), f"_session_ts_label ซ้ำกัน: {labels[:3]}"
