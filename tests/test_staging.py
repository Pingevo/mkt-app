"""Tests for staging — พื้นที่พักไฟล์อัปโหลดก่อน materialize (block A).

Staging เก็บไฟล์ต้นฉบับที่ user อัปโหลด + batch.json (segments + matches)
user เห็น preview ก่อนกดยืนยัน — ถึงจะ materialize เป็นสินค้าจริง

Lifecycle: หายถ้าปิดหน้าต่าง (discard_batch) หรือ commit แล้ว
"""
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def _stage(monkeypatch, tmp_path):
    """Setup tmp project root + reload staging + product_db."""
    import src.product_db as product_db

    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    importlib.reload(product_db)
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)

    import src.staging as staging
    importlib.reload(staging)
    monkeypatch.setattr(staging, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(staging, "product_db", product_db)
    # mock _generate_product_profile เพื่อไม่ให้ test เรียก LLM จริง
    monkeypatch.setattr(staging, "_generate_product_profile", lambda *a, **kw: None)
    return staging


# ------------------------------------------------------------------
#  Slice 1: create_batch + discard_batch
# ------------------------------------------------------------------

def test_create_batch_saves_files_to_staging(_stage, tmp_path):
    """create_batch เซฟไฟล์ไป data/.staging/{batch_id}/source/ และคืน batch_id."""
    staging = _stage

    batch_id = staging.create_batch([
        ("k2.txt", b"Lagenio K2 smartwatch"),
        ("spec.pdf", b"%PDF-1.4 fake"),
    ])

    assert batch_id, "ต้องคืน batch_id ที่ไม่ว่าง"
    staging_dir = tmp_path / "data" / ".staging" / batch_id
    assert staging_dir.exists(), "staging dir ต้องถูกสร้าง"
    assert (staging_dir / "source" / "k2.txt").read_bytes() == b"Lagenio K2 smartwatch"
    assert (staging_dir / "source" / "spec.pdf").read_bytes() == b"%PDF-1.4 fake"


def test_create_batch_skips_dotfiles(_stage, tmp_path):
    """dotfile (.DS_Store, .hidden) ต้องถูกข้าม."""
    staging = _stage

    batch_id = staging.create_batch([
        (".DS_Store", b"junk"),
        (".hidden", b"junk"),
        ("real.txt", b"data"),
    ])

    staging_dir = tmp_path / "data" / ".staging" / batch_id / "source"
    assert sorted(p.name for p in staging_dir.iterdir()) == ["real.txt"]


def test_create_batch_returns_unique_id_each_call(_stage):
    """เรียก create_batch หลายครั้ง → batch_id ต้องไม่ซ้ำกัน."""
    staging = _stage

    id1 = staging.create_batch([("a.txt", b"x")])
    id2 = staging.create_batch([("a.txt", b"x")])

    assert id1 != id2


def test_discard_batch_removes_staging_dir(_stage, tmp_path):
    """discard_batch ลบโฟลเดอร์ staging ทิ้งทั้งหมด."""
    staging = _stage

    batch_id = staging.create_batch([("k2.txt", b"data")])
    staging_dir = tmp_path / "data" / ".staging" / batch_id
    assert staging_dir.exists()

    staging.discard_batch(batch_id)

    assert not staging_dir.exists()


def test_discard_batch_nonexistent_is_noop(_stage):
    """discard_batch ที่ batch_id ไม่มี → ไม่ error (idempotent)."""
    staging = _stage
    staging.discard_batch("nonexistent-batch-id")  # ไม่ raise


def test_create_batch_creates_batch_json(_stage, tmp_path):
    """create_batch ต้องสร้าง batch.json เก็บ metadata ของ batch."""
    staging = _stage

    batch_id = staging.create_batch([("k2.txt", b"data")])
    batch_json = tmp_path / "data" / ".staging" / batch_id / "batch.json"

    assert batch_json.exists()
    import json
    data = json.loads(batch_json.read_text(encoding="utf-8"))
    assert data["batch_id"] == batch_id
    assert "created_at" in data
    assert data["status"] == "staged"
    assert [f["name"] for f in data["files"]] == ["k2.txt"]


# ------------------------------------------------------------------
#  Slice 2: run_segmentation — single file, no LLM
# ------------------------------------------------------------------

def test_run_segmentation_single_file_no_llm_returns_one_segment(_stage):
    """single file, no LLM → 1 segment (text จากไฟล์) + match=create (ไม่มีสินค้าเดิม)."""
    staging = _stage

    batch_id = staging.create_batch([("k2.txt", b"Lagenio K2 smartwatch for kids")])
    result = staging.run_segmentation(batch_id, llm=None)

    assert len(result["segments"]) == 1
    seg = result["segments"][0]
    assert "Lagenio K2 smartwatch" in seg["text"]
    assert seg["product_key"] == ""  # no LLM → no key
    assert seg["suggested_name"]  # มีชื่อที่เสนอ (จากชื่อไฟล์)

    assert len(result["matches"]) == 1
    assert result["matches"][0]["action"] == "create"
    assert result["matches"][0]["target"] is None


def test_run_segmentation_suggested_name_from_filename(_stage):
    """suggested_name ต้องมาจากชื่อไฟล์ (เหมือน _makeProductNameFromFile ใน UI)."""
    staging = _stage

    batch_id = staging.create_batch([("Lagenio K2 spec.txt", b"some content")])
    result = staging.run_segmentation(batch_id, llm=None)

    # ชื่อไฟล์ "Lagenio K2 spec.txt" → suggested_name ต้องมี "Lagenio K2"
    assert "Lagenio K2" in result["segments"][0]["suggested_name"]


def test_run_segmentation_persists_to_batch_json(_stage, tmp_path):
    """run_segmentation ต้องเซฟ segments + matches ลง batch.json เพื่อดึงภายหลัง."""
    staging = _stage

    batch_id = staging.create_batch([("k2.txt", b"content")])
    staging.run_segmentation(batch_id, llm=None)

    import json
    batch_json = tmp_path / "data" / ".staging" / batch_id / "batch.json"
    data = json.loads(batch_json.read_text(encoding="utf-8"))
    assert data["status"] == "segmented"
    assert len(data["segments"]) == 1
    assert len(data["matches"]) == 1


# ------------------------------------------------------------------
#  Slice 3: run_segmentation — single file + duplicate hash → match=update
# ------------------------------------------------------------------

def test_run_segmentation_matches_existing_by_text_hash(_stage):
    """single file ที่ text ตรงสินค้าเดิม (ไม่มี product_key) → match=update (text_hash).

    สถานการณ์: user อัปโหลด k2.txt เป็นสินค้าใหม่ แต่มี Lagenio K2 อยู่แล้ว
    ที่มี raw_text ตรงกัน → ระบบต้องเสนอ update ไม่ใช่ create.
    """
    staging = _stage
    import src.product_db as product_db

    # สร้างสินค้าเดิม "Lagenio K2" ที่มี raw_text ตรงไฟล์ที่จะอัปโหลด
    shared_text = "Lagenio K2 smartwatch for kids. Price $99."
    product_id = "Lagenio K2"
    data_dir = product_db._project_root() / "data" / product_id
    data_dir.mkdir(parents=True)
    (data_dir / "k2.txt").write_text(shared_text, encoding="utf-8")
    rec = product_db.load(product_id)
    rec["product_id"] = product_id
    rec["status"] = product_db.STATUS_READY
    rec["raw_text"] = shared_text
    rec["files"] = [{"name": "k2.txt", "path": str(data_dir / "k2.txt"),
                     "hash": product_db.compute_file_hash(data_dir / "k2.txt"),
                     "status": "ingested"}]
    product_db.save(product_id, rec)

    # อัปโหลดไฟล์เดิมซ้ำเป็น staging batch
    batch_id = staging.create_batch([("k2.txt", shared_text.encode("utf-8"))])
    result = staging.run_segmentation(batch_id, llm=None)

    assert len(result["matches"]) == 1
    assert result["matches"][0]["action"] == "update"
    assert result["matches"][0]["target"] == "Lagenio K2"
    assert result["matches"][0]["match_by"] == "text_hash"


def test_run_segmentation_no_match_when_text_differs(_stage):
    """single file ที่ text ต่างจากสินค้าเดิม → match=create (ไม่ update)."""
    staging = _stage
    import src.product_db as product_db

    # สินค้าเดิมมี raw_text อื่น
    product_id = "Other Product"
    data_dir = product_db._project_root() / "data" / product_id
    data_dir.mkdir(parents=True)
    (data_dir / "info.txt").write_text("completely different product", encoding="utf-8")
    rec = product_db.load(product_id)
    rec["product_id"] = product_id
    rec["status"] = product_db.STATUS_READY
    rec["raw_text"] = "completely different product"
    product_db.save(product_id, rec)

    batch_id = staging.create_batch([("k2.txt", b"Lagenio K2 smartwatch")])
    result = staging.run_segmentation(batch_id, llm=None)

    assert result["matches"][0]["action"] == "create"
    assert result["matches"][0]["match_by"] is None


# ------------------------------------------------------------------
#  Slice 4: run_segmentation — catalog + LLM → multi segments + matches
# ------------------------------------------------------------------

class _FakeLLM:
    """LLM double — คืน segmentation JSON เมื่อถูกเรียกด้วย response_format."""
    def __init__(self, seg_response: dict):
        self._seg = seg_response
        self.calls: list[dict] = []

    def chat(self, messages, *, response_format=None, source="", **kwargs):
        self.calls.append({"source": source, "response_format": response_format})
        if "segment" in (source or ""):
            return json.dumps(self._seg, ensure_ascii=False)
        return "summary"

    def close(self):
        pass


def _make_catalog_text() -> str:
    lines = [
        "CACGO Price List", "MOQ: 3000pcs",
        "1 K67 CPU: ATS3085L", "Price: $21.50", "Battery: 530mAh",
        "2 K72 CPU: ATS3085L", "Price: $22.50", "Battery: 580mAh",
        "3 K71 CPU: Realtek", "Price: $18.00", "Battery: 600mAh",
        "Payment: 30% deposit", "Delivery: 3 working days",
    ]
    return "\n".join(lines)


def _make_seg_response() -> dict:
    return {
        "products": [
            {"product_key": "K67", "suggested_name": "CACGO K67", "category": "smartwatch",
             "summary": "K67", "source_refs": [{"file": "catalog.txt", "line_start": 3, "line_end": 5}],
             "common_refs": [{"file": "catalog.txt", "line_start": 1, "line_end": 2},
                             {"file": "catalog.txt", "line_start": 12, "line_end": 13}]},
            {"product_key": "K72", "suggested_name": "CACGO K72", "category": "smartwatch",
             "summary": "K72", "source_refs": [{"file": "catalog.txt", "line_start": 6, "line_end": 8}],
             "common_refs": [{"file": "catalog.txt", "line_start": 1, "line_end": 2},
                             {"file": "catalog.txt", "line_start": 12, "line_end": 13}]},
            {"product_key": "K71", "suggested_name": "CACGO K71", "category": "smartwatch",
             "summary": "K71", "source_refs": [{"file": "catalog.txt", "line_start": 9, "line_end": 11}],
             "common_refs": [{"file": "catalog.txt", "line_start": 1, "line_end": 2},
                             {"file": "catalog.txt", "line_start": 12, "line_end": 13}]},
        ]
    }


def test_run_segmentation_catalog_with_llm_returns_multi_segments(_stage):
    """catalog 3 รุ่น + LLM → 3 segments แต่ละตัวมี product_key + text เฉพาะรุ่น."""
    staging = _stage

    batch_id = staging.create_batch([("catalog.txt", _make_catalog_text().encode("utf-8"))])
    llm = _FakeLLM(_make_seg_response())
    result = staging.run_segmentation(batch_id, llm=llm)

    assert len(result["segments"]) == 3
    keys = [s["product_key"] for s in result["segments"]]
    assert keys == ["K67", "K72", "K71"]
    # แต่ละ segment มี text เฉพาะรุ่น
    k67 = next(s for s in result["segments"] if s["product_key"] == "K67")
    assert "$21.50" in k67["text"]
    assert "K72" not in k67["text"]
    # common refs อยู่ในทุกรุ่น
    assert "MOQ" in k67["text"]


def test_run_segmentation_catalog_matches_existing_by_key(_stage):
    """catalog ที่มีรุ่นตรงสินค้าเดิม → match=update สำหรับตัวที่ตรง, create สำหรับตัวใหม่."""
    staging = _stage
    import src.product_db as product_db

    # สินค้าเดิม: K67, K72 (มี product_key ใน scope)
    for key, name in [("K67", "CACGO K67"), ("K72", "CACGO K72")]:
        d = product_db._project_root() / "data" / name
        d.mkdir(parents=True)
        rec = product_db.load(name)
        rec["product_id"] = name
        rec["status"] = product_db.STATUS_READY
        rec["scope"] = {"product_key": key}
        product_db.save(name, rec)

    batch_id = staging.create_batch([("catalog.txt", _make_catalog_text().encode("utf-8"))])
    llm = _FakeLLM(_make_seg_response())
    result = staging.run_segmentation(batch_id, llm=llm)

    # K67, K72 → update; K71 → create
    actions = {m["segment"]["product_key"]: m["action"] for m in result["matches"]}
    assert actions["K67"] == "update"
    assert actions["K72"] == "update"
    assert actions["K71"] == "create"
    # target ของ update ต้องเป็นชื่อสินค้าเดิม
    k67_match = next(m for m in result["matches"] if m["segment"]["product_key"] == "K67")
    assert k67_match["target"] == "CACGO K67"
    assert k67_match["match_by"] == "key"


def test_run_segmentation_llm_single_product_falls_to_single(_stage):
    """LLM บอก 1 สินค้า → ใช้ single flow (1 segment)."""
    staging = _stage

    single_seg = {"products": [{
        "product_key": "K2", "suggested_name": "Lagenio K2", "category": "smartwatch",
        "summary": "K2", "source_refs": [{"file": "k2.txt", "line_start": 1, "line_end": 1}],
        "common_refs": [],
    }]}
    batch_id = staging.create_batch([("k2.txt", b"Lagenio K2 smartwatch")])
    llm = _FakeLLM(single_seg)
    result = staging.run_segmentation(batch_id, llm=llm)

    assert len(result["segments"]) == 1
    assert result["segments"][0]["product_key"] == "K2"


# ------------------------------------------------------------------
#  Slice 5: commit_batch — single create
# ------------------------------------------------------------------

def test_commit_batch_single_create_makes_product(_stage, tmp_path):
    """commit_batch กับ action=create → สร้างสินค้าใหม่ + ลบ staging."""
    staging = _stage
    import src.product_db as product_db

    batch_id = staging.create_batch([("k2.txt", b"Lagenio K2 smartwatch")])
    staging.run_segmentation(batch_id, llm=None)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "Lagenio K2"},
    ])

    assert result["created"] == ["Lagenio K2"]
    assert result["updated"] == []
    # สินค้าถูกสร้าง
    rec = product_db.load("Lagenio K2")
    assert rec["status"] == product_db.STATUS_READY
    assert "Lagenio K2 smartwatch" in rec["raw_text"]
    # ไฟล์ถูกย้ายจาก staging ไป data/
    assert (tmp_path / "data" / "Lagenio K2" / "k2.txt").exists()
    # staging ถูกลบ
    assert not (tmp_path / "data" / ".staging" / batch_id).exists()


def test_commit_batch_skips_unselected_segments(_stage):
    """commit_batch ทำเฉพาะ segment ที่อยู่ใน choices — ตัวที่ไม่ส่งมาไม่ถูกสร้าง."""
    staging = _stage
    import src.product_db as product_db

    batch_id = staging.create_batch([("catalog.txt", _make_catalog_text().encode("utf-8"))])
    llm = _FakeLLM(_make_seg_response())
    staging.run_segmentation(batch_id, llm=llm)

    # เลือกแค่ K67 (segment 0) — K72, K71 ไม่ส่ง
    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "CACGO K67"},
    ])

    assert result["created"] == ["CACGO K67"]
    all_products = [p["product_id"] for p in product_db.get_all_products()]
    assert "CACGO K67" in all_products
    assert "CACGO K72" not in all_products
    assert "CACGO K71" not in all_products


def test_commit_batch_uses_suggested_name_if_name_omitted(_stage):
    """ถ้า choice ไม่ส่ง name → ใช้ suggested_name จาก segment."""
    staging = _stage
    import src.product_db as product_db

    batch_id = staging.create_batch([("k2.txt", b"content")])
    staging.run_segmentation(batch_id, llm=None)
    seg = staging.run_segmentation(batch_id, llm=None)["segments"][0]

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create"},  # ไม่มี name
    ])

    assert result["created"] == [seg["suggested_name"]]
    assert product_db.load(seg["suggested_name"])["status"] == product_db.STATUS_READY


# ------------------------------------------------------------------
#  Slice 6: commit_batch — single update (อัปเดตสินค้าเดิม)
# ------------------------------------------------------------------

def test_commit_batch_single_update_refreshes_existing_product(_stage):
    """commit_batch กับ action=update → อัปเดต record ของสินค้าเดิม ไม่สร้างใหม่."""
    staging = _stage
    import src.product_db as product_db

    # สินค้าเดิม "Lagenio K2" ที่มี raw_text เดิม
    shared_text = "Lagenio K2 smartwatch for kids. Price $99."
    product_id = "Lagenio K2"
    data_dir = product_db._project_root() / "data" / product_id
    data_dir.mkdir(parents=True)
    (data_dir / "k2.txt").write_text(shared_text, encoding="utf-8")
    rec = product_db.load(product_id)
    rec["product_id"] = product_id
    rec["status"] = product_db.STATUS_READY
    rec["raw_text"] = shared_text
    rec["files"] = [{"name": "k2.txt", "path": str(data_dir / "k2.txt"),
                     "hash": product_db.compute_file_hash(data_dir / "k2.txt"),
                     "status": "ingested"}]
    product_db.save(product_id, rec)

    # อัปโหลดไฟล์เดิมซ้ำ → segmentation เสนอ update
    batch_id = staging.create_batch([("k2.txt", shared_text.encode("utf-8"))])
    staging.run_segmentation(batch_id, llm=None)

    # user เลือก update
    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "update", "target": "Lagenio K2"},
    ])

    assert result["updated"] == ["Lagenio K2"]
    assert result["created"] == []
    # ไม่มีสินค้าใหม่โผล่
    all_products = [p["product_id"] for p in product_db.get_all_products()]
    assert all_products == ["Lagenio K2"]
    # record ยัง status=ready
    assert product_db.load("Lagenio K2")["status"] == product_db.STATUS_READY
    # staging ถูกลบ
    assert not staging._batch_dir(batch_id).exists()


def test_commit_batch_update_without_target_raises(_stage):
    """update ที่ไม่ส่ง target → error (ต้องบอกว่าจะ update สินค้าไหน)."""
    staging = _stage

    batch_id = staging.create_batch([("k2.txt", b"content")])
    staging.run_segmentation(batch_id, llm=None)

    with pytest.raises(ValueError, match="target"):
        staging.commit_batch(batch_id, [
            {"segment_index": 0, "action": "update"},  # ไม่มี target
        ])


def test_commit_batch_skip_action_does_nothing(_stage):
    """action=skip → ไม่สร้าง/อัปเดตอะไร แต่ commit สำเร็จ."""
    staging = _stage
    import src.product_db as product_db

    batch_id = staging.create_batch([("k2.txt", b"content")])
    staging.run_segmentation(batch_id, llm=None)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "skip"},
    ])

    assert result["created"] == []
    assert result["updated"] == []
    assert product_db.get_all_products() == []


def test_commit_batch_mix_create_and_skip(_stage):
    """catalog 2 รุ่น: เลือก create ตัวที่ 1, skip ตัวที่ 2 → สร้างแค่ตัวเดียว."""
    staging = _stage
    import src.product_db as product_db

    batch_id = staging.create_batch([("catalog.txt", _make_catalog_text().encode("utf-8"))])
    llm = _FakeLLM(_make_seg_response())
    staging.run_segmentation(batch_id, llm=llm)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "CACGO K67"},
        {"segment_index": 1, "action": "skip"},
        {"segment_index": 2, "action": "skip"},
    ])

    assert result["created"] == ["CACGO K67"]
    assert result["updated"] == []
    all_products = sorted(p["product_id"] for p in product_db.get_all_products())
    assert all_products == ["CACGO K67"]


# ------------------------------------------------------------------
#  Slice 7: commit_batch — catalog mix update/create
# ------------------------------------------------------------------

def test_commit_batch_catalog_mix_update_and_create(_stage):
    """catalog 3 รุ่น: K67/K72 update ของเดิม, K71 create ใหม่ ในการ commit ครั้งเดียว."""
    staging = _stage
    import src.product_db as product_db

    # สินค้าเดิม K67, K72
    for key, name in [("K67", "CACGO K67"), ("K72", "CACGO K72")]:
        d = product_db._project_root() / "data" / name
        d.mkdir(parents=True)
        rec = product_db.load(name)
        rec["product_id"] = name
        rec["status"] = product_db.STATUS_READY
        rec["scope"] = {"product_key": key}
        product_db.save(name, rec)

    batch_id = staging.create_batch([("catalog.txt", _make_catalog_text().encode("utf-8"))])
    llm = _FakeLLM(_make_seg_response())
    staging.run_segmentation(batch_id, llm=llm)

    # user เลือก: K67 update, K72 update, K71 create
    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "update", "target": "CACGO K67"},
        {"segment_index": 1, "action": "update", "target": "CACGO K72"},
        {"segment_index": 2, "action": "create", "name": "CACGO K71"},
    ])

    assert sorted(result["updated"]) == ["CACGO K67", "CACGO K72"]
    assert result["created"] == ["CACGO K71"]
    # มี 3 สินค้า ไม่มี duplicate
    all_products = sorted(p["product_id"] for p in product_db.get_all_products())
    assert all_products == ["CACGO K67", "CACGO K71", "CACGO K72"]


def test_commit_batch_catalog_user_skips_one(_stage):
    """catalog 3 รุ่น: user เลือกแค่ K67, K71 — ข้าม K72 → ไม่สร้าง K72."""
    staging = _stage
    import src.product_db as product_db

    batch_id = staging.create_batch([("catalog.txt", _make_catalog_text().encode("utf-8"))])
    llm = _FakeLLM(_make_seg_response())
    staging.run_segmentation(batch_id, llm=llm)

    # ส่งแค่ K67 (idx 0) และ K71 (idx 2) — ข้าม K72 (idx 1)
    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "CACGO K67"},
        {"segment_index": 2, "action": "create", "name": "CACGO K71"},
    ])

    all_products = sorted(p["product_id"] for p in product_db.get_all_products())
    assert all_products == ["CACGO K67", "CACGO K71"]
    assert "CACGO K72" not in all_products


def test_commit_batch_catalog_user_overrides_name(_stage):
    """user แก้ชื่อสินค้าใน preview → commit ใช้ชื่อที่ user ตั้ง ไม่ใช้ suggested_name."""
    staging = _stage
    import src.product_db as product_db

    batch_id = staging.create_batch([("catalog.txt", _make_catalog_text().encode("utf-8"))])
    llm = _FakeLLM(_make_seg_response())
    staging.run_segmentation(batch_id, llm=llm)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "My Custom K67 Name"},
    ])

    assert result["created"] == ["My Custom K67 Name"]
    assert product_db.load("My Custom K67 Name")["status"] == product_db.STATUS_READY
