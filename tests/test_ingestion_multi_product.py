"""Integration tests: ingest_product แยก catalog หลายสินค้าจากไฟล์เดียว.

ทดสอบผ่าน public interface `ingest_product()` + `product_db` readers
ไม่ mock internals ของ ingestion — ใช้ FakeLLM ที่จำลอง Structured Outputs เท่านั้น.

สถานการณ์:
  - อัปโหลด price list 3 รุ่น → ต้องได้ 3 สินค้าปกติ แยกโฟลเดอร์
  - แต่ละสินค้ามี raw_text เฉพาะรุ่น ไม่ปนกัน
  - source file อยู่ในทุกโฟลเดอร์สินค้า (hard link/copy)
  - โฟลเดอร์ต้นฉบับถูกลบหลังแยกเสร็จ
  - ถ้า error ระหว่างสร้าง → rollback ไม่มี partial products
"""
import importlib
import json
from pathlib import Path

import pytest


class _FakeLLM:
    """LLM double — คืน segmentation JSON เมื่อถูกเรียกด้วย response_format."""

    def __init__(self, seg_response: dict, summary_response: str = "summary"):
        self._seg = seg_response
        self._summary = summary_response
        self.calls: list[dict] = []

    def chat(self, messages, *, response_format=None, source="", **kwargs):
        self.calls.append({"source": source, "response_format": response_format})
        # ถ้าเป็น segmentation call (มี response_format และ source บอก) → คืน seg JSON
        if "segment" in (source or ""):
            return json.dumps(self._seg, ensure_ascii=False)
        # ถ้าเป็น summary/profile call → คืน text สั้น
        return self._summary

    def close(self):
        pass


@pytest.fixture
def _ingest(monkeypatch, tmp_path):
    """Setup tmp project root + reload ingestion/product_db/product_segmentation."""
    import src.config_loader as config_loader
    import src.product_db as product_db

    monkeypatch.setattr(config_loader, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    real_cfg = Path(__file__).resolve().parent.parent / "config" / "ingestion.yaml"
    if real_cfg.exists():
        (cfg_dir / "ingestion.yaml").write_bytes(real_cfg.read_bytes())
    else:
        (cfg_dir / "ingestion.yaml").write_text(
            "supported_formats:\n  text: ['.txt', '.md', '.pdf']\n"
            "  image: ['.jpg', '.jpeg', '.png']\n"
            "model: test/model\ntemperature: 0.3\ntimeout_seconds: 30\n",
            encoding="utf-8")

    import src.ingestion as ingestion
    importlib.reload(ingestion)
    monkeypatch.setattr(ingestion, "_project_root", lambda: tmp_path)
    return ingestion


def _make_catalog_text() -> str:
    """สร้าง text จำลอง price list 3 รุ่น."""
    lines = [
        "CACGO Price List",              # 1 — common
        "MOQ: 3000pcs",                  # 2 — common
        "1 K67 CPU: ATS3085L",           # 3 — product 1
        "Price: $21.50",                 # 4
        "Battery: 530mAh",               # 5
        "2 K72 CPU: ATS3085L",           # 6 — product 2
        "Price: $22.50",                 # 7
        "Battery: 580mAh",               # 8
        "3 K71 CPU: Realtek",            # 9 — product 3
        "Price: $18.00",                 # 10
        "Battery: 600mAh",               # 11
        "Payment: 30% deposit",          # 12 — common
        "Delivery: 3 working days",      # 13 — common
    ]
    return "\n".join(lines)


def _make_seg_response() -> dict:
    """LLM response สำหรับ 3 รุ่น."""
    return {
        "products": [
            {
                "product_key": "K67",
                "suggested_name": "CACGO K67",
                "category": "smartwatch",
                "summary": "K67 smartwatch",
                "source_refs": [{"file": "catalog.txt", "line_start": 3, "line_end": 5}],
                "common_refs": [
                    {"file": "catalog.txt", "line_start": 1, "line_end": 2},
                    {"file": "catalog.txt", "line_start": 12, "line_end": 13},
                ],
            },
            {
                "product_key": "K72",
                "suggested_name": "CACGO K72",
                "category": "smartwatch",
                "summary": "K72 smartwatch",
                "source_refs": [{"file": "catalog.txt", "line_start": 6, "line_end": 8}],
                "common_refs": [
                    {"file": "catalog.txt", "line_start": 1, "line_end": 2},
                    {"file": "catalog.txt", "line_start": 12, "line_end": 13},
                ],
            },
            {
                "product_key": "K71",
                "suggested_name": "CACGO K71",
                "category": "smartwatch",
                "summary": "K71 smartwatch",
                "source_refs": [{"file": "catalog.txt", "line_start": 9, "line_end": 11}],
                "common_refs": [
                    {"file": "catalog.txt", "line_start": 1, "line_end": 2},
                    {"file": "catalog.txt", "line_start": 12, "line_end": 13},
                ],
            },
        ]
    }


# ------------------------------------------------------------------
#  Test: multi-product materialization
# ------------------------------------------------------------------

def test_ingest_multi_product_creates_separate_products(_ingest, tmp_path, monkeypatch):
    """อัปโหลด catalog 3 รุ่น → ได้ 3 สินค้าปกติ แยกโฟลเดอร์."""
    ingestion = _ingest
    import src.product_db as product_db

    # สร้างโฟลเดอร์ต้นฉบับ (ชื่อจากไฟล์ — เหมือน _makeProductNameFromFile)
    temp_name = "catalog"
    data_dir = tmp_path / "data" / temp_name
    data_dir.mkdir(parents=True)
    (data_dir / "catalog.txt").write_text(_make_catalog_text(), encoding="utf-8")

    # patch _make_llm ให้คืน FakeLLM
    llm = _FakeLLM(_make_seg_response())
    monkeypatch.setattr(ingestion, "_make_llm", lambda: llm)

    # ingest แบบ one-to-many (is_new_upload=True)
    result = ingestion.ingest_product(temp_name, force=False, is_new_upload=True)

    # ต้องได้ 3 สินค้า
    all_products = product_db.get_all_products()
    product_names = [p["product_id"] for p in all_products]
    assert "CACGO K67" in product_names, f"K67 ไม่อยู่ใน {product_names}"
    assert "CACGO K72" in product_names, f"K72 ไม่อยู่ใน {product_names}"
    assert "CACGO K71" in product_names, f"K71 ไม่อยู่ใน {product_names}"

    # โฟลเดอร์ต้นฉบับถูกลบ
    assert not (tmp_path / "data" / temp_name).exists(), "โฟลเดอร์ต้นฉบับต้องถูกลบหลังแยกเสร็จ"

    # แต่ละสินค้ามี raw_text เฉพาะรุ่น
    k67 = product_db.load("CACGO K67")
    assert "K67" in k67["raw_text"]
    assert "$21.50" in k67["raw_text"]
    assert "K72" not in k67["raw_text"]
    assert "K71" not in k67["raw_text"]
    # common refs ต้องอยู่
    assert "MOQ" in k67["raw_text"]
    assert "Payment" in k67["raw_text"]

    k72 = product_db.load("CACGO K72")
    assert "K72" in k72["raw_text"]
    assert "$22.50" in k72["raw_text"]
    assert "K67" not in k72["raw_text"]

    k71 = product_db.load("CACGO K71")
    assert "K71" in k71["raw_text"]
    assert "$18.00" in k71["raw_text"]
    assert "K67" not in k71["raw_text"]

    # ทุกสินค้ามี status=ready
    assert k67["status"] == product_db.STATUS_READY
    assert k72["status"] == product_db.STATUS_READY
    assert k71["status"] == product_db.STATUS_READY


def test_ingest_multi_product_source_file_in_each_folder(_ingest, tmp_path, monkeypatch):
    """source file ต้องอยู่ในโฟลเดอร์สินค้าทุกตัว (hard link หรือ copy)."""
    ingestion = _ingest

    temp_name = "catalog"
    data_dir = tmp_path / "data" / temp_name
    data_dir.mkdir(parents=True)
    (data_dir / "catalog.txt").write_text(_make_catalog_text(), encoding="utf-8")

    llm = _FakeLLM(_make_seg_response())
    monkeypatch.setattr(ingestion, "_make_llm", lambda: llm)

    ingestion.ingest_product(temp_name, force=False, is_new_upload=True)

    # ตรวจว่า catalog.txt อยู่ในทุกโฟลเดอร์สินค้า
    for name in ["CACGO K67", "CACGO K72", "CACGO K71"]:
        src_file = tmp_path / "data" / name / "catalog.txt"
        assert src_file.exists(), f"{name}/catalog.txt ไม่มี"
        # ต้องอ่านได้เหมือนต้นฉบับ
        assert "CACGO Price List" in src_file.read_text(encoding="utf-8")


def test_ingest_single_product_unchanged_behavior(_ingest, tmp_path, monkeypatch):
    """อัปโหลดสินค้าเดียว → behavior เดิม ไม่แยก."""
    ingestion = _ingest
    import src.product_db as product_db

    product_id = "Lagenio K2"
    data_dir = tmp_path / "data" / product_id
    data_dir.mkdir(parents=True)
    (data_dir / "k2.txt").write_text("Lagenio K2 smartwatch for kids. Price $99.", encoding="utf-8")

    # FakeLLM บอก 1 สินค้า
    llm = _FakeLLM({
        "products": [{
            "product_key": "K2",
            "suggested_name": "Lagenio K2",
            "category": "smartwatch",
            "summary": "kids smartwatch",
            "source_refs": [{"file": "k2.txt", "line_start": 1, "line_end": 1}],
            "common_refs": [],
        }]
    })
    monkeypatch.setattr(ingestion, "_make_llm", lambda: llm)

    result = ingestion.ingest_product(product_id, force=False, is_new_upload=True)

    # ต้องมีแค่ 1 สินค้า ชื่อเดิม
    all_products = product_db.get_all_products()
    assert len(all_products) == 1
    assert all_products[0]["product_id"] == product_id
    assert all_products[0]["status"] == product_db.STATUS_READY


def test_ingest_multi_product_no_llm_uses_single_flow(_ingest, tmp_path, monkeypatch):
    """ไม่มี LLM → ใช้ single-product flow เดิม ไม่พยายามแยก."""
    ingestion = _ingest
    import src.product_db as product_db

    product_id = "NoLLM Product"
    data_dir = tmp_path / "data" / product_id
    data_dir.mkdir(parents=True)
    (data_dir / "info.txt").write_text("Some product info without LLM", encoding="utf-8")

    monkeypatch.setattr(ingestion, "_make_llm", lambda: None)

    result = ingestion.ingest_product(product_id, force=False, is_new_upload=True)

    all_products = product_db.get_all_products()
    assert len(all_products) == 1
    assert all_products[0]["product_id"] == product_id


# ------------------------------------------------------------------
#  Slice 4: scoped re-ingest + name collision + rollback
# ------------------------------------------------------------------

def test_reingest_scoped_product_keeps_scope(_ingest, tmp_path, monkeypatch):
    """re-ingest สินค้าที่เคยแยกแล้ว → ยังได้เฉพาะ scope เดิม ไม่กลับไปรวม catalog.

    สถานการณ์: ผู้ใช้แยก CACGO K67 จาก catalog แล้ว วันต่อมากด re-ingest
    ระบบต้องไม่เรียก segmentation ซ้ำ (เพราะ is_new_upload=False) และ
    ต้อง rebuild raw_text จาก text_extracts ของ K67 เท่านั้น.
    """
    ingestion = _ingest
    import src.product_db as product_db

    # สร้างสินค้า K67 เหมือนที่เคยแยกแล้ว — มี scope + source file
    product_id = "CACGO K67"
    data_dir = tmp_path / "data" / product_id
    data_dir.mkdir(parents=True)
    (data_dir / "catalog.txt").write_text(_make_catalog_text(), encoding="utf-8")

    # สร้าง record เหมือนหลัง split
    rec = product_db.load(product_id)
    rec["product_id"] = product_id
    rec["status"] = product_db.STATUS_READY
    rec["text_extracts"] = [{"file": "catalog.txt", "text": "1 K67 CPU: ATS3085L\nPrice: $21.50\nBattery: 530mAh"}]
    rec["raw_text"] = "1 K67 CPU: ATS3085L\nPrice: $21.50\nBattery: 530mAh"
    rec["scope"] = {
        "product_key": "K67",
        "source_refs": [{"file": "catalog.txt", "line_start": 3, "line_end": 5}],
        "common_refs": [
            {"file": "catalog.txt", "line_start": 1, "line_end": 2},
            {"file": "catalog.txt", "line_start": 12, "line_end": 13},
        ],
        "split_from": "catalog",
    }
    rec["files"] = [{
        "name": "catalog.txt",
        "path": str(data_dir / "catalog.txt"),
        "type": "text",
        "status": "ingested",
        "hash": product_db.compute_file_hash(data_dir / "catalog.txt"),
        "size": 200,
    }]
    product_db.save(product_id, rec)

    # re-ingest แบบ force=True (is_new_upload=False — ไม่ใช่ upload ใหม่)
    llm = _FakeLLM(_make_seg_response())  # ถ้าระบบเรียก seg ผิด → test จะตรวจได้
    monkeypatch.setattr(ingestion, "_make_llm", lambda: llm)

    ingestion.ingest_product(product_id, force=True, is_new_upload=False)

    # ต้องมีแค่ K67 ไม่เพิ่ม K72/K71
    all_products = product_db.get_all_products()
    names = [p["product_id"] for p in all_products]
    assert "CACGO K67" in names
    assert "CACGO K72" not in names, "re-ingest ต้องไม่แตก catalog ใหม่"
    assert "CACGO K71" not in names

    # raw_text ยังเป็นของ K67 เท่านั้น
    k67 = product_db.load("CACGO K67")
    assert "K67" in k67["raw_text"]
    assert "$21.50" in k67["raw_text"]
    # ห้ามมี K72/K71 ปน (ถ้า re-ingest ไม่ scope จะเจอทั้ง catalog)
    assert "K72" not in k67["raw_text"]
    assert "K71" not in k67["raw_text"]


def test_split_name_collision_appends_suffix(_ingest, tmp_path, monkeypatch):
    """ชื่อสินค้าที่แยกชนกับสินค้าที่มีอยู่ → เติม suffix ไม่เขียนทับ record เดิม."""
    ingestion = _ingest
    import src.product_db as product_db

    # สร้างสินค้า CACGO K67 ที่มีอยู่แล้ว
    existing_dir = tmp_path / "data" / "CACGO K67"
    existing_dir.mkdir(parents=True)
    (existing_dir / "old.txt").write_text("old K67 product", encoding="utf-8")
    rec = product_db.load("CACGO K67")
    rec["product_id"] = "CACGO K67"
    rec["status"] = product_db.STATUS_READY
    rec["raw_text"] = "old K67 product"
    product_db.save("CACGO K67", rec)

    # อัปโหลด catalog ใหม่ที่มี K67 ด้วย
    temp_name = "new_catalog"
    data_dir = tmp_path / "data" / temp_name
    data_dir.mkdir(parents=True)
    (data_dir / "catalog.txt").write_text(_make_catalog_text(), encoding="utf-8")

    llm = _FakeLLM(_make_seg_response())
    monkeypatch.setattr(ingestion, "_make_llm", lambda: llm)

    ingestion.ingest_product(temp_name, force=False, is_new_upload=True)

    all_products = product_db.get_all_products()
    names = [p["product_id"] for p in all_products]

    # สินค้าเดิมยังอยู่ ไม่ถูกเขียนทับ
    assert "CACGO K67" in names
    old_k67 = product_db.load("CACGO K67")
    assert "old K67 product" in old_k67["raw_text"], "สินค้าเดิมต้องไม่ถูกเขียนทับ"

    # สินค้าใหม่ต้องมี suffix (เช่น CACGO K67 (1))
    assert any("CACGO K67" in n and n != "CACGO K67" for n in names), \
        f"ต้องมี CACGO K67 พร้อม suffix — got {names}"


def test_split_failure_rolls_back_no_partial_products(_ingest, tmp_path, monkeypatch):
    """error ระหว่างสร้างสินค้า → rollback ไม่มี partial products และต้นฉบับยังอยู่."""
    ingestion = _ingest
    import src.product_db as product_db

    temp_name = "catalog"
    data_dir = tmp_path / "data" / temp_name
    data_dir.mkdir(parents=True)
    (data_dir / "catalog.txt").write_text(_make_catalog_text(), encoding="utf-8")

    llm = _FakeLLM(_make_seg_response())
    monkeypatch.setattr(ingestion, "_make_llm", lambda: llm)

    # ทำให้ product_db.save fail ตอนบันทึกสินค้าตัวที่ 2 (K72)
    # (จำลอง disk error หรือ corruption ระหว่าง materialize)
    original_save = product_db.save

    def _failing_save(pid, record):
        if pid == "CACGO K72":
            raise RuntimeError("simulated DB error on K72")
        return original_save(pid, record)

    monkeypatch.setattr(product_db, "save", _failing_save)
    monkeypatch.setattr(ingestion.product_db, "save", _failing_save)

    # ingest ต้อง raise (ไม่กลืน error เงียบ)
    with pytest.raises(Exception, match="Split products failed"):
        ingestion.ingest_product(temp_name, force=False, is_new_upload=True)

    # ตรวจ rollback — ไม่มี partial products
    all_products = product_db.get_all_products()
    names = [p["product_id"] for p in all_products]
    assert "CACGO K67" not in names, "rollback ต้องลบ K67 ที่สร้างครึ่งทาง"
    assert "CACGO K72" not in names
    assert "CACGO K71" not in names

    # ต้นฉบับยังอยู่ — ผู้ใช้รันใหม่ได้
    assert (tmp_path / "data" / temp_name).exists(), "โฟลเดอร์ต้นฉบับต้องยังอยู่หลัง rollback"
