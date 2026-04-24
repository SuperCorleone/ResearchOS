"""AgentRuntime Protocol — the one interface that decouples ResearchOS from
any specific agent host (OpenHarness / Claude Skill / MCP / raw SDK).

The core dispatcher holds an ``AgentRuntime`` instance via DI and calls
``runtime.run_agent(...)``. Each platform adapter implements this Protocol
and translates ResearchOS semantics into the host's tool/agent primitives.

Design rationale (from 2026-04 refactor):
  - OpenHarnessRuntime wraps `oh -p <prompt>` subprocess spawn
  - SDKRuntime hits OpenAI/Anthropic SDK directly; supplies its own tool
    implementations from researchos.agent_runtime.tools
  - SkillRuntime runs inside a Claude Code Skill sandbox; borrows host's
    native tool set (file_read, Bash, Grep, ...)
  - MCPRuntime turns each research step into an MCP tool exposed to an
    outer MCP client (Claude Desktop, Cursor, ...)

Keep this file < 150 lines. Platform-specific logic lives in implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    """Declarative descriptor of a tool exposed to an agent.

    The runtime is responsible for translating this into whatever format the
    host expects (OpenAI function schema, MCP tool descriptor, Anthropic
    tool_use block, etc.). ResearchOS core never calls the tool itself — it
    only declares what the agent is allowed to use.
    """

    name: str
    description: str
    json_schema: dict[str, Any]         # JSON-Schema for arguments
    category: Literal["io", "kb", "exec", "inspect", "net"] = "io"


@dataclass
class AgentContext:
    """Runtime context passed to every agent invocation.

    Keep this minimal — platform-specific state goes into the runtime impl.
    """

    pipeline_id: str
    project_root: Path
    working_dir: Path                    # e.g. artifacts/pipeline_<id>/
    agent_name: str
    model_tier: Literal["T1", "T2", "T3"] = "T2"
    max_tokens: int | None = None
    timeout_seconds: int = 600
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """Outcome of a single agent invocation.

    ``artifacts_written`` lists paths (relative to project_root) that the
    agent created/modified during this call. The scheduler uses this to
    update PipelineState and trigger contract guards.
    """

    success: bool
    content: str                         # raw LLM content (may be empty)
    artifacts_written: list[Path] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)   # tokens_in/out/cached
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class AgentRuntime(Protocol):
    """Platform-agnostic agent execution interface.

    Implement these 3 methods to plug ResearchOS into any host.
    """

    async def run_agent(
        self,
        agent_def: str,                  # loaded from researchos/agent_defs/<name>.md
        user_message: str,               # dispatcher-built prompt with typed digests
        tools: list[ToolSpec],
        context: AgentContext,
    ) -> AgentResult:
        """Execute one agent turn and return the structured result.

        Contract:
        - MUST NOT mutate ``context``.
        - MAY write files under ``context.working_dir`` (list them in
          ``result.artifacts_written``).
        - MUST propagate timeouts / API errors as ``AgentResult(success=False,
          error=...)``; do NOT raise.
        """
        ...

    def supports_tool(self, tool_name: str) -> bool:
        """Return True if this runtime can provide `tool_name`.

        Dispatcher uses this to filter ToolSpec list before `run_agent`.
        """
        ...

    async def shutdown(self) -> None:
        """Release any runtime resources (subprocesses, HTTP clients, ...).

        Idempotent. Called once at pipeline end or SIGTERM.
        """
        ...


# ---------------------------------------------------------------------------
# Stub registry (to be populated by adapters at import time)
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[AgentRuntime]] = {}


def register_runtime(name: str, cls: type[AgentRuntime]) -> None:
    """Adapters call this at import time to announce themselves."""
    _REGISTRY[name] = cls


def get_runtime(name: str) -> type[AgentRuntime]:
    """Look up a runtime class by name. Raise KeyError if not registered."""
    if name not in _REGISTRY:
        raise KeyError(
            f"AgentRuntime '{name}' not registered. "
            f"Available: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]


def available_runtimes() -> list[str]:
    return sorted(_REGISTRY.keys())
