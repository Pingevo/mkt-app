# M6 Qualification Plan — Forward Plan

**Purpose:** Describe the CURRENT forward qualification plan for MKTApp
Agent 1–4. This is the canonical source of truth for the plan we are
now following, not historical evidence.

**Status:** Active. Supersedes prior M6 remediation designs for
forward-planning purposes.

**Strategy pivot (2026-09-05):** The primary qualification question has
changed from external Frontier comparison to **same-model product uplift**.
External Frontier / different-model comparison is deferred to a future
phase. See `M6_STATUS.md` § Strategy Pivot for details.

**Historical evidence:** M6 run reports, judge reports, and UAT plans
remain unchanged as audit-trail evidence. See `M6_STATUS.md` for the
factual current state.

---

## Objective

Qualify the current MKTApp Agent 1–4 implementation by measuring
**product uplift**: does MKTApp produce materially better marketing work
than using the exact same underlying model directly?

The primary metric is:

> MKTApp score − Same-model direct baseline score

This is **product uplift** — the value created by MKTApp's architecture,
grounding, brand handling, review/repair, orchestration, and
deterministic safety/validation on top of the same underlying model.

External different-model Frontier comparison is deferred to a future
market-competitiveness benchmark after same-model uplift is established.

---

## Phase 1 — Backend/Product Integrity

Complete known generic defects before spending on another qualification run.

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

## Phase 2 — Same-Model Harness Readiness

Before a new paid uplift comparison:

- Keep core S1–S4 scenarios.
- Keep the established quality/Judge rubric unless a real methodology
  defect is separately approved.
- **Same-model baseline**: Candidate A uses the exact same underlying
  model as the corresponding MKTApp Agent (see model table in
  `M6_STATUS.md`).
- **Information parity**: both candidates receive equivalent user/source
  data (product facts, quick_brief, brand info, images, Content Pillars).
  Private MKTApp machinery (system prompts, validators, repair prompts,
  orchestration) is NOT leaked to the baseline.
- **Tool parity**: equivalent web-search capability for scenarios
  requiring it (S2, S3). Unresolved capability mismatch invalidates the
  scenario.
- Capture `finish_reason` for every candidate call.
- Detect truncation explicitly.
- Candidate completeness is a prerequisite for judging — incomplete
  candidates must not be scored as though they were complete.
- MKTApp candidate completeness must also be checked.
- Exact Product Owner-approved paid caps must be enforced.
- The harness must not reinterpret a larger cumulative ceiling as
  permission to exceed a smaller run-specific cap.
- **S4 presentation**: Judge sees production-equivalent rendered
  markdown, not raw JSON. Raw JSON preserved as audit artifact.
- **Blind Judge**: Judge sees only X/Y labels, not which is MKTApp vs
  baseline. Judge model may differ from candidate models.

No benchmark-specific answer keys.
No hardcoding expected baseline wording.

---

## Phase 3 — Same-Model Paid Uplift Qualification

Fresh current MKTApp S1–S4 vs fresh direct same-model baseline S1–S4.

- Generate both candidates in the same qualification generation window.
- Apply completeness checks to both sides.
- Judge evaluates blind X/Y pairs.
- Compute uplift: MKTApp score − baseline score per dimension and overall.
- Apply uplift gate (see § Uplift Gate below).

---

## Phase 4 — Residual-Gap Loop

If MKTApp fails to show uplift over the same-model baseline:

- Diagnose why MKTApp's architecture does not improve the result.
- Fix generic product root causes.
- Re-run same-model qualification.

Do NOT weaken the baseline to make MKTApp pass.
Do NOT change Judge scoring to manufacture a pass.

### Current Phase 4 status (2026-09-05)

S3 (campaign_strategy) failed with −1.167 uplift. Root cause identified
as a **harness asymmetry** in `scripts/m6_uplift_harness.py`:
`run_mktapp_candidate` does not parse `Agent settings:` from
`resource_context` into `agent_settings_override`, unlike the frontier
UAT harness. This means MKTApp never receives the user's budget
constraint in its structured `instructions` dict, causing the validator
to reject budget allocation and the repair loop to force hedging.

**Fix scope:** Harness-only (`scripts/m6_uplift_harness.py`). No
production code changes. See `M6_STATUS.md` § S3 Pre-Fix Investigation
for the exact implementation plan.

**Pre-condition for re-qualification:** Harness fix implemented + full
offline suite passes + new Product Owner authorization for paid run.

---

## Phase 5 — Web E2E / Browser Qualification

Prove actual UI delivery through automated browser tests.

Flow: UI → API/Orchestrator → Agent → Result/Renderer → UI

This is a smoke-test of the full production delivery path, separate
from both model-quality benchmark levels.

### Current status (2026-09-05)

**Web/Browser E2E: PASS (14/14 with Playwright).**
**API/integration E2E: PASS (16/16 with FastAPI TestClient).**

Two distinct E2E test layers:

1. **API/integration E2E** — `tests/test_beta_e2e_smoke.py` (16 tests).
   Uses FastAPI `TestClient`. Exercises HTTP routing, SSE streaming,
   file I/O, session management with mocked LLM. Does NOT execute real
   browser DOM/JavaScript.

2. **Web/Browser E2E** — `tests/test_browser_e2e.py` (14 tests). Uses
   **Playwright** with real Chromium. Exercises: browser → real
   frontend JavaScript → HTTP/SSE → backend → orchestrator → agent
   seam → renderer → DOM. LLM/Orchestrator mocked — no paid calls.

Browser E2E coverage: application boot (page renders, JS loads, no
fatal console errors), all 4 agents through the real wizard UI (select
product → select agent → run → result link appears in DOM), Agent 3
settings path proven (budget_max saved via API → reaches backend →
agent runs), file upload through real DOM file input, repeated runs in
same browser session, controlled error surfaces in UI, UI operable
after completed run, sessions sidebar populated.

**No gap remaining for Beta text/core flows.** Media generation
(image/video) is a **REQUIRED Beta capability** — browser E2E for
media paths is in progress.

---

## Benchmark Levels — Explicit Definitions

The M6 qualification uses two distinct benchmark levels. **Level 1
results must NOT be interpreted as Level 2 results.**

### Level 1 — Same-model API qualification

**Purpose:** Determine whether MKTApp adds value over using the same
underlying model through a neutral/direct API baseline.

**What it measures:** MKTApp architecture uplift (grounding, brand
handling, review/repair, orchestration, deterministic safety/validation)
versus a direct API call to the same model with equivalent user/source
information.

**Current status (2026-09-05):**

| Scenario | Agent | Uplift | Status |
|----------|-------|--------|--------|
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

**Status: Future milestone.** Not yet executed. Must NOT be inferred
from Level 1 results.

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
- Comparable available tools/search where reasonably comparable
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

Do NOT claim provider-UI parity or superiority until Level 2 has been
actually executed.

---

## Benchmark Roadmap

The progression from current state to Beta/Release decision:

1. **Same-model API capability qualification** (Level 1) — current: FAIL
2. **Fix any failing Agent(s)** — S3 campaign_strategy root cause identified (harness defect, deferred benchmark work)
3. **Re-run same-model qualification** — deferred; not required before user Beta
4. **Web E2E / Browser flow validation (text/core)** — Web/Browser E2E PASS (14/14 with Playwright); API/integration E2E PASS (16/16)
5. **Image Generation Browser E2E** — IN PROGRESS (required for Beta)
6. **Video Generation Browser E2E** — IN PROGRESS (required for Beta)
7. **Real Image + Video smoke** — REQUIRED (one real image + one real video)
8. **Full Beta regression/readiness** — REQUIRED
9. **Provider UI benchmark** (Level 2) — separate paid benchmark, future milestone
10. **Beta/Release decision** — pending completion of items 5-8

### Beta scope correction (2026-09-05)

Image generation and video generation are **REQUIRED Beta capabilities**.
Deployment/Docker/server rollout is owned externally and is NOT part of
this project's critical path.

---

## Testing Layers

These answer different questions and must not be conflated:

1. **Unit / offline contract tests** — deterministic logic, routing,
   isolation, validation, error handling. Run via `pytest` with no
   network/model/web calls.
2. **Integration / orchestration tests** — orchestration, API, context
   propagation across multiple components. Still offline; uses
   mocks/stubs for LLM.
3. **Same-model uplift qualification** — semantic output quality
   versus direct same-model baseline. Requires paid model calls and
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
- No same-model qualification rerun before Phase 2 readiness check.
- No Web E2E execution before Phase 5.
- No semantic keyword/regex lists for semantic concepts.
- No brand-specific hardcoded behavior.
- No benchmark-specific answer keys or expected-output validators.

---

## Same-Model Uplift — Methodology

### Candidate definitions

- **Candidate A — Direct Same-Model Baseline**: the exact same
  underlying model as the corresponding MKTApp Agent, given a fair,
  strong neutral task prompt with equivalent user/source information
  and equivalent tool capability. No MKTApp internal machinery.
- **Candidate B — MKTApp**: the current production MKTApp path using
  the same underlying model, including all architecture, grounding,
  brand handling, review/repair, orchestration, and deterministic
  safety/validation.

### Clean-candidate policy

A qualification candidate (MKTApp or baseline) is classified into one
of three mechanically knowable states:

- **COMPLETE** — final candidate has acceptable finish metadata and no
  unrecovered material truncation.
- **INCOMPLETE** — final candidate has an unrecovered truncation that
  materially contributes to the accepted output.
- **UNKNOWN** — required metadata is absent; completeness cannot be
  established. UNKNOWN is NOT treated as COMPLETE.

Rules:
- INCOMPLETE and UNKNOWN candidates cannot be reused as clean candidates.
- UNKNOWN candidates cannot proceed to Judge.
- Either side incomplete or unknown means no Judge invocation for that
  scenario.
- Both sides complete may proceed through Judge preflight.

### Completeness determination

Completeness is determined from per-call finish metadata persisted in
the usage log (`logs/llm_usage.jsonl`) and in run evidence. The
classification is generic and mechanically verifiable — no
scenario-specific text rules, keyword lists, or expected-output
matching.

For MKTApp (multi-call): the last non-superseded material-role call
(generate/repair) determines completeness. A truncated generate call
that is superseded by a successful repair call remains COMPLETE.

For the direct baseline (single call): `finish_reason == "length"` →
INCOMPLETE; `finish_reason in ("stop", "tool_calls")` → COMPLETE;
absent metadata → UNKNOWN.

### Information parity

Both candidates receive equivalent USER/SOURCE information:
- user request;
- product facts/data;
- quick_brief;
- uploaded attachment contents;
- brand information supplied/configured for that task;
- configured Content Pillars where relevant;
- equivalent web-search capability when the task requires current research.

Candidate A does NOT receive MKTApp INTERNAL implementation machinery:
- internal system prompts;
- validator instructions;
- repair prompts;
- hidden review prompts;
- orchestration implementation detail;
- benchmark-specific rules;
- internal schemas unless the schema itself is part of the
  user-facing deliverable contract.

### Tool parity

Tool capability must be SYMMETRIC. If either side has an extra research
capability, the comparison is invalid.

- S1 (product_spec): no web search on either side.
- S2 (competitor_analysis): equivalent web-search capability on both sides.
- S3 (campaign_strategy): equivalent web-search capability on both sides.
- S4 (content_creator): no web search on either side.

Do NOT require identical number of tool calls — let each candidate decide
whether/how to use the capability. Align: availability, provider/model
compatibility, allowed tool class, bounded tool/search budget.

Unresolved capability mismatch invalidates the scenario. Do not
silently compare MKTApp+web vs baseline-without-web, or vice versa.

### Canonical source fixture rule

Both candidates must reference the same canonical `SourceFixture` hash.
The fixture contains actual semantic content, not just product IDs:

- raw product facts (from `product_db.get_scoped_context_text`);
- product image paths;
- quick_brief;
- user-visible task constraints;
- user-owned brand guidelines;
- user-configured Content Pillars (loaded from `config/content_policy.yaml`
  via `src.config_loader.load_config()` — the SAME production loader the
  Orchestrator uses, NOT `agents.yaml`);
- explicit user-selected Pillar (if applicable);
- fixed upstream context for S3/S4 (agent-level isolation).

Provenance categories:
- `supplied`: user-owned, both sides receive;
- `fixed_qualification_context`: fixed upstream fixture, both sides receive;
- `mktapp_derived`: only MKTApp receives/creates — NOT in the shared fixture.

### Agent-level isolation

The qualification unit is an individual Agent, not the whole pipeline.

For S3 (campaign_strategy) and S4 (content_creator), FIXED upstream
context is supplied directly to both candidates from CANONICAL
QUALIFICATION FIXTURES in `data/m6_uplift/fixtures/`.  No upstream
Agent output is generated during the qualification run.

S3 receives fixed competitor analysis from
`data/m6_uplift/fixtures/S3_competitor_context.md` (~14KB).

S4 receives fixed competitor analysis AND fixed campaign strategy from
`data/m6_uplift/fixtures/S4_competitor_context.md` and
`data/m6_uplift/fixtures/S4_campaign_context.md`.

These fixtures were frozen from accepted historical MKTApp production
outputs (see `data/m6_uplift/fixtures/PROVENANCE.md`).  They are NOT
independently researched neutral truth — they originated from MKTApp.
Their purpose is fixed candidate-independent upstream context.

These are STATIC qualification inputs — neutral, factual, scenario-owned,
candidate-independent, versioned via the source fixture hash.  They are
NOT generated during either candidate run.  Normal MKTApp runs must NOT
overwrite them.

This measures Agent architecture uplift under a realistic representative
task, not standalone/no-context behavior.

### Multi-turn baseline completeness

S2/S3 baselines may use tool loops (model → tool call → tool result →
continuation). An intermediate `finish_reason="tool_calls"` turn is
normal and does NOT mean the candidate is complete.

The final visible-answer turn must terminate acceptably:
- `finish_reason="stop"` → COMPLETE;
- `finish_reason="length"` → INCOMPLETE;
- missing metadata → UNKNOWN.

S1/S4 baselines are single-call when that is actually true.

### Budget hierarchy

The next qualification must use nested independent caps where a larger
outer cap NEVER overrides a smaller inner cap:

1. **Per-candidate inner cap** — independently enforced for baseline and
   MKTApp within each scenario. Candidate A cannot consume Candidate B's
   reserved budget.
2. **Per-scenario maximum** — one scenario's total spend (hard cap).
3. **Generation-stage maximum** — all baseline + MKTApp generation.
4. **Judge maximum** — all Judge calls.
5. **Total execution maximum** — newly approved execution cap.

Every paid operation must pass pre-call budget authorization:

- **Model turns via `chat()`**: `BudgetGuardedLLMClient.chat()` calls
  `budget.check_call()` before delegating to `LLMClient.chat()`.  After
  the call, actual provider-reported cost (`_last_cost_usd`) is committed
  via `budget.commit_candidate_spend()`.  If no cost is reported, the
  conservative reserve is committed.
- **Model turns via `chat_with_tools()`**: `pre_model_hook` callback
  invoked before each iteration.  `pre_tool_hook` callback invoked before
  each client-side tool execution.  Both call `budget.check_call()`.
- **Web search (server-side)**: MKTApp uses `chat()` with `tools=`
  parameter (OpenRouter server-side plugins).  Web search cost is
  included in the model call's `usage.cost` — it is NOT billed as a
  separate tool operation.  No separate tool commit for web search.
  This is option B: provider/server-side tool usage whose charge is
  returned as part of the model/provider response.
- **Judge calls**: `_call_judge_raw()` accepts `budget_guard_fn`
  (pre-call) and `run_judge()` accepts `budget_commit_fn` (post-call).
  The guard is called BEFORE `httpx.Client.post()`.  After a successful
  response, actual cost is committed via `budget_commit_fn`.

This applies to:
- Candidate A: every model turn (initial, tool-call, continuation, final);
- Candidate B: every internal LLM call (generate, review, repair, brand
  interpretation, tool continuations);
- Judge: every Judge call.

Historical sunk spend is reported separately and does NOT consume the
new run cap. Resume preserves all spent amounts — counters are not
reset by starting a new process.

### S4 presentation

S4 (`content_creator`) candidates presented to the Judge must use the
production-equivalent rendered markdown (`render_posts_to_markdown`),
not the raw JSON. The raw JSON is preserved as a separate audit
artifact (`*_output_raw.json`). Renderer failure prevents clean
judging — no silent fallback to raw internal JSON.

### Neutral task specification

Required task constraints reach both MKTApp and the direct baseline via
a neutral task spec. MKTApp-only private implementation settings (retry
limits, internal engine config) are NOT leaked to the baseline prompt.
User-visible task constraints (budget, forbidden tactics, analysis
depth) ARE shared with both sides.

### Uplift gate (proposed — requires Product Owner approval)

The primary metric is:

> MKTApp score − Same-model direct baseline score

Proposed uplift gate (configurable — requires Product Owner approval):

## Aggregate uplift
Mean MKTApp − Baseline across all scenario/dimension scores: `>= +0.25`

## Scenario consistency
At least 3 of 4 scenario-level overall deltas: `>= 0`

## No material scenario regression
No scenario overall delta: `< -0.5`

## Core dimensions
No core product-value dimension delta: `< -0.5`
Core dimensions: Factuality, Instruction following, Brand/asset fit.

Do not require MKTApp to win every single stochastic subscore.
Do not tune the threshold after seeing paid results.
Do not change Judge scoring merely to make MKTApp pass.

### Quality vs efficiency reporting

MKTApp may legitimately use more model calls than the direct baseline.
That is part of its architecture.

The semantic Judge answers: "Is the result better?"

Separately record:
- total model calls;
- tool calls;
- token usage;
- cost;
- latency where available.

Do NOT blend cost directly into the semantic quality score.

After qualification we should be able to say something like:
> +0.6 quality uplift at 2.4× inference cost

or:
> +0.1 uplift at 5× cost

Those have very different product implications.

### Same-model paid budget proposal (NOT authorization)

Recalculated with per-turn actual-cost accounting.  Every model turn
inside `chat_with_tools()` commits its actual provider-reported cost
immediately via `post_model_hook`.  Conservative estimates include
multi-turn variance and prompt inflation from web results.

Per-scenario per-candidate accounting:

| Scenario | Baseline exp/cons/cap | MKTApp exp/cons/cap |
|----------|----------------------|---------------------|
| S1 | $0.043 / $0.050 / $0.06 | $0.035 / $0.045 / $0.10 |
| S2 | $0.101 / $0.130 / $0.15 | $0.200 / $0.230 / $0.25 |
| S3 | $0.071 / $0.090 / $0.15 | $0.035 / $0.080 / $0.30 |
| S4 | $0.074 / $0.078 / $0.10 | $0.045 / $0.060 / $0.10 |

Component totals:

| Category | Expected | Conservative | Hard cap |
|----------|----------|-------------|----------|
| Direct baseline S1–S4 | $0.289 | $0.348 | $0.46 |
| MKTApp S1–S4 | $0.315 | $0.415 | $0.75 |
| Judge S1–S4 | $0.170 | $0.185 | $0.20 |
| **Total** | **$0.774** | **$0.948** | **$1.41** |

All hard caps > conservative expected.  S4 baseline margin: 28%.
S2 MKTApp margin: 9% (includes Brand Interpretation call).

Nested budget hierarchy (outer cap NEVER overrides inner cap):

```
S1/baseline ($0.06)   S1/mktapp ($0.10)
S2/baseline ($0.15)   S2/mktapp ($0.25)
S3/baseline ($0.15)   S3/mktapp ($0.30)
S4/baseline ($0.10)   S4/mktapp ($0.10)
         ↓                    ↓
   baseline cap ($0.46)  mktapp cap ($0.75)
         ↓                    ↓
              generation stage cap
         ↓                                ↓
              total NEW run cap ($1.41)
              judge stage cap ($0.20)
```

Historical actual paid spend: **$2.683325** (separate, accounting only).
Historical ceiling: $2.86.

**Proposal only. No authorization.**
