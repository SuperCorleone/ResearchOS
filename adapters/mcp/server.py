"""MCP server — expose ResearchOS verbs as Model-Context-Protocol tools.

This adapter lets any MCP-speaking host (Claude Desktop, Cursor, a custom
orchestrator) drive ResearchOS over the standard MCP tool-call surface.

Tools exposed:
- ``researchos_launch``          — start the autonomous scheduler loop
- ``researchos_trial``           — run one pipeline end-to-end and stop
- ``researchos_get_state``       — return typed pipeline state as JSON
- ``researchos_inject_directive``— append a user directive to a pipeline
- ``researchos_kill``            — terminate a pipeline (stage → FAILED)

The server ships with a thin ``researchos.agent_runtime.sdk.SDKRuntime``
under the hood, which is the zero-dependency path. Swap via
``RESEARCHOS_RUNTIME=openharness`` if the host already has OH installed.

This file deliberately imports the MCP SDK lazily so the module loads (and
tools can still be introspected) on systems that don't have ``mcp`` installed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("researchos.mcp")

# ---------------------------------------------------------------------------
# Tool descriptors (MCP-style JSON-Schema). These are the single source of
# truth for the tool contract — both ``adapters/mcp/manifest.json`` and the
# live server handler read from this list.
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "researchos_launch",
        "description": (
            "Run the autonomous ResearchOS scheduler loop until idle. "
            "Returns a short status summary when the run stops."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "auto_select": {
                    "type": "boolean", "default": False,
                    "description": "Skip PROPOSAL-stage professor approval.",
                },
                "simulate": {
                    "type": "boolean", "default": False,
                    "description": "Mock LLM calls (for CI/testing).",
                },
            },
        },
    },
    {
        "name": "researchos_trial",
        "description": (
            "Execute exactly one pipeline end-to-end (heartbeat + miner + "
            "scheduler) and return the final pipeline id + stage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string", "enum": ["real", "simulate"],
                    "default": "real",
                },
            },
        },
    },
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


async def _handle_launch(args: dict[str, Any]) -> str:
    from researchos.runtime.scheduler import Scheduler
    scheduler = Scheduler(
        _project_root(),
        simulate=bool(args.get("simulate")),
        auto_professor=bool(args.get("auto_select")),
    )
    await scheduler.run_until_idle()
    return json.dumps({"status": "idle"})


async def _handle_trial(args: dict[str, Any]) -> str:
    from researchos.runtime.scheduler import Scheduler
    scheduler = Scheduler(
        _project_root(), simulate=args.get("mode") == "simulate",
    )
    await scheduler.run_trial()
    return json.dumps({"status": "trial_complete"})


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
    "researchos_launch":            _handle_launch,
    "researchos_trial":             _handle_trial,
    "researchos_get_state":         _handle_get_state,
    "researchos_inject_directive":  _handle_inject_directive,
    "researchos_kill":              _handle_kill,
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
# Stdio entry-point (for hosts that talk MCP over stdin/stdout).
# ---------------------------------------------------------------------------

def main() -> int:
    """Minimal stdio MCP server.

    Expects JSON-RPC over stdin — one request per line:
      { "method": "tools/list" }
      { "method": "tools/call", "params": {"name": "...", "arguments": {...}} }

    Writes a JSON-RPC response per line to stdout. Real MCP protocol framing
    (Content-Length headers) can sit on top of this if the host requires it.
    """
    import asyncio

    async def _serve() -> None:
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                print(json.dumps({"error": f"bad_json: {exc}"}), flush=True)
                continue
            method = req.get("method")
            rid = req.get("id")
            if method == "tools/list":
                response = {"id": rid, "result": {"tools": TOOLS}}
            elif method == "tools/call":
                params = req.get("params", {})
                name = params.get("name", "")
                args = params.get("arguments") or {}
                body = await dispatch(name, args)
                response = {"id": rid, "result": {"content": body}}
            else:
                response = {"id": rid, "error": f"unknown_method: {method}"}
            print(json.dumps(response), flush=True)

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
