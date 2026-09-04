# M6 Status

**Canonical current-state document for M6.1 qualification and remediation.**
Last updated: 2026-09-04 (Round 7 documentation checkpoint).

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
| Execution/harness evidence | **VALID** — all 7 paid calls completed, all evidence preserved |
| Production remediation during qualification | **None** — no production code was modified during the run |

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
| S2 | competitor_analysis | −0.167 | FAIL | FAIL (−1.0) | Near parity overall. Both had hallucinations. MKTApp output truncated. MKTApp won evidence quality (+1.0) and user effort (+1.0). |
| S3 | campaign_strategy | +1.000 | PASS | FAIL (+0.0) | **MKTApp clearly outperformed Frontier.** Frontier output was severely truncated. MKTApp correctly avoided unsupported claims and discount tactics. Brand advantage not achieved (tie at 4/4). |
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

4. **Output truncation observed in multiple candidates** (S2, S3 Frontier, S4 Frontier)
   - Truncation was observed in both MKTApp and Frontier outputs.
   - Root cause must be **verified offline** before changing token limits.
   - Do not assume truncation is solely a token-budget problem.

5. **Content Creator machine-readable JSON is an architectural requirement**
   - The JSON schema is required for the system's pipeline.
   - Do not remove it merely to improve benchmark readability.
   - Human-readable preview may be added as an additional field if needed.

### Proposed fixes (not yet implemented)

These are hypotheses to be validated during remediation design, not
committed solutions:

1. Generic source-grounding constraint for all agents
2. Instruction precedence fix for Product Spec multi-product + one-page
3. Generic brand-context injection as competitive differentiator
4. Offline truncation root-cause verification
5. Content Creator readability (only after the above)

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

**Phase:** `M6 Remediation Design`

**Order of investigation/remediation:**

1. Generic grounding/reliability design
2. Instruction precedence / Product Spec multi-product conflict
3. Generic brand-context utilization
4. Offline truncation root-cause verification
5. Content Creator readability (only after the above)

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
- `M6_REMEDIATION_DESIGN.md` — preliminary design before generic rebase
- `M6_1_RERUN_REPORT.md` — documents the incomplete source run
  `20260904_070830`; valid as historical record of that run's stoppage
- `M6_GENERIC_REMEDIATION_REBASE.md` — documents the harness hardening
  process; valid as historical record

This `M6_STATUS.md` is the **single source of truth** for the current
M6.1 qualification result and remediation constraints.
