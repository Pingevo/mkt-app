# Minimum Necessary Qualification Plan (M2–M5)

> **HISTORICAL / SUPERSEDED — preserved as evidence only.**
> This document is preserved as historical planning/evidence. It must NOT be used to determine current next actions, model configuration, paid-call authorization, or readiness status. Current execution status lives in `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md`; readiness criteria live in `AGENT_PRODUCTION_READINESS_SPEC.md`.

**Mode:** Offline only — no paid API, web search, image/video generation, OpenRouter free model, production code/config change, or commit.
**Goal:** Identify the smallest set of real-model qualification runs still needed to claim Full Beta, and the exact UI scope to hide if we choose a smaller Limited Beta.  
**Date:** 2026-09-02

---

## Full Beta contract summary (from `AGENT_PRODUCTION_READINESS_SPEC.md`)

Beta requires all of the following in representative UI-equivalent real-model runs:

1. **Zero-prompt default** works
2. **Quick Brief steering** changes output
3. **Persistent Agent Settings** change behavior
4. **Critical reliability** — no critical defect
5. **Real-model evidence** for each UI-open mode
6. **Honest boundary** — unproven features are closed/labelled

**Scope rule from `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md`:** if the UI exposes a control, we must either (a) qualify it with real-model evidence, or (b) explicitly close/hide the control and record the reduced public label.  
**No free pass:** a cut is a product/UI scope reduction, not a "skip".

---

## Reconciled remaining gaps

We reconciled `FULL_BETA_GAP_AUDIT.md`, `AGENT_PRODUCTION_READINESS_SPEC.md`, `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` and the current UI code (`src/wizard_ui.js`, `src/orchestrator.py`, `config/agent_instructions.json`) to determine which capabilities are actually open.

| Agent | UI-open capability | Current real-model evidence | Smallest test case | Paid / Offline | Expected calls | Expected cost | Hard cap | Classification |
|-------|--------------------|-----------------------------|--------------------|----------------|----------------|-------------------:|----------|----------------|
| **A1** | Multi-product selection (`product_ids` in `orchestrator.py:542+`, display in `wizard_ui.js:1132`) | **PASS** — `A1_multi_product_spec` ran 2 calls, $0.028, K2/K3 separate, no cross-contamination | `A1_multi_product_spec` (`product_ids=["Lagenio K2", "Lagenio K3"]`) | **Done** | 2 (generate + review) | `$0.10` expected / $0.028 actual | `product_spec` cap 2 | **Required for current UI** |
| **A2** | Persistent Agent Settings (`config/agent_instructions.json:49-134`, `openAgentSettings` in `wizard_ui.js:470`) | `UNPROVEN` | `A2_agent_settings_effect` — run with `agent_settings_override` and focused brief | **Paid** | 3 (1 text + 2 web_search) | `$0.35` | `competitor_analysis` cap 3 | **Required** |
| **A2** | Quick Brief deliverable steering (Quick Brief field in `wizard_ui.js:149`, table renderer in `competitor_evidence.py:350+`) | `UNPROVEN` | `A2_flexible_deliverable` — brief: "สรุปแบบ bullet points ไม่ใช่ตาราง" | **Paid** | 3 (1 text + 2 web_search) | `$0.35` | `competitor_analysis` cap 3 | **Required for Full Beta** |
| **A3** | Persistent Agent Settings (`config/agent_instructions.json:136-148`, `openAgentSettings`) | `UNPROVEN` | `A3_agent_settings_effect` — run with budget/forbid/discount override and brief | **Paid** | 4 (1 text + 1 web_search + 1 review + 1 repair) | `$0.20` | `campaign_strategy` cap 4 | **Required** |
| **A4** | TikTok platform (`wizard_ui.js:598`, `platform: ['facebook','tiktok']`) | **PASS** — `A4_tiktok_text_image` ran 3 calls, $0.105, 1 TikTok post with 9:16 image prompt and actual PNG | `A4_tiktok_text_image` — `platforms=["tiktok"], content_count=1, auto_image=True` | **Done** | 3 (2 text + 1 image) | `$0.18` expected / $0.105 actual | `content_creator` cap 6 | **Required for current UI** |
| **A4** | Multi-post count 1–20 (`wizard_ui.js:609`, `min=1 max=20`) | **PASS (functional)** — `A4_multi_post_facebook` ran 6 calls, $0.216, 2 distinct Facebook posts + 2 actual PNG images; cost-plan defect remediated offline | `A4_multi_post_facebook` — `platforms=["facebook"], content_count=2, auto_image=True` | **Done / no rerun** | 6 (4 text + 2 images) | `$0.36` expected / $0.216 actual | `content_creator` cap 6 | **Required for current UI** |

**Already proven — do not rerun:**
- A1 single-product one-page spec with image (`PAID_SMOKE_QUALIFICATION_REPORT.md` A1)
- A2 default web discovery with evidence (`PAID_SMOKE_QUALIFICATION_REPORT.md` A2)
- A3 web search + flexible executive brief + validator (`PAID_RERUN_A3_A4_REPORT.md` A3)
- A4 Facebook single-post + actual PNG image (`PAID_RERUN_A3_A4_REPORT.md` A4 re-run)
- A4 TikTok 1 post + 9:16 PNG image (`PAID_RERUN_A3_A4_REPORT.md` A4 re-run)
- A4 multi-post Facebook 2 distinct posts + 2 actual PNG images (`PAID_RERUN_A3_A4_REPORT.md` A4 multi-post)
- A4 ask-before media (`tests/test_bug3_ask_mode_no_auto_media.py`)

---

## Offline prerequisite (harness-only, not production)

**A1 multi-product:** `qual_runner.run_case()` now accepts `product_ids` as a list and joins them with `" + "` exactly like the production UI path (`wizard_ui.js:1132` expects `sel.product_ids.join(' + ')`).  
- **Done** — `scripts/qual_runner.py` updated; `run_case` sets `effective_product_id = " + ".join(product_ids)`, passes the list to `product_db.get_scoped_context_text()`, and records `product_ids`/`effective_product_id` in evidence.  
- The production path already supported `product_ids` (`orchestrator.py:542+, run_context.py:180+`); no production architecture changed.  
- **Offline regression tests added** in `tests/test_qual_runner_offline.py`:
  - `test_qual_runner_run_case_accepts_product_ids`
  - `test_qual_runner_run_case_single_product_no_product_ids`
  - `test_qual_runner_run_case_multi_product_no_cross_contamination`
- **Status:** harness is ready; awaiting one paid qualification run with `product_ids=["Lagenio K2", "Lagenio K3"]`.

---

## Three qualification options

### A. Current-UI Full Beta minimum

Qualify **every control that the UI currently exposes**. No scope reduction.

| Case | Expected cost |
|------|-------------------|
| A1 multi-product spec (after harness ready) | `$0.10` |
| A2 Agent Settings effect | `$0.35` |
| A2 flexible deliverable | `$0.35` |
| A3 Agent Settings effect | `$0.20` |
| A4 TikTok 1 post + image | `$0.18` |
| A4 multi-post Facebook 2 | `$0.36` |
| **Total expected** | **$1.54** |

**What you get:**
- Full Beta label for all 4 Agents with current UI open.
- Honest Beta for multi-product, TikTok, multi-post.

**Risk:** A1 requires the harness `product_ids` change first; A2 cases are web-search heavy and may exceed cost if search expands.

---

### B. Explicitly reduced Limited Beta

Close or hide several UI controls, then qualify only what remains open.

| UI control / route to hide or cap | Why it is reduced |
|--------------------------|-------------------|
| Multi-product selection route (`product_ids` in `orchestrator.py`, display in `wizard_ui.js:1132`) | Limit Agent 1 to single-product scope; disable second-product selection or ignore `product_ids` in UI handler |
| TikTok platform chip (`wizard_ui.js:598`, `platform: ['facebook','tiktok']`) | Limit Agent 4 to Facebook-only; remove `tiktok` from `platforms` list or hide the chip |
| Post count input (`wizard_ui.js:609`, `min=1 max=20`) | Limit Agent 4 to 1 post per run; cap `max=1` or hide the count input |
| Quick Brief deliverable-format steering for Competitor Analysis (`wizard_ui.js:149` Quick Brief field) | Limit Agent 2 to table-only output; document that Quick Brief does not change competitor report format |

| Case to run | Expected cost |
|-------------|-------------------|
| A2 Agent Settings effect | `$0.35` |
| A3 Agent Settings effect | `$0.20` |
| **Total expected** | **$0.55** |

**What you get:**
- A valid, honest Limited Beta with a smaller public scope.
- Agent 1: single-product only (already proven)
- Agent 2: table-only competitor analysis + Settings proven
- Agent 3: Settings proven (core campaign, web/flexible already proven)
- Agent 4: Facebook 1 post only (already proven)

**Public labels if this option is chosen:**
- Product Analyst — Limited Beta (single-product)
- Competitor Analyst — Limited Beta (table-only deliverable)
- Campaign Strategist — Limited Beta (Settings + web/flexible)
- Content Creator — Limited Beta (Facebook single-post only)

---

### C. Deferred / Post-Beta

No real-model qualification now. Keep as roadmap.

| Capability | Why deferred |
|------------|--------------|
| Video generation (`media_type: 'video'`, `auto_video`) | Not in current Beta scope |
| Multi-agent flow (`MAX_AGENTS_PER_FLOW = 1`) | Locked in UI; roadmap feature |
| Frontier comparison / side-by-side | Quality benchmark, not Beta qualification gate |
| Model/provider variance | Production-readiness, not Beta blockers |
| Full scheduling/run-later flow | UI control exists but not Beta acceptance material |

**Cost:** `$0`

---

## Per-case dry-run basis

All costs are computed from `config/qualification.yaml` with `qual_runner.dry_run_cost_plan()`; no paid calls were made.

| Case | Expected calls | Expected cost | Hard call cap |
|------|----------------|-------------------:|---------------|
| A1 multi-product spec | 2 | `$0.10` | `product_spec` 2 |
| A2 Agent Settings effect | 3 | `$0.35` | `competitor_analysis` 3 |
| A2 flexible deliverable | 3 | `$0.35` | `competitor_analysis` 3 |
| A3 Agent Settings effect | 4 | `$0.20` | `campaign_strategy` 4 |
| A4 TikTok 1 post + image | 3 (2 text + 1 image) | `$0.18` | `content_creator` 6 |
| A4 multi-post Facebook 2 | 6 (4 text + 2 images) | `$0.36` | `content_creator` 6 |

**Hard-cap feasibility / stop condition:**
- `A4 multi-post Facebook 2` uses the full `content_creator` hard cap of 6 calls (0 spare calls).
- Conditional repair or any extra call per post would exceed the cap and `PaidCallGuard` would block before the image-generation calls, so the run would fail to produce the second image.
- Current `max_review_iterations` for `content_creator` is 1, so the happy path (generate + one review per post + media) fits exactly; the estimate above is the expected happy path, not a conservative worst case.

---

## Rules observed in this report

- No paid API, web search, image/video generation, or OpenRouter free model calls.
- No production code or config changes.
- No commit or push.
- No rerun of already-proven cases.
- Every cut is framed as a UI scope reduction with public label, not an "optional test".
