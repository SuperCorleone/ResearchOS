"""Branch manager for parallel experiment exploration.

Allows MAIN stage pipelines to explore multiple experimental approaches
concurrently. Each branch gets its own subdirectory and shares the parent
hypothesis but diverges at the DESIGN step.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from researchos.schemas.pipeline import PipelineState, PipelineStep

logger = logging.getLogger(__name__)


class BranchState(BaseModel):
    """State of a single experiment branch."""

    branch_id: int
    step: PipelineStep | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    status: str = "active"  # active | completed | failed | selected
    critic_verdict: str | None = None


class BranchManager:
    """Manages parallel experiment branches for a pipeline."""

    def __init__(self, project_root: Path, max_branches: int = 3) -> None:
        self.project_root = project_root
        self.max_branches = max_branches

    def get_branch_dir(self, pipeline_id: str, branch_id: int) -> Path:
        """Return the directory for a specific branch."""
        return (
            self.project_root
            / "artifacts"
            / f"pipeline_{pipeline_id}"
            / f"branch_{branch_id}"
        )

    def spawn_branches(
        self, pipeline: PipelineState, num_branches: int
    ) -> list[BranchState]:
        """Create branch subdirectories, copy shared artifacts (hypothesis.md).

        Returns list of BranchState objects to store in pipeline.metadata["branches"].
        """
        num = min(num_branches, self.max_branches)
        pipeline_dir = (
            self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        )
        branches: list[BranchState] = []

        for i in range(num):
            branch_dir = self.get_branch_dir(pipeline.pipeline_id, i)
            branch_dir.mkdir(parents=True, exist_ok=True)

            # Copy shared artifacts from parent pipeline
            hypothesis_path = pipeline_dir / "hypothesis.md"
            if hypothesis_path.exists():
                shutil.copy2(hypothesis_path, branch_dir / "hypothesis.md")

            branch = BranchState(branch_id=i, step=PipelineStep.DESIGN)
            branches.append(branch)
            logger.info(
                "Spawned branch %d for pipeline %s", i, pipeline.pipeline_id
            )

        return branches

    def all_branches_at_or_past(
        self, branches: list[BranchState], target_step: PipelineStep
    ) -> bool:
        """Check if all active branches have reached or passed a step."""
        step_order = [s.value for s in PipelineStep]
        target_idx = step_order.index(target_step.value)

        for branch in branches:
            if branch.status not in ("active", "completed"):
                continue
            if branch.step is None:
                return False
            branch_idx = step_order.index(branch.step.value)
            if branch_idx < target_idx:
                return False
        return True

    def collect_branch_results(
        self, pipeline_id: str, branches: list[BranchState]
    ) -> list[dict[str, Any]]:
        """Gather critic reports from all branches for the selector agent."""
        results = []
        for branch in branches:
            if branch.status in ("failed",):
                continue
            branch_dir = self.get_branch_dir(pipeline_id, branch.branch_id)
            entry: dict[str, Any] = {"branch_id": branch.branch_id}

            for artifact_name in [
                "hypothesis.md",
                "experiment_plan.yaml",
                "analysis_report.md",
                "results.json",
            ]:
                path = branch_dir / artifact_name
                if path.exists():
                    try:
                        content = path.read_text()
                        if artifact_name == "results.json":
                            entry["results"] = json.loads(content)
                        else:
                            if len(content) > 2000:
                                logger.warning(
                                    "Truncating %s to 2000 chars for branch %d (original: %d chars)",
                                    artifact_name, branch.branch_id, len(content),
                                )
                            entry[artifact_name.replace(".", "_")] = content[:2000]
                    except Exception:
                        pass

            results.append(entry)
        return results

    def select_winner(
        self, pipeline_id: str, branches: list[BranchState], winner_id: int
    ) -> list[BranchState]:
        """Mark the winning branch and copy its artifacts to the main pipeline dir."""
        pipeline_dir = (
            self.project_root / "artifacts" / f"pipeline_{pipeline_id}"
        )
        winner_dir = self.get_branch_dir(pipeline_id, winner_id)

        if not winner_dir.exists():
            logger.error("Winner branch dir not found: %s", winner_dir)
            return branches

        # Copy winning branch artifacts to main pipeline dir
        for item in winner_dir.iterdir():
            if item.is_file():
                dest = pipeline_dir / item.name
                shutil.copy2(item, dest)

        # Update branch statuses
        updated = []
        for branch in branches:
            if branch.branch_id == winner_id:
                branch.status = "selected"
            elif branch.status == "active":
                branch.status = "completed"
            updated.append(branch)

        logger.info(
            "Selected branch %d as winner for pipeline %s",
            winner_id,
            pipeline_id,
        )
        return updated
