"""File-based state management for pipelines, decisions, and budget."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from researchos.core.contract_guard import ContractViolation, enforce_all
from researchos.schemas.budget import BudgetState
from researchos.schemas.decision import ProfessorDecision
from researchos.schemas.digest import (
    ConceptsDigest,
    HypothesisDigest,
    PlanDigest,
    RunDigest,
)
from researchos.schemas.pipeline import PipelineDefinition, PipelineStage, PipelineState
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

# Map digest filename → (Pydantic model, kwarg name used by enforce_all)
_DIGEST_LOADERS: dict[str, tuple[type, str]] = {
    "hypothesis.digest.json": (HypothesisDigest, "hypothesis"),
    "plan.digest.json":       (PlanDigest,       "plan"),
    "concepts.digest.json":   (ConceptsDigest,   "concepts"),
    "run.digest.json":        (RunDigest,        "run"),
}


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

    def _atomic_write_state(self, state: PipelineState) -> None:
        """Atomic write of ``state.json`` without running contract guards."""
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
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _load_step_digests(self, pdir: Path) -> dict[str, object]:
        """Load whatever StepDigest files are currently on disk for a pipeline.

        Each digest is validated by its Pydantic model — a malformed digest
        raises ``pydantic.ValidationError`` here, which the caller turns into
        a ``ContractViolation`` so the pipeline blocks instead of silently
        advancing on bad data.
        """
        loaded: dict[str, object] = {}
        for filename, (model_cls, kwarg) in _DIGEST_LOADERS.items():
            path = pdir / filename
            if not path.exists():
                continue
            try:
                loaded[kwarg] = model_cls.model_validate_json(path.read_text())
            except Exception as exc:
                raise ContractViolation(
                    f"digest_parse::{filename}",
                    f"Failed to parse {filename}: {exc}",
                ) from exc
        return loaded

    def save_pipeline(
        self,
        state: PipelineState,
        *,
        enforce_contracts: bool = True,
    ) -> None:
        """Atomic write + StepDigest contract enforcement.

        After the state write succeeds, every StepDigest present on disk is
        validated and cross-digest invariants are checked via
        ``contract_guard.enforce_all``. On ``ContractViolation`` the pipeline
        is re-saved with ``stage=BLOCKED`` and the exception is re-raised so
        the scheduler can log / surface it.

        ``enforce_contracts=False`` skips the guard — reserved for
        bootstrap writes where we genuinely have no digests yet (tests,
        initial proposal intake).
        """
        self._atomic_write_state(state)
        if not enforce_contracts:
            return
        # Terminal states should not re-block — preserve their final stage.
        if state.stage in {
            PipelineStage.COMPLETED,
            PipelineStage.FAILED,
            PipelineStage.BLOCKED,
        }:
            return

        pdir = self.artifacts_dir / f"pipeline_{state.pipeline_id}"
        try:
            digests = self._load_step_digests(pdir)
            manifest = pdir / "data_manifest.yaml"
            baseline_analysis_path = pdir / "baseline_analysis.yaml"
            ba = None
            if baseline_analysis_path.exists():
                from researchos.schemas.baseline_analysis import load_baseline_analysis
                ba = load_baseline_analysis(baseline_analysis_path)
            enforce_all(
                **digests,
                baseline_analysis=ba,
                data_manifest_path=manifest if manifest.exists() else None,
            )
        except ContractViolation as cv:
            logger.error(
                "[contract_violation][%s] %s", state.pipeline_id, cv
            )
            state.stage = PipelineStage.BLOCKED
            state.last_error = f"contract_violation:{cv.guard_name}:{cv}"
            try:
                self._atomic_write_state(state)
            except Exception:
                logger.exception(
                    "Failed to record BLOCKED stage for %s after violation",
                    state.pipeline_id,
                )
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
