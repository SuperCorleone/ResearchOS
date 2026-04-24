"""Professor decision schemas.

HITL 3-gate mechanism was removed in the 2026-04 refactor. The surviving
decision surface is:

  - ``DecisionType``  — what the professor (or a manual override) can
    ask the scheduler to do. KILL / TWEAK / APPROVE / PIVOT / PAUSE / HOLD.
  - ``ProfessorDecision``  — YAML file written to ``/decisions/``.

The old ``CanonicalAction`` vocabulary (continue/launch/branch/write/
finalize/stop/tweak) and ``GateDecision`` / ``HITLGate`` types are gone:
they existed only to route gate-specific HITL actions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class DecisionType(str, Enum):
    """Professor decision verbs."""

    APPROVE = "approve"    # proceed (auto or manual)
    KILL = "kill"          # terminate pipeline (emergency brake)
    PIVOT = "pivot"        # change direction, restart MAIN with new angle
    PAUSE = "pause"        # suspend until further input
    HOLD = "hold"          # wait for re-evaluation
    TWEAK = "tweak"        # inject directive, replan from DESIGN


class ProfessorDecision(BaseModel):
    """Decision file written to ``/decisions/approval_<pipeline_id>_<ts>.yaml``."""

    pipeline_id: str = Field(
        description="ID of the pipeline this decision applies to."
    )
    decision: DecisionType = Field(
        description=(
            "approve (proceed), kill (terminate), pivot (change direction), "
            "pause (suspend), hold (wait), tweak (inject directive + replan)."
        )
    )
    next_milestone: str | None = Field(
        default=None,
        description=(
            "Label for the next milestone to reach. "
            "None means no explicit milestone target."
        ),
    )
    comment: str = Field(
        default="",
        description="Free-form rationale for the decision. Shown to pipeline agents.",
    )
    directive: str | None = Field(
        default=None,
        description=(
            "Free-form directive text consumed when decision == TWEAK. "
            "Written into user_directives.yaml so the coder can pick it up."
        ),
    )
    budget_adjustment: int | None = Field(
        default=None,
        description=(
            "Signed token delta to add to or subtract from the pipeline budget. "
            "None means no change."
        ),
    )
    allocated_budget: int | None = Field(
        default=None,
        description=(
            "Absolute token budget to set. "
            "Overrides budget_adjustment when both are present."
        ),
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
