"""Professor decision schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class DecisionType(str, Enum):
    APPROVE = "approve"
    KILL = "kill"
    PIVOT = "pivot"
    PAUSE = "pause"
    HOLD = "hold"
    TWEAK = "tweak"


class ProfessorDecision(BaseModel):
    """Decision file written to ``/decisions/approval_<pipeline_id>_<ts>.yaml``."""

    pipeline_id: str
    decision: DecisionType
    next_milestone: str | None = None
    comment: str = ""
    budget_adjustment: int | None = None
    allocated_budget: int | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
