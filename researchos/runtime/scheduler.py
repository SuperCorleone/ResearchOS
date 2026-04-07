"""Research Runtime Scheduler -- stateless tick-based orchestrator.

Each ``tick()`` reads file state, applies decisions, and dispatches the next
atomic step for each active pipeline.  The scheduler itself holds no
long-lived state; everything is persisted in JSON/YAML files.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

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

# Legacy dicts kept as module-level fallbacks for external code that imports them.
# The scheduler now uses WorkflowConfig internally.
_NEXT_STEP: dict[tuple[PipelineStage, PipelineStep | None], PipelineStep | None] = {
    (PipelineStage.MAIN, None): PipelineStep.HYPOTHESIS,
    (PipelineStage.MAIN, PipelineStep.HYPOTHESIS): PipelineStep.DESIGN,
    (PipelineStage.MAIN, PipelineStep.DESIGN_REVIEW): PipelineStep.CODE,
    (PipelineStage.MAIN, PipelineStep.CODE): PipelineStep.CODE_PREFLIGHT,
    (PipelineStage.MAIN, PipelineStep.CODE_PREFLIGHT): PipelineStep.RUN,
    (PipelineStage.MAIN, PipelineStep.RUN): PipelineStep.CRITIC,
    (PipelineStage.MAIN, PipelineStep.CRITIC): PipelineStep.INTERROGATION,
    (PipelineStage.MAIN, PipelineStep.INTERROGATION): PipelineStep.REPORT,
    (PipelineStage.MAIN, PipelineStep.REPORT): PipelineStep.WAITING_APPROVAL,
    (PipelineStage.FAST_FAIL, None): PipelineStep.HYPOTHESIS,
    (PipelineStage.FAST_FAIL, PipelineStep.HYPOTHESIS): PipelineStep.DESIGN,
    (PipelineStage.FAST_FAIL, PipelineStep.CODE): PipelineStep.RUN,
    (PipelineStage.FAST_FAIL, PipelineStep.RUN): PipelineStep.CRITIC,
    (PipelineStage.FAST_FAIL, PipelineStep.CRITIC): PipelineStep.REPORT,
    (PipelineStage.FAST_FAIL, PipelineStep.REPORT): PipelineStep.WAITING_APPROVAL,
}

STEP_ARTIFACT: dict[PipelineStep, str] = {
    PipelineStep.HYPOTHESIS: "hypothesis.md",
    PipelineStep.DESIGN: "experiment_plan.yaml",
    PipelineStep.DESIGN_REVIEW: "design_review.md",
    PipelineStep.CODE: "code_patch.diff",
    PipelineStep.CODE_PREFLIGHT: "preflight_report.json",
    PipelineStep.RUN: "results.json",
    PipelineStep.CRITIC: "analysis_report.md",
    PipelineStep.INTERROGATION: "interrogation_report.md",
}


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
        self.retry_policy = RetryPolicy(max_retries=3)
        self.evaluator = TopicEvaluator(project_root, self.dispatcher)
        self.backlog = BacklogManager(project_root)

        # Load workflow configuration (YAML or hardcoded defaults)
        self.workflow = WorkflowConfig.load(project_root)
        self._next_step_map = self.workflow.build_next_step_map()

        # Load branching support
        from researchos.runtime.branch_manager import BranchManager
        self.branch_manager = BranchManager(project_root)

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
                self.branch_manager.max_branches = self.max_branches
            except Exception:
                pass

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

        When all branches reach CRITIC, dispatch branch_selector to pick the winner,
        then merge the winning branch back into the main pipeline.
        """
        from researchos.runtime.branch_manager import BranchState

        raw_branches = pipeline.metadata.get("branches", [])
        branches = [BranchState(**b) for b in raw_branches]

        # Check if all branches have completed CRITIC
        if self.branch_manager.all_branches_at_or_past(branches, PipelineStep.CRITIC):
            # All branches done — run branch selector
            self.state.append_log(
                "All branches completed CRITIC, running branch selection",
                pipeline.pipeline_id,
            )
            results = self.branch_manager.collect_branch_results(
                pipeline.pipeline_id, branches
            )
            import json as _json
            prompt = (
                f"Select the best experiment branch for pipeline {pipeline.pipeline_id}.\n\n"
                f"Branch results:\n{_json.dumps(results, indent=2, default=str)}\n\n"
                f"Write branch_selection.md with your analysis and selected branch number."
            )
            success = await self.dispatcher.dispatch_via_executor(
                "branch_selector", prompt, pipeline.pipeline_id
            )
            if success.get("success"):
                # Parse winner from selection file
                pdir = self.project_root / "artifacts" / f"pipeline_{pipeline.pipeline_id}"
                selection_file = pdir / "branch_selection.md"
                winner_id = 0  # default to first branch
                if selection_file.exists():
                    content = selection_file.read_text()
                    import re as _re
                    match = _re.search(r"Selected Branch:\s*(\d+)", content)
                    if match:
                        winner_id = int(match.group(1))

                branches = self.branch_manager.select_winner(
                    pipeline.pipeline_id, branches, winner_id
                )
                pipeline.selected_branch = winner_id
                pipeline.active_branch_count = 0
                pipeline.metadata["branches"] = [b.model_dump() for b in branches]
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
        for branch in branches:
            if branch.status != "active":
                continue
            if branch.step is None:
                continue
            # Determine next step for this branch (same logic as main pipeline)
            next_step = self._next_after_design(pipeline) if branch.step == PipelineStep.DESIGN else None
            if next_step is None:
                wf_key = (pipeline.stage.value, branch.step.value)
                next_step_name = self._next_step_map.get(wf_key)
                if next_step_name:
                    try:
                        next_step = PipelineStep(next_step_name)
                    except ValueError:
                        next_step = None

            if next_step and next_step != PipelineStep.WAITING_APPROVAL:
                branch_dir = self.branch_manager.get_branch_dir(
                    pipeline.pipeline_id, branch.branch_id
                )
                # Stop branches at CRITIC (don't go to INTERROGATION/REPORT)
                if next_step in (PipelineStep.INTERROGATION, PipelineStep.REPORT, PipelineStep.WAITING_APPROVAL):
                    branch.status = "completed"
                    continue

                tasks.append(
                    self._dispatch_branch_step(pipeline, branch, next_step, branch_dir)
                )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Save updated branch states
        pipeline.metadata["branches"] = [b.model_dump() for b in branches]
        self.state.save_pipeline(pipeline)

    async def _dispatch_branch_step(
        self,
        pipeline: PipelineState,
        branch: Any,
        step: PipelineStep,
        branch_dir: Path,
    ) -> None:
        """Dispatch a single step for a single branch."""
        agent_name = self.workflow.get_step_agent(step.value) or "pipeline_manager"
        prompt = (
            f"Pipeline: {pipeline.pipeline_id} (Branch {branch.branch_id})\n"
            f"Research question: {pipeline.research_question}\n"
            f"Step: {step.value}\n"
            f"IMPORTANT: Write all output artifacts to the branch directory: {branch_dir}\n"
            f"This is branch {branch.branch_id} of a parallel experiment. "
            f"Other branches are exploring alternative approaches."
        )
        try:
            result = await self.dispatcher.dispatch_via_executor(
                agent_name, prompt, pipeline.pipeline_id
            )
            if result.get("success"):
                branch.step = step
                logger.info(
                    "Branch %d step %s completed for %s",
                    branch.branch_id, step.value, pipeline.pipeline_id,
                )
            else:
                branch.status = "failed"
        except Exception as e:
            logger.error("Branch %d dispatch failed: %s", branch.branch_id, e)
            branch.status = "failed"

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
            for stale in ("experiment_plan.yaml", "design_review.md",
                          "code_patch.diff", "preflight_report.json",
                          "results.json", "analysis_report.md",
                          "interrogation_report.md"):
                stale_path = pdir / stale
                if stale_path.exists():
                    stale_path.unlink()

            # Clean artifact registry for removed files
            for key in ("design", "design_review", "code", "code_preflight",
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

    async def _generate_pre_main_review(
        self, pipeline: PipelineState, pdir: Path
    ) -> None:
        """Generate pre_main_review.md summarising FAST_FAIL results."""
        if self.simulate:
            self._simulate_pre_main_review(pipeline, pdir)
            return

        # Build a prompt for the pre_main_reviewer agent
        artifacts = [f.name for f in pdir.iterdir() if f.is_file() and f.name != "state.json"]
        refs_info = ""
        if pipeline.evidence_refs:
            refs_list = "\n".join(f"  - {r}" for r in pipeline.evidence_refs)
            refs_info = f"\n## Reference Papers\n{refs_list}\n"

        prompt = (
            f"Generate a pre-MAIN review for pipeline: {pipeline.pipeline_id}\n\n"
            f"## Pipeline Info\n"
            f"- Title: {pipeline.title}\n"
            f"- Research Question: {pipeline.research_question}\n"
            f"{refs_info}"
            f"\n## Available Artifacts (use file_read with relative names)\n"
            + "\n".join(f"- {a}" for a in artifacts)
            + "\n\n## Instructions\n"
            f"Read all artifacts, then write pre_main_review.md following "
            f"the pre_main_reviewer agent protocol.\n"
        )

        try:
            result = await self.dispatcher.dispatch_via_executor(
                "pre_main_reviewer", prompt, pipeline.pipeline_id
            )
            if not result["success"]:
                logger.error("Pre-main reviewer failed: %s", result.get("output", ""))
                # Write a minimal fallback review
                self._simulate_pre_main_review(pipeline, pdir)
        except Exception:
            logger.exception("Pre-main review generation error")
            self._simulate_pre_main_review(pipeline, pdir)

    def _simulate_pre_main_review(
        self, pipeline: PipelineState, pdir: Path
    ) -> None:
        """Write a mock pre_main_review.md for simulation / fallback."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()

        # Try to read results.json for metrics
        metrics_table = "| (no results available) | - | - | - |"
        results_path = pdir / "results.json"
        if results_path.exists():
            import json as _json
            try:
                results = _json.loads(results_path.read_text())
                rows = []
                for k, v in results.get("metrics", {}).items():
                    if isinstance(v, (int, float)):
                        rows.append(f"| {k} | - | {v} | - |")
                if rows:
                    metrics_table = "\n".join(rows)
            except Exception:
                pass

        refs_note = ""
        if pipeline.evidence_refs:
            refs_note = (
                f"Reference papers: {', '.join(pipeline.evidence_refs)}\n"
                f"Data consistency check: **pending user confirmation**\n\n"
            )

        (pdir / "pre_main_review.md").write_text(
            f"# Pre-MAIN Review: {pipeline.title}\n\n"
            f"## 1. Executive Summary\n"
            f"FAST_FAIL completed for pipeline {pipeline.pipeline_id}. "
            f"Preliminary results are available for review.\n\n"
            f"## 2. Data Source\n"
            f"{refs_note}"
            f"See experiment_plan.yaml for data configuration.\n\n"
            f"## 3. Baseline & Method\n"
            f"See hypothesis.md and experiment_plan.yaml for details.\n\n"
            f"## 4. Preliminary Results\n"
            f"| Metric | Baseline | Proposed | Δ |\n"
            f"|--------|----------|----------|---|\n"
            f"{metrics_table}\n\n"
            f"## 5. Professor Assessment\n"
            f"Automated review — see analysis_report.md for critic assessment.\n\n"
            f"## 6. MAIN Stage Plan\n"
            f"Full-scale experiments with additional seeds, ablations, "
            f"and comprehensive evaluation.\n\n"
            f"## 7. Key Decisions for User\n"
            f"- [ ] Approve data source and splits?\n"
            f"- [ ] Approve baseline method?\n"
            f"- [ ] Approve evaluation metrics?\n"
            f"- [ ] Any methodology changes requested?\n\n"
            f"## Recommendation\n"
            f"Review the artifacts and decide: `approve` / `tweak` / `kill`\n\n"
            f"_Generated at {ts} (simulation mode)_\n"
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
        """Decide what comes after DESIGN (or DESIGN_REVIEW in MAIN).

        In MAIN stage: DESIGN → DESIGN_REVIEW always.
        After DESIGN_REVIEW: reads requires_code to decide CODE vs RUN.
        In FAST_FAIL: DESIGN → CODE or RUN directly (no review).
        """
        import yaml as _yaml

        # MAIN stage: DESIGN always goes to DESIGN_REVIEW first
        if pipeline.stage == PipelineStage.MAIN and pipeline.step == PipelineStep.DESIGN:
            return PipelineStep.DESIGN_REVIEW

        # After DESIGN_REVIEW (MAIN) or after DESIGN (FAST_FAIL):
        # decide CODE vs RUN based on requires_code
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
            self.state.save_pipeline(pipeline)
            self.state.append_log("Knowledge compilation done, pipeline COMPLETED", pipeline.pipeline_id)
            return

        # Waiting for professor? Dispatch professor agent or auto-decide.
        if pipeline.step == PipelineStep.WAITING_APPROVAL:
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
                # After FAST_FAIL: generate pre_main_review.md, then block
                # for user approval (approve / tweak / kill)
                await self._generate_pre_main_review(pipeline, pdir)
                self._write_default_decision(
                    pipeline.pipeline_id,
                    "pause",
                    comment=(
                        "FAST_FAIL complete. Pre-MAIN review generated. "
                        "Awaiting user decision: approve / tweak / kill. "
                        "See artifacts/pipeline_"
                        f"{pipeline.pipeline_id}/pre_main_review.md"
                    ),
                )
                self.state.append_log(
                    "FAST_FAIL complete → pre_main_review.md generated, awaiting user decision",
                    pipeline.pipeline_id,
                )
            else:
                # After MAIN milestone: professor review (existing flow)
                milestones = sorted(pdir.glob("milestone_*.md"))
                context = [str(m) for m in milestones[-3:]]
                state_file = str(pdir / "state.json")
                context.append(state_file)

                if self.auto_professor:
                    await self._auto_professor_decision(pipeline, context)
                else:
                    await self.dispatcher.dispatch_professor(pipeline.pipeline_id, context)
                self.state.append_log(
                    f"Professor dispatched for milestone review",
                    pipeline.pipeline_id,
                )
            return

        # Determine next step using WorkflowConfig (falls back to legacy dict)
        stage_name = pipeline.stage.value
        step_name = pipeline.step.value if pipeline.step else None
        wf_key = (stage_name, step_name)

        # Dynamic routing: DESIGN and DESIGN_REVIEW use _next_after_design
        if pipeline.step in (PipelineStep.DESIGN, PipelineStep.DESIGN_REVIEW) and wf_key not in self._next_step_map:
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
            from researchos.runtime.branch_manager import BranchState
            branches = self.branch_manager.spawn_branches(pipeline, self.max_branches)
            pipeline.active_branch_count = len(branches)
            pipeline.metadata["branches"] = [b.model_dump() for b in branches]
            self.state.save_pipeline(pipeline)
            self.state.append_log(
                f"Spawned {len(branches)} parallel branches for exploration",
                pipeline.pipeline_id,
            )
            # Branches will be scheduled in subsequent ticks via _schedule_branch_steps
            return

        # Idempotent: check if output artifact already exists
        expected_output = self.workflow.get_step_output(next_step.value)
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
            self.state.append_log(
                f"Step dispatched: {next_step.value}", pipeline.pipeline_id
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

        # Generate a basic research brief from candidate data
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
            self.state.save_pipeline(pipeline)
            self.state.append_log("Paper + knowledge complete, pipeline COMPLETED", pid)

        else:
            # All done
            self._trigger_knowledge_compilation(pipeline)
            pipeline.stage = PipelineStage.COMPLETED
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
            f"### R1: Limited experimental scale\n"
            f"We acknowledge this limitation. In revision, we will add 5 additional seeds.\n\n"
            f"### R2: Related work\n"
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
