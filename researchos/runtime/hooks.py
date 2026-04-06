"""ResearchOS hooks: enforce artifact protocol and knowledge access rules.

These hooks validate:
1. Artifact writes go to correct directories
2. results.json follows required schema
3. Knowledge base access goes through kb_retrieve (not direct file reads)
4. Pipeline state.json is not modified by subagents
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from researchos.schemas.artifact import ResultsArtifact
from researchos.utils.paths import PROJECT_ROOT, ARTIFACTS_DIR, KNOWLEDGE_WIKI_DIR

logger = logging.getLogger(__name__)


class HookViolation(Exception):
    """Raised when a hook rule is violated."""

    pass


def validate_artifact_write(file_path: str, content: str | None = None) -> dict:
    """Validate that a file write follows the artifact protocol.

    Returns a dict with ``allowed: bool`` and ``reason: str``.
    Called as a post-tool-use hook for write operations.
    """
    path = Path(file_path).resolve()

    # Rule 1: state.json is read-only for subagents
    if path.name == "state.json" and "pipeline_" in str(path):
        return {
            "allowed": False,
            "reason": "state.json is managed by the scheduler. Do NOT modify it directly.",
        }

    # Rule 2: results.json must follow ResultsArtifact schema
    if path.name == "results.json" and content:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return {
                "allowed": False,
                "reason": "results.json must be valid JSON.",
            }
        # Enforce schema: metrics (dict of numbers), config (dict), seed (int)
        validation = validate_results_schema(data)
        if not validation["valid"]:
            return {
                "allowed": False,
                "reason": f"results.json schema violation: {validation['reason']}",
            }

    # Rule 3: Don't write directly to knowledge/wiki/
    if KNOWLEDGE_WIKI_DIR and path.is_relative_to(KNOWLEDGE_WIKI_DIR):
        # Only the Knowledge Compiler should write here
        # In practice, this is a warning, not a block
        return {
            "allowed": True,
            "reason": "WARNING: Direct writes to knowledge/wiki/ should go through "
            "the Knowledge Compiler. Consider writing to /artifacts/ first.",
        }

    return {"allowed": True, "reason": ""}


def validate_results_schema(data: dict) -> dict:
    """Validate that results.json conforms to ResultsArtifact schema.

    Required fields:
    - metrics: dict where all values are int or float
    - config: dict (any structure)
    - seed: int

    Returns dict with ``valid: bool`` and ``reason: str``.
    """
    if not isinstance(data, dict):
        return {"valid": False, "reason": "Root must be a JSON object."}

    # Check metrics
    metrics = data.get("metrics")
    if metrics is None:
        return {"valid": False, "reason": "Missing required field 'metrics' (dict of numeric values)."}
    if not isinstance(metrics, dict):
        return {"valid": False, "reason": f"'metrics' must be a dict, got {type(metrics).__name__}."}
    for k, v in metrics.items():
        if not isinstance(v, (int, float)):
            return {"valid": False, "reason": f"metrics['{k}'] must be numeric, got {type(v).__name__}: {v!r}"}

    # Check config
    config = data.get("config")
    if config is None:
        return {"valid": False, "reason": "Missing required field 'config' (dict)."}
    if not isinstance(config, dict):
        return {"valid": False, "reason": f"'config' must be a dict, got {type(config).__name__}."}

    # Check seed
    seed = data.get("seed")
    if seed is None:
        return {"valid": False, "reason": "Missing required field 'seed' (int)."}
    if not isinstance(seed, int):
        return {"valid": False, "reason": f"'seed' must be int, got {type(seed).__name__}: {seed!r}"}

    return {"valid": True, "reason": "Schema OK."}


def validate_data_source_compliance(pipeline_id: str, project_root: Path | None = None) -> dict:
    """Pre-run check: if experiment_plan says file/api, data must exist in /data/raw/.

    Called before the RUN step to ensure fetch_dataset was used when required.
    Returns dict with ``compliant: bool``, ``source: str``, ``reason: str``.
    """
    import yaml

    root = project_root or PROJECT_ROOT
    pdir = root / "artifacts" / f"pipeline_{pipeline_id}"
    plan_path = pdir / "experiment_plan.yaml"

    if not plan_path.exists():
        return {"compliant": True, "source": "unknown", "reason": "No experiment plan found, skipping check."}

    try:
        plan = yaml.safe_load(plan_path.read_text()) or {}
    except Exception:
        return {"compliant": True, "source": "unknown", "reason": "Could not parse experiment plan."}

    data_section = plan.get("data", {})
    source = data_section.get("source", "synthetic")

    if source in ("file", "api"):
        data_dir = root / "data" / "raw" / pipeline_id
        manifest_path = pdir / "data_manifest.yaml"

        # Check if data was actually downloaded
        has_data = data_dir.exists() and any(
            f for f in data_dir.iterdir()
            if f.is_file() and f.name != "data_manifest.yaml"
        ) if data_dir.exists() else False

        if not has_data:
            return {
                "compliant": False,
                "source": source,
                "reason": (
                    f"experiment_plan.yaml specifies data.source='{source}' but "
                    f"no data files found in data/raw/{pipeline_id}/. "
                    f"The experiment_runner must call fetch_dataset before running the experiment."
                ),
            }

        if not manifest_path.exists():
            return {
                "compliant": False,
                "source": source,
                "reason": (
                    f"Data files exist but data_manifest.yaml is missing. "
                    f"fetch_dataset should generate it automatically."
                ),
            }

    return {"compliant": True, "source": source, "reason": f"Data source '{source}' OK."}


def validate_knowledge_access(tool_name: str, arguments: dict) -> dict:
    """Validate that knowledge base access goes through kb_retrieve.

    Called as a pre-tool-use hook for read/grep operations.
    """
    if tool_name not in ("read", "file_read", "grep", "glob"):
        return {"allowed": True, "reason": ""}

    target = arguments.get("file_path", "") or arguments.get("path", "") or ""
    if "knowledge/wiki" in target:
        return {
            "allowed": False,
            "reason": "Direct access to knowledge/wiki/ is prohibited. "
            "Use the kb_retrieve MCP tool instead.",
        }

    return {"allowed": True, "reason": ""}


def validate_budget(
    pipeline_id: str, estimated_cost: int = 0, project_root: Path | None = None
) -> dict:
    """Check if a pipeline has sufficient budget for an operation."""
    from researchos.runtime.state_manager import StateManager

    sm = StateManager(project_root) if project_root else StateManager()
    budget = sm.load_budget()

    # System agents (e.g. knowledge_miner) bypass pipeline budget checks
    if pipeline_id.startswith("_system"):
        return {"allowed": True, "reason": "System agent — no pipeline budget check."}

    allocated = budget.allocations.get(pipeline_id, 0)
    used = budget.usage.get(pipeline_id, 0)
    remaining = allocated - used

    if estimated_cost > 0 and estimated_cost > remaining:
        return {
            "allowed": False,
            "reason": f"Insufficient budget. Remaining: {remaining} credits, "
            f"requested: {estimated_cost} credits.",
        }

    return {
        "allowed": True,
        "reason": f"Budget OK. Remaining: {remaining}/{allocated} credits.",
    }


def record_budget_usage(
    pipeline_id: str, credits_used: int, project_root: Path | None = None
) -> None:
    """Record credit usage for a pipeline (both global budget and pipeline state)."""
    from researchos.runtime.state_manager import StateManager

    sm = StateManager(project_root) if project_root else StateManager()

    # Update global budget
    budget = sm.load_budget()
    current = budget.usage.get(pipeline_id, 0)
    budget.usage[pipeline_id] = current + credits_used
    sm.save_budget(budget)

    # Update pipeline-level budget_used
    pipeline = sm.load_pipeline(pipeline_id)
    if pipeline:
        pipeline.budget_used = budget.usage[pipeline_id]
        sm.save_pipeline(pipeline)
