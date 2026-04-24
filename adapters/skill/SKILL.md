---
name: researchos
description: >-
  Run a ResearchOS autonomous research pipeline from inside Claude Code.
  Drives the same scheduler used by the OpenHarness and MCP entry points,
  but borrows the host's native tools (file_read, Bash, Grep, Glob) instead
  of spawning subprocess agents.
---

# ResearchOS Skill

This skill exposes ResearchOS's research loop as a Claude Skill. Inside a
Claude Code session it runs the scheduler via `SkillRuntime`, which
satisfies the `AgentRuntime` Protocol by delegating tool calls back to the
host's built-in tool set.

## When to invoke

- User says "run a research pipeline on X", "let ResearchOS handle this
  experiment", or references the `/researchos` slash command.
- A paper PDF is in `knowledge/raw/` and the user wants the full pipeline
  (compile → mine → hypothesis → design → code → run → judge → paper).
- User asks for the current state of a running pipeline, or to inject a
  directive into one.

## When NOT to invoke

- User is asking about the ResearchOS codebase itself (classes, files,
  semantics) — answer from the source, do not launch a pipeline.
- User wants a one-shot analysis that doesn't need the full lifecycle — use
  Claude's native tools directly.

## Tool surface

See `adapters/skill/TOOLS.md` for the exact tool descriptors. The skill
exposes five verbs:

| Verb                | Purpose                                          |
|---------------------|--------------------------------------------------|
| `launch`            | Run the autonomous scheduler loop until idle.    |
| `trial`             | Execute one pipeline end-to-end and stop.        |
| `state`             | Return typed `PipelineState` JSON.               |
| `inject_directive`  | Append a user directive to a pipeline.           |
| `kill`              | Transition a pipeline to stage=FAILED.           |

## How it runs

1. The skill activates with `RESEARCHOS_PROJECT_ROOT` pointing at the
   current workspace root (default: CWD).
2. `researchos.cli.main(["<verb>", ...])` dispatches the verb — same code
   path the command-line entry uses.
3. Inside the scheduler, `SkillRuntime.run_agent()` (in
   `adapters/skill/runtime.py`) is the active `AgentRuntime`. It does NOT
   spawn subprocess agents; every tool call is satisfied by the host's
   native tool bindings (file_read / Bash / Grep / Glob / WebFetch / …).
4. Outputs land in `artifacts/pipeline_<id>/` as usual. The typed digests
   (`hypothesis.digest.json`, `plan.digest.json`, `concepts.digest.json`,
   `run.digest.json`, `judge.digest.json`) are Pydantic-validated at
   every `state_manager.save_pipeline()` call.

## Safety & guardrails

- Skill invocation never bypasses the `contract_guard.enforce_all()` hook.
  A digest that violates an invariant still transitions the pipeline to
  `BLOCKED` and surfaces the `ContractViolation` to Claude for
  remediation.
- No HITL gates. Kill / tweak remain as emergency brakes via the `kill`
  and `inject_directive` tools.
- Budget is tracked in `runtime/budget.json`; the skill will stop when the
  per-pipeline budget is exhausted.
