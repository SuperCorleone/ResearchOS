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

    # After 2026-04 refactor: WAITING_APPROVAL gate removed. Pipelines
    # transition straight from REPORT to MILESTONE stage after the
    # professor auto-decides. Accept either step=REPORT (not yet transitioned)
    # or stage=MILESTONE/COMPLETED (already transitioned).
    for pid in pids:
        p = sm.load_pipeline(pid)
        check(
            p.step == PipelineStep.REPORT
            or p.stage in (PipelineStage.MILESTONE, PipelineStage.COMPLETED),
            f"{pid}: reached terminal step or milestone (step={p.step}, stage={p.stage})",
        )

    # Cleanup
    for pid in pids:
        _clean_pipeline(pid)


def _full_four_slot_plan() -> dict:
    """Build a plan dict that satisfies every required slot + sub-key."""
    return {
        "plan_dataset": {
            "dataset_description": "toy regression set",
            "dataset_location": "/data/raw/toy",
            "task_definition": "regression",
            "read_data_step": "pandas.read_csv",
            "data_preprocessing_step": "standardize",
            "data_dataloader_step": "torch DataLoader batch=32",
        },
        "plan_model": {
            "model_input": "x: float[N,d]",
            "model_output": "y: float[N]",
            "key_components": ["linear", "relu"],
        },
        "plan_training": {
            "training_pipeline": "epoch loop",
            "loss_function": "MSE",
            "optimizer": "Adam(lr=1e-3)",
            "training_configurations": "batch=32 epochs=10",
            "monitor_and_logging": "stdout every epoch",
        },
        "plan_testing": {
            "test_metric": "MAE",
            "test_data": "held-out split",
            "test_code": "scripts/eval.py",
        },
    }


def test_four_slot_plan_full_pass() -> None:
    print("\n--- Test: Four-slot plan (full) ---")
    from researchos.runtime.design_checks import check_four_slot_plan

    plan = _full_four_slot_plan()
    iss = check_four_slot_plan(plan)
    errs = [i for i in iss if i["severity"] == "ERROR"]
    warns = [i for i in iss if i["severity"] == "WARN"]
    check(errs == [], f"No ERRORs on full plan (got {errs})")
    check(warns == [], f"No WARNs on full plan (got {warns})")
    assert not errs and not warns, f"expected clean issues, got {iss}"


def test_four_slot_plan_missing_slot_blocks() -> None:
    print("\n--- Test: Four-slot plan (missing slot -> BLOCK) ---")
    from researchos.runtime.design_checks import check_four_slot_plan
    from researchos.runtime.preflight import run_preflight

    # Rule-level: missing slot → ERROR
    plan = _full_four_slot_plan()
    del plan["plan_training"]
    iss = check_four_slot_plan(plan)
    errs = [i for i in iss if i["severity"] == "ERROR"]
    check(
        any(i["check"] == "four_slot_plan_plan_training" for i in errs),
        "Missing plan_training produces ERROR",
    )
    assert any(i["check"] == "four_slot_plan_plan_training" for i in errs)

    # Preflight-level: missing slot → BLOCK verdict
    pid = "four_slot_block_test"
    pdir = PROJECT_ROOT / "artifacts" / f"pipeline_{pid}"
    try:
        if pdir.exists():
            shutil.rmtree(pdir)
        pdir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml
        (pdir / "experiment_plan.yaml").write_text(_yaml.safe_dump(plan))
        # Provide minimal code so we don't hit the "no code" early return path.
        (pdir / "run_experiment.py").write_text(
            "import argparse\n"
            "def main(seed=0):\n    return 0\n"
            "if __name__ == '__main__':\n    main()\n"
        )
        report = run_preflight(pdir, PROJECT_ROOT)
        check(report["verdict"] == "BLOCK", f"Preflight verdict=BLOCK (got {report['verdict']})")
        check(
            any(
                i["severity"] == "ERROR" and i["check"] == "four_slot_plan_plan_training"
                for i in report["issues"]
            ),
            "Preflight issues include ERROR for missing plan_training",
        )
        assert report["verdict"] == "BLOCK"
        assert any(
            i["severity"] == "ERROR" and i["check"] == "four_slot_plan_plan_training"
            for i in report["issues"]
        )
    finally:
        if pdir.exists():
            shutil.rmtree(pdir)


def test_four_slot_plan_missing_subkey_warns() -> None:
    print("\n--- Test: Four-slot plan (missing sub-key -> WARN) ---")
    from researchos.runtime.design_checks import check_four_slot_plan

    plan = _full_four_slot_plan()
    del plan["plan_testing"]["test_metric"]
    iss = check_four_slot_plan(plan)
    errs = [i for i in iss if i["severity"] == "ERROR"]
    warns = [
        i for i in iss
        if i["severity"] == "WARN"
        and i["check"] == "four_slot_plan_plan_testing"
        and "test_metric" in i["detail"]
    ]
    check(errs == [], f"No ERRORs when only sub-key missing (got {errs})")
    check(len(warns) == 1, f"Exactly one WARN for missing test_metric (got {warns})")
    assert errs == []
    assert len(warns) == 1


def test_branch_runner_seed_and_winner() -> None:
    """BranchRunner seeds K drafts and picks the best via metric filter."""
    print("\n--- Test: BranchRunner seed + winner selection ---")
    from researchos.runtime.branch_manager import BranchRunner
    from researchos.schemas.pipeline import PipelineStep

    pid = "branch_runner_seed_winner"
    pdir = PROJECT_ROOT / "artifacts" / f"pipeline_{pid}"
    try:
        if pdir.exists():
            shutil.rmtree(pdir)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "hypothesis.md").write_text("# test hypothesis\n")

        runner = BranchRunner(
            pipeline_id=pid,
            artifacts_root=PROJECT_ROOT / "artifacts",
            num_branches=3,
        )
        nodes = runner.seed("test hypothesis")
        check(len(nodes) == 3, f"Seeded 3 drafts (got {len(nodes)})")
        assert len(nodes) == 3

        # Per-branch hypothesis.md + branch_<n>/ dirs
        for i in range(3):
            bdir = runner.branch_dir(i)
            check(bdir.is_dir(), f"branch_{i}/ dir exists")
            check((bdir / "hypothesis.md").exists(), f"branch_{i}/hypothesis.md copied")

        # Advance each branch to CRITIC and write fake results.json metrics.
        fake_scores = {0: 0.72, 1: 0.91, 2: 0.65}
        for bid, score in fake_scores.items():
            for step in (
                PipelineStep.DESIGN,
                PipelineStep.CODE,
                PipelineStep.RUN,
                PipelineStep.CRITIC,
            ):
                runner.advance(bid, step)
            (runner.branch_dir(bid) / "results.json").write_text(
                json.dumps({"metrics": {"accuracy": score}})
            )

        check(
            runner.all_reached(PipelineStep.CRITIC),
            "All 3 branches reached CRITIC",
        )
        assert runner.all_reached(PipelineStep.CRITIC)

        winner = runner.select_winner()
        check(winner is not None, "select_winner returned a node")
        assert winner is not None
        check(
            winner.iter_index == 1,
            f"Winner is branch 1 (highest accuracy=0.91), got {winner.iter_index}",
        )
        assert winner.iter_index == 1

        archived = runner.archive(winner.iter_index)
        check(archived is not None, "archive() promoted winner")
        assert archived is not None
        # Selection sentinel written to pipeline dir
        check(
            (pdir / "branch_selection.json").exists(),
            "branch_selection.json written",
        )
    finally:
        if pdir.exists():
            shutil.rmtree(pdir)


def main() -> None:
    global passed, failed

    print("=" * 60)
    print("Batch 4: Engineering Hardening Tests")
    print("=" * 60)

    test_hooks()
    test_budget()
    test_retry_policy()
    test_checkpoint_and_recovery()
    test_four_slot_plan_full_pass()
    test_four_slot_plan_missing_slot_blocks()
    test_four_slot_plan_missing_subkey_warns()
    test_branch_runner_seed_and_winner()
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
