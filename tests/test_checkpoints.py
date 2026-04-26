"""Tests for checkpoint steps.

DESIGN_REVIEW was retired in the 2026-04 refactor — exp_designer now
runs its own scientific self-review, and design_checks.py handles the
structural checks (zero-token). The tests here cover the remaining
checkpoint step (CODE_PREFLIGHT) plus the DESIGN→CODE_RECON→CONCEPT→CODE
chain.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Pipeline Step enum ───────────────────────────────────────────

class TestPipelineStepEnum:
    def test_design_review_unrouted(self):
        """DESIGN_REVIEW step was retired in the 2026-04 refactor.

        The enum entry is kept for backward compat with old state.json
        files, but the step is no longer routed in either MAIN or
        FAST_FAIL — exp_designer now does its own self-review.
        """
        from researchos.runtime.scheduler import _NEXT_STEP
        from researchos.schemas.pipeline import PipelineStage, PipelineStep
        # DESIGN_REVIEW is not the destination of any active edge.
        assert PipelineStep.DESIGN_REVIEW not in _NEXT_STEP.values()
        # And there is no edge OUT of DESIGN_REVIEW in either stage.
        assert (PipelineStage.MAIN, PipelineStep.DESIGN_REVIEW) not in _NEXT_STEP
        assert (PipelineStage.FAST_FAIL, PipelineStep.DESIGN_REVIEW) not in _NEXT_STEP

    def test_code_preflight_exists(self):
        from researchos.schemas.pipeline import PipelineStep
        assert PipelineStep.CODE_PREFLIGHT.value == "code_preflight"

    def test_step_order(self):
        from researchos.schemas.pipeline import PipelineStep
        steps = list(PipelineStep)
        names = [s.value for s in steps]
        assert names.index("design") < names.index("code")
        assert names.index("code") < names.index("code_preflight")
        assert names.index("code_preflight") < names.index("run")


# ── Step routing (MAIN vs FAST_FAIL) ─────────────────────────────

class TestStepRouting:
    def test_main_design_goes_to_code_recon(self):
        """MAIN chain (2026-04-26 canonical): DESIGN → CODE_RECON →
        BASELINE_ANALYZE → DATA_RECON → CONCEPT_PAPER → CONCEPT_CODE → CODE.

        2-pass concept extraction (CONCEPT_PAPER + CONCEPT_CODE) replaces
        the legacy 1-pass CONCEPT_EXTRACT step. BASELINE_ANALYZE (LLM,
        code-first) sits between CODE_RECON and DATA_RECON.
        """
        from researchos.runtime.scheduler import _NEXT_STEP
        from researchos.schemas.pipeline import PipelineStage, PipelineStep
        assert (
            _NEXT_STEP[(PipelineStage.MAIN, PipelineStep.DESIGN)]
            == PipelineStep.CODE_RECON
        )
        assert (
            _NEXT_STEP[(PipelineStage.MAIN, PipelineStep.CODE_RECON)]
            == PipelineStep.BASELINE_ANALYZE
        )
        assert (
            _NEXT_STEP[(PipelineStage.MAIN, PipelineStep.BASELINE_ANALYZE)]
            == PipelineStep.DATA_RECON
        )
        assert (
            _NEXT_STEP[(PipelineStage.MAIN, PipelineStep.DATA_RECON)]
            == PipelineStep.CONCEPT_PAPER
        )
        assert (
            _NEXT_STEP[(PipelineStage.MAIN, PipelineStep.CONCEPT_PAPER)]
            == PipelineStep.CONCEPT_CODE
        )
        assert (
            _NEXT_STEP[(PipelineStage.MAIN, PipelineStep.CONCEPT_CODE)]
            == PipelineStep.CODE
        )

    def test_main_code_goes_to_preflight(self):
        from researchos.runtime.scheduler import _NEXT_STEP
        from researchos.schemas.pipeline import PipelineStage, PipelineStep
        key = (PipelineStage.MAIN, PipelineStep.CODE)
        assert _NEXT_STEP[key] == PipelineStep.CODE_PREFLIGHT

    def test_main_preflight_goes_to_run(self):
        from researchos.runtime.scheduler import _NEXT_STEP
        from researchos.schemas.pipeline import PipelineStage, PipelineStep
        key = (PipelineStage.MAIN, PipelineStep.CODE_PREFLIGHT)
        assert _NEXT_STEP[key] == PipelineStep.RUN

    def test_fast_fail_runs_full_research_backbone(self):
        """FAST_FAIL (2026-04-26 canonical) shares the research backbone
        with MAIN — including CODE_PREFLIGHT. The lighter terminal layer
        (no results_reverify, no figure_compose) is the only divergence;
        RUN itself is reduced via the scale_profile.fast_fail budget that
        exp_designer writes into experiment_plan.yaml.
        """
        from researchos.runtime.scheduler import _NEXT_STEP
        from researchos.schemas.pipeline import PipelineStage, PipelineStep
        assert (
            _NEXT_STEP[(PipelineStage.FAST_FAIL, PipelineStep.CODE)]
            == PipelineStep.CODE_PREFLIGHT
        )
        assert (
            _NEXT_STEP[(PipelineStage.FAST_FAIL, PipelineStep.CODE_PREFLIGHT)]
            == PipelineStep.RUN
        )
        assert (
            _NEXT_STEP[(PipelineStage.FAST_FAIL, PipelineStep.RUN)]
            == PipelineStep.JUDGE
        )

    def test_step_artifact_covers_active_steps(self):
        from researchos.runtime.scheduler import STEP_ARTIFACT
        from researchos.schemas.pipeline import PipelineStep
        # DESIGN_REVIEW was retired — no entry expected.
        assert STEP_ARTIFACT[PipelineStep.CODE_PREFLIGHT] == "preflight_report.json"


# ── Dispatcher mappings ──────────────────────────────────────────

class TestDispatcherMappings:
    def test_step_agent_covers_active_steps(self):
        from researchos.runtime.dispatcher import STEP_AGENT
        from researchos.schemas.pipeline import PipelineStep
        # DESIGN_REVIEW was retired — no entry expected.
        assert STEP_AGENT[PipelineStep.CODE_PREFLIGHT] == "code_preflight"

    def test_step_output_covers_active_steps(self):
        from researchos.runtime.dispatcher import STEP_OUTPUT
        from researchos.schemas.pipeline import PipelineStep
        # DESIGN_REVIEW was retired — no entry expected.
        assert STEP_OUTPUT[PipelineStep.CODE_PREFLIGHT] == "preflight_report.json"


# ── Design structural checks ────────────────────────────────────

class TestDesignChecks:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_plan(self, plan: dict):
        (self.tmpdir / "experiment_plan.yaml").write_text(yaml.dump(plan))

    def test_full_plan_all_pass(self):
        from researchos.runtime.design_checks import validate_design
        self._write_plan({
            "baselines": [{"name": "random"}, {"name": "strong_model"}],
            "evaluation": {
                "statistical_tests": [{"name": "t_test"}],
                "metrics": [{"name": "accuracy"}],
                "seeds": [42, 123, 456],
            },
            "success_criteria": ["accuracy > 0.7"],
            "failure_conditions": ["accuracy <= 0.5"],
            "data": {"source": "synthetic", "split": {"strategy": "random"}},
            "ablation": [{"component": "X"}],
            "confound_controls": {"random_seeds": True},
            "resource_budget": {"estimated_runtime": "5 min"},
        })
        result = validate_design(self.tmpdir)
        assert result["fail_count"] == 0
        assert result["pass_count"] >= 8

    def test_empty_plan_many_fails(self):
        from researchos.runtime.design_checks import validate_design
        self._write_plan({})
        result = validate_design(self.tmpdir)
        assert result["fail_count"] >= 4
        assert len(result["issues"]) >= 4

    def test_missing_baselines(self):
        from researchos.runtime.design_checks import validate_design
        self._write_plan({"evaluation": {"seeds": [1, 2, 3]}})
        result = validate_design(self.tmpdir)
        assert result["checks"].get("baselines_defined") is False

    def test_insufficient_seeds(self):
        from researchos.runtime.design_checks import validate_design
        self._write_plan({"evaluation": {"seeds": [42]}})
        result = validate_design(self.tmpdir)
        assert result["checks"].get("sufficient_seeds") is False

    def test_no_plan_file(self):
        from researchos.runtime.design_checks import validate_design
        result = validate_design(self.tmpdir)
        assert result["fail_count"] == 1
        assert "not found" in result["issues"][0]["detail"]


# ── Code preflight ───────────────────────────────────────────────

class TestCodePreflight:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_code(self, code: str):
        (self.tmpdir / "run_experiment.py").write_text(code)

    def _write_plan(self, plan: dict):
        (self.tmpdir / "experiment_plan.yaml").write_text(yaml.dump(plan))

    def _write_hypothesis(self, text: str):
        (self.tmpdir / "hypothesis.md").write_text(text)

    def test_valid_code_passes(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code("import numpy as np\nprint('hello')\n# --seed\n")
        self._write_plan({"evaluation": {"metrics": []}})
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        assert result["verdict"] == "PASS"

    def test_syntax_error_blocks(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code("def foo(\n  # missing close paren\n")
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        assert result["verdict"] == "BLOCK"
        assert result["checks"]["syntax_valid"] is False

    def test_missing_metrics_warns(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code("import numpy\nprint('hello')\n# seed\n")
        self._write_plan({
            "evaluation": {"metrics": [{"name": "accuracy"}, {"name": "f1_macro"}]},
        })
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        # Code doesn't mention accuracy or f1, should warn
        missing = [i for i in result["issues"] if i["check"] == "metrics"]
        assert len(missing) > 0

    def test_data_source_mismatch_errors(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "from sklearn.datasets import load_iris\n"
            "X, y = load_iris(return_X_y=True)\n"
            "# seed\n"
        )
        self._write_plan({"data": {"source": "file"}})
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        assert result["verdict"] == "BLOCK"
        data_issues = [i for i in result["issues"] if i["check"] == "data_source"]
        assert len(data_issues) > 0

    def test_synthetic_with_sklearn_ok(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "from sklearn.datasets import make_classification\n"
            "X, y = make_classification()\n"
            "# seed\n"
        )
        self._write_plan({"data": {"source": "synthetic"}})
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        # synthetic source allows sklearn
        data_issues = [i for i in result["issues"] if i["check"] == "data_source"]
        assert len(data_issues) == 0

    def test_timeout_risk_low(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code("x = 1\n# seed\n")
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        assert result["checks"]["timeout_risk"] == "LOW"

    def test_timeout_risk_high(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "import torch.nn\n"
            "for i in range(100000):\n"
            "    pass\n"
            "# seed\n"
        )
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        assert result["checks"]["timeout_risk"] in ("MEDIUM", "HIGH")

    def test_g3_mapping_detected(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "# seed\n"
            "# === CLAIM-TO-CODE MAPPING ===\n"
            "# | claim A | func_a() | mechanism |\n"
            "# | claim B | func_b() | mechanism |\n"
            "# === END MAPPING ===\n"
        )
        self._write_hypothesis("## Testable Predictions\n- Prediction A\n- Prediction B\n")
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        assert result["g3_implementation_coverage"]["claims_mapped"] == 2

    def test_no_code_returns_warn(self):
        from researchos.runtime.preflight import run_preflight
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        assert result["verdict"] == "WARN"

    def test_trivial_constant_return(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "def predict(x):\n"
            "    return 0.5\n"
            "# seed\n"
        )
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        trivial = result["g3_implementation_coverage"]["trivial_functions"]
        assert len(trivial) == 1
        assert trivial[0]["function"] == "predict"
        assert trivial[0]["reason"] == "constant_return"

    def test_trivial_passthrough(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "def transform(data):\n"
            "    return data\n"
            "# seed\n"
        )
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        trivial = result["g3_implementation_coverage"]["trivial_functions"]
        assert len(trivial) == 1
        assert trivial[0]["reason"] == "passthrough"

    def test_trivial_empty_body(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "def adapt_selector(state, options):\n"
            "    pass\n"
            "# seed\n"
        )
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        trivial = result["g3_implementation_coverage"]["trivial_functions"]
        assert len(trivial) == 1
        assert trivial[0]["reason"] == "empty_body"

    def test_trivial_all_paths_same(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "def classify(x):\n"
            "    if x > 0:\n"
            "        return 1\n"
            "    else:\n"
            "        return 1\n"
            "# seed\n"
        )
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        trivial = result["g3_implementation_coverage"]["trivial_functions"]
        assert len(trivial) == 1
        assert trivial[0]["reason"] == "all_paths_same_constant"

    def test_nontrivial_not_flagged(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "def predict(x):\n"
            "    w = compute_weights(x)\n"
            "    return np.dot(w, x)\n"
            "# seed\n"
        )
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        trivial = result["g3_implementation_coverage"]["trivial_functions"]
        assert len(trivial) == 0

    def test_docstring_not_counted_as_body(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "def stub(x):\n"
            '    """This does nothing."""\n'
            "    return 42\n"
            "# seed\n"
        )
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        trivial = result["g3_implementation_coverage"]["trivial_functions"]
        assert len(trivial) == 1
        assert trivial[0]["reason"] == "constant_return"

    def test_private_funcs_skipped(self):
        from researchos.runtime.preflight import run_preflight
        self._write_code(
            "def _helper():\n"
            "    return 0\n"
            "# seed\n"
        )
        result = run_preflight(self.tmpdir, self.tmpdir.parent)
        trivial = result["g3_implementation_coverage"]["trivial_functions"]
        assert len(trivial) == 0  # private funcs skipped


# ── Agent definitions ────────────────────────────────────────────

class TestAgentDefinitions:
    def test_design_reviewer_retired(self):
        """design_reviewer.md was deleted — its scientific review is now
        a Self-Review section inside exp_designer.md."""
        path = PROJECT_ROOT / "researchos/agent_defs/design_reviewer.md"
        assert not path.exists()
        exp_designer = (PROJECT_ROOT / "researchos/agent_defs/exp_designer.md").read_text()
        assert "Self-Review" in exp_designer

    def test_critic_references_preflight(self):
        path = PROJECT_ROOT / "researchos/agent_defs/critic.md"
        content = path.read_text()
        assert "preflight_report.json" in content
        assert "CODE_PREFLIGHT" in content


# ── Simulate mode ────────────────────────────────────────────────

class TestSimulateNewSteps:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        (self.tmpdir / "artifacts").mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # test_simulate_design_review removed — DESIGN_REVIEW step was retired
    # in the 2026-04 refactor (simulate path no longer produces design_review.md).

    def test_simulate_code_preflight(self):
        from researchos.runtime.dispatcher import Dispatcher
        from researchos.schemas.pipeline import PipelineState, PipelineStage, PipelineStep
        d = Dispatcher(self.tmpdir, simulate=True)
        p = PipelineState(
            pipeline_id="test_sim", title="T", stage=PipelineStage.MAIN,
            step=PipelineStep.CODE,
        )
        d._simulate_step(p, PipelineStep.CODE_PREFLIGHT)
        out = self.tmpdir / "artifacts" / "pipeline_test_sim" / "preflight_report.json"
        assert out.exists()
        report = json.loads(out.read_text())
        assert report["verdict"] == "PASS"
        assert report["simulated"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
