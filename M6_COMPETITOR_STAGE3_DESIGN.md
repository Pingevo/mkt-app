# Competitor Analysis — Brand Stage 3 Semantic Reasoning Design Proposal

**Status:** Design-only. No implementation authorized in the current phase.
**Created:** 2026-09-04 (Round 10 remediation).
**Approval required:** Product Owner must approve before any implementation.

---

## 1. Current architecture

### Stage A — objective evidence research

- `CompetitorAnalysisAgent` with `evidence_mode: true` uses
  `EVIDENCE_SYSTEM_PROMPT` (`src/agents/competitor_evidence.py:870-927`).
- `EVIDENCE_SYSTEM_PROMPT` contains **no brand fields and no brand
  reference**.
- `RESEARCH_RESPONSE_SCHEMA` contains **no brand fields**.
- Evidence records contain: `competitor`, `field`, `claim`, `url`,
  `geography`.
- Brand context does **not** modify evidence records.
- **Stage A is brand-independent and objective. This must be preserved.**

### Stage 2.5 — semantic evidence review (existing)

- `SemanticEvidenceReviewer.review`
  (`src/agents/competitor_evidence.py:672-857`) runs after Stage A
  validation, only when `research.evidence` is non-empty.
- It checks semantic entailment of evidence claims against source
  content.
- It uses a fixed system prompt (`_REVIEW_SYSTEM_PROMPT`) with no
  brand context.
- It returns a new `ResearchResponse`, copying
  `evidence_based_recommendations`, `strategic_hypotheses`, and
  `uncertainty` unchanged.
- It does **not** receive `brand_reference`.

### Stage 3 — deterministic report rendering

- `CompetitorReportRenderer`
  (`src/agents/competitor_evidence.py:517-598`) receives a validated
  `ResearchResponse` and annotations.
- It renders Markdown deterministically.
- It has **no semantic reasoning capability**.
- It **cannot** legitimately prioritize recommendations by brand
  relevance, because that requires understanding the relationship
  between brand positioning and competitor evidence — a semantic
  judgment, not a mechanical transformation.

### Current limitation

There is no semantic seam between objective evidence (Stage A) and
brand-aware recommendations (Stage 3). The renderer can only reproduce
evidence structurally; it cannot say "this evidence matters more for
this brand because..."

---

## 2. Current model-call lifecycle

### Typical successful run (evidence present)

| Step | Component | LLM calls | Tools |
|------|-----------|-----------|-------|
| Stage 1 — generate `ResearchResponse` | `BaseAgent.run` → `llm.chat` | 1 | web_search + web_fetch |
| Stage 2 — deterministic validation | `_validate_research_json` | 0 | — |
| Stage 2.5 — semantic evidence review | `SemanticEvidenceReviewer.review` → `llm.chat` | 1 | none |
| Stage 3 — deterministic rendering | `CompetitorReportRenderer.render` | 0 | — |
| **Total** | | **2** | |

### Maximum run (validation failure triggers revision)

| Step | LLM calls |
|------|-----------|
| Stage 1 generate | 1 |
| `_revise_research` (conditional, at most once) | 1 |
| Semantic evidence review | 1 |
| **Total** | **3** |

### Key config constraints

- `max_review_iterations: 0` — disables `BaseAgent._review_and_refine`.
- `max_retry_limit: 0` — disables BaseAgent repair loop.
- `_skip_agent_validation: True` — `validate_output` returns pass
  immediately in evidence mode.
- `_revise_research` is called at most once; if validation still fails,
  the agent returns a failure report.

### Existing calls and brand_reference

| Call | Receives `brand_reference`? | Can synthesize brand-aware recommendations? |
|------|----------------------------|---------------------------------------------|
| Stage 1 (`EVIDENCE_SYSTEM_PROMPT`) | No | No — and must not, to preserve evidence objectivity |
| `_revise_research` | No | No — conditional on validation failure, contract is JSON repair |
| `SemanticEvidenceReviewer.review` | No | No — but is the natural seam: it already consumes validated evidence and could be extended |

---

## 3. Why rejected approaches are unacceptable

### Raw brand-reference dumping / template injection

- Appending the raw `brand_reference` string to the rendered report
  would inject brand context as a visible appendix or template block.
- This is not semantic reasoning — it is a copy-paste of brand rules
  into output.
- It pollutes the report with internal brand context that should not
  appear in user-facing output.
- A prior `_append_brand_framing` implementation was correctly reverted
  for this reason.

### Deterministic brand-term selection

- Scanning evidence text for brand-specific terms and highlighting them
  would require a growing keyword list.
- This violates AGENTS.md rule 3: "Do not solve semantic problems with
  growing keyword/regex lists."
- Brand relevance is contextual, not lexical.

### Keyword rules / regex classification

- Same violation — semantic relevance cannot be reduced to substring
  matching.
- Would require per-brand keyword maintenance and would not generalize.

### Injecting brand_reference into Stage 1 (EVIDENCE_SYSTEM_PROMPT)

- Would mix brand positioning with objective evidence generation.
- Evidence records would be biased by brand context.
- Breaks the Stage A evidence contract.
- Rejected — it compromises objectivity.

---

## 4. Design options

### Option A — Dedicated post-evidence semantic Brand reasoning step

Add a new model reasoning step between Stage A evidence validation and
Stage 3 deterministic rendering.

**Flow:**

```
Stage A (brand-independent evidence research)
  → validate/provenance-check evidence (mechanical)
  → Semantic Brand Reasoning Pass (new, separately approved)
      inputs:  immutable validated evidence
               product facts (supplied)
               relevant brand reference (runtime-data-driven)
               task/user brief
      output:  structured implications + recommendations
               with provenance refs back to evidence records
               and category labels (supplied fact / researched
               evidence / inference / recommendation)
  → Stage 3 deterministic renderer
      renders the semantic output (not raw brand reference)
```

**Input contract:**

- Immutable validated `ResearchResponse` (evidence records unchanged).
- Product facts from `product_db` / supplied context.
- Brand reference from runtime brand config (not hardcoded).
- Task/user brief.

**Output contract:**

- Structured recommendations with:
  - `evidence_ref`: pointer to source evidence record (provenance).
  - `category`: `supplied_fact` | `researched_evidence` |
    `inference` | `recommendation`.
  - `brand_relevance`: model-explained reasoning (not a score).
  - `implication`: plain-text implication for the brand.
- Raw brand reference is **not** included in the output.

**Evidence isolation:**

- Evidence records from Stage A are never modified.
- The semantic pass reads evidence and produces new structured output.
- Provenance links back to evidence records by reference.

**Cost:**

- **+1 LLM call** per competitor analysis run (on top of the current
  2-call typical / 3-call maximum).
- Input size: validated evidence + brand reference + brief.
- Output size: structured recommendations (bounded by schema).
- Cost is bounded by the existing per-run budget cap.

### Option A-variant — Reuse `SemanticEvidenceReviewer.review` (zero extra calls)

Extend the existing `SemanticEvidenceReviewer.review` call to also
receive `brand_reference` and generate brand-aware
`evidence_based_recommendations` / `strategic_hypotheses` while
preserving `evidence` records unchanged.

**Cost:**

- **Zero incremental LLM calls** — the review call already runs once
  per successful competitor analysis.

**Trade-off:**

- Merges two responsibilities (entailment checking + brand-aware
  synthesis) into one call.
- Risk: the combined prompt becomes harder to reason about and the
  entailment check may be weakened.
- Requires careful prompt design and durable tests proving evidence
  records remain unchanged.

### Option B — Keep competitor_analysis objective; delegate brand-aware strategy to campaign_strategy

- Keep Stage 3 as a deterministic evidence renderer.
- Do not produce brand-aware recommendations in competitor analysis.
- Brand-aware strategy is handled downstream by `campaign_strategy`
  agent, which already receives brand context and competitor evidence.
- This is the current state and is honest about the limitation.

---

## 5. Option A vs Option B — detailed comparison

| Dimension | Option A (dedicated step) | Option A-variant (reuse review) | Option B (delegate to Agent 3) |
|-----------|--------------------------|--------------------------------|-------------------------------|
| Agent 2 standalone usefulness | High — competitor analysis produces brand-aware recommendations | High — same | Medium — Agent 2 is evidence-only; brand-awareness requires Agent 3 |
| Quality implications | High — dedicated prompt for brand-aware reasoning | Medium — combined prompt may dilute entailment checking | Depends on Agent 3's ability to synthesize from evidence + brand |
| Frontier parity implications | Closer to Frontier agents that produce brand-aware competitor analysis | Same as Option A | May underperform if Frontier produces brand-aware analysis in Agent 2 |
| Latency | +1 LLM call latency | No additional latency | No additional latency in Agent 2 |
| Token/cost impact | +1 call per run | Zero incremental | Zero incremental |
| Architecture complexity | New component, new schema | Modified existing component | No change |
| Evidence isolation | Strong — separate step, evidence immutable | Medium — same call, must enforce immutability by prompt+test | Strong — Agent 2 evidence unchanged |
| Duplication with Agent 3 | Low — Agent 2 does evidence+brand, Agent 3 does strategy | Low — same | Potential — Agent 3 must do both evidence-synthesis and strategy |
| Failure behavior | If semantic step fails, evidence still available | If review fails, evidence still available | If Agent 3 fails, no brand-aware output at all |
| Trade-off | More cost, cleaner separation | Zero cost, merged responsibility | Zero cost, but Agent 2 less useful standalone |

---

## 6. Recommendation

**Option B is recommended as the default** unless the Product Owner
specifically requires brand-aware competitor recommendations from
Agent 2 standalone.

**Rationale:**

- `campaign_strategy` (Agent 3) already receives both competitor
  evidence and brand context. It is the natural locus for brand-aware
  strategy synthesis.
- Adding a semantic step to Agent 2 increases cost and complexity
  for a benefit that Agent 3 already provides.
- Option B is honest about the limitation and avoids duplicating
  reasoning that Agent 3 performs.
- It preserves Stage A evidence objectivity with zero risk.

**If the Product Owner requires Agent 2 to produce brand-aware
recommendations standalone:**

- **Option A-variant (reuse `SemanticEvidenceReviewer.review`)** is
  preferred over Option A because it adds zero incremental LLM calls.
- However, it requires careful prompt design and durable tests proving
  evidence records remain unchanged and the entailment check is not
  weakened.
- Option A (dedicated step) is the cleaner architecture if cost is
  acceptable.

**Option A-variant and Option A both require explicit Product Owner
approval before implementation.**

---

## 7. Approval boundary

- **No implementation is performed now.**
- Implementation requires explicit Product Owner approval of Option A,
  Option A-variant, or Option B.
- If Option A or A-variant is approved, the implementation must:
  - Add the semantic reasoning as a separately testable component or
    extension.
  - Define a structured output schema with provenance and category
    labels.
  - Keep Stage A unchanged.
  - Keep the deterministic renderer for final output formatting.
  - Add durable tests proving evidence isolation, provenance, and
    grounding category labeling.
  - Not hardcode brand-specific wording or behavior.
  - Respect the per-run budget cap.

---

## 8. Current durable tests (already in place)

The following tests in `tests/test_competitor_analysis.py` prove the
current limitation and protect against regressions:

- Stage A schema has no brand fields.
- Stage A system prompt has no brand context.
- Renderer does not dump brand reference.
- Evidence records remain unchanged.
- Renderer does not fabricate brand-aware recommendations.

These tests ensure that no fake deterministic semantic behavior is
introduced and that the limitation remains honestly documented until
a semantic reasoning seam is separately approved.
