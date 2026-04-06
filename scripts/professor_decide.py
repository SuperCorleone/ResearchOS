#!/usr/bin/env python3
"""Interactive professor decision CLI.

Lists pending decision requests, shows context, and prompts
the user for a decision (approve/kill/pivot/pause).

Usage:
    python scripts/professor_decide.py
    python scripts/professor_decide.py --pipeline my_pipeline --decision approve
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from researchos.schemas.decision import DecisionType
from researchos.utils.paths import ARTIFACTS_DIR, DECISIONS_DIR


def list_pending_requests() -> list[dict]:
    """Scan decisions/ for request_*.yaml files and return parsed contents."""
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    requests = []
    for f in sorted(DECISIONS_DIR.glob("request_*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            if data:
                data["_file"] = f
                requests.append(data)
        except Exception:
            pass
    return requests


def show_pipeline_status(pipeline_id: str) -> None:
    """Display the current pipeline state and recent artifacts."""
    pdir = ARTIFACTS_DIR / f"pipeline_{pipeline_id}"
    state_file = pdir / "state.json"

    if state_file.exists():
        import json

        state = json.loads(state_file.read_text())
        print(f"\n  Stage: {state.get('stage', '?')}")
        print(f"  Step:  {state.get('step', '?')}")
        print(f"  Title: {state.get('title', '?')}")
        print(f"  Question: {state.get('research_question', '?')}")
        print(f"  Budget: {state.get('budget_used', 0)}/{state.get('budget_allocated', 0)} credits")
        print(f"  Milestones: {state.get('milestone_count', 0)}")

    # Show available artifacts
    if pdir.exists():
        files = sorted(f.name for f in pdir.iterdir() if f.is_file() and f.name != "state.json")
        if files:
            print(f"\n  Artifacts:")
            for f in files:
                print(f"    - {f}")


def show_context(request: dict) -> None:
    """Display context files referenced in a decision request."""
    pipeline_id = request.get("pipeline_id", "unknown")
    print(f"\n{'='*60}")
    print(f"  Pipeline: {pipeline_id}")
    show_pipeline_status(pipeline_id)

    context_files = request.get("context_files", [])
    for fpath in context_files:
        p = Path(fpath)
        if p.exists() and p.name != "state.json":
            print(f"\n  --- {p.name} ---")
            content = p.read_text(encoding="utf-8")
            # Show first 50 lines
            lines = content.splitlines()
            for line in lines[:50]:
                print(f"  {line}")
            if len(lines) > 50:
                print(f"  ... ({len(lines) - 50} more lines)")
    print(f"{'='*60}")


def prompt_decision() -> tuple[str, str, str | None, int | None]:
    """Interactive prompt for professor decision."""
    print("\n  Decision options:")
    print("    1. approve  - Pipeline progresses to next stage")
    print("    2. kill     - Terminate this pipeline")
    print("    3. pivot    - Change research direction")
    print("    4. pause    - Temporarily block pipeline")

    choices = {"1": "approve", "2": "kill", "3": "pivot", "4": "pause"}
    while True:
        choice = input("\n  Enter choice (1-4): ").strip()
        if choice in choices:
            decision = choices[choice]
            break
        if choice in choices.values():
            decision = choice
            break
        print("  Invalid choice. Try again.")

    comment = input("  Comment (optional): ").strip() or f"{decision} decision"

    next_milestone = None
    if decision == "approve":
        nm = input("  Next milestone (enter 'paper' for paper stage, or press Enter): ").strip()
        if nm:
            next_milestone = nm

    budget_adj = None
    adj = input("  Budget adjustment (integer, or press Enter to skip): ").strip()
    if adj:
        try:
            budget_adj = int(adj)
        except ValueError:
            print("  Invalid number, skipping budget adjustment.")

    return decision, comment, next_milestone, budget_adj


def write_decision(
    pipeline_id: str,
    decision: str,
    comment: str,
    next_milestone: str | None = None,
    budget_adjustment: int | None = None,
) -> Path:
    """Write a properly-formatted decision YAML file."""
    ts = datetime.now(timezone.utc)
    data: dict = {
        "pipeline_id": pipeline_id,
        "decision": decision,
        "comment": comment,
        "created_at": ts.isoformat(),
    }
    if next_milestone:
        data["next_milestone"] = next_milestone
    if budget_adjustment is not None:
        data["budget_adjustment"] = budget_adjustment

    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"approval_{pipeline_id}_{ts.strftime('%Y%m%d%H%M%S')}.yaml"
    path = DECISIONS_DIR / fname
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    print(f"\n  Decision written: {path.name}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Professor Decision CLI")
    parser.add_argument("--pipeline", help="Pipeline ID to decide on")
    parser.add_argument(
        "--decision",
        choices=["approve", "kill", "pivot", "pause"],
        help="Decision type",
    )
    parser.add_argument("--comment", default="", help="Decision comment")
    parser.add_argument("--next-milestone", default=None, help="Next milestone target")
    parser.add_argument("--budget-adj", type=int, default=None, help="Budget adjustment")
    args = parser.parse_args()

    # Non-interactive mode
    if args.pipeline and args.decision:
        comment = args.comment or f"{args.decision} decision"
        write_decision(
            args.pipeline, args.decision, comment,
            args.next_milestone, args.budget_adj,
        )
        return

    # Interactive mode
    requests = list_pending_requests()
    if not requests:
        # Also check for pipelines in MILESTONE/PROPOSAL stage
        print("\n  No pending decision requests found.")
        print("  (Decision requests appear when pipelines reach milestones.)")
        return

    print(f"\n  Found {len(requests)} pending decision request(s):\n")
    for i, req in enumerate(requests):
        pid = req.get("pipeline_id", "unknown")
        print(f"    [{i}] Pipeline: {pid}")

    if len(requests) == 1:
        idx = 0
    else:
        while True:
            try:
                idx = int(input("\n  Select request number: ").strip())
                if 0 <= idx < len(requests):
                    break
            except ValueError:
                pass
            print("  Invalid selection.")

    selected = requests[idx]
    show_context(selected)

    decision, comment, milestone, budget = prompt_decision()
    write_decision(
        selected["pipeline_id"], decision, comment, milestone, budget
    )

    # Remove the request file after processing
    req_file = selected.get("_file")
    if req_file and Path(req_file).exists():
        Path(req_file).rename(Path(req_file).with_suffix(".processed.yaml"))
        print(f"  Request file marked as processed.")


if __name__ == "__main__":
    main()
