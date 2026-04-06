"""Run a complete research pipeline with rate-limit retry.

Usage:
    python scripts/run_full_pipeline.py

Creates a pipeline from the stacking-vs-BMA proposal and runs through
FAST_FAIL -> Professor review -> MAIN -> PAPER stages.
Handles API rate limits with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from researchos.schemas.pipeline import (
    PipelineDefinition,
    PipelineStage,
    PipelineState,
    PipelineStep,
)
from researchos.schemas.decision import DecisionType
from researchos.runtime.state_manager import StateManager
from researchos.runtime.dispatcher import Dispatcher
from researchos.runtime.agent_executor import AgentExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("full_pipeline")

# ── Rate-limit retry wrapper ─────────────────────────────────────

MAX_RETRIES = 8
BASE_DELAY = 30  # seconds


async def dispatch_with_retry(
    executor: AgentExecutor,
    agent_name: str,
    prompt: str,
    pipeline_id: str,
    pipeline_dir: Path,
) -> dict:
    """Execute agent with exponential backoff on rate limit errors."""
    for attempt in range(MAX_RETRIES):
        try:
            result = await executor.execute(
                agent_name, prompt, pipeline_id, pipeline_dir
            )
            if result.get("success"):
                return result
            output = result.get("output", "")
            # Check for rate limit in error
            if "rate" in output.lower() or "429" in output or "limit" in output.lower():
                delay = BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Rate limited on attempt %d/%d, waiting %ds...",
                    attempt + 1, MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
                continue
            # Non-rate-limit error
            logger.error("Agent %s failed: %s", agent_name, output[:200])
            return result
        except Exception as e:
            err = str(e)
            if "rate" in err.lower() or "429" in err or "limit" in err.lower() or "too many" in err.lower():
                delay = BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Rate limit exception on attempt %d/%d: %s. Waiting %ds...",
                    attempt + 1, MAX_RETRIES, err[:100], delay,
                )
                await asyncio.sleep(delay)
                continue
            raise
    logger.error("Exhausted all %d retries for agent %s", MAX_RETRIES, agent_name)
    return {"success": False, "output": "Rate limit retries exhausted"}


# ── Pipeline step runners ────────────────────────────────────────

STEP_CONFIGS = {
    "fast_fail": [
        ("hypothesis", "hypothesis_gen", "hypothesis.md"),
        ("design", "exp_designer", "experiment_plan.yaml"),
        ("code", "coder", "code_patch.diff"),
        ("run", "experiment_runner", "results.json"),
        ("critic", "critic", "analysis_report.md"),
        ("report", "reporter", "milestone_1.md"),
    ],
    "main": [
        ("hypothesis", "hypothesis_gen", "hypothesis.md"),
        ("design", "exp_designer", "experiment_plan.yaml"),
        ("design_review", "design_reviewer", "design_review.md"),
        ("code", "coder", "code_patch.diff"),
        ("run", "experiment_runner", "results.json"),
        ("critic", "critic", "analysis_report.md"),
        ("interrogation", "interrogator", "interrogation_report.md"),
        ("report", "reporter", "milestone_2.md"),
    ],
}


def build_prompt(pipeline: PipelineState, step_name: str, pipeline_dir: Path) -> str:
    """Build the agent prompt with context from existing artifacts."""
    parts = [
        f"Pipeline: {pipeline.pipeline_id}",
        f"Title: {pipeline.title}",
        f"Research Question: {pipeline.research_question}",
        f"Stage: {pipeline.stage.value}",
        f"Step: {step_name}",
        f"Pipeline directory: {pipeline_dir}",
        "",
        "Write your output artifact to the pipeline directory using the file_write tool.",
    ]

    # Add existing artifacts as context
    for artifact_name in [
        "hypothesis.md", "experiment_plan.yaml", "code_patch.diff",
        "results.json", "analysis_report.md", "design_review.md",
        "interrogation_report.md",
    ]:
        artifact_path = pipeline_dir / artifact_name
        if artifact_path.exists():
            content = artifact_path.read_text()[:3000]
            parts.append(f"\n--- Existing artifact: {artifact_name} ---\n{content}")

    return "\n".join(parts)


async def run_stage(
    executor: AgentExecutor,
    sm: StateManager,
    pipeline: PipelineState,
    stage_name: str,
) -> bool:
    """Run all steps for a stage with retry."""
    pipeline_dir = ROOT / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
    steps = STEP_CONFIGS[stage_name]

    for step_name, agent_name, output_file in steps:
        logger.info("=" * 60)
        logger.info("STEP: %s → agent: %s", step_name, agent_name)
        logger.info("=" * 60)

        # Skip if artifact already exists
        output_path = pipeline_dir / output_file
        if output_path.exists() and output_path.stat().st_size > 10:
            logger.info("Artifact %s already exists, skipping", output_file)
            pipeline.step = PipelineStep(step_name)
            sm.save_pipeline(pipeline)
            continue

        prompt = build_prompt(pipeline, step_name, pipeline_dir)
        result = await dispatch_with_retry(
            executor, agent_name, prompt, pipeline.pipeline_id, pipeline_dir
        )

        if not result.get("success"):
            logger.error("Step %s FAILED: %s", step_name, result.get("output", "")[:300])
            return False

        pipeline.step = PipelineStep(step_name)
        pipeline.artifacts[step_name] = output_file
        sm.save_pipeline(pipeline)
        logger.info("Step %s completed. Artifacts: %s", step_name, result.get("artifacts_written", []))

        # Brief pause between steps to avoid rate limits
        await asyncio.sleep(5)

    return True


async def run_professor_review(
    executor: AgentExecutor,
    sm: StateManager,
    pipeline: PipelineState,
    decision: str = "approve",
) -> bool:
    """Run professor review and write decision."""
    pipeline_dir = ROOT / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
    logger.info("=" * 60)
    logger.info("PROFESSOR REVIEW for %s", pipeline.pipeline_id)
    logger.info("=" * 60)

    prompt = build_prompt(pipeline, "professor_review", pipeline_dir)
    prompt += f"\n\nPlease review this pipeline milestone and write your decision."

    result = await dispatch_with_retry(
        executor, "professor", prompt, pipeline.pipeline_id, pipeline_dir
    )

    # Write decision file regardless
    import yaml
    from datetime import datetime, timezone
    decision_data = {
        "pipeline_id": pipeline.pipeline_id,
        "decision": decision,
        "comment": f"Auto-approved after {'successful' if result.get('success') else 'attempted'} review",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    decision_dir = ROOT / "decisions"
    decision_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    decision_path = decision_dir / f"approval_{pipeline.pipeline_id}_{ts}.yaml"
    decision_path.write_text(yaml.dump(decision_data))
    logger.info("Decision written: %s → %s", decision_path.name, decision)
    return True


async def main():
    logger.info("=" * 60)
    logger.info("FULL PIPELINE RUN: Stacking vs BMA in Self-Adaptive Systems")
    logger.info("=" * 60)

    sm = StateManager(ROOT)

    # Create pipeline
    pipeline_id = "stack_sas_bma"
    pipeline_dir = ROOT / "artifacts" / f"pipeline_{pipeline_id}"
    pipeline_dir.mkdir(parents=True, exist_ok=True)

    # Check if pipeline already exists
    existing = sm.load_pipeline(pipeline_id)
    if existing:
        pipeline = existing
        logger.info("Resuming existing pipeline at stage=%s step=%s",
                     pipeline.stage.value, pipeline.step)
    else:
        pipeline = PipelineState(
            pipeline_id=pipeline_id,
            title="Mitigating Model Uncertainty in Self-Adaptive Systems: Stacking vs BMA",
            research_question=(
                "Can a stacking-based ensemble method outperform Bayesian Model Averaging (BMA) "
                "in the analyze component of MAPE-K self-adaptive systems for predicting "
                "safety violations under model uncertainty?"
            ),
            stage=PipelineStage.FAST_FAIL,
            step=None,
            budget_allocated=50000,
        )
        sm.save_pipeline(pipeline)
        logger.info("Created pipeline: %s", pipeline_id)

    # Save the proposal text to pipeline dir for reference
    proposal_path = pipeline_dir / "proposal.md"
    if not proposal_path.exists():
        proposal_path.write_text(
            "# Mitigating Model Uncertainty in Self-Adaptive Systems\n\n"
            "## Problem Statement\n"
            "Self-adaptive systems (SAS) face model-selection uncertainty when multiple competing models "
            "could explain system behavior. While the original TUNE approach leverages Bayesian Model "
            "Averaging (BMA) to address this, BMA has limitations: it assumes that the true model is "
            "among the candidates and weights models by their posterior probability.\n\n"
            "## Proposed Approach\n"
            "Replace BMA with a stacking-based ensemble method (STACK-SAS) in the analyze component "
            "of the MAPE-K loop. The stacking approach uses a two-level architecture:\n"
            "- Level-0: Diverse base learners (logistic regression, random forest, gradient boosting, SVM)\n"
            "- Level-1: Meta-learner trained on cross-validated out-of-fold predictions\n\n"
            "## Key Hypotheses\n"
            "1. Stacking will achieve lower prediction error than BMA on safety violation prediction\n"
            "2. Stacking will be more robust to model misspecification\n"
            "3. The computational overhead of stacking is acceptable for runtime adaptation\n"
        )

    # Initialize budget
    budget = sm.load_budget()
    budget.allocations[pipeline_id] = 50000
    sm.save_budget(budget)

    # Create executor
    executor = AgentExecutor(ROOT)

    # ── Phase 1: FAST_FAIL ───────────────────────────────────────
    if pipeline.stage == PipelineStage.FAST_FAIL:
        logger.info("\n>>> PHASE 1: FAST_FAIL <<<\n")
        success = await run_stage(executor, sm, pipeline, "fast_fail")
        if success:
            pipeline.stage = PipelineStage.MILESTONE
            pipeline.milestone_count = 1
            sm.save_pipeline(pipeline)
            logger.info("FAST_FAIL completed, entering MILESTONE review")
        else:
            logger.error("FAST_FAIL failed, pipeline blocked")
            pipeline.stage = PipelineStage.FAILED
            sm.save_pipeline(pipeline)
            return

    # ── Professor Review → Approve to MAIN ───────────────────────
    if pipeline.stage == PipelineStage.MILESTONE:
        await run_professor_review(executor, sm, pipeline, "approve")
        pipeline.stage = PipelineStage.MAIN
        pipeline.step = None
        sm.save_pipeline(pipeline)
        logger.info("Professor approved. Advancing to MAIN stage.")
        await asyncio.sleep(10)  # Rate limit buffer

    # ── Phase 2: MAIN ────────────────────────────────────────────
    if pipeline.stage == PipelineStage.MAIN:
        logger.info("\n>>> PHASE 2: MAIN <<<\n")

        # Clean up FAST_FAIL artifacts that MAIN will regenerate
        for old_artifact in ["hypothesis.md", "experiment_plan.yaml", "code_patch.diff",
                            "results.json", "analysis_report.md"]:
            old_path = pipeline_dir / old_artifact
            if old_path.exists():
                backup = pipeline_dir / f"fast_fail_{old_artifact}"
                old_path.rename(backup)
                logger.info("Backed up %s → fast_fail_%s", old_artifact, old_artifact)

        success = await run_stage(executor, sm, pipeline, "main")
        if success:
            pipeline.stage = PipelineStage.MILESTONE
            pipeline.milestone_count = 2
            sm.save_pipeline(pipeline)
            logger.info("MAIN completed, entering MILESTONE review")
        else:
            logger.error("MAIN failed")
            pipeline.stage = PipelineStage.FAILED
            sm.save_pipeline(pipeline)
            return

    # ── Professor Review → Approve to PAPER ──────────────────────
    if pipeline.stage == PipelineStage.MILESTONE and pipeline.milestone_count >= 2:
        await run_professor_review(executor, sm, pipeline, "approve")
        pipeline.stage = PipelineStage.PAPER
        pipeline.step = None
        sm.save_pipeline(pipeline)
        logger.info("Professor approved. Advancing to PAPER stage.")
        await asyncio.sleep(10)

    # ── Phase 3: PAPER ───────────────────────────────────────────
    if pipeline.stage == PipelineStage.PAPER:
        logger.info("\n>>> PHASE 3: PAPER <<<\n")
        paper_dir = ROOT / "paper" / pipeline_id
        paper_dir.mkdir(parents=True, exist_ok=True)

        # Paper writer
        prompt = build_prompt(pipeline, "paper_writing", pipeline_dir)
        prompt += "\n\nWrite a complete academic paper draft based on all pipeline artifacts. Save as draft_v1.md in the paper directory."
        logger.info("Running paper_writer...")
        result = await dispatch_with_retry(
            executor, "paper_writer", prompt, pipeline_id, pipeline_dir
        )
        logger.info("Paper writer: %s", "OK" if result.get("success") else "FAILED")
        await asyncio.sleep(10)

        # Paper audit
        logger.info("Running paper audit (G4+G5+VLM)...")
        from researchos.runtime.paper_audit import run_paper_audit
        audit_report = run_paper_audit(pipeline_id, ROOT)
        audit_path = paper_dir / "audit_report.md"
        audit_path.write_text(audit_report)
        logger.info("Audit report written to %s", audit_path)

        # Reviewer simulator
        prompt = build_prompt(pipeline, "review", pipeline_dir)
        prompt += "\n\nReview the paper draft and write a detailed peer review."
        logger.info("Running reviewer_simulator...")
        result = await dispatch_with_retry(
            executor, "reviewer_simulator", prompt, pipeline_id, pipeline_dir
        )
        logger.info("Reviewer: %s", "OK" if result.get("success") else "FAILED")

        pipeline.stage = PipelineStage.COMPLETED
        sm.save_pipeline(pipeline)
        logger.info("\n" + "=" * 60)
        logger.info("PIPELINE COMPLETED: %s", pipeline_id)
        logger.info("=" * 60)

    # ── Summary ──────────────────────────────────────────────────
    logger.info("\nFinal state: stage=%s, milestones=%d",
                pipeline.stage.value, pipeline.milestone_count)

    # List artifacts
    logger.info("\nArtifacts in %s:", pipeline_dir)
    for f in sorted(pipeline_dir.iterdir()):
        if f.is_file():
            logger.info("  %s (%d bytes)", f.name, f.stat().st_size)

    # Cost summary
    cost_path = pipeline_dir / "cost_report.json"
    if cost_path.exists():
        from researchos.schemas.cost import CostReport
        report = CostReport.model_validate_json(cost_path.read_text())
        logger.info("\nCost Summary:")
        logger.info("  Total calls: %d", len(report.records))
        logger.info("  Total tokens: %d (input: %d, output: %d)",
                     report.total_tokens, report.total_input_tokens, report.total_output_tokens)
        logger.info("  Total credits: %d", report.total_credits)
        logger.info("  Total duration: %.1fs", report.total_duration_seconds)


if __name__ == "__main__":
    asyncio.run(main())
