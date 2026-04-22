"""Topic evaluation schemas for the 8-step Topic Evaluator pipeline."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field, field_validator


class RepeatDegree(str, Enum):
    HIGH = "high"       # 4 axes similar -> direct reject
    MEDIUM = "medium"   # 3 axes similar -> professor review
    MEDIUM_LOW = "medium_low"  # 2 axes similar -> normal
    LOW = "low"         # 0-1 axes similar -> priority


class GateResult(str, Enum):
    """Outcome of the topic evaluation gate (Step 7).

    PASS — score >= 10 and repeat_degree LOW/MEDIUM_LOW: auto-promote to pipeline.
    REJECT — score < 5 or repeat_degree HIGH: permanently discarded.
    PROFESSOR_REVIEW — borderline score or MEDIUM repeat: needs human decision.
    BACKLOG — feasibility too low but idea has merit: re-evaluate next cycle.
    """

    PASS = "pass"
    REJECT = "reject"
    PROFESSOR_REVIEW = "professor_review"
    BACKLOG = "backlog"


class SimilarityAxis(BaseModel):
    """One of the four similarity axes against a reference paper."""
    problem: str = "different"   # "similar" or "different"
    method: str = "different"
    setting: str = "different"
    metric: str = "different"

    @property
    def similar_count(self) -> int:
        return sum(
            1 for v in [self.problem, self.method, self.setting, self.metric]
            if v == "similar"
        )


class SimilarPaper(BaseModel):
    """A paper found during online retrieval with similarity analysis."""
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str = ""
    citation_count: int = 0
    doi: str = ""
    arxiv_id: str = ""
    source: str = ""  # "semantic_scholar" or "arxiv"
    weighted_similarity: float = 0.0
    axes: SimilarityAxis = Field(default_factory=SimilarityAxis)


class InnovationStatement(BaseModel):
    """Forced innovation template (Step 5)."""
    problem_gap: str = ""   # "Existing methods fail because ______"
    our_delta: str = ""     # "We propose ______"
    why_it_matters: str = "" # "Which improves ______ under ______"


class FalsificationDesign(BaseModel):
    """Falsification design generated from most similar paper's diff axes (Step 6)."""
    hypothesis: str = ""
    baseline: str = ""
    metric: str = ""
    failure_condition: str = ""  # e.g. "improvement < 2%"
    expected_signal: str = ""
    min_budget_ratio: float = 0.1


class EvaluationScores(BaseModel):
    """Five-dimensional scoring (0-3 each, max 15)."""
    significance: int = 0
    novelty: int = 0
    differentiation: int = 0
    feasibility: int = 0
    falsifiability: int = 0

    @field_validator('significance', 'novelty', 'differentiation', 'feasibility', 'falsifiability')
    @classmethod
    def check_score_range(cls, v: int) -> int:
        if not (0 <= v <= 3):
            raise ValueError(f'Score must be 0-3, got {v}')
        return v

    @property
    def total(self) -> int:
        return (
            self.significance + self.novelty + self.differentiation
            + self.feasibility + self.falsifiability
        )


class TopicEvaluation(BaseModel):
    """Complete output of the 8-step Topic Evaluator pipeline.

    Written to ``knowledge/proposals/evaluated_<id>.yaml``.
    """
    proposal_id: str
    title: str
    research_question: str = ""
    direction: str = ""

    # Step 1: KB retrieval
    kb_duplicates: list[str] = Field(default_factory=list)
    kb_related: list[str] = Field(default_factory=list)

    # Step 2: Online retrieval
    similar_papers: list[SimilarPaper] = Field(default_factory=list)
    retrieval_cache_key: str = ""

    # Step 3: Four-axis similarity
    repeat_degree: RepeatDegree = RepeatDegree.LOW

    # Step 4: Five-dimensional scores
    scores: EvaluationScores = Field(default_factory=EvaluationScores)

    # Step 5: Innovation statement
    innovation: InnovationStatement = Field(default_factory=InnovationStatement)

    # Step 6: Falsification design
    falsification: FalsificationDesign = Field(default_factory=FalsificationDesign)

    # Step 7: Gate result
    gate_result: GateResult = Field(
        default=GateResult.BACKLOG,
        description="Final gate outcome: pass (auto-promote), reject (discard), "
        "professor_review (human decision needed), or backlog (re-evaluate next cycle)."
    )
    gate_reasons: list[str] = Field(
        default_factory=list,
        description="One-sentence justifications for the gate_result. Include score thresholds that triggered the decision."
    )

    # Metadata
    evaluator_model: str = ""
    evaluated_at: str = ""
    re_eval_count: int = 0  # For backlog items

    class Config:
        use_enum_values = True
