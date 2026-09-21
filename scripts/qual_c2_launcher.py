"""C2 Qualification Launcher — Lagenio K5 Reference Fidelity.

Qualification-only process that bounds a single paid ``run_auto`` execution:

A. Pre-paid capability gate — verify ``pricing_skus["per-video-second"]``
   through the existing ``get_model_capabilities`` refresh seam BEFORE any
   paid LLM or media call.
B. Paid LLM hard-call guard — one qualification-wide counter at the shared
   ``httpx.Client.send`` seam; blocks call ``CAP+1`` before network send.
C. Paid media one-shot guards — image=1, video=1, regenerate=0.
D. Evidence capture — sanitized SHA-256 fingerprints of outgoing encoded
   references from the actual HTTP request body (no base64 stored).

Does NOT modify production code.  All patches are process-level and vanish
when the process exits.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration (qualification-only, not production)
# ---------------------------------------------------------------------------
QUAL_C2_MAX_LLM_CALLS = 15
QUAL_C2_MAX_IMAGE = 1
QUAL_C2_MAX_VIDEO = 1
QUAL_EVIDENCE_DIR = Path("data/qualification_reports/c2_run_evidence")

# ---------------------------------------------------------------------------
# Guard state
# ---------------------------------------------------------------------------
_qual_lock = threading.Lock()
_qual_llm_count = 0
_qual_image_count = 0
_qual_video_count = 0
_qual_media_evidence: list[dict[str, Any]] = []

_original_httpx_send = None
_guards_installed = False


class QualificationLLMCapExceeded(Exception):
    """Raised when the qualification-wide LLM call cap is reached."""


class QualificationMediaCapExceeded(Exception):
    """Raised when the qualification-wide media submission cap is reached."""


# ---------------------------------------------------------------------------
# HTTP seam: httpx.Client.send
# ---------------------------------------------------------------------------
# All paid LLM calls in this application cross ``httpx.Client.send``:
#
#   chat() non-stream:
#       self._client.post("/chat/completions", json=payload)
#       → httpx.Client.request()
#       → httpx.Client.send(request, stream=False, ...)
#
#   chat() stream:
#       attempt_client.stream("POST", "/chat/completions", json=payload)
#       → httpx.Client.stream()
#       → httpx.Client.send(request, stream=True, ...)
#
#   chat_with_tools() (OpenAI SDK):
#       client.chat.completions.create(...)
#       → openai._base_client.SyncAPIClient.request()
#       → openai._base_client.SyncAPIClient._send_request()
#       → self._client.send(request, stream=..., ...)
#       (self._client is SyncHttpxClientWrapper which extends httpx.Client)
#
# All three converge on ``httpx.Client.send``.  Patching it at the class
# level intercepts every outgoing paid LLM HTTP request, regardless of which
# client instance or SDK wrapper originated it.


def _is_chat_completions(request) -> bool:
    """True if this is a paid LLM chat-completions POST."""
    return request.method == "POST" and "/chat/completions" in str(request.url)


def _is_image_submission(request) -> bool:
    """True if this is a paid image generation POST (not metadata GET)."""
    url = str(request.url)
    return (
        request.method == "POST"
        and "/api/v1/images" in url
        and "/images/models" not in url
    )


def _is_video_submission(request) -> bool:
    """True if this is a paid video generation POST (not metadata GET)."""
    url = str(request.url)
    return (
        request.method == "POST"
        and "/api/v1/videos" in url
        and "/videos/models" not in url
    )


def _capture_media_evidence(kind: str, request) -> None:
    """Capture sanitized fingerprints from the actual HTTP request body.

    Stores SHA-256 of the base64-encoded reference payload only.
    Never stores raw base64 content.
    """
    try:
        body = json.loads(request.content)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        body = {}

    refs = body.get("input_references", [])
    ref_fps: list[dict[str, str]] = []
    for r in refs:
        if isinstance(r, dict):
            iu = r.get("image_url", {})
            url_val = (
                iu.get("url", "") if isinstance(iu, dict) else str(iu)
            )
        else:
            url_val = str(r)
        if url_val.startswith("data:"):
            b64_part = url_val.split(",", 1)[-1] if "," in url_val else ""
            fp = (
                hashlib.sha256(b64_part.encode("ascii")).hexdigest()
                if b64_part
                else ""
            )
            mime = (
                url_val.split(";")[0].split(":")[1]
                if ":" in url_val
                else "image"
            )
        else:
            fp = hashlib.sha256(url_val.encode("utf-8")).hexdigest()
            mime = "url"
        ref_fps.append({"sha256": fp, "mime": mime})

    _qual_media_evidence.append(
        {
            "kind": kind,
            "url": str(request.url),
            "model": body.get("model"),
            "num_references": len(refs),
            "reference_fingerprints": ref_fps,
            "video_duration": body.get("duration")
            or body.get("duration_seconds"),
            "aspect_ratio": body.get("aspect_ratio"),
            "resolution": body.get("resolution"),
            "timestamp": time.time(),
        }
    )
    _write_evidence_file("media_evidence.jsonl", _qual_media_evidence[-1])


def _write_evidence_file(filename: str, data: dict) -> None:
    """Append evidence to a JSONL file in the evidence directory."""
    try:
        QUAL_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        with open(QUAL_EVIDENCE_DIR / filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    except Exception:
        pass  # evidence writing must not break the run


def _write_status_file() -> None:
    """Write current guard state to a status file."""
    try:
        QUAL_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "llm_count": _qual_llm_count,
            "image_count": _qual_image_count,
            "video_count": _qual_video_count,
            "timestamp": time.time(),
        }
        with open(QUAL_EVIDENCE_DIR / "guard_status.json", "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _qual_httpx_send(self, request, *, stream=False, **kwargs):
    """Qualification guard wrapper around ``httpx.Client.send``.

    Checks the qualification-wide counter BEFORE delegating to the original
    send.  Blocks call ``CAP+1`` by raising before any network activity.
    """
    global _qual_llm_count, _qual_image_count, _qual_video_count

    # --- Paid LLM chat-completions ---
    if _is_chat_completions(request):
        with _qual_lock:
            if _qual_llm_count >= QUAL_C2_MAX_LLM_CALLS:
                raise QualificationLLMCapExceeded(
                    f"Qualification LLM cap reached: "
                    f"{_qual_llm_count}/{QUAL_C2_MAX_LLM_CALLS} — "
                    f"next request BLOCKED before network send"
                )
            _qual_llm_count += 1
        _write_status_file()
        return _original_httpx_send(self, request, stream=stream, **kwargs)

    # --- Paid image submission ---
    if _is_image_submission(request):
        with _qual_lock:
            if _qual_image_count >= QUAL_C2_MAX_IMAGE:
                raise QualificationMediaCapExceeded(
                    f"Image submission cap reached: "
                    f"{_qual_image_count}/{QUAL_C2_MAX_IMAGE} — "
                    f"next request BLOCKED before network send"
                )
            _qual_image_count += 1
        _capture_media_evidence("image", request)
        return _original_httpx_send(self, request, stream=stream, **kwargs)

    # --- Paid video submission ---
    if _is_video_submission(request):
        with _qual_lock:
            if _qual_video_count >= QUAL_C2_MAX_VIDEO:
                raise QualificationMediaCapExceeded(
                    f"Video submission cap reached: "
                    f"{_qual_video_count}/{QUAL_C2_MAX_VIDEO} — "
                    f"next request BLOCKED before network send"
                )
            _qual_video_count += 1
        _capture_media_evidence("video", request)
        return _original_httpx_send(self, request, stream=stream, **kwargs)

    # --- All other requests (capability GET, etc.) pass through uncounted ---
    return _original_httpx_send(self, request, stream=stream, **kwargs)


def install_qualification_guards() -> None:
    """Install process-level HTTP guards.

    Saves the real ``httpx.Client.send`` and replaces it with the guard
    wrapper.  Call once before paid execution.
    """
    global _original_httpx_send, _guards_installed
    if _guards_installed:
        return
    import httpx

    _original_httpx_send = httpx.Client.send
    httpx.Client.send = _qual_httpx_send
    _guards_installed = True


def uninstall_qualification_guards() -> None:
    """Restore the original ``httpx.Client.send``."""
    global _guards_installed
    if not _guards_installed:
        return
    import httpx

    httpx.Client.send = _original_httpx_send
    _guards_installed = False


def reset_qualification_state() -> None:
    """Reset all counters and evidence.  Useful between test cases."""
    global _qual_llm_count, _qual_image_count, _qual_video_count
    global _qual_media_evidence
    with _qual_lock:
        _qual_llm_count = 0
        _qual_image_count = 0
        _qual_video_count = 0
        _qual_media_evidence = []


def get_qualification_state() -> dict[str, Any]:
    """Return a snapshot of the current guard state."""
    with _qual_lock:
        return {
            "llm_count": _qual_llm_count,
            "image_count": _qual_image_count,
            "video_count": _qual_video_count,
            "media_evidence": list(_qual_media_evidence),
        }


# ---------------------------------------------------------------------------
# Catalog/ordinal evidence capture (qualification-only monkeypatches)
# ---------------------------------------------------------------------------
_qual_catalog_evidence: list[dict] = []
_original_build_reference_catalog = None
_original_extract_reference_ordinals = None
_original_compose_media_input = None


def _capture_build_reference_catalog(original_fn):
    """Wrap build_reference_catalog to capture the full catalog."""
    def _wrapper(*args, **kwargs):
        catalog = original_fn(*args, **kwargs)
        try:
            # Serialize catalog entries (paths only, no binary data)
            serializable = []
            for entry in catalog:
                e = dict(entry)
                # Keep only serializable fields
                serializable.append({
                    "ordinal": e.get("ordinal"),
                    "label": e.get("label"),
                    "provenance": e.get("provenance"),
                    "path": str(e.get("path", "")),
                    "asset_id": e.get("asset_id"),
                    "filename": e.get("filename"),
                })
            _qual_catalog_evidence.append({
                "event": "build_reference_catalog",
                "catalog": serializable,
                "timestamp": time.time(),
            })
            _write_evidence_file("catalog_evidence.jsonl", _qual_catalog_evidence[-1])
        except Exception:
            pass
        return catalog
    return _wrapper


def _capture_extract_reference_ordinals(original_fn):
    """Wrap extract_reference_ordinals to capture Agent N mentions."""
    def _wrapper(prompt, *args, **kwargs):
        ordinals = original_fn(prompt, *args, **kwargs)
        try:
            _qual_catalog_evidence.append({
                "event": "extract_reference_ordinals",
                "prompt_preview": str(prompt)[:500],
                "extracted_ordinals": list(ordinals) if ordinals else [],
                "timestamp": time.time(),
            })
            _write_evidence_file("catalog_evidence.jsonl", _qual_catalog_evidence[-1])
        except Exception:
            pass
        return ordinals
    return _wrapper


def _capture_compose_media_input(original_fn):
    """Wrap compose_media_input to capture composed references."""
    def _wrapper(*args, **kwargs):
        result = original_fn(*args, **kwargs)
        try:
            # Capture the composed input_references and prompt
            composed = {
                "event": "compose_media_input",
                "prompt_preview": str(result.get("prompt", ""))[:500],
                "input_references_count": len(result.get("input_references", [])),
                "input_reference_paths": [
                    str(r) if not isinstance(r, dict) else r.get("image_url", {}).get("url", "")[:50]
                    for r in result.get("input_references", [])
                ],
                "duration": result.get("duration"),
                "aspect_ratio": result.get("aspect_ratio"),
                "resolution": result.get("resolution"),
                "timestamp": time.time(),
            }
            _qual_catalog_evidence.append(composed)
            _write_evidence_file("catalog_evidence.jsonl", _qual_catalog_evidence[-1])
        except Exception:
            pass
        return result
    return _wrapper


def install_catalog_capture() -> None:
    """Install monkeypatches to capture catalog/ordinal/compose evidence."""
    global _original_build_reference_catalog, _original_extract_reference_ordinals
    global _original_compose_media_input
    from src import asset_library
    import web_viewer

    _original_build_reference_catalog = asset_library.build_reference_catalog
    asset_library.build_reference_catalog = _capture_build_reference_catalog(
        _original_build_reference_catalog
    )

    _original_extract_reference_ordinals = asset_library.extract_reference_ordinals
    asset_library.extract_reference_ordinals = _capture_extract_reference_ordinals(
        _original_extract_reference_ordinals
    )

    _original_compose_media_input = web_viewer.compose_media_input
    web_viewer.compose_media_input = _capture_compose_media_input(
        _original_compose_media_input
    )


# ---------------------------------------------------------------------------
# Pre-paid pricing gate
# ---------------------------------------------------------------------------
def pre_paid_pricing_gate(media_gen_module=None):
    """Verify provider pricing metadata before any paid call.

    Calls ``get_model_capabilities`` which may make a free GET to the
    OpenRouter capability metadata endpoint.  Does NOT make any paid
    LLM or media call.

    Returns ``(ok: bool, evidence: dict, message: str)``.
    """
    if media_gen_module is None:
        from src import media_gen as media_gen_module

    cfg = media_gen_module._load_media_config()
    video_model = cfg.get("video_model", media_gen_module.DEFAULT_VIDEO_MODEL)

    # Force capability refresh through the existing stale-shape seam.
    # If the disk cache lacks pricing_skus, get_model_capabilities falls
    # through to a free GET /api/v1/videos/models.
    caps = media_gen_module.get_model_capabilities(video_model, kind="video")

    pricing_skus = caps.get("pricing_skus", {})

    caps_text = media_gen_module.format_capabilities_for_prompt(
        video_model, kind="video"
    )
    has_cost_text = "cost โดยประมาณ" in caps_text

    # Determine whether the formatted cost text provides usable duration-cost
    # guidance for Agent 4.  This is stronger than just checking bool(pricing_skus):
    #   - per-second pricing (duration_seconds*, cents_per_second*) → direct
    #   - token pricing (video_tokens*) + "longer duration = higher cost" → indirect
    #   - unknown SKUs only → no usable duration guidance
    #   - no pricing → no guidance
    has_usable_duration_guidance = False
    if has_cost_text:
        if "/วินาที" in caps_text:
            # Direct per-second pricing present
            has_usable_duration_guidance = True
        elif "$" in caps_text and "ค่าใช้จ่ายสูงขึ้น" in caps_text:
            # Token-based pricing with grounded duration-cost guidance
            has_usable_duration_guidance = True

    evidence = {
        "video_model": video_model,
        "pricing_skus": pricing_skus,
        "caps_text_contains_cost": has_cost_text,
        "has_usable_duration_guidance": has_usable_duration_guidance,
        "caps_text_preview": caps_text[:200] if caps_text else "",
        "durations": caps.get("durations", []),
    }

    if not pricing_skus:
        return (
            False,
            evidence,
            "FAIL: no pricing metadata present after refresh",
        )
    if not has_cost_text:
        return (
            False,
            evidence,
            "FAIL: format_capabilities_for_prompt does not contain cost text",
        )
    if not has_usable_duration_guidance:
        return (
            False,
            evidence,
            "FAIL: pricing present but no usable duration-cost guidance for Agent",
        )
    return (True, evidence, "PASS")


# ---------------------------------------------------------------------------
# Media one-shot wrappers (qualification-only)
# ---------------------------------------------------------------------------
# Production callers (web_viewer.py) invoke generate_image_with_retry /
# generate_video_with_retry with kwargs the one-shot functions do NOT accept:
#
#   generate_image_with_retry(prompt, path, *, llm=None, on_retry=None,
#       model=, aspect_ratio=, timeout=, input_references=, visual=)
#
#   generate_video_with_retry(prompt, path, *, llm=None, on_retry=None,
#       on_status=None, model=, duration=, aspect_ratio=, resolution=,
#       poll_interval=, max_wait=, input_references=, frame_images=, visual=)
#
# The one-shot generate_image / generate_video do NOT accept llm / on_retry.
# Direct assignment would TypeError when the UI passes those kwargs.
#
# These qualification wrappers accept the full retry-wrapper call signature,
# delegate to the real one-shot function exactly once, and return the same
# result shape (with retry_count=0 / retry_history=[] for UI compatibility).
#
# The HTTP-level guards (image=1, video=1) remain the final hard protection:
# even if a wrapper is somehow called twice, the second POST is blocked.


def _make_qual_image_with_retry(media_gen_module):
    """Build a qualification-only image wrapper bound to the real one-shot."""
    real_generate_image = media_gen_module.generate_image

    def qual_image_with_retry(
        prompt: str,
        output_path,
        *,
        llm=None,
        model: str | None = None,
        aspect_ratio: str | None = None,
        timeout: float | None = None,
        on_retry=None,
        input_references=None,
        visual: dict | None = None,
        **_extra,
    ):
        # llm / on_retry accepted but ignored — no prompt-repair retry path.
        result = real_generate_image(
            prompt,
            output_path,
            model=model,
            aspect_ratio=aspect_ratio,
            timeout=timeout,
            input_references=input_references,
            visual=visual,
            attempt=1,
        )
        # Preserve UI-compatible result shape
        result.setdefault("retry_count", 0)
        result.setdefault("retry_history", [])
        return result

    return qual_image_with_retry


def _make_qual_video_with_retry(media_gen_module):
    """Build a qualification-only video wrapper bound to the real one-shot."""
    real_generate_video = media_gen_module.generate_video

    def qual_video_with_retry(
        prompt: str,
        output_path,
        *,
        llm=None,
        model: str | None = None,
        duration: int | None = None,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        poll_interval: float | None = None,
        max_wait: float | None = None,
        on_status=None,
        on_retry=None,
        input_references=None,
        frame_images=None,
        visual: dict | None = None,
        **_extra,
    ):
        # llm / on_retry accepted but ignored — no prompt-repair retry path.
        # on_status is forwarded so UI progress callbacks still work.
        result = real_generate_video(
            prompt,
            output_path,
            model=model,
            duration=duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            poll_interval=poll_interval,
            max_wait=max_wait,
            on_status=on_status,
            input_references=input_references,
            frame_images=frame_images,
            visual=visual,
            attempt=1,
        )
        result.setdefault("retry_count", 0)
        result.setdefault("retry_history", [])
        return result

    return qual_video_with_retry


def install_media_one_shot(media_gen_module=None) -> None:
    """Replace retry wrappers with qualification one-shot wrappers.

    The wrappers accept the full production retry-wrapper call signature
    (llm, on_retry, on_status, etc.) but delegate to the real one-shot
    generate_image / generate_video exactly once.  No production functions
    are modified — only process-level attribute swap.
    """
    if media_gen_module is None:
        from src import media_gen as media_gen_module
    media_gen_module.generate_image_with_retry = _make_qual_image_with_retry(
        media_gen_module
    )
    media_gen_module.generate_video_with_retry = _make_qual_video_with_retry(
        media_gen_module
    )


# ---------------------------------------------------------------------------
# Pre-run price estimate
# ---------------------------------------------------------------------------
def pre_run_price_estimate(media_gen_module=None) -> dict:
    """Calculate a pre-run spend estimate from refreshed pricing metadata.

    Does NOT make any paid call.  Dollar amounts are ESTIMATES, not
    mechanically guaranteed.  Only request counts are hard-capped.

    For per-second providers, computes price × duration.
    For token-based providers, does NOT fabricate a per-second estimate —
    reports token pricing and historical evidence separately.
    """
    if media_gen_module is None:
        from src import media_gen as media_gen_module

    cfg = media_gen_module._load_media_config()
    video_model = cfg.get("video_model", media_gen_module.DEFAULT_VIDEO_MODEL)
    image_model = cfg.get("image_model", media_gen_module.DEFAULT_IMAGE_MODEL)

    caps = media_gen_module.get_model_capabilities(video_model, kind="video")
    pricing_skus = caps.get("pricing_skus", {})
    durations = caps.get("durations", [])
    max_duration = max(durations) if durations else 15
    min_duration = min(durations) if durations else 4

    # Detect pricing family
    per_second = None
    token_pricing = None
    for sku, price in pricing_skus.items():
        if sku.startswith("duration_seconds"):
            per_second = float(price)
        elif "cents_per_second" in sku or "cents_per_video_output_second" in sku:
            per_second = float(price) / 100
        elif "video_tokens" in sku and token_pricing is None:
            token_pricing = (sku, float(price))

    # Image: use historical estimate (no per-image SKU in capability cache)
    image_estimate = 0.07  # historical from logs/llm_usage.jsonl

    # Video estimates
    if per_second is not None:
        # Direct per-second pricing — can compute duration cost
        expected_video_duration = 5  # "short video" — not forced
        expected_video_cost = round(per_second * expected_video_duration, 4)
        worst_video_cost = round(per_second * max_duration, 4)
        video_estimate_type = "per_second"
    elif token_pricing is not None:
        # Token-based pricing — cannot compute exact duration cost
        # Report token pricing; do NOT fabricate a per-second estimate
        expected_video_cost = None
        worst_video_cost = None
        video_estimate_type = "token_based"
    else:
        expected_video_cost = None
        worst_video_cost = None
        video_estimate_type = "unknown"

    # LLM estimates (historical from logs/llm_usage.jsonl)
    llm_per_call_low = 0.01
    llm_per_call_high = 0.025
    expected_llm_calls = 8
    max_llm_calls = QUAL_C2_MAX_LLM_CALLS
    expected_llm_cost = round(
        expected_llm_calls * (llm_per_call_low + llm_per_call_high) / 2, 4
    )
    worst_llm_cost = round(max_llm_calls * llm_per_call_high, 4)

    expected_total = round(
        image_estimate
        + (expected_video_cost or 0)
        + expected_llm_cost,
        4,
    )
    worst_total = round(
        image_estimate + (worst_video_cost or 0) + worst_llm_cost, 4
    )

    return {
        "video_model": video_model,
        "image_model": image_model,
        "pricing_skus": pricing_skus,
        "video_estimate_type": video_estimate_type,
        "per_video_second": per_second,
        "token_pricing": token_pricing,
        "duration_range": durations,
        "max_duration": max_duration,
        "min_duration": min_duration,
        "image_estimate": image_estimate,
        "expected_video_duration_seconds": 5 if per_second else None,
        "expected_video_cost": expected_video_cost,
        "worst_video_cost_15s": worst_video_cost,
        "expected_llm_calls": expected_llm_calls,
        "max_llm_calls": max_llm_calls,
        "expected_llm_cost": expected_llm_cost,
        "worst_llm_cost": worst_llm_cost,
        "expected_total": expected_total,
        "worst_case_total": worst_total,
        "note": (
            "Dollar amounts are ESTIMATES from provider-advertised pricing "
            "and historical token costs. Only request counts are hard-capped. "
            "No provider-side dollar cap exists. "
            "For token-based providers, video cost cannot be estimated from "
            "duration alone — actual cost depends on token count which varies "
            "with resolution, duration, and provider encoding."
        ),
    }


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def main() -> None:
    """Qualification launcher main flow."""
    # Step A: Pre-paid pricing gate
    ok, evidence, msg = pre_paid_pricing_gate()
    print(f"[GATE] {msg}")
    print(
        f"[GATE] Evidence: {json.dumps(evidence, indent=2, ensure_ascii=False)}"
    )
    if not ok:
        print("[GATE] FAILED — aborting before any paid call")
        print(f"[GATE] paid_llm_calls=0 image_submissions=0 video_submissions=0")
        sys.exit(1)

    # Step B+C+D: Install guards
    QUAL_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    # Clear old evidence
    for f in QUAL_EVIDENCE_DIR.glob("*"):
        f.unlink()
    install_qualification_guards()
    install_media_one_shot()
    install_catalog_capture()

    # Pre-run price estimate (from refreshed pricing metadata)
    estimate = pre_run_price_estimate()
    print("[ESTIMATE] Pre-run spend estimate:")
    print(
        f"[ESTIMATE]   {json.dumps(estimate, indent=2, ensure_ascii=False)}"
    )

    print("[LAUNCHER] Guards installed. Ready for paid UI execution.")
    print(f"[LAUNCHER] LLM cap:   {QUAL_C2_MAX_LLM_CALLS}")
    print(f"[LAUNCHER] Image cap: {QUAL_C2_MAX_IMAGE}")
    print(f"[LAUNCHER] Video cap: {QUAL_C2_MAX_VIDEO}")
    print("[LAUNCHER] Start the UI run manually via /api/run_auto with:")
    print("[LAUNCHER]   product_count=1 content_count=1 platforms=['facebook']")
    print("[LAUNCHER]   media_type=both media_when=auto")


if __name__ == "__main__":
    main()
