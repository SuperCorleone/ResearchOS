"""Tests for baseline_diagnose: data hash + hyperparameter checks.

The actual diagnose() return dict has shape:
  {
    verdict: "auto_fixable" | "escalate_to_l2",
    fixer_directives: [...],
    checks: {
      data_hashes:     {passed, diff, auto_fixable},
      hyperparameters: {passed, diff},
    },
  }

The top-level convenience aliases ``data_hash_check`` / ``hyperparam_check``
referenced in the task brief map to ``result["checks"]["data_hashes"]`` and
``result["checks"]["hyperparameters"]`` respectively.  Tests use explicit
accessors so breakage in either layer is visible.

The missing-baseline_analysis stub returns:
  {verdict: "escalate_to_l2", reason: "..."}
  (no "checks" key).
"""

import json
import yaml
from pathlib import Path
import pytest

from researchos.runtime.baseline_diagnose import diagnose


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(p: Path, content):
    """Write a JSON or YAML file, creating parent dirs."""
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix == ".json":
        p.write_text(json.dumps(content))
    else:
        p.write_text(yaml.safe_dump(content))


def _data_hash_check(result: dict) -> dict:
    """Pull the data-hash sub-check from diagnose() output."""
    return result["checks"]["data_hashes"]


def _hyperparam_check(result: dict) -> dict:
    """Pull the hyperparameter sub-check from diagnose() output."""
    return result["checks"]["hyperparameters"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDiagnose:
    def test_both_pass_when_everything_matches(self, tmp_path):
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {"expected_hashes": {"data.csv": "abc123"}},
            "algorithm": {"hyperparameters_in_paper": {"lr": 0.01, "batch": 32}},
        })
        _write(tmp_path / "data_manifest.yaml", {
            "files": [{"path": "data.csv", "sha256": "abc123"}],
        })
        _write(tmp_path / "results.json", {
            "hyperparameters": {"lr": 0.01, "batch": 32},
        })
        result = diagnose(tmp_path)
        assert _data_hash_check(result)["passed"]
        assert _hyperparam_check(result)["passed"]
        assert "verdict" in result
        # Output file must exist
        assert (tmp_path / "baseline_diagnose.json").exists()

    def test_data_hash_mismatch_detected(self, tmp_path):
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {"expected_hashes": {"data.csv": "expected_hash_xyz"}},
            "algorithm": {"hyperparameters_in_paper": {"lr": 0.01}},
        })
        _write(tmp_path / "data_manifest.yaml", {
            "files": [{"path": "data.csv", "sha256": "wrong_hash"}],
        })
        _write(tmp_path / "results.json", {
            "hyperparameters": {"lr": 0.01},
        })
        result = diagnose(tmp_path)
        dhc = _data_hash_check(result)
        assert not dhc["passed"]
        assert any(d["file"] == "data.csv" for d in dhc["diff"])

    def test_data_hash_mismatch_is_auto_fixable(self, tmp_path):
        """When we can name exactly which files differ, the fix is clear."""
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {"expected_hashes": {"train.csv": "hash_a", "test.csv": "hash_b"}},
            "algorithm": {"hyperparameters_in_paper": {"lr": 0.01}},
        })
        _write(tmp_path / "data_manifest.yaml", {
            "files": [
                {"path": "train.csv", "sha256": "WRONG"},
                {"path": "test.csv", "sha256": "hash_b"},  # this one matches
            ],
        })
        _write(tmp_path / "results.json", {"hyperparameters": {"lr": 0.01}})
        result = diagnose(tmp_path)
        dhc = _data_hash_check(result)
        assert not dhc["passed"]
        assert dhc["auto_fixable"]

    def test_hyperparam_mismatch_detected(self, tmp_path):
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {},
            "algorithm": {"hyperparameters_in_paper": {"lr": 0.01, "batch": 64}},
        })
        _write(tmp_path / "data_manifest.yaml", {"files": []})
        _write(tmp_path / "results.json", {
            "hyperparameters": {"lr": 0.001, "batch": 64},
        })
        result = diagnose(tmp_path)
        hpc = _hyperparam_check(result)
        assert not hpc["passed"]
        diff_params = {d["param"] for d in hpc["diff"]}
        assert "lr" in diff_params
        assert "batch" not in diff_params  # batch matched

    def test_hyperparam_mismatch_verdict_includes_fix_directive(self, tmp_path):
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {},
            "algorithm": {"hyperparameters_in_paper": {"lr": 0.01}},
        })
        _write(tmp_path / "data_manifest.yaml", {"files": []})
        _write(tmp_path / "results.json", {"hyperparameters": {"lr": 0.5}})
        result = diagnose(tmp_path)
        assert result["verdict"] == "auto_fixable"
        assert any("fix_hyperparameters" in d for d in result["fixer_directives"])

    def test_missing_baseline_analysis_returns_escalate(self, tmp_path):
        """No baseline_analysis.yaml → verdict must be escalate_to_l2."""
        _write(tmp_path / "data_manifest.yaml", {"files": []})
        _write(tmp_path / "results.json", {})
        result = diagnose(tmp_path)
        assert result["verdict"] == "escalate_to_l2"

    def test_missing_baseline_analysis_still_writes_json(self, tmp_path):
        _write(tmp_path / "data_manifest.yaml", {"files": []})
        _write(tmp_path / "results.json", {})
        diagnose(tmp_path)
        out = tmp_path / "baseline_diagnose.json"
        assert out.exists()
        data = json.loads(out.read_text())
        assert "verdict" in data

    def test_no_expected_hashes_marks_hash_check_passed(self, tmp_path):
        """If baseline_analysis.yaml has no data.expected_hashes, check passes vacuously."""
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {},  # no expected_hashes
            "algorithm": {"hyperparameters_in_paper": {"lr": 0.01}},
        })
        _write(tmp_path / "data_manifest.yaml", {"files": []})
        _write(tmp_path / "results.json", {"hyperparameters": {"lr": 0.01}})
        result = diagnose(tmp_path)
        dhc = _data_hash_check(result)
        assert dhc["passed"]
        assert not dhc["auto_fixable"]

    def test_writes_json_artifact(self, tmp_path):
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {},
            "algorithm": {},
        })
        _write(tmp_path / "data_manifest.yaml", {"files": []})
        _write(tmp_path / "results.json", {})
        diagnose(tmp_path)
        out = tmp_path / "baseline_diagnose.json"
        assert out.exists()
        data = json.loads(out.read_text())
        assert "verdict" in data

    def test_json_artifact_round_trips(self, tmp_path):
        """The artifact written to disk must match the dict returned by diagnose()."""
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {"expected_hashes": {"f.csv": "sha_abc"}},
            "algorithm": {"hyperparameters_in_paper": {"lr": 0.01}},
        })
        _write(tmp_path / "data_manifest.yaml", {
            "files": [{"path": "f.csv", "sha256": "sha_abc"}],
        })
        _write(tmp_path / "results.json", {"hyperparameters": {"lr": 0.01}})
        result = diagnose(tmp_path)
        on_disk = json.loads((tmp_path / "baseline_diagnose.json").read_text())
        assert on_disk["verdict"] == result["verdict"]

    def test_both_checks_no_hyperparams_in_paper(self, tmp_path):
        """When paper lists no hyperparameters, hyperparam_check passes vacuously."""
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {},
            "algorithm": {},  # no hyperparameters_in_paper key
        })
        _write(tmp_path / "data_manifest.yaml", {"files": []})
        _write(tmp_path / "results.json", {"hyperparameters": {"lr": 99}})
        result = diagnose(tmp_path)
        assert _hyperparam_check(result)["passed"]

    def test_multiple_hash_mismatches_all_appear_in_diff(self, tmp_path):
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {
                "expected_hashes": {
                    "a.csv": "hash_a",
                    "b.csv": "hash_b",
                    "c.csv": "hash_c",
                }
            },
            "algorithm": {},
        })
        _write(tmp_path / "data_manifest.yaml", {
            "files": [
                {"path": "a.csv", "sha256": "WRONG_A"},
                {"path": "b.csv", "sha256": "hash_b"},   # correct
                {"path": "c.csv", "sha256": "WRONG_C"},
            ],
        })
        _write(tmp_path / "results.json", {})
        result = diagnose(tmp_path)
        dhc = _data_hash_check(result)
        assert not dhc["passed"]
        diff_files = {d["file"] for d in dhc["diff"]}
        assert "a.csv" in diff_files
        assert "c.csv" in diff_files
        assert "b.csv" not in diff_files

    def test_numeric_hyperparams_tolerance(self, tmp_path):
        """Values that differ by less than 1e-9 should be treated as equal."""
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {},
            "algorithm": {"hyperparameters_in_paper": {"lr": 0.01}},
        })
        _write(tmp_path / "data_manifest.yaml", {"files": []})
        # 0.01 + 1e-12 is within tolerance
        _write(tmp_path / "results.json", {"hyperparameters": {"lr": 0.01 + 1e-12}})
        result = diagnose(tmp_path)
        assert _hyperparam_check(result)["passed"]

    def test_missing_file_from_manifest_shows_actual_none(self, tmp_path):
        """If manifest doesn't list an expected file, actual should be None in diff."""
        _write(tmp_path / "baseline_analysis.yaml", {
            "confidence": {"overall": 0.9},
            "data": {"expected_hashes": {"missing.csv": "some_hash"}},
            "algorithm": {},
        })
        _write(tmp_path / "data_manifest.yaml", {"files": []})  # missing.csv not listed
        _write(tmp_path / "results.json", {})
        result = diagnose(tmp_path)
        dhc = _data_hash_check(result)
        assert not dhc["passed"]
        missing_entries = [d for d in dhc["diff"] if d["file"] == "missing.csv"]
        assert missing_entries
        assert missing_entries[0]["actual"] is None
