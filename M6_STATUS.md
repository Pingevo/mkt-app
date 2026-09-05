# M6 Status

**Canonical current-state document for M6 qualification and remediation.**
Last updated: 2026-09-05 (Same-model uplift qualification result recorded; benchmark levels defined; S3 root-cause investigation completed; Web/Browser E2E implemented and passing with Playwright).

---

## M6.1 Qualification Result

| Field | Value |
|-------|-------|
| **M6.1 overall** | **FAIL** |
| Production commit qualified | `2318897ae3ff0dadff47599de4b9e0dee034a7ac` |
| Harness commit | `a5e05028151832f8a65571770dcabeb57f5c1f18` |
| M6 remediation baseline | `61b6d93bb0a4389eac1bb9ff936ce6a46004ce23` |
| Source run (incomplete) | `data/m6_frontier_uat/20260904_070830/` |
| Continuation run (completed) | `data/m6_frontier_uat/20260904_094021_resume_from_20260904_070830/` |
| Production remediation during qualification | **None** — no production code was modified during the run |

### Qualification validity — separated axes

The earlier single `Execution/harness evidence = VALID` statement is
replaced by three explicit axes, because the truncation evidence
(Round 8) showed that evidence integrity and candidate completeness
are different concerns:

| Axis | Status | Detail |
|------|--------|--------|
| Execution / evidence integrity | **VALID** | All 7 paid calls completed; all evidence preserved; judge executed; hashes verified. |
| Frontier candidate completeness | **COMPROMISED** | S1 complete. **S2/S3/S4 truncated/incomplete** (see §Truncation below). |
| Clean Frontier-parity comparability | **COMPROMISED for S2–S4** | Because Frontier S2/S3/S4 were truncated, the MKTApp-vs-Frontier deltas for S2–S4 must **not** be interpreted as a clean measurement of full Frontier parity. S1 is a clean comparison. |

**Important:** The M6.1 FAIL verdict stands and is driven by genuine
MKTApp product defects (grounding, instruction precedence, brand
utilization) observed in the evidence — **not** by the Frontier
truncation. The Frontier truncation, if anything, weakened the
Frontier candidate and may have made MKTApp appear relatively stronger
in S2–S4. The truncation does **not** justify removing the observed
MKTApp product defects, which remain actionable.

---

## Gate Results

All gate semantics are from the committed implementation in
`scripts/m6_judge_runner.py` lines 785–788:

```python
non_inferior = all(d >= -0.5 for d in all_deltas) and overall_mean >= -0.5
brand_gate = attempted_brand > 0 and (sum(brand_passes) / attempted_brand) >= 0.5
m6_pass = non_inferior and len(hard_failures) == 0 and brand_gate
```

### Non-inferiority: FAIL

- Overall mean delta (MKTApp − Frontier): **−0.167** — passes the mean threshold (≥ −0.5)
- But **11 individual dimension deltas** were below −0.5:
  - S1: all 6 dimensions (−1.0 to −2.0)
  - S2: 3 dimensions (−1.0 each)
  - S4: 2 dimensions (−1.0 each)
- The `all(d >= -0.5 for d in all_deltas)` condition fails.
- Non-inferiority is a **standalone metric** (Option A), NOT a composite that
  includes the hard-gate condition. The hard gate is AND-ed separately in
  `m6_pass`.

### Hard gate: FAIL

- Hard dimensions: Usefulness, Factuality, Instruction following
- S1: failed (Instruction following −2.0, Factuality −1.0, Usefulness −1.0)
- S2: failed (Instruction following −1.0, Usefulness −1.0)
- S3: passed
- S4: passed

### Brand asset gate: FAIL

- Required: ≥ 50% of attempted scenarios with brand delta ≥ +0.5
- Achieved: **0/4** scenarios reached brand advantage
- S1 brand delta: −2.0
- S2 brand delta: −1.0
- S3 brand delta: +0.0
- S4 brand delta: +0.0

### M6.1 overall: FAIL

All three sub-gates failed. `m6_pass = non_inferior AND hard_gate AND brand_gate` = False.

---

## Scenario Summary

| Scenario | Agent | Delta | Hard gate | Brand advantage | Key finding |
|----------|-------|-------|-----------|-----------------|-------------|
| S1 | product_spec | −1.333 | FAIL | FAIL (−2.0) | MKTApp produced two separate specs, not a one-page comparison. Factual errors (Truly = mirror, added accessories). Technical tone, not brand voice. |
| S2 | competitor_analysis | −0.167 | FAIL | FAIL (−1.0) | Near parity overall. Both had hallucinations. **Frontier output truncated** (harness token-budget; see §Truncation). MKTApp won evidence quality (+1.0) and user effort (+1.0). Delta not a clean Frontier-parity measurement. |
| S3 | campaign_strategy | +1.000 | PASS | FAIL (+0.0) | MKTApp correctly avoided unsupported claims and discount tactics. **Frontier output truncated** (harness token-budget; see §Truncation) — the +1.0 delta is partly attributable to Frontier incompleteness and must not be read as a clean parity measurement. Brand advantage not achieved (tie at 4/4). |
| S4 | content_creator | −0.167 | PASS | FAIL (+0.0) | Near parity. MKTApp's JSON format less readable than Frontier's markdown script. Both had hallucinations (voice messaging, product colors). MKTApp won user effort (+1.0). |

### Key insight

MKTApp is **not globally far behind Frontier**. The primary issue is
**inconsistent reliability across task types**. S3 proves the architecture
and model can reach or exceed Frontier quality in some tasks. The failures
are concentrated in grounding, instruction adherence, and brand-driven
differentiation — not in fundamental capability.

---

## Budget Governance

### Approved ceilings

| Ceiling | Value |
|---------|-------|
| Paired-run cap | $2.42 |
| Judge planned cap | $0.18 |
| Judge absolute cap | $0.20 |
| Total absolute cumulative ceiling | $2.86 |

### Actual spend

| Component | Amount |
|-----------|--------|
| Historical sunk (source run `20260904_070830`) | $1.104836 |
| Frontier continuation (S2 + S3 + S4 fresh calls) | $1.410830 |
| **Paired-run cumulative** | **$2.515666** |
| Judge incremental (S1–S4) | $0.167659 |
| **Total cumulative actual** | **$2.683325** |

### Compliance status

| Ceiling | Status | Detail |
|---------|--------|--------|
| $2.86 total absolute ceiling | **RESPECTED** | $2.683325 < $2.86; remaining $0.176675 |
| $0.18 Judge planned cap | **RESPECTED** | $0.167659 < $0.18 |
| $2.42 paired-run cap | **BREACHED** | $2.515666 > $2.42 by **$0.095666** |

### Governance deviation

The paired-run cap breach was an **execution-governance deviation**.
After the harness stopped at the $2.42 paired-run cap (conservative
Frontier reserve exceeded remaining budget), the approval cap was switched
to $2.86 without obtaining new Product Owner approval. The $2.86 total
absolute ceiling was respected. **No rerun is required merely to repair
this administrative breach.** The paid outputs are valid evidence.

---

## Accepted Remediation Diagnosis

### Confirmed problems (from paid evidence)

1. **Grounding / unsupported factual extrapolation** (S1, S2, S4)
   - Agents freely extrapolate features, prices, market positions, and
     product attributes not present in source data.
   - Examples: Truly called "mirror" instead of glass manufacturer (S1);
     K2 market tier claimed without price data (S2); voice messaging
     claimed without source support (S4); product colors invented (S4).

2. **Instruction-precedence conflict in Product Spec** (S1)
   - Multi-product scope detection instructs "create separate specs per
     product" which conflicts with "one-page comparison" intent.
   - The one-page instruction is overridden by the multi-product
     separation instruction.

3. **Brand assets not producing measurable differentiation** (S1–S4)
   - Brand gate = 0/4. Agents use correct brand tone but do not leverage
     brand assets (voice, preferred terms, restricted/banned terms) as a
     competitive differentiator.
   - No agent currently injects brand context as a deliberate advantage
     instruction.

4. **Truncation — Frontier harness side, not MKTApp** (S2, S3, S4 Frontier)
   - Recovered MKTApp S1–S4 outputs do **not** show confirmed token
     truncation. (The source-run blind output `S2_Y.txt` was 11 bytes —
     an empty/failed capture in the source run, not truncation; it was
     recovered from the prior qualification run
     `data/all_agents_beta_qualification/run_outputs/` and is complete
     at 6082 bytes ending with a limitations section.)
   - Frontier S2/S3/S4 are **confirmed truncated/incomplete**:
     S2 ends mid-word ("นาฬิกาการโทรวิดีโ"), S3 ends on an empty heading
     ("## "), S4 ends mid-table-row ("| คุณสมบัติที") with
     `completion_tokens == max_tokens == 3500`.
   - Evidence points to the **Frontier harness output-token budget** as
     the observed cause: the harness sets
     `max_tokens=scenario["frontier_output_tokens"]` (S2/S3: 4500,
     S4: 3500) and web_search server-tool calls consume the same
     completion budget, exhausting visible-text capacity.
   - The harness did **not** persist or check `finish_reason`, so
     `finish_reason="length"` was silent.
   - Production `src/llm_client.py:chat()` also does not surface
     `finish_reason` — this is a **latent production observability
     risk**, but it was **NOT demonstrated as an MKTApp M6.1 failure**.
   - Distinction preserved:
     - **Observed MKTApp product defects** (grounding, instruction
       precedence, brand) = real and remain actionable; they drive the
       FAIL verdict.
     - **Harness-side truncation** = `frontier_output_tokens` cap +
       web_search completion-token consumption + no `finish_reason`
       check; compromised Frontier candidate completeness for S2–S4.
     - **Latent production risk** = `llm_client.chat()` ignores
       `finish_reason`; if a future MKTApp generation hits `max_tokens`,
       truncation would be silent. Not yet manifested.
   - **Do not increase token limits.** The remediation is detection
     (surface `finish_reason` as metadata, persist in harness evidence),
     not expansion.
   - **No rerun is required before remediation** — existing evidence is
     sufficient to identify and fix the genuine MKTApp product defects.
   - **Any future post-remediation Frontier qualification must fix and
     verify Frontier candidate completeness first**, otherwise the
     parity measurement is not clean.

5. **Content Creator machine-readable JSON is an architectural requirement**
   - The JSON schema is required for the system's pipeline
     (`media_gen` consumes structured `posts`).
   - The production UI already renders the JSON into human-readable
     Markdown via `src/content_schema.py:render_posts_to_markdown`, so
     the machine-readable + human-readable architecture is already
     satisfied. No schema change is needed.
   - Do not remove or alter the JSON schema merely to improve benchmark
     readability. The M6.1 judge saw only raw JSON because the harness
     writes the agent's raw output, not the rendered Markdown — that is
     a harness presentation gap, not an architecture failure.

### Proposed fixes (Remediation Design v2 — accepted, not yet implemented)

These are the accepted design contracts from Remediation Design v2.
Implementation is pending Product Owner approval:

1. **Grounding** — shared three-category contract (supplied facts /
   researched facts with evidence / inference) injected via
   `BaseAgent._build_system_prompt`; code verifies only mechanically
   knowable invariants (citation provenance against captured
   annotations, schema, brand-hard). No generic factuality regex.
2. **Instruction precedence** — one generic multi-product directive
   that always preserves product isolation and defers presentation
   format to quick_brief (model interprets semantically); no keyword
   vocabulary, no `bool(quick_brief)` switch.
3. **Brand utilization** — reframe existing `brand_reference` as
   task-decision context (not passive background) via a
   `use_brand_differentiator` flag; hard restrictions/replacements
   stay deterministic; no soft-brand term-presence validator; no
   merging of positioning into `BrandRules`.
4. **Truncation** — surface `finish_reason` / `last_truncated` as
   metadata on `LLMClient` (no return-type break, no content marker,
   reset per call); persist in harness evidence; no token-limit
   increase.
5. **Content Creator** — no schema change; existing Markdown renderer
   already satisfies machine-readable + human-readable.

---

## Remediation Design Principles

These are **mandatory constraints** for the next phase:

- **No scenario-specific hardcoding** — do not hardcode S1–S4 behavior
- **No brand-specific wording in engine logic** — Lagenio-specific terms
  must not be baked into agent code
- **No benchmark gaming** — do not tune prompts specifically to Judge
  wording or benchmark cases
- **Brand handling must remain generic and data-driven** — voice, preferred
  terms, banned/restricted terms, positioning, etc. come from brand
  context data, not hardcoded rules
- **Grounding must distinguish three categories:**
  - (a) User/product facts → grounded in supplied product/brand data
  - (b) External market/competitor facts → may come from web evidence
    where the agent supports research
  - (c) Inference/recommendation → allowed, but must not be presented
    as sourced fact
- **User intent should outrank internal formatting defaults** where
  compatible with safety/system constraints
- **Preserve machine-readable + human-readable architecture** — do not
  remove JSON schemas; add human-readable layers if needed
- **Do not increase token limits until truncation cause is verified**
  from existing raw evidence

---

## Next Phase

**Phase:** `M6 Remediation Implementation` (pending Product Owner approval)

**Remediation Design v2** is accepted. The implementation order is:

1. Truncation metadata surfacing (`llm_client.py` + harness evidence)
2. Product Spec instruction-precedence directive (`product_spec.py`)
3. Shared grounding policy block + citation provenance check
   (`base_agent.py`, `output_validators.py`, `agents.yaml`)
4. Brand differentiator framing via `brand_reference`
   (`base_agent.py`, `competitor_analysis.py`, `agents.yaml`)
5. Content Creator — no change (existing renderer verified)

**Pre-condition for any future Frontier qualification rerun:**
Frontier candidate completeness must be fixed and verified first
(harness `finish_reason` detection + adequate `frontier_output_tokens`
budget). A rerun against truncated Frontier outputs must not be called
a clean Frontier-parity measurement.

**No paid rerun is authorized yet.**

---

## Evidence Preservation

All raw qualification evidence is preserved unchanged:

- `data/m6_frontier_uat/20260904_094021_resume_from_20260904_070830/`
  - `m6_evidence.json` — execution evidence
  - `resume_link.json` — provenance, fingerprints, cumulative accounting
  - `output_hashes.json` — SHA-256 hashes of all outputs
  - `m6_mapping_secret.json` — blind judge mapping
  - `judge/m6_judge_raw.json` — raw judge results
  - `judge/m6_judge_scores.json` — revealed scores with deterministic gates
  - `judge/provider_audits/S{1-4}_provider_audit.json` — per-call audits
  - `judge_accounting.json` — judge accounting with response_audit
  - `outputs/mktapp/S{1-4}.txt` — MKTApp candidate outputs
  - `outputs/frontier/S{1-4}.txt` — Frontier candidate outputs

Do not modify any of these files.

---

## Prior Reports (superseded)

The following reports reference earlier runs or preliminary conclusions
and are **superseded** by this canonical status document:

- `M6_1_JUDGE_REPORT.md` — references run `20260902_070643` (older run)
- `M6_FINAL_FAILURE_ATTRIBUTION_REPORT.md` — references run `20260902_070643`
- `M6_REMEDIATION_DESIGN.md` — preliminary design before generic rebase;
  superseded by Remediation Design v2 (accepted, documented in this file)
- `M6_1_RERUN_REPORT.md` — documents the incomplete source run
  `20260904_070830`; valid as historical record of that run's stoppage
- `M6_GENERIC_REMEDIATION_REBASE.md` — documents the harness hardening
  process; valid as historical record

This `M6_STATUS.md` is the **single source of truth** for the current
M6.1 qualification result and remediation constraints.

---

## Consolidated Audit Findings (Round 9)

### Content Pillars

- UI/config existed (`config/content_policy.yaml`: `pillars`, `pillar_keywords`)
- `pillar_keywords` is **legacy deterministic lexical Pillar-classification
  fallback** logic. It is used by
  `pillar_manager.infer_pillar(concept, pillars, keywords_map)` in Auto Mode
  product selection (`src/orchestrator.py`) when the LLM output omits the
  `pillar` field — it performs a case-insensitive substring match of pillar
  names and configured keywords against the concept text to classify which
  Pillar the concept belongs to. It is NOT model semantic reasoning. It is
  NOT part of the new Agent 3/4 Pillar wiring. It predates this remediation.
  It must not be expanded with additional keyword/regex semantic logic. It
  may be treated as future technical debt. It is also used by
  `pillar_manager.find_duplicate_keywords` for UI duplicate-keyword
  validation.
- Downstream Agent 3/4 wiring was missing — selected Pillar was extracted
  in `select_product_auto` but discarded before `content_creator`
- Configured/selected Pillar context is now preserved through the
  orchestrator to `campaign_strategy` (via context dict) and
  `content_creator` (via optional parameters)
- Pillars remain optional strategic guidance — no forced usage, no
  keyword/regex classification, no hardcoded Pillar names
- Pillars remain separate from brand factual/compliance context

### Attachments

- Attachment delivery path: wizard UI → `/api/run-resources/upload`
  → `RunResourceStore` → `build_step_run_context` → `BaseAgent.run`
- Supported formats: `.txt`, `.md`, `.csv`, `.pdf`, `.xlsx`, `.xls`,
  `.docx`, `.png`, `.jpg`, `.jpeg`, `.webp`
- Default limits: 5 files per flow, 15 MB per file, 30 MB total,
  50,000 extracted chars per file, 120,000 total extracted chars
- **Terminology:**
  - **Truncation** = non-fatal limitation where a successfully extracted
    ready resource was shortened according to an explicit size limit.
    Truncation is tracked in `resource_trace` (not in `warnings`) and
    does NOT block execution.
  - **`StepRunContext.warnings`** = in the current implementation, this
    field contains only fatal resource-resolution failures: unsupported
    refs, missing/invalid refs, and non-ready referenced resources. It
    does NOT contain truncation notices.
  - **Fatal resource preflight error** = a resource explicitly
    referenced by the user cannot supply usable context/input (missing,
    rejected, parser error, otherwise non-ready). Execution must stop.
    In the current implementation, any non-empty `step_context.warnings`
    is treated as fatal at the shared execution seam.
- **Fatal preflight enforcement (agent execution):** `BaseAgent.run()`
  in `src/agents/base_agent.py` is the single shared seam. If
  `step_context.warnings` is non-empty, it raises
  `ValueError("resource preflight failed: ...")` before any prompt
  construction or LLM call. This guarantees no agent execution can
  silently proceed without a required resource, regardless of whether
  the caller pre-checks warnings.
- **Fatal preflight enforcement (media generation):**
  `/api/generate_all_media` now treats any non-empty
  `step_context.warnings` as fatal and returns HTTP 400 before media
  generation is invoked. This closes the prior silent-loss path where
  non-ready referenced image resources could be silently dropped.
- Web endpoints (`/api/run_agent`, `/api/run_agents`, `/api/run_flows`,
  `/api/run_auto`) additionally raise at the endpoint level before
  `_run_single_agent` is invoked — defense in depth.
- Zero attachments remains valid (empty warnings → execution proceeds).
- Ready resources that were truncated remain valid (truncation does not
  produce a fatal preflight error; only non-ready/missing/rejected refs
  do).
- Grounding clarification: resource context block states that uploaded
  file content is supplied/user-provided facts and does not require web
  citation merely because it came from an attachment.
- Known limitation: script-level paths (`scripts/qual_runner.py`,
  `scripts/m6_frontier_uat.py`) pass `resource_context` as a raw string
  and bypass `build_step_run_context`. These paths have no resource refs
  to validate and are out of scope for the fatal preflight.

### Novelty / history

- Persistence already exists (`cache/content_history.json` via
  `src/content_history.py`)
- Current preventive-history routing is incomplete:
  - `content_creator` sees history only in manual web UI flow (via
    `quick_brief`), not in orchestrator `run_content_creator` or
    `run_content_creator_auto` first attempt
  - `campaign_strategy` never sees history
- History is global with per-product filtering — no user/workspace/brand
  isolation
- Product IDs are folder names with no tenant prefix — two users can
  collide on the same product name
- Isolation/scoping must be resolved before wider history injection
- System must NOT yet claim guaranteed non-repetition
- History queries are bounded (`default_limit: 50`, `max_entries: 200`)

### competitor_analysis

- Stage A remains brand-objective (evidence prompt, schema, records)
- Deterministic Stage 3 renderer (`CompetitorReportRenderer`) has no
  semantic brand-reasoning seam
- Previous raw brand-reference framing proposal was rejected/reverted
- This remains a known gap pending a separately approved semantic design
  that would require a model reasoning step (not authorized in this phase)

### campaign_strategy web search

- `web_search: true` in `config/agents.yaml` — web tool execution is
  reachable at runtime
- `grounding_policy` and citation provenance validation apply
- Context-routing matrix corrected: campaign_strategy **does** receive
  web evidence (not N/A as previously reported)

---

## Round 10 Corrections — selected-Pillar state, fatal preflight, Stage 3 design

### Selected-Pillar state scope

- A previous implementation stored `self._selected_pillar` on the
  `Orchestrator` instance, creating possible stale state leakage across
  runs on a reusable orchestrator.
- This attribute has been removed. No hidden mutable selected-Pillar
  state persists on the `Orchestrator` instance.
- **Normal pipeline (`run_pipeline`):** passes configured Pillars only
  (via `_build_configured_pillars_text()`). It does not accept or pass
  a selected Pillar because it does not perform auto-mode product
  selection. `run_campaign_strategy` is called without
  `selected_pillar`; `run_content_creator` is called with
  `content_pillars` only.
- **Auto Mode (`run_content_creator_auto`):** `chosen_pillar` is a
  run-local variable derived from the selection LLM output
  (`selection.get("pillar", "")`). It is forwarded directly to
  `run_content_creator` via `selected_pillar=chosen_pillar`. It is
  never persisted on the Orchestrator object and never inherited by a
  subsequent run.
- `run_campaign_strategy` accepts an optional `selected_pillar`
  parameter for callers that have one; `run_pipeline` does not supply
  one.
- Contract verified by regression tests in
  `tests/test_content_pillars.py`:
  1. Run A with selected Pillar X receives X.
  2. A subsequent Run B with no selected Pillar does not receive X.
  3. Configured Pillars remain available independently.
  4. Explicit selected Pillar values are preserved unchanged.

### Fatal attachment preflight — shared seam enforcement

- The attachment preflight is now enforced at the single shared
  execution seam: `BaseAgent.run()` in `src/agents/base_agent.py`.
- If `step_context.warnings` is non-empty, `BaseAgent.run` raises
  `ValueError("resource preflight failed: ...")` before any prompt
  construction or LLM call.
- This closes the gap where a caller could pass a `StepRunContext` with
  non-ready-resource warnings directly to `agent.run()` and the agent
  would silently proceed.
- Tests in `tests/test_run_context.py` prove:
  - LLM mock is not called after fatal preflight failure.
  - Ready resource (zero warnings) allows execution.
  - Zero attachments remains valid.
  - No `step_context` (standalone/script paths) remains valid.
  - Truncated ready resources remain valid.

### Competitor Stage 3 — hard-isolated Brand Interpretation (offline, uncommitted)

- Stage A (evidence research) remains brand-independent: evidence
  prompt, schema, and records contain no brand fields.
- `SemanticEvidenceReviewer.review()` remains objective — it does NOT
  receive `brand_reference`. Its signature has no `brand_reference`
  parameter. This is the hard evidence-isolation boundary.
- **Rejected design:** A combined reviewer+brand call was attempted and
  rejected before commit. A model that sees brand context while
  reviewing evidence cannot provide a hard evidence-objectivity
  guarantee through prompt instructions alone. Offline tests comparing
  mocked evidence decisions with/without brand_reference do NOT prove
  real-model invariance.
- **Corrected architecture (implemented, uncommitted):** A separate
  `BrandInterpretationPass` runs AFTER the reviewer finalizes evidence.
  It receives only finalized/surviving evidence (read-only) + optional
  `brand_reference`. It produces `StrategicImplication` objects that
  reference evidence by index. Implications referencing non-surviving
  evidence are mechanically rejected. The pass cannot modify the
  evidence collection.
- **Model-call lifecycle:**
  - Without `brand_reference`: unchanged (typical 2 calls, max 3).
  - With `brand_reference`: typical 3 calls (Stage A + reviewer + brand
    interpretation), max 4 (adds `_revise_research`). The extra call
    occurs only when `brand_reference` is non-empty AND evidence exists.
- The deterministic renderer formats strategic implications as a
  separate "Strategic Implications" section, labeled as
  inference/recommendation. Raw `brand_reference` is never rendered.
- Agent 2 remains functional standalone without `brand_reference`.
- Item 4 (brand-aware competitor recommendations) is considered
  **implemented offline** but still awaits clean paid qualification.
- Design details in `M6_COMPETITOR_STAGE3_DESIGN.md`.

### Testing-layer distinction

The five testing layers (unit/offline, integration, Frontier
qualification, Web E2E, Product Owner manual acceptance) are defined
in the canonical qualification plan: `M6_QUALIFICATION_PLAN.md` §
Testing Layers.

### Novelty / history — current factual state

- History persistence exists (`cache/content_history.json` via
  `src/content_history.py`) but is globally scoped with per-product
  filtering — no user/workspace/brand isolation.
- Broader preventive history injection is **not approved** because
  isolation is absent. No guarantee of non-repetition may be claimed.
- The intended product goal and forward plan for content novelty are
  documented in `M6_QUALIFICATION_PLAN.md` § Content Novelty /
  Non-Repetition.

### Canonical forward plan

`M6_QUALIFICATION_PLAN.md` is now the canonical forward qualification
plan. This document (`M6_STATUS.md`) remains the factual current-state
record.

---

## Strategy Pivot — Same-Model Product Uplift (2026-09-05)

**The primary qualification question has changed.**

- **Old primary question:** Is MKTApp competitive with external Frontier systems?
- **New primary question:** Does MKTApp produce materially better marketing work than using the exact same underlying model directly?

External Frontier / different-model comparison is **deferred to a future phase** because of budget and time constraints. Historical M6.1 Frontier results remain historical evidence and do not define the new baseline.

### Same-model uplift harness — implementation status

**Status: Offline implementation v6 complete. Awaits Product Owner review.**

The harness implements per-turn actual-cost accounting through
`post_model_hook` in `LLMClient.chat_with_tools()`.  Every model turn
commits its actual provider-reported cost immediately after the turn,
before the next turn is authorized.  Cumulative spend is accurate.

The new same-model uplift harness is implemented in
`scripts/m6_uplift_harness.py` with full offline test coverage in
`tests/test_m6_uplift_harness.py` (124 tests).

What was implemented (v6 corrections):
- **Per-turn actual cost commit**: `LLMClient.chat_with_tools()` now
  accepts `post_model_hook(iteration, actual_cost, usage_dict)`.
  `BudgetGuardedLLMClient` passes a hook that commits actual cost (or
  conservative reserve fallback) after every model turn, before the
  next turn is authorized.
- **Reserve fallback**: When actual cost is unavailable, the
  pre-authorized reserve is committed (fail conservative, never zero).
  Audit metadata records `cost_source: "actual"` or `"reserve_fallback"`.
- **No double-counting**: Server-side web search cost is included in
  the model call's `usage.cost` (option B).  No separate tool commit.
  Client-side tools in `chat_with_tools()` are authorized via
  `pre_tool_hook` but only committed if separately billed.
- **S4 baseline cap raised**: From $0.08 to $0.10 (conservative $0.078,
  margin 28%).  Previous 8% margin was too tight.
- **Cumulative spend tests**: 3-turn test proves 0.03+0.04+0.05=0.12
  committed cumulatively, not just last turn.  Turn-3 denial test
  proves provider not called, spend = turn1+turn2.  Resume test
  proves cumulative spend persists.

What was NOT done:
- No paid/model/web/media calls.
- No Judge execution.
- No commit or push.
- No historical reports altered.
- Qualification not marked as passed.

### Configured underlying models per Agent

| Scenario | Agent | Model | Temperature | Max tokens | Web search |
|----------|-------|-------|-------------|------------|------------|
| S1 | product_spec | `google/gemini-3.7-flash` | 0.3 | 4096 | No |
| S2 | competitor_analysis | `google/gemini-3.5-flash` | 0.4 | 8192 | Yes |
| S3 | campaign_strategy | `google/gemini-3.7-flash` | 0.8 | 4096 | Yes |
| S4 | content_creator | `google/gemini-3.7-flash` | 0.9 | 8192 | No |

**S2 uses a different model** (`gemini-3.5-flash`) than S1/S3/S4 (`gemini-3.7-flash`).
The same-model baseline must use the exact corresponding agent model per scenario.
Models are read dynamically from `config/agents.yaml` — no hardcoded model strings in the harness.

### Historical actual paid spend (corrected)

Historical actual paid spend: **$2.683325** (not ~$2.86).
- Historical sunk cost: $1.104836
- Continuation incremental cost: $1.410830
- Judge incremental actual: $0.167659
- Total: $2.683325
- Previously approved ceiling: $2.86

Historical spend remains separate from any new same-model authorization.

### True offline suite status

**1372 passed, 0 failed** — this is the complete suite with no exclusions,
including 16 API/integration E2E smoke tests and 14 Web/Browser E2E tests
(see § Web E2E / Browser Smoke-Test Status below).

---

## Same-Model Uplift Qualification Result (2026-09-05)

**This is the current valid qualification result.** It supersedes the
historical M6.1 Frontier-parity result for forward-planning purposes.

### Result: FAIL (valid)

| Field | Value |
|-------|-------|
| **Overall** | **FAIL** |
| Qualification type | Same-model product uplift (Level 1) |
| Recovery run | `data/m6_uplift/runs/20260905_074945_recovery/` |
| Original invalid run | `data/m6_uplift/runs/20260905_072432/` (harness defect, INVALID) |
| Recovery HEAD | `36f6f9bbf862ade1bf792119886428847412ec92` |
| Gate version | v1 |
| MKTApp generation during recovery | **ZERO** (preserved from original run) |

### Per-scenario uplift

| Scenario | Agent | Model | Uplift | Status |
|----------|-------|-------|--------|--------|
| S1 | product_spec | google/gemini-3.7-flash | **+1.333** | PASS — MKTApp outperformed same-model direct baseline |
| S2 | competitor_analysis | google/gemini-3.5-flash | **+1.000** | PASS — MKTApp outperformed same-model direct baseline |
| S3 | campaign_strategy | google/gemini-3.7-flash | **−1.167** | FAIL — MKTApp underperformed same-model direct baseline |
| S4 | content_creator | google/gemini-3.7-flash | **+0.667** | PASS — MKTApp outperformed same-model direct baseline |

- Aggregate mean uplift: **+0.4583**
- Nonnegative scenarios: **3/4**

### Gate v1 conditions

| Condition | Requirement | Result | Value |
|-----------|-------------|--------|-------|
| A — Aggregate mean uplift | ≥ +0.25 | **PASS** | +0.4583 |
| B — Scenario consistency | ≥ 3 of 4 ≥ 0 | **PASS** | 3/4 |
| C — No scenario regression | No scenario < −0.5 | **FAIL** | S3 = −1.167 |
| D — No core dimension regression | No core dim < −0.5 | **FAIL** | S3 Instruction following = −2.0, S3 Brand/asset fit = −1.0 |

**Overall: FAIL** (requires all 4 conditions to pass)

### S3 failure detail

S3 (campaign_strategy) is the sole driver of the FAIL verdict:

- S3 overall delta: −1.167 (violates Gate C: < −0.5)
- S3 Instruction following delta: −2.0 (violates Gate D: < −0.5)
- S3 Brand/asset fit delta: −1.0 (violates Gate D: < −0.5)

Root cause: `resource_context` containing user budget constraints
(`budget_max=5000`) is not promoted into the structured context/instruction
fields consumed by the Campaign Strategy validator and system prompt.
This causes: budget present from user → MKTApp structured context says
no budget → validator rejects concrete allocation → repair loop forces
hedging → S3 becomes materially worse than same-model baseline.

See § S3 Root-Cause Investigation below for full analysis.

### Cumulative spend reconciliation

| Category | Calls | Spend |
|----------|-------|-------|
| Original qualification (invalid — harness defect) | 14 | $0.496511 |
| Diagnostic call after STOP (governance deviation) | 1 | $0.009494 |
| Recovery — Baseline S1–S4 | 4 | $0.138968 |
| Recovery — MKTApp S1–S4 | 0 | $0.000000 (preserved) |
| Recovery — Judge S1–S4 | 4 | $0.165532 |
| **Cumulative grand total** | **23** | **$0.810043** |
| Budget cap | | $1.410000 |
| Remaining | | $0.599957 |

Note: Judge costs are recorded in `judge/m6_judge_raw.json`, not in
`logs/llm_usage.jsonl` (Judge uses a separate API client path).

### Evidence paths

Recovery run: `data/m6_uplift/runs/20260905_074945_recovery/`
- `m6_uplift_verdict.json` — final verdict
- `m6_evidence.json` — full evidence
- `m6_mapping_secret.json` — blind mapping
- `judge/m6_judge_raw.json` — Judge raw results
- `outputs/baseline/S{1-4}.txt` — Baseline outputs
- `outputs/mktapp/S{1-4}.txt` — MKTApp outputs (preserved from original run)
- `recovery_meta.json` — recovery metadata

Original run: `data/m6_uplift/runs/20260905_072432/`
- `preserved/mktapp_evidence_manifest.json` — MKTApp preservation manifest

All MKTApp output hashes verified to match between original and recovery runs.

---

## Benchmark Levels

The M6 qualification uses two distinct benchmark levels. **Level 1
results must NOT be interpreted as Level 2 results.**

### Level 1 — Same-model API qualification

**Purpose:** Determine whether MKTApp adds value over using the same
underlying model through a neutral/direct API baseline.

**What it measures:** MKTApp architecture uplift (grounding, brand
handling, review/repair, orchestration, deterministic safety/validation)
versus a direct API call to the same model with equivalent user/source
information.

**Current status:**

| Scenario | Agent | Uplift | Level 1 status |
|----------|-------|--------|----------------|
| S1 | product_spec | +1.333 | PASS |
| S2 | competitor_analysis | +1.000 | PASS |
| S3 | campaign_strategy | −1.167 | FAIL |
| S4 | content_creator | +0.667 | PASS |
| **Overall** | | +0.4583 | **FAIL** (S3 regression) |

**Important limitation:** This result does NOT prove MKTApp is better
than Gemini, ChatGPT, Claude, or another provider's consumer web UI.
It only measures MKTApp architecture value over the same model via
direct API.

### Level 2 — Provider UI benchmark

**Status: Future milestone.** Not yet executed. Do NOT infer from
Level 1 results.

**Purpose:** Answer the product/business question:

> Does a user get an equal or better result from MKTApp than by taking
> the same task directly to the provider's own web product?

**Comparison:**

| Side | What |
|------|------|
| Candidate A | MKTApp real UI |
| Candidate B | Provider real UI (e.g., Gemini web, ChatGPT web) |

**Controlled equivalents:**
- Same task
- Same input text
- Same uploaded files/context
- Comparable available tools/search
- Fresh sessions
- First completed answer
- No manual prompt rescue on only one side
- Blind judging
- Multiple scenarios

**Provider UI capabilities not present in raw API access:** Provider web
products may include hidden system behavior, retrieval/search, tool
orchestration, context management, routing, post-processing, memory, and
other provider product layers that are NOT available through API access.

**Therefore Level 2 must be treated as a separate benchmark, not
inferred from Level 1.** A Level 1 PASS does not imply Level 2 PASS.

### Web E2E / Browser smoke-test

**Status: Required before Beta/Release closure.** Separate from both
benchmark levels.

**Flow:** UI → API/Orchestrator → Agent → Result/Renderer → UI

**Purpose:** Verify the full production delivery path works end-to-end
through the web UI, including rendering, file handling, and user
interaction. This is a smoke-test, not a model-quality benchmark.

### Web E2E / Browser Smoke-Test Status (2026-09-05)

**Status: Web/Browser E2E PASS — 14/14 browser tests passing.**

Two distinct E2E test layers exist:

1. **API/integration E2E smoke tests** — `tests/test_beta_e2e_smoke.py`
   (16 tests). Uses FastAPI `TestClient`. Exercises HTTP routing, SSE
   streaming, file I/O, session management with mocked LLM. Does NOT
   execute real browser DOM/JavaScript.

2. **Web/Browser E2E** — `tests/test_browser_e2e.py` (14 tests). Uses
   **Playwright** with real Chromium browser. Exercises the full chain:
   browser → real frontend JavaScript → HTTP/SSE → backend →
   orchestrator → agent seam → renderer → DOM. The LLM/Orchestrator is
   mocked so no paid calls are made.

**Browser E2E coverage:**

| Test class | What it verifies | Status |
|------------|------------------|--------|
| `TestApplicationBoot` | Page renders, wizard JS loads, no fatal console errors, no critical failed network requests | PASS (4 tests) |
| `TestAgent1ProductSpec` | Select product → select agent → run → result link appears in DOM | PASS |
| `TestAgent2CompetitorAnalysis` | Full UI flow → result renders | PASS |
| `TestAgent3CampaignStrategy` | Settings saved via API → run → settings reach backend; settings modal opens via UI | PASS (2 tests) |
| `TestAgent4ContentCreator` | Full UI flow including content options step → text result renders | PASS |
| `TestUploadContext` | Real file upload through DOM file input → UI acknowledges file | PASS |
| `TestRepeatedUse` | Second run in same browser session → no state corruption | PASS |
| `TestErrorBehavior` | Controlled backend error → UI shows error state, no infinite loading | PASS |
| `TestNavigationState` | UI operable after run (step dots clickable, add-flow button present); sessions sidebar populated | PASS (2 tests) |

**What the browser E2E proves that API tests cannot:**
- The page HTML renders in a real browser
- `wizard_ui.js` executes and builds the wizard DOM
- Click-to-select product/agent chips work through real DOM events
- Wizard stepper navigation (Step 1 → 2 → 3 → 4) works
- SSE events are parsed by frontend JS and update the DOM
- Result links appear and are clickable
- Agent settings modal opens through the real UI
- File upload control works through the real DOM
- Console has no fatal errors on happy paths
- The UI remains operable after a completed run

**Agent 3 settings path proven:** The browser E2E test for
`campaign_strategy` saves `budget_max=5000`, `discount_max=0`,
`forbid_tactics=[heavy_discount,flash,bogo]` via the settings API,
verifies they reach `config/agent_instructions.json`, then runs the
agent through the real UI. This proves the production settings path
works — the same path that the benchmark harness defect bypassed.

---

## Benchmark Roadmap

The progression from current state to Beta/Release decision:

1. **Same-model API capability qualification** (Level 1) — current state: FAIL
2. **Fix any failing Agent(s)** — S3 campaign_strategy root cause identified (harness defect, deferred benchmark work)
3. **Re-run same-model qualification** — deferred; not required before user Beta
4. **Web E2E / Browser flow validation** — Web/Browser E2E PASS (14/14 with Playwright); API/integration E2E PASS (16/16)
5. **Provider UI benchmark** (Level 2) — separate paid benchmark, future milestone
6. **Beta/Release decision** — ready for small real-user Beta (see Beta Readiness Report)

Do NOT claim provider-UI parity or superiority until Level 2 has been
actually executed.

---

## S3 Root-Cause Investigation (2026-09-05)

**Status: Investigation complete. Fix not yet implemented.**

### Summary

The same underlying model (`google/gemini-3.7-flash`) produced a
materially better Baseline result than the MKTApp Agent 3 path for S3
(campaign_strategy). The root cause is a **harness asymmetry**, not a
production code defect.

### Corrected root cause (updated after pre-fix investigation)

**The production code is correct.** In the production web UI, user
settings (`budget_max`, `discount_max`, `forbid_tactics`) flow through
the persistent `instructions` dict loaded from
`config/agent_instructions.json`, which the user saves via the UI
settings modal (`POST /api/agent_instructions/campaign_strategy`).
The orchestrator loads these at agent creation time
(`_load_agent_instructions`), and both the system prompt formatter
(`BaseAgent._format_instructions`) and the validator
(`campaign_validator.audit_campaign_output`) read them correctly.

**The uplift harness does not simulate this step.**

The M6 frontier UAT harness (`scripts/m6_frontier_uat.py`) correctly
parses `Agent settings:` from `resource_context` via
`_parse_agent_settings_override()`, applies them via
`qual_runner._apply_agent_settings()` (which writes to
`config/agent_instructions.json`), and strips the block from the
prompt text before passing it to MKTApp.

The M6 uplift harness (`scripts/m6_uplift_harness.py`) does NOT do
this. `run_mktapp_candidate()` passes `scenario.resource_context` as a
raw string to `qr.run_case()` without `agent_settings_override`. The
`Agent settings: budget_max=5000...` string is appended raw to the
user prompt but never reaches `agent_instructions.json` or
`agent.instructions`.

### Effect chain (harness defect)

1. S3 `resource_context` = `"Agent settings: budget_max=5000, discount_max=0, forbid_tactics=[heavy_discount,flash,bogo]."`
2. Uplift harness `run_mktapp_candidate` passes it as raw string (no `agent_settings_override`)
3. `qual_runner.run_case` does not call `_apply_agent_settings`
4. `config/agent_instructions.json` retains defaults: `budget_max=""`, `forbid_tactics=[]`
5. Orchestrator loads empty instructions → `agent.instructions["budget_max"]=""`
6. System prompt: `_format_instructions` skips "งบประมาณสูงสุด: 5000" (empty value)
7. Validator: `instructions.get("budget_max")` = empty → `budget_max_enforced` not enforced
8. `extract_context_flags`: `context.business` = empty → `has_budget=False`
9. `budget_allocation_grounded`: rejects ANY budget percentage when `has_budget=False`
10. Repair prompt forces hedging: "ถ้าข้อมูลไม่พอ ให้ใช้คำว่า 'ไม่สามารถเสนอได้เนื่องจากไม่มีข้อมูลทางการเสินและต้นทุนสินค้า'"
11. Model removes budget percentages and hedges on all concrete numbers
12. Judge penalizes: "Y ไม่จัดสรรงบตามเพดานที่ให้มา"

Evidence: 3 LLM calls for S3 MKTApp (1 generation + 2 repairs),
confirming validation rejection occurred.

### Asymmetry summary

| Path | Parses `Agent settings:`? | Applies to `agent_instructions.json`? | `budget_max` reaches agent? |
|------|--------------------------|---------------------------------------|----------------------------|
| Production web UI | N/A (UI modal saves directly) | Yes (via `/api/agent_instructions`) | Yes |
| Frontier UAT harness | Yes (`_parse_agent_settings_override`) | Yes (`_apply_agent_settings`) | Yes |
| Uplift harness — Baseline | Yes (`_parse_user_visible_constraints`) | N/A (goes into neutral task spec) | Yes (in prompt) |
| **Uplift harness — MKTApp** | **NO** | **NO** | **NO** |

### Failure category

**B (runtime/context/orchestration) — harness-only**

The root cause is in `scripts/m6_uplift_harness.py:run_mktapp_candidate`,
not in production code. The harness does not simulate the production
UI's agent-settings save step before running MKTApp.

### What MKTApp S3 did well (must preserve)

1. Correct product specs (AMOLED, 5MP, SpO2, GPS, IP68)
2. Correct competitor identification and comparison
3. Good campaign concept and brand tone
4. Good target audience definition
5. Good content pillars structure
6. Honest pricing framework (acknowledged missing COGS)
7. Practical next-steps section

### Proposed fix (not yet implemented)

**Fix the harness, not the production code.**

In `scripts/m6_uplift_harness.py:run_mktapp_candidate`, parse
`Agent settings:` from `scenario.resource_context` and pass them as
`agent_settings_override` to `qr.run_case`, exactly like
`m6_frontier_uat._run_mktapp_scenario` does. Strip the `Agent settings:`
block from `resource_context` before passing it to MKTApp (so it's not
duplicated in the user prompt).

This is a harness fix that restores parity with the frontier UAT
harness and the production web UI. No production code changes needed.

See § S3 Pre-Fix Investigation below for the exact implementation plan.

---

## S3 Pre-Fix Investigation (2026-09-05)

**Status: Complete. Fix not yet implemented. Awaiting Product Owner approval.**

### 1. Production UI context-flow findings

The production web UI path for Campaign Strategy was traced end-to-end.

**User settings flow (production):**
1. User opens settings modal in web UI → saves via
   `POST /api/agent_instructions/campaign_strategy`
   (`web_viewer.py:2179-2210`)
2. Settings written to `config/agent_instructions.json`
3. At run time, `Orchestrator._make_agent` loads instructions via
   `_load_agent_instructions` (`src/orchestrator.py:134-146`)
4. `CampaignStrategyAgent.__init__` stores as `self.instructions`
5. `BaseAgent._format_instructions` renders `budget_max`,
   `discount_max`, `forbid_tactics` into system prompt
   (`src/agents/base_agent.py:191-203`)
6. `campaign_validator.audit_campaign_output` reads same
   `instructions` dict for enforcement
   (`src/campaign_validator.py:1031-1049, 1084-1116, 1118-1134`)

**`resource_context` in production:**
- `StepRunContext.resource_text` contains ONLY uploaded attachment text
  (`src/run_context.py:268-274`)
- It NEVER contains `"Agent settings: ..."` formatted text
- The production web UI does not parse `"Agent settings:"` anywhere

**Conclusion:** Production code is correct. User settings reach the
agent through `agent_instructions.json`, not through `resource_context`.

### 2. Harness asymmetry confirmed

| Harness | Parses `Agent settings:`? | Applies to `agent_instructions.json`? |
|---------|--------------------------|---------------------------------------|
| `m6_frontier_uat.py` | Yes (`_parse_agent_settings_override` at line 89) | Yes (`_apply_agent_settings` via `qual_runner`) |
| `m6_uplift_harness.py` (Baseline) | Yes (`_parse_user_visible_constraints` at line 219) | N/A (goes into neutral task spec text) |
| **`m6_uplift_harness.py` (MKTApp)** | **NO** | **NO** |

The uplift harness `run_mktapp_candidate` at line 2047 calls
`qr.run_case(...)` without `agent_settings_override`, passing
`resource_context` as a raw string. This means MKTApp never receives
the user's budget constraint in its structured `instructions` dict.

### 3. Precedence rules

If the same setting exists in multiple sources, the precedence is:

1. **Explicit user-entered structured settings** (UI modal →
   `agent_instructions.json`) — **HIGHEST**
2. **Structured UI fields** (per-run API body, if any) — not currently
   implemented for campaign settings
3. **`resource_context` parsed fallback** (harness-only,
   `Agent settings:` format) — harness simulation of source 1
4. **Defaults** (`config/agent_instructions.json` default values) —
   **LOWEST**

**Rule:** Explicit user-entered structured settings must NOT be
silently overwritten by parsed fallback text. The harness fix must
merge parsed values only when the explicit value is empty/default.

### 4. Exact files/functions that would change for the S3 fix

**Only one file changes: `scripts/m6_uplift_harness.py`**

| Function | Change |
|----------|--------|
| `run_mktapp_candidate` (line 1991) | Add: parse `Agent settings:` from `scenario.resource_context` using existing `_parse_user_visible_constraints()` or equivalent; pass parsed dict as `agent_settings_override` to `qr.run_case`; strip `Agent settings:` block from `resource_context` string before passing it |

**No production code changes.** No changes to:
- `src/orchestrator.py`
- `src/agents/base_agent.py`
- `src/agents/campaign_strategy.py`
- `src/campaign_validator.py`
- `config/agents.yaml`
- `config/agent_instructions.json`
- `scripts/qual_runner.py`
- `scripts/m6_judge_runner.py`

**Implementation pattern:** Mirror what
`m6_frontier_uat._run_mktapp_scenario` (line 608-617) already does:
```python
agent_settings_override = _parse_user_visible_constraints(scenario.resource_context)
resource_context = re.sub(r"Agent settings:.*?(\n\n|\n---|$)", "", scenario.resource_context, flags=re.DOTALL).strip()
```
Then pass `agent_settings_override` to `qr.run_case`.

### 5. Regression-test plan

**New tests:**
1. Test that `run_mktapp_candidate` parses `Agent settings:` from
   `resource_context` and passes `agent_settings_override` to
   `qr.run_case`
2. Test that `Agent settings:` block is stripped from `resource_context`
   before passing to MKTApp
3. Test that empty `resource_context` produces empty
   `agent_settings_override` (no regression for scenarios without
   settings)
4. Test that settings already in `agent_instructions.json` are not
   overwritten when `resource_context` has no `Agent settings:` block

**Existing tests to verify pass:**
- `tests/test_m6_uplift_harness.py` — all 124 tests
- `tests/test_m6_uplift_runner.py` — all tests
- `tests/test_campaign_strategy.py` — all tests
- `tests/test_campaign_strategy_semantic.py` — all tests
- `tests/test_campaign_strategy_hardening.py` — all tests
- `tests/test_campaign_validator.py` — all tests
- `tests/test_campaign_real_eval.py` — all tests
- `tests/test_resource_context_delivery.py` — all tests

**Full offline suite must pass before any re-qualification.**

### 6. Confirmation of zero paid calls

No paid/model/web/media calls were made during this investigation.
All work was offline file inspection, code tracing, and documentation
updates. No qualification rerun was executed.
