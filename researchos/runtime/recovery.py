"""Failure recovery and checkpoint management for pipelines.

Provides:
- Checkpoint creation at milestones
- Rollback to previous checkpoint
- Failed pipeline recovery
- Exponential backoff retry logic
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from researchos.schemas.pipeline import PipelineStage, PipelineState, PipelineStep
from researchos.runtime.state_manager import StateManager

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Create and restore pipeline checkpoints."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.checkpoints_dir = project_root / "runtime" / "checkpoints"
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    def create_checkpoint(self, pipeline_id: str, label: str = "") -> Path:
        """Snapshot the entire pipeline directory to a checkpoint.

        Returns the checkpoint directory path.
        """
        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline_id}"
        if not pdir.exists():
            raise FileNotFoundError(f"Pipeline directory not found: {pdir}")

        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        tag = f"_{label}" if label else ""
        cp_name = f"pipeline_{pipeline_id}_{ts}{tag}"
        cp_dir = self.checkpoints_dir / cp_name

        shutil.copytree(pdir, cp_dir)
        logger.info("Checkpoint created: %s", cp_name)
        return cp_dir

    def list_checkpoints(self, pipeline_id: str) -> list[Path]:
        """List all checkpoints for a pipeline, newest first."""
        prefix = f"pipeline_{pipeline_id}_"
        cps = sorted(
            [d for d in self.checkpoints_dir.iterdir() if d.name.startswith(prefix)],
            reverse=True,
        )
        return cps

    def restore_checkpoint(self, pipeline_id: str, checkpoint_path: Path | None = None) -> bool:
        """Restore a pipeline from a checkpoint.

        If no checkpoint_path given, uses the most recent one.
        Returns True if successful.
        """
        if checkpoint_path is None:
            cps = self.list_checkpoints(pipeline_id)
            if not cps:
                logger.warning("No checkpoints found for %s", pipeline_id)
                return False
            checkpoint_path = cps[0]

        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline_id}"
        if pdir.exists():
            # Backup current state before restoring
            backup = self.checkpoints_dir / f"pipeline_{pipeline_id}_pre_restore"
            if backup.exists():
                shutil.rmtree(backup)
            shutil.copytree(pdir, backup)
            shutil.rmtree(pdir)

        shutil.copytree(checkpoint_path, pdir)
        logger.info("Checkpoint restored: %s → %s", checkpoint_path.name, pdir)
        return True


class RetryPolicy:
    """Exponential backoff retry logic."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 5.0,
        max_delay: float = 300.0,
        backoff_factor: float = 2.0,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor

    def should_retry(self, retry_count: int, error: str) -> bool:
        """Determine if the operation should be retried."""
        if retry_count >= self.max_retries:
            return False
        # Non-recoverable errors
        non_recoverable = ["schema validation", "budget exhausted", "permission denied"]
        return not any(nr in error.lower() for nr in non_recoverable)

    def delay_seconds(self, retry_count: int) -> float:
        """Calculate delay before next retry (exponential backoff)."""
        delay = self.base_delay * (self.backoff_factor ** retry_count)
        return min(delay, self.max_delay)


def recover_failed_pipeline(
    pipeline_id: str,
    project_root: Path,
    *,
    rollback: bool = False,
    reset_to_step: PipelineStep | None = None,
) -> PipelineState | None:
    """Recover a failed pipeline.

    Options:
    - ``rollback=True``: Restore from last checkpoint
    - ``reset_to_step``: Reset to a specific step (re-run from there)
    - Neither: Just reset FAILED → MAIN with retry_count=0
    """
    sm = StateManager(project_root)
    pipeline = sm.load_pipeline(pipeline_id)

    if pipeline is None:
        logger.error("Pipeline %s not found", pipeline_id)
        return None

    if pipeline.stage != PipelineStage.FAILED:
        logger.warning("Pipeline %s is not FAILED (stage=%s)", pipeline_id, pipeline.stage)
        return pipeline

    if rollback:
        cp = CheckpointManager(project_root)
        if not cp.restore_checkpoint(pipeline_id):
            logger.error("No checkpoint to restore for %s", pipeline_id)
            return None
        # Reload after restore
        pipeline = sm.load_pipeline(pipeline_id)
        if pipeline is None:
            return None

    if reset_to_step is not None:
        pipeline.step = reset_to_step

    pipeline.stage = PipelineStage.MAIN
    pipeline.retry_count = 0
    pipeline.last_error = None
    sm.save_pipeline(pipeline)
    sm.append_log(
        f"Pipeline recovered (rollback={rollback}, reset_to={reset_to_step})",
        pipeline_id,
    )
    return pipeline
