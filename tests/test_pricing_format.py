"""Focused tests for generic video pricing metadata formatting.

Covers the three actual pricing SKU families returned by the OpenRouter
video-models API:

  duration_seconds*  — direct USD per second
  cents_per_second*  — cents per second (needs /100)
  video_tokens*      — per-token pricing (no fake per-second conversion)

Plus: unknown SKUs, empty pricing, existing formatting regression, and
qualification gate semantics.

No real network — all tests monkeypatch get_model_capabilities.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _patch_caps(monkeypatch, caps):
    from src import media_gen
    monkeypatch.setattr(media_gen, "get_model_capabilities", lambda *a, **k: caps)
    return media_gen


# ---------------------------------------------------------------------------
# 1. duration_seconds* → $/second
# ---------------------------------------------------------------------------
class TestDurationSecondsFamily:
    def test_duration_seconds_formats_as_per_second(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5, 6, 7, 8],
            "aspect_ratios": ["16:9"],
            "resolutions": ["720p"],
            "pricing_skus": {"duration_seconds": "0.08"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "~$0.08/วินาที" in text, f"expected per-second: {text}"

    def test_duration_seconds_resolution_variants(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5],
            "pricing_skus": {
                "duration_seconds_480p": "0.05",
                "duration_seconds_768p": "0.08",
            },
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "~$0.05/วินาที" in text
        assert "~$0.08/วินาที" in text

    def test_duration_seconds_no_token_disclaimer(self, monkeypatch):
        """Per-second pricing should NOT get the token 'longer duration' suffix."""
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5],
            "pricing_skus": {"duration_seconds": "0.08"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "ค่าใช้จ่ายสูงขึ้น" not in text, \
            f"per-second pricing should not get token disclaimer: {text}"


# ---------------------------------------------------------------------------
# 2. cents_per_second* → convert cents to dollars
# ---------------------------------------------------------------------------
class TestCentsPerSecondFamily:
    def test_cents_per_second_converts_to_dollars(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5],
            "pricing_skus": {"cents_per_second_output": "12"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "~$0.12/วินาที" in text, f"12 cents → $0.12: {text}"

    def test_cents_per_second_resolution_variants(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5],
            "pricing_skus": {
                "cents_per_video_output_second_480p": "5",
                "cents_per_video_output_second_720p": "10",
            },
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "~$0.05/วินาที" in text
        assert "~$0.10/วินาที" in text

    def test_cents_per_second_no_token_disclaimer(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5],
            "pricing_skus": {"cents_per_second_output": "12"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "ค่าใช้จ่ายสูงขึ้น" not in text


# ---------------------------------------------------------------------------
# 3. video_tokens* → token pricing (no fake per-second)
# ---------------------------------------------------------------------------
class TestVideoTokensFamily:
    def test_video_tokens_formats_as_per_million(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            "pricing_skus": {"video_tokens": "0.0000042"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "$4.20" in text, f"0.0000042 → $4.20/M: {text}"
        assert "/M" in text or "1M" in text, f"per-million notation: {text}"

    def test_video_tokens_no_fake_per_second(self, monkeypatch):
        """Token pricing must NOT invent a $/second conversion."""
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5, 6],
            "pricing_skus": {"video_tokens": "0.0000042"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        # Must NOT contain a per-second price line for token models
        assert "/วินาที" not in text.split("cost")[1] if "cost" in text else True
        # More precisely: the cost line should not have ~$X/วินาที
        cost_line = [p for p in text.split("|") if "cost" in p.lower()]
        if cost_line:
            assert "~$" not in cost_line[0] or "/วินาที" not in cost_line[0], \
                f"token pricing must not show per-second: {text}"

    def test_video_tokens_includes_duration_cost_guidance(self, monkeypatch):
        """Token-based models get grounded 'longer duration = higher cost' guidance."""
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5, 6, 7, 8],
            "pricing_skus": {"video_tokens": "0.0000042"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "ค่าใช้จ่ายสูงขึ้น" in text, \
            f"token models must get duration-cost guidance: {text}"

    def test_video_tokens_variants_all_formatted(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5],
            "pricing_skus": {
                "video_tokens": "0.0000042",
                "video_tokens_without_audio": "0.0000042",
                "video_tokens_with_video_input": "0.000002475",
            },
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "$4.20" in text  # video_tokens
        assert "$2.48" in text or "$2.475" in text  # video_tokens_with_video_input


# ---------------------------------------------------------------------------
# 4. Unknown SKUs — safe fallback, no crash
# ---------------------------------------------------------------------------
class TestUnknownSkus:
    def test_unknown_sku_does_not_crash(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5],
            "pricing_skus": {"some_unknown_sku": "0.5"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "some_unknown_sku" in text
        assert "$0.5" in text

    def test_unknown_sku_no_duration_guidance(self, monkeypatch):
        """Unknown SKUs should not get the 'longer duration' guidance."""
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5],
            "pricing_skus": {"some_unknown_sku": "0.5"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "ค่าใช้จ่ายสูงขึ้น" not in text, \
            f"unknown SKU should not get duration guidance: {text}"


# ---------------------------------------------------------------------------
# 5. Empty/missing pricing
# ---------------------------------------------------------------------------
class TestEmptyPricing:
    def test_empty_pricing_skus_no_cost_line(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5],
            "pricing_skus": {},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "cost" not in text.lower()
        assert "$" not in text

    def test_no_pricing_skus_key(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5],
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "cost" not in text.lower()


# ---------------------------------------------------------------------------
# 6. Existing formatting unchanged
# ---------------------------------------------------------------------------
class TestExistingFormatting:
    def test_duration_aspect_resolution_still_present(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5, 6],
            "aspect_ratios": ["16:9", "9:16"],
            "resolutions": ["480p", "720p"],
            "generate_audio": True,
            "pricing_skus": {"video_tokens": "0.0000042"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert "duration: 4-6 วินาที" in text
        assert "16:9" in text and "9:16" in text
        assert "480p" in text and "720p" in text
        assert "audio: สร้างเสียงได้" in text

    def test_empty_caps_returns_empty_string(self, monkeypatch):
        mg = _patch_caps(monkeypatch, {})
        text = mg.format_capabilities_for_prompt("m", kind="video")
        assert text == ""


# ---------------------------------------------------------------------------
# 7. Agent 4 cost-conscious instruction preserved
# ---------------------------------------------------------------------------
class TestAgentInstruction:
    def test_agent_prompt_still_has_cost_instruction(self):
        """The Agent 4 prompt must still tell the agent to choose short durations."""
        from src.agents.content_creator import ContentCreatorAgent
        src = ContentCreatorAgent.__init__.__code__.co_consts
        # The instruction is in a format string in the prompt builder
        # Check the source file directly
        import inspect
        source = inspect.getsource(ContentCreatorAgent)
        assert "เลือก duration ที่สั้นที่สุด" in source, \
            "Agent 4 must still have cost-conscious duration instruction"
        assert "ถ้า user ระบุ duration" in source, \
            "Agent 4 must still respect explicit user duration"


# ---------------------------------------------------------------------------
# 8. No hard duration clamp
# ---------------------------------------------------------------------------
class TestNoDurationClamp:
    def test_no_hardcoded_duration_in_formatter(self, monkeypatch):
        """Formatter must not introduce a hardcoded duration."""
        mg = _patch_caps(monkeypatch, {
            "durations": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            "pricing_skus": {"video_tokens": "0.0000042"},
        })
        text = mg.format_capabilities_for_prompt("m", kind="video")
        # Must not force a specific duration
        assert "duration: 5" not in text.split("วินาที")[0] or "4-15" in text
        assert "duration: 4-15 วินาที" in text  # shows full range, no clamp


# ---------------------------------------------------------------------------
# 9. Qualification gate semantics
# ---------------------------------------------------------------------------
class TestQualificationGate:
    """Test the qualification gate's usable-duration-cost-guidance check."""

    def _make_fake_media_gen(self, caps, caps_text):
        from unittest.mock import MagicMock
        mg = MagicMock()
        mg._load_media_config.return_value = {"video_model": "bytedance/seedance-2.0-fast"}
        mg.DEFAULT_VIDEO_MODEL = "bytedance/seedance-2.0-fast"
        mg.get_model_capabilities.return_value = caps
        mg.format_capabilities_for_prompt.return_value = caps_text
        return mg

    def test_gate_passes_for_token_pricing_with_duration_guidance(self):
        """Gate passes when token pricing + duration-cost guidance present."""
        from scripts.qual_c2_launcher import pre_paid_pricing_gate
        caps = {
            "durations": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            "pricing_skus": {"video_tokens": "0.0000042"},
        }
        caps_text = (
            "Model bytedance/seedance-2.0-fast รองรับ: duration: 4-15 วินาที | "
            "cost โดยประมาณ (provider-advertised): tokens: $4.20/M "
            "(ระยะเวลาที่ยาวขึ้น = ค่าใช้จ่ายสูงขึ้น)"
        )
        mg = self._make_fake_media_gen(caps, caps_text)
        ok, evidence, msg = pre_paid_pricing_gate(mg)
        assert ok is True, f"gate should pass for token pricing: {msg}"

    def test_gate_passes_for_per_second_pricing(self):
        """Gate passes when direct per-second pricing present."""
        from scripts.qual_c2_launcher import pre_paid_pricing_gate
        caps = {
            "durations": [4, 5, 6],
            "pricing_skus": {"duration_seconds": "0.08"},
        }
        caps_text = (
            "Model m รองรับ: duration: 4-6 วินาที | "
            "cost โดยประมาณ (provider-advertised): ~$0.08/วินาที"
        )
        mg = self._make_fake_media_gen(caps, caps_text)
        ok, evidence, msg = pre_paid_pricing_gate(mg)
        assert ok is True, f"gate should pass for per-second pricing: {msg}"

    def test_gate_fails_when_pricing_present_but_no_duration_guidance(self):
        """Gate fails when pricing exists but no usable duration-cost guidance."""
        from scripts.qual_c2_launcher import pre_paid_pricing_gate
        caps = {
            "durations": [4, 5, 6],
            "pricing_skus": {"some_unknown_sku": "0.5"},
        }
        # Cost text present but no duration-cost guidance
        caps_text = (
            "Model m รองรับ: duration: 4-6 วินาที | "
            "cost โดยประมาณ (provider-advertised): some_unknown_sku: $0.5"
        )
        mg = self._make_fake_media_gen(caps, caps_text)
        ok, evidence, msg = pre_paid_pricing_gate(mg)
        assert ok is False, f"gate should fail for unknown SKU: {msg}"
        assert "duration" in msg.lower() or "guidance" in msg.lower()

    def test_gate_fails_with_no_pricing(self):
        """Gate fails when no pricing metadata present."""
        from scripts.qual_c2_launcher import pre_paid_pricing_gate
        caps = {"durations": [4, 5, 6], "pricing_skus": {}}
        caps_text = "Model m รองรับ: duration: 4-6 วินาที"
        mg = self._make_fake_media_gen(caps, caps_text)
        ok, evidence, msg = pre_paid_pricing_gate(mg)
        assert ok is False
        assert "pricing" in msg.lower()
