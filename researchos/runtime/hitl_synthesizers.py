"""HITL (Human-In-The-Loop) gate synthesizers.

Blocks the ``pipeline_manager`` 107K-token/run fallback leak with
pure-Python template composition for the three HITL gates.

Gate 1 (pre_main_review.md) is **hybrid**: the science judgement content
is produced by the ``pre_main_reviewer`` LLM agent; this module provides
``stage_user_approve_scaffold`` (filesystem layout only) plus a debug
pure-template writer ``synthesize_pre_main_review_markdown`` for CLI use.

Gate 2 (milestone_<N>.md) and Gate 3 (pre_publish_gate.md) are pure
markers — no LLM needed, content lives elsewhere.

All synthesizers are idempotent and degrade gracefully on missing inputs
via ``[MISSING: name]`` placeholders.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_SYNTH_FOOTER = "<!-- synthesized by hitl_synthesizers (no LLM) -->\n"


# ── helpers ─────────────────────────────────────────────────────────────


def _read_text(path: Path) -> str | None:
    """Return the text content of ``path`` or ``None`` on any read error."""
    try:
        if not path.exists():
            return None
        return path.read_text(errors="replace")
    except Exception:  # pragma: no cover — filesystem-level error
        logger.debug("failed to read %s", path, exc_info=True)
        return None


def _missing(label: str) -> str:
    return f"[MISSING: {label}]"


def _load_json(path: Path) -> dict[str, Any] | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load_yaml(path: Path) -> Any:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return yaml.safe_load(text)
    except Exception:
        return None


def _metrics_table(results: dict[str, Any] | None) -> str:
    """Render results.json ``metrics`` as a Markdown table."""
    if not results:
        return "| (no results.json available) | - | - | - |\n"
    metrics = results.get("metrics") or {}
    if not isinstance(metrics, dict) or not metrics:
        return "| (results.json has no `metrics` block) | - | - | - |\n"
    rows: list[str] = []
    for key, value in metrics.items():
        if isinstance(value, (int, float)):
            rows.append(f"| {key} | - | {value} | - |")
        else:
            rows.append(f"| {key} | - | {value!s} | - |")
    return "\n".join(rows) + "\n"


def _first_paragraph(text: str, *, max_chars: int = 600) -> str:
    """Return the first non-empty paragraph (first ``##``-delimited block)."""
    if not text:
        return ""
    lines = text.splitlines()
    # Skip to first non-heading, non-empty line.
    buf: list[str] = []
    started = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if started:
                break
            continue
        if stripped.startswith("#"):
            if started:
                break
            continue
        started = True
        buf.append(line)
        if sum(len(b) for b in buf) >= max_chars:
            break
    return "\n".join(buf).strip()


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Gate 1 — pre-MAIN ───────────────────────────────────────────────────


def synthesize_pre_main_review_markdown(
    pipeline_dir: Path,
    *,
    title: str | None = None,
    pipeline_id: str | None = None,
    evidence_refs: list[str] | None = None,
) -> Path:
    """Write ``pre_main_review.md`` from FAST_FAIL artifacts (template only).

    Debug / CLI-only. Production runs the ``pre_main_reviewer`` LLM agent
    so the Gate-1 science judgement (evidence-gap, GO/TWEAK/KILL) is from
    a model. Missing inputs degrade to ``[MISSING: name]`` placeholders.
    Does not touch ``user_approve/`` — use :func:`stage_user_approve_scaffold`.
    """
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    ts = _ts()
    pid = pipeline_id or pipeline_dir.name.replace("pipeline_", "")
    disp_title = title or pid

    critic_text = _read_text(pipeline_dir / "analysis_report.md")
    results = _load_json(pipeline_dir / "results.json")
    milestone = _latest_milestone(pipeline_dir)
    contract = _load_json(pipeline_dir / "metric_contract.json")

    refs_block = ""
    if evidence_refs:
        refs_block = "Reference papers (consistency check pending user confirmation):\n"
        refs_block += "\n".join(f"- {r}" for r in evidence_refs) + "\n\n"

    baseline_block = "See hypothesis.md / experiment_plan.yaml.\n"
    if contract:
        status = contract.get("status", "")
        if status:
            baseline_block = f"metric_contract.json status: **{status}**\n"
            if "baseline_values" in contract:
                baseline_block += (
                    f"\nBaseline values: `{contract['baseline_values']}`\n"
                )

    critic_summary = _first_paragraph(critic_text or "", max_chars=500) or _missing(
        "analysis_report.md"
    )
    milestone_summary = ""
    if milestone:
        milestone_summary = _first_paragraph(milestone, max_chars=400)

    body = (
        f"# Pre-MAIN Review: {disp_title}\n\n"
        f"**Pipeline**: `{pid}`  **Synthesized**: {ts}\n\n"
        "## 1. Executive Summary\n"
        f"FAST_FAIL completed for pipeline `{pid}`. Preliminary results "
        "and critic verdict are available below.\n"
        + (f"\n> {milestone_summary}\n" if milestone_summary else "")
        + "\n"
        "## 2. Data Source\n"
        f"{refs_block}"
        "See `experiment_plan.yaml` for data configuration.\n\n"
        "## 3. Baseline & Method\n"
        f"{baseline_block}\n"
        "## 4. Preliminary Results\n"
        "| Metric | Baseline | Proposed | Δ |\n"
        "|--------|----------|----------|---|\n"
        f"{_metrics_table(results)}\n"
        "## 5. Professor Assessment\n"
        "Critic first-paragraph excerpt (see `analysis_report.md` for full text):\n\n"
        f"{critic_summary}\n\n"
        "## 6. MAIN Stage Plan\n"
        "Full-scale experiments with additional seeds, ablations, and "
        "comprehensive evaluation. Budget scales with milestone count.\n\n"
        "## 7. Key Decisions for User\n"
        "- [ ] Approve data source and splits?\n"
        "- [ ] Approve baseline method?\n"
        "- [ ] Approve evaluation metrics?\n"
        "- [ ] Any methodology changes requested?\n\n"
        "## Recommendation\n"
        "Review the artifacts listed below and choose: "
        "`approve` / `tweak <directive>` / `kill`.\n\n"
        "### Source Artifacts\n"
        f"- `analysis_report.md` — {'present' if critic_text else _missing('analysis_report.md')}\n"
        f"- `results.json` — {'present' if results else _missing('results.json')}\n"
        f"- `metric_contract.json` — {'present' if contract else 'SKIP'}\n"
        f"- latest `milestone_*.md` — {'present' if milestone else _missing('milestone_*.md')}\n\n"
        f"_Generated at {ts} (pure-Python synthesis)._\n\n"
        f"{_SYNTH_FOOTER}"
    )

    out_path = pipeline_dir / "pre_main_review.md"
    out_path.write_text(body)
    return out_path


def stage_user_approve_scaffold(
    pipeline_dir: Path,
    *,
    title: str | None = None,
    pipeline_id: str | None = None,
    research_question: str | None = None,
    evidence_refs: list[str] | None = None,
    project_root: Path | None = None,
    metadata: dict[str, Any] | None = None,
    initial_hypothesis: str | None = None,
    budget_allocated: int | None = None,
) -> Path:
    """Stage ``user_approve/`` (papers/, proposal.md, feedback.md, README.md).

    Pure Python, zero LLM. Does not write ``pre_main_review.md`` — the
    ``pre_main_reviewer`` LLM agent owns that file. Reads it if present
    to assemble ``fast_fail_report.md`` / ``professor_suggestion.md``;
    degrades to ``[MISSING: ...]`` placeholders otherwise.
    """
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    pid = pipeline_id or pipeline_dir.name.replace("pipeline_", "")
    disp_title = title or pid
    pre_main_path = pipeline_dir / "pre_main_review.md"
    return _stage_user_approve_pkg(
        pipeline_dir=pipeline_dir,
        pipeline_id=pid,
        title=disp_title,
        research_question=research_question or "",
        evidence_refs=evidence_refs or [],
        project_root=project_root,
        budget_allocated=budget_allocated,
        initial_hypothesis=initial_hypothesis,
        metadata=metadata or {},
        pre_main_path=pre_main_path,
    )


def synthesize_pre_main_gate(
    pipeline_dir: Path,
    *,
    title: str | None = None,
    pipeline_id: str | None = None,
    research_question: str | None = None,
    evidence_refs: list[str] | None = None,
    project_root: Path | None = None,
    stage_user_approve: bool = False,
    metadata: dict[str, Any] | None = None,
    initial_hypothesis: str | None = None,
    budget_allocated: int | None = None,
) -> Path:
    """Back-compat wrapper (CLI/debug). Scheduler uses the LLM path instead."""
    out_path = synthesize_pre_main_review_markdown(
        pipeline_dir, title=title, pipeline_id=pipeline_id, evidence_refs=evidence_refs
    )
    if stage_user_approve:
        stage_user_approve_scaffold(
            pipeline_dir, title=title, pipeline_id=pipeline_id,
            research_question=research_question, evidence_refs=evidence_refs,
            project_root=project_root, metadata=metadata,
            initial_hypothesis=initial_hypothesis, budget_allocated=budget_allocated,
        )
    return out_path


def _latest_milestone(pipeline_dir: Path) -> str | None:
    candidates = sorted(pipeline_dir.glob("milestone_*.md"))
    if not candidates:
        return None
    return _read_text(candidates[-1])


def _stage_user_approve_pkg(
    *,
    pipeline_dir: Path,
    pipeline_id: str,
    title: str,
    research_question: str,
    evidence_refs: list[str],
    project_root: Path | None,
    budget_allocated: int | None,
    initial_hypothesis: str | None,
    metadata: dict[str, Any],
    pre_main_path: Path,
) -> Path:
    """Materialise ``user_approve/`` folder (papers, proposal, report, feedback)."""
    ua_dir = pipeline_dir / "user_approve"
    papers_dir = ua_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    # 1. Stage reference papers (hardlink → symlink → copy fallback).
    copied_papers: list[str] = []
    if project_root:
        raw_dir = project_root / "knowledge" / "raw"
        for ref in evidence_refs:
            src_candidates = [Path(ref), raw_dir / Path(ref).name]
            src = next((c for c in src_candidates if c.exists()), None)
            if src is None:
                continue
            dest = papers_dir / src.name
            if dest.exists() or dest.is_symlink():
                copied_papers.append(src.name)
                continue
            staged = False
            for staging in (_try_link, _try_symlink, _try_copy):
                if staging(src, dest):
                    staged = True
                    break
            if staged:
                copied_papers.append(src.name)

    # 2. proposal.md
    (ua_dir / "proposal.md").write_text(
        f"# Proposal: {title}\n\n"
        f"- Pipeline ID: {pipeline_id}\n"
        f"- Source: {metadata.get('source', 'unknown')}\n"
        f"- Budget allocated: {budget_allocated if budget_allocated is not None else 'n/a'}\n\n"
        f"## Research Question\n{research_question}\n\n"
        f"## Initial Hypothesis\n{initial_hypothesis or '(none provided)'}\n\n"
        "## Reference Papers\n"
        + ("\n".join(f"- {name}" for name in copied_papers) or "(none)")
        + f"\n\n{_SYNTH_FOOTER}"
    )

    # 3. fast_fail_report.md — critic + pre_main_review concatenation
    sections: list[str] = [f"# FAST_FAIL Report — {pipeline_id}\n"]
    analysis = _read_text(pipeline_dir / "analysis_report.md")
    if analysis:
        sections.append("## Critic Analysis\n")
        sections.append(analysis)
    pre_main_text = _read_text(pre_main_path)
    if pre_main_text:
        sections.append("\n## Pre-MAIN Review\n")
        sections.append(pre_main_text)
    (ua_dir / "fast_fail_report.md").write_text("\n".join(sections) + _SYNTH_FOOTER)

    # 4. professor_suggestion.md — extract the recommendation block.
    suggestion = ""
    if pre_main_text:
        for heading in ("## 5. Professor Assessment", "## Recommendation"):
            idx = pre_main_text.find(heading)
            if idx >= 0:
                suggestion = pre_main_text[idx:]
                break
        if not suggestion:
            suggestion = pre_main_text
    (ua_dir / "professor_suggestion.md").write_text(
        f"# Professor Suggestion — {pipeline_id}\n\n"
        + (suggestion or "(pre_main_review.md not yet generated)")
        + f"\n\n{_SYNTH_FOOTER}"
    )

    # 5. feedback.md — template only created once.
    feedback_path = ua_dir / "feedback.md"
    if not feedback_path.exists():
        feedback_path.write_text(
            "<!-- user_approve/feedback.md — edit this file, then run: -->\n"
            f"<!--   python scripts/run_research.py continue --pipeline {pipeline_id} -->\n"
            "<!-- Delete these comment lines and replace with your directives. -->\n\n"
            "# User Feedback\n\n## What to change\n\n- \n\n"
            "## What to keep\n\n- \n\n## Additional notes\n\n"
        )

    # 6. README.md
    (ua_dir / "README.md").write_text(
        f"# user_approve/ — pipeline {pipeline_id}\n\n"
        "FAST_FAIL complete. Review files, write opinion in `feedback.md`, "
        "then run continue.\n\n## Files\n"
        "- `papers/` — reference PDFs from knowledge/raw/\n"
        "- `proposal.md` — research question + initial hypothesis\n"
        "- `fast_fail_report.md` — critic analysis + pre-MAIN review\n"
        "- `professor_suggestion.md` — recommendation excerpt\n"
        "- `feedback.md` — **edit this** with your opinions\n\n"
        f"## Continue\n```\npython scripts/run_research.py continue --pipeline {pipeline_id}\n```\n"
        f"{_SYNTH_FOOTER}"
    )
    return ua_dir


def _try_link(src: Path, dest: Path) -> bool:
    try:
        os.link(src, dest)
        return True
    except (OSError, NotImplementedError):
        return False


def _try_symlink(src: Path, dest: Path) -> bool:
    try:
        os.symlink(src.resolve(), dest)
        return True
    except (OSError, NotImplementedError):
        return False


def _try_copy(src: Path, dest: Path) -> bool:
    try:
        shutil.copy2(src, dest)
        return True
    except Exception:
        return False


# ── Gate 2 — milestone-N ────────────────────────────────────────────────


def synthesize_milestone_gate(
    pipeline_dir: Path,
    milestone_n: int,
    *,
    title: str | None = None,
    pipeline_id: str | None = None,
) -> Path:
    """Write ``milestone_<N>.md`` aggregating critic + interrogation + results.

    This is the post-MAIN report that the existing REPORT step produces via
    the `reporter` LLM agent. The synthesizer is a fallback / HITL-gate
    variant so the scheduler does not burn tokens on the WAITING_APPROVAL
    step when the REPORT file already exists.
    """
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    ts = _ts()
    pid = pipeline_id or pipeline_dir.name.replace("pipeline_", "")
    disp_title = title or pid

    critic_text = _read_text(pipeline_dir / "analysis_report.md")
    interrogation_text = _read_text(pipeline_dir / "interrogation_report.md")
    results = _load_json(pipeline_dir / "results.json")

    critic_excerpt = _first_paragraph(critic_text or "", max_chars=600) or _missing(
        "analysis_report.md"
    )
    interrogation_excerpt = _first_paragraph(
        interrogation_text or "", max_chars=600
    ) or _missing("interrogation_report.md")

    body = (
        f"# Milestone Report M{milestone_n}: {disp_title}\n\n"
        f"**Pipeline**: `{pid}`  **Milestone**: M{milestone_n}  "
        f"**Synthesized**: {ts}\n\n"
        "## 1. Executive Summary\n"
        f"Milestone {milestone_n} complete. Summary of critic verdict, "
        "adversarial interrogation, and measured metrics below.\n\n"
        "## 2. Measured Metrics\n"
        "| Metric | Baseline | Observed | Δ |\n"
        "|--------|----------|----------|---|\n"
        f"{_metrics_table(results)}\n"
        "## 3. Critic Verdict (excerpt)\n"
        f"{critic_excerpt}\n\n"
        "## 4. Adversarial Interrogation (excerpt)\n"
        f"{interrogation_excerpt}\n\n"
        "## 5. Source Artifacts\n"
        f"- `analysis_report.md` — {'present' if critic_text else _missing('analysis_report.md')}\n"
        f"- `interrogation_report.md` — {'present' if interrogation_text else _missing('interrogation_report.md')}\n"
        f"- `results.json` — {'present' if results else _missing('results.json')}\n\n"
        "## 6. Recommendation for Professor\n"
        "See the per-artifact recommendations above. Suggested action: "
        "`approve` to continue to the next milestone, `pivot` to redirect, "
        "or `kill` if the signal is exhausted.\n\n"
        f"_Generated at {ts} (pure-Python synthesis)._\n\n"
        f"{_SYNTH_FOOTER}"
    )

    out_path = pipeline_dir / f"milestone_{milestone_n}.md"
    out_path.write_text(body)
    return out_path


# ── Gate 3 — pre-publish ────────────────────────────────────────────────


def synthesize_pre_publish_gate(
    pipeline_dir: Path,
    *,
    pipeline_id: str | None = None,
    project_root: Path | None = None,
) -> Path:
    """Write ``pre_publish_gate.md`` (Gate 3) pointing at draft+audit+review."""
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    ts = _ts()
    pid = pipeline_id or pipeline_dir.name.replace("pipeline_", "")

    paper_dir: Path | None = None
    draft_rel = "paper/<id>/draft_v1.md"
    audit_rel = "paper/<id>/audit_report.md"
    review_rel = "paper/<id>/review_v1.md"
    draft_present = audit_present = review_present = False
    if project_root:
        paper_dir = project_root / "paper" / pid
        draft = paper_dir / "draft_v1.md"
        audit = paper_dir / "audit_report.md"
        review = paper_dir / "review_v1.md"
        draft_present = draft.exists()
        audit_present = audit.exists()
        review_present = review.exists()
        draft_rel = str(draft.relative_to(project_root))
        audit_rel = str(audit.relative_to(project_root))
        review_rel = str(review.relative_to(project_root))

    body_parts: list[str] = [
        f"# Pre-Publish Gate — {pid}\n",
        f"**Synthesized**: {ts}\n",
        "",
        "Awaiting final user approval before LaTeX compilation and PDF output.\n",
        "",
        "## Paper artifacts\n",
        f"- Draft: `{draft_rel}` — "
        f"{'present' if draft_present else _missing('draft_v1.md')}\n",
        f"- Audit: `{audit_rel}` — "
        f"{'present' if audit_present else _missing('audit_report.md')}\n",
        f"- Review: `{review_rel}` — "
        f"{'present' if review_present else _missing('review_v1.md')}\n",
        "",
        "## Next steps\n",
        f"- Review the files under `paper/{pid}/`.\n",
        f"- Run `python scripts/run_research.py continue --pipeline {pid} "
        "--gate pre_publish` to unlock the final compile.\n",
        "- Or write `pre_publish_approved` into "
        "`artifacts/pipeline_<id>/user_approve/feedback.md` and re-run the "
        "scheduler.\n",
        "",
        f"_Generated at {ts} (pure-Python synthesis)._\n",
        "",
        _SYNTH_FOOTER,
    ]
    out_path = pipeline_dir / "pre_publish_gate.md"
    out_path.write_text("\n".join(body_parts))
    return out_path


__all__ = [
    "synthesize_pre_main_gate",  # back-compat wrapper (CLI/debug)
    "synthesize_pre_main_review_markdown",  # debug/CLI pure-template review
    "stage_user_approve_scaffold",  # pure-Python user_approve/ stager
    "synthesize_milestone_gate",
    "synthesize_pre_publish_gate",
]
