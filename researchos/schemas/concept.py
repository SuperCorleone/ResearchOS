"""Atomic concept and advisor verdict schemas (Wave 1 A12 / A16).

Adapted from HKU AI-Researcher's idea decomposition pattern — each paper is
broken into atomic academic definitions that are independently traceable to
math, code, and reference literature. ``AdvisorVerdict`` is the critic
side-output that pairs a pass/fail per concept with a one-sentence suggestion.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AtomicConcept(BaseModel):
    """A single self-contained academic concept with math + code anchors."""

    id: str                                    # C1, C2, ...
    definition: str                            # single self-contained concept
    math_formula: str = ""                     # LaTeX
    code_implementation: str = ""              # code snippet or file::func locator
    reference_papers: list[str] = Field(default_factory=list)
    reference_codebases: list[str] = Field(default_factory=list)


class AdvisorVerdict(BaseModel):
    """Critic's per-concept verdict written alongside ``analysis_report.md``."""

    pipeline_id: str
    fully_correct: bool
    per_concept: dict[str, Literal["pass", "fail", "skipped"]]
    suggestions: dict[str, str]                # concept_id -> improvement suggestion
    created_at: str
