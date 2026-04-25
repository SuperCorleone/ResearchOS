"""P1 + P3 unit tests.

P1a: data_shape scanner picks up data files; decide_strategy chooses
     code_primary iff any ok/cached repo has hits, else text_fallback.
P1b: assert_strategy_actions_consistent BLOCKS on the four anti-patterns.
P3 : _link_dataset_path materialises files via hardlink/symlink, leaving
     real bytes on disk so directory_hash is non-empty.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from researchos.core.contract_guard import (
    ContractViolation,
    assert_strategy_actions_consistent,
)
from researchos.runtime.data_recon import _link_dataset_path
from researchos.schemas.baseline_analysis import (
    BaselineAnalysis,
    DatasetPath,
    RepoAnalysis,
)
from researchos.utils.data_shape import (
    DataShapeHit,
    decide_strategy,
    scan_repo_for_data,
)


# ── P1a: data_shape ──────────────────────────────────────────────────


def _mkfile(p: Path, content: bytes = b"x" * 200) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


def test_scan_picks_high_tier_under_data_dir(tmp_path: Path):
    _mkfile(tmp_path / "data" / "training.csv")
    _mkfile(tmp_path / "datasets" / "test.parquet")
    _mkfile(tmp_path / "src" / "main.py")
    hits = scan_repo_for_data(tmp_path)
    paths = {h.path for h in hits}
    assert "data/training.csv" in paths
    assert "datasets/test.parquet" in paths
    assert "src/main.py" not in paths
    assert all(h.tier == "high" for h in hits if "data" in h.path or "datasets" in h.path)


def test_scan_picks_medium_tier_by_extension_anywhere(tmp_path: Path):
    _mkfile(tmp_path / "results" / "metrics.csv")  # not in data/, but .csv
    _mkfile(tmp_path / "scripts" / "run.py")
    hits = scan_repo_for_data(tmp_path)
    assert any(h.path == "results/metrics.csv" and h.tier == "medium" for h in hits)


def test_scan_excludes_test_and_docs_dirs(tmp_path: Path):
    _mkfile(tmp_path / "tests" / "fixture.csv")
    _mkfile(tmp_path / "docs" / "manual.csv")
    _mkfile(tmp_path / "data" / "real.csv")
    hits = scan_repo_for_data(tmp_path)
    paths = {h.path for h in hits}
    assert "data/real.csv" in paths
    assert "tests/fixture.csv" not in paths
    assert "docs/manual.csv" not in paths


def test_scan_skips_tiny_files(tmp_path: Path):
    _mkfile(tmp_path / "data" / "real.csv", content=b"x" * 200)
    _mkfile(tmp_path / "data" / ".gitkeep", content=b"")
    hits = scan_repo_for_data(tmp_path)
    paths = {h.path for h in hits}
    assert "data/real.csv" in paths
    assert "data/.gitkeep" not in paths


def test_decide_strategy_prefers_code_primary_when_any_repo_has_hits(tmp_path: Path):
    repos = [
        {"slug": "a/b", "status": "ok"},
        {"slug": "c/d", "status": "ok"},
    ]
    hits = {
        "a/b": [DataShapeHit(path="data/x.csv", tier="high", reason="r")],
        "c/d": [],
    }
    assert decide_strategy(repos, hits) == "code_primary"


def test_decide_strategy_text_fallback_when_no_hits(tmp_path: Path):
    repos = [{"slug": "a/b", "status": "ok"}]
    hits: dict = {"a/b": []}
    assert decide_strategy(repos, hits) == "text_fallback"


def test_decide_strategy_text_fallback_when_no_ok_repos(tmp_path: Path):
    repos = [
        {"slug": "a/b", "status": "error"},
        {"slug": "c/d", "status": "rejected_by_whitelist"},
    ]
    assert decide_strategy(repos, {}) == "text_fallback"


# ── P1b: contract guard ──────────────────────────────────────────────


def _make_analysis(strategy: str, **kwargs) -> BaselineAnalysis:
    defaults = dict(
        pipeline_id="test_pid",
        analysis_strategy=strategy,
        analyzed_repos=[],
        external_data_urls=[],
        fallback_used=False,
        status="success",
        notes="",
    )
    defaults.update(kwargs)
    return BaselineAnalysis(**defaults)


def test_contract_blocks_code_primary_with_no_dataset_paths(tmp_path: Path):
    a = _make_analysis(
        "code_primary",
        analyzed_repos=[
            RepoAnalysis(slug="a/b", role="r", entry_points=[], dataset_paths=[]),
        ],
    )
    with pytest.raises(ContractViolation, match="dataset_paths"):
        assert_strategy_actions_consistent(a)


def test_contract_blocks_code_primary_with_no_data_found_status(tmp_path: Path):
    a = _make_analysis(
        "code_primary",
        analyzed_repos=[
            RepoAnalysis(
                slug="a/b", role="r", entry_points=[],
                dataset_paths=[DatasetPath(path="data/x.csv", kind="training")],
            ),
        ],
        status="no_data_found",
    )
    with pytest.raises(ContractViolation, match="no_data_found"):
        assert_strategy_actions_consistent(a)


def test_contract_blocks_text_fallback_without_flag(tmp_path: Path):
    a = _make_analysis("text_fallback", fallback_used=False, notes="why")
    with pytest.raises(ContractViolation, match="fallback_used"):
        assert_strategy_actions_consistent(a)


def test_contract_blocks_text_fallback_without_notes(tmp_path: Path):
    a = _make_analysis("text_fallback", fallback_used=True, notes="   ")
    with pytest.raises(ContractViolation, match="notes"):
        assert_strategy_actions_consistent(a)


def test_contract_blocks_strategy_with_empty_executed_actions(tmp_path: Path):
    """The BMA-shape failure: strategy=code_primary but manifest has no actions."""
    a = _make_analysis(
        "code_primary",
        analyzed_repos=[
            RepoAnalysis(
                slug="a/b", role="r", entry_points=[],
                dataset_paths=[DatasetPath(path="data/x.csv", kind="training")],
            ),
        ],
    )
    manifest_path = tmp_path / "data_manifest.yaml"
    manifest_path.write_text(yaml.safe_dump({"executed_actions": []}))
    with pytest.raises(ContractViolation, match="executed_actions"):
        assert_strategy_actions_consistent(a, manifest_path)


def test_contract_passes_when_strategy_and_actions_align(tmp_path: Path):
    a = _make_analysis(
        "code_primary",
        analyzed_repos=[
            RepoAnalysis(
                slug="a/b", role="r", entry_points=[],
                dataset_paths=[DatasetPath(path="data/x.csv", kind="training")],
            ),
        ],
    )
    manifest_path = tmp_path / "data_manifest.yaml"
    manifest_path.write_text(yaml.safe_dump({
        "executed_actions": [{"source": "data/x.csv", "status": "ok"}],
    }))
    assert_strategy_actions_consistent(a, manifest_path)  # no raise


def test_contract_passes_for_well_formed_text_fallback(tmp_path: Path):
    a = _make_analysis(
        "text_fallback",
        fallback_used=True,
        notes="No cloned repo had data-shape files; analyser fell back to paper text.",
    )
    assert_strategy_actions_consistent(a)  # no raise


# ── P3: link/copy materialisation ────────────────────────────────────


def test_link_dataset_path_uses_hardlink_when_possible(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "data").mkdir()
    src_file = repo / "data" / "x.csv"
    src_file.write_bytes(b"col1,col2\n1,2\n3,4\n")
    src_inode = src_file.stat().st_ino

    dest_root = tmp_path / "dest"
    row = _link_dataset_path(repo, "data/x.csv", dest_root)

    assert row["status"] == "ok"
    dest = dest_root / "data" / "x.csv"
    assert dest.exists()
    # Same inode == hardlink. Same fs guarantees this on the test sandbox.
    assert dest.stat().st_ino == src_inode
    assert row["materialise_mode"] in ("hardlink", "symlink", "copy")
    assert row["size_bytes"] == src_file.stat().st_size


def test_link_dataset_path_handles_directory(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "data" / "sub").mkdir(parents=True)
    (repo / "data" / "a.csv").write_bytes(b"a" * 100)
    (repo / "data" / "sub" / "b.csv").write_bytes(b"b" * 100)

    dest_root = tmp_path / "dest"
    row = _link_dataset_path(repo, "data", dest_root)

    assert row["status"] == "ok"
    assert (dest_root / "data" / "a.csv").exists()
    assert (dest_root / "data" / "sub" / "b.csv").exists()
    assert row["size_bytes"] == 200


def test_link_dataset_path_error_on_missing_source(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    row = _link_dataset_path(repo, "data/nope.csv", tmp_path / "dest")
    assert row["status"] == "error"
    assert "source not found" in row["error"]
