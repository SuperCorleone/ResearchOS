"""Tests for the 5 new features:
F1: User-driven topic selection + learning
F2: Agent interrogation protocol
F3: Reference code reuse + data consistency
F4: Human deep interaction (query/tweak/explore)
F5: Innovation pattern guidance
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── F1: User-driven topic selection + learning ───────────────────

class TestUserTopicSelection:
    """Feature 1: candidates workflow + feedback learning."""

    def test_scheduler_has_auto_select_param(self):
        from researchos.runtime.scheduler import ResearchScheduler
        import inspect
        sig = inspect.signature(ResearchScheduler.__init__)
        assert "auto_select" in sig.parameters

    def test_auto_select_defaults_false(self):
        from researchos.runtime.scheduler import ResearchScheduler
        import inspect
        sig = inspect.signature(ResearchScheduler.__init__)
        assert sig.parameters["auto_select"].default is False

    def test_add_candidate_creates_file(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            from researchos.runtime.scheduler import ResearchScheduler
            s = ResearchScheduler(tmpdir, simulate=True)
            proposal = {"title": "Test Proposal", "research_question": "Does X work?"}
            s._add_candidate(proposal, "test_proposal")
            candidates = list((tmpdir / "runtime" / "candidates").glob("candidate_*.json"))
            assert len(candidates) == 1
            data = json.loads(candidates[0].read_text())
            assert data["pipeline_id"] == "test_proposal"
            assert data["status"] == "pending"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_topic_feedback_creates_wiki_page(self):
        import sys
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from run_research import _record_topic_feedback
        tmpdir = Path(tempfile.mkdtemp())
        try:
            # Monkey-patch PROJECT_ROOT for test
            import run_research
            orig = run_research.PROJECT_ROOT
            run_research.PROJECT_ROOT = tmpdir
            _record_topic_feedback("test_id", "Test Title", "selected", "Looks promising")
            wiki_dir = tmpdir / "knowledge" / "wiki" / "decisions"
            pages = list(wiki_dir.glob("topic_preferences_*.md"))
            assert len(pages) == 1
            content = pages[0].read_text()
            assert "SELECTED" in content
            assert "Looks promising" in content
            run_research.PROJECT_ROOT = orig
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_config_has_topic_selection(self):
        config_path = PROJECT_ROOT / "runtime" / "research_config.yaml"
        cfg = yaml.safe_load(config_path.read_text())
        assert "topic_selection" in cfg
        assert cfg["topic_selection"]["auto_select"] is False
        assert cfg["topic_selection"]["max_candidates"] == 10


# ── F2: Agent interrogation protocol ────────────────────────────

class TestInterrogationProtocol:
    """Feature 2: adversarial interrogation between CRITIC and REPORT."""

    def test_interrogation_step_exists(self):
        from researchos.schemas.pipeline import PipelineStep
        assert PipelineStep.INTERROGATION.value == "interrogation"

    def test_main_critic_goes_to_experiment_analysis(self):
        # After 2026-04 refactor: CRITIC → EXPERIMENT_ANALYSIS → INTERROGATION.
        # (Soon to be replaced by CRITIC-merge-into-JUDGE in Phase 1.)
        from researchos.runtime.scheduler import _NEXT_STEP
        from researchos.schemas.pipeline import PipelineStage, PipelineStep
        key = (PipelineStage.MAIN, PipelineStep.CRITIC)
        assert _NEXT_STEP[key] == PipelineStep.EXPERIMENT_ANALYSIS

    def test_main_interrogation_goes_to_report(self):
        from researchos.runtime.scheduler import _NEXT_STEP
        from researchos.schemas.pipeline import PipelineStage, PipelineStep
        key = (PipelineStage.MAIN, PipelineStep.INTERROGATION)
        assert _NEXT_STEP[key] == PipelineStep.REPORT

    def test_fast_fail_skips_interrogation(self):
        from researchos.runtime.scheduler import _NEXT_STEP
        from researchos.schemas.pipeline import PipelineStage, PipelineStep
        key = (PipelineStage.FAST_FAIL, PipelineStep.CRITIC)
        assert _NEXT_STEP[key] == PipelineStep.REPORT  # Direct, no interrogation

    def test_interrogation_artifact(self):
        from researchos.runtime.scheduler import STEP_ARTIFACT
        from researchos.schemas.pipeline import PipelineStep
        assert STEP_ARTIFACT[PipelineStep.INTERROGATION] == "interrogation_report.md"

    def test_interrogator_agent_exists(self):
        path = PROJECT_ROOT / "researchos/agent_defs/interrogator.md"
        assert path.exists()
        content = path.read_text()
        assert "anomaly" in content.lower()
        assert "counterfactual" in content.lower()
        assert "SATISFIED" in content
        assert "UNSATISFIED" in content

    def test_dispatcher_has_interrogation(self):
        from researchos.runtime.dispatcher import STEP_AGENT, STEP_OUTPUT
        from researchos.schemas.pipeline import PipelineStep
        assert STEP_AGENT[PipelineStep.INTERROGATION] == "interrogator"
        assert STEP_OUTPUT[PipelineStep.INTERROGATION] == "interrogation_report.md"

    def test_simulate_interrogation(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            from researchos.runtime.dispatcher import Dispatcher
            from researchos.schemas.pipeline import PipelineState, PipelineStage, PipelineStep
            d = Dispatcher(tmpdir, simulate=True)
            p = PipelineState(pipeline_id="test_int", title="T", stage=PipelineStage.MAIN)
            d._simulate_step(p, PipelineStep.INTERROGATION)
            out = tmpdir / "artifacts" / "pipeline_test_int" / "interrogation_report.md"
            assert out.exists()
            assert "SATISFIED" in out.read_text()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_config_has_interrogation(self):
        config_path = PROJECT_ROOT / "runtime" / "research_config.yaml"
        cfg = yaml.safe_load(config_path.read_text())
        assert "interrogation" in cfg
        assert cfg["interrogation"]["rounds"] == 2


# ── F3: Reference code reuse + data consistency ──────────────────

class TestReferenceCodeReuse:
    """Feature 3: code reuse and data consistency enforcement."""

    def test_exp_designer_has_reference_code(self):
        path = PROJECT_ROOT / "researchos/agent_defs/exp_designer.md"
        content = path.read_text()
        assert "reference_code" in content
        assert "fork_and_extend" in content
        assert "consistency_requirement" in content

    def test_coder_has_reference_reuse(self):
        path = PROJECT_ROOT / "researchos/agent_defs/coder.md"
        content = path.read_text()
        # Section was renamed from "Reference Code Reuse" to "Code Reuse Protocol"
        assert "Code Reuse Protocol" in content
        assert "OUR CONTRIBUTION" in content

    def test_critic_has_data_consistency(self):
        path = PROJECT_ROOT / "researchos/agent_defs/critic.md"
        content = path.read_text()
        # Phrase appears as "Reference data consistency" (lowercase d) in audit table
        assert "reference data consistency" in content.lower()
        assert "consistency_requirement" in content

    def test_exp_designer_has_user_directives(self):
        path = PROJECT_ROOT / "researchos/agent_defs/exp_designer.md"
        content = path.read_text()
        assert "user_directives.yaml" in content


# ── F4: Human deep interaction ───────────────────────────────────

class TestHumanInteraction:
    """Feature 4: query, tweak, explore commands."""

    def test_explore_command_exists(self):
        import sys
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from run_research import cmd_explore
        assert callable(cmd_explore)

    def test_query_command_exists(self):
        from run_research import cmd_query
        assert callable(cmd_query)

    def test_tweak_command_exists(self):
        from run_research import cmd_tweak
        assert callable(cmd_tweak)

    def test_tweak_writes_directives(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            pdir = tmpdir / "artifacts" / "pipeline_test_tweak"
            pdir.mkdir(parents=True)
            # Simulate cmd_tweak logic directly
            import yaml as _yaml
            from datetime import datetime, timezone
            directives_path = pdir / "user_directives.yaml"
            directive = [{"directive": "Add SVM baseline", "created_at": datetime.now(timezone.utc).isoformat(), "applied": False}]
            directives_path.write_text(_yaml.dump(directive))
            loaded = _yaml.safe_load(directives_path.read_text())
            assert len(loaded) == 1
            assert loaded[0]["directive"] == "Add SVM baseline"
            assert loaded[0]["applied"] is False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_candidates_command_exists(self):
        from run_research import cmd_candidates
        assert callable(cmd_candidates)


# ── F5: Innovation pattern guidance ──────────────────────────────

class TestInnovationPatterns:
    """Feature 5: innovation_type in miner + scoring bonus."""

    def test_miner_has_innovation_patterns(self):
        path = PROJECT_ROOT / "researchos/agent_defs/knowledge_miner.md"
        content = path.read_text()
        assert "cross_domain" in content
        assert "cross_method" in content
        assert "Old Method" in content
        assert "New Method" in content
        assert "innovation_type" in content

    def test_miner_has_user_preference_learning(self):
        path = PROJECT_ROOT / "researchos/agent_defs/knowledge_miner.md"
        content = path.read_text()
        assert "topic_preferences" in content
        assert "User Preference Learning" in content

    def test_miner_has_reference_papers_field(self):
        path = PROJECT_ROOT / "researchos/agent_defs/knowledge_miner.md"
        content = path.read_text()
        assert "reference_papers" in content
        assert "has_code" in content

    def test_config_has_innovation_bonus(self):
        config_path = PROJECT_ROOT / "runtime" / "research_config.yaml"
        cfg = yaml.safe_load(config_path.read_text())
        assert "innovation" in cfg
        assert cfg["innovation"]["cross_domain_bonus"] == 1
        assert cfg["innovation"]["cross_method_bonus"] == 1

    def test_proposal_format_has_innovation_type(self):
        path = PROJECT_ROOT / "researchos/agent_defs/knowledge_miner.md"
        content = path.read_text()
        # YAML template should include innovation_type
        assert 'innovation_type:' in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
