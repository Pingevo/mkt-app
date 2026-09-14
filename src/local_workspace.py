"""Local workspace — mutable user state isolated from product config.

All user-mutable state that was previously stored in git-tracked config files
now lives under ``workspace/local/``.  Product config in ``config/`` is
immutable from normal Web UI actions.

Layout::

    workspace/local/
        config/
            agent_instructions.json   — user's active per-agent instructions
            agent_overrides.yaml      — user overrides for model/temp/etc
            content_pillars.yaml       — user's content pillars + keywords
            media_overrides.yaml       — user overrides for media config

When a local file does not exist, runtime falls back to product defaults
or empty state — this is the first-use behavior.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


# --- Archive directory for historical artifacts --------------------------

def _archive_root() -> Path:
    """Non-runtime archive directory for historical artifacts."""
    return _project_root() / ".recovery_archive"


def _sha256(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _archive_directory(src: Path, archive_root: Path, label: str) -> Path:
    """Archive a directory to a non-runtime location with verified copy.

    Copies ``src`` into ``archive_root/<label>/`` and verifies every file
    hash matches before returning.  Raises ``RuntimeError`` if verification
    fails — the caller must NOT proceed with destructive cleanup if this
    raises.

    Returns the archive destination path.
    """
    if not src.exists():
        raise FileNotFoundError(f"Cannot archive non-existent path: {src}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = archive_root / f"{label}_{ts}"
    archive_root.mkdir(parents=True, exist_ok=True)

    # Handle timestamp collisions (multiple calls within the same second)
    counter = 0
    while dest.exists():
        counter += 1
        dest = archive_root / f"{label}_{ts}_{counter}"

    # Copy preserving metadata
    shutil.copytree(src, dest)

    # Verify: count + hash every file
    src_files = sorted(src.rglob("*"))
    src_files = [f for f in src_files if f.is_file()]
    dest_files = sorted(dest.rglob("*"))
    dest_files = [f for f in dest_files if f.is_file()]

    if len(src_files) != len(dest_files):
        raise RuntimeError(
            f"Archive verification FAILED: file count mismatch "
            f"(source={len(src_files)}, dest={len(dest_files)})"
        )

    manifest = []
    for sf, df in zip(src_files, dest_files):
        rel = sf.relative_to(src)
        sf_hash = _sha256(sf)
        df_hash = _sha256(df)
        if sf_hash != df_hash:
            raise RuntimeError(
                f"Archive verification FAILED: hash mismatch for {rel} "
                f"(source={sf_hash}, dest={df_hash})"
            )
        manifest.append({
            "relative_path": str(rel),
            "sha256": sf_hash,
            "size": sf.stat().st_size,
        })

    # Write manifest
    manifest_path = dest / "ARCHIVE_MANIFEST.json"
    manifest_data = {
        "source": str(src),
        "archived_at": ts,
        "file_count": len(src_files),
        "files": manifest,
    }
    manifest_path.write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return dest


def _archive_file(src: Path, archive_root: Path, label: str) -> Path:
    """Archive a single file to a non-runtime location with verified copy.

    Returns the archive destination path.
    """
    if not src.exists():
        raise FileNotFoundError(f"Cannot archive non-existent file: {src}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = archive_root / f"{label}_{ts}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name

    shutil.copy2(src, dest)

    # Verify hash
    src_hash = _sha256(src)
    dest_hash = _sha256(dest)
    if src_hash != dest_hash:
        raise RuntimeError(
            f"Archive verification FAILED: hash mismatch for {src.name} "
            f"(source={src_hash}, dest={dest_hash})"
        )

    manifest = [{
        "relative_path": src.name,
        "sha256": src_hash,
        "size": src.stat().st_size,
    }]
    manifest_path = dest_dir / "ARCHIVE_MANIFEST.json"
    manifest_data = {
        "source": str(src),
        "archived_at": ts,
        "file_count": 1,
        "files": manifest,
    }
    manifest_path.write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return dest


def local_root() -> Path:
    """Return the local workspace root (``workspace/local/``).

    When a per-user WorkspaceContext is active, this resolves to
    ``users/{user_id}/workspace/local/``.  Otherwise falls back to the
    global ``workspace/local/`` (CLI / backward compat).
    """
    from .workspace_context import user_state_root
    return user_state_root(_project_root()) / "workspace" / "local"


def local_config_dir() -> Path:
    """Return the local config directory (``workspace/local/config/``)."""
    return local_root() / "config"


def product_config_dir() -> Path:
    """Return the product config directory (``config/``)."""
    return _project_root() / "config"


def product_brand_dir() -> Path:
    """Return the product brand directory (``brand/`` — templates/examples)."""
    return _project_root() / "brand"


def local_brand_dir() -> Path:
    """Return the brand directory — brand-scoped, requires an active brand context.

    MB-02: brand files (voice/terms/visual/audience/profile/assets) live beneath
    ``users/<uid>/brands/<brand_id>/brand/``.  An authenticated user-only context
    (workspace set, no brand_id) must fail closed — never fall back to user root.
    Only a true no-workspace CLI context (``get_workspace()`` is None) retains
    the ``workspace/local/brand/`` fallback for CLI/test backward compat.
    """
    from .workspace_context import get_workspace
    ws = get_workspace()
    if ws is not None and ws.brand_id is not None:
        from .workspace_context import brand_state_root
        return brand_state_root() / "brand"
    if ws is None:
        return local_root() / "brand"
    raise ValueError("local_brand_dir requires an active brand context")


# --- Agent Instructions --------------------------------------------------

def agent_instructions_product_path() -> Path:
    """Product file: ``config/agent_instructions.json`` (presets/schema/defaults)."""
    return product_config_dir() / "agent_instructions.json"


def agent_instructions_local_path() -> Path:
    """Local file: ``workspace/local/config/agent_instructions.json`` (user active state)."""
    return local_config_dir() / "agent_instructions.json"


def load_agent_instructions_product() -> dict[str, Any]:
    """Load product preset/schema/default definitions (immutable)."""
    path = agent_instructions_product_path()
    if not path.exists():
        return {"_presets": {}, "_defaults": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_agent_instructions_local() -> dict[str, Any]:
    """Load user's active per-agent instructions from local workspace.

    Returns ``{}`` if no local file exists (first-use behavior).
    """
    path = agent_instructions_local_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_agent_instructions_local(data: dict[str, Any]) -> None:
    """Save user's active per-agent instructions to local workspace."""
    path = agent_instructions_local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_agent_instructions_merged() -> dict[str, Any]:
    """Merge product presets with local active state.

    Product keys (``_schema``, ``_defaults``, ``_presets``) come from product file.
    Per-agent keys come from local file.
    """
    product = load_agent_instructions_product()
    local = load_agent_instructions_local()
    merged = {k: v for k, v in product.items() if k.startswith("_")}
    merged.update(local)
    return merged


# --- Agent Overrides (agents.yaml editable fields) ----------------------

def agent_overrides_local_path() -> Path:
    """Local file: ``workspace/local/config/agent_overrides.yaml``."""
    return local_config_dir() / "agent_overrides.yaml"


def load_agent_overrides() -> dict[str, Any]:
    """Load user's agent setting overrides from local workspace.

    Returns ``{}`` if no local file exists (first-use: product defaults used).
    """
    import yaml
    path = agent_overrides_local_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def save_agent_overrides(data: dict[str, Any]) -> None:
    """Save user's agent setting overrides to local workspace."""
    import yaml
    path = agent_overrides_local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# --- Content Pillars ----------------------------------------------------

def content_pillars_local_path() -> Path:
    """Brand-scoped content pillars path (MB-02).

    ``users/<uid>/brands/<brand_id>/config/content_pillars.yaml`` when a brand
    context is active.  An authenticated user-only context (workspace set, no
    brand_id) must fail closed.  Only a true no-workspace CLI context
    (``get_workspace()`` is None) retains the local fallback for CLI compat.
    """
    from .workspace_context import get_workspace
    ws = get_workspace()
    if ws is not None and ws.brand_id is not None:
        from .workspace_context import brand_state_root
        return brand_state_root() / "config" / "content_pillars.yaml"
    if ws is None:
        return local_config_dir() / "content_pillars.yaml"
    raise ValueError("content_pillars_local_path requires an active brand context")


def load_content_pillars_local() -> dict[str, Any]:
    """Load user's content pillars from local workspace.

    Returns ``{"pillars": [], "pillar_keywords": {}}`` if no local file exists.
    """
    import yaml
    path = content_pillars_local_path()
    if not path.exists():
        return {"pillars": [], "pillar_keywords": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return {
                "pillars": data.get("pillars", []),
                "pillar_keywords": data.get("pillar_keywords", {}),
            }
    except Exception:
        return {"pillars": [], "pillar_keywords": {}}


def save_content_pillars_local(pillars: list[str], pillar_keywords: dict[str, list[str]]) -> None:
    """Save user's content pillars to local workspace."""
    import yaml
    path = content_pillars_local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"pillars": pillars, "pillar_keywords": pillar_keywords}
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# --- Media Overrides ----------------------------------------------------

def media_overrides_local_path() -> Path:
    """Local file: ``workspace/local/config/media_overrides.yaml``."""
    return local_config_dir() / "media_overrides.yaml"


def load_media_overrides() -> dict[str, Any]:
    """Load user's media config overrides from local workspace.

    Returns ``{}`` if no local file exists (first-use: product defaults used).
    """
    import yaml
    path = media_overrides_local_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def save_media_overrides(data: dict[str, Any]) -> None:
    """Save user's media config overrides to local workspace."""
    import yaml
    path = media_overrides_local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# --- Product ingestion cache (user-derived state) -----------------------

# Product-owned cache subdirectories that must NOT be removed during reset.
# These are product/runtime infrastructure, not user-derived state.
_PRODUCT_OWNED_CACHE_DIRS = frozenset({
    "_media_capabilities",  # model/provider capability cache (tracked in git)
})

# Product-owned data subdirectories (tracked test fixtures, not user uploads).
_PRODUCT_OWNED_DATA_DIRS = frozenset({
    "m6_uplift",  # tracked test fixtures
})


def _state_root() -> Path:
    """Brand-scoped root when a brand context is active, else per-user root (MB-02)."""
    from .workspace_context import get_workspace, user_state_root
    ws = get_workspace()
    if ws is not None and ws.brand_id is not None:
        from .workspace_context import brand_state_root
        return brand_state_root()
    return user_state_root(_project_root())


def _clear_product_cache() -> None:
    """Remove all user-derived product cache from ``cache/{product_id}/``.

    Preserves product-owned infrastructure directories like ``_media_capabilities``.
    Brand-scoped (MB-02): clears the active brand's cache.
    """
    cache_dir = _state_root() / "cache"
    if not cache_dir.exists():
        return
    for item in cache_dir.iterdir():
        if not item.is_dir():
            continue
        if item.name in _PRODUCT_OWNED_CACHE_DIRS:
            continue
        if item.name.startswith("."):
            continue
        shutil.rmtree(item)


def _clear_product_uploads() -> None:
    """Remove all user-uploaded product raw data from ``data/{product_id}/``.

    Preserves product-owned fixture directories (e.g. ``m6_uplift``).
    Brand-scoped (MB-02): clears the active brand's data.
    """
    data_dir = _state_root() / "data"
    if not data_dir.exists():
        return
    for item in data_dir.iterdir():
        if not item.is_dir():
            continue
        if item.name in _PRODUCT_OWNED_DATA_DIRS:
            continue
        if item.name.startswith("."):
            continue
        shutil.rmtree(item)


# --- Reset ---------------------------------------------------------------

def reset_local_workspace(*, purge_history: bool = False) -> None:
    """Remove all mutable local user state — equivalent to first local launch.

    By default (``purge_history=False``) this is a **safe reset**:

    - Output-affecting mutable state is removed (agent overrides, product
      data/cache, brand, content pillars, content history, assets, scheduler).
    - Historical artifacts (generated outputs, usage logs) are **archived**
      to ``.recovery_archive/`` with verified copy BEFORE removal.

    With ``purge_history=True`` this becomes a **destructive purge**:

    - Historical artifacts are deleted WITHOUT archiving.
    - This mode must be explicitly requested — never the default.

    Removes (both modes):
      - ``workspace/local/`` (agent instructions, overrides, pillars, media overrides)
      - ``cache/{product_id}/`` (product DB records, derived ingestion state)
      - ``data/{product_id}/`` (user-uploaded raw product files)
      - ``cache/assets/`` (asset library catalog)
      - ``cache/content_history.json`` (content history)
      - ``cache/scheduled_jobs.json``, ``cache/scheduled_runs.json`` (scheduler state)
      - ``cache/run_resources/`` (run resource sessions)
      - ``data/.staging/`` (staging batches)
      - Brand user state (voice, terms, visual, assets)

    Safe mode (default) archives before removing:
      - ``output/`` → ``.recovery_archive/outputs_<timestamp>/``
      - ``logs/`` → ``.recovery_archive/logs_<timestamp>/``

    Destructive mode (``purge_history=True``) deletes without archiving:
      - ``output/``
      - ``logs/``

    Preserves:
      - ``config/`` (product-owned configuration)
      - ``cache/_media_capabilities/`` (product-owned model capability cache)
      - ``data/m6_uplift/`` (tracked test fixtures)
      - ``.recovery_archive/`` (historical archives)
      - All source code, tests, and product defaults
    """
    archive_root = _archive_root()

    _ws_root = _state_root()

    # 1. Local workspace config
    root = local_root()
    if root.exists():
        shutil.rmtree(root)

    # 2. Product ingestion cache (user-derived)
    _clear_product_cache()

    # 3. Product raw uploads (user data)
    _clear_product_uploads()

    # 4. Other user-derived runtime state
    cache_dir = _ws_root / "cache"
    for state_file in ["content_history.json", "scheduled_jobs.json", "scheduled_runs.json"]:
        p = cache_dir / state_file
        if p.exists():
            p.unlink()

    # 5. Asset library catalog (legacy path — assets now live in workspace/local/)
    legacy_assets = cache_dir / "assets" / "db.json"
    if legacy_assets.exists():
        legacy_assets.unlink()

    # 6. Run resources
    rr_dir = cache_dir / "run_resources"
    if rr_dir.exists():
        shutil.rmtree(rr_dir)

    # 7. Staging
    staging_dir = _ws_root / "data" / ".staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    # 8. Generated outputs — archive before removal (safe) or delete (purge)
    output_dir = _ws_root / "output"
    if output_dir.exists():
        if purge_history:
            shutil.rmtree(output_dir)
        else:
            try:
                _archive_directory(output_dir, archive_root, "outputs")
                shutil.rmtree(output_dir)
            except (RuntimeError, FileNotFoundError) as e:
                # Archive failed — DO NOT delete outputs
                raise RuntimeError(
                    f"Refusing to delete output/ — archive verification failed: {e}. "
                    f"Use purge_history=True for explicit destructive cleanup."
                ) from e

    # 9. Brand user state — brand-scoped when a brand context is active (MB-02),
    #    else global brand/ (CLI backward compat).
    from .workspace_context import get_workspace
    ws = get_workspace()
    if ws is not None and ws.brand_id is not None:
        # Brand-scoped: clear brand files + content pillars under brand_state_root()
        brand_dir = _ws_root / "brand"
        if brand_dir.exists():
            shutil.rmtree(brand_dir)
        pillars = _ws_root / "config" / "content_pillars.yaml"
        if pillars.exists():
            pillars.unlink()
    elif ws is None:
        brand_dir = _project_root() / "brand"
        for fname in ("voice.json", "terms.json", "visual.json", "audience.json",
                      "brand_profile.md", "tone_of_voice.md", "visual_guidelines.md",
                      "target_audience.md"):
            p = brand_dir / fname
            if p.exists():
                p.unlink()
        brand_assets = brand_dir / "assets"
        if brand_assets.exists():
            shutil.rmtree(brand_assets)

    # 10. AI usage log (historical accounting) — archive before removal (safe)
    #     or delete (purge)
    logs_dir = _ws_root / "logs"
    if logs_dir.exists():
        log_files = [f for f in logs_dir.iterdir() if f.is_file()]
        if purge_history:
            for f in log_files:
                f.unlink()
        else:
            for f in log_files:
                try:
                    _archive_file(f, archive_root, "logs")
                    f.unlink()
                except (RuntimeError, FileNotFoundError) as e:
                    raise RuntimeError(
                        f"Refusing to delete {f} — archive verification failed: {e}. "
                        f"Use purge_history=True for explicit destructive cleanup."
                    ) from e
