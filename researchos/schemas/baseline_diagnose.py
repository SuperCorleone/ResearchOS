"""Schemas for baseline_diagnose and baseline reuse preflight.

These models are the canonical contract between three modules:

  - ``researchos/runtime/baseline_diagnose.py`` (producer of BaselineDiagnoseResult)
  - ``researchos/runtime/preflight.py``         (producer of BaselineReuseCheck)
  - ``researchos/runtime/scheduler.py``         (consumer of both, via JSON read)

Producers construct + validate the model internally and return ``model_dump()``
so dict-style consumers and the 62 existing tests stay unchanged. Schedulers
that read the on-disk JSON should call ``model_validate(...)`` defensively to
catch drift introduced by future edits or external tampering.

Why a dedicated module: ``baseline_analysis.py`` already owns the LLM-side
schema (BaselineAnalysis), produced by the baseline_analyzer agent. The
diagnose/reuse models below are produced by pure-Python steps after run() and
have a separate lifecycle, so we keep them in their own file rather than
overload baseline_analysis.py.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# baseline_diagnose: data hash + hyperparameter divergence triage
# ---------------------------------------------------------------------------


class HashDiff(BaseModel):
    """One row of the data-hash divergence table."""

    file: str
    expected: str
    actual: str | None = None


class HyperparamDiff(BaseModel):
    """One row of the hyperparameter divergence table."""

    param: str
    expected: Any
    actual: Any


class DataHashCheck(BaseModel):
    """Outcome of comparing data_manifest.yaml SHA-256s against
    ``baseline_analysis.yaml::data.expected_hashes``.

    ``passed=True`` when there is nothing to disagree with (no expected
    hashes recorded) OR every entry matches.
    """

    passed: bool
    diff: list[HashDiff] = Field(default_factory=list)
    auto_fixable: bool = False


class HyperparameterCheck(BaseModel):
    """Outcome of comparing run-time hyperparameters against
    ``baseline_analysis.yaml::algorithm.hyperparameters_in_paper``.

    ``passed=True`` when either side lacks hyperparameters (cannot disagree)
    OR every key matches under numeric tolerance 1e-9.
    """

    passed: bool
    diff: list[HyperparamDiff] = Field(default_factory=list)


class DiagnoseChecks(BaseModel):
    """The two atomic checks performed by ``diagnose()``."""

    data_hashes: DataHashCheck
    hyperparameters: HyperparameterCheck


class BaselineDiagnoseResult(BaseModel):
    """Top-level return of ``baseline_diagnose.diagnose(pipeline_dir)``.

    Stub form (when ``baseline_analysis.yaml`` is missing or malformed)
    omits ``checks`` and populates ``reason`` instead. ``checks`` is
    therefore optional even though the happy-path always provides it.
    """

    verdict: Literal["auto_fixable", "escalate_to_l2"]
    fixer_directives: list[str] = Field(default_factory=list)
    checks: DiagnoseChecks | None = None
    reason: str | None = None
    note: str | None = None  # used by scheduler when module is unavailable


# ---------------------------------------------------------------------------
# preflight._check_baseline_reuse: static enforcement of coder hard rules
# ---------------------------------------------------------------------------


class BaselineReuseCheck(BaseModel):
    """Outcome of ``preflight._check_baseline_reuse(pipeline_dir)``.

    ``case`` discriminates which branch of coder.md was taken:
      - "A": same-language reuse — must contain ``COPIED FROM:`` markers
      - "B": cross-language port — must have REWRITE_RATIONALE.md + non-empty baseline_original/
      - "C": low confidence or package not acquired — no enforcement
      - "skipped": baseline_analysis.yaml absent or unreadable

    ``reasons`` lists BLOCK reasons; non-empty implies passed=False (in the
    enforced cases). For "C" / "skipped" the list may carry an informational
    note even though passed=True.
    """

    passed: bool
    case: Literal["A", "B", "C", "skipped"]
    reasons: list[str] = Field(default_factory=list)
