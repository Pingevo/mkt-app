"""Content History — เก็บประวัติคอนเทนต์ข้ามสินค้า เพื่อไม่ให้คอนเทนต์ซ้ำกัน.

ใช้ใน auto mode: ตอน agent เลือกสินค้าเอง จะเห็นประวัติว่า
เคยทำคอนเทนต์อะไรไปแล้วบ้าง (สินค้าไหน + แนวคิดอะไร + caption สั้น)
เพื่อเลือกสินค้า/แนวคิดที่ยังไม่ซ้ำ

เก็บใน cache/content_history.json (global — ไม่ใช่ per-product)

โครงสร้าง:
{
  "entries": [
    {
      "product_ids": ["Lagenio K5"],
      "concept": "รีวิวฟังก์ชัน GPS",
      "platform": "TikTok",
      "caption_summary": "K5 มี GPS แม่่นยำ...",
      "embedding": [0.01, -0.03, ...],
      "timestamp": "2026-08-14T10:30:00"
    }
  ]
}

Dedup ใช้ embeddings + cosine similarity (ตามมาตรฐานตลาด: MarquIQ, Apaya, Gemini Lab)
ถ้า similarity > threshold → ซ้ำ → ปฏิเสธ
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Global lock — กัน race condition เมื่อหลาย flow รันพร้อมกัน
# ทั้ง read (get_recent_entries) และ write (record_entry) ต้องผ่าน lock นี้
_history_lock = threading.RLock()

try:
    from .ai_usage import record_ai_usage, make_entry
except ImportError:
    from ai_usage import record_ai_usage, make_entry  # type: ignore

# Default config — ใช้ตอนที่ไม่มี config ส่งเข้ามา (backward compat)
_DEFAULTS = {
    "max_entries": 200,
    "default_limit": 50,
    "caption_summary_length": 500,
    "caption_display_length": 150,
    "dedup_enabled": True,
    "dedup_model": "openai/text-embedding-3-small",
    "dedup_similarity_threshold": 0.85,
    "dedup_window_days": 30,
}


def _history_path(project_root: Path) -> Path:
    """Path to global content history file."""
    return project_root / "cache" / "content_history.json"


def load_history(project_root: Path) -> dict:
    """Load global content history. Returns {"entries": [...]}."""
    with _history_lock:
        path = _history_path(project_root)
        if not path.exists():
            return {"entries": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "entries" not in data:
                data = {"entries": []}
            return data
        except (json.JSONDecodeError, OSError):
            return {"entries": []}


def save_history(project_root: Path, history: dict) -> None:
    """Save global content history."""
    with _history_lock:
        path = _history_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def get_recent_entries(
    project_root: Path,
    limit: int | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict]:
    """Return recent content entries (newest first) for telling AI 'don't repeat'."""
    cfg = config or _DEFAULTS
    if limit is None:
        limit = cfg.get("default_limit", 50)
    history = load_history(project_root)
    entries = history.get("entries", [])
    sorted_entries = sorted(entries, key=lambda e: e.get("timestamp", ""), reverse=True)
    return sorted_entries[:limit]


def get_entries_for_product(
    project_root: Path,
    product_id: str,
    limit: int | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict]:
    """Return content entries for a specific product (newest first).

    ใช้ใน manual mode — ดูว่าสินค้านี้เคยทำแนวคิดอะไรไปแล้ว เพื่อไม่ให้ซ้ำ
    """
    cfg = config or _DEFAULTS
    if limit is None:
        limit = cfg.get("default_limit", 50)
    history = load_history(project_root)
    entries = [
        e for e in history.get("entries", [])
        if product_id in (e.get("product_ids") or [e.get("product_id", "")])
    ]
    sorted_entries = sorted(entries, key=lambda e: e.get("timestamp", ""), reverse=True)
    return sorted_entries[:limit]


def record_entry(
    project_root: Path,
    product_ids: list[str] | str,
    concept: str,
    platform: str,
    caption_summary: str,
    config: dict[str, Any] | None = None,
    embedding: list[float] | None = None,
    pillar: str = "",
    output_file: str = "",
) -> bool:
    """Record that a content piece was generated.

    Args:
        product_ids: สินค้าที่ทำ (1 หรือหลายชิ้น) — รับทั้ง str และ list (backward compat)
        concept: แนวคิด/มุมมองที่ใช้ (เดิมชื่อ angle)
        platform: แพลตฟอร์ม
        caption_summary: caption สั้นๆ สำหรับอ้างอิง
        config: content_history config section
        embedding: embedding vector สำหรับ dedup (optional — ถ้าไม่ส่งจะ generate ถ้า dedup_enabled)
        pillar: Content Pillar ที่ใช้ (optional — สำหรับหมุนเวียน)
        output_file: path ของไฟล์ output ที่สร้าง entry นี้ (optional — สำหรับลบ history ตอนลบ output)

    Returns:
        True ถ้าบันทึกสำเร็จ, False ถ้าเป็นซ้ำ (race condition กับ flow อื่นที่รันพร้อมกัน)

    Thread-safety: ใช้ _history_lock ครอบทั้ง read + check + write
    เพื่อกัน race condition เมื่อหลาย flow รันพร้อมกัน —
    flow ที่บันทึกทีหลังจะ re-check ซ้ำกับ entry ที่ flow แรกเพิ่งบันทึก
    """
    cfg = config or _DEFAULTS
    if isinstance(product_ids, str):
        product_ids = [product_ids]

    # Generate embedding ถ้า dedup เปิดอยู่และไม่ได้ส่งมา
    if embedding is None and cfg.get("dedup_enabled", True) and caption_summary:
        embedding = _generate_embedding(caption_summary, cfg)

    # Atomic check-and-record ภายใต้ lock — กัน race condition ระหว่าง flow ขนาน
    with _history_lock:
        history = load_history(project_root)
        entries = history.get("entries", [])

        # Re-check duplicate ตอนบันทึก — กันกรณี flow อื่นบันทึกก่อนเรา
        # (ตอนเรา generate อยู่ flow นั้นอาจเพิ่งบันทึก entry ใหม่เข้าไป)
        if cfg.get("dedup_enabled", True) and embedding and caption_summary:
            window_days = cfg.get("dedup_window_days", 30)
            threshold = cfg.get("dedup_similarity_threshold", 0.85)
            cutoff = datetime.now() - timedelta(days=window_days)
            for entry in entries:
                try:
                    entry_time = datetime.fromisoformat(entry.get("timestamp", ""))
                    if entry_time < cutoff:
                        continue
                except (ValueError, TypeError):
                    continue
                entry_embedding = entry.get("embedding")
                if not entry_embedding:
                    continue
                sim = _cosine_similarity(embedding, entry_embedding)
                if sim >= threshold:
                    # ซ้ำกับ entry ที่ flow อื่นเพิ่งบันทึก → ไม่บันทึกซ้ำ
                    return False

        entry = {
            "product_ids": product_ids,
            "concept": concept,
            "platform": platform,
            "caption_summary": caption_summary[:cfg.get("caption_summary_length", 500)],
            "embedding": embedding,
            "timestamp": datetime.now().isoformat(),
        }
        if pillar:
            entry["pillar"] = pillar
        if output_file:
            entry["output_file"] = output_file
        entries.append(entry)

        # จำกัดจำนวน entries สูงสุด (ป้องกันไฟล์ใหญ่เกิน)
        max_entries = cfg.get("max_entries", 200)
        if len(entries) > max_entries:
            entries = entries[-max_entries:]
        history["entries"] = entries
        save_history(project_root, history)
        return True


def delete_entry_by_output_file(project_root: Path, output_file: str) -> int:
    """ลบ history entries ที่เกี่ยวข้องกับไฟล์ output ที่ระบุ.

    ใช้ตอน user ลบ output — ลบ history ด้วยเพื่อไม่ให้ dedup บล็อก
    คอนเทนต์ที่ user ไม่ได้ใช้แล้ว

    Returns: จำนวน entries ที่ถูกลบ
    """
    history = load_history(project_root)
    entries = history.get("entries", [])
    before = len(entries)
    kept = [e for e in entries if e.get("output_file") != output_file]
    removed = before - len(kept)
    if removed > 0:
        history["entries"] = kept
        save_history(project_root, history)
    return removed


def update_last_entry_output_file(project_root: Path, output_file: str) -> bool:
    """อัปเดต output_file ของ entry ล่าสุด.

    ใช้ตอน auto mode — orchestrator บันทึก history ก่อน แล้ว web_viewer เซฟไฟล์ทีหลัง
    เราอัปเดต entry ล่าสุดให้มี output_file หลังจากเซฟไฟล์แล้ว

    Returns: True ถ้าอัปเดตสำเร็จ
    """
    history = load_history(project_root)
    entries = history.get("entries", [])
    if not entries:
        return False
    entries[-1]["output_file"] = output_file
    save_history(project_root, history)
    return True


def format_history_for_prompt(
    project_root: Path,
    limit: int | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Format content history as text for LLM prompt.

    บอก AI ว่าเคยทำคอนเทนต์อะไรไปแล้ว เพื่อหลีกเลี่ยงการทำซ้ำ
    ใช้ใน auto mode — แสดงประวัติทุกสินค้า
    """
    cfg = config or _DEFAULTS
    entries = get_recent_entries(project_root, limit=limit, config=cfg)
    if not entries:
        return ""
    display_len = cfg.get("caption_display_length", 150)
    lines = ["--- ประวัติคอนเทนต์ที่สร้างไปแล้ว (ห้ามซ้ำ) ---"]
    for e in entries:
        pids = e.get("product_ids") or [e.get("product_id", "?")]
        pid_str = " + ".join(pids)
        concept = e.get("concept") or e.get("angle", "?")  # backward compat
        platform = e.get("platform", "?")
        summary = e.get("caption_summary", "")[:display_len]
        lines.append(f"• [{platform}] {pid_str} — {concept} — {summary}")
    lines.append("--- สิ้นสุดประวัติ ---")
    return "\n".join(lines)


def format_product_history_for_prompt(
    project_root: Path,
    product_id: str,
    limit: int | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Format content history for a specific product — ใช้ใน manual mode.

    แสดงเฉพาะประวัติของสินค้านี้ เพื่อบอก AI ว่า "สินค้านี้เคยทำอะไรไปแล้ว ห้ามซ้ำ"
    """
    cfg = config or _DEFAULTS
    entries = get_entries_for_product(project_root, product_id, limit=limit, config=cfg)
    if not entries:
        return ""
    display_len = cfg.get("caption_display_length", 150)
    lines = [f"--- ประวัติคอนเทนต์ของ {product_id} ที่สร้างไปแล้ว (ห้ามซ้ำ) ---"]
    for e in entries:
        concept = e.get("concept") or e.get("angle", "?")
        platform = e.get("platform", "?")
        summary = e.get("caption_summary", "")[:display_len]
        lines.append(f"• [{platform}] {concept} — {summary}")
    lines.append("--- สิ้นสุดประวัติ ---")
    return "\n".join(lines)


def _log_embedding(
    model: str,
    usage: dict[str, Any] | None,
    *,
    duration_ms: int,
    status: str = "success",
    http_status: int | None = None,
    error_message: str | None = None,
    request_id: str | None = None,
) -> None:
    """บันทึก embeddings usage — fire-and-forget."""
    entry = make_entry(
        provider="openrouter",
        model=model,
        operation="embeddings.create",
        source="content_history.generate_embedding",
        request_id=request_id,
        duration_ms=duration_ms,
        status=status,
        http_status=http_status,
        error_message=error_message,
        raw_usage=usage,
    )
    if usage:
        cost = usage.get("cost") or usage.get("total_cost")
        if cost is not None:
            entry["cost_usd"] = float(cost)
        if usage.get("prompt_tokens") is not None:
            entry["prompt_tokens"] = usage.get("prompt_tokens")
    record_ai_usage(entry)


# ============================================================
# Embeddings dedup — ตามมาตรฐานตลาด
# ============================================================

def _generate_embedding(text: str, config: dict[str, Any]) -> list[float] | None:
    """Generate embedding vector for text using OpenRouter embeddings API.

    Uses the model specified in config (dedup_model).
    Returns None if API call fails (graceful degradation).
    """
    model = config.get("dedup_model", "openai/text-embedding-3-small")
    t0 = time.time()
    try:
        import os
        from dotenv import load_dotenv
        load_dotenv()
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return None

        import httpx
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "input": text[:8000],  # embedding API จำกัด input length
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # log usage (Hub + local)
            _log_embedding(model, data.get("usage"), request_id=data.get("id"),
                           duration_ms=int((time.time() - t0) * 1000))
            return data["data"][0]["embedding"]
    except Exception as e:
        http_status: int | None = None
        request_id: str | None = None
        if isinstance(e, httpx.HTTPStatusError):
            http_status = e.response.status_code
            try:
                request_id = e.response.json().get("id")
            except Exception:
                pass
        status = "timeout" if isinstance(e, httpx.TimeoutException) else "error"
        _log_embedding(model, None, request_id=request_id,
                       duration_ms=int((time.time() - t0) * 1000), status=status,
                       http_status=http_status, error_message=str(e))
        # Graceful degradation — ถ้า embedding API ไม่ได้ ก็ไม่เก็บ embedding
        return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def check_duplicate(
    project_root: Path,
    caption: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check if a caption is too similar to any existing content.

    ตามมาตรฐานตลาด: ใช้ embeddings + cosine similarity
    ถ้า similarity > threshold → ซ้ำ

    Returns:
        {
            "is_duplicate": bool,
            "similarity": float,  # highest similarity score
            "matched_entry": dict | None,  # entry ที่ซ้ำ
        }
    """
    cfg = config or _DEFAULTS
    if not cfg.get("dedup_enabled", True):
        return {"is_duplicate": False, "similarity": 0.0, "matched_entry": None}

    # Generate embedding สำหรับ caption ใหม่
    new_embedding = _generate_embedding(caption, cfg)
    if new_embedding is None:
        # ไม่สามารถ generate embedding ได้ → ข้าม dedup (graceful)
        return {"is_duplicate": False, "similarity": 0.0, "matched_entry": None}

    # ดึง entries ใน window ที่กำหนด
    window_days = cfg.get("dedup_window_days", 30)
    threshold = cfg.get("dedup_similarity_threshold", 0.85)

    history = load_history(project_root)
    cutoff = datetime.now() - timedelta(days=window_days)

    best_sim = 0.0
    best_entry = None

    for entry in history.get("entries", []):
        # กรองตาม window
        try:
            entry_time = datetime.fromisoformat(entry.get("timestamp", ""))
            if entry_time < cutoff:
                continue
        except (ValueError, TypeError):
            continue

        entry_embedding = entry.get("embedding")
        if not entry_embedding:
            continue

        sim = _cosine_similarity(new_embedding, entry_embedding)
        if sim > best_sim:
            best_sim = sim
            best_entry = entry

    return {
        "is_duplicate": best_sim >= threshold,
        "similarity": best_sim,
        "matched_entry": best_entry,
    }
