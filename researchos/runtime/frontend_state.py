"""Provides research state for the OpenHarness frontend dashboard.

Called by the scheduler or via a slash command to generate a JSON snapshot
that the React frontend can render.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from researchos.runtime.state_manager import StateManager


def get_research_state(project_root: Path) -> dict[str, Any]:
    """Generate a JSON-serializable snapshot of the research state.

    Used by:
    - Frontend dashboard component (via state_snapshot event)
    - /research dashboard slash command
    - scripts/dashboard.py
    """
    sm = StateManager(project_root)
    pipelines = sm.load_all_pipelines()
    budget = sm.load_budget()

    pipeline_data = []
    for p in pipelines:
        pipeline_data.append({
            "id": p.pipeline_id,
            "title": p.title,
            "stage": p.stage.value,
            "step": p.step.value if p.step else None,
            "milestone_count": p.milestone_count,
            "priority": p.priority,
            "budget_used": p.budget_used,
            "budget_allocated": p.budget_allocated,
            "last_error": p.last_error,
            "research_question": p.research_question,
            "created_at": p.created_at,
        })

    # Load cost summaries and tracking URLs per pipeline
    for p_data in pipeline_data:
        pid = p_data["id"]
        cost_path = project_root / "artifacts" / f"pipeline_{pid}" / "cost_report.json"
        if cost_path.exists():
            try:
                from researchos.schemas.cost import CostReport
                report = CostReport.model_validate_json(cost_path.read_text())
                p_data["cost"] = {
                    "total_input_tokens": report.total_input_tokens,
                    "total_output_tokens": report.total_output_tokens,
                    "total_tokens": report.total_tokens,
                    "total_credits": report.total_credits,
                    "total_duration_seconds": round(report.total_duration_seconds, 1),
                    "num_calls": len(report.records),
                }
            except Exception:
                pass
        # Tracking URL
        tracking_url_file = cost_path.parent / "tracking_url.txt"
        if tracking_url_file.exists():
            try:
                p_data["tracking_url"] = tracking_url_file.read_text().strip()
            except Exception:
                pass

    # Sort by priority descending, then by stage
    stage_order = {
        "blocked": 0, "fast_fail": 1, "main": 2, "milestone": 3,
        "paper": 4, "proposal": 5, "completed": 9, "failed": 9,
    }
    pipeline_data.sort(key=lambda p: (-p["priority"], stage_order.get(p["stage"], 5)))

    # Miner status
    miner_paused = (project_root / "runtime" / ".miner_paused").exists()

    # Directions from heartbeat config
    directions = []
    hb_path = project_root / "runtime" / "heartbeat_config.yaml"
    if hb_path.exists():
        try:
            hb = yaml.safe_load(hb_path.read_text()) or {}
            for d in hb.get("directions", []):
                name = d.get("name", "unknown")
                keywords = d.get("keywords", [])
                active = sum(
                    1 for p in pipeline_data
                    if any(kw.lower() in p["title"].lower() for kw in keywords)
                    and p["stage"] not in ("completed", "failed")
                )
                total = sum(
                    1 for p in pipeline_data
                    if any(kw.lower() in p["title"].lower() for kw in keywords)
                )
                directions.append({
                    "name": name,
                    "priority": d.get("priority", "low"),
                    "active": active,
                    "total": total,
                })
        except Exception:
            pass

    # Heartbeat status
    heartbeat_enabled = False
    if hb_path.exists():
        try:
            hb = yaml.safe_load(hb_path.read_text()) or {}
            heartbeat_enabled = hb.get("enabled", False)
        except Exception:
            pass

    # Global cost summary across all pipelines
    cost_summary = {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_tokens": 0,
        "total_credits": 0,
        "total_duration_seconds": 0.0,
        "total_calls": 0,
    }
    for p_data in pipeline_data:
        if "cost" in p_data:
            c = p_data["cost"]
            cost_summary["total_input_tokens"] += c["total_input_tokens"]
            cost_summary["total_output_tokens"] += c["total_output_tokens"]
            cost_summary["total_tokens"] += c["total_tokens"]
            cost_summary["total_credits"] += c["total_credits"]
            cost_summary["total_duration_seconds"] += c["total_duration_seconds"]
            cost_summary["total_calls"] += c["num_calls"]

    return {
        "pipelines": pipeline_data,
        "total_budget_used": sum(budget.usage.values()),
        "total_budget": budget.total_credits,
        "cost_summary": cost_summary,
        "miner_paused": miner_paused,
        "heartbeat_enabled": heartbeat_enabled,
        "directions": directions,
        "pending_decisions": len(list(
            (project_root / "decisions").glob("request_*.yaml")
        )) if (project_root / "decisions").exists() else 0,
    }


def print_research_state_json(project_root: Path) -> None:
    """Print research state as JSON (for backend → frontend protocol)."""
    state = get_research_state(project_root)
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    print_research_state_json(Path("."))
