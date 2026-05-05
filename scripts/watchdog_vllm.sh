#!/usr/bin/env bash
# Cron watchdog: restarts vLLM if it is not responding on port 30000.
# Add to crontab: */10 * * * * /path/to/scripts/watchdog_vllm.sh
#
# Note: start_vllm.sh refuses to launch when the 'vllm' tmux session
# already exists. That means this watchdog is a no-op while the server
# is loading (5-10 min) — by design, so we don't kill a slow startup.
# If the tmux session is alive but the python process inside it has
# died, kill the session manually before relying on the watchdog.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

PORT=30000

if curl -s --max-time 5 http://localhost:${PORT}/health > /dev/null 2>&1; then
  exit 0
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) vLLM not reachable, restarting..." >> "$LOG_DIR/watchdog.log"
bash "$SCRIPT_DIR/start_vllm.sh" >> "$LOG_DIR/watchdog.log" 2>&1
