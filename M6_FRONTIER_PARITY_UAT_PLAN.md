# M6 Frontier-Parity UAT Plan

**Status:** Offline plan — no OpenRouter calls, no model/pricing hardcode, no production changes, no commit.  
**Goal:** Design a blind side-by-side non-inferiority study of MKTApp against an OpenRouter Frontier model selected by Product Owner at run time.

---

## 1. UAT Objective

M6 does **not** prove MKTApp is better at every writing task.

It proves MKTApp is **not meaningfully worse** than an accessible Frontier model for the core marketing tasks the Beta UI exposes, and that MKTApp adds value in one or more of the following dimensions:

- product / brand context,
- factual guardrails and evidence,
- brand / asset fit,
- reduced user effort.

No Frontier parity claim may be made before M6 is executed and documented.

---

## 2. Representative Scenarios

| # | Scenario | Source of truth | Exact user-visible input pack | UI selections | Agent Settings | Quick Brief | What must NOT be sent |
|---|----------|----------------|------------------------------|---------------|----------------|-------------|----------------------|
| S1 | Multi-product product brief | product_profile.json + product images + xlsx spec (K2 + K3) | product_ids=["Lagenio K2","Lagenio K3"], product images, no free text other than UI labels | product_spec agent, multi-product selection | (default) | — | No hidden prompt, no validator, no pre-canned competitor list |
| S2 | Competitor analysis with web evidence | Same product pack + approved competitor scope | product_id="Lagenio K2", quick_brief="สรุปแบบ bullet executive brief ห้ามใช้ตาราง" | competitor_analysis agent | competitor_types=["direct"], analysis_depth="deep", importance=["positioning"] | Quick Brief exactly as UI field | No MKTApp evidence pipeline, no pre-fetched URLs, no provenance logic |
| S3 | Campaign with budget / constraints | product pack + real market context | budget="5000 THB", forbid_discount=true, quick_brief="executive brief" | campaign_strategy agent | (default or budget constraints) | Quick Brief exactly as UI field | No validator, no repair loop, no MKTApp campaign template |
| S4 | Content task for one platform | product pack + selected platform | platform="tiktok", content_count=1, auto_image=true, quick_brief="เด็กเดินทางคนเดียวปลอดภัย" | content_creator agent | (default) | Quick Brief exactly as UI field | No MKTApp content schema, no image-prompt template, no asset-id logic |

**Rationale for these scenarios:**
- They cover the four Beta-qualified agents.
- They use the same product/brand fixtures already in the qualification runs.
- They exercise UI-visible controls (product_ids, Quick Brief, Agent Settings, platform, content_count, auto_image).
- They avoid "hidden" MKTApp advantages such as pre-validated evidence or repair loops.

---

## 3. Baseline Levels

### 3.1 Direct-User baseline (minimum credible)

Frontier model receives exactly the same source pack a normal user would have:

- product images,
- product_profile / xlsx as plain text or as the user sees it,
- the same Quick Brief string,
- the same Agent Settings as plain instructions,
- platform/budget constraints as plain text.

**What it measures:** whether MKTApp provides meaningful value beyond a user copy-pasting context into a Frontier chat.

### 3.2 Expert-Prompt baseline (fuller M6)

Frontier model receives the same source pack plus a high-quality prompt from a power user, e.g.:

- role and output format,
- citation rules,
- constraints as explicit do/don't,
- desired tone and structure.

**What it measures:** the gap between an average MKTApp user and a highly skilled Frontier user.

Both baselines **must not** use any of the following:

- MKTApp hidden prompts,
- MKTApp validators,
- MKTApp business logic (e.g. provenance, evidence filtering, content schema),
- Pre-fetched MKTApp artifacts from qualification.

### 3.3 Recommended staged approach

| Stage | Baseline | Purpose | Order |
|-------|----------|---------|-------|
| M6.1 | Direct-User only | Minimum evidence that MKTApp is not inferior to an average user with a Frontier model | First |
| M6.2 | Expert-Prompt | Stress test against the best accessible Frontier output | Optional, after M6.1 passes |

---

## 4. MKTApp Side Protocol

MKTApp outputs must come from the same production-equivalent UI flow:

1. Start from a clean working tree.
2. Use `qual_runner.run_case` or `web_viewer` equivalent with the exact inputs above.
3. Capture the same artifacts as qualification:
   - `*_output.txt` (user-facing output),
   - `*_evidence.json` (reproducibility, not for scoring),
   - `*_image_*.png` if generated.
4. Do **not** show internal JSON, evidence JSON, or validator logs to reviewers.
5. Do **not** edit prompts, config, or code between runs.

---

## 5. Blind Side-by-Side Review

### 5.1 Anonymization

- Each comparison is presented as **Output X** and **Output Y**.
- Source labels (MKTApp / Frontier-Direct / Frontier-Expert) are recorded separately and revealed only after scoring.
- Pairings are run within a short, contiguous time window to reduce model-version drift.

### 5.2 Rubric (1–5 scale)

| Dimension | 1 | 3 | 5 |
|---|---|---|---|
| **Usefulness** | ไม่ใช้ได้ / ใช้เวลาแก้ทั้งหมด | ใช้ได้แต่ต้องแก้หลายจุด | พร้อมใช้หรือแก้น้อยมาก |
| **Factuality** | มีข้อมูลผิดชัดเจน | ถูกส่วนใหญ่ มีจุดคลุมเครือ | ถูกต้องครบถ้วนตาม source |
| **Instruction following** | ไม่ทำตามคำขอ | ทำตามบางส่วน | ทำตามทั้งหมดและชัดเจน |
| **Brand / asset fit** | ไม่เข้ากับ brand หรือ product | เข้ากันได้บางส่วน | เข้ากับ brand, product, ภาพ, tone |
| **Evidence quality** | ไม่มีหลักฐานหรืออ้างอิงปลอม | มีบางหลักฐาน แต่ไม่ครบ | มีหลักฐานชัดเจน น่าเชื่อถือ |
| **User effort** | ต้องทำใหม่เองเกือบทั้งหมด | ต้องแก้/ปรับหลายจุด | พร้อมใช้ หรือแก้ 1–2 จุด |

### 5.3 Pass / non-inferiority rule

For each scenario:

- MKTApp must not lose on **Factuality** and **Instruction following** by more than 0.5 points.
- Overall mean score across all dimensions must be within **0.5 points** of the Frontier baseline.
- MKTApp must win on **Brand / asset fit** by at least **0.5 points** in at least 50% of scenarios.

If MKTApp fails any rule, M6 is not passed and the gap must be analyzed.

### 5.4 Reviewers

Suitable evaluators:

- Product Owner or Marketing Lead with domain knowledge,
- Customer-facing sales/CS who would consume the output,
- External Thai-language content reviewer.

Reviewers must not know the source. Scoring is recorded in a simple spreadsheet: scenario, dimension, X score, Y score, reviewer, timestamp.

---

## 6. Reproducibility and Evidence

For every run (MKTApp and each baseline):

| Field | MKTApp | Frontier baselines |
|---|---|---|
| Exact payload (redacted API keys) | `*_evidence.json`, `qual_runner` invocation | prompt + attachments sent to model |
| Requested model / provider | as configured in `config/qualification.yaml` and `agents.yaml` | chosen by Product Owner at run time from OpenRouter account |
| Actual model / provider | recorded in `usage_log.csv` and evidence JSON | recorded in OpenRouter dashboard / API response |
| Tools enabled | `openrouter:web_search` / `openrouter:web_fetch` when agent uses web | same web tool set if scenario requires web evidence |
| Timestamp | ISO-8601 run timestamp | same, within same window |
| Token / cost | from usage log | from OpenRouter dashboard or response |
| Output | `*_output.txt` (user-facing) | model response saved as `*_output.txt` |
| Media artifact | `*_image_*.png` | any image generated by Frontier (if supported) |

### 6.1 Web-enabled scenarios

For S2 (competitor analysis) and S3 (campaign) where web evidence matters:

- Both MKTApp and Frontier must have the same web access condition.
- If the Frontier model / tier does **not** support web search, that baseline is declared unavailable and the scenario is excluded from that baseline or replaced by an offline-baseline prompt with the same source pack.

### 6.2 Text vs. media quality

- Text and media are scored separately.
- Image comparisons only happen when the same provider/model actually generated an image under the same scenario.
- Do not compare MKTApp image (e.g. SDXL/Flux via image endpoint) to Frontier text-only output.

---

## 7. Cost Control and Budget Options

No actual pricing is hardcoded here. All prices are taken from the Product Owner's OpenRouter account at run time.

### 7.1 Option A — Minimum credible M6 (M6.1)

- 4 scenarios × 1 Direct-User Frontier run.
- MKTApp runs: reuse only as smoke/context if needed; fresh production-equivalent runs recommended for fair pairing.
- **Stop rule:** any scenario where Frontier produces unusable output is documented as an outlier; no re-run.
- **Cap rule:** per-run cap defined by Product Owner at run time; total cap approved before starting.

### 7.2 Option B — Fuller M6 (M6.1 + M6.2)

- 4 scenarios × 2 baselines (Direct + Expert) = 8 Frontier runs.
- MKTApp paired runs for each comparison.
- Optional if M6.1 already shows non-inferiority and Product Owner wants deeper evidence.

### 7.3 Call counting method

| Side | Count method |
|---|---|
| MKTApp | number of `chat/completions`, `web_search` tool calls, and image endpoint calls in `usage_log.csv` / evidence JSON |
| Frontier | number of API calls and tool calls recorded in the script; image endpoint calls if any |

Cost estimates are produced by a real-time dry-run using `qual_runner.dry_run_cost_plan()` for MKTApp and the OpenRouter model list/pricing for the Frontier side **on the day of the run**.

### 7.4 Reuse of existing qualification artifacts

- Existing artifacts from M2–M5 may be used as **smoke checks / context** for MKTApp readiness.
- They **must not** be used as the paired parity outputs, because Frontier baselines were not run with the same inputs at the same time.
- Paired comparison outputs must be generated within the same run window.

---

## 8. Decisions Requiring Product Owner Approval Before Run

| # | Decision | Options |
|---|---|---|
| 1 | Exact Frontier model/provider | Selected from OpenRouter account at run time; not hardcoded |
| 2 | Baseline level | Direct-User only (M6.1) or Direct + Expert (M6.2) |
| 3 | Budget option | Option A or Option B |
| 4 | Per-run hard cap | e.g. max calls per scenario, max USD per scenario |
| 5 | Total UAT cap | total USD approved for M6 |
| 6 | Reviewers | names + language/marketing domain |
| 7 | Non-inferiority threshold | accept 0.5-point rule or propose alternative |
| 8 | Web tool parity | if Frontier model lacks web search, how to handle S2/S3 |

---

## 9. Output of M6 Execution

When the plan is approved and run, the M6 run folder must contain:

- `m6_inputs.json` — exact input packs per scenario,
- `m6_frontier_payloads_redacted.json` — prompts / attachments with API keys removed,
- `m6_outputs/` — user-facing outputs for MKTApp and baselines,
- `m6_scores.csv` — blind reviewer scores,
- `m6_decision.md` — pass/fail and next step (M7 release or remediation).

---

## 10. Constraints Observed in This Plan

- No OpenRouter, web search, image, video, or model calls were made to produce this document.
- No model names or prices were assumed from memory.
- No production code, config, prompt, or test was changed.
- No commit or push was made.
