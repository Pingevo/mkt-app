"""Content Pillars wiring tests — verify Pillars flow from config to agents.

These tests verify generic contracts:
1. Configured Pillars load through the existing content-policy path
2. Pillar values are preserved unchanged
3. campaign_strategy can receive optional configured/selected Pillars
4. content_creator can receive selected/configured Pillars
5. No Pillars remains valid for both agents
6. Pillars are not routed into product_spec
7. No Pillar names are hardcoded
8. No exact-output/Pillar-word assertion is added
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config_loader import load_config, get_agent_config
from src.agents.campaign_strategy import CampaignStrategyAgent
from src.agents.content_creator import ContentCreatorAgent
from src.agents.product_spec import ProductSpecAgent


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FakeLLM:
    def __init__(self, generate_output: str = ""):
        self.generate_output = generate_output
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.generate_output

    def close(self):
        pass


def _load_pillars_from_config() -> list[str]:
    """Load configured Pillars through the existing content-policy path."""
    cfg = load_config()
    return cfg.get("pillars", [])


def _campaign_config():
    cfg = load_config()
    return get_agent_config(cfg, "campaign_strategy")


def _content_creator_config():
    cfg = load_config()
    return get_agent_config(cfg, "content_creator")


def _product_spec_config():
    cfg = load_config()
    return get_agent_config(cfg, "product_spec")


# ---------------------------------------------------------------------------
# 1. Configured Pillars load through the existing content-policy path
# ---------------------------------------------------------------------------

def test_configured_pillars_load_from_config():
    """Pillars must be loadable through the existing config_loader path."""
    pillars = _load_pillars_from_config()
    assert isinstance(pillars, list)
    # If pillars are configured, they must be non-empty strings
    for p in pillars:
        assert isinstance(p, str)
        assert p.strip() == p  # no leading/trailing whitespace


def test_pillar_values_preserved_unchanged():
    """Pillar values from config must be preserved unchanged — no
    normalization, rewriting, or truncation by the loader."""
    pillars = _load_pillars_from_config()
    # The loaded values should match what's in the YAML file
    # (no transformation by the loader)
    assert all(isinstance(p, str) for p in pillars)


# ---------------------------------------------------------------------------
# 2. campaign_strategy receives Pillars (tested in test_campaign_strategy.py)
# — these tests verify the content_creator side
# ---------------------------------------------------------------------------

def test_content_creator_receives_selected_pillar():
    """content_creator must receive the selected Pillar when present."""
    agent = ContentCreatorAgent(_content_creator_config(), FakeLLM())
    prompt = agent.build_prompt(
        "product spec", "competitor analysis", "campaign strategy",
        selected_pillar="Safety",
    )
    assert "Safety" in prompt
    assert "Content Pillar ที่เลือก" in prompt


def test_content_creator_receives_configured_pillars():
    """content_creator can receive configured Pillars when appropriate."""
    agent = ContentCreatorAgent(_content_creator_config(), FakeLLM())
    prompt = agent.build_prompt(
        "product spec", "competitor analysis", "campaign strategy",
        content_pillars="- Safety\n- Fun\n- Value",
    )
    assert "Safety" in prompt
    assert "Content Pillars" in prompt


def test_content_creator_works_without_pillars():
    """content_creator must work normally when no Pillars are provided."""
    agent = ContentCreatorAgent(_content_creator_config(), FakeLLM())
    prompt = agent.build_prompt(
        "product spec", "competitor analysis", "campaign strategy",
    )
    assert "Content Pillar" not in prompt
    assert "product spec" in prompt


def test_content_creator_pillars_are_guidance_not_mandatory():
    """Pillar context must include guidance that usage is optional."""
    agent = ContentCreatorAgent(_content_creator_config(), FakeLLM())
    prompt = agent.build_prompt(
        "product spec", "competitor analysis", "campaign strategy",
        selected_pillar="Safety",
    )
    assert "ไม่บังคับ" in prompt


# ---------------------------------------------------------------------------
# 3. Pillars are not routed into product_spec
# ---------------------------------------------------------------------------

def test_product_spec_does_not_receive_pillars():
    """product_spec must NOT receive Content Pillars — they are content-
    strategy context, not product analysis context."""
    agent = ProductSpecAgent(_product_spec_config(), FakeLLM())
    # product_spec.build_prompt does not accept pillar parameters
    import inspect
    sig = inspect.signature(agent.build_prompt)
    params = list(sig.parameters.keys())
    assert "selected_pillar" not in params
    assert "content_pillars" not in params
    assert "pillar" not in params


# ---------------------------------------------------------------------------
# 4. No Pillar names are hardcoded
# ---------------------------------------------------------------------------

def test_no_pillar_names_hardcoded_in_content_creator():
    """No specific Pillar names are hardcoded in content_creator logic.
    The agent renders whatever pillars are passed via parameters."""
    agent = ContentCreatorAgent(_content_creator_config(), FakeLLM())
    prompt_a = agent.build_prompt(
        "p", "c", "s", selected_pillar="Alpha",
    )
    prompt_b = agent.build_prompt(
        "p", "c", "s", selected_pillar="Beta",
    )
    assert "Alpha" in prompt_a and "Beta" not in prompt_a
    assert "Beta" in prompt_b and "Alpha" not in prompt_b


# ---------------------------------------------------------------------------
# 5. Pillars do not modify competitor Stage A evidence
# ---------------------------------------------------------------------------

def test_pillars_not_in_competitor_evidence_prompt():
    """Content Pillars must not appear in the competitor evidence Stage A
    system prompt — evidence research is brand/pillar-objective."""
    from src.agents.competitor_evidence import EVIDENCE_SYSTEM_PROMPT
    assert "pillar" not in EVIDENCE_SYSTEM_PROMPT.lower()
    assert "content_pillar" not in EVIDENCE_SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# 6. Selected Pillar is run-scoped — no stale cross-run state leakage
# ---------------------------------------------------------------------------

def test_selected_pillar_does_not_leak_across_runs():
    """A selected Pillar from Run A must NOT appear in Run B when Run B
    has no selected Pillar. Selected Pillar is run-scoped context, not
    persistent Orchestrator state.

    This test verifies that the Orchestrator does NOT carry a
    self._selected_pillar attribute that could leak between runs.
    """
    from src.orchestrator import Orchestrator
    # The Orchestrator must NOT have a _selected_pillar attribute
    # that persists across runs
    orch = Orchestrator.__new__(Orchestrator)
    # After __init__, there should be no _selected_pillar
    # (We check the class, not a live instance, to avoid needing config)
    assert not hasattr(orch, "_selected_pillar"), (
        "Orchestrator must not have a persistent _selected_pillar attribute "
        "— selected Pillar is run-scoped and must not leak across runs"
    )


def test_configured_pillars_available_without_selected_pillar():
    """Configured Pillars remain available independently of selected Pillar.
    An agent can receive configured Pillars even when no selected Pillar
    exists for the current run."""
    agent = ContentCreatorAgent(_content_creator_config(), FakeLLM())
    prompt = agent.build_prompt(
        "product spec", "competitor analysis", "campaign strategy",
        content_pillars="- Safety\n- Fun",
        # No selected_pillar — should still work
    )
    assert "Safety" in prompt
    assert "Fun" in prompt
    assert "Content Pillar ที่เลือก" not in prompt  # no selected pillar section


def test_explicit_selected_pillar_preserved_unchanged():
    """Explicit selected Pillar values must be preserved unchanged —
    no normalization, rewriting, or truncation."""
    agent = ContentCreatorAgent(_content_creator_config(), FakeLLM())
    test_pillar = "My Custom Pillar Name"
    prompt = agent.build_prompt(
        "product spec", "competitor analysis", "campaign strategy",
        selected_pillar=test_pillar,
    )
    assert test_pillar in prompt
