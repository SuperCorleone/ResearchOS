"""Pure-Python milestone renderer. Replaces the former ``reporter`` LLM agent.

The previous design ran an LLM agent (reporter) to produce ``milestone_N.md``
from ``analysis_report.md`` + ``interrogation_report.md`` + ``results.json``.
After the 2026-04 refactor the judge agent emits a single Pydantic-validated
``judge.digest.json`` (``JudgeVerdict``) and the milestone document becomes a
deterministic transform of that verdict — no LLM needed.

The renderer reads typed digests when they're on disk and falls back to the
legacy artifacts only when a digest is missing (keeps in-flight pipelines
usable during the transition). It never makes up numbers — every field comes
from a digest, ``results.json``, or the pipeline state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from researchos.schemas.digest import (
    HypothesisDigest,
    JudgeVerdict,
    PlanDigest,
    RunDigest,
)

logger = logging.getLogger(__name__)


def _maybe_load(path: Path, model_cls: type) -> Any | None:
    if not path.exists():
        return None
    try:
        return model_cls.model_validate_json(path.read_text())
    except Exception:
        logger.debug("publisher: failed to parse %s", path, exc_info=True)
        return None


def _format_metrics(metrics: dict[str, Any]) -> str:
    if not metrics:
        return "_(no metrics recorded)_"
    rows = ["| Metric | Value |", "|--------|-------|"]
    for name, value in metrics.items():
        rows.append(f"| {name} | {value} |")
    return "\n".join(rows)


def _format_per_concept(per_concept: dict[str, str]) -> str:
    if not per_concept:
        return "_(no atomic concepts — FAST_FAIL stage)_"
    rows = ["| Concept | Verdict |", "|---------|---------|"]
    for cid, verdict in sorted(per_concept.items()):
        rows.append(f"| {cid} | {verdict} |")
    return "\n".join(rows)


def _format_failure_modes(failure_modes: list[str] | None) -> str:
    if not failure_modes:
        return "_(none flagged)_"
    return "\n".join(f"- {fm}" for fm in failure_modes)


def _format_visualization_plan(plan: list[Any] | None) -> str:
    if not plan:
        return "_(no plots proposed)_"
    rows = ["| Title | Chart type | Data source |", "|-------|-----------|------------|"]
    for fig in plan:
        data = fig.model_dump() if hasattr(fig, "model_dump") else dict(fig)
        rows.append(
            f"| {data.get('title','?')} | {data.get('chart_type','?')} | "
            f"`{data.get('data_source','?')}` |"
        )
    return "\n".join(rows)


def render_milestone(
    pipeline_id: str,
    pdir: Path,
    *,
    title: str,
    research_question: str,
    milestone_index: int,
    budget_allocated: float | int | None = None,
    budget_used: float | int | None = None,
) -> Path:
    """Deterministic milestone renderer.

    Reads whatever digests exist under ``pdir`` and writes
    ``milestone_{milestone_index}.md`` verbatim — no LLM, no filler.

    Returns the absolute path of the written milestone.
    """
    pdir.mkdir(parents=True, exist_ok=True)

    hypothesis = _maybe_load(pdir / "hypothesis.digest.json", HypothesisDigest)
    plan = _maybe_load(pdir / "plan.digest.json", PlanDigest)
    run = _maybe_load(pdir / "run.digest.json", RunDigest)
    judge = _maybe_load(pdir / "judge.digest.json", JudgeVerdict)

    ts = datetime.now(timezone.utc).isoformat()

    lines: list[str] = []
    lines.append(f"# Milestone {milestone_index}: {title}")
    lines.append("")
    lines.append(f"**Pipeline**: `{pipeline_id}`  ")
    lines.append(f"**Generated**: {ts}")
    lines.append("")
    lines.append("## Research Question")
    lines.append(research_question or "_(not recorded)_")
    lines.append("")

    if hypothesis is not None:
        lines.append("## Hypothesis")
        lines.append(hypothesis.falsifiable_claim)
        lines.append("")

    if plan is not None:
        lines.append("## Plan")
        lines.append(f"- Metrics: {', '.join(plan.metric_names) or '_(none)_'}")
        lines.append(f"- Baseline strategy: `{plan.baseline_strategy}`")
        if plan.baseline_reason:
            lines.append(f"- Baseline reason: {plan.baseline_reason}")
        lines.append(f"- Data source: `{plan.data_source}`")
        if plan.data_location:
            lines.append(f"- Data location: `{plan.data_location}`")
        lines.append("")

    lines.append("## Results")
    if run is not None:
        lines.append(_format_metrics(run.metrics))
        lines.append("")
        if run.baseline_metrics:
            lines.append("### Baseline")
            lines.append(_format_metrics(run.baseline_metrics))
            lines.append("")
        lines.append(
            f"_Runtime: {run.runtime_seconds:.1f}s · "
            f"seed={run.seed} · data_rows={run.data_rows} · "
            f"data_hash=`{run.data_hash[:12]}...`_"
        )
        if run.fast_fail_reason:
            lines.append("")
            lines.append(f"> ⚠ Fast-fail: {run.fast_fail_reason}")
        lines.append("")
    else:
        lines.append("_(results.json not available)_")
        lines.append("")

    lines.append("## Judge Verdict")
    if judge is not None:
        lines.append(f"- Recommendation: **{judge.recommendation}**")
        lines.append(f"- Confidence: **{judge.confidence}**")
        lines.append(f"- Fully correct: **{judge.fully_correct}**")
        lines.append("")
        lines.append("### Summary")
        lines.append(judge.summary)
        lines.append("")
        lines.append("### Per-concept verdicts")
        lines.append(_format_per_concept(judge.per_concept))
        lines.append("")
        if judge.suggestions:
            lines.append("### Suggestions")
            for cid, text in sorted(judge.suggestions.items()):
                lines.append(f"- **{cid}**: {text}")
            lines.append("")
        lines.append("### Failure modes flagged")
        lines.append(_format_failure_modes(judge.failure_modes))
        lines.append("")
        if judge.ablation_notes:
            lines.append("### Ablation notes")
            lines.append(judge.ablation_notes)
            lines.append("")
        if judge.sensitivity_notes:
            lines.append("### Sensitivity notes")
            lines.append(judge.sensitivity_notes)
            lines.append("")
        if judge.visualization_plan:
            lines.append("### Proposed visualizations")
            lines.append(_format_visualization_plan(judge.visualization_plan))
            lines.append("")
    else:
        lines.append("_(judge.digest.json not available — pipeline did not reach JUDGE)_")
        lines.append("")

    if budget_allocated is not None or budget_used is not None:
        lines.append("## Resource usage")
        if budget_allocated is not None:
            lines.append(f"- Budget allocated: {budget_allocated}")
        if budget_used is not None:
            lines.append(f"- Budget used: {budget_used}")
        lines.append("")

    lines.append("---")
    lines.append(
        "_Rendered deterministically from typed digests by "
        "`researchos.core.publisher.render_milestone` — no LLM call._"
    )

    path = pdir / f"milestone_{milestone_index}.md"
    path.write_text("\n".join(lines) + "\n")
    return path
