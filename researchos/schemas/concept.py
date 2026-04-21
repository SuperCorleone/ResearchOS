"""Atomic concept and advisor verdict schemas (Wave 1 A12 / A16).

Adapted from HKU AI-Researcher's idea decomposition pattern — each paper is
broken into atomic academic definitions that are independently traceable to
math, code, and reference literature. ``AdvisorVerdict`` is the critic
side-output that pairs a pass/fail per concept with a one-sentence suggestion.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# Accepted code_implementation formats:
#   1. <file>::<function>           e.g. "train.py::build_model"
#   2. <slug>::<path>::L<start>-L<end>  e.g. "repo__name::src/model.py::L10-L50"
#   3. Literal "NOT_AVAILABLE"      when no code exists yet
#   4. Empty string (default)
_FILE_FUNC_RE = re.compile(
    r"^[A-Za-z0-9_.\-/]+::[A-Za-z_][A-Za-z0-9_]*$"
)
_CODEBASE_TRIPLE_RE = re.compile(
    r"^[A-Za-z0-9_.\-]+::[^:]+::L\d+-L\d+$"
)


class AtomicConcept(BaseModel):
    """A single self-contained academic concept with math + code anchors."""

    id: str                                    # C1, C2, ...
    definition: str                            # single self-contained concept
    math_formula: str = ""                     # LaTeX
    code_implementation: str = ""              # file::func locator or NOT_AVAILABLE
    reference_papers: list[str] = Field(default_factory=list)
    reference_codebases: list[str] = Field(default_factory=list)

    @field_validator("code_implementation")
    @classmethod
    def validate_code_implementation(cls, v: str) -> str:
        """Reject vague english, package names, and pseudocode.

        Accepted formats:
        - Empty string (field not yet populated)
        - ``<file>::<function>`` e.g. ``train.py::build_model``
        - ``<slug>::<path>::L<start>-L<end>`` (codebase triple)
        - Literal ``NOT_AVAILABLE``
        """
        if not v or v == "NOT_AVAILABLE":
            return v
        if _FILE_FUNC_RE.match(v):
            return v
        if _CODEBASE_TRIPLE_RE.match(v):
            return v
        raise ValueError(
            f"code_implementation must be '<file>::<function>', "
            f"'<slug>::<path>::L<start>-L<end>', or 'NOT_AVAILABLE'. "
            f"Got: {v!r}"
        )


class AdvisorVerdict(BaseModel):
    """Critic's per-concept verdict written alongside ``analysis_report.md``."""

    pipeline_id: str
    fully_correct: bool
    per_concept: dict[str, Literal["pass", "fail", "skipped"]]
    suggestions: dict[str, str]                # concept_id -> improvement suggestion
    created_at: str
