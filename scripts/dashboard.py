#!/usr/bin/env python3
"""ResearchOS Pipeline Dashboard — Terminal UI.

Visual overview of all pipelines with priority control and miner toggle.

Usage:
    python scripts/dashboard.py             # View dashboard
    python scripts/dashboard.py --priority pipeline_id high
    python scripts/dashboard.py --pause-miner
    python scripts/dashboard.py --resume-miner
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from researchos.runtime.state_manager import StateManager
from researchos.schemas.pipeline import PipelineStage

# ANSI colors
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"
BG_GREEN = "\033[42m"
BG_YELLOW = "\033[43m"
BG_RED = "\033[41m"
BG_BLUE = "\033[44m"
BG_MAGENTA = "\033[45m"

STAGE_COLORS = {
    "proposal": YELLOW,
    "fast_fail": CYAN,
    "main": BLUE,
    "milestone": MAGENTA,
    "paper": GREEN,
    "knowledge": GREEN,
    "completed": f"{DIM}{GREEN}",
    "failed": RED,
    "blocked": f"{BOLD}{RED}",
    "direction": DIM,
}

STAGE_ICONS = {
    "proposal": "📋",
    "fast_fail": "⚡",
    "main": "🔬",
    "milestone": "🏁",
    "paper": "📝",
    "knowledge": "📚",
    "completed": "✅",
    "failed": "❌",
    "blocked": "⏸️ ",
}

PRIORITY_LABELS = {
    0: f"{DIM}low{RESET}",
    1: f"{WHITE}normal{RESET}",
    2: f"{YELLOW}high{RESET}",
    3: f"{BOLD}{RED}urgent{RESET}",
}


def render_dashboard(sm: StateManager) -> None:
    """Render the full dashboard."""
    pipelines = sm.load_all_pipelines()
    budget = sm.load_budget()

    # Sort by priority (highest first), then by stage
    stage_order = {
        "blocked": 0, "fast_fail": 1, "main": 2, "milestone": 3,
        "paper": 4, "proposal": 5, "completed": 9, "failed": 9,
    }
    pipelines.sort(key=lambda p: (-p.priority, stage_order.get(p.stage.value, 5)))

    # Header
    total_used = sum(budget.usage.values())
    print()
    print(f"  {BOLD}ResearchOS Pipeline Dashboard{RESET}")
    print(f"  {'═' * 70}")

    # Budget bar
    pct = total_used / max(budget.total_credits, 1) * 100
    bar_len = 40
    filled = int(bar_len * pct / 100)
    bar = f"{'█' * filled}{'░' * (bar_len - filled)}"
    print(f"  Budget: [{bar}] {total_used}/{budget.total_credits} credits ({pct:.0f}%)")

    # Miner status
    miner_paused = (PROJECT_ROOT / "runtime" / ".miner_paused").exists()
    miner_status = f"{RED}PAUSED{RESET}" if miner_paused else f"{GREEN}ACTIVE{RESET}"
    print(f"  Knowledge Miner: {miner_status}")
    print()

    # Pipeline summary counters
    active = sum(1 for p in pipelines if p.stage.value not in ("completed", "failed"))
    completed = sum(1 for p in pipelines if p.stage.value == "completed")
    blocked = sum(1 for p in pipelines if p.stage.value == "blocked")
    print(f"  {GREEN}● {active} active{RESET}  {DIM}● {completed} completed{RESET}  {RED}● {blocked} blocked{RESET}")
    print()

    if not pipelines:
        print(f"  {DIM}No pipelines found. Use 'launch' or 'inject' to start research.{RESET}")
        print()
        return

    # Pipeline table
    print(f"  {BOLD}{'Pipeline':<30} {'Stage':<12} {'Step':<15} {'M':<3} {'Priority':<8} {'Budget':<15}{RESET}")
    print(f"  {'─' * 85}")

    for p in pipelines:
        stage = p.stage.value
        color = STAGE_COLORS.get(stage, "")
        icon = STAGE_ICONS.get(stage, "  ")
        step = p.step.value if p.step else "-"
        priority = PRIORITY_LABELS.get(p.priority, f"{p.priority}")
        budget_str = f"{p.budget_used}/{p.budget_allocated}"

        # Truncate pipeline ID
        pid = p.pipeline_id[:28]

        print(
            f"  {icon} {color}{pid:<28}{RESET} "
            f"{color}{stage:<12}{RESET} "
            f"{step:<15} "
            f"{p.milestone_count:<3} "
            f"{priority:<8} "
            f"{budget_str}"
        )

        # Show title on next line for active pipelines
        if stage not in ("completed", "failed"):
            title = p.title[:70]
            print(f"     {DIM}{title}{RESET}")

        # Show error for blocked
        if p.last_error:
            err = p.last_error[:70]
            print(f"     {RED}{err}{RESET}")

    print(f"  {'─' * 85}")

    # Direction summary (from heartbeat config)
    hb_path = PROJECT_ROOT / "runtime" / "heartbeat_config.yaml"
    if hb_path.exists():
        import yaml
        hb = yaml.safe_load(hb_path.read_text()) or {}
        directions = hb.get("directions", [])
        if directions:
            print()
            print(f"  {BOLD}Research Directions{RESET}")
            for d in directions:
                name = d.get("name", "?")
                prio = d.get("priority", "?")
                # Count pipelines in this direction
                # (rough match by keywords in title)
                keywords = d.get("keywords", [])
                matching = sum(
                    1 for p in pipelines
                    if any(kw.lower() in p.title.lower() for kw in keywords)
                    and p.stage.value not in ("completed", "failed")
                )
                total_matching = sum(
                    1 for p in pipelines
                    if any(kw.lower() in p.title.lower() for kw in keywords)
                )
                prio_color = {"high": YELLOW, "medium": WHITE, "low": DIM}.get(prio, "")
                print(f"    {prio_color}[{prio:>6}]{RESET} {name}: {matching} active / {total_matching} total")

    print()
    print(f"  {DIM}Commands: --priority <id> <0-3> | --pause-miner | --resume-miner{RESET}")
    print()


def set_priority(sm: StateManager, pipeline_id: str, priority: int) -> None:
    """Set pipeline priority (0=low, 1=normal, 2=high, 3=urgent)."""
    p = sm.load_pipeline(pipeline_id)
    if p is None:
        print(f"  ERROR: Pipeline '{pipeline_id}' not found.")
        return
    p.priority = max(0, min(3, priority))
    sm.save_pipeline(p)
    label = {0: "low", 1: "normal", 2: "high", 3: "urgent"}.get(p.priority, str(p.priority))
    print(f"  Priority set: {pipeline_id} → {label} ({p.priority})")


def pause_miner() -> None:
    """Pause the Knowledge Miner from generating new proposals."""
    marker = PROJECT_ROOT / "runtime" / ".miner_paused"
    marker.touch()
    print("  Knowledge Miner paused. No new proposals will be generated.")


def resume_miner() -> None:
    """Resume the Knowledge Miner."""
    marker = PROJECT_ROOT / "runtime" / ".miner_paused"
    if marker.exists():
        marker.unlink()
    print("  Knowledge Miner resumed.")


def render_plain(sm: StateManager) -> None:
    """Render a plain-text dashboard (no ANSI colors, for LLM consumption)."""
    pipelines = sm.load_all_pipelines()
    budget = sm.load_budget()

    stage_order = {
        "blocked": 0, "fast_fail": 1, "main": 2, "milestone": 3,
        "paper": 4, "proposal": 5, "completed": 9, "failed": 9,
    }
    pipelines.sort(key=lambda p: (-p.priority, stage_order.get(p.stage.value, 5)))

    total_used = sum(budget.usage.values())
    active = sum(1 for p in pipelines if p.stage.value not in ("completed", "failed"))
    completed = sum(1 for p in pipelines if p.stage.value == "completed")
    blocked = sum(1 for p in pipelines if p.stage.value == "blocked")
    miner_paused = (PROJECT_ROOT / "runtime" / ".miner_paused").exists()

    print(f"ResearchOS Dashboard")
    print(f"Budget: {total_used}/{budget.total_credits} credits ({total_used*100//max(budget.total_credits,1)}%)")
    print(f"Miner: {'PAUSED' if miner_paused else 'ACTIVE'}")
    print(f"Pipelines: {active} active, {completed} completed, {blocked} blocked")
    print()

    prio_names = {0: "low", 1: "normal", 2: "high", 3: "urgent"}
    for p in pipelines:
        step = p.step.value if p.step else "-"
        prio = prio_names.get(p.priority, str(p.priority))
        print(f"  [{p.stage.value:12}] {p.pipeline_id[:30]:30} M{p.milestone_count} prio={prio:7} budget={p.budget_used}/{p.budget_allocated}")
        if p.stage.value not in ("completed", "failed"):
            print(f"               {p.title[:65]}")
        if p.last_error:
            print(f"               ERROR: {p.last_error[:65]}")

    # Directions
    import yaml as _yaml
    hb_path = PROJECT_ROOT / "runtime" / "heartbeat_config.yaml"
    if hb_path.exists():
        try:
            hb = _yaml.safe_load(hb_path.read_text()) or {}
            directions = hb.get("directions", [])
            if directions:
                print()
                print("Research Directions:")
                for d in directions:
                    name = d.get("name", "?")
                    prio = d.get("priority", "?")
                    print(f"  [{prio:>6}] {name}")
        except Exception:
            pass
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="ResearchOS Pipeline Dashboard")
    parser.add_argument("--priority", nargs=2, metavar=("PIPELINE", "LEVEL"),
                        help="Set pipeline priority (0=low, 1=normal, 2=high, 3=urgent)")
    parser.add_argument("--pause-miner", action="store_true", help="Pause new topic discovery")
    parser.add_argument("--resume-miner", action="store_true", help="Resume topic discovery")
    parser.add_argument("--plain", action="store_true", help="Plain text output (no ANSI colors)")
    args = parser.parse_args()

    sm = StateManager(PROJECT_ROOT)

    if args.priority:
        pid, level = args.priority
        set_priority(sm, pid, int(level))
        return
    if args.pause_miner:
        pause_miner()
        return
    if args.resume_miner:
        resume_miner()
        return

    if args.plain:
        render_plain(sm)
    else:
        render_dashboard(sm)


if __name__ == "__main__":
    main()
