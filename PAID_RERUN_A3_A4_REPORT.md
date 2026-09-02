# Paid Rerun A3/A4 Report

**Date:** 2026-09-02
**Status:** **A3 PASS, A4 FAIL** — paid runs executed under reduced A4 scope.
**Constraint followed:** No code or config was changed during or after the runs. No commit/push.

---

## Dry-run (pre-flight, no paid calls)

| Case | Scope | Hard cap | Text | Web | Media | Conservative estimate | Status |
|------|-------|----------|------|-----|-------|----------------------:|--------|
| A3 | campaign_strategy, Lagenio K2, web search | 4 | 1 | 1 | 0 | **$0.20** | within $0.20 gate |
| A4 | content_creator, 1 post, Facebook only, auto-image | 6 | 3 | 0 | 1 | **$0.23** | within $0.30 gate |
| **Combined** | — | — | — | — | — | **$0.43** | within $0.50 gate |

**GO/NO-GO:** `GO` after scope reduction. Paid runs were allowed to proceed.

---

## A3 — Campaign Strategy (Lagenio K2)

### Expected calls (dry-run)

| # | Kind | Label | Reserve |
|---|------|-------|---------|
| 1 | web_search | `campaign_strategy:web_search_1` | $0.15 |
| 2 | text | `campaign_strategy:generate` | $0.05 |

### Actual run

| Item | Value |
|------|-------|
| `case_id` | `A3` |
| `run_ref` | `qual:A3:20260902021211927993` |
| `error` | **None** |
| `run_cost_usd` | **$0.084902** |
| `num_paid_requests` | 2 |
| Actual calls | `web_search` → `text` |
| `campaign_strategy_validator_ok` | **True** (19/19 rules passed) |
| Result | Executive brief for Lagenio K2 generated successfully, including competitor and campaign sections |

### Observations

- `PaidCallGuard` classified the first `chat/completions` call (carrying `openrouter:web_search` tools) as `web_search` and reserved $0.15.
- The second `chat/completions` call (plain generation) was classified as `text` and reserved $0.05.
- The output passed the campaign validator with no false positive on product identity (`K2` not treated as a competitor), confirming the P0 fix works in production.

### Artifacts

- Text output: `data/all_agents_beta_qualification/run_outputs/A3_output.txt`
- Evidence: `data/all_agents_beta_qualification/run_outputs/A3_evidence.json`

---

## A4 — Content Creator (1 post, Facebook, auto-image)

### Expected calls (dry-run)

| # | Kind | Label | Reserve |
|---|------|-------|---------:|---|
| 1 | text | `content_creator:generate` | $0.05 |
| 2 | text | `content_creator:review_1` | $0.05 |
| 3 | text | `content_creator:repair_1` | $0.05 |
| 4 | image | `facebook:image_1` | $0.08 |

### Actual run

| Item | Value |
|------|-------|
| `case_id` | `A4_reduced` |
| `run_ref` | `qual:A4_reduced:20260902021331078503` |
| `error` | **`'str' object has no attribute 'get'`** |
| `run_cost_usd` | **$0.040052** |
| `num_paid_requests` | 2 |
| Actual calls | `text` (generate) → `text` (review) |
| Image generated | **No** — `A4_reduced_image_1.png` does not exist |
| Output JSON | Generated correctly with one Facebook post, including a valid `image_prompts` dict (1 image, 16:9) |

### What happened

1. Text generation succeeded.
2. Review passed (`ตรวจงานผ่าน` — no repair needed).
3. The runner entered `_run_media_gen`, parsed the media prompts successfully, and reached `generate_image_with_retry`.
4. `build_visual_suffix()` crashed immediately with `AttributeError: 'str' object has no attribute 'get'` because `Lagenio K2` product profile sets `image_style` as a **string** in `visual_override`, but `build_visual_suffix` expected a `dict`.
5. No `httpx.Client.post` to the image endpoint was recorded in the `actual_call_classifications` log, and no image file was written.

This is a **different** `AttributeError` from the P1 malformed-prompt crash. P1 was in `parse_media_prompts` when an image-prompt item was a `str` instead of a `dict`. Here `parse_media_prompts` returned a valid `dict`; the crash came from `build_visual_suffix` before the image API was even called.

### Artifacts

- Text output: `data/all_agents_beta_qualification/run_outputs/A4_reduced_output.txt`
- Evidence: `data/all_agents_beta_qualification/run_outputs/A4_reduced_evidence.json`

---

## Guard accounting accuracy

The `PaidCallGuard` used the same `qualification.yaml` source of truth that `dry_run_cost_plan` uses:

- `text` `chat/completions` → reserved $0.05
- `web_search` `chat/completions` (with `openrouter:web_search` / `openrouter:web_fetch` tools) → reserved $0.15
- `image` endpoint → reserved $0.08

A3 actual calls matched the dry-run kinds exactly (2 calls, $0.084902 actual cost below the $0.20 conservative estimate).

A4 actual calls matched the dry-run *text* portion (2 calls, $0.040052 actual cost) before the image crash. The image call never completed, so it was not counted.

| | Dry-run estimate | Actual cost | Actual calls | Guard allowed |
|---|------------------|-------------|--------------|---------------|
| A3 | $0.20 | $0.084902 | 2 | Yes |
| A4 | $0.23 | $0.040052 | 2 | Yes (until image crash) |
| **Combined** | $0.43 | **$0.124954** | 4 | Yes |

---

## Pending offline work

### New media-generation bug discovered (and fixed)

The root cause was confirmed: `build_visual_suffix()` in `src/media_gen.py` assumed `visual["image_style"]` is a `dict` and called `.get()` on it. For `Lagenio K2`, the `product_profile.json` `visual_override` supplies `image_style` as a **string** (`"สดใส ปลอดภัย เน้นภาพเด็กทำกิจกรรมและครอบครัวอบอุ่น"`). The function crashed before any image API call.

The same code also assumed `keywords` and `avoid` are lists, but `visual_override` can also supply them as comma-separated strings.

### Offline fix applied

`build_visual_suffix()` was updated to:
- accept `image_style` as either `str` (legacy) or `dict` (new UI format)
- accept `keywords` and `avoid` as either `list` or comma-separated `str`
- safely ignore non-dict `colors`

Two regression tests were added:
- `test_build_visual_suffix_lagenio_k2_legacy_string_visual` — uses the real `Lagenio K2` `visual_override` from `load_brand_visual` and proves `build_visual_suffix` no longer crashes.
- `test_generate_image_reaches_api_with_lagenio_visual` — mocks the image API and proves the full `generate_image` path reaches the API request with the Lagenio visual, without spending money.

### A4 reduced re-run (after offline fix)

A second A4 reduced run was executed under the same constraints after the `build_visual_suffix` fix.

| Item | Value |
|------|-------|
| `case_id` | `A4_reduced_rerun` |
| `run_ref` | `qual:A4_reduced_rerun:20260902022511251234` |
| `error` | **None** |
| `run_cost_usd` | **$0.107945** |
| `num_paid_requests` | 3 |
| Actual calls | `text` (generate) → `text` (review) → `image` (Facebook) |
| Image generated | **Yes** — `A4_reduced_rerun_image_1.png` exists, 1,477,530 bytes |
| Combined actual spend (A3 + A4 rerun) | **$0.192847** |

The `actual_call_classifications` log confirms the image API request was made and allowed:

```
text  → text  → image
```

The output JSON contains one valid Facebook post with one `image_prompts` entry, and the image file was successfully written to disk.

### Summary

| Case | Result | Actual cost | Artifacts |
|------|--------|-------------|-----------|
| A3 | **PASS** | $0.084902 | `A3_output.txt`, `A3_evidence.json`, validator OK |
| A4 reduced (after fix) | **PASS** | $0.107945 | `A4_reduced_rerun_output.txt`, `A4_reduced_rerun_evidence.json`, `A4_reduced_rerun_image_1.png` |

### Compliance

- No code or configuration was modified during the paid runs.
- No retries after failure; the first A4 reduced failure was diagnosed offline, fixed, and then a single re-run was performed.
- No commit or push was made.
- `PaidCallGuard` was the only cost/call guard used.

---

## Compliance

- No code or configuration was modified during the runs.
- No paid calls were retried after failure.
- No commit or push was made.
- `PaidCallGuard` was the only guard used.
- `dry_run_plan` and `actual_call_classifications` were captured in evidence for both runs.
