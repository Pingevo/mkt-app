"""Product segmentation — แยก catalog ที่มีหลายสินค้าออกจากไฟล์เดียว.

วิธีสากล (เหมือน Claude, ChatGPT, Cursor, Devin):
  - ส่ง text/PDF ต้นฉบับให้ LLM พร้อมหมายเลขบรรทัด
  - LLM ระบุเฉพาะ: ชื่อสินค้า, รหัส, หมวด, สรุป, และ **ตำแหน่ง** ในต้นฉบับ
  - ระบบคัด text จากต้นฉบับตามตำแหน่งเอง — LLM ไม่เขียนสเปก/ราคาใหม่
    (ป้องกัน hallucination ของราคา/spec ที่สำคัญต่อการตลาด)

Interface:
  segment_products(files, llm, config=None) -> dict
    files: list[{name, path, text, type}]
    คืน: {
      "mode": "single"|"multi",
      "products": [{
        "product_key", "suggested_name", "category", "summary",
        "source_refs", "common_refs", "text"  # text = คัดจากต้นฉบับ
      }],
      "error": str?,  # ถ้า LLM ตอบผิด schema หรือ validation ไม่ผ่าน
    }

ถ้าไม่มี LLM → single-product flow เดิม (deterministic, ไม่เสีย token)
ถ้า LLM ตอบ 1 สินค้า → single mode
ถ้า LLM ตอบ N สินค้า → multi mode + คัด text แยก
"""
from __future__ import annotations

import json
import re
from typing import Any

from .config_loader import load_config, get_section


def _strip_code_fence(text: str) -> str:
    """Strip markdown code fences (```json ... ```) จาก LLM response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


# ------------------------------------------------------------------
#  Schema สำหรับ Structured Outputs
# ------------------------------------------------------------------

_SEGMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_key": {
                        "type": "string",
                        "description": "รหัสสินค้าที่ใช้ dedup + scope re-ingest (เช่น K67, K72)",
                    },
                    "suggested_name": {
                        "type": "string",
                        "description": "ชื่อที่เสนอ (brand + model เช่น CACGO K67)",
                    },
                    "category": {"type": "string"},
                    "summary": {"type": "string", "description": "สรุปสั้น ไม่เกิน 50 คำ"},
                    "source_refs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "page": {"type": "integer", "description": "หมายเลขหน้า PDF (optional)"},
                                "line_start": {"type": "integer"},
                                "line_end": {"type": "integer"},
                            },
                            "required": ["file", "line_start", "line_end"],
                        },
                        "description": "ช่วงบรรทัดใน text ต้นฉบับที่เป็นข้อมูลของสินค้านี้",
                    },
                    "common_refs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "page": {"type": "integer"},
                                "line_start": {"type": "integer"},
                                "line_end": {"type": "integer"},
                            },
                            "required": ["file", "line_start", "line_end"],
                        },
                        "description": "ช่วงข้อมูลร่วม (MOQ, payment terms) ใส่ในทุกสินค้า",
                    },
                },
                "required": ["product_key", "suggested_name", "category", "summary", "source_refs"],
            },
        }
    },
    "required": ["products"],
}


# ------------------------------------------------------------------
#  Config
# ------------------------------------------------------------------

def _seg_config() -> dict:
    """อ่าน product_segmentation ที่อยู่ใต้ ingestion config."""
    try:
        ingestion = get_section(load_config(), "ingestion", {})
        return ingestion.get("product_segmentation", {})
    except Exception:
        return {}


def _ingestion_config() -> dict:
    """อ่าน ingestion config หลัก — สำหรับ model/timeout fallback."""
    try:
        cfg = load_config()
        return get_section(cfg, "ingestion", {})
    except Exception:
        return {}


# ------------------------------------------------------------------
#  Text slicing — คัด text จากต้นฉบับตาม line ranges
# ------------------------------------------------------------------

def _slice_lines(text: str, line_start: int, line_end: int) -> str:
    """คัดบรรทัด line_start ถึง line_end (1-based, inclusive) จาก text.

    ถ้า range เกินขอบ → ตัดให้ (ไม่ error — ปลอดภัยกว่า throw)
    ถ้า range ผิด (start > end หรือ < 1) → คืน ""
    """
    if line_start < 1 or line_end < line_start:
        return ""
    lines = text.split("\n")
    # ปรับให้ไม่เกินขอบจริง
    start_idx = max(0, line_start - 1)
    end_idx = min(len(lines), line_end)
    if start_idx >= len(lines) or end_idx <= start_idx:
        return ""
    return "\n".join(lines[start_idx:end_idx])


def _slice_refs(text_by_file: dict[str, str], refs: list[dict]) -> str:
    """คัด text จากหลาย refs ในไฟล์เดียวกันหรือต่างไฟล์ → รวมเป็น string."""
    parts: list[str] = []
    for ref in refs:
        fname = ref.get("file", "")
        text = text_by_file.get(fname, "")
        if not text:
            continue
        chunk = _slice_lines(text, ref.get("line_start", 0), ref.get("line_end", 0))
        if chunk:
            parts.append(chunk)
    return "\n".join(parts)


# ------------------------------------------------------------------
#  Validation — ตรวจ source_refs ก่อน materialize
# ------------------------------------------------------------------

def _validate_refs(refs: list[dict], file_names: set[str], text_by_file: dict[str, str]) -> list[str]:
    """ตรวจ source_refs — คืน list ของ error messages (ว่าง = ผ่าน).

    ตรวจ:
      - file ต้องมีในรายการไฟล์ที่ส่งมา
      - line_start/line_end ต้องเป็นจำนวนเต็มบวก
      - line_start <= line_end
      - ช่วงต้องไม่ว่าง (มี text จริง)
    """
    errors: list[str] = []
    for i, ref in enumerate(refs):
        fname = ref.get("file", "")
        if fname not in file_names:
            errors.append(f"ref[{i}]: file '{fname}' ไม่อยู่ในรายการไฟล์")
            continue
        ls = ref.get("line_start", 0)
        le = ref.get("line_end", 0)
        if not isinstance(ls, int) or not isinstance(le, int):
            errors.append(f"ref[{i}]: line_start/line_end ต้องเป็นจำนวนเติม")
            continue
        if ls < 1 or le < ls:
            errors.append(f"ref[{i}]: range ไม่ถูกต้อง ({ls}-{le})")
            continue
        # ตรวจว่ามี text จริงในช่วงนี้
        chunk = _slice_lines(text_by_file.get(fname, ""), ls, le)
        if not chunk.strip():
            errors.append(f"ref[{i}]: ช่วง {ls}-{le} ใน '{fname}' ว่าง")
    return errors


def _validate_segment(seg: dict, file_names: set[str], text_by_file: dict[str, str]) -> list[str]:
    """ตรวจ 1 segment — คืน error messages."""
    errors: list[str] = []
    if not seg.get("product_key"):
        errors.append("product_key ว่าง")
    if not seg.get("suggested_name"):
        errors.append(f"product_key={seg.get('product_key','?')}: suggested_name ว่าง")
    refs = seg.get("source_refs", [])
    if not refs:
        errors.append(f"product_key={seg.get('product_key','?')}: source_refs ว่าง")
    errors.extend(_validate_refs(refs, file_names, text_by_file))
    common = seg.get("common_refs", [])
    errors.extend(_validate_refs(common, file_names, text_by_file))
    return errors


# ------------------------------------------------------------------
#  Dedup — รวม segment ที่ซ้ำกัน (product_key เดียวกันข้าม batch)
# ------------------------------------------------------------------

def _merge_segments(segments: list[dict]) -> list[dict]:
    """รวม segments ที่มี product_key ซ้ำ — รักษาลำดับเดิม.

    ถ้า product_key ซ้ำ → รวม source_refs + common_refs (union แบบรักษาลำดับ)
    """
    seen: dict[str, dict] = {}
    order: list[str] = []
    for seg in segments:
        key = seg["product_key"]
        if key in seen:
            existing = seen[key]
            existing["source_refs"].extend(seg.get("source_refs", []))
            existing["common_refs"].extend(seg.get("common_refs", []))
        else:
            seen[key] = {**seg, "source_refs": list(seg.get("source_refs", [])), "common_refs": list(seg.get("common_refs", []))}
            order.append(key)
    return [seen[k] for k in order]


def _page_boundaries(text: str) -> list[tuple[int, int, int]]:
    """Return (page_num, start_line, end_line) for a text file.

    PDF text is split by ``\\n\\n`` page separators; other files are treated as
    one page. Line numbers are 1-indexed and match the numbered prompt sent to
    the segmentation LLM.
    """
    pages = text.split("\n\n")
    boundaries: list[tuple[int, int, int]] = []
    cumulative = 0
    for i, page in enumerate(pages, 1):
        lines = page.split("\n") if page else []
        start = cumulative + 1
        end = cumulative + len(lines)
        boundaries.append((i, start, end))
        cumulative = end + 1  # +1 for the blank separator line
    return boundaries


def _merge_refs(refs: list[dict]) -> list[dict]:
    """Merge overlapping/adjacent refs for the same file and sort by line_start."""
    by_file: dict[str, list[tuple[int, int, dict]]] = {}
    for ref in refs:
        fname = ref.get("file", "")
        ls = ref.get("line_start", 0)
        le = ref.get("line_end", 0)
        by_file.setdefault(fname, []).append((ls, le, ref))

    merged: list[dict] = []
    for fname, ranges in by_file.items():
        ranges.sort(key=lambda x: (x[0], x[1]))
        cur_start, cur_end, cur_ref = None, None, None
        for ls, le, ref in ranges:
            if cur_start is None:
                cur_start, cur_end, cur_ref = ls, le, ref
            elif ls <= cur_end + 1:
                # overlapping / adjacent: extend
                if le > cur_end:
                    cur_end = le
                    cur_ref = ref
            else:
                item = {"file": fname, "line_start": cur_start, "line_end": cur_end}
                if cur_ref.get("page"):
                    item["page"] = cur_ref["page"]
                merged.append(item)
                cur_start, cur_end, cur_ref = ls, le, ref
        if cur_start is not None:
            item = {"file": fname, "line_start": cur_start, "line_end": cur_end}
            if cur_ref.get("page"):
                item["page"] = cur_ref["page"]
            merged.append(item)
    return merged


def _enrich_page_header_refs(segments: list[dict], text_by_file: dict[str, str]) -> None:
    """Ensure each product segment inherits the table header of its page.

    The segmentation LLM may omit the shared column-header lines from
    ``common_refs``. This is a deterministic, content-agnostic fix: for every
    page that contains product rows, the leading block above the first product
    row is added to ``common_refs`` of every product on that page.

    No product names, prices, or line numbers are hardcoded; the header comes
    from the uploaded document itself.
    """
    # Index page boundaries per file
    file_boundaries: dict[str, list[tuple[int, int, int]]] = {
        fname: _page_boundaries(text)
        for fname, text in text_by_file.items()
    }

    def _find_page(line: int, boundaries: list[tuple[int, int, int]]) -> tuple[int, int, int] | None:
        for page_num, start, end in boundaries:
            if start <= line <= end:
                return (page_num, start, end)
        return None

    # Collect product row start lines per (file, page)
    page_info: dict[tuple[str, int], dict] = {}
    for seg in segments:
        for ref in seg.get("source_refs", []):
            fname = ref.get("file", "")
            ls = ref.get("line_start", 0)
            if not fname or ls < 1:
                continue
            boundaries = file_boundaries.get(fname, [])
            page = _find_page(ls, boundaries)
            if page is None:
                continue
            page_num, page_start, page_end = page
            key = (fname, page_num)
            info = page_info.setdefault(key, {"page_start": page_start, "page_end": page_end, "row_starts": set()})
            info["row_starts"].add(ls)

    # For each page, compute the header range and attach it to relevant segments
    for (fname, page_num), info in page_info.items():
        row_starts = sorted(info["row_starts"])
        if not row_starts:
            continue
        header_end = row_starts[0] - 1
        if header_end < info["page_start"]:
            continue
        header_ref = {
            "file": fname,
            "page": page_num,
            "line_start": info["page_start"],
            "line_end": header_end,
        }
        for seg in segments:
            # Only enrich segments that have a source_ref on this page
            on_page = False
            for ref in seg.get("source_refs", []):
                if ref.get("file") != fname:
                    continue
                page = _find_page(ref.get("line_start", 0), file_boundaries.get(fname, []))
                if page and page[0] == page_num:
                    on_page = True
                    break
            if not on_page:
                continue
            common = seg.setdefault("common_refs", [])
            # Add if not already covered
            covered = any(
                r.get("file") == fname
                and r.get("line_start", 0) <= header_ref["line_start"]
                and r.get("line_end", 0) >= header_ref["line_end"]
                for r in common
            )
            if not covered:
                common.append(header_ref)

    # Normalize common_refs to remove duplicates/overlap and keep them sorted
    for seg in segments:
        common = seg.get("common_refs")
        if common:
            seg["common_refs"] = _merge_refs(common)


# ------------------------------------------------------------------
#  LLM call
# ------------------------------------------------------------------

def _build_prompt(files: list[dict], config: dict) -> str:
    """สร้าง prompt สำหรับ LLM — แปะหมายเลขบรรทัดให้ LLM ระบุตำแหน่งได้แม่น."""
    parts: list[str] = []
    parts.append(
        "ต่อไปนี้คือเอกสารที่อัปโหลด อาจมีสินค้า 1 ตัวหรือหลายตัว (เช่น price list, catalog).\n"
        "อ่านแล้วระบุว่ามีสินค้ากี่ตัว แต่ละตัวชื่อ/รหัสอะไร และข้อมูลของแต่ละตัวอยู่บรรทัดใด.\n\n"
        "สำคัญ: อย่าเขียนสเปกหรือราคาเอง — ระบุแค่ **ตำแหน่ง** (line_start, line_end) "
        "ระบบจะคัด text จากต้นฉบับเอง เพื่อความถูกต้อง.\n\n"
        "ข้อมูลร่วม (เช่น MOQ, payment terms, delivery) ใส่ใน common_refs "
        "จะถูกแปะให้ทุกสินค้าอัตโนมัติ.\n"
    )
    for f in files:
        parts.append(f"\n--- FILE: {f['name']} ---")
        lines = f["text"].split("\n")
        for i, line in enumerate(lines, 1):
            parts.append(f"{i:5d}| {line}")
    return "\n".join(parts)


def _call_llm(llm, files: list[dict], config: dict) -> dict:
    """เรียก LLM พร้อม Structured Outputs — คืน parsed dict."""
    seg_cfg = config
    ing_cfg = _ingestion_config()

    prompt = _build_prompt(files, seg_cfg)
    messages = [
        {
            "role": "user",
            "content": prompt,
        }
    ]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "product_segments",
            "strict": True,
            "schema": _SEGMENT_SCHEMA,
        },
    }

    raw = llm.chat(
        messages,
        model=seg_cfg.get("model") or ing_cfg.get("model", "google/gemini-2.5-flash"),
        temperature=seg_cfg.get("temperature", 0.2),
        max_tokens=seg_cfg.get("max_output_tokens", 4096),
        stream=False,
        response_format=response_format,
        source="product_segmentation.segment_products",
    )

    # strip markdown code fences ก่อน parse (LLM บางตัวยังห่อแม้มี response_format)
    cleaned = _strip_code_fence(raw)
    return json.loads(cleaned)


# ------------------------------------------------------------------
#  Main interface
# ------------------------------------------------------------------

def segment_products(
    files: list[dict],
    llm,
    config: dict | None = None,
) -> dict[str, Any]:
    """แยกสินค้าจากไฟล์ที่อัปโหลด.

    Args:
        files: list[{name, path, text, type}] — text ดึงแล้ว
        llm: LLMClient หรือ None (None → single-product flow เดิม)
        config: override config (สำหรับ test)

    Returns:
        {
          "mode": "single"|"multi",
          "products": [{product_key, suggested_name, category, summary, source_refs, common_refs, text}],
          "error": str?,  # มีเฉพาะถ้า LLM ตอบผิด/validate ไม่ผ่าน
        }
    """
    seg_cfg = config or _seg_config()

    # ไม่มี LLM → single-product flow เดิม
    if llm is None:
        return _fallback_single(files)

    # เตรียม text lookup
    text_by_file = {f["name"]: f.get("text", "") for f in files}
    file_names = set(text_by_file.keys())

    # เรียก LLM
    try:
        parsed = _call_llm(llm, files, seg_cfg)
    except (json.JSONDecodeError, Exception) as e:
        # LLM ตอบผิด schema → เก็บต้นฉบับ ไม่สร้างสินค้าบางส่วน
        return _fallback_single(files, error=f"LLM segmentation failed: {e}")

    raw_segments = parsed.get("products", [])

    # กรณี LLM บอก 1 สินค้า → single mode (ใช้ text รวมเหมือนเดิม)
    if len(raw_segments) <= 1:
        all_text = "\n\n".join(f.get("text", "") for f in files if f.get("text"))
        seg = raw_segments[0] if raw_segments else {}
        return {
            "mode": "single",
            "products": [{
                "product_key": seg.get("product_key", ""),
                "suggested_name": seg.get("suggested_name", ""),
                "category": seg.get("category", ""),
                "summary": seg.get("summary", ""),
                "source_refs": seg.get("source_refs", []),
                "common_refs": seg.get("common_refs", []),
                "text": all_text,
            }],
        }

    # กรณีหลายสินค้า → validate ก่อน materialize
    merged = _merge_segments(raw_segments)

    # แก้ไข generic: ถ้า LLM ลืมใส่ header ของตารางใน common_refs ให้เติมโดยอัตโนมัติ
    _enrich_page_header_refs(merged, text_by_file)

    all_errors: list[str] = []
    for seg in merged:
        errs = _validate_segment(seg, file_names, text_by_file)
        if errs:
            all_errors.append(f"[{seg.get('product_key','?')}] " + "; ".join(errs))

    if all_errors:
        # validation ไม่ผ่าน → ไม่สร้างสินค้าบางส่วน เก็บต้นฉบับ
        return _fallback_single(files, error="Validation failed: " + " | ".join(all_errors))

    # materialize — คัด text จากต้นฉบับตาม refs
    products: list[dict] = []
    for seg in merged:
        own_text = _slice_refs(text_by_file, seg.get("source_refs", []))
        common_text = _slice_refs(text_by_file, seg.get("common_refs", []))
        # common ขึ้นก่อน + own ตามหลัง (common = MOQ/payment, own = spec/price)
        full_text = "\n".join(t for t in [common_text, own_text] if t)
        products.append({
            "product_key": seg["product_key"],
            "suggested_name": seg["suggested_name"],
            "category": seg.get("category", ""),
            "summary": seg.get("summary", ""),
            "source_refs": seg.get("source_refs", []),
            "common_refs": seg.get("common_refs", []),
            "text": full_text,
        })

    return {"mode": "multi", "products": products}


def _fallback_single(files: list[dict], error: str | None = None) -> dict[str, Any]:
    """สร้าง single-product result จาก text รวม — ใช้ตอน LLM ไม่มี/ล้มเหลว/validate ไม่ผ่าน.

    ไม่สร้างสินค้าบางส่วน เก็บต้นฉบ้างไว้ให้รันใหม่ได้.
    """
    all_text = "\n\n".join(f.get("text", "") for f in files if f.get("text"))
    result: dict[str, Any] = {
        "mode": "single",
        "products": [{
            "product_key": "",
            "suggested_name": "",
            "category": "",
            "summary": "",
            "source_refs": [],
            "common_refs": [],
            "text": all_text,
        }],
    }
    if error:
        result["error"] = error
    return result
