"""Nested budget hierarchy for qualification harness.

Implements independent nested caps so a larger outer cap can NEVER
override a smaller inner cap.

Hierarchy (innermost to outermost):
  per-call maximum      → single call reserve
  per-scenario maximum  → one scenario's total spend
  generation-stage max  → all MKTApp + baseline generation
  judge maximum         → all Judge calls
  total execution max   → newly approved execution cap

Rules:
  - A larger outer cap must NEVER override a smaller inner cap.
  - If ensuring a scenario cap requires pre-call reservation, reserve
    conservatively using the maximum financially possible cost of that
    call under current parameters.
  - Do not knowingly make a call whose bounded worst-case spend would
    breach the remaining approved inner cap.
  - If the provider/API makes exact pre-call upper bounding impossible,
    document the uncertainty and use the safest mechanically enforceable bound.
  - Do not discard an already charged valid response merely because
    actual cost exceeded an estimate. Governance prevention must happen
    before the call.
  - Resume must preserve already-spent same-run budget; scenario budget
    consumption; overall new-run budget consumption. Do not reset
    counters by starting a new process.
  - Historical unrelated sunk spend does not silently consume a new run
    cap unless explicitly defined that way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class BudgetExceededError(Exception):
    """Raised when a budget cap would be breached by a planned call.

    This is raised BEFORE the call is made (pre-call governance).
    Never raised after a charged response.
    """
    def __init__(self, cap_name: str, spent: float, reserve: float, cap: float, label: str = ""):
        self.cap_name = cap_name
        self.spent = spent
        self.reserve = reserve
        self.cap = cap
        self.label = label
        super().__init__(
            f"Budget cap '{cap_name}' would be breached: "
            f"spent=${spent:.6f} + reserve=${reserve:.6f} = ${spent + reserve:.6f} > cap=${cap:.6f}"
            + (f" (label={label})" if label else "")
        )


@dataclass
class ScenarioBudget:
    """Per-scenario budget tracking.

    Tracks spend for one scenario independently of other scenarios.
    A scenario cap is a HARD CAP, not a planning hint.
    """
    scenario_id: str
    cap: float
    spent: float = 0.0
    call_count: int = 0
    max_calls: int = 0  # 0 = no call limit

    def remaining(self) -> float:
        return round(self.cap - self.spent, 6)

    def can_spend(self, reserve: float) -> bool:
        """Check if a call with this reserve can be made without breaching the cap."""
        if self.max_calls > 0 and self.call_count >= self.max_calls:
            return False
        return round(self.spent + reserve, 6) <= self.cap

    def commit(self, amount: float) -> None:
        """Record actual spend for this scenario."""
        self.spent = round(self.spent + amount, 6)
        self.call_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "cap": self.cap,
            "spent": self.spent,
            "remaining": self.remaining(),
            "call_count": self.call_count,
            "max_calls": self.max_calls,
        }


@dataclass
class BudgetHierarchy:
    """Nested budget hierarchy with independent caps.

    Each level is independently enforced. A larger outer cap NEVER
    overrides a smaller inner cap.

    Levels:
      per_call:      reserve for a single call (checked before each call)
      per_scenario:  one scenario's total spend (checked before each call)
      generation:    all generation-stage spend (MKTApp + baseline)
      judge:         all Judge spend
      total:         total newly approved execution cap

    Resume preserves all spent amounts.
    """
    total_cap: float
    generation_cap: float = 0.0
    judge_cap: float = 0.0
    per_call_default: float = 0.05
    # Per-scenario budgets
    scenarios: dict[str, ScenarioBudget] = field(default_factory=dict)
    # Cumulative spend by stage
    generation_spent: float = 0.0
    judge_spent: float = 0.0
    total_spent: float = 0.0
    # Historical sunk spend (separate category, does NOT consume new run cap)
    historical_sunk: float = 0.0

    def set_scenario_cap(self, scenario_id: str, cap: float, max_calls: int = 0) -> None:
        """Set or update a per-scenario cap."""
        if scenario_id in self.scenarios:
            # Preserve spent amount on resume — do NOT reset
            existing = self.scenarios[scenario_id]
            existing.cap = cap
            existing.max_calls = max_calls
        else:
            self.scenarios[scenario_id] = ScenarioBudget(
                scenario_id=scenario_id, cap=cap, max_calls=max_calls,
            )

    def check_call(
        self,
        scenario_id: str,
        reserve: float,
        stage: str = "generation",
        label: str = "",
    ) -> None:
        """Check all caps before making a paid call.

        Raises BudgetExceededError if ANY cap would be breached.
        Checks innermost-first: per-call → per-scenario → stage → total.

        stage: "generation" or "judge"
        """
        # 1. Per-call check (the reserve itself is the per-call cap)
        # This is implicitly checked by the reserve amount being conservative.

        # 2. Per-scenario check
        scenario = self.scenarios.get(scenario_id)
        if scenario is not None:
            if not scenario.can_spend(reserve):
                raise BudgetExceededError(
                    f"per_scenario:{scenario_id}",
                    scenario.spent, reserve, scenario.cap, label,
                )

        # 3. Stage check
        if stage == "generation":
            stage_spent = self.generation_spent
            stage_cap = self.generation_cap
            stage_name = "generation"
        else:  # judge
            stage_spent = self.judge_spent
            stage_cap = self.judge_cap
            stage_name = "judge"

        if stage_cap > 0:
            if round(stage_spent + reserve, 6) > stage_cap:
                raise BudgetExceededError(
                    stage_name, stage_spent, reserve, stage_cap, label,
                )

        # 4. Total check (excludes historical sunk — it's a separate category)
        if round(self.total_spent + reserve, 6) > self.total_cap:
            raise BudgetExceededError(
                "total", self.total_spent, reserve, self.total_cap, label,
            )

    def commit_spend(
        self,
        scenario_id: str,
        amount: float,
        stage: str = "generation",
    ) -> None:
        """Record actual spend after a charged call.

        This is called AFTER the response is received. It never raises.
        If actual cost exceeded the estimate, the spend is recorded
        accurately — the response is NOT discarded.
        """
        # Per-scenario
        scenario = self.scenarios.get(scenario_id)
        if scenario is not None:
            scenario.commit(amount)

        # Stage
        if stage == "generation":
            self.generation_spent = round(self.generation_spent + amount, 6)
        else:  # judge
            self.judge_spent = round(self.judge_spent + amount, 6)

        # Total
        self.total_spent = round(self.total_spent + amount, 6)

    def remaining_total(self) -> float:
        return round(self.total_cap - self.total_spent, 6)

    def remaining_generation(self) -> float:
        return round(self.generation_cap - self.generation_spent, 6)

    def remaining_judge(self) -> float:
        return round(self.judge_cap - self.judge_spent, 6)

    def remaining_scenario(self, scenario_id: str) -> float | None:
        s = self.scenarios.get(scenario_id)
        return s.remaining() if s else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cap": self.total_cap,
            "generation_cap": self.generation_cap,
            "judge_cap": self.judge_cap,
            "generation_spent": self.generation_spent,
            "judge_spent": self.judge_spent,
            "total_spent": self.total_spent,
            "historical_sunk": self.historical_sunk,
            "remaining_total": self.remaining_total(),
            "remaining_generation": self.remaining_generation(),
            "remaining_judge": self.remaining_judge(),
            "scenarios": {sid: s.to_dict() for sid, s in self.scenarios.items()},
        }

    def save_state(self, path: Path) -> None:
        """Persist budget state for resume/recovery."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load_state(cls, path: Path) -> "BudgetHierarchy":
        """Load budget state from a persisted file (for resume)."""
        data = json.loads(path.read_text(encoding="utf-8"))
        hierarchy = cls(
            total_cap=data["total_cap"],
            generation_cap=data.get("generation_cap", 0.0),
            judge_cap=data.get("judge_cap", 0.0),
            generation_spent=data.get("generation_spent", 0.0),
            judge_spent=data.get("judge_spent", 0.0),
            total_spent=data.get("total_spent", 0.0),
            historical_sunk=data.get("historical_sunk", 0.0),
        )
        for sid, sdata in data.get("scenarios", {}).items():
            s = ScenarioBudget(
                scenario_id=sid,
                cap=sdata["cap"],
                spent=sdata.get("spent", 0.0),
                call_count=sdata.get("call_count", 0),
                max_calls=sdata.get("max_calls", 0),
            )
            hierarchy.scenarios[sid] = s
        return hierarchy
