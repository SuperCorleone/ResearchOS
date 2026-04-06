"""Tests for professor decision CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _setup_project(tmp_path):
    """Create minimal project structure for CLI tests."""
    (tmp_path / "decisions").mkdir()
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "runtime" / "pipelines").mkdir(parents=True)
    (tmp_path / "runtime" / "logs").mkdir(parents=True)
    return tmp_path


def test_write_decision(tmp_path):
    """Test that write_decision creates a valid YAML file."""
    _setup_project(tmp_path)

    # Patch paths to use tmp_path
    import researchos.utils.paths as paths

    orig_decisions = paths.DECISIONS_DIR
    paths.DECISIONS_DIR = tmp_path / "decisions"

    try:
        from scripts.professor_decide import write_decision

        path = write_decision(
            pipeline_id="test_pipe",
            decision="approve",
            comment="Looks good",
            next_milestone="paper",
            budget_adjustment=5000,
        )

        assert path.exists()
        data = yaml.safe_load(path.read_text())
        assert data["pipeline_id"] == "test_pipe"
        assert data["decision"] == "approve"
        assert data["comment"] == "Looks good"
        assert data["next_milestone"] == "paper"
        assert data["budget_adjustment"] == 5000
        assert "created_at" in data
    finally:
        paths.DECISIONS_DIR = orig_decisions


def test_list_pending_requests(tmp_path):
    """Test scanning for request_*.yaml files."""
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()

    # Create request files
    for pid in ["pipe_a", "pipe_b"]:
        req = {"pipeline_id": pid, "context_files": []}
        (decisions_dir / f"request_{pid}_20260101.yaml").write_text(
            yaml.dump(req, default_flow_style=False)
        )

    # Create a non-request file (should be ignored)
    (decisions_dir / "approval_pipe_c_20260101.yaml").write_text("test: true")

    from unittest.mock import patch

    # Patch the module-level DECISIONS_DIR in the script module
    with patch("scripts.professor_decide.DECISIONS_DIR", decisions_dir):
        from scripts.professor_decide import list_pending_requests

        requests = list_pending_requests()
        assert len(requests) == 2
        pids = {r["pipeline_id"] for r in requests}
        assert pids == {"pipe_a", "pipe_b"}


def test_write_decision_kill(tmp_path):
    """Test writing a kill decision."""
    _setup_project(tmp_path)

    import researchos.utils.paths as paths

    orig_decisions = paths.DECISIONS_DIR
    paths.DECISIONS_DIR = tmp_path / "decisions"

    try:
        from scripts.professor_decide import write_decision

        path = write_decision(
            pipeline_id="failing_pipe",
            decision="kill",
            comment="Results not promising",
        )

        data = yaml.safe_load(path.read_text())
        assert data["decision"] == "kill"
        assert data["comment"] == "Results not promising"
        assert "next_milestone" not in data
        assert "budget_adjustment" not in data
    finally:
        paths.DECISIONS_DIR = orig_decisions
