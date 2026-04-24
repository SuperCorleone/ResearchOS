"""Platform-agnostic agent runtime abstraction.

AgentRuntime is the Protocol that any host (OpenHarness, Claude Skill, MCP
client, bare SDK) must implement to drive ResearchOS agents. The core
dispatcher only depends on this Protocol — never on a specific runtime.

Shipping runtimes:
  - SDKRuntime          (researchos.agent_runtime.sdk)
  - OpenHarnessRuntime  (adapters.openharness.runtime)
  - SkillRuntime        (adapters.skill.runtime)
  - MCPRuntime          (adapters.mcp.server)
"""

from researchos.agent_runtime.base import (
    AgentContext,
    AgentResult,
    AgentRuntime,
    ToolSpec,
)

__all__ = ["AgentContext", "AgentResult", "AgentRuntime", "ToolSpec"]
