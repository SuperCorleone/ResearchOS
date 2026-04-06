"""Per-call cost tracking schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class CostRecord(BaseModel):
    """A single LLM call cost record."""

    pipeline_id: str
    step_name: str
    agent_name: str
    model_name: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    credits_used: int
    duration_seconds: float
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class CostReport(BaseModel):
    """Cumulative cost report for a pipeline."""

    pipeline_id: str
    records: list[CostRecord] = Field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_credits: int = 0
    total_duration_seconds: float = 0.0

    def add_record(self, record: CostRecord) -> None:
        self.records.append(record)
        self.total_input_tokens += record.input_tokens
        self.total_output_tokens += record.output_tokens
        self.total_tokens += record.total_tokens
        self.total_credits += record.credits_used
        self.total_duration_seconds += record.duration_seconds
