"""Briefer — deterministic research-brief renderer.

Replaces the LLM-based ``research_briefer`` agent with a pure Python
renderer. Reads the ``evaluated_<id>.yaml`` produced by the
``topic_evaluator`` and joins it with 2–3 knowledge-base snippets to
produce a compact (<=500 words) markdown brief.

No LLM calls; the output is fully deterministic for a given input.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import yaml

logger = logging.getLogger(__name__)


MAX_WORDS = 500
MAX_SNIPPETS = 3
MIN_SNIPPETS_NOTE = 2


# ── Helpers ─────────────────────────────────────────────────────


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Candidate evaluation not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping at top level of {path}, got {type(data).__name__}")
    return data


def _get(d: dict, key: str, default: Any = None) -> Any:
    v = d.get(key)
    return default if v in (None, "") else v


def _fmt_scores(scores: dict[str, Any]) -> tuple[str, int]:
    """Return (markdown table, total)."""
    if not isinstance(scores, dict):
        return "_(no scores available)_", 0
    dims = ["significance", "novelty", "differentiation", "feasibility", "falsifiability"]
    rows = ["| Dimension | Score (0-3) |", "|---|---|"]
    total = 0
    for d in dims:
        v = scores.get(d, 0) or 0
        try:
            v_int = int(v)
        except (TypeError, ValueError):
            v_int = 0
        total += v_int
        rows.append(f"| {d} | {v_int} |")
    rows.append(f"| **total** | **{total} / 15** |")
    return "\n".join(rows), total


def _fmt_innovation(inn: dict[str, Any]) -> str:
    if not isinstance(inn, dict):
        return "_(none)_"
    gap = _get(inn, "problem_gap", "")
    delta = _get(inn, "our_delta", "")
    matters = _get(inn, "why_it_matters", "")
    parts: list[str] = []
    if gap:
        parts.append(f"- **Problem gap**: {gap}")
    if delta:
        parts.append(f"- **Our delta**: {delta}")
    if matters:
        parts.append(f"- **Why it matters**: {matters}")
    return "\n".join(parts) if parts else "_(none)_"


def _fmt_falsification(fals: dict[str, Any]) -> str:
    if not isinstance(fals, dict):
        return "_(none)_"
    fields = [
        ("Hypothesis", "hypothesis"),
        ("Baseline", "baseline"),
        ("Metric", "metric"),
        ("Failure condition", "failure_condition"),
        ("Expected signal", "expected_signal"),
    ]
    parts: list[str] = []
    for label, key in fields:
        v = _get(fals, key, "")
        if v:
            parts.append(f"- **{label}**: {v}")
    ratio = fals.get("min_budget_ratio")
    if ratio is not None:
        try:
            parts.append(f"- **Min budget ratio**: {float(ratio):.2f}")
        except (TypeError, ValueError):
            pass
    return "\n".join(parts) if parts else "_(none)_"


def _clean_snippets(snippets: Iterable[str]) -> list[str]:
    out: list[str] = []
    for s in snippets or []:
        if not s:
            continue
        if not isinstance(s, str):
            s = str(s)
        s = s.strip()
        if s:
            out.append(s)
        if len(out) >= MAX_SNIPPETS:
            break
    return out


def _truncate_to_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    truncated = " ".join(words[:max_words])
    return truncated.rstrip(",.;:") + " …"


# ── Public API ──────────────────────────────────────────────────


def render_brief(candidate_yaml_path: Path, kb_snippets: list[str]) -> str:
    """Render a deterministic markdown brief from an evaluated candidate.

    Args:
        candidate_yaml_path: Path to ``evaluated_<id>.yaml`` (output of the
            topic_evaluator).
        kb_snippets: 2-3 KB snippets (plain text) to weave into context.
            Extra snippets beyond :data:`MAX_SNIPPETS` are dropped.

    Returns:
        A markdown brief bounded to :data:`MAX_WORDS` words. Never calls
        an LLM.
    """
    candidate_yaml_path = Path(candidate_yaml_path)
    data = _load_yaml(candidate_yaml_path)

    title = _get(data, "title", "(untitled)")
    pid = _get(data, "proposal_id", candidate_yaml_path.stem)
    question = _get(data, "research_question", "")
    direction = _get(data, "direction", "")
    gate_result = _get(data, "gate_result", "unknown")
    gate_reasons = data.get("gate_reasons") or []
    repeat_degree = _get(data, "repeat_degree", "unknown")

    scores_md, total = _fmt_scores(data.get("scores") or {})
    innovation_md = _fmt_innovation(data.get("innovation") or {})
    falsification_md = _fmt_falsification(data.get("falsification") or {})

    snippets = _clean_snippets(kb_snippets)

    # Build the brief
    lines: list[str] = []
    lines.append(f"# Research Brief: {title}")
    lines.append("")
    lines.append(f"- **Proposal ID**: `{pid}`")
    if direction:
        lines.append(f"- **Direction**: {direction}")
    lines.append(f"- **Gate**: `{gate_result}` (total={total}/15, repeat_degree=`{repeat_degree}`)")
    if gate_reasons:
        lines.append(f"- **Gate reasons**: {'; '.join(str(r) for r in gate_reasons)}")
    if question:
        lines.append("")
        lines.append("## Research Question")
        lines.append(question)

    lines.append("")
    lines.append("## Scores")
    lines.append(scores_md)

    lines.append("")
    lines.append("## Innovation Claim")
    lines.append(innovation_md)

    lines.append("")
    lines.append("## Falsification Design")
    lines.append(falsification_md)

    lines.append("")
    lines.append("## Knowledge Context")
    if snippets:
        if len(snippets) < MIN_SNIPPETS_NOTE:
            lines.append(f"_(only {len(snippets)} KB snippet available; minimum recommended is {MIN_SNIPPETS_NOTE})_")
        for i, snip in enumerate(snippets, 1):
            # Compact each snippet to ~80 words so the overall brief stays short
            short = _truncate_to_words(snip, 80)
            lines.append(f"{i}. {short}")
    else:
        lines.append("_(no KB snippets supplied)_")

    brief = "\n".join(lines).strip() + "\n"
    return _truncate_to_words(brief, MAX_WORDS)
