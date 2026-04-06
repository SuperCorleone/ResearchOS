"""File-based state management for pipelines, decisions, and budget."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from researchos.schemas.budget import BudgetState
from researchos.schemas.decision import ProfessorDecision
from researchos.schemas.pipeline import PipelineDefinition, PipelineState
from researchos.utils.paths import (
    ARTIFACTS_DIR,
    BUDGET_FILE,
    DECISIONS_DIR,
    RUNTIME_LOGS_DIR,
    RUNTIME_PIPELINES_DIR,
    pipeline_dir,
    pipeline_state_file,
)

logger = logging.getLogger(__name__)


class StateManager:
    """Read/write pipeline state files.  All operations are idempotent."""

    def __init__(self, project_root: Path | None = None) -> None:
        if project_root is not None:
            self.artifacts_dir = project_root / "artifacts"
            self.runtime_pipelines_dir = project_root / "runtime" / "pipelines"
            self.decisions_dir = project_root / "decisions"
            self.budget_file = project_root / "runtime" / "budget.json"
            self.logs_dir = project_root / "runtime" / "logs"
        else:
            self.artifacts_dir = ARTIFACTS_DIR
            self.runtime_pipelines_dir = RUNTIME_PIPELINES_DIR
            self.decisions_dir = DECISIONS_DIR
            self.budget_file = BUDGET_FILE
            self.logs_dir = RUNTIME_LOGS_DIR

    # ── Pipelines ────────────────────────────────────────────────

    def load_all_pipelines(self) -> list[PipelineState]:
        """Scan ``/artifacts/pipeline_*/state.json`` and return all states."""
        states: list[PipelineState] = []
        if not self.artifacts_dir.exists():
            return states
        for d in sorted(self.artifacts_dir.iterdir()):
            if d.is_dir() and d.name.startswith("pipeline_"):
                sf = d / "state.json"
                if sf.exists():
                    try:
                        states.append(PipelineState.model_validate_json(sf.read_text()))
                    except Exception:
                        logger.exception("Failed to load %s", sf)
        return states

    def load_pipeline(self, pipeline_id: str) -> PipelineState | None:
        sf = self.artifacts_dir / f"pipeline_{pipeline_id}" / "state.json"
        if not sf.exists():
            return None
        return PipelineState.model_validate_json(sf.read_text())

    def save_pipeline(self, state: PipelineState) -> None:
        """Atomic write: write to temp file then rename."""
        state.updated_at = datetime.now(timezone.utc).isoformat()
        dest = self.artifacts_dir / f"pipeline_{state.pipeline_id}" / "state.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = state.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
        try:
            os.write(fd, data.encode())
            os.close(fd)
            os.replace(tmp, dest)
        except BaseException:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── Proposals ────────────────────────────────────────────────

    def load_new_proposals(self) -> list[PipelineDefinition]:
        proposals: list[PipelineDefinition] = []
        if not self.runtime_pipelines_dir.exists():
            return proposals
        for f in sorted(self.runtime_pipelines_dir.glob("new_*.json")):
            try:
                proposals.append(PipelineDefinition.model_validate_json(f.read_text()))
            except Exception:
                logger.exception("Failed to load proposal %s", f)
        return proposals

    def archive_proposal(self, proposal: PipelineDefinition) -> None:
        src = self.runtime_pipelines_dir / f"new_{proposal.pipeline_id}.json"
        if not src.exists():
            return
        archived_dir = self.runtime_pipelines_dir / "archived"
        archived_dir.mkdir(exist_ok=True)
        src.rename(archived_dir / src.name)

    # ── Decisions ────────────────────────────────────────────────

    def load_pending_decisions(self) -> list[ProfessorDecision]:
        decisions: list[ProfessorDecision] = []
        if not self.decisions_dir.exists():
            return decisions
        for f in sorted(self.decisions_dir.glob("approval_*.yaml")):
            if f.name.endswith(".processed.yaml"):
                continue
            try:
                raw = yaml.safe_load(f.read_text())
                d = ProfessorDecision.model_validate(raw)
                d._source_file = str(f)  # Track source for precise marking
                decisions.append(d)
            except Exception:
                logger.exception("Failed to load decision %s", f)
        return decisions

    def mark_decision_processed(self, decision: ProfessorDecision) -> None:
        # Mark only the specific source file, not all matching files
        source = getattr(decision, "_source_file", None)
        if source:
            f = Path(source)
            if f.exists() and not f.name.endswith(".processed.yaml"):
                f.rename(f.with_suffix(".processed.yaml"))
            return
        # Fallback: mark all (legacy behavior)
        pattern = f"approval_{decision.pipeline_id}_*.yaml"
        for f in self.decisions_dir.glob(pattern):
            if f.name.endswith(".processed.yaml"):
                continue
            f.rename(f.with_suffix(".processed.yaml"))
            break  # Only mark one

    def write_decision_request(self, pipeline_id: str, context_files: list[str]) -> Path:
        """Write a decision request file for the professor."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        path = self.decisions_dir / f"request_{pipeline_id}_{ts}.yaml"
        data = {
            "pipeline_id": pipeline_id,
            "context_files": context_files,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(yaml.dump(data, default_flow_style=False))
        return path

    # ── Budget ───────────────────────────────────────────────────

    def load_budget(self) -> BudgetState:
        if self.budget_file.exists():
            return BudgetState.model_validate_json(self.budget_file.read_text())
        return BudgetState()

    def save_budget(self, budget: BudgetState) -> None:
        self.budget_file.parent.mkdir(parents=True, exist_ok=True)
        data = budget.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.budget_file.parent, suffix=".tmp")
        try:
            os.write(fd, data.encode())
            os.close(fd)
            os.replace(tmp, self.budget_file)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── Logging ──────────────────────────────────────────────────

    def append_log(self, message: str, pipeline_id: str | None = None) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        if pipeline_id:
            log_file = self.logs_dir / f"pipeline_{pipeline_id}.log"
        else:
            log_file = self.logs_dir / "scheduler.log"
        with open(log_file, "a") as f:
            f.write(f"[{ts}] {message}\n")
