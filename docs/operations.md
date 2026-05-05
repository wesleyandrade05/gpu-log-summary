# Operations Guide

This document covers the practical setup, known failure modes encountered in
production on `gpu003`, and their resolutions. It is the right starting point
when something stops working.

## Environment

- Node: `gpu003`
- Python: `python3.12`
- Project venv: `~/class_projects/wesley-gpu-monitor/gpu-log-summary/.venv`
- Model: `Qwen/Qwen3-235B-A22B-FP8` (see model history below)
- vLLM version: `0.18.0` (pinned — see version notes below)

Always use the project venv for all CLI commands:

```bash
cd ~/class_projects/wesley-gpu-monitor/gpu-log-summary
.venv/bin/python -m src.cli <command>
```

---

## Model Selection History

This section captures why the served model has changed over the lifetime of
the project. The pipeline does not depend on any specific model — only on an
OpenAI-compatible endpoint serving a capable instruction-tuned LLM. The
report-quality difference between the candidates considered here is small
relative to the operational constraints that drove each switch.

### Initial target: `Qwen/Qwen3.5-397B-A17B`

This was the model originally documented in `AGENTS.md`. It is a 397B-parameter
MoE with 17B active parameters, FP8-quantized variant available at
`/mnt/superalarm/models/Qwen3.5-397B-A17B-FP8`. We chose it for capability
ceiling: a large MoE with strong reasoning was attractive for nuanced
operational summaries.

Two problems prevented sustained use:

1. **Mount instability.** The model lives on `/mnt/superalarm/`, a FUSE-backed
   mount that is intermittently unreachable from `gpu003`. When the mount
   hangs, every `stat`/`open` syscall on a path under it blocks indefinitely
   in kernel state `D` (uninterruptible sleep). vLLM workers are unable to
   load shards, cannot be killed even with `kill -9`, and the only recovery
   is for the mount to come back or for the node to be rebooted. This is
   documented in detail in issue #9 below.
2. **Loading footprint.** Even when the mount worked, loading 94 FP8 shards
   across 8 GPUs took 5–10 minutes per restart, which made transient failures
   especially expensive.

### Replacement: `Qwen/Qwen3-235B-A22B-FP8`

We replaced Qwen3.5-397B with `Qwen/Qwen3-235B-A22B-FP8` for the following
reasons:

- **Same family, comparable capability.** Both are Qwen MoE models with FP8
  weights and similar instruction-following behavior on operational text.
  235B with 22B active parameters is the closest available substitute and
  is more than capable for telemetry summarization, anomaly narration, and
  report generation.
- **Lives in the local HuggingFace cache.** The weights are already present
  under `~/.cache/huggingface/hub/models--Qwen--Qwen3-235B-A22B-FP8/`. No
  `/mnt/superalarm/` dependency means no FUSE-related D-state hangs.
- **Operationally identical to the project.** vLLM serves it on the same
  port (30000) with the same OpenAI-compatible API. No code changes are
  needed beyond `config.yaml` and `scripts/start_vllm.sh`. The pipeline
  (collect → analyze → correlate → prompt → summarize) is unchanged.
- **Still uses the full 8-GPU configuration** with `--tensor-parallel-size 8`,
  preserving the scaling behavior we tested earlier.

### Why this change does not weaken the project

The summarizer's job is to produce conservative, high-signal operational
reports given structured evidence. The dominant determinants of report
quality, in our observed runs, are:

1. Sample density of the collected telemetry (covered by the 5-minute cron).
2. The structure of the prompt and the data-quality safeguards
   (`docs/architecture.md`).
3. The honesty of the system prompt about sparse data
   (`src/summarizer/prompt_builder.py`).

Within Qwen MoE family, the difference between 397B-A17B and 235B-A22B on
this kind of structured summarization is small compared to what we gained
in operational stability. Reports continue to be written to `reports/` in the
same format, and the cron schedule (every 12 hours) is unchanged.

### Switching back later

If `/mnt/superalarm/` becomes reliably available again, switching back is a
one-line change in `config.yaml` plus a matching `MODEL_PATH` /
`MODEL_NAME` environment variable for `scripts/start_vllm.sh`. The rest of
the pipeline does not need to change.

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

# Start vLLM
bash scripts/start_vllm.sh
tail -f logs/vllm.log  # wait for "Application startup complete"

# Install cron jobs (collection, summarization, vLLM watchdog)
bash scripts/install_cron.sh
```

---

## Managing the vLLM Server

The vLLM server runs in a tmux session named `vllm`. A cron watchdog checks
every 10 minutes and restarts it automatically if it goes down.

```bash
# Start manually
bash scripts/start_vllm.sh

# Follow startup logs
tail -f logs/vllm.log

# Check if it's up
curl -s http://localhost:30000/health && echo "UP" || echo "DOWN"

# Attach to the tmux session
tmux attach -t vllm

# Watchdog restart history
cat logs/watchdog.log
```

Model loading takes 5–10 minutes. The server is ready when `logs/vllm.log`
shows `Application startup complete`.

---

## Known Issues and Resolutions

### 1. vLLM crashes silently — summarization skips with "vLLM not reachable"

**Symptom:** `logs/summarize.log` shows `SKIP: vLLM not reachable on port 30000`.

**Cause:** The vLLM server process died (OOM kill, cluster maintenance, session
termination). Before the systemd service was in place, this happened because
the server ran in a tmux session that could be killed externally.

**Resolution:**
- Install the cron watchdog: `bash scripts/install_cron.sh`
- The watchdog checks every 10 minutes and restarts vLLM via `start_vllm.sh` if unreachable.
- Restart history is logged to `logs/watchdog.log`.
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

### 9b. FP8 quantization block-size mismatch with TP=8

**Symptom:** When serving `Qwen/Qwen3-235B-A22B-FP8` with `--tensor-parallel-size 8`,
all 8 worker processes fail to start with:

```
ValueError: The output_size of gate's and up's weight = 192 is not divisible
by weight quantization block_n = 128.
```

**Cause:** The model's FP8 gate/up projection has a hidden dimension of 1536.
With TP=8, each GPU receives a 192-wide shard. FP8 block quantization
requires shard dimensions divisible by `block_n = 128`. 192 is not.

Valid TP values for this model:

| TP | Shard size | Divisible by 128 |
|----|-----------|------------------|
| 1  | 1536      | yes              |
| 2  | 768       | yes              |
| 4  | 384       | yes              |
| 8  | 192       | **no**           |

**Resolution:** Use `--tensor-parallel-size 4`. The 250GB FP8 model fits
comfortably on 4×143GB H200s. The remaining 4 GPUs stay idle for the LLM
serving workload, which is acceptable for the project's inference rate
(two reports every 24 hours). The default `TP` in `scripts/start_vllm.sh`
is now 4 to reflect this.

To override for a different model: `TP=2 bash scripts/start_vllm.sh`.

---

### 9. vLLM hangs forever, all worker processes stuck in D state on FUSE

**Symptom:** vLLM startup never progresses past printing the banner. All `vllm`
processes show state `D` (uninterruptible sleep). They cannot be killed even
with `kill -9`. New `vllm serve` invocations hit the same state immediately.
GPU memory shows `0 MiB` on every device.

**Diagnosis:** Check the kernel stack of a stuck process:

```bash
sudo cat /proc/$(pgrep -f "vllm serve" | head -1)/stack
```

If you see `fuse_simple_request` and `fuse_lookup_name` in the stack, the
backing FUSE filesystem (e.g. `/mnt/superalarm/`) is unreachable. The kernel
will not return until the filesystem responds or the node reboots.

**Resolution:**
- Stop using paths under the broken mount. The default model in
  `scripts/start_vllm.sh` and `config.yaml` is now
  `Qwen/Qwen3-30B-A3B-Thinking-2507` which lives in the local HuggingFace
  cache (`~/.cache/huggingface/hub/`) and has no FUSE dependency.
- The zombie D-state processes will only clear when the FUSE mount returns
  or the node is rebooted. Contact a cluster admin if it stays down.
- Once the system is clean (`pgrep -f "vllm serve"` returns nothing),
  start the server normally: `bash scripts/start_vllm.sh`.

To override the default model temporarily without editing the script:

```bash
MODEL_PATH="Qwen/Qwen3-8B" bash scripts/start_vllm.sh
```

---

### 10. `no crontab for yale` after running `install_cron.sh`

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
