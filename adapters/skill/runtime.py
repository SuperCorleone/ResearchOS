"""SkillRuntime — AgentRuntime adapter for the Claude Skill host.

Inside a Claude Code session the host already owns a rich tool set
(file_read, file_write, Bash, Grep, Glob, WebFetch, ...). Rather than
re-implement those or subprocess-spawn agents, ``SkillRuntime`` routes
each ``run_agent`` call back through the host by writing a structured
directive file that the calling Skill executes in-process.

Design note: the Claude Skill runtime surface does not yet have a
synchronous "invoke this named sub-agent with these tools" primitive; the
Skill itself drives the tool calls. ``SkillRuntime.run_agent`` therefore
emits the prompt + required-tool list into a handoff file and returns a
``pending`` result. The Skill's outer loop picks up the file, runs the
tools via the host, and writes the response back. See ``SKILL.md`` for
the high-level flow.

For production use outside a Skill sandbox (CI, local CLI, MCP), use
``researchos.agent_runtime.sdk.SDKRuntime`` instead.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from researchos.agent_runtime.base import (
    AgentContext,
    AgentResult,
    AgentRuntime,
    ToolSpec,
    register_runtime,
)

logger = logging.getLogger(__name__)

# Tools the Skill host reliably provides. SkillRuntime does not introspect
# the host's live tool set — it trusts this allowlist.
_HOST_TOOLS: frozenset[str] = frozenset({
    "file_read", "file_write", "kb_retrieve", "execute_python",
    "fetch_dataset", "Bash", "Grep", "Glob", "WebFetch",
    "gen_code_tree_structure", "grep_code", "list_file_symbols",
})


class SkillRuntime:
    """File-handshake AgentRuntime for Claude Skill contexts."""

    HANDOFF_DIRNAME = ".skill_handoff"

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)
        self.handoff_dir = self.project_root / self.HANDOFF_DIRNAME
        self.handoff_dir.mkdir(parents=True, exist_ok=True)

    async def run_agent(
        self,
        agent_def: str,
        user_message: str,
        tools: list[ToolSpec],
        context: AgentContext,
    ) -> AgentResult:
        """Write a handoff request; the outer Skill loop fulfils it.

        The handoff file lives under ``.skill_handoff/<request_id>.json`` and
        contains the prompt + tool allowlist + context. A ``.response.json``
        sibling materialises once the Skill finishes; if it's already there
        (e.g. after re-entry), we consume it synchronously.
        """
        request_id = uuid.uuid4().hex
        req_path = self.handoff_dir / f"{request_id}.request.json"
        resp_path = self.handoff_dir / f"{request_id}.response.json"

        request_payload = {
            "request_id": request_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "agent_name": context.agent_name,
            "pipeline_id": context.pipeline_id,
            "working_dir": str(context.working_dir),
            "prompt": user_message,
            "agent_def": agent_def,
            "tools": [
                t.name for t in tools
                if t.name in _HOST_TOOLS
            ],
            "timeout_seconds": context.timeout_seconds,
        }
        req_path.write_text(json.dumps(request_payload, indent=2))

        # If the outer Skill loop already produced a response (re-entry
        # pattern), consume it and return. Otherwise report a ``pending``
        # success — the Skill will re-invoke us after resolving the handoff.
        if resp_path.exists():
            try:
                resp = json.loads(resp_path.read_text())
            except Exception as exc:
                return AgentResult(
                    success=False,
                    content="",
                    error=f"malformed_response:{exc}",
                )
            artifacts = [Path(p) for p in resp.get("artifacts_written", [])]
            return AgentResult(
                success=bool(resp.get("success", False)),
                content=str(resp.get("content") or ""),
                artifacts_written=artifacts,
                usage={"tokens_in": int(resp.get("tokens_in", 0)),
                       "tokens_out": int(resp.get("tokens_out", 0))},
                error=resp.get("error"),
                metadata={"request_id": request_id,
                          "handoff_path": str(req_path)},
            )

        return AgentResult(
            success=False,
            content="",
            error="pending_skill_handoff",
            metadata={"request_id": request_id,
                      "handoff_path": str(req_path),
                      "note": ("Write response JSON to "
                               f"{resp_path.name} and re-invoke.")},
        )

    def supports_tool(self, tool_name: str) -> bool:
        return tool_name in _HOST_TOOLS

    async def shutdown(self) -> None:
        """Cleanup handoff files older than 24h.

        Idempotent; callable from Skill deactivation hooks. We do not delete
        the directory itself — other runtimes may share it.
        """
        import time
        cutoff = time.time() - 86400
        for f in self.handoff_dir.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue


register_runtime("skill", SkillRuntime)
