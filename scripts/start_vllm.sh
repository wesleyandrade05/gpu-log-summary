#!/usr/bin/env bash
# Starts the vLLM OpenAI-compatible server inside a tmux session named "vllm".
# Requires: .venv is set up and the model is in the HuggingFace cache.
# Usage: bash scripts/start_vllm.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_VLLM="$PROJECT_DIR/.venv/bin/vllm"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/vllm.log"
mkdir -p "$LOG_DIR"

MODEL_PATH="/mnt/superalarm/models/Qwen3.5-397B-A17B-FP8"
MODEL_NAME="Qwen/Qwen3.5-397B-A17B"
PORT=30000
TP=8
MAX_LEN=32768
GPU_MEM_UTIL=0.70

if ! command -v tmux &>/dev/null; then
  echo "ERROR: tmux is not installed. Install it or start the server manually."
  exit 1
fi

if tmux has-session -t vllm 2>/dev/null; then
  echo "A tmux session named 'vllm' already exists."
  echo "Attach with: tmux attach -t vllm"
  echo "To restart, kill it first: tmux kill-session -t vllm"
  exit 1
fi

echo "Starting vLLM in tmux session 'vllm'..."
echo "  Model path:  $MODEL_PATH"
echo "  Model name:  $MODEL_NAME"
echo "  Port:        $PORT"
echo "  TP size:     $TP"
echo "  Max seq len: $MAX_LEN"
echo "  GPU mem util: $GPU_MEM_UTIL"
echo ""

echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) starting ---" >> "$LOG_FILE"

tmux new-session -d -s vllm \
  "bash -c 'export FLASHINFER_DISABLE_VERSION_CHECK=1; $VENV_VLLM serve $MODEL_PATH \
    --served-model-name $MODEL_NAME \
    --port $PORT \
    --tensor-parallel-size $TP \
    --trust-remote-code \
    --max-model-len $MAX_LEN \
    --gpu-memory-utilization $GPU_MEM_UTIL \
    >> $LOG_FILE 2>&1'"

echo "Server starting."
echo "  Follow logs:    tail -f $LOG_FILE"
echo "  Attach session: tmux attach -t vllm"
echo "  Poll readiness: watch -n 15 'curl -s --max-time 3 http://localhost:${PORT}/health && echo READY || echo loading...'"
