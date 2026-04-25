"""BaselineAnalysis — semantic understanding of cloned baseline packages.

Written by ``baseline_analyzer`` (LLM agent) and consumed by ``data_recon``
(pure Python executor). Replaces the v5.2 hardcoded heuristic tables
(`_BADGE_FRAGMENTS`, `_DATA_HOSTS`, `_DATA_EXTENSIONS`, `_CLONE_DATA_DIRS`)
with an LLM analysis of what each cloned repo actually does.

**Code-first principle**: when a baseline repo is cloned, the analyser
greps the entry-point code for real IO calls (``pd.read_csv``,
``torch.load``, ``urllib.request.urlretrieve``, ``wget`` subprocess, etc.)
and reports the *actually-used* files / URLs — not whatever the README
mentions in passing. Text scanning is a fallback only used when no code
is available.

**Upstream-trusted interface**: when ``upstream_sources.yaml`` is present
in the pipeline directory (written by future literature-research agents
that pre-tag sources with confidence), the analyser merges those entries
with highest precedence — they are treated as ground truth, not scored.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


# ── Per-repo analysis rows ──────────────────────────────────────────

class CodeDataCall(BaseModel):
    """A single IO call discovered in baseline code via grep.

    Captures the evidence behind each ``DatasetPath`` entry. Without this
    the analyser would just be claiming files exist; with it, the executor
    (and a human auditor) can see *why* this file is considered baseline
    data.
    """

    file: str = Field(description="Path relative to repo root, e.g. 'bma_adaptation.py'")
    lineno: int = Field(default=0, description="Line number of the call (0 if unknown)")
    call_text: str = Field(
        description="The matched call expression, e.g. \"pd.read_csv('data/training_rescueRobot_25600_64.csv')\""
    )
    resolved_path: str = Field(
        description="The data file path the call resolves to, relative to repo root."
    )


class DatasetPath(BaseModel):
    """A path inside a cloned repo that the analyser flags as real dataset."""

    path: str = Field(
        description="Path relative to the repo root (NOT to project root). "
        "e.g. 'data/training_rescueRobot_25600_64.csv'"
    )
    kind: Literal["training", "validation", "test", "config", "auxiliary", "unknown"] = Field(
        default="unknown",
        description="Role this file plays in the baseline experiment.",
    )
    purpose: str = Field(
        default="",
        description="One-sentence justification grounded in the entry-point code or paper.",
    )


class AuxiliaryPath(BaseModel):
    """A path the analyser explicitly flags as NOT-data (so DATA_RECON skips it).

    Useful when a directory looks dataset-like but isn't (logs, plots,
    regenerated artifacts). The reason field is what distinguishes this from
    a heuristic ban list.
    """

    path: str = Field(description="Path relative to the repo root.")
    reason: str = Field(description="Why the analyser excluded this from data copy.")


class ExternalDataURL(BaseModel):
    """An external URL the analyser claims is (or isn't) real data."""

    url: str
    should_download: bool = Field(
        description="True iff the analyser concluded this URL serves a real dataset "
        "the baseline depends on. Badge / shield / DOI-citation URLs MUST be False."
    )
    kind: Literal[
        "primary_dataset", "auxiliary_dataset", "model_weights",
        "decorative_badge", "doi_citation", "documentation", "other",
    ] = Field(default="other")
    reason: str = Field(
        default="",
        description="One-sentence rationale for the should_download decision.",
    )


class RepoAnalysis(BaseModel):
    """Per-cloned-repo analysis row inside ``BaselineAnalysis``."""

    slug: str = Field(
        description="Owner__repo slug used by code_recon (e.g. 'matteocamilli__bma-package')."
    )
    role: str = Field(
        description="One sentence: what this repo is and how it relates to the baseline. "
        "e.g. 'Complete replication package for the BMA paper.'"
    )
    entry_points: list[str] = Field(
        default_factory=list,
        description="Files the analyser inspected to discover IO calls. Path relative to repo root.",
    )
    code_data_calls: list[CodeDataCall] = Field(
        default_factory=list,
        description="Real IO calls that justify the dataset_paths entries below.",
    )
    dataset_paths: list[DatasetPath] = Field(
        default_factory=list,
        description="Files DATA_RECON should copy from this repo into data/raw/<id>/.",
    )
    auxiliary_paths: list[AuxiliaryPath] = Field(
        default_factory=list,
        description="Paths explicitly excluded from copy (logs, plots, alt-language impls).",
    )


# ── Top-level analysis document ─────────────────────────────────────

class BaselineAnalysis(BaseModel):
    """LLM analysis of the baseline package, consumed by DATA_RECON.

    Lives at ``artifacts/pipeline_<id>/baseline_analysis.yaml``.
    """

    pipeline_id: str
    baseline_paper: str = Field(
        default="",
        description="Title of the baseline paper (from paper_spec.yaml or evidence_refs).",
    )
    analysis_strategy: Literal["code_primary", "text_fallback", "upstream_trusted", "mixed"] = Field(
        description=(
            "Which strategy was applied:\n"
            " - code_primary: greped entry-point code for IO calls (default when repos cloned)\n"
            " - text_fallback: no code available, scanned paper text only\n"
            " - upstream_trusted: upstream_sources.yaml provided pre-tagged sources\n"
            " - mixed: combined two or more of the above"
        )
    )
    analyzed_repos: list[RepoAnalysis] = Field(default_factory=list)
    external_data_urls: list[ExternalDataURL] = Field(default_factory=list)
    upstream_trusted_sources_used: list[str] = Field(
        default_factory=list,
        description="URLs/paths copied verbatim from upstream_sources.yaml (high-confidence).",
    )
    fallback_used: bool = Field(
        default=False,
        description="True when no code repos were available and the analyser fell back "
        "to paper text scanning. Downstream may want to flag plans built on this.",
    )
    status: Literal["success", "partial", "no_data_found", "failed"] = Field(
        default="success",
        description=(
            "success: at least one dataset_path or downloadable URL identified. "
            "partial: some sources analysed but coverage limited. "
            "no_data_found: analyser concluded no baseline data exists / is required. "
            "failed: analyser errored — DATA_RECON should write a stub manifest and continue."
        )
    )
    notes: str = Field(default="", description="Free-form analyser commentary.")

    @field_validator("analyzed_repos")
    @classmethod
    def _no_duplicate_slugs(cls, v: list[RepoAnalysis]) -> list[RepoAnalysis]:
        seen = set()
        for r in v:
            if r.slug in seen:
                raise ValueError(f"duplicate repo slug in analyzed_repos: {r.slug}")
            seen.add(r.slug)
        return v


# ── Upstream-trusted-sources interface (forward-looking) ───────────

class UpstreamSource(BaseModel):
    """A pre-tagged source dropped by an upstream literature-research agent.

    The current pipeline does not yet have such an agent, but the interface
    is reserved so future versions can drop ``upstream_sources.yaml`` into
    a pipeline directory and have ``baseline_analyzer`` honour it.
    """

    kind: Literal["dataset_url", "code_repo", "paper", "benchmark_suite", "model_weights"]
    url: str = Field(description="URL or local path identifier.")
    confidence: Literal["trusted", "high", "medium"] = Field(
        description="How much weight the analyser should give this source. "
        "trusted = use verbatim, no verification; high = prefer over inference; "
        "medium = consider alongside other evidence."
    )
    annotated_by: str = Field(
        description="Agent or human name that produced this annotation. "
        "Carried into baseline_analysis.yaml for traceability."
    )
    annotation: str = Field(
        default="",
        description="Free-form note explaining why this source matters.",
    )


class UpstreamSources(BaseModel):
    """Top-level wrapper for ``upstream_sources.yaml``."""

    pipeline_id: str
    sources: list[UpstreamSource] = Field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────

def load_baseline_analysis(path: Path) -> BaselineAnalysis | None:
    """Load and validate ``baseline_analysis.yaml``. Returns None if missing/invalid."""
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        logger.warning("baseline_analysis: yaml parse failed for %s: %s", path, exc)
        return None
    try:
        return BaselineAnalysis.model_validate(raw)
    except Exception as exc:
        logger.warning("baseline_analysis: schema validation failed for %s: %s", path, exc)
        return None


def load_upstream_sources(path: Path) -> UpstreamSources | None:
    """Load and validate ``upstream_sources.yaml``. Returns None if missing/invalid."""
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        logger.warning("upstream_sources: yaml parse failed for %s: %s", path, exc)
        return None
    try:
        return UpstreamSources.model_validate(raw)
    except Exception as exc:
        logger.warning("upstream_sources: schema validation failed for %s: %s", path, exc)
        return None


__all__ = [
    "BaselineAnalysis",
    "RepoAnalysis",
    "DatasetPath",
    "AuxiliaryPath",
    "ExternalDataURL",
    "CodeDataCall",
    "UpstreamSources",
    "UpstreamSource",
    "load_baseline_analysis",
    "load_upstream_sources",
]
