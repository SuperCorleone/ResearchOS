"""MCP (Model Context Protocol) adapter.

Exposes high-level ResearchOS operations as MCP tools for consumption
by any MCP client (Claude Desktop, Cursor, Windsurf, etc.):

  - researchos.launch_pipeline
  - researchos.get_pipeline_state
  - researchos.inject_directive
  - researchos.kill_pipeline

The existing ``researchos.mcp_servers.kb_server`` is separate — it exposes
kb_retrieve only and remains unchanged.
"""
