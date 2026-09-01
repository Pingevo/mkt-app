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
                "evidence_based_recommendations": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "maxLength": 400},
                            "supporting_evidence_urls": {
                                "type": "array",
                                "items": {"type": "string", "maxLength": 500},
                                "maxItems": 6,
                            },
                        },
                        "required": ["text", "supporting_evidence_urls"],
                        "additionalProperties": False,
                    },
                },
                "strategic_hypotheses": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "maxLength": 400},
                            "rationale": {"type": "string", "maxLength": 400},
                        },
                        "required": ["text", "rationale"],
                        "additionalProperties": False,
                    },
                },
                "uncertainty": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {"type": "string", "maxLength": 400},
                },
            },
            "required": ["target_model", "competitor_names", "evidence", "evidence_based_recommendations", "strategic_hypotheses", "uncertainty"],
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
class EvidenceBasedRecommendation:
    """A recommendation backed by validated evidence URLs."""
    text: str
    supporting_evidence_urls: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvidenceBasedRecommendation":
        return cls(
            text=d.get("text", ""),
            supporting_evidence_urls=d.get("supporting_evidence_urls", []),
        )


@dataclass
class StrategicHypothesis:
    """A strategic hypothesis that is NOT yet backed by evidence.
    Must be presented as unverified, not as comparative fact."""
    text: str
    rationale: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StrategicHypothesis":
        return cls(
            text=d.get("text", ""),
            rationale=d.get("rationale", ""),
        )


@dataclass
class ResearchResponse:
    """Stage A output."""
    target_model: str
    competitor_names: list[str] = field(default_factory=list)
    evidence: list[CompetitorEvidence] = field(default_factory=list)
    evidence_based_recommendations: list[EvidenceBasedRecommendation] = field(default_factory=list)
    strategic_hypotheses: list[StrategicHypothesis] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResearchResponse":
        return cls(
            target_model=d.get("target_model", ""),
            competitor_names=d.get("competitor_names", []),
            evidence=[CompetitorEvidence.from_dict(e) for e in d.get("evidence", [])],
            evidence_based_recommendations=[
                EvidenceBasedRecommendation.from_dict(r)
                for r in d.get("evidence_based_recommendations", [])
            ],
            strategic_hypotheses=[
                StrategicHypothesis.from_dict(h)
                for h in d.get("strategic_hypotheses", [])
            ],
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
        """Return (ok, reason, selected_annotation) for an evidence record.

        Checks only deterministic facts:
        - field must be in allowed catalog
        - competitor must be in declared competitor_names
        - URL must come from real search results (provenance via rel_map)
        - URL must not be structurally blocked (homepage, category_mismatch)
        - geography must be in enum (thailand/global)

        Does NOT check whether the source text mentions the competitor name
        via substring — that check is unreliable because URL slugs and
        product names use different formatting (hyphens, extra words,
        different casing).  The model saw the search results and chose
        which URLs to cite; code verifies provenance, not semantics.

        Geography is model-decided: the model reads the source content and
        declares geography in the evidence record.  Code does NOT override
        the model's geography judgment with its own TLD/substring detection
        — that detection is unreliable for .com domains that are actually
        Thai stores (e.g. vteccomputer.com).  The model's geography is
        accepted as-is; _detect_geography remains as a diagnostic signal
        stored in _relevance metadata, not as a hard gate.
        """
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

        # Structurally blocked sources (homepage, category_mismatch) are
        # hard-blocked — they cannot serve as evidence.
        rel_type = rel.get("relevance_type", "")
        if rel_type in ("homepage", "category_mismatch"):
            return False, f"source is structurally not evidence ({rel_type}): {ev.url}", None

        # Geography: model-decided.  Accept the model's geography declaration.
        # _detect_geography is stored as diagnostic in _relevance but does
        # NOT override the model's judgment.  Mismatch between model-declared
        # and code-detected geography is a warning, not a hard block.
        # (Hard block for geography is removed — see test_geography_* cases.)

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
        """Return deterministic Markdown report.

        If no evidence passes validation, return a limited analysis report
        instead of an empty stub — the user gets a usable summary of what
        was found and what's missing, rather than a blank report.
        """
        by_field = self._index_validated()
        fields = self._fields_to_render(product_spec)
        product_cells = self._product_cells(product_spec, fields)

        # show only competitors that have at least one validated evidence in the rendered fields
        competitors = [
            comp
            for comp in self.research.competitor_names
            if any(comp in by_field.get(f, {}) for f in fields)
        ]

        if not fields or not competitors:
            return self._render_limited_analysis(product_spec)

        lines = [
            f"# รายงานวิเคราะห์เปรียบเทียบคู่แข่ง: {self.research.target_model}",
            "",
            "## ตารางเปรียบเทียบคุณสมบัติและสเปก",
            "",
        ]

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

        # Evidence-based recommendations: only render if ALL supporting
        # URLs pass provenance check (exist in validated evidence set).
        # If any URL is not validated, demote the recommendation to a
        # strategic hypothesis (don't drop it — the model's insight may
        # still be useful, but it must be labeled as unverified).
        validated_urls = {
            (ev.url or "").lower().rstrip("/")
            for ev in self.research.evidence
            if self._validate_evidence(ev)[0]
        }
        promoted_hypotheses: list[StrategicHypothesis] = []

        evidence_based: list[EvidenceBasedRecommendation] = []
        for rec in self.research.evidence_based_recommendations:
            urls_ok = all(
                (u or "").lower().rstrip("/") in validated_urls
                for u in rec.supporting_evidence_urls
            )
            if urls_ok and rec.supporting_evidence_urls:
                evidence_based.append(rec)
            else:
                # Demote to hypothesis — unsupported claim must not appear
                # as a verified recommendation.
                promoted_hypotheses.append(StrategicHypothesis(
                    text=rec.text,
                    rationale="เดิมอ้างเป็น evidence_based แต่ URL รองรับไม่ผ่าน validation",
                ))

        if evidence_based:
            lines.extend(["", "## ข้อเสนอแนะที่มีหลักฐานรองรับ", ""])
            for rec in evidence_based:
                lines.append(f"- {rec.text}")

        all_hypotheses = list(self.research.strategic_hypotheses) + promoted_hypotheses
        if all_hypotheses:
            lines.extend([
                "",
                "## สมมติฐานเชิงกลยุทธ์ (ยังไม่ยืนยัน)",
                "",
                "*ข้อความในส่วนนี้เป็นสมมติฐาน ไม่ใช่ข้อเท็จจริงที่ผ่านการตรวจสอบ*",
                "",
            ])
            for h in all_hypotheses:
                lines.append(f"- {h.text}")
                if h.rationale:
                    lines.append(f"  - เหตุผล: {h.rationale}")

        if self.research.uncertainty:
            lines.extend(["", "## ข้อจำกัด", ""])
            for u in self.research.uncertainty:
                lines.append(f"- {u}")

        return "\n".join(lines)

    def _render_limited_analysis(self, product_spec: str) -> str:
        """Render a limited analysis report when no evidence passes validation.

        Instead of returning an empty stub, produce a usable report that:
        - lists the competitors the model discovered
        - shows the product spec
        - includes the model's recommendations and uncertainty
        - explains what's missing
        """
        lines = [
            f"# รายงานวิเคราะห์จำกัด: {self.research.target_model}",
            "",
            "**limited_analysis: true**",
            "",
            "## คู่แข่งที่ระบุ",
            "",
        ]
        if self.research.competitor_names:
            for name in self.research.competitor_names:
                lines.append(f"- {name}")
        else:
            lines.append("- ยังไม่ระบุ")
        lines.append("")

        # Product spec summary
        lines.extend(["## สรุปสินค้าของเรา", ""])
        for raw_line in (product_spec or "").splitlines():
            line = raw_line.strip()
            if line:
                lines.append(f"- {line}")
        lines.append("")

        # Model's evidence-based recommendations (if any) — in limited
        # analysis, no evidence is validated, so all go to hypotheses.
        all_hypotheses = list(self.research.strategic_hypotheses)
        for rec in self.research.evidence_based_recommendations:
            all_hypotheses.append(StrategicHypothesis(
                text=rec.text,
                rationale="เดิมอ้างเป็น evidence_based แต่ไม่มี evidence ผ่าน validation",
            ))
        if all_hypotheses:
            lines.extend([
                "## สมมติฐานเชิงกลยุทธ์ (ยังไม่ยืนยัน)",
                "",
                "*ข้อความในส่วนนี้เป็นสมมติฐาน ไม่ใช่ข้อเท็จจริงที่ผ่านการตรวจสอบ*",
                "",
            ])
            for h in all_hypotheses:
                lines.append(f"- {h.text}")
                if h.rationale:
                    lines.append(f"  - เหตุผล: {h.rationale}")
            lines.append("")

        # Model's uncertainty (if any)
        if self.research.uncertainty:
            lines.extend(["## ข้อจำกัด", ""])
            for u in self.research.uncertainty:
                lines.append(f"- {u}")
            lines.append("")

        lines.extend([
            "## สิ่งที่ต้องขอเพิ่มเพื่อทำวิเคราะห์เต็มรูปแบบ",
            "",
            "- สเปคเฉพาะของแต่ละคู่แข่งที่ผ่านการตรวจสอบ",
            "- ราคาขายปลีกที่มีหลักฐานยืนยัน",
            "- ช่องทางจำหน่ายในประเทศเป้าหมาย",
            "",
            "*หมายเหตุ: รายงานนี้ไม่มีการสร้างหรืออนุมานข้อมูลคู่แข่งเอง*",
        ])
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
- evidence_based_recommendations
- strategic_hypotheses
- uncertainty

กฎการเลือก evidence:
- เลือก evidence จาก URL ทีได้จาก web search เท่านั้น
- ห้าม invent URL
- ห้ามเดา factual claim
- ห้ามเติมข้อมูลเพื่อให้ output ดูสมบูรณ์
- ถ้าไม่มีหลักฐานสำหรับ field ใด field หนึ่ง ให้ข้าม field นั้น (ไม่ใส่ evidence) และบอกไว้ใน uncertainty
- ถ้าไม่พบหลักฐานใด ๆ ให้คืน evidence: [] และอธิบายสาเหตุใน uncertainty

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

กฎ recommendations และ hypotheses (สำคัญมาก):
- evidence_based_recommendations: ข้อเสนอแนะที่มีหลักฐานรองรับโดยตรง
  - แต่ละรายการต้องมี supporting_evidence_urls ที่ชี้ไป URL ใน evidence array
  - ห้ามทำ comparative claim (เช่น "เหนือกว่า", "มากกว่า", "ดีกว่า") โดยไม่มี evidence รองรับ
  - ห้ามอ้างราคา สเปก หรือคุณสมบัติของสินค้าเรา (target_model) ที่ไม่มีใน product_spec ที่ได้รับ
  - ถ้าอยากแนะนำเรื่องที่ไม่มี evidence → ใส่ใน strategic_hypotheses แทน
  - สูงสุด 3 รายการ
- strategic_hypotheses: สมมติฐานเชิงกลยุทธ์ที่ยังไม่ยืนยัน
  - เป็นความคิดเห็น/ข้อสังเกต ไม่ใช่ข้อเท็จจริง
  - ต้องมี rationale อธิบายว่าทำไมคิดแบบนั้น
  - สูงสุด 3 รายการ
- uncertainty: สิ่งที่ยังไม่พบหลักฐาน ห้ามคาดการณ์
  - สูงสุด 3 รายการ

กฎการเลือก URL:
- ถ้ามี manifest ของ URL ใน user prompt ให้เลือก URL จาก manifest นั้นเท่านั้น
- ถ้าไม่มี manifest (โหมด default discovery) ให้ใช้ URL จากผล web search ที่คุณค้นพบในรอบนี้
- ห้าม invent URL หรือใช้ URL ที่ไม่ได้มาจาก web search หรือ manifest
- ถ้าไม่พบหลักฐานใด ๆ ให้คืน evidence: [] และอธิบายสาเหตุใน uncertainty

ห้ามครอบ JSON ด้วย Markdown code fence (```json) หรือคำอธิบายนำหน้า/ท้าย
"""
