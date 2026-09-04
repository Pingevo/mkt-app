"""Two-stage evidence contract for Competitor Analysis.

Stage A: the LLM returns a ResearchResponse (JSON) of selected evidence.
Stage B: CompetitorReportRenderer turns that into deterministic Markdown.

The evidence schema is open: the model returns field labels it actually found
in the source data. There is no fixed field catalog — the engine works for
any product domain (smartwatch, restaurant, apparel, SaaS, etc.) without
category-specific configuration.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


# Schema for OpenRouter /response_format
# The "field" property is an open string — the model returns whatever
# property label is relevant to the product being analyzed.
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
                                "maxLength": 120,
                                "description": "property/feature label relevant to this product (e.g. price, display, menu, material, plan tier — whatever the source data mentions)",
                            },
                            "claim": {"type": "string", "maxLength": 400},
                            "url": {"type": "string", "maxLength": 500},
                            "geography": {
                                "type": "string",
                                "maxLength": 120,
                                "description": "market/region from source (e.g. thailand, japan, eu, global — open string)",
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

    _NO_EVIDENCE = "*ไม่มีหลักฐานยืนยัน*"
    _NO_MATCH = "*ไม่พบค่าที่จับคู่ได้จากข้อมูลต้นทาง*"

    @staticmethod
    def _canonical_field(field: str) -> str:
        """Normalize a field label for grouping: casefold, trim, collapse
        whitespace and punctuation. This prevents duplicate rows from
        casing/spacing variants (e.g. 'Price', 'price', ' price ').
        """
        import unicodedata
        s = field.strip().casefold()
        s = unicodedata.normalize("NFKC", s)
        s = re.sub(r"[\s\-_:：]+", " ", s).strip()
        return s

    @staticmethod
    def _canonical_identity(name: str) -> str:
        """Normalize a product/competitor identity for matching: casefold,
        trim, NFKC normalize, collapse punctuation/URL slug separators/
        whitespace to single spaces. Mirrors CompetitorAnalysisAgent.
        _canonical_identity so the renderer's identity check is consistent
        with the agent's assessment.
        """
        import unicodedata
        s = (name or "").strip().casefold()
        s = unicodedata.normalize("NFKC", s)
        s = re.sub(r"[\s\-_+/\\|.,;:!?\"'`()\[\]{}<>@#$%^&*=~]+", " ", s).strip()
        s = re.sub(r"\s+", " ", s)
        return s

    def __init__(
        self,
        research: ResearchResponse,
        relevant_annotations: list[dict] | None = None,
        quick_brief: str = "",
    ):
        self.research = research
        self.quick_brief = (quick_brief or "").lower().strip()
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
        - field must be a non-empty string (open schema — any property label)
        - competitor must be in declared competitor_names
        - URL must come from verified annotations (relevant=True) only
        - annotation must be a competitor match (relevance_type == "competitor")
        - annotation's matched_competitor must match ev.competitor after
          canonical normalization (prevents cross-competitor attribution)
        - URL must not be structurally blocked (homepage)
        - geography must be a non-empty string (open — no enum)

        Provenance: the URL must be in the verified annotation set
        (relevant=True).  Candidate annotations (relevant=None) are NOT
        accepted as validated evidence.  Target-matched annotations
        (relevance_type == "target") are NOT accepted as competitor
        evidence — a source about the target product cannot validate a
        claim about a competitor.
        """
        if not ev.field or not ev.field.strip():
            return False, "evidence field is empty", None

        comp_lower = self._competitor_names_lower()
        if ev.competitor.lower() not in comp_lower:
            return False, f"competitor {ev.competitor!r} not in scope", None

        if not ev.geography or not str(ev.geography).strip():
            return False, "evidence geography is empty", None

        key = ev.url.lower().rstrip("/")
        a = self.rel_map.get(key)
        if not a:
            return False, f"URL not in verified evidence: {ev.url}", None

        rel = a.get("_relevance", {})

        # Structurally blocked sources (homepage) are hard-blocked.
        rel_type = rel.get("relevance_type", "")
        if rel_type in ("homepage", "category_mismatch"):
            return False, f"source is structurally not evidence ({rel_type}): {ev.url}", None

        # Only verified sources (relevant=True) can be validated evidence.
        if rel.get("relevant") is not True:
            return False, f"source is not verified (relevant={rel.get('relevant')}): {ev.url}", None

        # Source must be a competitor match — target-matched sources cannot
        # validate competitor evidence (prevents target→competitor attribution).
        if rel_type != "competitor":
            return False, f"source is not a competitor match (relevance_type={rel_type}): {ev.url}", None

        # The annotation's matched_competitor must match ev.competitor after
        # canonical normalization — prevents cross-competitor attribution
        # (e.g. source about Competitor B validating a claim about Competitor A).
        # For backward compatibility with annotations created before this
        # metadata existed, infer the match from the source text using the
        # same canonical identity + word-boundary matching as
        # _assess_source_relevance (no new heuristics).
        matched_comp = rel.get("matched_competitor", "")
        ev_canon = self._canonical_identity(ev.competitor)
        if matched_comp:
            matched_canon = self._canonical_identity(matched_comp)
            if matched_canon != ev_canon:
                return False, (
                    f"cross-competitor attribution: source matches {matched_comp!r} "
                    f"but evidence claims {ev.competitor!r}: {ev.url}"
                ), None
        else:
            # Backward compat: infer from source text. ev.competitor's
            # canonical identity must appear as a word-boundary match in
            # the normalized URL/title/content.
            source_text = " ".join([
                a.get("url", ""), a.get("title", ""), a.get("content", ""),
            ])
            source_canon = self._canonical_identity(source_text)
            if not (ev_canon and re.search(r"\b" + re.escape(ev_canon) + r"\b", source_canon)):
                return False, (
                    f"source does not match competitor {ev.competitor!r} "
                    f"(no matched_competitor metadata and no identity match in source): {ev.url}"
                ), None

        return True, "", a

    def _index_validated(self) -> dict[str, dict[str, tuple[CompetitorEvidence, dict]]]:
        """Index validated evidence by canonical field, then by competitor.
        Fields with different casing/spacing but same canonical form are
        grouped together (e.g. 'Price' and 'price' → same row).
        """
        index: dict[str, dict[str, tuple[CompetitorEvidence, dict]]] = {}
        for ev in self.research.evidence:
            ok, reason, a = self._validate_evidence(ev)
            if not ok:
                continue
            canon = self._canonical_field(ev.field)
            # Use the first-seen field label as display name for this canonical group
            if canon not in index:
                index[canon] = {"_display": ev.field}
            index[canon][ev.competitor] = (ev, a)
        return index

    def validate(self) -> list[str]:
        """Return a list of evidence-to-annotation validation errors."""
        errors: list[str] = []
        for i, ev in enumerate(self.research.evidence):
            ok, reason, _ = self._validate_evidence(ev)
            if not ok:
                errors.append(f"evidence[{i}]: {reason}")
        return errors

    def _product_cells(self, product_spec: str, fields: tuple[tuple[str, str], ...]) -> dict[str, str]:
        """Parse product spec lines using field labels from validated evidence.

        For each field, check if any product spec line starts with
        that label (case-insensitive, canonicalized). This is a best-effort
        match — if no line matches, the cell shows a clear message indicating
        no match was found, NOT '-' (which could be misread as "product has
        no data").
        """
        cells: dict[str, str] = {}
        for canon, display in fields:
            cells[canon] = self._NO_MATCH
            for raw_line in product_spec.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                line_canon = self._canonical_field(line.split(":")[0] if ":" in line else line.split("：")[0] if "：" in line else line)
                if line_canon == canon or line_canon.startswith(canon):
                    # Extract the value after the label
                    for sep in ("：", ":"):
                        if sep in line:
                            cells[canon] = line.split(sep, 1)[1].strip()
                            break
                    else:
                        cells[canon] = line
                    break
        return cells

    def _fields_to_render(self, product_spec: str) -> tuple[tuple[str, str], ...]:
        """Return ((canonical_field, display_label), ...) for fields with
        at least one validated competitor evidence. Preserve insertion order.
        """
        by_field = self._index_validated()
        seen: list[tuple[str, str]] = []
        for ev in self.research.evidence:
            canon = self._canonical_field(ev.field)
            if canon in by_field and canon not in [c for c, _ in seen]:
                seen.append((canon, ev.field))
        return tuple(seen)

    def _classify_recommendations(
        self,
    ) -> tuple[list[EvidenceBasedRecommendation], list[StrategicHypothesis]]:
        """Split recommendations into verified and hypothesis, same as table view."""
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
                promoted_hypotheses.append(StrategicHypothesis(
                    text=rec.text,
                    rationale="เดิมอ้างเป็น evidence_based แต่ URL รองรับไม่ผ่าน validation",
                ))

        all_hypotheses = list(self.research.strategic_hypotheses) + promoted_hypotheses
        return evidence_based, all_hypotheses

    def _wants_brief(self) -> bool:
        """Detect explicit non-table deliverable requests from the Quick Brief."""
        text = self.quick_brief
        brief_signals = [
            "bullet",
            "executive brief",
            "ห้ามใช้ตาราง",
            "ไม่ใช้ตาราง",
            "ไม่ตาราง",
            "ห้ามตาราง",
        ]
        return any(s in text for s in brief_signals)

    def _render_brief(self, product_spec: str) -> str:
        """Render a bullet executive brief from the same validated evidence.

        No new facts are introduced; URLs and provenance are preserved.
        Hypotheses and uncertainty are labelled the same way as the table view.
        """
        by_field = self._index_validated()
        fields = self._fields_to_render(product_spec)
        product_cells = self._product_cells(product_spec, fields)

        competitors = [
            comp
            for comp in self.research.competitor_names
            if any(comp in by_field.get(canon, {}) for canon, _ in fields)
        ]

        if not fields or not competitors:
            return self._render_limited_analysis(product_spec)

        lines = [
            f"# Executive Brief: {self.research.target_model}",
            "",
            "## คู่แข่งที่วิเคราะห์",
            "",
        ]
        for comp in competitors:
            lines.append(f"- {comp}")

        # product spec summary
        lines.extend(["", "## สินค้าของเรา", ""])
        for canon, display in fields:
            value = product_cells.get(canon, self._NO_MATCH)
            lines.append(f"- **{display}:** {value}")

        for comp in competitors:
            lines.extend(["", f"## {comp}", ""])
            for canon, display in fields:
                entry = by_field.get(canon, {}).get(comp)
                if entry:
                    ev, a = entry
                    title = (a.get("title") or ev.title or "แหล่งอ้างอิง").strip()
                    lines.append(
                        f"- **{display}:** {ev.claim} "
                        f"[{title}]({a.get('url', ev.url)})"
                    )
                else:
                    lines.append(f"- **{display}:** {self._NO_EVIDENCE}")

        # Evidence-based recommendations: same classification as table view
        evidence_based, all_hypotheses = self._classify_recommendations()

        if evidence_based:
            lines.extend(["", "## ข้อเสนอแนะที่มีหลักฐานรองรับ", ""])
            for rec in evidence_based:
                lines.append(f"- {rec.text}")
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

    def render(self, product_spec: str) -> str:
        """Return deterministic Markdown report.

        If the Quick Brief asks for a non-table deliverable, return a bullet
        executive brief instead.  Presentation changes; evidence and guardrails
        stay the same.

        If no evidence passes validation, return a limited analysis report
        instead of an empty stub — the user gets a usable summary of what
        was found and what's missing, rather than a blank report.
        """
        if self._wants_brief():
            return self._render_brief(product_spec)

        by_field = self._index_validated()
        fields = self._fields_to_render(product_spec)
        product_cells = self._product_cells(product_spec, fields)

        # show only competitors that have at least one validated evidence in the rendered fields
        competitors = [
            comp
            for comp in self.research.competitor_names
            if any(comp in by_field.get(canon, {}) for canon, _ in fields)
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

        for canon, display in fields:
            row = [f"**{display}**", self._esc_cell(product_cells.get(canon, self._NO_MATCH))]
            for comp in competitors:
                entry = by_field.get(canon, {}).get(comp)
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
        evidence_based, all_hypotheses = self._classify_recommendations()

        if evidence_based:
            lines.extend(["", "## ข้อเสนอแนะที่มีหลักฐานรองรับ", ""])
            for rec in evidence_based:
                lines.append(f"- {rec.text}")
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

field (เลือกจากข้อมูลจริงทีพบ):
- field คือชื่อคุณสมบัติทีเกี่ยวข้องกับสินค้าทีกำลังวิเคราะห์
- เลือกจากข้อมูลสินค้าและคำขอของผู้ใช้ — ไม่มีรายการ fixed
- ตัวอย่างเช่น price, menu, material, plan tier, display, battery — ขึ้นกับสินค้าจริง
- ห้ามตั้งชื่อ field ทีไม่มีในข้อมูลจริง
- ใช้ field label เดียวกันสำหรับคุณสมบัติเดียวกันข้าม competitors (เช่น ใช้ "price" ทั้งคู่ ไม่ใช่ "price" กับ "Price")

กฎการเลือก evidence:
- สูงสุด 6 evidence เท่านั้น
- เลือกอันทีมีประโยชน์ต่อการตัดสินใจมากที่สุด
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
