"""Tests for the optimization enhancements:
1. HOLD decision type + allocated_budget
2. Falsification design propagation to pipeline dir
3. Backlog archive → wiki/failures recording
4. Backlog sweep & re-eval in scheduler tick
5. Topic evaluator gating integration in proposal intake
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from researchos.schemas.decision import DecisionType, ProfessorDecision
from researchos.schemas.evaluation import (
    EvaluationScores,
    FalsificationDesign,
    GateResult,
    InnovationStatement,
    RepeatDegree,
    TopicEvaluation,
)
from researchos.schemas.pipeline import PipelineStage, PipelineState
from researchos.runtime.topic_evaluator import BacklogManager


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal project directory structure."""
    for d in [
        "knowledge/wiki/failures",
        "knowledge/proposals",
        "knowledge/backlog",
        "knowledge/archive/rejected_ideas",
        "knowledge/archive/expired_ideas",
        "artifacts",
        "runtime/pipelines",
        "decisions",
    ]:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_evaluation(
    proposal_id="test_idea",
    title="Test Idea",
    gate_result=GateResult.REJECT,
    repeat_degree=RepeatDegree.HIGH,
    total_score=3,
) -> TopicEvaluation:
    """Helper to create a TopicEvaluation for testing."""
    scores = EvaluationScores(
        significance=1,
        novelty=0,
        differentiation=1,
        feasibility=1,
        falsifiability=0,
    )
    if total_score >= 9:
        scores = EvaluationScores(
            significance=2, novelty=2, differentiation=2,
            feasibility=2, falsifiability=2,
        )
    elif total_score >= 5:
        scores = EvaluationScores(
            significance=2, novelty=1, differentiation=1,
            feasibility=1, falsifiability=1,
        )

    return TopicEvaluation(
        proposal_id=proposal_id,
        title=title,
        research_question="Is X better than Y?",
        direction="general",
        kb_duplicates=[],
        kb_related=[],
        similar_papers=[],
        repeat_degree=repeat_degree,
        scores=scores,
        innovation=InnovationStatement(
            problem_gap="Existing methods fail because ...",
            our_delta="We propose ...",
            why_it_matters="Which improves ... under ...",
        ),
        falsification=FalsificationDesign(
            hypothesis="X outperforms Y on metric Z",
            baseline="Y",
            metric="Z",
            failure_condition="improvement < 2%",
            expected_signal="5% improvement",
            min_budget_ratio=0.1,
        ),
        gate_result=gate_result,
        gate_reasons=["test reason"],
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )


# ── Test 1: HOLD decision type + allocated_budget ──────────────────


class TestHoldDecision:

    def test_hold_decision_type_exists(self):
        assert DecisionType.HOLD == "hold"
        assert "hold" in [d.value for d in DecisionType]

    def test_professor_decision_with_hold(self):
        d = ProfessorDecision(
            pipeline_id="pipe_1",
            decision=DecisionType.HOLD,
            comment="Waiting for related experiment results",
        )
        assert d.decision == DecisionType.HOLD
        assert d.comment == "Waiting for related experiment results"

    def test_allocated_budget_field(self):
        d = ProfessorDecision(
            pipeline_id="pipe_1",
            decision=DecisionType.APPROVE,
            allocated_budget=5000,
        )
        assert d.allocated_budget == 5000

    def test_allocated_budget_defaults_none(self):
        d = ProfessorDecision(
            pipeline_id="pipe_1",
            decision=DecisionType.APPROVE,
        )
        assert d.allocated_budget is None

    def test_hold_decision_serialization(self):
        d = ProfessorDecision(
            pipeline_id="pipe_1",
            decision=DecisionType.HOLD,
            allocated_budget=3000,
            comment="hold for review",
        )
        data = json.loads(d.model_dump_json())
        assert data["decision"] == "hold"
        assert data["allocated_budget"] == 3000


# ── Test 2: Falsification design propagation ───────────────────────


class TestFalsificationPropagation:

    def test_propagation_from_evaluated_yaml(self, tmp_project):
        """When pipeline transitions to FAST_FAIL, falsification_design.yaml
        should be copied from evaluated_*.yaml to pipeline artifact dir."""
        from researchos.runtime.scheduler import ResearchScheduler

        # Write an evaluated file with falsification data
        eval_data = {
            "proposal_id": "test_pipeline",
            "title": "Test Pipeline",
            "falsification": {
                "hypothesis": "X is better than Y",
                "baseline": "Method Y",
                "metric": "accuracy",
                "failure_condition": "improvement < 2%",
                "expected_signal": "5%",
                "min_budget_ratio": 0.1,
            },
        }
        eval_path = tmp_project / "knowledge" / "proposals" / "evaluated_test_pipeline.yaml"
        eval_path.write_text(yaml.dump(eval_data))

        # Create pipeline state
        pipeline = PipelineState(
            pipeline_id="test_pipeline",
            title="Test Pipeline",
            research_question="Is X better?",
            stage=PipelineStage.PROPOSAL,
        )

        # Create scheduler and call propagation
        scheduler = ResearchScheduler(tmp_project, simulate=True)
        scheduler._propagate_falsification_design(pipeline)

        # Check the file was created
        dest = tmp_project / "artifacts" / "pipeline_test_pipeline" / "falsification_design.yaml"
        assert dest.exists(), "falsification_design.yaml should be created in pipeline dir"

        content = yaml.safe_load(dest.read_text())
        assert content["hypothesis"] == "X is better than Y"
        assert content["baseline"] == "Method Y"
        assert content["failure_condition"] == "improvement < 2%"

    def test_propagation_no_evaluated_file(self, tmp_project):
        """No crash when there's no matching evaluated file."""
        from researchos.runtime.scheduler import ResearchScheduler

        pipeline = PipelineState(
            pipeline_id="no_eval",
            title="No Eval Pipeline",
            research_question="?",
            stage=PipelineStage.PROPOSAL,
        )
        scheduler = ResearchScheduler(tmp_project, simulate=True)
        # Should not raise
        scheduler._propagate_falsification_design(pipeline)

        dest = tmp_project / "artifacts" / "pipeline_no_eval" / "falsification_design.yaml"
        assert not dest.exists()


# ── Test 3: Backlog archive → wiki/failures recording ──────────────


class TestWikiFailuresRecording:

    def test_archive_rejected_writes_to_wiki(self, tmp_project):
        bm = BacklogManager(tmp_project)
        evaluation = _make_evaluation(
            proposal_id="dup_idea_1",
            title="Duplicate Attention Mechanism",
            gate_result=GateResult.REJECT,
            repeat_degree=RepeatDegree.HIGH,
        )

        bm.archive_rejected(evaluation)

        # Check rejected archive file
        rejected_file = tmp_project / "knowledge" / "archive" / "rejected_ideas" / "rejected_dup_idea_1.yaml"
        assert rejected_file.exists()

        # Check wiki failures file
        wiki_file = tmp_project / "knowledge" / "wiki" / "failures" / "duplicate_ideas.md"
        assert wiki_file.exists()

        content = wiki_file.read_text()
        assert "Duplicate Attention Mechanism" in content
        assert "dup_idea_1" in content
        assert "high" in content.lower()

    def test_multiple_rejections_append(self, tmp_project):
        bm = BacklogManager(tmp_project)

        eval1 = _make_evaluation(proposal_id="idea_a", title="Idea A")
        eval2 = _make_evaluation(proposal_id="idea_b", title="Idea B")

        bm.archive_rejected(eval1)
        bm.archive_rejected(eval2)

        wiki_file = tmp_project / "knowledge" / "wiki" / "failures" / "duplicate_ideas.md"
        content = wiki_file.read_text()
        assert "Idea A" in content
        assert "Idea B" in content

    def test_wiki_file_has_frontmatter_on_first_write(self, tmp_project):
        bm = BacklogManager(tmp_project)
        evaluation = _make_evaluation()
        bm.archive_rejected(evaluation)

        wiki_file = tmp_project / "knowledge" / "wiki" / "failures" / "duplicate_ideas.md"
        content = wiki_file.read_text()
        assert "---" in content
        assert "title: Duplicate and Rejected Ideas" in content


# ── Test 4: Backlog sweep & re-eval ────────────────────────────────


class TestBacklogSweep:

    def test_sweep_expired_by_age(self, tmp_project):
        bm = BacklogManager(tmp_project)
        config = {"backlog": {"max_age_days": 1, "max_re_evals": 3}}

        # Create a backlog entry that's "old"
        entry = {
            "proposal": {"title": "Old Idea"},
            "evaluation": {"proposal_id": "old_idea"},
            "added_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
            "re_eval_count": 0,
        }
        backlog_file = tmp_project / "knowledge" / "backlog" / "backlog_old_idea.yaml"
        backlog_file.write_text(yaml.dump(entry))

        archived = bm.sweep_expired(config)
        assert "old_idea" in archived
        assert not backlog_file.exists()
        assert (tmp_project / "knowledge" / "archive" / "expired_ideas" / "backlog_old_idea.yaml").exists()

    def test_sweep_expired_by_re_eval_count(self, tmp_project):
        bm = BacklogManager(tmp_project)
        config = {"backlog": {"max_age_days": 30, "max_re_evals": 2}}

        entry = {
            "proposal": {"title": "Retried Idea"},
            "evaluation": {"proposal_id": "retried"},
            "added_at": datetime.now(timezone.utc).isoformat(),
            "re_eval_count": 3,  # exceeds max_re_evals
        }
        backlog_file = tmp_project / "knowledge" / "backlog" / "backlog_retried.yaml"
        backlog_file.write_text(yaml.dump(entry))

        archived = bm.sweep_expired(config)
        assert "retried" in archived

    def test_get_re_eval_candidates(self, tmp_project):
        bm = BacklogManager(tmp_project)
        config = {"backlog": {"re_eval_interval_hours": 1, "max_re_evals": 3}}

        # Candidate: old enough for re-eval
        entry = {
            "proposal": {"title": "Ready for Re-eval"},
            "evaluation": {
                "proposal_id": "ready",
                "evaluated_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            },
            "added_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "re_eval_count": 0,
        }
        f = tmp_project / "knowledge" / "backlog" / "backlog_ready.yaml"
        f.write_text(yaml.dump(entry))

        candidates = bm.get_re_eval_candidates(config)
        assert len(candidates) == 1
        assert candidates[0][0] == f

    def test_promote_from_backlog(self, tmp_project):
        bm = BacklogManager(tmp_project)
        entry = {
            "proposal": {"title": "Promotable", "research_question": "Is it good?"},
            "evaluation": {"proposal_id": "prom"},
            "added_at": datetime.now(timezone.utc).isoformat(),
            "re_eval_count": 1,
        }
        f = tmp_project / "knowledge" / "backlog" / "backlog_prom.yaml"
        f.write_text(yaml.dump(entry))

        result = bm.promote_from_backlog(f)
        assert result is not None
        assert result["title"] == "Promotable"
        assert not f.exists()  # removed from backlog


# ── Test 5: Scheduler HOLD decision handling ───────────────────────


class TestSchedulerHoldDecision:

    def test_apply_hold_decision(self, tmp_project):
        from researchos.runtime.scheduler import ResearchScheduler

        scheduler = ResearchScheduler(tmp_project, simulate=True)

        # Create pipeline in MAIN stage
        pipeline = PipelineState(
            pipeline_id="hold_test",
            title="Hold Test",
            research_question="?",
            stage=PipelineStage.MAIN,
        )
        scheduler.state.save_pipeline(pipeline)

        # Apply HOLD decision
        decision = ProfessorDecision(
            pipeline_id="hold_test",
            decision=DecisionType.HOLD,
            comment="Wait for baseline results",
        )
        # Write decision file so mark_decision_processed works
        decisions_dir = tmp_project / "decisions"
        dec_path = decisions_dir / f"approval_hold_test_{datetime.now().strftime('%Y%m%d%H%M%S')}.yaml"
        dec_path.write_text(yaml.dump(json.loads(decision.model_dump_json())))

        scheduler._apply_decision(decision)

        pipeline = scheduler.state.load_pipeline("hold_test")
        assert pipeline.stage == PipelineStage.BLOCKED
        assert "On hold" in pipeline.last_error
        assert pipeline.metadata.get("hold_reason") == "Wait for baseline results"

    def test_apply_allocated_budget(self, tmp_project):
        from researchos.runtime.scheduler import ResearchScheduler

        scheduler = ResearchScheduler(tmp_project, simulate=True)

        pipeline = PipelineState(
            pipeline_id="budget_test",
            title="Budget Test",
            research_question="?",
            stage=PipelineStage.PROPOSAL,
        )
        scheduler.state.save_pipeline(pipeline)

        # Set initial budget
        budget = scheduler.state.load_budget()
        budget.allocations["budget_test"] = 1000
        scheduler.state.save_budget(budget)

        # Apply decision with allocated_budget
        decision = ProfessorDecision(
            pipeline_id="budget_test",
            decision=DecisionType.APPROVE,
            allocated_budget=5000,
        )
        decisions_dir = tmp_project / "decisions"
        dec_path = decisions_dir / f"approval_budget_test_{datetime.now().strftime('%Y%m%d%H%M%S')}.yaml"
        dec_path.write_text(yaml.dump(json.loads(decision.model_dump_json())))

        scheduler._apply_decision(decision)

        budget = scheduler.state.load_budget()
        assert budget.allocations["budget_test"] == 5000  # explicitly set, not adjusted


# ── Test 6: Scheduler _process_backlog integration ──────────────────


class TestSchedulerProcessBacklog:

    def test_process_backlog_sweep(self, tmp_project):
        from researchos.runtime.scheduler import ResearchScheduler

        scheduler = ResearchScheduler(tmp_project, simulate=True)

        # Add expired backlog entry
        entry = {
            "proposal": {"title": "Expired"},
            "evaluation": {"proposal_id": "expired_1"},
            "added_at": (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),
            "re_eval_count": 0,
        }
        f = tmp_project / "knowledge" / "backlog" / "backlog_expired_1.yaml"
        f.write_text(yaml.dump(entry))

        asyncio.run(scheduler._process_backlog())

        # Should have been swept
        assert not f.exists()
        assert (tmp_project / "knowledge" / "archive" / "expired_ideas" / "backlog_expired_1.yaml").exists()


# ── Test 7: Config values ──────────────────────────────────────────


class TestConfig:

    def test_config_values(self):
        config_path = PROJECT_ROOT / "runtime" / "research_config.yaml"
        config = yaml.safe_load(config_path.read_text())

        assert config["backlog"]["max_age_days"] == 30
        assert config["backlog"]["re_eval_interval_hours"] == 168
        assert config["backlog"]["max_re_evals"] == 3

        assert config["topic_gating"]["min_total_score"] == 9
        assert config["retrieval"]["cache_ttl_hours"] == 24
        assert len(config["retrieval"]["top_venues"]) == 8


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
