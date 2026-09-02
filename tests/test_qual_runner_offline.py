"""Offline tests for the qualification runner.

These tests do not call the model or perform any paid work.  They verify the
dry-run cost planner and the hard PaidCallGuard defined in scripts/qual_runner.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import json
import pytest

# qual_runner is in scripts/, not src/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_dry_run_cost_plan_content_creator():
    """dry_run_cost_plan returns a bounded, actionable cost plan without network."""
    import qual_runner

    plan = qual_runner.dry_run_cost_plan(
        agent_key="content_creator",
        product_id="Lagenio K2",
        quick_brief="Focus on family lifestyle",
        content_count=2,
        platforms=["facebook", "tiktok"],
        auto_image=True,
        auto_video=False,
    )
    assert plan["agent_key"] == "content_creator"
    assert plan["max_calls"] > 0
    assert plan["media_calls"] > 0
    assert plan["expected_estimate"] > 0
    assert plan["expected_estimate"] <= plan["ceiling_stage_a"]
    assert any(c["kind"] == "image" for c in plan["expected_calls"])
    assert all(c.get("kind") for c in plan["expected_calls"])


def test_dry_run_cost_plan_competitor_analysis_counts_web_search():
    """competitor_analysis plan separates web-search calls from text calls,
    and the configured hard call cap is enforced."""
    import qual_runner

    plan = qual_runner.dry_run_cost_plan(
        agent_key="competitor_analysis",
        product_id="Lagenio K2",
        quick_brief="Compare with imoo Z1 and myFirst R1s",
    )
    assert plan["web_search_calls"] > 0
    assert plan["expected_estimate"] > 0
    assert plan["max_calls"] == qual_runner._load_qualification_config()["default_max_calls"]["competitor_analysis"]
    assert plan["status"].startswith("exceeds call cap") or len(plan["expected_calls"]) <= plan["max_calls"]


def test_paid_call_guard_blocks_after_max_calls(monkeypatch):
    """PaidCallGuard hard-caps the number of paid httpx calls."""
    import qual_runner

    # Zero out cumulative spend so the USD guard does not fire first.
    monkeypatch.setattr(qual_runner, "_read_total_cost", lambda: 0.0)
    monkeypatch.setattr(qual_runner, "_SESSION_BASELINE", 0.0)

    calls = []

    def fake_post(client, url, **kwargs):
        calls.append(url)
        return MagicMock()

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    with qual_runner.PaidCallGuard("A", 2) as guard:
        httpx.Client.post(None, "https://openrouter.ai/api/v1/chat/completions")
        httpx.Client.post(None, "https://openrouter.ai/api/v1/chat/completions")
        with pytest.raises(qual_runner.QualificationBudgetExhausted):
            httpx.Client.post(None, "https://openrouter.ai/api/v1/chat/completions")

    assert len(calls) == 2
    assert guard.actual_calls() == [
        {"url": "https://openrouter.ai/api/v1/chat/completions", "kind": "text", "allowed": True},
        {"url": "https://openrouter.ai/api/v1/chat/completions", "kind": "text", "allowed": True},
    ]


def test_paid_call_guard_blocks_by_usd_ceiling(monkeypatch):
    """PaidCallGuard blocks the next call when the conservative per-call reserve
    would push cumulative spend over the stage-A ceiling."""
    import qual_runner

    monkeypatch.setattr(qual_runner, "_read_total_cost", lambda: 0.78)
    monkeypatch.setattr(qual_runner, "_SESSION_BASELINE", 0.0)

    with qual_runner.PaidCallGuard("A", 100):
        with pytest.raises(qual_runner.QualificationBudgetExhausted):
            httpx.Client.post(None, "https://openrouter.ai/api/v1/chat/completions")


def test_paid_call_guard_blocks_relative_chat_completions(monkeypatch):
    """Production calls /chat/completions relative to an openrouter base_url
    must be counted and blocked at the hard call cap."""
    import qual_runner

    monkeypatch.setattr(qual_runner, "_read_total_cost", lambda: 0.0)
    monkeypatch.setattr(qual_runner, "_SESSION_BASELINE", 0.0)

    calls = []

    def fake_post(client, url, **kwargs):
        calls.append(url)
        return MagicMock()

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    client = httpx.Client(base_url="https://openrouter.ai/api/v1/")
    with qual_runner.PaidCallGuard("A", 1) as guard:
        client.post("/chat/completions")
        with pytest.raises(qual_runner.QualificationBudgetExhausted):
            client.post("/chat/completions")

    assert calls == ["/chat/completions"]
    assert guard.actual_calls()[0] == {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "kind": "text",
        "allowed": True,
    }


def test_paid_call_guard_blocks_relative_stream(monkeypatch):
    """Production streaming requests (relative /chat/completions) are guarded."""
    import qual_runner

    monkeypatch.setattr(qual_runner, "_read_total_cost", lambda: 0.0)
    monkeypatch.setattr(qual_runner, "_SESSION_BASELINE", 0.0)

    calls = []

    def fake_stream(client, method, url, **kwargs):
        calls.append((method, url))
        return MagicMock()

    monkeypatch.setattr(httpx.Client, "stream", fake_stream)

    client = httpx.Client(base_url="https://openrouter.ai/api/v1/")
    with qual_runner.PaidCallGuard("A", 1) as guard:
        client.stream("POST", "/chat/completions")
        with pytest.raises(qual_runner.QualificationBudgetExhausted):
            client.stream("POST", "/chat/completions")

    assert calls == [("POST", "/chat/completions")]
    assert guard.actual_calls()[0] == {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "kind": "text",
        "allowed": True,
    }


def test_paid_call_guard_uses_web_search_reserve_for_tool_calls(monkeypatch):
    """A /chat/completions call carrying the real OpenRouter web tools
    reserves the web_search ceiling, not the cheaper text ceiling,
    for both relative post and relative stream."""
    import qual_runner

    # Zero out cumulative spend so the reserve does not block; we want to
    # see the allowed call log and confirm the higher reserve class.
    monkeypatch.setattr(qual_runner, "_read_total_cost", lambda: 0.0)
    monkeypatch.setattr(qual_runner, "_SESSION_BASELINE", 0.0)

    posts = []
    streams = []

    def fake_post(client, url, **kwargs):
        posts.append((url, kwargs.get("json", {})))
        return MagicMock()

    def fake_stream(client, method, url, **kwargs):
        streams.append((method, url, kwargs.get("json", {})))
        return MagicMock()

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    monkeypatch.setattr(httpx.Client, "stream", fake_stream)

    # Same tool shape that BaseAgent._build_web_search_tools() returns.
    web_search_tools = [
        {"type": "openrouter:web_search", "parameters": {"max_results": 5}},
        {"type": "openrouter:web_fetch"},
    ]

    client = httpx.Client(base_url="https://openrouter.ai/api/v1/")
    with qual_runner.PaidCallGuard("A", 3) as guard:
        # Plain chat completion (no tools) is classified as text.
        client.post(
            "/chat/completions",
            json={"model": "test", "messages": []},
        )
        # Post and stream with real OpenRouter web tools are classified as
        # web_search and therefore reserve the higher web_search ceiling.
        client.post(
            "/chat/completions",
            json={"model": "test", "messages": [], "tools": web_search_tools},
        )
        client.stream(
            "POST",
            "/chat/completions",
            json={"model": "test", "messages": [], "tools": web_search_tools},
        )

    assert qual_runner._per_call_ceiling("text") < qual_runner._per_call_ceiling("web_search")
    assert len(posts) == 2
    assert len(streams) == 1
    assert guard.actual_calls() == [
        {"url": "https://openrouter.ai/api/v1/chat/completions", "kind": "text", "allowed": True},
        {"url": "https://openrouter.ai/api/v1/chat/completions", "kind": "web_search", "allowed": True},
        {"url": "https://openrouter.ai/api/v1/chat/completions", "kind": "web_search", "allowed": True},
    ]


def test_qual_runner_run_case_accepts_product_ids(monkeypatch, tmp_path):
    """run_case accepts product_ids list and joins them like the production multi-product path."""
    import qual_runner
    from src.orchestrator import Orchestrator

    captured = {}

    def fake_run_product_spec(orch_self, raw_data, *args, **kwargs):
        captured["raw_data"] = raw_data
        return "ok"

    def fake_make_llm(orch):
        return MagicMock()

    monkeypatch.setattr(Orchestrator, "run_product_spec", fake_run_product_spec)
    monkeypatch.setattr(qual_runner, "make_llm", fake_make_llm)
    monkeypatch.setattr(qual_runner, "_init_session_baseline", lambda: None)

    evidence = qual_runner.run_case(
        case_id="A1_multi_product_spec",
        agent_key="product_spec",
        product_id="Lagenio K2",
        product_ids=["Lagenio K2", "Lagenio K3"],
        quick_brief="เปรียบเทียบสเปคทั้งสองรุ่น",
        output_dir=tmp_path,
    )

    assert evidence["product_ids"] == ["Lagenio K2", "Lagenio K3"]
    assert evidence["effective_product_id"] == "Lagenio K2 + Lagenio K3"
    assert "K2" in captured["raw_data"]
    assert "K3" in captured["raw_data"]
    assert captured["raw_data"].count("รหัสสินค้า:") == 2


def test_qual_runner_run_case_single_product_no_product_ids(monkeypatch, tmp_path):
    """product_ids=None keeps the single-product path and does not invent a multi-product ID."""
    import qual_runner
    from src.orchestrator import Orchestrator

    captured = {}

    def fake_run_product_spec(orch_self, raw_data, *args, **kwargs):
        captured["raw_data"] = raw_data
        return "ok"

    def fake_make_llm(orch):
        return MagicMock()

    monkeypatch.setattr(Orchestrator, "run_product_spec", fake_run_product_spec)
    monkeypatch.setattr(qual_runner, "make_llm", fake_make_llm)
    monkeypatch.setattr(qual_runner, "_init_session_baseline", lambda: None)

    evidence = qual_runner.run_case(
        case_id="A1_single_product_spec",
        agent_key="product_spec",
        product_id="Lagenio K2",
        quick_brief="",
        output_dir=tmp_path,
    )

    assert evidence["product_ids"] is None
    assert evidence["effective_product_id"] == "Lagenio K2"
    assert " + " not in evidence["effective_product_id"]
    assert "K2" in captured["raw_data"]
    assert captured["raw_data"].count("รหัสสินค้า:") == 1


def test_qual_runner_run_case_multi_product_no_cross_contamination(monkeypatch, tmp_path):
    """multi-product raw_data keeps distinct scoped context blocks, not a merged single ID."""
    import qual_runner
    from src.orchestrator import Orchestrator

    captured = {}

    def fake_run_product_spec(orch_self, raw_data, *args, **kwargs):
        captured["raw_data"] = raw_data
        return "ok"

    def fake_make_llm(orch):
        return MagicMock()

    monkeypatch.setattr(Orchestrator, "run_product_spec", fake_run_product_spec)
    monkeypatch.setattr(qual_runner, "make_llm", fake_make_llm)
    monkeypatch.setattr(qual_runner, "_init_session_baseline", lambda: None)

    qual_runner.run_case(
        case_id="A1_multi_product_spec",
        agent_key="product_spec",
        product_id="Lagenio K2",
        product_ids=["Lagenio K2", "Lagenio K3"],
        output_dir=tmp_path,
    )

    raw = captured["raw_data"]
    assert raw.count("รหัสสินค้า:") == 2
    assert "K2" in raw
    assert "K3" in raw
    assert "Lagenio K2 + Lagenio K3" not in raw  # must not appear as one bogus product id


def _setup_media_type_test(monkeypatch, tmp_path, captured):
    """Shared helper for media_type mapping tests."""
    import qual_runner
    from src.orchestrator import Orchestrator

    def fake_run_content_creator(orch_self, *args, **kwargs):
        captured["media_type"] = kwargs.get("media_type")
        return json.dumps({
            "posts": [{
                "platform": "TikTok",
                "concept": "c",
                "title": "t",
                "caption": "c",
                "hashtags": "#h",
                "asset_ids": [],
                "image_prompts": [],
                "video_prompts": [],
            }]
        })

    def fake_make_llm(orch):
        return MagicMock()

    monkeypatch.setattr(Orchestrator, "run_content_creator", fake_run_content_creator)
    monkeypatch.setattr(qual_runner, "make_llm", fake_make_llm)
    monkeypatch.setattr(qual_runner, "_init_session_baseline", lambda: None)


def test_qual_runner_content_creator_auto_image_maps_to_image(monkeypatch, tmp_path):
    """auto_image=True / auto_video=False forces media_type='image'."""
    import qual_runner
    captured = {}
    _setup_media_type_test(monkeypatch, tmp_path, captured)
    qual_runner.run_case(
        case_id="A4_map_image",
        agent_key="content_creator",
        product_id="Lagenio K2",
        platforms=["tiktok"],
        auto_image=True,
        auto_video=False,
        output_dir=tmp_path,
    )
    assert captured["media_type"] == "image"


def test_qual_runner_content_creator_auto_video_maps_to_video(monkeypatch, tmp_path):
    """auto_image=False / auto_video=True forces media_type='video'."""
    import qual_runner
    captured = {}
    _setup_media_type_test(monkeypatch, tmp_path, captured)
    qual_runner.run_case(
        case_id="A4_map_video",
        agent_key="content_creator",
        product_id="Lagenio K2",
        platforms=["tiktok"],
        auto_image=False,
        auto_video=True,
        output_dir=tmp_path,
    )
    assert captured["media_type"] == "video"


def test_qual_runner_content_creator_auto_both_maps_to_both(monkeypatch, tmp_path):
    """auto_image=True / auto_video=True forces media_type='both'."""
    import qual_runner
    captured = {}
    _setup_media_type_test(monkeypatch, tmp_path, captured)
    qual_runner.run_case(
        case_id="A4_map_both",
        agent_key="content_creator",
        product_id="Lagenio K2",
        platforms=["tiktok"],
        auto_image=True,
        auto_video=True,
        output_dir=tmp_path,
    )
    assert captured["media_type"] == "both"


def test_qual_runner_content_creator_auto_none_maps_to_empty(monkeypatch, tmp_path):
    """auto_image=False / auto_video=False leaves media_type empty."""
    import qual_runner
    captured = {}
    _setup_media_type_test(monkeypatch, tmp_path, captured)
    qual_runner.run_case(
        case_id="A4_map_none",
        agent_key="content_creator",
        product_id="Lagenio K2",
        platforms=["tiktok"],
        auto_image=False,
        auto_video=False,
        output_dir=tmp_path,
    )
    assert captured["media_type"] == ""


def test_qual_runner_content_creator_explicit_media_type_not_overridden(monkeypatch, tmp_path):
    """Explicit media_type takes precedence over auto_image/auto_video."""
    import qual_runner
    captured = {}
    _setup_media_type_test(monkeypatch, tmp_path, captured)
    qual_runner.run_case(
        case_id="A4_explicit_video",
        agent_key="content_creator",
        product_id="Lagenio K2",
        platforms=["tiktok"],
        media_type="video",
        auto_image=True,
        auto_video=False,
        output_dir=tmp_path,
    )
    assert captured["media_type"] == "video"


def test_qual_runner_content_creator_image_9_16_flow(monkeypatch, tmp_path):
    """Runner passes aspect_ratio through to image generation when image_prompts contain 9:16."""
    import qual_runner
    from src.orchestrator import Orchestrator

    captured = {}

    def fake_run_content_creator(orch_self, *args, **kwargs):
        captured["media_type"] = kwargs.get("media_type")
        return json.dumps({
            "posts": [{
                "platform": "TikTok",
                "concept": "c",
                "title": "t",
                "caption": "c",
                "hashtags": "#h",
                "asset_ids": [],
                "image_prompts": [{
                    "prompt": "vertical image",
                    "aspect_ratio": "9:16",
                    "resolution": "768x1366",
                }],
                "video_prompts": [],
            }]
        })

    def fake_make_llm(orch):
        return MagicMock()

    def fake_generate_image(prompt, img_path, llm=None, **kwargs):
        captured["generate_kwargs"] = kwargs
        return {"ok": True, "prompt": prompt}

    from src import media_gen

    monkeypatch.setattr(Orchestrator, "run_content_creator", fake_run_content_creator)
    monkeypatch.setattr(qual_runner, "make_llm", fake_make_llm)
    monkeypatch.setattr(qual_runner, "_init_session_baseline", lambda: None)
    monkeypatch.setattr(media_gen, "generate_image_with_retry", fake_generate_image)
    monkeypatch.setattr(media_gen, "save_retry_history", lambda *a, **k: None)

    qual_runner.run_case(
        case_id="A4_image_9_16",
        agent_key="content_creator",
        product_id="Lagenio K2",
        platforms=["tiktok"],
        auto_image=True,
        auto_video=False,
        output_dir=tmp_path,
    )

    assert captured["media_type"] == "image"
    assert captured.get("generate_kwargs", {}).get("aspect_ratio") == "9:16"


def test_a4_tiktok_artifact_replay_has_image_9_16():
    """A4 TikTok rerun artifact now carries a 9:16 image prompt; parser extracts it correctly."""
    from src import media_gen
    from pathlib import Path

    artifact = Path("data/all_agents_beta_qualification/run_outputs/A4_tiktok_text_image_output.txt")
    assert artifact.exists()
    content = artifact.read_text(encoding="utf-8")
    parsed = media_gen.parse_media_prompts(content)

    assert len(parsed["images"]) == 1
    assert parsed["images"][0].get("aspect_ratio") == "9:16"
    assert parsed["videos"] == []


def test_dry_run_cost_plan_content_creator_facebook_single_has_spare_calls():
    """Single Facebook post leaves spare calls for a possible repair before hitting the hard cap."""
    import qual_runner

    plan = qual_runner.dry_run_cost_plan(
        agent_key="content_creator",
        product_id="Lagenio K2",
        quick_brief="Single Facebook post with image",
        content_count=1,
        platforms=["facebook"],
        auto_image=True,
        auto_video=False,
    )
    assert plan["expected_estimate"] == 0.18
    assert plan["spare_calls"] == 3
    assert plan["status"] == "under ceiling"


def test_dry_run_cost_plan_content_creator_facebook_multi_post():
    """content_count=2 on a single Facebook platform counts per-post text review and 2 images."""
    import qual_runner

    plan = qual_runner.dry_run_cost_plan(
        agent_key="content_creator",
        product_id="Lagenio K2",
        quick_brief="Create 2 distinct Facebook posts with images",
        content_count=2,
        platforms=["facebook"],
        auto_image=True,
        auto_video=False,
    )
    assert plan["max_calls"] == qual_runner._load_qualification_config()["default_max_calls"]["content_creator"]
    assert plan["text_calls"] == 4
    assert plan["media_calls"] == 2
    assert plan["expected_estimate"] == 0.36
    assert len(plan["expected_calls"]) == 6
    assert plan["spare_calls"] == 0
    assert plan["status"] == "under ceiling"
    assert "no spare calls for conditional repair" in plan["feasibility_note"]
    image_labels = [c["label"] for c in plan["expected_calls"] if c["kind"] == "image"]
    assert image_labels == ["facebook:image_1", "facebook:image_2"]


def test_paid_call_guard_content_creator_hard_cap_6():
    """PaidCallGuard uses the configured default_max_calls for content_creator (6)."""
    import qual_runner

    cfg = qual_runner._load_qualification_config()
    configured = cfg["default_max_calls"]["content_creator"]
    guard = qual_runner.PaidCallGuard("A", configured)
    assert guard.max_calls == 6
    # Cap is reported by the dry-run planner as the hard ceiling.
    assert qual_runner.dry_run_cost_plan(
        agent_key="content_creator",
        content_count=2,
        platforms=["facebook"],
        auto_image=True,
    )["max_calls"] == 6


class _FakeLLM:
    """Deterministic LLM double for evidence-mode competitor analysis."""

    def __init__(self, generate_output: str, annotations=None):
        self.generate_output = generate_output
        self.annotations = annotations or []
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("return_annotations"):
            return self.generate_output, self.annotations
        return self.generate_output

    def close(self):
        pass


_GOOD_RESEARCH_RESPONSE = json.dumps(
    {
        "target_model": "K77",
        "competitor_names": ["Xiaomi Watch S3", "Kieslect"],
        "evidence": [
            {
                "competitor": "Xiaomi Watch S3",
                "field": "display",
                "claim": '1.43" AMOLED (466×466 px)',
                "url": "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                "geography": "global",
            },
            {
                "competitor": "Kieslect",
                "field": "battery",
                "claim": "510mAh",
                "url": "https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition",
                "geography": "thailand",
            },
        ],
        "evidence_based_recommendations": [
            {
                "text": "เน้นหน้าจอใหญ่ของ K77 เปรียบเทียบกับ Xiaomi Watch S3",
                "supporting_evidence_urls": [
                    "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
                ],
            }
        ],
        "strategic_hypotheses": [
            {
                "text": "ชูแบตอึดเป็นจุดขาย",
                "rationale": "K77 มีแบตใหญ่กว่า Kieslect ตามสเปก",
            }
        ],
        "uncertainty": ["ยังไม่พบราคา Kieslect"],
    },
    ensure_ascii=False,
)


_FAKE_ANNOTATIONS = [
    {
        "url": "https://www.siamphone.com/smartwatch/xiaomi/watch-s3",
        "title": "Xiaomi Watch S3",
        "content": "Xiaomi Watch S3 1.43 AMOLED 466x466",
        "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand"},
    },
    {
        "url": "https://www.kieslectthailand.com/en/product/72123/kieslect-ai-smartwatch-elite2-noir-edition",
        "title": "Kieslect AI Smartwatch Elite2 Noir Edition",
        "content": "Kieslect Elite2 510 mAh battery",
        "_relevance": {"relevant": True, "relevance_type": "competitor", "geography": "thailand"},
    },
]


def test_qual_runner_competitor_analysis_returns_bullet_brief(monkeypatch, tmp_path):
    """qual_runner.run_case passes quick_brief through and returns final Markdown brief."""
    import qual_runner

    fake = _FakeLLM(_GOOD_RESEARCH_RESPONSE, _FAKE_ANNOTATIONS)
    monkeypatch.setattr(qual_runner, "make_llm", lambda orch: fake)

    result = qual_runner.run_case(
        case_id="A2_brief_harness_test",
        agent_key="competitor_analysis",
        product_id="K77",
        quick_brief="สรุปแบบ bullet executive brief ห้ามใช้ตาราง",
        output_dir=tmp_path,
    )

    assert result["error"] is None
    assert result["num_paid_requests"] == 0
    assert result["result_text"]
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" not in result["result_text"]
    assert "| คุณสมบัติ |" not in result["result_text"]
    assert "https://www.siamphone.com/smartwatch/xiaomi/watch-s3" in result["result_text"]
    assert "## สมมติฐานเชิงกลยุทธ์ (ยังไม่ยืนยัน)" in result["result_text"]
    assert "## ข้อจำกัด" in result["result_text"]
    assert (tmp_path / "A2_brief_harness_test_output.txt").exists()


def test_qual_runner_competitor_analysis_default_still_table(monkeypatch, tmp_path):
    """qual_runner.run_case without non-table brief still returns table."""
    import qual_runner

    fake = _FakeLLM(_GOOD_RESEARCH_RESPONSE, _FAKE_ANNOTATIONS)
    monkeypatch.setattr(qual_runner, "make_llm", lambda orch: fake)

    result = qual_runner.run_case(
        case_id="A2_default_harness_test",
        agent_key="competitor_analysis",
        product_id="K77",
        quick_brief="",
        output_dir=tmp_path,
    )

    assert result["error"] is None
    assert result["num_paid_requests"] == 0
    assert "## ตารางเปรียบเทียบคุณสมบัติและสเปก" in result["result_text"]
    assert "| คุณสมบัติ |" in result["result_text"]
