# Cluster Testing Guide

## Scope

This project must be validated on the cluster, not in the local editing
environment. The primary execution target is `gpu003`.

Do not run the application locally. When you need validation, use the commands
below on the cluster node and inspect the resulting database, logs, and report
artifacts there.

## Preconditions

Expected environment:
- node: `gpu003`
- GPUs: `8x NVIDIA H200`
- local LLM endpoint: `http://localhost:30000/v1`
- model: `Qwen/Qwen3.5-397B-A17B`

Expected readable local logs:
- `/var/log/fabricmanager.log`
- `/var/log/ibacm.log`
- `/var/log/nvidia-dcgm/`

Important practical note:
- optional remote integrations may still show as unavailable, and that is
  acceptable if their URLs, credentials, or access paths are not yet in place

## Recommended Validation Order

Run the steps in this order.

### 1. Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

If a specific package is still missing, install it directly:

```bash
python3 -m pip install <package>
```

Expected result:
- installation completes without import errors for runtime dependencies

### 2. Probe runtime reachability

```bash
python3 -m src.cli probe
```

Expected result:
- local GPU: `OK`
- system: `OK`
- Fabric Manager and InfiniBand logs: `OK` if readable on this node
- LLM: `OK` and shows the configured Qwen model
- optional integrations may show `SKIP` when unconfigured or unreachable

Interpretation:
- a `SKIP` on optional integrations is acceptable
- a failure on local GPU, local logs, or the LLM blocks the main product path

### 3. Run a local-only collection cycle

Use local-only collection until the remote integrations are configured and
reachable:

```bash
python3 -m src.cli collect --no-remote
```

Expected result:
- GPU snapshots inserted
- one system snapshot inserted
- new Fabric Manager and InfiniBand log events parsed if present
- command ends with `Collection complete.`

If there are no recent notable log lines, the log count may legitimately be
zero.

### 4. Inspect database health

```bash
python3 -m src.cli status
```

Expected result:
- row counts for core tables
- a valid SQLite file path
- non-zero counts in `gpu_snapshots` and `system_snapshots` after collection

Useful interpretation:
- `bookmarks` is not shown here, but incremental log parsing depends on it
- row growth across repeated runs is the quickest sign that collection is
  working end to end

### 5. Inspect recent local telemetry

```bash
python3 -m src.cli show --hours 1
```

Expected result:
- rows for the recent GPU samples
- visible fields such as utilization, memory, temperature, power, ECC, and
  throttle reasons

To inspect recent events instead:

```bash
python3 -m src.cli show --events --hours 1
```

Expected result:
- recent Fabric Manager or InfiniBand events if any were parsed

### 6. Run anomaly detection

```bash
python3 -m src.cli analyze --hours 1
```

Expected result:
- either detected anomalies with severities
- or a clear `No anomalies detected` message

Interpretation:
- no anomalies is a perfectly valid healthy-state outcome
- sparse sampling means threshold findings are more likely than z-score findings

### 7. Dry-run the summarization prompt

```bash
python3 -m src.cli summarize --hours 1 --dry-run
```

Expected result:
- prints the system prompt and user prompt without spending LLM output
- shows prompt length
- includes a `Data quality` section

This is the most important debugging step for report quality because it lets
you inspect what the model would actually receive.

Operationally, this is also how you verify the exact LLM request shape in
practice: the application sends one fixed `system` prompt plus one generated
`user` prompt, with no tool-calling layer in between. See
`docs/architecture.md` for the full interaction contract.

### 8. Generate a real report

```bash
python3 -m src.cli summarize --hours 1
```

Expected result:
- LLM request completes
- a Markdown report is written under `reports/`
- the CLI prints token counts, latency, anomaly count, and incident count

Report path pattern:

```text
reports/report_YYYY-MM-DD_HHMMSS_gpu003.md
```

## Recommended Extended Validation

### Repeated collection cadence

To validate that the system becomes more informative with denser sampling, run
collection several times over time, then summarize again:

```bash
python3 -m src.cli collect --no-remote
sleep 300
python3 -m src.cli collect --no-remote
sleep 300
python3 -m src.cli collect --no-remote
python3 -m src.cli summarize --hours 1 --dry-run
```

What to look for:
- higher sample counts per GPU in the prompt
- better basis for trend discussion
- less chance of over-interpreting a one-point snapshot

### Cron validation

Install the scheduled jobs:

```bash
bash scripts/install_cron.sh
```

Expected result:
- collection every 5 minutes
- daily summarization at `06:00 UTC`

Then inspect the cron wrappers' logs:

```bash
tail -n 100 logs/collect.log
tail -n 100 logs/summarize.log
```

What to look for:
- repeated successful collections
- no import or path errors
- report generation entries at the expected time

## Optional Integration Testing

### Prometheus via S3

Once the cluster admin provides:
- bucket
- prefix
- credentials or IAM path

update `config.yaml` and run:

```bash
python3 -m src.cli probe
python3 -m src.cli collect
python3 -m src.cli status
python3 -m src.cli summarize --hours 1 --dry-run
```

Expected result:
- `Prometheus (S3)` changes from `SKIP` to `OK`
- `prometheus_snapshots` begins accumulating rows
- the prompt now includes Prometheus context

### Direct Prometheus

If a direct URL is provided later, validate it the same way, but treat it as a
secondary path compared with the agreed S3 snapshot workflow.

### AlertManager, Redfish, and multi-node SSH

Once those paths are configured, repeat:

```bash
python3 -m src.cli probe
python3 -m src.cli collect
python3 -m src.cli summarize --hours 1 --dry-run
```

Expected result:
- the new sources appear in `probe`
- their tables or derived log events begin showing up in SQLite
- the prompt gains new context sections

## Unit Test Coverage You Can Run on the Cluster

There is currently targeted automated coverage for the S3 Prometheus collector.

Run:

```bash
python3 -m pytest tests/test_s3_prometheus.py
```

What it covers:
- S3 probe behavior
- snapshot parsing
- bookmark progression
- prefix filtering
- SQLite insertion path for Prometheus snapshots

This does not replace full on-node validation of the main report pipeline, but
it is useful when changing the S3 ingestion path.

## Common Failure Modes

### `probe` says GPU is unavailable

Check:
- `nvidia-smi`
- NVML availability
- driver state on the node

### log sources show unreadable

Check:
- file path exists
- file permissions
- whether the process user can read `/var/log/...`

### summarization dry-run works but real summarize fails

Check:
- local vLLM is serving
- configured model name matches what `models.list()` exposes
- prompt size is not excessively large for the runtime configuration

### report quality seems overconfident

Check:
- sample counts in the `Data quality` section
- whether enough collection cycles happened before summarization
- whether the local logs were actually present for the window

## Minimum “Healthy Pipeline” Definition

On `gpu003`, the project should be considered basically healthy when all of the
following are true:
- `probe` shows local GPU, system, logs, and LLM as `OK`
- `collect --no-remote` populates the SQLite database
- `status` shows increasing row counts
- `summarize --dry-run` produces a sensible prompt with data-quality guidance
- `summarize` writes a Markdown report successfully

That is the core acceptance path today.
