"""Structured spec extracted from a single base paper.

Produced by the ``paper_spec_extractor`` agent during the ``PAPER_SPEC``
pipeline step. Consumed by ``alignment_synthesizer`` and dereferenced by
every downstream agent through ``alignment_contract.yaml`` value_refs.

Design contract:
  * The extractor is given exactly ONE document (the base paper) and is
    forbidden from any cross-document retrieval.
  * Every non-exempt non-empty field MUST be supported by a verbatim quote
    in ``evidence[]``. The validator does substring matching after PDF
    normalisation, so quotes are machine-checkable.
  * Unknown values are written as ``NOT_STATED`` (or ``"NOT_STATED in paper"``
    for free-text fields). Inferring from prior knowledge is forbidden.
  * Method-level structure is exposed via ``baseline_method.method_steps``;
    proposal_spec's ``new_components.replaces / adds_to`` reference these
    step IDs, so the IDs are part of the public contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Constants — kept here so paper_spec_extractor and validator share one source
# ---------------------------------------------------------------------------

NOT_STATED = "NOT_STATED"

#: Step ID format used in ``method_steps`` and referenced from proposal_spec.
#: Sequential ``S1``, ``S2``, ``S3``... — anchors the proposal's
#: ``replaces`` / ``adds_to`` fields.
_STEP_ID_RE = r"^S\d+$"


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


class ParaphraseReason(BaseModel):
    """Why an RQ ``question`` field is not a verbatim copy of the paper."""

    type: Literal[
        "split_across_sentences",
        "declarative_to_interrogative",
        "other",
    ] | None = Field(
        default=None,
        description="None when ``question`` is verbatim. Set to one of the "
        "named reasons when paraphrased; ``other`` requires explanation.",
    )
    explanation: str = Field(
        default="",
        description="Required when type != None. Empty otherwise.",
    )


class ResearchQuestion(BaseModel):
    """One RQ from the paper.

    ``question_verbatim`` is the substring-checkable field; ``question`` may
    be a light paraphrase to normalise interrogative form. Validators key on
    ``question_verbatim``.
    """

    id: str = Field(
        description="RQ identifier (RQ1, RQ2, ...). Sequential, paper-defined."
    )
    question: str = Field(
        description="Question as the spec presents it downstream. May be a "
        "paraphrase; ``paraphrase_reason.type`` must be non-null in that case."
    )
    question_verbatim: str = Field(
        description="Original sentence(s) from the paper. Validator does "
        "substring match against base_paper.md on this field."
    )
    paraphrase_reason: ParaphraseReason = Field(default_factory=ParaphraseReason)
    paper_metrics: list[str] = Field(
        default_factory=list,
        description="Metric names exactly as the paper writes them. Empty list "
        "is allowed only when paper does not state metrics for this RQ.",
    )
    paper_baseline_value: dict = Field(
        default_factory=lambda: {
            "table_or_section": NOT_STATED,
            "value": NOT_STATED,
        },
        description="Baseline value reported by the paper for this RQ, with "
        "table/section pointer.",
    )


class HyperParameterRef(BaseModel):
    name: str
    value: str = Field(
        description="String form of the hyperparameter value, or NOT_STATED."
    )
    source: str = Field(
        default=NOT_STATED,
        description="Paper section / equation reference, or NOT_STATED.",
    )


class MethodStep(BaseModel):
    """A named step in the baseline method.

    ``id`` is the public anchor: proposal_spec uses it for replaces/adds_to.
    Sequential ``S1``, ``S2``, ``S3``... so the proposal author and synthesizer
    can reason about the method's decomposition without inventing names.
    """

    id: str = Field(pattern=_STEP_ID_RE, description="S1, S2, ... sequential.")
    name: str = Field(description="Short human-readable name (monitor / analyze / plan / ...).")
    role: str = Field(
        default="",
        description="What this step does, paraphrased. May be empty when the "
        "name is self-explanatory.",
    )
    paper_section: str = Field(
        default=NOT_STATED,
        description="§-reference into the paper, e.g. '§3.2'.",
    )


class BaselineMethod(BaseModel):
    name: str = Field(description="Algorithm name as the paper calls it.")
    family: str = Field(
        default=NOT_STATED,
        description="High-level family (Bayesian, ensemble, GLM, transformer, ...).",
    )
    key_components: list[str] = Field(
        default_factory=list,
        description="Components at the level the proposal needs to reason about.",
    )
    hyperparameters_stated: list[HyperParameterRef] = Field(default_factory=list)
    algorithmic_sketch: str = Field(
        default="",
        description="Paraphrased step list. Free-form prose, not used for "
        "machine reasoning — method_steps is the structured anchor.",
    )
    method_steps: list[MethodStep] = Field(
        default_factory=list,
        description="Structured method decomposition. Step IDs are referenced "
        "by proposal_spec.proposed_method.new_components.{replaces,adds_to}.",
    )

    @field_validator("method_steps")
    @classmethod
    def _step_ids_unique_and_sequential(
        cls, steps: list[MethodStep]
    ) -> list[MethodStep]:
        if not steps:
            return steps
        ids = [s.id for s in steps]
        if len(set(ids)) != len(ids):
            raise ValueError(f"method_steps[].id must be unique; got {ids}")
        # Sequential check: S1, S2, ... — gaps are allowed (extractor may
        # legitimately skip a step that the paper omits) but ordering must
        # be ascending.
        nums = [int(i[1:]) for i in ids]
        if nums != sorted(nums):
            raise ValueError(f"method_steps must be in ascending S-id order; got {ids}")
        return steps


class CandidateModelPool(BaseModel):
    """For ensemble / SAS-type papers. Set ``applicable=False`` otherwise."""

    applicable: bool = Field(
        description="True iff the paper has an explicit pool of candidate "
        "models (typical for ensemble / model-selection / SAS work)."
    )
    members: list[str] = Field(default_factory=list)
    homogeneity: Literal["all_GLMs", "homogeneous", "heterogeneous", "NOT_STATED"] = Field(
        default="NOT_STATED"
    )
    notes: str = Field(default="")


class DatasetAccess(BaseModel):
    type: Literal[
        "paper_repo",
        "external_url",
        "shared_archive",
        "not_public",
        "NOT_STATED",
    ] = Field(default="NOT_STATED")
    location: str = Field(
        default=NOT_STATED,
        description="Path inside ref_code, external URL, or NOT_STATED.",
    )
    license: str = Field(default=NOT_STATED)


class Dataset(BaseModel):
    name: str = Field(
        description="Exact name as the paper writes it (preserve casing, "
        "underscores, version suffixes — e.g. 'rescueRobot_450', not "
        "'rescue robot dataset')."
    )
    domain: str = Field(default=NOT_STATED)
    size: str = Field(default=NOT_STATED, description="Sample count or NOT_STATED.")
    format: str = Field(default=NOT_STATED, description="csv | image | timeseries | ...")
    features: list[str] = Field(default_factory=list)
    target: str = Field(default=NOT_STATED)
    access: DatasetAccess = Field(default_factory=DatasetAccess)


class SplitProtocol(BaseModel):
    strategy: Literal[
        "random",
        "temporal",
        "k_fold",
        "replay",
        "stratified",
        "group",
        "NOT_STATED",
    ] = Field(default="NOT_STATED")
    ratio: str = Field(default=NOT_STATED, description="train:val:test or NOT_STATED.")
    seed: str = Field(default=NOT_STATED)
    notes: str = Field(default="")


class EvaluationMetric(BaseModel):
    name: str
    direction: Literal["higher_better", "lower_better", "NOT_STATED"] = Field(
        default="NOT_STATED"
    )
    formula_or_definition: str = Field(default=NOT_STATED)


class EntryPoint(BaseModel):
    file: str = Field(description="Relative path inside the ref_code repo.")
    symbol: str = Field(description="Class or function name.")
    role: str = Field(
        description="What this entry point does in the baseline pipeline "
        "(baseline_train, baseline_predict, data_loader, ...)."
    )


class ReferenceCode(BaseModel):
    available: bool = Field(
        description="True iff the paper publicly releases code and the user "
        "(or extractor) was able to identify a usable repo URL."
    )
    url: str = Field(default=NOT_STATED)
    commit_or_tag: str = Field(default=NOT_STATED)
    language: str = Field(default=NOT_STATED)
    install_command: str = Field(default=NOT_STATED)
    entry_points: list[EntryPoint] = Field(default_factory=list)


class AblationInPaper(BaseModel):
    description: str
    finding: str = Field(default=NOT_STATED)


class EvidenceQuote(BaseModel):
    """Verbatim quote backing one field of the paper_spec.

    ``field`` is a dotted path into paper_spec (e.g. ``dataset.name``,
    ``research_questions[id=RQ1].question_verbatim``). ``quote`` MUST be a
    substring of base_paper.md after normalisation; the validator enforces
    this. ``location`` is human-friendly section pointer for traceability.
    """

    field: str = Field(description="Dotted path to the spec field this quote backs.")
    quote: str = Field(description="Verbatim text from the paper.")
    location: str = Field(
        default="",
        description="Free-form pointer (e.g. '§3.1, p.5'). Optional but recommended.",
    )


# ---------------------------------------------------------------------------
# Top-level paper_spec
# ---------------------------------------------------------------------------


class PaperSpec(BaseModel):
    """Structured spec of a single base paper. Written to ``paper_spec.yaml``."""

    model_config = ConfigDict(extra="forbid")

    paper_id: str = Field(
        description="Slug derived from the title (lowercase, underscored). "
        "System-generated; exempt from evidence requirement."
    )
    title: str = Field(description="Exact title verbatim.")
    authors: list[str] = Field(default_factory=list)
    venue: str = Field(default=NOT_STATED)

    problem_statement: str = Field(
        default="",
        description="2-3 sentences quoting / paraphrasing the paper's stated "
        "problem. Empty allowed only when paper has no clear problem section.",
    )

    research_questions: list[ResearchQuestion] = Field(default_factory=list)
    baseline_method: BaselineMethod
    candidate_model_pool: CandidateModelPool
    dataset: Dataset
    split_protocol: SplitProtocol = Field(default_factory=SplitProtocol)
    preprocessing: list[str] = Field(default_factory=list)
    evaluation_metrics: list[EvaluationMetric] = Field(default_factory=list)
    reference_code: ReferenceCode
    ablations_in_paper: list[AblationInPaper] = Field(default_factory=list)

    evidence: list[EvidenceQuote] = Field(
        default_factory=list,
        description="Verbatim quotes backing non-exempt non-empty fields. "
        "Coverage threshold (>=0.90 of denominator) enforced by validator.",
    )

    @field_validator("research_questions")
    @classmethod
    def _rq_ids_unique(cls, rqs: list[ResearchQuestion]) -> list[ResearchQuestion]:
        ids = [r.id for r in rqs]
        if len(set(ids)) != len(ids):
            raise ValueError(f"research_questions[].id must be unique; got {ids}")
        return rqs

    @field_validator("research_questions")
    @classmethod
    def _paraphrase_explanation_required(
        cls, rqs: list[ResearchQuestion]
    ) -> list[ResearchQuestion]:
        for rq in rqs:
            pr = rq.paraphrase_reason
            if pr.type is not None and not pr.explanation:
                raise ValueError(
                    f"{rq.id}: paraphrase_reason.explanation is required when "
                    f"paraphrase_reason.type is set ({pr.type!r})."
                )
        return rqs


__all__ = [
    "AblationInPaper",
    "BaselineMethod",
    "CandidateModelPool",
    "Dataset",
    "DatasetAccess",
    "EntryPoint",
    "EvaluationMetric",
    "EvidenceQuote",
    "HyperParameterRef",
    "MethodStep",
    "NOT_STATED",
    "ParaphraseReason",
    "PaperSpec",
    "ReferenceCode",
    "ResearchQuestion",
    "SplitProtocol",
]
