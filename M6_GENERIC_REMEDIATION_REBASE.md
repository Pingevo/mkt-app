# M6 Generic Remediation Rebase

**Mode:** Analysis only — no production code/config/test changes, no model/web/image/video calls, no commit, no push.
**Current HEAD:** `557ab77b42428e82611152f2e87989130626fef1`
**Generalization baseline commit:** `557ab77b42428e82611152f2e87989130626fef1` (G3 — "test(generalization): verify cross-domain routes and preserve product identity")
**Prior M6 baseline (failure attribution source):** `f352aaf` (M6.1 parity harness) → artifacts under `data/m6_frontier_uat/20260902_070643/`
**Source documents:** `M6_FINAL_FAILURE_ATTRIBUTION_REPORT.md`, `M6_REMEDIATION_DESIGN.md`, `M6_1_JUDGE_REPORT.md`, `M6_1_RERUN_REPORT.md`, `M6_FRONTIER_PARITY_UAT_PLAN.md`, `M6_STATUS.md`, `G0_DISPOSITION.md`, `GENERALIZATION_REMEDIATION_PLAN.md`

---

## 1. Purpose

Re-evaluate the old M6 remediation assumptions against the CURRENT post-G2/G3 generic engine before any production fix is allowed. The old M6 remediation design was written against `f352aaf`, before G1 (runtime brand_dir, category removed from contract), G2 (source-driven evidence pipeline), and G3 (cross-domain integration tests). This report classifies each old finding against HEAD and identifies which parts of the old design are stale, overfit, or already resolved.

---

## 2. Current production-path map relevant to M6

```
web_viewer endpoint (api_run_agent / api_run_agents / api_run_flows / api_run_auto)
  → body.get("brand_dir") or "brand"            ← runtime parameter (G1)
  → Orchestrator(brand_dir=runtime)              ← fresh per request (G1)
  → build_step_run_context(brand_dir, product_refs, quick_brief, ...)
    → StepRunContext(brand_dir, quick_brief, resource_text, resource_image_paths)
      ← no category field (G1 removed it)
  → Orchestrator.run_product_spec / run_competitor_analysis / run_campaign_strategy / run_content_creator
    → agent = _make_agent(agent_name, AgentClass, llm)
      → BaseAgent.__init__(agent_config, llm, brand_context, brand_reference, instructions, brand_rules)
        ← brand_rules = BrandRules (hard_dict from terms.json + voice.json)
    → agent.build_prompt(...)
    → agent.run(prompt, quick_brief, image_paths, resource_context, step_context)
```

### BaseAgent.run pipeline (HEAD)

1. **Build system prompt** — `_build_system_prompt()`:
   - agent system_prompt + `build_priority_prompt(brand_rules, instructions)` (hard + soft brand rules injected as prompt text) + instruction block
   - competitor_analysis overrides this in evidence mode → `EVIDENCE_SYSTEM_PROMPT`
2. **Generate** — `llm.chat(...)` with optional web_search tools
3. **Blank check** — raise ValueError if output is empty
4. **Review & Refine (Phase 3)** — `_review_and_refine(...)` up to `max_review_iterations`:
   - Sends output back to LLM with source, checklist, evidence discipline, quick_brief
   - Early termination if LLM returns unchanged output
   - competitor_analysis: `max_review_iterations: 0` (evidence mode handles review differently)
   - product_spec: `max_review_iterations: 1`
   - content_creator: `max_review_iterations: 1`
5. **Validate** — `validate_output(output)`:
   - `output_validators.validate_output(agent_name, output, required_sections, output_quality)`
   - Checks: required sections (only if `strict_output_sections`), JSON schema (if `output_format: json`), output_quality (min_total_chars, min_non_citation_chars, min_analysis_blocks, forbid_citation_only)
   - **NO source-grounded validation, NO brand hard-term enforcement, NO line-count gate**
   - competitor_analysis overrides `validate_output` with evidence-mode-specific checks
6. **Repair loop** — up to `max_retry_limit`:
   - ALL validation errors go through `_repair_output` (LLM call)
   - **NO non-repairable error classification** — factual errors trigger paid resample
   - competitor_analysis: `max_retry_limit: 0` + custom `_repair_output` with deterministic fallbacks

### Key files and their current state

| File | Current state relevant to M6 |
|------|------------------------------|
| `src/output_validators.py` | Generic only: sections, JSON schema, output_quality. `context_flags` param accepted but unused (G0 reverted M6 validators). No source-grounded, no brand-hard, no line-count. |
| `src/agents/base_agent.py` | Generic run pipeline. No `_normalize_quick_brief`, no `_get_source_text`, no `_is_non_repairable_error`. All validation errors go through repair loop. |
| `src/agents/product_spec.py` | Thin: `build_prompt` only. No one-page normalization, no length validation, no `_trusted_source_text`. |
| `src/agents/competitor_analysis.py` | **Completely rewritten by G2.** Evidence mode: ResearchResponse schema → `_validate_research_json` → `CompetitorReportRenderer` → deterministic Markdown. Three-bucket annotation split, canonical identity matching, provenance verification. `max_retry_limit: 0`, `max_review_iterations: 0`. |
| `src/agents/competitor_evidence.py` | **G2 source-driven engine.** Open field schema (no FIELD_CATALOG enum). `_validate_evidence` rejects candidates, target-matched sources, cross-competitor attribution. `_NO_MATCH` replaces misleading `-`. |
| `src/agents/content_creator.py` | Thin: `build_prompt` only. No source-grounded validation, no forbidden claims, no image-prompt validation. |
| `src/agents/campaign_strategy.py` | G2 removed NLP heuristics. Source relevance via identity matching, not keyword lists. |
| `config/agents.yaml` | No `non_repairable_error_prefixes`, no `forbidden_claims`, no `technical_attribute_labels`, no `max_lines`, no `one_page` config. `one-page` appears only in system prompt text as a quick_brief format option. competitor_analysis has `evidence_mode: true`. |
| `src/brand_priority.py` | Loads `terms.json` (restricted, replacements) + `voice.json` (banned_phrases) into `BrandRules.hard_dict`. Injected into system prompt via `build_priority_prompt`. `detect_conflicts` checks user custom instructions. **No post-generation deterministic validation of output against hard rules.** |
| `src/product_db.py` | `get_scoped_context_text` uses identity envelopes (product ID only, no category). `get_agent_context` returns text + image_paths + file_paths. |
| `src/run_context.py` | `StepRunContext` carries brand_dir, quick_brief, resource_text, resource_image_paths. No category field. |
| `src/content_schema.py` | `CONTENT_RESPONSE_SCHEMA` / `CONTENT_ARTIFACT_SCHEMA` validate JSON structure only (posts, platform, caption, hashtags, image_prompts, video_prompts, asset_ids). No claim grounding. |

---

## 3. Old finding → current-state classification

### Finding 1: S1 one-page instruction following (MKTApp 89 lines vs Frontier 55 lines)

**Classification: STILL A REAL PRODUCTION GAP**

**Evidence at HEAD:**
- `product_spec.py` has no one-page normalization. `quick_brief="one-page"` is passed literally to the user prompt (BaseAgent.run appends it as-is).
- `config/agents.yaml` product_spec system prompt says "ถ้า quick_brief ระบุรูปแบบ เช่น executive brief, one-page, สั้นกระชับ ให้ทำตาม quick_brief" — this is advisory, not measurable.
- `output_validators.py` has no line-count gate.
- The review/refine phase (max_review_iterations: 1) with the source-grounding review_prompt may catch verbosity, but it is not deterministic and not length-specific.
- Archived S1_Y (MKTApp): 89 lines. S1_X (Frontier): 55 lines.

**What G2/G3 changed:** Nothing for product_spec one-page. G2 only touched product_spec for minor cleanup (4 lines changed).

**Old design assumption to NOT implement:** The hard `≤40 lines` limit (see Section 5 — contradiction resolution).

---

### Finding 2: S2 selected-product fact availability/identity (K2 CPU/OS missing, `ชิปเซ็ต / OS: -`)

**Classification: ALREADY FIXED / MATERIALLY CHANGED BY G2/G3**

**Evidence at HEAD:**
- The archived S2_Y output was generated by the LEGACY competitor_analysis code path (pre-evidence-mode). It used a free-form executive brief with inline citations and showed `ชิปเซ็ต / OS: -` for K2.
- At HEAD, competitor_analysis runs in `evidence_mode: true`. The output is now a `ResearchResponse` JSON rendered by `CompetitorReportRenderer`.
- `CompetitorReportRenderer._product_cells` parses product spec lines using field labels from validated evidence. If no match is found, it shows `*ไม่พบค่าที่จับคู่ได้จากข้อมูลต้นทาง*` — NOT `-` (which was misleading).
- The open field schema means the model returns fields it actually found in the source (e.g. "display", "battery", "price" — or for a restaurant: "menu", "location", "hours"). There is no fixed CPU/OS/RAM catalog to be "missing".

**What G2/G3 changed:** The entire competitor_analysis output path. The old failure cannot recur in the same form because the output format and validation are completely different.

**Old design assumption to NOT implement:** The `technical_attribute_labels` list (CPU/OS/RAM/ROM/หน้าจอ/กล้องหน้า/แบตเตอรี่) and the `SelectedProductCoverageValidator` that checks values against this fixed list. This is category-specific and conflicts with the open field schema.

---

### Finding 3: S2 unsupported factual inference (video call, performance, price claims)

**Classification: ALREADY FIXED / MATERIALLY CHANGED BY G2/G3**

**Evidence at HEAD:**
- The archived S2_Y output claimed "กล้องหน้า 5MP ของ K2 เพื่อการสนทนาและการถ่ายภาพ" (implying video call), "Android 8.1 ... อาจส่งผลให้มีการใช้พลังงานที่สูงกว่า" (battery speculation), "หาก K2 ... ต่ำกว่า 4,000 บาท" (price speculation). These were free-form text claims with no provenance.
- At HEAD, competitor_analysis evidence mode requires every evidence record to have a URL from the verified annotation set (relevant=True, relevance_type="competitor"). `_validate_evidence` rejects:
  - Candidates (relevant=None) — unverified sources
  - Target-matched sources (relevance_type="target") — can't validate competitor claims
  - Cross-competitor attribution (matched_competitor ≠ ev.competitor)
  - Homepage/blocked sources
- `CompetitorReportRenderer` renders ONLY validated evidence. Free-form claims without provenance cannot appear in the table or brief.
- The `strategic_hypotheses` field explicitly separates unverified strategic claims from evidence-based recommendations, and the renderer labels them as hypotheses.

**What G2/G3 changed:** The provenance contract replaces the old `forbidden_claims` approach entirely. Instead of blocking specific phrases (วิดีโอคอล, เรียลไทม์), the engine requires every factual claim to be backed by a verified source URL.

**Old design assumption to NOT implement:** The per-agent `forbidden_claims` list (e.g. competitor_analysis: ["วิดีโอคอล", "video call", "เรียลไทม์"]). This is a static vocabulary that would need to grow for every new domain and is weaker than the provenance contract already in place.

---

### Finding 4: S4 unsupported caption/script/image-prompt claims (แม่นยำ, เรียลไทม์, อัจฉริยะ, navigation interface, safety GPS tracking map)

**Classification: STILL A REAL PRODUCTION GAP**

**Evidence at HEAD:**
- `content_creator.py` has no source-grounded validation. `build_prompt` assembles product_spec + competitor_analysis + campaign_strategy + media capabilities + asset summary.
- `validate_output` for content_creator checks JSON schema only (`CONTENT_RESPONSE_SCHEMA` — posts, platform, caption, hashtags, image_prompts, video_prompts, asset_ids). No claim grounding.
- The review/refine phase (max_review_iterations: 1) uses the generic review_prompt from config defaults — it does not specifically instruct checking caption/script/image-prompt claims against the product source.
- Archived S4_Y shows: `ระบุตำแหน่งแม่นยำ` (precision — no data), `พร้อมส่งข้อความเสียง` (voice message — source has Family Group Chat), `รู้พิกัดเรียลไทม์` (real-time — no latency data), `ระบบระบุตำแหน่งอัจฉริยะ` ("อัจฉริยะ" not supported), image prompt `navigation interface`, `safety GPS tracking map` (no navigation UI data).

**What G2/G3 changed:** Nothing for content_creator. G2 did not touch content_creator.py.

**Old design assumption to NOT implement:** The per-agent `forbidden_claims` list for content_creator (["เรียลไทม์", "อัจฉริยะ", "navigation interface", "safety GPS tracking map"]). This is a static vocabulary overfit to the K2/smartwatch domain.

**What IS needed:** A generic source-grounding mechanism for content_creator that works for any domain (see Section 7).

---

### Finding 5: Brand hard-term enforcement (terms.json restricted, banned_phrases, replacements)

**Classification: STILL A REAL PRODUCTION GAP (deterministic enforcement missing)**

**Evidence at HEAD:**
- `brand_priority.py` loads `terms.json` (restricted, replacements) and `voice.json` (banned_phrases) into `BrandRules.hard_dict`.
- `build_priority_prompt` injects these as prompt text: "คำ/วลีที่ห้ามใช้: ...", "คำต้องห้าม: ...", "คำที่ควรใช้แทน: ...".
- `detect_conflicts` checks user custom instructions against brand hard rules (for UI warning before run).
- **There is NO deterministic post-generation validation** that the output text does not contain restricted/banned phrases, and NO automatic application of replacements.
- The review/refine phase may catch brand term violations, but it is not deterministic.

**What G2/G3 changed:** Nothing for brand term enforcement. The brand hard/soft split existed before G1.

**Old design assumption that IS valid:** The `BrandHardValidator` concept — checking `terms.json` `restricted`, `banned_phrases`, `replacements` deterministically. This is generic because it reads from source data (terms.json), not hardcoded lists. This is the one part of the old design that aligns with the source-driven principle.

---

### Finding 6: Retry/self-review behavior (factual failures trigger paid resample)

**Classification: PARTIALLY STILL A REAL PRODUCTION GAP**

**Evidence at HEAD:**
- `BaseAgent.run` repair loop (lines 462-484): ALL `validate_output` failures go through `_repair_output` up to `max_retry_limit`. No error classification, no non-repairable short-circuit.
- competitor_analysis: `max_retry_limit: 0` + custom `_repair_output` with deterministic fallbacks (`_limited_analysis_fallback`, `_structural_output_failure`, `_required_search_failure`). This agent already has controlled cost behavior.
- product_spec: `max_retry_limit: 3` — any validation error triggers up to 3 paid repair calls.
- content_creator: `max_retry_limit: 3` — same.
- campaign_strategy: `max_retry_limit: 3` — same.

**What G2/G3 changed:** competitor_analysis cost behavior is already controlled. The other three agents are unchanged.

**Old design assumption that IS valid:** Error classification (non-repairable vs repairable) in BaseAgent.run. Factual/source-grounded/brand failures should not trigger unbounded paid resample. Format/JSON/section failures can keep existing repair.

**Old design assumption to NOT implement:** `max_retry_limit: 0` globally. The old design correctly rejected this. Per-agent `max_retry_limit` should stay configurable.

---

### Finding 7: S4 budget/KPI rubric mismatch

**Classification: JUDGE/RUBRIC MISMATCH (unchanged)**

**Evidence at HEAD:**
- content_creator system prompt and schema do not include budget/KPI fields.
- campaign_strategy is responsible for budget/KPI.
- The M6.1 judge penalized MKTApp S4 for missing budget/KPI, which is a rubric error, not a production defect.

**What G2/G3 changed:** Nothing. This remains a judge/rubric issue.

**Action:** No production change. Rubric clarification for future judges.

---

## 4. Which G2/G3 changes alter the original failure attribution

| G2/G3 change | Impact on M6 failure attribution |
|---|---|
| **competitor_analysis evidence_mode** (G2) | **Eliminates Findings 2 and 3.** The S2 output path is completely different. The old `forbidden_claims` and `technical_attribute_labels` validators are obsolete — the provenance contract is stronger and generic. |
| **Open field schema** (G2) | **Eliminates the `technical_attribute_labels` assumption.** No fixed CPU/OS/RAM catalog. Fields come from source data. Works for restaurant/apparel/SaaS without new code. |
| **Three-bucket annotation + identity matching** (G2) | **Eliminates category-specific relevance keywords.** Source relevance is based on product/competitor identity matching, not smartwatch keywords. |
| **`_NO_MATCH` replacing `-`** (G2) | **Addresses Finding 2.** Missing product facts now show a clear message instead of misleading `-`. |
| **campaign_strategy NLP heuristic removal** (G2) | Removes `_STOPWORDS`, `_significant_tokens`, `context_match` fallback. Source relevance via identity matching. Not directly an M6 finding but supports generalization. |
| **brand_dir runtime + category removed** (G1) | **Enables multi-brand and cross-domain.** M6 validators can now be built on source-driven logic without category branches. |
| **G3 cross-domain integration tests** | Proves the engine works for restaurant/apparel/SaaS/unknown through production-equivalent paths. Any M6 remediation must not break these. |

**Net effect:** Of the 7 old findings, 2 (S2 fact availability, S2 unsupported inference) are already resolved by G2. 1 (S4 budget/KPI) is a rubric mismatch. 4 remain as real production gaps (S1 one-page, S4 unsupported claims, brand hard-term enforcement, retry classification).

---

## 5. Contradiction resolution: ≤40 lines vs 56 lines

### The contradiction

The old `M6_REMEDIATION_DESIGN.md` states:
- Section 2.2: "product_spec agent detects one-page intent and appends a concrete, measurable instruction: `จัดทำสเปคหนึ่งหน้า ไม่เกิน 40 บรรทัด`"
- Section 2.4: "validate_output checks len(output.splitlines()) <= 40 when one-page intent is active"
- Section 2.7: "Test that validate_output rejects the MKTApp output [89 lines] and passes the Frontier output [55 lines]"

**These are mutually exclusive.** A hard `≤40` limit would reject the Frontier output (55 lines) that the design says should pass. Verified against archived artifacts: S1_X = 55 lines, S1_Y = 89 lines.

### Root cause of the contradiction

The old design conflated two different problems:
1. **Instruction following** — MKTApp ignored "one-page" and produced 89 lines. Frontier produced 55 lines and was accepted as one-page.
2. **Arbitrary line budget** — someone picked 40 as a "concrete" translation of "one-page", but 40 is not what "one-page" means to a human or to the Frontier model.

### Recommended production behavior

**Do NOT enforce a hard line count.** Instead:

1. **Translate "one-page" into a concrete but soft instruction** in the product_spec prompt before the LLM call:
   - `หมายเหตุ: "one-page" หมายถึงสเปคหนึ่งหน้า A4 กระชับ ประมาณ 50-70 บรรทัด ไม่ใช่เอกสารหลายหน้า — ใช้เฉพาะข้อมูลที่สำคัญที่สุด`
   - The range 50-70 is chosen because the Frontier output (55 lines) is the empirical "good one-page" benchmark. This is a soft target, not a hard gate.

2. **Soft validation, not hard rejection:**
   - If one-page intent is active and output exceeds ~2x the upper bound (e.g. >140 lines), flag it as a repairable instruction-following error (not non-repairable). One repair call is justified because the model can compress.
   - If output is 70-140 lines, accept it — the model made a reasonable attempt at one-page.
   - If output is ≤70 lines, accept it.

3. **Why not hard 40:**
   - It would reject the Frontier output that won the comparison.
   - "One-page" is a human-readable document concept, not a fixed line budget. A4 one-page with normal formatting is roughly 40-70 lines depending on content density.
   - A hard gate creates false failures and increases user effort (user must rerun).

4. **Why not no validation at all:**
   - 89 lines vs 55 lines is a real instruction-following gap. The model needs a concrete target, not just "one-page".

### Trade-off

| Option | Pro | Con |
|---|---|---|
| Hard ≤40 (old design) | Deterministic, simple | Rejects good output (Frontier 55), false failures, increases user effort |
| Soft 50-70 target + 2x hard cap | Matches empirical "good one-page", allows model flexibility, low false-positive rate | Not perfectly deterministic (but instruction-following for length is inherently soft) |
| No validation | Zero false positives | 89-line output passes — instruction-following gap remains |

**Recommendation:** Soft 50-70 target + repairable hard cap at ~140 lines. The hard cap is repairable (one repair call), not non-repairable, because length compression is a format issue the model can fix, not a factuality issue.

---

## 6. Self-correction/recovery policy (critical rebase of old non-repairable policy)

### The old policy problem

The old design (Section 1.5, 6.4) says: on factual/source-grounded failure, `raise ValueError` and tell the user to modify `quick_brief` or source data before rerunning. This increases user effort and contradicts the M6 goal of proving "reduced user effort".

The old design assumes the source data is the problem. But in the M6 S4 case, the source data was sufficient (product spec had the facts) — the MODEL hallucinated (แม่นยำ, เรียลไทม์, navigation interface). Forcing the user to edit source data when the source is correct is wrong.

### Recommended smallest generic self-correction policy

**Principle: the review/refine phase IS the self-correction mechanism. It is already paid for. The fix is to make it more specific, not to add a new retry loop.**

#### For content_creator (S4) — the main remaining gap:

1. **Enhance the review prompt** (config, not code) to explicitly instruct the reviewer to:
   - Check every caption/script claim against the product spec source text
   - Check every image/video prompt object/feature against the product spec source text
   - Remove or soften any claim not supported by source (replace with "ไม่มีข้อมูลระบุ" or remove)
   - This is a prompt change, not a code change. The review call already exists (max_review_iterations: 1).

2. **Add ONE deterministic post-check** in `content_creator.validate_output` (or a shared validator):
   - Check output caption/script/image_prompts/video_prompts against `brand_rules.hard_dict` (restricted, banned_phrases, replacements)
   - Auto-apply replacements (deterministic, zero cost)
   - For restricted/banned phrases not in source: flag as repairable → one repair call with the specific phrase and instruction to remove/replace
   - This is generic: it reads from terms.json (source data), not a hardcoded list

3. **No new retry loop.** The existing review (1 call) + repair (up to max_retry_limit) is sufficient. The fix is targeting the review prompt and adding a deterministic brand-hard check, not adding iterations.

#### For product_spec (S1):

1. **Prompt enhancement** — translate "one-page" to concrete target (Section 5).
2. **Soft length validation** — repairable if >2x target.
3. No new retry loop.

#### For brand hard-term violations (all agents):

1. **Deterministic auto-replacement** for `replacements` (old → new) — zero cost, no LLM call.
2. **Deterministic flag** for `restricted`/`banned_phrases` not in source — repairable, one repair call.
3. This replaces the old `BrandHardValidator` concept but keeps it generic (reads terms.json, not hardcoded).

#### Cost behavior summary

| Failure type | Mechanism | Paid calls |
|---|---|---|
| Brand `replacements` | Auto-replace in code | 0 |
| Brand `restricted`/`banned` not in source | Flag → 1 repair call | ≤1 |
| S4 unsupported claim (not brand-related) | Review prompt enhancement (existing review call) | 0 extra (review already runs) |
| S1 one-page too long (>2x target) | Flag → 1 repair call | ≤1 |
| S2 fact/provenance (competitor) | Already handled by G2 evidence mode | 0 (max_retry_limit: 0) |
| JSON/section/format | Existing repair loop | ≤max_retry_limit |

**This preserves factuality without creating uncontrolled paid retry loops and without forcing the user to edit source data when the source is correct.**

---

## 7. Proposed generic remediation architecture

### Design principle

Code verifies only what can be verified deterministically from source data. The model keeps free-form generation capability. No product IDs, brand names, category keywords, or category-specific templates.

### Layer 1: Prompt-level steering (no code change for existing agents)

| Agent | Change | File |
|---|---|---|
| product_spec | Translate "one-page" quick_brief to concrete 50-70 line target before LLM call | `src/agents/product_spec.py` (normalize_quick_brief) |
| content_creator | Enhance review_prompt to explicitly check caption/script/image-prompt claims against product spec source | `config/agents.yaml` (content_creator.review_prompt) |
| product_spec | Enhance review_prompt to check length against one-page target when active | `config/agents.yaml` (product_spec.review_prompt) |

### Layer 2: Deterministic post-generation validation (shared, source-driven)

| Validator | Input | Rules | Cost |
|---|---|---|---|
| `BrandHardValidator` | output text, `brand_rules.hard_dict` | 1. Auto-apply replacements (old→new). 2. Flag restricted/banned phrases not in source text. | 0 for replacements; ≤1 repair for restricted/banned |
| `OnePageSoftValidator` | output text, quick_brief, source_text | If one-page intent active: flag if line count > 2x upper target. Repairable. | ≤1 repair |
| `ContentClaimValidator` (content_creator only) | output JSON (caption, script, image_prompts, video_prompts), source_text | Flag superlative/precision claims that use brand `restricted` terms not in source. Does NOT check arbitrary vocabulary — only terms.json-driven. | ≤1 repair |

**What is NOT in Layer 2:**
- No `forbidden_claims` static vocabulary (วิดีโอคอล, เรียลไทม์, navigation interface, etc.)
- No `technical_attribute_labels` (CPU/OS/RAM/ROM)
- No category-specific templates
- No raw substring presence as sufficient proof (the validator checks brand terms.json, which is explicit source data)

### Layer 3: Error classification in BaseAgent.run

| Error prefix | Classification | Behavior |
|---|---|---|
| `brand-hard-replacement` | Auto-fixable | Auto-replace, re-validate, no LLM call |
| `brand-hard-restricted` | Repairable | 1 repair call with specific phrase |
| `one-page-soft` | Repairable | 1 repair call with length target |
| `content-claim` | Repairable | 1 repair call with specific claim |
| `json-invalid` / `missing-section` / `output-format` | Repairable | Existing repair loop |
| competitor evidence errors | Already handled | competitor_analysis custom path (max_retry_limit: 0) |

**No non-repairable `ValueError` that forces user to edit source.** All failures are either auto-fixed or go through the existing repair mechanism with a targeted error message.

### Deterministic validation boundary vs model/self-review boundary

| What code verifies deterministically | What the model/reviewer handles |
|---|---|
| Brand `replacements` (old→new) — exact string match | Claim grounding against source (review prompt instructs this) |
| Brand `restricted`/`banned` phrases in output — exact string match | Tone, style, structure, creativity |
| Line count when one-page intent is active — integer comparison | Content selection, what to include/compress |
| JSON schema structure (existing) | Caption/script/image-prompt content quality |
| competitor evidence provenance (G2 — already in place) | Competitor discovery, evidence selection |

**The code does NOT attempt to verify semantic claim grounding** (e.g. "is แม่นยำ supported by source?") because that requires NLP/semantic understanding that is not deterministic. Instead, the review prompt instructs the reviewer LLM to do this, and the deterministic validator catches only brand hard-term violations (which are explicit source data).

---

## 8. Stale/overfit parts of M6_REMEDIATION_DESIGN.md that must NOT be implemented

| Section | What it proposes | Why it must NOT be implemented |
|---|---|---|
| 1.4 | Per-agent `forbidden_claims` list (วิดีโอคอล, เรียลไทม์, navigation interface, safety GPS tracking map) | Static vocabulary overfit to K2/smartwatch. Would need to grow for every domain. Weaker than G2 provenance contract (for competitor) and brand terms.json (for content). |
| 1.4 | `technical_attribute_labels` from product_db (CPU, OS, RAM, ROM, หน้าจอ, กล้องหน้า, แบตเตอรี่) | Category-specific fixed catalog. Conflicts with G2 open field schema. Would not work for restaurant/apparel/SaaS. |
| 1.4 Rule 4 | Comparison claim patterns ("...สูงกว่า...", "...ดีกว่า...") with attribute-in-source check | NLP heuristic that is not deterministic for Thai text. The G2 provenance contract handles this better for competitor. For content_creator, the review prompt is the right mechanism. |
| 1.5 | `raise ValueError` + "กรุณาตรวจสอบ quick_brief หรือข้อมูลต้นทางก่อนรันใหม่" | Increases user effort when source is correct and model hallucinated. Contradicts M6 "reduced user effort" goal. |
| 2.2-2.4 | Hard `≤40 บรรทัด` one-page limit | Rejects the Frontier output (55 lines) that the design itself says should pass. See Section 5. |
| 3.1-3.4 | `SelectedProductCoverageValidator` with `technical_attribute_labels` and `fact_coverage_mode` | Obsolete — G2 evidence mode + CompetitorReportRenderer already handles this with open field schema. |
| 6.3 | `non_repairable_error_prefixes` config with factual/source/brand/one-page as non-repairable | One-page and brand-restricted are repairable (model can compress/replace). Only true factuality failures (competitor) are non-repairable, and those are already handled by competitor_analysis max_retry_limit: 0. |
| 6.4 | `"กรุณาปรับ quick_brief, ข้อมูลสินค้า หรือแหล่งข้อมูลต้นทางก่อนรันใหม่"` | Same user-effort problem as 1.5. |

---

## 9. Retry/cost behavior

### Current (HEAD)

| Agent | max_retry_limit | max_review_iterations | Cost control |
|---|---|---|---|
| product_spec | 3 | 1 | Unbounded repair on any error |
| competitor_analysis | 0 | 0 | Controlled (evidence mode, deterministic fallbacks) |
| campaign_strategy | 3 | 0 | Unbounded repair on any error |
| content_creator | 3 | 1 | Unbounded repair on any error |

### Proposed (after remediation)

| Agent | max_retry_limit | max_review_iterations | Cost control |
|---|---|---|---|
| product_spec | 3 (unchanged) | 1 (unchanged) | Brand auto-replacement (0 cost); one-page soft flag → ≤1 repair |
| competitor_analysis | 0 (unchanged) | 0 (unchanged) | Already controlled by G2 |
| campaign_strategy | 3 (unchanged) | 0 (unchanged) | Brand auto-replacement (0 cost) |
| content_creator | 3 (unchanged) | 1 (unchanged) | Brand auto-replacement (0 cost); brand-restricted flag → ≤1 repair; claim grounding via enhanced review prompt (0 extra cost) |

**No global max_retry_limit change. No new retry loops. The existing review + repair mechanism is targeted, not expanded.**

---

## 10. Regression strategy

### Archived M6 failures as fixtures

Use the archived M6 outputs as offline test fixtures (no model calls):

| Test | Fixture | Asserts |
|---|---|---|
| `test_s1_one_page_soft_validation` | S1_Y (89 lines), S1_X (55 lines) | 89 lines triggers soft flag; 55 lines passes |
| `test_s4_brand_restricted_in_content` | S4_Y (แม่นยำ, เรียลไทม์, อัจฉริยะ if in terms.json restricted) | BrandHardValidator flags restricted phrases in caption/script/image_prompts |
| `test_brand_replacement_auto_apply` | Synthetic output with replacement old→new | Auto-replacement applies without LLM call |
| `test_competitor_evidence_mode_no_regression` | S2 inputs via FakeLLM | G2 evidence mode still produces provenance-validated output |

### G3 cross-domain coverage

The remediation must not break G3 tests. Run the full G3 suite:
- `tests/test_g1_runtime_contract.py` — brand_dir propagation, category as metadata
- `tests/test_g2_cross_domain.py` — canonicalization, no-match, geography, cross-competitor attribution
- `tests/test_g3_cross_domain_integration.py` — restaurant/apparel/SaaS/unknown through production paths

### New cross-domain tests for remediation

| Test | Domain | Asserts |
|---|---|---|
| `test_one_page_restaurant` | Restaurant product spec | one-page normalization works, no smartwatch assumptions |
| `test_one_page_saas` | SaaS product spec | one-page normalization works, no technical attribute labels |
| `test_brand_hard_validator_apparel` | Apparel brand terms.json | BrandHardValidator works with apparel-specific restricted terms |
| `test_content_claim_validator_unknown_domain` | Unknown product | No hardcoded vocabulary, only terms.json-driven checks |

---

## 11. Exact production files that would need changes in the next implementation phase

| File | Change | Layer |
|---|---|---|
| `src/agents/product_spec.py` | Add `_normalize_quick_brief` — translate "one-page" to concrete 50-70 line target | Prompt |
| `src/output_validators.py` | Add `_brand_hard_validator` (auto-replace + flag restricted/banned) and `_one_page_soft_validator` (line count when one-page active) | Deterministic |
| `src/agents/base_agent.py` | Add error classification in repair loop: auto-fix replacements, classify brand-restricted/one-page as repairable | Error classification |
| `config/agents.yaml` | Enhance `content_creator.review_prompt` and `product_spec.review_prompt` with explicit source-grounding instructions | Prompt |
| `config/agents.yaml` | (Optional) Add `one_page` config block under product_spec with `soft_target_lines` and `hard_cap_multiplier` | Config (no hardcode) |

**Files that do NOT need changes:**
- `src/agents/competitor_analysis.py` — already source-driven (G2)
- `src/agents/competitor_evidence.py` — already source-driven (G2)
- `src/agents/campaign_strategy.py` — no M6 finding (S3 won)
- `src/brand_priority.py` — already loads hard_dict correctly
- `src/product_db.py` — already source-driven
- `src/run_context.py` — already carries brand_dir

---

## 12. Trade-offs

| Decision | Pro | Con |
|---|---|---|
| Soft one-page target (50-70) vs hard 40 | Matches empirical good output, low false-positive rate | Not perfectly deterministic; relies on model compliance |
| Brand auto-replacement vs LLM repair | Zero cost, deterministic, immediate | May change output semantics in edge cases (mitigated: replacements are explicit brand source data) |
| Review prompt enhancement vs new content validator | Zero new code, uses existing review call, generic | Relies on LLM reviewer compliance (not deterministic); brand-hard check is the deterministic backstop |
| No non-repairable ValueError | Preserves user effort, model self-corrects | Rare case where model repeatedly hallucinates same claim → repair loop exhausts max_retry_limit → raises ValueError (acceptable: this is a model failure, not a source failure) |
| Open field schema (G2, kept) | Works for any domain | No fixed spec coverage guarantee (acceptable: provenance contract is stronger) |

---

## 13. Recommended P0/P1 order

### P0 (must fix before re-evaluation)

1. **Brand hard-term deterministic enforcement** — `BrandHardValidator` with auto-replacement + restricted/banned flagging. Generic (terms.json-driven). Cross-scenario impact (S1, S2, S4 brand gate). Zero cost for replacements.
2. **S4 content_creator source-grounding** — enhance review_prompt to check caption/script/image-prompt claims against product spec. This is the biggest remaining factuality gap.
3. **S1 one-page soft validation** — normalize "one-page" to concrete target + soft line-count check. Fixes instruction-following gap.

### P1 (strongly recommended)

4. **Error classification in BaseAgent.run** — auto-fix replacements, classify errors as repairable/non-repairable. Prevents unbounded paid resample on brand/length failures.
5. **S4 image/video prompt brand-term check** — extend BrandHardValidator to check image_prompts/video_prompts fields in content_creator JSON.

### P2 (nice to have)

6. **Rubric clarification for S4 budget/KPI** — document that content_creator is not responsible for campaign budget/KPI, for future judges.
7. **Offline self-consistency tests** — unit tests that read archived M6 outputs as fixtures without calling the model.

---

## 14. Confirmation

- **No production code, config, or test changes were made.**
- **No model, OpenRouter, web, image, video, or other paid/free external calls were made.**
- **No commit or push was made.**
- This report is analysis only, based on reading current source code at HEAD `557ab77b42428e82611152f2e87989130626fef1`, archived M6 artifacts, and historical M6 documents.
- Implementation has NOT started. Waiting for Product Owner/Codex checkpoint.
