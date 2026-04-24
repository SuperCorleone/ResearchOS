"""MCP server — expose ResearchOS verbs as Model-Context-Protocol tools.

Lets any MCP 1.x host (OpenCode, Claude Desktop, Cursor, …) drive
ResearchOS over the standard MCP tool-call surface. All six user-facing
lifecycle modes from the README plus the post-hoc control verbs:

- ``researchos_launch``             — heartbeat → miner → candidates
- ``researchos_trial``              — single pipeline then stop
- ``researchos_inject``             — PDF(s) → wiki → miner OR direct proposal
- ``researchos_create``             — manual proposal (no PDF)
- ``researchos_candidates_list``    — list pending candidate topics
- ``researchos_candidates_select``  — pick a candidate into a pipeline
- ``researchos_candidates_reject``  — reject a candidate
- ``researchos_get_state``          — typed PipelineState JSON
- ``researchos_inject_directive``   — append directive to user_directives.yaml
- ``researchos_kill``               — stage → FAILED

Uses ``researchos.agent_runtime.sdk.SDKRuntime`` by default; override via
``RESEARCHOS_RUNTIME=openharness|skill``. On import this module loads
``~/.env`` and ``<project_root>/.env`` (precedence: shell > project > home)
so opencode users don't have to put secrets in ``opencode.json``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger("researchos.mcp")


# ---------------------------------------------------------------------------
# .env auto-loading — same rules as researchos.cli._load_dotenv_files.
# ---------------------------------------------------------------------------

def _parse_dotenv(path: Path) -> dict[str, str]:
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
            k = k.strip(); v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
                v = v[1:-1]
            out[k] = v
    except OSError:
        pass
    return out


def _load_dotenv_files() -> None:
    """Merge .env files into os.environ; shell env always wins."""
    home_env = Path.home() / ".env"
    proj_root = Path(os.environ.get("RESEARCHOS_PROJECT_ROOT", Path.cwd())).resolve()
    proj_env = proj_root / ".env"
    for path in (home_env, proj_env):
        if path.exists() and path != (home_env if path == proj_env else proj_env):
            for k, v in _parse_dotenv(path).items():
                os.environ.setdefault(k, v)
        elif path.exists():
            for k, v in _parse_dotenv(path).items():
                os.environ.setdefault(k, v)


_load_dotenv_files()

# ---------------------------------------------------------------------------
# Tool descriptors (MCP-style JSON-Schema). These are the single source of
# truth for the tool contract — both ``adapters/mcp/manifest.json`` and the
# live server handler read from this list.
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    # ── lifecycle ────────────────────────────────────────────────
    {
        "name": "researchos_launch",
        "description": (
            "Start the autonomous ResearchOS loop: heartbeat → miner → "
            "candidates → (user select, unless auto_select=true) → pipeline "
            "→ milestones → paper. Use auto_select=true for full autonomy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "auto_select": {
                    "type": "boolean", "default": False,
                    "description": "Skip user selection — miner picks automatically.",
                },
                "mode": {
                    "type": "string", "enum": ["simulate", "autonomous", "interactive"],
                    "default": "autonomous",
                },
                "max_ticks":      {"type": "integer", "default": 50},
                "max_milestones": {"type": "integer", "default": 3},
            },
        },
    },
    {
        "name": "researchos_trial",
        "description": (
            "Heartbeat once + miner once + exactly one pipeline end-to-end, "
            "then stop. Typical smoke test / single-topic run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string", "enum": ["simulate", "autonomous", "interactive"],
                    "default": "autonomous",
                },
                "max_ticks": {"type": "integer", "default": 500},
            },
        },
    },
    {
        "name": "researchos_inject",
        "description": (
            "Inject evidence into ResearchOS. "
            "Typed mode (preferred): supply base_paper + title + question. "
            "The base paper is compiled to wiki AND saved to the pipeline dir. "
            "proposal is saved to the pipeline dir only (NOT wiki). "
            "ref_code URL is recorded for CODE_RECON (not cloned here). "
            "Legacy mode (deprecated): supply papers array; miner will surface "
            "candidates (Path A) or pipeline created directly with title+question (Path B)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "base_paper": {"type": "string",
                               "description": "Path to the base paper PDF or markdown (typed mode, preferred)."},
                "proposal":   {"type": "string",
                               "description": "Path to the user proposal file (typed mode). NOT compiled to wiki."},
                "ref_code":   {"type": "string",
                               "description": "Git URL of the reference code repo (typed mode). Recorded, not cloned."},
                "related_work": {"type": "array", "items": {"type": "string"},
                                 "description": "Optional related-work file paths (typed mode); can repeat."},
                "papers":     {"type": "array", "items": {"type": "string"},
                               "description": "[DEPRECATED] Paths to PDF/MD/TXT files. Use base_paper instead."},
                "title":      {"type": "string",
                               "description": "Pipeline title. Required in typed mode."},
                "question":   {"type": "string",
                               "description": "Research question. Required with title."},
                "id":         {"type": "string"},
                "budget":     {"type": "integer", "default": 20000},
            },
        },
    },
    {
        "name": "researchos_create",
        "description": (
            "Create a pipeline proposal by hand — no PDF injection. For when "
            "the research question is known a priori."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title", "question"],
            "properties": {
                "title":    {"type": "string"},
                "question": {"type": "string"},
                "id":       {"type": "string"},
                "budget":   {"type": "integer", "default": 20000},
                "refs":     {"type": "array", "items": {"type": "string"},
                             "description": "Evidence reference identifiers."},
            },
        },
    },
    # ── candidate triage ────────────────────────────────────────
    {
        "name": "researchos_candidates_list",
        "description": (
            "List pending candidate topics surfaced by the miner. Use this "
            "then researchos_candidates_select to approve one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "verbose": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "researchos_candidates_select",
        "description": "Select a candidate topic (by 1-based index) and start its pipeline.",
        "inputSchema": {
            "type": "object",
            "required": ["index"],
            "properties": {
                "index":    {"type": "integer"},
                "feedback": {"type": "string"},
            },
        },
    },
    {
        "name": "researchos_candidates_reject",
        "description": "Reject a candidate topic (by 1-based index).",
        "inputSchema": {
            "type": "object",
            "required": ["index"],
            "properties": {
                "index":    {"type": "integer"},
                "feedback": {"type": "string"},
            },
        },
    },
    # ── control / introspection ────────────────────────────────
    {
        "name": "researchos_get_state",
        "description": "Return the typed PipelineState as JSON.",
        "inputSchema": {
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {"pipeline_id": {"type": "string"}},
        },
    },
    {
        "name": "researchos_inject_directive",
        "description": (
            "Append a user directive to a pipeline's user_directives.yaml. "
            "The next DESIGN/CODE/RUN step's prompt will include it."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["pipeline_id", "directive"],
            "properties": {
                "pipeline_id": {"type": "string"},
                "directive":   {"type": "string"},
            },
        },
    },
    {
        "name": "researchos_kill",
        "description": "Transition a pipeline to stage=FAILED immediately.",
        "inputSchema": {
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "pipeline_id": {"type": "string"},
                "reason":      {"type": "string"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _project_root() -> Path:
    return Path(os.environ.get("RESEARCHOS_PROJECT_ROOT", Path.cwd())).resolve()


# -- canonical CLI delegation ---------------------------------------------
#
# launch / trial / inject / create / candidates_* share their behaviour with
# scripts.run_research's cmd_*. We build a minimal argparse.Namespace from
# the MCP tool arguments, capture stdout, and return the lines as the tool
# response body. This keeps one source of truth.

def _rr_module():
    from scripts import run_research
    return run_research


def _capture_stdout(fn, ns: SimpleNamespace) -> str:
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            fn(ns)
        except SystemExit as exc:
            buf.write(f"\n[exit_code={exc.code}]")
    return buf.getvalue()


async def _handle_launch(args: dict[str, Any]) -> str:
    rr = _rr_module()
    ns = SimpleNamespace(
        mode=args.get("mode", "autonomous"),
        max_ticks=int(args.get("max_ticks", 50)),
        interval=5,
        human_review=False,
        max_milestones=int(args.get("max_milestones", 3)),
        enable_heartbeat=False,
        auto_select=bool(args.get("auto_select", False)),
    )
    body = _capture_stdout(rr.cmd_launch, ns)
    return json.dumps({"status": "done", "output": body})


async def _handle_trial(args: dict[str, Any]) -> str:
    rr = _rr_module()
    ns = SimpleNamespace(
        mode=args.get("mode", "autonomous"),
        max_ticks=int(args.get("max_ticks", 500)),
        interval=5,
    )
    body = _capture_stdout(rr.cmd_trial, ns)
    return json.dumps({"status": "trial_complete", "output": body})


async def _handle_inject(args: dict[str, Any]) -> str:
    rr = _rr_module()
    ns = SimpleNamespace(
        papers=list(args.get("papers", [])),
        base_paper=args.get("base_paper"),
        proposal=args.get("proposal"),
        ref_code=args.get("ref_code"),
        related_work=list(args.get("related_work", [])),
        title=args.get("title"),
        question=args.get("question"),
        id=args.get("id"),
        budget=int(args.get("budget", 20000)),
    )
    body = _capture_stdout(rr.cmd_inject, ns)
    return json.dumps({"status": "done", "output": body})


async def _handle_create(args: dict[str, Any]) -> str:
    rr = _rr_module()
    ns = SimpleNamespace(
        id=args.get("id"),
        title=args["title"],
        question=args["question"],
        budget=int(args.get("budget", 20000)),
        refs=list(args.get("refs", [])),
    )
    body = _capture_stdout(rr.cmd_create, ns)
    return json.dumps({"status": "done", "output": body})


async def _handle_candidates_list(args: dict[str, Any]) -> str:
    rr = _rr_module()
    ns = SimpleNamespace(action="list", verbose=bool(args.get("verbose", False)))
    body = _capture_stdout(rr.cmd_candidates, ns)
    return json.dumps({"status": "done", "output": body})


async def _handle_candidates_select(args: dict[str, Any]) -> str:
    rr = _rr_module()
    ns = SimpleNamespace(
        action="select", index=int(args["index"]),
        feedback=args.get("feedback"),
    )
    body = _capture_stdout(rr.cmd_candidates, ns)
    return json.dumps({"status": "done", "output": body})


async def _handle_candidates_reject(args: dict[str, Any]) -> str:
    rr = _rr_module()
    ns = SimpleNamespace(
        action="reject", index=int(args["index"]),
        feedback=args.get("feedback"),
    )
    body = _capture_stdout(rr.cmd_candidates, ns)
    return json.dumps({"status": "done", "output": body})


async def _handle_get_state(args: dict[str, Any]) -> str:
    from researchos.runtime.state_manager import StateManager
    state = StateManager(project_root=_project_root()).load_pipeline(args["pipeline_id"])
    if state is None:
        return json.dumps({"error": "pipeline_not_found", "pipeline_id": args["pipeline_id"]})
    return state.model_dump_json()


async def _handle_inject_directive(args: dict[str, Any]) -> str:
    from datetime import datetime, timezone
    import yaml
    pdir = _project_root() / "artifacts" / f"pipeline_{args['pipeline_id']}"
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / "user_directives.yaml"
    existing = yaml.safe_load(path.read_text()) if path.exists() else []
    if not isinstance(existing, list):
        existing = []
    existing.append({
        "directive": args["directive"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "applied": False,
    })
    path.write_text(yaml.dump(existing, default_flow_style=False))
    return json.dumps({"status": "appended", "path": str(path.relative_to(_project_root()))})


async def _handle_kill(args: dict[str, Any]) -> str:
    from researchos.runtime.state_manager import StateManager
    from researchos.schemas.pipeline import PipelineStage
    sm = StateManager(project_root=_project_root())
    state = sm.load_pipeline(args["pipeline_id"])
    if state is None:
        return json.dumps({"error": "pipeline_not_found"})
    state.stage = PipelineStage.FAILED
    state.last_error = f"killed_via_mcp: {args.get('reason') or 'user'}"
    sm.save_pipeline(state, enforce_contracts=False)
    return json.dumps({"status": "killed", "stage": state.stage.value})


_HANDLERS = {
    "researchos_launch":                _handle_launch,
    "researchos_trial":                 _handle_trial,
    "researchos_inject":                _handle_inject,
    "researchos_create":                _handle_create,
    "researchos_candidates_list":       _handle_candidates_list,
    "researchos_candidates_select":     _handle_candidates_select,
    "researchos_candidates_reject":     _handle_candidates_reject,
    "researchos_get_state":             _handle_get_state,
    "researchos_inject_directive":      _handle_inject_directive,
    "researchos_kill":                  _handle_kill,
}


async def dispatch(name: str, arguments: dict[str, Any]) -> str:
    """Generic MCP tool dispatch. Hosts wire this up to their tool handler."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": "unknown_tool", "name": name})
    try:
        return await handler(arguments or {})
    except Exception as exc:
        logger.exception("MCP tool %s failed", name)
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# Stdio entry-point (uses the official MCP SDK so any MCP 1.x host —
# OpenCode, Claude Desktop, Cursor — can connect over stdio without
# reinventing JSON-RPC framing).
# ---------------------------------------------------------------------------

def main() -> int:
    """Run the MCP server over stdio using the ``mcp`` Python SDK."""
    import asyncio

    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent

    app = Server("researchos")

    @app.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in TOOLS
        ]

    @app.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        body = await dispatch(name, arguments or {})
        return [TextContent(type="text", text=body)]

    async def _serve() -> None:
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
