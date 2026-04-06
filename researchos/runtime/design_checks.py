"""Structural design validation — pure Python, no LLM.

Runs as part of the DESIGN_REVIEW step to check experiment_plan.yaml
for completeness before the LLM reviewer assesses scientific validity.

These checks catch missing sections that would waste code/run time.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def validate_design(pipeline_dir: Path) -> dict:
    """Validate experiment_plan.yaml structural completeness.

    Returns a report dict with checks and issues.
    """
    plan_path = pipeline_dir / "experiment_plan.yaml"
    if not plan_path.exists():
        return {
            "checks": {},
            "issues": [{"severity": "ERROR", "check": "plan_exists",
                        "detail": "experiment_plan.yaml not found"}],
            "pass_count": 0,
            "fail_count": 1,
        }

    try:
        plan = yaml.safe_load(plan_path.read_text()) or {}
    except Exception as e:
        return {
            "checks": {},
            "issues": [{"severity": "ERROR", "check": "plan_parse",
                        "detail": f"Failed to parse: {e}"}],
            "pass_count": 0,
            "fail_count": 1,
        }

    checks = {}
    issues = []

    # 1. Baselines defined (≥1 trivial + ≥1 strong)
    baselines = plan.get("baselines", [])
    if not baselines:
        checks["baselines_defined"] = False
        issues.append({"severity": "WARN", "check": "baselines",
                       "detail": "No baselines section — experiment has no comparison anchor"})
    elif len(baselines) < 2:
        checks["baselines_defined"] = False
        issues.append({"severity": "WARN", "check": "baselines",
                       "detail": f"Only {len(baselines)} baseline(s) — need ≥1 trivial + ≥1 strong"})
    else:
        checks["baselines_defined"] = True

    # 2. Statistical tests specified
    stats = plan.get("evaluation", {}).get("statistical_tests", [])
    if not stats:
        checks["statistical_tests"] = False
        issues.append({"severity": "WARN", "check": "statistical_tests",
                       "detail": "No statistical_tests specified — results won't have significance tests"})
    else:
        checks["statistical_tests"] = True

    # 3. Success criteria
    criteria = plan.get("success_criteria", [])
    if not criteria:
        checks["success_criteria"] = False
        issues.append({"severity": "WARN", "check": "success_criteria",
                       "detail": "No success_criteria — how to determine if experiment succeeded?"})
    else:
        checks["success_criteria"] = True

    # 4. Failure conditions
    failure = plan.get("failure_conditions", [])
    if not failure:
        checks["failure_conditions"] = False
        issues.append({"severity": "INFO", "check": "failure_conditions",
                       "detail": "No failure_conditions defined (recommended but not required)"})
    else:
        checks["failure_conditions"] = True

    # 5. Data section
    data = plan.get("data", {})
    if not data:
        checks["data_section"] = False
        issues.append({"severity": "WARN", "check": "data_section",
                       "detail": "No data section — data source, split strategy, and provenance unknown"})
    else:
        checks["data_section"] = True
        # Sub-checks
        if not data.get("source"):
            issues.append({"severity": "WARN", "check": "data.source",
                           "detail": "data.source not specified"})
        if not data.get("split"):
            issues.append({"severity": "WARN", "check": "data.split",
                           "detail": "data.split not specified — train/test split strategy unknown"})

    # 6. Ablation experiments
    ablation = plan.get("ablation", [])
    if not ablation:
        checks["ablation_defined"] = False
        issues.append({"severity": "INFO", "check": "ablation",
                       "detail": "No ablation experiments — contribution of components won't be isolated"})
    else:
        checks["ablation_defined"] = True

    # 7. Confound controls
    confounds = plan.get("confound_controls", {})
    if not confounds:
        checks["confound_controls"] = False
        issues.append({"severity": "INFO", "check": "confound_controls",
                       "detail": "No confound_controls section"})
    else:
        checks["confound_controls"] = True
        if not confounds.get("random_seeds"):
            issues.append({"severity": "WARN", "check": "confound_controls.random_seeds",
                           "detail": "Random seed control not specified"})

    # 8. Evaluation seeds (≥3)
    seeds = plan.get("evaluation", {}).get("seeds", [])
    if len(seeds) < 3:
        checks["sufficient_seeds"] = False
        issues.append({"severity": "WARN", "check": "seeds",
                       "detail": f"Only {len(seeds)} seed(s) — need ≥3 for variance estimation"})
    else:
        checks["sufficient_seeds"] = True

    # 9. Resource budget
    budget = plan.get("resource_budget", {})
    if not budget:
        checks["resource_budget"] = False
        issues.append({"severity": "INFO", "check": "resource_budget",
                       "detail": "No resource_budget section"})
    else:
        checks["resource_budget"] = True

    # 10. Metrics defined
    metrics = plan.get("evaluation", {}).get("metrics", [])
    deps = plan.get("variables", {}).get("dependent", [])
    if not metrics and not deps:
        checks["metrics_defined"] = False
        issues.append({"severity": "WARN", "check": "metrics",
                       "detail": "No evaluation metrics or dependent variables defined"})
    else:
        checks["metrics_defined"] = True

    pass_count = sum(1 for v in checks.values() if v is True)
    fail_count = sum(1 for v in checks.values() if v is False)

    return {
        "checks": checks,
        "issues": issues,
        "pass_count": pass_count,
        "fail_count": fail_count,
    }
