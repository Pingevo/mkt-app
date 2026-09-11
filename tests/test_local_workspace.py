"""Tests for local user-state isolation.

Verifies that:
1. Saving User Agent Instructions does NOT alter product default definitions.
2. Saving editable runtime settings does NOT mutate factory agents.yaml.
3. Fresh workspace (no local state) has no Product, Brand, instructions, pillars, etc.
4. After saving state locally, it persists and runtime reads it correctly.
5. Fresh-local reset removes user state and restores first-use semantics.
6. Product-owned system prompts/grounding/review remain intact.

No real model calls are made.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.local_workspace import (
    local_root,
    local_config_dir,
    reset_local_workspace,
    load_agent_instructions_local,
    save_agent_instructions_local,
    load_agent_instructions_product,
    load_agent_instructions_merged,
    load_agent_overrides,
    save_agent_overrides,
    load_content_pillars_local,
    save_content_pillars_local,
    load_media_overrides,
    save_media_overrides,
    _archive_directory,
    _archive_file,
    _archive_root,
    _sha256,
)


@pytest.fixture(autouse=True)
def clean_workspace():
    """Ensure clean local workspace before and after each test."""
    reset_local_workspace()
    yield
    reset_local_workspace()


# --- 1. Factory isolation: saving instructions does not alter product config ---

def test_product_agent_instructions_unchanged_after_user_save():
    """Saving user instructions must NOT alter config/agent_instructions.json."""
    product_path = PROJECT_ROOT / "config" / "agent_instructions.json"
    hash_before = product_path.read_bytes().__hash__()

    # Save user instructions
    save_agent_instructions_local({
        "product_spec": {"preset": "technical", "custom": "test instruction"}
    })

    hash_after = product_path.read_bytes().__hash__()
    assert hash_before == hash_after, "Product agent_instructions.json was mutated!"


def test_product_agents_yaml_unchanged_after_user_override():
    """Saving agent overrides must NOT alter config/agents.yaml."""
    product_path = PROJECT_ROOT / "config" / "agents.yaml"
    content_before = product_path.read_bytes()

    # Save user overrides
    save_agent_overrides({
        "product_spec": {"model": "gpt-4", "temperature": 0.1}
    })

    content_after = product_path.read_bytes()
    assert content_before == content_after, "Product agents.yaml was mutated!"


def test_product_content_policy_unchanged_after_pillars_save():
    """Saving pillars must NOT alter config/content_policy.yaml."""
    product_path = PROJECT_ROOT / "config" / "content_policy.yaml"
    content_before = product_path.read_bytes()

    save_content_pillars_local(["ทดสอบ"], {"ทดสอบ": ["คำ"]})

    content_after = product_path.read_bytes()
    assert content_before == content_after, "Product content_policy.yaml was mutated!"


def test_product_media_yaml_unchanged_after_media_save():
    """Saving media overrides must NOT alter config/media.yaml."""
    product_path = PROJECT_ROOT / "config" / "media.yaml"
    content_before = product_path.read_bytes()

    save_media_overrides({"image_model": "test-model"})

    content_after = product_path.read_bytes()
    assert content_before == content_after, "Product media.yaml was mutated!"


# --- 2. Fresh workspace: no user state exists ---

def test_fresh_workspace_no_agent_instructions():
    """Fresh workspace has no user agent instructions."""
    data = load_agent_instructions_local()
    assert data == {}, f"Expected empty, got {data}"


def test_fresh_workspace_no_agent_overrides():
    """Fresh workspace has no agent overrides."""
    data = load_agent_overrides()
    assert data == {}, f"Expected empty, got {data}"


def test_fresh_workspace_no_pillars():
    """Fresh workspace has no content pillars."""
    data = load_content_pillars_local()
    assert data["pillars"] == [], f"Expected empty pillars, got {data}"
    assert data["pillar_keywords"] == {}, f"Expected empty keywords, got {data}"


def test_fresh_workspace_no_media_overrides():
    """Fresh workspace has no media overrides."""
    data = load_media_overrides()
    assert data == {}, f"Expected empty, got {data}"


def test_fresh_workspace_merged_instructions_has_product_presets():
    """Merged instructions on fresh workspace has product presets but no per-agent sections."""
    merged = load_agent_instructions_merged()
    assert "_presets" in merged, "Product presets missing"
    assert "_defaults" in merged, "Product defaults missing"
    assert "product_spec" not in merged, "Per-agent section should not exist on fresh workspace"
    assert "content_creator" not in merged, "Per-agent section should not exist on fresh workspace"


def test_fresh_workspace_orchestrator_loads_empty_instructions():
    """Orchestrator's _load_agent_instructions returns {} on fresh workspace."""
    from src.orchestrator import Orchestrator
    # We can't construct a full Orchestrator (needs LLM client), but we can test the method
    # by creating a minimal mock
    class MockOrch:
        _load_agent_instructions = Orchestrator._load_agent_instructions
    mock = MockOrch()
    result = mock._load_agent_instructions("product_spec")
    assert result == {}, f"Expected empty instructions, got {result}"


# --- 3. Persistence: saved state survives and is read correctly ---

def test_agent_instructions_persist_and_read():
    """Saved agent instructions persist and are read correctly."""
    test_data = {
        "product_spec": {"preset": "technical", "custom": "test"},
        "content_creator": {"preset": "friendly", "custom": "logo test"},
    }
    save_agent_instructions_local(test_data)

    # Re-read
    loaded = load_agent_instructions_local()
    assert loaded["product_spec"]["custom"] == "test"
    assert loaded["content_creator"]["custom"] == "logo test"


def test_agent_overrides_persist_and_read():
    """Saved agent overrides persist and are read correctly."""
    save_agent_overrides({
        "product_spec": {"temperature": 0.5, "max_tokens": 2048}
    })
    loaded = load_agent_overrides()
    assert loaded["product_spec"]["temperature"] == 0.5
    assert loaded["product_spec"]["max_tokens"] == 2048


def test_pillars_persist_and_read():
    """Saved pillars persist and are read correctly."""
    save_content_pillars_local(["รีวิว", "เปรียบเทียบ"], {"รีวิว": ["รีวิว"]})
    loaded = load_content_pillars_local()
    assert loaded["pillars"] == ["รีวิว", "เปรียบเทียบ"]
    assert loaded["pillar_keywords"]["รีวิว"] == ["รีวิว"]


def test_media_overrides_persist_and_read():
    """Saved media overrides persist and are read correctly."""
    save_media_overrides({"image_model": "custom-model"})
    loaded = load_media_overrides()
    assert loaded["image_model"] == "custom-model"


# --- 4. Reset: removes user state ---

def test_reset_removes_all_user_state():
    """Reset removes all local user state."""
    # Create some state
    save_agent_instructions_local({"product_spec": {"custom": "test"}})
    save_agent_overrides({"product_spec": {"temperature": 0.1}})
    save_content_pillars_local(["test"], {"test": ["kw"]})
    save_media_overrides({"image_model": "test"})

    assert local_root().exists()

    reset_local_workspace()

    assert not local_root().exists(), "Local workspace still exists after reset"
    assert load_agent_instructions_local() == {}
    assert load_agent_overrides() == {}
    assert load_content_pillars_local()["pillars"] == []
    assert load_media_overrides() == {}


# --- 5. Product config integrity ---

def test_product_agents_yaml_has_system_prompts():
    """Product agents.yaml retains system prompts for all 4 agents."""
    import yaml
    with open(PROJECT_ROOT / "config" / "agents.yaml") as f:
        cfg = yaml.safe_load(f)
    for agent in ["product_spec", "competitor_analysis", "campaign_strategy", "content_creator"]:
        assert "system_prompt" in cfg[agent], f"{agent} missing system_prompt"
        assert "grounding_policy" in cfg[agent], f"{agent} missing grounding_policy"


def test_product_agent_instructions_has_presets():
    """Product agent_instructions.json retains _presets and _defaults."""
    product = load_agent_instructions_product()
    assert "_presets" in product
    assert "_defaults" in product
    for agent in ["product_spec", "competitor_analysis", "campaign_strategy", "content_creator"]:
        assert agent in product["_presets"], f"{agent} missing from _presets"
        assert agent in product["_defaults"], f"{agent} missing from _defaults"


def test_product_agent_instructions_no_per_agent_sections():
    """Product agent_instructions.json must NOT contain per-agent active sections."""
    product = load_agent_instructions_product()
    for agent in ["product_spec", "competitor_analysis", "campaign_strategy", "content_creator"]:
        assert agent not in product, f"{agent} found in product config — should be user-only"


def test_product_content_policy_no_pillars():
    """Product content_policy.yaml must NOT contain pillars or pillar_keywords."""
    import yaml
    with open(PROJECT_ROOT / "config" / "content_policy.yaml") as f:
        cfg = yaml.safe_load(f)
    assert "pillars" not in cfg, "pillars found in product config — should be user-only"
    assert "pillar_keywords" not in cfg, "pillar_keywords found in product config"


# --- 6. Config loader merges local overrides ---

def test_config_loader_merges_local_overrides():
    """config_loader.load_config merges local agent overrides over product defaults."""
    from src.config_loader import load_config, get_agent_config

    # Save an override
    save_agent_overrides({"product_spec": {"temperature": 0.123}})

    config = load_config()
    agent_cfg = get_agent_config(config, "product_spec")
    assert agent_cfg["temperature"] == 0.123, "Local override not merged"


def test_config_loader_merges_local_pillars():
    """config_loader.load_config merges local pillars."""
    from src.config_loader import load_config

    save_content_pillars_local(["ทดสอบ1", "ทดสอบ2"], {})

    config = load_config()
    assert config["pillars"] == ["ทดสอบ1", "ทดสอบ2"], "Local pillars not merged"


def test_config_loader_fresh_workspace_empty_pillars():
    """config_loader.load_config returns empty pillars on fresh workspace."""
    from src.config_loader import load_config

    config = load_config()
    assert config["pillars"] == [], "Fresh workspace should have empty pillars"


# --- 7. Product ingestion cache isolation ---

def test_reset_clears_product_cache():
    """Reset removes all user-derived product cache from cache/{product_id}/."""
    from src import product_db

    # Simulate a product cache record
    cache_dir = PROJECT_ROOT / "cache"
    test_product = cache_dir / "TestProduct123"
    test_product.mkdir(parents=True, exist_ok=True)
    (test_product / "product.json").write_text('{"product_id": "TestProduct123", "status": "ready"}', encoding="utf-8")

    assert test_product.exists()

    reset_local_workspace()

    assert not test_product.exists(), "Product cache directory still exists after reset"


def test_reset_preserves_media_capabilities_cache():
    """Reset must NOT remove cache/_media_capabilities/ (product infrastructure)."""
    media_caps = PROJECT_ROOT / "cache" / "_media_capabilities"
    # This directory is tracked in git — it should exist
    assert media_caps.exists(), "Test precondition: _media_capabilities should exist"

    reset_local_workspace()

    assert media_caps.exists(), "_media_capabilities was removed by reset!"


def test_reset_preserves_m6_uplift_fixtures():
    """Reset must NOT remove data/m6_uplift/ (tracked test fixtures)."""
    m6 = PROJECT_ROOT / "data" / "m6_uplift"
    assert m6.exists(), "Test precondition: m6_uplift fixtures should exist"

    reset_local_workspace()

    assert m6.exists(), "m6_uplift fixtures were removed by reset!"


def test_fresh_workspace_no_ready_products():
    """Fresh workspace has no ready products discoverable by product_db."""
    from src import product_db

    reset_local_workspace()

    ready = product_db.get_ready_products()
    assert ready == [], f"Fresh workspace should have no ready products, got {ready}"


def test_fresh_workspace_no_product_cache_records():
    """Fresh workspace has no product cache records."""
    from src import product_db

    reset_local_workspace()

    all_p = product_db.get_all_products()
    # Only m6_uplift (tracked fixtures) should appear, with status=empty
    user_products = [p for p in all_p if p["product_id"] != "m6_uplift"]
    assert len(user_products) == 0, f"User products remain: {user_products}"


def test_fresh_workspace_no_product_profile():
    """Fresh workspace has no product profile metadata."""
    from src.brand_loader import load_product_profile

    reset_local_workspace()

    for pid in ["TestProduct", "Lagenio K5", "ProductA"]:
        profile = load_product_profile(pid)
        assert profile == {}, f"Product profile for {pid} should be empty, got {profile}"


def test_fresh_workspace_no_product_images():
    """Fresh workspace has no product images resolvable by product_db."""
    from src import product_db

    reset_local_workspace()

    for pid in ["TestProduct", "Lagenio K5", "ProductA"]:
        paths = product_db.get_product_image_paths(pid)
        assert paths == [], f"Product images for {pid} should be empty, got {paths}"


def test_fresh_workspace_no_product_context():
    """Fresh workspace has no product context text."""
    from src import product_db

    reset_local_workspace()

    for pid in ["TestProduct", "Lagenio K5", "ProductA"]:
        ctx = product_db.get_agent_context(pid)
        assert ctx["text"] == "", f"Product context for {pid} should be empty"
        assert ctx["image_paths"] == [], f"Product images for {pid} should be empty"


def test_reset_clears_content_history():
    """Reset removes content history."""
    import json
    ch_path = PROJECT_ROOT / "cache" / "content_history.json"
    ch_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ch_path, "w") as f:
        json.dump({"entries": [{"test": True}]}, f)

    reset_local_workspace()

    assert not ch_path.exists(), "content_history.json still exists after reset"


def test_reset_clears_scheduler_state():
    """Reset removes scheduler state."""
    import json
    for fname in ["scheduled_jobs.json", "scheduled_runs.json"]:
        p = PROJECT_ROOT / "cache" / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump({"jobs": [], "runs": []}, f)

    reset_local_workspace()

    assert not (PROJECT_ROOT / "cache" / "scheduled_jobs.json").exists()
    assert not (PROJECT_ROOT / "cache" / "scheduled_runs.json").exists()


def test_reset_clears_run_resources():
    """Reset removes run resource sessions."""
    rr = PROJECT_ROOT / "cache" / "run_resources" / "test_session"
    rr.mkdir(parents=True, exist_ok=True)
    (rr / "resource.json").write_text("{}", encoding="utf-8")

    reset_local_workspace()

    assert not rr.exists(), "Run resources still exist after reset"


def test_reset_clears_staging():
    """Reset removes staging batches."""
    staging = PROJECT_ROOT / "data" / ".staging" / "test_batch"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "batch.json").write_text("{}", encoding="utf-8")

    reset_local_workspace()

    assert not staging.exists(), "Staging still exists after reset"


def test_reset_clears_output():
    """Reset removes generated outputs."""
    output = PROJECT_ROOT / "output" / "test_run"
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.txt").write_text("test", encoding="utf-8")

    reset_local_workspace()

    assert not output.exists(), "Output still exists after reset"


def test_reset_clears_asset_library():
    """Reset resets asset library catalog to empty."""
    import json
    # Assets now live in workspace/local/ — reset removes the whole workspace
    from src.local_workspace import local_root
    assets_db = local_root() / "cache" / "assets" / "db.json"
    assets_db.parent.mkdir(parents=True, exist_ok=True)
    with open(assets_db, "w") as f:
        json.dump({"assets": [{"id": "a_0001"}], "next_id": 2}, f)

    reset_local_workspace()

    assert not assets_db.exists(), "Asset library still exists after reset"
    # Verify asset_library reads empty after reset
    from src import asset_library
    assets = asset_library.list_all()
    assert assets == [], f"Asset library not empty after reset, got {assets}"


# --- 8. Lifecycle tests: Product, Content History, Brand ---

def test_product_lifecycle_create_and_reset():
    """Product lifecycle: fresh → create → runtime sees → reset → gone."""
    import json
    from src import product_db

    reset_local_workspace()

    # Fresh: no products
    assert product_db.get_ready_products() == []

    # Create a product (simulates ingestion — needs both data/ dir and cache/ record)
    data_dir = PROJECT_ROOT / "data" / "LifecycleTest"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "source.txt").write_text("Test product source", encoding="utf-8")
    record = {
        "product_id": "LifecycleTest",
        "status": product_db.STATUS_READY,
        "raw_text": "Test product description",
        "image_descriptions": [],
    }
    product_db.save("LifecycleTest", record)

    # Runtime sees it
    ready = product_db.get_ready_products()
    assert len(ready) == 1
    assert ready[0] == "LifecycleTest"

    # Reset
    reset_local_workspace()

    # Runtime can no longer resolve it
    assert product_db.get_ready_products() == []
    record = product_db.load("LifecycleTest")
    assert record["status"] == product_db.STATUS_EMPTY, f"Product still has data after reset: {record['status']}"


def test_content_history_lifecycle_create_and_reset():
    """Content history lifecycle: fresh → record → runtime sees → reset → gone."""
    import json
    from pathlib import Path
    from src.content_history import load_history, save_history, record_entry

    reset_local_workspace()
    project_root = PROJECT_ROOT

    # Fresh: no history
    history = load_history(project_root)
    assert history["entries"] == []

    # Record an entry (simulates Agent completion)
    record_entry(
        project_root,
        product_ids=["TestProduct"],
        concept="ทดสอบแคมเปญ",
        platform="facebook",
        caption_summary="คำบรรยายทดสอบ",
    )

    # Runtime sees it
    history = load_history(project_root)
    assert len(history["entries"]) >= 1

    # Reset
    reset_local_workspace()

    # Runtime can no longer resolve it
    history = load_history(project_root)
    assert history["entries"] == []


def test_brand_lifecycle_save_and_reset():
    """Brand lifecycle: fresh → save → runtime sees → reset → gone."""
    import json
    from src.brand_loader import load_brand_rules, load_brand_visual
    from src.local_workspace import local_brand_dir, save_agent_instructions_local

    reset_local_workspace()

    # Fresh: no brand rules
    rules = load_brand_rules(None)
    assert rules == ""

    # Save brand voice
    bdir = local_brand_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    with open(bdir / "voice.json", "w") as f:
        json.dump({"personality": "เป็นกันเอง", "tone_description": "สนุกสนาน"}, f, ensure_ascii=False)

    # Runtime sees it
    rules = load_brand_rules(None)
    assert "เป็นกันเอง" in rules

    # Reset
    reset_local_workspace()

    # Runtime can no longer resolve it
    rules = load_brand_rules(None)
    assert rules == ""


def test_product_profile_save_does_not_mutate_product_config():
    """Product profile save must NOT alter any file under config/."""
    import json
    # Simulate what the web_viewer endpoint does
    profile_dir = PROJECT_ROOT / "cache" / "ProfileTest"
    profile_dir.mkdir(parents=True, exist_ok=True)
    path = profile_dir / "product_profile.json"
    path.write_text(json.dumps({"audience": "test"}, ensure_ascii=False), encoding="utf-8")

    # Verify config/ is untouched
    config_dir = PROJECT_ROOT / "config"
    config_hash_before = hash(tuple(sorted(p.name for p in config_dir.iterdir())))

    # The profile is under cache/ (gitignored, cleared by reset)
    assert path.exists()

    reset_local_workspace()

    config_hash_after = hash(tuple(sorted(p.name for p in config_dir.iterdir())))
    assert config_hash_before == config_hash_after, "config/ directory changed!"
    assert not path.exists(), "Product profile still exists after reset"


def test_ai_usage_log_cleared_by_reset():
    """AI usage log (generated history) is cleared by reset."""
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "llm_usage.jsonl"
    log_file.write_text('{"test": true}\n', encoding="utf-8")

    assert log_file.exists()

    reset_local_workspace()

    assert not log_file.exists(), "AI usage log still exists after reset"


def test_media_capabilities_preserved_by_reset():
    """cache/_media_capabilities/ (product infrastructure) is preserved by reset."""
    media_caps = PROJECT_ROOT / "cache" / "_media_capabilities"
    assert media_caps.exists(), "Test precondition: _media_capabilities should exist"

    files_before = list(media_caps.glob("*.json"))

    reset_local_workspace()

    files_after = list(media_caps.glob("*.json"))
    assert len(files_after) == len(files_before), "_media_capabilities files changed!"
    assert media_caps.exists(), "_media_capabilities was removed by reset!"


# --- 9. Config file hash integrity ---

def test_product_config_hash_unchanged_by_user_saves():
    """All product config files must have identical hash before/after user saves."""
    import hashlib

    config_files = [
        "config/agent_instructions.json",
        "config/agents.yaml",
        "config/content_policy.yaml",
        "config/media.yaml",
    ]

    hashes_before = {}
    for f in config_files:
        p = PROJECT_ROOT / f
        if p.exists():
            hashes_before[f] = hashlib.sha256(p.read_bytes()).hexdigest()

    # Perform various user saves
    save_agent_instructions_local({"product_spec": {"custom": "test"}})
    save_agent_overrides({"product_spec": {"temperature": 0.1}})
    save_content_pillars_local(["test"], {"test": ["kw"]})
    save_media_overrides({"image_model": "test"})

    # Save brand state
    from src.local_workspace import local_brand_dir
    bdir = local_brand_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "voice.json").write_text('{"personality": "test"}', encoding="utf-8")

    # Verify all product config hashes unchanged
    for f in config_files:
        p = PROJECT_ROOT / f
        if p.exists():
            hash_after = hashlib.sha256(p.read_bytes()).hexdigest()
            assert hash_after == hashes_before[f], f"Product config {f} was mutated!"


# --- 10. Lifecycle/delete/reset symmetry ---

def test_product_delete_removes_content_history_references():
    """Deleting a Product must remove Content History entries referencing it.

    This proves lifecycle symmetry: delete the user object → all derived
    state that can influence future output is also removed.
    """
    import json
    from src import product_db
    from src.content_history import (
        record_entry,
        load_history,
        get_entries_for_product,
        delete_entries_for_product,
    )

    reset_local_workspace()

    # Create a product
    data_dir = PROJECT_ROOT / "data" / "SymTest"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "info.txt").write_text("Sym test product", encoding="utf-8")
    product_db.save("SymTest", {
        "product_id": "SymTest",
        "status": product_db.STATUS_READY,
        "raw_text": "Sym test product",
    })

    # Record content history referencing this product (auto-derived state)
    record_entry(
        PROJECT_ROOT,
        product_ids=["SymTest"],
        concept="แคมเปญทดสอบ",
        platform="facebook",
        caption_summary="คำบรรยายทดสอบ",
    )

    # Runtime sees the history
    entries = get_entries_for_product(PROJECT_ROOT, "SymTest")
    assert len(entries) >= 1, "Content history should have entries for SymTest"

    # Delete the product's content history entries (as api_delete_folder now does)
    removed = delete_entries_for_product(PROJECT_ROOT, "SymTest")
    assert removed >= 1, "Should have removed at least 1 entry"

    # Runtime can no longer see history for this product
    entries = get_entries_for_product(PROJECT_ROOT, "SymTest")
    assert entries == [], "Content history still references deleted product!"

    reset_local_workspace()


def test_product_delete_removes_product_profile():
    """Deleting a Product folder removes its product_profile.json too."""
    import json
    from src.brand_loader import load_product_profile

    reset_local_workspace()

    # Create product data + cache
    data_dir = PROJECT_ROOT / "data" / "ProfileSymTest"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "info.txt").write_text("Profile sym test", encoding="utf-8")

    cache_dir = PROJECT_ROOT / "cache" / "ProfileSymTest"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "product_profile.json").write_text(
        json.dumps({"audience": "test audience"}), encoding="utf-8"
    )

    # Runtime sees the profile
    profile = load_product_profile("ProfileSymTest")
    assert profile.get("audience") == "test audience"

    # Delete cache dir (as api_delete_folder does)
    import shutil
    shutil.rmtree(cache_dir)

    # Runtime can no longer resolve the profile
    profile = load_product_profile("ProfileSymTest")
    assert "audience" not in profile or profile.get("audience") != "test audience"

    reset_local_workspace()


def test_product_full_lifecycle_create_delete_reset():
    """Full product lifecycle: create → ingest → use → delete → verify gone → reset."""
    import json
    import shutil
    from src import product_db
    from src.content_history import (
        record_entry,
        get_entries_for_product,
        delete_entries_for_product,
    )
    from src.brand_loader import load_product_profile

    reset_local_workspace()

    # 1. Fresh: nothing exists
    assert product_db.get_ready_products() == []
    assert get_entries_for_product(PROJECT_ROOT, "FullLife") == []

    # 2. Create product (simulates ingestion)
    data_dir = PROJECT_ROOT / "data" / "FullLife"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "source.txt").write_text("Full lifecycle product", encoding="utf-8")

    cache_dir = PROJECT_ROOT / "cache" / "FullLife"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "product_profile.json").write_text(
        json.dumps({"category": "electronics"}), encoding="utf-8"
    )

    product_db.save("FullLife", {
        "product_id": "FullLife",
        "status": product_db.STATUS_READY,
        "raw_text": "Full lifecycle product",
    })

    # 3. Auto-derived state: content history
    record_entry(
        PROJECT_ROOT,
        product_ids=["FullLife"],
        concept="Launch campaign",
        platform="facebook",
        caption_summary="New product launch",
    )

    # 4. Runtime sees everything
    assert "FullLife" in product_db.get_ready_products()
    assert len(get_entries_for_product(PROJECT_ROOT, "FullLife")) >= 1
    assert load_product_profile("FullLife").get("category") == "electronics"

    # 5. Delete product (as api_delete_folder does)
    shutil.rmtree(data_dir)
    shutil.rmtree(cache_dir)
    delete_entries_for_product(PROJECT_ROOT, "FullLife")

    # 6. ALL product-specific runtime influence is gone
    assert product_db.get_ready_products() == []
    assert get_entries_for_product(PROJECT_ROOT, "FullLife") == []
    profile = load_product_profile("FullLife")
    assert profile.get("category") != "electronics"

    # 7. Reset also works
    reset_local_workspace()
    assert product_db.get_ready_products() == []


def test_content_history_delete_by_output_file():
    """Deleting an output file removes its content history entry (dedup symmetry)."""
    from src.content_history import (
        record_entry,
        load_history,
        delete_entry_by_output_file,
    )

    reset_local_workspace()

    # Record an entry with an output_file
    record_entry(
        PROJECT_ROOT,
        product_ids=["TestProduct"],
        concept="Test concept",
        platform="facebook",
        caption_summary="Test caption",
        output_file="/output/test.md",
    )

    # History has the entry
    history = load_history(PROJECT_ROOT)
    assert len(history["entries"]) >= 1

    # Delete by output_file (as api_delete_output_file does)
    removed = delete_entry_by_output_file(PROJECT_ROOT, "/output/test.md")
    assert removed >= 1

    # History no longer has the entry
    history = load_history(PROJECT_ROOT)
    assert all(e.get("output_file") != "/output/test.md" for e in history["entries"])

    reset_local_workspace()


def test_asset_delete_removes_from_runtime():
    """Deleting an asset removes it from the DB — runtime can no longer resolve it."""
    import json
    from src import asset_library
    from src.local_workspace import local_brand_dir

    reset_local_workspace()

    # Create an asset file
    assets_dir = local_brand_dir() / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_path = assets_dir / "test_asset.png"
    asset_path.write_bytes(b"fake png")

    # Register it in the DB (offline — mock tagger/embedder, no LLM)
    record = asset_library.ingest_asset(
        asset_path,
        user_note="test asset",
        tagger=lambda *a, **k: {"subject": "other", "style": "other", "tags": ["test"], "description": "test"},
        embedder=lambda *a, **k: [0.1, 0.2, 0.3],
    )
    asset_id = record["id"]
    assert asset_id, f"ingest_asset failed: {record}"

    # Runtime sees it
    assets = asset_library.list_all()
    assert len(assets) >= 1
    assert any(a["id"] == asset_id for a in assets)

    # Delete it
    deleted = asset_library.delete_asset(asset_id)
    assert deleted

    # Runtime can no longer resolve it
    assets = asset_library.list_all()
    assert not any(a["id"] == asset_id for a in assets)
    assert not asset_path.exists()

    reset_local_workspace()


def test_brand_reset_removes_all_influence():
    """Brand reset removes all brand influence from runtime."""
    import json
    from src.brand_loader import load_brand_rules, load_brand_visual
    from src.brand_priority import load_brand_priority
    from src.local_workspace import local_brand_dir

    reset_local_workspace()

    # Save brand state
    bdir = local_brand_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "voice.json").write_text(
        json.dumps({"personality": "เป็นกันเอง", "tone_description": "สนุก"}), encoding="utf-8"
    )
    (bdir / "terms.json").write_text(
        json.dumps({"must_use": ["สวัสดี"], "banned": ["เทอม"]}), encoding="utf-8"
    )
    (bdir / "visual.json").write_text(
        json.dumps({"style": "modern"}), encoding="utf-8"
    )

    # Runtime sees brand
    rules = load_brand_rules(None)
    assert "เป็นกันเอง" in rules
    visual = load_brand_visual(None)
    assert visual.get("style") == "modern"
    priority = load_brand_priority(None)
    assert priority is not None

    # Reset
    reset_local_workspace()

    # Runtime no longer sees brand
    rules = load_brand_rules(None)
    assert rules == ""
    visual = load_brand_visual(None)
    assert visual == {}


def test_full_reset_eliminates_all_previous_user_influence():
    """Full reset eliminates ALL previous User influence from runtime.

    This is the comprehensive test: create every type of user state,
    then reset, then verify none of it can influence future output.
    """
    import json
    from src import product_db, asset_library
    from src.content_history import (
        record_entry,
        get_entries_for_product,
        load_history,
    )
    from src.brand_loader import load_brand_rules, load_brand_visual
    from src.brand_priority import load_brand_priority
    from src.local_workspace import (
        local_brand_dir,
        load_agent_instructions_local,
        load_agent_overrides,
        load_content_pillars_local,
        load_media_overrides,
    )

    reset_local_workspace()

    # Create EVERY type of user state
    # 1. Agent instructions
    save_agent_instructions_local({"product_spec": {"custom": "test custom"}})
    # 2. Agent overrides
    save_agent_overrides({"product_spec": {"temperature": 0.1}})
    # 3. Content pillars
    save_content_pillars_local(["pillar1"], {"pillar1": ["kw1"]})
    # 4. Media overrides
    save_media_overrides({"image_model": "test"})
    # 5. Brand
    bdir = local_brand_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "voice.json").write_text('{"personality": "test"}', encoding="utf-8")
    # 6. Product
    data_dir = PROJECT_ROOT / "data" / "FullReset"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "info.txt").write_text("Full reset test", encoding="utf-8")
    product_db.save("FullReset", {
        "product_id": "FullReset",
        "status": product_db.STATUS_READY,
        "raw_text": "Full reset test",
    })
    # 7. Content history
    record_entry(
        PROJECT_ROOT,
        product_ids=["FullReset"],
        concept="test",
        platform="facebook",
        caption_summary="test",
    )
    # 8. Asset
    assets_dir = local_brand_dir() / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_path = assets_dir / "reset_test.png"
    asset_path.write_bytes(b"fake")
    asset_library.ingest_asset(
        asset_path,
        user_note="reset test",
        tagger=lambda *a, **k: {"subject": "other", "style": "other", "tags": ["test"], "description": "test"},
        embedder=lambda *a, **k: [0.1, 0.2, 0.3],
    )

    # Verify all state exists
    assert load_agent_instructions_local().get("product_spec", {}).get("custom") == "test custom"
    assert load_agent_overrides().get("product_spec", {}).get("temperature") == 0.1
    assert load_content_pillars_local()["pillars"] == ["pillar1"]
    assert load_media_overrides().get("image_model") == "test"
    assert "test" in load_brand_rules(None)  # voice is "test"
    assert "FullReset" in product_db.get_ready_products()
    assert len(get_entries_for_product(PROJECT_ROOT, "FullReset")) >= 1
    assert len(asset_library.list_all()) >= 1

    # RESET
    reset_local_workspace()

    # Verify ALL user influence is gone
    assert load_agent_instructions_local() == {}
    assert load_agent_overrides() == {}
    assert load_content_pillars_local() == {"pillars": [], "pillar_keywords": {}}
    assert load_media_overrides() == {}
    assert load_brand_rules(None) == ""
    assert load_brand_visual(None) == {}
    assert load_brand_priority(None).hard == ""
    assert product_db.get_ready_products() == []
    assert get_entries_for_product(PROJECT_ROOT, "FullReset") == []
    assert load_history(PROJECT_ROOT)["entries"] == []
    assert asset_library.list_all() == []


# --- 11. Safe reset preserves historical artifacts (Parts D-H) ---

def test_safe_reset_preserves_output_bytes(tmp_path, monkeypatch):
    """Safe reset archives output/ before removing — bytes survive in archive."""
    monkeypatch.setattr("src.local_workspace._archive_root", lambda: tmp_path / ".recovery_archive")
    import shutil

    # Create test output directory with real files
    output_dir = PROJECT_ROOT / "output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    (output_dir / "test_output").mkdir()
    (output_dir / "test_output" / "result.md").write_text("# Test output\n", encoding="utf-8")
    (output_dir / "test_output" / "result.json").write_text('{"test": true}', encoding="utf-8")

    # Clean any existing archive
    archive = tmp_path / ".recovery_archive"  # matches monkeypatched _archive_root
    if archive.exists():
        pass  # cleanup via monkeypatch if archive.exists() else None  # ponytail: safe cleanup

    # Safe reset (default)
    reset_local_workspace()

    # Output is gone from active workspace
    assert not output_dir.exists()

    # But archived copy exists
    archives = list(archive.glob("outputs_*"))
    assert len(archives) >= 1, "No output archive created"

    # Archive has the files
    archived_files = list(archives[0].rglob("*"))
    archived_files = [f for f in archived_files if f.is_file()]
    md_files = [f for f in archived_files if f.name == "result.md"]
    json_files = [f for f in archived_files if f.name == "result.json"]
    assert len(md_files) == 1, "result.md not in archive"
    assert len(json_files) == 1, "result.json not in archive"
    assert md_files[0].read_text(encoding="utf-8") == "# Test output\n"

    # Archive has manifest
    manifest_files = [f for f in archived_files if f.name == "ARCHIVE_MANIFEST.json"]
    assert len(manifest_files) == 1, "No ARCHIVE_MANIFEST.json"

    # Cleanup (only test archives, not the entire .recovery_archive/)
    for d in archive.glob("outputs_*"):
        shutil.rmtree(d)
    if output_dir.exists():
        shutil.rmtree(output_dir)


def test_safe_reset_preserves_log_bytes(tmp_path, monkeypatch):
    """Safe reset archives logs/ before removing — bytes survive in archive."""
    monkeypatch.setattr("src.local_workspace._archive_root", lambda: tmp_path / ".recovery_archive")
    import shutil

    # Create test log file
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "llm_usage.jsonl"
    log_file.write_text('{"test": "log entry"}\n', encoding="utf-8")

    # Clean any existing archive
    archive = tmp_path / ".recovery_archive"  # matches monkeypatched _archive_root
    if archive.exists():
        pass  # cleanup via monkeypatch if archive.exists() else None  # ponytail: safe cleanup

    # Safe reset (default)
    reset_local_workspace()

    # Log file is gone from active workspace
    assert not log_file.exists()

    # But archived copy exists
    archives = list(archive.glob("logs_*"))
    assert len(archives) >= 1, "No log archive created"

    # Archive has the file
    archived_files = list(archives[0].rglob("*"))
    archived_files = [f for f in archived_files if f.is_file()]
    log_files = [f for f in archived_files if f.name == "llm_usage.jsonl"]
    assert len(log_files) == 1, "llm_usage.jsonl not in archive"
    assert log_files[0].read_text(encoding="utf-8") == '{"test": "log entry"}\n'

    # Cleanup (only test archives)
    for d in archive.glob("logs_*"):
        shutil.rmtree(d)


def test_safe_reset_still_clears_output_affecting_state():
    """Safe reset still clears content history, product data, brand, overrides."""
    import json
    from src import product_db
    from src.content_history import record_entry, load_history
    from src.brand_loader import load_brand_rules
    from src.local_workspace import local_brand_dir

    reset_local_workspace()

    # Create output-affecting state
    save_agent_instructions_local({"product_spec": {"custom": "test"}})
    save_agent_overrides({"product_spec": {"temperature": 0.1}})
    save_content_pillars_local(["p1"], {"p1": ["kw"]})
    save_media_overrides({"image_model": "test"})

    data_dir = PROJECT_ROOT / "data" / "SafeResetTest"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "info.txt").write_text("test", encoding="utf-8")
    product_db.save("SafeResetTest", {
        "product_id": "SafeResetTest",
        "status": product_db.STATUS_READY,
        "raw_text": "test",
    })

    record_entry(
        PROJECT_ROOT,
        product_ids=["SafeResetTest"],
        concept="test",
        platform="facebook",
        caption_summary="test",
    )

    bdir = local_brand_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "voice.json").write_text('{"personality": "test"}', encoding="utf-8")

    # Safe reset
    reset_local_workspace()

    # All output-affecting state is cleared
    assert load_agent_instructions_local() == {}
    assert load_agent_overrides() == {}
    assert load_content_pillars_local() == {"pillars": [], "pillar_keywords": {}}
    assert load_media_overrides() == {}
    assert product_db.get_ready_products() == []
    assert load_history(PROJECT_ROOT)["entries"] == []
    assert load_brand_rules(None) == ""


def test_archive_not_visible_to_runtime_readers(tmp_path, monkeypatch):
    """Archived outputs do not enter Product DB, Content History, or Agent context."""
    monkeypatch.setattr("src.local_workspace._archive_root", lambda: tmp_path / ".recovery_archive")
    import shutil
    from src import product_db
    from src.content_history import load_history
    from src.brand_loader import load_brand_rules

    # Create output and archive it
    output_dir = PROJECT_ROOT / "output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    (output_dir / "archived_run").mkdir()
    (output_dir / "archived_run" / "result.md").write_text("archived", encoding="utf-8")

    archive = tmp_path / ".recovery_archive"  # matches monkeypatched _archive_root
    if archive.exists():
        pass  # cleanup via monkeypatch if archive.exists() else None  # ponytail: safe cleanup

    reset_local_workspace()

    # Archive exists
    archives = list(archive.glob("outputs_*"))
    assert len(archives) >= 1

    # Runtime readers do NOT see archived content
    assert product_db.get_ready_products() == []
    assert load_history(PROJECT_ROOT)["entries"] == []
    assert load_brand_rules(None) == ""

    # No code references recovery_archive as runtime state
    import subprocess
    result = subprocess.run(
        ["grep", "-r", "recovery_archive", "src/", "web_viewer.py", "--include=*.py"],
        capture_output=True, text=True,
    )
    # Only local_workspace.py should reference it (for archiving)
    assert "recovery_archive" not in result.stdout or "local_workspace" in result.stdout

    # Cleanup (only test archives)
    for d in archive.glob("outputs_*"):
        shutil.rmtree(d)


def test_archive_manifest_cannot_claim_uncopied_files(tmp_path):
    """Archive manifest is generated ONLY from successfully copied files."""
    import shutil

    # Create source with known files
    src = tmp_path / "source"
    src.mkdir()
    (src / "a.txt").write_text("content A", encoding="utf-8")
    (src / "b.txt").write_text("content B", encoding="utf-8")

    archive = tmp_path / "archive"

    # Archive it
    dest = _archive_directory(src, archive, "test")

    # Manifest exists and lists exactly the copied files
    manifest_path = dest / "ARCHIVE_MANIFEST.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["file_count"] == 2
    file_names = {f["relative_path"] for f in manifest["files"]}
    assert file_names == {"a.txt", "b.txt"}

    # Every file in manifest has correct hash
    for f in manifest["files"]:
        dest_file = dest / f["relative_path"]
        assert dest_file.exists(), f"Manifest claims {f['relative_path']} but file missing"
        assert _sha256(dest_file) == f["sha256"], f"Hash mismatch for {f['relative_path']}"


def test_failed_backup_verification_prevents_deletion(tmp_path, monkeypatch):
    """If archive verification fails, reset must NOT delete the source."""
    monkeypatch.setattr("src.local_workspace._archive_root", lambda: tmp_path / ".recovery_archive")
    import shutil

    # Create output with real files
    output_dir = PROJECT_ROOT / "output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    (output_dir / "important.md").write_text("important data", encoding="utf-8")

    archive = tmp_path / ".recovery_archive"  # matches monkeypatched _archive_root
    if archive.exists():
        pass  # cleanup via monkeypatch if archive.exists() else None  # ponytail: safe cleanup

    # Mock _sha256 to return different hashes (simulate corruption)
    original_sha256 = _sha256
    call_count = [0]
    original_fn = __import__("src.local_workspace", fromlist=["_sha256"])._sha256

    def mock_sha256(path):
        call_count[0] += 1
        # Return different hash on second call (destination check)
        if call_count[0] % 2 == 0:
            return "fake_hash_that_does_not_match"
        return original_fn(path)

    import src.local_workspace as lw
    lw._sha256 = mock_sha256

    try:
        # Reset should raise RuntimeError
        raised = False
        try:
            reset_local_workspace()
        except RuntimeError as e:
            raised = True
            assert "archive verification failed" in str(e).lower()

        assert raised, "Reset should have raised RuntimeError on archive failure"

        # Output must still exist (NOT deleted)
        assert output_dir.exists(), "Output was deleted despite archive failure!"
        assert (output_dir / "important.md").exists(), "important.md was deleted!"

    finally:
        # Restore
        lw._sha256 = original_fn
        # Cleanup (only test archives)
        for d in archive.glob("outputs_*"):
            shutil.rmtree(d)
        for d in archive.glob("logs_*"):
            shutil.rmtree(d)
        if output_dir.exists():
            shutil.rmtree(output_dir)


def test_purge_history_mode_deletes_without_archive(tmp_path, monkeypatch):
    """Explicit purge_history=True deletes outputs without archiving."""
    monkeypatch.setattr("src.local_workspace._archive_root", lambda: tmp_path / ".recovery_archive")
    import shutil

    # Create output
    output_dir = PROJECT_ROOT / "output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    (output_dir / "disposable.md").write_text("will be deleted", encoding="utf-8")

    archive = tmp_path / ".recovery_archive"  # matches monkeypatched _archive_root
    if archive.exists():
        pass  # cleanup via monkeypatch if archive.exists() else None  # ponytail: safe cleanup

    # Destructive purge
    reset_local_workspace(purge_history=True)

    # Output is gone
    assert not output_dir.exists()

    # No archive was created
    output_archives = list(archive.glob("outputs_*"))
    assert len(output_archives) == 0, "Archive was created in purge mode!"

    # Cleanup (only test archives)
    for d in archive.glob("outputs_*"):
        shutil.rmtree(d)
    for d in archive.glob("logs_*"):
        shutil.rmtree(d)


def test_default_reset_is_safe_not_destructive(tmp_path, monkeypatch):
    """Default reset_local_workspace() call is safe (archives, doesn't purge)."""
    monkeypatch.setattr("src.local_workspace._archive_root", lambda: tmp_path / ".recovery_archive")
    import shutil

    # Create output
    output_dir = PROJECT_ROOT / "output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    (output_dir / "preserve.md").write_text("should be archived", encoding="utf-8")

    archive = tmp_path / ".recovery_archive"  # matches monkeypatched _archive_root
    if archive.exists():
        pass  # cleanup via monkeypatch if archive.exists() else None  # ponytail: safe cleanup

    # Default call (no arguments)
    reset_local_workspace()

    # Output is gone from active workspace
    assert not output_dir.exists()

    # But archive was created
    output_archives = list(archive.glob("outputs_*"))
    assert len(output_archives) >= 1, "Default reset should archive, not purge"

    # Verify archived content
    archived_md = list(output_archives[0].rglob("preserve.md"))
    assert len(archived_md) == 1
    assert archived_md[0].read_text(encoding="utf-8") == "should be archived"

    # Cleanup (only test archives)
    for d in archive.glob("outputs_*"):
        shutil.rmtree(d)
