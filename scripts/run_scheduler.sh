#!/bin/bash
# Run the ResearchOS scheduler from the correct directory.
#
# Usage:
#   ./scripts/run_scheduler.sh                  # One tick, real LLM
#   ./scripts/run_scheduler.sh --simulate       # One tick, mock agents
#   ./scripts/run_scheduler.sh --loop           # Continuous (60s interval)
#   ./scripts/run_scheduler.sh --simulate --loop

set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

SIMULATE=""
LOOP=""
for arg in "$@"; do
    case "$arg" in
        --simulate) SIMULATE="--simulate" ;;
        --loop)     LOOP="1" ;;
    esac
done

export PYTHONPATH="$ROOT"

if [ -n "$LOOP" ]; then
    echo "Starting scheduler in continuous mode (interval=60s) ..."
    python -m researchos.runtime.scheduler --root "$ROOT" --interval 60 --verbose $SIMULATE
else
    echo "Running single scheduler tick ..."
    python -m researchos.runtime.scheduler --root "$ROOT" --once --verbose $SIMULATE
fi
