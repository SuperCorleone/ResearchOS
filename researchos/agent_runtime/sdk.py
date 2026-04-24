"""SDKRuntime — AgentRuntime adapter backed by the direct OpenAI-compatible SDK.

This runtime is the "platform-less" execution path: it hits the OpenAI /
Anthropic / OpenRouter / Ollama endpoints directly via the
``researchos.runtime.agent_executor.AgentExecutor`` tool-use loop, provides
its own tool implementations from ``researchos.agent_runtime.tools``, and
reports back through the ``AgentRuntime`` Protocol.

Use cases:
- ``researchos trial`` CLI entry (no OpenHarness installed)
- MCP server (``adapters/mcp/server.py``) backing each research step
- CI / tests that cannot launch subprocess agents

For the OpenHarness path (subprocess agent spawn with its own tool runtime)
see ``adapters/openharness/runtime.py::OpenHarnessRuntime``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from researchos.agent_runtime.base import (
    AgentContext,
    AgentResult,
    AgentRuntime,
    ToolSpec,
    register_runtime,
)

logger = logging.getLogger(__name__)

# Tool names the SDKRuntime can satisfy out-of-the-box via its bundled
# implementations in researchos.runtime.agent_executor + inspect tools in
# researchos.agent_runtime.tools.
_SUPPORTED_TOOLS: frozenset[str] = frozenset({
    "file_read",
    "file_write",
    "kb_retrieve",
    "execute_python",
    "fetch_dataset",
    "gen_code_tree_structure",
    "grep_code",
    "list_file_symbols",
})


class SDKRuntime:
    """Implement the ``AgentRuntime`` Protocol on top of ``AgentExecutor``."""

    def __init__(
        self,
        project_root: Path,
        *,
        model: str | None = None,
        max_turns: int = 20,
        max_tokens: int = 8192,
    ) -> None:
        self.project_root = Path(project_root)
        self._model = model
        self._max_turns = max_turns
        self._max_tokens = max_tokens
        self._executor: Any = None    # lazy AgentExecutor

    # ------------------------------------------------------------------
    # Lazy executor bootstrap — keeps the import cheap when the runtime
    # is listed but not actually used (MCP discovery, CLI help, tests).
    # ------------------------------------------------------------------

    def _ensure_executor(self) -> Any:
        if self._executor is None:
            from researchos.runtime.agent_executor import AgentExecutor
            self._executor = AgentExecutor(
                self.project_root,
                model=self._model,
                max_turns=self._max_turns,
                max_tokens=self._max_tokens,
            )
        return self._executor

    # ------------------------------------------------------------------
    # AgentRuntime Protocol
    # ------------------------------------------------------------------

    async def run_agent(
        self,
        agent_def: str,                  # unused here; executor loads from agent_defs/
        user_message: str,
        tools: list[ToolSpec],
        context: AgentContext,
    ) -> AgentResult:
        """Execute one agent turn via the SDK tool-use loop."""

        # ToolSpec filtering: warn if the caller asks for tools we don't serve.
        unsupported = [t.name for t in tools if t.name not in _SUPPORTED_TOOLS]
        if unsupported:
            logger.warning(
                "SDKRuntime: caller requested unsupported tools %s; they "
                "will not be available to the agent.",
                unsupported,
            )

        executor = self._ensure_executor()
        try:
            payload = await executor.execute(
                agent_name=context.agent_name,
                prompt=user_message,
                pipeline_id=context.pipeline_id,
                pipeline_dir=context.working_dir,
            )
        except Exception as exc:
            logger.exception(
                "SDKRuntime.run_agent: executor crashed for %s",
                context.agent_name,
            )
            return AgentResult(
                success=False,
                content="",
                error=f"{type(exc).__name__}: {exc}",
            )

        artifacts = [Path(p) for p in payload.get("artifacts_written", [])]
        usage = {
            "tokens_in": int(payload.get("tokens_used", 0)),
            "tokens_out": 0,               # executor reports total only
        }
        return AgentResult(
            success=bool(payload.get("success", False)),
            content=str(payload.get("output") or ""),
            artifacts_written=artifacts,
            usage=usage,
            error=None if payload.get("success") else str(payload.get("output", "")),
            metadata={k: v for k, v in payload.items()
                     if k not in {"success", "output", "artifacts_written", "tokens_used"}},
        )

    def supports_tool(self, tool_name: str) -> bool:
        return tool_name in _SUPPORTED_TOOLS

    async def shutdown(self) -> None:
        """No-op: the OpenAI SDK client doesn't hold long-lived resources
        beyond a thread pool we don't own."""
        self._executor = None


# Register on import so ``get_runtime("sdk")`` works without explicit wiring.
register_runtime("sdk", SDKRuntime)
