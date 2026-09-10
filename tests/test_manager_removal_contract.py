"""ARCH-CLEANUP-01 contract test: Manager agent/layer is absent from active
production code, while the four standalone agents and the auto-selection
helpers remain configured and callable.

This is an offline contract test — no LLM, network, or media calls.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Manager is absent from active production code
# ---------------------------------------------------------------------------

def test_manager_agent_module_deleted():
    """src/agents/manager.py must not exist."""
    manager_path = Path(__file__).resolve().parent.parent / "src" / "agents" / "manager.py"
    assert not manager_path.exists(), f"Manager agent module still exists: {manager_path}"


def test_manager_not_exported_from_agents():
    """src/agents/__init__.py must not export ManagerAgent."""
    from src import agents
    assert not hasattr(agents, "ManagerAgent"), "ManagerAgent is still exported from src.agents"
    assert "ManagerAgent" not in getattr(agents, "__all__", []), "ManagerAgent still in __all__"


def test_orchestrator_has_no_run_manager():
    """Orchestrator must not expose run_manager, llm_chat_raw, or a ManagerAgent import."""
    from src import orchestrator
    assert not hasattr(orchestrator.Orchestrator, "run_manager"), "Orchestrator.run_manager still exists"
    assert not hasattr(orchestrator.Orchestrator, "llm_chat_raw"), "Orchestrator.llm_chat_raw still exists"
    # The module must not import ManagerAgent.
    import inspect
    src = inspect.getsource(orchestrator)
    assert "ManagerAgent" not in src, "orchestrator.py still references ManagerAgent"
    assert "run_manager" not in src, "orchestrator.py still references run_manager"


def test_config_has_no_manager_section():
    """config/agents.yaml must not contain a `manager` section."""
    import yaml
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "agents.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    assert "manager" not in cfg, "agents.yaml still contains a `manager` section"


def test_main_has_no_interactive_manager_mode():
    """main.py must not define interactive_mode or call run_manager."""
    import inspect
    import main
    src = inspect.getsource(main)
    assert "interactive_mode" not in src, "main.py still defines interactive_mode"
    assert "run_manager" not in src, "main.py still references run_manager"
    assert "ManagerAgent" not in src, "main.py still references ManagerAgent"


# ---------------------------------------------------------------------------
# Four standalone agents still import and initialize normally
# ---------------------------------------------------------------------------

def test_four_standalone_agents_importable():
    """The four user-facing agents must still import."""
    from src.agents import (
        ProductSpecAgent,
        CompetitorAnalysisAgent,
        CampaignStrategyAgent,
        ContentCreatorAgent,
    )
    for cls in (ProductSpecAgent, CompetitorAnalysisAgent, CampaignStrategyAgent, ContentCreatorAgent):
        assert cls is not None


# ---------------------------------------------------------------------------
# Auto product/asset selection remains configured and callable
# ---------------------------------------------------------------------------

def test_auto_mode_has_selection_runtime_params():
    """auto_mode must carry the model/temperature/retry params that auto
    product/asset selection now reads (migrated from the removed manager section)."""
    import yaml
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "agents.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    auto = cfg.get("auto_mode", {})
    assert auto.get("model") == "google/gemini-3.8-flash"
    assert "temperature" in auto
    assert "max_retry_limit" in auto
    assert "max_iterations" in auto
    assert "max_tokens" in auto


def test_select_product_auto_and_select_assets_callable():
    """Orchestrator must still expose the auto-selection helpers with the
    same public signatures used by /api/run_auto."""
    from src.orchestrator import Orchestrator
    assert callable(getattr(Orchestrator, "select_product_auto", None)), \
        "Orchestrator.select_product_auto is missing"
    assert callable(getattr(Orchestrator, "_select_assets_for_content", None)), \
        "Orchestrator._select_assets_for_content is missing"


def test_no_hard_duration_clamp_introduced():
    """Sanity: orchestrator must not introduce a hard duration clamp as part
    of the cleanup. This is a negative guard against scope creep."""
    import inspect
    from src import orchestrator
    src = inspect.getsource(orchestrator)
    # The cleanup must not add new clamping logic.
    assert "max_video_duration" not in src, "Unexpected max_video_duration introduced"
