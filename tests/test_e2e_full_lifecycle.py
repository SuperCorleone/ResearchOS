"""End-to-end test: full pipeline lifecycle including paper and knowledge stages.

Tests: proposal → professor → fast_fail → professor → main → professor(paper)
       → paper_writer → reviewer → knowledge_compilation → COMPLETED

Usage:
    PYTHONPATH=. python tests/test_e2e_full_lifecycle.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from researchos.schemas.pipeline import PipelineDefinition, PipelineStage, PipelineStep
from researchos.runtime.scheduler import ResearchScheduler
from researchos.runtime.state_manager import StateManager
from researchos.runtime.knowledge_compiler import compile_pipeline_to_wiki

PIPELINE_ID = "lifecycle_test"


def _clean() -> None:
    for d in [
        PROJECT_ROOT / "artifacts" / f"pipeline_{PIPELINE_ID}",
        PROJECT_ROOT / "runtime" / "pipelines" / "archived",
    ]:
        if d.exists():
            shutil.rmtree(d)
    for f in (PROJECT_ROOT / "decisions").glob(f"*{PIPELINE_ID}*"):
        f.unlink()
    for f in (PROJECT_ROOT / "runtime" / "pipelines").glob(f"new_{PIPELINE_ID}*"):
        f.unlink()
    # Clean paper outputs
    for f in (PROJECT_ROOT / "paper").glob(f"*{PIPELINE_ID}*"):
        f.unlink()
    # Clean wiki pages generated for this pipeline
    for subdir in ["concepts", "methods", "experiments", "failures"]:
        for f in (PROJECT_ROOT / "knowledge" / "wiki" / subdir).glob(f"*{PIPELINE_ID}*"):
            f.unlink()


def _assert(condition: bool, msg: str) -> None:
    if not condition:
        print(f"  FAIL: {msg}")
        sys.exit(1)


async def run_test() -> bool:
    print("=" * 60)
    print("ResearchOS Full Lifecycle Test (Simulation)")
    print("=" * 60)

    _clean()
    scheduler = ResearchScheduler(PROJECT_ROOT, simulate=True)
    sm = StateManager(PROJECT_ROOT)

    # Phase 1: Proposal
    print("\n--- Phase 1: Proposal ---")
    proposal = PipelineDefinition(
        pipeline_id=PIPELINE_ID,
        title="Full Lifecycle: Attention Mechanisms for Long Text",
        research_question="Can modified attention mechanisms improve LLM performance on documents >10K tokens?",
        budget_request=25000,
    )
    (PROJECT_ROOT / "runtime" / "pipelines" / f"new_{PIPELINE_ID}.json").write_text(
        proposal.model_dump_json(indent=2)
    )
    await scheduler.tick()
    p = sm.load_pipeline(PIPELINE_ID)
    _assert(p.stage == PipelineStage.PROPOSAL, f"Expected PROPOSAL got {p.stage}")
    print(f"  Pipeline created: stage={p.stage.value}")

    # Phase 2: Professor approves → fast_fail
    print("\n--- Phase 2: Professor Approval → Fast Fail ---")
    scheduler.dispatcher._simulate_professor(PIPELINE_ID)
    await scheduler.tick()
    p = sm.load_pipeline(PIPELINE_ID)
    _assert(p.stage == PipelineStage.FAST_FAIL, f"Expected FAST_FAIL got {p.stage}")
    print(f"  stage={p.stage.value}")

    # Phase 3: Fast-fail cycle
    print("\n--- Phase 3: Fast-Fail Cycle ---")
    for step_name in ["hypothesis", "design", "run", "critic", "report"]:
        await scheduler.tick()
        p = sm.load_pipeline(PIPELINE_ID)
        print(f"  step={p.step}, stage={p.stage.value}")

    # WAITING_APPROVAL → MILESTONE
    await scheduler.tick()
    p = sm.load_pipeline(PIPELINE_ID)
    _assert(p.stage == PipelineStage.MILESTONE, f"Expected MILESTONE got {p.stage}")
    print(f"  -> stage={p.stage.value}, milestone_count={p.milestone_count}")

    # Phase 4: Professor approves → main
    print("\n--- Phase 4: Professor → Main Cycle ---")
    scheduler.dispatcher._simulate_professor(PIPELINE_ID)
    await scheduler.tick()
    p = sm.load_pipeline(PIPELINE_ID)
    _assert(p.stage == PipelineStage.MAIN, f"Expected MAIN got {p.stage}")

    # Main cycle
    for step_name in ["hypothesis", "design", "code", "run", "critic", "report"]:
        await scheduler.tick()
        p = sm.load_pipeline(PIPELINE_ID)
        print(f"  step={p.step}, stage={p.stage.value}")

    # WAITING_APPROVAL → MILESTONE
    await scheduler.tick()
    p = sm.load_pipeline(PIPELINE_ID)
    print(f"  -> stage={p.stage.value}, milestone_count={p.milestone_count}")

    # Phase 5: Professor approves → PAPER
    print("\n--- Phase 5: Professor → Paper Stage ---")
    # Simulate professor decision with next_milestone="paper"
    import yaml
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc)
    decision = {
        "pipeline_id": PIPELINE_ID,
        "decision": "approve",
        "next_milestone": "paper",
        "comment": "Results are strong. Proceed to paper writing.",
        "created_at": ts.isoformat(),
    }
    fname = f"approval_{PIPELINE_ID}_{ts.strftime('%Y%m%d%H%M%S')}.yaml"
    (PROJECT_ROOT / "decisions" / fname).write_text(yaml.dump(decision, default_flow_style=False))

    await scheduler.tick()
    p = sm.load_pipeline(PIPELINE_ID)
    _assert(p.stage == PipelineStage.PAPER, f"Expected PAPER got {p.stage}")
    print(f"  stage={p.stage.value}")

    # Phase 6: Paper writing
    print("\n--- Phase 6: Paper Writing ---")
    await scheduler.tick()  # Paper writer
    draft = PROJECT_ROOT / "paper" / f"draft_{PIPELINE_ID}_v1.md"
    _assert(draft.exists(), "Paper draft should exist")
    print(f"  Draft created: {draft.stat().st_size} bytes")

    await scheduler.tick()  # Reviewer
    review = PROJECT_ROOT / "paper" / f"review_{PIPELINE_ID}_v1.md"
    _assert(review.exists(), "Review should exist")
    print(f"  Review created: {review.stat().st_size} bytes")

    await scheduler.tick()  # → COMPLETED (paper done + knowledge compiled)
    p = sm.load_pipeline(PIPELINE_ID)
    _assert(p.stage == PipelineStage.COMPLETED, f"Expected COMPLETED got {p.stage}")
    print(f"  stage={p.stage.value} (paper + knowledge done)")

    # Check wiki pages were created
    wiki_pages = []
    for subdir in ["concepts", "methods", "experiments", "failures"]:
        for f in (PROJECT_ROOT / "knowledge" / "wiki" / subdir).glob(f"*{PIPELINE_ID}*"):
            wiki_pages.append(f)
    print(f"  Wiki pages created: {len(wiki_pages)}")
    for wp in wiki_pages:
        rel = wp.relative_to(PROJECT_ROOT / "knowledge" / "wiki")
        print(f"    - {rel}")

    # Phase 8: Verification
    print("\n" + "=" * 60)
    print("FULL LIFECYCLE VERIFICATION")
    print("=" * 60)

    pdir = PROJECT_ROOT / "artifacts" / f"pipeline_{PIPELINE_ID}"
    checks = {
        "state.json": pdir / "state.json",
        "hypothesis.md": pdir / "hypothesis.md",
        "experiment_plan.yaml": pdir / "experiment_plan.yaml",
        "code_patch.diff": pdir / "code_patch.diff",
        "results.json": pdir / "results.json",
        "analysis_report.md": pdir / "analysis_report.md",
        "milestone_1.md": pdir / "milestone_1.md",
        "milestone_2.md": pdir / "milestone_2.md",
        "paper draft": draft,
        "paper review": review,
    }

    all_ok = True
    for label, path in checks.items():
        ok = path.exists()
        if not ok:
            all_ok = False
        print(f"  [{'OK' if ok else 'MISSING'}] {label}")

    # Check topic map was updated
    topic_map = PROJECT_ROOT / "knowledge" / "index" / "topic_map.md"
    if topic_map.exists():
        content = topic_map.read_text()
        has_entry = PIPELINE_ID in content
        print(f"  [{'OK' if has_entry else 'MISSING'}] Topic map entry")
        if not has_entry:
            all_ok = False
    else:
        print(f"  [MISSING] Topic map file")
        all_ok = False

    p = sm.load_pipeline(PIPELINE_ID)
    print(f"\n  Final state: {p.stage.value}")
    print(f"  Milestones: {p.milestone_count}")
    print(f"  Artifacts: {len(p.artifacts)}")

    budget = sm.load_budget()
    print(f"  Budget allocated: {budget.allocations.get(PIPELINE_ID, 0)}")

    log_file = PROJECT_ROOT / "runtime" / "logs" / f"pipeline_{PIPELINE_ID}.log"
    if log_file.exists():
        lines = log_file.read_text().strip().split("\n")
        print(f"  Log entries: {len(lines)}")

    print("\n" + "=" * 60)
    if all_ok:
        print("ALL LIFECYCLE TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
    print("=" * 60)

    return all_ok


if __name__ == "__main__":
    ok = asyncio.run(run_test())
    sys.exit(0 if ok else 1)
