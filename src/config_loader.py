"""Load configuration from multiple YAML files, separated by concern.

Files loaded:
  config/agents.yaml          — agent definitions + prompts + auto_mode (domain)
  config/system.yaml          — timeouts, ports, display lengths (operational)
  config/content_policy.yaml  — content history, dedup, pillars (domain policy)
  config/web_search.yaml      — web search settings (operational)
  config/media.yaml           — media generation settings (operational)
  config/ingestion.yaml       — ingestion + product DB settings (operational)

All files are merged into a single dict. Callers use get_section() to access
non-agent sections — they don't need to know which file a section comes from.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# Sections ที่ไม่ใช่ agent config — เก็บแยก ไม่ merge กับ defaults
_NON_AGENT_SECTIONS = frozenset({
    "defaults", "system", "content_history", "auto_mode", "pillars",
    "ingestion", "product_db", "media_gen", "web_search",
})

# ไฟล์ config ที่โหลด (ยกเว้น agents.yaml ที่โหลดแยก)
# key = section name ใน merged dict, value = (filename, wrap_under_section)
#   wrap_under_section=True → ไฟล์มี flat keys, ห่อใต้ section name
#   wrap_under_section=False → ไฟล์มี sub-sections อยู่แล้ว, ใช้ได้ตรงๆ
_AUX_CONFIG_FILES = {
    "system":          ("system.yaml",          True),
    "content_history": ("content_policy.yaml",  False),  # มี sub-sections: content_history, pillars
    "pillars":         ("content_policy.yaml",  False),  # มาจากไฟล์เดียวกับ content_history
    "media_gen":       ("media.yaml",           True),
    "ingestion":       ("ingestion.yaml",       True),
    "web_search":      ("web_search.yaml",      True),
    "run_resources":   ("run_resources.yaml",   True),
}


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, return empty dict on error."""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load configuration from multiple YAML files and merge.

    Falls back to ``config/agents.yaml`` relative to project root if no path given.

    Returns dict with:
      - "defaults": default agent config
      - "agents": per-agent merged config
      - "system", "content_history", "auto_mode", "pillars", ...: non-agent sections
    """
    config_dir = _project_root() / "config"

    # 1. Load agents.yaml (agent definitions + defaults + auto_mode)
    if config_path is None:
        agents_path = config_dir / "agents.yaml"
    else:
        agents_path = Path(config_path)

    raw = _load_yaml(agents_path)
    defaults = raw.get("defaults", {})
    result: dict[str, Any] = {"defaults": defaults, "agents": {}}

    # 2. Load auxiliary config files
    for section_name, (filename, wrap) in _AUX_CONFIG_FILES.items():
        if section_name in result:
            continue  # ไม่ทับซ้อน (content_history + pillars มาจากไฟล์เดียวกัน)
        file_data = _load_yaml(config_dir / filename)
        if not file_data:
            continue
        if wrap:
            # ไฟล์มี flat keys → ห่อใต้ section name
            result[section_name] = file_data
        else:
            # ไฟล์มี sub-sections อยู่แล้ว → แตกลง top-level
            for sub_key, sub_value in file_data.items():
                if sub_key not in result:
                    result[sub_key] = sub_value

    # 3. Non-agent sections from agents.yaml (auto_mode, etc.) — pass through
    for section in _NON_AGENT_SECTIONS:
        if section == "defaults":
            continue
        if section in result:
            continue  # มาจาก aux file แล้ว
        if section in raw:
            result[section] = raw[section]

    # 4. Agent sections — merge with defaults
    for key, value in raw.items():
        if key in _NON_AGENT_SECTIONS:
            continue
        if not isinstance(value, dict):
            continue
        merged = {**defaults, **value}
        result["agents"][key] = merged

    return result


def get_agent_config(config: dict[str, Any], agent_name: str) -> dict[str, Any]:
    """Return merged config for a single agent."""
    agents = config.get("agents", {})
    if agent_name not in agents:
        raise KeyError(f"Agent '{agent_name}' not found in config. Available: {list(agents.keys())}")
    return agents[agent_name]


def get_section(config: dict[str, Any], section: str, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a non-agent config section, with optional defaults merged in.

    Example:
        ch = get_section(config, "content_history", {"max_entries": 200})
    """
    section_data = config.get(section, {})
    if defaults:
        return {**defaults, **section_data}
    return section_data if isinstance(section_data, dict) else {}


def get_env(key: str, default: str | None = None) -> str | None:
    """Read environment variable, loading .env first if available."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    return os.environ.get(key, default)
