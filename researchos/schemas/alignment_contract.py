"""Alignment contract — single source of truth for downstream agents.

Synthesized by ``alignment_synthesizer`` from ``paper_spec.yaml`` +
``proposal_spec.yaml`` + ``code_recon.yaml``. Validated by
``alignment_validator`` (pure Python). Consumed by HYPOTHESIS, DESIGN,
CONCEPT_PAPER, CONCEPT_CODE, CODE, CRITIC, JUDGE, and PUBLISHER.

Design contract:
  * Per-dimension Lock decisions are the only place where alignment policy
    lives. Downstream agents do not re-derive policy; they dereference.
  * ``value_ref`` always points to a leaf scalar in paper_spec or
    proposal_spec. ``value_ref_subtree`` points to a dict/list. Wrong
    expectation type is a validator error.
  * ``executive_summary`` is the auto-generated TL;DR the coder reads
    first; everything else is reference detail.
  * ``contract_status`` gates pipeline progression: only FINAL contracts
    let HYPOTHESIS proceed. DRAFT means open_tensions is non-empty;
    BLOCKED means a hard precondition failed (e.g., NOT_STATED in a
    LOCKED-required field, or ref_code unavailable for a replacement-type
    change).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Lock vocabulary — the 10 values we agreed on
# ---------------------------------------------------------------------------

Lock = Literal[
    "LOCKED_TO_PAPER",
    "LOCKED_TO_PAPER_VIA_REF_CODE",
    "LOCKED_TO_PAPER_VIA_REIMPL",
    "LOCKED_TO_PAPER_PLUS_PROPOSAL",
    "AUGMENTED_BY_PROPOSAL",
    "ARM_DEPENDENT",
    "NEGOTIATED",
    "DOMAIN_SUBSTITUTED",
    "FREE",
    "NOT_APPLICABLE",
]


#: The 9 dimensions every contract must cover. Order matters for diffability
#: and human review; the synthesizer should write them in this order.
REQUIRED_DIMENSIONS: tuple[str, ...] = (
    "dataset",
    "split_protocol",
    "preprocessing",
    "baseline_algorithm",
    "candidate_model_pool",
    "metrics",
    "rq_coverage",
    "evaluation_protocol",
    "code_implementation_strategy",
)


#: Locks that MUST resolve to a concrete paper_spec value at validate time.
#: If the dereferenced value is NOT_STATED or missing, the contract is BLOCKED.
LOCKS_REQUIRING_PAPER_VALUE: frozenset[str] = frozenset({
    "LOCKED_TO_PAPER",
    "LOCKED_TO_PAPER_VIA_REF_CODE",
    "LOCKED_TO_PAPER_VIA_REIMPL",
    "LOCKED_TO_PAPER_PLUS_PROPOSAL",
})


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


_VALUE_REF_RE = re.compile(
    # root.path.with.segments[id=val].more — leaf form:
    r"^(?:paper_spec|proposal_spec|code_recon)(?:\.[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\[[A-Za-z_][A-Za-z0-9_]*=[^\]]+\])?)+$"
)


def _validate_value_ref_shape(v: str | None) -> str | None:
    """Syntactic check only — actual dereferencing is the validator's job."""
    if v is None:
        return v
    if not _VALUE_REF_RE.match(v):
        raise ValueError(
            f"value_ref/value_ref_subtree must be a dotted path under "
            f"paper_spec/proposal_spec/code_recon (with optional [id=val] "
            f"array indices); got {v!r}"
        )
    return v


class CodeHandle(BaseModel):
    """Code-side handle for a LOCKED dimension that points at ref_code.

    Populated for ``baseline_algorithm`` (always) and ``dataset`` (when the
    loader strategy is import-based). Validator cross-checks that the
    ``import_path``/``symbol`` pair appears in code_recon.yaml.
    """

    model_config = ConfigDict(extra="forbid")

    # Filesystem / data side
    access_path: str | None = Field(
        default=None,
        description="Local path to a data file (set on dataset dim only).",
    )
    loader_strategy: Literal[
        "import_from_ref_code",
        "local_csv",
        "local_pickle",
        "fetch_from_url",
        "synthetic_with_seed",
        "NOT_APPLICABLE",
    ] | None = Field(default=None)

    # Ref-code import side
    ref_code_url: str | None = Field(default=None)
    install_command: str | None = Field(default=None)
    import_path: str | None = Field(
        default=None,
        description="Python import path, e.g. 'bma_package.bma_adaptation'.",
    )
    symbol: str | None = Field(
        default=None,
        description="Class or function name to be imported.",
    )


class ArmSpec(BaseModel):
    """One arm in an ARM_DEPENDENT dimension."""

    value_ref: str | None = Field(default=None)
    value_ref_subtree: str | None = Field(default=None)
    override: dict[str, Any] | None = Field(
        default=None,
        description="Inline override for the proposal_arm — used when the "
        "arm's value comes from proposal_spec.divergences and cannot be "
        "expressed as a single ref. Validator still requires that this "
        "override appears in proposal_spec somewhere.",
    )
    reason: str = Field(default="")
    ablation_required: bool = Field(default=False)
    code_handle: CodeHandle | None = Field(default=None)

    @field_validator("value_ref", "value_ref_subtree")
    @classmethod
    def _ref_shape(cls, v: str | None) -> str | None:
        return _validate_value_ref_shape(v)


class Dimension(BaseModel):
    """One alignment dimension — the per-knob policy decision.

    Most dimensions populate either ``value_ref``/``value_ref_subtree`` (the
    LOCKED family) or ``additions``/``proposal_added``/``arms`` (the augmented
    / arm-dependent family). The synthesizer decides which based on
    ``lock``; the validator cross-checks shape against lock.
    """

    model_config = ConfigDict(extra="allow")

    dim: str = Field(description="One of REQUIRED_DIMENSIONS.")
    lock: Lock

    # Generic refs — used by LOCKED_TO_PAPER and friends
    value_ref: str | None = Field(default=None)
    value_ref_subtree: str | None = Field(default=None)
    base_value_ref_subtree: str | None = Field(
        default=None,
        description="Used by AUGMENTED_BY_PROPOSAL to point at the paper's "
        "baseline value before augmentation.",
    )
    paper_metrics_ref_subtree: str | None = Field(
        default=None,
        description="Used by LOCKED_TO_PAPER_PLUS_PROPOSAL on the metrics "
        "dim — the paper-side metric set.",
    )

    # Augmentations / additions
    additions: list[str] = Field(default_factory=list)
    proposal_added: list[dict[str, Any]] = Field(default_factory=list)

    # Arm-dependent dimensions
    arms: dict[str, ArmSpec] = Field(default_factory=dict)
    open_tension: str | None = Field(default=None)

    # RQ coverage dim has its own shape
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[dict[str, Any]] = Field(default_factory=list)

    # Code implementation strategy dim has yet another shape
    baseline: dict[str, Any] | None = Field(default=None)
    proposal: dict[str, Any] | None = Field(default=None)
    glue: dict[str, Any] | None = Field(default=None)

    # Common
    code_handle: CodeHandle | None = Field(default=None)
    reason: str = Field(default="")
    blockers: list[str] = Field(default_factory=list)

    @field_validator("dim")
    @classmethod
    def _dim_in_required(cls, v: str) -> str:
        if v not in REQUIRED_DIMENSIONS:
            raise ValueError(
                f"dim must be one of {REQUIRED_DIMENSIONS}; got {v!r}"
            )
        return v

    @field_validator(
        "value_ref",
        "value_ref_subtree",
        "base_value_ref_subtree",
        "paper_metrics_ref_subtree",
    )
    @classmethod
    def _ref_shape(cls, v: str | None) -> str | None:
        return _validate_value_ref_shape(v)

    @model_validator(mode="after")
    def _arm_dependent_requires_ablation(self) -> "Dimension":
        if self.lock == "ARM_DEPENDENT":
            if not self.arms:
                raise ValueError(
                    f"dim={self.dim!r}: ARM_DEPENDENT requires non-empty arms."
                )
            if not any(arm.ablation_required for arm in self.arms.values()):
                raise ValueError(
                    f"dim={self.dim!r}: ARM_DEPENDENT requires at least one "
                    f"arm with ablation_required=True (the synthesizer must "
                    f"plan an isolation arm)."
                )
        return self

    @model_validator(mode="after")
    def _negotiated_requires_tension(self) -> "Dimension":
        if self.lock == "NEGOTIATED" and not self.open_tension:
            raise ValueError(
                f"dim={self.dim!r}: NEGOTIATED requires open_tension to be set."
            )
        return self

    @model_validator(mode="after")
    def _reason_required_for_locks(self) -> "Dimension":
        # rq_coverage uses out_of_scope reasons inline; the dim-level reason
        # is optional there. Everywhere else, lock decisions need a reason.
        if self.dim == "rq_coverage":
            return self
        if not self.reason.strip():
            raise ValueError(
                f"dim={self.dim!r}: reason is required (lock={self.lock})."
            )
        return self


class Arm(BaseModel):
    """One experimental arm referenced from executive_summary."""

    name: str = Field(description="Arm identifier; matches a function name in run_experiment.py.")
    composition: str = Field(description="Human description of the arm's setup.")
    source_dim: str | None = Field(
        default=None,
        description="Which contract dimension(s) defined this arm.",
    )
    required_by: str | None = Field(
        default=None,
        description="Set when this arm exists because of an "
        "ARM_DEPENDENT.ablation_required directive; cites the dim.",
    )


class ExecutiveSummary(BaseModel):
    """Auto-generated TL;DR for the coder. Synthesizer fills this last."""

    model_config = ConfigDict(extra="forbid")

    must_use_dataset: str
    must_use_split: str
    must_use_baseline: str
    arms_to_run: list[Arm]
    metrics_required: list[str]
    forbidden: list[str] = Field(
        default_factory=lambda: [
            "synthetic / proxy datasets",
            "baseline reimplementation when ref_code is available",
            "placeholder code: NotImplementedError, pass, ..., NOT_IMPLEMENTED string",
        ],
        description="Authoritative deny-list for the coder. Synthesizer "
        "may extend, but the placeholder/baseline-reimpl entries are "
        "always present.",
    )
    open_questions: int = Field(
        default=0,
        description="Mirrors len(open_tensions). >0 forces contract_status=DRAFT.",
    )


class Classification(BaseModel):
    """change_type classification with provenance."""

    change_type: Literal[
        "replacement_in_method",
        "extension_of_method",
        "cross_method_combination",
        "cross_domain_application",
        "new_task",
    ]
    change_type_source: Literal[
        "proposal_extractor_confirmed",
        "synthesizer_revised",
    ]
    superseded_extractor_value: str | None = Field(
        default=None,
        description="Set iff change_type_source='synthesizer_revised'; "
        "records what the extractor originally said.",
    )
    rationale: str
    evidence: dict[str, str] = Field(
        default_factory=dict,
        description="Optional {from_paper: quote, from_proposal: quote}.",
    )

    @model_validator(mode="after")
    def _superseded_consistency(self) -> "Classification":
        if self.change_type_source == "synthesizer_revised":
            if self.superseded_extractor_value is None:
                raise ValueError(
                    "synthesizer_revised requires superseded_extractor_value to be set."
                )
        else:
            if self.superseded_extractor_value is not None:
                raise ValueError(
                    "superseded_extractor_value must be null when "
                    "change_type_source != 'synthesizer_revised'."
                )
        return self


class Sources(BaseModel):
    """Hashes of input artifacts so downstream can detect drift."""

    paper_spec_hash: str
    proposal_spec_hash: str
    code_recon_hash: str | None = None


class Precondition(BaseModel):
    check: str = Field(description="Human-readable description of what is checked.")
    enforced_at: Literal[
        "ALIGNMENT_VALIDATOR",
        "CODE_RECON",
        "DESIGN_PREFLIGHT",
        "CODE_PREFLIGHT",
        "RUN",
    ]
    status: Literal["PENDING", "PASS", "FAIL"] = Field(default="PENDING")
    detail: str = Field(default="")


# ---------------------------------------------------------------------------
# Top-level contract
# ---------------------------------------------------------------------------


class AlignmentContract(BaseModel):
    """The alignment contract itself. Written to ``alignment_contract.yaml``."""

    model_config = ConfigDict(extra="forbid")

    contract_version: int = Field(default=1)
    contract_status: Literal["DRAFT", "FINAL", "BLOCKED"] = Field(
        description="Gate for HYPOTHESIS. Only FINAL allows progression."
    )
    block_reason: str | None = Field(
        default=None,
        description="Required when contract_status=BLOCKED. Empty otherwise.",
    )
    created_at: str

    sources: Sources

    executive_summary: ExecutiveSummary
    classification: Classification
    confidence: Literal["high", "medium", "low"] = Field(default="high")
    judge_must_address: list[str] = Field(
        default_factory=list,
        description="Risks the judge must explicitly address (e.g., "
        "'reimpl_drift_risk' when ref_code unavailable).",
    )

    dimensions: list[Dimension]
    preconditions: list[Precondition] = Field(default_factory=list)
    open_tensions: list[str] = Field(default_factory=list)

    @field_validator("dimensions")
    @classmethod
    def _all_dimensions_present(cls, dims: list[Dimension]) -> list[Dimension]:
        seen = [d.dim for d in dims]
        missing = [d for d in REQUIRED_DIMENSIONS if d not in seen]
        if missing:
            raise ValueError(
                f"alignment_contract.dimensions must cover all 9 dims; "
                f"missing: {missing}"
            )
        if len(set(seen)) != len(seen):
            raise ValueError(
                f"alignment_contract.dimensions: duplicate dim entries in {seen}"
            )
        return dims

    @model_validator(mode="after")
    def _status_consistency(self) -> "AlignmentContract":
        # open_tensions non-empty ⇒ DRAFT (or BLOCKED)
        if self.open_tensions and self.contract_status == "FINAL":
            raise ValueError(
                "contract_status=FINAL is incompatible with non-empty "
                "open_tensions. Either resolve tensions (clear list) or "
                "downgrade status to DRAFT."
            )
        # executive_summary.open_questions must mirror open_tensions length
        if self.executive_summary.open_questions != len(self.open_tensions):
            raise ValueError(
                "executive_summary.open_questions must equal "
                "len(open_tensions); got "
                f"{self.executive_summary.open_questions} vs "
                f"{len(self.open_tensions)}"
            )
        # BLOCKED requires block_reason
        if self.contract_status == "BLOCKED" and not (self.block_reason or "").strip():
            raise ValueError(
                "contract_status=BLOCKED requires non-empty block_reason."
            )
        if self.contract_status != "BLOCKED" and self.block_reason:
            raise ValueError(
                "block_reason must be null/empty unless contract_status=BLOCKED."
            )
        return self


__all__ = [
    "AlignmentContract",
    "Arm",
    "ArmSpec",
    "Classification",
    "CodeHandle",
    "Dimension",
    "ExecutiveSummary",
    "LOCKS_REQUIRING_PAPER_VALUE",
    "Lock",
    "Precondition",
    "REQUIRED_DIMENSIONS",
    "Sources",
]
