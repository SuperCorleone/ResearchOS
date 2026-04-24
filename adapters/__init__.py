"""Platform adapters for ResearchOS.

Each subpackage implements ``researchos.agent_runtime.AgentRuntime`` for
a specific host. Adapters are loaded by ``researchos.cli`` based on the
``RESEARCHOS_RUNTIME`` environment variable (default: sdk).
"""
