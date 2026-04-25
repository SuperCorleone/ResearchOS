"""Cross-digest invariant enforcement.

Each ``assert_*`` function validates an invariant that spans multiple
StepDigest instances (the intra-digest invariants live as Pydantic
model_validators). When an invariant is violated, the function raises
``ContractViolation`` — state_manager.save_pipeline() catches this and
transitions the pipeline to BLOCK, never WARN.

These are the guards that would have caught the 3 concrete failures
observed in pipeline_mitigating_model_uncertainty_i:

  1. results.json.data_rows = 39420 vs experiment_plan.sample_count = 462
     → assert_data_manifest_match
  2. results.json.metrics keys = {accuracy_rq1, accuracy_rq2}
     vs plan.metric_names = [accuracy]
     → assert_metric_alignment
  3. baseline = {accuracy: 0.50} placeholder with no source
     → assert_baseline_honesty

The 4th invariant (assert_concept_atomicity) is technically enforced by
ConceptsDigest's model_validator, but we keep a top-level wrapper here
so contract_guard is the single entry point the scheduler imports.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from researchos.schemas.baseline_analysis import BaselineAnalysis
from researchos.schemas.digest import (
    ConceptsDigest,
    HypothesisDigest,
    PlanDigest,
    RunDigest,
)


class ContractViolation(Exception):
    """Raised when a cross-digest invariant fails.

    Attributes:
        guard_name: Which assertion failed (for logging / retry policy).
        details: Machine-readable context for diagnostics.
    """

    def __init__(self, guard_name: str, message: str, details: dict | None = None):
        super().__init__(f"[{guard_name}] {message}")
        self.guard_name = guard_name
        self.details = details or {}


# ---------------------------------------------------------------------------
# 1. assert_concept_atomicity
# ---------------------------------------------------------------------------

def assert_concept_atomicity(concepts: ConceptsDigest) -> None:
    """Top-level wrapper that delegates to ConceptsDigest's own validators.

    Kept as a public entry point so the scheduler has one consistent API.
    The validators already raise ValueError — we re-wrap as ContractViolation.
    """
    # Re-validate by round-tripping through the model. If the validator
    # fails, Pydantic raises ValidationError.
    try:
        ConceptsDigest.model_validate(concepts.model_dump())
    except Exception as e:
        raise ContractViolation(
            "concept_atomicity",
            str(e),
            details={"code_side_coverage": concepts.code_side_coverage,
                     "n_concepts": len(concepts.concepts)},
        ) from e


# ---------------------------------------------------------------------------
# 2. assert_metric_alignment
# ---------------------------------------------------------------------------

def assert_metric_alignment(
    hypothesis: HypothesisDigest,
    plan: PlanDigest,
    run: RunDigest,
) -> None:
    """Enforce strict metric name identity across the 3 digests.

    - plan.metric_names MUST ⊇ hypothesis.metric_names
    - run.metrics.keys() MUST ⊇ plan.metric_names
    - Names are exact (no prefix/suffix inference): 'accuracy' ≠ 'accuracy_rq1'

    This is the guard that would have caught
    ``accuracy_rq1`` / ``accuracy_rq2`` in results.json when the plan
    only promised ``accuracy``.
    """

    hyp_set = set(hypothesis.metric_names)
    plan_set = set(plan.metric_names)
    run_set = set(run.metrics.keys())

    missing_in_plan = hyp_set - plan_set
    if missing_in_plan:
        raise ContractViolation(
            "metric_alignment",
            f"Hypothesis-level metrics {sorted(missing_in_plan)} not "
            f"present in plan.metric_names. Plan must carry every "
            f"hypothesis metric name verbatim.",
            details={
                "hypothesis_metrics": sorted(hyp_set),
                "plan_metrics": sorted(plan_set),
                "missing": sorted(missing_in_plan),
            },
        )

    missing_in_run = plan_set - run_set
    if missing_in_run:
        raise ContractViolation(
            "metric_alignment",
            f"Plan metrics {sorted(missing_in_run)} not present in "
            f"results.json. The coder must emit the exact key names. "
            f"(Silent renames like 'accuracy'→'accuracy_rq1' are "
            f"rejected — downstream cannot disambiguate.)",
            details={
                "plan_metrics": sorted(plan_set),
                "run_metrics": sorted(run_set),
                "missing": sorted(missing_in_run),
            },
        )


# ---------------------------------------------------------------------------
# 3. assert_data_manifest_match
# ---------------------------------------------------------------------------

def assert_data_manifest_match(
    manifest_path: Path,
    run: RunDigest,
) -> None:
    """Enforce that the data described in results.json matches the on-disk
    manifest.

    This is the guard that would have caught ``data_rows: 39420`` being
    written to results.json when the actual file ``training_rescueRobot_450.csv``
    had 462 rows.

    Parameters
    ----------
    manifest_path
        Path to ``data_manifest.yaml`` produced by data_manifest.generate_manifest.
    run
        Typed run digest.

    Missing manifest is a SKIP (not BLOCK) — some FAST_FAIL pipelines
    don't produce one. Mismatch IS a BLOCK.
    """

    if not manifest_path.exists():
        return                           # intentionally lenient — SKIP

    try:
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
    except Exception as e:
        raise ContractViolation(
            "data_manifest_match",
            f"Could not parse {manifest_path.name}: {e}",
        ) from e

    # Hash check
    declared_hash = manifest.get("directory_hash")
    if declared_hash and run.data_hash and declared_hash != run.data_hash:
        raise ContractViolation(
            "data_manifest_match",
            f"data_hash mismatch: manifest={declared_hash!r} vs "
            f"run.data_hash={run.data_hash!r}. Data referenced in the "
            f"experiment is not the data in the manifest — rerun with "
            f"consistent provenance.",
            details={"manifest": declared_hash, "run": run.data_hash},
        )

    # Row count check (only if run reported it)
    if run.data_rows is not None:
        declared_rows = manifest.get("row_count") or manifest.get("total_rows")
        if declared_rows is not None and declared_rows != run.data_rows:
            raise ContractViolation(
                "data_manifest_match",
                f"data_rows mismatch: manifest={declared_rows} vs "
                f"run={run.data_rows}. Likely cause: coder read a wrong "
                f"file or concatenated multiple sources. Verify "
                f"data_location against the real dataset.",
                details={"manifest_rows": declared_rows,
                         "run_rows": run.data_rows},
            )


# ---------------------------------------------------------------------------
# 4. assert_baseline_honesty
# ---------------------------------------------------------------------------

def assert_baseline_honesty(
    plan: PlanDigest,
    run: RunDigest,
) -> None:
    """Forbid the 'placeholder baseline' anti-pattern.

    Concrete failure mode this catches:
      plan.yaml baseline: {accuracy: 0.50, source: 'placeholder floor'}
      → then downstream reports delta = proposed - 0.50 as if 0.50 was real

    Rules:
      - baseline_strategy == 'run_inline' ⇒ run.baseline_metrics is non-empty
      - baseline_strategy == 'paper_values_with_source' ⇒ caller must supply
        the paper values out-of-band; we only check consistency loosely
      - baseline_strategy == 'waived_with_reason' ⇒ plan.baseline_reason
        already enforced by PlanDigest validator; run.baseline_metrics
        must be None (downstream must not manufacture a delta)
    """

    if plan.baseline_strategy == "run_inline":
        if not run.baseline_metrics:
            raise ContractViolation(
                "baseline_honesty",
                "baseline_strategy='run_inline' but run.baseline_metrics "
                "is empty. Either the coder forgot to run the baseline, "
                "or the strategy should be 'waived_with_reason'. Do not "
                "let downstream compute deltas against nothing.",
                details={"strategy": plan.baseline_strategy},
            )

    elif plan.baseline_strategy == "waived_with_reason":
        if run.baseline_metrics:
            raise ContractViolation(
                "baseline_honesty",
                "baseline_strategy='waived_with_reason' but "
                "run.baseline_metrics is non-empty. The waiver said no "
                "baseline would run; the presence of numbers contradicts "
                "that. Pick one posture.",
                details={"reason": plan.baseline_reason,
                         "strategy": plan.baseline_strategy},
            )


# ---------------------------------------------------------------------------
# 5. assert_strategy_actions_consistent
# ---------------------------------------------------------------------------

def assert_strategy_actions_consistent(
    baseline: BaselineAnalysis,
    data_manifest_path: Path | None = None,
) -> None:
    """Forbid the 'strategy chosen but no work done' anti-pattern.

    Concrete failure mode this catches (BMA pipeline, 2026-04-25):
      baseline_analysis.yaml: analysis_strategy=text_fallback
      data_manifest.yaml:     executed_actions=[], total_files=0
      → strategy claims a path was taken but no evidence on disk

    Rules:
      - analysis_strategy == 'code_primary' ⇒ at least one
        analyzed_repos[*].dataset_paths entry; status MUST NOT be
        'no_data_found'
      - analysis_strategy in {'code_primary','external_primary','mixed'} ⇒
        if data_manifest exists, executed_actions must be non-empty
      - analysis_strategy == 'text_fallback' ⇒ fallback_used=True AND
        notes is non-empty (one-line justification)
    """

    if baseline.analysis_strategy == "code_primary":
        has_paths = any(r.dataset_paths for r in baseline.analyzed_repos)
        if not has_paths:
            raise ContractViolation(
                "strategy_actions_consistent",
                "analysis_strategy='code_primary' but no "
                "analyzed_repos[*].dataset_paths populated. The strategy "
                "promises code-grounded data extraction; an empty result "
                "set means the LLM picked the wrong strategy or skipped "
                "the entry-point grep step.",
                details={"strategy": baseline.analysis_strategy},
            )
        if baseline.status == "no_data_found":
            raise ContractViolation(
                "strategy_actions_consistent",
                "analysis_strategy='code_primary' is incompatible with "
                "status='no_data_found'. If the repo truly has no data, "
                "the strategy should be 'text_fallback'.",
                details={"status": baseline.status},
            )

    if baseline.analysis_strategy == "text_fallback":
        if not baseline.fallback_used:
            raise ContractViolation(
                "strategy_actions_consistent",
                "analysis_strategy='text_fallback' requires "
                "fallback_used=true. Either set the flag or pick a "
                "different strategy.",
                details={"strategy": baseline.analysis_strategy},
            )
        if not baseline.notes.strip():
            raise ContractViolation(
                "strategy_actions_consistent",
                "analysis_strategy='text_fallback' requires a non-empty "
                "notes field justifying why no code path was viable.",
                details={"strategy": baseline.analysis_strategy},
            )

    if (
        baseline.analysis_strategy in {"code_primary", "external_primary", "mixed"}
        and data_manifest_path is not None
        and data_manifest_path.exists()
    ):
        try:
            manifest = yaml.safe_load(data_manifest_path.read_text()) or {}
        except Exception as exc:
            raise ContractViolation(
                "strategy_actions_consistent",
                f"data_manifest.yaml unreadable: {exc}",
                details={"path": str(data_manifest_path)},
            ) from exc
        actions = manifest.get("executed_actions", [])
        if not actions:
            raise ContractViolation(
                "strategy_actions_consistent",
                f"analysis_strategy='{baseline.analysis_strategy}' but "
                f"data_manifest.executed_actions is empty. Strategy claims "
                f"data was fetched/copied; the manifest disagrees. Either "
                f"DATA_RECON silently failed or the strategy is wrong.",
                details={
                    "strategy": baseline.analysis_strategy,
                    "manifest": str(data_manifest_path),
                },
            )


# ---------------------------------------------------------------------------
# Orchestration helper
# ---------------------------------------------------------------------------

def enforce_all(
    *,
    hypothesis: HypothesisDigest | None = None,
    plan: PlanDigest | None = None,
    concepts: ConceptsDigest | None = None,
    run: RunDigest | None = None,
    baseline_analysis: BaselineAnalysis | None = None,
    data_manifest_path: Path | None = None,
) -> None:
    """Run every applicable guard given whatever digests are currently
    available. Cheap when digests are missing (early pipeline stages).

    Called from state_manager.save_pipeline() after every artifact write.
    Fails fast on the first violation — caller converts to pipeline BLOCK.
    """

    if concepts is not None:
        assert_concept_atomicity(concepts)

    if hypothesis is not None and plan is not None and run is not None:
        assert_metric_alignment(hypothesis, plan, run)

    if plan is not None and run is not None:
        assert_baseline_honesty(plan, run)

    if run is not None and data_manifest_path is not None:
        assert_data_manifest_match(data_manifest_path, run)

    if baseline_analysis is not None:
        assert_strategy_actions_consistent(baseline_analysis, data_manifest_path)
