# How It Works In Practice

## The Practical Mental Model

In day-to-day use, this repository behaves less like a monitoring platform and
more like a staged reporting pipeline:

1. collect facts
2. keep them locally
3. derive suspicious patterns
4. give the model structured evidence
5. write a report an operator can read quickly

That practical framing matters because it changes how we debug the system. When
something looks wrong, the first question is usually not "what widget broke?"
It is:
- did collection happen
- did the right evidence reach SQLite
- did the prompt represent it honestly
- did the model respond within the intended confidence level

## What a Normal Day Looks Like

### Collection cadence

The intended steady-state path is:
- `scripts/run_collect.sh` every 5 minutes via cron
- `scripts/run_summarize.sh` every 12 hours at `00:00` and `12:00 UTC`
- vLLM server running as a persistent systemd user service (see `docs/operations.md`)

That means the database becomes the day’s fact store, and the report is a
derived artifact built later from those accumulated facts.

### Data accumulation

Every collection cycle typically adds:
- one GPU snapshot row per local GPU
- one system snapshot row
- zero or more normalized log events
- zero or more rows from optional integrations when available

On `gpu003`, a single local-only run usually means:
- `8` new `gpu_snapshots` rows
- `1` new `system_snapshots` row
- a small, variable number of `log_events`

### Log ingestion over time

Fabric Manager and InfiniBand parsing are incremental, not full rereads. The
bookmark table preserves where the parser left off. In practice, this means:
- repeated collection does not keep duplicating old log lines
- log rotation is handled by inode checks
- truncation is handled by resetting the offset

That behavior is important when the project is running from cron, because the
logs should behave like an append-only operational feed rather than a repeated
daily import job.

## What the Operator Actually Looks At

Most practical debugging and usage falls into a few loops.

### Loop 1: basic health check

If something feels broken, start here:

```bash
.venv/bin/python -m src.cli probe
.venv/bin/python -m src.cli status
```

This tells you whether:
- the node can still access the local GPU stack
- the configured logs are still readable
- the LLM endpoint is still alive
- the database is still filling with rows

### Loop 2: inspect recent raw evidence

If the report looks suspicious, inspect the raw material:

```bash
.venv/bin/python -m src.cli show --hours 1
.venv/bin/python -m src.cli show --events --hours 1
```

This is how you answer questions like:
- were GPUs actually busy
- was memory pressure really present
- did Fabric Manager emit anything interesting
- was there enough signal in the window for a strong conclusion

### Loop 3: inspect the prompt before blaming the model

If the final report feels off, the highest-value check is:

```bash
.venv/bin/python -m src.cli summarize --hours 1 --dry-run
```

In practice, many “LLM issues” turn out to be one of these:
- the window only had one or two samples
- no local logs were present
- optional integrations were unavailable
- the evidence mix was too thin for a confident diagnosis

The dry-run prompt reveals that immediately.

## Why Sparse Data Matters So Much

This project is intentionally conservative because the common failure mode in an
early telemetry pipeline is false certainty.

A practical example:
- a GPU can show high VRAM usage
- the same snapshot can show 0% utilization
- that still does not prove a stuck workload

In practice it may mean:
- a loaded inference server waiting for requests
- a paused workload
- a quiet period between bursts
- an actual scheduling or execution problem

That is why the prompt builder includes an explicit data-quality section and why
report quality depends heavily on collection cadence.

## How `analyze` and `summarize` Relate

These commands are related but not identical.

### `analyze`

Use this when you want a fast local diagnostic pass without involving the LLM.
It tells you:
- which thresholds fired
- whether any z-score outliers appeared
- how events cluster in time

### `summarize`

Use this when you want the final operator-facing deliverable. It:
- recomputes anomalies for the selected window
- correlates incidents again
- builds an LLM-oriented context package
- writes a Markdown report

Practical implication:
- if `analyze` looks wrong, fix the data or logic first
- if `analyze` looks right but the report looks wrong, inspect the prompt and
  summarization behavior next

## How Optional Sources Fit Into Daily Operations

The repo is intentionally usable even when cluster-wide integrations are absent.

Today the practical hierarchy is:
- local GPU metrics, system metrics, and local logs are the core path
- S3 Prometheus is the highest-value next expansion
- AlertManager, Redfish, and multi-node SSH enrich the report when available

In day-to-day use, optional collectors should behave like enrichments, not
dependencies. If one is missing, the report should still be generated from the
local-node path.

## What Changes When S3 Prometheus Becomes Available

Operationally, S3 Prometheus is the biggest step up because it turns the system
from a primarily node-local summarizer into something with real cluster context.

In practice that adds:
- cluster-wide metric evidence to the prompt
- more historical or broader-scope signals
- better support for statements about utilization, capacity, and comparative
  behavior across infrastructure

It also reduces pressure on the local-only interpretation of events, because the
model has more external evidence to correlate with the node view.

## Files and Artifacts That Matter Most

### `data/metrics.db`

The operational truth source for what the pipeline has collected so far.

### `reports/`

The final output directory. If reports exist but look weak, inspect prompt
quality and sampling density before assuming the model is the only issue.

### `logs/collect.log`

The cron-oriented collection trail.

### `logs/summarize.log`

The cron-oriented summarization trail.

### `config.yaml`

The control surface for thresholds, endpoints, optional access, and output
paths.

## The Most Useful Practical Rule

When the output looks wrong, debug in this order:

1. reachability with `probe`
2. stored evidence with `status` and `show`
3. analysis output with `analyze`
4. prompt content with `summarize --dry-run`
5. final report behavior with `summarize`

That order matches how the system actually works and keeps us from blaming the
wrong layer.
