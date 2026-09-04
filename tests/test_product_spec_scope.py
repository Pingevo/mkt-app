"""Tests: product_spec agent ต้องได้ raw_data แบบ scoped — เฉพาะรุ่นที่เลือก.

บั๊ก: เดิม _run_single_agent ส่ง raw_contents จาก _read_folder (ไฟล์ดิบทั้งไฟล์)
ให้ product_spec agent ทำให้ LLM เห็นทั้งแคตตาล็อกและเขียนสเปคทั้งซีรีส์
แทนที่จะเขียนเฉพาะรุ่นที่เลือก (เช่น K73 แต่ผลออกมาเป็น K52–K77 ทั้งซีรีส์)

แนวแก้: product_spec ดึง raw_data ผ่าน product_db.get_product_spec_raw_data(folders)
ซึ่งเคารพ scope (ตัดเฉพาะส่วนของรุ่นนั้นจาก catalog) เหมือน agent การตลาดอื่น
"""
import importlib

import pytest


@pytest.fixture
def _pdb(monkeypatch, tmp_path):
    """Setup tmp project root + reload product_db."""
    import src.product_db as product_db

    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    importlib.reload(product_db)
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    return product_db


def _make_scoped_product(product_db, product_id: str, product_key: str,
                         own_text: str, common_text: str = "MOQ 3000\n",
                         split_from: str = "Catalog"):
    """สร้าง product.json ของสินค้าที่แยกจาก catalog พร้อม scope."""
    record = product_db.load(product_id)
    record["status"] = "ready"
    record["raw_text"] = f"{common_text}\n{own_text}"
    record["scope"] = {
        "product_key": product_key,
        "source_refs": [{"file": "catalog.pdf", "line_start": 1, "line_end": 10}],
        "common_refs": [{"file": "catalog.pdf", "line_start": 0, "line_end": 1}],
        "split_from": split_from,
    }
    product_db.save(product_id, record)


# ------------------------------------------------------------------
#  Single product — scoped raw_data ต้องมีเฉพาะรุ่นนั้น
# ------------------------------------------------------------------

def test_single_scoped_product_excludes_other_models(_pdb):
    """K73 เพียงตัวเดียว → raw_data มี K73 แต่ห้ามมี K52/K77/K72."""
    product_db = _pdb
    _make_scoped_product(product_db, "CACGO K73", "K73",
                         own_text="K73 CPU Realtek 8763 หน้าจอ 1.96\"")
    _make_scoped_product(product_db, "CACGO K52", "K52",
                         own_text="K52 CPU ATS3085L หน้าจอ 1.43\"")
    _make_scoped_product(product_db, "CACGO K77", "K77",
                         own_text="K77 แบต 1000mAh ราคา US$15")

    raw_data = product_db.get_scoped_context_text(["CACGO K73"])

    assert "K73" in raw_data
    assert "K52" not in raw_data
    assert "K77" not in raw_data


def test_single_scoped_product_has_scope_header(_pdb):
    """raw_data ต้องมี header บอกว่าเป็นสินค้าใด — ให้ LLM ไม่สับสน."""
    product_db = _pdb
    _make_scoped_product(product_db, "CACGO K73", "K73",
                         own_text="K73 สเปค", split_from="CACGO Catalog")

    raw_data = product_db.get_scoped_context_text(["CACGO K73"])

    assert "รหัสสินค้า: K73" in raw_data
    assert "แยกจาก: CACGO Catalog" in raw_data


# ------------------------------------------------------------------
#  Combined products — รวมหลายรุ่น แต่ห้ามปนรุ่นอื่นนอกรายการ
# ------------------------------------------------------------------

def test_combined_scoped_products_includes_only_selected(_pdb):
    """เลือก K59 + K59A → raw_data มี K59 และ K59A แต่ห้ามมี K52/K77."""
    product_db = _pdb
    _make_scoped_product(product_db, "CACGO K59", "K59",
                         own_text="K59 หน้าจอ IPS 1.39\"")
    _make_scoped_product(product_db, "CACGO K59A", "K59A",
                         own_text="K59A หน้าจอ AMOLED 1.6\"")
    _make_scoped_product(product_db, "CACGO K52", "K52",
                         own_text="K52 รุ่นเริ่มต้น")
    _make_scoped_product(product_db, "CACGO K77", "K77",
                         own_text="K77 แบตใหญ่")

    raw_data = product_db.get_scoped_context_text(["CACGO K59", "CACGO K59A"])

    assert "K59" in raw_data
    assert "K59A" in raw_data
    assert "K52" not in raw_data
    assert "K77" not in raw_data


# ------------------------------------------------------------------
#  Product ที่ไม่มี scope (ไฟล์เดียว สินค้าเดียว) — ใช้ raw_text เต็ม
# ------------------------------------------------------------------

def test_product_without_scope_uses_full_raw_text(_pdb):
    """สินค้าที่ไม่ได้แยกจาก catalog → ใช้ raw_text ทั้งหมด (ไม่มี scope header)."""
    product_db = _pdb
    record = product_db.load("Lagenio K3")
    record["status"] = "ready"
    record["raw_text"] = "Lagenio K3 สมาร์ทวอทช์สำหรับเด็ก มี GPS โทรได้"
    product_db.save("Lagenio K3", record)

    raw_data = product_db.get_scoped_context_text(["Lagenio K3"])

    assert "Lagenio K3" in raw_data
    assert "ขอบเขตสินค้า" not in raw_data  # ไม่มี scope header


# ------------------------------------------------------------------
#  สินค้าที่ยังไม่ ingest — คืนว่าง ไม่ crash
# ------------------------------------------------------------------

def test_nonexistent_product_returns_empty(_pdb):
    """สินค้าที่ไม่มีใน DB → คืน "" (caller จัดการกรณีนี้เอง)."""
    product_db = _pdb
    raw_data = product_db.get_scoped_context_text(["สินค้าที่ไม่มีจริง"])
    assert raw_data == ""


# ------------------------------------------------------------------
#  Integration: _run_single_agent ส่ง scoped context เข้า orchestrator
#  ไม่ใช้ raw_contents (catalog เต็ม) เมื่อ folders ถูกส่งมา
# ------------------------------------------------------------------

def test_run_single_agent_uses_scoped_context_not_raw_catalog(_pdb, monkeypatch, tmp_path):
    """_run_single_agent('product_spec', folders=[...]) ต้องเรียก
    product_db.get_scoped_context_text(folders) แล้วส่ง scoped text
    เข้า orch.run_product_spec() — ไม่ใช่ raw_contents ที่เป็น catalog เต็ม.

    สถานการณ์จริง: K69+K72 จาก catalog ร่วม — raw_contents มีทั้งซีรีส์
    แต่ scoped text มีเฉพาะ K69/K72 เท่านั้น
    """
    product_db = _pdb
    _make_scoped_product(product_db, "CACGO K69", "K69",
                         own_text="K69 CPU ATS3085L หน้าจอ 1.85\"")
    _make_scoped_product(product_db, "CACGO K72", "K72",
                         own_text="K72 CPU ATS3085L หน้าจอ 2.13\"")
    _make_scoped_product(product_db, "CACGO K52", "K52",
                         own_text="K52 รุ่นเริ่มต้นที่ต้องไม่ปรากฏ")
    _make_scoped_product(product_db, "CACGO K77", "K77",
                         own_text="K77 รุ่นสูงสุดที่ต้องไม่ปรากฏ")

    # raw_contents จำลอง catalog เต็ม — มีทุกรุ่น
    raw_contents = ["K52 ... K69 ... K72 ... K77 ... ทั้งซีรีส์"]

    # Mock orchestrator — จับ raw_data ที่ส่งเข้า run_product_spec
    captured_raw_data = {}

    class FakeOrch:
        def __init__(self):
            self.product_id = ""
            self.product_images = []
            self.results = {}

        def run_product_spec(self, raw_data, product_images=None, llm=None, quick_brief=""):
            captured_raw_data["raw_data"] = raw_data
            captured_raw_data["product_images"] = product_images
            return "ชื่อสินค้า: mock"

        def save_result(self, key, output_dir):
            return {key: str(tmp_path / "output.txt")}

    # Import _run_single_agent from web_viewer
    import web_viewer

    fake_orch = FakeOrch()
    fake_llm = type("FakeLLM", (), {"close": lambda self: None})()

    results = web_viewer._run_single_agent(
        "product_spec", "CACGO K69 + CACGO K72", raw_contents,
        image_paths=[], ready_contents={}, orch=fake_orch, llm=fake_llm,
        output_dir=tmp_path, save_output=False,
        folders=["CACGO K69", "CACGO K72"],
    )

    # raw_data ที่ส่งเข้า run_product_spec ต้องมาจาก get_scoped_context_text
    # ไม่ใช่ raw_contents (catalog เต็ม)
    sent_raw = captured_raw_data["raw_data"]
    assert "K69" in sent_raw
    assert "K72" in sent_raw
    # ต้องไม่มี K52/K77 ที่อยู่นอก scope
    assert "K52" not in sent_raw
    assert "K77" not in sent_raw
    # ต้องไม่ใช่ raw_contents เดิม (ที่มี "ทั้งซีรีส์")
    assert "ทั้งซีรีส์" not in sent_raw


def test_run_single_agent_falls_back_to_raw_when_no_folders(_pdb, monkeypatch, tmp_path):
    """ถ้าไม่ส่ง folders → ใช้ raw_contents แทน (backward compatible)."""
    product_db = _pdb

    captured_raw_data = {}

    class FakeOrch:
        def __init__(self):
            self.product_id = ""
            self.product_images = []
            self.results = {}

        def run_product_spec(self, raw_data, product_images=None, llm=None, quick_brief=""):
            captured_raw_data["raw_data"] = raw_data
            return "ชื่อสินค้า: mock"

        def save_result(self, key, output_dir):
            return {key: str(tmp_path / "output.txt")}

    import web_viewer

    fake_orch = FakeOrch()
    fake_llm = type("FakeLLM", (), {"close": lambda self: None})()

    raw_contents = ["ข้อมูลดิบของสินค้าที่ไม่ได้แยก scope"]
    web_viewer._run_single_agent(
        "product_spec", "SomeProduct", raw_contents,
        image_paths=[], ready_contents={}, orch=fake_orch, llm=fake_llm,
        output_dir=tmp_path, save_output=False,
        folders=None,
    )

    # ต้องใช้ raw_contents เพราะไม่มี folders
    assert "ข้อมูลดิบของสินค้า" in captured_raw_data["raw_data"]


# ------------------------------------------------------------------
#  Identity envelope — generic multi-product source identity
#  (regression for get_scoped_context_text identity labels)
# ------------------------------------------------------------------


def _make_unscoped_product(product_db, product_id: str, raw_text: str):
    """สร้าง product ที่ไม่มี scope (ไฟล์เดียว สินค้าเดียว)."""
    record = product_db.load(product_id)
    record["status"] = "ready"
    record["raw_text"] = raw_text
    product_db.save(product_id, record)


def test_single_product_has_identity_envelope(_pdb):
    """Single product context ต้องมี identity boundary ครอบ —
    ระบุ product ID เสมอ ไม่เฉพาะ record ที่มี scope."""
    product_db = _pdb
    _make_unscoped_product(product_db, "SoloProd", "solo raw text content")

    raw_data = product_db.get_scoped_context_text(["SoloProd"])

    assert "เริ่มข้อมูลสินค้า: SoloProd" in raw_data
    assert "สิ้นสุดข้อมูลสินค้า: SoloProd" in raw_data
    # raw content ยังอยู่ภายใน envelope
    assert "solo raw text content" in raw_data


def test_multi_product_identity_boundaries_separate(_pdb):
    """Multi-product A/B ต้องมี boundary แยกและข้อมูลแต่ละตัวอยู่
    ภายใน boundary ของตัวเอง — ไม่ปนกัน."""
    product_db = _pdb
    _make_unscoped_product(product_db, "ProdA", "content alpha for A")
    _make_unscoped_product(product_db, "ProdB", "content beta for B")

    raw_data = product_db.get_scoped_context_text(["ProdA", "ProdB"])

    a_start = raw_data.find("เริ่มข้อมูลสินค้า: ProdA")
    a_end = raw_data.find("สิ้นสุดข้อมูลสินค้า: ProdA")
    b_start = raw_data.find("เริ่มข้อมูลสินค้า: ProdB")
    b_end = raw_data.find("สิ้นสุดข้อมูลสินค้า: ProdB")

    # ทั้งสอง boundary ปรากฏ
    assert a_start != -1 and a_end != -1
    assert b_start != -1 and b_end != -1

    # content alpha อยู่ใน boundary A เท่านั้น
    alpha_pos = raw_data.find("content alpha for A")
    assert a_start < alpha_pos < a_end, "alpha content outside A boundary"
    assert not (b_start < alpha_pos < b_end), "alpha content leaked into B boundary"

    # content beta อยู่ใน boundary B เท่านั้น
    beta_pos = raw_data.find("content beta for B")
    assert b_start < beta_pos < b_end, "beta content outside B boundary"
    assert not (a_start < beta_pos < a_end), "beta content leaked into A boundary"


def test_scoped_product_keeps_inner_scope_header_within_envelope(_pdb):
    """Product ที่มี scope.product_key ยังเก็บ inner scope header ไว้
    และไม่ทำข้อมูลหาย — envelope นอกเป็น identity จาก runtime arg,
    inner scope header เป็น metadata ของ record."""
    product_db = _pdb
    _make_scoped_product(product_db, "CACGO K73", "K73",
                         own_text="K73 CPU Realtek 8763", split_from="CACGO Catalog")

    raw_data = product_db.get_scoped_context_text(["CACGO K73"])

    # envelope นอก
    assert "เริ่มข้อมูลสินค้า: CACGO K73" in raw_data
    assert "สิ้นสุดข้อมูลสินค้า: CACGO K73" in raw_data
    # inner scope header ยังอยู่
    assert "รหัสสินค้า: K73" in raw_data
    assert "แยกจาก: CACGO Catalog" in raw_data
    # raw content ยังอยู่
    assert "K73 CPU Realtek 8763" in raw_data


def test_empty_or_missing_product_no_misleading_block(_pdb):
    """Empty/missing product ต้องไม่สร้าง misleading block —
    คืน "" ถ้าไม่มีสินค้าใดมีข้อมูล."""
    product_db = _pdb

    # product ที่ไม่มีใน DB
    assert product_db.get_scoped_context_text(["ไม่มีจริง"]) == ""

    # product ที่มี record แต่ raw_text ว่าง
    rec = product_db.load("EmptyProd")
    rec["status"] = "ready"
    rec["raw_text"] = ""
    product_db.save("EmptyProd", rec)
    assert product_db.get_scoped_context_text(["EmptyProd"]) == ""

    # mix: มี empty + มี content → มีเฉพาะ envelope ของตัวที่มี content
    _make_unscoped_product(product_db, "HasContent", "real content")
    raw_data = product_db.get_scoped_context_text(["EmptyProd", "HasContent"])
    assert "เริ่มข้อมูลสินค้า: HasContent" in raw_data
    assert "เริ่มข้อมูลสินค้า: EmptyProd" not in raw_data


def test_raw_contents_not_modified_within_envelope(_pdb):
    """raw source content ต้องไม่ถูกเปลี่ยน — envelope เพิ่มเฉพาะ
    boundary นอก ไม่แตะเนื้อหาข้างใน."""
    product_db = _pdb
    original_text = "line1\nline2 with $pecial chars\nline3"
    _make_unscoped_product(product_db, "RawCheck", original_text)

    raw_data = product_db.get_scoped_context_text(["RawCheck"])

    # raw text ต้องอยู่ครบเหมือนเดิม
    assert original_text in raw_data
