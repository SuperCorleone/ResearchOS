"""End-to-end test: run a single pipeline through all stages in simulation mode.

Usage:
    PYTHONPATH=. python tests/test_e2e_pipeline.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from researchos.schemas.pipeline import PipelineDefinition, PipelineStage, PipelineStep
from researchos.runtime.scheduler import ResearchScheduler
from researchos.runtime.state_manager import StateManager


def _clean_test_state() -> None:
    """Remove prior test pipeline artifacts."""
    for d in [
        PROJECT_ROOT / "artifacts" / "pipeline_e2e_test",
        PROJECT_ROOT / "runtime" / "pipelines" / "archived",
    ]:
        if d.exists():
            shutil.rmtree(d)

    # Clean decision files for this pipeline
    for f in (PROJECT_ROOT / "decisions").glob("*e2e_test*"):
        f.unlink()
    for f in (PROJECT_ROOT / "runtime" / "pipelines").glob("new_e2e_test*"):
        f.unlink()


def _create_proposal() -> None:
    """Write a test pipeline proposal."""
    proposal = PipelineDefinition(
        pipeline_id="e2e_test",
        title="E2E Test: LLM Scientific Reasoning",
        research_question="Can large language models perform multi-step scientific reasoning with self-correction?",
        hypothesis="LLMs can improve reasoning accuracy through iterative self-critique",
        estimated_effort="low",
        budget_request=15000,
    )
    path = PROJECT_ROOT / "runtime" / "pipelines" / "new_e2e_test.json"
    path.write_text(proposal.model_dump_json(indent=2))


async def run_test() -> bool:
    """Run the full pipeline simulation and verify each stage."""
    print("=" * 60)
    print("ResearchOS E2E Pipeline Test (Simulation Mode)")
    print("=" * 60)

    _clean_test_state()

    scheduler = ResearchScheduler(PROJECT_ROOT, simulate=True)
    sm = StateManager(PROJECT_ROOT)

    # ── Tick 1: Create proposal ──────────────────────────────────
    print("\n[Tick 1] Creating pipeline proposal...")
    _create_proposal()
    await scheduler.tick()

    pipeline = sm.load_pipeline("e2e_test")
    assert pipeline is not None, "Pipeline state.json should exist"
    assert pipeline.stage == PipelineStage.PROPOSAL, f"Expected PROPOSAL, got {pipeline.stage}"
    print(f"  -> Pipeline created: stage={pipeline.stage.value}")

    # ── Tick 2: Professor approves (simulated) → fast_fail ───────
    print("\n[Tick 2] Professor approval (simulated)...")
    # In simulate mode, dispatch_professor writes a mock approval
    # We need to trigger professor dispatch - the scheduler dispatches when stage=PROPOSAL
    # But our current flow needs a decision file. Let's simulate it:
    scheduler.dispatcher._simulate_professor("e2e_test")
    await scheduler.tick()

    pipeline = sm.load_pipeline("e2e_test")
    assert pipeline.stage == PipelineStage.FAST_FAIL, f"Expected FAST_FAIL, got {pipeline.stage}"
    print(f"  -> Decision applied: stage={pipeline.stage.value}")

    # ── Ticks 3-7: Fast-fail cycle (hypothesis → design → run → critic → report)
    fast_fail_steps = [
        (PipelineStep.HYPOTHESIS, "hypothesis.md"),
        (PipelineStep.DESIGN, "experiment_plan.yaml"),
        (PipelineStep.RUN, "results.json"),
        (PipelineStep.CRITIC, "analysis_report.md"),
        (PipelineStep.REPORT, "milestone_1.md"),
    ]

    for i, (expected_step, expected_artifact) in enumerate(fast_fail_steps, start=3):
        print(f"\n[Tick {i}] Executing step: {expected_step.value}...")
        await scheduler.tick()

        pipeline = sm.load_pipeline("e2e_test")
        print(f"  -> step={pipeline.step}, stage={pipeline.stage.value}")

        artifact_path = PROJECT_ROOT / "artifacts" / "pipeline_e2e_test" / expected_artifact
        assert artifact_path.exists(), f"Expected artifact {expected_artifact} not found"
        print(f"  -> Artifact produced: {expected_artifact} ({artifact_path.stat().st_size} bytes)")

    # ── Tick 8: WAITING_APPROVAL → MILESTONE + professor dispatch
    print("\n[Tick 8] Waiting for approval -> Milestone...")
    await scheduler.tick()

    pipeline = sm.load_pipeline("e2e_test")
    print(f"  -> stage={pipeline.stage.value}, milestone_count={pipeline.milestone_count}")

    # ── Tick 9: Professor approves → MAIN stage
    print("\n[Tick 9] Professor approves milestone (simulated)...")
    scheduler.dispatcher._simulate_professor("e2e_test")
    await scheduler.tick()

    pipeline = sm.load_pipeline("e2e_test")
    assert pipeline.stage == PipelineStage.MAIN, f"Expected MAIN, got {pipeline.stage}"
    print(f"  -> stage={pipeline.stage.value} (full experiment cycle unlocked)")

    # ── Ticks 10-16: Main cycle (full: hypothesis → design → code → run → critic → report)
    main_steps = [
        (PipelineStep.HYPOTHESIS, "hypothesis.md"),
        (PipelineStep.DESIGN, "experiment_plan.yaml"),
        (PipelineStep.CODE, "code_patch.diff"),
        (PipelineStep.RUN, "results.json"),
        (PipelineStep.CRITIC, "analysis_report.md"),
        (PipelineStep.REPORT, "milestone_2.md"),
    ]

    for i, (expected_step, expected_artifact) in enumerate(main_steps, start=10):
        print(f"\n[Tick {i}] Executing step: {expected_step.value}...")
        await scheduler.tick()

        pipeline = sm.load_pipeline("e2e_test")
        print(f"  -> step={pipeline.step}, stage={pipeline.stage.value}")

    # ── Final verification ───────────────────────────────────────
    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)

    pdir = PROJECT_ROOT / "artifacts" / "pipeline_e2e_test"
    expected_files = [
        "state.json",
        "hypothesis.md",
        "experiment_plan.yaml",
        "code_patch.diff",
        "results.json",
        "analysis_report.md",
        "milestone_1.md",
        "milestone_2.md",
    ]

    all_ok = True
    for f in expected_files:
        exists = (pdir / f).exists()
        status = "OK" if exists else "MISSING"
        if not exists:
            all_ok = False
        print(f"  [{status}] {f}")

    # Verify budget was allocated
    budget = sm.load_budget()
    assert "e2e_test" in budget.allocations, "Budget allocation missing"
    print(f"\n  Budget allocated: {budget.allocations['e2e_test']} credits")

    # Verify logs exist
    log_file = PROJECT_ROOT / "runtime" / "logs" / "pipeline_e2e_test.log"
    if log_file.exists():
        lines = log_file.read_text().strip().split("\n")
        print(f"  Log entries: {len(lines)}")
    else:
        print("  WARNING: No pipeline log file")
        all_ok = False

    # Verify state.json final state
    pipeline = sm.load_pipeline("e2e_test")
    print(f"\n  Final state: stage={pipeline.stage.value} step={pipeline.step}")
    print(f"  Artifacts tracked: {len(pipeline.artifacts)}")
    print(f"  Milestones completed: {pipeline.milestone_count}")

    print("\n" + "=" * 60)
    if all_ok:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
    print("=" * 60)

    return all_ok


if __name__ == "__main__":
    ok = asyncio.run(run_test())
    sys.exit(0 if ok else 1)
