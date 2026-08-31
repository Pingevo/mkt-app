"""Bug: Agent 2 relevance gate ตึงเกินไป ทำให้ model เห็น manifest ว่างเมื่อ
ผลค้นหาเป็น smartwatch ทั่วไปไม่ mention รุ่น.

Market parity fix: ส่งผลค้นหาทั้งหมดให้ model พร้อม label ให้ model
ตัดสินใจเองว่าจะใช้ source ไหน — เหมือน Claude/ChatGPT ทำ.

test นี้ยืนยันว่า:
1. เมื่อผลค้นหาเป็น generic smartwatch (ไม่ mention รุ่น) manifest ไม่ว่าง
2. model สามารถ cite generic source ได้ ถ้า source พูดถึง competitor
3. competitor_names มาจาก input เสมอ แม้ evidence บาง/ว่าง
4. limited analysis (evidence: [] + uncertainty) ผ่าน validation
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from src.agents.competitor_analysis import CompetitorAnalysisAgent
from src.agents.competitor_evidence import CompetitorReportRenderer, ResearchResponse


class FakeLLM:
    """Minimal fake LLM for unit testing agent internals."""

    def chat(self, *args, **kwargs):
        return ""

    def close(self):
        pass


def _make_agent(product_spec: str, competitor_data: str) -> CompetitorAnalysisAgent:
    cfg = {
        "web_search": True,
        "evidence_mode": True,
        "max_retry_limit": 0,
        "relevance_policy": {
            "target_category_keywords": ["smartwatch", "สมาร์ทวอทช์"],
            "mismatch_keywords": ["keyboard", "mouse", "headphones"],
            "subcategory_mismatch_keywords": ["kids", "children"],
            "market_keywords": ["smartwatch", "สมาร์ทวอทช์", "smartband"],
        },
    }
    agent = CompetitorAnalysisAgent(cfg, FakeLLM())
    agent.build_prompt(product_spec, competitor_data)
    return agent


# ---------------------------------------------------------------------------
# Seam 1: manifest ไม่ว่างเมื่อผลค้นหาเป็น generic smartwatch
# ---------------------------------------------------------------------------


def test_manifest_not_empty_when_only_generic_smartwatch_results():
    """เมื่อผลค้นหาเป็น smartwatch ทั่วไป (ไม่ mention รุ่น) manifest ต้องไม่ว่าง.

    สถานการณ์จริง: user สั่งวิเคราะห์ LAGENIO K2 vs imoo Z1
    web search คืน smartwatch ทั่วไป ไม่ mention รุ่นใดเลย
    ปัจจุบัน: _append_citations_and_verify กรอง relevant=None ออก →
             _last_relevant_annotations ว่าง → manifest ว่าง → model แก้ไม่ได้
    หลังแก้: _last_relevant_annotations รวม relevant=None ด้วย →
            manifest มีลิงก์ทั้งหมดพร้อม label ให้ model ตัดสินใจ
    """
    product_spec = "รหัสสินค้า: K2\nLAGENIO K2 kids smartwatch with GPS and Heart Rate"
    competitor_data = "imoo Z1\nHuawei Watch Kids"
    agent = _make_agent(product_spec, competitor_data)

    # Simulate web search results: generic smartwatch articles, no model names
    generic_annotations = [
        {
            "url": "https://example.com/smartwatch-review-2024",
            "title": "Best smartwatch for kids 2024",
            "content": "Review of kids smartwatches with GPS and heart rate monitoring",
        },
        {
            "url": "https://example.com/smartwatch-comparison",
            "title": "Smartwatch comparison guide",
            "content": "Comparing smartwatch features for children",
        },
    ]

    # Process through _append_citations_and_verify like the real code path
    agent._append_citations_and_verify("some output text", generic_annotations)

    # _last_relevant_annotations must include ALL non-rejected annotations
    # (relevant=True + relevant=None), not just relevant=True
    assert len(agent._last_relevant_annotations) >= 2, (
        f"_last_relevant_annotations ต้องมี relevant=None ด้วย ไม่ใช่เฉพาะ relevant=True. "
        f"ได้ {len(agent._last_relevant_annotations)} จาก 2"
    )

    manifest = agent._build_evidence_manifest()

    # Manifest must mention the URLs — model needs to see them
    assert "example.com/smartwatch-review-2024" in manifest, (
        "manifest ต้องมี URL ของผลค้นหา generic smartwatch ให้ model เห็น"
    )
    assert "example.com/smartwatch-comparison" in manifest, (
        "manifest ต้องมี URL ทุกลิงก์ ไม่ใช่เฉพาะที่ผ่าน gate"
    )


# ---------------------------------------------------------------------------
# Seam 2: renderer ยอมรับ URL จาก annotation ที่ไม่ใช่ relevance_type=competitor
# ---------------------------------------------------------------------------


def test_renderer_accepts_url_from_market_match_annotation():
    """renderer ต้องยอมรับ URL จาก annotation ที่เป็น market/category match
    ไม่ใช่เฉพาะ competitor-specific เท่านั้น.

    สถานการณ์: model เลือกใช้ smartwatch review ทั่วไปมาอ้างอิง
    ถ้า source พูดถึง competitor จริง → ผ่าน
    """
    research = ResearchResponse.from_dict({
        "target_model": "K2",
        "competitor_names": ["imoo Z1"],
        "evidence": [
            {
                "competitor": "imoo Z1",
                "field": "price_availability",
                "claim": "imoo Z1 ราคาประมาณ 3,000-4,000 บาท",
                "url": "https://example.com/smartwatch-review",
                "geography": "global",
            }
        ],
        "recommendations": [],
        "uncertainty": ["ไม่พบราคาที่แน่นอน"],
    })

    # Annotation is a market match (not competitor-specific by gate)
    # but the content DOES mention "imoo Z1"
    annotations = [
        {
            "url": "https://example.com/smartwatch-review",
            "title": "Best smartwatch for kids 2024",
            "content": "imoo Z1 is a popular kids smartwatch with GPS",
            "_relevance": {
                "relevant": None,
                "relevance_type": "market_unverified",
                "reason": "source is about smartwatch market/category but not product-specific",
                "geography": "global",
            },
        }
    ]

    renderer = CompetitorReportRenderer(research, relevant_annotations=annotations)
    errors = renderer.validate()

    # Should NOT error: source mentions "imoo Z1" even though gate said "market_unverified"
    url_errors = [e for e in errors if "URL not in selected evidence" in e or "not competitor-specific" in e]
    assert not url_errors, (
        f"renderer ไม่ควรปฏิเสธ URL ที่ source พูดถึง competitor จริง "
        f"แม้ relevance_type จะเป็น market_unverified: {url_errors}"
    )


# ---------------------------------------------------------------------------
# Seam 3: limited analysis (evidence: [] + uncertainty) ผ่าน validation
# ---------------------------------------------------------------------------


def test_limited_analysis_with_empty_evidence_passes_validation():
    """เมื่อไม่มี evidence เจอเลย model คืน evidence: [] + uncertainty
    ต้องผ่าน validation ไม่ใช่ fail ด้วย 'competitor_names is empty'.

    สถานการณ์: ค้นเว็บแล้วไม่พบข้อมูลเฉพาะรุ่นเลย
    model คืน competitor_names จาก input + evidence: [] + uncertainty
    ผลลัพธ์: limited analysis ที่บอกว่า "ไม่พบหลักฐานเฉพาะรุ่น"
    """
    research_json = {
        "target_model": "K2",
        "competitor_names": ["imoo Z1", "Huawei Watch Kids"],
        "evidence": [],
        "recommendations": ["แนะนำให้ค้นหาข้อมูลเพิ่มเติมจาก official product page"],
        "uncertainty": ["ไม่พบหลักฐานเฉพาะรุ่นของ imoo Z1 และ Huawei Watch Kids จากการค้นหา"],
    }

    product_spec = "รหัสสินค้า: K2\nLAGENIO K2 kids smartwatch"
    competitor_data = "imoo Z1\nHuawei Watch Kids"
    agent = _make_agent(product_spec, competitor_data)
    agent._last_relevant_annotations = []

    ok, err, research = agent._validate_research_json(json.dumps(research_json))

    assert ok, (
        f"limited analysis (evidence: [] + uncertainty) ต้องผ่าน validation: {err}"
    )
    assert research is not None
    assert research.competitor_names == ["imoo Z1", "Huawei Watch Kids"]


# ---------------------------------------------------------------------------
# Regression: competitor-specific evidence still works
# ---------------------------------------------------------------------------


def test_competitor_specific_evidence_still_validates():
    """เมื่อค้นเจอ source เฉพาะรุ่นคู่แข่ง ต้องผ่าน validation เหมือนเดิม."""
    research_json = {
        "target_model": "K2",
        "competitor_names": ["imoo Z1"],
        "evidence": [
            {
                "competitor": "imoo Z1",
                "field": "price_availability",
                "claim": "imoo Z1 ราคา 3,999 บาท",
                "url": "https://imoo.com/z1",
                "geography": "thailand",
            }
        ],
        "recommendations": [],
        "uncertainty": [],
    }

    product_spec = "รหัสสินค้า: K2\nLAGENIO K2 kids smartwatch"
    competitor_data = "imoo Z1"
    agent = _make_agent(product_spec, competitor_data)
    agent._last_relevant_annotations = [
        {
            "url": "https://imoo.com/z1",
            "title": "imoo Z1 official page",
            "content": "imoo Z1 ราคา 3,999 บาท",
            "_relevance": {
                "relevant": True,
                "relevance_type": "competitor",
                "reason": "source matches competitor imoo Z1",
                "geography": "thailand",
            },
        }
    ]

    ok, err, research = agent._validate_research_json(json.dumps(research_json))
    assert ok, f"competitor-specific evidence ต้องผ่าน validation: {err}"


if __name__ == "__main__":
    tests = [
        test_manifest_not_empty_when_only_generic_smartwatch_results,
        test_renderer_accepts_url_from_market_match_annotation,
        test_limited_analysis_with_empty_evidence_passes_validation,
        test_competitor_specific_evidence_still_validates,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
