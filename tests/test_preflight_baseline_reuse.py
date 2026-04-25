"""Tests for preflight._check_baseline_reuse.

Case routing:
  skipped  — baseline_analysis.yaml absent
  C        — confidence < threshold (0.7) OR package not acquired
  A        — confidence >= 0.7, acquired, same language (no cross_language_porting in plan)
           → passes when at least one file under experiment*/baseline/ or baseline/
             contains the literal bytes 'COPIED FROM:'
  B        — confidence >= 0.7, acquired, cross-language (cross_language_porting in plan)
           → passes when REWRITE_RATIONALE.md exists AND a non-empty
             baseline_original/ directory exists

The implementation scans:
  pipeline_dir.glob("experiment*/baseline")
  pipeline_dir.glob("experiment/baseline")
  pipeline_dir / "baseline"

so ``tmp_path / "experiment" / "baseline"`` satisfies the second pattern.
"""

import yaml
from pathlib import Path
import pytest

from researchos.runtime.preflight import _check_baseline_reuse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(p: Path, data):
    """Write a YAML file, creating parent dirs."""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckBaselineReuse:

    # ── skipped case ────────────────────────────────────────────────────────

    def test_skipped_when_baseline_analysis_missing(self, tmp_path):
        """No baseline_analysis.yaml → skipped, passed=True."""
        result = _check_baseline_reuse(tmp_path)
        assert result["passed"]
        assert result["case"] in ("skipped", "C")

    def test_skipped_reason_is_informative(self, tmp_path):
        """The skip reason string should mention the missing file."""
        result = _check_baseline_reuse(tmp_path)
        assert result["passed"]
        assert any("baseline_analysis" in r.lower() for r in result["reasons"])

    # ── Case C: low confidence or not acquired ──────────────────────────────

    def test_case_c_low_confidence(self, tmp_path):
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.5},
            "package": {"acquired": True},
        })
        result = _check_baseline_reuse(tmp_path)
        assert result["passed"]
        assert result["case"] == "C"

    def test_case_c_exactly_at_threshold_boundary(self, tmp_path):
        """Confidence == 0.7 is NOT below threshold → should not be Case C."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.7},
            "package": {"acquired": True},
        })
        result = _check_baseline_reuse(tmp_path)
        # At or above threshold AND acquired → should be Case A or B (not C)
        assert result["case"] != "C"

    def test_case_c_not_acquired(self, tmp_path):
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": False},
        })
        result = _check_baseline_reuse(tmp_path)
        assert result["passed"]
        assert result["case"] == "C"

    def test_case_c_missing_confidence_section(self, tmp_path):
        """Missing confidence section → confidence defaults to 0.0 → Case C."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "package": {"acquired": True},
        })
        result = _check_baseline_reuse(tmp_path)
        assert result["passed"]
        assert result["case"] == "C"

    def test_case_c_returns_empty_reasons(self, tmp_path):
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.3},
            "package": {"acquired": False},
        })
        result = _check_baseline_reuse(tmp_path)
        assert result["reasons"] == []

    # ── Case A: same language, COPIED FROM required ─────────────────────────

    def test_case_a_passes_with_copied_from_marker(self, tmp_path):
        """Baseline file has 'COPIED FROM:' marker → Case A passes."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True, "language": ["Python"]},
        })
        # No cross_language_porting in experiment_plan → same language
        _write_yaml(tmp_path / "experiment_plan.yaml", {
            "requires_code": True,
        })
        baseline_dir = tmp_path / "experiment" / "baseline"
        baseline_dir.mkdir(parents=True)
        (baseline_dir / "model.py").write_text(
            "# COPIED FROM: https://github.com/foo/bar@abc123 src/model.py\n"
            "def f(): pass\n"
        )
        result = _check_baseline_reuse(tmp_path)
        assert result["passed"], f"Case A should pass; reasons={result['reasons']}"
        assert result["case"] == "A"

    def test_case_a_fails_without_marker(self, tmp_path):
        """Baseline dir exists but no 'COPIED FROM:' annotation → Case A fails."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True, "language": ["Python"]},
        })
        _write_yaml(tmp_path / "experiment_plan.yaml", {
            "requires_code": True,
        })
        baseline_dir = tmp_path / "experiment" / "baseline"
        baseline_dir.mkdir(parents=True)
        (baseline_dir / "model.py").write_text("def f(): pass\n")
        result = _check_baseline_reuse(tmp_path)
        assert not result["passed"]
        assert result["case"] == "A"
        assert any("COPIED FROM" in r for r in result["reasons"])

    def test_case_a_passes_with_direct_baseline_dir(self, tmp_path):
        """pipeline_dir/baseline/ (no experiment subdirectory) also satisfies Case A."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True},
        })
        baseline_dir = tmp_path / "baseline"
        baseline_dir.mkdir(parents=True)
        (baseline_dir / "algo.py").write_text(
            "# COPIED FROM: https://github.com/org/repo train.py\n"
        )
        result = _check_baseline_reuse(tmp_path)
        assert result["passed"], f"Case A should pass; reasons={result['reasons']}"
        assert result["case"] == "A"

    def test_case_a_fails_with_empty_baseline_dirs(self, tmp_path):
        """Baseline dir exists but is empty → no file to scan → 'COPIED FROM:' not found."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True},
        })
        baseline_dir = tmp_path / "experiment" / "baseline"
        baseline_dir.mkdir(parents=True)
        # Nothing written inside; dir is empty
        result = _check_baseline_reuse(tmp_path)
        assert not result["passed"]
        assert result["case"] == "A"

    def test_case_a_no_baseline_dir_at_all(self, tmp_path):
        """No baseline dir → no 'COPIED FROM:' → Case A fails."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True},
        })
        result = _check_baseline_reuse(tmp_path)
        assert not result["passed"]
        assert result["case"] == "A"

    def test_case_a_marker_in_binary_header(self, tmp_path):
        """Implementation reads first 2 KB as bytes; a COPIED FROM in bytes is caught."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True},
        })
        baseline_dir = tmp_path / "experiment" / "baseline"
        baseline_dir.mkdir(parents=True)
        (baseline_dir / "stub.py").write_bytes(
            b"# COPIED FROM: https://github.com/x/y main.py\n" + b"x = 1\n"
        )
        result = _check_baseline_reuse(tmp_path)
        assert result["passed"], f"bytes-level 'COPIED FROM:' should be found; reasons={result['reasons']}"
        assert result["case"] == "A"

    # ── Case B: cross-language port ─────────────────────────────────────────

    def test_case_b_passes_with_rationale_and_originals(self, tmp_path):
        """Cross-language port with REWRITE_RATIONALE.md and non-empty baseline_original/."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True, "language": ["R"]},
        })
        _write_yaml(tmp_path / "experiment_plan.yaml", {
            "requires_code": True,
            "cross_language_porting": {
                "baseline_language": "R",
                "target_language": "Python",
                "rewrite_rationale_doc": "experiment/baseline/REWRITE_RATIONALE.md",
                "critic_must_verify": True,
            },
        })
        baseline_dir = tmp_path / "experiment" / "baseline"
        baseline_dir.mkdir(parents=True)
        (baseline_dir / "REWRITE_RATIONALE.md").write_text(
            "# Rationale\nR matrix ops were ported to numpy.\n"
        )
        original_dir = tmp_path / "experiment" / "baseline_original"
        original_dir.mkdir(parents=True)
        (original_dir / "main.R").write_text("# original R code\n")
        result = _check_baseline_reuse(tmp_path)
        assert result["passed"], f"Case B should pass; reasons={result['reasons']}"
        assert result["case"] == "B"

    def test_case_b_fails_without_rationale(self, tmp_path):
        """Cross-language port missing REWRITE_RATIONALE.md → Case B fails."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True, "language": ["R"]},
        })
        _write_yaml(tmp_path / "experiment_plan.yaml", {
            "requires_code": True,
            "cross_language_porting": {
                "baseline_language": "R",
                "target_language": "Python",
                "rewrite_rationale_doc": "experiment/baseline/REWRITE_RATIONALE.md",
                "critic_must_verify": True,
            },
        })
        # baseline_original exists but REWRITE_RATIONALE.md does not
        original_dir = tmp_path / "experiment" / "baseline_original"
        original_dir.mkdir(parents=True)
        (original_dir / "main.R").write_text("# R code\n")
        result = _check_baseline_reuse(tmp_path)
        assert not result["passed"]
        assert result["case"] == "B"
        assert any("REWRITE_RATIONALE" in r for r in result["reasons"])

    def test_case_b_fails_without_original_dir(self, tmp_path):
        """Cross-language port missing baseline_original/ → Case B fails."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True, "language": ["R"]},
        })
        _write_yaml(tmp_path / "experiment_plan.yaml", {
            "requires_code": True,
            "cross_language_porting": {
                "baseline_language": "R",
                "target_language": "Python",
                "rewrite_rationale_doc": "experiment/baseline/REWRITE_RATIONALE.md",
                "critic_must_verify": True,
            },
        })
        # REWRITE_RATIONALE.md exists but no baseline_original/
        baseline_dir = tmp_path / "experiment" / "baseline"
        baseline_dir.mkdir(parents=True)
        (baseline_dir / "REWRITE_RATIONALE.md").write_text("# Rationale\n...")
        result = _check_baseline_reuse(tmp_path)
        assert not result["passed"]
        assert result["case"] == "B"
        assert any("baseline_original" in r for r in result["reasons"])

    def test_case_b_fails_with_empty_original_dir(self, tmp_path):
        """baseline_original/ present but empty → Case B fails."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True, "language": ["R"]},
        })
        _write_yaml(tmp_path / "experiment_plan.yaml", {
            "cross_language_porting": {
                "baseline_language": "R",
                "target_language": "Python",
                "rewrite_rationale_doc": "REWRITE_RATIONALE.md",
                "critic_must_verify": True,
            },
        })
        baseline_dir = tmp_path / "experiment" / "baseline"
        baseline_dir.mkdir(parents=True)
        (baseline_dir / "REWRITE_RATIONALE.md").write_text("# Rationale\n...")
        # Create empty baseline_original/
        (tmp_path / "experiment" / "baseline_original").mkdir(parents=True)
        result = _check_baseline_reuse(tmp_path)
        assert not result["passed"]
        assert result["case"] == "B"

    def test_case_b_both_missing_gives_two_reasons(self, tmp_path):
        """When both REWRITE_RATIONALE.md and baseline_original/ are absent, both reasons appear."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "package": {"acquired": True},
        })
        _write_yaml(tmp_path / "experiment_plan.yaml", {
            "cross_language_porting": {
                "baseline_language": "R",
                "target_language": "Python",
                "rewrite_rationale_doc": "experiment/baseline/REWRITE_RATIONALE.md",
                "critic_must_verify": True,
            },
        })
        result = _check_baseline_reuse(tmp_path)
        assert not result["passed"]
        assert result["case"] == "B"
        assert len(result["reasons"]) >= 2

    # ── Return-value contract ───────────────────────────────────────────────

    def test_result_always_has_required_keys(self, tmp_path):
        """Every code path must return a dict with passed, case, and reasons."""
        result = _check_baseline_reuse(tmp_path)
        for key in ("passed", "case", "reasons"):
            assert key in result, f"missing key '{key}' in result"

    def test_passed_true_means_empty_reasons_for_case_c(self, tmp_path):
        """Case C always passes cleanly with no blocking reasons."""
        _write_yaml(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.0},
            "package": {"acquired": False},
        })
        result = _check_baseline_reuse(tmp_path)
        assert result["passed"]
        assert result["reasons"] == []
