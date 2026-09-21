"""Offline qualification tests for the C2 qualification launcher.

NO REAL NETWORK.  All HTTP is faked by replacing ``httpx.Client.send`` with a
fake handler BEFORE installing the guard, so the guard delegates to the fake
instead of the real network.

Covers 18 requirements:
  Pricing gate:     1-3
  Global LLM guard: 4-11
  Media one-shot:   12-15
  Evidence:         16-18
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import qual_c2_launcher as qual


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
IMAGE_URL = "https://openrouter.ai/api/v1/images"
VIDEO_URL = "https://openrouter.ai/api/v1/videos"
CAPS_URL = "https://openrouter.ai/api/v1/videos/models"


def _fake_chat_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "fake-id",
            "object": "chat.completion",
            "created": 1,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _fake_image_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [{"b64_json": base64.b64encode(b"fake").decode()}],
        },
    )


def _fake_video_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": "vid-job", "polling_url": "/api/v1/videos/vid-job"},
    )


def _fake_caps_response() -> httpx.Response:
    return httpx.Response(200, json={"data": []})


def _make_fake_send(call_log: list):
    """Create a fake send that records calls and returns appropriate responses."""

    def _fake_send(self, request, *, stream=False, **kwargs):
        call_log.append(
            {
                "method": request.method,
                "url": str(request.url),
                "stream": stream,
            }
        )
        url = str(request.url)
        if "/chat/completions" in url:
            resp = _fake_chat_response()
        elif "/api/v1/images" in url and "/images/models" not in url:
            resp = _fake_image_response()
        elif "/api/v1/videos" in url and "/videos/models" not in url:
            resp = _fake_video_response()
        else:
            resp = _fake_caps_response()
        # OpenAI SDK calls response.raise_for_status() which needs _request set
        resp.request = request
        return resp

    return _fake_send


@pytest.fixture
def guards_installed(call_log=None):
    """Install guards with a fake send as the underlying transport.

    Order:
      1. Save real httpx.Client.send
      2. Replace httpx.Client.send with fake_send
      3. Call install_qualification_guards() — captures fake_send as _original
      4. Now httpx.Client.send is the guard, delegating to fake_send
      5. Teardown: restore real httpx.Client.send
    """
    call_log: list = []
    real_send = httpx.Client.send
    fake_send = _make_fake_send(call_log)
    httpx.Client.send = fake_send
    qual.reset_qualification_state()
    qual._original_httpx_send = fake_send
    qual._guards_installed = True
    # Patch the module-level _original_httpx_send directly so the guard
    # wrapper delegates to the fake without needing install().
    # But we also need httpx.Client.send to be the guard wrapper.
    httpx.Client.send = qual._qual_httpx_send
    yield call_log
    httpx.Client.send = real_send
    qual._guards_installed = False
    qual._original_httpx_send = real_send
    qual.reset_qualification_state()


def _make_chat_request(
    url: str = CHAT_URL, json_body: dict | None = None
) -> httpx.Request:
    body = json_body or {"model": "test", "messages": []}
    return httpx.Request("POST", url, json=body)


def _make_image_request(refs: list[dict] | None = None) -> httpx.Request:
    body: dict = {"model": "test-image", "prompt": "test"}
    if refs is not None:
        body["input_references"] = refs
    return httpx.Request("POST", IMAGE_URL, json=body)


def _make_video_request(
    refs: list[dict] | None = None, duration: int = 10
) -> httpx.Request:
    body: dict = {
        "model": "test-video",
        "prompt": "test",
        "duration": duration,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    }
    if refs is not None:
        body["input_references"] = refs
    return httpx.Request("POST", VIDEO_URL, json=body)


def _make_caps_request() -> httpx.Request:
    return httpx.Request("GET", CAPS_URL)


# ===========================================================================
# Pricing gate tests (1-3)
# ===========================================================================


class TestPricingGate:
    """Requirements 1-3: pre-paid pricing gate behavior."""

    def _make_fake_media_gen(self, caps: dict, caps_text: str):
        """Create a fake media_gen module with controlled capabilities."""
        mg = MagicMock()
        mg._load_media_config.return_value = {"video_model": "bytedance/seedance-2.0-fast"}
        mg.DEFAULT_VIDEO_MODEL = "bytedance/seedance-2.0-fast"
        mg.get_model_capabilities.return_value = caps
        mg.format_capabilities_for_prompt.return_value = caps_text
        return mg

    def test_1_pricing_available_gate_pass(self):
        """Req 1: pricing metadata available → gate PASS."""
        caps = {
            "durations": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            "pricing_skus": {"duration_seconds": "0.09"},
        }
        caps_text = (
            "Model bytedance/seedance-2.0-fast รองรับ: duration: 4-15 วินาที | "
            "cost โดยประมาณ (provider-advertised): ~$0.09/วินาที"
        )
        mg = self._make_fake_media_gen(caps, caps_text)
        ok, evidence, msg = qual.pre_paid_pricing_gate(mg)
        assert ok is True
        assert msg == "PASS"
        assert evidence["has_usable_duration_guidance"] is True
        assert evidence["caps_text_contains_cost"] is True

    def test_2_pricing_skus_missing_gate_aborts(self):
        """Req 2: pricing_skus missing → launcher aborts before any paid request."""
        caps = {"durations": [4, 5, 6], "pricing_skus": {}}
        caps_text = "Model test รองรับ: duration: 4-6 วินาที"
        mg = self._make_fake_media_gen(caps, caps_text)
        ok, evidence, msg = qual.pre_paid_pricing_gate(mg)
        assert ok is False
        assert "pricing" in msg.lower()
        assert evidence["has_usable_duration_guidance"] is False

    def test_3_cost_text_missing_gate_aborts(self):
        """Req 3: formatted cost context missing → launcher aborts."""
        caps = {
            "durations": [4, 5, 6],
            "pricing_skus": {"duration_seconds": "0.09"},
        }
        caps_text = "Model test รองรับ: duration: 4-6 วินาที"  # no cost text
        mg = self._make_fake_media_gen(caps, caps_text)
        ok, evidence, msg = qual.pre_paid_pricing_gate(mg)
        assert ok is False
        assert "cost text" in msg
        assert evidence["caps_text_contains_cost"] is False


# ===========================================================================
# Global LLM guard tests (4-11)
# ===========================================================================


class TestLLMGuard:
    """Requirements 4-11: global LLM call guard at httpx.Client.send."""

    def test_4_non_stream_chat_counted(self, guards_installed):
        """Req 4: normal non-stream chat request is counted."""
        client = httpx.Client()
        client.post(CHAT_URL, json={"model": "x", "messages": []})
        state = qual.get_qualification_state()
        assert state["llm_count"] == 1
        assert len(guards_installed) == 1
        assert guards_installed[0]["url"] == CHAT_URL
        client.close()

    def test_5_stream_chat_counted(self, guards_installed):
        """Req 5: streaming chat request is counted."""
        client = httpx.Client()
        with client.stream("POST", CHAT_URL, json={"model": "x", "messages": []}) as resp:
            resp.read()
        state = qual.get_qualification_state()
        assert state["llm_count"] == 1
        assert guards_installed[0]["stream"] is True
        client.close()

    def test_6_openai_sdk_chat_counted(self, guards_installed):
        """Req 6: OpenAI-SDK/tool-style chat request counted through same guard."""
        from openai import OpenAI

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key="fake-key",
            max_retries=0,
        )
        # This goes through SyncAPIClient._send_request → self._client.send()
        # which is our patched httpx.Client.send.
        resp = client.chat.completions.create(
            model="test",
            messages=[{"role": "user", "content": "hi"}],
        )
        state = qual.get_qualification_state()
        assert state["llm_count"] == 1
        assert resp.choices[0].message.content == "ok"
        client.close()

    def test_7_metadata_get_not_counted(self, guards_installed):
        """Req 7: metadata GET is NOT counted as LLM."""
        client = httpx.Client()
        client.get(CAPS_URL)
        state = qual.get_qualification_state()
        assert state["llm_count"] == 0
        assert state["image_count"] == 0
        assert state["video_count"] == 0
        client.close()

    def test_8_image_video_not_counted_as_llm(self, guards_installed):
        """Req 8: image/video endpoints are NOT counted as LLM."""
        client = httpx.Client()
        client.post(IMAGE_URL, json={"model": "img", "prompt": "x"})
        client.post(VIDEO_URL, json={"model": "vid", "prompt": "x"})
        state = qual.get_qualification_state()
        assert state["llm_count"] == 0
        assert state["image_count"] == 1
        assert state["video_count"] == 1
        client.close()

    def test_9_calls_1_to_15_allowed(self, guards_installed):
        """Req 9: calls 1..15 are allowed."""
        client = httpx.Client()
        for i in range(qual.QUAL_C2_MAX_LLM_CALLS):
            client.post(CHAT_URL, json={"model": "x", "messages": []})
        state = qual.get_qualification_state()
        assert state["llm_count"] == qual.QUAL_C2_MAX_LLM_CALLS
        assert len(guards_installed) == qual.QUAL_C2_MAX_LLM_CALLS
        client.close()

    def test_10_call_16_blocked_before_send(self, guards_installed):
        """Req 10: call 16 is blocked BEFORE underlying HTTP send."""
        client = httpx.Client()
        for i in range(qual.QUAL_C2_MAX_LLM_CALLS):
            client.post(CHAT_URL, json={"model": "x", "messages": []})
        # Call 16 must be blocked
        with pytest.raises(qual.QualificationLLMCapExceeded) as exc_info:
            client.post(CHAT_URL, json={"model": "x", "messages": []})
        assert "BLOCKED" in str(exc_info.value)
        # Underlying send was called exactly 15 times, not 16
        assert len(guards_installed) == qual.QUAL_C2_MAX_LLM_CALLS
        state = qual.get_qualification_state()
        assert state["llm_count"] == qual.QUAL_C2_MAX_LLM_CALLS
        client.close()

    def test_11_second_client_cannot_bypass(self, guards_installed):
        """Req 11: counter cannot be bypassed by a second client instance."""
        client1 = httpx.Client()
        client2 = httpx.Client()
        # Exhaust cap with client1
        for i in range(qual.QUAL_C2_MAX_LLM_CALLS):
            client1.post(CHAT_URL, json={"model": "x", "messages": []})
        # client2 must also be blocked — same class-level patch
        with pytest.raises(qual.QualificationLLMCapExceeded):
            client2.post(CHAT_URL, json={"model": "x", "messages": []})
        state = qual.get_qualification_state()
        assert state["llm_count"] == qual.QUAL_C2_MAX_LLM_CALLS
        client1.close()
        client2.close()


# ===========================================================================
# Media one-shot tests (12-15)
# ===========================================================================


class TestMediaOneShot:
    """Requirements 12-15: image/video one-shot guards."""

    def test_12_first_image_allowed(self, guards_installed):
        """Req 12: first image request is allowed."""
        client = httpx.Client()
        resp = client.post(IMAGE_URL, json={"model": "img", "prompt": "x"})
        assert resp.status_code == 200
        state = qual.get_qualification_state()
        assert state["image_count"] == 1
        client.close()

    def test_13_second_image_blocked(self, guards_installed):
        """Req 13: second image request is blocked before network."""
        client = httpx.Client()
        client.post(IMAGE_URL, json={"model": "img", "prompt": "x"})
        with pytest.raises(qual.QualificationMediaCapExceeded) as exc_info:
            client.post(IMAGE_URL, json={"model": "img", "prompt": "x"})
        assert "Image" in str(exc_info.value)
        assert "BLOCKED" in str(exc_info.value)
        # Only 1 image send reached the fake
        image_sends = [
            c for c in guards_installed if "/api/v1/images" in c["url"]
        ]
        assert len(image_sends) == 1
        client.close()

    def test_14_first_video_allowed(self, guards_installed):
        """Req 14: first video request is allowed."""
        client = httpx.Client()
        resp = client.post(VIDEO_URL, json={"model": "vid", "prompt": "x"})
        assert resp.status_code == 200
        state = qual.get_qualification_state()
        assert state["video_count"] == 1
        client.close()

    def test_15_second_video_blocked(self, guards_installed):
        """Req 15: second video request is blocked before network."""
        client = httpx.Client()
        client.post(VIDEO_URL, json={"model": "vid", "prompt": "x"})
        with pytest.raises(qual.QualificationMediaCapExceeded) as exc_info:
            client.post(VIDEO_URL, json={"model": "vid", "prompt": "x"})
        assert "Video" in str(exc_info.value)
        assert "BLOCKED" in str(exc_info.value)
        video_sends = [
            c for c in guards_installed if "/api/v1/videos" in c["url"]
        ]
        assert len(video_sends) == 1
        client.close()


# ===========================================================================
# Evidence interception tests (16-18)
# ===========================================================================


class TestEvidenceCapture:
    """Requirements 16-18: outgoing-reference HTTP fingerprint capture."""

    def test_16_fingerprint_matches_input_references(self, guards_installed):
        """Req 16: provider payload fingerprint matches references entering generation."""
        # Create a data-URL reference
        raw_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"
        expected_fp = hashlib.sha256(b64.encode("ascii")).hexdigest()

        refs = [{"type": "image_url", "image_url": {"url": data_url}}]
        client = httpx.Client()
        client.post(IMAGE_URL, json={
            "model": "img",
            "prompt": "test",
            "input_references": refs,
        })
        state = qual.get_qualification_state()
        assert len(state["media_evidence"]) == 1
        ev = state["media_evidence"][0]
        assert ev["kind"] == "image"
        assert ev["num_references"] == 1
        assert len(ev["reference_fingerprints"]) == 1
        assert ev["reference_fingerprints"][0]["sha256"] == expected_fp
        client.close()

    def test_17_no_raw_base64_stored(self, guards_installed):
        """Req 17: no raw base64 is stored in evidence."""
        raw_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"
        refs = [{"type": "image_url", "image_url": {"url": data_url}}]
        client = httpx.Client()
        client.post(IMAGE_URL, json={
            "model": "img",
            "prompt": "test",
            "input_references": refs,
        })
        state = qual.get_qualification_state()
        ev = state["media_evidence"][0]
        # Serialize evidence to JSON and verify no base64 content leaks
        ev_json = json.dumps(ev)
        assert b64 not in ev_json, "Raw base64 content found in evidence JSON"
        assert "data:image" not in ev_json, "Data URL found in evidence JSON"
        client.close()

    def test_18_payload_forwarded_unchanged(self, guards_installed):
        """Req 18: interceptor forwards the payload unchanged to fake send."""
        # The fake send records the request. We verify the request content
        # matches what was sent (guard did not mutate it).
        original_body = {"model": "vid", "prompt": "test", "duration": 7}
        client = httpx.Client()
        client.post(VIDEO_URL, json=original_body)
        # The fake send should have received the unmodified request
        # We check via the call log — but the call log only records URL/method.
        # Instead, let's verify by checking that the response came through
        # correctly (pass-through).
        state = qual.get_qualification_state()
        assert state["video_count"] == 1
        ev = state["media_evidence"][0]
        assert ev["video_duration"] == 7
        assert ev["model"] == "vid"
        client.close()

    def test_18b_payload_not_mutated(self, guards_installed):
        """Req 18b: the request object content is not mutated by the guard."""
        # Use a custom fake send that captures the raw request content
        captured_content: list[bytes] = []

        def _capturing_send(self, request, *, stream=False, **kwargs):
            captured_content.append(bytes(request.content))
            url = str(request.url)
            if "/chat/completions" in url:
                return _fake_chat_response()
            if "/api/v1/images" in url:
                return _fake_image_response()
            if "/api/v1/videos" in url:
                return _fake_video_response()
            return _fake_caps_response()

        # Temporarily replace the _original_httpx_send with our capturing version
        real_original = qual._original_httpx_send
        qual._original_httpx_send = _capturing_send
        try:
            client = httpx.Client()
            original_body = {
                "model": "vid",
                "prompt": "test",
                "duration": 5,
                "input_references": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
                ],
            }
            client.post(VIDEO_URL, json=original_body)
            client.close()
            # Verify the captured content matches what we sent
            assert len(captured_content) == 1
            sent = json.loads(captured_content[0])
            assert sent["model"] == "vid"
            assert sent["duration"] == 5
            assert len(sent["input_references"]) == 1
            assert sent["input_references"][0]["image_url"]["url"] == "data:image/png;base64,abc"
        finally:
            qual._original_httpx_send = real_original


# ===========================================================================
# Production call-shape integration tests (req 2: real UI kwargs)
# ===========================================================================


class TestProductionCallShape:
    """Prove the qualification wrappers accept the real web_viewer kwargs.

    Real call sites (web_viewer.py):
      generate_image_with_retry(prompt, path, llm=retry_llm,
          on_retry=_img_retry, model=, aspect_ratio=, input_references=, visual=)

      generate_video_with_retry(prompt, path, llm=retry_llm,
          on_retry=_vid_retry, on_status=_vid_status,
          duration=, aspect_ratio=, resolution=, input_references=, visual=)
    """

    def _make_fake_media_gen(self):
        """Create a fake media_gen module with stubbed one-shot functions."""
        from unittest.mock import MagicMock
        mg = MagicMock()
        mg.DEFAULT_IMAGE_MODEL = "google/gemini-3.1-flash-image"
        mg.DEFAULT_VIDEO_MODEL = "bytedance/seedance-2.0-fast"
        mg._load_media_config.return_value = {
            "image_model": "google/gemini-3.1-flash-image",
            "video_model": "bytedance/seedance-2.0-fast",
        }
        mg.get_model_capabilities.return_value = {
            "durations": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            "pricing_skus": {"per-video-second": 0.09},
        }
        mg.format_capabilities_for_prompt.return_value = (
            "cost โดยประมาณ (provider-advertised): ~$0.09/วินาที"
        )
        # Stub the one-shot functions to return a success result
        mg.generate_image.return_value = {
            "ok": True,
            "path": "/tmp/test.png",
            "model": "google/gemini-3.1-flash-image",
            "prompt": "test",
        }
        mg.generate_video.return_value = {
            "ok": True,
            "path": "/tmp/test.mp4",
            "model": "bytedance/seedance-2.0-fast",
            "prompt": "test",
        }
        return mg

    def test_image_wrapper_accepts_production_kwargs(self):
        """Image wrapper accepts llm, on_retry, model, aspect_ratio, etc."""
        mg = self._make_fake_media_gen()
        qual.install_media_one_shot(mg)
        result = mg.generate_image_with_retry(
            "test prompt",
            "/tmp/out.png",
            llm=MagicMock(),
            on_retry=lambda old, new, err: None,
            model="google/gemini-3.1-flash-image",
            aspect_ratio="16:9",
            timeout=180.0,
            input_references=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}],
            visual={"tone": "warm"},
        )
        assert result["ok"] is True
        assert result["retry_count"] == 0
        assert result["retry_history"] == []
        # Exactly one underlying generate_image call
        assert mg.generate_image.call_count == 1

    def test_video_wrapper_accepts_production_kwargs(self):
        """Video wrapper accepts llm, on_retry, on_status, duration, etc."""
        mg = self._make_fake_media_gen()
        qual.install_media_one_shot(mg)
        result = mg.generate_video_with_retry(
            "test prompt",
            "/tmp/out.mp4",
            llm=MagicMock(),
            on_retry=lambda old, new, err: None,
            on_status=lambda s: None,
            model="bytedance/seedance-2.0-fast",
            duration=7,
            aspect_ratio="16:9",
            resolution="720p",
            poll_interval=5.0,
            max_wait=300.0,
            input_references=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}],
            frame_images=None,
            visual={"tone": "warm"},
        )
        assert result["ok"] is True
        assert result["retry_count"] == 0
        assert result["retry_history"] == []
        assert mg.generate_video.call_count == 1

    def test_image_wrapper_no_typeerror_on_extra_kwargs(self):
        """No TypeError from unexpected kwargs (defensive **_extra)."""
        mg = self._make_fake_media_gen()
        qual.install_media_one_shot(mg)
        result = mg.generate_image_with_retry(
            "prompt", "/tmp/out.png",
            llm=None, on_retry=None,
            unexpected_kwarg="value",
        )
        assert result["ok"] is True

    def test_video_wrapper_no_typeerror_on_extra_kwargs(self):
        """No TypeError from unexpected kwargs (defensive **_extra)."""
        mg = self._make_fake_media_gen()
        qual.install_media_one_shot(mg)
        result = mg.generate_video_with_retry(
            "prompt", "/tmp/out.mp4",
            llm=None, on_retry=None, on_status=None,
            unexpected_kwarg="value",
        )
        assert result["ok"] is True

    def test_image_result_shape_ui_compatible(self):
        """Result has ok, path, model, prompt, retry_count, retry_history."""
        mg = self._make_fake_media_gen()
        qual.install_media_one_shot(mg)
        result = mg.generate_image_with_retry("p", "/tmp/o.png")
        assert "ok" in result
        assert "path" in result
        assert "retry_count" in result
        assert "retry_history" in result

    def test_video_result_shape_ui_compatible(self):
        """Result has ok, path, model, prompt, retry_count, retry_history."""
        mg = self._make_fake_media_gen()
        qual.install_media_one_shot(mg)
        result = mg.generate_video_with_retry("p", "/tmp/o.mp4")
        assert "ok" in result
        assert "path" in result
        assert "retry_count" in result
        assert "retry_history" in result

    def test_image_exactly_one_underlying_call(self):
        """Exactly one underlying generate_image call per wrapper invocation."""
        mg = self._make_fake_media_gen()
        qual.install_media_one_shot(mg)
        mg.generate_image_with_retry("p1", "/tmp/o1.png")
        mg.generate_image_with_retry("p2", "/tmp/o2.png")
        assert mg.generate_image.call_count == 2  # one per invocation, no retry

    def test_video_exactly_one_underlying_call(self):
        """Exactly one underlying generate_video call per wrapper invocation."""
        mg = self._make_fake_media_gen()
        qual.install_media_one_shot(mg)
        mg.generate_video_with_retry("p1", "/tmp/o1.mp4")
        mg.generate_video_with_retry("p2", "/tmp/o2.mp4")
        assert mg.generate_video.call_count == 2  # one per invocation, no retry

    def test_http_hard_caps_remain_intact_with_wrappers(self, guards_installed):
        """Second HTTP media submission still blocked even with wrappers."""
        client = httpx.Client()
        # First image allowed
        resp1 = client.post(IMAGE_URL, json={"model": "img", "prompt": "x"})
        assert resp1.status_code == 200
        # Second image blocked at HTTP level
        with pytest.raises(qual.QualificationMediaCapExceeded):
            client.post(IMAGE_URL, json={"model": "img", "prompt": "x"})
        client.close()

    def test_on_status_forwarded_to_underlying(self):
        """on_status callback is forwarded to the real generate_video."""
        mg = self._make_fake_media_gen()
        qual.install_media_one_shot(mg)
        status_calls: list[str] = []
        mg.generate_video_with_retry(
            "p", "/tmp/o.mp4",
            on_status=lambda s: status_calls.append(s),
        )
        # Verify on_status was forwarded as a kwarg to generate_video
        call_kwargs = mg.generate_video.call_args.kwargs
        assert call_kwargs.get("on_status") is not None


# ===========================================================================
# Pre-run price estimate tests
# ===========================================================================


class TestPreRunEstimate:
    """Verify the pre-run price estimate uses refreshed pricing_skus."""

    def _make_fake_media_gen(self, per_second=0.09):
        from unittest.mock import MagicMock
        mg = MagicMock()
        mg.DEFAULT_IMAGE_MODEL = "google/gemini-3.1-flash-image"
        mg.DEFAULT_VIDEO_MODEL = "bytedance/seedance-2.0-fast"
        mg._load_media_config.return_value = {
            "image_model": "google/gemini-3.1-flash-image",
            "video_model": "bytedance/seedance-2.0-fast",
        }
        mg.get_model_capabilities.return_value = {
            "durations": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            "pricing_skus": {"duration_seconds": str(per_second)},
        }
        mg.format_capabilities_for_prompt.return_value = "cost text"
        return mg

    def test_estimate_uses_refreshed_per_second(self):
        """Estimate uses duration_seconds from refreshed caps."""
        mg = self._make_fake_media_gen(per_second=0.09)
        est = qual.pre_run_price_estimate(mg)
        assert est["per_video_second"] == 0.09
        assert est["video_estimate_type"] == "per_second"
        assert est["max_duration"] == 15
        assert est["worst_video_cost_15s"] == round(0.09 * 15, 4)

    def test_estimate_expected_video_uses_5s(self):
        """Expected video cost uses 5s (short video, not forced)."""
        mg = self._make_fake_media_gen(per_second=0.09)
        est = qual.pre_run_price_estimate(mg)
        assert est["expected_video_duration_seconds"] == 5
        assert est["expected_video_cost"] == round(0.09 * 5, 4)

    def test_estimate_states_not_guaranteed(self):
        """Estimate note says dollar amounts are not guaranteed."""
        mg = self._make_fake_media_gen()
        est = qual.pre_run_price_estimate(mg)
        assert "ESTIMATES" in est["note"]
        assert "hard-capped" in est["note"]

    def test_estimate_total_under_caps(self):
        """Worst-case total uses LLM=15, image=1, video=1 at max duration."""
        mg = self._make_fake_media_gen(per_second=0.09)
        est = qual.pre_run_price_estimate(mg)
        # worst = image + worst_video + worst_llm
        assert est["worst_case_total"] == round(
            0.07 + round(0.09 * 15, 4) + round(15 * 0.025, 4), 4
        )

    def test_estimate_token_based_no_fake_per_second(self):
        """Token-based provider: no fabricated per-second video cost."""
        from unittest.mock import MagicMock
        mg = MagicMock()
        mg.DEFAULT_VIDEO_MODEL = "bytedance/seedance-2.0-fast"
        mg.DEFAULT_IMAGE_MODEL = "google/gemini-3.1-flash-image"
        mg._load_media_config.return_value = {
            "video_model": "bytedance/seedance-2.0-fast",
            "image_model": "google/gemini-3.1-flash-image",
        }
        mg.get_model_capabilities.return_value = {
            "durations": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            "pricing_skus": {"video_tokens": "0.0000042"},
        }
        mg.format_capabilities_for_prompt.return_value = "cost text"
        est = qual.pre_run_price_estimate(mg)
        assert est["video_estimate_type"] == "token_based"
        assert est["per_video_second"] is None
        assert est["expected_video_cost"] is None
        assert est["worst_video_cost_15s"] is None
        assert est["token_pricing"] is not None
