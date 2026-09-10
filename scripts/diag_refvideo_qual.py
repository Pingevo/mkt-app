#!/usr/bin/env python3
"""Diagnostic harness — OpenRouter reference-to-video A/B qualification.

Tests bytedance/seedance-2.0 and bytedance/seedance-2.5 using input_references
ONLY (no frame_images, no intermediate image). Exactly 1 submission per model.

Uses the canonical production media seam (``media_gen.generate_video``) so
every real provider call is accounted to the AI Usage Hub automatically.
No direct OpenRouter access, no accounting bypass.

Run:
    set -a; source .env; set +a; python3 scripts/diag_refvideo_qual.py
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import media_gen
from src.openrouter_gateway import get_api_key as _gate_get_api_key

API_KEY = _gate_get_api_key() or ""
if not API_KEY:
    print("ERROR: OPENROUTER_API_KEY not set. Run: set -a; source .env; set +a")
    sys.exit(1)

# ── Source inputs (identical for both models) ─────────────────────────
PRODUCT_IMG = Path(__file__).resolve().parent.parent / "cache" / "Lagenio K5" / "extracted_images" / "xlsx_img_001.png"
CHILD_ASSET = Path(__file__).resolve().parent.parent / "brand" / "assets" / "images.jpeg"   # a_0007
LOGO_ASSET  = Path(__file__).resolve().parent.parent / "brand" / "assets" / "images.png"     # a_0008

assert PRODUCT_IMG.exists(), f"product image missing: {PRODUCT_IMG}"
assert CHILD_ASSET.exists(), f"child asset missing: {CHILD_ASSET}"
assert LOGO_ASSET.exists(),  f"logo asset missing: {LOGO_ASSET}"

def _fingerprint(p: Path) -> dict:
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    return {"path": str(p), "sha256": sha, "size_bytes": p.stat().st_size}

REFS = [
    {"label": "Reference 1 (product)", "asset_id": "", **_fingerprint(PRODUCT_IMG)},
    {"label": "Reference 2 (child/person a_0007)", "asset_id": "a_0007", **_fingerprint(CHILD_ASSET)},
    {"label": "Reference 3 (logo a_0008)", "asset_id": "a_0008", **_fingerprint(LOGO_ASSET)},
]

# ── Identical safe commercial prompt ──────────────────────────────────
PROMPT = (
    "A warm, natural advertising video of a happy child wearing a children's "
    "smartwatch on their wrist. The child's appearance matches the person shown "
    "in the reference image. The smartwatch retains the same design as the product "
    "reference — same shape, case, strap, and color. The child smiles and naturally "
    "shows the watch in a bright, cozy home setting with soft daylight. Simple slow "
    "camera motion. No unsafe or sensitive activity."
)

# ── Settings (lowest cost that exercises real ref-to-video) ──────────
DURATION = 4
RESOLUTION = "480p"
ASPECT_RATIO = "16:9"

MODELS = ["bytedance/seedance-2.0", "bytedance/seedance-2.5"]

# ── Output dir ────────────────────────────────────────────────────────
TS = datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_DIR = Path(__file__).resolve().parent.parent / "output" / f"diag_refvideo_{TS}"
OUT_DIR.mkdir(parents=True, exist_ok=True)

report: dict = {
    "timestamp": TS,
    "seam": "media_gen.generate_video",
    "prompt": PROMPT,
    "duration": DURATION,
    "resolution": RESOLUTION,
    "aspect_ratio": ASPECT_RATIO,
    "references": REFS,
    "frame_images_used": False,
    "input_references_used": True,
    "models": {},
}

print("=" * 70)
print("DIAGNOSTIC REFERENCE-TO-VIDEO A/B QUALIFICATION")
print(f"  Seam: media_gen.generate_video (AI Usage accounting enabled)")
print(f"  Output: {OUT_DIR}")
print(f"  Duration: {DURATION}s | Resolution: {RESOLUTION}")
print(f"  References: {len(REFS)} (product + child + logo)")
print(f"  frame_images: NOT USED | input_references: YES")
print("=" * 70)
print()
print("Reference fingerprints:")
for r in REFS:
    print(f"  {r['label']}: sha256={r['sha256'][:16]}... size={r['size_bytes']}")
print()

ref_paths = [str(PRODUCT_IMG), str(CHILD_ASSET), str(LOGO_ASSET)]
usage_log = Path(__file__).resolve().parent.parent / "logs" / "llm_usage.jsonl"

def _read_cost_for_job(job_id):
    if not usage_log.exists():
        return None, None
    for line in reversed(usage_log.read_text(encoding="utf-8").strip().splitlines()):
        try:
            e = json.loads(line)
            if e.get("request_id") == job_id and e.get("operation") == "videos.generate":
                return e.get("raw_usage"), e.get("cost_usd")
        except Exception:
            pass
    return None, None

# ── Run each model via the canonical seam ─────────────────────────────
for model_id in MODELS:
    print(f"\n{'='*70}")
    print(f"MODEL: {model_id}")
    print(f"{'='*70}")

    payload_redacted = {
        "model": model_id, "prompt": PROMPT, "aspect_ratio": ASPECT_RATIO,
        "resolution": RESOLUTION, "duration": DURATION,
        "input_references": f"[{len(REFS)} data URLs — built by media_gen]",
    }
    (OUT_DIR / f"payload_{model_id.replace('/', '_')}.json").write_text(
        json.dumps(payload_redacted, ensure_ascii=False, indent=2), encoding="utf-8")

    vid_path = OUT_DIR / f"video_{model_id.replace('/', '_')}.mp4"
    model_report: dict = {
        "model_id": model_id,
        "seam": "media_gen.generate_video",
        "input_references_count": len(REFS),
        "frame_images_used": False,
        "input_references_used": True,
    }

    t0 = time.time()
    try:
        print(f"  Submitting via media_gen.generate_video(model={model_id})...")
        result = media_gen.generate_video(
            PROMPT, vid_path,
            model=model_id,
            duration=DURATION,
            aspect_ratio=ASPECT_RATIO,
            resolution=RESOLUTION,
            input_references=ref_paths,
            poll_interval=5.0,
            max_wait=600.0,
            on_status=lambda s: print(f"    status: {s}"),
        )
        elapsed = time.time() - t0
        model_report["elapsed_s"] = round(elapsed, 1)
        model_report["ok"] = bool(result.get("ok"))

        if result.get("ok"):
            sz = vid_path.stat().st_size
            print(f"  SAVED: {vid_path} ({sz} bytes)")
            model_report["status"] = "completed"
            model_report["artifact_path"] = str(vid_path)
            model_report["artifact_size"] = sz
            model_report["video_url"] = result.get("url")
            model_report["job_id"] = result.get("job_id")
            model_report["warnings"] = result.get("warnings", [])
            usage, cost = _read_cost_for_job(result.get("job_id"))
            model_report["usage"] = usage
            model_report["cost_usd"] = cost
            print(f"  Job ID: {result.get('job_id')}")
            print(f"  Actual cost (from AI Usage accounting): ${cost}")
        else:
            print(f"  FAILED: {result.get('error')}")
            model_report["status"] = "failed"
            model_report["error"] = result.get("error", "")
            model_report["http_status"] = result.get("http_status")
            model_report["error_type"] = result.get("error_type")
            model_report["error_code"] = result.get("error_code")
            model_report["job_id"] = result.get("job_id")
            usage, cost = _read_cost_for_job(result.get("job_id"))
            model_report["usage"] = usage
            model_report["cost_usd"] = cost

    except Exception as e:
        print(f"  ERROR: {e}")
        model_report["status"] = "error"
        model_report["error"] = str(e)
        model_report["elapsed_s"] = round(time.time() - t0, 1)

    report["models"][model_id] = model_report
    (OUT_DIR / "diag_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Report saved.")

# ── Summary ───────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
for model_id, mr in report["models"].items():
    print(f"  {model_id}: status={mr.get('status')} cost=${mr.get('cost_usd', 'N/A')} "
          f"artifact={mr.get('artifact_path', 'none')}")
print(f"\nFull report: {OUT_DIR / 'diag_report.json'}")
print(f"\nAI Usage accounting was performed by media_gen.generate_video for every model.")
