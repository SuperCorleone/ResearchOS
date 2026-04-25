"""Dispatch subagent tasks via multiple backends.

Supports three backends:
- **simulate**: writes mock artifacts for testing without LLM calls
- **anthropic**: uses the Anthropic Python SDK directly (default)
- **openharness**: spawns real OpenHarness agents (requires ``oh`` CLI)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from researchos.schemas.digest import DIGEST_FILENAMES
from researchos.schemas.pipeline import PipelineState, PipelineStep
from researchos.runtime.workflow_loader import WorkflowConfig
from researchos.runtime.llm_client import (
    RETRY_EXCEPTIONS,
    estimate_tokens,
    extract_json_between_markers,
    get_context_size,
    strip_reasoning,
    trim_to_budget,
)
from researchos.runtime.step_cache import StepCache
from researchos.utils.paths import ARTIFACTS_DIR, DECISIONS_DIR

logger = logging.getLogger(__name__)

# Artifacts whose content must never be truncated in prompts (load-bearing
# signals the downstream agent relies on verbatim).
CRITICAL_ARTIFACTS: set[str] = {
    "results.json",
    "advisor_verdict.yaml",
    "advisor_verdict.json",
    "metric_contract.json",
    "reverify_report.json",
    "audit_report.md",
    "preflight_report.json",
    "concepts.yaml",
}


def _maybe_compress_artifact(content: str, artifact_name: str, step: str) -> str:
    """Size+importance heuristic: keep short/critical artifacts verbatim, head+tail
    truncate large non-critical artifacts to save prompt tokens."""
    if not isinstance(content, str):
        return content
    if len(content) < 8000:
        return content
    # Check full name or basename against critical whitelist
    base = artifact_name.rsplit("/", 1)[-1] if artifact_name else ""
    if artifact_name in CRITICAL_ARTIFACTS or base in CRITICAL_ARTIFACTS:
        return content
    head = content[:3000]
    tail = content[-2000:]
    dropped = len(content) - len(head) - len(tail)
    return f"{head}\n\n...[truncated {dropped} chars]...\n\n{tail}"


def _load_upstream_digests(
    filtered: dict[str, Any], pdir: Path
) -> tuple[dict[str, Any], list[str]]:
    """Return (digests, missing) for every upstream artifact that owns a digest.

    For each key in ``filtered`` that has a ``DIGEST_FILENAMES`` entry, try to
    load the digest JSON payload from ``pdir``. Returns a dict keyed by the
    artifact key (e.g. ``"hypothesis"``) and a list of keys whose digest was
    declared but not yet on disk (usually fine — step hasn't run).

    The caller injects ``digests`` verbatim into the prompt so the downstream
    agent sees typed ~2KB payloads instead of re-parsing a 15KB markdown.
    """
    digests: dict[str, Any] = {}
    missing: list[str] = []
    for key in filtered:
        digest_filename = DIGEST_FILENAMES.get(key)
        if digest_filename is None:
            continue
        digest_path = pdir / digest_filename
        if not digest_path.exists():
            missing.append(key)
            continue
        try:
            digests[key] = json.loads(digest_path.read_text())
        except Exception:
            logger.debug(
                "Failed to load digest %s for dispatcher injection",
                digest_path,
                exc_info=True,
            )
            missing.append(key)
    return digests, missing


# Agent definition files corresponding to each step.
# Steps whose value is ``None`` are handled by pure-Python hooks in the
# scheduler's ``_maybe_dispatch_specialised_step``. Listing them explicitly
# ensures the dispatcher raises RuntimeError if anyone tries to dispatch
# them through the LLM path — we no longer keep a pipeline_manager fallback.
STEP_AGENT: dict[PipelineStep, str | None] = {
    PipelineStep.HYPOTHESIS: "hypothesis_gen",
    PipelineStep.DESIGN: "exp_designer",
    # DESIGN_REVIEW retired 2026-04 — exp_designer self-reviews in its own turn.
    PipelineStep.CODE: "coder",
    PipelineStep.CODE_PREFLIGHT: "code_preflight",  # Python function, not LLM
    PipelineStep.RUN: "experiment_runner",
    PipelineStep.CRITIC: "critic",
    PipelineStep.INTERROGATION: "interrogator",
    PipelineStep.EXPERIMENT_ANALYSIS: "experiment_analyser",
    PipelineStep.JUDGE: "judge",
    # REPORT is deterministic — rendered by researchos.core.publisher.
    PipelineStep.REPORT: None,
    # Pure-Python hooks handled in the scheduler.
    PipelineStep.BASELINE_CONFIRM: None,
    PipelineStep.RESULTS_REVERIFY: None,
    PipelineStep.CODE_RECON: None,
    # Concept extraction (legacy single-pass + new 2-pass split).
    PipelineStep.CONCEPT_EXTRACT: "concept_extractor",
    PipelineStep.CONCEPT_PAPER: "concept_extract_paper",
    PipelineStep.CONCEPT_CODE: "concept_extract_code",
}

# Expected output artifact for each step
STEP_OUTPUT: dict[PipelineStep, str] = {
    PipelineStep.HYPOTHESIS: "hypothesis.md",
    PipelineStep.DESIGN: "experiment_plan.yaml",
    # DESIGN_REVIEW retired — no separate artifact.
    PipelineStep.CODE: "code_patch.diff",
    PipelineStep.CODE_PREFLIGHT: "preflight_report.json",
    PipelineStep.RUN: "results.json",
    PipelineStep.CRITIC: "analysis_report.md",
    PipelineStep.INTERROGATION: "interrogation_report.md",
    PipelineStep.EXPERIMENT_ANALYSIS: "experiment_analysis.md",
    PipelineStep.JUDGE: "judge_verdict.json",
    PipelineStep.REPORT: "milestone_{n}.md",
}


def _find_oh_binary() -> str:
    """Locate the OpenHarness CLI binary."""
    for name in ("oh", "openharness"):
        path = shutil.which(name)
        if path:
            return path
    return "python -m openharness"


async def _retry_llm_async(
    coro_fn: Any,
    *args: Any,
    _max_tries: int = 5,
    _base_delay: float = 1.0,
    _max_delay: float = 60.0,
    **kwargs: Any,
) -> Any:
    """Async mirror of :func:`researchos.runtime.llm_client.retry_llm`.

    ``retry_llm`` is synchronous (it calls ``time.sleep``), so using it
    around an ``await`` target would block the event loop. This helper
    preserves the same retry policy (``RETRY_EXCEPTIONS``, exponential
    backoff with jitter, 5 tries) but uses ``asyncio.sleep`` between
    attempts. Non-retriable exceptions propagate immediately; the final
    exception is re-raised after retries are exhausted.
    """
    last_exc: BaseException | None = None
    for attempt in range(_max_tries):
        try:
            return await coro_fn(*args, **kwargs)
        except RETRY_EXCEPTIONS as exc:
            last_exc = exc
            if attempt == _max_tries - 1:
                break
            delay = min(_max_delay, _base_delay * (2 ** attempt))
            delay += random.uniform(0, delay * 0.1)
            logger.warning(
                "LLM call failed (%s); retrying in %.1fs (attempt %d/%d)",
                type(exc).__name__, delay, attempt + 1, _max_tries,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


class Dispatcher:
    """Dispatch subagent tasks via configurable backend.

    Backends:
    - ``simulate``: mock artifacts (no LLM)
    - ``anthropic``: direct Claude API via AgentExecutor
    - ``openharness``: headless OpenHarness subprocess
    - ``auto``: try anthropic first (if API key set), else openharness
    """

    def __init__(
        self,
        project_root: Path,
        *,
        simulate: bool = False,
        backend: str = "auto",
    ) -> None:
        self.project_root = project_root
        self.simulate = simulate
        self.backend = backend
        self._running: dict[str, asyncio.subprocess.Process] = {}
        self._executor: Any = None  # lazy AgentExecutor
        self.workflow = WorkflowConfig.load(project_root)
        # Pre-load agent definitions once to avoid re-reading from disk on
        # every dispatch; miss path still falls back to live disk read below.
        self._agent_def_cache: dict[str, str] = {}
        self._preload_agent_definitions()
        # Pipeline-scoped KB query cache: (pipeline_id, normalized_query) -> result.
        self._kb_cache: dict[tuple[str, str], str] = {}

    def _agent_def_search_paths(self) -> list[Path]:
        """Resolution order for agent ``.md`` files.

        Primary home (post-2026-04 refactor): ``researchos/agent_defs/`` —
        platform-neutral; every adapter reads from here. The legacy
        ``.openharness/plugins/researchos/agents/`` path is still scanned as
        a fallback so external clones that haven't migrated keep working.
        """
        return [
            self.project_root / "researchos" / "agent_defs",
            self.project_root / ".openharness" / "plugins" / "researchos" / "agents",
        ]

    def _preload_agent_definitions(self) -> None:
        """Scan every known agent_defs location so per-step prompts don't hit disk."""
        for agents_dir in self._agent_def_search_paths():
            if not agents_dir.exists():
                continue
            for md_path in agents_dir.glob("*.md"):
                # First-match-wins: primary location takes precedence.
                if md_path.stem in self._agent_def_cache:
                    continue
                try:
                    self._agent_def_cache[md_path.stem] = md_path.read_text()
                except Exception:
                    logger.debug(
                        "Failed to preload agent definition %s", md_path, exc_info=True
                    )

    def _get_agent_definition(self, agent_name: str) -> str:
        """Return cached agent definition text; fall back to disk read + cache on miss."""
        cached = self._agent_def_cache.get(agent_name)
        if cached is not None:
            return cached
        for agents_dir in self._agent_def_search_paths():
            md_path = agents_dir / f"{agent_name}.md"
            if md_path.exists():
                try:
                    text = md_path.read_text()
                    self._agent_def_cache[agent_name] = text
                    return text
                except Exception:
                    logger.debug(
                        "Failed to read agent definition %s", md_path, exc_info=True
                    )
        return ""

    @staticmethod
    def _normalize_kb_query(query: str) -> str:
        """Normalize KB query for cache key: lowercase, strip, collapse whitespace."""
        return " ".join((query or "").lower().split())

    def kb_retrieve_cached(
        self,
        pipeline_id: str,
        query: str,
        retrieve_fn: Any,
    ) -> str:
        """Pipeline-scoped KB query cache wrapper; avoids redundant duplicate queries.

        ``retrieve_fn`` is invoked as ``retrieve_fn(query)`` only on cache miss.
        """
        key = (pipeline_id, self._normalize_kb_query(query))
        if key in self._kb_cache:
            return self._kb_cache[key]
        result = retrieve_fn(query)
        if isinstance(result, str):
            self._kb_cache[key] = result
        return result

    def _resolve_backend(self) -> str:
        """Determine which backend to use for live execution."""
        if self.backend != "auto":
            return self.backend
        # Prefer anthropic if API key is available
        if os.environ.get("RESEARCHOS_API_KEY"):
            return "anthropic"
        # Fall back to openharness
        oh = _find_oh_binary()
        if oh != "python -m openharness" or shutil.which("python"):
            return "openharness"
        raise RuntimeError(
            "No agent backend available. Set RESEARCHOS_API_KEY for the "
            "Anthropic backend, or install OpenHarness CLI."
        )

    def _get_executor(self) -> Any:
        """Lazy-init the AgentExecutor."""
        if self._executor is None:
            from researchos.runtime.agent_executor import AgentExecutor

            self._executor = AgentExecutor(self.project_root)
        return self._executor

    def _resolve_agent_model(self, agent_name: str, default_model: str | None) -> str | None:
        """Look up a per-agent model override; return default on miss or import error."""
        try:
            from researchos.runtime.llm_client import get_model_for_agent  # type: ignore
            step_model = get_model_for_agent(agent_name)
            return step_model or default_model
        except ImportError:
            return default_model
        except Exception:
            logger.debug("get_model_for_agent failed for %s", agent_name, exc_info=True)
            return default_model

    async def dispatch_via_executor(
        self, agent_name: str, prompt: str, pipeline_id: str
    ) -> dict[str, Any]:
        """Run an agent via AgentExecutor (Anthropic SDK).

        Output is normalised via :func:`strip_reasoning` to drop any
        ``<think>...</think>`` preamble, and a best-effort JSON extraction
        is attached as ``result["parsed_json"]`` when the output contains
        a fenced or bare JSON object.
        """
        executor = self._get_executor()
        # Per-agent model selection hook: temporarily override executor.model
        # if a model mapping is registered for this agent.
        default_model = getattr(executor, "model", None)
        step_model = self._resolve_agent_model(agent_name, default_model)
        model_overridden = False
        if step_model and step_model != default_model:
            try:
                executor.model = step_model
                model_overridden = True
            except Exception:
                logger.debug("Could not set executor.model; using default", exc_info=True)
        # Internal system calls (e.g. _system_evaluator, _system_miner) use
        # scratch/ instead of artifacts/ to avoid creating spurious pipeline dirs
        if pipeline_id.startswith("_system_"):
            pdir = self.project_root / "scratch" / pipeline_id
        else:
            pdir = self.project_root / "artifacts" / f"pipeline_{pipeline_id}"
        pdir.mkdir(parents=True, exist_ok=True)
        # Wrap the LLM-backed executor call with exponential-backoff retry so
        # transient RateLimitError / APITimeoutError / network errors don't
        # poison the pipeline. Exhausted retries re-raise the last exception.
        try:
            result = await _retry_llm_async(
                executor.execute, agent_name, prompt, pipeline_id, pdir
            )
        finally:
            # Always restore the default model so subsequent calls aren't
            # affected by this per-agent override.
            if model_overridden:
                try:
                    executor.model = default_model
                except Exception:
                    logger.debug("Could not restore executor.model", exc_info=True)

        # Best-effort post-processing: don't break existing callers
        try:
            output = result.get("output", "") if isinstance(result, dict) else ""
            if isinstance(output, str) and output:
                cleaned = strip_reasoning(output)
                if cleaned != output:
                    result["output"] = cleaned
                parsed = extract_json_between_markers(cleaned)
                if parsed is not None:
                    result["parsed_json"] = parsed
        except Exception:
            logger.debug("post-processing of executor output failed", exc_info=True)
        return result

    @staticmethod
    def parse_json_output(text: str) -> dict | None:
        """Convenience helper: extract a JSON object from raw LLM text.

        Delegates to :func:`extract_json_between_markers` after stripping
        any ``<think>`` reasoning block.
        """
        return extract_json_between_markers(strip_reasoning(text or ""))

    async def dispatch_step(
        self, pipeline: PipelineState, step: PipelineStep
    ) -> bool:
        """Dispatch a subagent for one atomic pipeline step. Returns True on success."""
        if self.simulate:
            self._simulate_step(pipeline, step)
            return True

        # ── Step cache: skip already-completed steps ─────────────
        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        cache = StepCache(pdir)
        if cache.has_completed(step.value, pipeline.stage.value):
            logger.info(
                "Step %s already cached for pipeline %s (stage %s), skipping",
                step.value, pipeline.pipeline_id, pipeline.stage.value,
            )
            return True

        # CODE_PREFLIGHT runs as pure Python — no LLM call
        if step == PipelineStep.CODE_PREFLIGHT:
            return self._run_code_preflight(pipeline)

        prompt = self._build_step_prompt(pipeline, step)
        agent_name = (
            self.workflow.get_step_agent(step.value)
            or STEP_AGENT.get(step)
        )
        if agent_name is None:
            raise RuntimeError(
                f"Step {step.value!r} has no registered agent. "
                f"Either the scheduler should have handled it as a "
                f"specialised pure-Python hook (BASELINE_CONFIRM, "
                f"RESULTS_REVERIFY, CODE_RECON, CODE_PREFLIGHT) or the "
                f"workflow_config.yaml is missing an entry. "
                f"Refusing to fall back to a generic agent."
            )
        logger.info(
            "Dispatching step=%s for pipeline=%s", step.value, pipeline.pipeline_id
        )

        backend = self._resolve_backend()
        if backend == "anthropic":
            result = await self.dispatch_via_executor(
                agent_name, prompt, pipeline.pipeline_id
            )
            if not result["success"]:
                logger.error(
                    "Agent %s failed: %s", agent_name, result.get("output", "unknown error")
                )
                return False
            # Save successful step to cache
            cache.save_completed(step.value, pipeline.stage.value, {
                "agent": agent_name,
                "tokens_used": result.get("tokens_used", 0),
                "artifacts_written": result.get("artifacts_written", []),
            })
            return True
        else:
            # OpenHarness subprocess path
            proc = await self._spawn_agent(
                prompt, f"step_{pipeline.pipeline_id}_{step.value}"
            )
            if proc:
                self._running[pipeline.pipeline_id] = proc
                return True
            return False

    async def dispatch_professor(
        self, pipeline_id: str, context_files: list[str]
    ) -> asyncio.subprocess.Process | None:
        """Dispatch the professor agent for a decision."""
        if self.simulate:
            self._simulate_professor(pipeline_id)
            return None

        prompt = self._build_professor_prompt(pipeline_id, context_files)
        logger.info("Dispatching professor for pipeline=%s", pipeline_id)

        backend = self._resolve_backend()
        if backend == "anthropic":
            result = await self.dispatch_via_executor(
                "professor", prompt, pipeline_id
            )
            if not result["success"]:
                logger.error("Professor agent failed: %s", result.get("output", ""))
            return None
        else:
            return await self._spawn_agent(prompt, f"professor_{pipeline_id}")

    def is_running(self, pipeline_id: str) -> bool:
        if self.simulate:
            return False
        # Anthropic backend completes inline, never "running"
        if self._resolve_backend() == "anthropic":
            return False
        proc = self._running.get(pipeline_id)
        if proc is None:
            return False
        if proc.returncode is not None:
            del self._running[pipeline_id]
            return False
        return True

    async def wait(self, pipeline_id: str, timeout: float = 300) -> int | None:
        """Wait for a dispatched task to complete."""
        proc = self._running.get(pipeline_id)
        if proc is None:
            return 0 if self.simulate else None
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            return proc.returncode
        except asyncio.TimeoutError:
            logger.warning("Task for pipeline=%s timed out", pipeline_id)
            proc.kill()
            return -1
        finally:
            self._running.pop(pipeline_id, None)

    # ── Simulation mode ──────────────────────────────────────────

    def _simulate_step(self, pipeline: PipelineState, step: PipelineStep) -> None:
        """Write mock artifacts for testing."""
        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        pdir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()

        if step == PipelineStep.HYPOTHESIS:
            (pdir / "hypothesis.md").write_text(
                f"# Hypothesis\n\n"
                f"## Research Question\n{pipeline.research_question}\n\n"
                f"## Hypothesis Statement\n"
                f"We hypothesise that the answer to the research question can be "
                f"addressed through systematic experimentation.\n\n"
                f"## Testable Predictions\n"
                f"1. Prediction A: measurable outcome expected\n"
                f"2. Prediction B: secondary outcome expected\n\n"
                f"## Key Variables\n"
                f"- Independent: experimental condition\n"
                f"- Dependent: measured outcome\n\n"
                f"_Generated at {ts} (simulation mode)_\n"
            )

        elif step == PipelineStep.DESIGN:
            plan = {
                "experiment": {
                    "name": f"exp_{pipeline.pipeline_id}",
                    "hypothesis_ref": "hypothesis.md",
                    "type": "validation",
                },
                "setup": {
                    "environment": "local",
                    "dependencies": [],
                    "estimated_runtime": "30 minutes",
                    "estimated_cost_credits": 500,
                },
                "variables": {
                    "independent": [{"name": "condition", "values": ["A", "B"]}],
                    "dependent": [{"name": "accuracy", "measurement": "percentage"}],
                    "controls": [{"name": "seed", "fixed_value": "42"}],
                },
                "evaluation": {
                    "metrics": [
                        {"name": "accuracy", "threshold": 0.7, "direction": "higher_better"}
                    ],
                    "seeds": [42, 123, 456],
                    "num_runs": 3,
                },
                "success_criteria": ["accuracy > 0.7 across all seeds"],
            }
            (pdir / "experiment_plan.yaml").write_text(
                yaml.dump(plan, default_flow_style=False)
            )

        elif step == PipelineStep.CODE:
            (pdir / "code_patch.diff").write_text(
                f"--- /dev/null\n"
                f"+++ experiment_{pipeline.pipeline_id}.py\n"
                f"@@ -0,0 +1,20 @@\n"
                f"+# Experiment: {pipeline.title}\n"
                f"+# Auto-generated experiment code (simulation)\n"
                f"+import random\n"
                f"+\n"
                f"+def run_experiment(seed=42, condition='A'):\n"
                f"+    random.seed(seed)\n"
                f"+    base = 0.65 if condition == 'A' else 0.72\n"
                f"+    noise = random.uniform(-0.05, 0.05)\n"
                f"+    return {{'accuracy': base + noise, 'condition': condition, 'seed': seed}}\n"
                f"+\n"
                f"+if __name__ == '__main__':\n"
                f"+    for seed in [42, 123, 456]:\n"
                f"+        for cond in ['A', 'B']:\n"
                f"+            result = run_experiment(seed, cond)\n"
                f"+            print(result)\n"
            )

        elif step == PipelineStep.RUN:
            results = {
                "metrics": {"accuracy": 0.73, "f1_score": 0.71, "latency_ms": 120.5},
                "config": {"condition": "B", "model_size": "base"},
                "seed": 42,
                "runtime_seconds": 1800.0,
                "notes": "Simulation mode - mock results",
            }
            (pdir / "results.json").write_text(json.dumps(results, indent=2))

        elif step == PipelineStep.CRITIC:
            (pdir / "analysis_report.md").write_text(
                f"# Analysis Report\n\n"
                f"## Summary\n"
                f"The experiment produced positive results with accuracy of 0.73, "
                f"exceeding the threshold of 0.70.\n\n"
                f"## Hypothesis Evaluation\n"
                f"- **Verdict**: SUPPORTED\n"
                f"- **Confidence**: MEDIUM\n\n"
                f"## Key Metrics\n"
                f"| Metric | Expected | Observed | Status |\n"
                f"|--------|----------|----------|--------|\n"
                f"| accuracy | > 0.70 | 0.73 | PASS |\n"
                f"| f1_score | > 0.65 | 0.71 | PASS |\n\n"
                f"## Recommendation\n"
                f"ADVANCE - results are promising, proceed to milestone report.\n\n"
                f"_Generated at {ts} (simulation mode)_\n"
            )

        elif step == PipelineStep.INTERROGATION:
            (pdir / "interrogation_report.md").write_text(
                f"# Interrogation Report\n\n"
                f"## Round 1\n\n"
                f"### Q1: Are the metrics consistent across all evaluation dimensions?\n"
                f"**Response**: Yes, accuracy and F1 trends align.\n"
                f"**Assessment**: SATISFIED\n\n"
                f"### Q2: Could the performance improvement be attributed to hyperparameter tuning alone?\n"
                f"**Response**: Ablation without tuning shows 90% of improvement retained.\n"
                f"**Assessment**: SATISFIED\n\n"
                f"## Final Verdict: SATISFIED\n"
                f"No critical issues found. Proceed to milestone report.\n\n"
                f"_Generated at {ts} (simulation mode)_\n"
            )

        elif step == PipelineStep.CODE_PREFLIGHT:
            report = {
                "verdict": "PASS",
                "checks": {
                    "syntax_valid": True,
                    "imports_available": True,
                    "models_match_plan": True,
                    "metrics_match_plan": True,
                    "data_source_match": True,
                    "seed_parameter_accepted": True,
                    "timeout_risk": "LOW",
                },
                "g3_implementation_coverage": {
                    "claims_total": 3,
                    "claims_mapped": 3,
                    "trivial_implementations": 0,
                },
                "issues": [],
                "simulated": True,
            }
            (pdir / "preflight_report.json").write_text(
                json.dumps(report, indent=2)
            )

        elif step == PipelineStep.REPORT:
            n = pipeline.milestone_count + 1
            (pdir / f"milestone_{n}.md").write_text(
                f"# Milestone Report: {pipeline.title}\n\n"
                f"**Pipeline ID**: {pipeline.pipeline_id}\n"
                f"**Milestone**: M{n}\n"
                f"**Date**: {ts}\n\n"
                f"## Executive Summary\n"
                f"Pipeline completed one full experiment cycle with positive results. "
                f"Accuracy of 0.73 exceeds the 0.70 threshold.\n\n"
                f"## Key Findings\n"
                f"- Hypothesis is supported with medium confidence\n"
                f"- Condition B outperforms Condition A\n\n"
                f"## Resource Usage\n"
                f"- Budget allocated: {pipeline.budget_allocated}\n"
                f"- Budget used: {pipeline.budget_used}\n\n"
                f"## Recommendation\n"
                f"CONTINUE -- refine approach with additional experiments.\n\n"
                f"_Generated at {ts} (simulation mode)_\n"
            )

        logger.info(
            "Simulated step=%s for pipeline=%s", step.value, pipeline.pipeline_id
        )

    # ── Code preflight (Python, no LLM) ─────────────────────────

    def _run_code_preflight(self, pipeline: PipelineState) -> bool:
        """Run code preflight checks as pure Python — no LLM call.

        Checks:
        1. Syntax validity (compile check)
        2. Import availability
        3. Models/metrics match experiment_plan.yaml
        4. Data source consistency
        5. G3 claim-to-code mapping completeness
        6. Timeout/cost risk estimation
        """
        from researchos.runtime.preflight import run_preflight

        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        report = run_preflight(pdir, self.project_root)

        # Write report
        report_path = pdir / "preflight_report.json"
        report_path.write_text(json.dumps(report, indent=2))

        verdict = report["verdict"]
        logger.info(
            "Code preflight for %s: %s (%d issues)",
            pipeline.pipeline_id,
            verdict,
            len(report.get("issues", [])),
        )
        if verdict == "BLOCK":
            logger.warning(
                "Code preflight BLOCKED pipeline %s: %s",
                pipeline.pipeline_id,
                [i["detail"] for i in report.get("issues", []) if i["severity"] == "ERROR"],
            )
            return False
        return True

    def _simulate_professor(self, pipeline_id: str) -> None:
        """Write a mock professor approval decision."""
        ts = datetime.now(timezone.utc)
        decision = {
            "pipeline_id": pipeline_id,
            "decision": "approve",
            "next_milestone": "M2",
            "comment": "Results look promising. Continue with expanded experiments. (simulation)",
            "created_at": ts.isoformat(),
        }
        fname = f"approval_{pipeline_id}_{ts.strftime('%Y%m%d%H%M%S')}.yaml"
        path = self.project_root / "decisions" / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(decision, default_flow_style=False))
        logger.info("Simulated professor decision for pipeline=%s", pipeline_id)

    # ── F' Reporter lazy injection ─────────────────────────────

    _PASS_VERDICTS = {"pass", "supported", "advance", "satisfied"}

    def _maybe_compress_report_artifacts(
        self, artifacts: dict[str, str], pdir: Path,
    ) -> dict[str, str]:
        """Compress non-metric artifacts for REPORT when upstream verdicts are clean.

        Checks critic and interrogation verdicts. If both are clean, replaces
        hypothesis/design/critic/interrogation with one-line sentinels.
        results.json kept verbatim (reporter needs full metrics for G4).
        """
        critic_path = pdir / "analysis_report.md"
        interrog_path = pdir / "interrogation_report.md"
        critic_clean = False
        interrog_clean = False
        try:
            if critic_path.exists():
                text = critic_path.read_text()[:2000].lower()
                for v in self._PASS_VERDICTS:
                    if f"verdict**: {v}" in text or f"verdict: {v}" in text:
                        critic_clean = True
                        break
        except Exception:
            pass
        try:
            if interrog_path.exists():
                text = interrog_path.read_text()[:2000].lower()
                for v in self._PASS_VERDICTS:
                    if f"verdict: {v}" in text or f"verdict**: {v}" in text:
                        interrog_clean = True
                        break
        except Exception:
            pass

        if not (critic_clean and interrog_clean):
            return artifacts

        compressed = dict(artifacts)
        _sentinels = {
            "hypothesis": "[HYPOTHESIS_PRESENT — read via file_read if details needed]",
            "design": "[DESIGN_PRESENT — read via file_read if details needed]",
            "critic": "[CRITIC_VERDICT: PASS — read analysis_report.md for details]",
            "interrogation": "[INTERROGATION_VERDICT: SATISFIED — read interrogation_report.md for details]",
        }
        for key, sentinel in _sentinels.items():
            if key in compressed:
                compressed[key] = sentinel
        return compressed

    # ── Prompt builders ──────────────────────────────────────────

    def _build_step_prompt(self, pipeline: PipelineState, step: PipelineStep) -> str:
        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        state_path = pdir / "state.json"
        agent_name = (
            self.workflow.get_step_agent(step.value)
            or STEP_AGENT.get(step)
        )
        if agent_name is None:
            raise RuntimeError(
                f"_build_step_prompt: step {step.value!r} has no registered "
                f"agent. workflow_config.yaml or STEP_AGENT is missing an entry."
            )

        # Load agent definition from pre-built cache (falls back to disk on miss).
        agent_def_text = self._get_agent_definition(agent_name)
        agent_instructions = ""
        if agent_def_text:
            agent_instructions = (
                f"\n## Agent Protocol\n"
                f"Follow the instructions in the {agent_name} agent definition:\n\n"
                f"{agent_def_text}\n"
            )

        # Get the expected output filename for this step
        expected_output = self.workflow.get_step_output(step.value) or STEP_OUTPUT.get(step, "output.md")
        if step == PipelineStep.REPORT:
            expected_output = f"milestone_{pipeline.milestone_count + 1}.md"

        # Automated structural checks — fed to exp_designer during its
        # self-review pass (the separate design_reviewer agent was retired
        # 2026-04; structural checks still live in design_checks.py, the
        # scientific-validity pass is integrated into exp_designer.md).
        # On DESIGN re-runs the previous draft sits at experiment_plan.yaml
        # and validate_design() can therefore surface its gaps inline.
        design_check_notice = ""
        if step == PipelineStep.DESIGN and (pdir / "experiment_plan.yaml").exists():
            from researchos.runtime.design_checks import validate_design
            design_report = validate_design(pdir)
            checks = design_report["checks"]
            fail_count = design_report["fail_count"]
            if fail_count == 0:
                design_check_notice = (
                    "\n## Automated Structural Checks (previous draft)\n"
                    "[ALL_STRUCTURAL_CHECKS_PASS — focus this revision on the "
                    "Self-Review items in your agent definition.]\n"
                )
            else:
                failed_lines = [
                    f"| {name} | FAIL |"
                    for name, passed in checks.items()
                    if not passed
                ]
                issues_text = "\n".join(
                    f"- [{i['severity']}] {i['check']}: {i['detail']}"
                    for i in design_report["issues"]
                )
                design_check_notice = (
                    f"\n## Automated Structural Checks (previous draft)\n"
                    f"{fail_count} FAIL / {design_report['pass_count']} PASS "
                    f"(PASS omitted).\n"
                    f"| Check | Status |\n|-------|--------|\n"
                    + "\n".join(failed_lines)
                    + "\n"
                )
                if issues_text:
                    design_check_notice += f"\n### Issues Found\n{issues_text}\n"
                design_check_notice += (
                    "\nAddress the failed structural checks in the revised "
                    "experiment_plan.yaml. Scientific validity items are "
                    "covered by your Self-Review section.\n"
                )

        # Build data compliance notice for RUN step
        data_notice = ""
        if step == PipelineStep.RUN:
            from researchos.runtime.hooks import validate_data_source_compliance
            compliance = validate_data_source_compliance(
                pipeline.pipeline_id, self.project_root
            )
            if not compliance["compliant"]:
                data_notice = (
                    f"\n## ⚠ DATA COMPLIANCE — ACTION REQUIRED\n"
                    f"The experiment plan specifies `data.source: \"{compliance['source']}\"`.\n"
                    f"**{compliance['reason']}**\n"
                    f"You MUST call `fetch_dataset` with the URL from experiment_plan.yaml "
                    f"BEFORE running the experiment. Do NOT use built-in dataset loaders.\n"
                )
            elif compliance["source"] in ("file", "api"):
                data_notice = (
                    f"\n## Data Source: {compliance['source']}\n"
                    f"Data is available in `data/raw/{pipeline.pipeline_id}/`. "
                    f"Use it directly — do NOT substitute with built-in loaders.\n"
                )
        # Inject data_manifest.yaml + baseline_analysis.yaml summary for DESIGN.
        # exp_designer.md mandates filling plan_dataset.dataset_location and
        # read_data_step from these artifacts; we surface the key fields right
        # in the prompt rather than relying on the LLM to remember to file_read.
        # The baseline_analysis context (with code-evidence per dataset path)
        # gives DESIGN a much stronger signal than just "files exist".
        if step == PipelineStep.DESIGN:
            manifest_path = pdir / "data_manifest.yaml"
            if manifest_path.exists():
                try:
                    mf = yaml.safe_load(manifest_path.read_text()) or {}
                except Exception:
                    mf = {}
                status = mf.get("data_recon_status", "unknown")
                files = mf.get("files", []) or []
                file_lines = "\n".join(
                    f"    - {f.get('path', '?')}  ({f.get('size_bytes', '?')} bytes"
                    + (f", {f.get('rows')} rows" if f.get("rows") else "")
                    + ")"
                    for f in files[:20]
                )
                more = (
                    f"\n    ... ({len(files) - 20} more files)" if len(files) > 20 else ""
                )
                data_notice += (
                    f"\n## DATA_RECON Output (data_manifest.yaml)\n"
                    f"DATA_RECON ran with status `{status}`. The following files "
                    f"are available under `data/raw/{pipeline.pipeline_id}/`:\n"
                    f"{file_lines}{more}\n"
                    f"\nManifest fields: dataset_name="
                    f"`{mf.get('dataset_name', '')}`, "
                    f"source_url=`{mf.get('source_url', '')}`, "
                    f"directory_hash=`{(mf.get('directory_hash') or '')[:30]}...`\n"
                    f"\nYou MUST use this data — set `data.source: \"file\"`, "
                    f"`plan_dataset.dataset_location: \"data/raw/{pipeline.pipeline_id}/\"`, "
                    f"and reference real filenames in `read_data_step`.\n"
                )

            analysis_path = pdir / "baseline_analysis.yaml"
            if analysis_path.exists():
                try:
                    ba = yaml.safe_load(analysis_path.read_text()) or {}
                except Exception:
                    ba = {}
                strategy = ba.get("analysis_strategy", "unknown")
                ba_status = ba.get("status", "unknown")
                rows = []
                for repo in ba.get("analyzed_repos", []) or []:
                    role = repo.get("role", "")
                    rows.append(f"  Repo `{repo.get('slug','?')}` — {role}")
                    for ds in (repo.get("dataset_paths", []) or [])[:8]:
                        rows.append(
                            f"    · {ds.get('path','?')} "
                            f"(kind={ds.get('kind','unknown')}) — {ds.get('purpose','')}"
                        )
                    calls = repo.get("code_data_calls", []) or []
                    if calls:
                        rows.append(
                            f"    · evidence: {len(calls)} IO call(s) grepped from entry points"
                        )
                analyser_block = "\n".join(rows) if rows else "  (no analysed repos)"
                ext_lines = []
                for ext in (ba.get("external_data_urls", []) or [])[:5]:
                    flag = "DOWNLOAD" if ext.get("should_download") else "SKIP"
                    ext_lines.append(
                        f"  · [{flag}] {ext.get('url','?')} "
                        f"(kind={ext.get('kind','other')}) — {ext.get('reason','')}"
                    )
                ext_block = "\n".join(ext_lines) if ext_lines else "  (no external URLs)"
                data_notice += (
                    f"\n## BASELINE_ANALYZE Output (baseline_analysis.yaml)\n"
                    f"strategy=`{strategy}` status=`{ba_status}` "
                    f"fallback_used=`{ba.get('fallback_used', False)}`\n\n"
                    f"Analysed repos:\n{analyser_block}\n\n"
                    f"External URL decisions:\n{ext_block}\n\n"
                    f"This is the LLM-grounded view of the baseline data — prefer it over "
                    f"any tentative conclusions you might draw from paper text alone.\n"
                )

        # Build reference paper context
        ref_notice = ""
        if pipeline.evidence_refs:
            refs_list = "\n".join(f"  - {ref}" for ref in pipeline.evidence_refs)
            ref_notice = (
                f"\n## Reference Papers\n"
                f"This pipeline is based on / aims to improve upon:\n{refs_list}\n"
                f"**Data Consistency Rule**: When improving upon a referenced paper, "
                f"you MUST use the same dataset, same train/test splits, and same "
                f"preprocessing pipeline as the reference. Do NOT generate synthetic "
                f"data or substitute with different datasets.\n"
            )

        # Inject pending user directives for DESIGN/CODE/RUN steps
        directives_notice = ""
        if step in (PipelineStep.DESIGN, PipelineStep.CODE, PipelineStep.RUN):
            directives_path = pdir / "user_directives.yaml"
            if directives_path.exists():
                try:
                    all_dirs = yaml.safe_load(directives_path.read_text()) or []
                    if isinstance(all_dirs, list):
                        pending = [d for d in all_dirs if not d.get("applied", False)]
                        if pending:
                            items = "\n".join(
                                f"  {i+1}. {d['directive']}"
                                for i, d in enumerate(pending)
                            )
                            directives_notice = (
                                f"\n## ⚠ USER DIRECTIVES\n"
                                f"The user has provided the following instructions "
                                f"that you MUST incorporate:\n{items}\n"
                                f"Integrate these directives into your output.\n"
                            )
                            # Mark directives as applied
                            for d in all_dirs:
                                if not d.get("applied", False):
                                    d["applied"] = True
                            directives_path.write_text(
                                yaml.dump(all_dirs, default_flow_style=False)
                            )
                except Exception:
                    pass

        # For CODE/RUN/CRITIC: extract consistency_requirement from experiment_plan
        consistency_notice = ""
        if step in (PipelineStep.CODE, PipelineStep.RUN, PipelineStep.CRITIC):
            plan_path = pdir / "experiment_plan.yaml"
            if plan_path.exists():
                try:
                    import yaml as _yaml
                    plan_data = _yaml.safe_load(plan_path.read_text()) or {}
                    cons_req = plan_data.get("data", {}).get("consistency_requirement")
                    if cons_req:
                        ref_paper = cons_req.get("reference_paper", "unknown")
                        must_match = cons_req.get("must_match", [])
                        verification = cons_req.get("verification", "")
                        consistency_notice = (
                            f"\n## ⚠ DATA CONSISTENCY REQUIREMENT\n"
                            f"Reference paper: **{ref_paper}**\n"
                            f"Must match: {', '.join(must_match)}\n"
                            f"Verification: {verification}\n"
                            f"You MUST use the exact same data as the reference paper. "
                            f"Do NOT generate your own data or use alternative datasets.\n"
                        )
                except Exception:
                    pass

        # For CONCEPT_EXTRACT: inject code_recon.yaml content directly into
        # the prompt so the agent has it without needing a file_read round-trip.
        code_recon_notice = ""
        if step == PipelineStep.CONCEPT_EXTRACT:
            recon_path = pdir / "code_recon.yaml"
            if recon_path.exists():
                try:
                    recon_text = recon_path.read_text()
                    code_recon_notice = (
                        "\n## Code Reconnaissance (code_recon.yaml)\n"
                        "The following code_recon.yaml was produced by the CODE_RECON step. "
                        "Use it to ground `code_implementation` and `reference_codebases` "
                        "fields in real code. Search the cloned repos listed below for "
                        "concrete implementations of each concept.\n\n"
                        f"```yaml\n{recon_text}\n```\n"
                    )
                except Exception:
                    logger.debug("Failed to read code_recon.yaml for prompt injection", exc_info=True)

        # For CODE step: inject concepts.yaml and code_recon.yaml content directly
        # so the coder agent has full context without extra file_read round-trips.
        code_step_context_notice = ""
        if step == PipelineStep.CODE:
            concepts_path = pdir / "concepts.yaml"
            if concepts_path.exists():
                try:
                    concepts_text = concepts_path.read_text()
                    code_step_context_notice += (
                        "\n## === ATOMIC CONCEPTS (concepts.yaml) ===\n"
                        "The following atomic concepts were extracted by the CONCEPT_EXTRACT step. "
                        "Use them to guide your implementation decisions.\n\n"
                        f"```yaml\n{concepts_text}\n```\n"
                    )
                except Exception:
                    logger.debug("Failed to read concepts.yaml for CODE prompt injection", exc_info=True)
            recon_path_code = pdir / "code_recon.yaml"
            if recon_path_code.exists():
                try:
                    recon_text_code = recon_path_code.read_text()
                    # Check if all repos failed
                    try:
                        import yaml as _yaml_recon
                        recon_data = _yaml_recon.safe_load(recon_text_code) or {}
                        repos = recon_data.get("repos", []) if isinstance(recon_data, dict) else []
                        all_error = repos and all(
                            r.get("status") == "error" for r in repos if isinstance(r, dict)
                        )
                    except Exception:
                        all_error = False
                    recon_warning = (
                        "\nNOTE: All reference code repos failed to clone. "
                        "Implement based on paper description only.\n"
                        if all_error else ""
                    )
                    code_step_context_notice += (
                        "\n## === REFERENCE CODE RECON (code_recon.yaml) ===\n"
                        "The following code_recon.yaml was produced by the CODE_RECON step. "
                        "Use it to ground your implementation in real reference codebases.\n"
                        f"{recon_warning}\n"
                        f"```yaml\n{recon_text_code}\n```\n"
                    )
                except Exception:
                    logger.debug("Failed to read code_recon.yaml for CODE prompt injection", exc_info=True)

        # Only inject artifacts relevant to this step to save tokens
        _relevant_artifacts = {
            PipelineStep.HYPOTHESIS: set(),  # reads from KB only
            PipelineStep.DESIGN: {"hypothesis"},
            PipelineStep.CONCEPT_PAPER: {"hypothesis", "design"},
            PipelineStep.CONCEPT_CODE: {"concept_paper", "code_recon"},
            PipelineStep.CODE: {"hypothesis", "design", "concept_extract", "concept_code", "code_recon"},
            PipelineStep.RUN: {"design", "code"},
            PipelineStep.CRITIC: {"hypothesis", "design", "run", "code_preflight"},
            PipelineStep.INTERROGATION: {"run", "critic"},
            PipelineStep.JUDGE: {"hypothesis", "design", "concept_code", "run", "code_preflight"},
            PipelineStep.REPORT: {"hypothesis", "design", "run", "critic", "interrogation", "judge"},
        }
        relevant_keys = _relevant_artifacts.get(step)
        if relevant_keys is not None:
            filtered = {k: v for k, v in pipeline.artifacts.items() if k in relevant_keys}
        else:
            filtered = pipeline.artifacts

        # F' — Reporter lazy injection: when critic+interrogation verdicts are
        # clean, compress non-metric artifacts to one-line sentinels. The
        # reporter still gets full results.json (metrics) but saves ~30-40K
        # tokens by not ingesting full hypothesis/design/critic/interrogation.
        if step == PipelineStep.REPORT:
            filtered = self._maybe_compress_report_artifacts(filtered, pdir)

        # Apply size+importance compression heuristic to any artifact whose
        # value is a large non-critical string (filenames stay untouched).
        filtered = {
            k: _maybe_compress_artifact(v, k if not isinstance(v, str) else v, step.value)
            if isinstance(v, str)
            else v
            for k, v in filtered.items()
        }

        # Typed-digest injection: for every upstream artifact that owns a
        # ``<step>.digest.json`` (see researchos.schemas.digest.DIGEST_FILENAMES),
        # embed the digest payload inline so the downstream agent consumes a
        # ~2KB typed JSON blob instead of re-parsing the original 15KB markdown.
        # Full markdown remains on disk for archival and can still be read via
        # ``file_read`` when the agent needs a specific excerpt.
        upstream_digests, digests_missing = _load_upstream_digests(filtered, pdir)
        digests_notice = ""
        if upstream_digests:
            digests_notice = (
                "\n## Upstream Typed Digests (Pydantic-validated, authoritative)\n"
                "These are the machine-consumable summaries of prior steps. "
                "Prefer them over re-reading the original markdown — every field "
                "is contract-enforced. Original files remain available via "
                "`file_read` if you need a specific excerpt.\n\n"
                f"```json\n{json.dumps(upstream_digests, indent=2)}\n```\n"
            )
            if digests_missing:
                digests_notice += (
                    "\nDigests pending for: "
                    + ", ".join(sorted(digests_missing))
                    + " (step not yet run — fall back to file_read on the "
                    "filename listed under Existing Artifacts).\n"
                )

        prompt = f"""You are executing an atomic research step as the **{agent_name}** agent.

## Task
Execute step: **{step.value}** for pipeline: **{pipeline.pipeline_id}**

## Pipeline Info
- Title: {pipeline.title}
- Research Question: {pipeline.research_question}
- Current Stage: {pipeline.stage.value}
{design_check_notice}{data_notice}{ref_notice}{consistency_notice}{directives_notice}{code_recon_notice}{code_step_context_notice}{digests_notice}
## CRITICAL: Required Output
You MUST write exactly this file using file_write: **{expected_output}**
Use the filename as-is (relative path). Do NOT use absolute paths.

## Instructions
1. Prefer Upstream Typed Digests (above) to re-reading raw markdown. Use `file_read` only for excerpts the digest omits.
2. Use `kb_retrieve` to gather relevant knowledge before producing output
3. For the RUN step: use `execute_python` to run actual experiments, then write results to results.json
4. Write the output artifact using file_write with path: {expected_output}
5. Do NOT modify state.json

## Existing Artifacts
{json.dumps(filtered, indent=2)}
{agent_instructions}"""

        # Proactive token budget — reserve 15% for output
        budget = int(get_context_size(self._resolve_agent_model(agent_name, None) or "") * 0.85)
        if estimate_tokens(prompt) > budget:
            logger.warning(
                "Prompt for %s/%s exceeds budget (%d tokens), trimming",
                pipeline.pipeline_id, step.value, estimate_tokens(prompt),
            )
            prompt = trim_to_budget(prompt, budget)
        return prompt

    def _build_professor_prompt(
        self, pipeline_id: str, context_files: list[str]
    ) -> str:
        files_list = "\n".join(f"- {f}" for f in context_files)
        decisions_dir = self.project_root / "decisions"

        # Load professor agent definition from the startup cache (with disk fallback).
        prof_def_text = self._get_agent_definition("professor")
        prof_instructions = ""
        if prof_def_text:
            prof_instructions = (
                f"\n## Agent Protocol\n{prof_def_text}\n"
            )

        return f"""You are the Professor agent making a research decision.

## Task
Review pipeline **{pipeline_id}** and write a decision.

## Files to Review
{files_list}

## Instructions
1. Read each file listed above using file_read
2. Use `kb_retrieve` to check relevant prior work and context
3. Assess progress and quality
4. Write a YAML decision file using file_write
   - path: decisions/approval_{pipeline_id}_decision.yaml
   - content: valid YAML (see format below)

IMPORTANT: Use exactly this path format: decisions/approval_{pipeline_id}_decision.yaml

## Decision Format (the content to write)
```yaml
pipeline_id: "{pipeline_id}"
decision: "approve"
next_milestone: "paper"
comment: "your reasoning here"
created_at: "2026-01-01T00:00:00+00:00"
```

Valid decision values: approve, kill, pivot, pause
Set next_milestone to "paper" when research results are satisfactory.
{prof_instructions}"""

    # ── Subprocess spawning ──────────────────────────────────────

    async def _spawn_agent(
        self, prompt: str, label: str
    ) -> asyncio.subprocess.Process | None:
        """Spawn an OpenHarness headless agent with the given prompt."""
        oh = _find_oh_binary()

        if oh.startswith("python"):
            cmd = [*oh.split(), "-p", prompt]
        else:
            cmd = [oh, "-p", prompt]

        logger.info("Spawning agent [%s]: %s ...", label, cmd[0])
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.project_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            return proc
        except Exception:
            logger.exception("Failed to spawn agent [%s]", label)
            return None
