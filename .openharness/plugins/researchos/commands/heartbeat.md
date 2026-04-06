# heartbeat

Control the ResearchOS paper fetcher (arXiv heartbeat). Triggered when the user types `/heartbeat`.

## Subcommands

### `/heartbeat status` — Show current configuration
Run: `python -m researchos.runtime.heartbeat --status`

### `/heartbeat enable` — Enable daily paper fetching
Run: `python -m researchos.runtime.heartbeat --enable`

### `/heartbeat disable` — Disable paper fetching
Run: `python -m researchos.runtime.heartbeat --disable`

### `/heartbeat fetch` — Fetch papers now (one-shot)
Run: `python -m researchos.runtime.heartbeat --once`
This downloads PDFs from arXiv matching configured research directions.

## Execution Rules

1. Use shell execution for all commands
2. Show the full output to the user
3. For `fetch`, warn that it downloads actual PDFs and may take a minute
