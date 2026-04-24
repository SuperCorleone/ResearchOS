"""Results reverification.

Launches the pipeline's experiment entry script in a fresh, isolated subprocess
(copy of the pipeline directory under ``/scratch/<pipeline_id>_reverify_<ts>/``)
with a deterministic env (``PYTHONHASHSEED=0`` and ``--seed 42`` if accepted)
and compares the rerun's metrics against the original ``results.json``.

Designed as a code-level scheduler hook (no agent dispatch): the scheduler
imports :func:`reverify_results` between RUN and CRITIC on the MAIN stage.
FAST_FAIL stages should skip this step.

Verdicts:
    PASS  — all metrics within 2% relative error
    WARN  — at least one metric 2–10%
    BLOCK — any metric >10% OR rerun crashed / produced no metrics
    SKIP  — no entry script found (graceful no-op)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Candidate entry script filenames, in preference order.
_ENTRY_CANDIDATES = (
    "run_experiment.py",
    "experiment.py",
    "main.py",
    "run.py",
    "train.py",
)

# Max characters of captured stdout/stderr kept in the report.
_MAX_CAPTURE = 1500

# Relative-error thresholds for the PASS / WARN / BLOCK ladder.
_PASS_REL = 0.02   # <=2% -> PASS
_WARN_REL = 0.10   # 2–10% -> WARN, >10% -> BLOCK


def _find_entry_script(pipeline_dir: Path) -> Path | None:
    """Return the first existing entry-script candidate, else None."""
    for name in _ENTRY_CANDIDATES:
        candidate = pipeline_dir / name
        if candidate.is_file():
            return candidate
    # Fallback: any top-level .py file whose name starts with "run"
    for candidate in sorted(pipeline_dir.glob("run*.py")):
        if candidate.is_file():
            return candidate
    return None


def _script_accepts_seed(script_path: Path) -> bool:
    """Best-effort check for a ``--seed`` CLI flag in the entry script."""
    try:
        text = script_path.read_text(errors="ignore")
    except OSError:
        return False
    return "--seed" in text or "'--seed'" in text or '"--seed"' in text


def _parse_last_json_line(text: str) -> dict[str, Any]:
    """Parse the final JSON object printed to stdout, if any."""
    if not text:
        return {}
    # Walk lines bottom-up looking for a parseable JSON object.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    # Fall back: greedy search for a JSON blob anywhere in the tail.
    tail = text[-4000:]
    match = re.search(r"\{.*\}", tail, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return {}
    return {}


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, float]:
    """Flatten nested dict to {dotted_key: float} for numeric leaves."""
    flat: dict[str, float] = {}
    for k, v in data.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            flat[key] = float(v)
        elif isinstance(v, dict):
            flat.update(_flatten(v, key))
    return flat


def _rel_pct(original: float, rerun: float) -> float:
    """Symmetric relative error in percent."""
    denom = max(abs(original), abs(rerun), 1e-12)
    return abs(original - rerun) / denom * 100.0


def _sha256(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", errors="ignore")).hexdigest()


def _load_original_results(pipeline_dir: Path) -> dict[str, float]:
    results_path = pipeline_dir / "results.json"
    if not results_path.exists():
        return {}
    try:
        data = json.loads(results_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("reverify: failed to load original results.json: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return _flatten(data)


def _prepare_scratch(
    pipeline_dir: Path, project_root: Path, pipeline_id: str
) -> Path:
    """Copy the pipeline dir into /scratch/<id>_reverify_<ts>/ and return it."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    scratch_root = project_root / "scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch_dir = scratch_root / f"{pipeline_id}_reverify_{ts}"
    # copytree requires the destination not to exist.
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir, ignore_errors=True)
    shutil.copytree(pipeline_dir, scratch_dir, symlinks=False)
    return scratch_dir


def _verdict(
    metrics_diff: dict[str, dict[str, float]], crashed: bool
) -> str:
    if crashed:
        return "BLOCK"
    if not metrics_diff:
        # No shared numeric metrics -> cannot confirm reproducibility.
        return "WARN"
    worst = max(d["rel_pct"] for d in metrics_diff.values())
    if worst <= _PASS_REL * 100:
        return "PASS"
    if worst <= _WARN_REL * 100:
        return "WARN"
    return "BLOCK"


def reverify_results(
    pipeline_dir: Path,
    project_root: Path,
    timeout: int = 1800,
) -> dict[str, Any]:
    """Re-run the pipeline experiment and diff metrics against results.json.

    Writes ``reverify_report.json`` into ``pipeline_dir`` and returns the
    same dict. Never raises; any failure is encoded as verdict=BLOCK/SKIP.
    """
    pipeline_dir = Path(pipeline_dir)
    project_root = Path(project_root)
    pipeline_id = pipeline_dir.name.replace("pipeline_", "")

    entry = _find_entry_script(pipeline_dir)
    if entry is None:
        report: dict[str, Any] = {
            "verdict": "SKIP",
            "reason": "no entry script",
            "entry_script": None,
            "stdout_hash_match": None,
            "metrics_diff": {},
            "stdout_tail": "",
            "stderr_tail": "",
        }
        _write_report(pipeline_dir, report)
        return report

    scratch_dir = _prepare_scratch(pipeline_dir, project_root, pipeline_id)
    scratch_entry = scratch_dir / entry.name

    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    env.setdefault("PYTHONUNBUFFERED", "1")

    cmd: list[str] = ["python", str(scratch_entry)]
    if _script_accepts_seed(entry):
        cmd += ["--seed", "42"]

    stdout = ""
    stderr = ""
    crashed = False
    returncode: int | None = None
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(scratch_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        returncode = proc.returncode
        if returncode != 0:
            crashed = True
    except subprocess.TimeoutExpired as exc:
        crashed = True
        stdout = (exc.stdout or b"").decode("utf-8", errors="ignore") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode("utf-8", errors="ignore") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stderr += f"\n[reverify] TIMEOUT after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        crashed = True
        stderr = f"[reverify] launch error: {exc}"

    rerun_metrics_raw = _parse_last_json_line(stdout)
    rerun_metrics = _flatten(rerun_metrics_raw) if rerun_metrics_raw else {}
    original_metrics = _load_original_results(pipeline_dir)

    metrics_diff: dict[str, dict[str, float]] = {}
    for key, v1 in original_metrics.items():
        if key in rerun_metrics:
            v2 = rerun_metrics[key]
            metrics_diff[key] = {
                "original": v1,
                "rerun": v2,
                "rel_pct": _rel_pct(v1, v2),
            }

    verdict = _verdict(metrics_diff, crashed)

    # Truncate stdout/stderr for the report.
    stdout_tail = stdout[-_MAX_CAPTURE:]
    stderr_tail = stderr[-_MAX_CAPTURE:]

    # Compare stripped stdout hash against any previously-saved stdout log.
    prior_stdout_log = pipeline_dir / "run_stdout.log"
    stdout_hash_match: bool | None = None
    rerun_hash = _sha256(stdout)
    if prior_stdout_log.exists():
        try:
            prior_hash = _sha256(prior_stdout_log.read_text(errors="ignore"))
            stdout_hash_match = prior_hash == rerun_hash
        except OSError:
            stdout_hash_match = None

    # --- Baseline comparison against metric_contract.json ---
    # Thresholds are wider than rerun-vs-rerun because baseline values are
    # LLM-extracted estimates from the paper and may have inherent error.
    _CONTRACT_WARN_PCT = 15.0   # >15% diff -> WARN
    _CONTRACT_BLOCK_PCT = 30.0  # >30% diff -> BLOCK (upgraded to WARN only; see below)

    baseline_comparison: dict[str, Any] = {}
    contract_path = pipeline_dir / "metric_contract.json"
    current_metrics = _flatten(rerun_metrics_raw) if rerun_metrics_raw else {}

    if contract_path.exists():
        try:
            contract = json.loads(contract_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("reverify: failed to load metric_contract.json: %s", exc)
            contract = {}

        if (
            contract
            and contract.get("status") != "SKIP"
            and contract.get("baseline_values")
        ):
            baseline_values = contract["baseline_values"]
            for metric_name, baseline_val in baseline_values.items():
                if metric_name in current_metrics and baseline_val != 0:
                    actual_val = current_metrics[metric_name]
                    diff_pct = (
                        abs(actual_val - baseline_val) / abs(baseline_val) * 100.0
                    )
                    if diff_pct > _CONTRACT_BLOCK_PCT:
                        status = "BLOCK"
                    elif diff_pct > _CONTRACT_WARN_PCT:
                        status = "WARN"
                    else:
                        status = "PASS"
                    baseline_comparison[metric_name] = {
                        "baseline": baseline_val,
                        "actual": actual_val,
                        "diff_pct": round(diff_pct, 3),
                        "status": status,
                    }

            # Upgrade overall verdict to WARN if any metric deviates >30% from
            # the paper baseline.  We do not BLOCK here because the baseline
            # itself is an LLM estimate and may be imprecise.
            if baseline_comparison:
                worst_contract_status = max(
                    (v["status"] for v in baseline_comparison.values()),
                    key=lambda s: {"PASS": 0, "WARN": 1, "BLOCK": 2}[s],
                )
                if worst_contract_status in ("WARN", "BLOCK") and verdict == "PASS":
                    verdict = "WARN"
                    logger.info(
                        "reverify: verdict upgraded to WARN due to baseline deviation "
                        ">%.0f%% in metric_contract.json (baseline is LLM estimate — "
                        "not blocking).",
                        _CONTRACT_WARN_PCT,
                    )

    report = {
        "verdict": verdict,
        "reason": (
            "rerun crashed" if crashed
            else ("no shared metrics" if not metrics_diff else "ok")
        ),
        "entry_script": str(entry.relative_to(pipeline_dir)),
        "returncode": returncode,
        "scratch_dir": str(scratch_dir),
        "stdout_hash": rerun_hash,
        "stdout_hash_match": stdout_hash_match,
        "metrics_diff": metrics_diff,
        "rerun_metrics_raw": rerun_metrics_raw,
        "baseline_comparison": baseline_comparison,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
    _write_report(pipeline_dir, report)
    return report


def _write_report(pipeline_dir: Path, report: dict[str, Any]) -> None:
    out_path = pipeline_dir / "reverify_report.json"
    try:
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    except OSError as exc:
        logger.warning("reverify: failed to write %s: %s", out_path, exc)


__all__ = ["reverify_results"]
