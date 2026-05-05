# Cluster Validation

The project must be validated on `gpu003`, not in the local editing
environment. Setup is covered in `docs/operations.md`. This document is the
acceptance checklist.

## Healthy pipeline definition

The system is considered healthy when all of the following hold:

- `probe` shows local GPU, system, logs, and LLM as `OK`
- `collect --no-remote` populates the database
- `status` shows non-zero, growing counts in `gpu_snapshots` and
  `system_snapshots`
- `summarize --dry-run` produces a sensible prompt with a `Data quality`
  section
- `summarize` writes a Markdown report under `reports/`

## Validation sequence

```bash
cd ~/class_projects/wesley-gpu-monitor/gpu-log-summary
.venv/bin/python -m src.cli probe                       # reachability
.venv/bin/python -m src.cli collect --no-remote         # one cycle
.venv/bin/python -m src.cli status                      # row counts
.venv/bin/python -m src.cli show --hours 1              # recent data
.venv/bin/python -m src.cli analyze --hours 1           # anomaly pass
.venv/bin/python -m src.cli summarize --hours 1 --dry-run
.venv/bin/python -m src.cli summarize --hours 1
```

Optional extended validation: run `collect --no-remote` several times
spaced 5 minutes apart, then dry-run again. Sample counts per GPU should
rise and the data-quality block should soften its sparse-window warnings.

## Cron validation

```bash
bash scripts/install_cron.sh
crontab -l                    # three entries: collect, summarize, watchdog
tail -n 100 logs/collect.log
tail -n 100 logs/summarize.log
```

Look for repeated successful collections on a 5-minute beat and report
generation entries at 00:00 / 12:00 UTC.

## Optional integration probe

When an optional source becomes available (Prometheus URL or S3 bucket,
AlertManager URL, Redfish credentials, multi-node SSH access), update
`config.yaml`, then re-run `probe` and `collect`. The corresponding line
in `probe` should change from `SKIP` to `OK`, and the matching SQLite
table should begin accumulating rows. The summarizer prompt will pick up
the new context automatically.

## Running unit tests

```bash
.venv/bin/python -m pytest tests/test_s3_prometheus.py
```

Targeted automated coverage exists for the S3 Prometheus collector
(probe, snapshot parsing, bookmark progression, prefix filtering, SQLite
insertion). It does not replace on-node validation of the full pipeline.

## Common failure modes

- **`probe` says GPU unavailable** — check `nvidia-smi`, NVML, and
  driver state on the node.
- **Logs unreadable** — check file path, permissions, and that the user
  running the pipeline can read `/var/log/...`.
- **`summarize --dry-run` works but real `summarize` fails** — vLLM is
  almost always the cause (down, wrong model name, prompt too large).
  Check `logs/vllm.log` first.
- **Report feels overconfident** — check sample counts in the
  `Data quality` section and whether enough collection cycles ran before
  summarization.
