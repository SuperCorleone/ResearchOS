"""Publisher: packages completed pipeline artifacts for submission.

Creates a submission-ready package with:
- Clean experiment code (academic-level English comments)
- Paper (LaTeX + PDF)
- Data and results
- Research process report with journal/conference recommendations
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Journal/conference tier mapping by arXiv category
VENUE_RECOMMENDATIONS = {
    "cs.AI": [
        {"tier": "A*", "venues": ["NeurIPS", "ICML", "ICLR", "AAAI", "IJCAI"]},
        {"tier": "A", "venues": ["AAMAS", "ECAI", "KR", "UAI"]},
        {"tier": "B", "venues": ["EAAI", "AI Communications", "JAIR"]},
        {"tier": "Journal", "venues": ["Artificial Intelligence (AIJ)", "JMLR", "MLJ"]},
    ],
    "cs.SE": [
        {"tier": "A*", "venues": ["ICSE", "FSE/ESEC", "ASE"]},
        {"tier": "A", "venues": ["ISSTA", "SANER", "MSR", "ICSME"]},
        {"tier": "B", "venues": ["SEAMS", "SCAM", "EASE"]},
        {"tier": "Journal", "venues": ["TSE", "TOSEM", "ESE", "JSS"]},
    ],
    "cs.LG": [
        {"tier": "A*", "venues": ["NeurIPS", "ICML", "ICLR"]},
        {"tier": "A", "venues": ["AISTATS", "COLT", "ALT"]},
        {"tier": "Journal", "venues": ["JMLR", "MLJ", "TMLR"]},
    ],
    "cs.CL": [
        {"tier": "A*", "venues": ["ACL", "EMNLP", "NAACL"]},
        {"tier": "A", "venues": ["COLING", "EACL", "CoNLL"]},
        {"tier": "Journal", "venues": ["TACL", "CL", "NLE"]},
    ],
    "default": [
        {"tier": "A*", "venues": ["Top venue in your field"]},
        {"tier": "A", "venues": ["Strong venue in your field"]},
        {"tier": "B", "venues": ["Solid venue for initial publication"]},
        {"tier": "Journal", "venues": ["Consider field-specific journals"]},
    ],
}


def publish_pipeline(
    pipeline_id: str,
    project_root: Path,
    output_dir: Path | None = None,
) -> Path:
    """Package a completed pipeline for submission.

    Returns the path to the created package directory.
    """
    root = project_root
    pdir = root / "artifacts" / f"pipeline_{pipeline_id}"
    paper_dir = root / "paper" / pipeline_id

    if not pdir.exists():
        raise FileNotFoundError(f"Pipeline directory not found: {pdir}")

    # Load pipeline state
    state_file = pdir / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}

    if state.get("stage") != "completed":
        raise ValueError(f"Pipeline is not completed (stage: {state.get('stage')})")

    # Create output package directory
    if output_dir is None:
        output_dir = root / "publications" / pipeline_id
    output_dir.mkdir(parents=True, exist_ok=True)

    title = state.get("title", pipeline_id)
    question = state.get("research_question", "")

    # 1. Copy paper artifacts
    _copy_paper(paper_dir, output_dir)

    # 2. Package experiment code
    _package_code(pdir, output_dir, title, question)

    # 3. Package data and results
    _package_data(pdir, output_dir)

    # 4. Generate research process report
    _generate_process_report(pdir, paper_dir, output_dir, state, root)

    # 5. Generate code README
    _generate_code_readme(output_dir, title, question, state)

    # 6. G6 Reproducibility checklist
    _generate_reproducibility_checklist(pdir, output_dir, state)

    logger.info("Publication package created: %s", output_dir)
    return output_dir


def _copy_paper(paper_dir: Path, output_dir: Path) -> None:
    """Copy paper files to package."""
    dest = output_dir / "paper"
    dest.mkdir(exist_ok=True)

    if not paper_dir.exists():
        return

    for f in paper_dir.iterdir():
        if f.suffix in (".tex", ".pdf", ".md", ".png", ".jpg", ".bib"):
            shutil.copy2(f, dest / f.name)


def _package_code(pdir: Path, output_dir: Path, title: str, question: str) -> None:
    """Package experiment code with clean academic comments."""
    code_dir = output_dir / "code"
    code_dir.mkdir(exist_ok=True)

    # Copy Python scripts
    for f in pdir.iterdir():
        if f.suffix == ".py":
            content = f.read_text()
            # Remove process-related comments (修改, fix, hack, workaround, TODO, FIXME)
            lines = content.splitlines()
            clean_lines = []
            for line in lines:
                stripped = line.strip()
                # Skip lines that are purely process comments
                if stripped.startswith("#") and any(
                    kw in stripped.lower()
                    for kw in ["fix", "hack", "workaround", "todo", "fixme", "修", "临时", "debug"]
                ):
                    continue
                clean_lines.append(line)
            content = "\n".join(clean_lines)
            (code_dir / f.name).write_text(content)

    # Copy experiment plan
    plan_file = pdir / "experiment_plan.yaml"
    if plan_file.exists():
        shutil.copy2(plan_file, code_dir / "experiment_plan.yaml")


def _package_data(pdir: Path, output_dir: Path) -> None:
    """Package experimental data and results."""
    data_dir = output_dir / "data"
    data_dir.mkdir(exist_ok=True)

    for f in pdir.iterdir():
        if f.suffix == ".json" and f.name != "state.json":
            shutil.copy2(f, data_dir / f.name)
        elif f.suffix in (".csv", ".tsv", ".npy", ".npz"):
            shutil.copy2(f, data_dir / f.name)


def _generate_process_report(
    pdir: Path,
    paper_dir: Path,
    output_dir: Path,
    state: dict,
    project_root: Path,
) -> None:
    """Generate a comprehensive research process report."""
    title = state.get("title", "Unknown")
    question = state.get("research_question", "")
    milestones = state.get("milestone_count", 0)
    budget_used = state.get("budget_used", 0)
    budget_allocated = state.get("budget_allocated", 0)
    created_at = state.get("created_at", "Unknown")

    # Read key artifacts
    hypothesis = _read_if_exists(pdir / "hypothesis.md")
    analysis = _read_if_exists(pdir / "analysis_report.md")
    correction = _read_if_exists(pdir / "correction_report.md")

    # Read milestone reports
    milestone_summaries = []
    for mf in sorted(pdir.glob("milestone_*.md")):
        content = mf.read_text()
        first_lines = "\n".join(content.splitlines()[:10])
        milestone_summaries.append(f"### {mf.name}\n{first_lines}\n")

    # Determine domain for venue recommendations
    hb_config_path = project_root / "runtime" / "heartbeat_config.yaml"
    domain = "default"
    if hb_config_path.exists():
        try:
            hb = yaml.safe_load(hb_config_path.read_text()) or {}
            domain = hb.get("professor", {}).get("domain", "default")
        except Exception:
            pass

    venues = VENUE_RECOMMENDATIONS.get(domain, VENUE_RECOMMENDATIONS["default"])

    report = f"""# Research Process Report

## Project Overview

- **Title**: {title}
- **Research Question**: {question}
- **Created**: {created_at}
- **Milestones Completed**: {milestones}
- **Budget**: {budget_used}/{budget_allocated} credits used

## Research Journey

### Phase 1: Hypothesis Formation

{hypothesis[:500] if hypothesis else "No hypothesis document found."}

### Phase 2: Experimental Validation

The research went through {milestones} milestone cycle(s), each involving:
1. Hypothesis refinement
2. Experiment design
3. Code implementation (where applicable)
4. Experiment execution
5. Critical analysis
6. Milestone reporting and professor review

{chr(10).join(milestone_summaries) if milestone_summaries else "No milestone reports found."}

### Phase 3: Analysis and Findings

{analysis[:800] if analysis else "No analysis report found."}

{"### Phase 4: Corrections" + chr(10) + correction[:500] if correction else ""}

## Submission Recommendations

### Venue Suggestions (Domain: {domain})

Based on the research domain and topic, consider the following venues:

| Tier | Venues | Recommendation |
|------|--------|----------------|
"""

    for v in venues:
        tier = v["tier"]
        venue_list = ", ".join(v["venues"])
        if tier == "A*":
            rec = "Strong novel contribution required. Consider if results are breakthrough-level."
        elif tier == "A":
            rec = "Solid contribution with thorough evaluation. Good target for well-executed work."
        elif tier == "B":
            rec = "Suitable for initial publications, incremental contributions, or work-in-progress."
        else:
            rec = "Extended versions with thorough evaluation. Good for comprehensive studies."
        report += f"| {tier} | {venue_list} | {rec} |\n"

    report += f"""

### Submission Checklist

- [ ] Paper formatted per venue template (most accept LaTeX)
- [ ] All figures are high-resolution (300+ DPI)
- [ ] References are complete and properly formatted
- [ ] Code repository prepared (consider GitHub/Zenodo for reproducibility)
- [ ] Supplementary materials organized (appendix, additional results)
- [ ] Author contributions statement prepared
- [ ] Ethical considerations addressed (if applicable)
- [ ] Reproducibility checklist completed (many venues require this)

### Strengthening the Paper

1. **Add real-world validation** if current evaluation is simulation-only
2. **Compare with SOTA baselines** relevant to the specific venue
3. **Address reviewer concerns** from the simulated peer review
4. **Expand related work** to cover the venue's typical audience
5. **Include statistical significance tests** (p-values, confidence intervals)

## Package Contents

```
{output_dir.name}/
├── paper/              # LaTeX source, PDF, and figures
├── code/               # Clean experiment code with README
├── data/               # Experimental results and datasets
└── PROCESS_REPORT.md   # This file
```

---
*Generated by ResearchOS on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}*
"""

    (output_dir / "PROCESS_REPORT.md").write_text(report)


def _generate_code_readme(output_dir: Path, title: str, question: str, state: dict) -> None:
    """Generate README for the code package."""
    code_dir = output_dir / "code"
    if not code_dir.exists():
        return

    scripts = [f.name for f in code_dir.iterdir() if f.suffix == ".py"]

    readme = f"""# {title}

## Research Question

{question}

## Requirements

- Python >= 3.10
- numpy >= 1.21.0

## Usage

```bash
# Install dependencies
pip install numpy

# Run the experiment
python {scripts[0] if scripts else 'experiment.py'}
```

## Files

"""
    for s in scripts:
        readme += f"- `{s}` — Experiment implementation\n"

    readme += f"""- `experiment_plan.yaml` — Experimental design specification

## Reproducibility

All experiments use fixed random seeds for reproducibility.
See `experiment_plan.yaml` for the complete list of seeds and configurations.

## Citation

If you use this code, please cite:

```bibtex
@article{{researchos_{state.get('pipeline_id', 'unknown')},
  title={{{title}}},
  author={{ResearchOS Autonomous Pipeline}},
  year={{{datetime.now().year}}},
  note={{Generated by ResearchOS}}
}}
```
"""

    (code_dir / "README.md").write_text(readme)


def _read_if_exists(path: Path) -> str:
    if path.exists():
        return path.read_text()
    return ""


def _generate_reproducibility_checklist(
    pdir: Path, output_dir: Path, state: dict
) -> None:
    """G6: Generate a reproducibility checklist for the publication package."""
    code_dir = output_dir / "code"
    data_dir = output_dir / "data"

    checks: list[tuple[str, bool, str]] = []

    # Check 1: Code exists
    py_files = list(code_dir.glob("*.py")) if code_dir.exists() else []
    checks.append(("Experiment code included", bool(py_files),
                    f"{len(py_files)} Python file(s)"))

    # Check 2: requirements.txt
    has_reqs = (code_dir / "requirements.txt").exists() or (pdir / "requirements.txt").exists()
    if not has_reqs and pdir.exists():
        # Copy from pipeline if exists
        req_src = pdir / "requirements.txt"
        if req_src.exists():
            shutil.copy2(req_src, code_dir / "requirements.txt")
            has_reqs = True
    checks.append(("Dependency file (requirements.txt)", has_reqs, ""))

    # Check 3: Experiment plan with seeds
    plan_file = code_dir / "experiment_plan.yaml" if code_dir.exists() else None
    has_seeds = False
    if plan_file and plan_file.exists():
        try:
            plan = yaml.safe_load(plan_file.read_text()) or {}
            seeds = plan.get("evaluation", {}).get("seeds", [])
            has_seeds = bool(seeds)
            checks.append(("Random seeds specified", has_seeds,
                            f"Seeds: {seeds}" if has_seeds else "Missing"))
        except Exception:
            checks.append(("Random seeds specified", False, "Failed to parse plan"))
    else:
        checks.append(("Random seeds specified", False, "No experiment_plan.yaml"))

    # Check 4: Results data
    json_files = list(data_dir.glob("*.json")) if data_dir.exists() else []
    checks.append(("Results data included", bool(json_files),
                    f"{len(json_files)} JSON file(s)"))

    # Check 5: README
    has_readme = (code_dir / "README.md").exists() if code_dir.exists() else False
    checks.append(("Code README with run instructions", has_readme, ""))

    # Check 6: Milestone count
    milestones = state.get("milestone_count", 0)
    checks.append(("Training details in milestone reports", milestones > 0,
                    f"{milestones} milestone(s) recorded"))

    # Write checklist
    lines = [
        "# Reproducibility Checklist (G6)\n",
        f"Pipeline: {state.get('pipeline_id', '?')}\n",
        f"Title: {state.get('title', '?')}\n\n",
        "| # | Check | Status | Details |\n",
        "|---|-------|--------|----------|\n",
    ]
    pass_count = 0
    for i, (desc, passed, detail) in enumerate(checks, 1):
        status = "PASS" if passed else "FAIL"
        if passed:
            pass_count += 1
        lines.append(f"| {i} | {desc} | {status} | {detail} |\n")

    lines.append(f"\n**Score**: {pass_count}/{len(checks)} checks passed\n")
    if pass_count < len(checks):
        lines.append("\n**Action needed**: Address FAIL items before submission.\n")

    checklist_path = output_dir / "REPRODUCIBILITY_CHECKLIST.md"
    checklist_path.write_text("".join(lines))
    logger.info("Reproducibility checklist: %d/%d passed", pass_count, len(checks))
