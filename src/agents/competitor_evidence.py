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
class StrategicImplication:
    """A brand-aware strategic implication derived from finalized evidence
    + optional runtime brand context. This is inference/recommendation,
    NOT evidence. It is produced by the separate BrandInterpretationPass
    AFTER the SemanticEvidenceReviewer has finalized evidence.

    evidence_ref links this implication to a specific surviving evidence
    record. Implications referencing non-surviving evidence are
    mechanically rejected.
    """
    evidence_ref: int
    implication: str
    category: str = "recommendation"  # recommendation | inference

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StrategicImplication":
        return cls(
            evidence_ref=d.get("evidence_ref", -1),
            implication=d.get("implication", ""),
            category=d.get("category", "recommendation"),
        )


@dataclass
class ResearchResponse:
    """Stage A output + optional strategic implications from the
    post-review Brand Interpretation pass.

    strategic_implications is additive (default empty) and does NOT
    appear in RESEARCH_RESPONSE_SCHEMA — it is populated only after
    the reviewer finalizes evidence and the BrandInterpretationPass runs.
    """
    target_model: str
    competitor_names: list[str] = field(default_factory=list)
    evidence: list[CompetitorEvidence] = field(default_factory=list)
    evidence_based_recommendations: list[EvidenceBasedRecommendation] = field(default_factory=list)
    strategic_hypotheses: list[StrategicHypothesis] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    strategic_implications: list[StrategicImplication] = field(default_factory=list)

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
            strategic_implications=[
                StrategicImplication.from_dict(i)
                for i in d.get("strategic_implications", [])
            ],
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
        evidence_based: list[EvidenceBasedRecommendation] = []

        for rec in self.research.evidence_based_recommendations:
            urls_ok = all(
                (u or "").lower().rstrip("/") in validated_urls
                for u in rec.supporting_evidence_urls
            )
            if urls_ok and rec.supporting_evidence_urls:
                evidence_based.append(rec)
            # else: drop — do NOT promote to strategic_hypotheses.
            # A recommendation authored as evidence_based contains factual
            # premises (prices, specs, offers).  Promoting it verbatim to
            # "hypothesis" re-labels it but preserves the unsupported factual
            # claims, which then reach the user as if they were strategic
            # reasoning.  The model would need to re-author it as a true
            # hypothesis (without the factual premise) for it to be valid.
            # Fail-closed: drop and let uncertainty capture the gap.

        all_hypotheses = list(self.research.strategic_hypotheses)
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

        # Strategic implications — rendered as a separate section.
        # These are inference/recommendation from the post-review Brand
        # Interpretation pass, NOT evidence. Raw brand_reference is
        # never dumped here — only structured implications.
        if self.research.strategic_implications:
            lines.extend([
                "",
                "## นัยยะเชิงกลยุทธ์ตามแบรนด์ (Strategic Implications)",
                "",
                "*ข้อความในส่วนนี้เป็น inference/recommendation ไม่ใช่ evidence*",
                "",
            ])
            for imp in self.research.strategic_implications:
                lines.append(f"- {imp.implication}")

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

        # Model's evidence-based recommendations — in limited analysis, no
        # evidence is validated, so none survive.  Do NOT promote them to
        # strategic_hypotheses: they were authored as evidence_based and
        # contain factual premises (prices, specs, offers) that would leak
        # as if they were strategic reasoning.  Drop fail-closed.
        all_hypotheses = list(self.research.strategic_hypotheses)
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

        # Strategic implications — also rendered in limited analysis.
        if self.research.strategic_implications:
            lines.extend([
                "",
                "## นัยยะเชิงกลยุทธ์ตามแบรนด์ (Strategic Implications)",
                "",
                "*ข้อความในส่วนนี้เป็น inference/recommendation ไม่ใช่ evidence*",
                "",
            ])
            for imp in self.research.strategic_implications:
                lines.append(f"- {imp.implication}")

        return "\n".join(lines)


class SemanticEvidenceReviewer:
    """Model-level semantic entailment review for competitor evidence.

    Sits between deterministic provenance validation (CompetitorReportRenderer.
    _validate_evidence) and the renderer.  Provenance checks that the URL is
    verified and the competitor identity matches.  This class checks that the
    *claim text* is semantically entailed by the source content.

    Architecture (one pass, no retry, no web calls):
        ResearchResponse + relevant_annotations
        → build review prompt (claim + source content per evidence)
        → single LLM call (no tools, no web_search/web_fetch)
        → parse JSON response (list of {index, action, claim?})
        → apply rewrites/removes to a new ResearchResponse
        → return

    If the LLM call fails or returns invalid JSON, the original research is
    returned unchanged (graceful degradation — provenance validation already
    ran, so the output is not unsafe, just not semantically verified).

    Cost: exactly one LLM call per competitor_analysis run (when evidence
    is non-empty).  Zero calls when evidence is empty.
    """

    _REVIEW_SYSTEM_PROMPT = """คุณคือผู้ตรวจสอบ semantic entailment ของ evidence claims

หน้าที่ของคุณคือตรวจแต่ละ evidence claim เทียบกับเนื้อหา source ที่ให้มา
และตัดสินใจว่า claim นั้นถูกสนับสนุนโดยตรงหรือไม่

กฎการตรวจ:
1. อ่าน source content ของแต่ละ evidence อย่างละเอียด
2. เทียบ claim กับ source — ถ้า source พูดเฉพาะ A ห้ามขยายเป็น A+B
3. ตรวจสิ่งที่เกิน source:
   - capability ที่ source ไม่ได้ระบุ (เช่น "video call" เมื่อ source บอกแค่ "ถ่ายภาพ")
   - ranking/superlative ที่ source ไม่ได้ระบุ (เช่น "ถูกที่สุดในตลาด")
   - performance/conclusion ที่ source ไม่ได้สรุป
   - causality ที่ source ไม่ได้อ้าง
4. ถ้า claim ถูกสนับสนุนโดยตรง → action: "keep"
5. ถ้า claim เกิน source เล็กน้อย → action: "rewrite" พร้อม claim ใหม่ที่กระชับเป็นเฉพาะสิ่งที่ source ระบุ
6. ถ้า claim ไม่มีอะไรใน source รองรับเลย → action: "remove"

ห้าม:
- ห้ามเพิ่มข้อมูลใหม่ที่ไม่มีใน source
- ห้ามใช้คำตัดสินที่ source ไม่ได้พูด (เช่น "แม่นยำ", "เรียลไทม์", "อัจฉริยะ")
- ห้ามเปลี่ยนแปลง strategic_hypotheses — ส่วนนั้นเป็น unverified by design

ส่งกลับ JSON array เท่านั้น แต่ละ element มี:
- index: ลำดับ evidence (เริ่มที่ 0)
- action: "keep" | "rewrite" | "remove"
- claim: (เฉพาะ action="rewrite") claim ใหม่ที่กระชับ

ห้ามครอบ JSON ด้วย markdown code fence
"""

    def __init__(self, llm, config: dict | None = None):
        self.llm = llm
        self.config = config or {}

    def review(
        self,
        research: ResearchResponse,
        relevant_annotations: list[dict] | None = None,
    ) -> ResearchResponse | None:
        """Review evidence claims for semantic entailment.

        Returns a new ResearchResponse with potentially rewritten or removed
        evidence.  strategic_hypotheses, evidence_based_recommendations, and
        uncertainty are passed through unchanged.

        If evidence is empty, no LLM call is made and the original research
        is returned (no claims to verify).

        Fail-closed contract: if the LLM call fails or returns invalid JSON,
        returns None — signaling the caller that unchecked evidence claims
        must NOT reach the renderer.  The caller is responsible for using a
        deterministic limited fallback that contains no unchecked
        model-generated factual claims.
        """
        if not research.evidence:
            return research

        # Build source map: URL → content
        source_map: dict[str, str] = {}
        for a in relevant_annotations or []:
            key = (a.get("url") or "").lower().rstrip("/")
            if key:
                source_map[key] = a.get("content", "")

        # Build review prompt
        evidence_lines: list[str] = []
        for i, ev in enumerate(research.evidence):
            key = (ev.url or "").lower().rstrip("/")
            source_content = source_map.get(key, "(ไม่พบ source content สำหรับ URL นี้)")
            evidence_lines.append(
                f"--- Evidence [{i}] ---\n"
                f"competitor: {ev.competitor}\n"
                f"field: {ev.field}\n"
                f"claim: {ev.claim}\n"
                f"source URL: {ev.url}\n"
                f"source content: {source_content}\n"
                f"--- สิ้นสุด Evidence [{i}] ---\n"
            )

        user_msg = (
            f"ตรวจสอบ {len(research.evidence)} evidence claims ต่อไปนี้ "
            f"เทียบกับ source content ของแต่ละ claim:\n\n"
            + "\n".join(evidence_lines)
            + "\nส่งกลับ JSON array ของผลการตรวจ (index, action, claim ถ้า rewrite)"
        )

        messages = [
            {"role": "system", "content": self._REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        try:
            response = self.llm.chat(
                messages,
                model=self.config.get("model"),
                temperature=0.1,
                max_tokens=self.config.get("max_tokens", 4096),
                max_retry_limit=0,  # no retry — one pass only
                stream=False,
                tools=None,  # no web tools — no web calls
                source="competitor_analysis.semantic_review",
            )
        except Exception:
            # Fail-closed: signal that unchecked claims must not reach renderer
            return None

        if not response or not response.strip():
            return None

        # Truncation guard — if the provider cut the response mid-generation
        # (finish_reason=length), the JSON is likely malformed.  Return None
        # (fail-closed) explicitly rather than relying on json.loads to fail.
        # This is a deterministic check on provider metadata.
        if getattr(self.llm, "last_truncated", False):
            return None

        # Parse JSON response
        try:
            clean = response.strip()
            # Strip markdown fence if present
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*\n?", "", clean)
                clean = re.sub(r"\n?```\s*$", "", clean)
            decisions = json.loads(clean)
        except (json.JSONDecodeError, ValueError):
            # Fail-closed: signal that unchecked claims must not reach renderer
            return None

        if not isinstance(decisions, list):
            return None

        # Build decision map: index → (action, claim)
        decision_map: dict[int, tuple[str, str]] = {}
        for d in decisions:
            if not isinstance(d, dict):
                continue
            idx = d.get("index")
            action = d.get("action", "keep")
            if isinstance(idx, int) and action in ("keep", "rewrite", "remove"):
                decision_map[idx] = (action, d.get("claim", ""))

        # Apply decisions to a new evidence list
        new_evidence: list[CompetitorEvidence] = []
        for i, ev in enumerate(research.evidence):
            action, new_claim = decision_map.get(i, ("keep", ""))
            if action == "remove":
                continue
            if action == "rewrite" and new_claim:
                new_ev = CompetitorEvidence(
                    competitor=ev.competitor,
                    field=ev.field,
                    claim=new_claim,
                    url=ev.url,
                    geography=ev.geography,
                    title=ev.title,
                    snippet=ev.snippet,
                )
                new_evidence.append(new_ev)
            else:
                new_evidence.append(ev)

        return ResearchResponse(
            target_model=research.target_model,
            competitor_names=research.competitor_names,
            evidence=new_evidence,
            evidence_based_recommendations=research.evidence_based_recommendations,
            strategic_hypotheses=research.strategic_hypotheses,
            uncertainty=research.uncertainty,
        )


class BrandInterpretationPass:
    """Post-review Brand Interpretation — a SEPARATE model call that runs
    AFTER the SemanticEvidenceReviewer has finalized evidence.

    Hard evidence-isolation boundary:
    - This class NEVER receives brand_reference during evidence review.
    - It receives ONLY finalized/surviving evidence from the reviewer.
    - It CANNOT modify the evidence collection — it only produces
      strategic implications that reference evidence by index.
    - Implications referencing non-surviving evidence are mechanically
      rejected.

    Cost: exactly one LLM call, only when brand_reference is non-empty.
    No web tools, no retry, no web search.

    If brand_reference is empty: zero calls, returns empty list.
    If LLM fails or returns invalid JSON: returns empty list (grace
    degradation — evidence is still available for rendering).
    """

    _SYSTEM_PROMPT = """คุณคือนักยุทธศาสตร์ที่วิเคราะห์นัยยะของหลักฐานคู่แข่ง
ต่อตำแหน่งของแบรนด์ของเรา

หน้าที่ของคุณคือรับหลักฐานที่ผ่านการตรวจสอบแล้ว (finalized evidence)
และ brand context ของเรา แล้วสร้าง strategic implications

กฎสำคัญ:
1. ห้ามสร้างข้อเท็จจริงเกี่ยวกับคู่แข่งที่ไม่มีใน evidence
2. ห้ามเปลี่ยนแปลง evidence — คุณไม่สามารถเพิ่ม ลบ หรือแก้ไข evidence ได้
3. implications เป็น inference/recommendation ไม่ใช่ evidence
4. แต่ละ implication ต้องอ้างอิง evidence ที่อยู่ในรายการที่ให้มา (evidence_ref = index)
5. ห้าม dump raw brand context ลงใน implications — ใช้เป็นเลนส์คิดเท่านั้น

ตัวอย่าง:
- evidence[0]: "CompA ราคา 5,000 THB" + brand: "premium positioning"
- valid implication: "การแข่งขันด้านราคาต่ำอาจทำให้ premium positioning อ่อนแอลง"
- invalid: "ลูกค้า CompA ชอบของถูก" (ไม่มีใน evidence)

ส่งกลับ JSON array แต่ละ element มี:
- evidence_ref: index ของ evidence ที่อ้างอิง (int)
- implication: ข้อความ inference/recommendation
- category: "recommendation" หรือ "inference"

ห้ามครอบ JSON ด้วย markdown code fence
"""

    def __init__(self, llm, config: dict | None = None):
        self.llm = llm
        self.config = config or {}

    def interpret(
        self,
        research: ResearchResponse,
        brand_reference: str = "",
        product_spec: str = "",
        quick_brief: str = "",
    ) -> list[StrategicImplication]:
        """Produce brand-aware strategic implications from finalized evidence.

        Returns a list of StrategicImplication, each referencing a surviving
        evidence index. Implications referencing non-surviving evidence are
        mechanically rejected.

        If brand_reference is empty, returns [] without making an LLM call.
        If the LLM call fails or returns invalid JSON, returns [] (grace
        degradation — evidence is still available for rendering).

        This method does NOT modify ``research`` — it only reads it.
        """
        if not brand_reference:
            return []
        if not research.evidence:
            return []

        # Build compact evidence summary for the prompt — only surviving evidence
        evidence_lines: list[str] = []
        for i, ev in enumerate(research.evidence):
            evidence_lines.append(
                f"[{i}] competitor: {ev.competitor} | field: {ev.field} | "
                f"claim: {ev.claim} | url: {ev.url}"
            )

        user_msg = (
            f"หลักฐานที่ผ่านการตรวจสอบแล้ว ({len(research.evidence)} รายการ):\n"
            + "\n".join(evidence_lines)
            + f"\n\n--- Brand Context ---\n{brand_reference}\n--- สิ้นสุด Brand Context ---\n"
        )
        if product_spec:
            user_msg += f"\nProduct spec: {product_spec[:500]}\n"
        if quick_brief:
            user_msg += f"Quick brief: {quick_brief[:300]}\n"
        user_msg += "\nส่งกลับ JSON array ของ strategic implications"

        messages = [
            {"role": "system", "content": self._SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        try:
            response = self.llm.chat(
                messages,
                model=self.config.get("model"),
                temperature=0.2,
                max_tokens=self.config.get("max_tokens", 2048),
                max_retry_limit=0,  # no retry — one pass only
                stream=False,
                tools=None,  # no web tools — no web calls
                source="competitor_analysis.brand_interpretation",
            )
        except Exception:
            return []  # graceful degradation

        if not response or not response.strip():
            return []

        # Truncation guard — if the provider cut the response mid-generation
        # (finish_reason=length), the JSON is likely malformed.  Return []
        # explicitly rather than relying on json.loads to fail silently.
        # This is a deterministic check on provider metadata, not a semantic
        # guess about content completeness.
        if getattr(self.llm, "last_truncated", False):
            return []

        # Parse JSON response
        try:
            clean = response.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*\n?", "", clean)
                clean = re.sub(r"\n?```\s*$", "", clean)
            raw_implications = json.loads(clean)
        except (json.JSONDecodeError, ValueError):
            return []

        if not isinstance(raw_implications, list):
            return []

        # Build set of valid evidence indices
        valid_indices = set(range(len(research.evidence)))

        # Filter implications — mechanically reject invalid evidence_refs
        implications: list[StrategicImplication] = []
        for imp in raw_implications:
            if not isinstance(imp, dict):
                continue
            ref = imp.get("evidence_ref", -1)
            if not isinstance(ref, int):
                continue
            if ref not in valid_indices:
                continue  # mechanically reject non-surviving evidence refs
            implication_text = imp.get("implication", "")
            if not implication_text:
                continue
            category = imp.get("category", "recommendation")
            if category not in ("recommendation", "inference"):
                category = "recommendation"
            implications.append(
                StrategicImplication(
                    evidence_ref=ref,
                    implication=implication_text,
                    category=category,
                )
            )

        return implications


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
