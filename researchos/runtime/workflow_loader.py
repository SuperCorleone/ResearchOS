"""Load pipeline workflow configuration from YAML or hardcoded defaults.

The WorkflowConfig class provides a single source of truth for step routing,
agent mappings, output filenames, timeouts, and retries.  It loads from
``runtime/workflow_config.yaml`` when available and falls back to built-in
defaults that match the original hardcoded scheduler/dispatcher behaviour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StepConfig:
    """Configuration for a single pipeline step."""

    name: str
    agent: str | None = None
    output: str | None = None
    timeout: int = 300
    retries: int = 3
    type: str = "llm"  # "llm" or "python"


@dataclass
class StageConfig:
    """Configuration for a pipeline stage (ordered list of steps)."""

    name: str
    sequence: list[str] = field(default_factory=list)
    dynamic_routing: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Hardcoded defaults (match original scheduler.py / dispatcher.py exactly)
# ---------------------------------------------------------------------------

_DEFAULT_STEPS: dict[str, dict[str, Any]] = {
    "hypothesis": {
        "agent": "hypothesis_gen",
        "output": "hypothesis.md",
        "timeout": 300,
        "retries": 3,
        "type": "llm",
    },
    "design": {
        "agent": "exp_designer",
        "output": "experiment_plan.yaml",
        "timeout": 300,
        "retries": 3,
        "type": "llm",
    },
    "design_review": {
        "agent": "design_reviewer",
        "output": "design_review.md",
        "timeout": 300,
        "retries": 3,
        "type": "llm",
    },
    "code": {
        "agent": "coder",
        "output": "code_patch.diff",
        "timeout": 300,
        "retries": 3,
        "type": "llm",
    },
    "code_preflight": {
        "agent": "code_preflight",
        "output": "preflight_report.json",
        "timeout": 120,
        "retries": 3,
        "type": "python",
    },
    "run": {
        "agent": "experiment_runner",
        "output": "results.json",
        "timeout": 600,
        "retries": 3,
        "type": "llm",
    },
    "critic": {
        "agent": "critic",
        "output": "analysis_report.md",
        "timeout": 300,
        "retries": 3,
        "type": "llm",
    },
    "interrogation": {
        "agent": "interrogator",
        "output": "interrogation_report.md",
        "timeout": 300,
        "retries": 3,
        "type": "llm",
    },
    "report": {
        "agent": "reporter",
        "output": "milestone_{n}.md",
        "timeout": 300,
        "retries": 3,
        "type": "llm",
    },
    "waiting_approval": {
        "agent": None,
        "output": None,
        "timeout": 0,
        "retries": 0,
        "type": "llm",
    },
}

_DEFAULT_STAGES: dict[str, dict[str, Any]] = {
    "main": {
        "sequence": [
            "hypothesis",
            "design",
            "design_review",
            "code",
            "code_preflight",
            "run",
            "critic",
            "interrogation",
            "report",
            "waiting_approval",
        ],
        "dynamic_routing": {
            "design": {
                "default": "design_review",
                "alternatives": [
                    {
                        "next": "code",
                        "condition": "design already reviewed or reused from prior milestone",
                    }
                ],
            },
        },
    },
    "fast_fail": {
        "sequence": [
            "hypothesis",
            "design",
            "code",
            "run",
            "critic",
            "report",
            "waiting_approval",
        ],
        "dynamic_routing": {
            "design": {
                "default": "code",
                "alternatives": [
                    {
                        "next": "run",
                        "condition": "code already exists from prior iteration",
                    }
                ],
            },
        },
    },
}


# ---------------------------------------------------------------------------
# WorkflowConfig
# ---------------------------------------------------------------------------


class WorkflowConfig:
    """Unified workflow configuration loaded from YAML or hardcoded defaults.

    Usage::

        cfg = WorkflowConfig.load(project_root)
        next_map = cfg.build_next_step_map()
        agent = cfg.get_step_agent("hypothesis")
    """

    def __init__(
        self,
        steps: dict[str, StepConfig],
        stages: dict[str, StageConfig],
    ) -> None:
        self.steps = steps
        self.stages = stages

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, project_root: Path) -> WorkflowConfig:
        """Load from ``runtime/workflow_config.yaml`` or fall back to defaults."""
        config_path = project_root / "runtime" / "workflow_config.yaml"
        if config_path.exists():
            try:
                raw = yaml.safe_load(config_path.read_text()) or {}
                config = cls._from_raw(raw)
                errors = config.validate()
                if errors:
                    logger.warning(
                        "Workflow config %s has validation errors: %s; falling back to defaults",
                        config_path, errors,
                    )
                    return cls._from_defaults()
                return config
            except Exception:
                logger.warning(
                    "Failed to parse %s; falling back to defaults", config_path,
                    exc_info=True,
                )
        return cls._from_defaults()

    @classmethod
    def _from_defaults(cls) -> WorkflowConfig:
        steps = {
            name: StepConfig(name=name, **params)
            for name, params in _DEFAULT_STEPS.items()
        }
        stages = {
            name: StageConfig(
                name=name,
                sequence=cfg["sequence"],
                dynamic_routing=cfg.get("dynamic_routing", {}),
            )
            for name, cfg in _DEFAULT_STAGES.items()
        }
        return cls(steps=steps, stages=stages)

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> WorkflowConfig:
        """Parse a raw YAML dict into WorkflowConfig."""
        steps: dict[str, StepConfig] = {}
        for name, params in (raw.get("steps") or {}).items():
            if params is None:
                params = {}
            steps[name] = StepConfig(
                name=name,
                agent=params.get("agent"),
                output=params.get("output"),
                timeout=int(params.get("timeout", 300)),
                retries=int(params.get("retries", 3)),
                type=params.get("type", "llm"),
            )

        stages: dict[str, StageConfig] = {}
        for name, cfg in (raw.get("stages") or {}).items():
            if cfg is None:
                cfg = {}
            stages[name] = StageConfig(
                name=name,
                sequence=cfg.get("sequence", []),
                dynamic_routing=cfg.get("dynamic_routing", {}),
            )

        return cls(steps=steps, stages=stages)

    # ------------------------------------------------------------------
    # Step accessors
    # ------------------------------------------------------------------

    def _get_step(self, step_name: str) -> StepConfig | None:
        return self.steps.get(step_name)

    def get_step_agent(self, step_name: str) -> str | None:
        """Return the agent name for a step, or ``None`` if not defined."""
        step = self._get_step(step_name)
        return step.agent if step else None

    def get_step_output(self, step_name: str) -> str | None:
        """Return the expected output filename for a step."""
        step = self._get_step(step_name)
        return step.output if step else None

    def get_step_timeout(self, step_name: str) -> int:
        """Return the timeout in seconds for a step (default 300)."""
        step = self._get_step(step_name)
        return step.timeout if step else 300

    def get_step_retries(self, step_name: str) -> int:
        """Return the max retries for a step (default 3)."""
        step = self._get_step(step_name)
        return step.retries if step else 3

    def get_step_type(self, step_name: str) -> str:
        """Return ``"llm"`` or ``"python"`` for a step."""
        step = self._get_step(step_name)
        return step.type if step else "llm"

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def build_next_step_map(self) -> dict[tuple[str, str | None], str | None]:
        """Build a mapping of ``(stage_name, step_name) -> next_step_name``.

        For the first step in each stage the key uses ``step_name=None``.
        Steps with dynamic routing use the ``default`` target; the caller
        should inspect ``get_dynamic_routing()`` when it needs to branch.
        The last step in each sequence maps to ``None`` (end of stage).
        """
        result: dict[tuple[str, str | None], str | None] = {}

        for stage_name, stage in self.stages.items():
            seq = stage.sequence
            if not seq:
                continue

            # (stage, None) -> first step
            result[(stage_name, None)] = seq[0]

            for i, step_name in enumerate(seq):
                dynamic = stage.dynamic_routing.get(step_name)
                if dynamic:
                    # Use the default route from dynamic_routing
                    result[(stage_name, step_name)] = dynamic.get("default")
                elif i + 1 < len(seq):
                    result[(stage_name, step_name)] = seq[i + 1]
                else:
                    # Last step in sequence -> None (terminal)
                    result[(stage_name, step_name)] = None

        return result

    def get_dynamic_routing(
        self, stage_name: str, step_name: str
    ) -> dict[str, Any] | None:
        """Return dynamic routing config for a step within a stage, if any.

        Returns a dict with ``"default"`` and ``"alternatives"`` keys, or
        ``None`` if the step has no dynamic routing.
        """
        stage = self.stages.get(stage_name)
        if stage is None:
            return None
        return stage.dynamic_routing.get(step_name)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Validate the workflow configuration.

        Returns a list of error strings. An empty list means the config is
        valid.
        """
        errors: list[str] = []

        # Check that every step referenced in a stage sequence is defined
        defined_steps = set(self.steps.keys())
        for stage_name, stage in self.stages.items():
            for step_name in stage.sequence:
                if step_name not in defined_steps:
                    errors.append(
                        f"Stage '{stage_name}' references undefined step "
                        f"'{step_name}'"
                    )

            # Check dynamic routing targets reference defined steps
            for src_step, routing in stage.dynamic_routing.items():
                if src_step not in defined_steps:
                    errors.append(
                        f"Stage '{stage_name}' dynamic_routing references "
                        f"undefined source step '{src_step}'"
                    )
                default_target = routing.get("default")
                if default_target and default_target not in defined_steps:
                    errors.append(
                        f"Stage '{stage_name}' dynamic_routing default target "
                        f"'{default_target}' is not a defined step"
                    )
                for alt in routing.get("alternatives", []):
                    alt_next = alt.get("next")
                    if alt_next and alt_next not in defined_steps:
                        errors.append(
                            f"Stage '{stage_name}' dynamic_routing alternative "
                            f"target '{alt_next}' is not a defined step"
                        )

                # Check that the source step is actually in the stage sequence
                if src_step not in stage.sequence:
                    errors.append(
                        f"Stage '{stage_name}' dynamic_routing source "
                        f"'{src_step}' is not in the stage sequence"
                    )

        # Check step types are valid
        for step_name, step in self.steps.items():
            if step.type not in ("llm", "python"):
                errors.append(
                    f"Step '{step_name}' has invalid type '{step.type}'; "
                    f"must be 'llm' or 'python'"
                )

        # Check that stages have at least one step
        for stage_name, stage in self.stages.items():
            if not stage.sequence:
                errors.append(f"Stage '{stage_name}' has an empty sequence")

        return errors
