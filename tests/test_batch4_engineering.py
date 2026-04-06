"""Batch 4 tests: hooks validation, failure recovery, parallel pipelines.

Usage:
    PYTHONPATH=. python tests/test_batch4_engineering.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from researchos.schemas.pipeline import PipelineDefinition, PipelineStage, PipelineStep, PipelineState
from researchos.runtime.scheduler import ResearchScheduler
from researchos.runtime.state_manager import StateManager
from researchos.runtime.recovery import CheckpointManager, RetryPolicy, recover_failed_pipeline
from researchos.runtime.hooks import (
    validate_artifact_write,
    validate_knowledge_access,
    validate_budget,
    record_budget_usage,
)

passed = 0
failed = 0


def check(condition: bool, msg: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {msg}")
    else:
        failed += 1
        print(f"  [FAIL] {msg}")


def _clean_pipeline(pid: str) -> None:
    for d in [
        PROJECT_ROOT / "artifacts" / f"pipeline_{pid}",
        PROJECT_ROOT / "runtime" / "checkpoints",
    ]:
        if d.exists():
            shutil.rmtree(d)
    for f in (PROJECT_ROOT / "decisions").glob(f"*{pid}*"):
        f.unlink()
    for f in (PROJECT_ROOT / "runtime" / "pipelines").glob(f"new_{pid}*"):
        f.unlink()


def test_hooks() -> None:
    print("\n--- Test: Hook Validation ---")

    # state.json write should be blocked
    r = validate_artifact_write("/artifacts/pipeline_x/state.json")
    check(not r["allowed"], "state.json write blocked")

    # Valid results.json
    valid_results = json.dumps({"metrics": {"acc": 0.9}, "config": {"lr": 0.01}, "seed": 42})
    r = validate_artifact_write("results.json", valid_results)
    check(r["allowed"], "Valid results.json accepted")

    # Invalid results.json (missing seed)
    invalid_results = json.dumps({"metrics": {}, "config": {}})
    r = validate_artifact_write("results.json", invalid_results)
    check(not r["allowed"], "Invalid results.json rejected (missing seed)")

    # Knowledge access hook
    r = validate_knowledge_access("read", {"file_path": "/knowledge/wiki/concepts/foo.md"})
    check(not r["allowed"], "Direct wiki read blocked")

    r = validate_knowledge_access("read", {"file_path": "/artifacts/pipeline_x/hypothesis.md"})
    check(r["allowed"], "Non-wiki read allowed")

    r = validate_knowledge_access("bash", {})
    check(r["allowed"], "Non-read tool allowed")


def test_budget() -> None:
    print("\n--- Test: Budget Management ---")

    sm = StateManager(PROJECT_ROOT)

    # Set up test budget
    budget = sm.load_budget()
    budget.allocations["budget_test"] = 1000
    budget.usage["budget_test"] = 0
    sm.save_budget(budget)

    r = validate_budget("budget_test", estimated_cost=500)
    check(r["allowed"], "Under-budget request allowed")

    r = validate_budget("budget_test", estimated_cost=1500)
    check(not r["allowed"], "Over-budget request blocked")

    record_budget_usage("budget_test", 800)
    r = validate_budget("budget_test", estimated_cost=300)
    check(not r["allowed"], "After usage, over-budget blocked")

    # Clean up
    budget = sm.load_budget()
    del budget.allocations["budget_test"]
    del budget.usage["budget_test"]
    sm.save_budget(budget)


def test_checkpoint_and_recovery() -> None:
    print("\n--- Test: Checkpoint & Recovery ---")

    pid = "recovery_test"
    _clean_pipeline(pid)

    sm = StateManager(PROJECT_ROOT)
    cp = CheckpointManager(PROJECT_ROOT)

    # Create a pipeline with some artifacts
    pipeline = PipelineState(
        pipeline_id=pid,
        title="Recovery Test Pipeline",
        research_question="Test question",
        stage=PipelineStage.MAIN,
        step=PipelineStep.CRITIC,
    )
    sm.save_pipeline(pipeline)

    pdir = PROJECT_ROOT / "artifacts" / f"pipeline_{pid}"
    (pdir / "hypothesis.md").write_text("# Test Hypothesis\n")
    (pdir / "results.json").write_text(json.dumps({"metrics": {"acc": 0.5}, "config": {}, "seed": 42}))

    # Create checkpoint
    cp_path = cp.create_checkpoint(pid, label="M1")
    check(cp_path.exists(), "Checkpoint created")

    cps = cp.list_checkpoints(pid)
    check(len(cps) == 1, f"Checkpoint listed (count={len(cps)})")

    # Simulate failure
    pipeline.stage = PipelineStage.FAILED
    pipeline.last_error = "Test failure"
    sm.save_pipeline(pipeline)

    # Corrupt an artifact
    (pdir / "hypothesis.md").write_text("CORRUPTED!")

    # Recover with rollback
    recovered = recover_failed_pipeline(pid, PROJECT_ROOT, rollback=True)
    check(recovered is not None, "Recovery returned pipeline")
    check(recovered.stage == PipelineStage.MAIN, f"Stage restored to MAIN (got {recovered.stage})")

    # Check artifact was restored
    content = (pdir / "hypothesis.md").read_text()
    check("Test Hypothesis" in content, "Artifact restored from checkpoint")

    # Test recovery without rollback
    pipeline = sm.load_pipeline(pid)
    pipeline.stage = PipelineStage.FAILED
    pipeline.last_error = "Another failure"
    pipeline.retry_count = 5
    sm.save_pipeline(pipeline)

    recovered = recover_failed_pipeline(pid, PROJECT_ROOT, reset_to_step=PipelineStep.HYPOTHESIS)
    check(recovered.step == PipelineStep.HYPOTHESIS, "Step reset to HYPOTHESIS")
    check(recovered.retry_count == 0, "Retry count reset to 0")

    _clean_pipeline(pid)


def test_retry_policy() -> None:
    print("\n--- Test: Retry Policy ---")

    rp = RetryPolicy(max_retries=3, base_delay=5.0, backoff_factor=2.0)

    check(rp.should_retry(0, "timeout"), "Retry 0: should retry")
    check(rp.should_retry(2, "timeout"), "Retry 2: should retry")
    check(not rp.should_retry(3, "timeout"), "Retry 3: should NOT retry (max)")
    check(not rp.should_retry(0, "budget exhausted"), "Non-recoverable: no retry")
    check(not rp.should_retry(0, "Schema Validation error"), "Non-recoverable: no retry")

    check(rp.delay_seconds(0) == 5.0, f"Delay 0: 5s (got {rp.delay_seconds(0)})")
    check(rp.delay_seconds(1) == 10.0, f"Delay 1: 10s (got {rp.delay_seconds(1)})")
    check(rp.delay_seconds(2) == 20.0, f"Delay 2: 20s (got {rp.delay_seconds(2)})")


async def test_parallel_pipelines() -> None:
    print("\n--- Test: Parallel Pipeline Scheduling ---")

    pids = ["parallel_a", "parallel_b", "parallel_c"]
    for pid in pids:
        _clean_pipeline(pid)

    sm = StateManager(PROJECT_ROOT)
    scheduler = ResearchScheduler(PROJECT_ROOT, simulate=True)

    # Create 3 proposals
    for i, pid in enumerate(pids):
        proposal = PipelineDefinition(
            pipeline_id=pid,
            title=f"Parallel Test {i+1}",
            research_question=f"Test question {i+1}",
            budget_request=10000,
        )
        (PROJECT_ROOT / "runtime" / "pipelines" / f"new_{pid}.json").write_text(
            proposal.model_dump_json(indent=2)
        )

    # Tick 1: All proposals processed
    await scheduler.tick()
    for pid in pids:
        p = sm.load_pipeline(pid)
        check(p is not None and p.stage == PipelineStage.PROPOSAL, f"{pid}: PROPOSAL stage")

    # Approve all
    for pid in pids:
        scheduler.dispatcher._simulate_professor(pid)

    # Tick 2: All approved → FAST_FAIL
    await scheduler.tick()
    for pid in pids:
        p = sm.load_pipeline(pid)
        check(p.stage == PipelineStage.FAST_FAIL, f"{pid}: FAST_FAIL stage")

    # Tick 3: All run hypothesis simultaneously
    await scheduler.tick()
    for pid in pids:
        p = sm.load_pipeline(pid)
        hyp = PROJECT_ROOT / "artifacts" / f"pipeline_{pid}" / "hypothesis.md"
        check(hyp.exists(), f"{pid}: hypothesis.md created")

    # Run through several more ticks to advance all pipelines
    for _ in range(5):
        await scheduler.tick()

    # All should be at WAITING_APPROVAL
    for pid in pids:
        p = sm.load_pipeline(pid)
        check(
            p.step == PipelineStep.WAITING_APPROVAL,
            f"{pid}: at WAITING_APPROVAL (step={p.step})",
        )

    # Cleanup
    for pid in pids:
        _clean_pipeline(pid)


def main() -> None:
    global passed, failed

    print("=" * 60)
    print("Batch 4: Engineering Hardening Tests")
    print("=" * 60)

    test_hooks()
    test_budget()
    test_retry_policy()
    test_checkpoint_and_recovery()
    asyncio.run(test_parallel_pipelines())

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("ALL BATCH 4 TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
