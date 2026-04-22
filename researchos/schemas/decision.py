"""Professor / HITL decision schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class DecisionType(str, Enum):
    """Legacy professor decision types. Preserved for back-compat."""

    APPROVE = "approve"
    KILL = "kill"
    PIVOT = "pivot"
    PAUSE = "pause"
    HOLD = "hold"
    TWEAK = "tweak"


class CanonicalAction(str, Enum):
    """A11 — canonical 3-point HITL action vocabulary.

    Represents the normalized set of verbs the Professor or a human
    reviewer can issue at any gate. Scheduler + dispatcher should map
    legacy ``DecisionType`` values onto these when routing.
    """

    CONTINUE = "continue"          # proceed to next step
    LAUNCH_EXPERIMENT = "launch"   # tree-search branch / new experiment
    BRANCH = "branch"              # explore alternative path
    WRITE = "write"                # move to paper stage
    FINALIZE = "finalize"          # publish
    STOP = "stop"                  # terminate pipeline
    TWEAK = "tweak"                # user directive, replan from design


class HITLGate(str, Enum):
    """Known HITL gate identifiers.

    Used by ``ProfessorDecision.gate`` to mark which checkpoint a decision
    answered. ``milestone_N`` is a template — callers substitute the
    numeric milestone index (e.g. ``milestone_3``).
    """

    PRE_MAIN = "pre_main"
    MILESTONE = "milestone"        # generic; prefer ``milestone_<N>`` string
    PRE_PUBLISH = "pre_publish"


class ProfessorDecision(BaseModel):
    """Decision file written to ``/decisions/approval_<pipeline_id>_<ts>.yaml``.

    ``gate`` marks which HITL checkpoint produced this decision. Accepted
    values: ``pre_main``, ``milestone_<N>`` (where ``<N>`` is an int), or
    ``pre_publish``. ``None`` preserves pre-A11 behaviour.

    ``canonical_action`` optionally carries the A11 canonical verb when
    callers want stricter routing semantics; legacy ``decision`` remains
    authoritative for back-compat.
    """

    pipeline_id: str = Field(
        description="ID of the pipeline this decision applies to."
    )
    decision: DecisionType = Field(
        description="Legacy decision verb. Valid values: approve (proceed), kill (terminate), "
        "pivot (change direction), pause (suspend), hold (wait for input), tweak (adjust parameters)."
    )
    next_milestone: str | None = Field(
        default=None,
        description="Label for the next milestone to reach before the next gate. "
        "None means no explicit milestone target."
    )
    comment: str = Field(
        default="",
        description="Free-form rationale for the decision. Shown to pipeline agents."
    )
    budget_adjustment: int | None = Field(
        default=None,
        description="Signed token delta to add to or subtract from the pipeline budget. "
        "None means no change."
    )
    allocated_budget: int | None = Field(
        default=None,
        description="Absolute token budget to set. Overrides budget_adjustment when both are present."
    )
    # A11 additions — all optional, default-None preserves back-compat.
    gate: str | None = Field(
        default=None,
        description="HITL gate that produced this decision: 'pre_main', 'milestone_<N>', or 'pre_publish'. "
        "None preserves pre-A11 behaviour."
    )
    canonical_action: CanonicalAction | None = Field(
        default=None,
        description="A11 canonical verb for stricter routing: continue, launch, branch, write, finalize, stop, tweak. "
        "None falls back to legacy decision field."
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class GateDecision(BaseModel):
    """Stricter HITL gate decision record (A11).

    Prefer this shape for new HITL gates (milestone_N, pre_publish). It
    requires an explicit ``gate`` and ``canonical_action`` so the
    scheduler can route unambiguously.
    """

    pipeline_id: str
    gate: str
    canonical_action: CanonicalAction
    comment: str = ""
    directive: str | None = None          # free-form user text for TWEAK
    next_milestone: str | None = None
    budget_adjustment: int | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
