"""Offline tests for harness readiness: Judge preflight, presentation, neutral task spec.

Tests the harness contracts required by Frontier Harness Readiness:
- Judge preflight rejects incomplete/unknown candidates
- S4 raw JSON preserved as audit artifact; Judge sees rendered markdown
- Renderer failure prevents clean judging
- Neutral task spec reaches both sides; private settings not leaked

No paid calls. No network. Pure logic tests.
"""

import json
import pytest
import tempfile
from pathlib import Path


class TestJudgeCompletenessPreflight:
    """Judge preflight rejects INCOMPLETE/UNKNOWN candidates."""

    def _create_run_dir(self, tmp_path, frontier_completeness="COMPLETE", mktapp_completeness="COMPLETE"):
        """Create a minimal run dir that passes all preflight except completeness."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import m6_judge_runner as judge

        run_dir = tmp_path / "judge_run"
        outputs = run_dir / "outputs"
        mktapp_dir = outputs / "mktapp"
        frontier_dir = outputs / "frontier"
        mktapp_dir.mkdir(parents=True, exist_ok=True)
        frontier_dir.mkdir(parents=True, exist_ok=True)

        for sid in ("S1", "S2", "S3", "S4"):
            (mktapp_dir / f"{sid}.txt").write_bytes(f"MKTApp {sid}".encode())
            (frontier_dir / f"{sid}.txt").write_bytes(f"Frontier {sid}".encode())
            (outputs / f"{sid}_X.txt").write_bytes(f"MKTApp {sid}".encode())
            (outputs / f"{sid}_Y.txt").write_bytes(f"Frontier {sid}".encode())

        (run_dir / "m6_mapping_secret.json").write_text(json.dumps({
            f"S{i}": {"X": "MKTApp", "Y": "Frontier"} for i in range(1, 5)
        }))

        scenarios = []
        for i in range(1, 5):
            scenarios.append({
                "id": f"S{i}",
                "frontier_charged_but_invalid": False,
                "frontier_model": "anthropic/claude-fable-5.1",
                "mktapp_model": "google/gemini-3.7-flash",
                "frontier_completeness": frontier_completeness,
                "mktapp_completeness": mktapp_completeness,
                "frontier_finish_reason": "stop" if frontier_completeness == "COMPLETE" else "length",
                "frontier_truncated": frontier_completeness != "COMPLETE",
                "mktapp_final_finish_reason": "stop" if mktapp_completeness == "COMPLETE" else "length",
                "mktapp_final_truncated": mktapp_completeness != "COMPLETE",
            })

        (run_dir / "m6_evidence.json").write_text(json.dumps({
            "stopped": False,
            "execution_mode": "paid",
            "valid_for_judging": True,
            "authorized": True,
            "scenarios": scenarios,
        }))

        import hashlib
        hashes = {"mktapp": {}, "frontier": {}}
        for sid in ("S1", "S2", "S3", "S4"):
            hashes["mktapp"][sid] = hashlib.sha256((mktapp_dir / f"{sid}.txt").read_bytes()).hexdigest()
            hashes["frontier"][sid] = hashlib.sha256((frontier_dir / f"{sid}.txt").read_bytes()).hexdigest()
        (run_dir / "output_hashes.json").write_text(json.dumps(hashes))

        (run_dir / "resume_link.json").write_text(json.dumps({
            "source_run": "20260904_070830",
            "source_execution_head": "testhead1234567890abcdef1234567890abcdef",
            "m6_remediation_baseline": "61b6d93bb0a4389eac1bb9ff936ce6a46004ce23",
            "continuation_harness_head": "testhead1234567890abcdef1234567890abcdef",
            "production_fingerprint": "test_pf",
            "scenario_fingerprint": "test_sf",
            "input_pack_fingerprint": "test_ipf",
            "historical_sunk_cost": 1.0,
            "continuation_incremental_cost": 0.5,
            "judge_reserve": 0.20,
            "execution_mode": "paid",
            "valid_for_judging": True,
            "authorized": True,
            "approved_cumulative_ceiling": 3.0,
        }))

        # Create source recovery manifest
        source_dir = run_dir.parent / "20260904_070830"
        source_dir.mkdir(exist_ok=True)
        (source_dir / "recovery_manifest.json").write_text(json.dumps({
            "source_run_id": "20260904_070830",
            "source_execution_head": "testhead1234567890abcdef1234567890abcdef",
            "m6_remediation_baseline": "61b6d93bb0a4389eac1bb9ff936ce6a46004ce23",
            "production_fingerprint": "test_pf",
            "scenario_fingerprint": "test_sf",
            "input_pack_fingerprint": "test_ipf",
        }))

        return run_dir

    def test_both_complete_preflight_passes(self, tmp_path):
        """Both sides COMPLETE → preflight may proceed."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import m6_judge_runner as judge
        run_dir = self._create_run_dir(tmp_path, "COMPLETE", "COMPLETE")
        ok, reason = judge.judge_preflight(run_dir)
        assert ok, f"Expected preflight to pass, got: {reason}"

    def test_frontier_incomplete_preflight_fails(self, tmp_path):
        """Frontier INCOMPLETE → preflight fails, no Judge call."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import m6_judge_runner as judge
        run_dir = self._create_run_dir(tmp_path, "INCOMPLETE", "COMPLETE")
        ok, reason = judge.judge_preflight(run_dir)
        assert not ok
        assert "INCOMPLETE" in reason
        assert "Frontier" in reason

    def test_mktapp_incomplete_preflight_fails(self, tmp_path):
        """MKTApp INCOMPLETE → preflight fails, no Judge call."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import m6_judge_runner as judge
        run_dir = self._create_run_dir(tmp_path, "COMPLETE", "INCOMPLETE")
        ok, reason = judge.judge_preflight(run_dir)
        assert not ok
        assert "INCOMPLETE" in reason
        assert "MKTApp" in reason

    def test_frontier_unknown_preflight_fails(self, tmp_path):
        """Frontier UNKNOWN → preflight fails, no Judge call."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import m6_judge_runner as judge
        run_dir = self._create_run_dir(tmp_path, "UNKNOWN", "COMPLETE")
        ok, reason = judge.judge_preflight(run_dir)
        assert not ok
        assert "UNKNOWN" in reason

    def test_mktapp_unknown_preflight_fails(self, tmp_path):
        """MKTApp UNKNOWN → preflight fails, no Judge call."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import m6_judge_runner as judge
        run_dir = self._create_run_dir(tmp_path, "COMPLETE", "UNKNOWN")
        ok, reason = judge.judge_preflight(run_dir)
        assert not ok
        assert "UNKNOWN" in reason


class TestS4PresentationParity:
    """S4 raw JSON preserved as audit; Judge sees rendered markdown."""

    def test_render_posts_to_markdown_produces_markdown(self):
        """The production renderer produces markdown, not JSON."""
        from src.content_schema import render_posts_to_markdown
        parsed = {
            "posts": [
                {
                    "platform": "TikTok",
                    "concept": "Test concept",
                    "title": "Test title",
                    "caption": "Test caption",
                    "hashtags": "#test",
                    "asset_ids": ["a_0001"],
                }
            ]
        }
        md = render_posts_to_markdown(parsed)
        assert md
        assert "TikTok" in md
        assert "Test caption" in md
        # Should NOT be JSON
        assert not md.strip().startswith("{")

    def test_render_posts_to_markdown_empty_posts(self):
        """Renderer returns empty string for no posts."""
        from src.content_schema import render_posts_to_markdown
        md = render_posts_to_markdown({"posts": []})
        assert md == ""

    def test_raw_json_preserved_separate_from_rendered(self):
        """The qual_runner should save raw JSON as a separate audit artifact."""
        # This is a structural test — verify the code path exists
        # by checking that qual_runner has the raw_json_file logic
        import inspect
        import scripts.qual_runner as qr
        source = inspect.getsource(qr.run_case)
        assert "_output_raw.json" in source
        assert "render_posts_to_markdown" in source


# Note: TestNeutralTaskSpec was moved to tests/test_m6_uplift_harness.py
# The neutral task spec concept now lives in the uplift harness, not m6_frontier_uat.
