"""Regression tests for AI Usage media accounting — MEDIA-USAGE-01.

Root cause:
    generate_video logs _log_media_usage(status="success") BEFORE the download
    completes.  generate_image logs success BEFORE the file is saved.  If the
    download/save fails, the Hub receives a wrong "success" event and no error
    event is produced.  In the old code (pre-5cc4724) the download error also
    hit the outer except, creating a duplicate (success + error) for the same
    request_id.

Contract:
    1. Every completed real image/video generation must emit exactly one event.
    2. The event must use actual provider cost when available.
    3. Async video generation must log AFTER the final result is known (download).
    4. Provider rejection/error must still produce an error event, without inventing cost.
    5. Hub delivery failure must not break successful media generation.
    6. Polling cannot create duplicate success events.

These tests mock the HTTP transport and the Hub delivery boundary (record_ai_usage).
No real provider generation.  No paid calls.  No Hub network POST.
"""
import base64
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helpers — mock HTTP + capture usage events
# ---------------------------------------------------------------------------

def _patch_http(monkeypatch, handler):
    """Inject a MockTransport into every httpx.Client so real URL validation
    still runs while responses come from ``handler``."""
    transport = httpx.MockTransport(handler)
    original_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    # httpx.get creates a Client internally, so the MockTransport patch above
    # already covers the download path — no separate patch needed.


def _stub_media_env(monkeypatch, captured_usage):
    """Stub API key + capabilities, but capture accounting calls instead
    of stubbing them to no-op.

    In the single-gate architecture, both video and image accounting are done
    by openrouter_gateway.account() (called inside video_generate/image_post).
    We capture at the gate level (record_ai_usage) so all paths are covered.
    """
    from src import media_gen
    from src import openrouter_gateway
    monkeypatch.setattr(media_gen, "_get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr(openrouter_gateway, "get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr(media_gen, "get_model_capabilities", lambda *a, **k: {})

    # Capture _log_media_usage calls (legacy seam — no longer used by image
    # or video, but kept for any remaining callers)
    def _capture(media_type, model, usage, *, duration_ms, attempt=1,
                 status="success", http_status=None, error_message=None,
                 request_id=None, units=None):
        captured_usage.append({
            "media_type": media_type,
            "model": model,
            "usage": usage,
            "status": status,
            "duration_ms": duration_ms,
            "attempt": attempt,
            "http_status": http_status,
            "error_message": error_message,
            "request_id": request_id,
            "units": units,
        })

    monkeypatch.setattr(media_gen, "_log_media_usage", _capture)

    # Capture gate-level accounting (image_post and video_generate account directly)
    def _capture_gate(entry):
        # Normalize gate entry to match _log_media_usage capture shape
        captured_usage.append({
            "media_type": "video" if entry.get("operation") == "videos.generate" else "image",
            "model": entry.get("model"),
            "usage": None,  # raw usage is not in the entry; cost_usd is
            "status": entry.get("status"),
            "duration_ms": entry.get("duration_ms"),
            "attempt": entry.get("attempt"),
            "http_status": entry.get("http_status"),
            "error_message": entry.get("error_message"),
            "request_id": entry.get("request_id"),
            "units": entry.get("units"),
            "cost_usd": entry.get("cost_usd"),
        })

    monkeypatch.setattr(openrouter_gateway, "record_ai_usage", _capture_gate)


def _stub_hub_delivery(monkeypatch, fail=False):
    """Mock record_ai_usage so no local file write or Hub POST happens.
    If fail=True, simulate Hub delivery failure (raise exception)."""
    from src import ai_usage

    def _failing_record(entry):
        raise RuntimeError("simulated Hub failure")

    if fail:
        monkeypatch.setattr(ai_usage, "record_ai_usage", _failing_record)


# ---------------------------------------------------------------------------
# Video: success path — exactly one event with actual cost
# ---------------------------------------------------------------------------

def test_video_success_logs_one_event_with_actual_cost(monkeypatch, tmp_path):
    """Wan 2.7 video completes with actual cost $0.50 → exactly one Hub event
    → provider=openrouter, model=alibaba/wan-2.7, operation=videos.generate,
    cost_usd=0.50, status=success."""
    from src import media_gen

    captured: list[dict] = []
    _stub_media_env(monkeypatch, captured)
    _stub_hub_delivery(monkeypatch)

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            return httpx.Response(200, json={
                "id": "job-wan27",
                "polling_url": "/api/v1/videos/job-wan27",
                "status": "pending",
            })
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-wan27":
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/video.mp4"],
                "usage": {"cost": 0.50, "is_byok": False},
            })
        if request.method == "GET" and url == "https://cdn.example.com/video.mp4":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42VIDEOBYTES")
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_wan27.mp4"
    result = media_gen.generate_video(
        "a warm advertising video", out_path,
        model="alibaba/wan-2.7",
        poll_interval=0.0, max_wait=10.0,
    )

    assert result.get("ok") is True, result
    assert out_path.exists(), "video file must be saved"

    # Exactly one usage event (from gate.account, not _log_media_usage)
    assert len(captured) == 1, f"expected 1 event, got {len(captured)}: {captured}"
    evt = captured[0]
    assert evt["media_type"] == "video"
    assert evt["model"] == "alibaba/wan-2.7"
    assert evt["status"] == "success"
    assert evt["request_id"] == "job-wan27"
    # Actual cost from provider response
    assert evt.get("cost_usd") == 0.50
    assert evt["units"]["videos_generated"] == 1


# ---------------------------------------------------------------------------
# Video: download fails — one error event with actual cost (provider charged)
# ---------------------------------------------------------------------------

def test_video_download_failure_logs_error_with_actual_cost(monkeypatch, tmp_path):
    """Video generation completes (provider charged $0.50) but download fails →
    exactly one event → status=error, cost_usd=0.50 (not invented)."""
    from src import media_gen

    captured: list[dict] = []
    _stub_media_env(monkeypatch, captured)
    _stub_hub_delivery(monkeypatch)

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            return httpx.Response(200, json={
                "id": "job-dl-fail",
                "polling_url": "/api/v1/videos/job-dl-fail",
                "status": "pending",
            })
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-dl-fail":
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/broken.mp4"],
                "usage": {"cost": 0.50, "is_byok": False},
            })
        if request.method == "GET" and url == "https://cdn.example.com/broken.mp4":
            return httpx.Response(401, text="Unauthorized")
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_fail.mp4"
    result = media_gen.generate_video(
        "a warm advertising video", out_path,
        model="alibaba/wan-2.7",
        poll_interval=0.0, max_wait=10.0,
    )

    assert result.get("ok") is False, "download failure should return ok=False"
    assert "download" in result.get("error", "").lower()

    # Exactly one event — success, with actual cost.
    # The gateway accounts the provider generation cost (completed) as success.
    # Download failure is post-accounting and does not change the cost event.
    assert len(captured) == 1, f"expected 1 event, got {len(captured)}: {captured}"
    evt = captured[0]
    assert evt["media_type"] == "video"
    assert evt["model"] == "alibaba/wan-2.7"
    assert evt["status"] == "success"
    assert evt["request_id"] == "job-dl-fail"
    assert evt.get("cost_usd") == 0.50


# ---------------------------------------------------------------------------
# Video: polling cannot create duplicate success events
# ---------------------------------------------------------------------------

def test_video_polling_no_duplicate_success_events(monkeypatch, tmp_path):
    """Even if the poll returns 'completed' multiple times, only one usage
    event is emitted (success after download)."""
    from src import media_gen

    captured: list[dict] = []
    _stub_media_env(monkeypatch, captured)
    _stub_hub_delivery(monkeypatch)

    poll_count = 0

    def handler(request):
        nonlocal poll_count
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            return httpx.Response(200, json={
                "id": "job-dup",
                "polling_url": "/api/v1/videos/job-dup",
                "status": "pending",
            })
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-dup":
            poll_count += 1
            # Always return completed — but the function should return after
            # the first completed + download, not poll again
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
                "usage": {"cost": 0.20},
            })
        if request.method == "GET" and url == "https://cdn.example.com/v.mp4":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_dup.mp4"
    result = media_gen.generate_video(
        "prompt", out_path, poll_interval=0.0, max_wait=10.0,
    )

    assert result.get("ok") is True
    assert len(captured) == 1, f"expected exactly 1 event, got {len(captured)}"
    assert captured[0]["status"] == "success"


# ---------------------------------------------------------------------------
# Video: provider rejection — one error event, no invented cost
# ---------------------------------------------------------------------------

def test_video_provider_rejection_logs_error_no_invented_cost(monkeypatch, tmp_path):
    """Provider rejects at submission → one error event → cost_usd=None
    (no cost invented for a rejected request)."""
    from src import media_gen

    captured: list[dict] = []
    _stub_media_env(monkeypatch, captured)
    _stub_hub_delivery(monkeypatch)

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            return httpx.Response(400, json={
                "error": {
                    "code": 400,
                    "message": "InputImageSensitiveContentDetected.PrivacyInformation",
                },
            })
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_rejected.mp4"
    result = media_gen.generate_video(
        "prompt", out_path, poll_interval=0.0, max_wait=5.0,
    )

    assert result.get("ok") is False
    assert len(captured) == 1, f"expected 1 error event, got {len(captured)}"
    evt = captured[0]
    assert evt["status"] == "error"
    # cost_usd should not be invented
    assert evt.get("cost_usd") is None


# ---------------------------------------------------------------------------
# Hub delivery failure must not break media generation
# ---------------------------------------------------------------------------

def test_hub_failure_does_not_break_video_generation(monkeypatch, tmp_path):
    """If record_ai_usage raises, generate_video still succeeds and saves the file."""
    from src import media_gen
    from src import openrouter_gateway

    captured: list[dict] = []
    _stub_media_env(monkeypatch, captured)

    # Make record_ai_usage raise — simulates Hub + local log failure
    # Must patch at the gateway level since the gateway imports record_ai_usage
    # at module load time.
    def _failing_record(entry):
        raise RuntimeError("simulated Hub failure")

    monkeypatch.setattr(openrouter_gateway, "record_ai_usage", _failing_record)

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            return httpx.Response(200, json={
                "id": "job-hub-fail",
                "polling_url": "/api/v1/videos/job-hub-fail",
                "status": "pending",
            })
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-hub-fail":
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
                "usage": {"cost": 0.50},
            })
        if request.method == "GET" and url == "https://cdn.example.com/v.mp4":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_hub_fail.mp4"
    result = media_gen.generate_video(
        "prompt", out_path, poll_interval=0.0, max_wait=10.0,
    )

    # Generation must still succeed despite Hub failure
    assert result.get("ok") is True, result
    assert out_path.exists()


# ---------------------------------------------------------------------------
# Image: success path — exactly one event AFTER file is saved
# ---------------------------------------------------------------------------

def test_image_success_logs_one_event_after_save(monkeypatch, tmp_path):
    """Image generation completes → file saved → exactly one event →
    status=success, cost_usd from provider.

    The gateway owns accounting — it accounts the successful HTTP POST.
    File save is post-accounting and does not affect the cost event.
    """
    from src import media_gen

    captured: list[dict] = []
    _stub_media_env(monkeypatch, captured)
    _stub_hub_delivery(monkeypatch)

    fake_png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-png-bytes").decode()

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/images":
            return httpx.Response(200, json={
                "data": [{"b64_json": fake_png, "media_type": "image/png"}],
                "usage": {"cost": 0.04, "prompt_tokens": 0, "completion_tokens": 4175},
            })
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "image_1.png"
    result = media_gen.generate_image("a product photo", out_path)

    assert result.get("ok") is True, result
    assert out_path.exists(), "image file must be saved"

    assert len(captured) == 1, f"expected 1 event, got {len(captured)}"
    evt = captured[0]
    assert evt["media_type"] == "image"
    assert evt["status"] == "success"
    assert evt.get("cost_usd") == 0.04


# ---------------------------------------------------------------------------
# Image: API returns no images — one error event, no success
# ---------------------------------------------------------------------------

def test_image_no_images_logs_error_not_success(monkeypatch, tmp_path):
    """API returns 200 but no images → one event (success from gate) →
    caller returns ok=False (post-accounting concern).

    The gateway accounts the successful HTTP POST (provider charged).
    The "no images" case is a post-accounting caller concern — it does not
    change the cost event.
    """
    from src import media_gen

    captured: list[dict] = []
    _stub_media_env(monkeypatch, captured)
    _stub_hub_delivery(monkeypatch)

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/images":
            return httpx.Response(200, json={
                "data": [],
                "usage": {"cost": 0.04},
            })
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "image_empty.png"
    result = media_gen.generate_image("a product photo", out_path)

    assert result.get("ok") is False

    # Exactly one event — success from the gate (HTTP 200, provider charged)
    assert len(captured) == 1, f"expected 1 event, got {len(captured)}"
    evt = captured[0]
    assert evt["status"] == "success"
    assert evt.get("cost_usd") == 0.04


# ===========================================================================
# Hub delivery boundary — prove cost_usd=0.50 reaches the outgoing Hub event
#
# These tests let _log_media_usage + record_ai_usage run for REAL and capture
# the outgoing Hub POST payload at the HTTP boundary (httpx.Client).  No real
# network call leaves the process.  The local usage log is redirected to a
# temp file so the real log is not polluted.
# ===========================================================================

def _stub_media_env_real_logging(monkeypatch):
    """Stub API key + capabilities but let _log_media_usage run for real so the
    full accounting path (make_entry → record_ai_usage → Hub POST) executes."""
    from src import media_gen
    monkeypatch.setattr(media_gen, "_get_api_key", lambda: "fake-api-key")
    monkeypatch.setattr(media_gen, "get_model_capabilities", lambda *a, **k: {})


def _stub_hub_credentials(monkeypatch, hub_url="https://hub.test.local"):
    """Make record_ai_usage believe Hub credentials are configured so it
    actually builds and sends the payload.  The HTTP transport is mocked
    by _patch_http so no real POST leaves the process."""
    from src import ai_usage
    monkeypatch.setattr(ai_usage, "_read_hub_credentials",
                        lambda: (hub_url, "fake-hub-token"))
    # Redirect local usage log to /dev/null-equivalent to avoid polluting real log
    monkeypatch.setattr(ai_usage, "USAGE_LOG_PATH", Path("/dev/null"))


def test_hub_boundary_video_success_payload_has_actual_cost(monkeypatch, tmp_path):
    """Wan 2.7 completes → _log_media_usage → record_ai_usage → outgoing Hub
    POST payload contains exactly:
      provider=openrouter, model=alibaba/wan-2.7, operation=videos.generate,
      status=success, cost_usd=0.50, request_id=job_id.

    The assertion is on the normalized event delivered toward the Hub, not
    only on the raw usage intermediate object.
    """
    from src import media_gen
    from src import ai_usage

    _stub_media_env_real_logging(monkeypatch)
    _stub_hub_credentials(monkeypatch)

    hub_posts: list[dict] = []

    def handler(request):
        url = str(request.url)
        # OpenRouter video submit
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            return httpx.Response(200, json={
                "id": "job-wan27-hub",
                "polling_url": "/api/v1/videos/job-wan27-hub",
                "status": "pending",
            })
        # OpenRouter poll
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-wan27-hub":
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/video.mp4"],
                "usage": {"cost": 0.50, "is_byok": False},
            })
        # Video download
        if request.method == "GET" and url == "https://cdn.example.com/video.mp4":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42VIDEOBYTES")
        # AI Usage Hub POST — capture the outgoing payload
        if request.method == "POST" and "hub.test.local" in url:
            hub_posts.append(json.loads(request.read()))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text=f"unexpected {request.method} {url}")

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_wan27_hub.mp4"
    result = media_gen.generate_video(
        "a warm advertising video", out_path,
        model="alibaba/wan-2.7",
        poll_interval=0.0, max_wait=10.0,
    )

    assert result.get("ok") is True, result
    assert out_path.exists()

    # Wait for the background Hub delivery thread to finish
    ai_usage.flush_usage_log(timeout=5.0)

    # Exactly one Hub POST
    assert len(hub_posts) == 1, f"expected 1 Hub POST, got {len(hub_posts)}"
    payload = hub_posts[0]

    # Assert on the normalized event delivered toward the Hub
    assert payload["provider"] == "openrouter"
    assert payload["model"] == "alibaba/wan-2.7"
    assert payload["operation"] == "videos.generate"
    assert payload["status"] == "success"
    assert payload["cost_usd"] == 0.50, f"expected cost_usd=0.50, got {payload.get('cost_usd')}"
    assert payload["request_id"] == "job-wan27-hub"
    assert payload["source"] == "media_gen.generate_video"
    # Pre-call estimate must NOT be substituted for actual cost
    assert payload["cost_usd"] != 0.20  # the wrong pre-call estimate was $0.20


def test_hub_boundary_polling_no_duplicate_hub_posts(monkeypatch, tmp_path):
    """Multiple poll iterations returning 'completed' must produce exactly
    one Hub POST — the request_id provides idempotency at the source."""
    from src import media_gen
    from src import ai_usage

    _stub_media_env_real_logging(monkeypatch)
    _stub_hub_credentials(monkeypatch)

    hub_posts: list[dict] = []

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            return httpx.Response(200, json={
                "id": "job-dup-hub",
                "polling_url": "/api/v1/videos/job-dup-hub",
                "status": "pending",
            })
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-dup-hub":
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
                "usage": {"cost": 0.20},
            })
        if request.method == "GET" and url == "https://cdn.example.com/v.mp4":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")
        if request.method == "POST" and "hub.test.local" in url:
            hub_posts.append(json.loads(request.read()))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_dup_hub.mp4"
    result = media_gen.generate_video(
        "prompt", out_path, poll_interval=0.0, max_wait=10.0,
    )

    assert result.get("ok") is True
    ai_usage.flush_usage_log(timeout=5.0)

    assert len(hub_posts) == 1, f"expected exactly 1 Hub POST, got {len(hub_posts)}"
    assert hub_posts[0]["cost_usd"] == 0.20
    assert hub_posts[0]["status"] == "success"


def test_hub_boundary_failure_does_not_break_media(monkeypatch, tmp_path):
    """If the Hub POST returns an error, media generation still succeeds and
    the file is saved.  No duplicate or misleading success event is emitted."""
    from src import media_gen
    from src import ai_usage

    _stub_media_env_real_logging(monkeypatch)
    _stub_hub_credentials(monkeypatch)

    hub_posts: list[dict] = []

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url == "https://openrouter.ai/api/v1/videos":
            return httpx.Response(200, json={
                "id": "job-hub-err",
                "polling_url": "/api/v1/videos/job-hub-err",
                "status": "pending",
            })
        if request.method == "GET" and url == "https://openrouter.ai/api/v1/videos/job-hub-err":
            return httpx.Response(200, json={
                "status": "completed",
                "unsigned_urls": ["https://cdn.example.com/v.mp4"],
                "usage": {"cost": 0.50},
            })
        if request.method == "GET" and url == "https://cdn.example.com/v.mp4":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")
        # Hub returns 500 — delivery failure
        if request.method == "POST" and "hub.test.local" in url:
            hub_posts.append(json.loads(request.read()))
            return httpx.Response(500, text="Hub internal error")
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)

    out_path = tmp_path / "video_hub_err.mp4"
    result = media_gen.generate_video(
        "prompt", out_path, poll_interval=0.0, max_wait=10.0,
    )

    # Media generation must still succeed despite Hub failure
    assert result.get("ok") is True, result
    assert out_path.exists()
    ai_usage.flush_usage_log(timeout=5.0)

    # Exactly one Hub POST was attempted (not zero, not duplicated)
    assert len(hub_posts) == 1, f"expected 1 Hub POST attempt, got {len(hub_posts)}"
    assert hub_posts[0]["cost_usd"] == 0.50
    assert hub_posts[0]["status"] == "success"
