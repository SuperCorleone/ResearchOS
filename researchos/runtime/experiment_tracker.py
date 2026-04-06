"""Experiment tracking integration for W&B and MLflow.

Provides a pluggable tracking backend that logs experiment metrics,
configs, and artifacts after each experiment_runner step.

Configuration in runtime/research_config.yaml:
    tracking:
      backend: "none"  # none | wandb | mlflow
      wandb_project: "researchos"
      wandb_entity: null
      mlflow_uri: "mlruns"
      mlflow_experiment: "researchos"
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ExperimentTracker(ABC):
    """Abstract base for experiment tracking backends."""

    @abstractmethod
    def log_run(
        self,
        pipeline_id: str,
        metrics: dict[str, float],
        config: dict[str, Any],
        code_version: str | None = None,
        data_hash: str | None = None,
        artifacts: list[Path] | None = None,
    ) -> str | None:
        """Log an experiment run. Returns tracking URL if available."""

    @abstractmethod
    def finish(self) -> None:
        """Clean up / finalize the tracking session."""


class NoOpTracker(ExperimentTracker):
    """Default tracker when tracking is disabled."""

    def log_run(self, **kwargs: Any) -> None:
        return None

    def finish(self) -> None:
        pass


class WandbTracker(ExperimentTracker):
    """Weights & Biases experiment tracker."""

    def __init__(self, project: str = "researchos", entity: str | None = None) -> None:
        try:
            import wandb
            self._wandb = wandb
        except ImportError:
            raise RuntimeError(
                "wandb not installed. Run: pip install researchos[wandb]"
            )
        self._project = project
        self._entity = entity

    def log_run(
        self,
        pipeline_id: str,
        metrics: dict[str, float],
        config: dict[str, Any],
        code_version: str | None = None,
        data_hash: str | None = None,
        artifacts: list[Path] | None = None,
    ) -> str | None:
        try:
            run_config = dict(config)
            if code_version:
                run_config["code_version"] = code_version
            if data_hash:
                run_config["data_hash"] = data_hash

            run = self._wandb.init(
                project=self._project,
                entity=self._entity,
                name=pipeline_id,
                config=run_config,
                reinit=True,
            )
            # Filter to numeric metrics only
            numeric_metrics = {
                k: v for k, v in metrics.items()
                if isinstance(v, (int, float))
            }
            self._wandb.log(numeric_metrics)

            if artifacts:
                for artifact_path in artifacts:
                    if artifact_path.exists() and artifact_path.stat().st_size < 50_000_000:
                        self._wandb.save(str(artifact_path))

            url = run.url if run else None
            self._wandb.finish()
            return url
        except Exception as e:
            logger.warning("W&B tracking failed: %s", e)
            try:
                self._wandb.finish(quiet=True)
            except Exception:
                pass
            return None

    def finish(self) -> None:
        try:
            self._wandb.finish(quiet=True)
        except Exception:
            pass


class MLflowTracker(ExperimentTracker):
    """MLflow experiment tracker."""

    def __init__(
        self,
        tracking_uri: str = "mlruns",
        experiment_name: str = "researchos",
    ) -> None:
        try:
            import mlflow
            self._mlflow = mlflow
        except ImportError:
            raise RuntimeError(
                "mlflow not installed. Run: pip install researchos[mlflow]"
            )
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment_name)

    def log_run(
        self,
        pipeline_id: str,
        metrics: dict[str, float],
        config: dict[str, Any],
        code_version: str | None = None,
        data_hash: str | None = None,
        artifacts: list[Path] | None = None,
    ) -> str | None:
        try:
            with self._mlflow.start_run(run_name=pipeline_id) as run:
                # Log params (flatten to strings, max 500 chars)
                for k, v in config.items():
                    self._mlflow.log_param(k, str(v)[:500])
                if code_version:
                    self._mlflow.log_param("code_version", code_version)
                if data_hash:
                    self._mlflow.log_param("data_hash", data_hash)

                # Log numeric metrics
                for k, v in metrics.items():
                    if isinstance(v, (int, float)):
                        self._mlflow.log_metric(k, v)

                # Log artifact files
                if artifacts:
                    for artifact_path in artifacts:
                        if artifact_path.exists() and artifact_path.stat().st_size < 50_000_000:
                            self._mlflow.log_artifact(str(artifact_path))

                run_id = run.info.run_id
                tracking_uri = self._mlflow.get_tracking_uri()
                return f"{tracking_uri}/#/experiments/{run.info.experiment_id}/runs/{run_id}"
        except Exception as e:
            logger.warning("MLflow tracking failed: %s", e)
            return None

    def finish(self) -> None:
        pass


def create_tracker(config: dict[str, Any]) -> ExperimentTracker:
    """Factory: create tracker from research_config.yaml tracking section."""
    tracking = config.get("tracking", {})
    backend = tracking.get("backend", "none")

    if backend == "wandb":
        try:
            return WandbTracker(
                project=tracking.get("wandb_project", "researchos"),
                entity=tracking.get("wandb_entity"),
            )
        except RuntimeError as e:
            logger.warning("Cannot create W&B tracker: %s", e)
            return NoOpTracker()

    elif backend == "mlflow":
        try:
            return MLflowTracker(
                tracking_uri=tracking.get("mlflow_uri", "mlruns"),
                experiment_name=tracking.get("mlflow_experiment", "researchos"),
            )
        except RuntimeError as e:
            logger.warning("Cannot create MLflow tracker: %s", e)
            return NoOpTracker()

    return NoOpTracker()
