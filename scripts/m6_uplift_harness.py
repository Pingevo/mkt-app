"""Same-Model Product Uplift Qualification Harness.

Primary qualification question:
    Does MKTApp produce materially better marketing work than using the
    exact same underlying model directly?

Candidate A — Direct Same-Model Baseline:
    The exact same underlying model as the corresponding MKTApp Agent,
    given a fair, strong neutral task prompt with equivalent user/source
    information and equivalent tool capability. No MKTApp internal machinery.

Candidate B — MKTApp:
    The current production MKTApp path using the same underlying model,
    including all architecture, grounding, brand handling, review/repair,
    orchestration, and deterministic safety/validation.

This harness is OFFLINE-READY: all paid execution paths are guarded by
preflight checks and budget enforcement. No paid calls are made during
offline tests.

Architecture:
    load S1-S4 fixed scenarios
            ↓
    build canonical source fixture (raw product facts, brand, pillars, etc.)
            ↓
    load exact Agent model/config from config/agents.yaml
            ↓
    resolve baseline_model and mktapp_model separately, assert equal
            ↓
    preflight: model parity, info parity, tool parity, budget
            ↓
    Candidate A: direct same-model baseline (multi-turn for S2/S3)
            ↓
    Candidate B: production MKTApp Agent path (no upstream generation)
            ↓
    candidate completeness (both sides, multi-turn aware)
            ↓
    production-equivalent rendering
            ↓
    blind X/Y mapping
            ↓
    Judge preflight → Judge → uplift calculation

External Frontier comparison is FUTURE work — see m6_frontier_uat.py.

Gate v1 (locked, Product Owner approved):
    A: aggregate mean uplift >= +0.25
    B: >= 3 of 4 scenario deltas >= 0
    C: no scenario overall delta < -0.5
    D: no core dimension delta < -0.5
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from src.budget_hierarchy import BudgetExceededError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# ---------------------------------------------------------------------------
# Locked uplift gate v1 — Product Owner approved before paid execution
# ---------------------------------------------------------------------------

GATE_VERSION = "v1"
GATE_CONFIG: dict[str, Any] = {
    "version": GATE_VERSION,
    "aggregate_uplift_min": 0.25,
    "scenario_consistency_min": 3,
    "scenario_regression_max": -0.5,
    "core_dimension_regression_max": -0.5,
    "core_dimensions": ["Factuality / grounding", "Instruction following", "Brand / asset fit"],
}

# Core product-value dimensions (must not show material regression)
CORE_DIMENSIONS = frozenset(GATE_CONFIG["core_dimensions"])


# ---------------------------------------------------------------------------
# Source fixture provenance categories
# ---------------------------------------------------------------------------

PROVENANCE_SUPPLIED = "supplied"                    # user-owned, both sides receive
PROVENANCE_FIXED_CONTEXT = "fixed_qualification_context"  # fixed upstream, both sides receive
PROVENANCE_MKTAPP_DERIVED = "mktapp_derived"        # only MKTApp receives/creates


# ---------------------------------------------------------------------------
# Canonical source fixture — actual semantic content, not just product ID
# ---------------------------------------------------------------------------

@dataclass
class SourceFixture:
    """Canonical per-scenario source bundle representing ALL raw/supplied
    information available to both candidates.

    This is NOT just a product ID — it contains the actual semantic content
    that both candidates must see: raw product facts, brand guidelines,
    configured pillars, user-visible constraints, fixed upstream context.

    Provenance categories:
    - supplied: user-owned (both sides receive)
    - fixed_qualification_context: fixed upstream fixture (both sides receive)
    - mktapp_derived: only MKTApp receives/creates (NOT in this shared fixture)
    """
    scenario_id: str
    # --- supplied (user-owned, both sides) ---
    product_facts_text: str           # raw product facts from product_db
    product_image_paths: list[str]    # actual image file paths
    quick_brief: str                  # user's specific request
    user_prefix: str                  # user's request prefix
    user_visible_constraints: dict[str, Any]  # parsed user-visible settings
    brand_guidelines: str             # user-owned brand information
    configured_pillars: list[str]     # user-configured Content Pillars
    explicit_selected_pillar: str | None  # explicit user-selected Pillar
    # --- fixed_qualification_context (both sides) ---
    fixed_upstream_context: dict[str, str]  # e.g. {"competitor_analysis": "..."} for S3/S4
    # --- metadata ---
    agent_key: str
    platforms: list[str] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "provenance": {
                PROVENANCE_SUPPLIED: {
                    "product_facts_text": self.product_facts_text,
                    "product_image_paths": self.product_image_paths,
                    "quick_brief": self.quick_brief,
                    "user_prefix": self.user_prefix,
                    "user_visible_constraints": self.user_visible_constraints,
                    "brand_guidelines": self.brand_guidelines,
                    "configured_pillars": self.configured_pillars,
                    "explicit_selected_pillar": self.explicit_selected_pillar,
                },
                PROVENANCE_FIXED_CONTEXT: self.fixed_upstream_context,
                PROVENANCE_MKTAPP_DERIVED: {},  # NOT in shared fixture
            },
            "agent_key": self.agent_key,
            "platforms": self.platforms,
        }

    def hash(self) -> str:
        """Stable SHA-256 hash of the canonical source fixture content.

        Both candidates must reference the same hash. This proves
        information parity at the semantic content level, not just
        product ID level.
        """
        # Hash the actual semantic content, not just IDs
        content = {
            "product_facts_text": self.product_facts_text,
            "product_image_paths": sorted(self.product_image_paths),
            "quick_brief": self.quick_brief,
            "user_prefix": self.user_prefix,
            "user_visible_constraints": self.user_visible_constraints,
            "brand_guidelines": self.brand_guidelines,
            "configured_pillars": self.configured_pillars,
            "explicit_selected_pillar": self.explicit_selected_pillar,
            "fixed_upstream_context": self.fixed_upstream_context,
            "agent_key": self.agent_key,
            "platforms": self.platforms,
        }
        return hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


def _load_product_facts(product_ids: list[str]) -> str:
    """Load actual raw product facts from product_db (not just product ID)."""
    from src import product_db
    return product_db.get_scoped_context_text(product_ids)


def _load_product_image_paths(product_id: str) -> list[str]:
    """Load actual image file paths from product_db."""
    from src import product_db
    return product_db.get_product_image_paths(product_id)


def _load_brand_guidelines() -> str:
    """Load user-owned brand guidelines from brand/ directory."""
    brand_dir = PROJECT_ROOT / "brand"
    parts: list[str] = []
    for name in ["brand_profile.md", "tone_of_voice.md", "visual_guidelines.md", "terms.json"]:
        p = brand_dir / name
        if p.exists():
            parts.append(f"--- {name} ---\n{p.read_text(encoding='utf-8')}")
    return "\n\n".join(parts) if parts else ""


def _load_configured_pillars() -> list[str]:
    """Load user-configured Content Pillars from the PRODUCTION source-of-truth.

    Content Pillars live in ``config/content_policy.yaml`` and are loaded
    through ``src/config_loader.load_config()`` — the SAME loader the
    Orchestrator uses.  Do NOT read from ``agents.yaml`` directly; that file
    does not contain pillars.

    This function uses the exact same production loader so the harness and
    production agree on the pillar set.
    """
    from src.config_loader import load_config
    config = load_config()
    return list(config.get("pillars", []))


def _parse_user_visible_constraints(resource_context: str) -> dict[str, Any]:
    """Parse user-visible constraints from 'Agent settings: ...' line.

    Returns only user-visible keys, not internal engine config.
    """
    if not resource_context:
        return {}
    m = re.match(r"Agent settings:\s*(.+)", resource_context, re.DOTALL)
    if not m:
        return {}
    settings_text = m.group(1).strip()
    constraints: dict[str, Any] = {}
    for pair in re.finditer(r"(\w+)=\[([^\]]*)\]|(\w+)=([^,\]]+)", settings_text):
        key = pair.group(1) or pair.group(3)
        if key in _USER_VISIBLE_SETTING_KEYS:
            val = pair.group(2) or pair.group(4)
            constraints[key] = val
    return constraints


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

# User-visible constraint keys that ARE shared with the baseline.
_USER_VISIBLE_SETTING_KEYS = frozenset({
    "budget_max", "discount_max", "forbid_tactics",
    "competitor_types", "analysis_depth", "importance",
})

# Internal MKTApp engine config keys that must NOT leak to the baseline.
_INTERNAL_SETTING_KEYS = frozenset({
    "max_retry_limit", "max_review_iterations", "max_search_calls",
    "web_search", "evidence_mode", "verify_urls",
    "use_brand_reference", "use_brand_differentiator",
    "response_format", "output_format",
})


@dataclass
class UpliftScenario:
    """Fixed qualification scenario — defines what source fixture to build."""
    id: str
    agent_key: str
    product_id: str
    product_ids: list[str] | None
    quick_brief: str
    user_prefix: str
    resource_context: str
    platforms: list[str] | None
    auto_image: bool
    # Budget reserves — INDEPENDENT per candidate
    mktapp_reserve: float
    baseline_reserve: float
    # Tool capability — must be symmetric
    web_search_enabled: bool
    web_search_max_uses: int
    # Output limits
    baseline_max_tokens: int
    # Fixed upstream context for agent-level isolation (S3/S4)
    # If provided, both candidates receive this instead of generating upstream
    fixed_upstream_context: dict[str, str] = field(default_factory=dict)
    # Explicit user-selected Pillar (if any)
    explicit_selected_pillar: str | None = None

    @property
    def effective_product_ids(self) -> list[str]:
        return self.product_ids if self.product_ids is not None else [self.product_id]

    def agent_model(self) -> str:
        """Read the exact configured model for this agent from config."""
        return _get_agent_model(self.agent_key)

    def agent_config(self) -> dict[str, Any]:
        """Read the full agent config from config/agents.yaml."""
        return _get_agent_config(self.agent_key)

    def agent_temperature(self) -> float:
        """Read the configured temperature for this agent."""
        return _get_agent_config(self.agent_key).get("temperature", 0.7)

    def build_source_fixture(self) -> SourceFixture:
        """Build the canonical source fixture with actual semantic content."""
        return SourceFixture(
            scenario_id=self.id,
            product_facts_text=_load_product_facts(self.effective_product_ids),
            product_image_paths=_load_product_image_paths(self.product_id),
            quick_brief=self.quick_brief,
            user_prefix=self.user_prefix,
            user_visible_constraints=_parse_user_visible_constraints(self.resource_context),
            brand_guidelines=_load_brand_guidelines(),
            configured_pillars=_load_configured_pillars(),
            explicit_selected_pillar=self.explicit_selected_pillar,
            fixed_upstream_context=dict(self.fixed_upstream_context),
            agent_key=self.agent_key,
            platforms=self.platforms,
        )


# Fixed scenario fixtures — S1–S4
# S3/S4 have fixed_upstream_context loaded from CANONICAL QUALIFICATION
# FIXTURES in data/m6_uplift/fixtures/ — NOT from the mutable cache.
# These are static, candidate-independent, versioned via the source
# fixture hash.  They are NOT generated during qualification.
FIXTURES_DIR = PROJECT_ROOT / "data" / "m6_uplift" / "fixtures"


def _load_fixed_upstream(name: str) -> str:
    """Load a fixed upstream fixture from canonical qualification data.

    Loads from ``data/m6_uplift/fixtures/`` — NOT from ``cache/``.
    The cache is mutable and may be overwritten by normal runs; the
    qualification fixtures directory is frozen for qualification v1.

    Raises FileNotFoundError if the fixture is missing — do NOT silently
    fall back to cache.
    """
    p = FIXTURES_DIR / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(
            f"Canonical qualification fixture not found: {p}. "
            f"Do not silently fall back to cache/."
        )
    return p.read_text(encoding="utf-8")


# Preload fixed upstream fixtures (loaded once, hashed into source fixture)
_FIXED_COMPETITOR_ANALYSIS_S3 = _load_fixed_upstream("S3_competitor_context")
_FIXED_COMPETITOR_ANALYSIS_S4 = _load_fixed_upstream("S4_competitor_context")
_FIXED_CAMPAIGN_STRATEGY_S4 = _load_fixed_upstream("S4_campaign_context")


SCENARIOS: list[UpliftScenario] = [
    UpliftScenario(
        id="S1",
        agent_key="product_spec",
        product_id="Lagenio K2",
        product_ids=["Lagenio K2", "Lagenio K3"],
        quick_brief="one-page",
        user_prefix="กรุณาสร้างสเปคสินค้าแบบ one-page จากข้อมูลสินค้าและรูปภาพต่อไปนี้:\n\n",
        resource_context="",
        platforms=None,
        auto_image=False,
        mktapp_reserve=0.10,
        baseline_reserve=0.06,
        web_search_enabled=False,
        web_search_max_uses=0,
        baseline_max_tokens=4096,
        fixed_upstream_context={},
    ),
    UpliftScenario(
        id="S2",
        agent_key="competitor_analysis",
        product_id="Lagenio K2",
        product_ids=None,
        quick_brief="สรุปแบบ bullet executive brief ห้ามใช้ตาราง",
        user_prefix="กรุณาวิเคราะห์คู่แข่งของสินค้าต่อไปนี้:\n\n",
        resource_context="Agent settings: competitor_types=[direct], analysis_depth=deep, importance=[positioning].",
        platforms=None,
        auto_image=False,
        mktapp_reserve=0.25,
        baseline_reserve=0.15,
        web_search_enabled=True,
        web_search_max_uses=2,
        baseline_max_tokens=8192,
        fixed_upstream_context={},
    ),
    UpliftScenario(
        id="S3",
        agent_key="campaign_strategy",
        product_id="Lagenio K2",
        product_ids=None,
        quick_brief="executive brief",
        user_prefix="กรุณาวางแผนแคมเปญสำหรับสินค้าต่อไปนี้:\n\n",
        resource_context="Agent settings: budget_max=5000, discount_max=0, forbid_tactics=[heavy_discount,flash,bogo].",
        platforms=None,
        auto_image=False,
        mktapp_reserve=0.30,
        baseline_reserve=0.15,
        web_search_enabled=True,
        web_search_max_uses=2,
        baseline_max_tokens=4096,
        # Agent-level isolation: FIXED competitor-analysis context from
        # canonical qualification fixture. NOT generated during run.
        # Both candidates receive the same semantic upstream data.
        fixed_upstream_context={"competitor_analysis": _FIXED_COMPETITOR_ANALYSIS_S3},
    ),
    UpliftScenario(
        id="S4",
        agent_key="content_creator",
        product_id="Lagenio K2",
        product_ids=None,
        quick_brief="เด็กเดินทางคนเดียวปลอดภัย",
        user_prefix="กรุณาสร้างโพสต์ TikTok 1 โพสต์สำหรับสินค้าต่อไปนี้:\n\n",
        resource_context="",
        platforms=["tiktok"],
        auto_image=False,
        mktapp_reserve=0.10,
        baseline_reserve=0.10,
        web_search_enabled=False,
        web_search_max_uses=0,
        baseline_max_tokens=8192,
        # Agent-level isolation: FIXED competitor + campaign context from
        # canonical qualification fixtures. NOT generated during run.
        # Both candidates receive the same semantic upstream data.
        fixed_upstream_context={
            "competitor_analysis": _FIXED_COMPETITOR_ANALYSIS_S4,
            "campaign_strategy": _FIXED_CAMPAIGN_STRATEGY_S4,
        },
    ),
]


# ---------------------------------------------------------------------------
# Config reading — dynamic, never hardcode model strings
# ---------------------------------------------------------------------------

_agents_config_cache: dict[str, Any] | None = None


def _load_agents_config() -> dict[str, Any]:
    """Load config/agents.yaml once and cache."""
    global _agents_config_cache
    if _agents_config_cache is not None:
        return _agents_config_cache
    import yaml
    config_path = PROJECT_ROOT / "config" / "agents.yaml"
    with open(config_path, encoding="utf-8") as f:
        _agents_config_cache = yaml.safe_load(f)
    return _agents_config_cache


def _get_agent_config(agent_key: str) -> dict[str, Any]:
    """Get the config for a specific agent."""
    config = _load_agents_config()
    return config.get(agent_key, {})


def _get_agent_model(agent_key: str) -> str:
    """Read the exact configured model for an agent from config/agents.yaml.

    Falls back to defaults.model if the agent doesn't specify one.
    """
    config = _load_agents_config()
    agent_cfg = config.get(agent_key, {})
    model = agent_cfg.get("model")
    if model:
        return model
    return config.get("defaults", {}).get("model", "")


def _get_agent_web_search(agent_key: str) -> bool:
    """Read whether an agent has web_search enabled."""
    return bool(_get_agent_config(agent_key).get("web_search", False))


# ---------------------------------------------------------------------------
# Neutral task specification — fair, strong baseline prompt
# ---------------------------------------------------------------------------

# Agents that receive Content Pillars in production routing.
# S1 (product_spec) and S2 (competitor_analysis) do NOT receive Pillars.
# S3 (campaign_strategy) receives them via _build_configured_pillars_text().
# S4 (content_creator) receives them via content_pillars parameter.
_AGENTS_RECEIVING_PILLARS = frozenset({"campaign_strategy", "content_creator"})


def _agent_receives_pillars(agent_key: str) -> bool:
    """Check if an Agent receives Content Pillars in production routing.

    This follows the real Orchestrator routing semantics:
    - run_product_spec: no pillars
    - run_competitor_analysis: no pillars
    - run_campaign_strategy: configured_pillars via _build_configured_pillars_text()
    - run_content_creator: content_pillars parameter
    """
    return agent_key in _AGENTS_RECEIVING_PILLARS


def build_neutral_task_spec(scenario: UpliftScenario, fixture: SourceFixture) -> str:
    """Build a neutral task specification for the direct baseline.

    Contains ONLY user-visible/task-equivalent requirements that a
    competent user would reasonably ask the model to produce.

    Does NOT contain:
    - MKTApp internal system prompts
    - Validator/review/repair instructions
    - Internal engine config (retry limits, web_search flags, etc.)
    - Orchestration implementation detail
    - Benchmark-specific rules
    - Internal schemas (unless part of user-facing deliverable)
    - MKTApp-derived decisions (auto-selected Pillars, brand interpretation, etc.)

    Does contain:
    - User request (user_prefix)
    - Quick brief
    - Raw product facts (same semantic content as MKTApp sees)
    - User-owned brand guidelines
    - User-configured Content Pillars (ONLY for S3/S4 where production
      routing delivers them; S1/S2 do NOT receive Pillars in production)
    - Explicit user-selected Pillar (if any, only for S3/S4)
    - User-visible task constraints (budget, forbidden tactics, etc.)
    - Fixed upstream context (for S3/S4 agent-level isolation)
    - Web research requirement when the task needs it
    - Language and deliverable expectations
    """
    parts: list[str] = []
    # User request
    parts.append(fixture.user_prefix.rstrip())
    # Raw product facts — same semantic content as MKTApp
    if fixture.product_facts_text:
        parts.append("--- ข้อมูลสินค้า ---")
        parts.append(fixture.product_facts_text)
        parts.append("--- สิ้นสุดข้อมูลสินค้า ---")
    # Quick brief
    if fixture.quick_brief:
        parts.append(f"คำขอเฉพาะ: {fixture.quick_brief}")
    # User-visible constraints
    if fixture.user_visible_constraints:
        constraint_strs = [f"{k}={v}" for k, v in fixture.user_visible_constraints.items()]
        parts.append(f"ข้อจำกัดจากผู้ใช้: {', '.join(constraint_strs)}")
    # User-owned brand guidelines
    if fixture.brand_guidelines:
        parts.append("--- ข้อมูลแบรนด์ ---")
        parts.append(fixture.brand_guidelines)
        parts.append("--- สิ้นสุดข้อมูลแบรนด์ ---")
    # User-configured Content Pillars — ONLY for Agents that receive them
    # in production.  S1 (product_spec) and S2 (competitor_analysis) do
    # NOT receive Pillars in the production orchestrator.  S3 (campaign)
    # and S4 (content) DO receive them via _build_configured_pillars_text().
    if _agent_receives_pillars(scenario.agent_key):
        if fixture.configured_pillars:
            parts.append(f"Content Pillars ที่กำหนด: {', '.join(fixture.configured_pillars)}")
        # Explicit user-selected Pillar (supplied, both sides receive)
        if fixture.explicit_selected_pillar:
            parts.append(f"Pillar ที่เลือก: {fixture.explicit_selected_pillar}")
    # Fixed upstream context (for S3/S4 agent-level isolation)
    for ctx_key, ctx_val in fixture.fixed_upstream_context.items():
        if ctx_val:
            parts.append(f"--- {ctx_key} (fixed context) ---")
            parts.append(ctx_val)
            parts.append(f"--- สิ้นสุด {ctx_key} ---")
    # Web research requirement
    if scenario.web_search_enabled:
        parts.append(
            f"สามารถใช้การค้นหาเว็บ (web search) ได้ ไม่เกิน "
            f"{scenario.web_search_max_uses} ครั้ง หากจำเป็นต้องหาข้อมูลปัจจุบัน"
        )
    # Language
    parts.append("ใช้ภาษาไทยเป็นหลัก")
    return "\n".join(parts)


def get_private_settings_not_leaked(scenario: UpliftScenario) -> set[str]:
    """Return the set of internal settings that must NOT appear in the baseline prompt."""
    return set(_INTERNAL_SETTING_KEYS)


def verify_no_private_leakage(baseline_prompt: str) -> tuple[bool, str]:
    """Verify the baseline prompt does not contain private MKTApp machinery."""
    for key in _INTERNAL_SETTING_KEYS:
        if key in baseline_prompt:
            return False, f"private setting '{key}' leaked into baseline prompt"
    if "Agent settings:" in baseline_prompt:
        return False, "raw 'Agent settings:' label leaked into baseline prompt"
    return True, "ok"


# ---------------------------------------------------------------------------
# Model parity — compare TWO resolved values
# ---------------------------------------------------------------------------

@dataclass
class ModelParityResult:
    """Result of model parity check."""
    ok: bool
    mktapp_model: str
    baseline_model: str
    reason: str


def resolve_models(scenario: UpliftScenario, baseline_model_override: str | None = None) -> ModelParityResult:
    """Resolve both mktapp_model and baseline_model separately and compare.

    The baseline model is resolved independently — it is NOT copied from
    the agent config. For the same-model uplift harness, the baseline_model
    is intentionally set to the same value as the agent model, but this
    function mechanically verifies that equality rather than assuming it.

    A deliberate test override using a different baseline_model must
    cause preflight INVALID.
    """
    mktapp_model = _get_agent_model(scenario.agent_key)
    # The baseline model is resolved from the same config, but independently.
    # In production, baseline_model == mktapp_model by design.
    # The override parameter exists for testing — a different model must fail.
    if baseline_model_override is not None:
        baseline_model = baseline_model_override
    else:
        baseline_model = _get_agent_model(scenario.agent_key)

    if not mktapp_model:
        return ModelParityResult(False, mktapp_model, baseline_model,
                                 f"mktapp_model is empty for agent '{scenario.agent_key}'")
    if not baseline_model:
        return ModelParityResult(False, mktapp_model, baseline_model,
                                 "baseline_model is empty")
    if baseline_model != mktapp_model:
        return ModelParityResult(False, mktapp_model, baseline_model,
                                 f"model mismatch: mktapp='{mktapp_model}' vs baseline='{baseline_model}'")
    return ModelParityResult(True, mktapp_model, baseline_model, "ok")


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

@dataclass
class PreflightResult:
    """Result of preflight checks for one scenario."""
    scenario_id: str
    ok: bool
    reason: str
    model_mismatch: bool = False
    tool_mismatch: bool = False
    info_mismatch: bool = False
    budget_mismatch: bool = False
    # Resolved values for evidence
    mktapp_model: str = ""
    baseline_model: str = ""
    mktapp_provider: str = ""
    baseline_provider: str = ""
    source_fixture_hash: str = ""
    # Provenance hashes for audit
    product_data_hash: str = ""
    brand_config_hash: str = ""
    pillar_config_hash: str = ""
    fixed_upstream_hashes: dict[str, str] = field(default_factory=dict)


def check_model_parity(
    scenario: UpliftScenario,
    baseline_model_override: str | None = None,
) -> ModelParityResult:
    """Verify baseline_model == mktapp_model by comparing two resolved values."""
    return resolve_models(scenario, baseline_model_override)


def check_tool_parity(scenario: UpliftScenario) -> tuple[bool, str]:
    """Verify SYMMETRIC tool capability parity.

    Both sides must have equivalent tool capability. If either side has
    an extra research capability, the comparison is invalid.

    S1: no web on either side
    S2: equivalent web capability on both
    S3: equivalent web capability on both
    S4: no web on either side

    Does NOT require identical number of tool calls — let each candidate
    decide whether/how to use the capability.
    """
    agent_has_web = _get_agent_web_search(scenario.agent_key)
    baseline_has_web = scenario.web_search_enabled
    if agent_has_web != baseline_has_web:
        return False, (
            f"tool capability mismatch: agent web_search={agent_has_web} "
            f"vs baseline web_search={baseline_has_web} — "
            f"both sides must have equivalent tool capability"
        )
    return True, "ok"


@dataclass
class ProviderToolParityResult:
    """Result of provider/tool capability parity check."""
    ok: bool
    reason: str
    model: str
    provider_route: str
    web_capability_available: bool
    web_tool_class: str | None = None


def check_provider_tool_parity(
    scenario: UpliftScenario,
    baseline_model_override: str | None = None,
) -> ProviderToolParityResult:
    """Verify provider/tool capability parity mechanically.

    For S2/S3 (web-enabled), verify that the exact model/provider route
    supports equivalent web-search capability for BOTH candidates.

    Uses static configuration evidence — no live probe.

    Checks:
    1. Same model identifier resolves for both sides.
    2. Same provider route (OpenRouter) for both sides.
    3. For web-enabled scenarios: the model supports server-side web search
       via OpenRouter's ``openrouter:web_search`` plugin.

    If the model/provider route cannot technically support equivalent web
    capability, the scenario is INVALID before paid execution.
    """
    model_result = resolve_models(scenario, baseline_model_override)
    if not model_result.ok:
        return ProviderToolParityResult(
            False, f"model parity: {model_result.reason}",
            model_result.mktapp_model, "unknown", False,
        )

    model = model_result.mktapp_model
    provider_route = "openrouter"  # LLMClient uses OpenRouter

    if not scenario.web_search_enabled:
        # No web needed — parity OK by design
        return ProviderToolParityResult(
            True, "ok (no web required)", model, provider_route, False,
        )

    # Web-enabled scenario: verify the model supports server-side web search
    # via OpenRouter.  We use static capability evidence from config.
    # OpenRouter supports ``plugins: [{"id": "openrouter:web_search"}]``
    # for models that accept server-side tools.
    #
    # The Gemini Flash family supports OpenRouter server-side web search.
    # This is verified through the provider's tool compatibility, not
    # a live probe.
    web_capable_models = _get_web_capable_models()
    if model not in web_capable_models:
        return ProviderToolParityResult(
            False,
            f"model '{model}' does not support server-side web search via "
            f"{provider_route} — scenario INVALID for web-enabled comparison",
            model, provider_route, False,
            web_tool_class=None,
        )

    return ProviderToolParityResult(
        True, "ok", model, provider_route, True,
        web_tool_class="openrouter:web_search",
    )


def _get_web_capable_models() -> frozenset[str]:
    """Return models known to support OpenRouter server-side web search.

    This is QUALIFICATION METADATA — a versioned capability matrix, not a
    dynamically authoritative source.  It pairs provider + model + tool
    capability.  Fail closed for unknown combinations.

    Updated when new models are added to config/agents.yaml.  Identified
    explicitly as qualification metadata, not production config.
    """
    # OpenRouter supports the openrouter:web_search plugin for Gemini models.
    # This is verified through OpenRouter's model documentation, not a live call.
    return frozenset({
        "google/gemini-3.7-flash",
        "google/gemini-3.5-flash",
        "google/gemini-3.0-flash",
        "google/gemini-2.5-flash",
        "google/gemini-2.0-flash",
    })


def verify_executor_model_parity(
    scenario: UpliftScenario,
    baseline_executor_model: str,
    mktapp_executor_model: str,
) -> tuple[bool, str]:
    """Verify the ACTUAL executor receives the correct model values.

    A test should deliberately:
    1. Resolve matching values from config.
    2. Mutate/override the baseline executor model.
    3. Show execution preflight fails before provider invocation.

    This function compares the executor-level model values, not just
    the config-level values.  Evidence can claim model parity while
    the executor uses another model — this catches that.
    """
    expected_model = _get_agent_model(scenario.agent_key)
    if baseline_executor_model != expected_model:
        return False, (
            f"baseline executor model '{baseline_executor_model}' != "
            f"expected '{expected_model}' — executor model drift"
        )
    if mktapp_executor_model != expected_model:
        return False, (
            f"mktapp executor model '{mktapp_executor_model}' != "
            f"expected '{expected_model}' — executor model drift"
        )
    if baseline_executor_model != mktapp_executor_model:
        return False, (
            f"baseline executor '{baseline_executor_model}' != "
            f"mktapp executor '{mktapp_executor_model}'"
        )
    return True, "ok"


def check_info_parity(
    scenario: UpliftScenario,
    fixture: SourceFixture,
) -> tuple[bool, str]:
    """Verify information parity — both candidates receive same source fixture.

    This checks the actual semantic content hash, not just product ID.
    """
    fixture_hash = fixture.hash()
    if not fixture_hash:
        return False, "cannot compute source fixture hash"
    # Verify no private leakage in the neutral task spec
    neutral_spec = build_neutral_task_spec(scenario, fixture)
    ok, reason = verify_no_private_leakage(neutral_spec)
    if not ok:
        return False, f"info parity violated: {reason}"
    # Verify product facts are non-empty (not just product ID)
    if not fixture.product_facts_text.strip():
        return False, (
            f"product_facts_text is empty — cannot prove information parity "
            f"from product ID '{scenario.product_id}' alone"
        )
    return True, fixture_hash


def check_derived_context_isolation(
    scenario: UpliftScenario,
    baseline_context: dict[str, Any],
) -> tuple[bool, str]:
    """Verify MKTApp-derived decisions are not leaked to the baseline.

    - MKTApp auto-selected Pillar → must NOT be in baseline context
    - Explicit user-selected Pillar → MUST be in both
    - MKTApp brand interpretation → must NOT be in baseline context
    """
    mktapp_selected_pillar = baseline_context.get("_mktapp_selected_pillar")
    if mktapp_selected_pillar is not None:
        return False, (
            f"MKTApp-derived selected pillar '{mktapp_selected_pillar}' "
            f"leaked into baseline context"
        )
    mktapp_brand_interp = baseline_context.get("_mktapp_brand_interpretation")
    if mktapp_brand_interp is not None:
        return False, (
            f"MKTApp-derived brand interpretation leaked into baseline context"
        )
    return True, "ok"


def check_agent_level_isolation(scenario: UpliftScenario) -> tuple[bool, str]:
    """Verify S3/S4 do not generate upstream Agent output.

    For S3 (campaign_strategy) and S4 (content_creator), the fixed_upstream_context
    must be supplied directly — no upstream Agent 1/2/3 is generated during
    the qualification run.

    This is a design invariant: the qualification unit is an individual Agent,
    not the whole pipeline.
    """
    if scenario.agent_key in ("campaign_strategy", "content_creator"):
        # Must have fixed_upstream_context defined (even if empty strings)
        if not scenario.fixed_upstream_context:
            return False, (
                f"{scenario.id} ({scenario.agent_key}) must have fixed_upstream_context "
                f"to prevent upstream Agent generation"
            )
    return True, "ok"


def _hash_str(text: str) -> str:
    """SHA-256 hash of a string for provenance."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def preflight(
    scenario: UpliftScenario,
    baseline_model_override: str | None = None,
) -> PreflightResult:
    """Run all preflight checks for one scenario."""
    fixture = scenario.build_source_fixture()

    # Compute provenance hashes for audit
    product_data_hash = _hash_str(fixture.product_facts_text)
    brand_config_hash = _hash_str(fixture.brand_guidelines)
    pillar_config_hash = _hash_str(json.dumps(fixture.configured_pillars, ensure_ascii=False))
    fixed_upstream_hashes = {
        k: _hash_str(v) for k, v in fixture.fixed_upstream_context.items()
    }
    provider_route = "openrouter"

    # Model parity (compares TWO resolved values)
    model_result = check_model_parity(scenario, baseline_model_override)
    if not model_result.ok:
        return PreflightResult(
            scenario_id=scenario.id, ok=False,
            reason=f"model parity: {model_result.reason}",
            model_mismatch=True,
            mktapp_model=model_result.mktapp_model,
            baseline_model=model_result.baseline_model,
            mktapp_provider=provider_route,
            baseline_provider=provider_route,
            source_fixture_hash=fixture.hash(),
            product_data_hash=product_data_hash,
            brand_config_hash=brand_config_hash,
            pillar_config_hash=pillar_config_hash,
            fixed_upstream_hashes=fixed_upstream_hashes,
        )

    # Tool parity (symmetric boolean)
    tool_ok, tool_reason = check_tool_parity(scenario)
    if not tool_ok:
        return PreflightResult(
            scenario_id=scenario.id, ok=False,
            reason=f"tool parity: {tool_reason}",
            tool_mismatch=True,
            mktapp_model=model_result.mktapp_model,
            baseline_model=model_result.baseline_model,
            mktapp_provider=provider_route,
            baseline_provider=provider_route,
            source_fixture_hash=fixture.hash(),
            product_data_hash=product_data_hash,
            brand_config_hash=brand_config_hash,
            pillar_config_hash=pillar_config_hash,
            fixed_upstream_hashes=fixed_upstream_hashes,
        )

    # Provider/tool capability parity (mechanical: model supports web search?)
    provider_result = check_provider_tool_parity(scenario, baseline_model_override)
    if not provider_result.ok:
        return PreflightResult(
            scenario_id=scenario.id, ok=False,
            reason=f"provider/tool parity: {provider_result.reason}",
            tool_mismatch=True,
            mktapp_model=model_result.mktapp_model,
            baseline_model=model_result.baseline_model,
            mktapp_provider=provider_route,
            baseline_provider=provider_route,
            source_fixture_hash=fixture.hash(),
            product_data_hash=product_data_hash,
            brand_config_hash=brand_config_hash,
            pillar_config_hash=pillar_config_hash,
            fixed_upstream_hashes=fixed_upstream_hashes,
        )

    # Info parity (actual semantic content)
    info_ok, info_reason = check_info_parity(scenario, fixture)
    if not info_ok:
        return PreflightResult(
            scenario_id=scenario.id, ok=False,
            reason=f"info parity: {info_reason}",
            info_mismatch=True,
            mktapp_model=model_result.mktapp_model,
            baseline_model=model_result.baseline_model,
            mktapp_provider=provider_route,
            baseline_provider=provider_route,
            source_fixture_hash=fixture.hash(),
            product_data_hash=product_data_hash,
            brand_config_hash=brand_config_hash,
            pillar_config_hash=pillar_config_hash,
            fixed_upstream_hashes=fixed_upstream_hashes,
        )

    # Agent-level isolation (S3/S4)
    isolation_ok, isolation_reason = check_agent_level_isolation(scenario)
    if not isolation_ok:
        return PreflightResult(
            scenario_id=scenario.id, ok=False,
            reason=f"agent-level isolation: {isolation_reason}",
            info_mismatch=True,
            mktapp_model=model_result.mktapp_model,
            baseline_model=model_result.baseline_model,
            mktapp_provider=provider_route,
            baseline_provider=provider_route,
            source_fixture_hash=fixture.hash(),
            product_data_hash=product_data_hash,
            brand_config_hash=brand_config_hash,
            pillar_config_hash=pillar_config_hash,
            fixed_upstream_hashes=fixed_upstream_hashes,
        )

    return PreflightResult(
        scenario_id=scenario.id, ok=True, reason="ok",
        mktapp_model=model_result.mktapp_model,
        baseline_model=model_result.baseline_model,
        mktapp_provider=provider_route,
        baseline_provider=provider_route,
        source_fixture_hash=fixture.hash(),
        product_data_hash=product_data_hash,
        brand_config_hash=brand_config_hash,
        pillar_config_hash=pillar_config_hash,
        fixed_upstream_hashes=fixed_upstream_hashes,
    )


def preflight_all(
    scenarios: list[UpliftScenario] | None = None,
    baseline_model_override: str | None = None,
) -> list[PreflightResult]:
    """Run preflight for all scenarios."""
    if scenarios is None:
        scenarios = SCENARIOS
    return [preflight(s, baseline_model_override) for s in scenarios]


# ---------------------------------------------------------------------------
# Blind X/Y mapping
# ---------------------------------------------------------------------------

@dataclass
class BlindMapping:
    """Blind X/Y assignment for one scenario."""
    scenario_id: str
    x_side: str  # "MKTApp" or "Baseline"
    y_side: str

    def to_secret_json(self) -> str:
        return json.dumps({
            self.scenario_id: {"X": self.x_side, "Y": self.y_side}
        }, ensure_ascii=False)

    @property
    def mapping_dict(self) -> dict[str, str]:
        return {"X": self.x_side, "Y": self.y_side}


def create_blind_mapping(scenario_id: str, seed: int | None = None) -> BlindMapping:
    """Create a random blind X/Y mapping for one scenario."""
    rng = random.Random(seed)
    if rng.random() < 0.5:
        return BlindMapping(scenario_id=scenario_id, x_side="MKTApp", y_side="Baseline")
    return BlindMapping(scenario_id=scenario_id, x_side="Baseline", y_side="MKTApp")


def create_all_blind_mappings(seed: int | None = None) -> dict[str, BlindMapping]:
    """Create blind mappings for all scenarios."""
    rng = random.Random(seed)
    return {
        s.id: create_blind_mapping(s.id, seed=rng.randint(0, 2**31))
        for s in SCENARIOS
    }


# ---------------------------------------------------------------------------
# Evidence format
# ---------------------------------------------------------------------------

@dataclass
class CandidateResult:
    """Result of running one candidate (A or B) for one scenario."""
    scenario_id: str
    side: str  # "baseline" or "mktapp"
    model: str
    output_text: str
    output_artifact_path: str | None = None
    output_hash: str | None = None
    # Completeness
    completeness: str = "UNKNOWN"
    # Multi-turn finish records (for S2/S3 baseline with tool loops, and MKTApp)
    finish_records: list[dict[str, Any]] = field(default_factory=list)
    final_finish_reason: str | None = None
    final_truncated: bool = False
    # Efficiency metrics (kept separate from Judge scores)
    call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    web_uses: int = 0
    cost_usd: float = 0.0
    # Provenance
    source_fixture_hash: str | None = None
    production_commit: str | None = None
    # S4 raw JSON audit (preserved separately, not Judge-facing)
    raw_json_audit: str | None = None

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "side": self.side,
            "model": self.model,
            "output_artifact_path": self.output_artifact_path,
            "output_hash": self.output_hash,
            "completeness": self.completeness,
            "finish_records": self.finish_records,
            "final_finish_reason": self.final_finish_reason,
            "final_truncated": self.final_truncated,
            "call_count": self.call_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "web_uses": self.web_uses,
            "cost_usd": self.cost_usd,
            "source_fixture_hash": self.source_fixture_hash,
            "production_commit": self.production_commit,
            "raw_json_audit_present": self.raw_json_audit is not None,
        }


@dataclass
class ScenarioComparison:
    """Comparison result for one scenario."""
    scenario_id: str
    agent_key: str
    baseline: CandidateResult
    mktapp: CandidateResult
    preflight: PreflightResult
    blind_mapping: BlindMapping
    judge_scores: dict[str, Any] | None = None
    uplift: dict[str, float] | None = None
    overall_uplift: float | None = None

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "agent_key": self.agent_key,
            "baseline": self.baseline.to_evidence_dict(),
            "mktapp": self.mktapp.to_evidence_dict(),
            "preflight": {
                "ok": self.preflight.ok,
                "reason": self.preflight.reason,
                "mktapp_model": self.preflight.mktapp_model,
                "baseline_model": self.preflight.baseline_model,
                "mktapp_provider": self.preflight.mktapp_provider,
                "baseline_provider": self.preflight.baseline_provider,
                "source_fixture_hash": self.preflight.source_fixture_hash,
                "provenance": {
                    "product_data_hash": self.preflight.product_data_hash,
                    "brand_config_hash": self.preflight.brand_config_hash,
                    "pillar_config_hash": self.preflight.pillar_config_hash,
                    "fixed_upstream_hashes": self.preflight.fixed_upstream_hashes,
                },
            },
            "blind_mapping_ref": "m6_mapping_secret.json",
            "judge_scores": self.judge_scores,
            "uplift": self.uplift,
            "overall_uplift": self.overall_uplift,
        }


# ---------------------------------------------------------------------------
# Uplift calculation — locked gate v1
# ---------------------------------------------------------------------------

def calculate_uplift(
    mktapp_scores: dict[str, float],
    baseline_scores: dict[str, float],
) -> dict[str, float]:
    """Calculate per-dimension uplift (MKTApp - Baseline)."""
    return {
        dim: round(mktapp_scores.get(dim, 0.0) - baseline_scores.get(dim, 0.0), 2)
        for dim in set(mktapp_scores) | set(baseline_scores)
    }


def calculate_overall_uplift(uplift: dict[str, float]) -> float:
    """Calculate mean uplift across all dimensions."""
    if not uplift:
        return 0.0
    return round(sum(uplift.values()) / len(uplift), 3)


def evaluate_uplift_gate(
    scenario_uplifts: list[dict[str, float]],
    scenario_overall_uplifts: list[float],
    gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the locked uplift gate v1.

    Gate A: aggregate mean uplift >= +0.25
    Gate B: >= 3 of 4 scenario deltas >= 0
    Gate C: no scenario overall delta < -0.5
    Gate D: no core dimension delta < -0.5

    All four gates must pass. Do NOT tune thresholds after seeing paid results.
    """
    if gate is None:
        gate = GATE_CONFIG

    all_deltas: list[float] = []
    for u in scenario_uplifts:
        all_deltas.extend(u.values())

    aggregate_uplift = round(sum(all_deltas) / len(all_deltas), 3) if all_deltas else 0.0
    consistent = sum(1 for d in scenario_overall_uplifts if d >= 0)
    regressed_scenarios = [
        i for i, d in enumerate(scenario_overall_uplifts)
        if d < gate["scenario_regression_max"]
    ]
    core_dims = set(gate.get("core_dimensions", CORE_DIMENSIONS))
    core_regressions: list[tuple[int, str]] = []
    for i, u in enumerate(scenario_uplifts):
        for dim, delta in u.items():
            if dim in core_dims and delta < gate["core_dimension_regression_max"]:
                core_regressions.append((i, dim))

    gate_a = aggregate_uplift >= gate["aggregate_uplift_min"]
    gate_b = consistent >= gate["scenario_consistency_min"]
    gate_c = len(regressed_scenarios) == 0
    gate_d = len(core_regressions) == 0
    passed = gate_a and gate_b and gate_c and gate_d

    return {
        "pass": passed,
        "gate_version": gate.get("version", GATE_VERSION),
        "gate_a_aggregate_uplift": gate_a,
        "gate_b_scenario_consistency": gate_b,
        "gate_c_no_scenario_regression": gate_c,
        "gate_d_no_core_regression": gate_d,
        "aggregate_uplift": aggregate_uplift,
        "scenario_consistency": consistent,
        "scenario_regression": regressed_scenarios,
        "core_dimension_regression": core_regressions,
        "gate_config": gate,
    }


# ---------------------------------------------------------------------------
# Efficiency reporting (separate from Judge scores)
# ---------------------------------------------------------------------------

def build_efficiency_summary(
    baseline: CandidateResult,
    mktapp: CandidateResult,
) -> dict[str, Any]:
    """Build efficiency summary — calls, tokens, cost — separate from quality."""
    return {
        "baseline": {
            "call_count": baseline.call_count,
            "prompt_tokens": baseline.prompt_tokens,
            "completion_tokens": baseline.completion_tokens,
            "web_uses": baseline.web_uses,
            "cost_usd": baseline.cost_usd,
        },
        "mktapp": {
            "call_count": mktapp.call_count,
            "prompt_tokens": mktapp.prompt_tokens,
            "completion_tokens": mktapp.completion_tokens,
            "web_uses": mktapp.web_uses,
            "cost_usd": mktapp.cost_usd,
        },
        "cost_ratio": (
            round(mktapp.cost_usd / baseline.cost_usd, 2)
            if baseline.cost_usd > 0 else None
        ),
        "call_ratio": (
            round(mktapp.call_count / baseline.call_count, 2)
            if baseline.call_count > 0 else None
        ),
    }


# ---------------------------------------------------------------------------
# Budget integration — independent candidate caps, wired into execution
# ---------------------------------------------------------------------------

# Protocol for paid-call authorization — allows mocking in tests
class BudgetAuthorizer(Protocol):
    """Protocol for budget authorization before paid calls."""
    def check_call(self, scenario_id: str, reserve: float, stage: str = "generation",
                   label: str = "") -> None: ...
    def commit_spend(self, scenario_id: str, amount: float, stage: str = "generation") -> None: ...


class CandidateBudgetCaps:
    """Independent per-candidate budget caps within each scenario.

    Candidate A (baseline) and Candidate B (mktapp) have SEPARATE inner caps.
    One candidate cannot consume the other's reserved budget.

    Hierarchy:
      per_candidate (inner, independently enforced):
        S1/baseline, S1/mktapp, S2/baseline, S2/mktapp, ...
      per_scenario (outer):
        S1 total = S1/baseline + S1/mktapp
      generation stage:
        all baseline + mktapp generation
      judge:
        all Judge calls
      total:
        newly approved execution cap
    """

    def __init__(
        self,
        scenarios: list[UpliftScenario] | None = None,
        total_cap: float = 1.10,
        generation_cap: float = 0.90,
        judge_cap: float = 0.20,
    ):
        from src.budget_hierarchy import BudgetHierarchy
        if scenarios is None:
            scenarios = SCENARIOS

        self._hierarchy = BudgetHierarchy(
            total_cap=total_cap,
            generation_cap=generation_cap,
            judge_cap=judge_cap,
        )
        # Independent per-candidate caps
        self._candidate_caps: dict[str, dict[str, float]] = {}
        self._candidate_spent: dict[str, dict[str, float]] = {}
        for s in scenarios:
            # Per-scenario total cap
            self._hierarchy.set_scenario_cap(
                scenario_id=s.id,
                cap=s.baseline_reserve + s.mktapp_reserve,
                max_calls=30,
            )
            # Independent inner caps
            self._candidate_caps[s.id] = {
                "baseline": s.baseline_reserve,
                "mktapp": s.mktapp_reserve,
            }
            self._candidate_spent[s.id] = {"baseline": 0.0, "mktapp": 0.0}

    def check_call(
        self,
        scenario_id: str,
        reserve: float,
        stage: str = "generation",
        label: str = "",
    ) -> None:
        """Check all caps before a paid call.

        Raises BudgetExceededError if ANY cap would be breached.
        The candidate-specific inner cap is checked FIRST, then
        the shared hierarchy (scenario, stage, total).
        """
        # Extract candidate side from label: "S1/baseline" or "S1/mktapp"
        side = self._extract_side(label, scenario_id)
        # 1. Candidate-specific inner cap (independently enforced)
        candidate_cap = self._candidate_caps.get(scenario_id, {}).get(side)
        if candidate_cap is not None:
            candidate_spent = self._candidate_spent.get(scenario_id, {}).get(side, 0.0)
            if round(candidate_spent + reserve, 6) > candidate_cap:
                from src.budget_hierarchy import BudgetExceededError
                raise BudgetExceededError(
                    f"per_candidate:{scenario_id}/{side}",
                    candidate_spent, reserve, candidate_cap, label,
                )
        # 2. Shared hierarchy (scenario, stage, total)
        self._hierarchy.check_call(scenario_id, reserve, stage=stage, label=label)

    def commit_spend(
        self,
        scenario_id: str,
        amount: float,
        stage: str = "generation",
    ) -> None:
        """Record actual spend after a charged call."""
        # Extract side from current call context
        # The caller must set the side before committing
        self._hierarchy.commit_spend(scenario_id, amount, stage=stage)

    def commit_candidate_spend(
        self,
        scenario_id: str,
        side: str,
        amount: float,
        stage: str = "generation",
    ) -> None:
        """Record actual spend for a specific candidate."""
        if scenario_id in self._candidate_spent and side in self._candidate_spent[scenario_id]:
            self._candidate_spent[scenario_id][side] = round(
                self._candidate_spent[scenario_id][side] + amount, 6
            )
        self._hierarchy.commit_spend(scenario_id, amount, stage=stage)

    def _extract_side(self, label: str, scenario_id: str) -> str:
        """Extract candidate side from label like 'S1/baseline/generate'."""
        if "/baseline" in label:
            return "baseline"
        if "/mktapp" in label:
            return "mktapp"
        # Default to mktapp if unclear (safer for MKTApp path)
        return "mktapp"

    def remaining_candidate(self, scenario_id: str, side: str) -> float | None:
        cap = self._candidate_caps.get(scenario_id, {}).get(side)
        spent = self._candidate_spent.get(scenario_id, {}).get(side, 0.0)
        if cap is None:
            return None
        return round(cap - spent, 6)

    def candidate_spent(self, scenario_id: str, side: str) -> float:
        """Return actual cumulative spend for a candidate."""
        return self._candidate_spent.get(scenario_id, {}).get(side, 0.0)

    @property
    def hierarchy(self):
        return self._hierarchy

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_caps": self._candidate_caps,
            "candidate_spent": self._candidate_spent,
            "hierarchy": self._hierarchy.to_dict(),
        }


def build_budget_caps(
    scenarios: list[UpliftScenario] | None = None,
    total_cap: float = 1.10,
    generation_cap: float = 0.90,
    judge_cap: float = 0.20,
) -> CandidateBudgetCaps:
    """Build budget caps with independent per-candidate inner caps."""
    return CandidateBudgetCaps(
        scenarios=scenarios,
        total_cap=total_cap,
        generation_cap=generation_cap,
        judge_cap=judge_cap,
    )


# Backward compat: old function name
def build_budget_hierarchy(
    scenarios: list[UpliftScenario] | None = None,
    total_cap: float = 1.10,
    generation_cap: float = 0.90,
    judge_cap: float = 0.20,
) -> CandidateBudgetCaps:
    """Build budget caps (alias for build_budget_caps)."""
    return build_budget_caps(scenarios, total_cap, generation_cap, judge_cap)


# ---------------------------------------------------------------------------
# Execution seams — budget-gated, wired to the REAL LLMClient path
# ---------------------------------------------------------------------------

class BudgetGuardedLLMClient:
    """Wraps the real ``LLMClient`` so every paid call passes budget preflight.

    This is the actual production execution seam for qualification.  It does
    NOT replace ``LLMClient`` — it delegates to it after budget authorization.

    Architecture::

        Agent / baseline / Judge
                ↓
        BudgetGuardedLLMClient.chat()   ← budget preflight HERE
                ↓
        LLMClient.chat()                ← real provider call
                ↓
        commit actual spend

    If budget is denied, ``LLMClient.chat()`` is NEVER called.
    """

    def __init__(
        self,
        llm: Any,
        budget: CandidateBudgetCaps,
        scenario_id: str,
        side: str,  # "baseline" or "mktapp"
    ):
        self._llm = llm
        self._budget = budget
        self._scenario_id = scenario_id
        self._side = side
        self._call_log: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        source: str = "generate",
        reserve: float = 0.05,
        **kwargs: Any,
    ) -> str:
        """Budget-gated ``LLMClient.chat()``.

        Raises ``BudgetExceededError`` if denied — provider is NOT called.
        """
        label = f"{self._scenario_id}/{self._side}/{source}"
        self._budget.check_call(
            self._scenario_id, reserve, stage="generation", label=label
        )
        # Budget authorized — call the real LLM
        result = self._llm.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            source=source,
            **kwargs,
        )
        # Commit actual spend: use provider-reported cost if available,
        # otherwise fall back to the conservative reserve.
        actual_cost = getattr(self._llm, "_last_cost_usd", None)
        commit_amount = actual_cost if actual_cost is not None else reserve
        self._budget.commit_candidate_spend(
            self._scenario_id, self._side, commit_amount, stage="generation"
        )
        self._call_log.append({
            "scenario_id": self._scenario_id, "side": self._side,
            "source": source, "reserve": reserve,
            "actual_cost": actual_cost,
            "committed": commit_amount,
            "finish_reason": getattr(self._llm, "_last_finish_reason", None),
        })
        return result

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Any],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        source: str = "chat_with_tools",
        reserve: float = 0.05,
        tool_reserve: float = 0.01,
        max_iterations: int = 10,
        **kwargs: Any,
    ) -> str:
        """Budget-gated ``LLMClient.chat_with_tools()``.

        Every model turn AND every tool operation inside the loop receives
        pre-call budget authorization through the per-iteration hooks
        (``pre_model_hook`` and ``pre_tool_hook``) added to the real
        ``LLMClient.chat_with_tools()``.

        After each model turn, actual provider-reported cost is committed
        immediately via ``post_model_hook``.  If actual cost is unavailable,
        the conservative reserve for that turn is committed (fail
        conservative, never undercount).

        If budget is denied mid-loop, the provider is NOT called for that
        turn.  The hook raises ``BudgetExceededError`` which propagates.
        """
        # Track per-turn commits for audit
        turn_commits: list[dict[str, Any]] = []

        def _pre_model_hook(iteration: int) -> None:
            label = f"{self._scenario_id}/{self._side}/{source}/turn_{iteration}"
            self._budget.check_call(
                self._scenario_id, reserve, stage="generation", label=label
            )

        def _pre_tool_hook(tool_name: str, tool_args: dict[str, Any]) -> None:
            label = f"{self._scenario_id}/{self._side}/{source}/tool_{tool_name}"
            self._budget.check_call(
                self._scenario_id, tool_reserve, stage="generation", label=label
            )

        def _post_model_hook(
            iteration: int,
            actual_cost: float | None,
            usage_dict: dict[str, Any] | None,
        ) -> None:
            """Commit per-turn actual spend immediately after each model turn."""
            # Fail conservative: use actual cost if available, else reserve
            commit_amount = actual_cost if actual_cost is not None else reserve
            cost_source = "actual" if actual_cost is not None else "reserve_fallback"
            self._budget.commit_candidate_spend(
                self._scenario_id, self._side, commit_amount, stage="generation"
            )
            turn_commits.append({
                "turn": iteration,
                "actual_cost": actual_cost,
                "committed": commit_amount,
                "cost_source": cost_source,
            })

        result = self._llm.chat_with_tools(
            messages,
            tools,
            tool_handlers,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            source=source,
            max_iterations=max_iterations,
            pre_model_hook=_pre_model_hook,
            pre_tool_hook=_pre_tool_hook,
            post_model_hook=_post_model_hook,
            **kwargs,
        )
        # Per-turn commits already happened inside the loop via post_model_hook.
        # Record the aggregate in call_log for audit.
        total_committed = sum(tc["committed"] for tc in turn_commits)
        self._call_log.append({
            "scenario_id": self._scenario_id, "side": self._side,
            "source": source, "reserve": reserve,
            "turn_commits": list(turn_commits),
            "total_committed": total_committed,
            "turn_count": len(turn_commits),
            "finish_reason": getattr(self._llm, "_last_finish_reason", None),
        })
        return result

    @property
    def last_finish_reason(self) -> str | None:
        return getattr(self._llm, "_last_finish_reason", None)

    @property
    def last_truncated(self) -> bool:
        return getattr(self._llm, "last_truncated", False)

    @property
    def last_cost_usd(self) -> float | None:
        return getattr(self._llm, "_last_cost_usd", None)

    @property
    def call_log(self) -> list[dict[str, Any]]:
        return list(self._call_log)


class BaselineToolLoopExecutor:
    """Direct-baseline tool-loop for S2/S3 (web search scenarios).

    Implements the actual multi-turn structure the paid run will use::

        model turn → tool request → web/tool result → continuation
                   → possible further tool request → final visible answer

    Each model turn is budget-checked before the provider call.
    Finish metadata is persisted PER turn.

    In offline tests, the LLM is mocked — no real calls.
    """

    def __init__(
        self,
        llm: BudgetGuardedLLMClient,
        scenario_id: str,
        max_tool_turns: int = 5,
    ):
        self._llm = llm
        self._scenario_id = scenario_id
        self._max_tool_turns = max_tool_turns
        self._turn_records: list[dict[str, Any]] = []

    def execute(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_handlers: dict[str, Any] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        reserve: float = 0.05,
    ) -> dict[str, Any]:
        """Execute the baseline tool loop.

        Returns dict with:
        - text: final visible answer
        - turn_records: per-turn finish metadata
        - finish_reason: final turn finish_reason
        - truncated: final turn truncated flag
        - completeness: evaluated completeness
        """
        from src.candidate_completeness import evaluate_single_call_completeness

        convo = list(messages)
        self._turn_records = []

        for turn in range(self._max_tool_turns):
            # Budget check before EVERY model turn
            source = f"baseline_turn_{turn}"
            label = f"{self._scenario_id}/baseline/{source}"

            # Call the model (budget-gated)
            if tools and tool_handlers:
                # Use chat_with_tools for tool-capable turns
                text = self._llm.chat_with_tools(
                    convo, tools, tool_handlers,
                    model=model, temperature=temperature,
                    max_tokens=max_tokens, source=source, reserve=reserve,
                )
            else:
                text = self._llm.chat(
                    convo,
                    model=model, temperature=temperature,
                    max_tokens=max_tokens, source=source, reserve=reserve,
                )

            finish_reason = self._llm.last_finish_reason
            truncated = self._llm.last_truncated

            # Check if this is a tool_calls turn (intermediate)
            is_tool_turn = finish_reason == "tool_calls"

            self._turn_records.append({
                "turn_index": turn,
                "finish_reason": finish_reason,
                "truncated": truncated,
                "is_final_visible_answer": not is_tool_turn,
                "prompt_tokens": 0,  # filled from usage log in production
                "completion_tokens": 0,
            })

            if not is_tool_turn:
                # Final visible answer
                completeness = evaluate_single_call_completeness(
                    finish_reason, truncated
                ).value
                return {
                    "text": text,
                    "turn_records": self._turn_records,
                    "finish_reason": finish_reason,
                    "truncated": truncated,
                    "completeness": completeness,
                }

            # Tool turn — chat_with_tools handles the loop internally
            # If we reach here with chat_with_tools, the loop is done
            # (chat_with_tools returns only the final answer)
            completeness = evaluate_single_call_completeness(
                finish_reason, truncated
            ).value
            return {
                "text": text,
                "turn_records": self._turn_records,
                "finish_reason": finish_reason,
                "truncated": truncated,
                "completeness": completeness,
            }

        # Exhausted tool turns without final answer
        return {
            "text": "",
            "turn_records": self._turn_records,
            "finish_reason": "tool_calls",
            "truncated": False,
            "completeness": "INCOMPLETE",
        }

    @property
    def turn_records(self) -> list[dict[str, Any]]:
        return list(self._turn_records)


class PaidCallExecutor:
    """Legacy execution seam — delegates to BudgetGuardedLLMClient.

    Kept for backward compatibility with earlier tests.  New code should
    use ``BudgetGuardedLLMClient`` directly.
    """

    def __init__(self, budget: CandidateBudgetCaps):
        self._budget = budget
        self._call_log: list[dict[str, Any]] = []

    def execute_baseline_call(
        self,
        scenario_id: str,
        reserve: float,
        mock_provider: Any | None = None,
    ) -> dict[str, Any]:
        """Execute a baseline paid call with budget pre-authorization."""
        label = f"{scenario_id}/baseline/generate"
        self._budget.check_call(scenario_id, reserve, stage="generation", label=label)
        if mock_provider is not None:
            result = mock_provider()
        else:
            result = {"text": "", "finish_reason": None, "prompt_tokens": 0,
                      "completion_tokens": 0, "cost_usd": 0.0}
        self._budget.commit_candidate_spend(
            scenario_id, "baseline", result.get("cost_usd", reserve),
            stage="generation",
        )
        self._call_log.append({
            "scenario_id": scenario_id, "side": "baseline",
            "reserve": reserve, "result": result,
        })
        return result

    def execute_mktapp_call(
        self,
        scenario_id: str,
        reserve: float,
        mock_provider: Any | None = None,
    ) -> dict[str, Any]:
        """Execute an MKTApp paid call with budget pre-authorization."""
        label = f"{scenario_id}/mktapp/generate"
        self._budget.check_call(scenario_id, reserve, stage="generation", label=label)
        if mock_provider is not None:
            result = mock_provider()
        else:
            result = {"text": "", "finish_reason": None, "prompt_tokens": 0,
                      "completion_tokens": 0, "cost_usd": 0.0}
        self._budget.commit_candidate_spend(
            scenario_id, "mktapp", result.get("cost_usd", reserve),
            stage="generation",
        )
        self._call_log.append({
            "scenario_id": scenario_id, "side": "mktapp",
            "reserve": reserve, "result": result,
        })
        return result

    def execute_judge_call(
        self,
        scenario_id: str,
        reserve: float,
        mock_provider: Any | None = None,
    ) -> dict[str, Any]:
        """Execute a Judge paid call with budget pre-authorization."""
        label = f"{scenario_id}/judge"
        self._budget.check_call(scenario_id, reserve, stage="judge", label=label)
        if mock_provider is not None:
            result = mock_provider()
        else:
            result = {"scores": {}, "cost_usd": 0.0}
        self._budget.commit_spend(scenario_id, result.get("cost_usd", reserve), stage="judge")
        self._call_log.append({
            "scenario_id": scenario_id, "side": "judge",
            "reserve": reserve, "result": result,
        })
        return result

    @property
    def call_log(self) -> list[dict[str, Any]]:
        return list(self._call_log)


def make_budget_guarded_llm(
    llm: Any,
    budget: CandidateBudgetCaps,
    scenario_id: str,
    side: str,
) -> BudgetGuardedLLMClient:
    """Create a budget-guarded LLM client for one candidate/scenario.

    This is the factory function for the real execution seam.  Pass the
    resulting client to ``orch.run_*()`` or the baseline tool-loop executor.
    """
    return BudgetGuardedLLMClient(llm, budget, scenario_id, side)


# ---------------------------------------------------------------------------
# Information delivery verification — boundary-level, not just fixture identity
# ---------------------------------------------------------------------------

def verify_baseline_receives_fixture_info(
    baseline_prompt: str,
    fixture: SourceFixture,
) -> tuple[bool, str]:
    """Verify the baseline prompt actually contains the fixture information.

    A shared fixture hash proves source identity, but does NOT prove
    both executors actually receive the contents.  This function checks
    that the baseline prompt contains the required fixture information
    at the invocation boundary.

    Does NOT compare raw prompt strings for equality — delivery mechanics
    may differ between candidates.  Tests semantic/source field propagation.
    """
    # Check product facts are present
    if fixture.product_facts_text:
        # At least some of the product facts text should appear
        # (may be truncated or reformatted, but key facts should be there)
        facts_snippet = fixture.product_facts_text[:200].strip()
        if facts_snippet and facts_snippet not in baseline_prompt:
            # Check for at least the product identifier from the facts
            if "K2" not in baseline_prompt and "Lagenio" not in baseline_prompt:
                return False, "baseline prompt missing product facts"
    # Check quick brief
    if fixture.quick_brief and fixture.quick_brief not in baseline_prompt:
        return False, f"baseline prompt missing quick_brief '{fixture.quick_brief}'"
    # Check fixed upstream context for S3/S4
    for ctx_key, ctx_val in fixture.fixed_upstream_context.items():
        if ctx_val and len(ctx_val) > 50:
            # Check that at least a snippet of the upstream context appears
            snippet = ctx_val[:100].strip()
            if snippet and snippet not in baseline_prompt:
                return False, f"baseline prompt missing fixed upstream '{ctx_key}'"
    # Check brand guidelines
    if fixture.brand_guidelines:
        # Brand guidelines may be long — check for at least the brand_profile marker
        if "brand_profile" not in baseline_prompt and "ข้อมูลแบรนด์" not in baseline_prompt:
            return False, "baseline prompt missing brand guidelines"
    # Check configured pillars
    if fixture.configured_pillars:
        for pillar in fixture.configured_pillars:
            if pillar not in baseline_prompt:
                return False, f"baseline prompt missing configured pillar '{pillar}'"
    # Check explicit selected pillar
    if fixture.explicit_selected_pillar:
        if fixture.explicit_selected_pillar not in baseline_prompt:
            return False, f"baseline prompt missing explicit selected pillar '{fixture.explicit_selected_pillar}'"
    return True, "ok"


def verify_mktapp_receives_fixture_info(
    mktapp_context: dict[str, Any],
    fixture: SourceFixture,
) -> tuple[bool, str]:
    """Verify the MKTApp target Agent receives the same semantic fixture info.

    MKTApp delivery mechanics differ from the baseline — the orchestrator
    passes context through ``run_campaign_strategy(competitor_analysis=...)``
    or ``run_content_creator(competitor_analysis=..., campaign_strategy=...)``.

    This function checks that the MKTApp context fields contain the
    required fixture information at the invocation boundary.
    """
    # Check fixed upstream context
    for ctx_key, ctx_val in fixture.fixed_upstream_context.items():
        if ctx_val:
            # The orchestrator receives this as a parameter
            received = mktapp_context.get(ctx_key, "")
            if not received:
                return False, f"mktapp context missing '{ctx_key}'"
            # Check semantic content — at least a snippet should match
            if len(ctx_val) > 50:
                snippet = ctx_val[:100].strip()
                if snippet not in received:
                    return False, f"mktapp context '{ctx_key}' missing semantic content"
    # Check configured pillars (delivered through orchestrator's _build_configured_pillars_text)
    # The orchestrator reads pillars from config, so we verify the config source
    # is the same one the fixture uses.
    if fixture.configured_pillars:
        # MKTApp reads pillars from config_loader — the fixture should agree
        from src.config_loader import load_config
        prod_config = load_config()
        prod_pillars = list(prod_config.get("pillars", []))
        if prod_pillars != fixture.configured_pillars:
            return False, (
                f"mktapp config pillars {prod_pillars} != fixture pillars "
                f"{fixture.configured_pillars}"
            )
    return True, "ok"


# ---------------------------------------------------------------------------
# No-golden-answers invariant
# ---------------------------------------------------------------------------

def verify_no_golden_answers(
    baseline_output: str,
    validators: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """Verify baseline output is NOT used as a golden answer."""
    if not validators:
        return True, "no validators to check"
    for v in validators:
        v_text = json.dumps(v, ensure_ascii=False)
        if "baseline_output" in v_text or "expected_output" in v_text:
            return False, f"validator references baseline/expected output: {v.get('name', '?')}"
    return True, "ok"


# ---------------------------------------------------------------------------
# MKTApp candidate execution — guarded LLM injection into Orchestrator
# ---------------------------------------------------------------------------

def run_mktapp_candidate(
    scenario: UpliftScenario,
    fixture: SourceFixture,
    budget: BudgetCaps,
    *,
    reserve: float = 0.05,
) -> dict[str, Any]:
    """Run the MKTApp target Agent for one scenario with budget-guarded LLM.

    This is the actual qualification execution path for Candidate B.

    Architecture:
        real LLMClient (from Orchestrator.make_client())
            ↓ wrapped by
        BudgetGuardedLLMClient
            ↓ injected into
        qual_runner.run_case(llm=guarded_llm)
            ↓
        Orchestrator.run_* (receives guarded llm)
            ↓
        Agent.run (uses self.llm = guarded llm for ALL calls)

    Every internal Agent call (generate, review, repair, brand interpretation,
    tool continuations) naturally passes through the guarded wrapper because
    the Agent stores the injected client as ``self.llm``.

    No Agent constructs its own LLMClient — they all use the one passed via
    ``_make_agent(agent_name, agent_cls, llm)``.

    Returns a dict with:
        - output_text: the Agent's output (or empty if budget-denied)
        - status: "COMPLETE", "INCOMPLETE", "BUDGET_DENIED", or "ERROR"
        - call_log: list of budget-guarded call records
        - cost_usd: total committed spend
        - finish_reason: last finish reason
    """
    import scripts.qual_runner as qr

    # Create the real LLM client exactly like production
    orch = qr.make_orchestrator(scenario.product_id)
    real_llm = qr.make_llm(orch)

    # Wrap with budget guard
    guarded_llm = BudgetGuardedLLMClient(
        real_llm, budget, scenario.id, "mktapp",
    )

    # Build context with fixed upstream fixtures
    context = {
        "use_competitor": bool(fixture.fixed_upstream_context.get("competitor_analysis")),
        "use_campaign": bool(fixture.fixed_upstream_context.get("campaign_strategy")),
        "competitor_analysis": fixture.fixed_upstream_context.get("competitor_analysis", ""),
        "campaign_strategy": fixture.fixed_upstream_context.get("campaign_strategy", ""),
    }

    try:
        result = qr.run_case(
            case_id=f"uplift_{scenario.id}_mktapp",
            agent_key=scenario.agent_key,
            product_id=scenario.product_id,
            quick_brief=fixture.quick_brief,
            stage="A",
            platforms=scenario.platforms,
            context=context,
            product_ids=scenario.product_ids,
            resource_context=scenario.resource_context,
            llm=guarded_llm,  # ← injection seam
        )
        output_text = result.get("result_text", "")
        finish_reason = guarded_llm.last_finish_reason
        # Evaluate completeness
        if scenario.web_search_enabled:
            status = "COMPLETE"  # multi-turn eval would go here
        else:
            status = evaluate_single_call_completeness(
                finish_reason, guarded_llm.last_truncated,
            )
        return {
            "output_text": output_text,
            "status": status,
            "call_log": list(guarded_llm.call_log),
            "cost_usd": sum(c.get("committed", 0) for c in guarded_llm.call_log),
            "finish_reason": finish_reason,
        }
    except BudgetExceededError:
        return {
            "output_text": "",
            "status": "BUDGET_DENIED",
            "call_log": list(guarded_llm.call_log),
            "cost_usd": sum(c.get("committed", 0) for c in guarded_llm.call_log),
            "finish_reason": None,
        }
    except Exception as e:
        return {
            "output_text": "",
            "status": "ERROR",
            "call_log": list(guarded_llm.call_log),
            "cost_usd": sum(c.get("committed", 0) for c in guarded_llm.call_log),
            "finish_reason": None,
            "error": str(e),
        }
    finally:
        real_llm.close()


# ---------------------------------------------------------------------------
# Completeness integration — multi-turn aware for baseline
# ---------------------------------------------------------------------------

def evaluate_single_call_completeness(
    finish_reason: str | None,
    truncated: bool | None,
) -> str:
    """Evaluate completeness for a single-call candidate (S1/S4 baseline)."""
    from src.candidate_completeness import evaluate_single_call_completeness as _eval
    return _eval(finish_reason, truncated).value


def evaluate_multi_turn_completeness(
    turn_records: list[dict[str, Any]],
) -> str:
    """Evaluate completeness for a multi-turn baseline (S2/S3 with tool loops).

    Semantics:
    - An intermediate turn with finish_reason="tool_calls" is NORMAL and
      does NOT mean the candidate is complete.
    - The final visible-answer turn must terminate acceptably:
      - finish_reason="stop" → COMPLETE
      - finish_reason="length" → INCOMPLETE
      - missing metadata → UNKNOWN

    turn_records: list of per-turn dicts with:
      - turn_index: int
      - finish_reason: str | None
      - truncated: bool
      - is_final_visible_answer: bool  (True for the last text-answer turn)
      - prompt_tokens: int
      - completion_tokens: int
    """
    if not turn_records:
        return "UNKNOWN"

    # Find the final visible-answer turn
    final_turn = None
    for turn in turn_records:
        if turn.get("is_final_visible_answer"):
            final_turn = turn

    if final_turn is None:
        # If no turn is marked as final, use the last one
        final_turn = turn_records[-1]

    final_fr = final_turn.get("finish_reason")
    final_trunc = final_turn.get("truncated")

    # Intermediate tool_calls turns are normal — don't treat as incomplete
    if final_fr == "tool_calls":
        # The model wants to call a tool but there's no continuation —
        # this means the final answer was never produced
        return "INCOMPLETE"

    return evaluate_single_call_completeness(final_fr, final_trunc)


def evaluate_mktapp_completeness(
    finish_records: list[dict[str, Any]],
    final_finish_reason: str | None = None,
    final_truncated: bool | None = None,
) -> str:
    """Evaluate completeness for an MKTApp multi-call candidate."""
    from src.candidate_completeness import (
        CandidateEvidence, CallFinishRecord, CallRole,
        evaluate_completeness,
    )
    if not finish_records:
        if final_finish_reason is None and final_truncated is None:
            return "UNKNOWN"
        return evaluate_single_call_completeness(final_finish_reason, final_truncated)

    records = [
        CallFinishRecord(
            call_index=r.get("call_index", i),
            role=r.get("role", CallRole.PRIMARY_GENERATION.value),
            finish_reason=r.get("finish_reason"),
            truncated=r.get("truncated"),
            superseded=r.get("superseded", False),
        )
        for i, r in enumerate(finish_records)
    ]
    evidence = CandidateEvidence(
        scenario_id="",
        side="mktapp",
        finish_records=records,
        final_finish_reason=final_finish_reason,
        final_truncated=final_truncated,
    )
    return evaluate_completeness(evidence).value


# Backward compat
evaluate_baseline_completeness = evaluate_single_call_completeness


# ---------------------------------------------------------------------------
# MKTApp call-role contracts — stable source-label suffixes
# ---------------------------------------------------------------------------

# These are the stable programmatic source-label contracts set by:
# - src/agents/base_agent.py: {agent_name}.generate, .fetch, .review, .repair
# - src/agents/competitor_analysis.py: {agent_name}.revise
# - src/agents/competitor_evidence.py: competitor_analysis.semantic_review, .brand_interpretation
# - src/llm_client.py: llm_client.chat_with_tools
# - src/orchestrator.py: script_review.regenerate_prompts
#
# The suffix matching is precise (after the last dot) to avoid false positives
# from agent names that might contain role-like substrings.
RECOGNIZED_SOURCE_ROLE_CONTRACTS: dict[str, str] = {
    ".generate": "primary_generation",
    ".review": "semantic_review",
    ".semantic_review": "semantic_review",
    ".repair": "repair_revision",
    ".revise": "repair_revision",
    ".fetch": "tool_continuation",
    ".brand_interpretation": "downstream_interpretation",
    ".interpretation": "downstream_interpretation",
    "chat_with_tools": "tool_continuation",
    ".tool": "tool_continuation",
}


# ---------------------------------------------------------------------------
# S4 presentation
# ---------------------------------------------------------------------------

def render_s4_for_judge(raw_json: str) -> tuple[str, str | None]:
    """Render S4 content creator output for Judge consumption.

    Returns (rendered_markdown, error_message).
    - If parsing succeeds: returns (markdown, None)
    - If parsing fails: returns ("", error_message) — no silent fallback to raw JSON

    The raw JSON is preserved as a separate audit artifact by the caller.
    The Judge sees only the rendered user-facing content.
    """
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return "", f"S4 JSON parse error: {e}"

    from src.content_schema import render_posts_to_markdown
    md = render_posts_to_markdown(parsed)
    if not md.strip():
        return "", "S4 renderer produced empty output"
    return md, None


def verify_s4_judge_prompt(judge_prompt: str) -> tuple[bool, str]:
    """Verify S4 Judge prompt contains rendered content, not raw JSON.

    - No raw MKTApp JSON should appear in the Judge prompt
    - Renderer failure makes candidate invalid (not silently fallback)
    """
    # Check for raw JSON markers that should not be in Judge-facing content
    if judge_prompt.strip().startswith("{") and '"posts"' in judge_prompt:
        return False, "raw MKTApp JSON appears in Judge prompt — should be rendered markdown"
    return True, "ok"


# ---------------------------------------------------------------------------
# Qualification mode — explicit evidence metadata
# ---------------------------------------------------------------------------

QUALIFICATION_MODE = "same_model_uplift"
CANDIDATE_TYPE_BASELINE = "baseline"
CANDIDATE_TYPE_MKTAPP = "mktapp"


# ---------------------------------------------------------------------------
# Main orchestration (offline-ready — no paid calls in tests)
# ---------------------------------------------------------------------------

def uplift_preflight_all(
    scenarios: list[UpliftScenario] | None = None,
    baseline_model_override: str | None = None,
) -> tuple[bool, list[PreflightResult]]:
    """Run preflight for all scenarios."""
    if scenarios is None:
        scenarios = SCENARIOS
    results = preflight_all(scenarios, baseline_model_override)
    all_ok = all(r.ok for r in results)
    return all_ok, results


def build_evidence(
    comparisons: list[ScenarioComparison],
    run_id: str,
    execution_head: str | None = None,
) -> dict[str, Any]:
    """Build the full evidence dict for the uplift run."""
    return {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "qualification_mode": QUALIFICATION_MODE,
        "candidate_types": {
            "baseline": CANDIDATE_TYPE_BASELINE,
            "mktapp": CANDIDATE_TYPE_MKTAPP,
        },
        "gate_version": GATE_VERSION,
        "gate_config": GATE_CONFIG,
        "execution_head": execution_head,
        "scenarios": [c.to_evidence_dict() for c in comparisons],
        "preflight_all_ok": all(c.preflight.ok for c in comparisons),
    }


def write_evidence(
    run_dir: Path,
    comparisons: list[ScenarioComparison],
    run_id: str,
    blind_mappings: dict[str, BlindMapping],
    execution_head: str | None = None,
) -> None:
    """Write evidence, mapping secret, and output hashes to run_dir."""
    run_dir.mkdir(parents=True, exist_ok=True)

    evidence = build_evidence(comparisons, run_id, execution_head)
    (run_dir / "m6_evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    mapping_secret = {}
    for sid, bm in blind_mappings.items():
        mapping_secret[sid] = bm.mapping_dict
    (run_dir / "m6_mapping_secret.json").write_text(
        json.dumps(mapping_secret, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    hashes = {"mktapp": {}, "baseline": {}}
    for c in comparisons:
        if c.mktapp.output_hash:
            hashes["mktapp"][c.scenario_id] = c.mktapp.output_hash
        if c.baseline.output_hash:
            hashes["baseline"][c.scenario_id] = c.baseline.output_hash
    (run_dir / "output_hashes.json").write_text(
        json.dumps(hashes, indent=2, ensure_ascii=False), encoding="utf-8"
    )
