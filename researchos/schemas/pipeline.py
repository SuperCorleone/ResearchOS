"""Pipeline state machine schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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
    # Alignment workflow (2026-04): runs before HYPOTHESIS for user_inject
    # pipelines that provide a base_paper. Legacy pipelines transition
    # straight to HYPOTHESIS — the scheduler picks the entry step based on
    # whether typed evidence_refs include role='base_paper'.
    PAPER_SPEC = "paper_spec"           # extract paper_spec.yaml from base_paper.md
    PROPOSAL_SPEC = "proposal_spec"     # extract proposal_spec.yaml from proposal.md
    ALIGNMENT = "alignment"             # synthesize alignment_contract.yaml
    ALIGNMENT_VALIDATOR = "alignment_validator"  # pure-Python preflight gate

    HYPOTHESIS = "hypothesis"
    DESIGN = "design"
    # DESIGN_REVIEW retired (2026-04): exp_designer now runs its own
    # scientific self-review; structural checks remain in design_checks.py.
    DESIGN_REVIEW = "design_review"
    CODE_RECON = "code_recon"  # shallow clone + static scan of reference codebases
    BASELINE_ANALYZE = "baseline_analyze"  # LLM: code-first analysis of cloned baseline package
    BASELINE_DIAGNOSE = "baseline_diagnose"  # Python: post-run divergence triage for baseline reproduction
    DATA_RECON = "data_recon"  # thin executor: copy dataset_paths / download external URLs from baseline_analysis.yaml
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
    FIGURE_COMPOSE = "figure_compose"  # automated figure generation (C15)
    REPORT = "report"
    WAITING_APPROVAL = "waiting_approval"  # pipeline paused; awaiting professor decision
    AWAITING_HUMAN_PIVOT = "awaiting_human_pivot"  # hard/kill-flag divergence; human must write pivot_request.md
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
    evidence_refs: list["TypedEvidenceRef | str"] = Field(
        default_factory=list,
        description="Typed references backing this pipeline. Bare strings from "
        "older state.json files are auto-normalised to role='unknown'."
    )

    # Parallel branching support
    active_branch_count: int = 0
    selected_branch: int | None = None

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _normalize_evidence_refs(cls, v):
        """Backward compat: bare-string entries → TypedEvidenceRef(role='unknown')."""
        if not isinstance(v, list):
            return v
        out = []
        for item in v:
            if isinstance(item, str):
                out.append({"role": "unknown", "path_or_url": item})
            else:
                out.append(item)
        return out


class TypedEvidenceRef(BaseModel):
    """A typed reference to a piece of evidence backing a pipeline.

    Roles:
      base_paper   — the paper this pipeline builds on or improves
      proposal     — the user's research proposal text
      ref_code     — git URL for the reference paper's open-source code
      related_work — supplementary literature (read by HYPOTHESIS only when in scope)
      unknown      — legacy entries from before typed refs (migration sentinel)
    """

    role: Literal["base_paper", "proposal", "ref_code", "related_work", "unknown"]
    path_or_url: str
    sha256: str | None = None


class PipelineDefinition(BaseModel):
    """New pipeline proposal, written to ``/runtime/pipelines/new_*.json``."""

    pipeline_id: str
    title: str
    research_question: str
    hypothesis: str | None = None
    estimated_effort: str = "medium"
    budget_request: int = 10000
    source: str = "professor"
    evidence_refs: list[TypedEvidenceRef | str] = Field(default_factory=list)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _normalize_evidence_refs(cls, v):
        """Backward compat: a bare string becomes role='unknown'."""
        if not isinstance(v, list):
            return v
        out = []
        for item in v:
            if isinstance(item, str):
                out.append({"role": "unknown", "path_or_url": item})
            else:
                out.append(item)
        return out
