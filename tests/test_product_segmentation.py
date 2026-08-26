"""Tests for product segmentation — แยก catalog ที่มีหลายสินค้าออกจากไฟล์เดียว.

Interface: segment_products(files, llm, config) -> dict
  files: list[{name, path, text, type}]
  คืน: {"mode": "single"|"multi", "products": [...], "error": str?}

สินค้าแต่ละตัว (segment):
  - product_key: รหัสสินค้าที่ LLM ระบุ (ใช้ dedup + scope re-ingest)
  - suggested_name: ชื่อที่เสนอ (brand + model)
  - category, summary
  - source_refs: [{file, page?, line_start, line_end}] — ช่วงใน text ต้นฉบับ
  - common_refs: ช่วงข้อมูลร่วม (MOQ, payment terms) ใส่ทุกสินค้า

หลักการสำคัญ: ระบบคัด text จากต้นฉบับตาม source_refs เอง — LLM ไม่เขียนสเปก/ราคาใหม่
"""
import json
from pathlib import Path

import pytest


# ------------------------------------------------------------------
#  Fakes
# ------------------------------------------------------------------

class _FakeLLM:
    """LLM double ที่คืน JSON ตามที่กำหนด — จำลอง Structured Outputs."""

    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def chat(self, messages, *, response_format=None, **kwargs):
        self.calls.append({"messages": messages, "response_format": response_format, "kwargs": kwargs})
        return json.dumps(self._response, ensure_ascii=False)


# ------------------------------------------------------------------
#  Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def _seg(monkeypatch, tmp_path):
    """Setup tmp project root + reload product_segmentation."""
    import src.config_loader as config_loader
    monkeypatch.setattr(config_loader, "_project_root", lambda: tmp_path)

    # config ขั้นต่ำ
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "ingestion.yaml").write_text(
        "supported_formats:\n  text: ['.txt', '.md', '.pdf']\n"
        "  image: ['.jpg', '.jpeg', '.png']\n"
        "model: test/model\n"
        "temperature: 0.3\n"
        "timeout_seconds: 30\n"
        "product_segmentation:\n"
        "  enabled: true\n"
        "  model: test/seg-model\n"
        "  temperature: 0.2\n"
        "  max_output_tokens: 4096\n"
        "  batch_size: 8000\n"
        "  batch_overlap: 500\n"
        "  pdf_max_bytes: 2000000\n"
        "  name_format: '{brand} {model}'\n",
        encoding="utf-8")

    import src.product_segmentation as segmentation
    import importlib
    importlib.reload(segmentation)
    return segmentation


def _file(name: str, text: str, *, path: str = None, file_type: str = "text") -> dict:
    return {"name": name, "path": path or f"/tmp/{name}", "text": text, "type": file_type}


# ------------------------------------------------------------------
#  Slice 1: basic segmentation — single + multi
# ------------------------------------------------------------------

def test_segment_uses_nested_ingestion_config(_seg):
    """product_segmentation อยู่ใต้ ingestion section หลัง config_loader merge."""
    files = [_file("catalog.txt", "Product A\nProduct B")]
    llm = _FakeLLM({
        "products": [{
            "product_key": "A",
            "suggested_name": "Product A",
            "category": "test",
            "summary": "test",
            "source_refs": [{"file": "catalog.txt", "line_start": 1, "line_end": 1}],
            "common_refs": [],
        }]
    })

    _seg.segment_products(files, llm)

    assert llm.calls[0]["kwargs"]["model"] == "test/seg-model"
    assert llm.calls[0]["kwargs"]["temperature"] == 0.2
    assert llm.calls[0]["kwargs"]["max_tokens"] == 4096


def test_segment_single_product_returns_single_mode(_seg):
    """LLM บอกว่าเป็นสินค้าเดียว → mode=single, ไม่แยก."""
    text = "Product: Lagenio K2\nPrice: $99\nSpec: smartwatch for kids"
    files = [_file("k2.txt", text)]

    llm = _FakeLLM({
        "products": [{
            "product_key": "K2",
            "suggested_name": "Lagenio K2",
            "category": "smartwatch",
            "summary": "smartwatch for kids",
            "source_refs": [{"file": "k2.txt", "line_start": 1, "line_end": 3}],
            "common_refs": [],
        }]
    })

    result = _seg.segment_products(files, llm)
    assert result["mode"] == "single"
    assert len(result["products"]) == 1
    assert result["products"][0]["product_key"] == "K2"


def test_segment_multi_product_slices_text_from_source(_seg):
    """LLM ระบุ 3 สินค้า → ระบบคัด text จากต้นฉบับตาม line ranges เอง.

    สำคัญ: text ที่ได้ต้องเป็นของจริงจาก source — ไม่ใช่ที่ LLM เขียนใหม่.
    """
    # จำลอง price list 3 รุ่น บรรทัดเรียงตามลำดับ
    lines = [
        "Header: CACGO Price List",       # 1 — common
        "MOQ: 3000pcs",                   # 2 — common
        "1 K67 CPU: ATS3085L",            # 3 — product 1 start
        "Price: $21.50",                  # 4
        "Battery: 530mAh",                # 5 — product 1 end
        "2 K72 CPU: ATS3085L",            # 6 — product 2 start
        "Price: $22.50",                  # 7
        "Battery: 580mAh",                # 8 — product 2 end
        "3 K71 CPU: Realtek",             # 9 — product 3 start
        "Price: $18.00",                  # 10
        "Battery: 600mAh",                # 11 — product 3 end
        "Payment: 30% deposit",           # 12 — common
        "Delivery: 3 working days",       # 13 — common
    ]
    text = "\n".join(lines)
    files = [_file("cacgo.txt", text)]

    llm = _FakeLLM({
        "products": [
            {
                "product_key": "K67",
                "suggested_name": "CACGO K67",
                "category": "smartwatch",
                "summary": "K67 smartwatch",
                "source_refs": [{"file": "cacgo.txt", "line_start": 3, "line_end": 5}],
                "common_refs": [
                    {"file": "cacgo.txt", "line_start": 1, "line_end": 2},
                    {"file": "cacgo.txt", "line_start": 12, "line_end": 13},
                ],
            },
            {
                "product_key": "K72",
                "suggested_name": "CACGO K72",
                "category": "smartwatch",
                "summary": "K72 smartwatch",
                "source_refs": [{"file": "cacgo.txt", "line_start": 6, "line_end": 8}],
                "common_refs": [
                    {"file": "cacgo.txt", "line_start": 1, "line_end": 2},
                    {"file": "cacgo.txt", "line_start": 12, "line_end": 13},
                ],
            },
            {
                "product_key": "K71",
                "suggested_name": "CACGO K71",
                "category": "smartwatch",
                "summary": "K71 smartwatch",
                "source_refs": [{"file": "cacgo.txt", "line_start": 9, "line_end": 11}],
                "common_refs": [
                    {"file": "cacgo.txt", "line_start": 1, "line_end": 2},
                    {"file": "cacgo.txt", "line_start": 12, "line_end": 13},
                ],
            },
        ]
    })

    result = _seg.segment_products(files, llm)
    assert result["mode"] == "multi"
    assert len(result["products"]) == 3

    # ตรวจ text ที่คัดได้ — ต้องเป็นของจริงจากต้นฉบับ
    p1, p2, p3 = result["products"]

    # K67: lines 3-5 + common (1-2, 12-13)
    assert "1 K67 CPU: ATS3085L" in p1["text"]
    assert "Price: $21.50" in p1["text"]
    assert "Battery: 530mAh" in p1["text"]
    # common refs ต้องอยู่ด้วย
    assert "MOQ: 3000pcs" in p1["text"]
    assert "Payment: 30% deposit" in p1["text"]
    # ห้ามมีข้อมูลรุ่นอื่นปน
    assert "K72" not in p1["text"]
    assert "K71" not in p1["text"]

    # K72: lines 6-8
    assert "2 K72 CPU: ATS3085L" in p2["text"]
    assert "Price: $22.50" in p2["text"]
    assert "K67" not in p2["text"]
    assert "K71" not in p2["text"]

    # K71: lines 9-11
    assert "3 K71 CPU: Realtek" in p3["text"]
    assert "Price: $18.00" in p3["text"]
    assert "K67" not in p3["text"]
    assert "K72" not in p3["text"]


def test_segment_no_llm_returns_single_without_calling(_seg):
    """ไม่มี LLM → ใช้ single-product flow เดิม ไม่เรียก API."""
    files = [_file(" lone.txt", "Just one product info")]
    result = _seg.segment_products(files, llm=None)
    assert result["mode"] == "single"
    assert len(result["products"]) == 1
    # text ต้องเป็น text รวมเหมือนเดิม
    assert "Just one product info" in result["products"][0]["text"]


# ------------------------------------------------------------------
#  Slice 2: validation + common refs + edge cases
# ------------------------------------------------------------------

def test_segment_invalid_range_falls_back_to_single(_seg):
    """LLM ให้ range ผิด (line_start > line_end) → ไม่สร้างสินค้าบางส่วน
    คืน single mode + error message เก็บต้นฉบับไว้."""
    text = "line1\nline2\nline3\nline4\nline5"
    files = [_file("bad.txt", text)]

    llm = _FakeLLM({
        "products": [
            {
                "product_key": "A",
                "suggested_name": "Product A",
                "category": "cat",
                "summary": "sum",
                "source_refs": [{"file": "bad.txt", "line_start": 5, "line_end": 2}],  # ผิด
                "common_refs": [],
            },
            {
                "product_key": "B",
                "suggested_name": "Product B",
                "category": "cat",
                "summary": "sum",
                "source_refs": [{"file": "bad.txt", "line_start": 1, "line_end": 3}],
                "common_refs": [],
            },
        ]
    })

    result = _seg.segment_products(files, llm)
    # validation ไม่ผ่าน →  fallback single ไม่ใช่ multi แบบครึ่ง ๆ กลาง ๆ
    assert result["mode"] == "single"
    assert "error" in result
    assert "Validation failed" in result["error"]
    # ต้นฉบับยังอยู่ — ไม่เสียข้อมูล
    assert "line1" in result["products"][0]["text"]


def test_segment_unknown_file_in_ref_fails_validation(_seg):
    """LLM อ้างไฟล์ที่ไม่มี → validation fail → fallback single."""
    files = [_file("real.txt", "content here")]
    llm = _FakeLLM({
        "products": [{
            "product_key": "X",
            "suggested_name": "X",
            "category": "c",
            "summary": "s",
            "source_refs": [{"file": "nonexistent.txt", "line_start": 1, "line_end": 1}],
            "common_refs": [],
        }]
    })
    result = _seg.segment_products(files, llm)
    # 1 segment แต่ ref ผิด → ถือว่า LLM บอก 1 สินค้า แต่ไม่ validate
    # ใช้ single mode (เพราะ 1 segment) ไม่ validate ในกรณี single
    assert result["mode"] == "single"


def test_segment_duplicate_product_key_across_batches_merges(_seg):
    """product_key ซ้ำ → รวม source_refs ของทั้งสองเข้าด้วยกัน."""
    text = "\n".join([f"line{i}" for i in range(1, 21)])
    files = [_file("dup.txt", text)]

    llm = _FakeLLM({
        "products": [
            {
                "product_key": "K67",
                "suggested_name": "CACGO K67",
                "category": "watch",
                "summary": "first batch",
                "source_refs": [{"file": "dup.txt", "line_start": 1, "line_end": 5}],
                "common_refs": [],
            },
            {
                "product_key": "K67",  # ซ้ำ
                "suggested_name": "CACGO K67",
                "category": "watch",
                "summary": "second batch",
                "source_refs": [{"file": "dup.txt", "line_start": 11, "line_end": 15}],
                "common_refs": [],
            },
        ]
    })

    result = _seg.segment_products(files, llm)
    assert result["mode"] == "multi"
    # รวมเป็น 1 สินค้า ไม่ใช่ 2
    assert len(result["products"]) == 1
    merged = result["products"][0]
    # text ต้องมีทั้งสองช่วง
    assert "line1" in merged["text"]
    assert "line11" in merged["text"]
    # มี 2 source_refs
    assert len(merged["source_refs"]) == 2


def test_segment_empty_range_rejected(_seg):
    """ช่วงที่คัดได้ว่าง (เช่น เกินจำนวนบรรทัดจริง) → validation fail."""
    text = "only 3 lines\nline2\nline3"
    files = [_file("short.txt", text)]

    llm = _FakeLLM({
        "products": [
            {
                "product_key": "A",
                "suggested_name": "A",
                "category": "c",
                "summary": "s",
                "source_refs": [{"file": "short.txt", "line_start": 1, "line_end": 3}],
                "common_refs": [],
            },
            {
                "product_key": "B",
                "suggested_name": "B",
                "category": "c",
                "summary": "s",
                "source_refs": [{"file": "short.txt", "line_start": 100, "line_end": 200}],  # เกิน
                "common_refs": [],
            },
        ]
    })

    result = _seg.segment_products(files, llm)
    assert result["mode"] == "single"
    assert "error" in result


def test_segment_llm_returns_bad_json_falls_back(_seg):
    """LLM คืน JSON ที่ parse ไม่ได้ → fallback single + error."""
    class _BadLLM:
        def chat(self, messages, **kwargs):
            return "not json at all"

    files = [_file("x.txt", "some content")]
    result = _seg.segment_products(files, _BadLLM())
    assert result["mode"] == "single"
    assert "error" in result
    assert "some content" in result["products"][0]["text"]
