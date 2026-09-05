"""Offline tests for the nested budget hierarchy.

Tests the generic budget contract:
- Inner scenario cap beats outer total cap
- Same-run resume preserves scenario spend
- Pre-call reservation stops before potential hard-cap breach
- Charged response is never discarded solely to hide cap breach
- Historical sunk spend does not consume new run cap

No paid calls. No network. Pure logic tests.
"""

import json
import pytest
from pathlib import Path
from src.budget_hierarchy import BudgetExceededError, BudgetHierarchy, ScenarioBudget


class TestNestedCaps:
    """Inner scenario cap beats outer total cap."""

    def test_inner_scenario_cap_blocks_even_when_total_has_room(self):
        """S2 cannot consume S3's approved budget."""
        h = BudgetHierarchy(total_cap=10.0, generation_cap=10.0)
        h.set_scenario_cap("S2", cap=0.50)
        h.set_scenario_cap("S3", cap=0.50)
        # Spend 0.40 on S2
        h.check_call("S2", reserve=0.40, stage="generation")
        h.commit_spend("S2", 0.40, stage="generation")
        # Now try to spend 0.20 on S2 — should fail (0.40 + 0.20 = 0.60 > 0.50)
        # even though total has plenty of room (10.0 - 0.40 = 9.60)
        with pytest.raises(BudgetExceededError) as exc:
            h.check_call("S2", reserve=0.20, stage="generation")
        assert "per_scenario:S2" in str(exc.value)

    def test_total_cap_blocks_even_when_scenario_has_room(self):
        """Total ceiling cannot authorize exceeding a smaller scenario cap."""
        h = BudgetHierarchy(total_cap=1.0, generation_cap=10.0)
        h.set_scenario_cap("S1", cap=5.0)  # scenario cap is large
        # But total cap is only 1.0
        with pytest.raises(BudgetExceededError) as exc:
            h.check_call("S1", reserve=1.50, stage="generation")
        assert "total" in str(exc.value)

    def test_generation_stage_cap_blocks_independently(self):
        """Generation stage cap is independent of total cap."""
        h = BudgetHierarchy(total_cap=10.0, generation_cap=0.30)
        h.set_scenario_cap("S1", cap=5.0)
        with pytest.raises(BudgetExceededError) as exc:
            h.check_call("S1", reserve=0.40, stage="generation")
        assert "generation" in str(exc.value)

    def test_judge_cap_blocks_independently(self):
        """Judge cap is independent of generation cap."""
        h = BudgetHierarchy(total_cap=10.0, generation_cap=5.0, judge_cap=0.18)
        with pytest.raises(BudgetExceededError) as exc:
            h.check_call("S1", reserve=0.20, stage="judge")
        assert "judge" in str(exc.value)

    def test_per_scenario_call_count_cap(self):
        """Scenario max_calls is independently enforced."""
        h = BudgetHierarchy(total_cap=10.0, generation_cap=10.0)
        h.set_scenario_cap("S1", cap=5.0, max_calls=2)
        # First two calls OK
        h.check_call("S1", reserve=0.05, stage="generation")
        h.commit_spend("S1", 0.05, stage="generation")
        h.check_call("S1", reserve=0.05, stage="generation")
        h.commit_spend("S1", 0.05, stage="generation")
        # Third call should fail on call count
        with pytest.raises(BudgetExceededError) as exc:
            h.check_call("S1", reserve=0.05, stage="generation")
        assert "per_scenario:S1" in str(exc.value)


class TestResumePreservesSpend:
    """Same-run resume preserves scenario spend."""

    def test_resume_preserves_scenario_spend(self):
        """Resume does not reset scenario spend counters."""
        h = BudgetHierarchy(total_cap=10.0, generation_cap=10.0)
        h.set_scenario_cap("S1", cap=0.50)
        h.check_call("S1", reserve=0.30, stage="generation")
        h.commit_spend("S1", 0.30, stage="generation")
        # Save state
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        h.save_state(path)
        # Load state (simulating resume)
        h2 = BudgetHierarchy.load_state(path)
        # S1 should still have 0.30 spent
        assert h2.scenarios["S1"].spent == 0.30
        assert h2.scenarios["S1"].call_count == 1
        # Only 0.20 remaining
        assert h2.remaining_scenario("S1") == 0.20
        # Can spend 0.20 but not 0.21
        h2.check_call("S1", reserve=0.20, stage="generation")
        with pytest.raises(BudgetExceededError):
            h2.check_call("S1", reserve=0.21, stage="generation")
        path.unlink()

    def test_resume_preserves_total_spend(self):
        """Resume preserves total run spend."""
        h = BudgetHierarchy(total_cap=1.0, generation_cap=1.0)
        h.set_scenario_cap("S1", cap=0.50)
        h.check_call("S1", reserve=0.30, stage="generation")
        h.commit_spend("S1", 0.30, stage="generation")
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        h.save_state(path)
        h2 = BudgetHierarchy.load_state(path)
        assert h2.total_spent == 0.30
        assert h2.remaining_total() == 0.70
        path.unlink()

    def test_resume_preserves_generation_stage_spend(self):
        """Resume preserves generation stage spend."""
        h = BudgetHierarchy(total_cap=10.0, generation_cap=0.50)
        h.set_scenario_cap("S1", cap=5.0)
        h.check_call("S1", reserve=0.30, stage="generation")
        h.commit_spend("S1", 0.30, stage="generation")
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        h.save_state(path)
        h2 = BudgetHierarchy.load_state(path)
        assert h2.generation_spent == 0.30
        assert h2.remaining_generation() == 0.20
        path.unlink()


class TestPreCallReservation:
    """Pre-call reservation stops before potential hard-cap breach."""

    def test_pre_call_reservation_stops_before_breach(self):
        """If reserve + spent > cap, the call is blocked before it's made."""
        h = BudgetHierarchy(total_cap=1.0, generation_cap=1.0)
        h.set_scenario_cap("S1", cap=0.50)
        # Manually set spent (simulating a prior call that was committed)
        h.scenarios["S1"].spent = 0.45
        h.scenarios["S1"].call_count = 1
        h.generation_spent = 0.45
        h.total_spent = 0.45
        # Try to reserve 0.10 — 0.45 + 0.10 = 0.55 > 0.50
        with pytest.raises(BudgetExceededError):
            h.check_call("S1", reserve=0.10, stage="generation")
        # The call was NOT made, spend unchanged
        assert h.scenarios["S1"].spent == 0.45
        assert h.scenarios["S1"].call_count == 1  # still 1, no new commit


class TestChargedResponseNotDiscarded:
    """Charged response is never discarded solely to hide cap breach."""

    def test_commit_spend_never_raises(self):
        """Even if actual cost exceeds estimate, commit_spend records it."""
        h = BudgetHierarchy(total_cap=1.0, generation_cap=1.0)
        h.set_scenario_cap("S1", cap=0.50)
        # Pre-call check passes with reserve 0.10
        h.check_call("S1", reserve=0.10, stage="generation")
        # But actual cost was 0.15 (exceeded estimate)
        # commit_spend must NOT raise — the response is already charged
        h.commit_spend("S1", 0.15, stage="generation")
        assert h.scenarios["S1"].spent == 0.15
        # The next call check should account for the actual spend
        with pytest.raises(BudgetExceededError):
            h.check_call("S1", reserve=0.40, stage="generation")


class TestHistoricalSunkSpend:
    """Historical sunk spend does not silently consume a new run cap."""

    def test_historical_sunk_does_not_consume_new_cap(self):
        """Historical sunk is a separate category."""
        h = BudgetHierarchy(total_cap=1.0, generation_cap=1.0, historical_sunk=5.0)
        # Historical sunk is 5.0, but total_cap is 1.0 (new authorization)
        # The new run should still have 1.0 available
        assert h.remaining_total() == 1.0
        h.set_scenario_cap("S1", cap=0.50)
        h.check_call("S1", reserve=0.50, stage="generation")
        # Should succeed — historical sunk doesn't consume the new cap
        h.commit_spend("S1", 0.50, stage="generation")
        assert h.total_spent == 0.50
        assert h.remaining_total() == 0.50

    def test_historical_sunk_tracked_separately(self):
        h = BudgetHierarchy(total_cap=1.0, historical_sunk=2.5)
        assert h.historical_sunk == 2.5
        assert h.total_spent == 0.0
        assert h.remaining_total() == 1.0
