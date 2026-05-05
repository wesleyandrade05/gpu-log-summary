# How It Works In Practice

The mental model day-to-day: this is a staged reporting pipeline, not a
monitoring platform. Collect → store → derive → prompt → write a report
an operator can read in two minutes.

## What a normal day looks like

Cron runs collection every 5 minutes and summarization every 12 hours
(00:00 / 12:00 UTC). A typical local-only collection cycle on `gpu003`
adds 8 `gpu_snapshots` rows, 1 `system_snapshots` row, and a variable
number of `log_events`. Log parsing is incremental, so cron-driven
collection does not duplicate old log lines.

Reports land under `reports/` as
`report_YYYY-MM-DD_HHMMSS_gpu003.md`.

## Debugging in the right order

When something looks wrong, the question is rarely "what widget broke?"
It is one of:

- did collection happen
- did the right evidence reach SQLite
- did the prompt represent it honestly
- did the model respond within the intended confidence level

Debug in that order. The fastest path:

```bash
.venv/bin/python -m src.cli probe                     # reachability
.venv/bin/python -m src.cli status                    # row counts
.venv/bin/python -m src.cli show --hours 1            # raw evidence
.venv/bin/python -m src.cli show --events --hours 1   # log events
.venv/bin/python -m src.cli summarize --hours 1 --dry-run   # prompt content
.venv/bin/python -m src.cli summarize --hours 1       # full pipeline
```

The dry-run is the highest-leverage debugging step. Many "the LLM is
wrong" reports are actually:

- the window had only one or two samples
- no local logs were present in the window
- optional integrations were unavailable
- the evidence mix was too thin for a confident diagnosis

The dry-run prompt makes that obvious before any tokens are spent.

## Why sparse data drives so much of the design

The most common failure mode for an early telemetry pipeline is false
confidence, not lack of facts. A GPU showing high VRAM with 0%
utilization can mean a stuck workload — but it can also mean a loaded
inference server between requests, a paused job, or a quiet period. The
prompt builder includes an explicit data-quality section so the model
treats sparse windows as hypothesis-generating, not diagnosis-producing.
Report quality therefore depends on collection cadence at least as much
as on the model.

## `analyze` vs `summarize`

| | `analyze` | `summarize` |
|---|---|---|
| Calls the LLM? | no | yes |
| Recomputes anomalies | yes | yes |
| Correlates incidents | yes | yes |
| Writes a Markdown report | no | yes |
| Use when | fast local diagnostic pass | end-of-day operator deliverable |

If `analyze` looks wrong, fix the data or the analysis logic first. If
`analyze` looks right but the report looks wrong, inspect the prompt
with `summarize --dry-run` next.

## Files that matter

- `data/metrics.db` — the operational truth source
- `reports/` — the final output directory
- `logs/collect.log`, `logs/summarize.log`, `logs/vllm.log`,
  `logs/watchdog.log` — cron and serving traces
- `config.yaml` — control surface for thresholds, endpoints, and outputs
