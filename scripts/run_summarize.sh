#!/usr/bin/env bash
# Wrapper for cron: generates a daily LLM summary with logging.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

python3 -m src.cli summarize --hours 24 >> "$LOG_DIR/summarize.log" 2>&1
