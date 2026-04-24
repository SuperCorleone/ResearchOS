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
    # DESIGN_REVIEW retired (2026-04): exp_designer now runs its own
    # scientific self-review; structural checks remain in design_checks.py.
    CODE_RECON = "code_recon"  # shallow clone + static scan of reference codebases
    CONCEPT_EXTRACT = "concept_extract"  # atomic concept decomposition (legacy 1-pass)
    CONCEPT_PAPER = "concept_paper"  # 2-pass: paper side (math + refs)
    CONCEPT_CODE = "concept_code"  # 2-pass: code side (locators)
    CODE = "code"
    CODE_PREFLIGHT = "code_preflight"
    RUN = "run"
    RESULTS_REVERIFY = "results_reverify"  # reproducibility rerun
    CRITIC = "critic"
    INTERROGATION = "interrogation"
    EXPERIMENT_ANALYSIS = "experiment_analysis"
    JUDGE = "judge"  # merges critic + experiment_analysis + interrogation
    REPORT = "report"
    BASELINE_CONFIRM = "baseline_confirm"  # baseline lock-in


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipelineState(BaseModel):
    """State machine for a single research pipeline.

    Stored at ``/artifacts/pipeline_<id>/state.json``.
    """

    pipeline_id: str
    title: str
    research_question: str = Field(
        default="",
        description="Specific, falsifiable research question driving this pipeline."
    )
    stage: PipelineStage = PipelineStage.DIRECTION
    step: PipelineStep | None = None
    retry_count: int = 0
    max_retries: int = 3
    last_error: str | None = None

    artifacts: dict[str, str] = Field(
        default_factory=dict,
        description="Maps artifact key to relative filename within the pipeline directory."
    )

    priority: int = 0
    budget_allocated: int = 0
    budget_used: int = 0

    milestone_count: int = 0

    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="Reference paper filenames from knowledge/raw/ that motivated this pipeline."
    )

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
