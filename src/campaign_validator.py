"""Deterministic semantic validator and evidence contract for Agent 3 (campaign_strategy).

This module does not call any model or web service.  It checks text output
against a structural evidence contract and campaign-specific guardrails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class AuditResult:
    rule: str
    ok: bool
    reason: str


_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?")
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^ )]+)\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_MODEL_TOKEN_RE = re.compile(r"\b([A-Za-z]+\d[\w]{0,4})\b")
_QUARTER_TOKENS = {"q1", "q2", "q3", "q4"}


def _has_word(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(kw.lower() in low for kw in keywords)


def _line_has_number(line: str) -> bool:
    return bool(_NUMBER_RE.search(line))


def _line_has_percent(line: str) -> bool:
    return bool(_PERCENT_RE.search(line))


def _line_numbers(line: str) -> list[float]:
    nums: list[float] = []
    for m in _NUMBER_RE.finditer(line):
        try:
            nums.append(float(m.group().replace(",", "")))
        except ValueError:
            pass
    return nums


def _percentages(line: str) -> list[float]:
    return [float(p.rstrip("%").strip()) for p in _PERCENT_RE.findall(line)]


def _currency_numbers(line: str) -> list[float]:
    nums: list[float] = []
    for m in _NUMBER_RE.finditer(line):
        start = max(0, m.start() - 10)
        end = min(len(line), m.end() + 10)
        window = line[start:end]
        if any(s in window for s in ("฿", "บาท", "baht", "bath")):
            try:
                nums.append(float(m.group().replace(",", "")))
            except ValueError:
                pass
    return nums


def _find_first_currency_number(text: str) -> float | None:
    nums = _currency_numbers(text)
    return nums[0] if nums else None


def _has_inline_citation(line: str) -> bool:
    """A numeric external claim is only supported by an inline Markdown URL on the same line."""
    return bool(_MD_LINK_RE.search(line))


def _extract_markdown_links(text: str) -> list[tuple[str, str]]:
    return _MD_LINK_RE.findall(text)


def _extract_bare_urls(text: str) -> list[str]:
    # Remove inline Markdown links first so their URLs are not counted as bare URLs.
    text_without_md = _MD_LINK_RE.sub("", text)
    urls = _BARE_URL_RE.findall(text_without_md)
    return [url.rstrip(".,;:!?()[]{}") for url in urls]


def _all_urls(text: str) -> set[str]:
    """Return every URL (bare or Markdown) found in the text."""
    urls: set[str] = set(_extract_bare_urls(text))
    for _, url in _MD_LINK_RE.findall(text):
        urls.add(url)
    return urls


def _split_source_section(output: str, headings: list[str]) -> tuple[str, str]:
    """Return (body, source_section).  source_section may be empty."""
    lines = output.split("\n")
    split_at: int | None = None
    for i, line in enumerate(lines):
        cleaned = line.lstrip("#").strip("* ")
        for h in headings:
            if cleaned.startswith(h) and len(cleaned) <= len(h) + 20:
                split_at = i
                break
        if split_at is not None:
            break
    if split_at is None:
        return output, ""
    return "\n".join(lines[:split_at]), "\n".join(lines[split_at:])


def _looks_like_search_or_tag_source(url_or_title: str, non_direct: list[str]) -> bool:
    low = url_or_title.lower()
    return any(nd in low for nd in non_direct)


def _normalize_url(url: str) -> str:
    return url.lower().rstrip("/")


_BASELINE_KEYWORDS = ["baseline", "ค่าฐาน", "ย้อนหลัง", "historical", "actual"]
_BASELINE_NEGATION_WORDS = [
    "no", "not", "none", "without", "lack", "missing", "not provided",
    "not available", "ไม่มี", "ไม่", "ยังไม่", "ไม่ได้", "ไม่พบ",
    "ไม่ระบุ", "ไม่ปรากฏ", "ไม่มีย้อนหลัง",
]


def _has_baseline_in_context(context_text: str) -> bool:
    """Return True only if a clause asserts a positive baseline/historical value."""
    if not context_text:
        return False
    clauses = re.split(r"[,;.!?•\n]", context_text)
    for clause in clauses:
        low = clause.lower()
        if not any(kw in low for kw in _BASELINE_KEYWORDS):
            continue
        if any(neg in low for neg in _BASELINE_NEGATION_WORDS):
            continue
        return True
    return False


def _extract_model_tokens(text: str) -> set[str]:
    tokens = {m.lower() for m in _MODEL_TOKEN_RE.findall(text)}
    # Quarter notations (Q1-Q4) are business timing, not product model IDs.
    tokens -= _QUARTER_TOKENS
    return tokens


def _extract_canonical_identities(product: str) -> tuple[str, set[str]]:
    """Return (canonical_product_id, set of acceptable aliases) from product text."""
    aliases: set[str] = set()
    canonical = ""
    # รหัสสินค้า: X หรือ Product ID: X
    m = re.search(r"(?:รหัสสินค้า|product\s*id)[:=]\s*([A-Za-z0-9\-_ ]+?)(?:\s|$|\n)", product, re.IGNORECASE)
    if m:
        canonical = m.group(1).strip()
        aliases.add(canonical.lower())
        for tok in re.findall(r"[A-Za-z0-9\-_]+", canonical):
            aliases.add(tok.lower())
    # รุ่นทั่วไป: brand + number เช่น LAGENIO K2
    for pat in [r"\b([A-Za-z]{2,}\s+[A-Za-z]*\d[\w]{0,4})\b", r"\b([A-Za-z]+\d[\w]{0,4})\b"]:
        for m2 in re.finditer(pat, product):
            aliases.add(m2.group(1).lower().replace(" ", ""))
            aliases.add(m2.group(1).split()[-1].lower())
            if not canonical:
                canonical = m2.group(1).strip()
    # ถ้ายังไม่มี ใช้ token แรกทีมีตัวเลข
    if not canonical:
        toks = _extract_model_tokens(product)
        if toks:
            canonical = sorted(toks, key=len, reverse=True)[0]
    return canonical, aliases


def _extract_competitor_models(competitors: str) -> set[str]:
    models: set[str] = set()
    for m in re.finditer(r"\b([A-Za-z]{2,}\s+[A-Za-z]*\d[\w]{0,4})\b", competitors):
        models.add(m.group(1).split()[-1].lower())
    for m in re.finditer(r"\b([A-Za-z]+\d[\w]{0,4})\b", competitors):
        token = m.group(1).lower()
        if token not in _QUARTER_TOKENS:
            models.add(token)
    return models


def _extract_competitor_evidence_urls(competitors: str) -> set[str]:
    """Return URLs that appear in the competitors context (selected evidence for a competitor)."""
    return {_normalize_url(u) for _, u in _extract_markdown_links(competitors)} | _extract_context_urls(competitors)


def _extract_context_urls(text: str) -> set[str]:
    return {_normalize_url(u) for u in _all_urls(text)}


def extract_context_flags(context: dict[str, Any]) -> dict[str, bool]:
    """Infer validation flags from the semantic context passed to build_prompt."""
    flags: dict[str, bool] = {}

    def _section_has_number_and_one_of(keywords: list[str], text: str) -> bool:
        if not text or not _line_has_number(text):
            return False
        low = text.lower()
        return any(kw in low for kw in keywords)

    product = str(context.get("product", ""))
    business = str(context.get("business", ""))
    competitors = str(context.get("competitors", ""))
    market = str(context.get("market", ""))
    customers = str(context.get("customers", ""))
    combined = product + "\n" + business + "\n" + competitors + "\n" + market

    cogs_kw = ["ต้นทุน", "cogs", "cost price", "ราคาต้นทุน"]
    margin_kw = ["margin", "กำไรขั้นต้น", "อัตรากำไร", "gross margin"]
    budget_kw = ["งบประมาณ", "budget", "ค่าใช้จ่าย"]
    fee_kw = ["ค่าธรรมเนียม", "fee", "commission", "คอมมิชชั่น"]
    retail_kw = ["retail price", "ราคาขายปลีก", "target price", "ราคาเป้าหมาย"]

    flags["has_cogs"] = _section_has_number_and_one_of(cogs_kw, combined)
    flags["has_margin"] = _section_has_number_and_one_of(margin_kw, combined)
    flags["has_budget"] = _section_has_number_and_one_of(budget_kw, business)
    flags["has_fee"] = _section_has_number_and_one_of(fee_kw, business)
    flags["has_retail_price"] = _section_has_number_and_one_of(retail_kw, combined)
    flags["has_market"] = bool(market.strip())

    # Treat competitor price as verified only if the artifact gives a number
    # and some kind of source pointer (URL, evidence label, or price marker).
    flags["has_competitor_prices"] = (
        bool(competitors.strip())
        and _line_has_number(competitors)
        and ("http" in competitors or "ราคา" in competitors or "evidence" in competitors.lower())
    )

    flags["has_baseline"] = _has_baseline_in_context(combined)

    flags["has_conflict"] = any(
        kw in combined.lower() for kw in ["ขัดแย้ง", "conflict", "contradict"]
    )

    # Extract all plain numeric values from product/business context so rules can
    # distinguish unsupported output claims from numbers that were given in context.
    number_matches = re.findall(r"\d{1,3}(?:,\d{3})+|\d+", combined)
    flags["context_numbers"] = sorted({float(m.replace(",", "")) for m in number_matches})

    canonical, aliases = _extract_canonical_identities(product)
    flags["canonical_product_id"] = canonical
    flags["product_aliases"] = sorted(aliases)
    flags["competitor_models"] = sorted(_extract_competitor_models(competitors))
    flags["competitor_evidence_urls"] = sorted(_extract_competitor_evidence_urls(competitors))
    flags["context_urls"] = sorted(
        _extract_context_urls("\n".join([product, business, competitors, market, customers]))
    )

    return flags


def audit_campaign_output(
    output: str,
    context_flags: dict[str, bool],
    instructions: dict[str, Any],
    rules: dict[str, Any],
) -> list[AuditResult]:
    """Run every deterministic guardrail rule and return per-rule verdicts."""
    if not output or not output.strip():
        return [AuditResult("not_blank", False, "output ว่างเปล่า")]

    flags = {k: v for k, v in (context_flags or {}).items()}

    estimate_labels = rules.get("estimate_labels", [])
    retail_markers = rules.get("retail_price_markers", [])
    promo_markers = rules.get("promo_price_markers", [])
    wholesale_markers = rules.get("wholesale_markers", [])
    financial_markers = rules.get("financial_claim_markers", [])
    benchmark_markers = rules.get("benchmark_markers", [])
    mechanic_markers = rules.get("cost_mechanic_markers", [])
    pending_phrases = rules.get("pending_financial_validation_phrases", [])
    target_markers = rules.get("guaranteed_target_markers", [])
    target_intent_markers = rules.get("guaranteed_target_intent_markers", [])
    target_kpi_metrics = rules.get("guaranteed_target_kpi_metrics", [])
    target_excluded_markers = rules.get("target_kpi_excluded_markers", [])
    target_refusal_phrases = rules.get("target_refusal_or_qualification_phrases", [])
    non_direct = rules.get("non_direct_source_substrings", [])
    source_headings = rules.get("source_section_headings", [])
    currency = rules.get("currency_symbols", [])
    tactic_map = rules.get("tactic_label_map", {})
    budget_markers = rules.get("budget_markers", [])
    discount_markers = rules.get("discount_markers", [])

    has_cogs = flags.get("has_cogs", False)
    has_margin = flags.get("has_margin", False)
    has_fee = flags.get("has_fee", False)
    has_budget = flags.get("has_budget", False)
    has_retail_price = flags.get("has_retail_price", False)
    has_market = flags.get("has_market", False)
    has_competitor_prices = flags.get("has_competitor_prices", False)
    has_baseline = flags.get("has_baseline", False)
    has_conflict = flags.get("has_conflict", False)
    # If COGS and a retail/target price are present, margin can be implied even if the
    # word "margin" does not appear in the context.
    implied_margin = has_margin or (has_cogs and has_retail_price)
    has_financials = has_cogs and implied_margin and has_budget and has_fee
    context_numbers = set(flags.get("context_numbers", []))

    canonical_product = str(flags.get("canonical_product_id", ""))
    product_aliases = {a.lower() for a in (flags.get("product_aliases") or [])}
    competitor_models = {m.lower() for m in (flags.get("competitor_models") or [])}
    competitor_evidence_urls = {_normalize_url(u) for u in (flags.get("competitor_evidence_urls") or [])}

    # Runtime selected evidence = context URLs + web-search annotations passed by agent.
    context_urls = {_normalize_url(u) for u in (flags.get("context_urls") or [])}
    selected_evidence = {_normalize_url(u) for u in (instructions.get("selected_evidence_urls") or [])}
    selected_evidence = selected_evidence | context_urls

    body, source_section = _split_source_section(output, source_headings)
    all_links = _extract_markdown_links(output)
    body_links = _extract_markdown_links(body)
    source_links = _extract_markdown_links(source_section)
    source_urls = {url for _, url in source_links}
    body_urls = {url for _, url in body_links}
    all_urls = _all_urls(output)

    results: list[AuditResult] = []

    # Protocol/format check: pseudo-tool markup that leaks into user-facing output.
    pseudo_match = re.search(
        r"(<tool_call\b|<\/tool_call>|\bweb_search\s*\(|\bgoogle\s*\(|User Safety:\s*)",
        output,
        re.IGNORECASE,
    )
    pseudo_err = f"พบ pseudo-tool markup ใน output: {pseudo_match.group(0)[:80]}" if pseudo_match else ""
    results.append(AuditResult("no_pseudo_tool_markup", not bool(pseudo_err), pseudo_err))

    # -----------------------------------------------------------------------
    # 0. Product identity
    # -----------------------------------------------------------------------
    identity_err = ""
    identity_refusal_phrases = rules.get("identity_refusal_or_correction_phrases", [])
    allowed_product_tokens = product_aliases | competitor_models
    body_model_tokens = _extract_model_tokens(body)
    unknown_models = body_model_tokens - allowed_product_tokens
    # Only fail an unknown model token if it appears outside a clear refusal/correction line.
    forbidden_models = set()
    for tok in unknown_models:
        lines_with_tok = [ln for ln in body.split("\n") if tok in ln.lower()]
        if any(not _has_word(ln, identity_refusal_phrases) for ln in lines_with_tok):
            forbidden_models.add(tok)
    if forbidden_models:
        identity_err = f"output อ้างถึงรุ่นที่ไม่ได้อยู่ในบริบท: {', '.join(sorted(forbidden_models))}"
    elif not canonical_product and body_model_tokens:
        # context กำกวม/ไม่มี canonical แต่ output กล้าอ้างรุ่น → ห้ามเดา
        identity_err = f"context ไม่มี product identity ชัดเจน แต่ output อ้างรุ่น: {', '.join(sorted(body_model_tokens))}"
    results.append(AuditResult("product_identity_intact", not bool(identity_err), identity_err))

    # -----------------------------------------------------------------------
    # 0.5 Competitor mentions must be tied to evidence
    # -----------------------------------------------------------------------
    competitor_err = ""
    if competitor_models and not competitor_evidence_urls:
        for line in body.split("\n"):
            if any(tok in line.lower() for tok in competitor_models):
                competitor_err = line[:120]
                break
    elif competitor_models and competitor_evidence_urls:
        for line in body.split("\n"):
            if not any(tok in line.lower() for tok in competitor_models):
                continue
            line_urls = {_normalize_url(url) for _, url in _extract_markdown_links(line)}
            if not (line_urls & competitor_evidence_urls):
                competitor_err = line[:120]
                break
    results.append(
        AuditResult(
            "competitor_mentions_grounded",
            not bool(competitor_err),
            competitor_err,
        )
    )

    # -----------------------------------------------------------------------
    # 1. Evidence contract / source section
    # -----------------------------------------------------------------------
    bare_urls = _extract_bare_urls(output)
    bare_url_err = f"พบ bare URL ใน output: {bare_urls[0][:120]}" if bare_urls else ""
    results.append(AuditResult("no_bare_urls", not bool(bare_url_err), bare_url_err))

    unauthorized_url = ""
    for url in all_urls:
        if _normalize_url(url) not in selected_evidence:
            unauthorized_url = f"URL ไม่อยู่ใน selected evidence manifest: {url[:120]}"
            break
    results.append(
        AuditResult(
            "unauthorized_url_or_source",
            not bool(unauthorized_url),
            unauthorized_url,
        )
    )

    source_only_fail = False
    for _, url in source_links:
        if url not in body_urls:
            source_only_fail = True
            break
    results.append(
        AuditResult(
            "source_section_used_only",
            not source_only_fail,
            "แหล่งอ้างอิงมี URL ที่ไม่ถูกอ้างอิงในเนื้อหาหลัก" if source_only_fail else "",
        )
    )

    non_direct_fail = ""
    for _, url in all_links:
        if _looks_like_search_or_tag_source(url, non_direct):
            non_direct_fail = f"source URL ไม่ใช่ direct source: {url[:120]}"
            break
    for label, _ in all_links:
        if _looks_like_search_or_tag_source(label, non_direct):
            non_direct_fail = f"source label ไม่ใช่ direct source: {label[:120]}"
            break
    results.append(
        AuditResult(
            "no_search_tag_sources",
            not bool(non_direct_fail),
            non_direct_fail,
        )
    )

    citation_untied = []
    for line in body.split("\n"):
        links_in_line = _extract_markdown_links(line)
        if not links_in_line:
            continue
        text_without_links = _MD_LINK_RE.sub("", line).strip("- *")
        # A citation must support a claim; a line that is only the link is a dump.
        if not text_without_links or _line_has_number(line) or _has_word(line, benchmark_markers + wholesale_markers):
            continue
        citation_untied.append(line[:120])
    results.append(
        AuditResult(
            "citations_tied_to_claims",
            not citation_untied,
            "citation ไม่ผูกกับ claim: " + citation_untied[0] if citation_untied else "",
        )
    )

    # -----------------------------------------------------------------------
    # 2. Financial certainty guardrails
    # -----------------------------------------------------------------------
    wholesale_violations = []
    for line in output.split("\n"):
        if _line_has_number(line) and _has_word(line, wholesale_markers):
            if not (has_cogs and has_fee):
                wholesale_violations.append(line[:120])
        if _line_has_number(line) and _has_word(line, financial_markers):
            if not implied_margin:
                wholesale_violations.append(line[:120])
    results.append(
        AuditResult(
            "no_numeric_wholesale_margin_without_financials",
            not wholesale_violations,
            wholesale_violations[0] if wholesale_violations else "",
        )
    )

    # A numeric retail/promo price must have an explicit numerical basis:
    # COGS, a target retail price, or verified competitor prices in context.
    # Without such basis, no numeric retail/promo may appear — not even as "estimate".
    price_basis = has_cogs or has_retail_price or has_competitor_prices
    retail_violations = []
    for line in output.split("\n"):
        if not _line_has_number(line):
            continue
        if _has_word(line, retail_markers) or _has_word(line, promo_markers):
            if not price_basis and _has_word(line, currency):
                retail_violations.append(line[:120])
            elif not has_cogs and not _has_word(line, estimate_labels):
                retail_violations.append(line[:120])
    results.append(
        AuditResult(
            "indicative_retail_promo_labelled",
            not retail_violations,
            retail_violations[0] if retail_violations else "",
        )
    )

    mechanic_violation = ""
    if not has_financials:
        for line in output.split("\n"):
            if _has_word(line, mechanic_markers) and not _has_word(line, pending_phrases):
                # Warranty/guarantee mentions alone are not cost-bearing offers
                # unless they refer to a product/device coverage duration.
                if _has_word(line, ["รับประกัน", "warranty"]) and not _has_word(
                    line, ["ปี", "year", "เดือน", "month", "ระยะเวลา", "ตัวเครื่อง", "อุปกรณ์"]
                ):
                    continue
                mechanic_violation = line[:120]
                break
    results.append(
        AuditResult(
            "cost_mechanics_pending_financial_validation",
            not bool(mechanic_violation),
            mechanic_violation,
        )
    )

    # -----------------------------------------------------------------------
    # 3. External factual claims / benchmarks
    # -----------------------------------------------------------------------
    benchmark_violations = []
    competitor_only_markers = {"คู่แข่ง", "competitor"}
    price_or_metric_markers = ["ราคา", "price", "benchmark"]
    for line in output.split("\n"):
        if not _line_has_number(line) and not _line_has_percent(line):
            continue
        if not _has_word(line, benchmark_markers):
            continue
        # If the claim is clearly a target phrase, let the target rule handle it.
        if _has_word(line, target_markers):
            continue
        # A bare "competitor" mention with a number is not a benchmark unless
        # it is also a price or explicit benchmark comparison.
        if _has_word(line, list(competitor_only_markers)):
            other_markers = [m for m in benchmark_markers if m not in competitor_only_markers]
            if not _has_word(line, other_markers) and not _has_word(line, price_or_metric_markers):
                continue
        if not _has_inline_citation(line):
            benchmark_violations.append(line[:120])
    results.append(
        AuditResult(
            "benchmarks_cited_or_removed",
            not benchmark_violations,
            benchmark_violations[0] if benchmark_violations else "",
        )
    )

    # -----------------------------------------------------------------------
    # 4. Guaranteed / baseline-less numeric targets
    # -----------------------------------------------------------------------
    target_violations = []
    for line in output.split("\n"):
        if not _line_has_number(line):
            continue
        # Refusal / provisional qualification is allowed even with target numbers.
        if _has_word(line, estimate_labels + target_refusal_phrases):
            continue
        # Price, discount, budget and other financial-allocation lines are not KPI targets.
        if _has_word(line, target_excluded_markers):
            continue
        has_intent = _has_word(line, target_intent_markers)
        has_kpi = _has_word(line, target_kpi_metrics)
        has_standalone = _has_word(line, target_markers)
        if (has_intent and has_kpi) or has_standalone:
            line_numbers = set(_line_numbers(line))
            unsupported_numbers = line_numbers - context_numbers
            if not has_baseline and unsupported_numbers:
                target_violations.append(line[:120])
    results.append(
        AuditResult(
            "no_guaranteed_numeric_targets_without_baseline",
            not target_violations,
            target_violations[0] if target_violations else "",
        )
    )

    # -----------------------------------------------------------------------
    # 5. Instruction constraints (budget, discount, forbid, quick brief)
    # -----------------------------------------------------------------------
    budget_err = ""
    budget_max = instructions.get("budget_max")
    if budget_max:
        try:
            max_b = float(str(budget_max).replace(",", ""))
            for line in output.split("\n"):
                if _has_word(line, source_headings):
                    continue
                if not _line_has_number(line):
                    continue
                if _has_word(line, budget_markers):
                    for n in _currency_numbers(line):
                        if n > max_b:
                            budget_err = f"งบประมาณ {n:,.0f} เกิน ceiling {max_b:,.0f}"
                            break
                if budget_err:
                    break
        except (TypeError, ValueError):
            pass
    results.append(AuditResult("budget_max_enforced", not bool(budget_err), budget_err))

    # -----------------------------------------------------------------------
    # 5.5 Budget percentage allocation must be grounded in context
    # -----------------------------------------------------------------------
    budget_alloc_err = ""
    if not has_budget and not has_baseline:
        for line in output.split("\n"):
            if not _line_has_percent(line):
                continue
            if _has_word(line, budget_markers) or _has_word(line, ["allocated", "cost allocation", "สัดส่วน", "allocation"]):
                budget_alloc_err = line[:120]
                break
    results.append(
        AuditResult(
            "budget_allocation_grounded",
            not bool(budget_alloc_err),
            budget_alloc_err,
        )
    )

    # Pre-compute retail / promo prices once for discount money checks.
    retail_price: float | None = None
    promo_price: float | None = None
    for line in output.split("\n"):
        if _has_word(line, retail_markers):
            v = _find_first_currency_number(line)
            if v is not None and (retail_price is None or v > retail_price):
                retail_price = v
        if _has_word(line, promo_markers):
            v = _find_first_currency_number(line)
            if v is not None and (promo_price is None or v < promo_price):
                promo_price = v

    discount_err = ""
    discount_max = instructions.get("discount_max")
    if discount_max:
        try:
            max_d = float(str(discount_max).replace(",", ""))
            for line in output.split("\n"):
                is_discount_line = _has_word(line, discount_markers)
                if not is_discount_line:
                    continue
                # Percentage discount
                if _line_has_percent(line):
                    for p in _percentages(line):
                        if p > max_d:
                            discount_err = f"ส่วนลด {p:.0f}% เกิน ceiling {max_d:.0f}%"
                            break
                # Money discount off a known retail price
                elif retail_price:
                    nums = _currency_numbers(line)
                    for n in nums:
                        if n < retail_price:
                            pct = (n / retail_price) * 100
                            if pct > max_d:
                                discount_err = f"ส่วนลดเงิน {n:,.0f} บาท ({pct:.0f}%) เกิน ceiling {max_d:.0f}%"
                                break
                        if promo_price and n < retail_price:
                            pct = ((retail_price - n) / retail_price) * 100
                            if pct > max_d:
                                discount_err = f"ราคาโปร {n:,.0f} บาท ({pct:.0f}% off) เกิน ceiling {max_d:.0f}%"
                                break
                if discount_err:
                    break
        except (TypeError, ValueError):
            pass
    results.append(AuditResult("discount_max_enforced", not bool(discount_err), discount_err))

    forbid_tactics = instructions.get("forbid_tactics", [])
    forbidden_found = []
    if forbid_tactics:
        low_out = output.lower()
        for t in forbid_tactics:
            if t.lower() in low_out:
                forbidden_found.append(t)
            for label in tactic_map.get(t, []):
                if label.lower() in low_out:
                    forbidden_found.append(label)
    results.append(
        AuditResult(
            "forbid_tactics_enforced",
            not forbidden_found,
            f"พบ tactic ที่ห้ามใช้: {', '.join(forbidden_found)}" if forbidden_found else "",
        )
    )

    # -----------------------------------------------------------------------
    # 6. Conflict handling
    # -----------------------------------------------------------------------
    conflict_err = ""
    if has_conflict and not (
        _has_word(output, ["ขัดแย้ง", "conflict", "contradict", "แตกต่าง"])
    ):
        conflict_err = "context มีข้อมูลขัดแย้งแต่ output ไม่ได้ระบุ"
    results.append(AuditResult("conflict_acknowledged", not bool(conflict_err), conflict_err))

    return results


def validate_campaign_output(
    output: str,
    context_flags: dict[str, bool],
    instructions: dict[str, Any],
    rules: dict[str, Any],
) -> tuple[bool, str]:
    """Return (ok, error) for all failed semantic rules.

    The error string lists every failing rule so the repair call can address
    the full audit, not just the first one.
    """
    failures = [r for r in audit_campaign_output(output, context_flags, instructions, rules) if not r.ok]
    if not failures:
        return True, ""
    error = "\n".join(f"- {r.rule}: {r.reason}" for r in failures)
    return False, error
