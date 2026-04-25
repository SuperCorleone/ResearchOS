"""Pipeline lineage builder — joins existing artifacts into a graph view.

This is a *derived* document. Every fact already exists somewhere on disk;
this module just resolves cross-references and writes them as a single
``lineage.json`` per pipeline.

Design constraints:

- **Pure read-only over artifacts** (plus one write: lineage.json itself).
- **Never raises** — each extractor is wrapped in try/except. Missing
  artifacts → fewer edges, never a crash. Diagnostics go into
  ``PipelineLineage.extraction_log``.
- **Idempotent** — same input artifacts → same output graph (modulo
  generated_at timestamp).
- **Stable IDs** so a future Layer 3 (project-wide graph) can merge
  per-pipeline lineages without renaming nodes.

Hooked into ``scheduler.py`` after every step completes so the graph is
always current. The hook is best-effort — lineage failure must not block
the pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

import yaml

from researchos.schemas.lineage import (
    LineageEdge,
    LineageNode,
    PipelineLineage,
)

logger = logging.getLogger(__name__)


# ── ID helpers (stable across pipelines for cross-pipeline kinds) ─────

def _short_hash(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:n]


def _paper_id(path_or_url: str) -> str:
    """Stable paper id from PDF path/URL (filename normalised)."""
    name = Path(path_or_url).name.lower()
    return f"paper:{_short_hash(name)}"


def _wiki_id(wiki_path: Path) -> str:
    return f"wiki:{wiki_path.stem}"


def _code_repo_id(slug_or_url: str) -> str:
    """github.com/foo/bar or foo__bar → code_repo:foo__bar."""
    if "://" in slug_or_url:
        parts = [p for p in slug_or_url.rstrip("/").split("/") if p]
        if len(parts) >= 2:
            slug = f"{parts[-2]}__{parts[-1].removesuffix('.git')}"
        else:
            slug = slug_or_url.replace("/", "_")
    else:
        slug = slug_or_url
    return f"code_repo:{slug}"


def _dataset_file_id(rel_path: str, sha: str | None) -> str:
    """Prefer content hash; fall back to path hash for unhashed files."""
    if sha:
        return f"dataset_file:{sha[:12] if not sha.startswith('sha256:') else sha[7:19]}"
    return f"dataset_file:p{_short_hash(rel_path)}"


def _dataset_url_id(url: str) -> str:
    return f"dataset_url:{_short_hash(url)}"


def _concept_id(pipeline_id: str, cid: str) -> str:
    return f"concept:{pipeline_id}:{cid}"


def _step_id(pipeline_id: str, step: str) -> str:
    return f"step:{pipeline_id}:{step}"


def _decision_id(pipeline_id: str, fname: str) -> str:
    return f"decision:{pipeline_id}:{Path(fname).stem}"


def _milestone_id(pipeline_id: str, n: int) -> str:
    return f"milestone:{pipeline_id}:{n}"


def _pipeline_node_id(pipeline_id: str) -> str:
    return f"pipeline:{pipeline_id}"


# ── Safe-read helpers ────────────────────────────────────────────────

def _safe_yaml(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(errors="replace"))
    except Exception as exc:
        logger.debug("lineage: yaml load failed for %s: %s", path, exc)
        return None


def _safe_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(errors="replace"))
    except Exception as exc:
        logger.debug("lineage: json load failed for %s: %s", path, exc)
        return None


# ── Per-source extractors ────────────────────────────────────────────
# Each extractor takes (lineage, pdir, project_root) and mutates lineage.
# All MUST tolerate missing/malformed inputs silently.

def _extract_root_pipeline(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    state = _safe_json(pdir / "state.json") or {}
    label = state.get("title") or lineage.pipeline_id
    lineage.add_node(LineageNode(
        id=_pipeline_node_id(lineage.pipeline_id),
        kind="pipeline",
        label=label,
        attrs={
            "stage": state.get("stage", ""),
            "step": state.get("step", ""),
            "research_question": state.get("research_question", ""),
            "budget_used": state.get("budget_used", 0),
            "budget_allocated": state.get("budget_allocated", 0),
        },
    ))


def _extract_evidence_refs_and_wiki(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    state = _safe_json(pdir / "state.json") or {}
    refs = state.get("evidence_refs", []) or []
    wiki_root = project_root / "knowledge" / "wiki"

    # Build slug index of wiki pages once.
    wiki_pages: dict[str, Path] = {}
    if wiki_root.exists():
        for page in wiki_root.rglob("*.md"):
            wiki_pages[page.stem.lower()] = page

    pid_node = _pipeline_node_id(lineage.pipeline_id)
    seen_papers = 0

    for ref in refs:
        if isinstance(ref, str):
            por, role = ref, "unknown"
        elif isinstance(ref, dict):
            por = ref.get("path_or_url") or ref.get("path") or ""
            role = ref.get("role", "unknown")
        else:
            continue
        if not por:
            continue
        # Skip URLs here; they'll be handled by other extractors (code_recon
        # or as bare URL refs not tied to PDFs).
        if por.startswith(("http://", "https://", "git@")):
            continue

        node_id = _paper_id(por) if role != "proposal" else f"proposal:{lineage.pipeline_id}"
        kind = "proposal" if role == "proposal" else "paper"
        label = Path(por).stem
        attrs = {"path_or_url": por, "role": role}

        # Try to attach the matching wiki page.
        from researchos.runtime.knowledge_compiler import _slugify
        stem_slug = _slugify(Path(por).stem)
        wiki_match = next(
            (p for stem, p in wiki_pages.items()
             if stem.startswith(stem_slug) or stem_slug.startswith(stem)),
            None,
        )
        if wiki_match is not None:
            try:
                attrs["wiki"] = str(wiki_match.relative_to(project_root))
            except ValueError:
                attrs["wiki"] = str(wiki_match)

        lineage.add_node(LineageNode(
            id=node_id, kind=kind, label=label, attrs=attrs,
        ))
        # pipeline -[derived_from]-> paper / proposal
        lineage.add_edge(LineageEdge(
            src=pid_node, dst=node_id, rel="derived_from",
        ))
        seen_papers += 1

    lineage.extraction_log.append(
        f"evidence_refs: {seen_papers} paper/proposal nodes from state.json"
    )


def _extract_steps(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    state = _safe_json(pdir / "state.json") or {}
    artifacts_map = state.get("artifacts", {}) or {}
    pid = lineage.pipeline_id
    pid_node = _pipeline_node_id(pid)

    # Per-step artifact agent assignment from workflow_config.yaml is the
    # source of truth, but we tolerate its absence — fall back to "?" agent.
    step_agent: dict[str, str] = {}
    try:
        from researchos.runtime.workflow_loader import WorkflowConfig
        cfg = WorkflowConfig.load(project_root)
        for step_name in artifacts_map.keys():
            step = cfg.steps.get(step_name)
            if step:
                step_agent[step_name] = step.agent or "(python)"
    except Exception:
        pass

    prev_step_node: str | None = None
    # state.json::artifacts is insertion-ordered (Python 3.7+ dict), but the
    # actual sequence is encoded in workflow_config — use workflow ordering
    # when available so next_step edges are stable.
    try:
        from researchos.runtime.workflow_loader import WorkflowConfig as _WC
        cfg = _WC.load(project_root)
        sequenced = []
        for stage in cfg.stages.values():
            for s in stage.sequence:
                if s in artifacts_map and s not in sequenced:
                    sequenced.append(s)
        # append any artifacts not in sequence (e.g. legacy step names)
        for s in artifacts_map.keys():
            if s not in sequenced:
                sequenced.append(s)
    except Exception:
        sequenced = list(artifacts_map.keys())

    for step_name in sequenced:
        artifact_filename = artifacts_map.get(step_name)
        node_id = _step_id(pid, step_name)
        attrs = {
            "agent": step_agent.get(step_name, "?"),
            "artifact": artifact_filename or "",
        }
        # If state.json indicates the pipeline is currently at this step,
        # tag accordingly so renderers can highlight it.
        if state.get("step") == step_name:
            attrs["current"] = True

        lineage.add_node(LineageNode(
            id=node_id, kind="step", label=step_name, attrs=attrs,
        ))
        # step -[produces]-> artifact (we model artifacts as attribute, not
        # separate node, to keep the graph manageable)
        if prev_step_node is not None:
            lineage.add_edge(LineageEdge(
                src=prev_step_node, dst=node_id, rel="next_step",
            ))
        prev_step_node = node_id

    lineage.extraction_log.append(f"steps: {len(sequenced)} step nodes")


def _extract_code_recon(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    recon = _safe_yaml(pdir / "code_recon.yaml")
    if not isinstance(recon, dict):
        return
    pid = lineage.pipeline_id
    repos_added = 0

    # If any paper nodes already exist, link them to the repos via
    # `references` — this captures the "this paper has a code repo" relation
    # surfaced by extract_code_refs.
    paper_nodes = [n for n in lineage.nodes if n.kind == "paper"]

    for repo in recon.get("repos", []) or []:
        if not isinstance(repo, dict):
            continue
        slug = repo.get("slug") or _short_hash(repo.get("url", ""))
        node_id = _code_repo_id(slug)
        attrs = {
            "url": repo.get("url", ""),
            "slug": slug,
            "status": repo.get("status", "unknown"),
            "clone_path": repo.get("path", ""),
            "commit_sha": repo.get("commit_sha", ""),
            "license": repo.get("license", ""),
            "entry_points": repo.get("entry_points", []),
        }
        lineage.add_node(LineageNode(
            id=node_id, kind="code_repo",
            label=slug.split("__")[-1] if "__" in slug else slug,
            attrs=attrs,
        ))
        # CODE_RECON step -[produces]-> code_repo (logical sense)
        step_recon = _step_id(pid, "code_recon")
        if step_recon in lineage.node_ids():
            lineage.add_edge(LineageEdge(
                src=step_recon, dst=node_id, rel="produces",
            ))
        # Each evidence paper -[references]-> repo (heuristic: all papers
        # in this pipeline are candidate referrers; tighter binding would
        # require parsing each paper's wiki frontmatter individually).
        for paper in paper_nodes:
            lineage.add_edge(LineageEdge(
                src=paper.id, dst=node_id, rel="references",
            ))
        repos_added += 1

    lineage.extraction_log.append(f"code_recon: {repos_added} code_repo nodes")


def _extract_baseline_analysis(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    ba = _safe_yaml(pdir / "baseline_analysis.yaml")
    if not isinstance(ba, dict):
        return
    pid = lineage.pipeline_id
    step_ba = _step_id(pid, "baseline_analyze")
    datasets_seen = 0
    urls_seen = 0

    for repo_block in ba.get("analyzed_repos", []) or []:
        if not isinstance(repo_block, dict):
            continue
        repo_slug = repo_block.get("slug", "")
        repo_node_id = _code_repo_id(repo_slug)
        # repo -[ships]-> dataset_file for every dataset_paths entry
        for ds in repo_block.get("dataset_paths", []) or []:
            if not isinstance(ds, dict):
                continue
            rel = ds.get("path", "")
            if not rel:
                continue
            full_rel_path = f"{repo_slug}/{rel}"
            ds_node_id = _dataset_file_id(full_rel_path, None)
            lineage.add_node(LineageNode(
                id=ds_node_id, kind="dataset_file",
                label=Path(rel).name,
                attrs={
                    "repo_slug": repo_slug,
                    "rel_path": rel,
                    "kind": ds.get("kind", "unknown"),
                    "purpose": ds.get("purpose", ""),
                },
            ))
            lineage.add_edge(LineageEdge(
                src=repo_node_id, dst=ds_node_id, rel="ships",
            ))
            # baseline_analyze -[selects]-> dataset_file (with code evidence)
            evidence = None
            for call in repo_block.get("code_data_calls", []) or []:
                if isinstance(call, dict) and call.get("resolved_path") == rel:
                    evidence = {
                        "file": call.get("file", ""),
                        "lineno": call.get("lineno", 0),
                        "call": call.get("call_text", ""),
                    }
                    break
            if step_ba in lineage.node_ids():
                lineage.add_edge(LineageEdge(
                    src=step_ba, dst=ds_node_id, rel="selects",
                    evidence=evidence,
                ))
            datasets_seen += 1

    for ext in ba.get("external_data_urls", []) or []:
        if not isinstance(ext, dict):
            continue
        url = ext.get("url", "")
        if not url:
            continue
        url_id = _dataset_url_id(url)
        lineage.add_node(LineageNode(
            id=url_id, kind="dataset_url", label=Path(url).name or url,
            attrs={
                "url": url,
                "should_download": ext.get("should_download", False),
                "kind": ext.get("kind", "other"),
                "reason": ext.get("reason", ""),
            },
        ))
        if step_ba in lineage.node_ids():
            rel = "selects" if ext.get("should_download") else "selects"
            lineage.add_edge(LineageEdge(
                src=step_ba, dst=url_id, rel=rel,
                evidence={"decision": "download" if ext.get("should_download") else "skip"},
            ))
        urls_seen += 1

    lineage.extraction_log.append(
        f"baseline_analysis: {datasets_seen} dataset_file + {urls_seen} dataset_url"
    )


def _extract_data_manifest(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    mf = _safe_yaml(pdir / "data_manifest.yaml")
    if not isinstance(mf, dict):
        return
    pid = lineage.pipeline_id
    step_dr = _step_id(pid, "data_recon")
    files_seen = 0

    for f in mf.get("files", []) or []:
        if not isinstance(f, dict):
            continue
        rel = f.get("path", "")
        if not rel:
            continue
        sha = f.get("sha256", "")
        ds_node_id = _dataset_file_id(rel, sha)
        lineage.add_node(LineageNode(
            id=ds_node_id, kind="dataset_file",
            label=Path(rel).name,
            attrs={
                "sha256": sha,
                "size_bytes": f.get("size_bytes", 0),
                "rows": f.get("rows"),
                "rel_path": rel,
                "downloaded_to": f"data/raw/{pid}/{rel}",
            },
        ))
        if step_dr in lineage.node_ids():
            lineage.add_edge(LineageEdge(
                src=step_dr, dst=ds_node_id, rel="produces",
            ))
        files_seen += 1

    lineage.extraction_log.append(
        f"data_manifest: {files_seen} dataset_file nodes (with sha256)"
    )


def _extract_concepts(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    raw = _safe_yaml(pdir / "concepts.yaml")
    if not raw:
        return
    if isinstance(raw, dict):
        concepts = raw.get("concepts", []) or []
    elif isinstance(raw, list):
        concepts = raw
    else:
        return

    pid = lineage.pipeline_id
    step_concept = _step_id(pid, "concept_extract")
    paper_nodes = {n.attrs.get("path_or_url", "").lower(): n.id
                   for n in lineage.nodes if n.kind == "paper"}
    paper_label_index = {n.label.lower(): n.id
                         for n in lineage.nodes if n.kind == "paper"}
    seen = 0

    for c in concepts:
        if not isinstance(c, dict):
            continue
        cid = c.get("id") or f"C{seen+1}"
        node_id = _concept_id(pid, cid)
        lineage.add_node(LineageNode(
            id=node_id, kind="concept", label=cid,
            attrs={
                "definition": (c.get("definition") or "").strip()[:200],
                "math_formula": (c.get("math_formula") or "").strip()[:120],
                "code_implementation": c.get("code_implementation", ""),
            },
        ))
        if step_concept in lineage.node_ids():
            lineage.add_edge(LineageEdge(
                src=step_concept, dst=node_id, rel="produces",
            ))
        # concept -[defined_in]-> paper (best-effort: substring match on title)
        for ref in c.get("reference_papers", []) or []:
            if not isinstance(ref, str):
                continue
            ref_low = ref.lower()
            paper_id_match = None
            for k, v in paper_nodes.items():
                if Path(k).stem in ref_low or ref_low in k:
                    paper_id_match = v
                    break
            if paper_id_match is None:
                for k, v in paper_label_index.items():
                    if k in ref_low or ref_low in k:
                        paper_id_match = v
                        break
            if paper_id_match:
                lineage.add_edge(LineageEdge(
                    src=node_id, dst=paper_id_match, rel="defined_in",
                ))
        # concept -[implemented_in]-> code locator (only when not NOT_AVAILABLE/empty)
        impl = (c.get("code_implementation") or "").strip()
        if impl and impl not in ("NOT_AVAILABLE",):
            # The locator is a string (not a separate node) — encode as edge
            # evidence; we don't create one node per locator to keep graph
            # density reasonable.
            lineage.add_edge(LineageEdge(
                src=node_id,
                # Synthetic dst: locator text becomes its own pseudo-node id.
                dst=f"code_locator:{_short_hash(impl)}",
                rel="implemented_in",
                evidence={"locator": impl},
            ))
        seen += 1

    lineage.extraction_log.append(f"concepts: {seen} concept nodes")


def _extract_judge(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    jv = _safe_json(pdir / "judge_verdict.json")
    if not isinstance(jv, dict):
        return
    pid = lineage.pipeline_id
    step_judge = _step_id(pid, "judge")
    step_run = _step_id(pid, "run")
    if step_judge in lineage.node_ids() and step_run in lineage.node_ids():
        lineage.add_edge(LineageEdge(
            src=step_judge, dst=step_run, rel="evaluates",
        ))
    # per_concept verdicts → concept nodes
    per = jv.get("per_concept", {}) or {}
    if isinstance(per, dict):
        for cid, verdict in per.items():
            cn_id = _concept_id(pid, cid)
            if cn_id in lineage.node_ids():
                # Update concept attrs with verdict
                for n in lineage.nodes:
                    if n.id == cn_id:
                        n.attrs["judge_verdict"] = verdict
                        break
    lineage.extraction_log.append("judge: linked judge→run + concept verdicts")


def _extract_decisions(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    pid = lineage.pipeline_id
    pid_node = _pipeline_node_id(pid)
    decisions_dir = project_root / "decisions"
    if not decisions_dir.exists():
        return
    seen = 0
    for f in decisions_dir.iterdir():
        if not f.is_file():
            continue
        if pid not in f.name:
            continue
        if not f.suffix in (".yaml", ".yml"):
            continue
        d = _safe_yaml(f)
        if not isinstance(d, dict):
            continue
        if d.get("pipeline_id") not in (pid, None):
            continue
        node_id = _decision_id(pid, f.name)
        lineage.add_node(LineageNode(
            id=node_id, kind="decision", label=d.get("decision", "?"),
            attrs={
                "verdict": d.get("decision", ""),
                "comment": (d.get("comment") or "")[:300],
                "next_milestone": d.get("next_milestone", ""),
                "created_at": d.get("created_at", ""),
                "file": str(f.relative_to(project_root)),
            },
        ))
        lineage.add_edge(LineageEdge(
            src=node_id, dst=pid_node, rel="applies_to",
        ))
        seen += 1
    lineage.extraction_log.append(f"decisions: {seen} decision nodes")


def _extract_milestones(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    pid = lineage.pipeline_id
    pid_node = _pipeline_node_id(pid)
    pat = re.compile(r"^milestone_(\d+)\.md$")
    seen = 0
    for f in pdir.iterdir():
        m = pat.match(f.name)
        if not m:
            continue
        n = int(m.group(1))
        node_id = _milestone_id(pid, n)
        body_preview = ""
        try:
            body_preview = f.read_text(errors="replace")[:200].replace("\n", " ")
        except OSError:
            pass
        lineage.add_node(LineageNode(
            id=node_id, kind="milestone_report", label=f"milestone {n}",
            attrs={
                "path": str(f.relative_to(project_root)),
                "preview": body_preview,
            },
        ))
        lineage.add_edge(LineageEdge(
            src=pid_node, dst=node_id, rel="produces",
        ))
        seen += 1
    lineage.extraction_log.append(f"milestones: {seen} milestone_report nodes")


def _extract_paper_drafts(
    lineage: PipelineLineage, pdir: Path, project_root: Path,
) -> None:
    pid = lineage.pipeline_id
    pid_node = _pipeline_node_id(pid)
    paper_dir = project_root / "paper"
    if not paper_dir.exists():
        return
    seen = 0
    for f in paper_dir.iterdir():
        if not f.is_file():
            continue
        if pid not in f.name:
            continue
        node_id = f"paper_draft:{pid}:{f.stem}"
        lineage.add_node(LineageNode(
            id=node_id, kind="paper_draft", label=f.name,
            attrs={"path": str(f.relative_to(project_root))},
        ))
        lineage.add_edge(LineageEdge(
            src=pid_node, dst=node_id, rel="produces",
        ))
        seen += 1
    lineage.extraction_log.append(f"paper_drafts: {seen} paper_draft nodes")


# ── Top-level builder ────────────────────────────────────────────────

EXTRACTORS = (
    _extract_root_pipeline,
    _extract_evidence_refs_and_wiki,
    _extract_steps,
    _extract_code_recon,
    _extract_baseline_analysis,
    _extract_data_manifest,
    _extract_concepts,
    _extract_judge,
    _extract_decisions,
    _extract_milestones,
    _extract_paper_drafts,
)


def build_pipeline_lineage(
    pdir: Path, project_root: Path,
) -> PipelineLineage:
    """Build the lineage graph for one pipeline by reading existing artifacts.

    Always returns a valid PipelineLineage (possibly with empty nodes/edges
    if nothing was readable). Each extractor is independent — one's failure
    does not stop the others. Diagnostics land in ``extraction_log``.
    """
    pipeline_id = pdir.name.removeprefix("pipeline_")
    lineage = PipelineLineage(pipeline_id=pipeline_id)
    for extractor in EXTRACTORS:
        try:
            extractor(lineage, pdir, project_root)
        except Exception as exc:
            logger.exception("lineage extractor %s failed", extractor.__name__)
            lineage.extraction_log.append(
                f"{extractor.__name__}: ERROR {exc}"
            )
    return lineage


def write_pipeline_lineage(
    pdir: Path, project_root: Path,
) -> Path:
    """Build + serialise to ``artifacts/pipeline_<id>/lineage.json``.

    Wrapped in try/except by callers that must never fail (the scheduler
    post-step hook).
    """
    lineage = build_pipeline_lineage(pdir, project_root)
    out_path = pdir / "lineage.json"
    out_path.write_text(json.dumps(lineage.model_dump(), indent=2))
    return out_path


# ── Mermaid renderer (Layer 4 will use this in CLI) ──────────────────

_MERMAID_KIND_SHAPES: dict[str, tuple[str, str]] = {
    # kind → (open, close) bracket pair for Mermaid node shape
    "pipeline":         ("[[", "]]"),  # subroutine
    "paper":            ("[(", ")]"),  # cylinder
    "wiki_page":        ("[(", ")]"),
    "proposal":         ("[(", ")]"),
    "code_repo":        ("[/", "/]"),  # parallelogram
    "dataset_file":     ("[/", "/]"),
    "dataset_url":      ("[/", "/]"),
    "concept":          ("((", "))"),  # circle
    "step":             ("[", "]"),    # square
    "artifact":         ("[", "]"),
    "decision":         ("{{", "}}"),  # hexagon
    "milestone_report": ("[", "]"),
    "paper_draft":      ("[", "]"),
}


def _mermaid_safe_id(node_id: str) -> str:
    """Turn 'paper:abc' into 'paper_abc' — Mermaid needs alphanumeric ids."""
    return re.sub(r"[^A-Za-z0-9_]", "_", node_id)


def _mermaid_escape_label(label: str) -> str:
    return label.replace('"', '\\"').replace("\n", " ")[:60]


def to_mermaid(lineage: PipelineLineage) -> str:
    """Render lineage as a Mermaid flowchart code block."""
    lines = ["```mermaid", "flowchart LR"]
    # Group nodes by kind for visual clarity (Mermaid renders subgraphs).
    by_kind: dict[str, list] = {}
    for n in lineage.nodes:
        by_kind.setdefault(n.kind, []).append(n)

    # Emit nodes
    for kind, nodes in by_kind.items():
        for n in nodes:
            mid = _mermaid_safe_id(n.id)
            open_b, close_b = _MERMAID_KIND_SHAPES.get(kind, ("[", "]"))
            label = _mermaid_escape_label(n.label)
            current_marker = " ★" if n.attrs.get("current") else ""
            lines.append(f'    {mid}{open_b}"{kind}: {label}{current_marker}"{close_b}')

    # Emit edges
    for e in lineage.edges:
        src = _mermaid_safe_id(e.src)
        dst = _mermaid_safe_id(e.dst)
        rel_label = e.rel
        if e.evidence:
            # Pick one short evidence field for the label
            for k in ("file", "decision", "locator"):
                if k in e.evidence and e.evidence[k]:
                    rel_label = f"{e.rel} ({e.evidence[k]})"
                    break
        lines.append(f"    {src} -.{rel_label}.-> {dst}")

    lines.append("```")
    return "\n".join(lines)


__all__ = [
    "build_pipeline_lineage",
    "write_pipeline_lineage",
    "to_mermaid",
]
