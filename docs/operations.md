# Operations Guide

Practical setup and the constraints that shaped current operations. For
debugging detail, prefer the source over re-reading this document.

## Environment

| | |
|---|---|
| Node | `gpu003` |
| Python | `python3.12` (project venv at `.venv/`) |
| LLM endpoint | `http://localhost:30000/v1` (local vLLM) |
| Model | `Qwen/Qwen3-235B-A22B-FP8` (see "Model choice" below) |
| vLLM | `0.18.0` (pinned for CUDA 12.8 compatibility) |

All CLI commands must use the venv:

```bash
.venv/bin/python -m src.cli <command>
```

## Setup on a fresh checkout

```bash
cd ~/class_projects/wesley-gpu-monitor/gpu-log-summary
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install vllm==0.18.0

bash scripts/start_vllm.sh          # starts vLLM in tmux session 'vllm'
tail -f logs/vllm.log                # wait for "Application startup complete"

bash scripts/install_cron.sh         # installs the three cron jobs
```

## Cron schedule

| Job | Cadence | Purpose |
|---|---|---|
| `run_collect.sh`   | every 5 min       | collect telemetry → SQLite |
| `run_summarize.sh` | 00:00 / 12:00 UTC | generate Markdown report |
| `watchdog_vllm.sh` | every 10 min      | restart vLLM if unreachable |

`run_summarize.sh` exits cleanly with a `SKIP` line when vLLM is down, so a
temporary outage does not produce cron error mail.

## Model choice

The project does not depend on any specific model — it depends on an
OpenAI-compatible chat endpoint. We started with
`Qwen/Qwen3.5-397B-A17B`, served from a shared FUSE mount
(`/mnt/superalarm/`). That mount proved intermittently unreachable, and when
it hangs vLLM workers enter uninterruptible kernel sleep that survives even
`kill -9`. We switched to `Qwen/Qwen3-235B-A22B-FP8`, which lives in the
local HuggingFace cache and has no FUSE dependency. The new model is in the
same family, FP8-quantized, and more than capable of the structured
summarization this pipeline does. Switching back later is one config line
plus one environment variable for `start_vllm.sh`.

## vLLM serving constraints worth remembering

- **TP=4, not 8.** The FP8 gate/up dimension is 1536. With TP=8 each shard
  is 192, which is not divisible by the FP8 block size of 128 and vLLM
  rejects it. TP=4 → 384 ✓. The remaining four GPUs sit idle for the LLM
  workload, which is fine at two requests per day.
- **Pin `vllm==0.18.0`.** Newer vLLM pulls torch 2.10+, which requires a
  newer CUDA driver than this cluster has (CUDA 12.8).
- **Context window is 32k.** `max_tokens: 4096` in `config.yaml` keeps the
  combined prompt + completion safely under the limit even on dense days.
- **Restart safely.** If vLLM is hung in D-state (FUSE), `kill -9` will not
  work; check `/proc/<pid>/stack` for `fuse_lookup_name` and wait for the
  mount to recover or reboot the node. With the local model in use this
  should no longer occur.

## Health check

```bash
curl -s http://localhost:30000/health && echo "LLM UP" || echo "LLM DOWN"
.venv/bin/python -m src.cli probe
.venv/bin/python -m src.cli status
tail -n 20 logs/collect.log
tail -n 30 logs/summarize.log
```
