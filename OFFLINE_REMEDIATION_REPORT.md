# Offline Remediation Report

**Date:** 2026-09-01
**Scope:** Fixes for bugs found during Paid Smoke Qualification (A3, A4, M1)
**Constraint:** No additional paid model calls or media generation costs.

---

## Summary

Three issues from the Paid Smoke Qualification run were remediated entirely
offline, using artifact replay and regression tests. No paid API calls were
made during remediation.

| Issue | Severity | Root Cause | Fix | Verified By |
|-------|----------|------------|-----|-------------|
| P0 | High | Validator treated product's own model token (K2) as a competitor | Exclude `product_aliases` from `all_competitor_models` | A3 artifact replay + regression test |
| P1 | High | `parse_media_prompts` crashed on non-dict items in `image_prompts`/`video_prompts` | Type-check each item before `.get()` | A4 artifact replay + regression test |
| P2 | Medium | No per-run hard guard; `budget_guard()` only wrapped `run_case()` start; `max_calls` was a lower bound | `PaidCallGuard` wraps every `httpx.Client.post`/`stream`; `budget_guard()` reserves a conservative per-call USD ceiling before each paid call; `dry_run_cost_plan()` uses the hard `configured_max_calls` cap | Offline unit tests |

---

## P0 — Campaign Validator False Positive

### Symptom (A3)
`competitor_mentions_grounded` rule failed on the header line:
```
# Executive Brief: กลยุทธ์แคมเปญและการวางตำแหน่งราคา Lagenio K2
```
The line contains "K2" — the product's own model token — but the validator
treated it as a competitor mention requiring evidence.

### Root Cause
`audit_campaign_output()` in `src/campaign_validator.py` computed:
```python
all_competitor_models = competitor_models | requested_competitor_models
```
When the user's quick_brief contained "K2", it entered
`requested_competitor_models`. The validator then flagged any line containing
"K2" as a competitor mention — including the product's own name.

### Fix
`src/campaign_validator.py` line 715:
```python
all_competitor_models = (competitor_models | requested_competitor_models) - product_aliases
```
`product_aliases` is already extracted by `_extract_canonical_identities()` and
includes all tokens of the canonical product ID (e.g. `lagenio k2`, `lagenio`,
`k2`).

### Verification
1. **Regression test** `test_product_name_not_treated_as_competitor_for_grounding`
   in `tests/test_campaign_validator.py` — passes.
2. **A3 artifact replay** — loaded the smoke run's `campaign_strategy_output`
   through the current validator with the same context flags. All 19 rules
   pass (0 failures). Before the fix, `competitor_mentions_grounded` failed.

---

## P1 — Media Runner AttributeError

### Symptom (A4)
Auto-image pipeline crashed with:
```
AttributeError: 'str' object has no attribute 'get'
```

### Root Cause
`parse_media_prompts()` in `src/media_gen.py` iterated over
`post.get("image_prompts", [])` and called `ip.get("prompt", "")` on each
item. If the LLM returned a string instead of a dict (e.g.
`["a product photo"]` instead of `[{"prompt": "a product photo"}]`), the
`.get()` call crashed.

### Fix
`src/media_gen.py` — added `isinstance(ip, dict)` / `isinstance(vp, dict)`
guards before accessing each prompt item. Non-dict items are skipped and
recorded in a `warnings` list returned alongside `images`/`videos`.

Also hardened `scripts/qual_runner.py` content_creator path: if
`json.loads(result)` returns a non-dict or fails, a clear `ValueError` is
raised instead of silently swallowing the error.

### Verification
1. **Regression test**
   `test_parse_media_prompts_regression_a4_no_crash_on_string_prompts`
   in `tests/test_phase3_asset_integration.py` — passes.
2. **A4 artifact replay** — loaded the smoke run's
   `A4_fb_tiktok_image_output.txt` through the fixed `parse_media_prompts()`.
   Returns 2 images, 0 videos, 0 warnings (the artifact's prompts were
   well-formed dicts). A synthetic malformed input with string prompts
   returns 0 images + 1 warning, no crash.

---

## P2 — Cost Controls

### Problems
1. Cost ceilings were hardcoded constants in `qual_runner.py`.
2. No per-run call budget — a runaway agent could make unlimited calls
   until the USD ceiling was hit.
3. No dry-run cost plan — operator had no pre-flight estimate before
   committing paid calls.
4. Web-search calls were not separately accounted for.

### Fix
**New config file** `config/qualification.yaml`:
```yaml
qualification:
  ceilings:
    stage_a: 0.80
    stage_b: 0.95
    total: 1.35
  cost_per_call_ceiling:
    text: 0.05
    web_search: 0.15
    image: 0.08
    video: 0.60
  default_max_calls:
    product_spec: 2
    competitor_analysis: 3
    campaign_strategy: 4
    content_creator: 6
```

**`scripts/qual_runner.py` changes:**
- `_load_qualification_config()` reads ceilings, `cost_per_call_ceiling`,
  and hard `default_max_calls` caps from config (no hardcoded prices).
- `PaidCallGuard` is a context manager that patches
  `httpx.Client.post`/`stream` and calls `budget_guard()` before every
  paid outbound request.  It resolves relative paths (`/chat/completions`)
  against `client.base_url` so it also catches text calls made by
  `LLMClient`.  This hard-caps generation, review, repair, web-search and
  media calls with a single guard.  For `chat/completions`
  it also inspects the JSON payload for `tools` of type
  `openrouter:web_search` or `openrouter:web_fetch` (the exact shape
  `BaseAgent._build_web_search_tools()` sends) and reserves the
  `web_search` ceiling instead of the cheaper `text` ceiling.
- `budget_guard()` reserves a conservative per-call USD ceiling (from
  `cost_per_call_ceiling`) before the request is actually sent, and
  increments the call counter only on ALLOW.
- `dry_run_cost_plan()` returns a pre-flight plan listing every expected
  call with a **conservative estimate** and the hard `configured_max_calls`
  cap.  The plan is printed before each run and stored in `evidence`.
- Evidence now includes `dry_run_plan` and `actual_call_classifications`
  so the operator can compare planned vs actual calls.
- `_read_total_cost()` and `_read_entries_since()` guard against
  non-dict JSON lines in the usage log.

### Verification
7 offline unit tests in `tests/test_qual_runner_offline.py`:
- `test_dry_run_cost_plan_content_creator` — verifies media call counting
  and conservative cost estimate.
- `test_dry_run_cost_plan_competitor_analysis_counts_web_search` — verifies
  web-search call separation and that the configured `max_calls` is a hard cap.
- `test_paid_call_guard_blocks_after_max_calls` — proves the next paid call
  is blocked once the hard call count is reached (full URL).
- `test_paid_call_guard_blocks_by_usd_ceiling` — proves a paid call is
  blocked before the conservative per-call reserve would exceed the USD ceiling.
- `test_paid_call_guard_blocks_relative_chat_completions` — proves relative
  `post("/chat/completions", base_url="https://openrouter.ai/api/v1/")`
  is counted and capped.
- `test_paid_call_guard_blocks_relative_stream` — proves relative streaming
  `stream("POST", "/chat/completions")` is also guarded.
- `test_paid_call_guard_uses_web_search_reserve_for_tool_calls` — proves
  relative `post` and `stream` of `/chat/completions` carrying the real
  `openrouter:web_search`/`openrouter:web_fetch` tools reserve the higher
  `web_search` ceiling and the `actual_call_classifications` log records
  `kind: web_search`.

---

## Full Test Suite

```
791 passed, 43 warnings in 35.05s
```

All existing tests continue to pass alongside the 7 new regression tests.

---

## Files Changed

| File | Change |
|------|--------|
| `src/campaign_validator.py` | P0: exclude `product_aliases` from competitor grounding |
| `src/media_gen.py` | P1: type-guard prompt items + return `warnings` |
| `scripts/qual_runner.py` | P1: raise on non-dict content output; P2v2: `PaidCallGuard` hard-wraps every paid httpx call, conservative per-call USD reserve, hard `max_calls`, dry-run plan + evidence |
| `config/qualification.yaml` | P2: new config for ceilings, cost-per-call, max-calls |
| `tests/test_campaign_validator.py` | P0 regression test |
| `tests/test_phase3_asset_integration.py` | P1 regression test |
| `tests/test_qual_runner_offline.py` | P2 dry-run plan tests |
| `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` | Doc alignment (one agent per flow) |

---

## Next Steps

1. **Re-run A3 standalone** with the fixed validator to confirm the
   campaign_strategy agent now passes qualification within budget.
2. **Re-run A4 standalone** with the fixed media parser to confirm
   auto-image generation completes without crash.
3. **M2–M5** standalone qualification of Agents 1–4 via UI-equivalent flow.
4. **M6** lightweight Frontier comparison.
5. **M7** Full Beta release.
