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


def _check_data_consistency(plan: dict, issues: list[dict]) -> bool:
    """Check that data consistency requirement is properly specified when referencing papers."""
    data = plan.get("data", {})
    cons_req = data.get("consistency_requirement")

    # If no consistency requirement, check if there are reference papers that suggest one is needed
    ref_code = plan.get("reference_code", {})
    ref_papers = ref_code.get("papers", [])

    if ref_papers and not cons_req:
        # Reference papers exist but no consistency requirement — warn
        issues.append({
            "severity": "WARN",
            "check": "data_consistency",
            "detail": (
                "experiment_plan references papers in reference_code.papers but "
                "data.consistency_requirement is missing. If improving upon these papers, "
                "you should specify which data aspects must match."
            ),
        })
        return False

    if cons_req:
        # Validate consistency requirement fields
        ok = True
        if not cons_req.get("reference_paper"):
            issues.append({
                "severity": "WARN",
                "check": "data_consistency",
                "detail": "data.consistency_requirement.reference_paper is empty",
            })
            ok = False
        must_match = cons_req.get("must_match", [])
        if not must_match:
            issues.append({
                "severity": "WARN",
                "check": "data_consistency",
                "detail": "data.consistency_requirement.must_match is empty — specify what must match (dataset_version, train_test_split, preprocessing_pipeline)",
            })
            ok = False
        if not cons_req.get("verification"):
            issues.append({
                "severity": "WARN",
                "check": "data_consistency",
                "detail": "data.consistency_requirement.verification is empty — specify how to verify data matches reference",
            })
            ok = False
        return ok

    return True  # No reference papers, no consistency needed


def _check_baseline_faithfulness(plan: dict, issues: list[dict]) -> bool:
    """Check that baseline methodology matches reference paper when specified."""
    ref_methodology = plan.get("reference_methodology", {})
    ref_code = plan.get("reference_code", {})

    if not ref_methodology and not ref_code:
        return True  # No reference paper, skip

    ok = True
    baselines = plan.get("baselines", [])

    # Check 1: If reference methodology exists, baselines should reflect it
    if ref_methodology:
        ref_metrics = ref_methodology.get("evaluation_metrics", [])
        plan_metrics = [m.get("name", "") for m in plan.get("evaluation", {}).get("metrics", [])]

        # Check that reference paper's evaluation metrics are included
        missing_metrics = [m for m in ref_metrics if m.lower() not in [p.lower() for p in plan_metrics]]
        if missing_metrics:
            issues.append({
                "severity": "WARN",
                "check": "baseline_faithfulness",
                "detail": (
                    f"Reference paper uses metrics {ref_metrics} but experiment plan "
                    f"is missing: {missing_metrics}. Include reference metrics for fair comparison."
                ),
            })
            ok = False

        # Check that reference paper's CV strategy matches
        ref_cv = ref_methodology.get("cv_strategy", "")
        plan_split = plan.get("data", {}).get("split", {}).get("strategy", "")
        if ref_cv and plan_split and ref_cv.lower() != plan_split.lower():
            issues.append({
                "severity": "WARN",
                "check": "baseline_faithfulness",
                "detail": (
                    f"Reference paper uses '{ref_cv}' but experiment plan uses '{plan_split}'. "
                    f"Use the same CV strategy for fair comparison unless justified."
                ),
            })
            ok = False

    # Check 2: If reference provides data files, data.source should not be "synthetic"
    ref_data_files = ref_methodology.get("data_files", []) if ref_methodology else []
    data_source = plan.get("data", {}).get("source", "")
    if ref_data_files and data_source == "synthetic":
        issues.append({
            "severity": "ERROR",
            "check": "baseline_faithfulness",
            "detail": (
                f"Reference paper provides data files {ref_data_files} but experiment plan "
                f"uses data.source='synthetic'. Use the reference paper's actual data."
            ),
        })
        ok = False

    return ok


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

    # 10. Data consistency
    checks["data_consistency"] = _check_data_consistency(plan, issues)

    checks["baseline_faithfulness"] = _check_baseline_faithfulness(plan, issues)

    # 11. Metrics defined
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
