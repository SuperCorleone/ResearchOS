"""Knowledge Miner gating in the scheduler.

Focused-mode invariant: while a user-injected pipeline is alive
(non-COMPLETED / non-FAILED), the Miner must not run — otherwise
``knowledge/proposals/`` gets polluted with off-topic ``evaluated_*.yaml``
files that contradict the inject design (one user-given topic at a time).

Operator override (``runtime/.miner_paused``) and the legacy "no active
pipelines" guard stay in place; this test only asserts the new gate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from researchos.runtime.scheduler import ResearchScheduler
from researchos.schemas.pipeline import PipelineStage, PipelineState


def _write_pipeline(
    project_root: Path,
    pipeline_id: str,
    *,
    source: str,
    stage: PipelineStage,
) -> None:
    state = PipelineState(
        pipeline_id=pipeline_id,
        title=f"test {pipeline_id}",
        research_question="?",
        stage=stage,
        metadata={"source": source},
    )
    pdir = project_root / "artifacts" / f"pipeline_{pipeline_id}"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "state.json").write_text(state.model_dump_json(indent=2))


def _wiki_seed(project_root: Path) -> None:
    wiki = project_root / "knowledge" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "seed.md").write_text("# seed\n")


def _make_scheduler(project_root: Path) -> ResearchScheduler:
    (project_root / "runtime").mkdir(parents=True, exist_ok=True)
    return ResearchScheduler(project_root, simulate=True)


class _DispatchSpy:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, agent: str, prompt: str, pipeline_id: str):
        self.calls.append((agent, pipeline_id))
        return {"success": True, "artifacts_written": []}


@pytest.fixture
def spy_scheduler(tmp_path: Path):
    _wiki_seed(tmp_path)
    sch = _make_scheduler(tmp_path)
    spy = _DispatchSpy()
    sch.dispatcher.dispatch_via_executor = spy  # type: ignore[assignment]
    return sch, spy, tmp_path


def test_miner_skipped_when_user_inject_pipeline_is_active(spy_scheduler):
    sch, spy, root = spy_scheduler
    _write_pipeline(root, "alpha", source="user_inject", stage=PipelineStage.MAIN)

    asyncio.run(sch._maybe_run_knowledge_miner())

    assert spy.calls == [], "Miner must not dispatch while inject pipeline is alive"


def test_miner_skipped_for_typed_inject_source(spy_scheduler):
    """Both legacy ('user_inject') and typed ('user_inject_typed') gate."""
    sch, spy, root = spy_scheduler
    _write_pipeline(root, "beta", source="user_inject_typed", stage=PipelineStage.FAST_FAIL)

    asyncio.run(sch._maybe_run_knowledge_miner())

    assert spy.calls == []


def test_miner_skipped_when_inject_pipeline_is_blocked(spy_scheduler):
    """BLOCKED counts as alive: user may be waiting on a professor decision."""
    sch, spy, root = spy_scheduler
    _write_pipeline(root, "gamma", source="user_inject", stage=PipelineStage.BLOCKED)

    asyncio.run(sch._maybe_run_knowledge_miner())

    assert spy.calls == []


def test_miner_silent_after_inject_completes(spy_scheduler):
    """Inject scope is closed: COMPLETED inject does NOT re-enable Miner.
    Once the project enters focused mode it stays focused — operator must
    remove the inject pipeline directory to opt back into discovery.
    """
    sch, spy, root = spy_scheduler
    _write_pipeline(root, "delta", source="user_inject", stage=PipelineStage.COMPLETED)

    asyncio.run(sch._maybe_run_knowledge_miner())

    assert spy.calls == []


def test_miner_silent_when_inject_failed(spy_scheduler):
    sch, spy, root = spy_scheduler
    _write_pipeline(root, "epsilon", source="user_inject", stage=PipelineStage.FAILED)

    asyncio.run(sch._maybe_run_knowledge_miner())

    assert spy.calls == []


def test_miner_not_gated_by_non_inject_sources(spy_scheduler):
    """Backlog / professor-sourced pipelines do not trigger focused mode.
    The legacy 'no active pipelines' guard still applies, so we put it in
    COMPLETED to isolate the focused-mode check.
    """
    sch, spy, root = spy_scheduler
    _write_pipeline(root, "zeta", source="backlog_promotion", stage=PipelineStage.COMPLETED)

    asyncio.run(sch._maybe_run_knowledge_miner())

    assert len(spy.calls) == 1


def test_intake_gated_in_focused_mode(spy_scheduler):
    """Stale new_idea_*.yaml from a previous run must NOT spawn parallel
    pipelines while inject owns the scope. Intake must short-circuit."""
    sch, spy, root = spy_scheduler
    _write_pipeline(root, "iota", source="user_inject_typed", stage=PipelineStage.MAIN)
    proposals = root / "knowledge" / "proposals"
    proposals.mkdir(parents=True, exist_ok=True)
    (proposals / "new_idea_001.yaml").write_text(
        "title: stale idea\nresearch_question: x?\nbudget_request: 10000\n"
    )

    pre_runtime_count = len(list((root / "runtime" / "pipelines").glob("*"))) \
        if (root / "runtime" / "pipelines").exists() else 0
    asyncio.run(sch._intake_knowledge_proposals())
    post_runtime_count = len(list((root / "runtime" / "pipelines").glob("*"))) \
        if (root / "runtime" / "pipelines").exists() else 0

    # No new pipeline proposals should be queued; the stale yaml stays put
    # (not archived, not processed) so the operator can audit it.
    assert pre_runtime_count == post_runtime_count
    assert (proposals / "new_idea_001.yaml").exists()


def test_pause_flag_overrides_everything(spy_scheduler):
    sch, spy, root = spy_scheduler
    (root / "runtime" / ".miner_paused").write_text("")
    _write_pipeline(root, "eta", source="user_inject", stage=PipelineStage.COMPLETED)

    asyncio.run(sch._maybe_run_knowledge_miner())

    assert spy.calls == []
