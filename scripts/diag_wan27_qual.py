#!/usr/bin/env python3
"""Diagnostic harness — Wan 2.7 reference-to-video qualification.

Exactly ONE paid submission to alibaba/wan-2.7 via the canonical production
media seam (``media_gen.generate_video``).  Uses input_references ONLY
(no frame_images, no intermediate image).

Accounting: every real provider call goes through ``media_gen.generate_video``
which performs AI Usage Hub accounting automatically (records actual provider
cost, emits exactly one Hub event, survives Hub failure).  No direct
OpenRouter access, no accounting bypass.

Run:
    set -a; source .env; set +a; python3 scripts/diag_wan27_qual.py
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

# ── Source inputs (identical to prior phases) ─────────────────────────
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

# ── Settings (minimum cost exercising real ref-to-video) ─────────────
MODEL_ID = "alibaba/wan-2.7"
DURATION = 2
RESOLUTION = "720p"
ASPECT_RATIO = "16:9"

# ── Output dir ────────────────────────────────────────────────────────
TS = datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_DIR = Path(__file__).resolve().parent.parent / "output" / f"diag_wan27_{TS}"
OUT_DIR.mkdir(parents=True, exist_ok=True)

report: dict = {
    "timestamp": TS,
    "model_id": MODEL_ID,
    "seam": "media_gen.generate_video",
    "prompt": PROMPT,
    "duration": DURATION,
    "resolution": RESOLUTION,
    "aspect_ratio": ASPECT_RATIO,
    "references": REFS,
    "frame_images_used": False,
    "input_references_used": True,
    "projected_cost": 0.20,
}

print("=" * 70)
print("WAN 2.7 REFERENCE-TO-VIDEO QUALIFICATION")
print(f"  Model: {MODEL_ID}")
print(f"  Seam: media_gen.generate_video (AI Usage accounting enabled)")
print(f"  Output: {OUT_DIR}")
print(f"  Duration: {DURATION}s | Resolution: {RESOLUTION}")
print(f"  References: {len(REFS)} (product + child + logo)")
print(f"  frame_images: NOT USED | input_references: YES")
print(f"  Projected cost: ${report['projected_cost']:.2f}")
print("=" * 70)
print()
print("Reference fingerprints:")
for r in REFS:
    print(f"  {r['label']}: sha256={r['sha256'][:16]}... size={r['size_bytes']}")
print()

# Save payload metadata (without base64 data for readability)
payload_redacted = {
    "model": MODEL_ID, "prompt": PROMPT, "aspect_ratio": ASPECT_RATIO,
    "resolution": RESOLUTION, "duration": DURATION,
    "input_references": f"[{len(REFS)} data URLs — built by media_gen]",
}
(OUT_DIR / "payload.json").write_text(
    json.dumps(payload_redacted, ensure_ascii=False, indent=2), encoding="utf-8")

# ── Submit via canonical seam ─────────────────────────────────────────
ref_paths = [str(PRODUCT_IMG), str(CHILD_ASSET), str(LOGO_ASSET)]
vid_path = OUT_DIR / "video_wan27.mp4"

t0 = time.time()
print(f"Submitting via media_gen.generate_video(model={MODEL_ID})...")
result = media_gen.generate_video(
    PROMPT, vid_path,
    model=MODEL_ID,
    duration=DURATION,
    aspect_ratio=ASPECT_RATIO,
    resolution=RESOLUTION,
    input_references=ref_paths,
    poll_interval=5.0,
    max_wait=600.0,
    on_status=lambda s: print(f"  status: {s}"),
)
elapsed = time.time() - t0

report["elapsed_s"] = round(elapsed, 1)
report["ok"] = bool(result.get("ok"))
report["result"] = {k: v for k, v in result.items() if k != "prompt"}

if result.get("ok"):
    sz = vid_path.stat().st_size
    print(f"  SAVED: {vid_path} ({sz} bytes)")
    report["status"] = "completed"
    report["artifact_path"] = str(vid_path)
    report["artifact_size"] = sz
    report["video_url"] = result.get("url")
    report["job_id"] = result.get("job_id")
    report["warnings"] = result.get("warnings", [])
    print(f"  Job ID: {result.get('job_id')}")
    # Actual cost is recorded by media_gen._log_media_usage → AI Usage Hub.
    # Read it back from the local usage log (last media_gen video entry).
    usage_log = Path(__file__).resolve().parent.parent / "logs" / "llm_usage.jsonl"
    if usage_log.exists():
        media_lines = [l for l in usage_log.read_text(encoding="utf-8").strip().splitlines()
                       if "media_gen.generate_video" in l and l.get("videos.generate") if False]
        # simpler: parse last line matching this job_id
        for line in reversed(usage_log.read_text(encoding="utf-8").strip().splitlines()):
            try:
                e = json.loads(line)
                if e.get("request_id") == result.get("job_id") and e.get("operation") == "videos.generate":
                    report["usage"] = e.get("raw_usage")
                    report["cost_usd"] = e.get("cost_usd")
                    break
            except Exception:
                pass
    print(f"  Actual cost (from AI Usage accounting): ${report.get('cost_usd', 'N/A')}")
else:
    print(f"  FAILED: {result.get('error')}")
    report["status"] = "failed"
    report["error"] = result.get("error", "")
    report["http_status"] = result.get("http_status")
    report["error_type"] = result.get("error_type")
    report["error_code"] = result.get("error_code")
    report["job_id"] = result.get("job_id")

(OUT_DIR / "diag_report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nReport saved: {OUT_DIR / 'diag_report.json'}")
print(f"Status: {report.get('status')}")
print(f"Cost: ${report.get('cost_usd', 'N/A')}")
print(f"\nAI Usage accounting was performed by media_gen.generate_video.")
