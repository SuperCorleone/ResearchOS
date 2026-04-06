# Research OS - Project Instructions

## Overview

This is a Knowledge-Driven Multi-Agent Research Lab OS built on OpenHarness.
All agents operating in this project MUST follow the rules below.

## Directory Protocol

| Directory | Purpose | Who writes |
|-----------|---------|-----------|
| `/knowledge/raw/` | Raw inputs (papers, logs, data) | External / manual |
| `/knowledge/wiki/` | Compiled structured knowledge | Knowledge Compiler only |
| `/knowledge/index/` | Summaries and topic maps | Knowledge Maintainer only |
| `/knowledge/proposals/` | New research ideas from Miner | Knowledge Miner only |
| `/artifacts/pipeline_<id>/` | Pipeline outputs and state | Pipeline agents only |
| `/scratch/` | Temporary agent workspace | Any agent (auto-cleaned) |
| `/runtime/` | Scheduler state, logs, budget | Scheduler only |
| `/decisions/` | Professor decision files | Professor agent only |
| `/paper/` | Paper drafts and reviews | Paper Writer / Reviewer |

## Mandatory Rules

1. **Knowledge Access**: ALWAYS use the `kb_retrieve` MCP tool to read from the knowledge base.
   NEVER use `cat`, `read`, or `grep` on `/knowledge/wiki/` files directly.

2. **Artifact Protocol**: Pipeline agents write outputs ONLY to their pipeline directory
   `/artifacts/pipeline_<id>/`. Each artifact must be self-contained with references.

3. **Scratch Usage**: Use `/scratch/<call_id>/` for temporary reasoning. Never reference
   scratch files in persistent artifacts.

4. **State Machine**: Do NOT modify `state.json` files manually. The runtime scheduler
   handles all state transitions.

5. **Decision Protocol**: Only the Professor agent writes to `/decisions/`. Decision files
   use YAML format: `approval_<pipeline_id>_<timestamp>.yaml`.

6. **Idempotency**: Before producing an artifact, check if it already exists. If the
   expected output file is present and complete, skip the step.

## Python Package

The `researchos` Python package contains:
- `researchos.schemas` -- Pydantic models for pipeline state, decisions, budget, artifacts
- `researchos.runtime` -- Scheduler, dispatcher, state manager
- `researchos.mcp_servers` -- MCP server for `kb_retrieve`

Install: `pip install -e .`
