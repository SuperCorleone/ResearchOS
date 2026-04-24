"""Platform-neutral CLI entry point for ResearchOS.

Where ``scripts/run_research.py`` carries the OpenHarness-specific command
set (inject / review / candidates / ...), this module exposes the minimal
platform-neutral verbs that every adapter must implement:

    researchos launch [--auto-select] [--simulate]
    researchos trial  [--mode simulate]
    researchos state  <pipeline_id>
    researchos kill   <pipeline_id>
    researchos tweak  <pipeline_id> --directive "..."

It drives the ResearchOS scheduler via whichever ``AgentRuntime`` is
registered — default is ``sdk`` (OpenAI-compatible SDK). Override with
``RESEARCHOS_RUNTIME=openharness|mcp|skill``.

The larger ``scripts/run_research.py`` CLI remains the source of truth for
the OpenHarness workflow; this file intentionally stays small so other
hosts (MCP client, Claude Skill) can import the same verbs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("researchos.cli")


def _bootstrap_runtime() -> None:
    """Import the runtime selected by ``RESEARCHOS_RUNTIME`` so it registers itself."""
    name = os.environ.get("RESEARCHOS_RUNTIME", "sdk").lower()
    if name == "sdk":
        import researchos.agent_runtime.sdk        # noqa: F401 — side-effect import
    elif name == "openharness":
        # adapters.openharness.runtime registers "openharness"
        from adapters.openharness import runtime  # noqa: F401
    elif name == "mcp":
        from adapters.mcp import server           # noqa: F401
    elif name == "skill":
        from adapters.skill import runtime        # noqa: F401
    else:
        raise SystemExit(
            f"Unknown RESEARCHOS_RUNTIME={name!r}. "
            f"Choose sdk | openharness | mcp | skill."
        )


def _project_root() -> Path:
    """Honour ``RESEARCHOS_PROJECT_ROOT`` or fall back to CWD."""
    return Path(os.environ.get("RESEARCHOS_PROJECT_ROOT", Path.cwd())).resolve()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_launch(args: argparse.Namespace) -> int:
    """Full autonomous loop: heartbeat → miner → scheduler → paper."""
    from researchos.runtime.scheduler import Scheduler
    root = _project_root()
    scheduler = Scheduler(root, simulate=args.simulate, auto_professor=args.auto_select)
    try:
        asyncio.run(scheduler.run_until_idle())
    except KeyboardInterrupt:
        print("^C received; scheduler state persisted.", file=sys.stderr)
    return 0


def cmd_trial(args: argparse.Namespace) -> int:
    """Single-pipeline trial — heartbeat + miner + one pipeline, then stop."""
    from researchos.runtime.scheduler import Scheduler
    root = _project_root()
    scheduler = Scheduler(root, simulate=(args.mode == "simulate"))
    asyncio.run(scheduler.run_trial())
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    """Dump the typed state for one pipeline."""
    from researchos.runtime.state_manager import StateManager
    sm = StateManager(project_root=_project_root())
    state = sm.load_pipeline(args.pipeline_id)
    if state is None:
        print(f"No pipeline found for id={args.pipeline_id!r}", file=sys.stderr)
        return 1
    print(state.model_dump_json(indent=2))
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    """Write a STOP decision for the given pipeline and mark state=FAILED."""
    from researchos.runtime.state_manager import StateManager
    from researchos.schemas.pipeline import PipelineStage
    sm = StateManager(project_root=_project_root())
    state = sm.load_pipeline(args.pipeline_id)
    if state is None:
        print(f"No pipeline found for id={args.pipeline_id!r}", file=sys.stderr)
        return 1
    state.stage = PipelineStage.FAILED
    state.last_error = f"killed_by_cli: {args.reason or 'user-requested'}"
    # enforce_contracts=False: terminal transition is explicit user action.
    sm.save_pipeline(state, enforce_contracts=False)
    print(f"Pipeline {args.pipeline_id} → FAILED")
    return 0


def cmd_tweak(args: argparse.Namespace) -> int:
    """Append a user directive to the pipeline's ``user_directives.yaml``."""
    from datetime import datetime, timezone
    import yaml
    pdir = _project_root() / "artifacts" / f"pipeline_{args.pipeline_id}"
    if not pdir.exists():
        print(f"No pipeline dir: {pdir}", file=sys.stderr)
        return 1
    path = pdir / "user_directives.yaml"
    existing = yaml.safe_load(path.read_text()) if path.exists() else []
    if not isinstance(existing, list):
        existing = []
    existing.append({
        "directive": args.directive,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "applied": False,
    })
    path.write_text(yaml.dump(existing, default_flow_style=False))
    print(f"Appended directive to {path.relative_to(_project_root())}")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="researchos",
        description="Platform-neutral CLI for ResearchOS.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    launch = sub.add_parser("launch", help="Run the autonomous scheduler loop")
    launch.add_argument("--auto-select", action="store_true")
    launch.add_argument("--simulate", action="store_true")
    launch.set_defaults(func=cmd_launch)

    trial = sub.add_parser("trial", help="Run one pipeline end-to-end and stop")
    trial.add_argument("--mode", choices=("real", "simulate"), default="real")
    trial.set_defaults(func=cmd_trial)

    state = sub.add_parser("state", help="Dump typed pipeline state as JSON")
    state.add_argument("pipeline_id")
    state.set_defaults(func=cmd_state)

    kill = sub.add_parser("kill", help="Transition pipeline to FAILED")
    kill.add_argument("pipeline_id")
    kill.add_argument("--reason", default=None)
    kill.set_defaults(func=cmd_kill)

    tweak = sub.add_parser("tweak", help="Append a user directive for the next step")
    tweak.add_argument("pipeline_id")
    tweak.add_argument("--directive", required=True)
    tweak.set_defaults(func=cmd_tweak)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("RESEARCHOS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    _bootstrap_runtime()
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":          # pragma: no cover
    raise SystemExit(main())
