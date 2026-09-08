"""Production-path offline tests for the six review findings.

All tests use fakes/mocks at provider boundaries only. No paid/model/web/media calls.

Finding coverage:
  1. Multi-product regression — Agents 2 and 4 receive both products' text and images.
  2. Scoped fallback guard — scoped product with empty raw_text + no images fails
     before Agent/model construction via the real _run_single_agent path.
  3. Staging update — new batch media is propagated, existing media preserved,
     all paths survive discard_batch().
  4. Evidence isolation — Stage A prompt excludes brand_reference; brand
     interpretation receives finalized evidence only.
  5. Product-profile delivery — audience, positioning, tone, visual override
     reach final model input for Agents 1-4.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class FakeLLM:
    """Deterministic LLM double — no provider calls."""

    def __init__(self, output: str = "mock output"):
        self._output = output
        self.calls: list[dict] = []
        self.last_truncated = False

    def chat(self, messages, **kwargs):
        source = kwargs.get("source", "")
        if "final_grounding_check" in source:
            return '{"grounded": true, "unsupported_claims": []}'
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("return_annotations"):
            return self._output, []
        return self._output

    def close(self):
        pass

    def abort(self):
        pass


def _make_product_profile(cache_dir: Path, product_id: str, profile: dict) -> None:
    """Write a product_profile.json in cache/{product_id}/."""
    p = cache_dir / product_id / "product_profile.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_brand_dir(tmp_path: Path) -> Path:
    """Create a minimal brand directory with voice.json and audience.json."""
    brand_dir = tmp_path / "brand"
    brand_dir.mkdir(exist_ok=True)
    (brand_dir / "voice.json").write_text(json.dumps({
        "personality": "base personality",
        "tone_description": "base tone",
    }), encoding="utf-8")
    (brand_dir / "audience.json").write_text(json.dumps({
        "primary": {"age": "25-45", "gender": "all"},
    }), encoding="utf-8")
    return brand_dir


# ---------------------------------------------------------------------------
# Finding 1: Multi-product regression — Agents 2 and 4 receive both products
# ---------------------------------------------------------------------------

def test_multi_product_agent2_receives_both_products_text_and_images(tmp_path, monkeypatch):
    """Agent 2 (competitor_analysis) must receive scoped text and images
    for BOTH products in a multi-product run via _run_single_agent."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.config_loader import load_config
    from src.agents import base_agent

    monkeypatch.chdir(tmp_path)

    # Create two ready products with text and images
    for pid, text in [("ProductA", "ProductA spec data CPU ATS3085L"),
                      ("ProductB", "ProductB spec data CPU RTL8763E")]:
        cache_dir = tmp_path / "cache" / pid
        cache_dir.mkdir(parents=True)
        img = cache_dir / "extracted_images" / f"{pid}_img.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        product_db.save(pid, {
            "product_id": pid,
            "status": product_db.STATUS_READY,
            "raw_text": text,
            "text_extracts": [{"file": "test.txt", "text": text}],
            "image_descriptions": [{"path": str(img), "file": f"{pid}_img.png"}],
        })

    brand_dir = _make_brand_dir(tmp_path)

    # Capture what the agent receives
    captured_prompts: dict[str, str] = {}
    captured_images: dict[str, list[str]] = {}
    orig_run = base_agent.BaseAgent.run

    def _capture_run(self, user_prompt, **kwargs):
        captured_prompts[self.agent_name] = user_prompt
        captured_images[self.agent_name] = list(kwargs.get("image_paths") or [])
        if self.agent_name == "content_creator":
            return '{"posts":[{"platform":"Facebook","concept":"c","title":"T","caption":"C","hashtags":"#h","asset_ids":[]}]}'
        return f"[{self.agent_name} result]"

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator(brand_dir=str(brand_dir))

    # Simulate what _run_single_agent does for competitor_analysis with 2 folders
    folders = ["ProductA", "ProductB"]
    combined_id = " + ".join(folders)
    orch.bind_product(combined_id, product_images=[])

    # Agent 2: competitor_analysis
    result = orch.run_competitor_analysis("", None, llm=FakeLLM())

    # Both products' text must reach the agent
    prompt = captured_prompts.get("competitor_analysis", "")
    assert "ProductA" in prompt
    assert "ProductB" in prompt
    assert "ATS3085L" in prompt
    assert "RTL8763E" in prompt

    # Both products' images must reach the agent
    images = captured_images.get("competitor_analysis", [])
    image_names = [Path(p).name for p in images]
    assert "ProductA_img.png" in image_names
    assert "ProductB_img.png" in image_names


def test_multi_product_agent4_receives_both_products_text_and_images(tmp_path, monkeypatch):
    """Agent 4 (content_creator) must receive scoped text and images
    for BOTH products in a multi-product run."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.agents import base_agent

    monkeypatch.chdir(tmp_path)

    for pid, text in [("ProductA", "ProductA spec data CPU ATS3085L"),
                      ("ProductB", "ProductB spec data CPU RTL8763E")]:
        cache_dir = tmp_path / "cache" / pid
        cache_dir.mkdir(parents=True)
        img = cache_dir / "extracted_images" / f"{pid}_img.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        product_db.save(pid, {
            "product_id": pid,
            "status": product_db.STATUS_READY,
            "raw_text": text,
            "text_extracts": [{"file": "test.txt", "text": text}],
            "image_descriptions": [{"path": str(img), "file": f"{pid}_img.png"}],
        })

    brand_dir = _make_brand_dir(tmp_path)

    captured_prompts: dict[str, str] = {}
    captured_images: dict[str, list[str]] = {}
    orig_run = base_agent.BaseAgent.run

    def _capture_run(self, user_prompt, **kwargs):
        captured_prompts[self.agent_name] = user_prompt
        captured_images[self.agent_name] = list(kwargs.get("image_paths") or [])
        return '{"posts":[{"platform":"Facebook","concept":"c","title":"T","caption":"C","hashtags":"#h","asset_ids":[]}]}'

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator(brand_dir=str(brand_dir))

    folders = ["ProductA", "ProductB"]
    combined_id = " + ".join(folders)
    orch.bind_product(combined_id, product_images=[])

    # Agent 4: content_creator
    orch.run_content_creator("", "", "", llm=FakeLLM())

    prompt = captured_prompts.get("content_creator", "")
    assert "ProductA" in prompt
    assert "ProductB" in prompt

    images = captured_images.get("content_creator", [])
    image_names = [Path(p).name for p in images]
    assert "ProductA_img.png" in image_names
    assert "ProductB_img.png" in image_names


def test_multi_product_run_single_agent_agent2(tmp_path, monkeypatch):
    """Production-path test: _run_single_agent for Agent 2 with two ready
    products proves both products' text and images reach the constructed Agent."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.agents import base_agent
    import web_viewer

    monkeypatch.chdir(tmp_path)

    for pid, text in [("ProductA", "ProductA spec data CPU ATS3085L"),
                      ("ProductB", "ProductB spec data CPU RTL8763E")]:
        cache_dir = tmp_path / "cache" / pid
        cache_dir.mkdir(parents=True)
        img = cache_dir / "extracted_images" / f"{pid}_img.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        product_db.save(pid, {
            "product_id": pid,
            "status": product_db.STATUS_READY,
            "raw_text": text,
            "text_extracts": [{"file": "test.txt", "text": text}],
            "image_descriptions": [{"path": str(img), "file": f"{pid}_img.png"}],
        })

    brand_dir = _make_brand_dir(tmp_path)

    captured_prompts: dict[str, str] = {}
    captured_images: dict[str, list[str]] = {}

    def _capture_run(self, user_prompt, **kwargs):
        captured_prompts[self.agent_name] = user_prompt
        captured_images[self.agent_name] = list(kwargs.get("image_paths") or [])
        return f"[{self.agent_name} result]"

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator(brand_dir=str(brand_dir))
    llm = FakeLLM()

    web_viewer._run_single_agent(
        "competitor_analysis", "ProductA", [], [], {},
        orch, llm, tmp_path / "output", save_output=False,
        folders=["ProductA", "ProductB"],
    )

    prompt = captured_prompts.get("competitor_analysis", "")
    assert "ProductA" in prompt
    assert "ProductB" in prompt
    assert "ATS3085L" in prompt
    assert "RTL8763E" in prompt

    images = captured_images.get("competitor_analysis", [])
    image_names = [Path(p).name for p in images]
    assert "ProductA_img.png" in image_names
    assert "ProductB_img.png" in image_names


def test_multi_product_run_single_agent_agent4(tmp_path, monkeypatch):
    """Production-path test: _run_single_agent for Agent 4 with two ready
    products proves both products' text and images reach the constructed Agent."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.agents import base_agent
    import web_viewer

    monkeypatch.chdir(tmp_path)

    for pid, text in [("ProductA", "ProductA spec data CPU ATS3085L"),
                      ("ProductB", "ProductB spec data CPU RTL8763E")]:
        cache_dir = tmp_path / "cache" / pid
        cache_dir.mkdir(parents=True)
        img = cache_dir / "extracted_images" / f"{pid}_img.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        product_db.save(pid, {
            "product_id": pid,
            "status": product_db.STATUS_READY,
            "raw_text": text,
            "text_extracts": [{"file": "test.txt", "text": text}],
            "image_descriptions": [{"path": str(img), "file": f"{pid}_img.png"}],
        })

    brand_dir = _make_brand_dir(tmp_path)

    captured_prompts: dict[str, str] = {}
    captured_images: dict[str, list[str]] = {}

    def _capture_run(self, user_prompt, **kwargs):
        captured_prompts[self.agent_name] = user_prompt
        captured_images[self.agent_name] = list(kwargs.get("image_paths") or [])
        return '{"posts":[{"platform":"Facebook","concept":"c","title":"T","caption":"C","hashtags":"#h","asset_ids":[]}]}'

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator(brand_dir=str(brand_dir))
    llm = FakeLLM()

    web_viewer._run_single_agent(
        "content_creator", "ProductA", [], [], {},
        orch, llm, tmp_path / "output", save_output=False,
        folders=["ProductA", "ProductB"],
        platforms=["facebook"],
    )

    prompt = captured_prompts.get("content_creator", "")
    assert "ProductA" in prompt
    assert "ProductB" in prompt

    images = captured_images.get("content_creator", [])
    image_names = [Path(p).name for p in images]
    assert "ProductA_img.png" in image_names
    assert "ProductB_img.png" in image_names


# ---------------------------------------------------------------------------
# Finding 2: Scoped fallback guard — real _run_single_agent path
# ---------------------------------------------------------------------------

def test_scoped_product_empty_text_no_images_fails_before_agent(tmp_path, monkeypatch):
    """A scoped product with empty raw_text and no valid images must fail
    before Agent/model construction via the real _run_single_agent path.
    The fake Agent/LLM must never be invoked."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.agents import base_agent
    import web_viewer

    monkeypatch.chdir(tmp_path)

    # Create a scoped product with empty raw_text and no images
    cache_dir = tmp_path / "cache" / "ScopedProduct"
    cache_dir.mkdir(parents=True)
    product_db.save("ScopedProduct", {
        "product_id": "ScopedProduct",
        "status": product_db.STATUS_READY,
        "raw_text": "",
        "text_extracts": [],
        "image_descriptions": [],
        "scope": {"product_key": "ScopedProduct", "source_refs": [], "common_refs": []},
    })

    brand_dir = _make_brand_dir(tmp_path)

    agent_was_called = False

    class _GuardAgent:
        agent_name = "product_spec"
        def run(self, *args, **kwargs):
            nonlocal agent_was_called
            agent_was_called = True
            return "should never reach here"

    def _make_guard_agent(agent_name, agent_cls, llm):
        return _GuardAgent()

    orch = Orchestrator(brand_dir=str(brand_dir))
    orch._make_agent = _make_guard_agent

    llm = FakeLLM()

    with pytest.raises(ValueError, match="scope"):
        web_viewer._run_single_agent(
            "product_spec", "ScopedProduct",
            ["FULL CATALOG TEXT THAT SHOULD NEVER REACH THE AGENT"],
            [], {}, orch, llm, tmp_path / "output", save_output=False,
            folders=["ScopedProduct"],
        )

    assert not agent_was_called, "Agent/LLM must never be invoked for scoped product with no usable data"


def test_scoped_product_with_images_but_no_text_proceeds(tmp_path, monkeypatch):
    """A scoped product with empty raw_text BUT valid images must proceed
    (image-only execution is allowed)."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.agents import base_agent
    import web_viewer

    monkeypatch.chdir(tmp_path)

    cache_dir = tmp_path / "cache" / "ImageScoped"
    cache_dir.mkdir(parents=True)
    img = cache_dir / "extracted_images" / "img1.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    product_db.save("ImageScoped", {
        "product_id": "ImageScoped",
        "status": product_db.STATUS_READY,
        "raw_text": "",
        "text_extracts": [],
        "image_descriptions": [{"path": str(img), "file": "img1.png"}],
        "scope": {"product_key": "ImageScoped", "source_refs": [], "common_refs": []},
    })

    brand_dir = _make_brand_dir(tmp_path)

    agent_was_called = False

    def _capture_run(self, user_prompt, **kwargs):
        nonlocal agent_was_called
        agent_was_called = True
        return "[product_spec result]"

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator(brand_dir=str(brand_dir))
    llm = FakeLLM()

    web_viewer._run_single_agent(
        "product_spec", "ImageScoped",
        [], [str(img)], {}, orch, llm, tmp_path / "output",
        save_output=False, folders=["ImageScoped"],
    )

    assert agent_was_called, "Image-only scoped product must proceed to agent"


def test_unscoped_product_empty_text_no_images_raw_fallback(tmp_path, monkeypatch):
    """An unscoped product with empty DB text must still use raw_contents
    fallback (backward compatible)."""
    from src import product_db
    from src.orchestrator import Orchestrator
    from src.agents import base_agent
    import web_viewer

    monkeypatch.chdir(tmp_path)

    product_db.save("LegacyProduct", {
        "product_id": "LegacyProduct",
        "status": product_db.STATUS_READY,
        "raw_text": "",
        "text_extracts": [],
        "image_descriptions": [],
    })

    brand_dir = _make_brand_dir(tmp_path)

    captured_prompt = {}

    def _capture_run(self, user_prompt, **kwargs):
        captured_prompt["prompt"] = user_prompt
        return "[product_spec result]"

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch = Orchestrator(brand_dir=str(brand_dir))
    llm = FakeLLM()

    web_viewer._run_single_agent(
        "product_spec", "LegacyProduct",
        ["legacy raw file content"], [], {}, orch, llm,
        tmp_path / "output", save_output=False,
        folders=["LegacyProduct"],
    )

    assert "legacy raw file content" in captured_prompt.get("prompt", "")


# ---------------------------------------------------------------------------
# Finding 3: Staging update propagates new batch media, preserves existing
# ---------------------------------------------------------------------------

@pytest.fixture
def _stage(monkeypatch, tmp_path):
    """Setup tmp project root + reload staging + product_db."""
    import importlib
    import src.product_db as product_db

    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    importlib.reload(product_db)
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)

    import src.staging as staging
    importlib.reload(staging)
    monkeypatch.setattr(staging, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(staging, "product_db", product_db)
    monkeypatch.setattr(staging, "_generate_product_profile", lambda *a, **kw: None)
    return staging


def _make_xlsx_with_image() -> bytes:
    """Create a minimal xlsx with one embedded image in xl/media/."""
    import zipfile
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '</Types>'
        ))
        zf.writestr("xl/workbook.xml", (
            '<?xml version="1.0"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
            '</sheets></workbook>'
        ))
        zf.writestr("xl/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return buf.getvalue()


def test_staging_create_propagates_extracted_media(_stage, tmp_path):
    """Staging create must extract and propagate embedded media from the
    new batch. All paths must survive discard_batch()."""
    staging = _stage
    import src.product_db as product_db

    xlsx_bytes = _make_xlsx_with_image()

    batch_id = staging.create_batch([("catalog.xlsx", xlsx_bytes)])
    staging.run_segmentation(batch_id, llm=None)

    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "create", "name": "NewProduct"},
    ])

    assert result["created"] == ["NewProduct"]

    rec = product_db.load("NewProduct")
    imgs = rec.get("image_descriptions", [])
    assert len(imgs) > 0, "create must propagate extracted images"

    for img in imgs:
        p = img.get("path", "")
        assert p, "image path must not be empty"
        assert "NewProduct" in p
        assert Path(p).exists(), f"image must survive discard_batch: {p}"

    assert not (tmp_path / "data" / ".staging" / batch_id).exists()

    meta = rec.get("metadata", {})
    assert meta.get("image_count", 0) == len(imgs)


def test_staging_update_propagates_new_media_and_preserves_existing(_stage, tmp_path):
    """Staging update must:
    1. Copy new batch source files into the target data dir
    2. Extract media from the NEW files
    3. Preserve existing valid media (merge, not overwrite)
    4. All paths must survive discard_batch()
    """
    staging = _stage
    import src.product_db as product_db

    # 1. Create an existing product with existing media
    existing_data_dir = tmp_path / "data" / "ExistingProduct"
    existing_data_dir.mkdir(parents=True)
    (existing_data_dir / "old_spec.txt").write_text("existing product spec", encoding="utf-8")

    existing_cache_dir = tmp_path / "cache" / "ExistingProduct" / "extracted_images"
    existing_cache_dir.mkdir(parents=True)
    existing_img = existing_cache_dir / "old_img.png"
    existing_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    product_db.save("ExistingProduct", {
        "product_id": "ExistingProduct",
        "status": product_db.STATUS_READY,
        "raw_text": "existing product spec",
        "text_extracts": [{"file": "old_spec.txt", "text": "existing product spec"}],
        "image_descriptions": [{"path": str(existing_img), "file": "old_img.png"}],
        "video_transcripts": [{"file": "vid.mp4", "transcript": "old transcript"}],
        "audio_transcripts": [{"file": "aud.mp3", "transcript": "old audio"}],
    })

    # 2. Upload a new batch with a new document containing new embedded media
    xlsx_bytes = _make_xlsx_with_image()
    batch_id = staging.create_batch([("new_catalog.xlsx", xlsx_bytes)])
    staging.run_segmentation(batch_id, llm=None)

    # 3. Commit as update to ExistingProduct
    result = staging.commit_batch(batch_id, [
        {"segment_index": 0, "action": "update", "target": "ExistingProduct"},
    ])

    assert result["updated"] == ["ExistingProduct"]

    # 4. Verify both old and new media are present
    rec = product_db.load("ExistingProduct")
    imgs = rec.get("image_descriptions", [])

    # Old image must still be present
    old_img_paths = [i for i in imgs if i.get("file") == "old_img.png"]
    assert len(old_img_paths) == 1, "existing image must be preserved"
    assert Path(old_img_paths[0]["path"]).exists(), "old image path must still exist"

    # New image must be present
    new_img_paths = [i for i in imgs if i.get("file") != "old_img.png"]
    assert len(new_img_paths) > 0, "new extracted image must be propagated"
    for img in new_img_paths:
        assert Path(img["path"]).exists(), f"new image must survive discard: {img['path']}"

    # 5. Existing transcripts must be preserved
    assert rec.get("video_transcripts") == [{"file": "vid.mp4", "transcript": "old transcript"}]
    assert rec.get("audio_transcripts") == [{"file": "aud.mp3", "transcript": "old audio"}]

    # 6. New source file must be in the data dir
    assert (existing_data_dir / "new_catalog.xlsx").exists(), \
        "new batch source file must be copied to target data dir"

    # 7. Staging must be discarded
    assert not (tmp_path / "data" / ".staging" / batch_id).exists()

    # 8. Metadata must report correct image count
    meta = rec.get("metadata", {})
    assert meta.get("image_count", 0) == len(imgs)


def test_staging_update_idempotent_no_duplicate_records(_stage, tmp_path):
    """Committing the same update twice must not create duplicate media records."""
    staging = _stage
    import src.product_db as product_db

    # Create existing product
    data_dir = tmp_path / "data" / "IdempotentProduct"
    data_dir.mkdir(parents=True)
    (data_dir / "spec.txt").write_text("spec", encoding="utf-8")
    product_db.save("IdempotentProduct", {
        "product_id": "IdempotentProduct",
        "status": product_db.STATUS_READY,
        "raw_text": "spec",
        "text_extracts": [],
        "image_descriptions": [],
    })

    xlsx_bytes = _make_xlsx_with_image()

    # First update
    batch_id1 = staging.create_batch([("new.xlsx", xlsx_bytes)])
    staging.run_segmentation(batch_id1, llm=None)
    staging.commit_batch(batch_id1, [
        {"segment_index": 0, "action": "update", "target": "IdempotentProduct"},
    ])

    rec1 = product_db.load("IdempotentProduct")
    imgs1 = rec1.get("image_descriptions", [])
    count1 = len(imgs1)

    # Second update with same file
    batch_id2 = staging.create_batch([("new.xlsx", xlsx_bytes)])
    staging.run_segmentation(batch_id2, llm=None)
    staging.commit_batch(batch_id2, [
        {"segment_index": 0, "action": "update", "target": "IdempotentProduct"},
    ])

    rec2 = product_db.load("IdempotentProduct")
    imgs2 = rec2.get("image_descriptions", [])

    # Should not duplicate — existing paths are filtered, new paths replace
    paths = [i.get("path") for i in imgs2]
    assert len(paths) == len(set(paths)), "no duplicate image paths"


# ---------------------------------------------------------------------------
# Finding 4: Evidence isolation — Stage A excludes brand_reference
# ---------------------------------------------------------------------------

def test_evidence_stage_a_prompt_excludes_brand_reference():
    """Stage A system prompt must NOT contain brand_reference content.
    Brand interpretation happens in a separate BrandInterpretationPass."""
    from src.agents.competitor_analysis import CompetitorAnalysisAgent
    from src.config_loader import load_config, get_agent_config

    cfg = dict(get_agent_config(load_config(), "competitor_analysis"))
    cfg["evidence_mode"] = True
    cfg["web_search"] = True
    cfg["use_brand_reference"] = True

    brand_ref = "### Target Audience\nparents age 30-45\n### Product Positioning\npremium"
    agent = CompetitorAnalysisAgent(cfg, FakeLLM(), brand_reference=brand_ref)
    agent._evidence_mode = True
    prompt = agent._build_system_prompt()

    # Evidence core is present
    assert "Competitor Analyst" in prompt
    # Brand reference is NOT present (hard isolation)
    assert "parents age 30-45" not in prompt
    assert "ข้อมูลแบรนด์อ้างอิง" not in prompt
    # Shared grounding policy IS present
    assert "ห้าม invent URL" in prompt
    assert "นโยบายข้อมูลต้นทาง" in prompt or "Grounding Policy" in prompt


def test_evidence_brand_interpretation_receives_finalized_evidence_only():
    """BrandInterpretationPass must receive finalized evidence and cannot
    modify the evidence collection."""
    from src.agents.competitor_evidence import (
        BrandInterpretationPass, ResearchResponse, CompetitorEvidence,
    )

    research = ResearchResponse(
        target_model="TestProduct",
        competitor_names=["CompA"],
        evidence=[CompetitorEvidence(
            competitor="CompA", field="price", claim="$100",
            url="https://example.com", geography="global",
        )],
        evidence_based_recommendations=[],
        strategic_hypotheses=[],
        uncertainty=[],
    )

    # BrandInterpretationPass.interpret must not modify research
    original_evidence = list(research.evidence)
    original_claims = [ev.claim for ev in research.evidence]

    interp = BrandInterpretationPass(llm=FakeLLM())
    # Even if interpret runs, it must not change research.evidence
    try:
        interp.interpret(research, brand_reference="premium brand")
    except Exception:
        pass  # LLM fake may not return valid JSON — that's OK

    # Evidence collection must be unchanged
    assert len(research.evidence) == len(original_evidence)
    for orig, now in zip(original_claims, [ev.claim for ev in research.evidence]):
        assert orig == now


def test_evidence_semantic_reviewer_does_not_receive_brand_reference():
    """SemanticEvidenceReviewer must not receive brand_reference."""
    from src.agents.competitor_evidence import SemanticEvidenceReviewer
    import inspect

    # Check the review method signature — it must not accept brand_reference
    sig = inspect.signature(SemanticEvidenceReviewer.review)
    params = set(sig.parameters.keys())
    assert "brand_reference" not in params
    assert "brand" not in params


# ---------------------------------------------------------------------------
# Finding 5: Product-profile delivery — prompt-capture for Agents 1-4
# ---------------------------------------------------------------------------

def test_agent1_receives_tone_adjustment_in_system_prompt(tmp_path, monkeypatch):
    """Agent 1 (product_spec) must receive tone_adjustment from the product
    profile in its system prompt via brand_context."""
    from src.orchestrator import Orchestrator
    from src.agents import base_agent

    monkeypatch.chdir(tmp_path)
    brand_dir = _make_brand_dir(tmp_path)

    _make_product_profile(tmp_path / "cache", "ProfiledProduct", {
        "tone_adjustment": "โทนเฉพาะสินค้านี้เป็นพิเศษ",
    })

    orch = Orchestrator(brand_dir=str(brand_dir))
    orch.bind_product("ProfiledProduct")

    from src.agents import ProductSpecAgent
    agent = orch._make_agent("product_spec", ProductSpecAgent, FakeLLM())
    prompt = agent._build_system_prompt()

    assert "โทนเฉพาะสินค้านี้เป็นพิเศษ" in prompt


def test_agent3_receives_audience_and_positioning(tmp_path, monkeypatch):
    """Agent 3 (campaign_strategy) must receive audience and positioning
    from the product profile."""
    from src.orchestrator import Orchestrator
    from src.agents import base_agent

    monkeypatch.chdir(tmp_path)
    brand_dir = _make_brand_dir(tmp_path)

    _make_product_profile(tmp_path / "cache", "ProfiledProduct", {
        "audience": {"primary": {"age": "30-50", "interest": "fitness"}},
        "differentiators": ["unique feature X"],
        "price_tier": "premium",
    })

    orch = Orchestrator(brand_dir=str(brand_dir))
    orch.bind_product("ProfiledProduct")

    captured = {}

    def _capture_run(self, user_prompt, **kwargs):
        captured["system"] = self._build_system_prompt()
        # Return long enough output to pass validation
        return (
            "## ราคาแนะนำ\n- pending\n"
            "## แคมเปญหลัก\n- Launch\n"
            "## แคมเปญเสริม\n- Influencer\n"
            "## ช่องทางโปรโมท\n- Facebook\n"
            "## KPI\n- Reach\n"
            "## งบประมาณ\n- pending\n"
            "## แหล่งอ้างอิง\n- none\n"
        )

    monkeypatch.setattr(base_agent.BaseAgent, "run", _capture_run)

    orch.run_campaign_strategy("test spec", "", llm=FakeLLM())

    system = captured.get("system", "")
    assert "30-50" in system or "fitness" in system
    assert "unique feature X" in system


# NOTE: Agent 2, Agent 4, and Agent 1 audience property-only tests have been
# replaced by tests/test_review_findings_v2.py which captures actual final
# production input (system prompt, user prompt, visual_style, media config)
# instead of asserting on object properties only.


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
