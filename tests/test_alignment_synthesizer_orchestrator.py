"""Tests for researchos.runtime.alignment_synthesizer orchestrator.

Covers:
- Happy path: fake dispatch_fn copies a known-valid contract → run_synthesizer
  returns path and validates.
- Hash mismatch: dispatch_fn writes a contract with wrong paper_spec_hash →
  run_synthesizer raises RuntimeError.
- Missing paper_spec.yaml → FileNotFoundError.
- Missing proposal_spec.yaml → FileNotFoundError.
- Dispatch produces no file → RuntimeError.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml

from researchos.runtime.alignment_synthesizer import file_sha256, run_synthesizer
from researchos.schemas.alignment_contract import AlignmentContract


# ---------------------------------------------------------------------------
# Helpers to build minimal valid YAML fixtures
# ---------------------------------------------------------------------------

def _paper_spec_yaml() -> str:
    return yaml.dump({
        "paper_id": "test_paper_001",
        "title": "A Test Paper on Ensemble Methods",
        "authors": ["Alice", "Bob"],
        "venue": "ICML 2024",
        "problem_statement": "We study ensemble prediction for binary classification.",
        "research_questions": [
            {
                "id": "RQ1",
                "question": "Does the ensemble method outperform the baseline GLM?",
                "question_verbatim": "Does the ensemble method outperform the baseline GLM?",
                "paraphrase_reason": {"type": None, "explanation": ""},
                "paper_metrics": ["accuracy", "F1"],
                "paper_baseline_value": {"table_or_section": "Table 2", "value": "0.82"},
            }
        ],
        "baseline_method": {
            "name": "BMA_GLM",
            "family": "GLM",
            "key_components": ["GLM weighting", "Bayesian averaging"],
            "hyperparameters_stated": [],
            "algorithmic_sketch": "Weighted combination of GLM outputs.",
            "method_steps": [
                {"id": "S1", "name": "data_ingestion", "role": "Load data.", "paper_section": "§3.1"},
                {"id": "S2", "name": "glm_prediction", "role": "Predict with GLMs.", "paper_section": "§3.2"},
                {"id": "S3", "name": "bma_aggregation", "role": "Average predictions.", "paper_section": "§3.3"},
            ],
        },
        "candidate_model_pool": {
            "applicable": True,
            "members": ["GLM_logit", "GLM_probit", "GLM_cloglog"],
            "homogeneity": "all_GLMs",
            "notes": "",
        },
        "dataset": {
            "name": "rescueRobot_450",
            "domain": "robotics",
            "size": "450",
            "format": "csv",
            "features": ["speed", "distance"],
            "target": "rescue_success",
            "access": {
                "type": "paper_repo",
                "location": "data/rescueRobot_450.csv",
                "license": "MIT",
            },
        },
        "split_protocol": {
            "strategy": "random",
            "ratio": "70:15:15",
            "seed": "42",
            "notes": "",
        },
        "preprocessing": ["standardize", "impute_median"],
        "evaluation_metrics": [
            {"name": "accuracy", "direction": "higher_better", "formula_or_definition": "correct/total"},
            {"name": "F1", "direction": "higher_better", "formula_or_definition": "2*P*R/(P+R)"},
        ],
        "reference_code": {
            "available": True,
            "url": "https://github.com/example/bma",
            "commit_or_tag": "v1.0",
            "language": "Python",
            "install_command": "pip install -e .",
            "entry_points": [
                {"file": "bma/run.py", "symbol": "BMAAdaptation", "role": "baseline_train"},
            ],
        },
        "ablations_in_paper": [],
        "evidence": [
            {"field": "title", "quote": "A Test Paper on Ensemble Methods", "location": "title"},
            {"field": "dataset.name", "quote": "rescueRobot_450", "location": "§2"},
            {"field": "baseline_method.name", "quote": "BMA_GLM", "location": "§3"},
        ],
    }, default_flow_style=False)


def _proposal_spec_yaml() -> str:
    return yaml.dump({
        "proposal_id": "heterogeneous_pool_proposal",
        "title": "Heterogeneous Pool Extension for BMA",
        "target_paper": "test_paper_001",
        "change_type": "extension_of_method",
        "change_type_evidence": "We extend step S3 by augmenting the candidate pool with heterogeneous models.",
        "change_type_confidence": "high",
        "change_type_source": "extractor",
        "motivation": {
            "summary": "GLM-only pool may miss nonlinear patterns.",
            "cited_limitations_of_baseline": [
                {"text": "All candidates are GLMs.", "quote": "All candidates are GLMs."},
            ],
        },
        "proposed_method": {
            "name": "HeterogeneousBMA",
            "high_level_idea": "Add random forest and XGBoost to the candidate pool.",
            "new_components": [
                {
                    "id": "C_new_1",
                    "name": "heterogeneous_pool",
                    "role": "Augment GLM pool with RF and XGBoost.",
                    "math_sketch": "NOT_STATED in proposal",
                    "replaces": None,
                    "adds_to": "S3",
                },
            ],
            "borrowed_from_paper": [
                {"paper_field": "dataset.name", "quote": "We use the same rescueRobot_450 dataset."},
                {"paper_field": "split_protocol", "quote": "We keep the same 70:15:15 split."},
            ],
            "divergences": [
                {
                    "paper_field": "candidate_model_pool.members",
                    "proposal_says": "GLMs + RandomForest + XGBoost",
                    "proposal_quote": "We extend the pool with RF and XGBoost.",
                    "arbitration_hint": {
                        "content": "Use ARM_DEPENDENT to isolate pool composition effect.",
                        "authored_by": "proposal",
                        "binding": False,
                    },
                },
            ],
            "ablation_claims": [
                {
                    "id": "A1",
                    "description": "Ablation: heterogeneous pool vs GLM-only pool.",
                    "hypothesis": "Heterogeneous pool improves F1 by >=2pp.",
                    "threshold": "0.02",
                },
            ],
        },
        "scope": {
            "in_scope_rqs": ["RQ1"],
            "out_of_scope_rqs": [],
            "added_questions": [],
        },
        "success_criteria": [
            {"rq_id": "RQ1", "metric": "F1", "direction": "higher_better", "threshold": 0.02},
        ],
        "evidence": [
            {"field": "change_type", "quote": "We extend step S3 by augmenting the candidate pool.", "location": "§1"},
        ],
    }, default_flow_style=False)


def _valid_contract_yaml(paper_hash: str, proposal_hash: str, code_recon_hash: str | None = None) -> str:
    """Build a minimal valid AlignmentContract YAML that round-trips through pydantic."""
    data = {
        "contract_version": 1,
        "contract_status": "FINAL",
        "block_reason": None,
        "created_at": "2026-04-24T00:00:00Z",
        "sources": {
            "paper_spec_hash": paper_hash,
            "proposal_spec_hash": proposal_hash,
            "code_recon_hash": code_recon_hash,
        },
        "executive_summary": {
            "must_use_dataset": "rescueRobot_450",
            "must_use_split": "random 70:15:15 seed=42",
            "must_use_baseline": "BMA_GLM (import: bma.run.BMAAdaptation)",
            "arms_to_run": [
                {
                    "name": "baseline_arm",
                    "composition": "Paper BMA_GLM with GLM-only pool.",
                    "source_dim": None,
                    "required_by": None,
                },
                {
                    "name": "proposal_arm",
                    "composition": "HeterogeneousBMA with RF+XGBoost in pool.",
                    "source_dim": None,
                    "required_by": None,
                },
            ],
            "metrics_required": ["accuracy", "F1"],
            "forbidden": [
                "synthetic / proxy datasets",
                "baseline reimplementation when ref_code is available",
                "placeholder code: NotImplementedError, pass, ..., NOT_IMPLEMENTED string",
            ],
            "open_questions": 0,
        },
        "classification": {
            "change_type": "extension_of_method",
            "change_type_source": "proposal_extractor_confirmed",
            "superseded_extractor_value": None,
            "rationale": "Proposal augments step S3 without replacing it.",
            "evidence": {
                "from_paper": "§3.3: BMA aggregation step.",
                "from_proposal": "We extend step S3 by augmenting the candidate pool.",
            },
        },
        "confidence": "high",
        "judge_must_address": [],
        "dimensions": [
            {
                "dim": "dataset",
                "lock": "LOCKED_TO_PAPER",
                "value_ref": "paper_spec.dataset.name",
                "reason": "extension_of_method default; proposal_spec.proposed_method.borrowed_from_paper confirms dataset is unchanged.",
            },
            {
                "dim": "split_protocol",
                "lock": "LOCKED_TO_PAPER",
                "value_ref_subtree": "paper_spec.split_protocol",
                "reason": "extension_of_method default; proposal_spec.proposed_method.borrowed_from_paper confirms split unchanged.",
            },
            {
                "dim": "preprocessing",
                "lock": "AUGMENTED_BY_PROPOSAL",
                "base_value_ref_subtree": "paper_spec.preprocessing",
                "additions": [],
                "reason": "extension_of_method default; no preprocessing divergence in proposal_spec.",
            },
            {
                "dim": "baseline_algorithm",
                "lock": "LOCKED_TO_PAPER_VIA_REF_CODE",
                "value_ref_subtree": "paper_spec.baseline_method",
                "code_handle": {
                    "ref_code_url": "https://github.com/example/bma",
                    "install_command": "pip install -e .",
                    "import_path": "bma.run",
                    "symbol": "BMAAdaptation",
                    "loader_strategy": "import_from_ref_code",
                    "access_path": None,
                },
                "reason": "extension_of_method default; ref_code is available so VIA_REF_CODE applies.",
            },
            {
                "dim": "candidate_model_pool",
                "lock": "LOCKED_TO_PAPER",
                "value_ref_subtree": "paper_spec.candidate_model_pool.members",
                "reason": "extension_of_method default. Overrode proposal's arbitration_hint because ARM_DEPENDENT requires a clean arm split which was not achievable here; keeping LOCKED_TO_PAPER for baseline safety.",
            },
            {
                "dim": "metrics",
                "lock": "LOCKED_TO_PAPER_PLUS_PROPOSAL",
                "paper_metrics_ref_subtree": "paper_spec.evaluation_metrics",
                "proposal_added": [{"name": "F1", "direction": "higher_better"}],
                "reason": "extension_of_method default; proposal_spec.success_criteria adds F1.",
            },
            {
                "dim": "rq_coverage",
                "lock": "LOCKED_TO_PAPER",
                "in_scope": ["RQ1"],
                "out_of_scope": [],
                "reason": "extension_of_method default; proposal_spec.scope.in_scope_rqs=[RQ1].",
            },
            {
                "dim": "evaluation_protocol",
                "lock": "LOCKED_TO_PAPER",
                "value_ref_subtree": "paper_spec.split_protocol",
                "reason": "extension_of_method default; no evaluation_protocol divergence in proposal_spec.",
            },
            {
                "dim": "code_implementation_strategy",
                "lock": "FREE",
                "baseline": {
                    "source": "ref_code",
                    "import_path": "bma.run.BMAAdaptation",
                },
                "proposal": {
                    "strategy": "extend_baseline",
                    "new_components": ["C_new_1"],
                },
                "glue": {"harness": "run_experiment.py"},
                "reason": "code_implementation_strategy is always FREE per default-lock table.",
            },
        ],
        "preconditions": [
            {
                "check": "ref_code installable: pip install -e .",
                "enforced_at": "CODE_RECON",
                "status": "PENDING",
                "detail": "",
            },
            {
                "check": "ref_code symbol bma.run.BMAAdaptation importable",
                "enforced_at": "CODE_RECON",
                "status": "PENDING",
                "detail": "",
            },
            {
                "check": "data file at data/rescueRobot_450.csv exists",
                "enforced_at": "DESIGN_PREFLIGHT",
                "status": "PENDING",
                "detail": "",
            },
            {
                "check": "every in_scope RQ has metrics dim entry",
                "enforced_at": "ALIGNMENT_VALIDATOR",
                "status": "PENDING",
                "detail": "",
            },
        ],
        "open_tensions": [],
    }
    return yaml.dump(data, default_flow_style=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def pipeline_dir(tmp_path: Path) -> Path:
    """Return a temp pipeline directory pre-populated with paper/proposal specs."""
    pdir = tmp_path / "artifacts" / "pipeline_test001"
    pdir.mkdir(parents=True)

    (pdir / "paper_spec.yaml").write_text(_paper_spec_yaml(), encoding="utf-8")
    (pdir / "proposal_spec.yaml").write_text(_proposal_spec_yaml(), encoding="utf-8")
    return pdir


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestRunSynthesizerHappyPath:
    def test_returns_contract_path(self, pipeline_dir: Path) -> None:
        paper_hash = file_sha256(pipeline_dir / "paper_spec.yaml")
        proposal_hash = file_sha256(pipeline_dir / "proposal_spec.yaml")

        def fake_dispatch(*, pipeline_dir: Path, agent_name: str, prompt_extras: str) -> None:
            contract_yaml = _valid_contract_yaml(paper_hash, proposal_hash, code_recon_hash=None)
            (pipeline_dir / "alignment_contract.yaml").write_text(contract_yaml, encoding="utf-8")

        result = run_synthesizer(pipeline_dir, dispatch_fn=fake_dispatch)
        assert result == pipeline_dir / "alignment_contract.yaml"
        assert result.exists()

    def test_returned_contract_validates(self, pipeline_dir: Path) -> None:
        paper_hash = file_sha256(pipeline_dir / "paper_spec.yaml")
        proposal_hash = file_sha256(pipeline_dir / "proposal_spec.yaml")

        def fake_dispatch(*, pipeline_dir: Path, agent_name: str, prompt_extras: str) -> None:
            (pipeline_dir / "alignment_contract.yaml").write_text(
                _valid_contract_yaml(paper_hash, proposal_hash), encoding="utf-8"
            )

        result = run_synthesizer(pipeline_dir, dispatch_fn=fake_dispatch)
        data = yaml.safe_load(result.read_text(encoding="utf-8"))
        contract = AlignmentContract.model_validate(data)
        assert contract.contract_status == "FINAL"

    def test_prompt_extras_contains_hashes(self, pipeline_dir: Path) -> None:
        """dispatch_fn must receive prompt_extras with correct hashes."""
        paper_hash = file_sha256(pipeline_dir / "paper_spec.yaml")
        proposal_hash = file_sha256(pipeline_dir / "proposal_spec.yaml")
        captured: dict = {}

        def fake_dispatch(*, pipeline_dir: Path, agent_name: str, prompt_extras: str) -> None:
            captured["prompt_extras"] = prompt_extras
            (pipeline_dir / "alignment_contract.yaml").write_text(
                _valid_contract_yaml(paper_hash, proposal_hash), encoding="utf-8"
            )

        run_synthesizer(pipeline_dir, dispatch_fn=fake_dispatch)
        assert paper_hash in captured["prompt_extras"]
        assert proposal_hash in captured["prompt_extras"]

    def test_code_recon_hash_included_when_present(self, pipeline_dir: Path) -> None:
        """When code_recon.yaml exists, its hash is forwarded and verified."""
        # Create a minimal code_recon stub
        code_recon_content = yaml.dump({"stub": True})
        (pipeline_dir / "code_recon.yaml").write_text(code_recon_content, encoding="utf-8")

        paper_hash = file_sha256(pipeline_dir / "paper_spec.yaml")
        proposal_hash = file_sha256(pipeline_dir / "proposal_spec.yaml")
        code_recon_hash = file_sha256(pipeline_dir / "code_recon.yaml")

        def fake_dispatch(*, pipeline_dir: Path, agent_name: str, prompt_extras: str) -> None:
            (pipeline_dir / "alignment_contract.yaml").write_text(
                _valid_contract_yaml(paper_hash, proposal_hash, code_recon_hash=code_recon_hash),
                encoding="utf-8",
            )

        result = run_synthesizer(pipeline_dir, dispatch_fn=fake_dispatch)
        assert result.exists()

    def test_agent_name_passed_correctly(self, pipeline_dir: Path) -> None:
        paper_hash = file_sha256(pipeline_dir / "paper_spec.yaml")
        proposal_hash = file_sha256(pipeline_dir / "proposal_spec.yaml")
        captured: dict = {}

        def fake_dispatch(*, pipeline_dir: Path, agent_name: str, prompt_extras: str) -> None:
            captured["agent_name"] = agent_name
            (pipeline_dir / "alignment_contract.yaml").write_text(
                _valid_contract_yaml(paper_hash, proposal_hash), encoding="utf-8"
            )

        run_synthesizer(pipeline_dir, dispatch_fn=fake_dispatch)
        assert captured["agent_name"] == "alignment_synthesizer"


# ---------------------------------------------------------------------------
# Hash mismatch failures
# ---------------------------------------------------------------------------

class TestRunSynthesizerHashMismatch:
    def test_paper_hash_mismatch_raises(self, pipeline_dir: Path) -> None:
        proposal_hash = file_sha256(pipeline_dir / "proposal_spec.yaml")

        def fake_dispatch(*, pipeline_dir: Path, agent_name: str, prompt_extras: str) -> None:
            bad_paper_hash = "deadbeef" * 8  # wrong hash
            (pipeline_dir / "alignment_contract.yaml").write_text(
                _valid_contract_yaml(bad_paper_hash, proposal_hash), encoding="utf-8"
            )

        with pytest.raises(RuntimeError, match="paper_spec_hash mismatch"):
            run_synthesizer(pipeline_dir, dispatch_fn=fake_dispatch)

    def test_proposal_hash_mismatch_raises(self, pipeline_dir: Path) -> None:
        paper_hash = file_sha256(pipeline_dir / "paper_spec.yaml")

        def fake_dispatch(*, pipeline_dir: Path, agent_name: str, prompt_extras: str) -> None:
            bad_proposal_hash = "cafebabe" * 8  # wrong hash
            (pipeline_dir / "alignment_contract.yaml").write_text(
                _valid_contract_yaml(paper_hash, bad_proposal_hash), encoding="utf-8"
            )

        with pytest.raises(RuntimeError, match="proposal_spec_hash mismatch"):
            run_synthesizer(pipeline_dir, dispatch_fn=fake_dispatch)

    def test_code_recon_hash_mismatch_raises(self, pipeline_dir: Path) -> None:
        (pipeline_dir / "code_recon.yaml").write_text(yaml.dump({"stub": True}), encoding="utf-8")

        paper_hash = file_sha256(pipeline_dir / "paper_spec.yaml")
        proposal_hash = file_sha256(pipeline_dir / "proposal_spec.yaml")

        def fake_dispatch(*, pipeline_dir: Path, agent_name: str, prompt_extras: str) -> None:
            bad_code_recon_hash = "00000000" * 8
            (pipeline_dir / "alignment_contract.yaml").write_text(
                _valid_contract_yaml(paper_hash, proposal_hash, code_recon_hash=bad_code_recon_hash),
                encoding="utf-8",
            )

        with pytest.raises(RuntimeError, match="code_recon_hash mismatch"):
            run_synthesizer(pipeline_dir, dispatch_fn=fake_dispatch)


# ---------------------------------------------------------------------------
# Missing input files
# ---------------------------------------------------------------------------

class TestRunSynthesizerMissingInputs:
    def test_missing_paper_spec_raises(self, tmp_path: Path) -> None:
        pdir = tmp_path / "pipeline_no_paper"
        pdir.mkdir()
        # Only write proposal_spec
        (pdir / "proposal_spec.yaml").write_text(_proposal_spec_yaml(), encoding="utf-8")

        def fake_dispatch(**_kwargs) -> None:  # type: ignore[misc]
            pass

        with pytest.raises(FileNotFoundError, match="paper_spec.yaml"):
            run_synthesizer(pdir, dispatch_fn=fake_dispatch)

    def test_missing_proposal_spec_raises(self, tmp_path: Path) -> None:
        pdir = tmp_path / "pipeline_no_proposal"
        pdir.mkdir()
        # Only write paper_spec
        (pdir / "paper_spec.yaml").write_text(_paper_spec_yaml(), encoding="utf-8")

        def fake_dispatch(**_kwargs) -> None:  # type: ignore[misc]
            pass

        with pytest.raises(FileNotFoundError, match="proposal_spec.yaml"):
            run_synthesizer(pdir, dispatch_fn=fake_dispatch)

    def test_missing_both_raises_paper_spec_first(self, tmp_path: Path) -> None:
        pdir = tmp_path / "pipeline_empty"
        pdir.mkdir()

        def fake_dispatch(**_kwargs) -> None:  # type: ignore[misc]
            pass

        with pytest.raises(FileNotFoundError, match="paper_spec.yaml"):
            run_synthesizer(pdir, dispatch_fn=fake_dispatch)


# ---------------------------------------------------------------------------
# Dispatch produces no file
# ---------------------------------------------------------------------------

class TestRunSynthesizerNoOutputFile:
    def test_no_contract_file_raises(self, pipeline_dir: Path) -> None:
        def fake_dispatch(*, pipeline_dir: Path, agent_name: str, prompt_extras: str) -> None:
            # Deliberately produce nothing
            pass

        with pytest.raises(RuntimeError, match="did not produce"):
            run_synthesizer(pipeline_dir, dispatch_fn=fake_dispatch)


# ---------------------------------------------------------------------------
# Stub dispatch raises NotImplementedError by default
# ---------------------------------------------------------------------------

class TestRunSynthesizerDefaultStub:
    def test_default_dispatch_raises_not_implemented(self, pipeline_dir: Path) -> None:
        with pytest.raises(NotImplementedError, match="dispatch_fn must be wired"):
            run_synthesizer(pipeline_dir)


# ---------------------------------------------------------------------------
# file_sha256 utility
# ---------------------------------------------------------------------------

class TestFileSha256:
    def test_matches_hashlib(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        content = b"hello alignment synthesizer"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert file_sha256(f) == expected

    def test_different_content_different_hash(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_bytes(b"alpha")
        f2.write_bytes(b"beta")
        assert file_sha256(f1) != file_sha256(f2)
