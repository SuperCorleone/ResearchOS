"""Canonical path constants for ResearchOS."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Data directories
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
KNOWLEDGE_RAW_DIR = KNOWLEDGE_DIR / "raw"
KNOWLEDGE_WIKI_DIR = KNOWLEDGE_DIR / "wiki"
KNOWLEDGE_INDEX_DIR = KNOWLEDGE_DIR / "index"
KNOWLEDGE_PROPOSALS_DIR = KNOWLEDGE_DIR / "proposals"

ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
SCRATCH_DIR = PROJECT_ROOT / "scratch"

# Data directories (Phase 2 data management)
DATA_DIR = PROJECT_ROOT / "data"
DATA_RAW_DIR = DATA_DIR / "raw"
DATA_PROCESSED_DIR = DATA_DIR / "processed"

RUNTIME_DIR = PROJECT_ROOT / "runtime"
RUNTIME_PIPELINES_DIR = RUNTIME_DIR / "pipelines"
RUNTIME_LOGS_DIR = RUNTIME_DIR / "logs"
BUDGET_FILE = RUNTIME_DIR / "budget.json"

DECISIONS_DIR = PROJECT_ROOT / "decisions"
PAPER_DIR = PROJECT_ROOT / "paper"


def pipeline_dir(pipeline_id: str) -> Path:
    """Return the artifact directory for a pipeline."""
    return ARTIFACTS_DIR / f"pipeline_{pipeline_id}"


def pipeline_state_file(pipeline_id: str) -> Path:
    """Return the state.json path for a pipeline."""
    return pipeline_dir(pipeline_id) / "state.json"
