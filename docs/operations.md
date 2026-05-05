# Operations Guide

This document covers the practical setup, known failure modes encountered in
production on `gpu003`, and their resolutions. It is the right starting point
when something stops working.

## Environment

- Node: `gpu003`
- Python: `python3.12`
- Project venv: `~/class_projects/wesley-gpu-monitor/gpu-log-summary/.venv`
- Model: `Qwen/Qwen3.5-397B-A17B` served from `/mnt/superalarm/models/Qwen3.5-397B-A17B-FP8`
- vLLM version: `0.18.0` (pinned — see version notes below)

Always use the project venv for all CLI commands:

```bash
cd ~/class_projects/wesley-gpu-monitor/gpu-log-summary
.venv/bin/python -m src.cli <command>
```

---

## Initial Setup on a Fresh Checkout

```bash
cd ~/class_projects/wesley-gpu-monitor/gpu-log-summary

# Create venv with the right Python
python3.12 -m venv .venv

# Install project deps + vLLM (pinned version)
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install vllm==0.18.0

# Verify
.venv/bin/python -c "import vllm, pynvml, psutil, requests; print('OK')"
.venv/bin/python -c "import torch; print(torch.cuda.is_available(), torch.cuda.device_count())"

# Install cron jobs
bash scripts/install_cron.sh

# Install vLLM as a persistent systemd service
bash scripts/install_vllm_service.sh
```

---

## Managing the vLLM Service

The vLLM server runs as a systemd user service that auto-restarts on failure
and survives node reboots. Use `scripts/start_vllm.sh` only for ad-hoc manual
starts; the service installer is preferred for sustained operation.

```bash
# Check status
systemctl --user status gpu-log-vllm

# Follow live logs
journalctl --user -u gpu-log-vllm -f
# or
tail -f logs/vllm.log

# Restart after a config change
systemctl --user restart gpu-log-vllm

# Stop temporarily
systemctl --user stop gpu-log-vllm

# Remove the service entirely
bash scripts/install_vllm_service.sh --remove
```

Model loading takes 5–10 minutes after the service starts. The server is ready
when `logs/vllm.log` shows `Application startup complete`.

---

## Known Issues and Resolutions

### 1. vLLM crashes silently — summarization skips with "vLLM not reachable"

**Symptom:** `logs/summarize.log` shows `SKIP: vLLM not reachable on port 30000`.

**Cause:** The vLLM server process died (OOM kill, cluster maintenance, session
termination). Before the systemd service was in place, this happened because
the server ran in a tmux session that could be killed externally.

**Resolution:**
- Install the systemd service: `bash scripts/install_vllm_service.sh`
- The service restarts automatically with `Restart=always` and a 30s delay.
- Check what killed it: `grep -i "oom\|killed" /var/log/syslog | tail -20`

---

### 2. `TypeError: can't compare offset-naive and offset-aware datetimes`

**Symptom:** Every `summarize` run fails at `correlator.py` line 137 with this
error. Anomaly detection runs successfully but the correlator crashes before
reaching the LLM.

**Cause:** Anomaly timestamps stored in SQLite are UTC-aware ISO strings while
some Fabric Manager log timestamps were parsed without timezone info. When both
are fed to the correlator's sort, Python cannot compare the two types.

**Resolution:** Fixed in `src/analysis/correlator.py` — `_parse_ts` now always
returns a UTC-aware datetime by calling `.replace(tzinfo=timezone.utc)` on any
naive datetime it produces.

---

### 3. `ModuleNotFoundError: No module named 'vllm'` when using `python3`

**Symptom:** Running `python3 -m src.cli ...` raises an import error for vllm,
pynvml, or other dependencies.

**Cause:** The system `python3` binary (`/usr/bin/python3`) does not have the
project dependencies. They live in the project venv under `python3.12`.

**Resolution:** Always use `.venv/bin/python` or the venv's explicit binary:

```bash
.venv/bin/python -m src.cli probe
```

Never use bare `python3` or `python3.12` for this project outside the venv.

---

### 4. vLLM version incompatibility with CUDA driver

**Symptom:** vLLM worker crashes with:
```
RuntimeError: The NVIDIA driver on your system is too old (found version 12080).
```

**Cause:** The cluster CUDA driver supports CUDA 12.8. Installing the latest
vLLM via `pip install vllm` pulls `torch>=2.10.0` which requires a newer
driver. vLLM `0.19.0` and later exhibit this problem on this cluster.

**Resolution:** Pin to `vllm==0.18.0`, which resolves to `torch~=2.9.x` and
is compatible with CUDA 12.8:

```bash
.venv/bin/pip install vllm==0.18.0
```

Do not upgrade vLLM without first verifying the resulting torch version is
compatible: `.venv/bin/python -c "import torch; print(torch.cuda.is_available())"`.

---

### 5. `flashinfer` version mismatch warning/crash

**Symptom:** vLLM fails to start with:
```
RuntimeError: flashinfer-cubin version (X) does not match flashinfer version (Y).
```

**Cause:** The installed `flashinfer` and `flashinfer-python` packages have
mismatched versions, which can occur after pip upgrades.

**Resolution:** The `start_vllm.sh` and `install_vllm_service.sh` scripts both
set `FLASHINFER_DISABLE_VERSION_CHECK=1` to bypass this check. If the mismatch
causes actual runtime errors (beyond the startup check), reinstall flashinfer:

```bash
.venv/bin/pip install --force-reinstall flashinfer-python
```

---

### 6. Context window exceeded — summarize fails with HTTP 400

**Symptom:**
```
RuntimeError: Context window exceeded: prompt is too long for max_tokens=8192.
```

**Cause:** The model's context window is 32,768 tokens. With a dense 24h window
and large log history, the prompt can reach ~25,000 tokens. Adding 8,192 output
tokens exceeds the limit.

**Resolution:** Set `max_tokens: 4096` in `config.yaml`. This leaves sufficient
headroom while still producing complete reports:

```yaml
llm:
  max_tokens: 4096
```

If the prompt itself grows too large (e.g. many anomalies + dense logs), reduce
the summarization window: `.venv/bin/python -m src.cli summarize --hours 12`.

---

### 7. `ImportError: cannot import name 'DEFAULT_CIPHERS' from 'urllib3'`

**Symptom:** Running with the system `python3` produces this error when
importing `boto3`.

**Cause:** The system `boto3` (`/usr/lib/python3/dist-packages/boto3`) uses an
old `urllib3` API removed in newer versions installed in `~/.local`.

**Resolution:** Use `.venv/bin/python` which has a consistent, isolated set of
dependencies including a compatible `boto3` and `urllib3`.

---

### 8. `no crontab for yale` after running `install_cron.sh`

**Symptom:** Running `crontab -l` immediately after `install_cron.sh` still
shows this message.

**Cause:** With `set -euo pipefail`, the `grep -v` pipes in the installer return
exit code 1 when the existing crontab is empty, causing the script to abort
before writing the new crontab.

**Resolution:** Fixed in `scripts/install_cron.sh` by appending `|| true` to
the grep pipeline. After the fix, `install_cron.sh` runs cleanly from an empty
crontab state.

---

## Checking System Health

Quick end-to-end health check:

```bash
# Is vLLM up?
curl -s http://localhost:30000/health && echo "LLM UP" || echo "LLM DOWN"

# Is the service running?
systemctl --user status gpu-log-vllm | head -5

# Is collection happening?
tail -n 20 logs/collect.log

# Did summarization run?
tail -n 30 logs/summarize.log

# What does the database look like?
.venv/bin/python -m src.cli status

# Full pipeline check
.venv/bin/python -m src.cli probe
```

---

## Cron Schedule

| Job | Schedule | Script |
|-----|----------|--------|
| Collection | Every 5 minutes | `scripts/run_collect.sh` |
| Summarization | 00:00 and 12:00 UTC | `scripts/run_summarize.sh` |

Reinstall with: `bash scripts/install_cron.sh`

The summarize script skips silently if vLLM is unreachable, so a temporary
server outage does not cause cron errors — it just logs `SKIP: vLLM not
reachable` and exits cleanly.
