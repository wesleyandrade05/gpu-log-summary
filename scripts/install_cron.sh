#!/usr/bin/env bash
# Installs cron jobs for automated collection (every 5 min) and
# daily summarization (6:00 AM UTC).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

COLLECT="$SCRIPT_DIR/run_collect.sh"
SUMMARIZE="$SCRIPT_DIR/run_summarize.sh"
WATCHDOG="$SCRIPT_DIR/watchdog_vllm.sh"

chmod +x "$COLLECT" "$SUMMARIZE" "$WATCHDOG"

CRON_COLLECT="*/5 * * * * $COLLECT"
CRON_SUMMARIZE="0 */12 * * * $SUMMARIZE"
CRON_WATCHDOG="*/10 * * * * $WATCHDOG"

# Append to crontab without duplicating existing entries
(crontab -l 2>/dev/null || true) \
  | grep -v "run_collect.sh" \
  | grep -v "run_summarize.sh" \
  | grep -v "watchdog_vllm.sh" \
  > /tmp/gpu_cron_tmp || true
echo "$CRON_COLLECT"   >> /tmp/gpu_cron_tmp
echo "$CRON_SUMMARIZE" >> /tmp/gpu_cron_tmp
echo "$CRON_WATCHDOG"  >> /tmp/gpu_cron_tmp
crontab /tmp/gpu_cron_tmp
rm -f /tmp/gpu_cron_tmp

echo "Cron jobs installed:"
echo "  Collection:    every 5 minutes"
echo "  Summarization: every 12 hours (00:00 and 12:00 UTC)"
echo "  vLLM watchdog: every 10 minutes (restarts if unreachable)"
echo ""
crontab -l | grep -E "run_collect|run_summarize|watchdog_vllm"
