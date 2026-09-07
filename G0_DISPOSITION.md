# G0 Disposition: Isolate M6 overfit from working tree

**Scope**: classify every modified/untracked file in the working tree. No model/tool/image/video calls, no commit/push. Targeted reverts only after this table is clear.

## Modified files (tracked)

| File | Disposition | Reason | Exact hunks to revert | Notes |
|------|-------------|--------|------------------------|-------|
| `config/agents.yaml` | **revert because M6 product remediation overfit** | Adds `output_quality.source_grounded` with smartwatch-specific `technical_attributes` (`CPU`/`OS`), `factual_claim_patterns` (`real_time`, `ai`, `navigation`, `voice`, `accuracy`), and static Thai `puffery`/`soft_wording` lists under `product_spec`, `competitor_analysis`, and `content_creator`. Also adds `output_quality.one_page` and `strict_output_sections: false`. | Hunk 1: `@@ -132,6 +132,72` inserts `output_quality` + `strict_output_sections` under `product_spec`. Hunk 2: `@@ -257,6 +323,60` inserts `output_quality.source_grounded` under `competitor_analysis`. Hunk 3: `@@ -710,3 +833,56` inserts `output_quality.source_grounded` under `content_creator`. | All these blocks are M6 uncommitted additions. Reverting restores HEAD config. `strict_output_sections: false` is not overfit but is part of the same M6 hunk and its absence defaults to the same behavior. |
| `src/output_validators.py` | **revert because M6 product remediation overfit** | Adds `_validate_technical_attributes`, `_validate_factual_claim_patterns`, `_validate_comparative`, `_source_grounded_validator`, `_one_page_limit`, plus `_extract_source_values`/`_build_source_ngrams` helpers. Changes `validate_output` signature to inject `quick_brief`, `source_text`, `brand_rules` and calls the new validators. Also deletes several docstrings/comments as cleanup. | Hunk `@@ -16,30 +16,14` removes comments. Hunks `@@ -60,7 +44,6`, `@@ -68,11 +51,6`, `@@ -150,7 +130,0`, `@@ -161,31 +140,0`, `@@ -171,7 +146,0`, `@@ -177,7 +151,0`, `@@ -182,7 +155,0`, `@@ -188,7 +160,0`, `@@ -200,7 +171,0` remove docstrings/comments. Hunk `@@ -221,18 +192,288` adds M6 validator block. Hunk `@@ -229,0 +467,3` adds `quick_brief`/`source_text`/`brand_rules` to `validate_output` signature. Hunk `@@ -233,3 +473,4` rewrites `validate_output` docstring. Hunk `@@ -253,0 +495,15` inserts validator calls. Hunk `@@ -258,5 +513,0` removes `validate_content_output` docstring. | The whole file M6 diff is overfit remediation. Reverting to HEAD restores the M1–M5 generic validator framework. The `_brand_hard_validator` function is generic but depends on the same M6 signature change, so it is removed with the patch and can be reintroduced cleanly in G1 if needed. |
| `src/agents/base_agent.py` | **revert because M6 product remediation overfit** | Adds `_normalize_quick_brief`, `_get_source_text`, `_last_source_text`, `_last_quick_brief`, `_is_non_repairable_error`, and injects `quick_brief`/`source_text`/`brand_rules` into `_validate_output`. | Hunk `@@ -59,6 +59,23` adds `_normalize_quick_brief` and `_get_source_text`. Hunk `@@ -321,8 +338,17` stores `_last_source_text` and `_last_quick_brief`. Hunk `@@ -461,6 +487,11` and `@@ -475,6 +506,11` add `_is_non_repairable_error` raises in repair loop. Hunk `@@ -788,6 +824,9` updates `validate_output` docstring. Hunk `@@ -795,7 +834,29` changes `validate_output` call to pass M6 kwargs and adds `_is_non_repairable_error`. | All these changes are plumbing for the M6 source-grounded/one-page validators. Reverting restores the generic M1–M5 repair loop. |
| `src/agents/product_spec.py` | **revert because M6 product remediation overfit** | Adds `import re`, `_ONE_PAGE_RE`, `_normalize_quick_brief` one-page conversion, and `_trusted_source_text`. | Hunk `@@ -4,0 +5,1` adds `import re`. Hunk `@@ -14,0 +16,13` adds `_ONE_PAGE_RE` and `_normalize_quick_brief`. Hunk `@@ -15,0 +30,1` adds `_trusted_source_text = raw_data`. | The one-page normalization and source-text assignment are M6 product-specific remediation. |
| `src/agents/competitor_analysis.py` | **revert because M6 product remediation overfit** | Adds `_trusted_source_text = product_spec or ""`. | Hunk `@@ -41,0 +42,3` adds `_trusted_source_text` assignment. | Source-text isolation added for M6 source-grounded validator. |
| `src/agents/content_creator.py` | **revert because M6 product remediation overfit** | Adds `_trusted_source_text = product_spec`. | Hunk `@@ -21,0 +22,3` adds `_trusted_source_text` assignment. | Source-text isolation added for M6 source-grounded validator. |
| `tests/test_competitor_analysis.py` | **revert because M6 product remediation overfit** | Rewrites `test_run_triggers_repair_when_output_quality_fails` to expect `source-grounded` ValueError and adds `test_real_time_unsupported_rejected_no_repair`. | Hunk `@@ -191,8 +191,8` through `@@ -212,10 +212,38` rewrites existing test; hunk continues adding `test_real_time_unsupported_rejected_no_repair`. | These tests assert the smartwatch-specific `real-time` source-grounded behavior. Revert restores the original M1–M5 repair test. |
| `M6_STATUS.md` | **preserve as evidence/report** | Status update of M6.1 rerun/judge/failure-attribution. | None | Not production code; keep as audit trail. |

## Untracked files / directories

| Path | Disposition | Reason |
|------|-------------|--------|
| `BRAND_GENERALIZATION_AUDIT.md` | **keep for G1** | Audit deliverable that drives the generalization work. |
| `GENERALIZATION_REMEDIATION_PLAN.md` | **keep for G1** | Approved plan for G0–G5. |
| `G0_DISPOSITION.md` | **keep for G0/G1** | This disposition table. |
| `BETA_QUALIFICATION_EXECUTIVE_EVIDENCE.html` | **preserve as evidence/report** | M6 Beta qualification artifact. |
| `M6_1_JUDGE_REPORT.md` | **preserve as evidence/report** | M6.1 judge report. |
| `M6_1_RERUN_REPORT.md` | **preserve as evidence/report** | M6.1 rerun report. |
| `M6_FINAL_FAILURE_ATTRIBUTION_REPORT.md` | **preserve as evidence/report** | M6 failure attribution. |
| `M6_FRONTIER_PARITY_UAT_PLAN.md` | **preserve as evidence/report** | M6 UAT plan. |
| `M6_REMEDIATION_DESIGN.md` | **preserve as evidence/report** | M6 remediation design. |
| `cache/m6_frontier_uat/` | **preserve as evidence/report** | M6 run cache. |
| `data/m6_frontier_uat/` | **preserve as evidence/report** | M6 run data. |
| `tests/test_m6_remediation.py` | **preserve as evaluation harness; needs G2 migration** | M6 regression test harness. It is overfit to `Lagenio K2`/smartwatch, but it is test evidence, not production code. It will not be collected by the targeted offline test run in G0; it must be rebased onto `config/categories/smartwatch.yaml` in G2. |

## What will NOT be reverted

- Committed M1–M5 work remains untouched. The files above are the only tracked files with uncommitted M6 remediation changes.
- `competitor_evidence.py` `FIELD_CATALOG`, `agents.yaml` `relevance_policy`, `competitor_analysis.py`/`campaign_strategy.py` `_derive_target_category`, and other committed smartwatch/electronics debt are **not** reverted in G0 because they are in the committed baseline. They are scheduled for generalization in G1–G2 of `GENERALIZATION_REMEDIATION_PLAN.md`.
- M6 reports, run artifacts, and the two generalization plan/audit files are preserved.

## Revert method

Use targeted `git checkout -- <file>` (or `git restore --source=HEAD --worktree -- <file>`) per file for the 7 production/test files listed as "revert". This restores each file to its HEAD (M1–M5) state and is not a broad `git reset --hard` or `git checkout .

## G0 execution summary

Targeted reverts completed for the 7 production/test files listed above.

```
$ git checkout -- config/agents.yaml src/output_validators.py src/agents/base_agent.py src/agents/product_spec.py src/agents/competitor_analysis.py src/agents/content_creator.py tests/test_competitor_analysis.py
```

Verification:
- `git diff --check` → clean
- `git diff --stat` → only `M6_STATUS.md` remains modified (report, not production code)
- Targeted offline tests:
  - `python3 -m pytest tests/test_output_validators.py tests/test_base_agent_validation.py tests/test_product_spec_quick_brief.py tests/test_competitor_analysis.py -q` → 91 passed
  - `python3 -m pytest tests/test_campaign_strategy.py tests/test_campaign_validator.py -q` → 100 passed
  - `python3 -m pytest tests/test_orchestrator_campaign_integration.py tests/test_run_context.py -q` → 21 passed

Working tree now contains only the preserved M6 reports/artifacts and the two generalization documents plus this disposition file. No G1 implementation has started and no commit was made.
