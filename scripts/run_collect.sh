#!/usr/bin/env bash
# Wrapper for cron: runs one data collection cycle with logging.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) ---" >> "$LOG_DIR/collect.log"
"$PROJECT_DIR/.venv/bin/python" -m src.cli collect >> "$LOG_DIR/collect.log" 2>&1
