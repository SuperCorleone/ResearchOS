#!/usr/bin/env python -u
"""Unified ResearchOS CLI.

Usage:
    python scripts/run_research.py create --title "My Research" --question "Does X work?"
    python scripts/run_research.py run simulate --max-ticks 30
    python scripts/run_research.py run autonomous --max-ticks 50
    python scripts/run_research.py run interactive
    python scripts/run_research.py status
    python scripts/run_research.py recover --pipeline my_pipeline
    python scripts/run_research.py trial                          # Single-topic test run
"""

from __future__ import annotations

import argparse
import os

# Force unbuffered stdout so tee/pipe captures output in real time
os.environ.setdefault("PYTHONUNBUFFERED", "1")
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from researchos.schemas.pipeline import PipelineDefinition, PipelineStage
from researchos.runtime.state_manager import StateManager
from researchos.runtime.scheduler import ResearchScheduler
from researchos.runtime.recovery import recover_failed_pipeline
from researchos.utils.paths import (
    ARTIFACTS_DIR,
    DECISIONS_DIR,
    RUNTIME_PIPELINES_DIR,
)


def _slugify(text: str) -> str:
    """Convert text to a safe pipeline ID."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:30]


# ── create ──────────────────────────────────────────────────────


def cmd_create(args: argparse.Namespace) -> None:
    """Create a new research pipeline proposal."""
    pipeline_id = args.id or _slugify(args.title)

    # Check for conflicts
    pdir = ARTIFACTS_DIR / f"pipeline_{pipeline_id}"
    if pdir.exists():
        print(f"  ERROR: Pipeline '{pipeline_id}' already exists.")
        sys.exit(1)

    proposal = PipelineDefinition(
        pipeline_id=pipeline_id,
        title=args.title,
        research_question=args.question,
        budget_request=args.budget,
        source="user",
        evidence_refs=args.refs,
    )

    RUNTIME_PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RUNTIME_PIPELINES_DIR / f"new_{pipeline_id}.json"
    out_path.write_text(proposal.model_dump_json(indent=2))

    print(f"\n  Pipeline proposal created: {pipeline_id}")
    print(f"  File: {out_path}")
    print(f"\n  Next steps:")
    print(f"    python scripts/run_research.py run simulate --max-ticks 30")
    print(f"    python scripts/run_research.py run autonomous --max-ticks 50")
    print(f"    python scripts/run_research.py run interactive")


# ── run ─────────────────────────────────────────────────────────


async def _run_ticks(
    scheduler: ResearchScheduler,
    max_ticks: int,
    interactive: bool = False,
) -> None:
    """Run scheduler ticks, optionally pausing at milestones."""
    sm = scheduler.state

    # Record pre-existing pipeline IDs so single_run only tracks new ones
    pre_existing_ids: set[str] = set()
    if scheduler.single_run:
        pre_existing_ids = {p.pipeline_id for p in sm.load_all_pipelines()}

    for tick_num in range(1, max_ticks + 1):
        print(f"\n  --- Tick {tick_num}/{max_ticks} ---")

        try:
            await scheduler.tick()
        except Exception as e:
            print(f"  ERROR in tick: {e}")

        # Show pipeline states
        pipelines = sm.load_all_pipelines()
        all_done = True
        for p in pipelines:
            status = f"  [{p.pipeline_id}] stage={p.stage.value}"
            if p.step:
                status += f" step={p.step.value}"
            if p.last_error:
                status += f" error={p.last_error}"
            print(status)

            if p.stage not in (PipelineStage.COMPLETED, PipelineStage.FAILED):
                all_done = False

        if all_done and pipelines:
            print("\n  All pipelines completed!")
            break

        # single_run mode: stop once the NEW pipeline reaches a terminal or
        # near-terminal stage (COMPLETED, FAILED, or PAPER which means the
        # main research is done and only writing remains)
        if scheduler.single_run and pipelines:
            new_done = [
                p for p in pipelines
                if p.pipeline_id not in pre_existing_ids
                and p.stage in (
                    PipelineStage.COMPLETED,
                    PipelineStage.FAILED,
                    PipelineStage.PAPER,
                )
            ]
            if new_done:
                p0 = new_done[0]
                if p0.stage == PipelineStage.PAPER:
                    print(f"\n  Trial: pipeline '{p0.pipeline_id}' reached PAPER stage. Continuing to finish paper...")
                    # Don't break yet — let it finish paper writing
                    # But do break if paper is fully done (draft + review + tex exist)
                    paper_dir = PROJECT_ROOT / "paper" / p0.pipeline_id
                    if (paper_dir / "review_v1.md").exists():
                        print(f"  Trial complete: paper draft + review done for '{p0.pipeline_id}'.")
                        break
                else:
                    print(f"\n  Trial complete: pipeline '{p0.pipeline_id}' finished ({p0.stage.value}).")
                    break

        # Interactive mode: pause at milestones for professor decisions
        if interactive:
            requests = list(_pending_requests())
            if requests:
                print(f"\n  {len(requests)} pipeline(s) awaiting professor decision.")
                for req in requests:
                    _interactive_decide(req)

        # Small delay between ticks
        if tick_num < max_ticks:
            await asyncio.sleep(0.1 if scheduler.simulate else scheduler.interval)


def _pending_requests():
    """Yield pending decision request files."""
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(DECISIONS_DIR.glob("request_*.yaml")):
        try:
            data = yaml.safe_load(f.read_text())
            if data:
                data["_file"] = f
                yield data
        except Exception:
            pass


def _interactive_decide(request: dict) -> None:
    """Handle a single interactive professor decision."""
    pid = request.get("pipeline_id", "unknown")
    print(f"\n  Pipeline: {pid}")

    # Show brief context
    pdir = ARTIFACTS_DIR / f"pipeline_{pid}"
    state_file = pdir / "state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text())
        print(f"  Title: {state.get('title', '?')}")
        print(f"  Question: {state.get('research_question', '?')}")
        print(f"  Milestones: {state.get('milestone_count', 0)}")

    # Show latest milestone report
    milestones = sorted(pdir.glob("milestone_*.md"))
    if milestones:
        content = milestones[-1].read_text()
        lines = content.splitlines()[:20]
        print(f"\n  Latest milestone ({milestones[-1].name}):")
        for line in lines:
            print(f"    {line}")

    print("\n  Options: [a]pprove  [k]ill  [p]ivot  [s]kip")
    choice = input("  Decision: ").strip().lower()

    decision_map = {"a": "approve", "k": "kill", "p": "pivot", "s": None}
    decision = decision_map.get(choice)

    if decision is None:
        print("  Skipped.")
        return

    next_milestone = None
    if decision == "approve":
        nm = input("  Enter 'paper' for paper stage, or press Enter: ").strip()
        if nm:
            next_milestone = nm

    ts = datetime.now(timezone.utc)
    decision_data = {
        "pipeline_id": pid,
        "decision": decision,
        "comment": f"Interactive decision: {decision}",
        "created_at": ts.isoformat(),
    }
    if next_milestone:
        decision_data["next_milestone"] = next_milestone

    fname = f"approval_{pid}_{ts.strftime('%Y%m%d%H%M%S')}.yaml"
    path = DECISIONS_DIR / fname
    path.write_text(yaml.dump(decision_data, default_flow_style=False))
    print(f"  Decision written: {fname}")

    # Mark request as processed
    req_file = request.get("_file")
    if req_file and Path(req_file).exists():
        Path(req_file).rename(Path(req_file).with_suffix(".processed.yaml"))


def cmd_run(args: argparse.Namespace) -> None:
    """Run the research scheduler."""
    mode = args.mode
    max_ticks = args.max_ticks

    human_review = getattr(args, "human_review", False)
    max_milestones = getattr(args, "max_milestones", 3)

    if mode == "simulate":
        scheduler = ResearchScheduler(
            PROJECT_ROOT,
            interval_seconds=0,
            simulate=True,
        )
        print("  Running in SIMULATE mode (no LLM calls)...")
    elif mode == "autonomous":
        scheduler = ResearchScheduler(
            PROJECT_ROOT,
            interval_seconds=args.interval,
            backend=args.backend,
            auto_professor=True,
            auto_select=getattr(args, "auto_select", False),
            human_review=human_review,
            max_milestones=max_milestones,
        )
        label = "AUTONOMOUS"
        if human_review:
            label += " + HUMAN REVIEW (professor writes report, you decide)"
        print(f"  Running in {label} mode...")
    elif mode == "interactive":
        scheduler = ResearchScheduler(
            PROJECT_ROOT,
            interval_seconds=args.interval,
            backend=args.backend,
        )
        print("  Running in INTERACTIVE mode (you are the professor)...")
    else:
        print(f"  Unknown mode: {mode}")
        return

    asyncio.run(_run_ticks(scheduler, max_ticks, interactive=(mode == "interactive")))


# ── status ──────────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> None:
    """Show status of all pipelines, budget, and pending decisions."""
    sm = StateManager(PROJECT_ROOT)

    # Pipelines
    pipelines = sm.load_all_pipelines()
    if not pipelines:
        print("\n  No pipelines found.")
    else:
        print(f"\n  Pipelines ({len(pipelines)}):")
        print(f"  {'ID':<25} {'Stage':<15} {'Step':<20} {'Milestones':<10} {'Budget'}")
        print(f"  {'-'*85}")
        for p in pipelines:
            step = p.step.value if p.step else "-"
            budget = f"{p.budget_used}/{p.budget_allocated}"
            print(f"  {p.pipeline_id:<25} {p.stage.value:<15} {step:<20} {p.milestone_count:<10} {budget}")
            if p.last_error:
                print(f"    Error: {p.last_error}")

    # Budget
    budget = sm.load_budget()
    total_used = sum(budget.usage.values())
    print(f"\n  Global Budget: {total_used}/{budget.total_credits} credits used")
    print(f"  Reserve: {budget.reserve} credits")

    # Pending decisions
    requests = list(_pending_requests())
    if requests:
        print(f"\n  Pending Decisions ({len(requests)}):")
        for req in requests:
            print(f"    - Pipeline: {req.get('pipeline_id', '?')}")

    # Proposals
    proposals = sm.load_new_proposals()
    if proposals:
        print(f"\n  Pending Proposals ({len(proposals)}):")
        for prop in proposals:
            print(f"    - {prop.pipeline_id}: {prop.title}")


# ── recover ─────────────────────────────────────────────────────


def cmd_recover(args: argparse.Namespace) -> None:
    """Recover a failed pipeline."""
    result = recover_failed_pipeline(
        args.pipeline,
        PROJECT_ROOT,
        rollback=args.rollback,
    )
    if result:
        print(f"\n  Pipeline '{args.pipeline}' recovered.")
        print(f"  Stage: {result.stage.value}")
        print(f"  Step:  {result.step.value if result.step else '-'}")
    else:
        print(f"\n  Failed to recover pipeline '{args.pipeline}'.")
        print(f"  Check if it exists and is in FAILED state.")


# ── publish ────────────────────────────────────────────────────


def cmd_publish(args: argparse.Namespace) -> None:
    """Package a completed pipeline for publication submission."""
    from researchos.runtime.publisher import publish_pipeline

    pipeline_id = args.pipeline
    try:
        output_dir = publish_pipeline(pipeline_id, PROJECT_ROOT)
        print(f"\n  Publication package created:")
        print(f"  {output_dir}/")
        for f in sorted(output_dir.rglob("*")):
            if f.is_file():
                rel = f.relative_to(output_dir)
                print(f"    {rel} ({f.stat().st_size // 1024} KB)")
        print(f"\n  See PROCESS_REPORT.md for submission guidance.")
    except Exception as e:
        print(f"  ERROR: {e}")


# ── fix ────────────────────────────────────────────────────────


def cmd_fix(args: argparse.Namespace) -> None:
    """Auto-fix pipeline based on professor review report, then resume."""
    pipeline_id = args.pipeline

    # Verify pipeline exists and is BLOCKED
    sm = StateManager(PROJECT_ROOT)
    pipeline = sm.load_pipeline(pipeline_id)
    if pipeline is None:
        print(f"  ERROR: Pipeline '{pipeline_id}' not found.")
        return
    if pipeline.stage.value != "blocked":
        print(f"  ERROR: Pipeline is '{pipeline.stage.value}', not BLOCKED. Fix only works on paused pipelines.")
        return

    # Find professor review report
    review_path = PROJECT_ROOT / "decisions" / f"review_{pipeline_id}_recommendation.md"
    if not review_path.exists():
        print(f"  ERROR: No review report found at {review_path}")
        return

    review_content = review_path.read_text()
    print(f"  Found review report: {review_path.name} ({len(review_content)} chars)")

    # List pipeline artifacts
    pdir = ARTIFACTS_DIR / f"pipeline_{pipeline_id}"
    artifacts = [f.name for f in pdir.iterdir() if f.is_file()] if pdir.exists() else []

    # Build fixer prompt
    prompt = (
        f"You are the Fixer agent for pipeline: {pipeline_id}\n\n"
        f"## Pipeline Info\n"
        f"- Title: {pipeline.title}\n"
        f"- Research Question: {pipeline.research_question}\n\n"
        f"## Professor Review Report\n"
        f"Read the full report using file_read:\n"
        f"  path: decisions/review_{pipeline_id}_recommendation.md\n\n"
        f"## Available Artifacts (use file_read with relative names)\n"
        + "\n".join(f"- {a}" for a in artifacts) + "\n\n"
        f"## Instructions\n"
        f"1. Read the professor's review report to extract ALL required corrections\n"
        f"2. Read the current experiment code\n"
        f"3. Fix all issues identified in the report\n"
        f"4. Re-run the corrected experiment using execute_python\n"
        f"5. Write updated results.json with the new results\n"
        f"6. Write correction_report.md documenting all changes\n"
    )

    print(f"  Dispatching fixer agent...")

    # Run fixer via agent executor
    from researchos.runtime.dispatcher import Dispatcher
    dispatcher = Dispatcher(PROJECT_ROOT, backend="auto")

    result = asyncio.run(
        dispatcher.dispatch_via_executor("fixer", prompt, pipeline_id)
    )

    if result["success"]:
        print(f"  Fixer completed: {result['artifacts_written']}")
        print(f"  Tokens used: {result['tokens_used']}")

        # Check if correction_report.md was written
        correction_report = pdir / "correction_report.md"
        if correction_report.exists():
            print(f"\n  Correction report:")
            lines = correction_report.read_text().splitlines()[:20]
            for line in lines:
                print(f"    {line}")
            if len(correction_report.read_text().splitlines()) > 20:
                print(f"    ... ({len(correction_report.read_text().splitlines())} lines total)")

        # Auto-approve to resume
        if not args.no_resume:
            import yaml as _yaml
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc)
            decision = {
                "pipeline_id": pipeline_id,
                "decision": "approve",
                "comment": "Auto-approved after fixer agent corrections.",
                "created_at": ts.isoformat(),
            }
            fname = f"approval_{pipeline_id}_{ts.strftime('%Y%m%d%H%M%S')}.yaml"
            (DECISIONS_DIR / fname).write_text(_yaml.dump(decision, default_flow_style=False))
            print(f"\n  Pipeline approved to resume. Run scheduler to continue.")
        else:
            print(f"\n  Fixes applied. Review correction_report.md, then approve manually.")
    else:
        print(f"  ERROR: Fixer failed: {result.get('output', '')[:300]}")


# ── inject ─────────────────────────────────────────────────────


def cmd_inject(args: argparse.Namespace) -> None:
    """Inject user-provided papers (and optional proposal) into the system.

    Mode 1 - papers only:
        Papers → compile to wiki → Knowledge Miner discovers topic → pipeline
    Mode 2 - papers + proposal:
        Papers → compile to wiki → skip Miner → pipeline created directly
    Both share the knowledge base.
    """
    import shutil
    from researchos.runtime.knowledge_compiler import compile_raw_to_wiki

    papers = args.papers
    if not papers:
        print("  ERROR: Provide at least one paper file.")
        return

    # Copy papers to knowledge/raw/
    raw_dir = PROJECT_ROOT / "knowledge" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    imported = []
    for paper_path in papers:
        src = Path(paper_path)
        if not src.exists():
            print(f"  WARNING: File not found: {paper_path}")
            continue
        dest = raw_dir / src.name
        if dest.exists():
            print(f"  Skip (already exists): {src.name}")
        else:
            shutil.copy2(src, dest)
            imported.append(dest)
            print(f"  Imported: {src.name}")

    # Compile all imported papers to wiki
    compiled = 0
    for f in imported:
        page = compile_raw_to_wiki(f, PROJECT_ROOT)
        if page:
            compiled += 1
            print(f"  Compiled to wiki: {page.name}")

    print(f"\n  {len(imported)} papers imported, {compiled} compiled to wiki.")

    # Mode 2: user also provides a proposal → create pipeline directly
    if args.title and args.question:
        pipeline_id = args.id or _slugify(args.title)
        pdir = ARTIFACTS_DIR / f"pipeline_{pipeline_id}"
        if pdir.exists():
            print(f"  ERROR: Pipeline '{pipeline_id}' already exists.")
            return

        proposal = PipelineDefinition(
            pipeline_id=pipeline_id,
            title=args.title,
            research_question=args.question,
            budget_request=args.budget,
            source="user_inject",
            evidence_refs=args.papers,
        )

        RUNTIME_PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
        out_path = RUNTIME_PIPELINES_DIR / f"new_{pipeline_id}.json"
        out_path.write_text(proposal.model_dump_json(indent=2))

        print(f"\n  Pipeline proposal created: {pipeline_id}")
        print(f"  Papers + proposal injected → run scheduler to start pipeline.")
    else:
        print(f"\n  Papers injected → Knowledge Miner will discover topics on next scheduler run.")
        print(f"  To skip Miner and create a pipeline directly, add --title and --question.")

    print(f"\n  Next: python scripts/run_research.py run autonomous --max-ticks 50")


# ── launch ─────────────────────────────────────────────────────


def cmd_launch(args: argparse.Namespace) -> None:
    """One-click launch: heartbeat fetch + scheduler run."""
    from researchos.runtime.heartbeat import load_config as load_hb_config, fetch_papers

    print("  ResearchOS Launch")
    print("  " + "=" * 40)

    # Step 1: Heartbeat fetch (if enabled)
    hb_config = load_hb_config()
    if hb_config.get("enabled", False):
        print("\n  [1/3] Heartbeat: fetching papers from arXiv...")
        try:
            saved = fetch_papers(hb_config)
            print(f"        Downloaded {len(saved)} new PDFs")
        except Exception as e:
            print(f"        Heartbeat error: {e}")
    else:
        print("\n  [1/3] Heartbeat: disabled (use --enable-heartbeat or edit heartbeat_config.yaml)")

    if args.enable_heartbeat:
        from researchos.runtime.heartbeat import set_enabled
        set_enabled(True)
        print("        Heartbeat enabled for future runs.")

    # Step 2: Show current status
    sm = StateManager(PROJECT_ROOT)
    pipelines = sm.load_all_pipelines()
    active = [p for p in pipelines if p.stage.value not in ("completed", "failed", "blocked")]
    print(f"\n  [2/3] Status: {len(pipelines)} pipelines ({len(active)} active)")

    # Step 3: Start scheduler
    mode = args.mode or "autonomous"
    max_ticks = args.max_ticks
    interval = args.interval

    print(f"\n  [3/3] Starting scheduler: mode={mode}, ticks={max_ticks}, interval={interval}s")
    if args.human_review:
        print("        Human review: ON (professor writes reports, you decide)")
    print()

    scheduler = ResearchScheduler(
        PROJECT_ROOT,
        interval_seconds=interval,
        backend="auto",
        auto_professor=(mode == "autonomous"),
        auto_select=getattr(args, "auto_select", False),
        human_review=args.human_review,
        max_milestones=args.max_milestones,
        simulate=(mode == "simulate"),
    )
    asyncio.run(_run_ticks(scheduler, max_ticks, interactive=(mode == "interactive")))


# ── trial ─────────────────────────────────────────────────────


def cmd_trial(args: argparse.Namespace) -> None:
    """Trial mode: heartbeat once → one topic → one pipeline → stop.

    Designed for testing the full research lifecycle end-to-end
    without spawning additional topics or pipelines.
    """
    from researchos.runtime.heartbeat import load_config as load_hb_config, fetch_papers

    print("  ResearchOS Trial Mode")
    print("  " + "=" * 40)
    print("  Heartbeat once → discover one topic → run one pipeline → stop")

    # Step 1: Heartbeat — fetch papers once
    hb_config = load_hb_config()
    if hb_config.get("enabled", False):
        print("\n  [1/4] Heartbeat: fetching papers from arXiv (one-shot)...")
        try:
            saved = fetch_papers(hb_config)
            print(f"        Downloaded {len(saved)} new PDFs")
        except Exception as e:
            print(f"        Heartbeat error (non-fatal): {e}")
    else:
        print("\n  [1/4] Heartbeat: disabled — skipping paper fetch")
        print("        (enable in runtime/heartbeat_config.yaml or use --enable-heartbeat)")

    if args.enable_heartbeat:
        from researchos.runtime.heartbeat import set_enabled
        set_enabled(True)
        print("        Heartbeat enabled for this run.")
        hb_config = load_hb_config()
        try:
            saved = fetch_papers(hb_config)
            print(f"        Downloaded {len(saved)} new PDFs")
        except Exception as e:
            print(f"        Heartbeat error (non-fatal): {e}")

    # Step 2: Pause miner after first run so no new topics are discovered
    miner_pause = PROJECT_ROOT / "runtime" / ".miner_paused"
    was_paused = miner_pause.exists()
    print(f"\n  [2/4] Miner: will run once to find one topic, then auto-pause")

    # Step 3: Show current state and record pre-existing pipeline IDs
    sm = StateManager(PROJECT_ROOT)
    pipelines = sm.load_all_pipelines()
    pre_existing_ids = {p.pipeline_id for p in pipelines}
    active = [p for p in pipelines if p.stage.value not in ("completed", "failed")]
    print(f"\n  [3/4] Status: {len(pipelines)} existing pipelines ({len(active)} active)")
    if pre_existing_ids:
        print(f"        (will ignore these for completion check)")

    # Step 4: Run scheduler in single_run mode
    mode = args.mode or "autonomous"
    max_ticks = args.max_ticks

    print(f"\n  [4/4] Starting scheduler: mode={mode}, max_ticks={max_ticks}, single_run=True")
    if args.human_review:
        print("        Human review: ON")
    print()

    scheduler = ResearchScheduler(
        PROJECT_ROOT,
        interval_seconds=args.interval,
        backend="auto",
        auto_professor=(mode == "autonomous"),
        auto_select=getattr(args, "auto_select", True),  # trial defaults to auto
        human_review=args.human_review,
        max_milestones=args.max_milestones,
        simulate=(mode == "simulate"),
        single_run=True,
    )
    asyncio.run(_run_ticks(scheduler, max_ticks, interactive=(mode == "interactive")))

    # Restore miner pause state if it wasn't paused before
    if not was_paused and miner_pause.exists():
        miner_pause.unlink()

    # Auto-publish only NEW completed pipelines (not pre-existing ones)
    sm = StateManager(PROJECT_ROOT)
    completed = [
        p for p in sm.load_all_pipelines()
        if p.stage == PipelineStage.COMPLETED
        and p.pipeline_id not in pre_existing_ids
    ]
    if completed:
        from researchos.runtime.publisher import publish_pipeline
        for p in completed:
            pub_dir = PROJECT_ROOT / "publications" / p.pipeline_id
            if pub_dir.exists():
                print(f"\n  [publish] Already packaged: {p.pipeline_id}")
                continue
            try:
                out = publish_pipeline(p.pipeline_id, PROJECT_ROOT)
                print(f"\n  [publish] Packaged: {out}")
            except Exception as e:
                print(f"\n  [publish] Skipped {p.pipeline_id}: {e}")

    print("\n  Trial finished.")


# ── candidates (Feature 1: user-driven topic selection) ───────


def cmd_candidates(args: argparse.Namespace) -> None:
    """Show, select, or reject candidate topics awaiting user decision."""
    candidates_dir = PROJECT_ROOT / "runtime" / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)

    pending = sorted(candidates_dir.glob("candidate_*.json"))
    pending = [f for f in pending if not f.name.endswith(".selected.json")
               and not f.name.endswith(".rejected.json")]

    if args.action == "list":
        if not pending:
            print("\n  No pending candidates. Run the scheduler to discover topics.")
            return
        print(f"\n  Pending Candidates ({len(pending)}):")
        print(f"  {'#':<4} {'Pipeline ID':<32} {'Title'}")
        print(f"  {'-'*80}")
        for i, f in enumerate(pending, 1):
            data = json.loads(f.read_text())
            print(f"  {i:<4} {data['pipeline_id']:<32} {data['title']}")
            if args.verbose:
                print(f"       Q: {data.get('research_question', '')[:80]}")
                print(f"       Motivation: {data.get('motivation', '')[:80]}")
                if data.get('innovation_type'):
                    print(f"       Innovation: {data['innovation_type']}")
                # Show research brief if available
                brief_path = candidates_dir / f"research_brief_{data['pipeline_id']}.md"
                if brief_path.exists():
                    print(f"       Brief: {brief_path.name}")
                    for line in brief_path.read_text().splitlines()[:8]:
                        print(f"         {line}")
                    print(f"         ...")
                print()

    elif args.action == "select":
        idx = args.index
        if not pending or idx < 1 or idx > len(pending):
            print(f"  Invalid index. Use 'candidates list' to see options (1-{len(pending)}).")
            return
        f = pending[idx - 1]
        data = json.loads(f.read_text())
        pid = data["pipeline_id"]

        # Create pipeline
        proposal = PipelineDefinition(
            pipeline_id=pid,
            title=data["title"],
            research_question=data["research_question"],
            budget_request=data.get("budget_request", 20000),
            source="user_selected",
            evidence_refs=data.get("evidence", []),
        )
        RUNTIME_PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
        out = RUNTIME_PIPELINES_DIR / f"new_{pid}.json"
        out.write_text(proposal.model_dump_json(indent=2))

        # Record user feedback if provided
        feedback = args.feedback or ""
        _record_topic_feedback(pid, data["title"], "selected", feedback)

        # Mark as selected
        f.rename(f.with_suffix(".selected.json"))
        print(f"  Selected: {data['title']}")
        print(f"  Pipeline {pid} created. Run scheduler to start.")

    elif args.action == "reject":
        idx = args.index
        if not pending or idx < 1 or idx > len(pending):
            print(f"  Invalid index.")
            return
        f = pending[idx - 1]
        data = json.loads(f.read_text())

        feedback = args.feedback or ""
        _record_topic_feedback(data["pipeline_id"], data["title"], "rejected", feedback)

        f.rename(f.with_suffix(".rejected.json"))
        print(f"  Rejected: {data['title']}")
        if feedback:
            print(f"  Feedback recorded for learning.")

    elif args.action == "brief":
        idx = args.index
        if not pending or idx < 1 or idx > len(pending):
            print(f"  Invalid index.")
            return
        f = pending[idx - 1]
        data = json.loads(f.read_text())
        pid = data["pipeline_id"]

        # Check if brief already exists
        brief_path = candidates_dir / f"research_brief_{pid}.md"
        if brief_path.exists():
            print(f"\n{brief_path.read_text()}")
        else:
            print(f"  No research brief found for {pid}.")
            print(f"  Run the scheduler to generate briefs, or use 'candidates list -v'.")


def _record_topic_feedback(pipeline_id: str, title: str, decision: str, feedback: str) -> None:
    """Record user topic selection feedback to wiki for learning."""
    from datetime import datetime, timezone
    wiki_dir = PROJECT_ROOT / "knowledge" / "wiki" / "decisions"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc)
    month = ts.strftime("%Y_%m")
    feedback_file = wiki_dir / f"topic_preferences_{month}.md"

    if not feedback_file.exists():
        feedback_file.write_text(
            f"---\ntitle: Topic Preferences {ts.strftime('%Y-%m')}\n"
            f"tags: [topic-feedback, user-preferences]\n"
            f"created: {ts.isoformat()}\n---\n\n"
            f"# Topic Selection Feedback — {ts.strftime('%B %Y')}\n\n"
            f"User decisions on candidate topics. Used by Knowledge Miner to learn preferences.\n\n"
        )

    entry = (
        f"## [{decision.upper()}] {title}\n"
        f"- **Pipeline ID**: {pipeline_id}\n"
        f"- **Date**: {ts.strftime('%Y-%m-%d %H:%M')}\n"
        f"- **Decision**: {decision}\n"
    )
    if feedback:
        entry += f"- **Reason**: {feedback}\n"
    entry += "\n"

    with open(feedback_file, "a") as fh:
        fh.write(entry)


# ── review (pre-MAIN user decision) ────────────────────────────


def cmd_review(args: argparse.Namespace) -> None:
    """Review a pipeline after FAST_FAIL and decide: approve / tweak / kill."""
    pipeline_id = args.pipeline
    pdir = ARTIFACTS_DIR / f"pipeline_{pipeline_id}"
    if not pdir.exists():
        print(f"  ERROR: Pipeline '{pipeline_id}' not found.")
        return

    state_file = pdir / "state.json"
    if not state_file.exists():
        print(f"  ERROR: No state.json for pipeline '{pipeline_id}'.")
        return

    # Show pre_main_review if it exists
    review_path = pdir / "pre_main_review.md"
    if review_path.exists() and not (args.approve or args.tweak or args.kill):
        print(f"\n{'='*60}")
        print(review_path.read_text())
        print(f"{'='*60}")
        print(f"\n  Actions:")
        print(f"    review --pipeline {pipeline_id} --approve")
        print(f"    review --pipeline {pipeline_id} --tweak \"your directive here\"")
        print(f"    review --pipeline {pipeline_id} --kill")
        return

    # Write decision
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc)
    fname = f"approval_{pipeline_id}_{ts.strftime('%Y%m%d%H%M%S')}.yaml"
    decision_path = DECISIONS_DIR / fname
    decision_path.parent.mkdir(parents=True, exist_ok=True)

    if args.approve:
        decision = {
            "pipeline_id": pipeline_id,
            "decision": "approve",
            "comment": "User approved after pre-MAIN review.",
            "created_at": ts.isoformat(),
        }
        decision_path.write_text(yaml.dump(decision, default_flow_style=False))
        print(f"  Pipeline '{pipeline_id}' approved → will enter MAIN stage on next tick.")

    elif args.tweak:
        decision = {
            "pipeline_id": pipeline_id,
            "decision": "tweak",
            "comment": args.tweak,
            "created_at": ts.isoformat(),
        }
        decision_path.write_text(yaml.dump(decision, default_flow_style=False))
        print(f"  Pipeline '{pipeline_id}' tweaked → will re-run from DESIGN with directive:")
        print(f"    \"{args.tweak}\"")

    elif args.kill:
        decision = {
            "pipeline_id": pipeline_id,
            "decision": "kill",
            "comment": args.kill if isinstance(args.kill, str) and args.kill != "True" else "Killed by user after pre-MAIN review.",
            "created_at": ts.isoformat(),
        }
        decision_path.write_text(yaml.dump(decision, default_flow_style=False))
        print(f"  Pipeline '{pipeline_id}' killed.")

    else:
        if review_path.exists():
            print(f"\n{'='*60}")
            print(review_path.read_text())
            print(f"{'='*60}")
        print(f"\n  Use --approve, --tweak \"directive\", or --kill to decide.")

    print(f"\n  Next: python scripts/run_research.py run autonomous --max-ticks 50")


# ── query / tweak / explore (Feature 4: deep interaction) ─────


def cmd_query(args: argparse.Namespace) -> None:
    """Ask a question about a pipeline's results or intermediate state."""
    pipeline_id = args.pipeline
    question = args.question

    pdir = ARTIFACTS_DIR / f"pipeline_{pipeline_id}"
    if not pdir.exists():
        print(f"  ERROR: Pipeline '{pipeline_id}' not found.")
        return

    # Gather context
    context_parts = []
    for artifact in ["results.json", "analysis_report.md", "experiment_plan.yaml",
                     "hypothesis.md", "interrogation_report.md", "preflight_report.json"]:
        path = pdir / artifact
        if path.exists():
            content = path.read_text()[:3000]  # Truncate for prompt
            context_parts.append(f"## {artifact}\n```\n{content}\n```\n")

    prompt = (
        f"You are a research assistant answering a question about pipeline: {pipeline_id}\n\n"
        f"## User Question\n{question}\n\n"
        f"## Available Artifacts\n{''.join(context_parts)}\n"
        f"Answer concisely, citing specific numbers and artifacts.\n"
        f"Write your answer using file_write to: query_response.md\n"
    )

    from researchos.runtime.dispatcher import Dispatcher
    dispatcher = Dispatcher(PROJECT_ROOT, backend="auto")
    result = asyncio.run(dispatcher.dispatch_via_executor("reporter", prompt, pipeline_id))

    if result["success"]:
        resp_path = pdir / "query_response.md"
        if resp_path.exists():
            print(f"\n{resp_path.read_text()}")
        else:
            print(f"  Response generated. Check {pdir}/")
    else:
        print(f"  ERROR: {result.get('output', '')[:300]}")


def cmd_tweak(args: argparse.Namespace) -> None:
    """Inject a user directive into a pipeline for the next experiment cycle."""
    pipeline_id = args.pipeline
    directive = args.directive

    pdir = ARTIFACTS_DIR / f"pipeline_{pipeline_id}"
    if not pdir.exists():
        print(f"  ERROR: Pipeline '{pipeline_id}' not found.")
        return

    directives_path = pdir / "user_directives.yaml"
    existing = []
    if directives_path.exists():
        existing = yaml.safe_load(directives_path.read_text()) or []
        if not isinstance(existing, list):
            existing = [existing]

    from datetime import datetime, timezone
    existing.append({
        "directive": directive,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "applied": False,
    })
    directives_path.write_text(yaml.dump(existing, default_flow_style=False))

    print(f"  Directive added to pipeline '{pipeline_id}':")
    print(f"    \"{directive}\"")
    print(f"  Will be applied in the next experiment design/code cycle.")


def cmd_explore(args: argparse.Namespace) -> None:
    """Show key intermediate artifacts and metrics for a pipeline."""
    pipeline_id = args.pipeline
    pdir = ARTIFACTS_DIR / f"pipeline_{pipeline_id}"
    if not pdir.exists():
        print(f"  ERROR: Pipeline '{pipeline_id}' not found.")
        return

    state_file = pdir / "state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text())
        print(f"\n  Pipeline: {pipeline_id}")
        print(f"  Title: {state.get('title', '?')}")
        print(f"  Stage: {state.get('stage', '?')} / Step: {state.get('step', '?')}")
        print(f"  Milestones: {state.get('milestone_count', 0)}")
        print(f"  Budget: {state.get('budget_used', 0)}/{state.get('budget_allocated', 0)}")

    # Show results summary
    results_path = pdir / "results.json"
    if results_path.exists():
        results = json.loads(results_path.read_text())
        print(f"\n  Metrics:")
        for k, v in results.get("metrics", {}).items():
            if isinstance(v, dict):
                # Nested model metrics
                for mk, mv in v.items():
                    if isinstance(mv, (int, float)):
                        print(f"    {k}.{mk}: {mv:.4f}")
            elif isinstance(v, (int, float)):
                print(f"    {k}: {v:.4f}")

        config = results.get("config", {})
        if config.get("data_source"):
            print(f"\n  Data: {config['data_source']}")
        if config.get("data_rows"):
            print(f"  Rows: {config['data_rows']}")

    # Show artifacts
    print(f"\n  Artifacts:")
    for f in sorted(pdir.iterdir()):
        if f.is_file() and f.name != "state.json":
            size = f.stat().st_size
            print(f"    {f.name} ({size // 1024}KB)" if size > 1024 else f"    {f.name} ({size}B)")

    # Show user directives if any
    directives_path = pdir / "user_directives.yaml"
    if directives_path.exists():
        dirs = yaml.safe_load(directives_path.read_text()) or []
        if dirs:
            print(f"\n  User Directives:")
            for d in dirs:
                status = "applied" if d.get("applied") else "pending"
                print(f"    [{status}] {d.get('directive', '?')}")

    # Show interrogation summary if exists
    interrog_path = pdir / "interrogation_report.md"
    if interrog_path.exists():
        content = interrog_path.read_text()
        if "Final Verdict:" in content:
            for line in content.splitlines():
                if "Final Verdict:" in line:
                    print(f"\n  Interrogation: {line.strip()}")
                    break


# ── main ────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ResearchOS - Knowledge-Driven Multi-Agent Research Lab",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s launch                                # One-click start\n"
            "  %(prog)s launch --human-review                 # Professor writes report, you decide\n"
            "  %(prog)s trial                                 # Heartbeat once + one topic + one pipeline\n"
            "  %(prog)s trial --mode simulate                 # Trial in simulate mode (no LLM)\n"
            "  %(prog)s inject paper1.pdf paper2.pdf           # Add papers, let Miner find topics\n"
            "  %(prog)s inject paper.pdf --title 'My Research' --question 'Does X work?'  # Papers + proposal\n"
            "  %(prog)s create --title 'SVM vs LR' --question 'Which is better?'\n"
            "  %(prog)s run autonomous --max-ticks 50\n"
            "  %(prog)s status\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    # create
    p_create = subparsers.add_parser("create", help="Create a research proposal")
    p_create.add_argument("--id", help="Pipeline ID (auto-generated from title if omitted)")
    p_create.add_argument("--title", required=True, help="Research title")
    p_create.add_argument("--question", required=True, help="Research question")
    p_create.add_argument("--budget", type=int, default=20000, help="Budget in credits (default 20000)")
    p_create.add_argument("--refs", nargs="*", default=[], help="Reference paper files or identifiers")

    # run
    p_run = subparsers.add_parser("run", help="Run the research scheduler")
    p_run.add_argument("mode", choices=["simulate", "autonomous", "interactive"], help="Execution mode")
    p_run.add_argument("--max-ticks", type=int, default=50, help="Maximum scheduler ticks (default 50)")
    p_run.add_argument("--interval", type=int, default=5, help="Seconds between ticks (default 5)")
    p_run.add_argument("--backend", choices=["auto", "openharness", "anthropic"], default="auto")
    p_run.add_argument("--human-review", action="store_true",
                       help="Professor LLM writes recommendation report; human makes final decision")
    p_run.add_argument("--max-milestones", type=int, default=3,
                       help="Max milestone cycles before paper stage (default 3)")

    # status
    subparsers.add_parser("status", help="Show pipeline status")

    # recover
    p_recover = subparsers.add_parser("recover", help="Recover a failed pipeline")
    p_recover.add_argument("--pipeline", required=True, help="Pipeline ID to recover")
    p_recover.add_argument("--rollback", action="store_true", help="Rollback to last checkpoint")

    # dashboard
    p_dash = subparsers.add_parser("dashboard", help="Visual pipeline dashboard with priority control")
    p_dash.add_argument("--priority", nargs=2, metavar=("PIPELINE", "LEVEL"),
                        help="Set priority (0=low, 1=normal, 2=high, 3=urgent)")
    p_dash.add_argument("--pause-miner", action="store_true", help="Pause new topic discovery")
    p_dash.add_argument("--resume-miner", action="store_true", help="Resume topic discovery")

    # publish
    p_publish = subparsers.add_parser("publish", help="Package completed pipeline for publication")
    p_publish.add_argument("--pipeline", required=True, help="Pipeline ID to package")

    # fix
    p_fix = subparsers.add_parser("fix", help="Auto-fix pipeline based on professor review report")
    p_fix.add_argument("--pipeline", required=True, help="Pipeline ID to fix")
    p_fix.add_argument("--no-resume", action="store_true", help="Don't auto-approve after fix")

    # inject
    p_inject = subparsers.add_parser("inject", help="Inject papers (+ optional proposal) into the system")
    p_inject.add_argument("papers", nargs="+", help="Paper files (PDF, MD, TXT) to import")
    p_inject.add_argument("--title", help="Proposal title (creates pipeline directly, skips Miner)")
    p_inject.add_argument("--question", help="Research question (required with --title)")
    p_inject.add_argument("--id", help="Pipeline ID (auto-generated from title if omitted)")
    p_inject.add_argument("--budget", type=int, default=20000, help="Budget in credits (default 20000)")

    # candidates
    p_cand = subparsers.add_parser("candidates", help="View/select/reject candidate topics")
    p_cand_sub = p_cand.add_subparsers(dest="action")
    p_cand_list = p_cand_sub.add_parser("list", help="List pending candidates")
    p_cand_list.add_argument("--verbose", "-v", action="store_true", help="Show details")
    p_cand_select = p_cand_sub.add_parser("select", help="Select a candidate for pipeline")
    p_cand_select.add_argument("index", type=int, help="Candidate number (from 'list')")
    p_cand_select.add_argument("--feedback", "-f", help="Why you selected this topic")
    p_cand_reject = p_cand_sub.add_parser("reject", help="Reject a candidate")
    p_cand_reject.add_argument("index", type=int, help="Candidate number (from 'list')")
    p_cand_reject.add_argument("--feedback", "-f", help="Why you rejected this topic")
    p_cand_brief = p_cand_sub.add_parser("brief", help="Generate detailed brief for a candidate")
    p_cand_brief.add_argument("index", type=int, help="Candidate number (from 'list')")

    # review
    p_review = subparsers.add_parser("review", help="Review pipeline after FAST_FAIL (approve/tweak/kill)")
    p_review.add_argument("--pipeline", required=True, help="Pipeline ID")
    p_review.add_argument("--approve", action="store_true", help="Approve → enter MAIN stage")
    p_review.add_argument("--tweak", metavar="DIRECTIVE", help="Tweak → re-run DESIGN with directive")
    p_review.add_argument("--kill", nargs="?", const=True, help="Kill pipeline (optional reason)")

    # query
    p_query = subparsers.add_parser("query", help="Ask a question about a pipeline")
    p_query.add_argument("--pipeline", required=True, help="Pipeline ID")
    p_query.add_argument("question", help="Your question about the experiment")

    # tweak
    p_tweak = subparsers.add_parser("tweak", help="Inject a directive into a pipeline")
    p_tweak.add_argument("--pipeline", required=True, help="Pipeline ID")
    p_tweak.add_argument("directive", help="Instruction for the next experiment cycle")

    # explore
    p_explore = subparsers.add_parser("explore", help="Show pipeline artifacts and metrics")
    p_explore.add_argument("--pipeline", required=True, help="Pipeline ID")

    # launch
    p_launch = subparsers.add_parser("launch", help="One-click launch: heartbeat + scheduler")
    p_launch.add_argument("--mode", choices=["simulate", "autonomous", "interactive"], default="autonomous")
    p_launch.add_argument("--max-ticks", type=int, default=50, help="Maximum scheduler ticks")
    p_launch.add_argument("--interval", type=int, default=5, help="Seconds between ticks")
    p_launch.add_argument("--human-review", action="store_true", help="Professor writes report, you decide")
    p_launch.add_argument("--max-milestones", type=int, default=3, help="Milestones before paper stage")
    p_launch.add_argument("--enable-heartbeat", action="store_true", help="Enable heartbeat before launch")
    p_launch.add_argument("--auto-select", action="store_true",
                          help="Auto-select topics (default: present candidates for user decision)")

    # trial
    p_trial = subparsers.add_parser("trial", help="Trial: heartbeat once + one topic + one pipeline, then stop")
    p_trial.add_argument("--mode", choices=["simulate", "autonomous", "interactive"], default="autonomous")
    p_trial.add_argument("--max-ticks", type=int, default=500, help="Max scheduler ticks (default 500)")
    p_trial.add_argument("--interval", type=int, default=5, help="Seconds between ticks")
    p_trial.add_argument("--human-review", action="store_true", help="Professor writes report, you decide")
    p_trial.add_argument("--max-milestones", type=int, default=3, help="Milestones before paper stage")
    p_trial.add_argument("--enable-heartbeat", action="store_true", help="Enable heartbeat for this run")
    p_trial.add_argument("--auto-select", action="store_true",
                         help="Auto-select topics (default: present candidates for user decision)")

    # run also gets --auto-select
    p_run.add_argument("--auto-select", action="store_true",
                       help="Auto-select topics (default: present candidates for user decision)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    def cmd_dashboard(a):
        """Delegate to dashboard.py."""
        import subprocess as _sp
        cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "dashboard.py")]
        if a.priority:
            cmd += ["--priority", a.priority[0], a.priority[1]]
        if getattr(a, "pause_miner", False):
            cmd.append("--pause-miner")
        if getattr(a, "resume_miner", False):
            cmd.append("--resume-miner")
        _sp.run(cmd)

    dispatch = {
        "create": cmd_create,
        "run": cmd_run,
        "status": cmd_status,
        "recover": cmd_recover,
        "dashboard": cmd_dashboard,
        "publish": cmd_publish,
        "fix": cmd_fix,
        "inject": cmd_inject,
        "launch": cmd_launch,
        "trial": cmd_trial,
        "candidates": cmd_candidates,
        "review": cmd_review,
        "query": cmd_query,
        "tweak": cmd_tweak,
        "explore": cmd_explore,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
