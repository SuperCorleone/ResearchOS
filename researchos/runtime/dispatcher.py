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
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from researchos.schemas.pipeline import PipelineState, PipelineStep
from researchos.runtime.workflow_loader import WorkflowConfig
from researchos.utils.paths import ARTIFACTS_DIR, DECISIONS_DIR

logger = logging.getLogger(__name__)

# Agent definition files corresponding to each step
STEP_AGENT: dict[PipelineStep, str] = {
    PipelineStep.HYPOTHESIS: "hypothesis_gen",
    PipelineStep.DESIGN: "exp_designer",
    PipelineStep.DESIGN_REVIEW: "design_reviewer",
    PipelineStep.CODE: "coder",
    PipelineStep.CODE_PREFLIGHT: "code_preflight",  # Python function, not LLM
    PipelineStep.RUN: "experiment_runner",
    PipelineStep.CRITIC: "critic",
    PipelineStep.INTERROGATION: "interrogator",
    PipelineStep.REPORT: "reporter",
}

# Expected output artifact for each step
STEP_OUTPUT: dict[PipelineStep, str] = {
    PipelineStep.HYPOTHESIS: "hypothesis.md",
    PipelineStep.DESIGN: "experiment_plan.yaml",
    PipelineStep.DESIGN_REVIEW: "design_review.md",
    PipelineStep.CODE: "code_patch.diff",
    PipelineStep.CODE_PREFLIGHT: "preflight_report.json",
    PipelineStep.RUN: "results.json",
    PipelineStep.CRITIC: "analysis_report.md",
    PipelineStep.INTERROGATION: "interrogation_report.md",
    PipelineStep.REPORT: "milestone_{n}.md",
}


def _find_oh_binary() -> str:
    """Locate the OpenHarness CLI binary."""
    for name in ("oh", "openharness"):
        path = shutil.which(name)
        if path:
            return path
    return "python -m openharness"


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

    async def dispatch_via_executor(
        self, agent_name: str, prompt: str, pipeline_id: str
    ) -> dict[str, Any]:
        """Run an agent via AgentExecutor (Anthropic SDK)."""
        executor = self._get_executor()
        # Internal system calls (e.g. _system_evaluator, _system_miner) use
        # scratch/ instead of artifacts/ to avoid creating spurious pipeline dirs
        if pipeline_id.startswith("_system_"):
            pdir = self.project_root / "scratch" / pipeline_id
        else:
            pdir = self.project_root / "artifacts" / f"pipeline_{pipeline_id}"
        pdir.mkdir(parents=True, exist_ok=True)
        return await executor.execute(agent_name, prompt, pipeline_id, pdir)

    async def dispatch_step(
        self, pipeline: PipelineState, step: PipelineStep
    ) -> bool:
        """Dispatch a subagent for one atomic pipeline step. Returns True on success."""
        if self.simulate:
            self._simulate_step(pipeline, step)
            return True

        # CODE_PREFLIGHT runs as pure Python — no LLM call
        if step == PipelineStep.CODE_PREFLIGHT:
            return self._run_code_preflight(pipeline)

        prompt = self._build_step_prompt(pipeline, step)
        agent_name = self.workflow.get_step_agent(step.value) or STEP_AGENT.get(step, "pipeline_manager")
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

        elif step == PipelineStep.DESIGN_REVIEW:
            (pdir / "design_review.md").write_text(
                f"# Design Review\n\n"
                f"## Structural Checks\n"
                f"| Check | Status |\n"
                f"|-------|--------|\n"
                f"| baselines defined | PASS |\n"
                f"| statistical tests defined | PASS |\n"
                f"| success criteria defined | PASS |\n"
                f"| data section present | PASS |\n\n"
                f"## Scientific Review\n"
                f"Design is adequate for the research question.\n\n"
                f"## Verdict: PASS\n\n"
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

    # ── Prompt builders ──────────────────────────────────────────

    def _build_step_prompt(self, pipeline: PipelineState, step: PipelineStep) -> str:
        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        state_path = pdir / "state.json"
        agent_name = self.workflow.get_step_agent(step.value) or STEP_AGENT.get(step, "pipeline_manager")

        # Load agent definition if available
        agent_def_path = (
            self.project_root
            / ".openharness"
            / "plugins"
            / "researchos"
            / "agents"
            / f"{agent_name}.md"
        )
        agent_instructions = ""
        if agent_def_path.exists():
            agent_instructions = (
                f"\n## Agent Protocol\n"
                f"Follow the instructions in the {agent_name} agent definition:\n\n"
                f"{agent_def_path.read_text()}\n"
            )

        # Get the expected output filename for this step
        expected_output = self.workflow.get_step_output(step.value) or STEP_OUTPUT.get(step, "output.md")
        if step == PipelineStep.REPORT:
            expected_output = f"milestone_{pipeline.milestone_count + 1}.md"

        # Build structural design check for DESIGN_REVIEW step
        design_check_notice = ""
        if step == PipelineStep.DESIGN_REVIEW:
            from researchos.runtime.design_checks import validate_design
            design_report = validate_design(pdir)
            check_lines = []
            for name, passed in design_report["checks"].items():
                status = "PASS" if passed else "FAIL"
                check_lines.append(f"| {name} | {status} |")
            issues_text = "\n".join(
                f"- [{i['severity']}] {i['check']}: {i['detail']}"
                for i in design_report["issues"]
            )
            design_check_notice = (
                f"\n## Automated Structural Checks (pre-computed)\n"
                f"| Check | Status |\n|-------|--------|\n"
                + "\n".join(check_lines)
                + f"\n\nPass: {design_report['pass_count']}, "
                f"Fail: {design_report['fail_count']}\n"
            )
            if issues_text:
                design_check_notice += f"\n### Issues Found\n{issues_text}\n"
            design_check_notice += (
                "\nIncorporate these structural findings into your review. "
                "Focus your analysis on scientific validity, baseline adequacy, "
                "and success criteria realism — the structural checks above "
                "already cover completeness.\n"
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

        return f"""You are executing an atomic research step as the **{agent_name}** agent.

## Task
Execute step: **{step.value}** for pipeline: **{pipeline.pipeline_id}**

## Pipeline Info
- Title: {pipeline.title}
- Research Question: {pipeline.research_question}
- Current Stage: {pipeline.stage.value}
{design_check_notice}{data_notice}{ref_notice}{consistency_notice}
## CRITICAL: Required Output
You MUST write exactly this file using file_write: **{expected_output}**
Use the filename as-is (relative path). Do NOT use absolute paths.

## Instructions
1. Read relevant existing artifacts from the pipeline using file_read (relative paths only)
2. Use `kb_retrieve` to gather relevant knowledge before producing output
3. For the RUN step: use `execute_python` to run actual experiments, then write results to results.json
4. Write the output artifact using file_write with path: {expected_output}
5. Do NOT modify state.json

## Existing Artifacts
{json.dumps(pipeline.artifacts, indent=2)}
{agent_instructions}"""

    def _build_professor_prompt(
        self, pipeline_id: str, context_files: list[str]
    ) -> str:
        files_list = "\n".join(f"- {f}" for f in context_files)
        decisions_dir = self.project_root / "decisions"

        # Load professor agent definition
        prof_def_path = (
            self.project_root
            / ".openharness"
            / "plugins"
            / "researchos"
            / "agents"
            / "professor.md"
        )
        prof_instructions = ""
        if prof_def_path.exists():
            prof_instructions = (
                f"\n## Agent Protocol\n{prof_def_path.read_text()}\n"
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
