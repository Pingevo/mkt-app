# M6 Status

**Canonical current-state document for M6.1 qualification and remediation.**
Last updated: 2026-09-04 (Round 8 — truncation evidence correction + qualification-validity axes).

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
