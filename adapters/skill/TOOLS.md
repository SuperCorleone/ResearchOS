# ResearchOS Skill — Tool Descriptors

These are the tool descriptors the Skill exposes to Claude. The shape mirrors
`adapters/mcp/server.py::TOOLS` so the same handlers work in both contexts.

## launch

Run the autonomous scheduler loop until idle.

Arguments:
- `auto_select` (boolean, default `false`) — skip PROPOSAL-stage approval.
- `simulate` (boolean, default `false`) — mock LLM calls.

## trial

Execute exactly one pipeline end-to-end (heartbeat + miner + scheduler) and stop.

Arguments:
- `mode` (enum `real|simulate`, default `real`).

## state

Return the typed `PipelineState` as JSON.

Arguments:
- `pipeline_id` (string, required).

## inject_directive

Append a directive to a pipeline's `user_directives.yaml`. The next
DESIGN/CODE/RUN step's prompt will include unapplied directives.

Arguments:
- `pipeline_id` (string, required).
- `directive` (string, required) — free-form English instruction.

## kill

Transition a pipeline to `stage=FAILED` immediately. Emergency brake.

Arguments:
- `pipeline_id` (string, required).
- `reason` (string, optional).

## Implementation

Every verb delegates to `researchos.cli.<cmd_fn>` or
`adapters.mcp.server.dispatch()` — see those files for the authoritative
behavior. The skill-level invocation lives in `adapters/skill/runtime.py`
and registers itself as the ``"skill"`` runtime via
`researchos.agent_runtime.base.register_runtime`.

## Handoff protocol

When running inside a Skill sandbox, the scheduler's `AgentRuntime` is
`SkillRuntime`. For each agent step it writes a request file to
`.skill_handoff/<id>.request.json` and expects the Skill outer loop to
materialise `.skill_handoff/<id>.response.json` using Claude's native
tools. The response JSON schema:

```json
{
  "success": true,
  "content": "string",
  "artifacts_written": ["artifacts/pipeline_xyz/hypothesis.md"],
  "tokens_in": 1234,
  "tokens_out": 567,
  "error": null
}
```
