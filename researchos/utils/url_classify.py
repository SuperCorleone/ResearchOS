"""Deterministic URL classification for baseline_analyzer pre-filter.

Used by ``scheduler._run_baseline_analyze`` to pre-classify external URLs
before LLM dispatch. The LLM no longer decides whether an SVG badge or
DOI shield is "real data" — that's a regex over hostname and path. The
LLM only sees the small ambiguous tail (kind=``unknown``).

Classification output mirrors ``BaselineAnalysis.ExternalDataURL`` enums
so the scheduler can splice these directly into the agent prompt or
auto-fill the resulting yaml when confidence is ``high``.

Confidence semantics:
  - ``high``:   committed classification, LLM should trust verbatim.
  - ``medium``: educated guess (extension hint with no host signal,
    or known host with ambiguous path); LLM may override with reason.
  - ``low``:    LLM must classify; we have no useful signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

URLKind = Literal[
    "primary_dataset", "auxiliary_dataset", "model_weights",
    "decorative_badge", "doi_citation", "documentation", "other",
]


@dataclass(frozen=True)
class URLClassification:
    kind: URLKind
    should_download: bool
    confidence: Literal["high", "medium", "low"]
    reason: str  # one-line explanation suitable for ExternalDataURL.reason


# ── Hosts that are NEVER data (badges, CI, social, decorative SVGs) ─

_BADGE_HOSTS: set[str] = {
    "img.shields.io", "shields.io",
    "badges.gitter.im", "badge.fury.io",
    "coveralls.io", "codecov.io",
    "travis-ci.org", "travis-ci.com",
    "circleci.com", "appveyor.com",
    "github.com",  # github.com path-based handling below (badges live on /badges/)
    "api.codacy.com",
    "snyk.io",
    "img.youtube.com",  # video thumbnails embedded in READMEs
    "twitter.com", "x.com",
    "linkedin.com",
    "github.io",  # docs sites
}

# Hosts we treat as DOI / citation (never download as data).
_DOI_HOSTS: set[str] = {
    "doi.org", "dx.doi.org",
    "scholar.google.com",
    "semanticscholar.org", "api.semanticscholar.org",
}

# Hosts that primarily serve documentation / human-readable web pages.
_DOC_HOSTS: set[str] = {
    "readthedocs.io", "readthedocs.org",
    "wikipedia.org", "en.wikipedia.org",
    "stackoverflow.com", "stackexchange.com",
    "medium.com",
}

# Hosts that primarily serve real datasets (download = True default).
_DATA_HOSTS: set[str] = {
    "zenodo.org",
    "figshare.com",
    "huggingface.co",
    "datasets.huggingface.co",
    "kaggle.com",
    "openml.org",
    "data.world",
    "archive.ics.uci.edu",
    "dataverse.harvard.edu",
    "physionet.org",
    "openneuro.org",
}

# Hosts that primarily serve model weights.
_MODEL_HOSTS: set[str] = {
    "huggingface.co",  # also data — disambiguate by path (/datasets/ vs /<repo>/)
    "ngc.nvidia.com",
}

# Path patterns that mark a github.com URL as a badge link.
_GITHUB_BADGE_PATH = re.compile(
    r"/(?:badges?|workflow/.*?/badge\.svg|raw/.*\.svg)/",
    re.IGNORECASE,
)

# File extensions that are obviously real data files.
_DATA_EXTENSIONS: set[str] = {
    ".csv", ".tsv", ".parquet", ".feather", ".arrow",
    ".h5", ".hdf5", ".npy", ".npz",
    ".pkl", ".pickle",
    ".mat", ".jsonl", ".ndjson",
    ".xlsx", ".xls",
    ".bin", ".dat",
    ".tar", ".tar.gz", ".tgz", ".zip", ".bz2", ".xz", ".gz",
}

# Extensions that are model weights.
_MODEL_EXTENSIONS: set[str] = {
    ".pt", ".pth", ".ckpt", ".safetensors", ".onnx",
}

# Extensions that are images (badge candidates) or docs.
_DECORATIVE_EXTENSIONS: set[str] = {
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
}

_DOC_EXTENSIONS: set[str] = {".pdf", ".html", ".htm", ".md", ".rst"}


def _path_extension(path: str) -> str:
    """Lower-cased extension including double-extensions like .tar.gz."""
    p = path.lower().rstrip("/")
    if p.endswith(".tar.gz") or p.endswith(".tar.bz2") or p.endswith(".tar.xz"):
        return ".tar.gz" if p.endswith(".tar.gz") else (".tar.bz2" if p.endswith(".tar.bz2") else ".tar.xz")
    dot = p.rfind(".")
    return p[dot:] if dot >= 0 and "/" not in p[dot:] else ""


def _host_in(host: str, host_set: set[str]) -> bool:
    """Match host against *host_set* with suffix semantics: ``foo.example.com``
    matches ``example.com``. Exact match also wins. Empty host returns False.
    """
    if not host:
        return False
    if host in host_set:
        return True
    for h in host_set:
        if host.endswith("." + h):
            return True
    return False


def classify_url(url: str) -> URLClassification:
    """Classify *url* using host + extension rules. Pure function, no I/O."""
    if not url:
        return URLClassification(
            kind="other", should_download=False, confidence="high",
            reason="empty url",
        )

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    ext = _path_extension(path)

    # ── 1. Decorative badge by extension or known badge host ─────
    if ext in _DECORATIVE_EXTENSIONS:
        return URLClassification(
            kind="decorative_badge", should_download=False, confidence="high",
            reason=f"image extension {ext} — not a dataset",
        )
    badge_only = _BADGE_HOSTS - {"github.com", "github.io"}
    if _host_in(host, badge_only):
        return URLClassification(
            kind="decorative_badge", should_download=False, confidence="high",
            reason=f"known badge host: {host}",
        )
    if host == "github.com" and _GITHUB_BADGE_PATH.search(path):
        return URLClassification(
            kind="decorative_badge", should_download=False, confidence="high",
            reason="github.com badge / workflow status path",
        )
    if host == "github.io" or host.endswith(".github.io"):
        return URLClassification(
            kind="documentation", should_download=False, confidence="high",
            reason="github.io project docs site",
        )

    # ── 2. DOI / citation ────────────────────────────────────────
    if _host_in(host, _DOI_HOSTS):
        return URLClassification(
            kind="doi_citation", should_download=False, confidence="high",
            reason=f"citation host: {host}",
        )

    # ── 3. Documentation hosts ───────────────────────────────────
    if _host_in(host, _DOC_HOSTS) or ext in _DOC_EXTENSIONS:
        return URLClassification(
            kind="documentation", should_download=False, confidence="high",
            reason=f"documentation host/extension: {host or ext}",
        )

    # ── 4. Model weights — extension wins, then huggingface paths ─
    if ext in _MODEL_EXTENSIONS:
        return URLClassification(
            kind="model_weights", should_download=True, confidence="high",
            reason=f"model-weight extension {ext}",
        )
    if host == "huggingface.co" or host.endswith(".huggingface.co"):
        if "/datasets/" in path or host == "datasets.huggingface.co":
            return URLClassification(
                kind="primary_dataset", should_download=True, confidence="high",
                reason="huggingface.co/datasets — dataset repo",
            )
        # Otherwise a model repo (no /datasets/ prefix)
        return URLClassification(
            kind="model_weights", should_download=True, confidence="high",
            reason="huggingface.co model repo",
        )
    if _host_in(host, {"ngc.nvidia.com"}):
        return URLClassification(
            kind="model_weights", should_download=True, confidence="high",
            reason="ngc.nvidia.com model registry",
        )

    # ── 5. Known data hosts ──────────────────────────────────────
    if _host_in(host, _DATA_HOSTS):
        return URLClassification(
            kind="primary_dataset", should_download=True, confidence="high",
            reason=f"known dataset host: {host}",
        )

    # ── 6. Generic data file by extension ────────────────────────
    if ext in _DATA_EXTENSIONS:
        return URLClassification(
            kind="primary_dataset", should_download=True, confidence="medium",
            reason=f"data-shape extension {ext} on unknown host",
        )

    # ── 7. Fall through — let LLM decide ─────────────────────────
    return URLClassification(
        kind="other", should_download=False, confidence="low",
        reason="no host or extension signal — LLM must classify",
    )


def classify_many(urls: list[str]) -> list[tuple[str, URLClassification]]:
    """Convenience: classify a list, preserving order."""
    return [(u, classify_url(u)) for u in urls]


__all__ = [
    "URLClassification",
    "URLKind",
    "classify_url",
    "classify_many",
]
