#!/usr/bin/env bash
# Wrapper for cron: generates an LLM summary for the last 24h. Skips
# cleanly (no error mail) when the local vLLM endpoint is unreachable.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) ---" >> "$LOG_DIR/summarize.log"

if ! curl -s --max-time 5 http://localhost:30000/health > /dev/null 2>&1; then
  echo "SKIP: vLLM not reachable on port 30000, skipping summary" >> "$LOG_DIR/summarize.log"
  exit 0
fi

"$PROJECT_DIR/.venv/bin/python" -m src.cli summarize --hours 24 >> "$LOG_DIR/summarize.log" 2>&1
