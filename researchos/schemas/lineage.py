"""Pipeline lineage graph — derived view linking every research artifact.

Layer 2 of the v5.3 lineage system. ``PipelineLineage`` is a *derived* document
rebuilt from existing artifacts (state.json, paper_spec.yaml,
baseline_analysis.yaml, …) every time a pipeline step completes. It is **never
the source of truth** for any individual fact — it just joins them.

Node IDs are designed to be stable across pipelines (paper hashes, GitHub
slugs) so a future Layer 3 (project-wide ``research_graph.json``) can merge
per-pipeline lineages without renaming.

Pipeline-local nodes (steps, concepts, decisions) carry a ``pipeline:<id>:``
prefix to keep them disambiguated when later merged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


# Node kind taxonomy. Add cautiously — adding a kind requires updating the
# extractor + Mermaid renderer.
NodeKind = Literal[
    "pipeline",            # the pipeline itself (root node)
    "paper",               # input PDF in knowledge/raw + its wiki page
    "wiki_page",           # standalone wiki entry not tied to a single paper
    "proposal",            # user proposal text
    "code_repo",           # cloned reference GitHub repo
    "dataset_file",        # one data file with sha256
    "dataset_url",         # external URL (downloaded or skipped per analyser)
    "concept",             # atomic concept from concepts.yaml
    "step",                # one pipeline step (with status + agent)
    "artifact",            # a step's output artifact (yaml/json/md)
    "decision",            # Professor decision file
    "milestone_report",    # milestone_N.md
    "paper_draft",         # final paper artefact (paper/<id>.tex etc.)
]


# Edge relation taxonomy. Direction is src --rel--> dst. Add cautiously.
RelKind = Literal[
    "consumes",            # step --consumes--> artifact (read input)
    "produces",            # step --produces--> artifact (wrote output)
    "derived_from",        # artifact --derived_from--> source
    "references",          # paper --references--> code_repo (URL in paper)
    "ships",               # code_repo --ships--> dataset_file (in repo subdir)
    "selects",             # baseline_analyze --selects--> dataset_file
                           # (with evidence: which IO call grounded the choice)
    "defined_in",          # concept --defined_in--> paper
    "implemented_in",      # concept --implemented_in--> code locator
    "next_step",           # step --next_step--> step (workflow ordering)
    "applies_to",          # decision --applies_to--> pipeline
    "evaluates",           # judge --evaluates--> step:run
    "cites",               # milestone --cites--> concept | result
    "tested_with",         # step:run --tested_with--> dataset_file
]


class LineageNode(BaseModel):
    """A vertex in the pipeline lineage graph."""

    id: str = Field(
        description="Stable identifier. Cross-pipeline kinds use content "
        "hashes (paper:<sha[:12]>, code_repo:<owner>__<repo>); pipeline-local "
        "kinds use prefix (concept:<pid>:<cid>, step:<pid>:<step>)."
    )
    kind: NodeKind
    label: str = Field(
        description="Human-readable short label for rendering (e.g. 'Taming BMA paper')."
    )
    attrs: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form bag of node-specific fields: path, sha256, "
        "size_bytes, status, agent, verdict, …",
    )


class LineageEdge(BaseModel):
    """A directed edge linking two nodes in the lineage graph."""

    src: str = Field(description="Source node id")
    dst: str = Field(description="Destination node id")
    rel: RelKind
    evidence: dict[str, Any] | None = Field(
        default=None,
        description="Optional grounding for the edge — e.g. for 'selects': "
        "{file: bma_adaptation.py, lineno: 87, call: \"pd.read_csv(...)\"}.",
    )


class PipelineLineage(BaseModel):
    """Derived lineage view for one pipeline.

    Rebuilt by ``researchos.runtime.lineage.build_pipeline_lineage`` after
    every step completes. Persisted to ``artifacts/pipeline_<id>/lineage.json``.

    Idempotent: same input artifacts → same output. Each rebuild fully
    overwrites the previous file (no diff merge).
    """

    pipeline_id: str
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 timestamp of this rebuild.",
    )
    schema_version: int = 1
    nodes: list[LineageNode] = Field(default_factory=list)
    edges: list[LineageEdge] = Field(default_factory=list)
    extraction_log: list[str] = Field(
        default_factory=list,
        description="Per-extractor diagnostics (e.g. 'paper_spec.yaml: 7 quotes, "
        "3 RQs'). Useful when debugging missing edges.",
    )

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}

    def add_node(self, node: LineageNode) -> None:
        """Add a node, deduplicating by id (last write wins for attrs)."""
        for i, existing in enumerate(self.nodes):
            if existing.id == node.id:
                # Merge attrs — extractor order shouldn't matter.
                merged_attrs = {**existing.attrs, **node.attrs}
                self.nodes[i] = LineageNode(
                    id=node.id, kind=node.kind, label=node.label,
                    attrs=merged_attrs,
                )
                return
        self.nodes.append(node)

    def add_edge(self, edge: LineageEdge) -> None:
        """Add an edge, deduplicating exact-match (src, dst, rel) tuples."""
        for existing in self.edges:
            if (existing.src == edge.src and existing.dst == edge.dst
                    and existing.rel == edge.rel):
                # Same-relation duplicate — merge evidence if newer adds it.
                if edge.evidence and not existing.evidence:
                    existing.evidence = edge.evidence
                return
        self.edges.append(edge)


__all__ = [
    "NodeKind",
    "RelKind",
    "LineageNode",
    "LineageEdge",
    "PipelineLineage",
]
