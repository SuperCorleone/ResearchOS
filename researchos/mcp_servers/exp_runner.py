"""Experiment runner: executes experiment scripts in a sandboxed environment.

V1 implementation: runs Python scripts locally with timeout and output capture.
Future: slurm, docker, cloud execution backends.

Can be used standalone or registered as an MCP tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


async def run_experiment(
    pipeline_id: str,
    *,
    script_path: str | None = None,
    command: str | None = None,
    timeout_seconds: int = 3600,
    working_dir: str | None = None,
) -> dict:
    """Execute an experiment and return structured results.

    Either ``script_path`` or ``command`` must be provided.
    Returns a dict compatible with ``ResultsArtifact`` schema.
    """
    pdir = PROJECT_ROOT / "artifacts" / f"pipeline_{pipeline_id}"
    cwd = Path(working_dir) if working_dir else pdir

    if script_path:
        cmd = ["python", str(script_path)]
    elif command:
        cmd = ["bash", "-c", command]
    else:
        return {
            "metrics": {},
            "config": {},
            "seed": 0,
            "runtime_seconds": 0,
            "notes": "ERROR: No script_path or command provided",
        }

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
        elapsed = time.monotonic() - start

        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")

        # Try to parse last line of stdout as JSON result
        metrics = {}
        config = {}
        seed = 0
        for line in reversed(stdout_text.strip().split("\n")):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    metrics = {
                        k: v for k, v in data.items() if isinstance(v, (int, float))
                    }
                    config = {
                        k: v for k, v in data.items() if isinstance(v, str)
                    }
                    seed = data.get("seed", 0)
                    break
                except json.JSONDecodeError:
                    pass

        result = {
            "metrics": metrics,
            "config": config,
            "seed": seed,
            "runtime_seconds": round(elapsed, 2),
            "notes": "",
            "exit_code": proc.returncode,
            "stdout_tail": stdout_text[-2000:] if stdout_text else "",
            "stderr_tail": stderr_text[-1000:] if stderr_text else "",
        }

        if proc.returncode != 0:
            result["notes"] = f"Process exited with code {proc.returncode}"

        # Write results.json to pipeline dir
        results_path = pdir / "results.json"
        results_path.write_text(json.dumps(result, indent=2))
        logger.info("Experiment completed for %s in %.1fs", pipeline_id, elapsed)

        return result

    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        result = {
            "metrics": {},
            "config": {},
            "seed": 0,
            "runtime_seconds": round(elapsed, 2),
            "notes": f"TIMEOUT after {timeout_seconds}s",
        }
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "results.json").write_text(json.dumps(result, indent=2))
        return result

    except Exception as e:
        elapsed = time.monotonic() - start
        result = {
            "metrics": {},
            "config": {},
            "seed": 0,
            "runtime_seconds": round(elapsed, 2),
            "notes": f"ERROR: {e}",
        }
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "results.json").write_text(json.dumps(result, indent=2))
        return result


def run_experiment_sync(pipeline_id: str, **kwargs) -> dict:
    """Synchronous wrapper for ``run_experiment``."""
    return asyncio.run(run_experiment(pipeline_id, **kwargs))
