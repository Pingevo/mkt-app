"""Regression tests for URL-PRODUCT-ISOLATION-REMEDIATION-01.

Proven by URL-PRODUCT-ISOLATION-AUDIT-01 on the real K2/K3/K5/K9 records:
  - every split/staged product inherited ALL source-page images (hard-linked
    files + wholesale image_descriptions) instead of scoped media;
  - split products kept image paths pointing at the deleted temp folder;
  - split products never got derived_facts (no _generate_metadata_summary);
  - metadata.image_count double-counted the same image (16 vs 8 unique).

Covers BOTH generic materialization routes:
  - staged commit:      staging.create_batch → commit_batch → _write_product_record
  - ingest split:       ingest_product → _try_segment_and_split
                        → _materialize_split_products

Contract under test: when a source cannot deterministically associate an
image with a product (no page/position provenance), the media stays
persisted as unassigned_source_media source evidence — never returned by
get_product_image_paths as owned product media.
"""
import json
from pathlib import Path

import pytest


# ------------------------------------------------------------------
#  Deterministic fakes / fixtures
# ------------------------------------------------------------------

_MARKERS = ["ALPHA-UNIQ-7X", "BRAVO-UNIQ-9Q", "CHARLIE-UNIQ-3Z", "DELTA-UNIQ-5W"]
_NAMES = ["Prod Alpha", "Prod Bravo", "Prod Charlie", "Prod Delta"]
_KEYS = ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]


def _catalog_text() -> str:
    """4-product source page — shared header/footer + unique marker lines."""
    lines = [
        "SHARED CATALOG HEADER",          # 1 — common
        "MOQ: 100",                       # 2 — common
        "Product Alpha section",          # 3
        "Spec: ALPHA-UNIQ-7X",            # 4
        "Price: 100",                     # 5
        "Product Bravo section",          # 6
        "Spec: BRAVO-UNIQ-9Q",            # 7
        "Price: 200",                     # 8
        "Product Charlie section",        # 9
        "Spec: CHARLIE-UNIQ-3Z",          # 10
        "Price: 300",                     # 11
        "Product Delta section",          # 12
        "Spec: DELTA-UNIQ-5W",            # 13
        "Price: 400",                     # 14
        "SHARED FOOTER",                  # 15 — common
    ]
    return "\n".join(lines)


def _segments(file_name: str = "source_page.txt") -> list[dict]:
    common = [
        {"file": file_name, "line_start": 1, "line_end": 2},
        {"file": file_name, "line_start": 15, "line_end": 15},
    ]
    blocks = [(3, 5), (6, 8), (9, 11), (12, 14)]
    lines = _catalog_text().split("\n")
    out = []
    for key, name, (s, e) in zip(_KEYS, _NAMES, blocks):
        # scoped text = what _slice_refs produces: common + product block
        text = "\n".join(lines[0:2] + lines[s - 1:e] + lines[14:15])
        out.append({
            "product_key": key,
            "suggested_name": name,
            "category": "gadget",
            "summary": f"{name} summary",
            "text": text,
            "source_refs": [{"file": file_name, "line_start": s, "line_end": e}],
            "common_refs": common,
        })
    return out


def _seg_response() -> dict:
    return {"products": _segments()}


class _FakeLLM:
    """Deterministic LLM double — segmentation JSON + per-product facts.

    The metadata_summary responder scans the prompt for the unique marker
    and echoes it as this product's derived fact — proving extraction ran
    on the scoped per-product text.
    """

    def __init__(self, seg_response=None, fail_summary=False):
        self._seg = seg_response or _seg_response()
        self._fail_summary = fail_summary
        self.calls: list[dict] = []

    def chat(self, messages, *, response_format=None, source="", **kwargs):
        prompt = " ".join(str(m.get("content", "")) for m in messages)
        self.calls.append({"source": source, "prompt_head": prompt[:80]})
        if "segment" in (source or ""):
            return json.dumps(self._seg, ensure_ascii=False)
        if "metadata_summary" in (source or ""):
            if self._fail_summary:
                raise RuntimeError("deterministic summary failure")
            marker = next((m for m in _MARKERS if m in prompt), "NONE")
            return json.dumps({
                "summary": f"summary for {marker}",
                "category": "gadget",
                "derived_facts": {
                    "spec": {"label": "สเปก", "value": marker},
                },
            }, ensure_ascii=False)
        # product profile call → valid positioning JSON
        return json.dumps({
            "audience": {"primary": {"age": "adult", "role": "user"}},
            "competitors": [],
            "differentiators": [],
            "use_cases": [],
            "price_tier": "mid",
            "tone_adjustment": "",
            "visual_override": {},
        })

    def close(self):
        pass


@pytest.fixture
def _ws(monkeypatch, tmp_path):
    """Tmp project root for product_db/staging/ingestion — no provider calls."""
    import src.config_loader as config_loader
    import src.ingestion as ingestion
    import src.product_db as product_db
    import src.staging as staging

    monkeypatch.setattr(config_loader, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(product_db, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(staging, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(ingestion, "_project_root", lambda: tmp_path)

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    real_cfg = Path(__file__).resolve().parent.parent / "config" / "ingestion.yaml"
    if real_cfg.exists():
        (cfg_dir / "ingestion.yaml").write_bytes(real_cfg.read_bytes())
    else:
        (cfg_dir / "ingestion.yaml").write_text(
            "supported_formats:\n  text: ['.txt', '.md', '.pdf']\n"
            "  image: ['.jpg', '.jpeg', '.png', '.webp']\n"
            "model: test/model\ntemperature: 0.3\ntimeout_seconds: 30\n",
            encoding="utf-8")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return tmp_path


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_IMG_FILES = [("page_img_001.png", _PNG), ("page_img_002.png", _PNG + b"x")]


def _assert_isolated(tmp_path, product_db):
    """Per-product isolation + media honesty for the 4-product fixture."""
    for i, name in enumerate(_NAMES):
        own = _MARKERS[i]
        foreign = [m for j, m in enumerate(_MARKERS) if j != i]
        rec = product_db.load(name)
        assert rec.get("status") == "ready", f"{name}: {rec.get('status')}"

        # textual scoping — own marker only
        raw = rec.get("raw_text", "")
        assert own in raw, f"{name} missing own marker"
        for f in foreign:
            assert f not in raw, f"{name} raw_text bleeds {f}"

        # derived facts — produced from scoped text, own marker only
        df = rec.get("derived_facts") or {}
        assert df, f"{name} has no derived_facts"
        values = " ".join(str(v.get("value", "")) for v in df.values())
        assert own in values, f"{name} derived_facts missing own: {df}"
        for f in foreign:
            assert f not in values, f"{name} derived_facts bleeds {f}"

        # media honesty — every entry without deterministic page provenance
        # must be unassigned; every persisted path must be a live file inside
        # THIS product's own data dir (never the deleted temp/source dir)
        imgs = rec.get("image_descriptions", [])
        assert len(imgs) == len(_IMG_FILES), f"{name} images: {imgs}"
        for img in imgs:
            if img.get("page") is None:
                assert img.get("unassigned_source_media") is True, \
                    f"{name} unlabeled image: {img}"
            p = img.get("path")
            if p is not None:
                assert Path(p).exists(), f"{name} dead path: {p}"
                assert str(tmp_path / "data" / name) in p, \
                    f"{name} path outside own dir: {p}"

        # product-facing accessor must not claim unassigned media
        assert product_db.get_product_image_paths(name) == []

        # image_count = unique associated media, never double-counted
        assert rec["metadata"]["image_count"] == len(_IMG_FILES), \
            f"{name} image_count: {rec['metadata']}"


# ------------------------------------------------------------------
#  Route 1: ingest split path (save files → ingest_product → split)
# ------------------------------------------------------------------

class TestSplitRoute:
    def test_split_media_and_facts_isolation(self, _ws, tmp_path, monkeypatch):
        import src.ingestion as ingestion
        import src.product_db as product_db

        temp_name = "source-page"
        data_dir = tmp_path / "data" / temp_name
        data_dir.mkdir(parents=True)
        (data_dir / "source_page.txt").write_text(_catalog_text(), encoding="utf-8")
        for fname, content in _IMG_FILES:
            (data_dir / fname).write_bytes(content)

        llm = _FakeLLM()
        result = ingestion.ingest_product(temp_name, force=False, is_new_upload=True, llm=llm)

        assert set(result.get("split_products", [])) == set(_NAMES)
        assert not data_dir.exists(), "temp product dir must be deleted"
        _assert_isolated(tmp_path, product_db)

    def test_split_enrichment_failure_is_truthful(self, _ws, tmp_path, monkeypatch):
        """AI failure on the explicit split path must not demote the
        materialized products: ready + ai_error, never silent and never
        no_usable_data (FREE-IMPORT contract)."""
        import src.ingestion as ingestion
        import src.product_db as product_db

        temp_name = "source-page"
        data_dir = tmp_path / "data" / temp_name
        data_dir.mkdir(parents=True)
        (data_dir / "source_page.txt").write_text(_catalog_text(), encoding="utf-8")

        llm = _FakeLLM(fail_summary=True)
        ingestion.ingest_product(temp_name, force=False, is_new_upload=True, llm=llm)

        for name in _NAMES:
            rec = product_db.load(name)
            assert rec.get("status") == "ready", \
                f"{name} demoted by AI failure: {rec.get('status')}"
            assert rec.get("ai_error"), f"{name} missing ai_error"
            assert not rec.get("derived_facts"), \
                f"{name} derived_facts on failed extraction"


# ------------------------------------------------------------------
#  Route 2: staged commit path (create_batch → commit_batch)
# ------------------------------------------------------------------

class TestStagedRoute:
    def test_staged_media_and_facts_isolation(self, _ws, tmp_path):
        import src.product_db as product_db
        import src.staging as staging

        files = [("source_page.txt", _catalog_text().encode("utf-8"))]
        files += list(_IMG_FILES)
        batch_id = staging.create_batch(files)
        batch = staging._load_batch(batch_id)
        batch["status"] = "segmented"
        batch["segments"] = _segments()
        batch["seg_mode"] = "multi"
        staging._save_batch(batch_id, batch)

        choices = [
            {"segment_index": i, "action": "create", "name": n}
            for i, n in enumerate(_NAMES)
        ]
        result = staging.commit_batch(batch_id, choices, llm=_FakeLLM())
        assert set(result["created"]) == set(_NAMES)
        _assert_isolated(tmp_path, product_db)

    def test_staged_enrichment_failure_is_truthful(self, _ws, tmp_path):
        """Explicit-llm commit with failing AI: products stay ready
        (deterministic materialization succeeded), ai_error recorded."""
        import src.product_db as product_db
        import src.staging as staging

        batch_id = staging.create_batch([("source_page.txt", _catalog_text().encode())])
        batch = staging._load_batch(batch_id)
        batch["status"] = "segmented"
        batch["segments"] = _segments()
        batch["seg_mode"] = "multi"
        staging._save_batch(batch_id, batch)

        choices = [
            {"segment_index": i, "action": "create", "name": n}
            for i, n in enumerate(_NAMES)
        ]
        staging.commit_batch(batch_id, choices, llm=_FakeLLM(fail_summary=True))
        for name in _NAMES:
            rec = product_db.load(name)
            assert rec.get("status") == "ready"
            assert rec.get("ai_error")
