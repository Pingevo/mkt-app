# M6.1 Paid Paired Evaluation — Corrected Run Report (Round 3)

## Run Identity

| Field | Value |
|-------|-------|
| Run ID | `20260904_070830` |
| Artifact location | `data/m6_frontier_uat/20260904_070830/` |
| Source execution HEAD | `2318897ae3ff0dadff47599de4b9e0dee034a7ac` |
| M6 remediation baseline | `61b6d93bb0a4389eac1bb9ff936ce6a46004ce23` |
| Approval cap (paired run) | $2.62 |

## Verdict: INCOMPLETE — Paired evidence incomplete, judge NOT run

The run stopped at Frontier S2 due to a post-call per-scenario cost breach.
All four MKTApp scenarios completed. Frontier S1 completed. Frontier S2
received a paid response that was charged $0.601450 but then discarded by
the harness's post-call cap exception. Frontier S3 and S4 were not reached.

## Corrected Cost Accounting

| Component | Amount |
|-----------|--------|
| MKTApp S1 | $0.030999 |
| MKTApp S2 | $0.183729 |
| MKTApp S3 | $0.028928 |
| MKTApp S4 | $0.040330 |
| **MKTApp total** | **$0.283986** |
| Frontier S1 | $0.219400 |
| Frontier S2 (charged, discarded — not recoverable) | $0.601450 |
| **Historical sunk cost** | **$1.104836** |
| Judge cost | $0.00 (not run) |

## Recovered Outputs (Verified at Source HEAD)

Recovery executed at source HEAD `2318897...` with all hashes verified:

| Output | Path | SHA-256 |
|--------|------|---------|
| MKTApp S1 | `outputs/mktapp/S1.txt` | `fb16b06e9c586e7147ca6cf3b1a07f667f889d7540ead70a3170746b6466f9d3` |
| MKTApp S2 | `outputs/mktapp/S2.txt` | `45cf98f9a95d1163b356ec2b65921e690af3913fb250295ce553d436b841ae30` |
| MKTApp S3 | `outputs/mktapp/S3.txt` | `64c57400e1985e0298c611d1c68b361ae97dc7cdae62e9682717d81b7b8d5e98` |
| MKTApp S4 | `outputs/mktapp/S4.txt` | `731805b2fe4805d2fb785d07952e7bc2d929553d7a02dd07b4f87ed1a1e6fb7d` |
| Frontier S1 | `outputs/frontier/S1.txt` | `05bf1a33da296439bc85dd2fab32cfc453af4d06b64dc11c16f8faed75b31af0` |
| Frontier S2 | charged $0.601450, content discarded — NOT recoverable | N/A |

**All five output hashes unchanged after manifest regeneration.**

### Regenerated Recovery Fingerprints

| Fingerprint | Value (first 16) |
|-------------|------------------|
| Production | `3abed00df90ff3f1...` |
| Scenario | `766ada8417cfadf6...` (includes system/user_prefix/full prompts) |
| Input pack | `b3977b92f385576b...` (includes image BYTES, not just paths) |

## Eight Blockers — All Resolved

### Blocker 1 — Dry-run artifacts are non-judgeable

- `_write_evidence()` now takes `execution_mode`, `valid_for_judging`, `authorized` params.
- Dry-run writes `execution_mode: "dry_run"`, `valid_for_judging: false`, `authorized: false`.
- Dry-run NEVER creates X/Y judging pairs (only paid + valid_for_judging does).
- Dry-run NEVER writes an "approved" ceiling — writes `planning_ceiling` instead.
- `resume_link.json` records `approved_cumulative_ceiling: null` for dry-run.
- Judge preflight requires `execution_mode == "paid"` and `authorized == true`.

**Verified:** Real dry-run directory has no X/Y files. Judge preflight rejects it:
```
ok=False
reason=resume_link execution_mode must be 'paid', got 'dry_run'
```

### Blocker 2 — Correct S2 $0.65 override and safety-inclusive totals

Dry-run now applies the S2 override and shows BOTH totals:

```
S2: estimate=$0.650000, payload_reserve=$0.070825, chosen=$0.670000
S3: estimate=$0.600000, payload_reserve=$0.070830, chosen=$0.620000
S4: estimate=$0.240000, payload_reserve=$0.055785, chosen=$0.260000
Estimate-only cumulative: $2.794836
Safety-inclusive cumulative: $2.854836
Safety margins total: $0.060000 (3 calls x $0.020000)
Margin under proposed $2.86: $0.005164
```

Per-call conservative reserve = `max(payload-derived, scenario estimate) + safety margin`.

### Blocker 3 — Paid continuation requires committed clean harness

- `_verify_paid_continuation_clean()` checks:
  1. Current HEAD != source execution HEAD
  2. No tracked working-tree changes (untracked data/reports allowed)
  3. Source-to-continuation diff contains only approved harness/test files
- Recovery and dry-run may run at source HEAD. Paid may not.

**Verified:** Paid resume rejected:
```
ERROR: Paid continuation cannot run from source execution HEAD (2318897a...).
Commit harness fixes to a new commit first, then run paid resume.
```

### Blocker 4 — Resume authorization is unambiguous

- Dry-run: `planning_ceiling`, `authorized=false`
- Paid run: `approved_cumulative_ceiling`, `authorized=true`
- `_validate_approval_cap()` rejects NaN, infinity, negative, zero, and caps below sunk + judge reserve
- `_validate_judge_reserve()` rejects non-finite or negative
- `_validate_reserve_overrides()` rejects unknown scenario keys and non-finite values

### Blocker 5 — Judge resume provenance is required (not optional)

- `resume_link.json` is REQUIRED (missing = fail, not skip)
- Verifies: `execution_mode == "paid"`, `authorized == true`, `valid_for_judging == true`
- Verifies: non-null `approved_cumulative_ceiling`
- Verifies: all required fields present (source_run, source_execution_head, continuation_harness_head, fingerprints, costs, judge_reserve)
- Cross-checks against `m6_evidence.json` (execution_mode, valid_for_judging, authorized)
- Cross-checks source_execution_head and production_fingerprint against recovery_manifest.json

### Blocker 6 — Judge guard never discards a paid response

`M6JudgeGuard` redesigned:
- Pre-call: conservative reserve check. If insufficient, stops BEFORE the call (no charge).
- Post-call: ALWAYS retains content, cost, model, tokens, request ID. Never raises after a paid response.
- Never converts a charged response to zero cost.
- `_update_judge_accounting()` writes `judge_accounting.json` and updates `resume_link.json` with:
  - judge incremental actual
  - cumulative program actual
  - remaining authorized amount
  - complete/incomplete status

### Blocker 7 — Input fingerprint hashes image bytes

`_input_pack_fingerprint()` now:
- Reads actual image file bytes
- Records path, size, and SHA-256 of each image
- Fails if a required image disappears or changes

**Verified:** Input pack fingerprint changed from `4ef146ab94d8be8b...` to `b3977b92f385576b...` after including image bytes. All 5 output hashes remain unchanged.

### Blocker 8 — Complete audit/evidence with reserve decisions

`_write_audit_evidence()` now persists per-call:
- payload-derived reserve
- scenario estimate
- chosen conservative reserve
- safety margin
- actual cost
- variance
- request ID
- model
- token counts
- web uses
- validity
- execution mode
- reused/new provenance

## Real Resume Dry-Run Output (with S2 $0.65 override)

```
Resume from: 20260904_070830
  Source execution HEAD: 2318897ae3ff0dadff47599de4b9e0dee034a7ac
  Continuation harness HEAD: 2318897ae3ff0dadff47599de4b9e0dee034a7ac
  Historical sunk cost: $1.104836
  Reusing MKTApp: ['S1', 'S2', 'S3', 'S4']
  Reusing Frontier: ['S1']
  Charged-invalid Frontier: ['S2']
  Missing MKTApp: []
  Missing Frontier: ['S2', 'S3', 'S4']
  Cumulative ceiling (PROPOSED (NOT AUTHORIZED)): $2.860000
  Judge reserve (protected): $0.200000
  Frontier budget (ceiling - sunk - judge): $1.555164

--- DRY-RUN PLAN (no paid calls) ---
  Execution mode: dry_run
  Valid for judging: False
  Authorized: False
  MKTApp calls: 0 (all reused)
  Frontier reused: ['S1']
  Frontier planned calls: ['S2', 'S3', 'S4']
  Judge calls: 0 (during this command)
  Historical sunk cost: $1.104836
  Planning ceiling: $2.860000
  Protected judge reserve: $0.200000
  Remaining Frontier budget: $1.555164
  S2: estimate=$0.650000, payload_reserve=$0.070825, chosen=$0.670000, max_tokens=4500, web_uses=2
  S3: estimate=$0.600000, payload_reserve=$0.070830, chosen=$0.620000, max_tokens=4500, web_uses=2
  S4: estimate=$0.240000, payload_reserve=$0.055785, chosen=$0.260000, max_tokens=3500, web_uses=0
  Estimate-only cumulative: $2.794836
  Safety-inclusive cumulative: $2.854836
  Safety margins total: $0.060000 (3 calls x $0.020000)
  Margin under proposed $2.86: $0.005164
--- END DRY-RUN PLAN ---

Execution mode: dry_run | Valid for judging: False | Authorized: False
```

## Real Dry-Run Judge Rejection

```
ok=False
reason=resume_link execution_mode must be 'paid', got 'dry_run'
```

## Exact Totals

| Total | Amount |
|-------|--------|
| Historical sunk | $1.104836 |
| Frontier estimates (S2 $0.65 + S3 $0.60 + S4 $0.24) | $1.49 |
| Judge reserve | $0.20 |
| **Estimate-only cumulative** | **$2.794836** |
| Safety margins (3 × $0.02) | $0.06 |
| **Safety-inclusive cumulative** | **$2.854836** |
| Proposed ceiling (NOT authorized) | $2.86 |
| **Margin** | **$0.005164** |

## Proposed (NOT Authorized) Seven-Call Paid Plan

| # | Call | Side | Scenario | Estimate | Conservative Reserve |
|---|------|------|----------|----------|---------------------|
| 1 | Frontier S2 | Frontier | S2 | $0.65 | $0.67 |
| 2 | Frontier S3 | Frontier | S3 | $0.60 | $0.62 |
| 3 | Frontier S4 | Frontier | S4 | $0.24 | $0.26 |
| 4 | Judge S1 | Judge | S1 | part of $0.20 | — |
| 5 | Judge S2 | Judge | S2 | part of $0.20 | — |
| 6 | Judge S3 | Judge | S3 | part of $0.20 | — |
| 7 | Judge S4 | Judge | S4 | part of $0.20 | — |

**Total planned additional paid calls: 7** (3 Frontier + 4 judge)

## Files Changed

| File | Change |
|------|--------|
| `scripts/m6_frontier_uat.py` | Dry-run non-judgeable, S2 override + safety totals, paid clean harness check, cap validation, complete fingerprints (image bytes), audit with reserve decisions |
| `scripts/m6_judge_runner.py` | Required resume provenance, guard never discards paid response, judge accounting persistence, no partial verdict |
| `tests/test_m6_frontier_uat.py` | 15 blocker regression tests + updated existing tests |
| `M6_1_RERUN_REPORT.md` | Corrected report (untracked) |

## Test Results

| Suite | Result |
|-------|--------|
| `tests/test_m6_frontier_uat.py` | **77 passed** |
| `tests/test_m6_judge_runner.py` | **21 passed** (unchanged) |
| Full offline suite | **1001 passed, 0 failed** (55.21s) |

## Compile Result

```
frontier OK
judge OK
```

## `git diff --check`

**Clean** — no whitespace errors.

## `git status --short`

```
 M M6_STATUS.md                          (NOT committed — excluded)
 M scripts/m6_frontier_uat.py            (harness corrections)
 M scripts/m6_judge_runner.py            (judge preflight + guard + accounting)
 M tests/test_m6_frontier_uat.py         (15 blocker tests + updated tests)
?? M6_1_RERUN_REPORT.md                  (this report — not staged)
?? (other untracked reports/artifacts — not staged)
```

## Confirmation

- **Zero external/model/web/media/judge calls** during offline corrections
- **No production files changed** (`src/`, `config/agents.yaml` untouched)
- **No commit made**
- **No push made**
- Recovery regenerated at source HEAD with image-byte fingerprints
- All 5 output hashes unchanged after regeneration
- Resume dry-run completed with zero HTTP calls
- Dry-run artifacts rejected by judge preflight
- Paid resume rejected (current HEAD == source HEAD)

Stopping for review. Waiting for approval before committing or executing the paid continuation.
