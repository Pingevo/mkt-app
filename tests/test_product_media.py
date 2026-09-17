"""Extracted embedded media — Product Detail thumbnails via the secure
brand/user-scoped route.

Contract (FREE-INGEST media acceptance):
  - source files stay listed as source files;
  - media extracted FROM a source document (xlsx/docx/pdf embedded images)
    is represented separately as media assets;
  - /api/product_info returns safe metadata only (names, never paths);
  - /api/product_media/{folder}/{name} serves the bytes through the same
    brand-context gate + contain_path containment as the source route;
  - only media recorded in the product's image_descriptions (with a
    ``source`` marker — extracted from a document) is listable/servable;
  - uploaded product photos (no ``source``) and unassigned source media
    are NOT duplicated as extracted media;
  - unrecorded files physically present in the extracted dir are not
    servable (allow-list, not directory browse);
  - cross-user/cross-brand access is rejected.

No model/provider calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def _client(monkeypatch, tmp_path):
    """Authed brand-scoped client — same pattern as test_staging_api."""
    import web_viewer
    import src.ingestion as ing
    monkeypatch.setattr(ing, "_make_llm", lambda: None)

    from tests.conftest import make_brand_client
    client, user_id, brand_id, brand_root = make_brand_client(
        web_viewer.app, tmp_path, monkeypatch)
    return client, brand_root, user_id, brand_id, tmp_path


def _seed_product_with_media(brand_root: Path, product: str = "K2",
                             user_id: str = "", brand_id: str = "",
                             project_root: Path | None = None):
    """Create a product: a real source file in data/ + an extracted image
    under cache/ + image_descriptions recorded in product.json."""
    from src import product_db
    from src.workspace_context import WorkspaceContext, set_workspace

    data_dir = brand_root / "data" / product
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "spec.txt").write_text("Battery | 680mAh", encoding="utf-8")

    media_dir = brand_root / "cache" / product / "extracted_images"
    media_dir.mkdir(parents=True, exist_ok=True)
    png = media_dir / "spec_img_001.png"
    png.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\xdac\xfc\xcf\xc0P\x0f\x00\x04\x85\x01\x80\x84\xa9\x8c!\x00\x00\x00\x00IEND\xaeB`\x82')

    token = set_workspace(WorkspaceContext.for_brand(
        user_id, brand_id, project_root))
    try:
        record = product_db.load(product) or {}
        record["files"] = [{"name": "spec.txt", "type": "text", "status": "ready"}]
        record["image_descriptions"] = [
            {"file": "spec_img_001.png", "path": str(png),
             "source": "spec.txt", "description": "embedded"},
            # Second recorded shape (staging/commit): file = source doc,
            # no ``source`` key — the real K2 record uses this form.
            {"path": str(png.parent / "spec_img_002.png"),
             "file": "spec.txt"},
        ]
        (png.parent / "spec_img_002.png").write_bytes(png.read_bytes())
        product_db.save(product, record)
    finally:
        set_workspace(None)
    return png


def _record(product: str, uid: str, bid: str, tmp: Path, mutate=None):
    """Read (and optionally mutate+save) product.json under a workspace."""
    from src import product_db
    from src.workspace_context import WorkspaceContext, set_workspace
    set_workspace(WorkspaceContext.for_brand(uid, bid, tmp))
    try:
        record = product_db.load(product) or {}
        if mutate:
            mutate(record)
            product_db.save(product, record)
        return record
    finally:
        set_workspace(None)


# ---------------------------------------------------------------------------
# /api/product_info → extracted_media metadata
# ---------------------------------------------------------------------------

def test_product_info_exposes_extracted_media_names_only(_client):
    """product_info lists extracted media as {name, source} — never paths."""
    client, brand_root, uid, bid, tmp = _client
    png = _seed_product_with_media(brand_root, "K2", uid, bid, tmp)

    r = client.get("/api/product_info/K2")
    assert r.status_code == 200
    body = r.json()
    media = body.get("extracted_media")
    assert media == [
        {"name": "spec_img_001.png", "source": "spec.txt"},
        {"name": "spec_img_002.png", "source": "spec.txt"},
    ], f"extracted media must be listed separately, got {media}"
    # no filesystem path material anywhere in the media entries
    for m in media:
        assert "path" not in m and "abs" not in str(m)


def test_source_file_and_media_are_distinct(_client):
    """The source file remains listed; the extracted image is not a
    duplicate of the source file itself."""
    client, brand_root, uid, bid, tmp = _client
    _seed_product_with_media(brand_root, "K2", uid, bid, tmp)
    body = client.get("/api/product_info/K2").json()
    names = [f["name"] for f in body["source_files"]]
    assert "spec.txt" in names
    assert "spec_img_001.png" not in names, (
        "extracted image must not duplicate as a source file")


def test_uploaded_image_not_listed_as_extracted(_client):
    """A direct product photo (image_descriptions WITHOUT a ``source``
    marker) is the product's own media — not 'extracted from a file'."""
    client, brand_root, uid, bid, tmp = _client
    _seed_product_with_media(brand_root, "K2", uid, bid, tmp)
    _record("K2", uid, bid, tmp, lambda r: r["image_descriptions"].append(
        {"file": "photo.png",
         "path": str(brand_root / "data" / "K2" / "photo.png"),
         "description": "user photo"}))
    media = client.get("/api/product_info/K2").json()["extracted_media"]
    assert [m["name"] for m in media] == ["spec_img_001.png", "spec_img_002.png"]


def test_unassigned_source_media_excluded(_client):
    client, brand_root, uid, bid, tmp = _client
    _seed_product_with_media(brand_root, "K2", uid, bid, tmp)
    _record("K2", uid, bid, tmp, lambda r: r["image_descriptions"].append(
        {"file": "unassigned.png", "path": str(brand_root / "cache" / "K2"
         / "extracted_images" / "unassigned.png"),
         "source": "spec.txt", "unassigned_source_media": True}))
    media = client.get("/api/product_info/K2").json()["extracted_media"]
    assert [m["name"] for m in media] == ["spec_img_001.png", "spec_img_002.png"]


def test_missing_file_not_listed(_client):
    """A recorded image whose bytes were deleted is not listed."""
    client, brand_root, uid, bid, tmp = _client
    png = _seed_product_with_media(brand_root, "K2", uid, bid, tmp)
    for f in png.parent.iterdir():
        f.unlink()
    media = client.get("/api/product_info/K2").json()["extracted_media"]
    assert media == []


# ---------------------------------------------------------------------------
# /api/product_media/{folder}/{filename} — secure byte route
# ---------------------------------------------------------------------------

def test_media_route_serves_recorded_image(_client):
    client, brand_root, uid, bid, tmp = _client
    png = _seed_product_with_media(brand_root, "K2", uid, bid, tmp)
    r = client.get("/api/product_media/K2/spec_img_001.png")
    assert r.status_code == 200
    assert r.content == png.read_bytes()
    assert r.headers["content-type"].startswith("image/")


def test_media_route_rejects_unrecorded_file(_client):
    """A file physically present but NOT recorded in image_descriptions
    is not servable — the route is an allow-list, not a directory browse."""
    client, brand_root, uid, bid, tmp = _client
    _seed_product_with_media(brand_root, "K2", uid, bid, tmp)
    media_dir = brand_root / "cache" / "K2" / "extracted_images"
    (media_dir / "sneaky.png").write_bytes(b"sneaky")
    r = client.get("/api/product_media/K2/sneaky.png")
    assert r.status_code == 404


def test_media_route_rejects_traversal_and_unknown(_client):
    client, brand_root, uid, bid, tmp = _client
    _seed_product_with_media(brand_root, "K2", uid, bid, tmp)
    for bad in ("..%2F..%2Fspec.txt", "nonexistent.png", ".."):
        r = client.get(f"/api/product_media/K2/{bad}")
        assert r.status_code in (404, 422), f"{bad} → {r.status_code}"


def test_media_route_rejects_non_image_suffix(_client):
    client, brand_root, uid, bid, tmp = _client
    _seed_product_with_media(brand_root, "K2", uid, bid, tmp)
    media_dir = brand_root / "cache" / "K2" / "extracted_images"
    (media_dir / "note.txt").write_text("not an image", encoding="utf-8")
    _record("K2", uid, bid, tmp, lambda r: r["image_descriptions"].append(
        {"file": "note.txt", "path": str(media_dir / "note.txt"),
         "source": "spec.txt"}))
    r = client.get("/api/product_media/K2/note.txt")
    assert r.status_code == 404


def test_media_route_cross_brand_blocked(_client, monkeypatch):
    """A different user's client cannot reach the product's media."""
    client, brand_root, uid, bid, tmp = _client
    _seed_product_with_media(brand_root, "K2", uid, bid, tmp)

    import web_viewer
    from tests.conftest import make_authed_client
    other, other_uid, other_ws = make_authed_client(web_viewer.app, tmp / "o",
                                                    monkeypatch)
    # other user's workspace has no K2 product at all
    r = other.get("/api/product_media/K2/spec_img_001.png")
    assert r.status_code in (400, 403, 404), (
        f"cross-user access must be rejected, got {r.status_code}")
