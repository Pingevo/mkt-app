"""Qualification server startup — runs the web server with C2 guards installed.

This is a qualification-only wrapper that:
1. Runs the pre-paid pricing gate (free GET, no paid calls)
2. Installs qualification guards (LLM cap, media one-shot, evidence capture)
3. Starts the web server (uvicorn) in the same process

All patches are process-level and vanish when the process exits.
No production files are modified.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qual_c2_launcher as qual

# Step 1: Pre-paid pricing gate
print("=" * 60)
print("C2 QUALIFICATION LAUNCHER")
print("=" * 60)
ok, evidence, msg = qual.pre_paid_pricing_gate()
print(f"[GATE] {msg}")
print(f"[GATE] Evidence: {evidence}")
if not ok:
    print("[GATE] FAILED — aborting before any paid call")
    print("[GATE] paid_llm_calls=0 image_submissions=0 video_submissions=0")
    sys.exit(1)

# Step 2: Install guards
qual.QUAL_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
for f in qual.QUAL_EVIDENCE_DIR.glob("*"):
    f.unlink()
qual.install_qualification_guards()
qual.install_media_one_shot()
qual.install_catalog_capture()

# Step 3: Pre-run estimate
estimate = qual.pre_run_price_estimate()
print(f"[ESTIMATE] {estimate}")

print(f"[LAUNCHER] LLM cap:   {qual.QUAL_C2_MAX_LLM_CALLS}")
print(f"[LAUNCHER] Image cap: {qual.QUAL_C2_MAX_IMAGE}")
print(f"[LAUNCHER] Video cap: {qual.QUAL_C2_MAX_VIDEO}")
print("[LAUNCHER] Guards installed. Starting web server...")

# Step 4: Start web server
import uvicorn
from web_viewer import app

port = int(os.environ.get("VIEWER_PORT", "8778"))
uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
