"""Step-level checkpoint cache for pipeline execution.

Saves completed step results to .step_cache/ within the pipeline artifact
directory. On restart, completed steps are skipped (idempotent replay).
Partial/failed steps are not cached -- only successful completions.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class StepCache:
    """Manages step-level checkpoints for a single pipeline."""

    def __init__(self, pipeline_dir: Path) -> None:
        self.cache_dir = pipeline_dir / ".step_cache"

    def _cache_path(self, step: str, stage: str) -> Path:
        return self.cache_dir / f"{stage}__{step}.json"

    def has_completed(self, step: str, stage: str) -> bool:
        """Check if a step has a completed cache entry."""
        path = self._cache_path(step, stage)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
            return data.get("status") == "completed"
        except (json.JSONDecodeError, OSError):
            return False

    def get_result(self, step: str, stage: str) -> dict[str, Any] | None:
        """Load cached result for a completed step. Returns None if not cached."""
        path = self._cache_path(step, stage)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            if data.get("status") == "completed":
                return data.get("result")
        except (json.JSONDecodeError, OSError):
            pass
        return None

    def save_completed(self, step: str, stage: str, result: dict[str, Any]) -> None:
        """Save a successful step completion to cache."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "step": step,
            "stage": stage,
            "status": "completed",
            "result": result,
            "cached_at": time.time(),
        }
        path = self._cache_path(step, stage)
        try:
            path.write_text(json.dumps(entry, indent=2, default=str))
            logger.debug("Step cache saved: %s/%s", stage, step)
        except OSError:
            logger.warning("Failed to save step cache: %s/%s", stage, step)

    def invalidate(self, step: str, stage: str) -> None:
        """Remove cache for a specific step (e.g., after user tweak)."""
        path = self._cache_path(step, stage)
        if path.exists():
            path.unlink(missing_ok=True)
            logger.debug("Step cache invalidated: %s/%s", stage, step)

    def invalidate_from(self, step: str, stage: str, stage_sequence: list[str]) -> None:
        """Invalidate cache for this step and all subsequent steps in the stage sequence.

        Used when a user tweaks a pipeline -- all downstream results become stale.
        """
        try:
            idx = stage_sequence.index(step)
        except ValueError:
            return
        for s in stage_sequence[idx:]:
            self.invalidate(s, stage)

    def clear_all(self) -> None:
        """Remove all cached steps (full re-run)."""
        if self.cache_dir.exists():
            import shutil

            shutil.rmtree(self.cache_dir, ignore_errors=True)
            logger.info("Step cache cleared")
