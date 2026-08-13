"""Load agent configuration from YAML file."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load agent configuration from YAML.

    Falls back to ``config/agents.yaml`` relative to project root if no path given.
    """
    if config_path is None:
        project_root = Path(__file__).resolve().parent.parent
        config_path = project_root / "config" / "agents.yaml"

    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    defaults = config.get("defaults", {})
    agents: dict[str, Any] = {}

    for key, value in config.items():
        if key in ("defaults",):
            continue
        if not isinstance(value, dict):
            continue
        merged = {**defaults, **value}
        agents[key] = merged

    return {"defaults": defaults, "agents": agents}


def get_agent_config(config: dict[str, Any], agent_name: str) -> dict[str, Any]:
    """Return merged config for a single agent."""
    agents = config.get("agents", {})
    if agent_name not in agents:
        raise KeyError(f"Agent '{agent_name}' not found in config. Available: {list(agents.keys())}")
    return agents[agent_name]


def get_env(key: str, default: str | None = None) -> str | None:
    """Read environment variable, loading .env first if available."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    return os.environ.get(key, default)
