"""Budget / resource schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BudgetState(BaseModel):
    """Global budget state at ``/runtime/budget.json``."""

    total_credits: int = 100000
    allocations: dict[str, int] = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)
    reserve: int = 50000
