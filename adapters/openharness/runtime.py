"""OpenHarnessRuntime — AgentRuntime adapter for the OH headless CLI.

The OH path spawns an `oh -p <prompt>` subprocess that executes the agent in
its own sandboxed React-terminal environment, complete with its native
tool runtime (file_read / Bash / Grep / ...). From ResearchOS's point of
view we hand it the prompt and collect stdout — the concrete tool
implementations live inside OpenHarness itself.

This file absorbs the subprocess spawning previously inlined in
``researchos.runtime.dispatcher.Dispatcher._spawn_agent`` so the dispatcher
no longer hard-codes a specific host. The dispatcher can now delegate to
whichever runtime is registered (SDKRuntime, OpenHarnessRuntime, MCP,
Skill) without an ``if backend == ...`` ladder.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from researchos.agent_runtime.base import (
    AgentContext,
    AgentResult,
    AgentRuntime,
    ToolSpec,
    register_runtime,
)

logger = logging.getLogger(__name__)

# OpenHarness exposes a broad native tool set via its plugin system. We
# don't enumerate every tool here — we trust whatever the agent's `.md`
# definition declares and let OH enforce access control.
_OPENHARNESS_BUILTIN_TOOLS: frozenset[str] = frozenset({
    "file_read", "file_write", "kb_retrieve", "execute_python",
    "fetch_dataset", "Bash", "Grep", "Glob", "WebFetch",
    "gen_code_tree_structure", "grep_code", "list_file_symbols",
})


def _find_oh_binary() -> str:
    """Locate the OpenHarness CLI binary (same search order as dispatcher)."""
    for name in ("oh", "openharness"):
        path = shutil.which(name)
        if path:
            return path
    return "python -m openharness"


class OpenHarnessRuntime:
    """Execute agents by spawning `oh -p <prompt>` subprocesses."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)
        self._running: dict[str, asyncio.subprocess.Process] = {}

    async def run_agent(
        self,
        agent_def: str,                  # unused: OH agents carry their own prompts
        user_message: str,
        tools: list[ToolSpec],
        context: AgentContext,
    ) -> AgentResult:
        """Spawn OH headless and await completion. Capture stdout as content."""
        label = f"{context.agent_name}_{context.pipeline_id}"
        oh = _find_oh_binary()
        cmd = (
            [*oh.split(), "-p", user_message]
            if oh.startswith("python")
            else [oh, "-p", user_message]
        )
        logger.info("OpenHarnessRuntime spawn [%s]", label)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.project_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.exception("OpenHarnessRuntime: failed to spawn [%s]", label)
            return AgentResult(
                success=False,
                content="",
                error=f"spawn_failed:{type(exc).__name__}: {exc}",
            )

        self._running[label] = proc
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=context.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return AgentResult(
                success=False,
                content="",
                error=f"timeout_after_{context.timeout_seconds}s",
            )
        finally:
            self._running.pop(label, None)

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        success = proc.returncode == 0
        return AgentResult(
            success=success,
            content=stdout,
            error=None if success else stderr[-2000:],
            metadata={"return_code": proc.returncode},
        )

    def supports_tool(self, tool_name: str) -> bool:
        return tool_name in _OPENHARNESS_BUILTIN_TOOLS

    async def shutdown(self) -> None:
        """Kill any lingering agent subprocesses. Idempotent."""
        for label, proc in list(self._running.items()):
            try:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            except Exception:
                logger.debug("OpenHarnessRuntime shutdown: %s", label, exc_info=True)
        self._running.clear()


register_runtime("openharness", OpenHarnessRuntime)
