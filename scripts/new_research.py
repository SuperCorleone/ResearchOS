#!/usr/bin/env python3
"""Create a new research pipeline proposal interactively.

Usage:
    python scripts/new_research.py
    python scripts/new_research.py --id my_topic --title "My Research" --question "Does X cause Y?"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from researchos.schemas.pipeline import PipelineDefinition


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:30]


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new research pipeline")
    parser.add_argument("--id", help="Pipeline ID (auto-generated if omitted)")
    parser.add_argument("--title", help="Research title")
    parser.add_argument("--question", help="Research question")
    parser.add_argument("--budget", type=int, default=20000, help="Budget in credits (default: 20000)")
    args = parser.parse_args()

    print("=" * 50)
    print("  ResearchOS - New Research Pipeline")
    print("=" * 50)

    title = args.title
    if not title:
        title = input("\nResearch title: ").strip()
        if not title:
            print("Error: title is required")
            sys.exit(1)

    question = args.question
    if not question:
        question = input("Research question: ").strip()
        if not question:
            print("Error: question is required")
            sys.exit(1)

    pipeline_id = args.id or slugify(title)
    budget = args.budget

    print(f"\n--- Preview ---")
    print(f"  ID:       {pipeline_id}")
    print(f"  Title:    {title}")
    print(f"  Question: {question}")
    print(f"  Budget:   {budget} credits")

    # Check for conflicts
    state_file = PROJECT_ROOT / "artifacts" / f"pipeline_{pipeline_id}" / "state.json"
    if state_file.exists():
        print(f"\nError: Pipeline '{pipeline_id}' already exists!")
        sys.exit(1)

    proposal = PipelineDefinition(
        pipeline_id=pipeline_id,
        title=title,
        research_question=question,
        budget_request=budget,
    )

    proposal_path = PROJECT_ROOT / "runtime" / "pipelines" / f"new_{pipeline_id}.json"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(proposal.model_dump_json(indent=2))

    print(f"\nProposal created: {proposal_path.relative_to(PROJECT_ROOT)}")
    print(f"\nNext steps:")
    print(f"  1. cd {PROJECT_ROOT}")
    print(f"  2. PYTHONPATH=. python -m researchos.runtime.scheduler --once --verbose")
    print(f"     (Scheduler will create the pipeline and request professor approval)")
    print(f"  3. Run again to advance through each step")
    print(f"     Or use --simulate for mock execution:")
    print(f"     PYTHONPATH=. python -m researchos.runtime.scheduler --simulate --once --verbose")


if __name__ == "__main__":
    main()
