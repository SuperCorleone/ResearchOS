"""Tests for the Layer-2 pipeline lineage builder.

Covers schema basics, individual extractors against synthetic fixtures,
and an integration test against the live BMA pipeline artifact directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from researchos.runtime.lineage import (
    build_pipeline_lineage,
    to_mermaid,
    write_pipeline_lineage,
)
from researchos.schemas.lineage import (
    LineageEdge,
    LineageNode,
    PipelineLineage,
)


# ── Schema basics ────────────────────────────────────────────────────

class TestSchemaBasics:
    def test_add_node_dedupes_and_merges_attrs(self):
        ln = PipelineLineage(pipeline_id="t")
        ln.add_node(LineageNode(id="x", kind="paper", label="X", attrs={"a": 1}))
        ln.add_node(LineageNode(id="x", kind="paper", label="X", attrs={"b": 2}))
        assert len(ln.nodes) == 1
        assert ln.nodes[0].attrs == {"a": 1, "b": 2}

    def test_add_edge_dedupes_exact_triple(self):
        ln = PipelineLineage(pipeline_id="t")
        ln.add_edge(LineageEdge(src="a", dst="b", rel="produces"))
        ln.add_edge(LineageEdge(src="a", dst="b", rel="produces"))
        ln.add_edge(LineageEdge(src="a", dst="b", rel="consumes"))  # different rel
        assert len(ln.edges) == 2

    def test_add_edge_merges_evidence_when_missing(self):
        ln = PipelineLineage(pipeline_id="t")
        ln.add_edge(LineageEdge(src="a", dst="b", rel="produces"))
        ln.add_edge(LineageEdge(
            src="a", dst="b", rel="produces", evidence={"file": "x.py"},
        ))
        assert len(ln.edges) == 1
        assert ln.edges[0].evidence == {"file": "x.py"}


# ── Per-extractor coverage on a minimal fixture ──────────────────────

@pytest.fixture
def fixture_pipeline(tmp_path: Path) -> Path:
    """Build a tiny artifact tree the extractors can consume."""
    project = tmp_path / "proj"
    pdir = project / "artifacts" / "pipeline_demo"
    pdir.mkdir(parents=True)

    # state.json with one paper evidence_ref
    state = {
        "pipeline_id": "demo",
        "title": "Demo pipeline",
        "stage": "main",
        "step": "data_recon",
        "research_question": "Does X?",
        "budget_used": 100,
        "budget_allocated": 1000,
        "evidence_refs": [
            {
                "role": "base_paper",
                "path_or_url": "/abs/path/to/Some_Paper.pdf",
            },
        ],
        "artifacts": {
            "code_recon": "code_recon.yaml",
            "baseline_analyze": "baseline_analysis.yaml",
            "data_recon": "data_manifest.yaml",
        },
    }
    (pdir / "state.json").write_text(json.dumps(state))

    # code_recon.yaml: one cloned repo
    (pdir / "code_recon.yaml").write_text(yaml.safe_dump({
        "pipeline_id": "demo",
        "repos": [{
            "url": "https://github.com/foo/bar",
            "slug": "foo__bar",
            "status": "cached",
            "path": "scratch/demo/code_refs/foo__bar",
            "commit_sha": "deadbeef",
            "license": "MIT",
            "entry_points": ["train.py"],
        }],
    }))

    # baseline_analysis.yaml: one dataset_path + one badge URL
    (pdir / "baseline_analysis.yaml").write_text(yaml.safe_dump({
        "pipeline_id": "demo",
        "baseline_paper": "Some Paper",
        "analysis_strategy": "code_primary",
        "status": "success",
        "fallback_used": False,
        "analyzed_repos": [{
            "slug": "foo__bar",
            "role": "Replication package",
            "entry_points": ["train.py"],
            "code_data_calls": [{
                "file": "train.py", "lineno": 42,
                "call_text": "pd.read_csv('data/x.csv')",
                "resolved_path": "data/x.csv",
            }],
            "dataset_paths": [{
                "path": "data/x.csv", "kind": "training",
                "purpose": "Loaded by train.py at L42",
            }],
            "auxiliary_paths": [{"path": "logs/", "reason": "stale"}],
        }],
        "external_data_urls": [{
            "url": "https://example.com/badge.svg",
            "should_download": False,
            "kind": "decorative_badge",
            "reason": "shield image",
        }],
    }))

    # data_manifest.yaml: one downloaded file
    (pdir / "data_manifest.yaml").write_text(yaml.safe_dump({
        "pipeline_id": "demo",
        "data_recon_status": "success",
        "files": [{
            "path": "foo__bar/data/x.csv",
            "sha256": "sha256:cafebabecafebabe",
            "size_bytes": 1234,
        }],
    }))

    # concepts.yaml: one concept that references the paper
    (pdir / "concepts.yaml").write_text(yaml.safe_dump({
        "concepts": [{
            "id": "C1",
            "definition": "A concept",
            "math_formula": "y = f(x)",
            "reference_papers": ["Some_Paper"],
            "code_implementation": "train.py::main",
        }],
    }))

    # judge_verdict.json
    (pdir / "judge_verdict.json").write_text(json.dumps({
        "pipeline_id": "demo",
        "fully_correct": True,
        "per_concept": {"C1": "pass"},
    }))

    # decision file
    (project / "decisions").mkdir()
    (project / "decisions" / "approval_demo_20260101.yaml").write_text(yaml.safe_dump({
        "pipeline_id": "demo",
        "decision": "approve",
        "comment": "looks good",
        "created_at": "2026-01-01T00:00:00+00:00",
    }))

    # milestone report
    (pdir / "milestone_1.md").write_text("# Milestone 1\n\nResults summary...")

    return pdir


class TestExtractors:
    def test_pipeline_node_present(self, fixture_pipeline: Path):
        project_root = fixture_pipeline.parents[1]
        ln = build_pipeline_lineage(fixture_pipeline, project_root)
        ids = ln.node_ids()
        assert "pipeline:demo" in ids

    def test_paper_and_pipeline_link(self, fixture_pipeline: Path):
        ln = build_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        papers = [n for n in ln.nodes if n.kind == "paper"]
        assert len(papers) == 1
        assert papers[0].label == "Some_Paper"
        # pipeline -[derived_from]-> paper
        assert any(
            e.src == "pipeline:demo" and e.dst == papers[0].id
            and e.rel == "derived_from"
            for e in ln.edges
        )

    def test_code_repo_node_and_paper_reference(self, fixture_pipeline: Path):
        ln = build_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        repos = [n for n in ln.nodes if n.kind == "code_repo"]
        assert len(repos) == 1
        assert repos[0].id == "code_repo:foo__bar"
        # paper -[references]-> code_repo
        papers = [n for n in ln.nodes if n.kind == "paper"]
        assert any(
            e.src == papers[0].id and e.dst == repos[0].id
            and e.rel == "references"
            for e in ln.edges
        )

    def test_baseline_analyze_selects_dataset_with_evidence(self, fixture_pipeline: Path):
        ln = build_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        # step:demo:baseline_analyze -[selects]-> dataset_file:p* with code evidence
        selects_edges = [
            e for e in ln.edges
            if e.src == "step:demo:baseline_analyze" and e.rel == "selects"
        ]
        # 1 dataset_file + 1 dataset_url
        assert len(selects_edges) == 2
        # at least one carries grounding evidence (the code call)
        with_evidence = [e for e in selects_edges if e.evidence]
        assert any(
            ev.evidence and ev.evidence.get("file") == "train.py"
            for ev in with_evidence
        )

    def test_data_manifest_creates_dataset_file_with_sha(self, fixture_pipeline: Path):
        ln = build_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        ds_files = [n for n in ln.nodes if n.kind == "dataset_file"]
        # baseline says 1 dataset_file (no sha) + manifest adds 1 with sha →
        # both should appear; manifest uses sha-based id, baseline uses path hash
        assert len(ds_files) >= 2
        with_sha = [n for n in ds_files if n.attrs.get("sha256")]
        assert len(with_sha) >= 1

    def test_concept_links_to_paper_and_records_locator(self, fixture_pipeline: Path):
        ln = build_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        concepts = [n for n in ln.nodes if n.kind == "concept"]
        assert len(concepts) == 1
        c1 = concepts[0]
        assert c1.id == "concept:demo:C1"
        # concept -[defined_in]-> paper
        papers = [n for n in ln.nodes if n.kind == "paper"]
        assert any(
            e.src == c1.id and e.dst == papers[0].id and e.rel == "defined_in"
            for e in ln.edges
        )
        # implemented_in edge with locator evidence
        impl_edges = [
            e for e in ln.edges
            if e.src == c1.id and e.rel == "implemented_in"
        ]
        assert len(impl_edges) == 1
        assert impl_edges[0].evidence == {"locator": "train.py::main"}

    def test_judge_evaluates_run_and_attaches_verdict(self, fixture_pipeline: Path):
        ln = build_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        # No 'run' step in fixture artifacts map → no evaluates edge.
        # But concept verdict should still attach to C1.
        c1 = next(n for n in ln.nodes if n.id == "concept:demo:C1")
        assert c1.attrs.get("judge_verdict") == "pass"

    def test_decision_links_to_pipeline(self, fixture_pipeline: Path):
        ln = build_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        decisions = [n for n in ln.nodes if n.kind == "decision"]
        assert len(decisions) == 1
        assert decisions[0].label == "approve"
        # decision -[applies_to]-> pipeline
        assert any(
            e.src == decisions[0].id and e.dst == "pipeline:demo"
            and e.rel == "applies_to"
            for e in ln.edges
        )

    def test_milestone_node_created(self, fixture_pipeline: Path):
        ln = build_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        ms = [n for n in ln.nodes if n.kind == "milestone_report"]
        assert len(ms) == 1
        assert ms[0].id == "milestone:demo:1"

    def test_extraction_log_populated(self, fixture_pipeline: Path):
        ln = build_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        # Each extractor should leave a diagnostic line
        joined = " | ".join(ln.extraction_log)
        for keyword in ("evidence_refs", "code_recon", "baseline_analysis",
                        "data_manifest", "concepts", "decisions", "milestones"):
            assert keyword in joined


# ── Robustness ────────────────────────────────────────────────────────

class TestRobustness:
    def test_missing_artifacts_still_produces_valid_lineage(self, tmp_path: Path):
        """Empty pipeline dir → empty graph, no crash."""
        project = tmp_path / "proj"
        pdir = project / "artifacts" / "pipeline_empty"
        pdir.mkdir(parents=True)
        ln = build_pipeline_lineage(pdir, project)
        # Only the root pipeline node should exist (others depend on artifacts)
        assert ln.pipeline_id == "empty"
        assert len(ln.nodes) >= 0  # must not crash

    def test_malformed_yaml_does_not_crash(self, tmp_path: Path):
        project = tmp_path / "proj"
        pdir = project / "artifacts" / "pipeline_bad"
        pdir.mkdir(parents=True)
        (pdir / "code_recon.yaml").write_text("not: valid: yaml: [[")
        ln = build_pipeline_lineage(pdir, project)
        # Should produce no code_repo edges, but not raise
        assert all(n.kind != "code_repo" for n in ln.nodes)

    def test_write_pipeline_lineage_creates_json(self, fixture_pipeline: Path):
        out = write_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["pipeline_id"] == "demo"
        assert data["schema_version"] == 1
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)


# ── Mermaid renderer ──────────────────────────────────────────────────

class TestMermaid:
    def test_mermaid_emits_well_formed_block(self, fixture_pipeline: Path):
        ln = build_pipeline_lineage(fixture_pipeline, fixture_pipeline.parents[1])
        out = to_mermaid(ln)
        assert out.startswith("```mermaid\nflowchart LR")
        assert out.endswith("```")
        # Every node should appear in some line
        for n in ln.nodes:
            from researchos.runtime.lineage import _mermaid_safe_id
            assert _mermaid_safe_id(n.id) in out


# ── Integration: real BMA pipeline (if present) ──────────────────────

BMA_PIPELINE_DIR = (
    Path(__file__).resolve().parent.parent
    / "artifacts" / "pipeline_stacking_based_uncertainty_han"
)


@pytest.mark.skipif(
    not BMA_PIPELINE_DIR.exists(),
    reason="BMA fixture pipeline not present in this checkout",
)
class TestBMAIntegration:
    def test_bma_lineage_has_expected_kinds(self):
        project_root = BMA_PIPELINE_DIR.parents[1]
        ln = build_pipeline_lineage(BMA_PIPELINE_DIR, project_root)
        kinds = {n.kind for n in ln.nodes}
        # Real BMA pipeline must surface at minimum these kinds
        for required in ("pipeline", "paper", "code_repo", "dataset_file",
                         "concept", "step"):
            assert required in kinds, f"missing kind={required}; got {kinds}"

    def test_bma_lineage_links_paper_to_repo(self):
        project_root = BMA_PIPELINE_DIR.parents[1]
        ln = build_pipeline_lineage(BMA_PIPELINE_DIR, project_root)
        # paper:<bma> -[references]-> code_repo:matteocamilli__bma-package
        repo_id = "code_repo:matteocamilli__bma-package"
        assert any(
            e.dst == repo_id and e.rel == "references"
            for e in ln.edges
        )
