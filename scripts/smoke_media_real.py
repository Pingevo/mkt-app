#!/usr/bin/env python3
"""Real grounded media smoke test — exercises the REAL provider pipeline.

Budget ceiling: <= $0.20 (image ~$0.04 + video ~$0.16 for 4s @ 720p).

Grounded inputs (NOT bare text):
  - Real product image: brand/assets/images.jpeg (a real watch product photo)
  - Brand visual suffix: brand/visual.json (keywords, colors, image_style)
  - input_references: the real image, converted to base64 data URL by media_gen

Outputs:
  - output/smoke/image_1.png  (image gen result)
  - output/smoke/video_1.mp4   (video gen result)

Reports exact OpenRouter spend from the API usage field.

Run:
    set -a; source .env; set +a; python3 scripts/smoke_media_real.py
"""
import json
import sys
import time
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

from src.openrouter_gateway import get_api_key as _gate_get_api_key

API_KEY = _gate_get_api_key() or ""
if not API_KEY:
    print("ERROR: OPENROUTER_API_KEY not set in environment. Run: set -a; source .env; set +a")
    sys.exit(1)

from src import media_gen

# --- Grounded inputs ---
PRODUCT_IMAGE = Path(__file__).resolve().parent.parent / "brand" / "assets" / "images.jpeg"
VISUAL_PATH = Path(__file__).resolve().parent.parent / "brand" / "visual.json"

assert PRODUCT_IMAGE.exists(), f"product image not found: {PRODUCT_IMAGE}"
assert VISUAL_PATH.exists(), f"visual.json not found: {VISUAL_PATH}"

visual = json.loads(VISUAL_PATH.read_text(encoding="utf-8"))

OUT_DIR = Path(__file__).resolve().parent.parent / "output" / "smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)

print("=" * 60)
print("REAL GROUNDED MEDIA SMOKE TEST")
print("=" * 60)
print(f"Product image : {PRODUCT_IMAGE} ({PRODUCT_IMAGE.stat().st_size} bytes)")
print(f"Visual suffix : keywords={visual.get('keywords', [])[:3]}...")
print(f"Visual colors : primary={visual.get('colors', {}).get('primary', '')[:40]}...")
print(f"Output dir    : {OUT_DIR}")
print()

total_cost = 0.0

# ---------------------------------------------------------------------------
# 1. IMAGE — google/gemini-3.1-flash-image with real input_references + visual
# ---------------------------------------------------------------------------
print("[1/2] IMAGE: google/gemini-3.1-flash-image")
img_out = OUT_DIR / "image_1.png"
t0 = time.time()
img_result = media_gen.generate_image(
    "a kids smartwatch product photo on a clean light background, studio lighting",
    img_out,
    input_references=[str(PRODUCT_IMAGE)],
    visual=visual,
    aspect_ratio="16:9",
)
img_elapsed = time.time() - t0
print(f"  ok={img_result.get('ok')}")
print(f"  elapsed={img_elapsed:.1f}s")
if img_result.get("ok"):
    print(f"  path={img_result.get('path')}")
    print(f"  size={Path(img_result['path']).stat().st_size} bytes")
    print(f"  warnings={img_result.get('warnings', [])}")
else:
    print(f"  ERROR: {img_result.get('error')}")
    print(f"  http_status={img_result.get('http_status')}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 2. VIDEO — bytedance/seedance-2.0-fast with real input_references, 4s 720p
# ---------------------------------------------------------------------------
print()
print("[2/2] VIDEO: bytedance/seedance-2.0-fast (4s, 720p)")
vid_out = OUT_DIR / "video_1.mp4"
t0 = time.time()
vid_result = media_gen.generate_video(
    "a cinematic 4-second product shot of a kids smartwatch, slow dolly-in, soft natural light",
    vid_out,
    input_references=[str(PRODUCT_IMAGE)],
    visual=visual,
    duration=4,
    aspect_ratio="16:9",
    resolution="720p",
    poll_interval=5.0,
    max_wait=600.0,
    on_status=lambda s: print(f"    status: {s}"),
)
vid_elapsed = time.time() - t0
print(f"  ok={vid_result.get('ok')}")
print(f"  elapsed={vid_elapsed:.1f}s")
if vid_result.get("ok"):
    print(f"  path={vid_result.get('path')}")
    print(f"  size={Path(vid_result['path']).stat().st_size} bytes")
    print(f"  url={vid_result.get('url')}")
    print(f"  warnings={vid_result.get('warnings', [])}")
else:
    print(f"  ERROR: {vid_result.get('error')}")
    print(f"  http_status={vid_result.get('http_status')}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 3. Spend from usage log (last 2 media entries)
# ---------------------------------------------------------------------------
print()
print("=" * 60)
print("SPEND REPORT")
print("=" * 60)
usage_log = Path(__file__).resolve().parent.parent / "logs" / "llm_usage.jsonl"
if usage_log.exists():
    lines = usage_log.read_text(encoding="utf-8").strip().splitlines()
    media_lines = [l for l in lines if "media_gen" in l]
    recent = media_lines[-2:] if len(media_lines) >= 2 else media_lines
    for line in recent:
        try:
            entry = json.loads(line)
            cost = entry.get("cost_usd")
            op = entry.get("operation")
            status = entry.get("status")
            print(f"  {op}: status={status}, cost_usd={cost}")
            if cost is not None:
                total_cost += float(cost)
        except Exception:
            pass
print(f"  TOTAL SPEND: ${total_cost:.4f}")
print(f"  BUDGET CEILING: $0.2000")
print(f"  WITHIN BUDGET: {'YES' if total_cost <= 0.20 else 'NO — OVER BUDGET'}")
print()

# ---------------------------------------------------------------------------
# 4. File verification
# ---------------------------------------------------------------------------
print("=" * 60)
print("OUTPUT FILE VERIFICATION")
print("=" * 60)
for f in (img_out, vid_out):
    if f.exists():
        head = f.read_bytes()[:8]
        print(f"  {f.name}: {f.stat().st_size} bytes, header={head!r}")
    else:
        print(f"  {f.name}: MISSING")
print()
print("SMOKE TEST COMPLETE")
