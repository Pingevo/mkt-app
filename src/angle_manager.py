"""Angle Manager — ระบบจำประวัติมุมมอง (Angles) สำหรับ Content Creator.

เก็บประวัติมุมมองที่ AI สร้างขึ้นใน cache/{product_id}/angle_history.json
เมื่อสร้างคอนเทนต์ใหม่ จะส่งประวัติให้ AI เพื่อบอกว่า "ห้ามซ้ำอันเดิม"

โครงสร้าง angle_history.json:
{
  "angles": [
    {"angle": "ประหยัดไฟ 30%", "count": 2, "last_used": "2026-08-13T14:30:00"},
    {"angle": "เงียบเหมาะกับห้องนอน", "count": 1, "last_used": "2026-08-13T14:25:00"}
  ]
}
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _history_path(project_root: Path, product_id: str) -> Path:
    """Path to angle history file for a product."""
    return project_root / "cache" / product_id / "angle_history.json"


def load_history(project_root: Path, product_id: str) -> dict:
    """Load angle usage history for a product.

    Returns dict like {"angles": [...]}.
    """
    path = _history_path(project_root, product_id)
    if not path.exists():
        return {"angles": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "angles" not in data:
            data = {"angles": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"angles": []}


def save_history(project_root: Path, product_id: str, history: dict) -> None:
    """Save angle usage history for a product."""
    path = _history_path(project_root, product_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def get_used_angles(history: dict) -> list[str]:
    """Return list of angle names that have been used (for telling AI 'don't repeat')."""
    return [a["angle"] for a in history.get("angles", []) if a.get("angle")]


def record_usage(project_root: Path, product_id: str, angle_names: list[str]) -> None:
    """Record that these angles were used for this product."""
    history = load_history(project_root, product_id)
    angles_list = history.get("angles", [])
    now = datetime.now().isoformat()
    # Build a lookup for existing angles
    by_name = {a["angle"]: a for a in angles_list}
    for name in angle_names:
        if not name or not name.strip():
            continue
        name = name.strip()
        if name in by_name:
            by_name[name]["count"] = by_name[name].get("count", 0) + 1
            by_name[name]["last_used"] = now
        else:
            new_entry = {"angle": name, "count": 1, "last_used": now}
            angles_list.append(new_entry)
            by_name[name] = new_entry
    history["angles"] = angles_list
    save_history(project_root, product_id, history)
