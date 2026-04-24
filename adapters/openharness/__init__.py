"""OpenHarness adapter.

Translates ResearchOS AgentRuntime calls into ``oh -p <prompt>`` subprocess
spawns. This is the historical default runtime.

Migration note: agent .md definitions used to live under
``.openharness/plugins/researchos/agents/``. They are in the process of
moving to ``researchos/agent_defs/``. Until that migration completes,
the OH adapter reads from both locations — new files win.
"""
