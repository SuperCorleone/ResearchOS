"""Baseline / metric-contract registry (A17).

Records what a pipeline *must* report against, and provides a simple
comparability check between claimed results and the declared contract.

Two sibling artifacts live under ``artifacts/pipeline_<id>/``:

- ``metric_contract.json`` — the canonical contract for this pipeline.
- ``metric_waiver.json``   — present only when the contract is waived; the
  presence of a waiver signals downstream agents (and the Professor) that
  baseline comparability cannot be enforced for this run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


MetricDirection = Literal["higher", "lower"]


class MetricContract(BaseModel):
    """Frozen declaration of how a baseline comparison will be judged."""

    task: str
    dataset: str
    eval_script: str
    metrics: dict[str, MetricDirection]
    baseline_values: dict[str, float]
    source_commit: str
    known_deviations: list[str] = Field(default_factory=list)


# -- writers -------------------------------------------------------------


def _pipeline_dir(pipeline_id: str, artifacts_root: Path) -> Path:
    """Return ``{artifacts_root}/pipeline_{id}`` (created on demand)."""
    pid = pipeline_id if pipeline_id.startswith("pipeline_") else f"pipeline_{pipeline_id}"
    path = Path(artifacts_root) / pid
    path.mkdir(parents=True, exist_ok=True)
    return path


def confirm_baseline(
    contract: MetricContract,
    pipeline_id: str,
    artifacts_root: Path,
) -> Path:
    """Write ``metric_contract.json`` for *pipeline_id* and return its path."""
    pdir = _pipeline_dir(pipeline_id, artifacts_root)
    out = pdir / "metric_contract.json"
    payload = json.loads(contract.model_dump_json())
    payload["confirmed_at"] = datetime.now(timezone.utc).isoformat()
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    logger.info("Confirmed baseline contract at %s", out)
    return out


def waive_baseline(
    pipeline_id: str,
    reason: str,
    artifacts_root: Path,
) -> Path:
    """Write ``metric_waiver.json`` for *pipeline_id* and return its path."""
    pdir = _pipeline_dir(pipeline_id, artifacts_root)
    out = pdir / "metric_waiver.json"
    payload = {
        "pipeline_id": pipeline_id,
        "reason": reason,
        "waived_at": datetime.now(timezone.utc).isoformat(),
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    logger.info("Wrote metric waiver at %s", out)
    return out


# -- checker -------------------------------------------------------------


def check_comparability(
    results: dict,
    contract: MetricContract,
) -> dict:
    """Verify *results* against *contract*.

    Returns a report dict:
        {
            "ok": bool,
            "missing_metrics": [...],   # metrics in contract not in results
            "extra_metrics": [...],     # metrics in results not in contract
            "direction_ok": {metric: bool, ...},
            "improvements": {metric: delta, ...},
            "notes": [...]
        }

    Direction check: for ``higher`` we expect ``results[m] >= baseline``
    (delta non-negative is good); for ``lower`` we expect ``results[m] <=
    baseline``. ``direction_ok`` reports whether the improvement has the
    correct sign (not whether it's large).
    """
    notes: list[str] = []
    result_metrics = dict(results or {})

    missing = [m for m in contract.metrics if m not in result_metrics]
    extra = [m for m in result_metrics if m not in contract.metrics]

    direction_ok: dict[str, bool] = {}
    improvements: dict[str, float] = {}

    for metric, direction in contract.metrics.items():
        if metric not in result_metrics:
            continue
        try:
            observed = float(result_metrics[metric])
        except (TypeError, ValueError):
            direction_ok[metric] = False
            notes.append(f"metric {metric} in results is not numeric")
            continue
        baseline = contract.baseline_values.get(metric)
        if baseline is None:
            direction_ok[metric] = True
            improvements[metric] = 0.0
            notes.append(f"no baseline_value for {metric}; direction unchecked")
            continue
        try:
            baseline_f = float(baseline)
        except (TypeError, ValueError):
            direction_ok[metric] = False
            notes.append(f"baseline for {metric} is not numeric")
            continue

        if direction == "higher":
            delta = observed - baseline_f
            direction_ok[metric] = delta >= 0
        elif direction == "lower":
            delta = baseline_f - observed
            direction_ok[metric] = delta >= 0
        else:
            direction_ok[metric] = False
            notes.append(f"unknown direction {direction!r} for {metric}")
            continue
        improvements[metric] = round(delta, 6)

    ok = (
        not missing
        and all(direction_ok.values())
        and all(isinstance(result_metrics[m], (int, float)) for m in contract.metrics if m in result_metrics)
    )

    return {
        "ok": ok,
        "missing_metrics": missing,
        "extra_metrics": extra,
        "direction_ok": direction_ok,
        "improvements": improvements,
        "notes": notes,
    }
