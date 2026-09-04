# Generalization Remediation Plan

> **Revised direction (Product Owner decision):**
> MKTApp is a **source-driven generic marketing engine**.
> `category` is **optional metadata** for search/grouping/observability only.
> It does NOT control prompt, schema, validator, field requirements, agent
> selection, or output format. The engine works at full capability with no
> category at all. Multi-product across categories works from the source of
> every product — no fallback, no warning, no first-product selection.
> Category packs (`config/categories/*.yaml`) are a **future optional
> extension** for when a real use case needs them, not the core architecture.

## 1. Scope & Principles

- **Mode**: offline only. No model/web/image/video calls, no commit/push without approval.
- **Source of truth**: product data, brand data, assets, user request, Quick Brief,
  and Agent Settings.
- **Design principle**: the engine reads the product source pack and brand policy
  at runtime and produces marketing content. No hardcoded product domain in code.
- **No rewrite**: the pipeline, validators, agents, UI, orchestration stay.
  Only the domain-specific assumptions are generalized to be source-driven.
- **No category-driven architecture**: `category` is metadata. It does not pick
  a config file, does not choose a schema, does not select agents, and does not
  change prompts. Category packs are a future optional extension, not a core axis.

---

## 2. TL;DR

| Question | Answer |
|----------|--------|
| What can stay from M1–M5? | UI/orchestration, `StepRunContext`, Quick Brief/Settings, cost guard, media pipeline, generic validators, product DB, brand loader, file loader, evidence pipeline. |
| What was done in G1? | `brand_dir` is a runtime parameter flowing through every production route. `category` is reduced to optional metadata. No category guessing, no consensus, no fallback. Multi-product cross-category works from source. |
| What is deferred to G2? | Removing smartwatch-specific `FIELD_CATALOG`/schema, keyword-based `_derive_target_category`, `relevance_policy` keywords, and spec regexes — all replaced with source-driven generic logic, not category packs. |
| What is the G2 design axis? | Source-driven, not category-driven. The engine reads what the product source and user request actually contain, and adapts. No `config/categories/{category}.yaml` loader. |
| What about category packs? | Future optional extension only, when a real use case needs per-category tuning. Not required for the engine to work. |

---

## 3. G1 — Completed (uncommitted, awaiting approval)

### 3.1 What was preserved

- Runtime `brand_dir` flows through `api_run_agent`, `api_run_agents`,
  `api_run_flows`, `api_run_auto`, `Orchestrator`, `StepRunContext`, and agent.
- Fresh orchestrator per request — no state/brand mixing across parallel runs.
- Product/resource/assets/settings context propagation in every route.
- `api_run_agents` now builds `StepRunContext` per agent run (separate + combined).

### 3.2 What was removed

- `_GENERIC_CATEGORY` constant — no more "generic" as a behavior mode.
- `_resolve_category_from_refs` — no first-product category selection.
- `resolve_combined_category` — no category consensus, no mismatch warnings.
- Category guessing in `Orchestrator._make_agent` — no `product_db` metadata
  lookup to set `agent.category`.
- `category` as a control value in `build_step_run_context` — removed entirely.
- `StepRunContext.category` — removed (YAGNI: no production consumer).
- `StepRunContext.product_categories` — removed (YAGNI: no production consumer).
- `BaseAgent.category` — removed (YAGNI: no production consumer).
- `_product_category()` and `_collect_categories()` helpers — removed.

### 3.3 What category is now

Category is **not in the runtime contract at all**. It exists only as:
- An optional explicit field in `product_profile.json`.
- A metadata field in `product_db` records (for future search/grouping/reporting).
- Synced from `product_profile.json` to `product_db` on profile save.

No infer/guess, no sending category to agent or `StepRunContext`.

### 3.4 Runtime flow after G1

```
web_viewer endpoint
  → body.get("brand_dir") or "brand"        ← runtime, backward-compatible
  → Orchestrator(brand_dir=runtime)         ← fresh per request
  → build_step_run_context(brand_dir=..., product_refs=[...])
    → no category field in StepRunContext
  → StepRunContext(brand_dir)
  → agent.run(step_context=ctx)
    → self.brand_dir = ctx.brand_dir        ← runtime brand
```

---

## 4. G2 — Revised Design (source-driven, not category-driven)

### 4.1 Principle

The engine must not branch on a category string. Instead, it reads what the
product source and user request actually contain and adapts:

- Evidence fields come from what the source mentions, not a fixed catalog.
- Source relevance comes from matching the product's own terms, not a
  hardcoded keyword list.
- Spec/distribution/price detectors use general patterns, not a
  smartwatch-specific token set.

### 4.2 Inventory of production hardcodes (read-only, G2 scope)

| File | Lines | Hardcode | What it controls | G2 fix direction |
|------|-------|----------|------------------|------------------|
| `src/agents/competitor_evidence.py` | 14–55 | `FIELD_CATALOG` (display, battery, GPS, etc.) | Evidence schema enum | Make the field catalog open: accept any field ID the model returns, validate against the product source, not a fixed list. |
| `src/agents/competitor_evidence.py` | 57 | `ALLOWED_FIELD_IDS` | JSON schema enum | Remove the enum constraint; let the model return fields it actually found. |
| `src/agents/competitor_evidence.py` | 61–138 | `RESEARCH_RESPONSE_SCHEMA` | OpenRouter `response_format` | Open schema — no fixed field enum. |
| `src/agents/competitor_evidence.py` | 661–675 | `EVIDENCE_SYSTEM_PROMPT` field list | LLM prompt | Generate field examples from the product source, not a hardcoded list. |
| `src/agents/competitor_analysis.py` | 24 | `_FABRICATABLE_SPEC_RE` | Fabricated-spec detector | Use a general "claim has no source" check instead of a token allowlist. |
| `src/agents/competitor_analysis.py` | 25 | `_CURRENCY_RE` | Price detector | Keep — currency detection is generic. |
| `src/agents/competitor_analysis.py` | 27 | `_DISTRIBUTION_RE` (Shopee/Lazada) | Distribution detector | Generalize to "channel/distribution" terms or remove. |
| `src/agents/competitor_analysis.py` | 80 | Prompt example fields | Prompt nudge | Remove smartwatch field examples; let the model discover fields from source. |
| `src/agents/competitor_analysis.py` | 445–460 | `_extract_target_model` + `_derive_target_category` | Model/category guessing | Remove category guessing. Model extraction can stay if generic. |
| `src/agents/competitor_analysis.py` | 1219 | Smartwatch spec list in prompt | Prompt | Remove hardcoded spec categories. |
| `src/agents/campaign_strategy.py` | 109–116 | `_derive_target_category` | Category guessing | Remove — use product source terms for relevance. |
| `src/agents/campaign_strategy.py` | 170–185 | `if target_category == "smartwatch"` branches | Source relevance | Replace with source-term matching, not category branches. |
| `config/agents.yaml` | 228–251 | `relevance_policy` keywords | Source filtering | Remove hardcoded keywords; let the product source define relevance. |
| `src/agents/product_spec.py` | 33–39 | `K2+K3` comment, `หน้าปัด` guardrail | Prompt | Remove watch-specific examples. |
| `src/orchestrator.py` | 262–267 | `K5 + K2` comments | Multi-product split | Comments only — the `+` split is generic. |
| `src/orchestrator.py` | 888–891 | `list_products` category example | Tool prompt | Use a generic example, not `สมาร์ทวอทช์`. |

### 4.3 G2 design questions (to resolve before implementing)

1. **Evidence schema**: should the engine accept any field ID the model returns,
   or should it derive allowed fields from the product source text?
2. **Source relevance**: should relevance be based on term overlap with the
   product source, or should it be removed entirely (let the model decide)?
3. **Spec detector**: is a general "claim has no source citation" check
   sufficient, or do we need a minimal generic spec-token detector?
4. **Prompt examples**: should field examples be generated from the product
   source, or removed entirely?

These questions need Product Owner input before G2 implementation begins.

### 4.4 What G2 is NOT

- NOT `config/categories/{category}.yaml` — no category packs.
- NOT a category config loader — no `load_category_config()`.
- NOT a new architecture component — the engine stays, only the hardcodes
  are replaced with source-driven logic.
- NOT a schema migration — the evidence schema opens up, it doesn't move.

---

## 5. Category Packs — Future Optional Extension

Category packs (`config/categories/*.yaml`) are NOT part of G2. They are a
future optional extension for when a real use case needs per-category tuning
(e.g. a customer wants stricter validation for electronics specs).

If that use case arrives, the pack would:
- Be loaded by explicit user/tenant opt-in, not by `product_db.metadata.category`.
- Layer on top of the source-driven engine, not replace it.
- Never be required for the engine to work.

---

## 6. M6 Remediation Status

M6 remediation is on hold. The M6 validators were overfit to Lagenio K2 /
smartwatch. G0 reverted them. M6 remediation waits for the G2 source-driven
engine cleanup, NOT for a category architecture.

See `M6_STATUS.md` for the current M6 verdict and failure attribution.

---

## 7. Test Hygiene

- `tests/test_m6_remediation.py` was moved to `tests/evaluation_artifacts/`
  to remove it from pytest discovery. It is preserved as evaluation evidence,
  not production regression. It will be rebased onto the source-driven engine
  in G2.
- `tests/test_g1_runtime_contract.py` verifies the source-driven contract:
  brand_dir propagation, category as optional metadata, multi-product
  cross-category, no category needed for any product type.
