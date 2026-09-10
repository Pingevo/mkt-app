#!/usr/bin/env python3
"""Product-Level Media UAT — exercises the real MKTApp path:

  Quick Brief → Agent 4 (content_creator) → product + brand context
  → numbered reference catalog → Agent 4 media prompts
  → production compose_media_input → real image/video provider
  → persisted artifacts.

Frozen Checkpoint C: 5cc4724
No production code modified.
Media retry wrappers BYPASSED — exactly 1 image + 1 video generation max.

Run:
    set -a; source .env; set +a; python3 scripts/uat_media_product.py
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.openrouter_gateway import get_api_key as _gate_get_api_key

API_KEY = _gate_get_api_key() or ""
if not API_KEY:
    print("ERROR: OPENROUTER_API_KEY not set. Run: set -a; source .env; set +a")
    sys.exit(1)

from src.orchestrator import Orchestrator
from src import media_gen
from src import asset_library
from src.content_schema import render_posts_to_markdown

# Import production compose_media_input from web_viewer (the shared seam)
from web_viewer import compose_media_input

# ── Configuration ──────────────────────────────────────────────
PRODUCT_ID = "Lagenio K5"
SELECTED_ASSET_ID = "a_0008"
CATALOG_ASSET_IDS = ["a_0008"]

QUICK_BRIEF = (
    "สร้างคอนเทนต์โปรโมท Lagenio K5 สมาร์ทวอตช์เด็ก โฟกัสที่ฟีเจอร์ "
    "GPS Tracking และ Family Group Chat สำหรับคุณแม่ที่กังวลเรื่องความปลอดภัยของลูก "
    "อยากให้ภาพดูอบอุ่น น่าไว้วางใจ ใช้โทนสีของแบรนด์ "
    "และใช้โลโก้ LAGENIO จาก Brand Asset ที่ให้มา สร้างทั้ง prompt รูปและวิดีโอ"
)

# ── Unique output directory ──────────────────────────────────
TS = datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_DIR = Path(__file__).resolve().parent.parent / "output" / f"uat_media_{TS}"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Record usage log offset for paid-call accounting ─────────
usage_log = Path(__file__).resolve().parent.parent / "logs" / "llm_usage.jsonl"
usage_offset_before = 0
if usage_log.exists():
    usage_offset_before = sum(1 for _ in usage_log.open(encoding="utf-8"))

report: dict = {
    "timestamp": TS,
    "checkpoint": "5cc4724",
    "product_id": PRODUCT_ID,
    "asset_id": SELECTED_ASSET_ID,
    "quick_brief": QUICK_BRIEF,
    "output_dir": str(OUT_DIR),
    "usage_offset_before": usage_offset_before,
}

print("=" * 70)
print("PRODUCT-LEVEL MEDIA UAT")
print(f"  Checkpoint C: 5cc4724 (frozen)")
print(f"  Product: {PRODUCT_ID}")
print(f"  Asset: {SELECTED_ASSET_ID}")
print(f"  Output: {OUT_DIR}")
print("=" * 70)

# ── Build asset summary for Agent 4 (mirrors _select_assets_for_content) ──
asset_rec = asset_library.get_asset(SELECTED_ASSET_ID)
if not asset_rec:
    print(f"ERROR: Asset {SELECTED_ASSET_ID} not found in library")
    sys.exit(1)

tags_str = ", ".join(asset_rec.get("tags", [])) if asset_rec.get("tags") else "-"
asset_summary = (
    f"เหตุผลที่เลือก: preselected for controlled UAT\n"
    f"- ID: {asset_rec['id']} | {asset_rec.get('file', '')} | "
    f"type: {asset_rec.get('type', '')} | subject: {asset_rec.get('subject', '')} | "
    f"tags: {tags_str} | คำบรรยาย: {asset_rec.get('description', '')}"
)

# ── Step 1: Run Agent 4 through real production path ─────────
print("\n[1] Running Agent 4 (content_creator) via _run_content_creator_raw...")
orch = Orchestrator(product_id=PRODUCT_ID)
orch._selected_asset_ids = list(CATALOG_ASSET_IDS)

# Capture the reference catalog Agent 4 will see
product_image_paths = orch._get_product_image_paths()
agent4_catalog = asset_library.build_reference_catalog(
    product_image_paths, CATALOG_ASSET_IDS, [],
)
report["agent4_reference_catalog"] = agent4_catalog

print(f"  Product images: {len(product_image_paths)}")
for i, p in enumerate(product_image_paths, 1):
    print(f"    {i}. {Path(p).name} ({Path(p).stat().st_size} bytes)")
print(f"  Reference catalog ({len(agent4_catalog)} entries):")
for ref in agent4_catalog:
    print(f"    {ref['label']}: {ref['provenance']} | {ref['filename']}")

t0 = time.time()
try:
    raw_output = orch._run_content_creator_raw(
        product_spec="",
        competitor_analysis="",
        campaign_strategy="",
        quick_brief=QUICK_BRIEF,
        media_type="both",
        asset_summary=asset_summary,
    )
except Exception as e:
    print(f"  AGENT 4 FAILED: {e}")
    (OUT_DIR / "agent4_error.txt").write_text(str(e), encoding="utf-8")
    report["agent4_error"] = str(e)
    (OUT_DIR / "uat_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nUAT STOPPED — Agent 4 failed. No media generation attempted.")
    sys.exit(1)

agent4_elapsed = time.time() - t0
print(f"  Agent 4 completed in {agent4_elapsed:.1f}s")

# Save raw + parsed output
(OUT_DIR / "agent4_output_raw.json").write_text(raw_output, encoding="utf-8")

try:
    parsed = json.loads(raw_output)
except json.JSONDecodeError as e:
    print(f"  AGENT 4 OUTPUT PARSE FAILED: {e}")
    report["agent4_parse_error"] = str(e)
    (OUT_DIR / "uat_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(1)

markdown = render_posts_to_markdown(parsed)
(OUT_DIR / "agent4_output.md").write_text(markdown, encoding="utf-8")

posts = parsed.get("posts", [])
if not posts:
    print("  AGENT 4 OUTPUT HAS NO POSTS — cannot proceed")
    report["agent4_no_posts"] = True
    (OUT_DIR / "uat_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(1)

post = posts[0]
image_prompts = post.get("image_prompts", [])
video_prompts = post.get("video_prompts", [])
post_asset_ids = post.get("asset_ids", [])

report["agent4"] = {
    "elapsed_s": round(agent4_elapsed, 1),
    "post_count": len(posts),
    "platform": post.get("platform", ""),
    "concept": post.get("concept", ""),
    "asset_ids": post_asset_ids,
    "image_prompt_count": len(image_prompts),
    "video_prompt_count": len(video_prompts),
    "image_prompt": image_prompts[0]["prompt"] if image_prompts else "",
    "video_prompt": video_prompts[0]["prompt"] if video_prompts else "",
}

print(f"  Posts: {len(posts)}")
print(f"  Post[0] platform: {post.get('platform', '')}")
print(f"  Post[0] concept: {post.get('concept', '')}")
print(f"  Post[0] asset_ids: {post_asset_ids}")
print(f"  Image prompts: {len(image_prompts)}")
print(f"  Video prompts: {len(video_prompts)}")

if image_prompts:
    print(f"  Image prompt (first 200): {image_prompts[0]['prompt'][:200]}...")
if video_prompts:
    print(f"  Video prompt (first 200): {video_prompts[0]['prompt'][:200]}...")

# ── Step 2: Compose + generate IMAGE ──────────────────────────
image_result = None
composed_img = None

if image_prompts:
    img_prompt_text = image_prompts[0].get("prompt", "")
    img_aspect = image_prompts[0].get("aspect_ratio") or "16:9"

    print("\n[2] Composing image input via compose_media_input...")
    composed_img = compose_media_input(
        prompt=img_prompt_text,
        product_id=PRODUCT_ID,
        asset_ids=post_asset_ids,
        catalog_asset_ids=CATALOG_ASSET_IDS,
        aspect_ratio=img_aspect,
        use_retry=False,
    )
    (OUT_DIR / "composed_image.json").write_text(
        json.dumps(composed_img, ensure_ascii=False, indent=2), encoding="utf-8")

    if composed_img.get("preflight_error"):
        print(f"  PREFLIGHT FAILED: {composed_img['preflight_error']}")
        print("  → Skipping image generation (no paid call)")
        report["image_preflight_error"] = composed_img["preflight_error"]
    else:
        print(f"  Final prompt (first 200): {composed_img['prompt'][:200]}...")
        print(f"  input_references ({len(composed_img['input_references'])}):")
        for ref in composed_img.get("reference_catalog", []):
            print(f"    {ref['label']}: {ref['provenance']} | {ref['filename']}")

        img_out = OUT_DIR / "image_1.png"
        print(f"\n[3] Generating image: google/gemini-3.1-flash-image")
        t0 = time.time()
        image_result = media_gen.generate_image(
            composed_img["prompt"],
            img_out,
            input_references=composed_img["input_references"],
            visual=composed_img["visual"],
            aspect_ratio=composed_img["aspect_ratio"],
        )
        img_elapsed = time.time() - t0
        print(f"  ok={image_result.get('ok')}")
        print(f"  elapsed={img_elapsed:.1f}s")
        if image_result.get("ok"):
            sz = Path(image_result["path"]).stat().st_size
            print(f"  path={image_result['path']}")
            print(f"  size={sz} bytes")
            report["image"] = {
                "ok": True, "path": image_result["path"], "size": sz,
                "model": image_result.get("model", ""),
                "elapsed_s": round(img_elapsed, 1),
                "warnings": image_result.get("warnings", []),
            }
        else:
            print(f"  ERROR: {image_result.get('error')}")
            report["image"] = {
                "ok": False, "error": image_result.get("error", ""),
                "http_status": image_result.get("http_status"),
                "model": image_result.get("model", ""),
            }
else:
    print("\n[2] No image prompts — skipping image generation")
    report["image"] = {"ok": False, "reason": "no image prompts in Agent 4 output"}

# ── Step 3: Compose + generate VIDEO ──────────────────────────
video_result = None
composed_vid = None

if video_prompts:
    vid_prompt_text = video_prompts[0].get("prompt", "")
    vid_duration = video_prompts[0].get("duration") or 5
    vid_aspect = video_prompts[0].get("aspect_ratio") or "16:9"
    vid_resolution = video_prompts[0].get("resolution") or "720p"

    print("\n[4] Composing video input via compose_media_input...")
    composed_vid = compose_media_input(
        prompt=vid_prompt_text,
        product_id=PRODUCT_ID,
        asset_ids=post_asset_ids,
        catalog_asset_ids=CATALOG_ASSET_IDS,
        duration=int(vid_duration),
        aspect_ratio=vid_aspect,
        resolution=vid_resolution,
        use_retry=False,
    )
    (OUT_DIR / "composed_video.json").write_text(
        json.dumps(composed_vid, ensure_ascii=False, indent=2), encoding="utf-8")

    if composed_vid.get("preflight_error"):
        print(f"  PREFLIGHT FAILED: {composed_vid['preflight_error']}")
        print("  → Skipping video generation (no paid call)")
        report["video_preflight_error"] = composed_vid["preflight_error"]
    else:
        print(f"  Final prompt (first 200): {composed_vid['prompt'][:200]}...")
        print(f"  input_references ({len(composed_vid['input_references'])}):")
        for ref in composed_vid.get("reference_catalog", []):
            print(f"    {ref['label']}: {ref['provenance']} | {ref['filename']}")

        vid_out = OUT_DIR / "video_1.mp4"
        print(f"\n[5] Generating video: bytedance/seedance-2.0-fast")
        t0 = time.time()
        video_result = media_gen.generate_video(
            composed_vid["prompt"],
            vid_out,
            input_references=composed_vid["input_references"],
            visual=composed_vid["visual"],
            duration=composed_vid["duration"],
            aspect_ratio=composed_vid["aspect_ratio"],
            resolution=composed_vid["resolution"],
            poll_interval=5.0,
            max_wait=600.0,
            on_status=lambda s: print(f"    status: {s}"),
        )
        vid_elapsed = time.time() - t0
        print(f"  ok={video_result.get('ok')}")
        print(f"  elapsed={vid_elapsed:.1f}s")
        if video_result.get("ok"):
            sz = Path(video_result["path"]).stat().st_size
            print(f"  path={video_result['path']}")
            print(f"  size={sz} bytes")
            print(f"  url={video_result.get('url')}")
            print(f"  job_id={video_result.get('job_id')}")
            report["video"] = {
                "ok": True, "path": video_result["path"], "size": sz,
                "model": video_result.get("model", ""),
                "url": video_result.get("url", ""),
                "job_id": video_result.get("job_id", ""),
                "elapsed_s": round(vid_elapsed, 1),
                "warnings": video_result.get("warnings", []),
            }
        else:
            print(f"  ERROR: {video_result.get('error')}")
            report["video"] = {
                "ok": False, "error": video_result.get("error", ""),
                "http_status": video_result.get("http_status"),
                "model": video_result.get("model", ""),
            }
else:
    print("\n[4] No video prompts — skipping video generation")
    report["video"] = {"ok": False, "reason": "no video prompts in Agent 4 output"}

# ── Step 4: Paid call accounting from usage log ───────────────
print("\n" + "=" * 70)
print("PAID CALL ACCOUNTING")
print("=" * 70)

paid_calls: list[dict] = []
if usage_log.exists():
    lines = usage_log.read_text(encoding="utf-8").strip().splitlines()
    new_lines = lines[usage_offset_before:]
    for line in new_lines:
        try:
            entry = json.loads(line)
            paid_calls.append({
                "operation": entry.get("operation"),
                "model": entry.get("model"),
                "status": entry.get("status"),
                "cost_usd": entry.get("cost_usd"),
                "source": entry.get("source"),
                "attempt": entry.get("attempt"),
            })
        except Exception:
            pass

total_cost = sum(float(c["cost_usd"]) for c in paid_calls if c["cost_usd"] is not None)
agent4_llm_calls = [c for c in paid_calls if (c.get("source") or "").startswith("content_creator")]
image_gen_calls = [c for c in paid_calls if c["operation"] == "images.generate"]
video_gen_calls = [c for c in paid_calls if c["operation"] == "videos.generate"]

for i, c in enumerate(paid_calls, 1):
    print(f"  {i}. {c['operation']} | {c['model']} | {c['status']} | "
          f"${c['cost_usd'] or 0:.6f} | {c.get('source', '')}")

print(f"\n  Total paid calls: {len(paid_calls)}")
print(f"  Total cost: ${total_cost:.6f}")
print(f"  Agent 4 LLM calls: {len(agent4_llm_calls)}")
print(f"  Image gen submissions: {len(image_gen_calls)}")
print(f"  Video gen submissions: {len(video_gen_calls)}")

report["paid_calls"] = paid_calls
report["total_cost"] = total_cost
report["agent4_llm_call_count"] = len(agent4_llm_calls)
report["image_gen_count"] = len(image_gen_calls)
report["video_gen_count"] = len(video_gen_calls)

# ── Step 5: File verification ─────────────────────────────────
print("\n" + "=" * 70)
print("ARTIFACT VERIFICATION")
print("=" * 70)
for name in ("image_1.png", "video_1.mp4"):
    f = OUT_DIR / name
    if f.exists():
        head = f.read_bytes()[:8]
        print(f"  {name}: {f.stat().st_size} bytes, header={head!r}")
    else:
        print(f"  {name}: MISSING")

# ── Save final report ─────────────────────────────────────────
(OUT_DIR / "uat_report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"\nReport saved: {OUT_DIR / 'uat_report.json'}")
print("\nUAT COMPLETE")
