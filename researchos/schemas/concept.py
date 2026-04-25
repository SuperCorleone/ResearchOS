"""Atomic concept and advisor verdict schemas.

Adapted from HKU AI-Researcher's idea decomposition pattern — each paper is
broken into atomic academic definitions that are independently traceable to
math, code, and reference literature. ``AdvisorVerdict`` is the critic
side-output that pairs a pass/fail per concept with a one-sentence suggestion.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# Anything that looks like a URL or VCS endpoint is invalid as a code
# implementation locator — the schema demands "<file>::<func>" / "<slug>::
# <path>::L<a>-L<b>" / "NOT_AVAILABLE". LLMs occasionally leak the source
# repository URL into this field; ``sanitize_concepts_yaml`` coerces such
# values to ``NOT_AVAILABLE`` so CODE_PREFLIGHT does not BLOCK the run.
_URL_LIKE_RE = re.compile(r"^(https?://|git@|ftp://|ssh://|www\.)", re.IGNORECASE)


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

    id: str = Field(
        description="Concept identifier (C1, C2, ...). Assign sequentially."
    )
    definition: str = Field(
        description="Single self-contained academic definition of this concept. "
        "Must be precise enough to verify implementation correctness."
    )
    math_formula: str = Field(
        default="",
        description="LaTeX formula capturing the core computation. "
        "Use display-math notation ($$...$$). Leave empty only if concept is purely architectural."
    )
    code_implementation: str = Field(
        default="",
        description="Exact code location in <file>::<function> or <slug>::<path>::L<start>-L<end> format. "
        "Use NOT_AVAILABLE if no reference code exists. "
        "NEVER use natural language descriptions, package names, or pseudocode."
    )
    reference_papers: list[str] = Field(
        default_factory=list,
        description="PDF filenames from evidence_refs that define this concept."
    )
    reference_codebases: list[str] = Field(
        default_factory=list,
        description="Code locations as <slug>::<path>::L<start>-L<end> triples. "
        "Each triple must point to a real file and line range in a cloned reference repo."
    )

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


def _is_valid_code_implementation(v: str) -> bool:
    """Return True iff *v* matches one of the accepted locator patterns."""
    if not v or v == "NOT_AVAILABLE":
        return True
    return bool(_FILE_FUNC_RE.match(v) or _CODEBASE_TRIPLE_RE.match(v))


def sanitize_concepts_yaml(path: Path) -> dict:
    """Coerce invalid ``code_implementation`` values to ``NOT_AVAILABLE`` in-place.

    Called by the scheduler immediately after the concept_extractor agent
    writes its output. We rewrite the file rather than block the pipeline so
    a single rogue LLM completion does not waste an entire run. The original
    invalid values are recorded in the returned report for telemetry.

    Returns ``{"sanitized": [{"id": ..., "was": ..., "now": "NOT_AVAILABLE"}],
    "ok": True/False}``. ``ok=False`` means the file could not be parsed —
    the caller should leave it alone and let preflight raise the proper error.
    """
    report: dict = {"sanitized": [], "ok": True, "path": str(path)}
    if not path.exists():
        report["ok"] = False
        report["error"] = "concepts.yaml not found"
        return report
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        report["ok"] = False
        report["error"] = f"yaml parse failed: {exc}"
        return report

    # concepts.yaml may be either {"concepts": [...]} or a bare list.
    if isinstance(raw, list):
        concepts = raw
        wrapper_key = None
    elif isinstance(raw, dict) and isinstance(raw.get("concepts"), list):
        concepts = raw["concepts"]
        wrapper_key = "concepts"
    else:
        report["ok"] = False
        report["error"] = "unexpected concepts.yaml shape"
        return report

    changed = False
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        impl = concept.get("code_implementation")
        if not isinstance(impl, str):
            continue
        impl_stripped = impl.strip()
        if _is_valid_code_implementation(impl_stripped):
            continue
        # Coerce: any URL-like or otherwise invalid string → NOT_AVAILABLE.
        cid = concept.get("id", "?")
        reason = "url" if _URL_LIKE_RE.match(impl_stripped) else "invalid_format"
        logger.warning(
            "sanitize_concepts: concept %s has invalid code_implementation=%r "
            "(reason=%s) — coercing to NOT_AVAILABLE",
            cid, impl_stripped, reason,
        )
        report["sanitized"].append({"id": cid, "was": impl_stripped, "reason": reason})
        concept["code_implementation"] = "NOT_AVAILABLE"
        changed = True

    if changed:
        try:
            if wrapper_key is None:
                path.write_text(yaml.safe_dump(concepts, sort_keys=False))
            else:
                raw[wrapper_key] = concepts
                path.write_text(yaml.safe_dump(raw, sort_keys=False))
        except Exception as exc:
            report["ok"] = False
            report["error"] = f"yaml write failed: {exc}"
    return report
