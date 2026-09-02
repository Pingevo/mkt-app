# Paid Smoke Qualification Report

- **Run:** 20260901_080019
- **Product fixture:** Lagenio K2 (ready, has 1 extracted product image)
- **Hard cap:** $0.80 USD
- **Actual cumulative spend:** $0.722649 USD
- **Total paid requests:** 20
- **Qualification artifacts:** `data/all_agents_beta_qualification/smoke_20260901_080019`
- **Rules observed:** single run per case, no retries, no code edits, no commits

## 1. Pre-run call / cost estimate

| case | agent | estimated cost | actual cost | variance | notes |
|------|-------|----------------|-------------|----------|-------|
| A1 | product_spec | $0.03 | $0.022075 | -$0.008 | 2 calls (gen + review) |
| A2 | competitor_analysis | $0.05 | $0.238686 | +$0.189 | web search expanded scope to multiple competitors |
| A3 | campaign_strategy | $0.05 | $0.059511 | +$0.010 | 4 calls, validator rejected after 3 repair rounds |
| A4 | content_creator | $0.12 | $0.076037 | -$0.044 | 4 calls; content produced, image pipeline did not run |
| M1 | all 4 agents | $0.25 | $0.326337* | +$0.076 | product_spec + competitor + campaign; content blocked by budget |
| **Total** | — | **~$0.50** | **$0.722649** | **+$0.223** | Overrun mainly from web-search competitor analysis |

*M1 total includes M1_product_spec ($0.023728), M1_competitor ($0.226968), M1_campaign ($0.075644).

## 2. Qualification matrix

| case | scenario | agent(s) | status | cost | calls | evidence / output | reason |
|------|----------|----------|--------|------|-------|-------------------|--------|
| **A1** | Product spec with image + deliverable format | product_spec | **PASS** | $0.022075 | 2 | `A1_product_spec_image_output.txt` | One-page Thai product spec generated, image used, no fixed-template fallback |
| **A2** | Competitor / current data with web evidence | competitor_analysis | **PASS** | $0.238686 | 2 | `A2_competitor_evidence_output.txt` (5 cited URLs) | Live competitor comparison with cited evidence (imoo, myFirst, Xiaomi) |
| **A3** | Campaign quick brief changing scope/format + research | campaign_strategy | **QUALITY GAP** | $0.059511 | 4 | `A3_campaign_research_brief_evidence.json` | Web evidence collected (5 URLs selected) but output failed validator after 3 repair rounds |
| **A4** | Facebook + TikTok, text + image, media pipeline | content_creator | **QUALITY GAP** | $0.076037 | 4 | `A4_fb_tiktok_image_output.txt` (2 posts: Facebook + TikTok) | 2 posts generated and validated, but auto-image generation did not produce any PNG |
| **M1** | End-to-end multi-agent flow | all 4 | **FAIL / BLOCKED** | $0.326337 | 14 | `M1_*_evidence.json`, `M1_*_output.txt` | Campaign step failed validation; final content_creator step blocked by hard cap |

## 3. Per-case findings

### A1 — Product spec with image (PASS)
- Generated a structured Thai product-spec deliverable from the product image + xlsx data.
- No fixed-template reversion; deliverable adapted to the requested one-page spec format.
- Cost well under estimate.

### A2 — Competitor / current data (PASS)
- Produced a comparison table with 5 cited URLs from the current web search run.
- Scope included imoo Z1 plus other relevant competitors; cost was high because the search agent expanded to multiple models.

### A3 — Campaign with quick brief (QUALITY GAP)
- Quick brief requested an executive-brief format and current competitor pricing.
- The agent performed web search and selected 5 evidence URLs.
- The campaign output repeatedly failed validation (`competitor_mentions_grounded`, `indicative_retail_promo_labelled`, `benchmarks_cited_or_removed`) and exhausted 3 repair iterations.
- No final deliverable was produced beyond the validator log.

### A4 — Facebook + TikTok text + image (QUALITY GAP)
- Content Creator produced a valid 2-post JSON output with separate `Facebook` and `TikTok` posts.
- The content schema and multi-post pipeline work.
- Auto-image generation did not run; the error recorded was `'str' object has no attribute 'get'`, originating from the content-creator / media-gen runner before any image file was written.

### M1 — End-to-end multi-agent flow (FAIL / BLOCKED)
- Steps completed: `product_spec` (2 calls, OK), `competitor_analysis` (2 calls, OK), `campaign_strategy` (4 calls, **validator error**).
- Final `content_creator` step was **blocked by the hard cap** at $0.722649 + $0.10 estimated = $0.822649 > $0.80.
- The flow therefore did not produce final content and did not fully exercise context handoff.

## 4. Targeted fixes

| # | failure | targeted fix |
|---|---------|--------------|
| 1 | A3 / M1 campaign output repeatedly fails `competitor_mentions_grounded` and `indicative_retail_promo_labelled` validators. | Investigate the `campaign_strategy` output validator / repair loop. The current strict grounding + label checks prevent the executive-brief format from passing even when the model cites sources and uses estimate/disclaimer language. Consider reducing `max_review_iterations` or tuning the validator to allow indicative pricing and benchmark claims when they are properly labelled. |
| 2 | A4 media pipeline did not generate images; error `'str' object has no attribute 'get'`. | The `qual_runner` / `media_gen` `parse_media_prompts` path (or the runner's content post-processor) assumes `json.loads` always returns a dict. When the model returns a string it handles as a raw string, `parsed.get()` throws. Add `AttributeError` handling or normalise the input before calling `.get()`. |
| 3 | M1 end-to-end flow did not finish because of hard cap. | For a full end-to-end run, either raise the smoke hard cap (e.g. $1.20) or lower per-case estimates / disable auto web-search for one agent to keep the 4-agent flow under the cap. |
| 4 | A2 competitor call cost 4.8x estimate. | Add a tighter scope guard (e.g. force competitor name list from quick_brief) so the agent does not expand the web search to a large set of competitors. |

## 5. Recommended fix priority

1. **P0 — Campaign validator / repair loop** (blocks A3 and M1, core deliverable).
2. **P1 — Media pipeline runner error** (blocks A4 image generation, visible pipeline failure).
3. **P2 — Budget / scope control for competitor web search** (prevents cost overruns in A2 / M1).
4. **P3 — Hard-cap sizing for multi-agent e2e** (re-run M1 once P0-P1 are fixed).

## 6. Raw artifact paths

All artifacts are under:

```
data/all_agents_beta_qualification/smoke_20260901_080019/
```

Evidence JSON:
- `A1_product_spec_image_evidence.json`
- `A2_competitor_evidence_evidence.json`
- `A3_campaign_research_brief_evidence.json`
- `A4_fb_tiktok_image_evidence.json`
- `M1_product_spec_evidence.json`
- `M1_competitor_evidence.json`
- `M1_campaign_evidence.json`
- `M1_content_evidence.json` (blocked)

Generated outputs:
- `run_outputs/A1_product_spec_image_output.txt`
- `run_outputs/A2_competitor_evidence_output.txt`
- `run_outputs/A3_campaign_research_brief_output.txt`
- `run_outputs/A4_fb_tiktok_image_output.txt`
- `run_outputs/M1_product_spec_output.txt`
- `run_outputs/M1_competitor_output.txt`
- `run_outputs/M1_campaign_output.txt`

Ledger:
- `summary.json`
- `ledger.json`
