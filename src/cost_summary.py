"""Cost summary — รวมค่าใช้จ่าย LLM ทั้งหมดของ flow หนึ่ง แล้วเซฟเป็นไฟล์หลังบ้าน.

อ่าน ``logs/llm_usage.jsonl`` กรองเฉพาะ entries ที่มี ``flow_id`` ตรงกับที่ระบุ
แล้วเขียนไฟล์ ``_cost_summary_{flow_id}.json`` ลงใน output folder ของ flow นั้น
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .ai_usage import USAGE_LOG_PATH as _USAGE_LOG_PATH
except ImportError:
    from ai_usage import USAGE_LOG_PATH as _USAGE_LOG_PATH  # type: ignore


def collect_flow_entries(flow_id: str) -> list[dict[str, Any]]:
    """อ่าน llm_usage.jsonl แล้วกรองเฉพาะ entries ที่ flow_id ตรง — คืน list."""
    if not _USAGE_LOG_PATH.exists():
        return []
    entries: list[dict[str, Any]] = []
    with open(_USAGE_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("flow_id") == flow_id:
                entries.append(d)
    return entries


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """รวม cost จาก entries — คืน dict สรุปยอดรวม + breakdown."""
    total_cost = 0.0
    total_calls = 0
    by_source: dict[str, float] = {}
    by_operation: dict[str, float] = {}
    by_model: dict[str, float] = {}
    tokens_in = 0
    tokens_out = 0
    started = ""
    finished = ""

    for e in entries:
        cost = e.get("cost_usd") or 0.0
        total_cost += cost
        total_calls += 1
        src = e.get("source", "?")
        op = e.get("operation", "?")
        model = e.get("model", "?")
        by_source[src] = by_source.get(src, 0.0) + cost
        by_operation[op] = by_operation.get(op, 0.0) + cost
        by_model[model] = by_model.get(model, 0.0) + cost
        tokens_in += e.get("prompt_tokens") or 0
        tokens_out += e.get("completion_tokens") or 0
        ts = e.get("timestamp", "")
        if ts and (not started or ts < started):
            started = ts
        if ts and ts > finished:
            finished = ts

    return {
        "total_cost_usd": round(total_cost, 6),
        "total_calls": total_calls,
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "started_at": started,
        "finished_at": finished,
        "by_source": {k: round(v, 6) for k, v in sorted(by_source.items(), key=lambda x: -x[1])},
        "by_operation": {k: round(v, 6) for k, v in sorted(by_operation.items(), key=lambda x: -x[1])},
        "by_model": {k: round(v, 6) for k, v in sorted(by_model.items(), key=lambda x: -x[1])},
    }


def write_cost_summary(
    output_dir: Path,
    flow_id: str,
    *,
    label: str = "",
    agents: list[str] | None = None,
    products: str = "",
) -> Path | None:
    """รวม cost ของ flow_id แล้วเขียนไฟล์ summary ลง output_dir.

    คืน path ของไฟล์ที่เขียน หรือ None ถ้าไม่มี entry ของ flow_id นี้เลย
    """
    entries = collect_flow_entries(flow_id)
    if not entries:
        return None

    summary = summarize(entries)
    summary["flow_id"] = flow_id
    summary["label"] = label
    summary["agents"] = agents or []
    summary["products"] = products
    summary["written_at"] = datetime.now().isoformat()

    out_path = output_dir / f"_cost_summary_{flow_id}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


# ============================================================
# Flow metadata — mapping output file → flow_id
# เก็บใน _flow_meta_{flow_id}.json ใน session folder
# ใช้ตอน "สร้างสื่อภายหลัง" เพื่อหา flow_id ของ output file นั้น
# ============================================================

def write_flow_meta(
    output_dir: Path,
    flow_id: str,
    output_files: list[str],
    *,
    label: str = "",
    agents: list[str] | None = None,
) -> Path | None:
    """เขียนไฟล์ _flow_meta_{flow_id}.json — เก็บ mapping flow_id → output_files.

    ใช้ตอน flow จบ เพื่อให้ "สร้างสื่อภายหลัง" หา flow_id ของ output file ได้
    คืน path ของไฟล์ที่เขียน หรือ None ถ้าไม่มี output_files
    """
    if not output_files:
        return None
    meta = {
        "flow_id": flow_id,
        "label": label,
        "agents": agents or [],
        "output_files": output_files,
        "written_at": datetime.now().isoformat(),
    }
    out_path = output_dir / f"_flow_meta_{flow_id}.json"
    out_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def find_flow_id_for_file(output_dir: Path, file_path: str | Path) -> str:
    """หา flow_id ของ output file จาก _flow_meta_*.json ทั้งหมดใน output_dir.

    เปรียบเทียบทั้ง absolute path และ relative path (ตัวท้าย)
    คืน flow_id ถ้าเจอ ไม่งั้นคืน ""
    """
    if not output_dir.exists():
        return ""
    target = Path(file_path)
    target_name = target.name
    target_str = str(target)
    for meta_file in output_dir.glob("_flow_meta_*.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for f in meta.get("output_files", []):
            if f == target_str or f == target_name or Path(f).name == target_name:
                return meta.get("flow_id", "")
    return ""


def find_any_flow_id(output_dir: Path) -> str:
    """หา flow_id แรกที่เจอใน output_dir.

    ลองจาก _flow_meta_*.json ก่อน ถ้าไม่มี ลองจาก _cost_summary_*.json
    (กรณีเซิร์ฟเวอร์รันโค้ดเก่าที่ยังไม่มี write_flow_meta)

    คืน flow_id ถ้าเจอ ไม่งั้นคืน ""
    """
    if not output_dir.exists():
        return ""
    # ลองจาก _flow_meta_*.json ก่อน
    for meta_file in output_dir.glob("_flow_meta_*.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            fid = meta.get("flow_id", "")
            if fid:
                return fid
        except (json.JSONDecodeError, OSError):
            continue
    # fallback: ลองจาก _cost_summary_*.json (ชื่อไฟล์ = _cost_summary_{flow_id}.json)
    for summary_file in output_dir.glob("_cost_summary_*.json"):
        name = summary_file.name
        prefix = "_cost_summary_"
        suffix = ".json"
        if name.startswith(prefix) and name.endswith(suffix):
            fid = name[len(prefix):-len(suffix)]
            if fid:
                return fid
    return ""


def update_cost_summary(
    output_dir: Path,
    flow_id: str,
) -> Path | None:
    """อ่าน cost summary เดิม (ถ้ามี) แล้วอัปเดตด้วย entries ล่าสุด.

    ใช้ตอน "สร้างสื่อภายหลัง" จบ — รวม cost ใหม่เข้าไปในไฟล์เดิม
    คืน path ของไฟล์ที่เขียน หรือ None ถ้าไม่มี entry ของ flow_id
    """
    entries = collect_flow_entries(flow_id)
    if not entries:
        return None

    # อ่าน label/agents/products จากไฟล์เดิม (ถ้ามี)
    existing_path = output_dir / f"_cost_summary_{flow_id}.json"
    label = ""
    agents: list[str] = []
    products = ""
    if existing_path.exists():
        try:
            old = json.loads(existing_path.read_text(encoding="utf-8"))
            label = old.get("label", "")
            agents = old.get("agents", [])
            products = old.get("products", "")
        except (json.JSONDecodeError, OSError):
            pass

    return write_cost_summary(
        output_dir, flow_id,
        label=label, agents=agents, products=products,
    )
