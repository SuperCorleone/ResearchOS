"""Pipeline state machine schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PipelineStage(str, Enum):
    DIRECTION = "direction"
    PROPOSAL = "proposal"
    FAST_FAIL = "fast_fail"
    MAIN = "main"
    MILESTONE = "milestone"
    PAPER = "paper"
    KNOWLEDGE = "knowledge"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class PipelineStep(str, Enum):
    HYPOTHESIS = "hypothesis"
    DESIGN = "design"
    DESIGN_REVIEW = "design_review"
    CODE_RECON = "code_recon"  # A20 — shallow clone + static scan of reference codebases
    CONCEPT_EXTRACT = "concept_extract"  # Stream C — atomic concept decomposition
    CODE = "code"
    CODE_PREFLIGHT = "code_preflight"
    RUN = "run"
    RESULTS_REVERIFY = "results_reverify"  # Stream D — reproducibility rerun
    CRITIC = "critic"
    INTERROGATION = "interrogation"
    REPORT = "report"
    WAITING_APPROVAL = "waiting_approval"
    BRANCH_SELECT = "branch_select"
    BASELINE_CONFIRM = "baseline_confirm"  # Stream F / A17 — baseline lock-in
    PRE_PUBLISH_GATE = "pre_publish_gate"  # Stream E / A11 — HITL before paper publish


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipelineState(BaseModel):
    """State machine for a single research pipeline.

    Stored at ``/artifacts/pipeline_<id>/state.json``.
    """

    pipeline_id: str
    title: str
    research_question: str = ""
    stage: PipelineStage = PipelineStage.DIRECTION
    step: PipelineStep | None = None
    retry_count: int = 0
    max_retries: int = 3
    last_error: str | None = None

    artifacts: dict[str, str] = Field(default_factory=dict)
    """Maps artifact key -> relative filename within the pipeline directory."""

    priority: int = 0
    budget_allocated: int = 0
    budget_used: int = 0

    milestone_count: int = 0

    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    """Reference papers/evidence that motivated this pipeline."""

    # Parallel branching support
    active_branch_count: int = 0
    selected_branch: int | None = None


class PipelineDefinition(BaseModel):
    """New pipeline proposal, written to ``/runtime/pipelines/new_*.json``."""

    pipeline_id: str
    title: str
    research_question: str
    hypothesis: str | None = None
    estimated_effort: str = "medium"
    budget_request: int = 10000
    source: str = "professor"
    evidence_refs: list[str] = Field(default_factory=list)
