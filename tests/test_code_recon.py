"""Tests for A20 — code-reference reconnaissance + grounded locators.

Covers:
- URL extraction from PDF-style text
- code_recon.run_code_recon end-to-end using a local git repo fixture
- preflight ``reference_codebases`` verification (BLOCK on bad triple)
- scheduler enum / routing presence
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from researchos.runtime.code_recon import (
    extract_code_refs,
    gather_urls_for_pipeline,
    run_code_recon,
)
from researchos.runtime.knowledge_compiler import compile_raw_to_wiki
from researchos.runtime.preflight import run_preflight
from researchos.schemas.pipeline import PipelineStage, PipelineStep


# ── URL extraction ──────────────────────────────────────────────────


class TestExtractCodeRefs:
    def test_github_plain(self):
        text = "see https://github.com/matteocamilli/bma-package for code"
        assert extract_code_refs(text) == [
            "https://github.com/matteocamilli/bma-package"
        ]

    def test_strips_trailing_punctuation(self):
        # PDFs often glue "." or ")" onto the URL.
        text = "Code: (https://github.com/foo/bar)."
        assert extract_code_refs(text) == ["https://github.com/foo/bar"]

    def test_dedup_and_order(self):
        text = (
            "https://github.com/foo/bar and again https://github.com/foo/bar "
            "plus https://gitlab.com/alice/project"
        )
        assert extract_code_refs(text) == [
            "https://github.com/foo/bar",
            "https://gitlab.com/alice/project",
        ]

    def test_truncates_to_repo_root(self):
        text = "https://github.com/foo/bar/tree/main/src/module.py"
        assert extract_code_refs(text) == ["https://github.com/foo/bar"]

    def test_ignores_non_code_urls(self):
        text = "paper at https://arxiv.org/abs/2201.12345 and doi.org/10.1/2"
        assert extract_code_refs(text) == []


# ── knowledge_compiler wiki frontmatter integration ─────────────────


class TestWikiCodeRefsFrontmatter:
    def test_code_refs_written_to_frontmatter(self, tmp_path: Path):
        root = tmp_path
        raw_dir = root / "knowledge" / "raw"
        raw_dir.mkdir(parents=True)
        paper = raw_dir / "example_paper.md"
        paper.write_text(
            "Replication package available at "
            "https://github.com/example/demo-repo."
        )
        created = compile_raw_to_wiki(paper, root)
        assert created is not None
        head = created.read_text().split("---", 2)[1]
        fm = yaml.safe_load(head)
        assert "code_refs" in fm
        assert fm["code_refs"] == ["https://github.com/example/demo-repo"]

    def test_no_code_refs_key_when_empty(self, tmp_path: Path):
        root = tmp_path
        raw_dir = root / "knowledge" / "raw"
        raw_dir.mkdir(parents=True)
        paper = raw_dir / "plain_paper.md"
        paper.write_text("No repository mentioned in this abstract.")
        created = compile_raw_to_wiki(paper, root)
        assert created is not None
        head = created.read_text().split("---", 2)[1]
        fm = yaml.safe_load(head)
        # Missing key is the contract — not an empty list.
        assert "code_refs" not in fm


# ── code_recon with a local git fixture ─────────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True
    )


def _build_local_repo(tmp_path: Path) -> Path:
    """Build a tiny local git repo that run_code_recon can clone from."""
    origin = tmp_path / "fake-origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "test@example.com")
    _git(origin, "config", "user.name", "Test")
    (origin / "train.py").write_text(
        "\n".join(f"# line {i}" for i in range(1, 51)) + "\n"
    )
    (origin / "README.md").write_text(
        "# Demo\n\nDownload the dataset via `load_dataset('cifar10')`.\n"
    )
    (origin / "LICENSE").write_text("MIT License\n\nCopyright ...\n")
    (origin / "weights.pth").write_bytes(b"\x00" * 1024)  # heavy; should be pruned
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "init")
    return origin


class TestRunCodeRecon:
    def test_clones_prunes_and_reports(self, tmp_path: Path):
        origin = _build_local_repo(tmp_path)

        project_root = tmp_path / "proj"
        pdir = project_root / "artifacts" / "pipeline_demo"
        pdir.mkdir(parents=True)
        plan = {
            "reference_codebases": [f"file://{origin}"],
        }
        (pdir / "experiment_plan.yaml").write_text(yaml.safe_dump(plan))

        report = run_code_recon(pdir, project_root)
        assert report["pipeline_id"] == "demo"
        assert len(report["repos"]) == 1
        repo = report["repos"][0]
        assert repo["status"] in ("ok", "cached")
        assert repo["commit_sha"]
        assert "train.py" in repo["entry_points"]
        # weights.pth must be pruned.
        clone_root = project_root / repo["path"]
        assert not (clone_root / "weights.pth").exists()
        # v5.3: dataset_hints field removed — README dataset hint scraping moved to
        # the BASELINE_ANALYZE LLM step, which can read code semantics not just keyword lines.
        assert "dataset_hints" not in repo

        # code_recon.yaml was written next to state.
        out_path = pdir / "code_recon.yaml"
        assert out_path.exists()
        on_disk = yaml.safe_load(out_path.read_text())
        assert on_disk["repos"][0]["slug"] == repo["slug"]

    def test_error_on_missing_remote_writes_stub(self, tmp_path: Path):
        project_root = tmp_path / "proj"
        pdir = project_root / "artifacts" / "pipeline_demo2"
        pdir.mkdir(parents=True)
        plan = {
            # Deliberately bogus: a nonexistent file path.
            "reference_codebases": [
                f"file://{tmp_path}/does-not-exist"
            ],
        }
        (pdir / "experiment_plan.yaml").write_text(yaml.safe_dump(plan))
        report = run_code_recon(pdir, project_root)
        assert report["repos"][0]["status"] == "error"

    def test_gather_urls_from_evidence_refs_string(self, tmp_path: Path):
        project_root = tmp_path / "proj"
        pdir = project_root / "artifacts" / "pipeline_demo3"
        pdir.mkdir(parents=True)
        (pdir / "state.json").write_text(json.dumps({
            "evidence_refs": [
                "See the replication package at https://github.com/x/y.",
            ],
        }))
        urls = gather_urls_for_pipeline(pdir, project_root)
        assert urls == ["https://github.com/x/y"]


# ── Preflight grounded-locator verification ─────────────────────────


class TestPreflightReferenceCodebases:
    def _build_plan(self, pdir: Path) -> None:
        plan = {
            "plan_dataset": {
                "dataset_description": "demo",
                "dataset_location": "demo",
                "task_definition": "demo",
                "read_data_step": "demo",
                "data_preprocessing_step": "demo",
                "data_dataloader_step": "demo",
            },
            "plan_model": {
                "model_input": "x",
                "model_output": "y",
                "key_components": ["c"],
            },
            "plan_training": {
                "training_pipeline": "t",
                "loss_function": "l",
                "optimizer": "o",
                "training_configurations": "cfg",
                "monitor_and_logging": "m",
            },
            "plan_testing": {
                "test_metric": "acc",
                "test_data": "d",
                "test_code": "c",
            },
        }
        (pdir / "experiment_plan.yaml").write_text(yaml.safe_dump(plan))

    def _seed_clone(self, project_root: Path, slug: str) -> Path:
        repo = project_root / "scratch" / "demo" / "code_refs" / slug
        repo.mkdir(parents=True)
        (repo / "train.py").write_text("\n".join(f"row {i}" for i in range(1, 21)) + "\n")
        return repo

    def _write_recon(self, pdir: Path, project_root: Path, slug: str, repo: Path) -> None:
        recon = {
            "pipeline_id": "demo",
            "repos": [
                {
                    "url": "file:///tmp/x",
                    "slug": slug,
                    "path": str(repo.relative_to(project_root)),
                    "status": "ok",
                    "commit_sha": "deadbeef",
                    "entry_points": ["train.py"],
                    "dataset_hints": [],
                    "license": "MIT",
                }
            ],
        }
        (pdir / "code_recon.yaml").write_text(yaml.safe_dump(recon))

    def _write_code_patch(self, pdir: Path) -> None:
        code = (
            "# === CLAIM-TO-CODE MAPPING ===\n"
            "# | C1 | demo concept | demo.py::demo_fn | softmax |\n"
            "# === END MAPPING ===\n"
            "def demo_fn():\n    return 1\n"
        )
        # Write as run_experiment.py so preflight._load_code picks it up.
        (pdir / "run_experiment.py").write_text(code)

    def test_valid_triple_passes(self, tmp_path: Path):
        project_root = tmp_path / "proj"
        pdir = project_root / "artifacts" / "pipeline_demo"
        pdir.mkdir(parents=True)
        self._build_plan(pdir)
        repo = self._seed_clone(project_root, "example__demo")
        self._write_recon(pdir, project_root, "example__demo", repo)
        self._write_code_patch(pdir)
        (pdir / "concepts.yaml").write_text(yaml.safe_dump([
            {
                "id": "C1",
                "definition": "demo",
                "code_implementation": "demo.py::demo_fn",
                "reference_papers": ["X"],
                "reference_codebases": ["example__demo::train.py::L2-L10"],
            }
        ]))
        report = run_preflight(pdir, project_root)
        assert report["reference_codebases"]["verdict"] == "PASS"
        assert report["reference_codebases"]["triples_total"] == 1
        assert report["reference_codebases"]["triples_verified"] == 1

    def test_out_of_range_lines_block(self, tmp_path: Path):
        project_root = tmp_path / "proj"
        pdir = project_root / "artifacts" / "pipeline_demo"
        pdir.mkdir(parents=True)
        self._build_plan(pdir)
        repo = self._seed_clone(project_root, "example__demo")
        self._write_recon(pdir, project_root, "example__demo", repo)
        self._write_code_patch(pdir)
        (pdir / "concepts.yaml").write_text(yaml.safe_dump([
            {
                "id": "C1",
                "definition": "demo",
                "code_implementation": "demo.py::demo_fn",
                "reference_papers": ["X"],
                # train.py has only 20 lines; L100 must fail.
                "reference_codebases": ["example__demo::train.py::L50-L100"],
            }
        ]))
        report = run_preflight(pdir, project_root)
        assert report["reference_codebases"]["verdict"] == "BLOCK"
        assert report["verdict"] == "BLOCK"

    def test_unknown_slug_blocks(self, tmp_path: Path):
        project_root = tmp_path / "proj"
        pdir = project_root / "artifacts" / "pipeline_demo"
        pdir.mkdir(parents=True)
        self._build_plan(pdir)
        # No matching recon entry — locator slug is fabricated.
        (pdir / "code_recon.yaml").write_text(yaml.safe_dump(
            {"pipeline_id": "demo", "repos": []}
        ))
        self._write_code_patch(pdir)
        (pdir / "concepts.yaml").write_text(yaml.safe_dump([
            {
                "id": "C1",
                "definition": "demo",
                "code_implementation": "demo.py::demo_fn",
                "reference_papers": ["X"],
                "reference_codebases": ["phantom_repo::src/x.py::L1-L3"],
            }
        ]))
        report = run_preflight(pdir, project_root)
        assert report["reference_codebases"]["verdict"] == "BLOCK"

    def test_url_only_does_not_block(self, tmp_path: Path):
        project_root = tmp_path / "proj"
        pdir = project_root / "artifacts" / "pipeline_demo"
        pdir.mkdir(parents=True)
        self._build_plan(pdir)
        (pdir / "code_recon.yaml").write_text(yaml.safe_dump(
            {"pipeline_id": "demo", "repos": []}
        ))
        self._write_code_patch(pdir)
        (pdir / "concepts.yaml").write_text(yaml.safe_dump([
            {
                "id": "C1",
                "definition": "demo",
                "code_implementation": "demo.py::demo_fn",
                "reference_papers": ["X"],
                "reference_codebases": ["https://github.com/x/y"],
            }
        ]))
        report = run_preflight(pdir, project_root)
        assert report["reference_codebases"]["verdict"] == "PASS"


# ── Scheduler routing (A20) ─────────────────────────────────────────


class TestCodeReconRouting:
    def test_design_routes_to_code_recon(self):
        """Post-2026-04: DESIGN → CODE_RECON directly (DESIGN_REVIEW retired)."""
        from researchos.runtime.scheduler import _NEXT_STEP
        key = (PipelineStage.MAIN, PipelineStep.DESIGN)
        assert _NEXT_STEP[key] == PipelineStep.CODE_RECON

    def test_code_recon_routes_through_baseline_then_data(self):
        """2026-04-26 canonical: CODE_RECON → BASELINE_ANALYZE → DATA_RECON
        → CONCEPT_PAPER (then CONCEPT_CODE → CODE)."""
        from researchos.runtime.scheduler import _NEXT_STEP
        assert (
            _NEXT_STEP[(PipelineStage.MAIN, PipelineStep.CODE_RECON)]
            == PipelineStep.BASELINE_ANALYZE
        )
        assert (
            _NEXT_STEP[(PipelineStage.MAIN, PipelineStep.BASELINE_ANALYZE)]
            == PipelineStep.DATA_RECON
        )
        assert (
            _NEXT_STEP[(PipelineStage.MAIN, PipelineStep.DATA_RECON)]
            == PipelineStep.CONCEPT_PAPER
        )

    def test_fast_fail_includes_code_recon(self):
        """2026-04-26 canonical: FAST_FAIL is the rich research path
        (not the lean shortcut). It runs the full DESIGN → CODE_RECON →
        BASELINE_ANALYZE → DATA_RECON → CONCEPT_PAPER → CONCEPT_CODE
        backbone, lightening only the RUN budget (via scale_profile)
        and the terminal layer (no results_reverify / figure_compose).
        """
        from researchos.runtime.scheduler import _NEXT_STEP
        assert (
            _NEXT_STEP[(PipelineStage.FAST_FAIL, PipelineStep.DESIGN)]
            == PipelineStep.CODE_RECON
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
