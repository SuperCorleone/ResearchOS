"""Branch manager -- v2 tree-search primitives for the MAIN stage.

This module hosts two complementary abstractions for the MAIN-stage
tree-search flow:

* The low-level BFTS primitives (``Node``, ``Journal``, ``BFTSConfig``,
  ``select_parallel_nodes``, ``stage_complete``, ``JournalPersistence``)
  adapted from Sakana AI-Scientist-v2's tree-search journal.  These are
  reused across tree-search modes.
* A thin, scheduler-facing ``BranchRunner`` that drives the K-parallel
  DESIGN -> ... -> CRITIC branching paradigm by spawning K draft
  ``Node`` instances on top of a shared ``Journal``.  BranchRunner is the
  sole replacement for the legacy ``BranchManager`` / ``BranchState``
  objects that previously lived at the bottom of this file.

Design goals
------------
* Single source of truth: all branching state lives in the journal
  (``artifacts/pipeline_<id>/journal.json``).  No parallel bookkeeping.
* On-disk layout unchanged: one ``branch_<n>/`` sub-directory per draft
  node keeps existing agent prompts / artifacts working.
* Winner selection goes through ``Journal.get_best_node`` with an
  optional LLM tiebreaker -- so the scheduler can still hand off to the
  ``branch_selector`` agent when no numeric metric dominates.
"""

from __future__ import annotations

import json
import logging
import random
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from researchos.schemas.pipeline import PipelineStep

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# The five BFTS stages a Node can live in.
NodeStage = Literal["draft", "debug", "improve", "hyperparam", "ablation"]


# ---------------------------------------------------------------------------
# Flat Node dataclass (AI-Scientist-v2 journal.Node equivalent)
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """A single tree-search candidate.

    Flat by design: no subclasses, no nested dataclasses -- this allows the
    whole journal to be serialised to ``journal.json`` via ``asdict`` and
    reloaded without bespoke deserialisers.
    """

    id: str
    parent_id: str | None = None
    stage: NodeStage = "draft"
    plan: str = ""
    code: str = ""
    exec_result: dict[str, Any] = field(default_factory=dict)
    plot_analyses: dict[str, Any] = field(default_factory=dict)
    metric: dict[str, Any] = field(default_factory=dict)
    is_buggy: bool = False
    is_buggy_plots: bool = False
    is_seed_node: bool = False
    is_seed_agg_node: bool = False
    ablation_name: str | None = None
    hyperparam_name: str | None = None
    created_at: str = field(default_factory=_now_iso)
    # Extras used by the scheduler to thread back to its own pipeline state.
    pipeline_id: str | None = None
    iter_index: int = 0

    def is_good(self) -> bool:
        """A node is good if it ran successfully (not buggy, not buggy plots)."""
        return not self.is_buggy and not self.is_buggy_plots and bool(self.metric)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# BFTS configuration
# ---------------------------------------------------------------------------


@dataclass
class BFTSConfig:
    """Tree-search knobs loaded from ``runtime/tree_search_defaults.yaml``."""

    num_workers: int = 4
    num_drafts: int = 3
    debug_prob: float = 0.5
    max_debug_depth: int = 3
    num_seeds: int = 3
    exec_timeout: int = 3600
    stage1_max_iters: int = 20
    stage2_max_iters: int = 12
    stage3_max_iters: int = 12
    stage4_max_iters: int = 18

    @classmethod
    def load(cls, project_root: Path) -> BFTSConfig:
        path = project_root / "runtime" / "tree_search_defaults.yaml"
        if not path.exists():
            return cls()
        try:
            import yaml as _yaml

            raw = _yaml.safe_load(path.read_text()) or {}
        except Exception:
            logger.warning("Failed to parse %s; using BFTS defaults", path, exc_info=True)
            return cls()

        stages = raw.get("stages", {}) or {}
        exec_cfg = raw.get("exec", {}) or {}
        seed_cfg = raw.get("multi_seed_eval", {}) or {}

        return cls(
            num_workers=int(raw.get("num_workers", 4)),
            num_drafts=int(raw.get("num_drafts", 3)),
            debug_prob=float(raw.get("debug_prob", 0.5)),
            max_debug_depth=int(raw.get("max_debug_depth", 3)),
            num_seeds=int(seed_cfg.get("num_seeds", 3)),
            exec_timeout=int(exec_cfg.get("timeout", 3600)),
            stage1_max_iters=int(stages.get("stage1_max_iters", 20)),
            stage2_max_iters=int(stages.get("stage2_max_iters", 12)),
            stage3_max_iters=int(stages.get("stage3_max_iters", 12)),
            stage4_max_iters=int(stages.get("stage4_max_iters", 18)),
        )

    def max_iters_for_stage(self, stage: str) -> int:
        return {
            "stage1": self.stage1_max_iters,
            "stage2": self.stage2_max_iters,
            "stage3": self.stage3_max_iters,
            "stage4": self.stage4_max_iters,
        }.get(stage, self.stage1_max_iters)


# ---------------------------------------------------------------------------
# Journal -- wrapper around list[Node]
# ---------------------------------------------------------------------------


class Journal:
    """Ordered collection of Nodes with structured filters."""

    def __init__(self, nodes: list[Node] | None = None) -> None:
        self.nodes: list[Node] = list(nodes) if nodes else []

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self):
        return iter(self.nodes)

    def append(self, node: Node) -> Node:
        self.nodes.append(node)
        return node

    def get(self, node_id: str) -> Node | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    @property
    def draft_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.stage == "draft"]

    @property
    def buggy_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.is_buggy]

    @property
    def good_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.is_good()]

    def buggy_leaves(self) -> list[Node]:
        parent_ids = {n.parent_id for n in self.nodes if n.parent_id}
        return [n for n in self.buggy_nodes if n.id not in parent_ids]

    def get_best_node(
        self,
        metric_key: str | None = None,
        higher_is_better: bool = True,
        llm_tiebreaker: Any = None,
    ) -> Node | None:
        """Return the highest-quality good node (tier-1 metric, tier-2 LLM)."""
        candidates = self.good_nodes
        if not candidates:
            return None

        if metric_key is None:
            shared: set[str] | None = None
            for n in candidates:
                keys = {
                    k for k, v in n.metric.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                }
                shared = keys if shared is None else (shared & keys)
            if shared and len(shared) == 1:
                metric_key = next(iter(shared))

        best: Node | None = None
        ties: list[Node] = []
        if metric_key:
            for n in candidates:
                v = n.metric.get(metric_key)
                if not isinstance(v, (int, float)):
                    continue
                if best is None:
                    best, ties = n, [n]
                    continue
                cur = best.metric.get(metric_key)
                if not isinstance(cur, (int, float)):
                    best, ties = n, [n]
                    continue
                if higher_is_better:
                    if v > cur:
                        best, ties = n, [n]
                    elif v == cur:
                        ties.append(n)
                else:
                    if v < cur:
                        best, ties = n, [n]
                    elif v == cur:
                        ties.append(n)

        if best is None:
            candidates_sorted = sorted(candidates, key=lambda n: n.created_at)
            best = candidates_sorted[-1]
            ties = [best]

        if llm_tiebreaker and len(ties) > 1:
            try:
                picked = llm_tiebreaker(ties)
                if isinstance(picked, Node):
                    return picked
                if isinstance(picked, str):
                    for n in ties:
                        if n.id == picked:
                            return n
            except Exception:
                logger.warning("LLM tiebreaker failed; using tier-1 result", exc_info=True)

        return best

    def to_dict(self) -> dict[str, Any]:
        return {"nodes": [n.to_dict() for n in self.nodes]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Journal:
        raw_nodes = data.get("nodes", []) if isinstance(data, dict) else []
        return cls([Node.from_dict(n) for n in raw_nodes if isinstance(n, dict)])


# ---------------------------------------------------------------------------
# Tree-search policy helpers
# ---------------------------------------------------------------------------


def debug_depth(node: Node, journal: Journal) -> int:
    """Consecutive buggy ancestors walking up the parent chain (inclusive)."""
    depth = 0
    current: Node | None = node
    visited: set[str] = set()
    while current is not None and current.is_buggy:
        if current.id in visited:
            break
        visited.add(current.id)
        depth += 1
        if not current.parent_id:
            break
        current = journal.get(current.parent_id)
    return depth


def select_parallel_nodes(
    journal: Journal,
    cfg: BFTSConfig,
    *,
    rng: random.Random | None = None,
    metric_key: str | None = None,
    higher_is_better: bool = True,
) -> list[Node]:
    """BFTS parallel-selection policy.

    Emits up to ``cfg.num_workers`` parent nodes from which new children
    should be spawned this tick.  Synthetic draft slots carry
    ``id == "__draft__"`` so callers can distinguish them from real
    parents.
    """
    rng = rng or random.Random()
    selected: list[Node] = []

    for _ in range(max(1, cfg.num_workers)):
        if len(journal.draft_nodes) + sum(
            1 for s in selected if s.id == "__draft__"
        ) < cfg.num_drafts:
            selected.append(
                Node(id="__draft__", parent_id=None, stage="draft", plan="", code="")
            )
            continue

        debuggable = [
            n for n in journal.buggy_leaves()
            if debug_depth(n, journal) <= cfg.max_debug_depth
        ]
        if debuggable and rng.random() < cfg.debug_prob:
            selected.append(rng.choice(debuggable))
            continue

        best = journal.get_best_node(
            metric_key=metric_key, higher_is_better=higher_is_better
        )
        if best is not None:
            selected.append(best)
            continue

        selected.append(
            Node(id="__draft__", parent_id=None, stage="draft", plan="", code="")
        )

    return selected


def stage_complete(journal: Journal, stage: str, cfg: BFTSConfig) -> bool:
    """Per-stage stopping criterion for the BFTS orchestrator."""
    if len(journal.nodes) == 0:
        return False

    if stage == "stage1":
        return len(journal.good_nodes) > 0

    if stage == "stage2":
        best = journal.get_best_node()
        if best is None:
            return False
        differs = best.id != journal.nodes[0].id
        vlm_stable = not best.is_buggy_plots
        return differs and vlm_stable

    if stage == "stage3":
        best = journal.get_best_node()
        if best is None:
            return False
        prior_best_id = next(
            (n.id for n in reversed(journal.nodes[:-1]) if n.is_good()),
            None,
        )
        if prior_best_id is None:
            return False
        return best.id != prior_best_id

    if stage == "stage4":
        return len(journal.nodes) >= cfg.max_iters_for_stage("stage4")

    return False


# ---------------------------------------------------------------------------
# Journal persistence
# ---------------------------------------------------------------------------


class JournalPersistence:
    """Save/load Journal objects to ``artifacts/pipeline_<id>/journal.json``."""

    @staticmethod
    def path_for(pipeline_id: str, artifacts_root: Path) -> Path:
        return Path(artifacts_root) / f"pipeline_{pipeline_id}" / "journal.json"

    @classmethod
    def save(
        cls, pipeline_id: str, journal: Journal, artifacts_root: Path
    ) -> Path:
        out = cls.path_for(pipeline_id, artifacts_root)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pipeline_id": pipeline_id,
            "saved_at": _now_iso(),
            **journal.to_dict(),
        }
        out.write_text(json.dumps(payload, indent=2, sort_keys=False))
        return out

    @classmethod
    def load(cls, pipeline_id: str, artifacts_root: Path) -> Journal:
        p = cls.path_for(pipeline_id, artifacts_root)
        if not p.exists():
            return Journal()
        try:
            data = json.loads(p.read_text())
        except Exception:
            logger.warning("Failed to parse journal at %s", p, exc_info=True)
            return Journal()
        return Journal.from_dict(data)


# ---------------------------------------------------------------------------
# BranchRunner -- K-parallel branching on top of Journal/Node
# ---------------------------------------------------------------------------


# Marker plan values are stashed on the Node's ``plan`` field so we can keep
# per-branch pipeline-step state without inflating the dataclass further.
_STATUS_KEY = "status"
_STEP_KEY = "step"
_SELECTION_FILE = "branch_selection.json"


class BranchRunner:
    """Drives K parallel draft branches backed by a single Journal.

    On creation the runner exposes the shared ``Journal`` under
    ``artifacts/pipeline_<id>/journal.json``; K draft ``Node`` instances
    are seeded from the initial hypothesis.  Each branch tracks its
    current ``PipelineStep`` inside its ``Node.exec_result`` so the
    scheduler can drive DESIGN -> ... -> CRITIC independently per branch.

    The on-disk ``branch_<n>/`` layout is preserved for backwards
    compatibility with existing agent prompts that copy artifacts into
    per-branch sub-directories.
    """

    def __init__(
        self,
        pipeline_id: str,
        artifacts_root: Path,
        num_branches: int = 3,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.artifacts_root = Path(artifacts_root)
        self.num_branches = max(1, int(num_branches))
        self._journal: Journal | None = None

    # ── Paths ─────────────────────────────────────────────────────

    @property
    def pipeline_dir(self) -> Path:
        return self.artifacts_root / f"pipeline_{self.pipeline_id}"

    def branch_dir(self, branch_id: int) -> Path:
        return self.pipeline_dir / f"branch_{branch_id}"

    # ── Journal (lazy) ─────────────────────────────────────────────

    def load(self) -> Journal:
        """Load (or initialise) the backing journal from disk."""
        if self._journal is None:
            self._journal = JournalPersistence.load(
                self.pipeline_id, self.artifacts_root
            )
        return self._journal

    def _save(self) -> None:
        if self._journal is None:
            return
        JournalPersistence.save(
            self.pipeline_id, self._journal, self.artifacts_root
        )

    # ── Branch bookkeeping helpers ───────────────────────────────

    @staticmethod
    def _is_branch_node(node: Node) -> bool:
        return node.stage == "draft" and _STEP_KEY in (node.exec_result or {})

    def branch_nodes(self) -> list[Node]:
        """Ordered list of draft nodes that represent current branches."""
        journal = self.load()
        return sorted(
            (n for n in journal.nodes if self._is_branch_node(n)),
            key=lambda n: n.iter_index,
        )

    def _branch_by_index(self, branch_id: int) -> Node | None:
        for n in self.branch_nodes():
            if n.iter_index == branch_id:
                return n
        return None

    @staticmethod
    def _get_status(node: Node) -> str:
        return (node.exec_result or {}).get(_STATUS_KEY, "active")

    @staticmethod
    def _get_step(node: Node) -> PipelineStep | None:
        raw = (node.exec_result or {}).get(_STEP_KEY)
        if not raw:
            return None
        try:
            return PipelineStep(raw)
        except ValueError:
            return None

    @staticmethod
    def _set_step(node: Node, step: PipelineStep | None) -> None:
        node.exec_result[_STEP_KEY] = step.value if step else None

    @staticmethod
    def _set_status(node: Node, status: str) -> None:
        node.exec_result[_STATUS_KEY] = status

    # ── Lifecycle ─────────────────────────────────────────────────

    def seed(self, hypothesis_text: str | None = None) -> list[Node]:
        """Spawn K draft Nodes from the hypothesis seed.

        Also creates ``branch_<n>/`` dirs and copies ``hypothesis.md``
        into each one for backwards compatibility with legacy agents.
        """
        journal = self.load()
        created: list[Node] = []

        hypothesis_path = self.pipeline_dir / "hypothesis.md"
        for i in range(self.num_branches):
            bdir = self.branch_dir(i)
            bdir.mkdir(parents=True, exist_ok=True)
            if hypothesis_path.exists():
                shutil.copy2(hypothesis_path, bdir / "hypothesis.md")

            node = Node(
                id=f"branch_{i}",
                parent_id=None,
                stage="draft",
                plan=hypothesis_text or "",
                code="",
                is_seed_node=True,
                pipeline_id=self.pipeline_id,
                iter_index=i,
            )
            self._set_step(node, PipelineStep.DESIGN)
            self._set_status(node, "active")
            journal.append(node)
            created.append(node)
            logger.info(
                "Seeded branch %d for pipeline %s", i, self.pipeline_id
            )

        self._save()
        return created

    # ── Per-tick operations ───────────────────────────────────────

    def advance(
        self,
        branch_id: int,
        new_step: PipelineStep,
        artifacts: dict[str, Any] | None = None,
    ) -> None:
        """Record that branch ``branch_id`` has completed up to ``new_step``."""
        node = self._branch_by_index(branch_id)
        if node is None:
            logger.warning(
                "advance(): unknown branch %d for pipeline %s",
                branch_id, self.pipeline_id,
            )
            return
        self._set_step(node, new_step)
        if artifacts:
            node.exec_result.setdefault("artifacts", {}).update(artifacts)
        self._save()

    def set_status(self, branch_id: int, status: str) -> None:
        node = self._branch_by_index(branch_id)
        if node is None:
            return
        self._set_status(node, status)
        if status == "failed":
            node.is_buggy = True
        self._save()

    def active_branches(self) -> list[Node]:
        return [n for n in self.branch_nodes() if self._get_status(n) == "active"]

    def all_reached(self, target_step: PipelineStep) -> bool:
        """True when every active/completed branch is at or past ``target_step``."""
        step_order = [s.value for s in PipelineStep]
        try:
            target_idx = step_order.index(target_step.value)
        except ValueError:
            return False

        nodes = self.branch_nodes()
        if not nodes:
            return False

        for node in nodes:
            status = self._get_status(node)
            if status not in ("active", "completed", "selected"):
                continue
            current = self._get_step(node)
            if current is None:
                return False
            try:
                idx = step_order.index(current.value)
            except ValueError:
                return False
            if idx < target_idx:
                return False
        return True

    def collect_results(self) -> list[dict[str, Any]]:
        """Gather on-disk artifacts for each non-failed branch."""
        results: list[dict[str, Any]] = []
        for node in self.branch_nodes():
            if self._get_status(node) == "failed":
                continue
            bdir = self.branch_dir(node.iter_index)
            entry: dict[str, Any] = {"branch_id": node.iter_index}
            for artifact_name in (
                "hypothesis.md",
                "experiment_plan.yaml",
                "analysis_report.md",
                "results.json",
            ):
                path = bdir / artifact_name
                if not path.exists():
                    continue
                try:
                    content = path.read_text()
                    if artifact_name == "results.json":
                        entry["results"] = json.loads(content)
                    else:
                        if len(content) > 2000:
                            logger.warning(
                                "Truncating %s to 2000 chars for branch %d",
                                artifact_name, node.iter_index,
                            )
                        entry[artifact_name.replace(".", "_")] = content[:2000]
                except Exception:
                    pass
            results.append(entry)
        return results

    # ── Winner selection ─────────────────────────────────────────

    def _ingest_results_metrics(self) -> None:
        """Hydrate ``node.metric`` from ``branch_<n>/results.json`` on disk.

        Only numeric top-level entries from a ``metrics`` block are
        imported -- this lets ``Journal.get_best_node`` run its
        structured filter without bespoke scheduler wiring.
        """
        for node in self.branch_nodes():
            if node.metric:
                continue
            rp = self.branch_dir(node.iter_index) / "results.json"
            if not rp.exists():
                continue
            try:
                raw = json.loads(rp.read_text())
            except Exception:
                continue
            metrics = raw.get("metrics", {}) if isinstance(raw, dict) else {}
            if not isinstance(metrics, dict):
                continue
            node.metric = {
                k: v for k, v in metrics.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
        self._save()

    def select_winner(
        self,
        *,
        metric_key: str | None = None,
        higher_is_better: bool = True,
        llm_tiebreaker: Callable[[list[Node]], Node | str | None] | None = None,
    ) -> Node | None:
        """Pick the best branch via Journal.get_best_node (+ LLM tiebreaker)."""
        self._ingest_results_metrics()
        journal = self.load()

        # Restrict candidates to branch drafts only -- get_best_node would
        # otherwise see improvement / debug nodes from future ticks.
        branch_nodes = [n for n in self.branch_nodes() if self._get_status(n) != "failed"]
        if not branch_nodes:
            return None

        # Use a scoped Journal view to avoid contamination.
        scoped = Journal(branch_nodes)
        best = scoped.get_best_node(
            metric_key=metric_key,
            higher_is_better=higher_is_better,
            llm_tiebreaker=llm_tiebreaker,
        )
        if best is None:
            # No good node yet -- pick the first non-failed branch as a
            # deterministic fallback so the scheduler can still progress.
            best = branch_nodes[0]
        return best

    # ── Teardown ─────────────────────────────────────────────────

    def archive(
        self,
        winner_id: int,
        *,
        prune_dirs: bool = False,
    ) -> Node | None:
        """Promote winner, mark losers completed, copy artifacts back to main."""
        winner = self._branch_by_index(winner_id)
        if winner is None:
            logger.error(
                "archive(): unknown winner branch %d for pipeline %s",
                winner_id, self.pipeline_id,
            )
            return None

        winner_dir = self.branch_dir(winner_id)
        if winner_dir.exists():
            for item in winner_dir.iterdir():
                if item.is_file():
                    shutil.copy2(item, self.pipeline_dir / item.name)

        for node in self.branch_nodes():
            if node.iter_index == winner_id:
                self._set_status(node, "selected")
            elif self._get_status(node) == "active":
                self._set_status(node, "completed")

        # Persist a small record of the selection outside the journal so
        # human inspection is trivial.
        try:
            (self.pipeline_dir / _SELECTION_FILE).write_text(
                json.dumps(
                    {
                        "pipeline_id": self.pipeline_id,
                        "winner_id": winner_id,
                        "winner_node_id": winner.id,
                        "archived_at": _now_iso(),
                    },
                    indent=2,
                )
            )
        except Exception:
            logger.debug("Could not write branch_selection.json", exc_info=True)

        if prune_dirs:
            for node in self.branch_nodes():
                if node.iter_index == winner_id:
                    continue
                bdir = self.branch_dir(node.iter_index)
                if bdir.exists():
                    shutil.rmtree(bdir, ignore_errors=True)

        self._save()
        return winner


__all__ = [
    # BFTS primitives
    "Node",
    "NodeStage",
    "Journal",
    "BFTSConfig",
    "debug_depth",
    "select_parallel_nodes",
    "stage_complete",
    "JournalPersistence",
    # K-parallel runner
    "BranchRunner",
]
