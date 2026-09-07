# Brand-Generalization Architecture Audit

## Scope & Methodology

- **Mode**: read-only / offline only. No paid/free model calls, no web/image/video, no code/config/test edits, no commit/push.
- **Sources inspected**:
  - `git log` from M1 baseline (`a85d3bd`) to `HEAD`
  - `git diff` of the current working tree (uncommitted M6 remediation changes)
  - `config/agents.yaml`
  - `src/output_validators.py`
  - `src/agents/{base_agent,product_spec,competitor_analysis,competitor_evidence,campaign_strategy,content_creator}.py`
  - `src/brand_loader.py`, `src/brand_priority.py`, `src/product_db.py`, `src/orchestrator.py`, `src/run_context.py`, `src/file_loader.py`, `src/campaign_validator.py`, `src/content_history.py`
  - `web_viewer.py`
  - `brand/terms.example.json`
- **Categorization**:
  1. **Platform invariant** — works for every brand (format/schema, no-invent rules, citations).
  2. **Dynamic brand policy** — loaded from the selected brand (`brand/*.json`).
  3. **Dynamic product/category facts** — loaded from the selected product/category source (`product_db`, `product_profile.json`, raw source pack).
  4. **Overfit/static assumption** — tied to smartwatch/electronics/Lagenio or a single domain without belonging in core.

---

## TL;DR

1. **M1–M5 already contained production overfit**: `competitor_analysis` evidence mode ships a hardcoded smartwatch/electronics field catalog (`display`, `chipset_os`, `battery`, `gps`, ...), `relevance_policy` in `config/agents.yaml` hardcodes smartwatch/kids keywords and `keyboard/mouse/headphones` mismatch lists, and `campaign_strategy`/`competitor_analysis` derive category by grepping for `smartwatch`/`นาฬิกา`/`kids`.
2. **M6 remediation adds a second layer of overfit**: the uncommitted `output_quality.source_grounded` config in `config/agents.yaml` and `src/output_validators.py` hardcodes a smartwatch/electronics claim catalog (`CPU`, `OS`, `real-time`, `AI`, `navigation`, `voice`, `accuracy`) plus static Thai `puffery`/`soft` word lists as platform defaults.
3. **Hypothetical restaurant / apparel brands will fail** because the evidence catalog and claim validators expect electronics specs, and the category/relevance heuristics either misclassify them or give no category match.
4. **Do not commit the M6 `agents.yaml` `source_grounded` defaults as-is**, and do not leave `competitor_evidence.py` `FIELD_CATALOG` in code. Move domain-specific catalogs to per-category / per-product config.
5. **Recommended boundary**: keep `output_validators.py` as a generic source-grounded engine; inject claim patterns, field catalogs, and relevance keywords from `product_db.metadata.category` + `config/categories/{category}.yaml` (or `cache/{product_id}/validation_config.json`). Make `brand_dir` a runtime UI/tenant parameter instead of hardcoding `"brand"`.

---

## Direct Answers

### 1. Did M1–M5 already contain production overfit?

**Yes.** The strongest pre-M6 overfit is in `competitor_analysis` evidence mode and the `relevance_policy` config.

- `src/agents/competitor_evidence.py:14-55` defines `FIELD_CATALOG` with smartwatch/electronics fields: `display`, `chipset_os`, `battery`, `connectivity`, `water_resistance`, `gps`, `sensors`, `sports_modes`, `features`, `price_availability`. The same file enforces these as the only allowed field IDs through `ALLOWED_FIELD_IDS` and the JSON schema enum at `src/agents/competitor_evidence.py:84-85`.
- `config/agents.yaml:209` sets `evidence_mode: true`, so the field catalog is active by default.
- `config/agents.yaml:293-316` `relevance_policy` hardcodes `target_category_keywords` (`smartwatch`, `smart watch`, `สมาร์ทวอทช์`, `นาฬิกาอัจฉริยะ`), `mismatch_keywords` (`keyboard`, `mouse`, `headphones`, `earbuds`, ...), `subcategory_mismatch_keywords` (`kids`, `children`, `elderly`), and `market_keywords` (smartwatch terms).
- `src/agents/competitor_analysis.py:456-462` returns `"smartwatch"` for any keyword match from `target_category_keywords`.
- `src/agents/campaign_strategy.py:109-116` hardcodes its own `_derive_target_category` with `smartwatch`/`kids` keyword lists and a special market-match branch at `src/agents/campaign_strategy.py:170-185`.

These were introduced/kept through commits before and at M1 (`ed56f3b`, `6dc694c`, `a85d3bd`). The `6dc694c` commit message explicitly lists "prompt/field catalog ยัง hard-code ใน competitor_evidence.py" as known technical debt.

### 2. Which M6 remediation part first made the architecture overfit-risk?

The **first M6 remediation change that made the architecture risky is `output_quality.source_grounded` in `config/agents.yaml` and its implementation in `src/output_validators.py`**.

- `config/agents.yaml:138-181` (product_spec), `326-349` (competitor_analysis), and `834-888` (content_creator) add `source_grounded` blocks.
- They include `technical_attributes` with labels `["CPU", "ชิปเซ็ต", "ชิปประมวลผล"]` and `["OS", "ระบบปฏิบัติการ"]` (`config/agents.yaml:140-142`, `327-329`).
- They include `factual_claim_patterns` for `real_time`, `accuracy`, `ai`, `navigation`, `voice` (`config/agents.yaml:144-156`, `330-342`, `835-847`).
- They include long static Thai `puffery_allowlist` and `soft_wording_allowlist` (`config/agents.yaml:157-181`, `343-349`, `848-888`).

The implementation in `src/output_validators.py:254-453` (`_extract_source_values`, `_build_source_ngrams`, `_validate_technical_attributes`, `_validate_factual_claim_patterns`, `_validate_comparative`) is *generic* and derives values from the source pack at runtime, which is good. But the **catalog of claim types and allowed soft words is being injected as a platform default**, not as a per-category or per-product config. That is the overfit boundary violation.

### 3. Where would a hypothetical restaurant brand and apparel brand fail / be wrongly blocked?

#### Restaurant brand example

- `competitor_analysis` evidence mode: the model is told to collect evidence only for `display`, `chipset_os`, `battery`, `gps`, etc. A restaurant has none of these, so `evidence` array becomes empty or filled with forced mismatches. The renderer still tries to build a comparison table using those field labels (`src/agents/competitor_evidence.py:426-436`, `513-517`), producing irrelevant or blank sections.
- `_derive_target_category` in `competitor_analysis.py` and `campaign_strategy.py` does not detect "restaurant", so `target_category` stays `"unknown"`. Web-search sources will not get the `market` relevance boost and may be dropped as `unknown`.
- `factual_claim_patterns` in `source_grounded` will block common restaurant marketing claims:
  - "AI แนะนำเมนู" → `ai` pattern rejects if `AI`/`artificial intelligence` not in source (`config/agents.yaml:152-153`).
  - "real-time สั่งอาหาร" → `real_time` pattern rejects (`config/agents.yaml:150-151`).
  - "แม่นยำตามสูตร" → `accuracy` pattern rejects (`config/agents.yaml:154-155`).
  - "วิดีโอคอลดูครัว" → `voice`/`video call` pattern rejects (`config/agents.yaml:158-159`).
- `soft_wording_allowlist` does not contain restaurant adjectives such as `อร่อย`, `สด`, `เผ็ด`, `หอม`, `กรอบ`. A comparative caption like "อร่อยกว่าร้านข้างๆ" or "สดกว่า" will be flagged as `ungrounded-advantage` unless the exact word appears in the source pack (`src/output_validators.py:401-417`).

#### Apparel brand example

- `competitor_analysis` evidence mode: same problem. Apparel specs (fabric, fit, size, breathability, color) do not fit `chipset_os`/`battery`/`gps`/`water_resistance`, so the model is forced into an electronics-shaped output.
- Category misclassification: if the product description contains the English word `watch` (e.g. "watch brand", "watch collection"), `_derive_target_category` returns `"smartwatch"` (`src/agents/campaign_strategy.py:110-112`, `src/agents/competitor_analysis.py:460-462`). Then the mismatch keywords (`keyboard`, `mouse`, `headphones`, `earbuds`) are applied to web sources, even though they are irrelevant for apparel.
- `factual_claim_patterns`: an apparel brand using "AI แนะนำไซส์", "real-time stock", "แม่นยำ" will be blocked.
- `soft_wording_allowlist` lacks apparel-specific adjectives: `นุ่ม` (soft), `สบาย` (comfortable), `ระบายอากาศ` (breathable), `ทนทาน` (durable), `ยืดหยุ่น` (stretchy). Comparatives such as "นุ่มกว่า", "สบายกว่า", "ทนทานกว่า" will be rejected if the source pack does not literally contain those words.
- `_FABRICATABLE_SPEC_RE` in `src/agents/competitor_analysis.py:24` matches units like `mm`, `cm`, `w`, `v`. Apparel size charts commonly use `cm`; if a competitor source mentions `chest 90 cm` and the product source does not, the validator may flag it as a fabricated spec.
- `_extract_target_model` regex `\b([A-Za-z]+\d[\w]{0,4})\b` (`src/agents/competitor_analysis.py:453`) assumes model names are letter+digit. Apparel SKUs like "Shirt-M-Blue" or "Dress-Summer24" may not match, weakening competitor identity checks.

### 4. What should not be committed as-is?

**Do not commit the uncommitted `config/agents.yaml` `source_grounded` defaults** until the following are moved out of platform defaults:

- `technical_attributes` with `CPU`/`OS` labels.
- `factual_claim_patterns` (`real_time`, `accuracy`, `ai`, `navigation`, `voice`).
- `puffery_allowlist` and `soft_wording_allowlist` as long static Thai lists.

**Also do not ship `src/agents/competitor_evidence.py` `FIELD_CATALOG` as a Python constant** for a multi-brand platform. It is acceptable as a temporary Beta artifact, but it is a hardcoded smartwatch/electronics schema.

**Do not keep `src/agents/competitor_analysis.py:456-462` and `src/agents/campaign_strategy.py:109-116` as hardcoded keyword classifiers**. They return `"smartwatch"` for anything that matches the keyword list, regardless of the actual product category stored in `product_db`.

`M6_STATUS.md`, `M6_*_REPORT.md`, and `M6_REMEDIATION_DESIGN.md` are documentation/running artifacts and can stay, but they should not be confused with the production code boundary.

### 5. Recommended architecture boundary for a multi-brand platform

- **Core validators remain generic**. `src/output_validators.py` should keep `_extract_source_values`, `_build_source_ngrams`, `_validate_factual_claim_patterns`, `_validate_comparative`, `_validate_technical_attributes` as pure functions that accept a config object.
- **Domain-specific catalogs live outside `agents.yaml` defaults**:
  - `config/categories/{category}.yaml` or `cache/{product_id}/validation_config.json` supplies `technical_attributes`, `factual_claim_patterns`, `puffery_allowlist`, `soft_wording_allowlist`, and `relevance_policy`.
  - `src/agents/competitor_evidence.py` `FIELD_CATALOG` should be loaded from category config, not hardcoded in code.
- **Category is a first-class runtime value**:
  - `product_db` already stores `metadata.category` (`src/product_db.py:494-520`).
  - `orchestrator.py` should pass `category` (from `product_db.get_product_metadata(product_id)['category']`) into agents.
  - `competitor_analysis._derive_target_category` and `campaign_strategy._derive_target_category` should use `metadata.category` instead of keyword grepping.
- **Brand should be selectable at runtime**:
  - `web_viewer.py` currently hardcodes `BRAND_DIR = PROJECT_ROOT / "brand"` (`web_viewer.py:59`) and instantiates `Orchestrator(brand_dir="brand")` in many places (`web_viewer.py:166, 1594, 1646, 1883, 2077, ...`).
  - Accept `brand_id`/`brand_dir` from UI/tenant and pass it to `Orchestrator` and `load_brand_*` functions.
- **Source text isolation stays**: keep `base_agent.py:70-77` `_get_source_text` hook and `_trusted_source_text` assignments in `product_spec.py:30`, `competitor_analysis.py:44`, `content_creator.py:24`. This is the right boundary between trusted product facts and generated artifacts.

---

## Timeline

| Commit | Date | What it introduced | Brand-generalization impact |
|--------|------|-------------------|------------------------------|
| `ed56f3b` | 2026-08-27 | Added `relevance_policy` to `config/agents.yaml` with smartwatch/kids keywords and keyboard/mouse/headphones mismatch keywords. Added offline source relevance filtering. | First hardcoded category heuristic in production config. |
| `6dc694c` | 2026-08-28 | Added `src/agents/competitor_evidence.py` with `FIELD_CATALOG` (display, chipset_os, battery, gps, ...) and `evidence_mode`. Commit message lists "prompt/field catalog ยัง hard-code" as technical debt. | Hardcoded smartwatch/electronics evidence schema. |
| `a85d3bd` (M1) | 2026-09-01 | "Remove over-constraint and fixed templates." | Removed some templates but kept `relevance_policy`, `FIELD_CATALOG`, and category keyword heuristics. Did not generalize. |
| Current working tree | — | M6 remediation adds `source_grounded` validators to `src/output_validators.py` and `config/agents.yaml`. | Adds a second layer of domain-specific claim patterns (`CPU`, `OS`, `real-time`, `AI`, ...) as platform defaults. |

---

## Findings by Group

### 1. Platform invariant (correctly generic)

| Component | Why it is platform invariant |
|-----------|------------------------------|
| `src/output_validators.py` JSON schema, blank, markdown section, quality gate | Format/schema checks do not depend on brand or product domain. |
| `src/output_validators.py` `_brand_hard_validator` mechanism | The *mechanism* searches `brand_rules.hard_dict` loaded at runtime; the values come from `brand/terms.json`. |
| `src/output_validators.py` `_one_page_limit` | Length contract based on user `quick_brief`, not product domain. |
| `src/campaign_validator.py` number/percent/currency/inline citation checks | Generic financial/evidence rules. |
| `src/agents/base_agent.py` `_get_source_text` / `_last_source_text` | Separates trusted source pack from untrusted generated context (`src/agents/base_agent.py:70-77`, `344`). |
| `src/product_db.py` per-product storage and `get_agent_context_text` | Product facts come from `cache/{product_id}/` and `data/{product_id}/`; structure is generic. |
| `src/run_context.py` `StepRunContext` typed refs and phase views | Workflow plumbing; no domain assumptions. |
| `src/brand_loader.py` / `src/brand_priority.py` | Load `brand/*.json` generically; product-profile overrides are per `product_id`. |

### 2. Dynamic brand policy (loaded from selected brand)

| Source | Loaded by | Used for |
|--------|-----------|----------|
| `brand/voice.json` | `src/brand_loader.py:126-160` | Personality, tone, formality, language, banned_phrases. |
| `brand/terms.json` (example only: `brand/terms.example.json`) | `src/brand_loader.py:139-148`, `src/brand_priority.py:137-152` | `approved`, `restricted`, `replacements`. Hard rules enforced in `src/output_validators.py:225-247`. |
| `brand/audience.json` | `src/brand_loader.py:178-180` | Target audience reference. |
| `brand/visual.json` | `src/brand_loader.py:203-225`, `web_viewer.py:101-107` | Visual keywords for `media_gen`. |
| `cache/{product_id}/product_profile.json` `tone_adjustment` / `visual_override` | `src/brand_loader.py:151-155`, `218-223` | Per-product brand overrides. |

**Gap**: `web_viewer.py` hardcodes `BRAND_DIR = PROJECT_ROOT / "brand"` (`web_viewer.py:59`) and `Orchestrator(brand_dir="brand")` in many routes. The loading functions are ready for multi-brand, but the UI is singleton.

### 3. Dynamic product/category facts (loaded from selected product source)

| Component | How it loads product facts |
|-----------|---------------------------|
| `src/product_db.py` | `data/{product_id}/` raw files → `cache/{product_id}/product.json` with `raw_text`, `image_descriptions`, `video_transcripts`, `audio_transcripts`, `metadata` including `category`. |
| `src/orchestrator.py:244-288` `_get_product_data` / `_get_product_image_paths` | Reads from `product_db` per `product_id`; supports multi-product `product_id = "A + B"`. |
| `src/agents/product_spec.py:30` | `self._trusted_source_text = raw_data`. |
| `src/agents/competitor_analysis.py:44` | `self._trusted_source_text = product_spec`. |
| `src/agents/content_creator.py:24` | `self._trusted_source_text = product_spec`. |
| `src/output_validators.py:254-278` `_extract_source_values` | Parses runtime source text for label values (`CPU: ...`, `OS: ...`). |
| `src/output_validators.py:280-289` `_build_source_ngrams` | Builds 1-3 word n-grams from trusted source text for claim lookup. |

**Gap**: `product_db` already stores `metadata.category`, but `competitor_analysis` and `campaign_strategy` ignore it and use keyword grepping.

### 4. Overfit / static assumptions (smartwatch / electronics / Lagenio)

#### A. Pre-M1 / M1–M5 overfit

| Location | Overfit detail | Evidence |
|----------|----------------|----------|
| `config/agents.yaml:209` | `evidence_mode: true` activates `competitor_evidence` catalog. | `evidence_mode: true` |
| `src/agents/competitor_evidence.py:14-55` | `FIELD_CATALOG` hardcoded to smartwatch/electronics fields. | `FIELD_CATALOG = { ... }` |
| `src/agents/competitor_evidence.py:84-85` | JSON schema enum enforces only those field IDs. | `enum: list(ALLOWED_FIELD_IDS)` |
| `src/agents/competitor_evidence.py:639-701` | `EVIDENCE_SYSTEM_PROMPT` instructs model to use those field IDs. | field catalog list in prompt |
| `config/agents.yaml:293-316` | `relevance_policy` hardcoded smartwatch/kids keywords and keyboard/mouse/headphones mismatch. | `target_category_keywords`, `mismatch_keywords`, etc. |
| `src/agents/competitor_analysis.py:456-462` | Returns `"smartwatch"` for any keyword match, regardless of actual category. | `def _derive_target_category` |
| `src/agents/campaign_strategy.py:109-116` | Hardcoded smartwatch/kids keyword classifier. | `def _derive_target_category` |
| `src/agents/campaign_strategy.py:170-185` | Special market-match branches only for `smartwatch` and `kids`. | `if target_category == "smartwatch"` / `== "kids"` |
| `src/agents/competitor_analysis.py:24` | `_FABRICATABLE_SPEC_RE` lists electronics units (`mah`, `ghz`, `amoled`, `gps`, `cpu`, ...). | regex constant |
| `src/agents/competitor_analysis.py:27` | `_DISTRIBUTION_RE` hardcodes `shopee`/`lazada`. | regex constant |
| `src/agents/competitor_analysis.py:448-454` | `_extract_target_model` regex assumes letter+digit model names and comments `K77, Lagenio K5`. | regex + comment |

#### B. M6 uncommitted overfit

| Location | Overfit detail | Evidence |
|----------|----------------|----------|
| `config/agents.yaml:138-181` | `product_spec` `source_grounded` with `CPU`/`OS` technical attributes and `real_time`/`ai`/`navigation`/`voice`/`accuracy` claim patterns. | `source_grounded` block |
| `config/agents.yaml:326-349` | Same `source_grounded` block under `competitor_analysis`. | `source_grounded` block |
| `config/agents.yaml:834-888` | Same `source_grounded` block under `content_creator`. | `source_grounded` block |
| `src/output_validators.py:296-353` | `_validate_technical_attributes` expects `CPU`/`OS` labels. | function implementation |
| `src/output_validators.py:356-382` | `_validate_factual_claim_patterns` validates the five hardcoded claim types. | function implementation |
| `src/output_validators.py:385-418` | `_validate_comparative` uses static `puffery`/`soft` lists from config. | function implementation |

#### C. UI / brand singleton

| Location | Overfit detail | Evidence |
|----------|----------------|----------|
| `web_viewer.py:59` | `BRAND_DIR = PROJECT_ROOT / "brand"` | constant |
| `web_viewer.py:166, 1594, 1646, 1883, 2077, 2953, 3156, 3552` | `Orchestrator(brand_dir="brand")` | repeated hardcoded brand path |
| `src/orchestrator.py:97-99` | Constructor accepts `brand_dir`, but UI always passes `"brand"`. | `__init__` signature vs call sites |

#### D. Trace / smell (comments/docstrings/examples, not runtime)

| Location | Why it is a smell | Evidence |
|----------|-------------------|----------|
| `src/content_history.py:13` | Docstring example uses `Lagenio K5` and GPS. | `product_ids: ["Lagenio K5"]` |
| `src/agents/product_spec.py:48,52` | Comments mention `K2+K3` and `หน้าปัด` (watch face). | comments |
| `src/campaign_validator.py:221,713,847,891` | Comments mention `LAGENIO K2` / `Lagenio`. | comments |
| `src/orchestrator.py:250,254` | Comments use `K5 + K2` / `Lagenio K5 + Lagenio K2` as examples. | comments |

---

## Risk Matrix

| Component | Risk | Why | Severity | Mitigation |
|-----------|------|-----|------------|------------|
| `competitor_evidence.py` `FIELD_CATALOG` | Competitor reports are forced into a smartwatch/electronics schema. | Hardcoded field IDs and prompt. | **High** | Load `FIELD_CATALOG` from category config. |
| `competitor_analysis._derive_target_category` | Any product containing `watch`/`นาฬิกา` is classified as `smartwatch`. | Returns hardcoded string. | **High** | Use `product_db.get_product_metadata(product_id)["category"]`. |
| `campaign_strategy._derive_target_category` | Same as above, plus special-cases only `smartwatch`/`kids` for market match. | Hardcoded keyword classifier. | **High** | Use product metadata category and config-driven market keywords. |
| `config/agents.yaml` `relevance_policy` | Web sources for non-smartwatch categories get no market match and may be dropped. | Smartwatch/kids keywords and keyboard/mouse mismatch list. | **High** | Move to `config/categories/{category}.yaml`. |
| M6 `source_grounded` in `config/agents.yaml` | Restaurant/apparel claims like `AI`, `real-time`, `accuracy`, `navigation`, `voice` are blocked if not in source. | Static `factual_claim_patterns` platform defaults. | **High** | Move claim patterns to category config. |
| M6 `technical_attributes` (`CPU`/`OS`) | Irrelevant for non-electronics; if user asks "สเปค" for apparel/food, output may be blocked. | Labels are electronics-specific. | **Medium** | Move to category config. |
| M6 `puffery`/`soft_wording` allowlists | Comparative adjectives outside the list are rejected. | Static Thai word lists. | **Medium** | Derive from brand/category `terms.json` or category config. |
| `web_viewer.py` single `brand` folder | No multi-tenant/multi-brand support at UI layer. | `BRAND_DIR` and `Orchestrator(brand_dir="brand")` hardcoded. | **Medium** | Pass `brand_id` from UI to orchestrator. |
| `competitor_analysis._FABRICATABLE_SPEC_RE` | Apparel size charts using `cm`/`mm` may be flagged as fabricated specs. | Electronics unit regex. | **Low-Medium** | Make spec-unit regex category-specific. |
| `competitor_analysis._DISTRIBUTION_RE` | Only recognizes `shopee`/`lazada` as distribution signals. | Hardcoded marketplace names. | **Low** | Load from category/brand config. |
| `competitor_analysis._extract_target_model` | SKUs without letter+digit pattern are not recognized. | Regex assumption. | **Low** | Use `รหัสสินค้า` label first; fallback to any uppercase/token heuristic. |

---

## Recommended Next Steps

1. **Stop and redesign the M6 `source_grounded` config before committing.** Move `technical_attributes`, `factual_claim_patterns`, `puffery_allowlist`, and `soft_wording_allowlist` out of `config/agents.yaml` defaults.
2. **Create `config/categories/` directory** (e.g., `smartwatch.yaml`, `restaurant.yaml`, `apparel.yaml`) or store per-product `validation_config.json` in `cache/{product_id}/`.
3. **Use `product_db` `metadata.category`** as the category source of truth and pass it into agents/validators.
4. **Refactor `competitor_evidence.py`** to load `FIELD_CATALOG` from category config and remove `ALLOWED_FIELD_IDS` as a Python constant.
5. **Remove hardcoded keyword classifiers** in `competitor_analysis.py` and `campaign_strategy.py`; replace with category-driven config.
6. **Parameterize `brand_dir`** in `web_viewer.py` routes so each workspace/tenant can point to its own brand folder.
7. **Keep the generic source-grounded engine** in `src/output_validators.py` and `base_agent.py`; it is the right abstraction, it just needs domain-agnostic inputs.

---

## Audit Confirmation

- This audit was performed **read-only / offline**.
- No paid or free model API calls, no web/image/video calls, no code/config/test edits, no `git commit` or `git push` were made.
- The only file created is this report: `BRAND_GENERALIZATION_AUDIT.md`.
- Evidence is cited from the working tree at the time of audit; line numbers refer to the current working tree before any M6 remediation is committed.
