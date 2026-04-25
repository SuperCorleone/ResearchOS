"""Deterministic data-shape detection for cloned baseline repos.

Used by BASELINE_ANALYZE *before* the LLM dispatch to decide ``analysis_strategy``
without leaving it to LLM discretion. The LLM gets a concrete file list and
its job becomes "describe what's there", not "decide if there's data".

Two confidence tiers:
  - ``high``: file lives under a conventional data directory segment
    (``data/``, ``datasets/``, ``fixtures/``, ``simulation*/``,
    ``examples/``, ``benchmarks/``).
  - ``medium``: file extension is in the tabular/array data set
    (``.csv``, ``.parquet``, ``.npy``, etc.) — extension alone, anywhere
    in the tree.

A repo with ANY high- or medium-tier hit forces ``code_primary``. A repo
with zero hits leaves the strategy free to fall through to
``text_fallback`` (or ``external_primary`` if URLs surface elsewhere).

Exclusions (these segments override hits inside them):
  ``.git``, ``.github``, ``__pycache__``, ``node_modules``, ``venv``,
  ``.venv``, ``dist``, ``build``, ``docs``, ``doc``, ``figures``,
  ``figs``, ``plots``, ``test``, ``tests``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# File extensions that *strongly* suggest tabular / array data. Config-y
# extensions (.json / .yaml / .yml / .xml) are intentionally NOT in this
# set — they only count as data when they sit under a high-confidence
# directory segment.
_DATA_EXTENSIONS: set[str] = {
    ".csv", ".tsv",
    ".parquet", ".feather", ".arrow",
    ".h5", ".hdf5",
    ".npy", ".npz",
    ".pkl", ".pickle",
    ".mat",
    ".pt", ".pth", ".ckpt", ".safetensors",
    ".gz", ".bz2", ".xz", ".zip",  # compressed archives often hold data
    ".bin", ".dat",
    ".xlsx", ".xls",
    ".jsonl", ".ndjson",
}

# Path segments (case-insensitive, exact match on a single path component)
# that mark a directory as conventional data home.
_DATA_DIR_SEGMENTS: set[str] = {
    "data", "datasets", "dataset",
    "fixtures", "fixture",
    "simulations", "simulation", "simulation_data", "sim_data",
    "examples", "example", "samples", "sample",
    "benchmarks", "benchmark", "bench",
    "inputs", "input",
    "raw", "raw_data",
}

_EXCLUDED_SEGMENTS: set[str] = {
    ".git", ".github", "__pycache__",
    "node_modules", "venv", ".venv",
    "dist", "build",
    "docs", "doc",
    "figures", "figs", "plots", "img", "images",
    "test", "tests",
}

# Files under high-confidence dirs are filtered to ignore obvious
# non-data clutter even when the directory says "data".
_HIGH_TIER_NOISE_NAMES: set[str] = {
    "readme", "license", "changelog", ".gitignore", ".gitkeep",
    "__init__.py",
}


@dataclass(frozen=True)
class DataShapeHit:
    """One file flagged as candidate baseline data."""

    path: str  # relative to repo root, posix-style
    tier: str  # "high" | "medium"
    reason: str  # one-line evidence for an auditor / LLM prompt


def _segments_excluded(parts: Iterable[str]) -> bool:
    return any(p.lower() in _EXCLUDED_SEGMENTS for p in parts)


def _is_high_tier_dir(parts: Iterable[str]) -> bool:
    for p in parts:
        if p.lower() in _DATA_DIR_SEGMENTS:
            return True
    return False


def scan_repo_for_data(
    repo_root: Path,
    *,
    max_files: int = 200,
    min_file_size_bytes: int = 64,
) -> list[DataShapeHit]:
    """Walk *repo_root* and return data-shape candidates, capped at *max_files*.

    Tiny files (< ``min_file_size_bytes``) are ignored to avoid catching
    placeholder/.gitkeep stubs and 0-byte initialisers.
    """
    if not repo_root.exists() or not repo_root.is_dir():
        return []

    hits: list[DataShapeHit] = []
    for fp in repo_root.rglob("*"):
        if not fp.is_file():
            continue
        try:
            rel = fp.relative_to(repo_root)
        except ValueError:
            continue
        parts = rel.parts[:-1]  # directory segments only
        if _segments_excluded(parts):
            continue
        try:
            size = fp.stat().st_size
        except OSError:
            continue
        if size < min_file_size_bytes:
            continue

        rel_posix = rel.as_posix()
        ext = fp.suffix.lower()
        name_low = fp.name.lower()
        in_data_dir = _is_high_tier_dir(parts)

        if in_data_dir and name_low not in _HIGH_TIER_NOISE_NAMES:
            hits.append(
                DataShapeHit(
                    path=rel_posix,
                    tier="high",
                    reason=f"under conventional data dir ({'/'.join(parts) or '<root>'})",
                )
            )
        elif ext in _DATA_EXTENSIONS:
            hits.append(
                DataShapeHit(
                    path=rel_posix,
                    tier="medium",
                    reason=f"data-shape extension {ext}",
                )
            )

        if len(hits) >= max_files:
            break

    # High-tier first, then medium; deterministic order by path otherwise.
    hits.sort(key=lambda h: (0 if h.tier == "high" else 1, h.path))
    return hits


def decide_strategy(repos: list[dict], hits_by_slug: dict[str, list[DataShapeHit]]) -> str:
    """Decide ``analysis_strategy`` from clone status + scan results.

    Pure function — no I/O. Inputs:
      * ``repos`` is the ``code_recon.yaml::repos`` list (each dict has
        ``slug`` and ``status``).
      * ``hits_by_slug`` maps each ok/cached repo's slug to its
        ``scan_repo_for_data`` output.

    Returns one of: ``code_primary``, ``external_primary``, ``text_fallback``.
    ``external_primary`` is currently never returned here — surfacing
    external URLs is left to the LLM, but the function leaves the door
    open for a future deterministic URL extractor.
    """
    ok_repos = [r for r in repos if r.get("status") in ("ok", "cached")]
    if not ok_repos:
        return "text_fallback"
    for r in ok_repos:
        if hits_by_slug.get(r.get("slug", ""), []):
            return "code_primary"
    return "text_fallback"
