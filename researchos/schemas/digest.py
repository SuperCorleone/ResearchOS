"""StepDigest Pydantic models — typed contracts between pipeline steps.

Purpose: replace the current "every downstream agent re-parses upstream
markdown" pattern with typed, machine-consumable digests. Each step writes
BOTH:

  1. <step>.<ext>                    — human-readable artifact (archived)
  2. <step>.digest.json              — Pydantic-validated typed payload

The dispatcher injects ONLY the digest JSON into downstream prompts, never
the full markdown. Typical digest ≈ 2KB vs markdown ≈ 15KB → ~85% context
savings, plus guaranteed schema stability.

Validation is ENFORCEMENT not guidance — model_validator raises
ValueError when contracts are violated. state_manager.save_pipeline()
wraps this in ``ContractViolation`` → pipeline BLOCK.

See ``researchos/core/contract_guard.py`` for cross-digest invariants
(e.g. metric name alignment across Hypothesis → Plan → Run).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from researchos.schemas.concept import AtomicConcept


# ---------------------------------------------------------------------------
# Regex patterns for atomicity check
# ---------------------------------------------------------------------------

# Verbs that signal an algorithmic / computational concept (vs. a background
# noun like "MAPE-K loop" or "Self-Adaptive Systems"). If a concept's
# definition contains NONE of these AND has no math_formula, it's almost
# certainly a background noun that got through over-splitting prompt bias.
_ALGORITHMIC_VERBS = re.compile(
    r"\b(combine|select|learn|optimize|compute|estimate|aggregate|minimize|"
    r"maximize|sample|propagate|encode|decode|project|factor|normalize|"
    r"regress|classify|predict|infer|transform|weight|average|search|"
    r"approximate|reconstruct|match|rank|filter|cluster|embed|attend|"
    r"replace|substitute|augment)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 1. HypothesisDigest
# ---------------------------------------------------------------------------

class RQMetricBinding(BaseModel):
    """One research question paired with the paper's exact metric names."""

    rq_id: str                           # "RQ1", "RQ2", ...
    question: str
    metric_names: list[str]              # exact names from paper — binding


class HypothesisDigest(BaseModel):
    """Typed summary of hypothesis.md.

    Consumed by: exp_designer, concept_extract_paper, judge.
    Key invariant: ``metric_names`` must be preserved through Plan and Run.
    """

    research_question: str
    falsifiable_claim: str               # 1-2 sentences, operationalized
    metric_names: list[str]              # union across rq_bindings
    rq_bindings: list[RQMetricBinding]
    baseline_paper_ref: str | None = None
    reference_methodology_summary: str | None = None


# ---------------------------------------------------------------------------
# 2. PlanDigest
# ---------------------------------------------------------------------------

BaselineStrategy = Literal[
    "run_inline",                        # baseline code ships in run_experiment
    "paper_values_with_source",          # paper reports numbers we compare against
    "waived_with_reason",                # explicit waiver, reason required
]


class PlanDigest(BaseModel):
    """Typed summary of experiment_plan.yaml.

    Consumed by: coder, experiment_runner, judge.
    """

    metric_names: list[str]              # must ⊇ HypothesisDigest.metric_names
    baseline_strategy: BaselineStrategy
    baseline_reason: str | None = Field(
        default=None,
        description="REQUIRED when baseline_strategy == 'waived_with_reason'",
    )
    data_source: Literal["file", "api", "synthetic", "manual"]
    data_location: str | None = None
    data_sample_count: int | None = None
    data_hash_expected: str | None = None   # from data_manifest.yaml
    uses_reference_paper_data: bool = False

    @model_validator(mode="after")
    def _waiver_requires_reason(self) -> PlanDigest:
        if self.baseline_strategy == "waived_with_reason" and not self.baseline_reason:
            raise ValueError(
                "baseline_strategy='waived_with_reason' requires baseline_reason"
            )
        return self


# ---------------------------------------------------------------------------
# 3. ConceptsDigest
# ---------------------------------------------------------------------------

class ConceptsDigest(BaseModel):
    """Typed wrapper for concepts.yaml with atomicity enforcement.

    The key difference vs. the bare ``list[AtomicConcept]`` is the
    ``_reject_background_nouns`` validator, which catches the failure
    mode observed in pipeline_mitigating_model_uncertainty_i where 85%
    of concepts were background nouns like "Self-Adaptive Systems"
    rather than atomic algorithmic mechanisms.
    """

    concepts: list[AtomicConcept]
    code_side_coverage: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Fraction of concepts whose code_implementation is NOT "
            "'NOT_AVAILABLE'. Must be ≥ 0.5 when code_recon has usable repos."
        ),
    )
    code_recon_had_repos: bool = False

    @model_validator(mode="after")
    def _reject_background_nouns(self) -> ConceptsDigest:
        """A concept whose definition contains NO algorithmic verb AND
        has NO math_formula is almost certainly a background noun. If
        more than 30% of concepts match this pattern, something went
        wrong in extraction — BLOCK."""

        if not self.concepts:
            return self
        bad = [
            c for c in self.concepts
            if not _ALGORITHMIC_VERBS.search(c.definition)
            and not c.math_formula.strip()
        ]
        ratio = len(bad) / len(self.concepts)
        if ratio > 0.3:
            bad_ids = [c.id for c in bad[:5]]
            raise ValueError(
                f"{len(bad)}/{len(self.concepts)} concepts are background "
                f"nouns (no algorithmic verb, no formula). Examples: "
                f"{bad_ids}. This indicates over-splitting or a concept "
                f"boundary set too wide — re-run concept_extract with "
                f"tighter scope."
            )
        return self

    @model_validator(mode="after")
    def _coverage_when_repos_available(self) -> ConceptsDigest:
        if self.code_recon_had_repos and self.code_side_coverage < 0.5:
            raise ValueError(
                f"code_recon had {'' if self.code_recon_had_repos else 'no '}"
                f"usable repos but code_side_coverage={self.code_side_coverage:.2f} "
                f"< 0.5. Concept_extract_code skipped its job — re-run."
            )
        return self


# ---------------------------------------------------------------------------
# 4. RunDigest
# ---------------------------------------------------------------------------

MetricValue = float | int | Literal["NOT_IMPLEMENTED"] | str


class RunDigest(BaseModel):
    """Typed summary of results.json.

    Consumed by: judge (primary), publisher, paper_writer.
    Key invariants enforced by contract_guard.assert_metric_alignment():
    - self.metrics.keys() ⊇ PlanDigest.metric_names
    - self.data_hash == data_manifest.directory_hash
    """

    metrics: dict[str, MetricValue]
    baseline_metrics: dict[str, float] | None = None
    data_hash: str
    data_rows: int | None = None
    seed: int
    runtime_seconds: float
    entry_module: str = "run_experiment"     # for judge's smoke test
    fast_fail_reason: str | None = None      # non-null iff blocked run


# ---------------------------------------------------------------------------
# 5. JudgeVerdict (replaces critic + experiment_analyser + interrogator +
#    AdvisorVerdict in a single typed output)
# ---------------------------------------------------------------------------

class CodeEvidence(BaseModel):
    """Evidence the judge gathered for one concept."""

    concept_id: str
    locator: str                         # from AtomicConcept.code_implementation
    file_exists: bool
    symbol_found: bool                   # via list_file_symbols
    line_range: tuple[int, int] | None = None
    smoke_test_passed: bool | None = None


class FigureSpec(BaseModel):
    """A figure judge deems worth producing (figure_composer input)."""

    title: str
    chart_type: str                      # e.g. "grouped_bar", "heatmap"
    data_source: str                     # JSON path into results.json
    rationale: str


Recommendation = Literal["ITERATE", "ADVANCE", "PIVOT"]


class JudgeVerdict(BaseModel):
    """Authoritative post-run verdict. Merges critic + experiment_analyser
    + interrogator + AdvisorVerdict into one typed payload.

    The judge agent is the ONLY agent allowed to set ``fully_correct``.
    Downstream (publisher, paper_writer) consumes this without re-parsing.
    """

    pipeline_id: str
    fully_correct: bool
    recommendation: Recommendation
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    summary: str = Field(max_length=1500)

    per_concept: dict[str, Literal["pass", "fail", "skipped"]] = Field(
        default_factory=dict
    )
    suggestions: dict[str, str] = Field(default_factory=dict)
    code_evidence: dict[str, CodeEvidence] = Field(default_factory=dict)

    # Optional fields — null when no data supports them. NEVER filled with
    # generic language just to satisfy a template.
    ablation_notes: str | None = None
    sensitivity_notes: str | None = None
    visualization_plan: list[FigureSpec] | None = None
    failure_modes: list[str] | None = None

    @model_validator(mode="after")
    def _empty_per_concept_requires_correct(self) -> JudgeVerdict:
        """If concepts.yaml existed (per_concept non-empty), we must report
        per-concept verdicts. If empty, fully_correct MUST be True —
        otherwise we're claiming failure with no evidence."""

        if self.per_concept == {} and not self.fully_correct:
            raise ValueError(
                "per_concept is empty but fully_correct=False. "
                "Either (a) concepts existed — populate per_concept, or "
                "(b) no concepts existed — set fully_correct=True. "
                "The current state is the empty-contract "
                "failure mode and is rejected."
            )
        return self

    @model_validator(mode="after")
    def _no_fail_without_suggestion(self) -> JudgeVerdict:
        """Every 'fail' concept must have a suggestion keyed by concept_id."""
        missing = [
            cid for cid, verdict in self.per_concept.items()
            if verdict == "fail" and cid not in self.suggestions
        ]
        if missing:
            raise ValueError(
                f"Concepts {missing} marked 'fail' but no suggestion "
                f"provided. Fail without guidance is unactionable."
            )
        return self


# ---------------------------------------------------------------------------
# Registry of digest filenames per step (used by dispatcher to locate
# upstream digests when building downstream prompts)
# ---------------------------------------------------------------------------

DIGEST_FILENAMES: dict[str, str] = {
    "hypothesis": "hypothesis.digest.json",
    "design": "plan.digest.json",
    "concept_paper": "concepts_paper.digest.json",
    "concept_code": "concepts.digest.json",        # final merged
    "run": "run.digest.json",
    "judge": "judge.digest.json",
}


def digest_path_for(step: str, pipeline_dir) -> "Path | None":   # noqa: F821
    """Return absolute path of the digest file for ``step``, if defined."""
    from pathlib import Path

    filename = DIGEST_FILENAMES.get(step)
    if filename is None:
        return None
    return Path(pipeline_dir) / filename
