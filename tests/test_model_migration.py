"""Offline tests for the Gemini 3.8 Flash production model migration.

Verifies that every intended production text/reasoning component resolves
to ``google/gemini-3.8-flash`` and that image/video/embedding models are
unchanged.  Historical fixtures and UAT/qualification harness tests are
intentionally not modified — they record older runs.
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_config, get_agent_config, get_section


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _agents_yaml() -> dict:
    """Load raw config/agents.yaml (not merged)."""
    with open(_project_root() / "config" / "agents.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _ingestion_yaml() -> dict:
    with open(_project_root() / "config" / "ingestion.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _assets_yaml() -> dict:
    with open(_project_root() / "config" / "assets.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _merged_config() -> dict:
    return load_config()


# ---------------------------------------------------------------------------
# Config-level model resolution (raw YAML)
# ---------------------------------------------------------------------------

def test_auto_mode_model_is_38():
    # The former `manager` agent config section was removed (ARCH-CLEANUP-01);
    # auto product/asset selection now reads model/temperature/retry from auto_mode.
    cfg = _agents_yaml()
    assert cfg.get("auto_mode", {}).get("model") == "google/gemini-3.8-flash"


def test_defaults_model_is_38():
    cfg = _agents_yaml()
    assert cfg.get("defaults", {}).get("model") == "google/gemini-3.8-flash"


def test_product_spec_model_is_38():
    cfg = _agents_yaml()
    assert cfg.get("product_spec", {}).get("model") == "google/gemini-3.8-flash"


def test_competitor_analysis_model_is_38():
    cfg = _agents_yaml()
    assert cfg.get("competitor_analysis", {}).get("model") == "google/gemini-3.8-flash"


def test_campaign_strategy_model_is_38():
    cfg = _agents_yaml()
    assert cfg.get("campaign_strategy", {}).get("model") == "google/gemini-3.8-flash"


def test_content_creator_model_is_38():
    cfg = _agents_yaml()
    assert cfg.get("content_creator", {}).get("model") == "google/gemini-3.8-flash"


def test_ingestion_model_is_38():
    cfg = _ingestion_yaml()
    assert cfg.get("model") == "google/gemini-3.8-flash"


def test_product_segmentation_model_is_38():
    cfg = _ingestion_yaml()
    assert cfg.get("product_segmentation", {}).get("model") == "google/gemini-3.8-flash"


def test_asset_tagging_model_is_38():
    cfg = _assets_yaml()
    assert cfg.get("tagging", {}).get("model") == "google/gemini-3.8-flash"


# ---------------------------------------------------------------------------
# Merged config resolution (via config_loader)
# ---------------------------------------------------------------------------

def test_merged_defaults_model_is_38():
    cfg = _merged_config()
    assert cfg.get("defaults", {}).get("model") == "google/gemini-3.8-flash"


def test_merged_product_spec_model_is_38():
    cfg = _merged_config()
    agent_cfg = get_agent_config(cfg, "product_spec")
    assert agent_cfg.get("model") == "google/gemini-3.8-flash"


def test_merged_competitor_analysis_model_is_38():
    cfg = _merged_config()
    agent_cfg = get_agent_config(cfg, "competitor_analysis")
    assert agent_cfg.get("model") == "google/gemini-3.8-flash"


def test_merged_campaign_strategy_model_is_38():
    cfg = _merged_config()
    agent_cfg = get_agent_config(cfg, "campaign_strategy")
    assert agent_cfg.get("model") == "google/gemini-3.8-flash"


def test_merged_content_creator_model_is_38():
    cfg = _merged_config()
    agent_cfg = get_agent_config(cfg, "content_creator")
    assert agent_cfg.get("model") == "google/gemini-3.8-flash"


def test_merged_ingestion_model_is_38():
    cfg = _merged_config()
    ing_cfg = get_section(cfg, "ingestion", {})
    assert ing_cfg.get("model") == "google/gemini-3.8-flash"


# ---------------------------------------------------------------------------
# Source-code fallbacks
# ---------------------------------------------------------------------------

def test_orchestrator_make_client_fallback_is_38():
    """If config/agents.yaml is missing defaults.model, the fallback must
    be gemini-3.8-flash, not an older model."""
    src = _project_root() / "src" / "orchestrator.py"
    content = src.read_text()
    assert 'defaults.get("model", "google/gemini-3.8-flash")' in content, (
        "orchestrator.py must have gemini-3.8-flash fallback in make_client"
    )
    assert "gemini-3.5-sonnet" not in content, (
        "orchestrator.py must not have old claude-3.5-sonnet fallback"
    )


def test_ingestion_fallback_is_38():
    """If config/ingestion.yaml is missing model, the fallback must be
    gemini-3.8-flash."""
    src = _project_root() / "src" / "ingestion.py"
    content = src.read_text()
    assert 'google/gemini-3.8-flash' in content, (
        "ingestion.py must have gemini-3.8-flash fallback"
    )
    assert "gemini-2.5-flash" not in content, (
        "ingestion.py must not have old gemini-2.5-flash fallback"
    )


def test_product_segmentation_fallback_is_38():
    """If both seg_cfg and ing_cfg are missing model, the fallback must be
    gemini-3.8-flash."""
    src = _project_root() / "src" / "product_segmentation.py"
    content = src.read_text()
    assert 'google/gemini-3.8-flash' in content, (
        "product_segmentation.py must have gemini-3.8-flash fallback"
    )
    assert "gemini-2.5-flash" not in content, (
        "product_segmentation.py must not have old gemini-2.5-flash fallback"
    )


def test_asset_library_fallback_is_38():
    """asset_library.py fallback must be gemini-3.8-flash."""
    src = _project_root() / "src" / "asset_library.py"
    content = src.read_text()
    assert 'google/gemini-3.8-flash' in content, (
        "asset_library.py must have gemini-3.8-flash fallback"
    )
    assert "gemini-3.7-flash" not in content, (
        "asset_library.py must not have old gemini-3.7-flash fallback"
    )


# ---------------------------------------------------------------------------
# No old Gemini text/reasoning fallback in production code
# ---------------------------------------------------------------------------

def test_no_gemini_35_flash_in_production_code():
    """No production source file may reference gemini-3.5-flash."""
    src_dir = _project_root() / "src"
    for py in src_dir.rglob("*.py"):
        content = py.read_text()
        assert "gemini-3.5-flash" not in content, (
            f"{py} still references gemini-3.5-flash"
        )


def test_no_gemini_37_flash_in_production_code():
    """No production source file may reference gemini-3.7-flash."""
    src_dir = _project_root() / "src"
    for py in src_dir.rglob("*.py"):
        content = py.read_text()
        assert "gemini-3.7-flash" not in content, (
            f"{py} still references gemini-3.7-flash"
        )


def test_no_gemini_35_flash_in_production_config():
    """No production config file may reference gemini-3.5-flash."""
    cfg_dir = _project_root() / "config"
    for f in cfg_dir.iterdir():
        if f.suffix in (".yaml", ".yml"):
            content = f.read_text()
            assert "gemini-3.5-flash" not in content, (
                f"{f} still references gemini-3.5-flash"
            )


def test_no_gemini_37_flash_in_production_config():
    """No production config file may reference gemini-3.7-flash."""
    cfg_dir = _project_root() / "config"
    for f in cfg_dir.iterdir():
        if f.suffix in (".yaml", ".yml"):
            content = f.read_text()
            assert "gemini-3.7-flash" not in content, (
                f"{f} still references gemini-3.7-flash"
            )


# ---------------------------------------------------------------------------
# Image/video/embedding models unchanged
# ---------------------------------------------------------------------------

def test_image_model_unchanged():
    """Image generation model must NOT be gemini-3.8-flash (it's a separate
    image model)."""
    from src import media_gen
    assert media_gen._FALLBACK_IMAGE_MODEL == "google/gemini-3.1-flash-image"
    assert "gemini-3.8-flash" not in media_gen._FALLBACK_IMAGE_MODEL


def test_video_model_unchanged():
    """Video generation model must be unchanged."""
    from src import media_gen
    assert media_gen._FALLBACK_VIDEO_MODEL == "bytedance/seedance-2.0-fast"


def test_embedding_model_unchanged():
    """Embedding model must be unchanged."""
    cfg = _assets_yaml()
    assert cfg.get("embedding", {}).get("model") == "openai/text-embedding-3-small"


# ---------------------------------------------------------------------------
# Structured-output / tool-calling / multimodal request shapes intact
# ---------------------------------------------------------------------------

def test_competitor_analysis_structured_output_shape_intact():
    """Competitor Analysis response_format must still be json_schema with
    the same schema structure."""
    cfg = _agents_yaml()
    rf = cfg.get("competitor_analysis", {}).get("response_format", {})
    assert rf["type"] == "json_schema"
    js = rf["json_schema"]
    assert js["name"] == "competitor_research"
    assert js["strict"] is True
    schema = js["schema"]
    assert "evidence" in schema["properties"]
    assert "competitor_names" in schema["properties"]


def test_tool_calling_config_intact():
    """Auto mode config must still have tool-calling selection prompt."""
    cfg = _agents_yaml()
    assert "auto_mode" in cfg
    assert "selection_prompt" in cfg["auto_mode"]


def test_web_search_config_intact():
    """Competitor Analysis and Campaign Strategy must still have web_search
    enabled."""
    comp_cfg = _agents_yaml().get("competitor_analysis", {})
    assert comp_cfg.get("web_search") is True
    camp_cfg = _agents_yaml().get("campaign_strategy", {})
    assert camp_cfg.get("web_search") is True


def test_multimodal_config_intact():
    """Content Creator must still support image/video prompts in config."""
    cc_cfg = _agents_yaml().get("content_creator", {})
    assert cc_cfg.get("output_format") == "json"
    # The system prompt must still mention image_prompts and video_prompts
    sp = cc_cfg.get("system_prompt", "")
    assert "image_prompts" in sp
    assert "video_prompts" in sp


# ---------------------------------------------------------------------------
# Usage logging records actual provider-returned model
# ---------------------------------------------------------------------------

def test_usage_log_records_actual_model():
    """LLMClient.chat must use the model parameter or _default_model and
    the usage log must record that model."""
    src = _project_root() / "src" / "llm_client.py"
    content = src.read_text()
    # The chat method must use `model or self._default_model`
    assert "used_model = model or self._default_model" in content
    # Accounting is delegated to the OpenRouter gate (single-gate architecture).
    # LLMClient must call the gate's accounting seam, not record_ai_usage directly.
    assert "_gate_account" in content


# ---------------------------------------------------------------------------
# Checkpoint A suite still passes (smoke check)
# ---------------------------------------------------------------------------

def test_checkpoint_a_imports_clean():
    """Verify that the Checkpoint A test modules can still be imported
    after the model migration."""
    import importlib
    importlib.import_module("tests.test_checkpoint_a_run_paths")
    importlib.import_module("tests.test_final_grounding_boundary")


# ---------------------------------------------------------------------------
# Media retry model (text/reasoning model for prompt rewriting)
# ---------------------------------------------------------------------------

def _media_yaml():
    with open(_project_root() / "config" / "media.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_media_retry_model_config_is_38():
    """config/media.yaml media_retry_model must resolve to gemini-3.8-flash."""
    cfg = _media_yaml()
    assert cfg.get("media_retry_model") == "google/gemini-3.8-flash"


def test_media_image_model_unchanged():
    """config/media.yaml image_model must remain unchanged."""
    cfg = _media_yaml()
    assert cfg.get("image_model") == "google/gemini-3.1-flash-image"


def test_media_video_model_unchanged():
    """config/media.yaml video_model must remain unchanged."""
    cfg = _media_yaml()
    assert cfg.get("video_model") == "alibaba/wan-2.7"


def test_image_retry_path_passes_38_to_rewrite_llm():
    """generate_image_with_retry must pass google/gemini-3.8-flash as the
    prompt-rewrite model to _rewrite_prompt_with_llm."""
    from unittest.mock import patch, MagicMock
    from src import media_gen
    from pathlib import Path as P

    # Force a content-policy rejection so the retry loop reaches the LLM
    # rewrite stage (attempt == max_retries).
    reject_result = {
        "ok": False,
        "error": "content_policy_violation",
        "error_type": "content_policy_violation",
    }
    ok_result = {"ok": True, "path": str(P("/tmp/x.png"))}

    captured_model = {}

    def fake_rewrite(orig, error, mtype, llm, model):
        captured_model["model"] = model
        return "rewritten prompt"

    fake_llm = MagicMock()

    with patch.object(media_gen, "_media_cfg", return_value={
        "media_retry_model": "google/gemini-3.8-flash",
        "max_content_policy_retries": 1,
        "content_policy_retry_delay_seconds": 0.0,
    }):
        with patch.object(media_gen, "_load_media_config", return_value={
            "media_retry_model": "google/gemini-3.8-flash",
            "max_content_policy_retries": 1,
            "content_policy_retry_delay_seconds": 0.0,
        }):
            with patch.object(media_gen, "generate_image", side_effect=[reject_result, ok_result]):
                with patch.object(media_gen, "_rewrite_prompt_with_llm", side_effect=fake_rewrite):
                    media_gen.generate_image_with_retry(
                        "Super Girl prompt", P("/tmp/out.png"),
                        llm=fake_llm,
                    )
    assert captured_model.get("model") == "google/gemini-3.8-flash", (
        f"Image retry path passed {captured_model.get('model')} "
        f"instead of google/gemini-3.8-flash"
    )


def test_video_retry_path_passes_38_to_rewrite_llm():
    """generate_video_with_retry must pass google/gemini-3.8-flash as the
    prompt-rewrite model to _rewrite_prompt_with_llm."""
    from unittest.mock import patch, MagicMock
    from src import media_gen
    from pathlib import Path as P

    reject_result = {
        "ok": False,
        "error": "content_policy_violation",
        "error_type": "content_policy_violation",
    }
    ok_result = {"ok": True, "path": str(P("/tmp/x.mp4"))}

    captured_model = {}

    def fake_rewrite(orig, error, mtype, llm, model):
        captured_model["model"] = model
        return "rewritten prompt"

    fake_llm = MagicMock()

    with patch.object(media_gen, "_media_cfg", return_value={
        "media_retry_model": "google/gemini-3.8-flash",
        "max_content_policy_retries": 1,
        "content_policy_retry_delay_seconds": 0.0,
    }):
        with patch.object(media_gen, "_load_media_config", return_value={
            "media_retry_model": "google/gemini-3.8-flash",
            "max_content_policy_retries": 1,
            "content_policy_retry_delay_seconds": 0.0,
        }):
            with patch.object(media_gen, "generate_video", side_effect=[reject_result, ok_result]):
                with patch.object(media_gen, "_rewrite_prompt_with_llm", side_effect=fake_rewrite):
                    media_gen.generate_video_with_retry(
                        "Super Girl prompt", P("/tmp/out.mp4"),
                        llm=fake_llm,
                    )
    assert captured_model.get("model") == "google/gemini-3.8-flash", (
        f"Video retry path passed {captured_model.get('model')} "
        f"instead of google/gemini-3.8-flash"
    )


def test_image_retry_fallback_38_when_config_missing():
    """When media config omits media_retry_model, the image retry path
    must still resolve the source fallback to google/gemini-3.8-flash."""
    from unittest.mock import patch, MagicMock
    from src import media_gen
    from pathlib import Path as P

    reject_result = {
        "ok": False,
        "error": "content_policy_violation",
        "error_type": "content_policy_violation",
    }
    ok_result = {"ok": True, "path": str(P("/tmp/x.png"))}

    captured_model = {}

    def fake_rewrite(orig, error, mtype, llm, model):
        captured_model["model"] = model
        return "rewritten prompt"

    fake_llm = MagicMock()

    # Config has max_retries=1 but NO media_retry_model key — forces fallback
    empty_cfg = {"max_content_policy_retries": 1, "content_policy_retry_delay_seconds": 0.0}
    with patch.object(media_gen, "_media_cfg", return_value=empty_cfg), \
         patch.object(media_gen, "_load_media_config", return_value=empty_cfg), \
         patch.object(media_gen, "generate_image", side_effect=[reject_result, ok_result]), \
         patch.object(media_gen, "_rewrite_prompt_with_llm", side_effect=fake_rewrite):
        media_gen.generate_image_with_retry(
            "Super Girl prompt", P("/tmp/out.png"),
            llm=fake_llm,
        )
    assert captured_model.get("model") == "google/gemini-3.8-flash", (
        f"Image retry fallback resolved to {captured_model.get('model')} "
        f"instead of google/gemini-3.8-flash"
    )


def test_video_retry_fallback_38_when_config_missing():
    """When media config omits media_retry_model, the video retry path
    must still resolve the source fallback to google/gemini-3.8-flash."""
    from unittest.mock import patch, MagicMock
    from src import media_gen
    from pathlib import Path as P

    reject_result = {
        "ok": False,
        "error": "content_policy_violation",
        "error_type": "content_policy_violation",
    }
    ok_result = {"ok": True, "path": str(P("/tmp/x.mp4"))}

    captured_model = {}

    def fake_rewrite(orig, error, mtype, llm, model):
        captured_model["model"] = model
        return "rewritten prompt"

    fake_llm = MagicMock()

    empty_cfg = {"max_content_policy_retries": 1, "content_policy_retry_delay_seconds": 0.0}
    with patch.object(media_gen, "_media_cfg", return_value=empty_cfg), \
         patch.object(media_gen, "_load_media_config", return_value=empty_cfg), \
         patch.object(media_gen, "generate_video", side_effect=[reject_result, ok_result]), \
         patch.object(media_gen, "_rewrite_prompt_with_llm", side_effect=fake_rewrite):
        media_gen.generate_video_with_retry(
            "Super Girl prompt", P("/tmp/out.mp4"),
            llm=fake_llm,
        )
    assert captured_model.get("model") == "google/gemini-3.8-flash", (
        f"Video retry fallback resolved to {captured_model.get('model')} "
        f"instead of google/gemini-3.8-flash"
    )


def test_no_claude_sonnet_4_in_media_gen_fallbacks():
    """No production source fallback in media_gen.py may reference
    anthropic/claude-sonnet-4 as a media retry model."""
    src = _project_root() / "src" / "media_gen.py"
    content = src.read_text()
    # The library-level LLMClient default is separate; media_gen must not
    # use claude-sonnet-4 as a media_retry_model fallback.
    assert 'media_retry_model", "anthropic/claude-sonnet-4"' not in content, (
        "media_gen.py still has anthropic/claude-sonnet-4 as media_retry_model fallback"
    )
