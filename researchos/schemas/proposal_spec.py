"""Structured spec extracted from the user-provided proposal.

Produced by ``proposal_spec_extractor`` during the ``PROPOSAL_SPEC`` step.
Consumed by ``alignment_synthesizer`` together with ``paper_spec.yaml``.

Design contract:
  * The extractor sees ONLY proposal.md. It must not read paper_spec or
    base_paper.md — its job is to capture the proposal's stated intent
    without inferring paper details. The synthesizer is the only agent
    allowed to reason across both.
  * ``change_type`` is a forced enum. When the proposal text does not
    support a confident classification, the extractor sets
    ``change_type_source = "INFERRED"`` and ``change_type_confidence = "low"``;
    the synthesizer is then permitted to override.
  * ``new_components.replaces`` and ``new_components.adds_to`` reference
    method-step IDs from paper_spec (S1, S2, ...). Validity of the IDs is
    checked by alignment_validator, not here — at extraction time we only
    enforce the syntactic shape.
  * ``divergences[].arbitration_hint`` is explicitly marked
    ``binding=false``. Synthesizer reads it as evidence, not as a decision.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOT_STATED_IN_PROPOSAL = "NOT_STATED in proposal"

#: Forced enum. The synthesizer's default-lock table keys on these values.
ChangeType = Literal[
    "replacement_in_method",
    "extension_of_method",
    "cross_method_combination",
    "cross_domain_application",
    "new_task",
]


#: Method-step ID format (mirrors paper_spec._STEP_ID_RE). Replicated here
#: rather than imported because proposal extraction is intentionally a
#: separate concern — the validator is the one that actually checks
#: cross-spec consistency.
_STEP_ID_PATTERN = r"^S\d+$"


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


class ProposalEvidence(BaseModel):
    """Verbatim quote from proposal.md backing a proposal_spec field."""

    field: str = Field(description="Dotted path to the spec field this quote backs.")
    quote: str = Field(description="Verbatim text from the proposal.")
    location: str = Field(default="", description="Optional pointer (e.g. paragraph #).")


class CitedLimitation(BaseModel):
    text: str = Field(description="Paraphrased limitation the proposal cites.")
    quote: str = Field(description="Verbatim quote backing the limitation.")


class Motivation(BaseModel):
    summary: str = Field(default="")
    cited_limitations_of_baseline: list[CitedLimitation] = Field(default_factory=list)


class NewComponent(BaseModel):
    """A new piece of method that the proposal introduces.

    ``replaces`` and ``adds_to`` are mutually exclusive: a component either
    swaps out a paper step or augments one. Pointing nowhere (both null) is
    allowed for components that are entirely new (no paper anchor).
    """

    id: str = Field(description="Component identifier (C_new_1, C_new_2, ...).")
    name: str = Field(description="Short human-readable name.")
    role: str = Field(default="", description="What this component does.")
    math_sketch: str = Field(
        default=NOT_STATED_IN_PROPOSAL,
        description="Math sketch when the proposal states one, else "
        "NOT_STATED_IN_PROPOSAL. Inferring formulas from prior knowledge "
        "is forbidden.",
    )
    replaces: str | None = Field(
        default=None,
        description="Paper method-step ID (S1, S2, ...) this component "
        "replaces. Mutually exclusive with adds_to.",
    )
    adds_to: str | None = Field(
        default=None,
        description="Paper method-step ID this component augments. Mutually "
        "exclusive with replaces.",
    )

    @field_validator("replaces", "adds_to")
    @classmethod
    def _step_id_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        import re
        if not re.match(_STEP_ID_PATTERN, v):
            raise ValueError(
                f"replaces/adds_to must match {_STEP_ID_PATTERN} (e.g. 'S2'); got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _replaces_adds_to_exclusive(self) -> "NewComponent":
        if self.replaces is not None and self.adds_to is not None:
            raise ValueError(
                f"{self.id}: replaces and adds_to are mutually exclusive; "
                f"set at most one (got replaces={self.replaces!r}, adds_to={self.adds_to!r})."
            )
        return self


class BorrowedField(BaseModel):
    """A paper_spec field path the proposal explicitly inherits unchanged."""

    paper_field: str = Field(
        description="Dotted path into paper_spec (validator dereferences "
        "this; extractor only checks the shape)."
    )
    quote: str = Field(
        default="",
        description="Verbatim quote from proposal showing the inheritance "
        "claim. Empty allowed but coverage will be lower.",
    )


class ArbitrationHint(BaseModel):
    """Proposal author's preferred resolution of a divergence.

    NOT BINDING. The synthesizer reads ``content`` as evidence only and may
    explicitly disagree in ``alignment_contract.dimensions[i].reason``.
    Schema enforces ``binding=False`` and ``authored_by="proposal"`` so this
    role boundary cannot be silently violated.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(description="Proposal author's suggested resolution.")
    authored_by: Literal["proposal"] = Field(default="proposal")
    binding: Literal[False] = Field(default=False)


class Divergence(BaseModel):
    """A point where the proposal explicitly differs from the paper.

    The synthesizer must decide LOCKED vs override per dimension. The proposal
    can only document the divergence and (optionally) hint at a resolution.
    """

    paper_field: str = Field(description="Dotted path into paper_spec.")
    proposal_says: str = Field(description="What the proposal proposes instead.")
    proposal_quote: str = Field(description="Verbatim quote from proposal.")
    arbitration_hint: ArbitrationHint | None = Field(
        default=None,
        description="Optional. Author's preferred resolution; non-binding.",
    )


class AblationClaim(BaseModel):
    id: str = Field(description="Ablation identifier (A1, A2, ...).")
    description: str
    hypothesis: str = Field(default="")
    threshold: str = Field(default=NOT_STATED_IN_PROPOSAL)


class ProposedMethod(BaseModel):
    name: str
    high_level_idea: str = Field(default="")
    new_components: list[NewComponent] = Field(default_factory=list)
    borrowed_from_paper: list[BorrowedField] = Field(default_factory=list)
    divergences: list[Divergence] = Field(default_factory=list)
    ablation_claims: list[AblationClaim] = Field(default_factory=list)

    @field_validator("new_components")
    @classmethod
    def _component_ids_unique(cls, comps: list[NewComponent]) -> list[NewComponent]:
        ids = [c.id for c in comps]
        if len(set(ids)) != len(ids):
            raise ValueError(f"new_components[].id must be unique; got {ids}")
        return comps

    @field_validator("ablation_claims")
    @classmethod
    def _ablation_ids_unique(cls, abls: list[AblationClaim]) -> list[AblationClaim]:
        ids = [a.id for a in abls]
        if len(set(ids)) != len(ids):
            raise ValueError(f"ablation_claims[].id must be unique; got {ids}")
        return abls


class OutOfScopeRQ(BaseModel):
    id: str = Field(description="RQ id from paper (RQ1, RQ2, ...).")
    reason: str


class Scope(BaseModel):
    in_scope_rqs: list[str] = Field(default_factory=list)
    out_of_scope_rqs: list[OutOfScopeRQ] = Field(default_factory=list)
    added_questions: list[str] = Field(default_factory=list)


class SuccessCriterion(BaseModel):
    rq_id: str = Field(description="Paper RQ id this criterion targets.")
    metric: str
    direction: Literal["higher_better", "lower_better"]
    threshold: float


# ---------------------------------------------------------------------------
# Top-level proposal_spec
# ---------------------------------------------------------------------------


class ProposalSpec(BaseModel):
    """Structured spec of the user proposal. Written to ``proposal_spec.yaml``."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(description="Slug derived from title.")
    title: str
    target_paper: str = Field(
        description="paper_id of the base paper, or 'multi' for multi-paper "
        "fusion proposals, or 'none' for self-contained proposals.",
    )

    change_type: ChangeType = Field(description="Forced enum; see top of module.")
    change_type_evidence: str = Field(
        description="Verbatim quote from proposal supporting the change_type "
        "classification. Empty when source==INFERRED.",
    )
    change_type_confidence: Literal["high", "medium", "low"] = Field(
        default="high",
        description="'low' iff source==INFERRED — synthesizer will re-evaluate.",
    )
    change_type_source: Literal["extractor", "INFERRED"] = Field(default="extractor")

    motivation: Motivation = Field(default_factory=Motivation)
    proposed_method: ProposedMethod
    scope: Scope = Field(default_factory=Scope)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)

    evidence: list[ProposalEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _confidence_matches_source(self) -> "ProposalSpec":
        if self.change_type_source == "INFERRED" and self.change_type_confidence != "low":
            raise ValueError(
                "change_type_source=INFERRED requires change_type_confidence='low' "
                "(synthesizer relies on this signal to decide whether to re-evaluate)."
            )
        if self.change_type_source == "INFERRED" and self.change_type_evidence:
            # Allowed but unusual — extractor 'guessed' yet provided a quote.
            # Not a hard error; synthesizer will read both and decide.
            pass
        return self

    @model_validator(mode="after")
    def _scope_rqs_disjoint(self) -> "ProposalSpec":
        in_set = set(self.scope.in_scope_rqs)
        out_set = {r.id for r in self.scope.out_of_scope_rqs}
        overlap = in_set & out_set
        if overlap:
            raise ValueError(
                f"scope.in_scope_rqs and out_of_scope_rqs overlap on {sorted(overlap)}"
            )
        return self


__all__ = [
    "AblationClaim",
    "ArbitrationHint",
    "BorrowedField",
    "ChangeType",
    "CitedLimitation",
    "Divergence",
    "Motivation",
    "NOT_STATED_IN_PROPOSAL",
    "NewComponent",
    "OutOfScopeRQ",
    "ProposalEvidence",
    "ProposalSpec",
    "ProposedMethod",
    "Scope",
    "SuccessCriterion",
]
