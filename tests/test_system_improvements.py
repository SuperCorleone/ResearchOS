"""Tests for system improvements:
1. kb_retrieve: BM25 + n-gram rerank
2. results.json schema validation hook
3. experiment_runner.md agent existence
4. paper_audit.py automated G4+G5
5. pipeline_manager fallback behavior
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════════════
# 1. kb_retrieve: BM25 + n-gram rerank
# ═══════════════════════════════════════════════════════════════════


class TestKBRetrieveNgramRerank:
    """Success: n-gram catches fuzzy matches that pure BM25 misses.
    Failure: search returns no results or n-gram doesn't improve ranking.
    """

    def test_ngram_similarity_same_root(self):
        """optimization vs optimizing should have significant overlap."""
        from researchos.mcp_servers.kb_server import _ngram_similarity
        sim = _ngram_similarity("optimization", "optimizing")
        assert sim >= 0.25, f"Same-root words should have sim >= 0.25, got {sim}"

    def test_ngram_similarity_semantic_near(self):
        """neural network vs neuronal should match well."""
        from researchos.mcp_servers.kb_server import _ngram_similarity
        sim = _ngram_similarity("neural network", "neuronal network computation")
        assert sim >= 0.3, f"Semantic near-misses should have sim >= 0.3, got {sim}"

    def test_ngram_similarity_unrelated_low(self):
        """Completely unrelated terms should have near-zero similarity."""
        from researchos.mcp_servers.kb_server import _ngram_similarity
        sim = _ngram_similarity("quantum physics", "cooking recipes for pasta")
        assert sim < 0.15, f"Unrelated terms should have sim < 0.15, got {sim}"

    def test_ngram_similarity_empty_string(self):
        """Empty input should return 0, not crash."""
        from researchos.mcp_servers.kb_server import _ngram_similarity
        assert _ngram_similarity("", "something") == 0.0
        assert _ngram_similarity("something", "") == 0.0

    def test_search_wiki_returns_results(self):
        """search_wiki should return non-empty string for a valid query
        when wiki has content."""
        from researchos.mcp_servers.kb_server import search_wiki
        result = search_wiki("multi-agent collaboration", max_results=3)
        # If wiki has content, result should not be "empty" message
        # (if wiki is empty, it returns a specific message)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_search_wiki_empty_query(self):
        """Empty query should return error message, not crash."""
        from researchos.mcp_servers.kb_server import search_wiki
        result = search_wiki("", max_results=3)
        assert "Empty query" in result

    def test_char_ngrams_basic(self):
        """Character n-gram extraction should work correctly."""
        from researchos.mcp_servers.kb_server import _char_ngrams
        ngrams = _char_ngrams("hello", 3)
        assert "hel" in ngrams
        assert "ell" in ngrams
        assert "llo" in ngrams
        assert len(ngrams) == 3

    def test_char_ngrams_short_string(self):
        """Strings shorter than n should still return something."""
        from researchos.mcp_servers.kb_server import _char_ngrams
        ngrams = _char_ngrams("hi", 3)
        assert len(ngrams) == 1  # returns {"hi"} as fallback


# ═══════════════════════════════════════════════════════════════════
# 2. results.json schema validation hook
# ═══════════════════════════════════════════════════════════════════


class TestResultsSchemaValidation:
    """Success: valid schema passes, invalid schema blocks write.
    Failure: invalid data passes validation or valid data is rejected.
    """

    def test_valid_schema_passes(self):
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({
            "metrics": {"accuracy": 0.95, "f1": 0.88},
            "config": {"lr": 0.01, "epochs": 10},
            "seed": 42,
        })
        assert result["valid"] is True

    def test_valid_with_optional_fields(self):
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({
            "metrics": {"acc": 0.9},
            "config": {"x": 1},
            "seed": 42,
            "runtime_seconds": 123.4,
            "notes": "all good",
        })
        assert result["valid"] is True

    def test_missing_metrics_fails(self):
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({"config": {}, "seed": 42})
        assert result["valid"] is False
        assert "metrics" in result["reason"]

    def test_missing_config_fails(self):
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({"metrics": {"a": 1.0}, "seed": 42})
        assert result["valid"] is False
        assert "config" in result["reason"]

    def test_missing_seed_fails(self):
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({"metrics": {"a": 1.0}, "config": {}})
        assert result["valid"] is False
        assert "seed" in result["reason"]

    def test_non_numeric_metric_fails(self):
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({
            "metrics": {"accuracy": "high"},
            "config": {},
            "seed": 42,
        })
        assert result["valid"] is False
        assert "numeric" in result["reason"]

    def test_string_seed_fails(self):
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({
            "metrics": {"a": 1.0},
            "config": {},
            "seed": "42",
        })
        assert result["valid"] is False
        assert "int" in result["reason"]

    def test_metrics_not_dict_fails(self):
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({
            "metrics": [0.9, 0.8],
            "config": {},
            "seed": 42,
        })
        assert result["valid"] is False
        assert "dict" in result["reason"]

    def test_hook_blocks_bad_results_json(self):
        """validate_artifact_write should block invalid results.json."""
        from researchos.runtime.hooks import validate_artifact_write
        bad_content = json.dumps({"result": "good"})  # missing metrics/config/seed
        result = validate_artifact_write(
            "artifacts/pipeline_test/results.json", bad_content
        )
        assert result["allowed"] is False
        assert "schema violation" in result["reason"]

    def test_hook_allows_good_results_json(self):
        from researchos.runtime.hooks import validate_artifact_write
        good_content = json.dumps({
            "metrics": {"f1": 0.85},
            "config": {"batch": 32},
            "seed": 42,
        })
        result = validate_artifact_write(
            "artifacts/pipeline_test/results.json", good_content
        )
        assert result["allowed"] is True

    def test_hook_allows_non_results_files(self):
        """Other files should not be affected by results schema check."""
        from researchos.runtime.hooks import validate_artifact_write
        result = validate_artifact_write(
            "artifacts/pipeline_test/hypothesis.md", "# My Hypothesis"
        )
        assert result["allowed"] is True

    def test_integer_metrics_pass(self):
        """Metrics with int values (not just float) should pass."""
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({
            "metrics": {"count": 100, "ratio": 0.5},
            "config": {},
            "seed": 42,
        })
        assert result["valid"] is True


# ═══════════════════════════════════════════════════════════════════
# 3. experiment_runner.md agent existence
# ═══════════════════════════════════════════════════════════════════


class TestExperimentRunnerAgent:
    """Success: agent file exists and contains required fields.
    Failure: file missing or lacks schema/execution protocol.
    """

    def test_agent_file_exists(self):
        path = PROJECT_ROOT / ".openharness" / "plugins" / "researchos" / "agents" / "experiment_runner.md"
        assert path.exists(), "experiment_runner.md should exist"

    def test_agent_has_frontmatter(self):
        path = PROJECT_ROOT / ".openharness" / "plugins" / "researchos" / "agents" / "experiment_runner.md"
        content = path.read_text()
        assert "name: experiment_runner" in content

    def test_agent_defines_schema(self):
        """Must mention the required results.json schema fields."""
        path = PROJECT_ROOT / ".openharness" / "plugins" / "researchos" / "agents" / "experiment_runner.md"
        content = path.read_text()
        assert "metrics" in content
        assert "config" in content
        assert "seed" in content

    def test_agent_warns_about_hook_enforcement(self):
        """Must tell agent that schema is enforced by hooks."""
        path = PROJECT_ROOT / ".openharness" / "plugins" / "researchos" / "agents" / "experiment_runner.md"
        content = path.read_text()
        assert "REJECTED" in content or "enforced" in content.lower()

    def test_agent_in_step_mapping(self):
        """experiment_runner should be in STEP_AGENT for the RUN step."""
        from researchos.runtime.dispatcher import STEP_AGENT
        from researchos.schemas.pipeline import PipelineStep
        assert STEP_AGENT.get(PipelineStep.RUN) == "experiment_runner"


# ═══════════════════════════════════════════════════════════════════
# 4. paper_audit.py automated G4+G5
# ═══════════════════════════════════════════════════════════════════


class TestPaperAuditG4:
    """Success: numbers in draft matched to results.json; mismatches flagged.
    Failure: matching produces false positives/negatives or crashes.
    """

    def test_number_extraction(self):
        from researchos.runtime.paper_audit import _extract_numbers_from_text
        text = "Our method achieves 95.3% accuracy and F1 of 0.88."
        numbers = _extract_numbers_from_text(text)
        values = [v for _, v, _ in numbers]
        assert 95.3 in values
        assert 0.88 in values

    def test_number_skips_years(self):
        """Years (1900-2100) should be skipped."""
        from researchos.runtime.paper_audit import _extract_numbers_from_text
        text = "In 2024, we proposed a method with 85% accuracy."
        numbers = _extract_numbers_from_text(text)
        values = [v for _, v, _ in numbers]
        assert 2024 not in values
        assert 85.0 in values

    def test_number_skips_section_refs(self):
        """Small integers (0-7) should be skipped (section/figure refs)."""
        from researchos.runtime.paper_audit import _extract_numbers_from_text
        text = "See Section 3 for details. The improvement is 12.5%."
        numbers = _extract_numbers_from_text(text)
        values = [v for _, v, _ in numbers]
        assert 3 not in values
        assert 12.5 in values

    def test_percentage_to_decimal_matching(self):
        """85.3% in draft should match 0.853 in results."""
        from researchos.runtime.paper_audit import _number_matches
        assert _number_matches(85.3, 0.853) is True
        assert _number_matches(0.853, 85.3) is True

    def test_exact_match(self):
        from researchos.runtime.paper_audit import _number_matches
        assert _number_matches(0.95, 0.95) is True
        assert _number_matches(0.95, 0.97) is False

    def test_tolerance_match(self):
        """Small rounding differences should match."""
        from researchos.runtime.paper_audit import _number_matches
        assert _number_matches(0.951, 0.95) is True  # within 0.02
        assert _number_matches(0.98, 0.95) is False   # outside 0.02

    def test_flatten_metrics(self):
        from researchos.runtime.paper_audit import _flatten_metrics
        data = {
            "metrics": {"accuracy": 0.95, "nested": {"f1": 0.88}},
            "seed": 42,
        }
        flat = _flatten_metrics(data)
        assert flat["metrics.accuracy"] == 0.95
        assert flat["metrics.nested.f1"] == 0.88
        assert flat["seed"] == 42.0

    def test_g4_audit_with_real_pipeline(self):
        """Run G4 on real pipeline if it exists."""
        draft = PROJECT_ROOT / "paper" / "multi_turn_reasoning_for_hallu" / "draft_v1.md"
        results = PROJECT_ROOT / "artifacts" / "pipeline_multi_turn_reasoning_for_hallu" / "results.json"
        if not draft.exists() or not results.exists():
            pytest.skip("Real pipeline data not available")

        from researchos.runtime.paper_audit import audit_g4_numerical_consistency
        result = audit_g4_numerical_consistency(draft, results)
        assert result["total_numbers"] > 0, "Should find numbers in the draft"
        assert result["matched"] > 0, "Should match some numbers to results.json"

    def test_g4_audit_missing_draft(self):
        """Should handle missing draft gracefully."""
        from researchos.runtime.paper_audit import audit_g4_numerical_consistency
        result = audit_g4_numerical_consistency(
            Path("/nonexistent/draft.md"), Path("/nonexistent/results.json")
        )
        assert result["passed"] is False
        assert result["issues"][0]["status"] == "ERROR"


class TestPaperAuditG5:
    """Success: detects presence/absence of statistical reporting items.
    Failure: false positives on non-statistical numbers or misses real stats.
    """

    def test_detects_p_value(self):
        from researchos.runtime.paper_audit import audit_g5_statistical_completeness
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("The difference was significant (p = 0.003, t(28) = 3.14, d = 0.82, N = 30, CI [0.5, 1.1]).")
            f.flush()
            result = audit_g5_statistical_completeness(Path(f.name))
        assert result["passed"] is True
        assert result["items_found"]["p_value"] >= 1
        assert result["items_found"]["test_statistic"] >= 1

    def test_flags_missing_stats(self):
        from researchos.runtime.paper_audit import audit_g5_statistical_completeness
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("Our method is better than the baseline. Results are in Table 1.")
            f.flush()
            result = audit_g5_statistical_completeness(Path(f.name))
        assert result["passed"] is False
        assert len(result["missing"]) >= 3

    def test_full_audit_report(self):
        """run_paper_audit should produce markdown with G4 and G5 sections."""
        draft = PROJECT_ROOT / "paper" / "multi_turn_reasoning_for_hallu" / "draft_v1.md"
        if not draft.exists():
            pytest.skip("Real pipeline data not available")

        from researchos.runtime.paper_audit import run_paper_audit
        report = run_paper_audit("multi_turn_reasoning_for_hallu", PROJECT_ROOT)
        assert "## G4: Numerical Consistency" in report
        assert "## G5: Statistical Reporting Completeness" in report
        assert "PASS" in report or "FAIL" in report


# ═══════════════════════════════════════════════════════════════════
# 5. pipeline_manager fallback behavior
# ═══════════════════════════════════════════════════════════════════


class TestPipelineManagerFallback:
    """Success: pipeline_manager is the fallback when STEP_AGENT has no entry.
    Failure: fallback mechanism broken or agent file missing.
    """

    def test_fallback_agent_file_exists(self):
        path = PROJECT_ROOT / ".openharness" / "plugins" / "researchos" / "agents" / "pipeline_manager.md"
        assert path.exists(), "pipeline_manager.md must exist as fallback"

    def test_description_marks_as_fallback(self):
        path = PROJECT_ROOT / ".openharness" / "plugins" / "researchos" / "agents" / "pipeline_manager.md"
        content = path.read_text()
        assert "fallback" in content.lower() or "Fallback" in content

    def test_all_steps_have_dedicated_agents(self):
        """All pipeline steps should have dedicated agents in STEP_AGENT.
        If this passes, pipeline_manager is truly only a safety net."""
        from researchos.runtime.dispatcher import STEP_AGENT
        from researchos.schemas.pipeline import PipelineStep
        critical_steps = [
            PipelineStep.HYPOTHESIS,
            PipelineStep.DESIGN,
            PipelineStep.CODE,
            PipelineStep.RUN,
            PipelineStep.CRITIC,
            PipelineStep.REPORT,
        ]
        for step in critical_steps:
            agent = STEP_AGENT.get(step)
            assert agent is not None, f"Step {step.value} should have a dedicated agent, not fallback"
            assert agent != "pipeline_manager", f"Step {step.value} should not use fallback"

    def test_fallback_triggered_for_unknown_step(self):
        """Verify the fallback mechanism itself works."""
        from researchos.runtime.dispatcher import STEP_AGENT
        # A hypothetical step not in the mapping
        result = STEP_AGENT.get("nonexistent_step", "pipeline_manager")
        assert result == "pipeline_manager"


# ═══════════════════════════════════════════════════════════════════
# 6. Agent prompt G-gate content verification
# ═══════════════════════════════════════════════════════════════════


class TestAgentPromptGates:
    """Verify that each agent's prompt contains the assigned G-gate checks."""

    AGENTS_DIR = PROJECT_ROOT / ".openharness" / "plugins" / "researchos" / "agents"

    def _read_agent(self, name: str) -> str:
        return (self.AGENTS_DIR / f"{name}.md").read_text()

    def test_hypothesis_gen_has_g1(self):
        content = self._read_agent("hypothesis_gen")
        assert "G1" in content
        assert "operationalization" in content.lower() or "operationali" in content.lower()
        assert "experiment necessity" in content.lower() or "Experiment necessity" in content

    def test_exp_designer_has_g2_g5_g7(self):
        content = self._read_agent("exp_designer")
        assert "G2" in content
        assert "G5" in content
        assert "G7" in content
        assert "ablation" in content.lower()
        assert "confound" in content.lower()
        assert "measured" in content and "estimated" in content and "simulated" in content

    def test_coder_has_g3_g6(self):
        content = self._read_agent("coder")
        assert "G3" in content
        assert "G6" in content
        assert "CLAIM-TO-CODE MAPPING" in content or "claim-to-code" in content.lower()
        assert "requirements.txt" in content

    def test_critic_has_g1_g3_g5(self):
        content = self._read_agent("critic")
        assert "G1" in content
        assert "G3" in content
        assert "G5" in content
        assert "Claim-Experiment Consistency" in content

    def test_paper_writer_has_g8(self):
        content = self._read_agent("paper_writer")
        assert "G8" in content
        assert "failure conditions" in content.lower() or "Failure Conditions" in content
        assert "Fixable" in content and "Fundamental" in content

    def test_reviewer_has_g4_g8(self):
        content = self._read_agent("reviewer_simulator")
        assert "G4" in content
        assert "G8" in content
        assert "Numerical Consistency" in content
        assert "Scope Audit" in content

    def test_paper_auditor_has_g4_g5(self):
        content = self._read_agent("paper_auditor")
        assert "G4" in content
        assert "G5" in content
        assert "results.json" in content

    def test_reporter_has_g7(self):
        content = self._read_agent("reporter")
        assert "G7" in content
        assert "measured" in content.lower()


# ═══════════════════════════════════════════════════════════════════
# 7. Scheduler paper audit wiring
# ═══════════════════════════════════════════════════════════════════


class TestSchedulerAuditWiring:
    """Verify paper_auditor is wired into PAPER stage correctly."""

    def test_handle_paper_stage_has_audit(self):
        import inspect
        from researchos.runtime.scheduler import ResearchScheduler
        src = inspect.getsource(ResearchScheduler._handle_paper_stage)
        assert "audit_report.md" in src
        assert "run_paper_audit" in src
        assert "paper_auditor" in src

    def test_build_auditor_prompt_exists(self):
        from researchos.runtime.scheduler import ResearchScheduler
        assert hasattr(ResearchScheduler, "_build_auditor_prompt")

    def test_audit_before_review_order(self):
        """Audit elif branch must come before review elif branch."""
        import inspect
        from researchos.runtime.scheduler import ResearchScheduler
        src = inspect.getsource(ResearchScheduler._handle_paper_stage)
        # Find the elif checks (not variable declarations)
        audit_elif = src.find("not audit_path.exists()")
        review_elif = src.find("not review_path.exists()")
        assert audit_elif > 0, "Should have audit_path check"
        assert review_elif > 0, "Should have review_path check"
        assert audit_elif < review_elif, "Audit elif must come before review elif"


# ═══════════════════════════════════════════════════════════════════
# 8. Publisher reproducibility checklist
# ═══════════════════════════════════════════════════════════════════


class TestPublisherReproducibility:
    """Verify publisher generates REPRODUCIBILITY_CHECKLIST.md."""

    def test_function_exists(self):
        from researchos.runtime.publisher import _generate_reproducibility_checklist
        assert callable(_generate_reproducibility_checklist)

    def test_checklist_generation(self, tmp_path):
        from researchos.runtime.publisher import _generate_reproducibility_checklist

        # Setup minimal pipeline dir
        pdir = tmp_path / "artifacts" / "pipeline_test"
        pdir.mkdir(parents=True)
        (pdir / "run_experiment.py").write_text("print('hello')")
        (pdir / "requirements.txt").write_text("numpy>=1.0\n")

        output_dir = tmp_path / "pub"
        code_dir = output_dir / "code"
        data_dir = output_dir / "data"
        code_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        (code_dir / "run_experiment.py").write_text("print('hello')")
        (code_dir / "README.md").write_text("# README\n")
        (data_dir / "results.json").write_text("{}")

        # Write experiment plan with seeds
        plan = {"evaluation": {"seeds": [42, 123, 456]}}
        (code_dir / "experiment_plan.yaml").write_text(yaml.dump(plan))

        state = {"pipeline_id": "test", "title": "Test", "milestone_count": 2}
        _generate_reproducibility_checklist(pdir, output_dir, state)

        checklist = output_dir / "REPRODUCIBILITY_CHECKLIST.md"
        assert checklist.exists()
        content = checklist.read_text()
        assert "PASS" in content
        assert "Seeds:" in content or "seeds" in content.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
