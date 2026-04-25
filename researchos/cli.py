"""Platform-neutral CLI for ResearchOS.

All six user-facing modes the README documents:

    researchos launch                          # 默认：heartbeat → miner → candidates → user select → pipeline → paper
    researchos launch --auto-select            # 全自主
    researchos trial  [--mode simulate]        # 试运行：一次心跳一个课题一个 pipeline
    researchos inject  paper1.pdf [paper2.pdf] # PDF → wiki → miner 发现 → 候选
    researchos inject  paper.pdf --title "..." --question "..."   # PDF → wiki + 直接建 pipeline
    researchos create  --title "..." --question "..." [--refs ref.pdf]  # 手动提案

Plus the post-hoc control verbs:

    researchos state           <pipeline_id>
    researchos kill            <pipeline_id> [--reason ...]
    researchos tweak           <pipeline_id> --directive "..."
    researchos candidates      list | select <n> | reject <n>

Runtime backend is chosen via ``RESEARCHOS_RUNTIME=sdk|openharness|skill``
(default ``sdk``). API key / base URL / model come from env:
``RESEARCHOS_API_KEY`` / ``RESEARCHOS_BASE_URL`` / ``RESEARCHOS_MODEL``.
A ``~/.env`` file (and/or ``<project_root>/.env``) is auto-loaded on import
so you don't have to ``export`` them every time.

Most commands delegate to the canonical implementations in
``scripts.run_research``, which is the single source of truth for verb
behaviour. This file is just argparse + arg translation + dotenv loading.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("researchos.cli")

# ---------------------------------------------------------------------------
# .env auto-loading — runs at import time so every entry point benefits.
# Precedence: shell env > project <root>/.env > ~/.env.
# ---------------------------------------------------------------------------

def _parse_dotenv(path: Path) -> dict[str, str]:
    """Tiny dotenv parser — handles ``KEY=value``, ``KEY="quoted"``,
    ``export KEY=value``, comments, and blank lines. No shell expansion."""
    out: dict[str, str] = {}
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
                v = v[1:-1]
            out[k] = v
    except OSError:
        pass
    return out


def _load_dotenv_files() -> None:
    """Merge ~/.env and <project_root>/.env into ``os.environ`` without
    clobbering values already present (shell export always wins)."""
    sources: list[Path] = []
    home_env = Path.home() / ".env"
    if home_env.exists():
        sources.append(home_env)
    proj_root = Path(os.environ.get("RESEARCHOS_PROJECT_ROOT", Path.cwd())).resolve()
    proj_env = proj_root / ".env"
    if proj_env.exists() and proj_env != home_env:
        sources.append(proj_env)
    for path in sources:
        for k, v in _parse_dotenv(path).items():
            os.environ.setdefault(k, v)


_load_dotenv_files()


# ---------------------------------------------------------------------------
# Runtime bootstrap
# ---------------------------------------------------------------------------

def _bootstrap_runtime() -> None:
    """Import the runtime named in ``RESEARCHOS_RUNTIME`` so it self-registers."""
    name = os.environ.get("RESEARCHOS_RUNTIME", "sdk").lower()
    if name == "sdk":
        import researchos.agent_runtime.sdk           # noqa: F401
    elif name == "openharness":
        from adapters.openharness import runtime     # noqa: F401
    elif name == "mcp":
        from adapters.mcp import server              # noqa: F401
    elif name == "skill":
        from adapters.skill import runtime           # noqa: F401
    else:
        raise SystemExit(
            f"Unknown RESEARCHOS_RUNTIME={name!r}. "
            f"Choose sdk | openharness | mcp | skill."
        )


def _project_root() -> Path:
    return Path(os.environ.get("RESEARCHOS_PROJECT_ROOT", Path.cwd())).resolve()


# ---------------------------------------------------------------------------
# Delegation helpers — call the canonical cmd_* in scripts.run_research
# so verb behaviour stays consistent whether invoked via OpenHarness or
# this CLI or MCP.
# ---------------------------------------------------------------------------

def _rr_module():
    """Lazy-import scripts.run_research (heavy transitive deps)."""
    from scripts import run_research   # pylint: disable=import-outside-toplevel
    return run_research


def _delegate(cmd_name: str, ns: argparse.Namespace) -> int:
    """Call ``scripts.run_research.cmd_<name>`` and convert its SystemExit /
    None return into an int exit code."""
    rr = _rr_module()
    fn = getattr(rr, f"cmd_{cmd_name}")
    try:
        rc = fn(ns)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    return int(rc or 0)


# ---------------------------------------------------------------------------
# Commands — lifecycle verbs
# ---------------------------------------------------------------------------

def cmd_launch(args: argparse.Namespace) -> int:
    return _delegate("launch", args)


def cmd_trial(args: argparse.Namespace) -> int:
    return _delegate("trial", args)


def cmd_create(args: argparse.Namespace) -> int:
    return _delegate("create", args)


def cmd_inject(args: argparse.Namespace) -> int:
    return _delegate("inject", args)


def cmd_candidates(args: argparse.Namespace) -> int:
    return _delegate("candidates", args)


# ---------------------------------------------------------------------------
# Commands — control verbs (reuse local lightweight implementations)
# ---------------------------------------------------------------------------

def cmd_state(args: argparse.Namespace) -> int:
    from researchos.runtime.state_manager import StateManager
    sm = StateManager(project_root=_project_root())
    state = sm.load_pipeline(args.pipeline_id)
    if state is None:
        print(f"No pipeline found for id={args.pipeline_id!r}", file=sys.stderr)
        return 1
    print(state.model_dump_json(indent=2))
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    from researchos.runtime.state_manager import StateManager
    from researchos.schemas.pipeline import PipelineStage
    sm = StateManager(project_root=_project_root())
    state = sm.load_pipeline(args.pipeline_id)
    if state is None:
        print(f"No pipeline found for id={args.pipeline_id!r}", file=sys.stderr)
        return 1
    state.stage = PipelineStage.FAILED
    state.last_error = f"killed_by_cli: {args.reason or 'user-requested'}"
    sm.save_pipeline(state, enforce_contracts=False)
    print(f"Pipeline {args.pipeline_id} → FAILED")
    return 0


def cmd_lineage(args: argparse.Namespace) -> int:
    """Render the lineage graph for a pipeline (json | mermaid | summary)."""
    import json
    from researchos.runtime.lineage import build_pipeline_lineage, to_mermaid
    pdir = _project_root() / "artifacts" / f"pipeline_{args.pipeline_id}"
    if not pdir.exists():
        print(f"No pipeline dir: {pdir}", file=sys.stderr)
        return 1
    lineage = build_pipeline_lineage(pdir, _project_root())

    if args.format == "mermaid":
        print(to_mermaid(lineage))
        return 0
    if args.format == "json":
        print(json.dumps(lineage.model_dump(), indent=2))
        return 0
    # summary: human-readable counts + filterable node listing
    from collections import Counter
    print(f"Pipeline: {lineage.pipeline_id}")
    print(f"Generated: {lineage.generated_at}")
    print(f"Nodes: {len(lineage.nodes)}  Edges: {len(lineage.edges)}")
    print()
    kinds = Counter(n.kind for n in lineage.nodes)
    print("Node counts by kind:")
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} {v}")
    print()
    rels = Counter(e.rel for e in lineage.edges)
    print("Edge counts by relation:")
    for r, v in sorted(rels.items(), key=lambda x: -x[1]):
        print(f"  {r:20s} {v}")
    if args.kind:
        print()
        print(f"Nodes of kind={args.kind!r}:")
        for n in lineage.nodes:
            if n.kind == args.kind:
                print(f"  {n.id}  →  {n.label}")
                for k, v in sorted(n.attrs.items()):
                    if isinstance(v, (str, int, float, bool)) and v != "":
                        print(f"      {k}: {v}")
    if args.diagnostics:
        print()
        print("Extraction log:")
        for line in lineage.extraction_log:
            print(f"  · {line}")
    return 0


def cmd_tweak(args: argparse.Namespace) -> int:
    """Local implementation — appends to user_directives.yaml without
    needing the whole scheduler stack."""
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
# argparse
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="researchos",
        description="Platform-neutral CLI for ResearchOS (6 modes + control verbs).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # launch — default mode: heartbeat + candidates + user select + pipeline
    launch = sub.add_parser(
        "launch",
        help="Autonomous loop: heartbeat → miner → candidates → user select → pipeline",
    )
    launch.add_argument("--mode", choices=("simulate", "autonomous", "interactive"),
                        default="autonomous")
    launch.add_argument("--max-ticks", type=int, default=50)
    launch.add_argument("--interval", type=int, default=5)
    launch.add_argument("--human-review", action="store_true")
    launch.add_argument("--max-milestones", type=int, default=3)
    launch.add_argument("--enable-heartbeat", action="store_true")
    launch.add_argument("--auto-select", action="store_true",
                        help="Auto-select topics (default: present candidates for user).")
    launch.set_defaults(func=cmd_launch)

    # trial — single pipeline then stop
    trial = sub.add_parser(
        "trial",
        help="Heartbeat once + one topic + one pipeline, then stop",
    )
    trial.add_argument("--mode", choices=("simulate", "autonomous", "interactive"),
                       default="autonomous")
    trial.add_argument("--max-ticks", type=int, default=500)
    trial.add_argument("--interval", type=int, default=5)
    trial.set_defaults(func=cmd_trial)

    # inject — register PDFs; optional proposal shortcut
    inject = sub.add_parser(
        "inject",
        help=(
            "Inject evidence into ResearchOS. "
            "Preferred (typed mode): --base-paper + --proposal + --title + --question. "
            "Legacy (deprecated): positional 'papers' argument."
        ),
    )
    inject.add_argument(
        "papers", nargs="*", default=[],
        help=(
            "[DEPRECATED] Paper files (PDF / MD / TXT). "
            "Use --base-paper / --proposal / --ref-code for typed roles instead."
        ),
    )
    inject.add_argument(
        "--base-paper",
        help=(
            "Path to the base paper PDF or markdown file (typed mode). "
            "This is the PREFERRED way to inject a paper. "
            "The file is compiled to wiki AND saved to the pipeline directory as base_paper.md."
        ),
    )
    inject.add_argument(
        "--proposal",
        help=(
            "Path to the user proposal markdown/text file (typed mode). "
            "Written to the pipeline directory as proposal.md. NOT compiled to wiki."
        ),
    )
    inject.add_argument(
        "--ref-code",
        help=(
            "Git URL of the reference paper's open-source code (typed mode). "
            "Recorded for later shallow-clone by CODE_RECON; not cloned here."
        ),
    )
    inject.add_argument(
        "--related-work", action="append", default=[],
        help=(
            "Optional related-work file path; can be repeated. "
            "Read by HYPOTHESIS when in scope."
        ),
    )
    inject.add_argument("--title", help="Proposal title (required in typed mode; skips miner)")
    inject.add_argument("--question", help="Research question (required with --title)")
    inject.add_argument("--id", help="Pipeline ID (auto from title if omitted)")
    inject.add_argument("--budget", type=int, default=20000)
    inject.set_defaults(func=cmd_inject)

    # create — pure manual proposal
    create = sub.add_parser("create", help="Create a pipeline proposal by hand")
    create.add_argument("--id")
    create.add_argument("--title", required=True)
    create.add_argument("--question", required=True)
    create.add_argument("--budget", type=int, default=20000)
    create.add_argument("--refs", nargs="*", default=[])
    create.set_defaults(func=cmd_create)

    # candidates — list / select / reject / brief (delegated)
    cand = sub.add_parser("candidates", help="View/select/reject candidate topics")
    cand_sub = cand.add_subparsers(dest="action", required=True)
    cl = cand_sub.add_parser("list")
    cl.add_argument("--verbose", "-v", action="store_true")
    cs = cand_sub.add_parser("select")
    cs.add_argument("index", type=int)
    cs.add_argument("--feedback", "-f")
    cr = cand_sub.add_parser("reject")
    cr.add_argument("index", type=int)
    cr.add_argument("--feedback", "-f")
    cb = cand_sub.add_parser("brief")
    cb.add_argument("index", type=int)
    cand.set_defaults(func=cmd_candidates)

    # state / kill / tweak — local control verbs
    state = sub.add_parser("state", help="Dump pipeline state as JSON")
    state.add_argument("pipeline_id")
    state.set_defaults(func=cmd_state)

    kill = sub.add_parser("kill", help="Force pipeline to FAILED")
    kill.add_argument("pipeline_id")
    kill.add_argument("--reason", default=None)
    kill.set_defaults(func=cmd_kill)

    tweak = sub.add_parser("tweak", help="Append a directive to user_directives.yaml")
    tweak.add_argument("pipeline_id")
    tweak.add_argument("--directive", required=True)
    tweak.set_defaults(func=cmd_tweak)

    lineage = sub.add_parser(
        "lineage",
        help="Render the pipeline's lineage graph (joins paper/code/data/concept/decision)",
    )
    lineage.add_argument("pipeline_id")
    lineage.add_argument(
        "--format", choices=("summary", "json", "mermaid"), default="summary",
        help="Output format. summary = counts + optional --kind listing.",
    )
    lineage.add_argument(
        "--kind", default=None,
        help="When --format=summary, list every node of this kind "
             "(paper / code_repo / dataset_file / concept / step / decision …)",
    )
    lineage.add_argument(
        "--diagnostics", action="store_true",
        help="Print per-extractor diagnostics (helpful when edges are missing).",
    )
    lineage.set_defaults(func=cmd_lineage)

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


if __name__ == "__main__":
    raise SystemExit(main())
