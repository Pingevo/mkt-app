# M6.1 Final Failure-Attribution Audit

**Run:** `data/m6_frontier_uat/20260902_070643/`
**Mode:** Offline-only (no model calls, no paid calls, no rerun/rejudge)
**Purpose:** Separate real production gaps from judge/rubric mismatches and produce a safe remediation backlog.

---

## 1. Method

- Compared exact inputs (`m6_inputs.json`), effective settings (harness code), MKTApp outputs (`S{1..4}_Y.txt`), Frontier outputs (`S{1..4}_X.txt`) and judge rationale (`m6_judge_scores.json`).
- Each finding classified as: **valid production gap**, **model-inference acceptable**, or **judge/rubric mismatch**.
- Owner layer is fixed at the smallest place that can prevent the issue without changing model or rubric.

---

## 2. Verified facts

| Fact | Source |
|---|---|
| S1 quick_brief = `"one-page"` | `m6_inputs.json` S1 |
| S2 quick_brief = `"สรุปแบบ bullet executive brief ห้ามใช้ตาราง"` | `m6_inputs.json` S2 |
| S3 budget/tactics were sent via `agent_settings_override` (UI contract) | `qual_runner` + `_parse_agent_settings_override` |
| S4 user request = `"สร้างโพสต์ TikTok 1 โพสต์"`, quick_brief = child safety | `m6_inputs.json` S4 |
| Brand guidelines loaded for all sides | `m6_frontier_uat._get_brand_guidelines()`, `m6_judge_runner._get_brand_guidelines()` |
| Product images sent as data URLs to MKTApp, Frontier, judge | `build_multimodal_content()` in `src/run_context.py` and harness tests |
| `SOURCE_GROUNDED_RULE` was in MKTApp prompt and Frontier system | harness code + judge source pack |

---

## 3. Per-scenario attribution

### S1 one-page

**Finding:** MKTApp produced 90 lines; Frontier 56 lines. MKTApp not one-page.

**Root cause:** The prompt constraint `"one-page"` is too abstract for `product_spec`. No line/token ceiling is enforced by the agent or renderer.

**Valid/invalid:** Valid production gap.

**Owner layer:** Input / prompt.

**Smallest safe fix:**
- Translate `one-page` quick_brief into a concrete, measurable instruction before appending to the prompt:
  `"จัดทำสเปคหนึ่งหน้า A4 ไม่เกิน 40 บรรทัด หรือประมาณ 600 คำ"`.
- Alternatively, add `max_lines`/`max_words` to `product_spec` agent settings and append to the prompt.
- Do **not** create a fixed template; keep free-form output with a length ceiling.

**Regression test:**
- `tests/test_product_spec_one_page.py`: run `qual_runner` dry-run or static check that prompt contains "ไม่เกิน 40 บรรทัด".
- `tests/test_s1_mktapp_output_length.py`: after a paid rerun, assert ≤ 60 lines.

**Expected M6 rubric impact:** +Instruction following, +User effort on S1.

**Paid rerun needed?** Yes, to measure actual output length under the new prompt.

---

### S2 hallucination / source-grounding

**Unsupported MKTApp claims found by judge and by manual check:**

| Claim in MKTApp S2 output | Issue | Deterministic guard should catch? |
|---|---|---|
| `ชิปเซ็ต / OS: -` for K2 | Source pack has W377 / Android 8.1 | Yes — product data missing from output is a schema/retrieval bug |
| `กล้องหน้า 5MP ของ K2 เพื่อการสนทนาและการถ่ายภาพ` | Implies video call; not supported | Yes — forbidden term "สนทนา" near camera |
| `Android 8.1 ... อาจส่งผลให้มีการใช้พลังงานที่สูงกว่า` | Battery/performance speculation | Yes — speculative phrasing "อาจ" without data |
| `K2 ใช้ระบบปฏิบัติการและหน้าจอที่กินไฟมากกว่า` | Not in source pack | Yes — "กินไฟ" without test data |
| `หาก K2 ... ต่ำกว่า 4,000 บาท` | Price speculation | Yes — numbers not from source |

**Acceptable inference (judge allowed):**
- Comparison of screen size/resolution/brightness from source pack is acceptable.
- Stating K2 has no competitor data for pricing is acceptable.

**Root cause:** `competitor_analysis` prompt has no deterministic fact validator. It asks the model to reason about K2 vs competitors but does not reject/resample unsupported claims.

**Valid/invalid:** Valid production gap.

**Owner layer:** Model choice + missing validator.

**Smallest safe fix:**
1. Add a post-generation deterministic guard:
   - Parse output for terms in `terms.json` `restricted` and for unsupported superlatives (`อัจฉริยะ`, `แม่นยำ`, `เรียลไทม์`, `กินไฟ`, `สนทนา`, `วิดีโอคอล` when not in source).
   - If found, resample or append a correction instruction and regenerate (without retry loops beyond the existing stage A ceiling).
2. Add `forbidden_claims` list per agent, loaded from `terms.json` + `restricted` words.
3. Ensure product context is injected with a forced `## สินค้าของเรา` schema so K2 specs are not omitted.

**Regression test:**
- `tests/test_competitor_no_video_call.py`: assert output does not claim K2 supports video call.
- `tests/test_competitor_k2_specs_present.py`: assert K2 CPU, OS, battery are included.

**Expected M6 rubric impact:** +Factuality, +Evidence quality on S2.

**Paid rerun needed?** Yes — to confirm hallucination rate drops.

---

### S4 content creator

**Valid production gaps (over-claims):**

| MKTApp S4 claim | Issue |
|---|---|
| `ระบุตำแหน่งแม่นยำ` | No precision data in source pack |
| `พร้อมส่งข้อความเสียง` | Source pack has Family Group Chat, no voice message |
| `รู้พิกัดเรียลไทม์` | No real-time latency data |
| `ระบบระบุตำแหน่งอัจฉริยะ` | "อัจฉริยะ" not supported |
| `หน้าจอนาฬิกาแสดง navigation interface` / `safety GPS tracking map` | No navigation UI data |
| Image prompt: `navigation interface`, `safety GPS tracking map` | Same as above |

**Invalid / rubric mismatch:**
- Judge expects `5,000 บาท` budget and concrete KPIs in S4 output.
- **S4 user request was to create one TikTok post for a child-safety theme.**
- **Budget and KPI are `campaign_strategy` (S3) requirements, not `content_creator` requirements.**
- Therefore the "no budget/KPI" penalty is a judge/rubric mismatch, not a production defect.

**Root cause:** Content creator has no source-grounded term validator for captions and image/video prompts.

**Valid/invalid:** Mixed — valid over-claims, invalid budget/KPI expectation.

**Owner layer:**
- Valid: prompt / content validator.
- Invalid: rubric/judge.

**Smallest safe fix:**
1. Add a `content_creator` post validator that checks caption/script/image prompts against `terms.json` and a hard-coded `forbidden_superlatives` list.
2. Add brand `approved` terms to the prompt and ask the model to prefer them.
3. Do **not** add budget/KPI to content creator; leave that to `campaign_strategy`.

**Regression test:**
- `tests/test_content_no_unsupported_superlatives.py`: assert no "แม่นยำ", "เรียลไทม์", "อัจฉริยะ", "ข้อความเสียง" when not in source.
- `tests/test_content_no_budget_kpi_required.py`: assert content creator score not penalized for missing campaign budget.

**Expected M6 rubric impact:**
- Valid: +Factuality, +Brand on S4.
- Invalid: would improve S4 if rubric is fixed.

**Paid rerun needed?** Yes for the valid fix; rubric fix can be validated offline.

---

### S3 campaign strategy

**Finding:** MKTApp won S3. No production gap found in the output.

**Note:** The output did not show explicit `5,000` baht allocation. The judge still scored MKTApp higher on Factuality/Instruction following because it avoided unsupported claims. If future rubrics require concrete budget numbers, that becomes a new requirement, not a defect.

**Valid/invalid:** No finding.

---

## 4. Brand / asset fit gap

### What the judge had
- Full brand profile, tone of voice, visual guidelines, `terms.json`.

### What MKTApp failed to use
- **S1:** English-heavy technical terms (`Oncell Full Lamination`, `AF Coating`, `IC Chipsemi`) dominate; parent-friendly warmth missing.
- **S2:** Uses aggressive comparison and competitor pricing without clear source; no warm "คุณพ่อคุณแม่" framing.
- **S4:** Caption uses unsupported superlatives (`แม่นยำ`, `เรียลไทม์`) and image prompts invent unsupported UI (`navigation interface`).

### Valid/invalid
Valid production gap: the brand terms are available but not enforced by a validator.

### Smallest safe fix
- Add `terms.json` validator (approved/restricted/replacements) per agent.
- Add sample brand-acceptable phrases to `product_spec`, `competitor_analysis`, and `content_creator` prompts.
- Restrict `navigation interface`, `safety GPS tracking map` in image/video prompt validator.

### Regression test
- `tests/test_output_brand_terms.py`: no restricted words, approved words present.
- `tests/test_image_prompts_no_unsupported_ui.py`: no UI elements not in source pack.

### Expected M6 rubric impact
+Brand / asset fit on S1, S2, S4.

### Paid rerun needed?
Yes.

---

## 5. Remediation matrix

| # | Issue | Valid? | Owner layer | Smallest safe fix | Regression test | Expected rubric impact | Paid rerun? |
|---|---|---|---|---|---|---|---|
| 1 | S1 one-page too long | Valid | Input / prompt | Rewrite `one-page` to `≤ 40 บรรทัด` or add `max_lines` setting | `test_s1_output_under_60_lines` | +Instruction following, +User effort (S1) | Yes |
| 2 | S2 missing K2 CPU/OS in product summary | Valid | Model / prompt schema | Force `## สินค้าของเรา` schema with required fields | `test_competitor_k2_specs_present` | +Factuality (S2) | Yes |
| 3 | S2 video call / performance / price claims | Valid | Missing validator | Add deterministic forbidden-claim validator + resample | `test_competitor_no_video_call` | +Factuality, +Evidence (S2) | Yes |
| 4 | S4 unsupported superlatives (AI, real-time, voice) | Valid | Missing validator | Add `content_creator` term/claim validator | `test_content_no_unsupported_superlatives` | +Factuality, +Brand (S4) | Yes |
| 5 | S4 image prompts invent UI | Valid | Missing validator | Restrict image prompt objects to source pack features | `test_image_prompts_no_unsupported_ui` | +Brand, +Evidence (S4) | Yes |
| 6 | S4 missing budget/KPI | **Invalid** | Rubric / judge | Do **not** fix | `test_content_no_budget_kpi_required` | N/A (rubric fix) | No |
| 7 | Brand terms not enforced | Valid | Prompt / validator | Load `terms.json` into validator and prompt | `test_output_brand_terms` | +Brand (S1, S2, S4) | Yes |
| 8 | S4 visual prompt not matching brand colors | Partially valid | Prompt | Add "no specific colors unless in source" to image prompt | `test_image_prompts_no_invented_colors` | +Brand (S4) | Yes |

---

## 6. P0 / P1 / P2 remediation backlog

### P0 (must fix before any re-evaluation)
1. **#1 S1 length** — small prompt/setting change, high rubric impact.
2. **#3 S2 forbidden-claim validator** — prevents the biggest factuality loss.
3. **#4 / #5 S4 content/image prompt validator** — fixes over-claims that damage Factuality and Brand.
4. **#7 Brand terms validator** — cross-scenario impact.

### P1 (strongly recommended)
5. **#2 S2 product schema** — ensure K2 specs are not omitted.
6. **#8 S4 color/UI hallucination in image prompts** — visual guideline alignment.

### P2 (nice to have)
7. Rubric clarification for #6 (S4 budget/KPI) so future judges do not apply campaign requirements to content creator.
8. Add offline self-consistency unit tests for each agent that read `m6_judge_scores.json` as a reference, without calling the model.

---

## 7. Offline remediation plan before paid re-evaluation

### Step 1: Unit tests (no paid calls)
- Add the regression tests listed above using the existing artifacts as fixtures.
- Each test asserts the production code would reject the exact failing outputs from `20260902_070643`.

### Step 2: Implement the validators (no paid calls)
- Add a shared `claim_guard.py` or extend `BaseAgent` with a `validate_output` hook.
- Load `terms.json` and forbidden terms per agent.
- Implement resample logic that fits within the existing stage A call budget.

### Step 3: Prompt updates (no paid calls)
- Update `quick_brief` mapping for `one-page` to a concrete length limit.
- Add brand-acceptable phrase examples and restricted-term warnings to each agent prompt.

### Step 4: Offline verification
- Run the full offline test suite (`pytest`) and `git diff --check`.
- Re-run `m6_frontier_uat.py --dry-run` to ensure prompts contain the new constraints.

### Step 5: Request paid re-evaluation
- Only after Step 1–4 pass, request a new M6.1 cap for paired rerun + judge.
- Do not reuse the `20260902_070643` outputs.

---

## 8. M6 status (corrected)

- **M6.1 fair rerun + judge completed** under `data/m6_frontier_uat/20260902_070643/`.
- **Final verdict: FAIL** (hard gate S1/S2/S4, brand gate 1/4, overall mean delta −0.542).
- **Failure is mostly attributable to deterministic, fixable production gaps:**
  - No length enforcement for one-page.
  - No forbidden-claim validator for competitor / content agents.
  - Missing product-spec schema enforcement.
  - Brand terms not validated.
- **One finding is a rubric mismatch:** S4 content creator judged for missing campaign budget/KPI.
- **No production code/config was changed during this audit; no commit/push.**

---

## 9. No paid call confirmation

- This audit used only local files, JSON outputs, and code inspection.
- No model calls, web calls, image/video generation, or paid API usage occurred.
- The only paid calls in this session were the already-completed M6.1 paired rerun and the S4 judge call.
