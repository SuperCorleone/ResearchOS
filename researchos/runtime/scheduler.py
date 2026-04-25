"""Research Runtime Scheduler -- stateless tick-based orchestrator.

Each ``tick()`` reads file state, applies decisions, and dispatches the next
atomic step for each active pipeline.  The scheduler itself holds no
long-lived state; everything is persisted in JSON/YAML files.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from researchos.runtime.branch_manager import BranchRunner

from researchos.schemas.pipeline import (
    PipelineDefinition,
    PipelineStage,
    PipelineState,
    PipelineStep,
)
from researchos.schemas.decision import DecisionType, ProfessorDecision
from researchos.runtime.dispatcher import Dispatcher
from researchos.runtime.knowledge_compiler import (
    compile_all_raw,
    compile_pipeline_to_wiki,
)
from researchos.runtime.latex_compiler import (
    compile_latex,
    copy_figures_to_paper_dir,
    md_to_latex,
)
from researchos.runtime.recovery import CheckpointManager, RetryPolicy
from researchos.runtime.state_manager import StateManager
from researchos.runtime.topic_evaluator import (
    BacklogManager,
    TopicEvaluator,
)
from researchos.runtime.workflow_loader import WorkflowConfig
from researchos.schemas.evaluation import GateResult

logger = logging.getLogger(__name__)

# Legacy dicts kept as module-level fallbacks for external code that imports
# them. The scheduler now uses WorkflowConfig internally.
#
# MAIN pipeline: DESIGN -> CODE_RECON -> CONCEPT_EXTRACT -> CODE ->
#                RUN -> RESULTS_REVERIFY -> JUDGE (or legacy CRITIC chain) ->
#                REPORT. (DESIGN_REVIEW retired 2026-04 — scientific review
#                folded into exp_designer self-review; structural checks
#                still run via design_checks.py.)
# FAST_FAIL pipeline keeps the lean path: no CODE_RECON, no CONCEPT_EXTRACT,
# no RESULTS_REVERIFY, no CODE_PREFLIGHT, no INTERROGATION.
#
# After the 2026-04 refactor, REPORT is the terminal step — HITL gates
# (WAITING_APPROVAL, PRE_PUBLISH_GATE) are removed. Professor auto-decides
# stage transitions via decisions/*.yaml.
_NEXT_STEP: dict[tuple[PipelineStage, PipelineStep | None], PipelineStep | None] = {
    # Alignment workflow (2026-04). All MAIN-stage pipelines enter via
    # PAPER_SPEC; legacy pipelines without typed evidence_refs SKIP each
    # alignment step in the dispatcher's specialised handler and advance
    # immediately to HYPOTHESIS.
    (PipelineStage.MAIN, None): PipelineStep.PAPER_SPEC,
    (PipelineStage.MAIN, PipelineStep.PAPER_SPEC): PipelineStep.PROPOSAL_SPEC,
    (PipelineStage.MAIN, PipelineStep.PROPOSAL_SPEC): PipelineStep.ALIGNMENT,
    (PipelineStage.MAIN, PipelineStep.ALIGNMENT): PipelineStep.ALIGNMENT_VALIDATOR,
    (PipelineStage.MAIN, PipelineStep.ALIGNMENT_VALIDATOR): PipelineStep.HYPOTHESIS,
    (PipelineStage.MAIN, PipelineStep.HYPOTHESIS): PipelineStep.DESIGN,
    (PipelineStage.MAIN, PipelineStep.DESIGN): PipelineStep.CODE_RECON,
    (PipelineStage.MAIN, PipelineStep.CODE_RECON): PipelineStep.BASELINE_ANALYZE,
    (PipelineStage.MAIN, PipelineStep.BASELINE_ANALYZE): PipelineStep.DATA_RECON,
    (PipelineStage.MAIN, PipelineStep.DATA_RECON): PipelineStep.CONCEPT_EXTRACT,
    (PipelineStage.MAIN, PipelineStep.CONCEPT_EXTRACT): PipelineStep.CODE,
    (PipelineStage.MAIN, PipelineStep.CODE): PipelineStep.CODE_PREFLIGHT,
    (PipelineStage.MAIN, PipelineStep.CODE_PREFLIGHT): PipelineStep.RUN,
    (PipelineStage.MAIN, PipelineStep.RUN): PipelineStep.RESULTS_REVERIFY,
    (PipelineStage.MAIN, PipelineStep.RESULTS_REVERIFY): PipelineStep.CRITIC,
    (PipelineStage.MAIN, PipelineStep.CRITIC): PipelineStep.EXPERIMENT_ANALYSIS,
    (PipelineStage.MAIN, PipelineStep.EXPERIMENT_ANALYSIS): PipelineStep.INTERROGATION,
    (PipelineStage.MAIN, PipelineStep.INTERROGATION): PipelineStep.REPORT,
    (PipelineStage.FAST_FAIL, None): PipelineStep.HYPOTHESIS,
    (PipelineStage.FAST_FAIL, PipelineStep.HYPOTHESIS): PipelineStep.DESIGN,
    # FAST_FAIL skips CODE_PREFLIGHT / INTERROGATION for speed.
    (PipelineStage.FAST_FAIL, PipelineStep.DESIGN): PipelineStep.CODE,
    (PipelineStage.FAST_FAIL, PipelineStep.CODE): PipelineStep.RUN,
    (PipelineStage.FAST_FAIL, PipelineStep.RUN): PipelineStep.CRITIC,
    (PipelineStage.FAST_FAIL, PipelineStep.CRITIC): PipelineStep.REPORT,
}

STEP_ARTIFACT: dict[PipelineStep, str] = {
    # Alignment workflow artifacts
    PipelineStep.PAPER_SPEC: "paper_spec.yaml",
    PipelineStep.PROPOSAL_SPEC: "proposal_spec.yaml",
    PipelineStep.ALIGNMENT: "alignment_contract.yaml",
    PipelineStep.ALIGNMENT_VALIDATOR: "alignment_validation_report.json",

    PipelineStep.HYPOTHESIS: "hypothesis.md",
    PipelineStep.DESIGN: "experiment_plan.yaml",
    # DESIGN_REVIEW retired 2026-04 (scientific review is now exp_designer's
    # own self-review pass; structural checks stay in design_checks.py).
    PipelineStep.CODE_RECON: "code_recon.yaml",
    PipelineStep.BASELINE_ANALYZE: "baseline_analysis.yaml",
    PipelineStep.DATA_RECON: "data_manifest.yaml",
    PipelineStep.CONCEPT_EXTRACT: "concepts.yaml",
    PipelineStep.CONCEPT_PAPER: "concepts_paper.digest.json",
    PipelineStep.CONCEPT_CODE: "concepts.digest.json",
    PipelineStep.CODE: "code_patch.diff",
    PipelineStep.CODE_PREFLIGHT: "preflight_report.json",
    PipelineStep.RUN: "results.json",
    PipelineStep.RESULTS_REVERIFY: "reverify_report.json",
    PipelineStep.CRITIC: "analysis_report.md",
    PipelineStep.INTERROGATION: "interrogation_report.md",
    PipelineStep.EXPERIMENT_ANALYSIS: "experiment_analysis.md",
    PipelineStep.JUDGE: "judge_verdict.json",
    PipelineStep.BASELINE_CONFIRM: "metric_contract.json",
}


# ── Stream-provided imports (guarded) ────────────────────────────
# All of these come from other Wave 2 streams that may land after this
# scheduler update.  Missing modules are tolerated — features that need
# them degrade to no-ops rather than crashing the scheduler.

try:  # results reverification
    from researchos.runtime.reverify import reverify_results as _reverify_results  # noqa: F401
except ImportError:  # pragma: no cover — optional Wave 2 dep
    _reverify_results = None  # type: ignore[assignment]
    logger.warning("reverify module not available; RESULTS_REVERIFY will be a no-op")

try:  # baseline divergence triage (Change 4C / 4D)
    from researchos.runtime.baseline_diagnose import diagnose as _baseline_diagnose_fn  # noqa: F401
except ImportError:  # pragma: no cover
    _baseline_diagnose_fn = None  # type: ignore[assignment]
    logger.info("baseline_diagnose module not available; BASELINE_DIAGNOSE will be a no-op")

try:  # code-reference reconnaissance
    from researchos.runtime.code_recon import run_code_recon as _run_code_recon_fn  # noqa: F401
except ImportError:  # pragma: no cover
    _run_code_recon_fn = None  # type: ignore[assignment]
    logger.info("code_recon module not available; CODE_RECON will SKIP")

try:  # data-reference reconnaissance + auto-download
    from researchos.runtime.data_recon import run_data_recon as _run_data_recon_fn  # noqa: F401
except ImportError:  # pragma: no cover — module always present, guard is defensive
    _run_data_recon_fn = None  # type: ignore[assignment]
    logger.info("data_recon module not available; DATA_RECON will SKIP")

try:  # lineage view (Layer 2): rebuild artifacts/pipeline_<id>/lineage.json
    from researchos.runtime.lineage import write_pipeline_lineage as _write_lineage_fn  # noqa: F401
except ImportError:  # pragma: no cover
    _write_lineage_fn = None  # type: ignore[assignment]
    logger.info("lineage module not available; lineage.json will not be rebuilt")

try:  # baseline metric contract registry
    from researchos.runtime import baseline_registry as _baseline_registry  # noqa: F401
except ImportError:  # pragma: no cover
    _baseline_registry = None  # type: ignore[assignment]
    logger.info("baseline_registry module not available; BASELINE_CONFIRM will skip")

try:  # findings memory service
    from researchos.runtime import findings_memory as _findings_memory  # noqa: F401
except ImportError:  # pragma: no cover
    _findings_memory = None  # type: ignore[assignment]
    logger.info("findings_memory module not available; pipeline completion will not persist findings")

try:  # JSON-marker LLM helper
    from researchos.runtime.llm_client import (
        extract_json_between_markers as _extract_json_between_markers,
    )  # noqa: F401
except ImportError:  # pragma: no cover
    _extract_json_between_markers = None  # type: ignore[assignment]
    logger.debug("llm_client.extract_json_between_markers not available; using inline parsers")


class ResearchScheduler:
    """Stateless scheduler that reads file state and dispatches work."""

    def __init__(
        self,
        project_root: Path,
        interval_seconds: int = 60,
        *,
        simulate: bool = False,
        backend: str = "auto",
        auto_professor: bool = False,
        auto_select: bool = False,
        max_milestones: int = 3,
        human_review: bool = False,
        single_run: bool = False,
    ) -> None:
        self.project_root = project_root
        self.interval = interval_seconds
        self.simulate = simulate
        self.auto_professor = auto_professor
        self.auto_select = auto_select
        self.max_milestones = max_milestones
        self.human_review = human_review
        self.single_run = single_run
        self._miner_has_run = False
        self.state = StateManager(project_root)
        self.dispatcher = Dispatcher(
            project_root, simulate=simulate, backend=backend
        )
        self.checkpoints = CheckpointManager(project_root)
        # Load retry constants from workflow_config.yaml (not hardcoded).
        self.retry_policy = RetryPolicy(project_root=project_root)
        self.evaluator = TopicEvaluator(project_root, self.dispatcher)
        self.backlog = BacklogManager(project_root)

        # Load workflow configuration (YAML or hardcoded defaults)
        self.workflow = WorkflowConfig.load(project_root)
        self._next_step_map = self.workflow.build_next_step_map()
        # Wave 2: overlay new-step edges on top of the workflow-derived map so
        # CONCEPT_EXTRACT, RESULTS_REVERIFY, BASELINE_CONFIRM, and the
        # PRE_PUBLISH_GATE land in the routing table even when the YAML
        # config hasn't been migrated yet.
        self._apply_wave2_edges()

        # Branching support now flows through BranchRunner (per-pipeline).
        # The scheduler only holds the knobs; runners are constructed on
        # demand below.
        self._artifacts_root = project_root / "artifacts"

        # Load config
        self.interrogation_rounds = 2
        self.branching_enabled = False
        self.max_branches = 3
        config_path = project_root / "runtime" / "research_config.yaml"
        if config_path.exists():
            import yaml as _yaml
            try:
                cfg = _yaml.safe_load(config_path.read_text()) or {}
                self.interrogation_rounds = cfg.get("interrogation", {}).get("rounds", 2)
                if not self.auto_select:
                    self.auto_select = cfg.get("topic_selection", {}).get("auto_select", False)
                branching_cfg = cfg.get("branching", {})
                self.branching_enabled = branching_cfg.get("enabled", False)
                self.max_branches = branching_cfg.get("max_branches", 3)
            except Exception:
                pass

    def _branch_runner(self, pipeline_id: str) -> "BranchRunner":
        """Construct a BranchRunner for ``pipeline_id`` on demand."""
        from researchos.runtime.branch_manager import BranchRunner
        return BranchRunner(
            pipeline_id=pipeline_id,
            artifacts_root=self._artifacts_root,
            num_branches=self.max_branches,
        )

    async def run_loop(self) -> None:
        """Main scheduler loop."""
        logger.info("Scheduler started (interval=%ds)", self.interval)
        self.state.append_log("Scheduler started")
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Tick failed")
                self.state.append_log("ERROR: tick failed, see logs")
            await asyncio.sleep(self.interval)

    # ── Wave 2 routing overlay ────────────────────────────────────

    def _apply_wave2_edges(self) -> None:
        """Patch ``_next_step_map`` with the new Wave 2 edges.

        The YAML-driven WorkflowConfig doesn't (yet) know about
        CONCEPT_EXTRACT, RESULTS_REVERIFY, BASELINE_CONFIRM, or the
        PRE_PUBLISH_GATE.  We inject those edges here so the scheduler can
        route through them without requiring every deployment to migrate
        the workflow YAML simultaneously.
        """
        main_stage = PipelineStage.MAIN.value
        fast_fail_stage = PipelineStage.FAST_FAIL.value
        edges_main = {
            # Alignment workflow (2026-04): PAPER_SPEC -> PROPOSAL_SPEC ->
            # ALIGNMENT -> ALIGNMENT_VALIDATOR -> HYPOTHESIS. Legacy pipelines
            # SKIP each alignment step in the dispatcher.
            (main_stage, None): PipelineStep.PAPER_SPEC.value,
            (main_stage, PipelineStep.PAPER_SPEC.value): PipelineStep.PROPOSAL_SPEC.value,
            (main_stage, PipelineStep.PROPOSAL_SPEC.value): PipelineStep.ALIGNMENT.value,
            (main_stage, PipelineStep.ALIGNMENT.value): PipelineStep.ALIGNMENT_VALIDATOR.value,
            (main_stage, PipelineStep.ALIGNMENT_VALIDATOR.value): PipelineStep.HYPOTHESIS.value,
            # DESIGN -> CODE_RECON -> BASELINE_ANALYZE -> DATA_RECON -> CONCEPT_EXTRACT -> CODE
            # (DESIGN_REVIEW step retired 2026-04; exp_designer self-reviews.)
            # (BASELINE_ANALYZE inserted v5.3 between CODE_RECON and DATA_RECON:
            #  LLM analyses cloned baseline package code-first, identifies real
            #  data files vs. badges/logs/plots; replaces the v5.2 hardcoded
            #  heuristic tables in data_recon.py.)
            (main_stage, PipelineStep.DESIGN.value): PipelineStep.CODE_RECON.value,
            (main_stage, PipelineStep.CODE_RECON.value): PipelineStep.BASELINE_ANALYZE.value,
            (main_stage, PipelineStep.BASELINE_ANALYZE.value): PipelineStep.DATA_RECON.value,
            (main_stage, PipelineStep.DATA_RECON.value): PipelineStep.CONCEPT_EXTRACT.value,
            (main_stage, PipelineStep.CONCEPT_EXTRACT.value): PipelineStep.CODE.value,
            # RUN -> RESULTS_REVERIFY -> CRITIC -> JUDGE -> REPORT
            (main_stage, PipelineStep.RUN.value): PipelineStep.RESULTS_REVERIFY.value,
            (main_stage, PipelineStep.RESULTS_REVERIFY.value): PipelineStep.CRITIC.value,
            (main_stage, PipelineStep.CRITIC.value): PipelineStep.JUDGE.value,
            (main_stage, PipelineStep.JUDGE.value): PipelineStep.REPORT.value,
        }
        # H1: BASELINE_CONFIRM is the first step on FAST_FAIL entry.  The
        # handler (_run_baseline_confirm) writes metric_contract.json when
        # experiment_plan.yaml has a `baseline` block, and SKIPs gracefully
        # otherwise — so every pipeline passes through, but only baselined
        # pipelines produce the contract artifact.  After that we resume
        # the original HYPOTHESIS entry.
        edges_fast_fail = {
            (fast_fail_stage, None): PipelineStep.BASELINE_CONFIRM.value,
            (fast_fail_stage, PipelineStep.BASELINE_CONFIRM.value): PipelineStep.HYPOTHESIS.value,
        }
        # FAST_FAIL: stay lean otherwise. Keep existing RUN->CRITIC intact.
        for key, value in edges_main.items():
            self._next_step_map[key] = value
        for key, value in edges_fast_fail.items():
            self._next_step_map[key] = value
        # Make sure FAST_FAIL routes don't accidentally pick up the new MAIN
        # edges if some future YAML declares a shared step.
        self._next_step_map.setdefault(
            (fast_fail_stage, PipelineStep.RUN.value), PipelineStep.CRITIC.value
        )

    async def tick(self) -> None:
        """Single scheduling cycle -- idempotent."""
        logger.debug("=== tick ===")

        # 0. Compile any new raw files into wiki (PDF, md, txt)
        try:
            raw_pages = compile_all_raw(self.project_root)
            if raw_pages:
                self.state.append_log(
                    f"Raw compilation: {len(raw_pages)} new wiki pages from /knowledge/raw/"
                )
        except Exception:
            logger.exception("Raw compilation failed")

        # 1. Apply pending professor decisions
        for decision in self.state.load_pending_decisions():
            self._apply_decision(decision)

        # 1b. Intake research proposals from /knowledge/proposals/
        await self._intake_knowledge_proposals()

        # 2. Process new pipeline proposals (from runtime/pipelines/)
        for proposal in self.state.load_new_proposals():
            self._initialise_pipeline(proposal)

        # 3. Check budget
        budget = self.state.load_budget()

        # 4. Schedule next step for each active pipeline (priority order)
        pipelines = self.state.load_all_pipelines()
        pipelines.sort(key=lambda p: -p.priority)  # Higher priority first
        schedule_tasks = []

        for pipeline in pipelines:
            # ── Change 4D: check for human pivot response ────────────────
            # If the pipeline is BLOCKED and waiting for a human pivot
            # decision, poll for a pivot_response file and act on it.
            if (
                pipeline.stage == PipelineStage.BLOCKED
                and pipeline.step == PipelineStep.AWAITING_HUMAN_PIVOT
            ):
                try:
                    self._check_pivot_response(pipeline)
                except Exception:
                    logger.exception(
                        "Pivot response check failed for %s", pipeline.pipeline_id
                    )
                continue  # stay blocked until response processed

            if pipeline.stage in (
                PipelineStage.COMPLETED,
                PipelineStage.FAILED,
                PipelineStage.BLOCKED,
            ):
                continue

            if self.dispatcher.is_running(pipeline.pipeline_id):
                continue

            # Check budget
            allocated = budget.allocations.get(pipeline.pipeline_id, 0)
            used = budget.usage.get(pipeline.pipeline_id, 0)
            if allocated > 0 and used >= allocated:
                logger.info(
                    "Pipeline %s over budget (%d/%d)",
                    pipeline.pipeline_id, used, allocated,
                )
                pipeline.stage = PipelineStage.BLOCKED
                pipeline.last_error = "Budget exhausted"
                self.state.save_pipeline(pipeline)
                self.state.append_log(
                    f"Pipeline {pipeline.pipeline_id} BLOCKED: budget exhausted",
                    pipeline.pipeline_id,
                )
                continue

            # If pipeline has active branches, schedule branch steps instead
            if pipeline.active_branch_count > 0 and "branches" in pipeline.metadata:
                schedule_tasks.append(self._schedule_branch_steps(pipeline))
            else:
                schedule_tasks.append(self._schedule_next(pipeline))

        # Dispatch all pipeline steps concurrently
        if schedule_tasks:
            await asyncio.gather(*schedule_tasks, return_exceptions=True)

        # 5. After all pipelines done, run Knowledge Miner to discover new topics
        if self.auto_professor:
            await self._maybe_run_knowledge_miner()

        # 6. Backlog management: sweep expired and re-evaluate candidates
        await self._process_backlog()

    # ── Backlog processing ──────────────────────────────────────

    async def _schedule_branch_steps(self, pipeline: PipelineState) -> None:
        """Schedule next steps for all active branches in parallel.

        When all branches reach CRITIC, dispatch branch_selector to pick
        the winner, then merge the winning branch back into the main
        pipeline.  Branch state lives in the BranchRunner journal --
        ``pipeline.metadata['branches']`` is no longer the source of
        truth.
        """
        runner = self._branch_runner(pipeline.pipeline_id)
        branches = runner.branch_nodes()
        if not branches:
            # Edge case: no seeded branches on disk (e.g. legacy metadata
            # lingering from a previous run).  Clear branch flags and
            # fall back to normal scheduling.
            pipeline.active_branch_count = 0
            pipeline.metadata.pop("branches", None)
            self.state.save_pipeline(pipeline)
            return

        # Check if all branches have completed CRITIC
        if runner.all_reached(PipelineStep.CRITIC):
            self.state.append_log(
                "All branches completed CRITIC, selecting winner via journal metrics",
                pipeline.pipeline_id,
            )
            # branch_selector agent was removed in 2026-04 refactor; the
            # journal-based selector (select_winner) is the single source
            # of truth. Pure Python, no LLM cost.
            picked = runner.select_winner()
            winner_id = picked.iter_index if picked else 0

            runner.archive(winner_id)
            pipeline.selected_branch = winner_id
            pipeline.active_branch_count = 0
            # Keep a minimal breadcrumb in metadata so external tools
            # that inspect pipeline state still see the branching
            # ran (full history lives in journal.json).
            pipeline.metadata["branches"] = [
                {
                    "branch_id": n.iter_index,
                    "status": (n.exec_result or {}).get("status", "completed"),
                    "step": (n.exec_result or {}).get("step"),
                }
                for n in runner.branch_nodes()
            ]
            # Resume normal pipeline after CRITIC of winning branch
            pipeline.step = PipelineStep.CRITIC
            self.state.save_pipeline(pipeline)
            self.state.append_log(
                f"Branch {winner_id} selected as winner, resuming pipeline",
                pipeline.pipeline_id,
            )
            return

        # Otherwise, dispatch next step for each active branch
        tasks = []
        for node in runner.active_branches():
            current_step_val = (node.exec_result or {}).get("step")
            if not current_step_val:
                continue
            try:
                current_step = PipelineStep(current_step_val)
            except ValueError:
                continue
            # Determine next step for this branch (same logic as main pipeline)
            next_step = (
                self._next_after_design(pipeline)
                if current_step == PipelineStep.DESIGN
                else None
            )
            if next_step is None:
                wf_key = (pipeline.stage.value, current_step.value)
                next_step_name = self._next_step_map.get(wf_key)
                if next_step_name:
                    try:
                        next_step = PipelineStep(next_step_name)
                    except ValueError:
                        next_step = None

            if next_step is not None:
                bdir = runner.branch_dir(node.iter_index)
                # Stop branches at CRITIC (don't go past it).
                if next_step in (
                    PipelineStep.INTERROGATION,
                    PipelineStep.REPORT,
                ):
                    runner.set_status(node.iter_index, "completed")
                    continue

                tasks.append(
                    self._dispatch_branch_step(
                        pipeline, runner, node.iter_index, next_step, bdir
                    )
                )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self.state.save_pipeline(pipeline)

    async def _dispatch_branch_step(
        self,
        pipeline: PipelineState,
        runner: "BranchRunner",
        branch_id: int,
        step: PipelineStep,
        branch_dir: Path,
    ) -> None:
        """Dispatch a single step for a single branch."""
        agent_name = self.workflow.get_step_agent(step.value)
        if not agent_name:
            raise RuntimeError(
                f"_dispatch_branch_step: no agent registered for step "
                f"{step.value!r}. workflow_config.yaml is missing an entry."
            )
        prompt = (
            f"Pipeline: {pipeline.pipeline_id} (Branch {branch_id})\n"
            f"Research question: {pipeline.research_question}\n"
            f"Step: {step.value}\n"
            f"IMPORTANT: Write all output artifacts to the branch directory: {branch_dir}\n"
            f"This is branch {branch_id} of a parallel experiment. "
            f"Other branches are exploring alternative approaches."
        )
        try:
            result = await self.dispatcher.dispatch_via_executor(
                agent_name, prompt, pipeline.pipeline_id
            )
            if result.get("success"):
                runner.advance(branch_id, step)
                logger.info(
                    "Branch %d step %s completed for %s",
                    branch_id, step.value, pipeline.pipeline_id,
                )
            else:
                runner.set_status(branch_id, "failed")
        except Exception as e:
            logger.error("Branch %d dispatch failed: %s", branch_id, e)
            runner.set_status(branch_id, "failed")

    async def _process_backlog(self) -> None:
        """Sweep expired backlog items and re-evaluate candidates."""
        config = self.evaluator.config

        # Sweep expired items to archive
        try:
            archived = self.backlog.sweep_expired(config)
            if archived:
                self.state.append_log(
                    f"Backlog sweep: archived {len(archived)} expired proposals"
                )
        except Exception:
            logger.exception("Backlog sweep failed")

        # Re-evaluate candidates that are ready
        try:
            candidates = self.backlog.get_re_eval_candidates(config)
            for backlog_path, entry in candidates:
                proposal_data = entry.get("proposal", {})
                title = proposal_data.get("title", "?")
                self.state.append_log(f"Re-evaluating backlog proposal: {title}")

                new_eval = await self.evaluator.evaluate(proposal_data)

                if new_eval.gate_result == GateResult.PASS:
                    # Promote to pipeline
                    promoted = self.backlog.promote_from_backlog(backlog_path)
                    if promoted:
                        import re as _re
                        pid = _re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:30]
                        proposal = PipelineDefinition(
                            pipeline_id=pid,
                            title=promoted.get("title", title),
                            research_question=promoted.get("research_question", ""),
                            budget_request=promoted.get("budget_request", 20000),
                            source="backlog_promotion",
                        )
                        runtime_dir = self.project_root / "runtime" / "pipelines"
                        runtime_dir.mkdir(parents=True, exist_ok=True)
                        out_path = runtime_dir / f"new_{pid}.json"
                        out_path.write_text(proposal.model_dump_json(indent=2))
                        self.state.append_log(
                            f"Backlog promoted → pipeline: {pid} ({title})"
                        )
                elif new_eval.gate_result == GateResult.REJECT:
                    # Permanently rejected after re-eval
                    self.backlog.archive_rejected(new_eval)
                    backlog_path.unlink(missing_ok=True)
                    self.state.append_log(f"Backlog re-eval rejected: {title}")
                else:
                    # Still in backlog, update eval
                    self.backlog.update_after_re_eval(backlog_path, new_eval)
                    self.state.append_log(
                        f"Backlog re-eval: {title} still pending (score={new_eval.scores.total})"
                    )
        except Exception:
            logger.exception("Backlog re-evaluation failed")

    # ── Decision handling ────────────────────────────────────────

    def _apply_decision(self, decision: ProfessorDecision) -> None:
        pipeline = self.state.load_pipeline(decision.pipeline_id)
        if pipeline is None:
            logger.warning("Decision for unknown pipeline: %s", decision.pipeline_id)
            self.state.mark_decision_processed(decision)
            return

        if decision.decision == DecisionType.APPROVE:
            if pipeline.stage == PipelineStage.PROPOSAL:
                pipeline.stage = PipelineStage.FAST_FAIL
                pipeline.step = None
                # Propagate falsification_design from evaluated yaml to pipeline dir
                self._propagate_falsification_design(pipeline)
            elif pipeline.stage in (PipelineStage.MILESTONE, PipelineStage.FAST_FAIL):
                if decision.next_milestone and decision.next_milestone.lower() == "paper":
                    pipeline.stage = PipelineStage.PAPER
                    pipeline.step = None
                else:
                    pipeline.stage = PipelineStage.MAIN
                    pipeline.step = None
            elif pipeline.stage == PipelineStage.BLOCKED:
                # Unblock: resume to MAIN (or PAPER if specified)
                if decision.next_milestone and decision.next_milestone.lower() == "paper":
                    pipeline.stage = PipelineStage.PAPER
                    pipeline.step = None
                else:
                    pipeline.stage = PipelineStage.MAIN
                    pipeline.step = None
            pipeline.last_error = None
            pipeline.retry_count = 0

        elif decision.decision == DecisionType.KILL:
            pipeline.stage = PipelineStage.COMPLETED
            pipeline.metadata["kill_reason"] = decision.comment
            # Trigger knowledge compilation for killed pipelines too
            self._trigger_knowledge_compilation(pipeline)
            # Persist findings card if critic already produced a verdict.
            self._maybe_save_findings(pipeline)

        elif decision.decision == DecisionType.PIVOT:
            pipeline.stage = PipelineStage.MAIN
            pipeline.step = None
            pipeline.metadata["pivot_comment"] = decision.comment

        elif decision.decision == DecisionType.PAUSE:
            pipeline.stage = PipelineStage.BLOCKED
            pipeline.last_error = f"Paused: {decision.comment}"

        elif decision.decision == DecisionType.HOLD:
            # Hold: keep current stage but block until professor re-evaluates
            pipeline.stage = PipelineStage.BLOCKED
            pipeline.last_error = f"On hold: {decision.comment}"
            pipeline.metadata["hold_reason"] = decision.comment

        elif decision.decision == DecisionType.TWEAK:
            # Tweak: write user directive and reset to DESIGN step for re-run
            import yaml as _tweak_yaml
            pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
            directives_path = pdir / "user_directives.yaml"
            existing = []
            if directives_path.exists():
                existing = _tweak_yaml.safe_load(directives_path.read_text()) or []
                if not isinstance(existing, list):
                    existing = [existing]
            if decision.comment:
                from datetime import datetime, timezone
                existing.append({
                    "directive": decision.comment,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "applied": False,
                })
                directives_path.write_text(
                    _tweak_yaml.dump(existing, default_flow_style=False)
                )

            # Remove stale artifacts so DESIGN re-runs from scratch
            for stale in ("experiment_plan.yaml",
                          "code_patch.diff", "preflight_report.json",
                          "results.json", "analysis_report.md",
                          "interrogation_report.md"):
                stale_path = pdir / stale
                if stale_path.exists():
                    stale_path.unlink()

            # Clean artifact registry for removed files
            for key in ("design", "code", "code_preflight",
                        "run", "critic", "interrogation"):
                pipeline.artifacts.pop(key, None)

            # Reset to re-run from DESIGN in the appropriate stage
            if pipeline.stage in (PipelineStage.MILESTONE, PipelineStage.BLOCKED):
                pipeline.stage = PipelineStage.MAIN
            pipeline.step = PipelineStep.HYPOTHESIS  # will advance to DESIGN on next tick
            pipeline.last_error = None
            pipeline.retry_count = 0

        # Apply budget allocation (explicit amount overrides adjustment)
        if decision.allocated_budget is not None:
            budget = self.state.load_budget()
            budget.allocations[pipeline.pipeline_id] = decision.allocated_budget
            self.state.save_budget(budget)

        # Apply budget adjustment
        if decision.budget_adjustment is not None:
            budget = self.state.load_budget()
            current = budget.allocations.get(pipeline.pipeline_id, 0)
            budget.allocations[pipeline.pipeline_id] = current + decision.budget_adjustment
            self.state.save_budget(budget)

        self.state.save_pipeline(pipeline)
        self.state.mark_decision_processed(decision)
        self.state.append_log(
            f"Decision applied: {decision.decision.value} for {decision.pipeline_id}",
            decision.pipeline_id,
        )

    def _load_gate_config(self) -> dict:
        """Load the ``gates:`` block from workflow_config.yaml."""
        import yaml as _yaml

        cfg_path = self.project_root / "runtime" / "workflow_config.yaml"
        try:
            if cfg_path.exists():
                raw = _yaml.safe_load(cfg_path.read_text()) or {}
                gates = raw.get("gates")
                if isinstance(gates, dict):
                    return gates
        except Exception:
            logger.debug("Failed to read gates config; using defaults", exc_info=True)
        return {}

    def _check_fast_fail_gate(
        self, pipeline: PipelineState
    ) -> tuple[bool, str]:
        """Check whether a FAST_FAIL pipeline should be auto-blocked.

        Returns ``(should_block, reason)``.  Blocks when the critic's
        ``analysis_report.md`` has verdict INCONCLUSIVE **and** every row
        in the Key Metrics table shows FAIL (no PASS or PARTIAL).
        """
        gates = self._load_gate_config()
        if not gates.get("fast_fail_auto_block", True):
            return False, ""

        pdir = (
            self.project_root
            / "artifacts"
            / f"pipeline_{pipeline.pipeline_id}"
        )
        report_path = pdir / "analysis_report.md"
        if not report_path.exists():
            return False, ""

        try:
            text = report_path.read_text(errors="replace")
        except Exception:
            logger.debug("Could not read analysis_report.md for auto-gate check")
            return False, ""

        verdict_inconclusive = bool(
            re.search(r"\*\*Verdict\*\*\s*:\s*INCONCLUSIVE", text, re.IGNORECASE)
        )
        has_iterate = bool(
            re.search(r"^##\s*Recommendation\s*\n+\s*ITERATE", text, re.MULTILINE)
        )

        if not (verdict_inconclusive and has_iterate):
            return False, ""

        km_match = re.search(
            r"###\s*Key Metrics\s*\n(.*?)(?=\n##|\Z)", text, re.DOTALL
        )
        if not km_match:
            return False, ""

        km_section = km_match.group(1)
        metric_rows = re.findall(r"\|\s*(FAIL|PASS|PARTIAL)\s*\|", km_section)
        if not metric_rows:
            return False, ""

        statuses = {s.upper() for s in metric_rows}
        all_fail = statuses == {"FAIL"}

        if not all_fail:
            return False, ""

        reason = (
            f"FAST_FAIL auto-gate: verdict is INCONCLUSIVE with ITERATE recommendation "
            f"and all {len(metric_rows)} key metrics are FAIL. "
            f"Pipeline blocked to prevent token waste in MAIN stage."
        )
        return True, reason

    async def _generate_pre_main_review(
        self, pipeline: PipelineState, pdir: Path
    ) -> None:
        """DEPRECATED — kept as a no-op stub.

        Pre-main HITL gate was removed in the 2026-04 refactor. The
        judge agent handles verdict after RUN; professor auto-decides
        stage transitions through decisions/*.yaml.

        Callers that still invoke this (legacy FAST_FAIL hooks) get a
        silent no-op — they will be removed alongside this stub once
        all references are migrated.
        """
        return

    def _simulate_pre_main_review(self, *_args, **_kwargs) -> None:
        """DEPRECATED no-op. See _generate_pre_main_review docstring."""
        return

    def _stage_user_approve(self, *_args, **_kwargs) -> None:
        """DEPRECATED no-op. user_approve/ HITL mechanism was removed in the
        2026-04 refactor. Professor auto-decisions now write directly to
        decisions/*.yaml without staging user-facing review artifacts.
        """
        return

    def _deprecated_stage_user_approve_impl(
        self, pipeline: PipelineState, pdir: Path
    ) -> None:
        """Archived original implementation — never called. Delete after
        Phase 1 when all call sites have been migrated."""
        import shutil

        if pipeline.metadata.get("source") != "user_inject":
            return

        ua_dir = pdir / "user_approve"
        ua_dir.mkdir(parents=True, exist_ok=True)
        papers_dir = ua_dir / "papers"
        papers_dir.mkdir(parents=True, exist_ok=True)

        # 1. Link reference papers from evidence_refs.  Prefer os.link
        # (hard link — O(1), zero extra space, safe to unlink without
        # touching the source).  Fall back to os.symlink when hard link
        # is not supported (cross-filesystem).  Copy is the last resort.
        import os as _os
        copied_papers: list[str] = []
        raw_dir = self.project_root / "knowledge" / "raw"
        for ref in pipeline.evidence_refs:
            src_candidates = [Path(ref), raw_dir / Path(ref).name]
            src = next((c for c in src_candidates if c.exists()), None)
            if src is None:
                continue
            dest = papers_dir / src.name
            if dest.exists() or dest.is_symlink():
                copied_papers.append(src.name)
                continue
            staged = False
            # Try hard link first.
            try:
                _os.link(src, dest)
                staged = True
            except (OSError, NotImplementedError):
                pass
            # Fall back to symlink.
            if not staged:
                try:
                    _os.symlink(src.resolve(), dest)
                    staged = True
                except (OSError, NotImplementedError):
                    pass
            # Last resort: actual copy.
            if not staged:
                try:
                    shutil.copy2(src, dest)
                    staged = True
                except Exception:
                    logger.warning("Failed to stage paper: %s", src)
            if staged:
                copied_papers.append(src.name)

        # 2. Write proposal snapshot
        proposal_md = ua_dir / "proposal.md"
        initial_hypothesis = pipeline.metadata.get("initial_hypothesis", "")
        proposal_md.write_text(
            f"# Proposal: {pipeline.title}\n\n"
            f"- Pipeline ID: {pipeline.pipeline_id}\n"
            f"- Source: {pipeline.metadata.get('source', 'unknown')}\n"
            f"- Budget allocated: {pipeline.budget_allocated}\n\n"
            f"## Research Question\n{pipeline.research_question}\n\n"
            f"## Initial Hypothesis\n{initial_hypothesis or '(none provided)'}\n\n"
            f"## Reference Papers\n"
            + ("\n".join(f"- {name}" for name in copied_papers) or "(none)")
            + "\n"
        )

        # 3. Combined FAST_FAIL report (analysis + pre-main review)
        analysis_path = pdir / "analysis_report.md"
        pre_main_path = pdir / "pre_main_review.md"
        report_sections: list[str] = [f"# FAST_FAIL Report — {pipeline.pipeline_id}\n"]
        if analysis_path.exists():
            report_sections.append("## Critic Analysis\n")
            report_sections.append(analysis_path.read_text())
        if pre_main_path.exists():
            report_sections.append("\n## Pre-MAIN Review\n")
            report_sections.append(pre_main_path.read_text())
        (ua_dir / "fast_fail_report.md").write_text("\n".join(report_sections))

        # 4. Professor suggestion — extract "Professor Assessment" + Recommendation
        suggestion = ""
        if pre_main_path.exists():
            body = pre_main_path.read_text()
            for heading in ("## 5. Professor Assessment", "## Recommendation"):
                if heading in body:
                    # Include everything from this heading forward
                    suggestion += body[body.find(heading):] + "\n"
                    break
            if not suggestion:
                suggestion = body
        (ua_dir / "professor_suggestion.md").write_text(
            f"# Professor Suggestion — {pipeline.pipeline_id}\n\n"
            + (suggestion or "(pre_main_review.md not yet generated)")
        )

        # 5. Feedback template — user edits this before running /research continue.
        #    The `continue` command detects the unchanged template marker and refuses
        #    to proceed until the user has written an actual opinion.
        feedback_path = ua_dir / "feedback.md"
        if not feedback_path.exists():
            feedback_path.write_text(
                "<!-- user_approve/feedback.md — edit this file, then run: -->\n"
                f"<!--   python scripts/run_research.py continue --pipeline {pipeline.pipeline_id} -->\n"
                "<!-- Delete these comment lines and replace with your directives. -->\n\n"
                "# User Feedback\n\n"
                "## What to change\n\n"
                "- \n\n"
                "## What to keep\n\n"
                "- \n\n"
                "## Additional notes\n\n"
            )

        # 6. README with instructions
        (ua_dir / "README.md").write_text(
            f"# user_approve/ — pipeline {pipeline.pipeline_id}\n\n"
            f"FAST_FAIL is complete for an inject-mode pipeline. "
            f"Review the files below, write your opinion in `feedback.md`, "
            f"then run the continue command.\n\n"
            f"## Files\n"
            f"- `papers/` — reference PDFs copied from knowledge/raw/\n"
            f"- `proposal.md` — research question + initial hypothesis\n"
            f"- `fast_fail_report.md` — critic analysis + pre-MAIN review\n"
            f"- `professor_suggestion.md` — Professor's fix recommendation\n"
            f"- `feedback.md` — **edit this** with your modification opinions\n\n"
            f"## Continue\n"
            f"```\n"
            f"python scripts/run_research.py continue --pipeline "
            f"{pipeline.pipeline_id}\n"
            f"```\n"
            f"The fixer agent will follow your directives exactly and, on "
            f"success, unblock the pipeline into the MAIN stage.\n"
        )

        # 7. Mark metadata so frontend / CLI can detect the block
        pipeline.metadata["user_approve_pending"] = True
        pipeline.metadata["user_approve_dir"] = str(
            ua_dir.relative_to(self.project_root)
        )
        self.state.save_pipeline(pipeline)
        self.state.append_log(
            f"user_approve staged at {ua_dir.relative_to(self.project_root)}",
            pipeline.pipeline_id,
        )

    def _simulate_research_brief(
        self, candidate: dict, pipeline_id: str, candidates_dir: Path
    ) -> None:
        """Write a basic research_brief from candidate metadata."""
        brief = (
            f"# Research Brief: {candidate.get('title', 'Untitled')}\n\n"
            f"## Background & Context\n"
            f"Research question: {candidate.get('research_question', 'N/A')}\n"
            f"Motivation: {candidate.get('motivation', 'N/A')}\n\n"
            f"## Core Innovation\n"
            f"Innovation type: {candidate.get('innovation_type', 'N/A')}\n"
            f"Hypothesis: {candidate.get('hypothesis', 'N/A')}\n\n"
            f"## Feasibility Assessment\n"
            f"| Dimension | Rating | Notes |\n"
            f"|-----------|--------|-------|\n"
            f"| Budget    | -      | {candidate.get('budget_request', 20000)} credits requested |\n\n"
            f"## Alignment with Research Directions\n"
            f"Direction: {candidate.get('direction', 'unspecified')}\n\n"
            f"## Evidence\n"
        )
        evidence = candidate.get("evidence", [])
        if evidence:
            for e in evidence:
                brief += f"- {e}\n"
        else:
            brief += "- No evidence refs provided\n"
        brief += (
            f"\n## Recommendation\n"
            f"Review via `candidates list -v` or run full briefing with "
            f"`candidates brief {pipeline_id}`.\n"
        )
        brief_path = candidates_dir / f"research_brief_{pipeline_id}.md"
        brief_path.write_text(brief)

    def _propagate_falsification_design(self, pipeline: PipelineState) -> None:
        """Copy falsification_design from evaluated_<id>.yaml to pipeline artifact dir.

        The exp_designer agent reads this file during FAST_FAIL to design
        experiments aligned with the proposal's falsification criteria.
        """
        import yaml as _yaml

        proposals_dir = self.project_root / "knowledge" / "proposals"
        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        pdir.mkdir(parents=True, exist_ok=True)

        # Search for matching evaluated file
        for eval_file in proposals_dir.glob("evaluated_*.yaml"):
            try:
                data = _yaml.safe_load(eval_file.read_text()) or {}
                if data.get("proposal_id") == pipeline.pipeline_id or (
                    data.get("title", "").lower() == pipeline.title.lower()
                ):
                    fals = data.get("falsification")
                    if fals:
                        dest = pdir / "falsification_design.yaml"
                        dest.write_text(
                            _yaml.dump(fals, default_flow_style=False, allow_unicode=True)
                        )
                        self.state.append_log(
                            "Propagated falsification_design.yaml from evaluation",
                            pipeline.pipeline_id,
                        )
                    break
            except Exception:
                logger.warning("Failed to read evaluated file: %s", eval_file)

    # ── Proposal handling ────────────────────────────────────────

    def _initialise_pipeline(self, proposal: PipelineDefinition) -> None:
        """Create pipeline state from proposal and queue for professor approval."""
        pipeline = PipelineState(
            pipeline_id=proposal.pipeline_id,
            title=proposal.title,
            research_question=proposal.research_question,
            stage=PipelineStage.PROPOSAL,
            budget_allocated=proposal.budget_request,
            evidence_refs=proposal.evidence_refs,
        )
        if proposal.hypothesis:
            pipeline.metadata["initial_hypothesis"] = proposal.hypothesis
        pipeline.metadata["source"] = proposal.source
        self.state.save_pipeline(pipeline)

        # Allocate budget
        budget = self.state.load_budget()
        budget.allocations[proposal.pipeline_id] = proposal.budget_request
        self.state.save_budget(budget)

        # Queue professor review
        state_file = str(
            self.project_root
            / "artifacts"
            / f"pipeline_{proposal.pipeline_id}"
            / "state.json"
        )
        self.state.write_decision_request(
            proposal.pipeline_id, [state_file]
        )

        self.state.archive_proposal(proposal)
        self.state.append_log(
            f"New pipeline created: {proposal.pipeline_id} ({proposal.title})",
            proposal.pipeline_id,
        )

    # ── Step scheduling ──────────────────────────────────────────

    def _next_after_design(self, pipeline: PipelineState) -> PipelineStep:
        """Decide what comes after DESIGN.

        MAIN: DESIGN → CODE_RECON (happens via _NEXT_STEP, not this helper).
        Caller hits this helper after CODE_RECON/CONCEPT_EXTRACT when it
        needs to branch on ``requires_code``. In FAST_FAIL: DESIGN → CODE or
        RUN directly (no code_recon / concept_extract chain).
        """
        import yaml as _yaml

        # Branch on requires_code flag
        pdir = (
            self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        )
        plan_file = pdir / "experiment_plan.yaml"
        if plan_file.exists():
            try:
                plan = _yaml.safe_load(plan_file.read_text()) or {}
                needs_code = plan.get("requires_code")
                if needs_code is not None:
                    if needs_code:
                        self.state.append_log(
                            "exp_designer decided: CODE step required",
                            pipeline.pipeline_id,
                        )
                        return PipelineStep.CODE
                    else:
                        self.state.append_log(
                            "exp_designer decided: skip CODE (no demo needed)",
                            pipeline.pipeline_id,
                        )
                        return PipelineStep.RUN
            except Exception:
                pass

        # Default: MAIN gets CODE, FAST_FAIL skips it
        if pipeline.stage == PipelineStage.MAIN:
            return PipelineStep.CODE
        return PipelineStep.RUN

    async def _schedule_next(self, pipeline: PipelineState) -> None:
        """Determine and dispatch the next atomic step."""

        # PROPOSAL stage: needs professor approval to proceed
        if pipeline.stage == PipelineStage.PROPOSAL:
            if self.simulate or self.auto_professor:
                # Auto-approve proposal in simulate/autonomous mode
                self.dispatcher._simulate_professor(pipeline.pipeline_id)
                self.state.append_log(
                    "Auto-approved proposal", pipeline.pipeline_id
                )
            return

        # Handle PAPER stage
        if pipeline.stage == PipelineStage.PAPER:
            await self._handle_paper_stage(pipeline)
            return

        # Handle KNOWLEDGE stage
        if pipeline.stage == PipelineStage.KNOWLEDGE:
            self._trigger_knowledge_compilation(pipeline)
            pipeline.stage = PipelineStage.COMPLETED
            self._maybe_save_findings(pipeline)
            self.state.save_pipeline(pipeline)
            self.state.append_log("Knowledge compilation done, pipeline COMPLETED", pipeline.pipeline_id)
            return

        # REPORT completed — transition to MILESTONE stage and dispatch
        # professor (autonomous decision flow; HITL gate removed in the
        # 2026-04 refactor).
        if pipeline.step == PipelineStep.REPORT and pipeline.stage in (
            PipelineStage.FAST_FAIL, PipelineStage.MAIN,
        ):
            was_fast_fail = pipeline.stage == PipelineStage.FAST_FAIL
            pipeline.stage = PipelineStage.MILESTONE
            pipeline.milestone_count += 1
            self.state.save_pipeline(pipeline)

            # Create checkpoint at each milestone
            try:
                self.checkpoints.create_checkpoint(
                    pipeline.pipeline_id,
                    label=f"M{pipeline.milestone_count}",
                )
                self.state.append_log(
                    f"Checkpoint created at M{pipeline.milestone_count}",
                    pipeline.pipeline_id,
                )
            except Exception:
                logger.exception("Checkpoint creation failed")

            pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"

            if was_fast_fail:
                # Auto-gate: block pipelines where all metrics FAIL and
                # verdict is INCONCLUSIVE to avoid wasting tokens in MAIN.
                should_block, block_reason = self._check_fast_fail_gate(pipeline)
                if should_block:
                    logger.warning(
                        "FAST_FAIL auto-gate triggered for %s: %s",
                        pipeline.pipeline_id, block_reason,
                    )
                    pipeline.last_error = block_reason
                    self.state.save_pipeline(pipeline)
                    self._write_default_decision(
                        pipeline.pipeline_id,
                        "pause",
                        comment=f"[AUTO-GATE] {block_reason}",
                    )
                    self.state.append_log(
                        f"FAST_FAIL auto-gate PAUSED pipeline: {block_reason}",
                        pipeline.pipeline_id,
                    )
                    return

                # After FAST_FAIL (HITL gate removed): auto-approve and
                # proceed to MAIN. Professor agent can still write a
                # kill/tweak decision asynchronously if needed.
                self._write_default_decision(
                    pipeline.pipeline_id, "approve",
                    comment="FAST_FAIL complete → auto-advance to MAIN (HITL gate removed).",
                )
                self.state.append_log(
                    "FAST_FAIL complete → auto-approved for MAIN",
                    pipeline.pipeline_id,
                )
            else:
                # After MAIN milestone: dispatch professor (autonomous).
                milestones = sorted(pdir.glob("milestone_*.md"))
                context = [str(m) for m in milestones[-3:]]
                state_file = str(pdir / "state.json")
                context.append(state_file)

                if self.auto_professor:
                    await self._auto_professor_decision(pipeline, context)
                else:
                    await self.dispatcher.dispatch_professor(
                        pipeline.pipeline_id, context
                    )
                self.state.append_log(
                    "Professor dispatched for milestone review",
                    pipeline.pipeline_id,
                )
            return

        # Determine next step using WorkflowConfig (falls back to legacy dict)
        stage_name = pipeline.stage.value
        step_name = pipeline.step.value if pipeline.step else None
        wf_key = (stage_name, step_name)

        # Dynamic routing: DESIGN uses _next_after_design
        if pipeline.step == PipelineStep.DESIGN and wf_key not in self._next_step_map:
            next_step = self._next_after_design(pipeline)
        else:
            next_step_name = self._next_step_map.get(wf_key)
            if next_step_name:
                try:
                    next_step = PipelineStep(next_step_name)
                except ValueError:
                    next_step = None
            else:
                next_step = None

        if next_step is None:
            logger.debug(
                "No next step for pipeline=%s stage=%s step=%s",
                pipeline.pipeline_id,
                pipeline.stage.value,
                pipeline.step,
            )
            return

        # ── Parallel branching support ──────────────────────────────
        # When branching is enabled and we just completed HYPOTHESIS in MAIN,
        # spawn branches that will independently explore DESIGN→...→CRITIC.
        # After all branches complete CRITIC, run BRANCH_SELECT to pick the best.
        if (
            self.branching_enabled
            and pipeline.stage == PipelineStage.MAIN
            and pipeline.step == PipelineStep.HYPOTHESIS
            and next_step == PipelineStep.DESIGN
            and pipeline.active_branch_count == 0
        ):
            runner = self._branch_runner(pipeline.pipeline_id)
            hypothesis_path = (
                self.project_root
                / "artifacts"
                / f"pipeline_{pipeline.pipeline_id}"
                / "hypothesis.md"
            )
            hyp_text = (
                hypothesis_path.read_text(errors="replace")
                if hypothesis_path.exists()
                else ""
            )
            nodes = runner.seed(hyp_text)
            pipeline.active_branch_count = len(nodes)
            pipeline.metadata["branches"] = [
                {
                    "branch_id": n.iter_index,
                    "status": (n.exec_result or {}).get("status", "active"),
                    "step": (n.exec_result or {}).get("step"),
                }
                for n in nodes
            ]
            self.state.save_pipeline(pipeline)
            self.state.append_log(
                f"Spawned {len(nodes)} parallel branches for exploration",
                pipeline.pipeline_id,
            )
            # Branches will be scheduled in subsequent ticks via _schedule_branch_steps
            return

        # Idempotent: check if output artifact already exists
        expected_output = self.workflow.get_step_output(next_step.value) or STEP_ARTIFACT.get(next_step)
        if expected_output:
            expected = (
                self.project_root
                / "artifacts"
                / f"pipeline_{pipeline.pipeline_id}"
                / expected_output
            )
            if expected.exists():
                logger.info(
                    "Artifact %s already exists, advancing", expected.name
                )
                pipeline.step = next_step
                self.state.save_pipeline(pipeline)
                return

        # ── Change 4B: inject baseline confidence brief before DESIGN ───────
        # Written as a sidecar file (design_baseline_brief.txt) so the
        # exp_designer agent knows whether to use the verified baseline data
        # files or fall back to a public benchmark.
        if next_step == PipelineStep.DESIGN:
            self._write_design_baseline_brief(pipeline)

        # ── Change 4A: inject run_mode before RUN ────────────────────────────
        # The experiment_runner agent reads artifacts/pipeline_<id>/run_mode.txt
        # to decide whether to run a fast-fail variant or the full MAIN run.
        # We write a sidecar file rather than passing through the dispatcher's
        # prompt so the approach works regardless of backend (OpenHarness /
        # Anthropic / simulate).
        if next_step == PipelineStep.RUN:
            try:
                run_mode = self.workflow.get_run_mode_for_milestone(
                    pipeline.milestone_count,
                    skip_fast_fail=self._read_skip_fast_fail(pipeline),
                )
            except Exception:
                run_mode = "main"  # safe default
            pipeline.metadata["run_mode"] = run_mode
            self.state.save_pipeline(pipeline)
            # Write sidecar file for experiment_runner agent
            pdir_run = (
                self.project_root
                / "artifacts"
                / f"pipeline_{pipeline.pipeline_id}"
            )
            pdir_run.mkdir(parents=True, exist_ok=True)
            try:
                (pdir_run / "run_mode.txt").write_text(run_mode)
            except Exception:
                logger.warning(
                    "Could not write run_mode.txt for pipeline %s",
                    pipeline.pipeline_id,
                )

        # Pre-flight: if RUN step and data.source is file/api, check data exists
        if next_step == PipelineStep.RUN:
            from researchos.runtime.hooks import validate_data_source_compliance
            compliance = validate_data_source_compliance(
                pipeline.pipeline_id, self.project_root
            )
            if not compliance["compliant"]:
                logger.warning(
                    "Data compliance check failed for %s: %s",
                    pipeline.pipeline_id, compliance["reason"],
                )
                self.state.append_log(
                    f"Data pre-flight: {compliance['reason']}", pipeline.pipeline_id
                )

        # ── Wave 2: code-only / specialised step handlers ────────────
        # These steps bypass the generic agent dispatch: they either run
        # pure-Python hooks (RESULTS_REVERIFY, BASELINE_CONFIRM) or stage
        # HITL artifacts (PRE_PUBLISH_GATE).
        specialised = await self._maybe_dispatch_specialised_step(pipeline, next_step)
        if specialised is not None:
            success = specialised
            if success:
                pipeline.step = next_step
                artifact_name = STEP_ARTIFACT.get(next_step)
                if artifact_name:
                    pipeline.artifacts[next_step.value] = artifact_name
                self.state.save_pipeline(pipeline)
                self._rebuild_lineage(pipeline)
                self.state.append_log(
                    f"Step completed (code): {next_step.value}",
                    pipeline.pipeline_id,
                )
            else:
                # Specialised step failed in a non-recoverable way.
                pipeline.retry_count += 1
                pipeline.last_error = f"Specialised step failed: {next_step.value}"
                self.state.save_pipeline(pipeline)
                self.state.append_log(
                    f"Specialised step failed: {next_step.value}",
                    pipeline.pipeline_id,
                )
            return

        # Dispatch the step
        success = await self.dispatcher.dispatch_step(pipeline, next_step)
        if success:
            pipeline.step = next_step
            # Record artifact in pipeline state
            artifact_name = self.workflow.get_step_output(next_step.value)
            if artifact_name:
                if next_step == PipelineStep.REPORT:
                    artifact_name = f"milestone_{pipeline.milestone_count + 1}.md"
                pipeline.artifacts[next_step.value] = artifact_name
            self.state.save_pipeline(pipeline)
            self._rebuild_lineage(pipeline)
            self.state.append_log(
                f"Step dispatched: {next_step.value}", pipeline.pipeline_id
            )
            # H3 + H5: post-CRITIC hooks — run VLM figure review and append
            # a Node snapshot to journal.json.  Both are best-effort, never
            # block pipeline progress.
            if next_step == PipelineStep.CRITIC:
                try:
                    await self._post_critic_vlm_review(pipeline)
                except Exception:
                    logger.exception("Post-CRITIC VLM review failed (non-fatal)")
                try:
                    self._journal_record_critic(pipeline)
                except Exception:
                    logger.exception("Journal record failed (non-fatal)")

                # ── Change 4C: route on critic flag ──────────────────────
                # After CRITIC completes, inspect the verdict and diverge:
                #   SOFT_FLAG  → dispatch BASELINE_DIAGNOSE (pure Python),
                #                then either fixer+re-run or escalate to L2.
                #   HARD_FLAG  → AWAITING_HUMAN_PIVOT (Change 4D).
                #   KILL_FLAG  → write failure wiki + kill pipeline.
                #   NONE       → normal flow continues (REPORT on next tick).
                try:
                    await self._handle_post_critic_routing(pipeline)
                except Exception:
                    logger.exception(
                        "Post-CRITIC routing failed (non-fatal); pipeline continues normally"
                    )
        else:
            pipeline.retry_count += 1
            error_msg = f"Failed to dispatch step {next_step.value}"
            if self.retry_policy.should_retry(pipeline.retry_count, error_msg):
                delay = self.retry_policy.delay_seconds(pipeline.retry_count)
                pipeline.last_error = f"{error_msg} (retry {pipeline.retry_count}, next in {delay:.0f}s)"
                self.state.append_log(
                    f"Retry {pipeline.retry_count}: {next_step.value} (delay {delay:.0f}s)",
                    pipeline.pipeline_id,
                )
            else:
                pipeline.stage = PipelineStage.FAILED
                pipeline.last_error = f"{error_msg} after {pipeline.retry_count} retries"
                self.state.append_log(
                    f"FAILED: {next_step.value} after {pipeline.retry_count} retries",
                    pipeline.pipeline_id,
                )
            self.state.save_pipeline(pipeline)

    # ── Change 4C: post-CRITIC routing ───────────────────────────────────────

    async def _handle_post_critic_routing(self, pipeline: "PipelineState") -> None:
        """Route pipeline after CRITIC based on the critic's flag.

        SOFT_FLAG  → run BASELINE_DIAGNOSE (pure Python); if auto_fixable and
                     fixer_directives non-empty, dispatch fixer agent and reset
                     to RUN.  If escalate_to_l2, treat as HARD_FLAG.
        HARD_FLAG  → transition to AWAITING_HUMAN_PIVOT (Change 4D).
        KILL_FLAG  → write failure wiki entry, then kill the pipeline.
        NONE/other → no-op; pipeline continues normally to REPORT on next tick.
        """
        verdict = self._read_critic_verdict(pipeline)
        flag = verdict.get("flag", "NONE")

        if flag == "NONE":
            return  # normal flow

        pid = pipeline.pipeline_id
        pdir = self.project_root / "artifacts" / f"pipeline_{pid}"

        if flag == "SOFT_FLAG":
            self.state.append_log(
                f"Critic SOFT_FLAG detected — running BASELINE_DIAGNOSE for {pid}", pid
            )
            pipeline.step = PipelineStep.BASELINE_DIAGNOSE
            self.state.save_pipeline(pipeline)

            diag = self._run_baseline_diagnose_step(pipeline)
            diag_verdict = diag.get("verdict", "escalate_to_l2")
            fixer_directives = diag.get("fixer_directives") or []

            if diag_verdict == "auto_fixable" and fixer_directives:
                # Dispatch fixer agent, then reset pipeline to re-run from RUN
                directives_text = "\n".join(f"- {d}" for d in fixer_directives)
                fixer_prompt = (
                    f"Pipeline: {pid}\n"
                    f"Baseline divergence detected. Apply the following fix directives "
                    f"to the experiment code/data configuration, then confirm.\n\n"
                    f"## Directives\n{directives_text}\n\n"
                    f"Read experiment_plan.yaml and code_patch.diff, apply the "
                    f"minimal changes needed, and re-write them. Do NOT re-run the "
                    f"experiment — the scheduler will dispatch RUN again.\n"
                )
                try:
                    result = await self.dispatcher.dispatch_via_executor(
                        "experiment_fixer", fixer_prompt, pid
                    )
                    if result.get("success"):
                        self.state.append_log(
                            f"Fixer applied directives for {pid}; resetting to RUN", pid
                        )
                    else:
                        self.state.append_log(
                            f"Fixer failed for {pid}: "
                            f"{(result.get('output') or '')[:200]}",
                            pid,
                        )
                except Exception:
                    logger.exception("Fixer dispatch failed for pipeline %s", pid)

                # Reset pipeline: delete stale RUN and CRITIC artifacts so they re-run
                for stale in ("results.json", "analysis_report.md",
                              "critic_verdict.json", "reverify_report.json",
                              "baseline_diagnose.json"):
                    p = pdir / stale
                    if p.exists():
                        p.unlink()
                pipeline.step = PipelineStep.CODE_PREFLIGHT  # re-enters at CODE_PREFLIGHT→RUN
                pipeline.last_error = None
                self.state.save_pipeline(pipeline)
                self.state.append_log(
                    f"Pipeline {pid} reset to CODE_PREFLIGHT after fixer", pid
                )

            elif diag_verdict == "escalate_to_l2" or (
                diag_verdict == "auto_fixable" and not fixer_directives
            ):
                # Escalate: treat as HARD_FLAG
                self.state.append_log(
                    f"baseline_diagnose escalated to L2 for {pid} — triggering pivot",
                    pid,
                )
                await self._trigger_awaiting_human_pivot(pipeline)

        elif flag == "HARD_FLAG":
            self.state.append_log(
                f"Critic HARD_FLAG detected — triggering pivot for {pid}", pid
            )
            await self._trigger_awaiting_human_pivot(pipeline)

        elif flag == "KILL_FLAG":
            self.state.append_log(
                f"Critic KILL_FLAG detected — killing pipeline {pid}", pid
            )
            self._write_kill_flag_wiki(pipeline)
            self._write_default_decision(
                pid, "kill",
                comment="[AUTO-KILL] Critic emitted KILL_FLAG (L3): "
                        "baseline reproduction irrecoverable.",
            )

    # ── Wave 2 specialised step handlers ─────────────────────────

    async def _maybe_dispatch_specialised_step(
        self, pipeline: PipelineState, step: PipelineStep
    ) -> bool | None:
        """Handle Wave 2 steps that aren't plain agent dispatches.

        Returns:
            True  — step ran successfully (or was a graceful no-op).
            False — step ran but failed in a recoverable way.
            None  — not a specialised step; caller should fall through to
                    the standard :meth:`Dispatcher.dispatch_step`.
        """
        pdir = (
            self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        )

        # ── Alignment workflow (2026-04) ────────────────────────────────
        # PAPER_SPEC / PROPOSAL_SPEC / ALIGNMENT are LLM-dispatched but need
        # a no-op SKIP path for legacy pipelines that have no typed
        # base_paper evidence. ALIGNMENT_VALIDATOR is pure Python.
        if step in (
            PipelineStep.PAPER_SPEC,
            PipelineStep.PROPOSAL_SPEC,
            PipelineStep.ALIGNMENT,
        ):
            if not self._has_alignment_inputs(pipeline, pdir):
                return self._skip_alignment_step(step, pipeline, pdir)
            # Otherwise fall through to standard dispatch (None return).
            return None

        if step == PipelineStep.ALIGNMENT_VALIDATOR:
            return self._run_alignment_validator(pipeline, pdir)

        # CODE_RECON — pure-Python shallow clone + static scan.
        if step == PipelineStep.CODE_RECON:
            return self._run_code_recon(pipeline, pdir)

        # BASELINE_ANALYZE — LLM, code-first analysis of cloned baseline package.
        if step == PipelineStep.BASELINE_ANALYZE:
            return await self._run_baseline_analyze(pipeline, pdir)

        # DATA_RECON — pure-Python executor of baseline_analysis.yaml.
        if step == PipelineStep.DATA_RECON:
            return self._run_data_recon(pipeline, pdir)

        # CONCEPT_EXTRACT — Stream C agent, but custom prompt so we keep
        # control over artifact placement.
        if step == PipelineStep.CONCEPT_EXTRACT:
            return await self._run_concept_extract(pipeline, pdir)

        # RESULTS_REVERIFY — code-only, delegated to Stream D.
        if step == PipelineStep.RESULTS_REVERIFY:
            return self._run_results_reverify(pipeline, pdir)

        # BASELINE_CONFIRM — code-only, delegated to Stream F.
        if step == PipelineStep.BASELINE_CONFIRM:
            return self._run_baseline_confirm(pipeline, pdir)

        # REPORT — deterministic milestone renderer (replaced reporter LLM).
        if step == PipelineStep.REPORT:
            return self._run_report(pipeline, pdir)

        return None

    def _run_report(
        self, pipeline: PipelineState, pdir: Path
    ) -> bool:
        """Render milestone_N.md from typed digests via pure Python.

        Supersedes the former reporter agent LLM call. Pulls hypothesis /
        plan / run / judge digests (each Pydantic-validated on load) and
        composes the markdown deterministically. Zero token cost.
        """
        from researchos.core.publisher import render_milestone
        try:
            path = render_milestone(
                pipeline_id=pipeline.pipeline_id,
                pdir=pdir,
                title=pipeline.title,
                research_question=pipeline.research_question,
                milestone_index=pipeline.milestone_count + 1,
                budget_allocated=pipeline.budget_allocated,
                budget_used=pipeline.budget_used,
            )
            logger.info(
                "REPORT: rendered %s (deterministic, no LLM)", path.name
            )
            return True
        except Exception:
            logger.exception(
                "REPORT: publisher failed for pipeline %s", pipeline.pipeline_id
            )
            return False

    # ── Alignment workflow helpers (2026-04) ────────────────────────────

    def _has_alignment_inputs(
        self, pipeline: PipelineState, pdir: Path
    ) -> bool:
        """Return True iff the pipeline qualifies for the alignment workflow.

        Two signals must both hold:
          * pipeline.metadata['source'] == 'user_inject' (typed inject CLI)
          * a TypedEvidenceRef with role='base_paper' is present
            (or, as a runtime fallback, base_paper.md exists in pdir).
        """
        if pipeline.metadata.get("source") != "user_inject":
            return False
        # Typed evidence_refs path
        for ref in pipeline.evidence_refs or []:
            role = getattr(ref, "role", None)
            if role is None and isinstance(ref, dict):
                role = ref.get("role")
            if role == "base_paper":
                return True
        # Fallback: base_paper.md staged on disk
        return (pdir / "base_paper.md").exists()

    def _skip_alignment_step(
        self,
        step: PipelineStep,
        pipeline: PipelineState,
        pdir: Path,
    ) -> bool:
        """Write a SKIP marker artifact so the scheduler can advance.

        Used for legacy pipelines that lack a base_paper. The pipeline still
        progresses through PAPER_SPEC / PROPOSAL_SPEC / ALIGNMENT, but each
        step writes a one-line skip notice rather than a real product.
        Downstream agents detect missing alignment_contract.yaml and fall
        back to legacy behaviour (kb_retrieve, etc.).
        """
        pdir.mkdir(parents=True, exist_ok=True)
        artifact = STEP_ARTIFACT.get(step)
        if artifact is None:
            return True
        target = pdir / artifact
        if not target.exists():
            content = (
                f"# {step.value} SKIPPED: pipeline has no typed base_paper "
                f"evidence (legacy mode). Downstream agents will use "
                f"kb_retrieve fallbacks.\n"
            )
            target.write_text(content, encoding="utf-8")
        self.state.append_log(
            f"{step.value} SKIPPED (legacy pipeline, no base_paper)",
            pipeline.pipeline_id,
        )
        return True

    def _run_alignment_validator(
        self, pipeline: PipelineState, pdir: Path
    ) -> bool:
        """Pure-Python validator for the alignment contract.

        Returns True on PASS or WARN (pipeline continues), False on BLOCK
        (scheduler treats as failure and halts the pipeline). For legacy
        pipelines where alignment was skipped, write a permissive PASS
        report and continue.
        """
        report_path = pdir / "alignment_validation_report.json"
        # Legacy short-circuit: contract absent → write trivial PASS.
        contract_path = pdir / "alignment_contract.yaml"
        if not contract_path.exists() or not self._has_alignment_inputs(pipeline, pdir):
            import json as _json
            pdir.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                _json.dumps(
                    {
                        "verdict": "PASS",
                        "summary": {"PASS": 1, "WARN": 0, "ERROR": 0},
                        "checks": [{
                            "check": "alignment_workflow.applicable",
                            "severity": "PASS",
                            "detail": "skipped: legacy pipeline (no contract)",
                        }],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            return True

        try:
            from researchos.runtime.alignment_validator import run_validator
        except ImportError:
            logger.warning(
                "alignment_validator module unavailable; treating as PASS"
            )
            return True

        try:
            report = run_validator(pdir)
        except Exception:
            logger.exception(
                "ALIGNMENT_VALIDATOR: run_validator crashed for pipeline %s",
                pipeline.pipeline_id,
            )
            return False

        self.state.append_log(
            f"ALIGNMENT_VALIDATOR verdict={report.verdict} "
            f"({report.summary})",
            pipeline.pipeline_id,
        )
        # PASS or WARN → pipeline continues; BLOCK → halt.
        return report.verdict != "BLOCK"

    def _run_code_recon(
        self, pipeline: PipelineState, pdir: Path
    ) -> bool:
        """Shallow-clone reference codebases and write code_recon.yaml.

        Pure Python, no LLM. Never raises: on any error we still produce
        a report so the pipeline can advance. The concept_extractor step
        reads this file to ground its reference_codebases locators.
        """
        report_path = pdir / "code_recon.yaml"
        if self.simulate or _run_code_recon_fn is None:
            pdir.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                "# code_recon simulate/stub — module unavailable or simulate mode\n"
                f"pipeline_id: {pipeline.pipeline_id}\n"
                "repos: []\n"
            )
            return True
        try:
            _run_code_recon_fn(pdir, self.project_root)
        except Exception:
            logger.exception(
                "code_recon failed for pipeline %s; writing stub report",
                pipeline.pipeline_id,
            )
            pdir.mkdir(parents=True, exist_ok=True)
            if not report_path.exists():
                report_path.write_text(
                    "# code_recon errored; pipeline advances without reference repos\n"
                    f"pipeline_id: {pipeline.pipeline_id}\n"
                    "repos: []\n"
                )
        return True

    def _rebuild_lineage(self, pipeline: PipelineState) -> None:
        """Best-effort post-step rebuild of artifacts/pipeline_<id>/lineage.json.

        Called after every successful step. Failure is logged and swallowed —
        the lineage view is derived (no source-of-truth role) and a stale
        lineage.json is strictly better than blocking pipeline progress.

        Skipped in simulate mode (the pipeline writes synthetic stubs that
        wouldn't produce a meaningful graph).
        """
        if self.simulate or _write_lineage_fn is None:
            return
        pdir = (
            self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        )
        if not pdir.exists():
            return
        try:
            _write_lineage_fn(pdir, self.project_root)
        except Exception:
            logger.exception(
                "lineage rebuild failed for pipeline %s (non-fatal)",
                pipeline.pipeline_id,
            )

    async def _run_baseline_analyze(
        self, pipeline: PipelineState, pdir: Path
    ) -> bool:
        """Dispatch baseline_analyzer LLM agent — writes baseline_analysis.yaml.

        The agent reads code_recon.yaml + cloned repos + paper_spec, then
        greps entry-point code for real IO calls (pd.read_csv, torch.load,
        wget, …) to identify which files / URLs the baseline actually uses.
        Output is consumed verbatim by DATA_RECON.

        On any failure (agent missing, validation failure, no repos), write
        a status=failed stub so DATA_RECON sees a known shape and writes
        an empty manifest — pipeline continues, downstream sees the gap.
        """
        analysis_path = pdir / "baseline_analysis.yaml"
        raw_sidecar_path = pdir / "baseline_analysis.agent_raw.yaml"
        from researchos.schemas.baseline_analysis import BaselineAnalysis, load_baseline_analysis

        def _write_failed_stub(reason: str) -> None:
            # Preserve the agent's actual output (if any) so the next debug
            # run can see what the LLM tried to write. Without this, the stub
            # below silently overwrites — losing all evidence of the failure
            # mode (wrong field names, partial yaml, hallucinated structure).
            if analysis_path.exists():
                try:
                    raw_sidecar_path.write_text(
                        analysis_path.read_text(errors="replace")
                    )
                    logger.info(
                        "baseline_analyze: preserved agent raw output to %s",
                        raw_sidecar_path.name,
                    )
                except OSError:
                    logger.warning(
                        "baseline_analyze: failed to preserve agent raw output",
                        exc_info=True,
                    )
            stub = BaselineAnalysis(
                pipeline_id=pipeline.pipeline_id,
                analysis_strategy="code_primary",
                status="failed",
                notes=(
                    f"baseline_analyzer fallback stub — {reason}. "
                    f"Agent raw output preserved at "
                    f"{raw_sidecar_path.name if raw_sidecar_path.exists() else 'N/A'}."
                ),
            )
            analysis_path.write_text(
                yaml.safe_dump(stub.model_dump(), sort_keys=False)
            )

        if self.simulate:
            pdir.mkdir(parents=True, exist_ok=True)
            stub = BaselineAnalysis(
                pipeline_id=pipeline.pipeline_id,
                analysis_strategy="code_primary",
                status="no_data_found",
                notes="simulate mode — no LLM call, no analysis performed",
            )
            analysis_path.write_text(
                yaml.safe_dump(stub.model_dump(), sort_keys=False)
            )
            return True

        # Inject code_recon.yaml summary into the prompt so the LLM doesn't
        # have to remember to file_read it.
        recon_path = pdir / "code_recon.yaml"
        recon_summary = "(code_recon.yaml not found — text_fallback strategy expected)"
        if recon_path.exists():
            try:
                recon = yaml.safe_load(recon_path.read_text()) or {}
                ok_repos = [
                    r for r in recon.get("repos", [])
                    if r.get("status") in ("ok", "cached")
                ]
                if not ok_repos:
                    recon_summary = (
                        "code_recon.yaml present but no repos with status ok/cached. "
                        "Use text_fallback strategy."
                    )
                else:
                    lines = [
                        f"  - slug={r['slug']}, status={r['status']}, "
                        f"path={r.get('path','?')}, "
                        f"entry_points={r.get('entry_points', [])}"
                        for r in ok_repos
                    ]
                    recon_summary = (
                        "Cloned repos available for analysis (paths relative to project root):\n"
                        + "\n".join(lines)
                    )
            except Exception:
                recon_summary = "code_recon.yaml exists but failed to parse"

        prompt = (
            f"Analyse the baseline package(s) for pipeline "
            f"{pipeline.pipeline_id} and write `baseline_analysis.yaml`.\n\n"
            f"## Pipeline\n"
            f"- Title: {pipeline.title}\n"
            f"- Research Question: {pipeline.research_question}\n\n"
            f"## CODE_RECON output\n"
            f"{recon_summary}\n\n"
            f"## Instructions\n"
            f"Follow the workflow in your agent definition (baseline_analyzer.md). "
            f"Use code-first strategy when repos are available; text_fallback otherwise. "
            f"Read upstream_sources.yaml in the pipeline directory if present. "
            f"Write `baseline_analysis.yaml` validating against "
            f"`researchos.schemas.baseline_analysis.BaselineAnalysis`.\n"
        )
        try:
            result = await self.dispatcher.dispatch_via_executor(
                "baseline_analyzer", prompt, pipeline.pipeline_id
            )
        except Exception:
            logger.exception("baseline_analyze dispatch failed")
            _write_failed_stub("dispatch raised exception")
            return True
        if not result.get("success"):
            logger.warning(
                "baseline_analyze failed: %s",
                (result.get("output") or "")[:200],
            )
            if not analysis_path.exists():
                _write_failed_stub("agent run failed and wrote no output")
            return True
        # Validate the agent's output against the schema. If invalid, replace
        # with a failed stub so DATA_RECON gets a known shape.
        analysis = load_baseline_analysis(analysis_path)
        if analysis is None:
            logger.warning(
                "baseline_analyze: output failed BaselineAnalysis schema validation"
            )
            _write_failed_stub("schema validation failed on agent output")
            return True
        logger.info(
            "baseline_analyze: pipeline=%s strategy=%s status=%s repos=%d ext_urls=%d",
            pipeline.pipeline_id, analysis.analysis_strategy, analysis.status,
            len(analysis.analyzed_repos), len(analysis.external_data_urls),
        )
        return True

    def _run_data_recon(
        self, pipeline: PipelineState, pdir: Path
    ) -> bool:
        """Discover dataset URLs (paper + code_recon hints) and download them.

        Pure Python, no LLM. Never raises: on any failure we write a stub
        manifest with ``data_recon_status: error`` so DESIGN can still run.
        """
        manifest_path = pdir / "data_manifest.yaml"
        if self.simulate or _run_data_recon_fn is None:
            pdir.mkdir(parents=True, exist_ok=True)
            if not manifest_path.exists():
                manifest_path.write_text(
                    "# data_recon simulate/stub — module unavailable or simulate mode\n"
                    f"pipeline_id: {pipeline.pipeline_id}\n"
                    "data_recon_status: skipped\n"
                    "discovered_urls: []\n"
                    "downloaded: []\n"
                )
            return True
        try:
            _run_data_recon_fn(pdir, self.project_root)
        except Exception:
            logger.exception(
                "data_recon failed for pipeline %s; writing stub manifest",
                pipeline.pipeline_id,
            )
            pdir.mkdir(parents=True, exist_ok=True)
            if not manifest_path.exists():
                manifest_path.write_text(
                    "# data_recon errored; pipeline advances without dataset manifest\n"
                    f"pipeline_id: {pipeline.pipeline_id}\n"
                    "data_recon_status: error\n"
                    "discovered_urls: []\n"
                    "downloaded: []\n"
                )
        return True

    async def _run_concept_extract(
        self, pipeline: PipelineState, pdir: Path
    ) -> bool:
        """Dispatch the concept_extractor agent (Stream C) — writes concepts.yaml."""
        if self.simulate:
            # Minimal simulate fallback so tests pass when Stream C is absent.
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "concepts.yaml").write_text(
                "# Auto-generated (simulate mode)\n"
                "concepts:\n"
                f"  - id: C1\n"
                f"    definition: \"Core concept for {pipeline.title}\"\n"
                f"    math_formula: \"\"\n"
                f"    code_implementation: \"\"\n"
            )
            return True

        prompt = (
            f"Extract atomic concepts from the research design for pipeline "
            f"{pipeline.pipeline_id}.\n\n"
            f"## Pipeline\n"
            f"- Title: {pipeline.title}\n"
            f"- Research Question: {pipeline.research_question}\n\n"
            f"## Instructions\n"
            f"1. Read hypothesis.md and experiment_plan.yaml via file_read.\n"
            f"2. Decompose the design into a flat list of atomic academic "
            f"concepts (ids C1, C2, ...).  Each concept gets a one-sentence "
            f"definition, optional LaTeX formula, and optional code locator.\n"
            f"3. Write concepts.yaml with a top-level ``concepts:`` list.\n"
        )
        try:
            result = await self.dispatcher.dispatch_via_executor(
                "concept_extractor", prompt, pipeline.pipeline_id
            )
        except Exception:
            logger.exception("concept_extract dispatch failed")
            return False
        if not result.get("success"):
            logger.warning(
                "concept_extract failed: %s",
                (result.get("output") or "")[:200],
            )
            # Tolerate missing agent — write a placeholder so pipeline advances.
            concepts_path = pdir / "concepts.yaml"
            if not concepts_path.exists():
                concepts_path.write_text(
                    "# concept_extractor unavailable; placeholder concepts.yaml\n"
                    "concepts: []\n"
                )
            return True
        # Guardrail: LLMs occasionally leak source-repo URLs into
        # ``code_implementation``, which violates the AtomicConcept schema and
        # would BLOCK CODE_PREFLIGHT downstream. Coerce in-place to
        # NOT_AVAILABLE so the pipeline keeps moving.
        from researchos.schemas.concept import sanitize_concepts_yaml
        sanitize_report = sanitize_concepts_yaml(pdir / "concepts.yaml")
        if sanitize_report["sanitized"]:
            logger.info(
                "concept_extract: sanitized %d invalid code_implementation "
                "entries in %s",
                len(sanitize_report["sanitized"]),
                pipeline.pipeline_id,
            )
        return True

    def _run_results_reverify(
        self, pipeline: PipelineState, pdir: Path
    ) -> bool:
        """Run the Stream-D reverification hook and write reverify_report.json.

        Opt-in: reverify is disabled by default.  Enable via
        ``research_config.yaml::reverify.enabled: true`` (or env var
        ``RESEARCHOS_REVERIFY=1``).  When disabled, writes a SKIP stub and
        advances without running the second subprocess.
        """
        import os as _os
        report_path = pdir / "reverify_report.json"
        # Check opt-in gate first.
        enabled = False
        env_flag = _os.environ.get("RESEARCHOS_REVERIFY", "").strip().lower()
        if env_flag in ("1", "true", "yes", "on"):
            enabled = True
        else:
            cfg_path = self.project_root / "runtime" / "research_config.yaml"
            if cfg_path.exists():
                try:
                    import yaml as _y
                    cfg = _y.safe_load(cfg_path.read_text()) or {}
                    enabled = bool((cfg.get("reverify") or {}).get("enabled", False))
                except Exception:
                    logger.debug("research_config.yaml reverify block unparsable")
        if not enabled:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                '{"verdict": "SKIP", '
                '"reason": "reverify disabled (opt-in via research_config.yaml::reverify.enabled)", '
                '"metrics_diff": {}}\n'
            )
            self.state.append_log(
                "RESULTS_REVERIFY skipped (opt-in; enable via research_config.yaml)",
                pipeline.pipeline_id,
            )
            return True
        if _reverify_results is None:
            # Stream D not available — write a SKIP stub so we advance.
            stub = (
                '{"verdict": "SKIP", '
                '"reason": "reverify module unavailable", '
                '"metrics_diff": {}}\n'
            )
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(stub)
            self.state.append_log(
                "RESULTS_REVERIFY skipped (reverify module unavailable)",
                pipeline.pipeline_id,
            )
            return True

        try:
            report = _reverify_results(pdir, self.project_root)
            verdict = (report or {}).get("verdict", "SKIP")
            self.state.append_log(
                f"RESULTS_REVERIFY verdict={verdict}", pipeline.pipeline_id
            )
        except Exception:
            logger.exception("reverify_results crashed")
            # Never block the pipeline on reverify internal bugs.
            import json as _json
            report_path.write_text(
                _json.dumps(
                    {"verdict": "SKIP", "reason": "reverify crashed"}, indent=2
                )
            )
        return True

    def _run_baseline_confirm(
        self, pipeline: PipelineState, pdir: Path
    ) -> bool:
        """Code-only baseline confirmation.  Writes metric_contract.json or
        skips gracefully if the plan has no baseline block or Stream F isn't
        ready."""
        import yaml as _yaml
        plan_path = pdir / "experiment_plan.yaml"
        contract_path = pdir / "metric_contract.json"

        if not plan_path.exists():
            # No plan yet — SKIP.
            contract_path.write_text(
                '{"status": "SKIP", "reason": "no experiment_plan.yaml"}\n'
            )
            return True

        try:
            plan = _yaml.safe_load(plan_path.read_text()) or {}
        except Exception:
            logger.warning("baseline_confirm: failed to parse experiment_plan.yaml")
            contract_path.write_text(
                '{"status": "SKIP", "reason": "unparsable experiment_plan.yaml"}\n'
            )
            return True

        baseline_block = (
            plan.get("baseline")
            or plan.get("baselines")
            or plan.get("plan_testing", {}).get("baselines")
            or plan.get("plan_testing", {}).get("baseline")
        )
        if not baseline_block:
            contract_path.write_text(
                '{"status": "SKIP", "reason": "no baseline block in experiment_plan.yaml"}\n'
            )
            self.state.append_log(
                "BASELINE_CONFIRM skipped (no baseline in plan)",
                pipeline.pipeline_id,
            )
            return True

        if _baseline_registry is None:
            contract_path.write_text(
                '{"status": "SKIP", "reason": "baseline_registry module unavailable"}\n'
            )
            self.state.append_log(
                "BASELINE_CONFIRM skipped (baseline_registry unavailable)",
                pipeline.pipeline_id,
            )
            return True

        try:
            confirm = getattr(_baseline_registry, "confirm_baseline", None)
            MetricContract = getattr(_baseline_registry, "MetricContract", None)
            if not callable(confirm) or MetricContract is None:
                contract_path.write_text(
                    '{"status": "SKIP", "reason": "confirm_baseline not implemented"}\n'
                )
                return True

            # Best-effort construction of a MetricContract from the plan's
            # baseline block.  If the plan is missing required fields we
            # record a SKIP so the pipeline keeps moving.
            block = baseline_block
            if isinstance(block, list) and block:
                # "baselines: [...]" shape — pick first entry as canonical.
                block = block[0] if isinstance(block[0], dict) else {}
            if not isinstance(block, dict):
                contract_path.write_text(
                    '{"status": "SKIP", "reason": "baseline block has unexpected shape"}\n'
                )
                return True

            try:
                contract = MetricContract(
                    task=block.get("task") or plan.get("task", "unspecified"),
                    dataset=block.get("dataset") or plan.get("dataset", "unspecified"),
                    eval_script=block.get("eval_script", "run_experiment.py"),
                    metrics=block.get("metrics", {}) or {},
                    baseline_values=block.get("baseline_values", {}) or {},
                    source_commit=block.get("source_commit", "HEAD"),
                    known_deviations=block.get("known_deviations", []) or [],
                )
            except Exception:
                logger.warning(
                    "baseline_confirm: plan baseline block did not validate",
                    exc_info=True,
                )
                contract_path.write_text(
                    '{"status": "SKIP", "reason": "baseline block validation failed"}\n'
                )
                return True

            confirm(
                contract=contract,
                pipeline_id=pipeline.pipeline_id,
                artifacts_root=self.project_root / "artifacts",
            )
            self.state.append_log(
                "BASELINE_CONFIRM completed", pipeline.pipeline_id
            )
        except Exception:
            logger.exception("baseline_confirm crashed")
            contract_path.write_text(
                '{"status": "SKIP", "reason": "confirm_baseline crashed"}\n'
            )
        return True

    # ── Change 4A helper ─────────────────────────────────────────────────────
    # Reads experiment_plan.yaml to determine whether the fast-fail prefix
    # should be skipped.  Called just before the RUN step is dispatched so the
    # run_mode sidecar file is always fresh.

    def _read_skip_fast_fail(self, pipeline: "PipelineState") -> bool:
        """Return True when experiment_plan.yaml sets skip_fast_fail: true."""
        plan_path = (
            self.project_root
            / "artifacts"
            / f"pipeline_{pipeline.pipeline_id}"
            / "experiment_plan.yaml"
        )
        if not plan_path.exists():
            return False
        try:
            import yaml as _yaml
            plan = _yaml.safe_load(plan_path.read_text()) or {}
            return bool(plan.get("skip_fast_fail", False))
        except Exception:
            return False

    # ── Change 4B helpers ────────────────────────────────────────────────────
    # Read baseline_analysis.yaml before the DESIGN step and write a sidecar
    # design_baseline_brief.txt so the exp_designer agent knows whether the
    # baseline confidence is high enough to use the analysed data files
    # directly or whether it must fall back to a public benchmark.

    def _baseline_confidence_threshold(self) -> float:
        """Read baseline_recon.confidence_threshold from research_config.yaml."""
        cfg_path = self.project_root / "runtime" / "research_config.yaml"
        if not cfg_path.exists():
            return 0.7
        try:
            import yaml as _yaml
            cfg = _yaml.safe_load(cfg_path.read_text()) or {}
            return float(cfg.get("baseline_recon", {}).get("confidence_threshold", 0.7))
        except Exception:
            return 0.7

    def _write_design_baseline_brief(self, pipeline: "PipelineState") -> None:
        """Write design_baseline_brief.txt sidecar before the DESIGN step.

        The exp_designer agent reads this file to decide whether to use the
        verified baseline data files (high-confidence path) or fall back to
        a public benchmark (low-confidence path).

        Called immediately before the DESIGN step is dispatched so the file
        is always present when the agent starts.
        """
        pdir = (
            self.project_root
            / "artifacts"
            / f"pipeline_{pipeline.pipeline_id}"
        )
        ba_path = pdir / "baseline_analysis.yaml"
        if not ba_path.exists():
            return
        try:
            import yaml as _yaml
            ba = _yaml.safe_load(ba_path.read_text()) or {}
        except Exception:
            return
        threshold = self._baseline_confidence_threshold()
        overall = float(
            (ba.get("confidence") or {}).get("overall", 0.0) or 0.0
        )
        if overall >= threshold:
            msg = (
                f"Baseline analysis confidence: {overall:.2f} (>= {threshold} threshold)\n"
                "→ You MUST use baseline_analysis.yaml.data.files / algorithm.code_locations.\n"
                "→ Do NOT write 'TBD' for dataset path; populate from baseline_analysis.\n"
            )
        else:
            msg = (
                f"Baseline analysis confidence: {overall:.2f} (< {threshold} threshold)\n"
                "→ baseline package unverified. Use the public benchmark fallback path.\n"
                "→ Document the fallback choice in experiment_plan.yaml.fallback_rationale.\n"
            )
        try:
            (pdir / "design_baseline_brief.txt").write_text(msg)
        except Exception:
            logger.warning(
                "Could not write design_baseline_brief.txt for pipeline %s",
                pipeline.pipeline_id,
            )

    # ── Change 4C helpers ────────────────────────────────────────────────────
    # After CRITIC/JUDGE completes, inspect the critic verdict and route:
    #   SOFT_FLAG  → BASELINE_DIAGNOSE (Python)
    #   HARD_FLAG  → AWAITING_HUMAN_PIVOT (Change 4D)
    #   KILL_FLAG  → write failure wiki entry, kill pipeline
    #   NONE       → normal flow (REPORT)

    def _read_critic_verdict(self, pipeline: "PipelineState") -> dict:
        """Return {'flag': 'NONE'|'SOFT_FLAG'|'HARD_FLAG'|'KILL_FLAG', ...}.

        Tries critic_verdict.json first, then falls back to scanning
        analysis_report.md or judge_report.md for the literal flag tokens.
        """
        pdir = (
            self.project_root
            / "artifacts"
            / f"pipeline_{pipeline.pipeline_id}"
        )
        json_path = pdir / "critic_verdict.json"
        if json_path.exists():
            try:
                import json as _json
                return _json.loads(json_path.read_text())
            except Exception:
                pass
        # Fallback: scan reports for literal flag tokens
        for fname in ("analysis_report.md", "judge_report.md"):
            path = pdir / fname
            if not path.exists():
                continue
            try:
                text = path.read_text(errors="ignore")
            except Exception:
                continue
            for token in ("KILL_FLAG", "HARD_FLAG", "SOFT_FLAG"):
                if token in text:
                    return {"flag": token}
        return {"flag": "NONE"}

    def _run_baseline_diagnose_step(self, pipeline: "PipelineState") -> dict:
        """Run baseline_diagnose.diagnose() and return the verdict dict.

        Writes baseline_diagnose.json to the pipeline directory.
        Falls back to a stub dict when the module is unavailable.
        """
        pdir = (
            self.project_root
            / "artifacts"
            / f"pipeline_{pipeline.pipeline_id}"
        )
        if _baseline_diagnose_fn is None:
            # Module not yet landed; write stub and return escalate so the
            # scheduler doesn't loop on an unresolvable soft flag.
            import json as _json
            stub = {
                "verdict": "escalate_to_l2",
                "fixer_directives": [],
                "note": "baseline_diagnose module unavailable",
            }
            try:
                (pdir / "baseline_diagnose.json").write_text(
                    _json.dumps(stub, indent=2)
                )
            except Exception:
                pass
            return stub
        try:
            return _baseline_diagnose_fn(pdir) or {}
        except Exception:
            logger.exception(
                "baseline_diagnose.diagnose() crashed for pipeline %s",
                pipeline.pipeline_id,
            )
            return {"verdict": "escalate_to_l2", "fixer_directives": []}

    def _write_kill_flag_wiki(self, pipeline: "PipelineState") -> None:
        """Write a failure wiki entry when critic emits KILL_FLAG."""
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        wiki_dir = self.project_root / "wiki" / "failures"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        out = wiki_dir / f"baseline_reproduction_{pipeline.pipeline_id}_{ts}.md"
        pdir = (
            self.project_root
            / "artifacts"
            / f"pipeline_{pipeline.pipeline_id}"
        )
        # Best-effort content assembly
        lines = [
            f"# Baseline Reproduction Failure — {pipeline.pipeline_id}",
            "",
            f"- **Pipeline**: {pipeline.pipeline_id}",
            f"- **Title**: {pipeline.title}",
            f"- **Milestone**: M{pipeline.milestone_count}",
            f"- **Timestamp**: {datetime.now(timezone.utc).isoformat()}",
            "",
            "## Critic Verdict",
            "KILL_FLAG",
            "",
        ]
        # Include a snippet from analysis_report.md if present
        report = pdir / "analysis_report.md"
        if report.exists():
            try:
                snippet = report.read_text(errors="ignore")[:2000]
                lines += ["## Analysis Report (excerpt)", "```", snippet, "```", ""]
            except Exception:
                pass
        # Include baseline_diagnose.json if present
        diag = pdir / "baseline_diagnose.json"
        if diag.exists():
            try:
                lines += [
                    "## Baseline Diagnose",
                    "```json",
                    diag.read_text(errors="ignore")[:1000],
                    "```",
                    "",
                ]
            except Exception:
                pass
        try:
            out.write_text("\n".join(lines))
            self.state.append_log(
                f"KILL_FLAG: failure wiki written to {out.relative_to(self.project_root)}",
                pipeline.pipeline_id,
            )
        except Exception:
            logger.warning("Could not write KILL_FLAG wiki for %s", pipeline.pipeline_id)

    # ── Change 4D: HARD_FLAG → AWAITING_HUMAN_PIVOT ─────────────────────────
    # Block the pipeline and dispatch the professor to draft a pivot request.
    # The tick() loop watches for decisions/pivot_response_<pid>_*.yaml and
    # resumes or kills the pipeline when one appears.

    async def _trigger_awaiting_human_pivot(
        self, pipeline: "PipelineState"
    ) -> None:
        """Transition pipeline to BLOCKED/AWAITING_HUMAN_PIVOT and draft pivot request.

        Called when critic emits HARD_FLAG or when baseline_diagnose escalates
        (escalate_to_l2 verdict).  The professor agent writes
        decisions/pivot_request_<pid>_<ts>.md with three options; a human then
        writes decisions/pivot_response_<pid>_<ts>.yaml to resume.
        """
        from datetime import datetime, timezone

        pid = pipeline.pipeline_id
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        pdir = self.project_root / "artifacts" / f"pipeline_{pid}"

        # Transition pipeline state
        pipeline.stage = PipelineStage.BLOCKED
        pipeline.step = PipelineStep.AWAITING_HUMAN_PIVOT
        self.state.save_pipeline(pipeline)
        self.state.append_log(
            f"Pipeline {pid} BLOCKED awaiting_human_pivot at M{pipeline.milestone_count}",
            pid,
        )

        # Gather context for the professor
        baseline_confidence = "unknown"
        ba_path = pdir / "baseline_analysis.yaml"
        if ba_path.exists():
            try:
                import yaml as _yaml
                ba = _yaml.safe_load(ba_path.read_text()) or {}
                baseline_confidence = str(
                    (ba.get("confidence") or {}).get("overall", "unknown")
                )
            except Exception:
                pass

        g10_divergences = "(see analysis_report.md)"
        report_path = pdir / "analysis_report.md"
        if report_path.exists():
            try:
                text = report_path.read_text(errors="ignore")
                # Extract a small snippet around G10 or baseline fidelity
                for marker in ("g10_baseline_fidelity", "G10", "baseline fidelity"):
                    idx = text.lower().find(marker.lower())
                    if idx >= 0:
                        g10_divergences = text[max(0, idx - 50): idx + 500].strip()
                        break
            except Exception:
                pass

        diag_summary = "(baseline_diagnose.json not present)"
        diag_path = pdir / "baseline_diagnose.json"
        if diag_path.exists():
            try:
                diag_summary = diag_path.read_text(errors="ignore")[:600]
            except Exception:
                pass

        pivot_prompt = (
            f"Mode: pivot_request_draft. "
            f"Write decisions/pivot_request_{pid}_{ts}.md following Pivot Protocol "
            f"from your agent definition.\n\n"
            f"## Pipeline\n"
            f"- Pipeline ID: {pid}\n"
            f"- Title: {pipeline.title}\n"
            f"- Milestone: M{pipeline.milestone_count}\n"
            f"- Baseline confidence: {baseline_confidence}\n\n"
            f"## G10 Divergences\n{g10_divergences}\n\n"
            f"## Baseline Diagnose Summary\n{diag_summary}\n\n"
            f"## Required sections in pivot_request_{pid}_{ts}.md\n"
            f"1. Baseline confidence and G10 divergences list\n"
            f"2. Baseline_diagnose findings\n"
            f"3. Three suggested directions:\n"
            f"   a) fix data/hyperparams + retry\n"
            f"   b) public benchmark fallback\n"
            f"   c) terminate + knowledge sediment\n"
            f"4. Your recommendation (one of the above)\n\n"
            f"Human will respond via decisions/pivot_response_{pid}_<ts>.yaml\n"
            f"with fields: decision (kill|pivot|continue_with_known_divergence), "
            f"direction, comment.\n"
        )
        try:
            result = await self.dispatcher.dispatch_via_executor(
                "professor", pivot_prompt, pid
            )
            if result.get("success"):
                self.state.append_log(
                    f"Professor pivot_request drafted for {pid}", pid
                )
            else:
                self.state.append_log(
                    f"Professor pivot_request draft failed for {pid}: "
                    f"{(result.get('output') or '')[:200]}",
                    pid,
                )
        except Exception:
            logger.exception("Professor pivot_request dispatch failed for %s", pid)

    def _check_pivot_response(self, pipeline: "PipelineState") -> None:
        """Check for a human pivot_response file and act on it.

        Called from tick() for each pipeline in BLOCKED / AWAITING_HUMAN_PIVOT.
        Reads the newest decisions/pivot_response_<pid>_*.yaml and applies:
          - decision == 'kill'                         → kill pipeline
          - decision == 'pivot'                        → restart from DESIGN in MAIN
          - decision == 'continue_with_known_divergence' → write approve decision
        Moves the pivot_request file to .processed.md to avoid double-processing.
        """
        from datetime import datetime, timezone

        pid = pipeline.pipeline_id
        decisions_dir = self.project_root / "decisions"
        pattern = f"pivot_response_{pid}_*.yaml"
        responses = sorted(decisions_dir.glob(pattern), key=lambda p: p.name)
        if not responses:
            return  # No response yet — leave pipeline blocked

        response_path = responses[-1]  # Newest
        try:
            import yaml as _yaml
            response = _yaml.safe_load(response_path.read_text()) or {}
        except Exception:
            logger.warning("Could not parse pivot_response: %s", response_path)
            return

        decision = response.get("decision", "")
        direction = response.get("direction", "")
        comment = response.get("comment", "")

        if decision == "kill":
            self._write_kill_flag_wiki(pipeline)
            self._write_default_decision(
                pid, "kill",
                comment=f"[PIVOT_RESPONSE kill] {comment}",
            )
            self.state.append_log(
                f"Pivot response: KILL for pipeline {pid}", pid
            )

        elif decision == "pivot":
            # Restart from DESIGN in MAIN; cache prefix steps reused naturally
            # via the idempotency check (STEP_ARTIFACT already exists for
            # steps before DESIGN).
            pdir = self.project_root / "artifacts" / f"pipeline_{pid}"
            for stale in (
                "experiment_plan.yaml",
                "code_patch.diff",
                "preflight_report.json",
                "results.json",
                "analysis_report.md",
                "reverify_report.json",
                "judge_verdict.json",
                "critic_verdict.json",
                "baseline_diagnose.json",
            ):
                stale_path = pdir / stale
                if stale_path.exists():
                    stale_path.unlink()
            pipeline.stage = PipelineStage.MAIN
            pipeline.step = PipelineStep.HYPOTHESIS  # advances to DESIGN next tick
            pipeline.last_error = None
            pipeline.retry_count = 0
            pipeline.metadata["pivot_direction"] = direction
            pipeline.metadata["pivot_comment"] = comment
            self.state.save_pipeline(pipeline)
            self.state.append_log(
                f"Pivot response: PIVOT → restarting from DESIGN for {pid} "
                f"(direction={direction})",
                pid,
            )

        elif decision == "continue_with_known_divergence":
            self._write_default_decision(
                pid, "approve",
                comment=f"[PIVOT_RESPONSE continue_with_known_divergence] {comment}",
            )
            self.state.append_log(
                f"Pivot response: CONTINUE with known divergence for {pid}", pid
            )

        else:
            logger.warning(
                "Unknown pivot_response decision %r for pipeline %s; ignoring",
                decision, pid,
            )
            return

        # Archive the pivot_request file so it isn't reprocessed
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        for req in decisions_dir.glob(f"pivot_request_{pid}_*.md"):
            try:
                req.rename(req.with_suffix(f".processed_{ts}.md"))
            except Exception:
                logger.debug("Could not rename pivot_request: %s", req)
        # Archive the response itself
        try:
            response_path.rename(
                response_path.with_suffix(f".processed_{ts}.yaml")
            )
        except Exception:
            pass

    def _maybe_save_findings(self, pipeline: PipelineState) -> None:
        """Persist a FindingCard when critic verdict is fully_correct.

        Gracefully no-ops when findings_memory is unavailable or when there
        is no advisor_verdict.json / verdict is not fully_correct.
        """
        if _findings_memory is None:
            return
        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"

        # critic.md emits advisor_verdict.yaml; earlier draft assumed .json.
        # Try YAML first (authoritative), then JSON (legacy/alternate).
        fully_correct = False
        verdict_payload: dict = {}
        for ext, loader in (("yaml", yaml.safe_load), ("json", None)):
            verdict_path = pdir / f"advisor_verdict.{ext}"
            if not verdict_path.exists():
                continue
            try:
                if loader is not None:
                    verdict_payload = loader(verdict_path.read_text()) or {}
                else:
                    import json as _json
                    verdict_payload = _json.loads(verdict_path.read_text()) or {}
                if isinstance(verdict_payload, dict):
                    fully_correct = bool(verdict_payload.get("fully_correct"))
                    break
            except Exception:
                logger.debug("advisor_verdict.%s unparsable", ext, exc_info=True)

        if not fully_correct:
            return

        try:
            ms = getattr(_findings_memory, "MemoryService", None)
            FindingCard = getattr(_findings_memory, "FindingCard", None)
            if ms is None or FindingCard is None:
                return
            service = ms(self.project_root)
            body_parts = [
                f"Pipeline {pipeline.pipeline_id} completed with a "
                f"fully-correct advisor verdict.",
            ]
            if verdict_payload.get("summary"):
                body_parts.append(str(verdict_payload.get("summary")))
            card = FindingCard(
                id=f"finding_{pipeline.pipeline_id}",
                kind="findings",
                title=pipeline.title,
                body="\n\n".join(body_parts),
                tags=list(pipeline.evidence_refs or []),
                source_pipeline=pipeline.pipeline_id,
            )
            service.save(card)
            self.state.append_log(
                "Findings memory: persisted FindingCard on COMPLETED",
                pipeline.pipeline_id,
            )
        except Exception:
            logger.exception("Findings memory save failed")

    async def _post_critic_vlm_review(self, pipeline: PipelineState) -> None:
        """H3: dispatch vlm_reviewer on figures produced by the pipeline.

        Runs after CRITIC.  Only fires when the pipeline directory contains
        image files (.png/.jpg/.jpeg/.pdf figures).  Writes ``vlm_review.json``
        alongside analysis_report.md.  Best-effort: failures logged, pipeline
        proceeds unchanged.
        """
        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
        if not pdir.exists():
            return
        fig_exts = {".png", ".jpg", ".jpeg"}
        figures = [p for p in pdir.rglob("*") if p.suffix.lower() in fig_exts]
        if not figures:
            return
        # In simulate mode, write a stub.
        if self.simulate:
            import json as _json
            stub = {
                "figures_reviewed": [f.name for f in figures],
                "simulate": True,
                "duplicate_candidates": [],
            }
            (pdir / "vlm_review.json").write_text(_json.dumps(stub, indent=2))
            self.state.append_log(
                f"VLM stub review written ({len(figures)} figures)",
                pipeline.pipeline_id,
            )
            return
        prompt = (
            f"Pipeline: {pipeline.pipeline_id}\n\n"
            f"Review the figures in the pipeline directory using the "
            f"img_review_prompt protocol from your agent definition. "
            f"Emit a single JSON object with keys: "
            f"`per_figure: {{filename: review_dict}}`, "
            f"`duplicate_candidates: [...]`.\n\n"
            f"Figures to review:\n"
            + "\n".join(f"- {f.relative_to(pdir)}" for f in figures)
            + "\n\nWrite the final JSON to `vlm_review.json`.\n"
        )
        try:
            result = await self.dispatcher.dispatch_via_executor(
                "vlm_reviewer", prompt, pipeline.pipeline_id
            )
            if not result.get("success"):
                logger.warning(
                    "vlm_reviewer returned failure for %s", pipeline.pipeline_id
                )
                return
            self.state.append_log(
                f"VLM review dispatched ({len(figures)} figures)",
                pipeline.pipeline_id,
            )
        except Exception:
            logger.exception("vlm_reviewer dispatch failed")

    def _journal_record_critic(self, pipeline: PipelineState) -> None:
        """H5: append a Node snapshot to journal.json after CRITIC (MAIN only).

        The new tree-search primitives in branch_manager.py are opt-in via
        ``research_config.yaml::branching.enabled``.  Even when disabled we
        still persist each CRITIC result as a Journal Node so future runs
        with branching turned on inherit the history.
        """
        if pipeline.stage != PipelineStage.MAIN:
            return
        try:
            from researchos.runtime.branch_manager import (
                Node, Journal, JournalPersistence,
            )
        except Exception:
            logger.debug("branch_manager new-style primitives unavailable")
            return

        artifacts_root = self.project_root / "artifacts"
        pdir = artifacts_root / f"pipeline_{pipeline.pipeline_id}"

        # Load existing journal or start fresh.
        try:
            journal = JournalPersistence.load(pipeline.pipeline_id, artifacts_root)
        except Exception:
            journal = Journal()

        # Build a Node from the critic artifact.
        import json as _json
        metric: dict = {}
        results_path = pdir / "results.json"
        if results_path.exists():
            try:
                metric = _json.loads(results_path.read_text()).get("metrics", {}) or {}
            except Exception:
                metric = {}
        analysis_path = pdir / "analysis_report.md"
        analysis_text = ""
        if analysis_path.exists():
            analysis_text = analysis_path.read_text(errors="replace")[:4000]

        # Is this result buggy?  Inherit critic's verdict if surfaced.
        verdict_lower = analysis_text.lower()
        is_buggy = (
            "refuted" in verdict_lower
            or "failed to support" in verdict_lower
            or "crash" in verdict_lower
        )

        from datetime import datetime, timezone
        node_id = f"m{pipeline.milestone_count}_{datetime.now(timezone.utc).strftime('%H%M%S')}"
        # Parent = last non-buggy node if any, else last node.
        parent_id: str | None = None
        if journal.nodes:
            best = journal.get_best_node()
            parent_id = best.id if best else journal.nodes[-1].id

        node = Node(
            id=node_id,
            parent_id=parent_id,
            stage="improve" if parent_id else "draft",
            plan="",  # filled from experiment_plan.yaml if present
            code="",  # pointer only; actual code lives in pipeline dir
            exec_result={},
            plot_analyses={},
            metric=metric,
            is_buggy=is_buggy,
            is_buggy_plots=False,
            is_seed_node=False,
            is_seed_agg_node=False,
            ablation_name=None,
            hyperparam_name=None,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        journal.nodes.append(node)
        try:
            JournalPersistence.save(pipeline.pipeline_id, journal, artifacts_root)
            self.state.append_log(
                f"Journal node appended: {node_id} "
                f"(buggy={is_buggy}, nodes={len(journal.nodes)})",
                pipeline.pipeline_id,
            )
        except Exception:
            logger.exception("Journal save failed")

    def _pre_publish_gate_needed(self, pipeline_id: str) -> bool:
        """DEPRECATED — always returns False. Pre-publish HITL gate was
        removed in the 2026-04 refactor; papers now auto-publish after
        paper_auditor passes.
        """
        return False

    def _run_pre_publish_gate(self, *_args, **_kwargs) -> bool:
        """DEPRECATED no-op. See _pre_publish_gate_needed."""
        return True

    # ── Knowledge Miner & Proposal intake ─────────────────────────

    async def _maybe_run_knowledge_miner(self) -> None:
        """Run Knowledge Miner if there are no active pipelines and no pending proposals."""
        # Check miner pause flag
        if (self.project_root / "runtime" / ".miner_paused").exists():
            return

        # single_run mode: miner runs at most once. Set flag eagerly to
        # prevent a second run on the next tick before the first proposal
        # is consumed.
        if self.single_run:
            if self._miner_has_run:
                return
            self._miner_has_run = True  # set BEFORE dispatch, not after

        import re

        # single_run mode: skip the "no active pipelines" guard — we need
        # the miner to run once regardless of existing pipeline state
        if not self.single_run:
            pipelines = self.state.load_all_pipelines()
            active = [p for p in pipelines if p.stage not in (
                PipelineStage.COMPLETED, PipelineStage.FAILED, PipelineStage.BLOCKED
            )]
            if active:
                return  # Still have work to do

        proposals_dir = self.project_root / "knowledge" / "proposals"
        existing_proposals = list(proposals_dir.glob("new_idea_*.yaml")) if proposals_dir.exists() else []
        if existing_proposals:
            return  # Already have proposals waiting

        # Check there's enough wiki content to mine
        wiki_dir = self.project_root / "knowledge" / "wiki"
        wiki_pages = list(wiki_dir.rglob("*.md")) if wiki_dir.exists() else []
        if len(wiki_pages) < 1:
            return  # Not enough knowledge to mine

        self.state.append_log("Running Knowledge Miner to discover new research directions")

        # Load heartbeat config to inform miner about research directions
        directions_info = ""
        hb_config_path = self.project_root / "runtime" / "heartbeat_config.yaml"
        if hb_config_path.exists():
            try:
                import yaml as _yaml
                hb_config = _yaml.safe_load(hb_config_path.read_text()) or {}
                prof = hb_config.get("professor", {})
                dirs = hb_config.get("directions", [])
                if prof or dirs:
                    directions_info = (
                        f"\n## Research Domain Context\n"
                        f"- Domain: {prof.get('domain_description', 'General CS')}\n"
                        f"- Tracked directions:\n"
                    )
                    for d in dirs:
                        directions_info += (
                            f"  - **{d.get('name', '?')}** (priority: {d.get('priority', '?')}): "
                            f"{', '.join(d.get('keywords', []))}\n"
                        )
                    directions_info += (
                        f"\nWhen proposing research, indicate which direction it belongs to "
                        f"by including a `direction` field in the YAML.\n"
                    )
            except Exception:
                pass

        prompt = (
            "Survey the knowledge base to find research gaps and propose new directions.\n\n"
            "## Instructions\n"
            "1. Use `kb_retrieve` extensively with varied queries to survey the landscape\n"
            "2. Identify gaps, contradictions, open problems, or unexplored angles\n"
            "3. Write exactly ONE research proposal as a YAML file\n"
            "4. Use file_write to save it to: knowledge/proposals/new_idea_001.yaml\n\n"
            f"{directions_info}"
            "## Proposal Format (YAML)\n"
            "```yaml\n"
            "title: \"<concise title>\"\n"
            "research_question: \"<specific, answerable question>\"\n"
            "hypothesis: \"<initial hypothesis>\"\n"
            "direction: \"<which research direction this belongs to>\"\n"
            "evidence:\n"
            "  - \"<wiki page supporting this idea>\"\n"
            "motivation: \"<why this is worth investigating>\"\n"
            "estimated_effort: \"low\"\n"
            "priority: \"high\"\n"
            "budget_request: 20000\n"
            "```\n\n"
            "IMPORTANT: Write the file path as: knowledge/proposals/new_idea_001.yaml\n"
        )

        try:
            # Use a virtual pipeline dir for the miner — it writes to knowledge/proposals/
            miner_dir = self.project_root / "scratch" / "knowledge_miner"
            miner_dir.mkdir(parents=True, exist_ok=True)
            result = await self.dispatcher.dispatch_via_executor(
                "knowledge_miner", prompt, "_system_miner"
            )
            if result["success"]:
                self.state.append_log(f"Knowledge Miner completed: {result['artifacts_written']}")
            else:
                self.state.append_log(f"Knowledge Miner failed: {result.get('output', '')[:200]}")
        except Exception:
            logger.exception("Knowledge Miner error")

    @staticmethod
    def _archive_proposal(f: Path) -> None:
        """Move a processed proposal to archived/ subdirectory."""
        archived_dir = f.parent / "archived"
        archived_dir.mkdir(exist_ok=True)
        dest = archived_dir / f.name
        if dest.exists():
            f.unlink()
        else:
            f.rename(dest)

    def _add_candidate(self, proposal_data: dict, pipeline_id: str) -> None:
        """Add an evaluated proposal to the candidates list for user selection."""
        import json as _json
        from datetime import datetime, timezone

        candidates_dir = self.project_root / "runtime" / "candidates"
        candidates_dir.mkdir(parents=True, exist_ok=True)

        candidate = {
            "pipeline_id": pipeline_id,
            "title": proposal_data.get("title", "Untitled"),
            "research_question": proposal_data.get("research_question", ""),
            "hypothesis": proposal_data.get("hypothesis", ""),
            "motivation": proposal_data.get("motivation", ""),
            "evidence": proposal_data.get("evidence", []),
            "direction": proposal_data.get("direction", ""),
            "innovation_type": proposal_data.get("innovation_type", ""),
            "budget_request": proposal_data.get("budget_request", 20000),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",  # pending / selected / rejected
        }
        out = candidates_dir / f"candidate_{pipeline_id}.json"
        out.write_text(_json.dumps(candidate, indent=2, ensure_ascii=False))

        # Generate research brief deterministically via briefer.render_brief
        # when the topic_evaluator's evaluated_<id>.yaml exists (richer output
        # with 5D scores + innovation claim + falsification). Fall back to
        # the legacy template generator when the evaluator artifact is absent.
        self._emit_research_brief(candidate, pipeline_id, candidates_dir)

    def _emit_research_brief(
        self, candidate: dict, pipeline_id: str, candidates_dir: Path
    ) -> None:
        """Prefer briefer.render_brief over the legacy template."""
        evaluated_path = (
            self.project_root / "knowledge" / "proposals"
            / f"evaluated_{pipeline_id}.yaml"
        )
        brief_path = candidates_dir / f"research_brief_{pipeline_id}.md"
        if evaluated_path.exists():
            try:
                from researchos.runtime.briefer import render_brief
                # Best-effort: pull 2–3 KB snippets from wiki via kb_retrieve
                # (text-only; we don't need perfect retrieval here).
                snippets: list[str] = []
                try:
                    from researchos.mcp_servers.kb_server import search_wiki
                    query = (
                        candidate.get("title", "")
                        + " "
                        + candidate.get("research_question", "")
                    ).strip()
                    if query:
                        raw = search_wiki(query, max_results=3) or ""
                        # search_wiki returns concatenated text; chunk it.
                        snippets = [s.strip() for s in raw.split("\n---\n") if s.strip()][:3]
                except Exception:
                    logger.debug("kb_retrieve failed for briefer", exc_info=True)
                brief_text = render_brief(evaluated_path, snippets)
                brief_path.write_text(brief_text)
                self.state.append_log(
                    f"Research brief rendered via briefer.render_brief "
                    f"({brief_path.name})",
                    pipeline_id,
                )
                return
            except Exception:
                logger.exception(
                    "briefer.render_brief failed; falling back to legacy template"
                )
        # Legacy template fallback.
        self._simulate_research_brief(candidate, pipeline_id, candidates_dir)

    async def _intake_knowledge_proposals(self) -> None:
        """Evaluate miner proposals via TopicEvaluator + Professor gating.

        Flow: proposal.yaml → TopicEvaluator (8-step) → gate result:
          - PASS → create pipeline
          - PROFESSOR_REVIEW → Professor LLM decides
          - BACKLOG → add to backlog for periodic re-evaluation
          - REJECT → archive permanently + record to wiki/failures
        """
        import re
        proposals_dir = self.project_root / "knowledge" / "proposals"
        if not proposals_dir.exists():
            return

        import yaml as _yaml
        for f in sorted(proposals_dir.glob("new_idea_*.yaml")):
            if ".processed" in f.name:
                continue
            try:
                data = _yaml.safe_load(f.read_text())
                if not data or not isinstance(data, dict):
                    continue

                title = data.get("title", "Untitled")
                question = data.get("research_question", data.get("hypothesis", title))
                budget_req = data.get("budget_request", 20000)
                pipeline_id = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:30]

                # Check if pipeline already exists
                pdir = self.project_root / "artifacts" / f"pipeline_{pipeline_id}"
                if pdir.exists():
                    self._archive_proposal(f)
                    continue

                if self.simulate:
                    # Simulate mode: skip evaluation, auto-approve
                    approved = True
                else:
                    # Run 8-step Topic Evaluator
                    evaluation = await self.evaluator.evaluate(data)

                    if evaluation.gate_result == GateResult.PASS:
                        approved = True
                    elif evaluation.gate_result == GateResult.PROFESSOR_REVIEW:
                        # Professor must review — use LLM evaluation
                        approved = await self._professor_evaluate_proposal(data)
                        rd = evaluation.repeat_degree.value if hasattr(evaluation.repeat_degree, 'value') else str(evaluation.repeat_degree)
                        self.state.append_log(
                            f"Professor reviewed (repeat_degree={rd}): "
                            f"{'approved' if approved else 'rejected'} — {title}"
                        )
                    elif evaluation.gate_result == GateResult.BACKLOG:
                        self.backlog.add_to_backlog(data, evaluation)
                        self.state.append_log(
                            f"Proposal → backlog (score={evaluation.scores.total}): {title}"
                        )
                        self._archive_proposal(f)
                        continue
                    else:
                        # REJECT
                        self.backlog.archive_rejected(evaluation)
                        self.state.append_log(
                            f"Proposal rejected ({', '.join(evaluation.gate_reasons)}): {title}"
                        )
                        self._archive_proposal(f)
                        continue

                if approved:
                    if self.auto_select or self.simulate:
                        # Auto mode: create pipeline directly
                        proposal = PipelineDefinition(
                            pipeline_id=pipeline_id,
                            title=title,
                            research_question=question,
                            budget_request=budget_req,
                            source="knowledge_miner",
                        )
                        runtime_dir = self.project_root / "runtime" / "pipelines"
                        runtime_dir.mkdir(parents=True, exist_ok=True)
                        out_path = runtime_dir / f"new_{pipeline_id}.json"
                        out_path.write_text(proposal.model_dump_json(indent=2))
                        self.state.append_log(
                            f"Proposal approved → pipeline: {pipeline_id} ({title})"
                        )
                    else:
                        # User-driven mode: write to candidates for user selection
                        self._add_candidate(data, pipeline_id)
                        self.state.append_log(
                            f"Proposal → candidates (awaiting user selection): {title}"
                        )
                else:
                    self.state.append_log(f"Professor rejected proposal: {title}")

                self._archive_proposal(f)

            except Exception:
                logger.exception("Failed to process proposal: %s", f)

    async def _professor_evaluate_proposal(self, proposal_data: dict) -> bool:
        """Have the Professor LLM evaluate a miner proposal. Returns True if approved."""
        title = proposal_data.get("title", "Untitled")
        question = proposal_data.get("research_question", "")
        hypothesis = proposal_data.get("hypothesis", "")
        motivation = proposal_data.get("motivation", "")
        evidence = proposal_data.get("evidence", [])

        prompt = (
            f"You are the Professor evaluating a research proposal from the Knowledge Miner.\n\n"
            f"## Proposal\n"
            f"- **Title**: {title}\n"
            f"- **Research Question**: {question}\n"
            f"- **Hypothesis**: {hypothesis}\n"
            f"- **Motivation**: {motivation}\n"
            f"- **Evidence**: {evidence}\n\n"
            f"## Your Task\n"
            f"1. Use `kb_retrieve` to check if this research direction has merit\n"
            f"2. Evaluate: Is the question specific and answerable? Is there enough "
            f"evidence to justify the effort?\n"
            f"3. Write your decision using file_write:\n"
            f"   - path: decisions/proposal_evaluation.yaml\n"
            f"   - content: a YAML with `decision: approve` or `decision: kill` and `comment: <reason>`\n"
        )

        try:
            self.state.append_log(f"Professor evaluating proposal: {title}")
            result = await self.dispatcher.dispatch_via_executor(
                "professor", prompt, "_system_professor"
            )
        except Exception:
            logger.exception("Professor proposal evaluation failed")
            return True  # Fallback: approve on error

        # Check if professor wrote a decision file
        eval_path = self.project_root / "decisions" / "proposal_evaluation.yaml"
        if eval_path.exists():
            import yaml as _yaml
            try:
                decision = _yaml.safe_load(eval_path.read_text())
                eval_path.unlink()  # Clean up
                return decision.get("decision", "approve") == "approve"
            except Exception:
                eval_path.unlink()
        return True  # Default approve if no decision file

    # ── Paper & Knowledge stages ─────────────────────────────────

    async def _handle_paper_stage(self, pipeline: PipelineState) -> None:
        """Handle paper writing, review, LaTeX compilation.

        Each pipeline gets its own subdirectory: paper/<pipeline_id>/
        Output sequence: draft.md → review.md → paper.tex → paper.pdf
        """
        pid = pipeline.pipeline_id
        paper_dir = self.project_root / "paper" / pid
        paper_dir.mkdir(parents=True, exist_ok=True)

        draft_path = paper_dir / "draft_v1.md"
        review_path = paper_dir / "review_v1.md"
        tex_path = paper_dir / "paper.tex"
        pdf_path = paper_dir / "paper.pdf"

        audit_path = paper_dir / "audit_report.md"

        if not draft_path.exists():
            if self.simulate:
                self._simulate_paper_writer(pipeline)
            else:
                prompt = self._build_paper_writer_prompt(pipeline)
                result = await self.dispatcher.dispatch_via_executor(
                    "paper_writer", prompt, pid
                )
                if not result["success"]:
                    logger.error("Paper writer failed: %s", result.get("output", ""))
            self.state.append_log("Paper draft generated", pid)

        elif not audit_path.exists():
            # G4+G5 audit: automated code check first, then LLM auditor
            from researchos.runtime.paper_audit import run_paper_audit

            # Stage 1: Automated numerical + statistical scan (zero LLM cost)
            try:
                auto_report = run_paper_audit(pid, self.project_root)
                audit_path.write_text(auto_report)
                self.state.append_log("Automated paper audit completed (G4+G5)", pid)
            except Exception:
                logger.exception("Automated paper audit failed")
                audit_path.write_text("# Audit Report\n\nAutomated audit failed.\n")

            # Stage 2: LLM auditor supplements with semantic checks (if not simulate)
            if not self.simulate:
                try:
                    prompt = self._build_auditor_prompt(pipeline)
                    result = await self.dispatcher.dispatch_via_executor(
                        "paper_auditor", prompt, pid
                    )
                    if result["success"]:
                        self.state.append_log("LLM paper audit completed", pid)
                    else:
                        logger.error("LLM paper auditor failed: %s", result.get("output", ""))
                except Exception:
                    logger.exception("LLM paper auditor error")

        elif not review_path.exists():
            if self.simulate:
                self._simulate_reviewer(pipeline)
            else:
                prompt = self._build_reviewer_prompt(pipeline)
                result = await self.dispatcher.dispatch_via_executor(
                    "reviewer_simulator", prompt, pid
                )
                if not result["success"]:
                    logger.error("Reviewer failed: %s", result.get("output", ""))
            self.state.append_log("Paper review generated", pid)

        elif not tex_path.exists():
            # Convert markdown draft to LaTeX
            try:
                md_content = draft_path.read_text()
                tex_content = md_to_latex(md_content, pipeline.title, pid)
                tex_path.write_text(tex_content)
                # Copy any figures from artifacts to paper dir
                figs = copy_figures_to_paper_dir(pid, paper_dir, self.project_root)
                if figs:
                    self.state.append_log(f"Copied {len(figs)} figures to paper dir", pid)
                self.state.append_log("LaTeX source generated", pid)
            except Exception:
                logger.exception("LaTeX generation failed")
                # Skip LaTeX, go to completion
                tex_path.write_text("% LaTeX generation failed\n")

        elif not pdf_path.exists():
            # Compile LaTeX to PDF (one attempt, then move on)
            result_pdf = compile_latex(tex_path)
            if result_pdf:
                self.state.append_log(f"PDF compiled: {result_pdf.name}", pid)
            else:
                self.state.append_log("PDF compilation failed or skipped", pid)
                # Write a placeholder so we don't retry forever
                pdf_path.write_text("PDF compilation was not successful. See paper.tex for source.\n")

            self._trigger_knowledge_compilation(pipeline)
            pipeline.stage = PipelineStage.COMPLETED
            self._maybe_save_findings(pipeline)
            self.state.save_pipeline(pipeline)
            self.state.append_log("Paper + knowledge complete, pipeline COMPLETED", pid)

        else:
            # All done
            self._trigger_knowledge_compilation(pipeline)
            pipeline.stage = PipelineStage.COMPLETED
            self._maybe_save_findings(pipeline)
            self.state.save_pipeline(pipeline)
            self.state.append_log("Paper + knowledge complete, pipeline COMPLETED", pid)

    async def _auto_professor_decision(
        self, pipeline: PipelineState, context_files: list[str]
    ) -> None:
        """Professor decision with two modes controlled by ``human_review``.

        human_review=False (default):
            Professor LLM is final authority.  Writes decision YAML directly.
            On failure → pause pipeline (never auto-approve).

        human_review=True:
            Professor LLM writes a *recommendation report* (markdown) to
            ``decisions/review_<pid>_<ts>.md``.  Pipeline enters BLOCKED
            until a human writes an approval YAML via the CLI.
        """
        pid = pipeline.pipeline_id

        # After max_milestones cycles, auto-advance to paper stage
        if pipeline.milestone_count >= self.max_milestones:
            logger.info(
                "Pipeline %s reached %d milestones, auto-advancing to paper",
                pid, pipeline.milestone_count,
            )
            self._write_default_approval(pid, next_milestone="paper")
            self.state.append_log(
                f"Auto-advanced to paper after {pipeline.milestone_count} milestones",
                pid,
            )
            return

        # ── Change 4E: auto-professor safety for L2 (HARD_FLAG) ─────────────
        # In auto mode, if the latest critic report shows HARD_FLAG, the
        # professor must NOT auto-decide — route to AWAITING_HUMAN_PIVOT
        # instead.  KILL_FLAG (L3) is still allowed in auto mode (the
        # post-critic routing already handles it via _handle_post_critic_routing,
        # but we guard here too in case the professor is dispatched after JUDGE
        # without the CRITIC routing path running first).
        critic_verdict = self._read_critic_verdict(pipeline)
        if critic_verdict.get("flag") == "HARD_FLAG":
            logger.info(
                "Auto-professor: HARD_FLAG detected for %s — routing to AWAITING_HUMAN_PIVOT",
                pid,
            )
            self.state.append_log(
                f"Auto-professor: HARD_FLAG → AWAITING_HUMAN_PIVOT for {pid}", pid
            )
            await self._trigger_awaiting_human_pivot(pipeline)
            return

        prompt = self.dispatcher._build_professor_prompt(pid, context_files)

        if self.human_review:
            # ── Human-in-the-loop mode ──────────────────────────────
            # Professor LLM writes a recommendation report, then pipeline
            # pauses for human final decision.
            review_prompt = (
                prompt + "\n\n"
                "## MODE: Recommendation Report\n"
                "You are writing a **recommendation** (not a binding decision).\n"
                "Write a markdown report using file_write:\n"
                f"  path: decisions/review_{pid}_recommendation.md\n\n"
                "Include:\n"
                "1. Summary of progress\n"
                "2. Strengths and weaknesses\n"
                "3. Your recommendation: approve / kill / pivot / pause\n"
                "4. Reasoning\n"
                "5. Suggested next_milestone (if approve)\n\n"
                "Do NOT write a YAML decision file — the human will decide.\n"
            )
            try:
                result = await self.dispatcher.dispatch_via_executor(
                    "professor", review_prompt, pid
                )
                if result["success"]:
                    self.state.append_log(
                        "Professor recommendation report written — awaiting human decision",
                        pid,
                    )
                else:
                    self.state.append_log(
                        f"Professor report failed: {result.get('output', '')[:200]}",
                        pid,
                    )
            except Exception:
                logger.exception("Professor recommendation error")

            # Pause pipeline for human review regardless of LLM outcome
            self._write_default_decision(
                pid, "pause",
                comment="Professor recommendation written. Awaiting human decision. "
                        f"See decisions/review_{pid}_recommendation.md",
            )

        else:
            # ── Fully autonomous mode ───────────────────────────────
            # Professor LLM writes binding decision YAML directly.
            try:
                result = await self.dispatcher.dispatch_via_executor(
                    "professor", prompt, pid
                )
                if result["success"]:
                    self.state.append_log(
                        "Professor decision completed", pid
                    )
                else:
                    logger.warning(
                        "Professor agent failed, pausing pipeline: %s",
                        result.get("output", ""),
                    )
                    self._write_default_decision(
                        pid, "pause",
                        comment="Professor agent failed — paused for review.",
                    )
            except Exception:
                logger.exception("Professor agent error, pausing pipeline")
                self._write_default_decision(
                    pid, "pause",
                    comment="Professor agent error — paused for review.",
                )

    def _write_default_approval(
        self, pipeline_id: str, *, next_milestone: str | None = None
    ) -> None:
        """Write a default approval decision."""
        comment = "Auto-approved (fallback)."
        if next_milestone:
            comment = f"Auto-advanced to {next_milestone} stage."
        self._write_default_decision(pipeline_id, "approve",
                                     next_milestone=next_milestone, comment=comment)

    def _write_default_decision(
        self,
        pipeline_id: str,
        decision_type: str = "approve",
        *,
        next_milestone: str | None = None,
        comment: str = "",
    ) -> None:
        """Write a decision file."""
        from datetime import datetime, timezone

        import yaml

        ts = datetime.now(timezone.utc)
        decision = {
            "pipeline_id": pipeline_id,
            "decision": decision_type,
            "comment": comment or f"Auto-{decision_type}.",
            "created_at": ts.isoformat(),
        }
        if next_milestone:
            decision["next_milestone"] = next_milestone
        fname = f"approval_{pipeline_id}_{ts.strftime('%Y%m%d%H%M%S')}.yaml"
        path = self.project_root / "decisions" / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(decision, default_flow_style=False))

    def _build_paper_writer_prompt(self, pipeline: PipelineState) -> str:
        """Build prompt for the paper_writer agent."""
        pid = pipeline.pipeline_id
        pdir = self.project_root / "artifacts" / f"pipeline_{pid}"
        artifacts = [str(f.name) for f in pdir.iterdir() if f.is_file()]

        return (
            f"Write an academic paper based on the research pipeline.\n\n"
            f"## Pipeline Info\n"
            f"- Title: {pipeline.title}\n"
            f"- Research Question: {pipeline.research_question}\n\n"
            f"## Available Artifacts (read with file_read, use relative names)\n"
            f"{chr(10).join('- ' + a for a in artifacts)}\n\n"
            f"## Instructions\n"
            f"1. Read the pipeline artifacts using file_read (e.g. path: hypothesis.md)\n"
            f"2. Use kb_retrieve to find related work and background context\n"
            f"3. Write the paper draft using file_write with path: paper/{pid}/draft_v1.md\n"
            f"4. Include: Abstract, Introduction, Related Work, Method, "
            f"Experiments, Discussion, Limitations, Conclusion\n"
            f"5. Include a ## References section at the end with numbered citations\n"
        )

    def _build_reviewer_prompt(self, pipeline: PipelineState) -> str:
        """Build prompt for the reviewer_simulator agent."""
        pid = pipeline.pipeline_id
        return (
            f"Review the following academic paper draft.\n\n"
            f"## Pipeline Info\n"
            f"- Title: {pipeline.title}\n"
            f"- Research Question: {pipeline.research_question}\n\n"
            f"## Instructions\n"
            f"1. Read the paper draft using file_read with path: paper/{pid}/draft_v1.md\n"
            f"2. Use kb_retrieve to check claims against the knowledge base\n"
            f"3. Write review using file_write with path: paper/{pid}/review_v1.md\n"
            f"4. Evaluate: clarity, novelty, methodology, results, significance\n"
        )

    def _build_auditor_prompt(self, pipeline: PipelineState) -> str:
        """Build prompt for the paper_auditor agent (G4+G5 scan)."""
        pid = pipeline.pipeline_id
        return (
            f"Perform a G4 (numerical consistency) and G5 (statistical completeness) "
            f"audit of the paper draft.\n\n"
            f"## Pipeline Info\n"
            f"- Pipeline ID: {pid}\n"
            f"- Title: {pipeline.title}\n"
            f"- Research Question: {pipeline.research_question}\n\n"
            f"## Instructions\n"
            f"1. Read the paper draft: file_read with path: paper/{pid}/draft_v1.md\n"
            f"2. Read experiment results: file_read with path: artifacts/pipeline_{pid}/results.json\n"
            f"3. Read experiment plan: file_read with path: artifacts/pipeline_{pid}/experiment_plan.yaml\n"
            f"4. Cross-reference every number in the Abstract and Results sections against results.json\n"
            f"5. Check statistical reporting completeness for each comparison\n"
            f"6. Write the audit report: file_write with path: paper/{pid}/audit_report.md\n"
        )

    def _trigger_knowledge_compilation(self, pipeline: PipelineState) -> None:
        """Compile pipeline artifacts into wiki pages."""
        try:
            pages = compile_pipeline_to_wiki(pipeline.pipeline_id, self.project_root)
            self.state.append_log(
                f"Knowledge compiled: {len(pages)} pages created",
                pipeline.pipeline_id,
            )
        except Exception:
            logger.exception("Knowledge compilation failed for %s", pipeline.pipeline_id)
            self.state.append_log(
                "ERROR: Knowledge compilation failed", pipeline.pipeline_id
            )

    def _simulate_paper_writer(self, pipeline: PipelineState) -> None:
        """Write a mock paper draft."""
        paper_dir = self.project_root / "paper" / pipeline.pipeline_id
        paper_dir.mkdir(parents=True, exist_ok=True)
        draft_path = paper_dir / "draft_v1.md"
        draft_path.write_text(
            f"# {pipeline.title}\n\n"
            f"## Abstract\n"
            f"This paper investigates: {pipeline.research_question} "
            f"Our experiments demonstrate positive results with accuracy exceeding baseline thresholds.\n\n"
            f"## 1. Introduction\n"
            f"The research question addressed in this work is: {pipeline.research_question}\n\n"
            f"## 2. Related Work\n"
            f"See knowledge base for comprehensive related work survey.\n\n"
            f"## 3. Method\n"
            f"Our approach follows the experiment design detailed in the pipeline artifacts.\n\n"
            f"## 4. Experiments\n"
            f"### 4.1 Setup\nSee experiment_plan.yaml for full configuration.\n"
            f"### 4.2 Results\nKey metrics: accuracy=0.73, f1=0.71\n"
            f"### 4.3 Analysis\nResults support the hypothesis with medium confidence.\n\n"
            f"## 5. Discussion\n"
            f"The findings suggest promising directions for future research.\n\n"
            f"## 6. Limitations\n"
            f"- Limited experimental scale\n"
            f"- Single domain evaluation\n\n"
            f"## 7. Conclusion\n"
            f"We have demonstrated initial evidence supporting our hypothesis.\n\n"
            f"_(simulation mode)_\n"
        )

    def _simulate_reviewer(self, pipeline: PipelineState) -> None:
        """Write a mock review."""
        paper_dir = self.project_root / "paper" / pipeline.pipeline_id
        paper_dir.mkdir(parents=True, exist_ok=True)
        review_path = paper_dir / "review_v1.md"
        review_path.write_text(
            f"# Peer Review: {pipeline.title}\n\n"
            f"## Overall Assessment\n"
            f"**Recommendation**: WEAK_ACCEPT\n"
            f"**Confidence**: MEDIUM\n\n"
            f"## Strengths\n"
            f"1. Clear research question and hypothesis\n"
            f"2. Systematic experimental approach\n\n"
            f"## Weaknesses\n"
            f"1. Limited experimental scale -- needs more seeds and conditions\n"
            f"2. Related work section needs expansion\n\n"
            f"## Questions for Authors\n"
            f"1. How does this compare to recent SOTA approaches?\n"
            f"2. What is the computational cost?\n\n"
            f"_(simulation mode)_\n"
        )
        rebuttal_path = paper_dir / "rebuttal_draft_v1.md"
        rebuttal_path.write_text(
            f"# Rebuttal Draft: {pipeline.title}\n\n"
            f"## Response to Reviewer\n\n"
            f"### Limited experimental scale\n"
            f"We acknowledge this limitation. In revision, we will add 5 additional seeds.\n\n"
            f"### Related work\n"
            f"We will expand the related work section with recent findings from the knowledge base.\n\n"
            f"_(simulation mode)_\n"
        )


# ── CLI entry point ──────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="ResearchOS Scheduler")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent,
        help="Project root directory",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between scheduling ticks",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick and exit",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Use mock agents instead of real LLM calls",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "openharness", "anthropic"],
        default="auto",
        help="Agent execution backend (default: auto)",
    )
    parser.add_argument(
        "--auto-professor",
        action="store_true",
        help="Auto-generate professor decisions via LLM",
    )
    parser.add_argument(
        "--human-review",
        action="store_true",
        help="Professor LLM writes recommendation; human makes final decision",
    )
    parser.add_argument(
        "--max-milestones",
        type=int,
        default=3,
        help="Max milestone cycles before auto-advancing to paper (default 3)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    scheduler = ResearchScheduler(
        args.root,
        args.interval,
        simulate=args.simulate,
        backend=args.backend,
        auto_professor=args.auto_professor,
        max_milestones=args.max_milestones,
        human_review=args.human_review,
    )

    if args.once:
        asyncio.run(scheduler.tick())
    else:
        asyncio.run(scheduler.run_loop())


if __name__ == "__main__":
    main()
