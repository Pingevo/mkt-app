"""Two-stage evidence contract for Competitor Analysis.

Stage A: the LLM returns a ResearchResponse (JSON) of selected evidence.
Stage B: CompetitorReportRenderer turns that into deterministic Markdown.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


FIELD_CATALOG = {
    "display": {
        "label": "หน้าจอ",
        "keywords": ["หน้าจอ", "display", "screen"],
    },
    "chipset_os": {
        "label": "ชิปเซ็ต / OS",
        "keywords": ["CPU", "chipset", "processor", "ชิป", "หน่วยประมวลผล"],
    },
    "battery": {
        "label": "แบตเตอรี่",
        "keywords": ["แบตเตอรี่", "battery", "batt"],
    },
    "connectivity": {
        "label": "การเชื่อมต่อ",
        "keywords": ["Bluetooth", "connectivity", "wifi", "เชื่อมต่อ", "การเชื่อมต่อ"],
    },
    "water_resistance": {
        "label": "กันน้ำ",
        "keywords": ["กันน้ำ", "water resistance", "IP68", "5ATM"],
    },
    "gps": {
        "label": "GPS",
        "keywords": ["GPS", "GLONASS", "จีพีเอส"],
    },
    "sensors": {
        "label": "เซนเซอร",
        "keywords": ["เซนเซอร", "sensor", "Heart Rate", "SpO2"],
    },
    "sports_modes": {
        "label": "โหมดกีฬา",
        "keywords": ["โหมดกีฬา", "sports modes", "โหมดกีฬา"],
    },
    "features": {
        "label": "ฟังก์ชัน",
        "keywords": ["ฟังก์ชัน", "features", "Flashlight", "calling", "music"],
    },
    "price_availability": {
        "label": "ราคา / ช่องทางจำหน่ายในประเทศไทย",
        "keywords": ["ราคา", "price", "จำหน่าย", "availability", "ช่องทาง"],
    },
}

ALLOWED_FIELD_IDS: tuple[str, ...] = tuple(FIELD_CATALOG.keys())


# Schema for OpenRouter /response_format
RESEARCH_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "competitor_research",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "target_model": {"type": "string", "maxLength": 120},
                "competitor_names": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 120},
                    "maxItems": 10,
                },
                "evidence": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "competitor": {"type": "string", "maxLength": 120},
                            "field": {
                                "type": "string",
                                "enum": list(ALLOWED_FIELD_IDS),
                                "description": f"canonical field ID; valid values: {', '.join(ALLOWED_FIELD_IDS)}",
                            },
                            "claim": {"type": "string", "maxLength": 400},
                            "url": {"type": "string", "maxLength": 500},
                            "geography": {
                                "type": "string",
                                "enum": ["thailand", "global"],
                            },
                        },
                        "required": ["competitor", "field", "claim", "url", "geography"],
                        "additionalProperties": False,
                    },
                },
                "recommendations": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {"type": "string", "maxLength": 400},
                },
                "uncertainty": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {"type": "string", "maxLength": 400},
                },
            },
            "required": ["target_model", "competitor_names", "evidence", "recommendations", "uncertainty"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class CompetitorEvidence:
    """One selected competitor fact."""
    competitor: str
    field: str
    claim: str
    url: str
    geography: str = "global"
    title: str = ""
    snippet: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CompetitorEvidence":
        return cls(
            competitor=d.get("competitor", ""),
            field=d.get("field", ""),
            claim=d.get("claim", ""),
            url=d.get("url", ""),
            geography=d.get("geography", "global"),
            title=d.get("title", ""),
            snippet=d.get("snippet", ""),
        )


@dataclass
class ResearchResponse:
    """Stage A output."""
    target_model: str
    competitor_names: list[str] = field(default_factory=list)
    evidence: list[CompetitorEvidence] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResearchResponse":
        return cls(
            target_model=d.get("target_model", ""),
            competitor_names=d.get("competitor_names", []),
            evidence=[CompetitorEvidence.from_dict(e) for e in d.get("evidence", [])],
            recommendations=d.get("recommendations", []),
            uncertainty=d.get("uncertainty", []),
        )

    @classmethod
    def from_json(cls, text: str) -> "ResearchResponse":
        return cls.from_dict(json.loads(text))


class EvidenceValidationError(Exception):
    """Raised when ResearchResponse cannot be rendered safely."""
    pass


class CompetitorReportRenderer:
    """Stage B: render a ResearchResponse into deterministic Markdown.

    All source metadata (title, content) is taken from the selected
    `relevant_annotations` map, not from the model-provided `evidence` object.
    """

    DEFAULT_FIELDS = ALLOWED_FIELD_IDS

    _NO_EVIDENCE = "*ไม่มีหลักฐานยืนยัน*"

    def __init__(self, research: ResearchResponse, relevant_annotations: list[dict] | None = None):
        self.research = research
        self.rel_map: dict[str, dict] = {}
        for a in relevant_annotations or []:
            key = (a.get("url") or "").lower().rstrip("/")
            if key:
                self.rel_map[key] = a

    def _competitor_names_lower(self) -> list[str]:
        return [n.lower() for n in self.research.competitor_names]

    @staticmethod
    def _esc_cell(text: str) -> str:
        """Escape Markdown table delimiters and newlines in a table cell."""
        return text.replace("|", "&#124;").replace("\n", "<br>")

    def _validate_evidence(self, ev: CompetitorEvidence) -> tuple[bool, str, dict | None]:
        """Return (ok, reason, selected_annotation) for an evidence record."""
        if ev.field not in ALLOWED_FIELD_IDS:
            return False, f"invalid field {ev.field!r}; allowed: {', '.join(ALLOWED_FIELD_IDS)}", None

        comp_lower = self._competitor_names_lower()
        if ev.competitor.lower() not in comp_lower:
            return False, f"competitor {ev.competitor!r} not in scope", None

        key = ev.url.lower().rstrip("/")
        a = self.rel_map.get(key)
        if not a:
            return False, f"URL not in selected evidence: {ev.url}", None

        rel = a.get("_relevance", {})

        # Market parity: let the model decide which sources to cite, but keep
        # a structural guardrail against sources that are clearly not evidence
        # (homepage, category_mismatch).  Other relevance types (competitor,
        # target, market_unverified, unknown) are left to the model's judgment.
        rel_type = rel.get("relevance_type", "")
        if rel_type in ("homepage", "category_mismatch"):
            return False, f"source is structurally not evidence ({rel_type}): {ev.url}", None

        # Factual check: the source text must actually mention the competitor.
        src_text = f"{a.get('url','')} {a.get('title','')} {a.get('content','')}".lower()
        if ev.competitor.lower() not in src_text:
            return False, f"source does not mention competitor {ev.competitor}: {ev.url}", None

        if ev.geography == "thailand" and rel.get("geography") != "thailand":
            return False, f"source is not Thailand-specific: {ev.url}", None

        return True, "", a

    def _index_validated(self) -> dict[str, dict[str, tuple[CompetitorEvidence, dict]]]:
        index: dict[str, dict[str, tuple[CompetitorEvidence, dict]]] = {}
        for ev in self.research.evidence:
            ok, reason, a = self._validate_evidence(ev)
            if not ok:
                continue
            index.setdefault(ev.field, {})[ev.competitor] = (ev, a)
        return index

    def validate(self) -> list[str]:
        """Return a list of evidence-to-annotation validation errors."""
        errors: list[str] = []
        for i, ev in enumerate(self.research.evidence):
            ok, reason, _ = self._validate_evidence(ev)
            if not ok:
                errors.append(f"evidence[{i}]: {reason}")
        return errors

    def _product_cells(self, product_spec: str, fields: tuple[str, ...]) -> dict[str, str]:
        """Parse product spec lines using canonical field keywords."""
        cells: dict[str, str] = {f: "-" for f in fields}
        for raw_line in product_spec.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            low = line.lower()
            for f in fields:
                for kw in sorted(FIELD_CATALOG[f]["keywords"], key=len, reverse=True):
                    klow = kw.lower()
                    if low.startswith(klow):
                        cells[f] = line[len(kw):].strip()
                        break
        return cells

    def _fields_to_render(self, product_spec: str) -> tuple[str, ...]:
        by_field = self._index_validated()
        # render only fields that have at least one validated competitor evidence
        selected = [f for f in self.DEFAULT_FIELDS if f in by_field]
        return tuple(selected)

    def render(self, product_spec: str) -> str:
        """Return deterministic Markdown report."""
        by_field = self._index_validated()
        fields = self._fields_to_render(product_spec)
        product_cells = self._product_cells(product_spec, fields)

        # show only competitors that have at least one validated evidence in the rendered fields
        competitors = [
            comp
            for comp in self.research.competitor_names
            if any(comp in by_field.get(f, {}) for f in fields)
        ]

        lines = [
            f"# รายงานวิเคราะห์เปรียบเทียบคู่แข่ง: {self.research.target_model}",
            "",
            "## ตารางเปรียบเทียบคุณสมบัติและสเปก",
            "",
        ]

        if not fields or not competitors:
            lines.append("ไม่พบข้อมูลคู่แข่งทีผ่านการตรวจสอบ")
            return "\n".join(lines)

        lines.extend([
            "| คุณสมบัติ | " + " | ".join([self.research.target_model] + competitors) + " |",
            "| :--- | " + " | ".join([":---"] * (len(competitors) + 1)) + " |",
        ])

        for f in fields:
            row = [f"**{FIELD_CATALOG[f]['label']}**", self._esc_cell(product_cells.get(f, "-"))]
            for comp in competitors:
                entry = by_field.get(f, {}).get(comp)
                if entry:
                    ev, a = entry
                    title = self._esc_cell((a.get("title") or ev.title or "แหล่งอ้างอิง").strip())
                    row.append(f"{self._esc_cell(ev.claim)} [{title}]({a.get('url', ev.url)})")
                else:
                    row.append(self._esc_cell(self._NO_EVIDENCE))
            lines.append("| " + " | ".join(row) + " |")

        if self.research.recommendations:
            lines.extend(["", "## ข้อเสนอแนะ", ""])
            for r in self.research.recommendations:
                lines.append(f"- {r}")

        if self.research.uncertainty:
            lines.extend(["", "## ข้อจำกัด", ""])
            for u in self.research.uncertainty:
                lines.append(f"- {u}")

        return "\n".join(lines)


def render_competitor_report(
    json_text: str,
    product_spec: str,
    relevant_annotations: list[dict],
) -> str:
    """Convenience: JSON -> ResearchResponse -> Markdown."""
    research = ResearchResponse.from_json(json_text)
    return CompetitorReportRenderer(research, relevant_annotations).render(product_spec)


EVIDENCE_SYSTEM_PROMPT = """คุณคือ Competitor Analyst

เป้าหมายของคุณคือสร้าง JSON ทีตรงกับ schema "competitor_research" เท่านั้น
Stage B (CompetitorReportRenderer) จะเป็นผู้สร้างรายงาน Markdown ภายหลัง
คุณไม่ต้องเขียน Markdown, ตาราง, รายงาน, หรือคำอธิบายใด ๆ นอก JSON

JSON ต้องมี key เหล่านี้เท่านั้น:
- target_model
- competitor_names
- evidence
- recommendations
- uncertainty

กฎการเลือก evidence:
- เลือก evidence จาก URL ทีได้จาก web search เท่านั้น
- ห้าม invent URL
- ห้ามเดา factual claim
- ห้ามเติมข้อมูลเพื่อให้ output ดูสมบูรณ์
- ถ้าไม่มีหลักฐานสำหรับ field ใด field หนึ่ง ให้ข้าม field นั้น (ไม่ใส่ evidence) และบอกไว้ใน uncertainty
- ถ้าไม่พบหลักฐานใด ๆ ให้คืน evidence: [] และอธิบายสาเหตุใน uncertainty
- recommendation คือข้อเสนอแนะเท่านั้น ห้ามใส่ facts หรือ URL
- uncertainty คือสิ่งที่ยังไม่พบหลักฐาน ห้ามคาดการณ์

field catalog (ใช้ field ID เหล่านี้เท่านั้น ห้ามตั้งชื่อใหม่):
- "display" → หน้าจอ
- "chipset_os" → ชิปเซ็ต / OS
- "battery" → แบตเตอรี่
- "connectivity" → การเชื่อมต่อ
- "water_resistance" → กันน้ำ
- "gps" → GPS
- "sensors" → เซนเซอร
- "sports_modes" → โหมดกีฬา
- "features" → ฟังก์ชัน
- "price_availability" → ราคา / ช่องทางจำหน่ายในประเทศไทย

กฎการเลือก evidence:
- สูงสุด 6 evidence เท่านั้น
- เลือกอันทีมีประโยชน์ต่อการตัดสินใจมากที่สุด โดยเฉพาะ display, battery, price_availability, features, connectivity
- ให้ความสำคัญกับ evidence ทีมี geography: thailand
- ไม่ต้องสร้าง evidence ครบทุก field หรือทุก competitor
- แต่ละ claim ต้องกระชับ ไม่เกิน 400 ตัวอักษร
- recommendations สูงสุด 3 รายการ ไม่ใส่ facts หรือ URL
- uncertainty สูงสุด 3 รายการ

คุณจะได้รับ manifest ของ URL ทีผ่าน relevance gate ใน user prompt ให้เลือก URL จาก manifest นั้นเท่านั้น
หาก URL ไม่อยู่ใน manifest ห้ามใช้

ห้ามครอบ JSON ด้วย Markdown code fence (```json) หรือคำอธิบายนำหน้า/ท้าย
"""
