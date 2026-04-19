# research

ResearchOS main command hub. Triggered when the user types `/research` or asks about research pipelines.

## Available Subcommands

When the user invokes `/research`, determine the subcommand and execute it using the `bash` tool:

### `/research dashboard` — Show pipeline overview
Run: `python scripts/dashboard.py`
Show the output to the user with pipeline statuses, priorities, and budget.

### `/research launch [--human-review]` — Start the scheduler
Run: `python scripts/run_research.py launch [--human-review] --max-ticks 50 --interval 5`
Start the research scheduler. If `--human-review` is specified, professor writes reports for human decision.

### `/research trial [--human-review] [--mode simulate]` — Trial run (one topic, one pipeline)
Run: `python scripts/run_research.py trial [--human-review] [--mode simulate]`
Heartbeat fetches papers once, Knowledge Miner finds one topic, runs one pipeline to completion, then stops. No additional topics or pipelines are spawned. Ideal for testing the full lifecycle end-to-end.

### `/research create --title "..." --question "..."` — Create a new research proposal
Run: `python scripts/run_research.py create --title "<title>" --question "<question>" [--budget <N>]`

### `/research inject <files...> [--title "..." --question "..."]` — Import papers
Run: `python scripts/run_research.py inject <files> [--title "<title>" --question "<question>"]`
If only files are given, Knowledge Miner discovers topics. If title+question also given, pipeline is created directly.

### `/research fix --pipeline <id>` — Auto-fix a blocked pipeline
Run: `python scripts/run_research.py fix --pipeline <id>`
Reads professor review report, fixes experiment code, reruns experiment, writes correction report.

### `/research continue --pipeline <id>` — Resume inject-mode pipeline after user feedback
Run: `python scripts/run_research.py continue --pipeline <id>`
Only applies to inject-mode pipelines paused after FAST_FAIL. Before running,
the user edits `artifacts/pipeline_<id>/user_approve/feedback.md` with their
modification opinions. The command then dispatches the fixer agent with the
user's feedback as the authoritative directive (overrides professor suggestion),
re-runs the experiment, and unblocks the pipeline into the MAIN stage.

### `/research publish --pipeline <id>` — Package for publication
Run: `python scripts/run_research.py publish --pipeline <id>`
Creates submission-ready package with clean code, paper, data, process report.

### `/research priority <pipeline_id> <0-3>` — Set pipeline priority
Run: `python scripts/dashboard.py --priority <pipeline_id> <level>`
Levels: 0=low, 1=normal, 2=high, 3=urgent. Higher priority pipelines are scheduled first.

### `/research approve <pipeline_id> [--paper]` — Approve a blocked pipeline
Write a YAML decision file to `decisions/` directory:
```yaml
pipeline_id: "<id>"
decision: "approve"
next_milestone: "paper"  # only if --paper flag
comment: "User approved via frontend."
created_at: "<ISO timestamp>"
```

### `/research kill <pipeline_id>` — Terminate a pipeline
Write a YAML decision file with `decision: "kill"`.

### `/research pause-miner` / `/research resume-miner` — Toggle Knowledge Miner
Run: `python scripts/dashboard.py --pause-miner` or `--resume-miner`

### `/research status` — Quick status summary
Run: `python scripts/run_research.py status`

## Execution Rules

1. ALWAYS use the `bash` tool to run commands — do NOT simulate or fabricate results
2. Show command output to the user formatted cleanly
3. For `dashboard`: run `python scripts/dashboard.py --plain` via bash (MUST use --plain flag)
4. For `launch`: run `python scripts/run_research.py launch` via bash (warn about multiple ticks)
5. For `approve`/`kill`: use `write` tool to create a YAML decision file in `decisions/`
6. For `status`: run `python scripts/run_research.py status` via bash
7. ALL paths are relative to the project root directory
