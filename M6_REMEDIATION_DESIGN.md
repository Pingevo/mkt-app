# M6 Remediation Design Review (Revised)

**Scope:** Offline design only. No production code/config/tests modified, no model/tool/image/video calls, no commit/push.

**Source:** `M6_FINAL_FAILURE_ATTRIBUTION_REPORT.md` and `data/m6_frontier_uat/20260902_070643/`

**Design constraints (mandatory):**
1. `one-page ≤ 40 lines` is an explicit user intent, interpreted inside the production `BaseAgent`/agent path; not a default template or global cap.
2. S2 missing facts use a conditional coverage rule, not a fixed section/schema; technical/spec comparisons must ground selected-product facts, while positioning/executive briefs only ground the claims they actually make.
3. S2/S4 unsupported claims use deterministic source-grounded validation. Factual/source-grounding failures stop without paid resample; transient/transport/format failures keep existing retry behavior.
4. Brand term enforcement uses only `brand/terms.json` hard rules (`restricted`, `banned_phrases`, `replacements`). No generic style validator.
5. S4 budget/KPI is not in remediation scope.

---

## 1. Priority P0 — Source-grounded / unsupported factual claims

### 1.1 Production file/function

- `src/output_validators.py` — `validate_output(...)`
- `src/agents/base_agent.py` — `BaseAgent.validate_output(...)`, `BaseAgent.run(...)`
- `src/agents/product_spec.py`, `src/agents/competitor_analysis.py`, `src/agents/content_creator.py` — `validate_output` overrides if agent-specific logic needed
- `config/agents.yaml` — `output_quality` and `non_repairable_error_prefixes`
- `brand/terms.json` — `restricted` list

### 1.2 Behavior before/after

| Before | After |
|---|---|
| `BaseAgent.validate_output` only checks sections, JSON, and `output_quality` length/structure. | `validate_output` also runs deterministic `SourceGroundedValidator` and `BrandHardValidator`. |
| `_repair_output` treats all validation errors the same and may call LLM up to `max_retry_limit=3`. | Validation errors are classified. Factual/source-grounded failures skip `_repair_output` and raise `ValueError`. Format/transport failures keep existing repair/retry. |
| Model can emit `วิดีโอคอล`, `เรียลไทม์`, `อัจฉริยะ`, `navigation interface`, `safety GPS tracking map` with no consequence. | Any output containing a source-grounded forbidden phrase without that phrase appearing in the source pack fails validation. |

### 1.3 Why not a fixed template or rewrite

- The `BaseAgent.run` pipeline already ends with `validate_output` and a repair loop. We reuse this seam.
- No output format is prescribed. The model keeps free-form generation; the validator only rejects outputs that contain known bad phrases or make ungrounded technical claims.
- Factual failures are non-repairable because LLM resampling is not guaranteed to be truthful and costs money.

### 1.4 Deterministic validation boundary

```text
Input to validator:
  - output text
  - source pack text (the user_prompt the agent received)
  - terms.json hard_dict (restricted, banned_phrases, replacements)
  - per-agent forbidden_claims list, e.g.
      competitor_analysis: ["วิดีโอคอล", "video call", "เรียลไทม์"]
      content_creator:    ["เรียลไทม์", "อัจฉริยะ", "navigation interface", "safety GPS tracking map"]
  - technical_attribute_labels from product_db, e.g.
      ["CPU", "OS", "RAM", "ROM", "หน้าจอ", "กล้องหน้า", "แบตเตอรี่"]

Rules:
  1. For each forbidden phrase: if output contains phrase but source pack does NOT contain it → FAIL.
  2. For each replacement (old → new): if output contains `old` → FAIL.
  3. For each technical attribute label in output: the value after the label must appear in the source pack or be explicitly marked "ไม่มีข้อมูลระบุ".
  4. For each technical advantage/comparison claim (patterns like "...สูงกว่า...", "...ดีกว่า...", "...มี...", "...ไม่มี..."): the claimed attribute must appear in source pack.
  5. Restricted and banned phrases from `terms.json` are added to the forbidden list automatically.
  6. Case-insensitive, whitespace-insensitive, phrase match within word boundaries.

Output:
  - (True, "") if all pass
  - (False, "source-grounded: <offending_claim>") if fail
  - (False, "ungrounded-advantage: <claim>") for unsupported comparison
```

### 1.5 UI failure state

- Factual failure returns an error message to the user:
  `"ผลงานมีข้อกล่าวอ้างทีไม่มีข้อมูลต้นทางรองรับ: <offending_claim>. กรุณาตรวจสอบ quick_brief หรือข้อมูลต้นทางก่อนรันใหม่."`
- No output is saved or displayed; user must modify input or source pack.

### 1.6 Regression tests

- `tests/test_competitor_analysis_no_video_call.py`: run `competitor_analysis` with M6 S2 inputs; assert validator rejects `เพื่อการสนทนา` and `วิดีโอคอล`.
- `tests/test_competitor_analysis_k2_os.py`: assert output does not say `ชิปเซ็ต / OS: -` when source pack has `W377` and `Android 8.1`.
- `tests/test_content_creator_no_unsupported_ui.py`: assert `navigation interface`, `เรียลไทม์`, `อัจฉริยะ` are rejected.
- `tests/test_validate_output_source_grounded_unit.py`: unit test `output_validators.validate_output` with known output/source pack pairs.

### 1.7 Offline proof plan

- Run unit tests with the M6 S2 and S4 output files as fixtures.
- Each test asserts the new `validate_output` would have rejected the exact failing outputs.
- No model call; deterministic functions only.

### 1.8 Paid-call behavior on failure

- Factual/source-grounded failure: `BaseAgent.run` skips `_repair_output`; no extra LLM call.
- Format/JSON failure: existing `_repair_output` may run up to `max_retry_limit` (config default).
- Transport/API failure: existing `LLMClient` retry behavior.

---

## 2. Priority P1 — One-page as explicit user intent in production path

### 2.1 Production file/function

- `src/agents/product_spec.py` — `ProductSpecAgent.normalize_quick_brief(...)` or `_build_system_prompt`/`_build_user_prompt`
- `src/agents/base_agent.py` — `BaseAgent.run(...)` stores `quick_brief` for `validate_output`
- `src/output_validators.py` — `validate_output(...)` line-count gate

### 2.2 Behavior before/after

| Before | After |
|---|---|
| `quick_brief="one-page"` is passed literally. | `product_spec` agent detects one-page intent and appends a concrete, measurable instruction: `"จัดทำสเปคหนึ่งหน้า ไม่เกิน 40 บรรทัด โดยไม่สร้างเอกสารหลายหน้า"`. |
| No length validation. | `validate_output` counts lines if and only if one-page intent is active. |

### 2.3 Why not a fixed template or global cap

- Only `product_spec` and only when the user asks for one-page explicitly.
- The limit is stored in the prompt as a user instruction, not a system-wide `max_lines`.
- Other product_spec calls and other agents keep existing behavior.
- No required section headings or Markdown templates.

### 2.4 Deterministic validation boundary

```text
One-page intent detection:
  - agent_name == "product_spec"
  - quick_brief matches regex: r"(?i)(one[- ]?page|หน้าเดียว|หน้าเดียวเท่านั้น|one page)"

Production path:
  1. Agent normalizes quick_brief before LLM call:
       quick_brief = f"{quick_brief}\nหมายเหตุ: จัดทำสเปคหนึ่งหน้า ไม่เกิน 40 บรรทัด"
  2. BaseAgent appends this normalized quick_brief to user prompt as before.
  3. validate_output checks len(output.splitlines()) <= 40 when one-page intent is active.

Regex for the 40-line contract:
  r"(one[- ]?page|หน้าเดียว).*?ไม่เกิน\s*(\d+)\s*บรรทัด"
```

### 2.5 Regression tests

- `tests/test_product_spec_one_page_production.py`: run `Orchestrator.run_product_spec(raw_data, images, quick_brief="one-page")`; assert the final prompt includes `ไม่เกิน 40 บรรทัด` and `validate_output` rejects a 90-line output.
- `tests/test_product_spec_default_no_cap.py`: assert `quick_brief=""` does not trigger the 40-line cap.
- `tests/test_orchestrator_one_page.py`: production-equivalent route, not harness-only.

### 2.6 Effect on user-facing output

- When user asks for one-page, the agent receives a concrete deliverable size and is hard-validated against it.
- Failure returns an explicit error; no truncated-or-saved output.

### 2.7 Offline proof plan

- Unit tests on M6 S1 MKTApp output (90 lines) and Frontier output (56 lines).
- Test that `validate_output` rejects the MKTApp output and passes the Frontier output.

### 2.8 Paid-call behavior on failure

- Line-count failure is a factual/instruction-following failure; it is non-repairable.
- `BaseAgent.run` skips `_repair_output`; no extra LLM call.

---

## 3. Priority P1 — Conditional selected-product fact coverage

### 3.1 Production file/function

- `src/output_validators.py` — `validate_output(...)` with `SelectedProductCoverageValidator`
- `src/product_db.py` — `get_scoped_context_text` / `get_product_summary` to derive technical attributes
- `config/agents.yaml` — `competitor_analysis.output_quality.fact_coverage_mode = "conditional"`

### 3.2 Behavior before/after

| Before | After |
|---|---|
| `competitor_analysis` can output `ชิปเซ็ต / OS: -` for K2. | If the output contains a technical attribute label, its value must be in the source pack or explicitly marked as not available. |
| Blanket `W377` / `Android 8.1` required in every output. | No blanket list. Technical facts are only enforced when the output makes a technical/spec comparison or claims an advantage about the selected product. |

### 3.3 Why not a user-facing section/schema

- The validator does not require a `## สินค้าของเรา` heading or any fixed structure.
- It only checks that claims about selected-product attributes are grounded.
- The model can still write a pure positioning/executive brief; in that case, the validator is silent unless a technical claim is made.

### 3.4 Deterministic validation boundary

```text
Input to validator:
  - output text
  - source pack text
  - technical_attribute_labels (from product_db for the selected product)
  - user_intent hint from quick_brief, e.g.
      technical/spec intent: contains "สเปค", "เปรียบเทียบ", "spec"
      positioning/executive intent: contains "executive", "positioning", "กลยุทธ์"

Rules:
  1. If technical/spec intent is detected:
     - For each technical attribute label in output, the value must appear in source pack OR be marked "ไม่มีข้อมูลระบุ".
     - If a label is listed but the value is blank, "-", or contradicts source pack → FAIL.
  2. If the output makes a technical advantage/comparison claim about the selected product:
     - The attribute used in the comparison must appear in source pack.
  3. Pure positioning/executive output with no technical claims: PASS.

Example failures from M6 S2:
  - `ชิปเซ็ต / OS: -` for K2 when source pack has `W377` / `Android 8.1` → FAIL (Rule 1).
  - `K2 หน้าจอ AMOLED สูงกว่า` when screen attributes are in source pack → PASS (grounded).
  - `K2 ใช้ระบบปฏิบัติการและหน้าจอที่กินไฟมากกว่า` when power consumption is not in source pack → FAIL (Rule 2).
```

### 3.5 Regression tests

- `tests/test_competitor_analysis_conditional_coverage.py`: assert M6 S2 MKTApp output fails because `ชิปเซ็ต / OS: -` contradicts source pack.
- `tests/test_competitor_positioning_no_blanket.py`: assert a positioning brief with no technical claims passes even if `W377` is not mentioned.
- `tests/test_competitor_ungrounded_advantage.py`: assert an advantage claim using an attribute not in source pack fails.

### 3.6 Effect on user-facing output

- The `competitor_analysis` agent cannot make ungrounded technical comparisons or list `–` for attributes that are in source pack.
- A pure positioning brief is still allowed.

### 3.7 Offline proof plan

- Unit tests on M6 S2 MKTApp and Frontier outputs.
- MKTApp output fails on `OS: -` and ungrounded power/battery claims; Frontier output passes.

### 3.8 Paid-call behavior on failure

- Conditional coverage failure is a factual/source-grounding failure; it is non-repairable.
- `BaseAgent.run` skips `_repair_output`; no extra LLM call.

---

## 4. Priority P2 — Brand hard terms only

Unchanged from previous design. `validate_output` checks `brand/terms.json` `restricted`, `banned_phrases`, and `replacements` only. No generic style validation.

---

## 5. S4 budget/KPI — not in scope

Unchanged. `content_creator` is not responsible for campaign budget/KPI.

---

## 6. Retry / classification policy

### 6.1 Production file/function

- `src/agents/base_agent.py` — `BaseAgent.run(...)` and `BaseAgent.validate_output(...)`
- `src/output_validators.py` — `validate_output(...)` error prefixes
- `config/agents.yaml` — `non_repairable_error_prefixes` per agent

### 6.2 Behavior before/after

| Before | After |
|---|---|
| All `validate_output` failures go through `_repair_output` up to `max_retry_limit=3`. | `validate_output` errors are classified. Deterministic factual/source-grounding/brand failures are non-repairable. Format/JSON/section failures still use `_repair_output`. |
| `max_retry_limit: 0` was proposed globally for several agents. | No global `max_retry_limit: 0`. Factual failures are short-circuited before `_repair_output`. Other errors keep the existing retry limit. |

### 6.3 Deterministic classification

```text
Error prefixes produced by validate_output:
  - "source-grounded: ..."      -> non-repairable
  - "ungrounded-advantage: ..." -> non-repairable
  - "brand-hard: ..."           -> non-repairable
  - "missing-fact: ..."         -> non-repairable
  - "one-page: ..."             -> non-repairable
  - "json-invalid: ..."         -> repairable (existing _repair_output)
  - "missing-section: ..."      -> repairable
  - "output-format: ..."        -> repairable
  - Transport/API errors        -> existing LLMClient retry

BaseAgent.run logic:
  ok, error = self.validate_output(output)
  if not ok:
      if _is_non_repairable(error):
          raise ValueError(error)
      # else existing repair loop up to max_retry_limit
```

### 6.4 UI failure state

- Non-repairable failure raises `ValueError` with a user-facing message:
  `"ผลงานถูกปฏิเสธ: <error>. กรุณาปรับ quick_brief, ข้อมูลสินค้า หรือแหล่งข้อมูลต้นทางก่อนรันใหม่."`
- The UI shows the error and does not save the output.
- No automatic rerun, resample, or paid call expansion.

### 6.5 Why not `max_retry_limit: 0` global

- `max_retry_limit` also handles format/JSON/section mistakes, which are transient or prompt-related and can often be fixed by one repair call.
- Setting it to `0` globally would break that useful behavior and increase user friction for non-factual errors.
- Factual errors are instead detected by their prefix and bypassed.

### 6.6 Regression tests

- `tests/test_base_agent_non_repairable_error.py`: assert `source-grounded: ...` error raises without calling `_repair_output`.
- `tests/test_base_agent_repairable_error.py`: assert `json-invalid: ...` still goes through `_repair_output`.

### 6.7 Paid-call behavior on failure

- Non-repairable (factual/source/brand/one-page): paid cost stops at the current call.
- Repairable (format/section): up to `max_retry_limit` repair calls as before.
- Transport: existing retry/backoff.

---

## 7. Revised design matrix

| # | Issue | Valid? | Owner layer | Smallest safe fix | Regression test | Expected rubric impact | Cost behavior |
|---|---|---|---|---|---|---|---|
| 1 | S1 one-page too long | Valid | `product_spec` prompt + `BaseAgent.validate_output` | `product_spec` normalizes one-page quick_brief to a concrete 40-line instruction before LLM call; validator counts lines only when one-page intent is active | `tests/test_product_spec_one_page_production.py` | +Instruction following, +User effort (S1) | Non-repairable; no extra call |
| 2 | S2 K2 CPU/OS missing/contradicted | Valid | `SourceGroundedValidator` / `SelectedProductCoverageValidator` | Conditional: if output lists a technical attribute, its value must match source pack or be labeled unavailable | `tests/test_competitor_analysis_conditional_coverage.py` | +Factuality (S2) | Non-repairable; no extra call |
| 3 | S2 video call / performance / price claims | Valid | `SourceGroundedValidator` | Forbid phrases not in source pack; reject ungrounded advantage/comparison claims | `tests/test_competitor_analysis_no_video_call.py`, `tests/test_competitor_ungrounded_advantage.py` | +Factuality, +Evidence (S2) | Non-repairable; no extra call |
| 4 | S4 unsupported superlatives (AI, real-time, voice, navigation UI) | Valid | `SourceGroundedValidator` for `content_creator` | Per-agent forbidden-claim list; label or block unsupported phrases in caption/script/image prompts | `tests/test_content_creator_no_unsupported_ui.py` | +Factuality, +Brand (S4) | Non-repairable; no extra call |
| 5 | S4 missing budget/KPI | **Invalid** | Rubric/judge | No production change | `tests/test_content_no_budget_kpi_required.py` | N/A | N/A |
| 6 | Brand hard terms not enforced | Valid | `BrandHardValidator` | Enforce `terms.json` `restricted`, `banned_phrases`, `replacements` deterministically | `tests/test_output_brand_hard_terms.py` | +Brand (S1, S2, S4) | Non-repairable; no extra call |
| 7 | Factual failures currently trigger paid resample | Valid | `BaseAgent.run` retry policy | Classify `validate_output` errors; factual/source/brand/one-page bypass `_repair_output`, format errors keep existing repair | `tests/test_base_agent_non_repairable_error.py`, `tests/test_base_agent_repairable_error.py` | Prevents cost expansion | Factual: stop; Format: existing retry |

---

## 8. Offline proof plan before paid re-evaluation

1. Implement the regression tests above using M6 artifacts as fixtures.
2. Make the minimal code/config changes.
3. Run `pytest` on the new tests.
4. Run the full offline suite.
5. Run `git diff --check`.
6. Run `m6_frontier_uat.py --dry-run` and confirm:
   - `product_spec` one-page prompt contains `ไม่เกิน 40 บรรทัด`.
   - `competitor_analysis` validator triggers on M6 S2 fixtures.
   - `content_creator` validator triggers on M6 S4 fixtures.
7. Only after all pass, request a new M6.1 cap and rerun.

---

## 9. Why this revised design respects the Frontier-style contract

- No output template, no fixed section schema.
- No generic style validator; only hard brand rules.
- One-page cap is triggered by explicit user intent inside the production agent path, not the harness.
- Source-grounded check is negative/deterministic (reject known bad phrases and ungrounded technical claims).
- Factual failures stop without paid resample, while format/transport failures keep their existing behavior.
- S4 budget/KPI is excluded because it is not a `content_creator` requirement.

---

## 10. No paid call confirmation

- This document is a revised design only.
- No model, web, image, video, or other paid tool was called while producing it.
- No production code, config, or tests were modified.
- No commit/push.
