# M6 Uplift Qualification Fixtures — Provenance

## Version: v1 (frozen 2026-09-05)

## Purpose

Fixed candidate-independent upstream context for Agent-level isolation
in the same-model uplift qualification.

Both Candidate A (direct baseline) and Candidate B (MKTApp) receive
identical semantic fixture content from these files.

## Source

These fixtures were frozen from accepted historical MKTApp production
outputs preserved in `cache/Lagenio K2/`. They are NOT independently
researched neutral truth — they originated from MKTApp Agent runs.

Their purpose is to provide a realistic, representative, static upstream
context so that S3 (campaign_strategy) and S4 (content_creator) can be
evaluated as individual Agents rather than as a full pipeline.

## Files

| File | Source | Frozen from |
|------|--------|-------------|
| `S3_competitor_context.md` | `cache/Lagenio K2/competitor_analysis.md` | 2026-09-05 |
| `S4_competitor_context.md` | `cache/Lagenio K2/competitor_analysis.md` | 2026-09-05 |
| `S4_campaign_context.md` | `cache/Lagenio K2/campaign_strategy.md` | 2026-09-05 |

## Immutability

Once frozen for qualification version v1, normal MKTApp runs must NOT
overwrite these files. They are versioned via the source fixture hash.

Modifying these files changes the source fixture hash, which changes
the benchmark. Do not modify after paid qualification begins.
