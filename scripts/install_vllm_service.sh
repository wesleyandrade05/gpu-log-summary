#!/usr/bin/env bash
# Installs vLLM as a systemd user service that starts on login and restarts
# automatically on crash.
#
# Requirements:
#   - .venv must already be set up (run: python3.12 -m venv .venv && .venv/bin/pip install ...)
#   - systemd must be available (standard on Ubuntu/Debian clusters)
#
# Usage:
#   bash scripts/install_vllm_service.sh          # install and start
#   bash scripts/install_vllm_service.sh --remove  # uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_VLLM="$PROJECT_DIR/.venv/bin/vllm"
LOG_DIR="$PROJECT_DIR/logs"
SERVICE_NAME="gpu-log-vllm"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/${SERVICE_NAME}.service"

MODEL_PATH="/mnt/superalarm/models/Qwen3.5-397B-A17B-FP8"
MODEL_NAME="Qwen/Qwen3.5-397B-A17B"
PORT=30000
TP=8
MAX_LEN=32768
GPU_MEM_UTIL=0.70

# ── Remove mode ────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--remove" ]]; then
  echo "Removing ${SERVICE_NAME} service..."
  systemctl --user stop  "${SERVICE_NAME}" 2>/dev/null || true
  systemctl --user disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl --user daemon-reload
  echo "Service removed."
  exit 0
fi

# ── Preflight checks ───────────────────────────────────────────────────────────
if [[ ! -x "$VENV_VLLM" ]]; then
  echo "ERROR: $VENV_VLLM not found. Set up the venv first:"
  echo "  python3.12 -m venv .venv"
  echo "  .venv/bin/pip install -r requirements.txt"
  echo "  .venv/bin/pip install vllm==0.18.0"
  exit 1
fi

if [[ ! -d "$MODEL_PATH" ]]; then
  echo "ERROR: Model not found at $MODEL_PATH"
  exit 1
fi

mkdir -p "$SERVICE_DIR" "$LOG_DIR"

# ── Write service file ─────────────────────────────────────────────────────────
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=vLLM inference server (${MODEL_NAME})
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
Environment=FLASHINFER_DISABLE_VERSION_CHECK=1
ExecStart=${VENV_VLLM} serve ${MODEL_PATH} \\
    --served-model-name ${MODEL_NAME} \\
    --port ${PORT} \\
    --tensor-parallel-size ${TP} \\
    --trust-remote-code \\
    --max-model-len ${MAX_LEN} \\
    --gpu-memory-utilization ${GPU_MEM_UTIL}
Restart=always
RestartSec=30
StandardOutput=append:${LOG_DIR}/vllm.log
StandardError=append:${LOG_DIR}/vllm.log

[Install]
WantedBy=default.target
EOF

echo "Service file written to $SERVICE_FILE"

# ── Enable linger so the service survives logout ───────────────────────────────
if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
  echo "Enabling linger for $USER (keeps service running after logout)..."
  loginctl enable-linger "$USER" || echo "WARNING: loginctl failed — service may stop on logout. Ask an admin to run: loginctl enable-linger $USER"
fi

# ── Enable and start ───────────────────────────────────────────────────────────
systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}"
systemctl --user start  "${SERVICE_NAME}"

echo ""
echo "Service installed and started."
echo ""
echo "Useful commands:"
echo "  Status:   systemctl --user status ${SERVICE_NAME}"
echo "  Logs:     journalctl --user -u ${SERVICE_NAME} -f"
echo "  Logfile:  tail -f ${LOG_DIR}/vllm.log"
echo "  Stop:     systemctl --user stop ${SERVICE_NAME}"
echo "  Restart:  systemctl --user restart ${SERVICE_NAME}"
echo "  Remove:   bash scripts/install_vllm_service.sh --remove"
