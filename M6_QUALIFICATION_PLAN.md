# M6 Qualification Plan — Forward Plan

**Purpose:** Describe the CURRENT forward qualification plan for MKTApp
Agent 1–4. This is the canonical source of truth for the plan we are
now following, not historical evidence.

**Status:** Active. Supersedes prior M6 remediation designs for
forward-planning purposes.

**Historical evidence:** M6 run reports, judge reports, and UAT plans
remain unchanged as audit-trail evidence. See `M6_STATUS.md` for the
factual current state.

---

## Objective

Qualify the current MKTApp Agent 1–4 implementation against a complete
Frontier comparison while separately proving delivery through automated
Web E2E tests.

---

## Phase 1 — Backend/Product Integrity

Complete known generic defects before spending on another Frontier run.

Current checkpoint includes:

- M6.1 root-cause remediation;
- grounding improvements (supplied facts, researched facts with
  evidence, inference/recommendation);
- Product Spec instruction precedence (quick_brief outranks internal
  presentation defaults; product identity isolation preserved
  independently);
- finish_reason/truncation observability;
- Content Pillars routing (optional strategic guidance, not mandatory
  output text);
- run-scoped selected Pillar (no hidden mutable state on Orchestrator);
- attachment fatal preflight at the single shared execution seam
  (`BaseAgent.run`) plus media generation path;
- remaining competitor_analysis Brand Stage 3 decision (design-only,
  not yet implemented).

All production changes require offline regression first.

**Exit criteria:** Full offline suite passes. No new paid calls needed
to verify Phase 1.

---

## Phase 2 — Frontier Harness Readiness

Before a new paid comparison:

- Keep core S1–S4 scenarios.
- Keep the established quality/Judge rubric unless a real methodology
  defect is separately approved.
- Capture `finish_reason` for every candidate call.
- Detect truncation explicitly.
- Candidate completeness is a prerequisite for judging — incomplete
  candidates must not be scored as though they were complete.
- Frontier S2–S4 from the previous run are NOT acceptable as the clean
  baseline because they were incomplete (truncated).
- MKTApp candidate completeness must also be checked.
- Exact Product Owner-approved paid caps must be enforced.
- The harness must not reinterpret a larger cumulative ceiling as
  permission to exceed a smaller run-specific cap.

No benchmark-specific answer keys.
No hardcoding expected Frontier wording.

---

## Phase 3 — Clean Paid Qualification

Generate NEW candidates for:

- current remediated MKTApp;
- fresh complete Frontier outputs.

Then:

1. Candidate completeness check.
2. Blind Judge.
3. Existing scoring dimensions.
4. Existing gates.
5. Evidence preservation.

Frontier output is a comparison candidate, NOT a golden answer used to
write deterministic expected-output tests.

---

## Phase 4 — Residual-Gap Loop

After each clean comparison, classify the remaining gap as:

- product defect;
- context/routing defect;
- qualification/harness defect;
- stochastic variance requiring evidence;
- accepted trade-off;
- model capability ceiling.

Then:

generic root-cause fix → offline regression → rerun only when
justified.

Stop iterating when:

- qualification passes;
- remaining issue is accepted explicitly;
- or evidence shows further prompt/rule engineering is no longer
  useful and the remaining limitation is predominantly model
  capability.

Do NOT respond by accumulating:

- semantic keyword lists;
- benchmark-specific validators;
- brand-specific hacks;
- brittle expected outputs.

---

## Phase 5 — Web E2E / Browser Qualification

This is separate from Frontier semantic-quality qualification.

Automated browser tests should cover representative real user flows:

UI → product/context → quick_brief → attachment upload → configured
Content Pillars → selected Pillar where applicable →
API/orchestration → Agent → output → renderer → UI.

Also include important failure paths such as:

- unreadable/rejected attachment;
- backend/API failure;
- missing optional context;
- loading/error state;
- result rendering.

The purpose is to minimize Product Owner manual regression clicking.

Manual Product Owner acceptance remains a light final UX/product check
rather than the primary regression strategy.

---

## Testing Layers

These answer different questions and must not be conflated:

1. **Unit / offline contract tests** — deterministic logic, routing,
   isolation, validation, error handling. Run via `pytest` with no
   network/model/web calls.
2. **Integration / orchestration tests** — orchestration, API, context
   propagation across multiple components. Still offline; uses
   mocks/stubs for LLM.
3. **Frontier semantic-quality qualification** — semantic output
   quality versus Frontier AI baseline. Requires paid model calls and
   is run only against a fixed production state under an approved
   budget cap.
4. **Web E2E / browser qualification** — real user interaction and
   end-to-end delivery path through the web UI. Not run during backend
   remediation.
5. **Product Owner manual acceptance** — light final UX/product
   judgment. Not repetitive regression clicking.

---

## Content Novelty / Non-Repetition

**Intended product goal:** Repeated generation should maintain
strategic consistency while reducing unnecessary repetition of:

- concepts;
- angles;
- hooks;
- formats;
- topic/feature focus;
- messaging;
- CTA.

Content Pillars help strategic rotation but do NOT guarantee novelty.

Existing history persistence exists (`cache/content_history.json` via
`src/content_history.py`), but wider preventive history routing is
currently blocked by missing user/workspace isolation.

Therefore:

MKTApp must NOT yet claim guaranteed non-repetition.

Before broader history injection:

- resolve tenancy/scope;
- prevent cross-user history leakage;
- reuse existing history infrastructure rather than creating a
  duplicate store.

---

## Known competitor_analysis gap

Stage A evidence remains objective and brand-independent.

Current deterministic Stage 3 (`CompetitorReportRenderer`) cannot
genuinely apply brand positioning semantically — it has no semantic
reasoning capability.

A design decision is pending. See
`M6_COMPETITOR_STAGE3_DESIGN.md` for the design-only proposal.

Do NOT claim competitor Brand Item 4 is completely resolved until that
decision is implemented and qualified.

---

## Approval Boundaries

- No commit before Product Owner review.
- No push before Product Owner approval.
- No paid/model/web/media calls before Phase 3 approval.
- No Frontier rerun before Phase 2 readiness check.
- No Web E2E execution before Phase 5.
- No semantic keyword/regex lists for semantic concepts.
- No brand-specific hardcoded behavior.
- No benchmark-specific answer keys or expected-output validators.
