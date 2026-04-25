"""Baseline divergence diagnostic — pure Python, no LLM.

This module is a deliberately simple two-check diagnose tool that hands off
complex root-cause analysis to humans at L2/L3.

It compares two aspects of a completed pipeline run against the expectations
recorded in ``baseline_analysis.yaml``:

1. **Data hashes** — did the experiment code read the exact same data files
   that the baseline analyser identified (SHA-256 match)?
2. **Hyperparameters** — did the run use the same hyperparameter values that
   the paper reported?

If either check fails and the cause is clear enough to write a machine-readable
directive, the verdict is ``auto_fixable``.  When the check failed but the
root cause is ambiguous (e.g. missing hashes, no hyperparameters in paper), the
verdict escalates to ``escalate_to_l2`` so a human can investigate.

The function is deterministic and has no side-effects other than writing
``baseline_diagnose.json`` to the pipeline directory.  No subprocess calls,
no network access, no LLM calls.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from researchos.schemas.baseline_diagnose import (
    BaselineDiagnoseResult,
    DataHashCheck,
    DiagnoseChecks,
    HashDiff,
    HyperparamDiff,
    HyperparameterCheck,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _compare_data_hashes(pipeline_dir: Path) -> dict:
    """Compare data_manifest.yaml SHA256 against baseline_analysis.expected_hashes.

    Returns:
      {
        "passed": bool,
        "diff": [{"file": str, "expected": str, "actual": str|None}],
        "auto_fixable": bool,    # True if we can write a directive like 'rehash data'
      }

    If baseline_analysis.yaml has no expected_hashes section, mark
    passed=True (we have no ground truth to disagree with) and
    auto_fixable=False.
    """
    result: dict = {"passed": True, "diff": [], "auto_fixable": False}

    # Load baseline_analysis.yaml
    analysis_path = pipeline_dir / "baseline_analysis.yaml"
    if not analysis_path.exists():
        return result
    try:
        analysis = yaml.safe_load(analysis_path.read_text()) or {}
    except Exception:
        return result

    # Extract expected hashes — field is data.expected_hashes: {path: sha256}
    data_section = analysis.get("data") or {}
    expected_hashes: dict[str, str] = data_section.get("expected_hashes") or {}
    if not expected_hashes:
        # No ground truth — cannot disagree
        return result

    # Load data_manifest.yaml
    manifest_path = pipeline_dir / "data_manifest.yaml"
    actual_hashes: dict[str, str] = {}
    if manifest_path.exists():
        try:
            manifest = yaml.safe_load(manifest_path.read_text()) or {}
            for entry in manifest.get("files") or []:
                if isinstance(entry, dict):
                    path = entry.get("path") or entry.get("name")
                    sha = entry.get("sha256") or entry.get("hash")
                    if path and sha:
                        actual_hashes[str(path)] = str(sha)
        except Exception:
            pass

    diffs: list[HashDiff] = []
    for fpath, expected_sha in expected_hashes.items():
        actual_sha = actual_hashes.get(fpath)
        if actual_sha != expected_sha:
            diffs.append(HashDiff(
                file=fpath,
                expected=expected_sha,
                actual=actual_sha,
            ))

    if diffs:
        # If we can name exactly which files differ, the fix is clear: re-download
        # or re-hash.  Mark auto_fixable so the fixer can generate a directive.
        return DataHashCheck(passed=False, diff=diffs, auto_fixable=True).model_dump()
    return DataHashCheck(passed=True, diff=[], auto_fixable=False).model_dump()


def _compare_hyperparameters(pipeline_dir: Path) -> dict:
    """Compare results.json::hyperparameters (or config.hyperparameters)
    against baseline_analysis.algorithm.hyperparameters_in_paper.

    Plain key-by-key diff. Numeric tolerance: equal if abs(a-b) < 1e-9
    when both are numeric, otherwise string equality.

    Returns:
      {
        "passed": bool,
        "diff": [{"param": str, "expected": Any, "actual": Any}],
      }

    If either source lacks hyperparameters, passed=True (cannot disagree).
    """
    result: dict = {"passed": True, "diff": []}

    # Load baseline_analysis.yaml
    analysis_path = pipeline_dir / "baseline_analysis.yaml"
    if not analysis_path.exists():
        return result
    try:
        analysis = yaml.safe_load(analysis_path.read_text()) or {}
    except Exception:
        return result

    algorithm = analysis.get("algorithm") or {}
    paper_params: dict[str, Any] = algorithm.get("hyperparameters_in_paper") or {}
    if not paper_params:
        return result

    # Load results.json
    results_path = pipeline_dir / "results.json"
    if not results_path.exists():
        return result
    try:
        results = json.loads(results_path.read_text())
    except Exception:
        return result

    # Try results["hyperparameters"] first, then results["config"]["hyperparameters"]
    actual_params: dict[str, Any] = {}
    if isinstance(results.get("hyperparameters"), dict):
        actual_params = results["hyperparameters"]
    elif isinstance(results.get("config"), dict):
        cfg = results["config"]
        if isinstance(cfg.get("hyperparameters"), dict):
            actual_params = cfg["hyperparameters"]

    if not actual_params:
        return result

    diffs: list[HyperparamDiff] = []
    for param, expected_val in paper_params.items():
        actual_val = actual_params.get(param)
        if actual_val is None:
            diffs.append(HyperparamDiff(param=param, expected=expected_val, actual=None))
            continue
        # Numeric tolerance
        try:
            if abs(float(actual_val) - float(expected_val)) < 1e-9:
                continue
        except (TypeError, ValueError):
            pass
        # String equality fallback
        if str(actual_val) == str(expected_val):
            continue
        diffs.append(HyperparamDiff(param=param, expected=expected_val, actual=actual_val))

    if diffs:
        return HyperparameterCheck(passed=False, diff=diffs).model_dump()
    return HyperparameterCheck(passed=True, diff=[]).model_dump()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diagnose(pipeline_dir: Path) -> dict:
    """Run two simple checks against a pipeline directory and return verdict.

    Reads:
      - pipeline_dir / "baseline_analysis.yaml"
      - pipeline_dir / "data_manifest.yaml"
      - pipeline_dir / "results.json"

    Writes:
      - pipeline_dir / "baseline_diagnose.json"

    Returns the dict that gets written.

    Verdict logic:
      - If both checks PASSED: verdict='auto_fixable', fixer_directives=[]
        (this case shouldn't happen if critic flagged a divergence; record
        it as 'unexpected_pass' in fixer_directives so critic can revisit)
      - If at least one check failed AND we can pinpoint a fix: verdict='auto_fixable',
        fixer_directives=concrete instructions
      - Otherwise: verdict='escalate_to_l2', fixer_directives=[]

    The function must be deterministic, side-effect-free except for the
    output file write. No subprocess, no network, no LLM calls.
    """
    out_path = pipeline_dir / "baseline_diagnose.json"

    # Guard: missing or malformed baseline_analysis.yaml
    analysis_path = pipeline_dir / "baseline_analysis.yaml"
    if not analysis_path.exists():
        stub = BaselineDiagnoseResult(
            verdict="escalate_to_l2",
            reason="baseline_analysis.yaml unavailable",
        ).model_dump(exclude_none=True)
        out_path.write_text(json.dumps(stub, indent=2))
        return stub
    try:
        yaml.safe_load(analysis_path.read_text())
    except Exception as exc:
        stub = BaselineDiagnoseResult(
            verdict="escalate_to_l2",
            reason=f"baseline_analysis.yaml malformed: {exc}",
        ).model_dump(exclude_none=True)
        out_path.write_text(json.dumps(stub, indent=2))
        return stub

    hash_check = _compare_data_hashes(pipeline_dir)
    hp_check = _compare_hyperparameters(pipeline_dir)

    both_passed = hash_check["passed"] and hp_check["passed"]
    any_failed = not hash_check["passed"] or not hp_check["passed"]

    fixer_directives: list[str] = []
    verdict: str

    if both_passed:
        # Unexpected: critic said there's a divergence but our checks see nothing wrong.
        verdict = "auto_fixable"
        fixer_directives = ["unexpected_pass: both checks passed; critic should revisit"]
    elif any_failed:
        # Build concrete directives where possible
        if not hash_check["passed"] and hash_check.get("auto_fixable"):
            bad_files = [d["file"] for d in hash_check["diff"]]
            fixer_directives.append(
                f"rehash_data: re-download or re-copy files with hash mismatch: {bad_files}"
            )
        if not hp_check["passed"]:
            bad_params = [d["param"] for d in hp_check["diff"]]
            fixer_directives.append(
                f"fix_hyperparameters: align params to paper values: {bad_params}"
            )

        if fixer_directives:
            verdict = "auto_fixable"
        else:
            verdict = "escalate_to_l2"
    else:
        verdict = "escalate_to_l2"

    # Build via Pydantic so the contract with consumers is enforced at the
    # producer site. Tests + scheduler still see a plain dict via model_dump().
    # Note: full form uses default model_dump() (not exclude_none) because
    # HashDiff.actual=None is a meaningful "file missing from manifest" signal
    # that must survive the round-trip. Stub form (no checks) uses exclude
    # to keep optional top-level fields out.
    result = BaselineDiagnoseResult(
        verdict=verdict,  # type: ignore[arg-type]
        fixer_directives=fixer_directives,
        checks=DiagnoseChecks(
            data_hashes=DataHashCheck(**hash_check),
            hyperparameters=HyperparameterCheck(**hp_check),
        ),
    )
    report = result.model_dump(exclude={"reason", "note"})

    out_path.write_text(json.dumps(report, indent=2))
    logger.info(
        "baseline_diagnose: pipeline_dir=%s verdict=%s",
        pipeline_dir,
        verdict,
    )
    return report


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Module CLI: ``python -m researchos.runtime.baseline_diagnose <pipeline_dir>``.

    Exit code 0 always (the runtime should not exit-on-failure for a diagnostic).
    """
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Run baseline divergence diagnostics on a pipeline directory."
    )
    parser.add_argument("pipeline_dir", type=Path, help="Path to the pipeline artifact directory")
    args = parser.parse_args(argv)

    pipeline_dir: Path = args.pipeline_dir.resolve()
    if not pipeline_dir.exists():
        logger.error("pipeline_dir does not exist: %s", pipeline_dir)
        return 0  # Still exit 0 — diagnostic, not a hard failure

    result = diagnose(pipeline_dir)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
