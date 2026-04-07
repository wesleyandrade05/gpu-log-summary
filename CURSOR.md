# GPU Log Summarizer Agent Guide

## Project Goal

This project builds an automated log and telemetry summarization pipeline for a GPU cluster.

Current product goal:
- Collect real telemetry from a GPU node and related cluster services.
- Store the data locally in SQLite.
- Detect anomalies and correlate related events.
- Feed the structured context into a local LLM running on the cluster.
- Generate a human-readable Markdown report with summary, risks, and recommendations.

Important project direction:
- We are **not** building a dashboard right now.
- The main deliverable is a **report-first AIOps summarizer**.
- The cluster currently has a local vLLM endpoint serving `Qwen/Qwen3.5-397B-A17B`.

## Current Status

Working today:
- Local GPU metric collection from `gpu003`
- Local system metric collection
- Fabric Manager and InfiniBand log parsing
- SQLite storage
- Threshold and statistical anomaly detection
- Temporal incident correlation
- Prompt building for LLM summaries
- Markdown report generation
- CLI commands for probe, collect, show, analyze, summarize
- Cron helper scripts

Partially implemented, but only active when cluster access is available:
- Prometheus collector
- AlertManager collector
- Redfish collector
- Multi-node SSH collector

## Important Cluster Facts

Known environment from testing:
- Main tested node: `gpu003`
- GPUs: `8x NVIDIA H200`
- Local LLM endpoint: `http://localhost:30000/v1`
- Model: `Qwen/Qwen3.5-397B-A17B`
- Readable logs:
  - `/var/log/fabricmanager.log`
  - `/var/log/ibacm.log`
  - `/var/log/nvidia-dcgm/` exists but is usually empty

Known access gaps at the time of writing:
- Prometheus URL not yet configured / not reachable from `gpu003`
- AlertManager URL not yet configured / not reachable from `gpu003`
- Redfish URL not yet configured / not reachable from `gpu003`
- SSH access to other GPU nodes not yet working from `gpu003`
- `kubectl` access still unavailable

The code is already written so these sources are optional:
- If a URL is empty or a service is unreachable, the collector should skip cleanly.

## File Structure

### Top-level files

- `config.yaml`
  - Main runtime configuration.
  - Holds local collection settings, LLM config, anomaly thresholds, and optional external source config.

- `requirements.txt`
  - Python runtime dependencies for the project.

- `README.md`
  - High-level project readme.
  - Note: it may lag behind the current implementation and should not be treated as the most precise agent guide.

- `CURSOR.md`
  - This file.
  - Intended as the best quick-start reference for future agents.

## Source Layout

### `src/cli.py`

Main operator entrypoint.

Commands:
- `probe`
  - Checks which data sources are currently reachable.
- `collect`
  - Runs one collection cycle.
- `show`
  - Shows recent GPU or log data from SQLite.
- `analyze`
  - Runs anomaly detection and event correlation.
- `status`
  - Shows database table counts and DB size.
- `summarize`
  - Builds the prompt, calls the LLM, and writes a report.

This file is the best place to understand the runtime flow.

### `src/collectors/`

Collectors ingest data from different sources.

- `gpu_metrics.py`
  - Local GPU metrics via `pynvml` first, `nvidia-smi` fallback.
  - Collects:
    - GPU utilization
    - memory usage
    - temperature
    - power draw / power limit
    - ECC counters
    - clocks
    - throttle reasons
    - running GPU processes
    - NVLink counters when available

- `system_metrics.py`
  - Local system telemetry via `psutil`.
  - Collects:
    - CPU utilization
    - memory usage
    - swap
    - disk I/O
    - network I/O
    - load averages

- `log_parser.py`
  - Parses local logs incrementally using file-offset bookmarks.
  - Supported today:
    - Fabric Manager
    - InfiniBand ACM
    - DCGM directory parsing
  - Produces normalized `LogEvent` objects.

- `prometheus.py`
  - Optional Prometheus collector.
  - Intended to query cluster-wide metrics through PromQL.
  - Skips automatically if URL is unset or unreachable.

- `alertmanager.py`
  - Optional AlertManager collector.
  - Pulls firing alerts and converts them into log-like events for the pipeline.

- `redfish.py`
  - Optional Redfish BMC collector.
  - Intended to collect:
    - chassis temperatures
    - fans
    - power supplies
    - SEL / hardware event log

- `multinode.py`
  - Optional SSH-based collector for remote GPU nodes.
  - Runs `nvidia-smi` on remote nodes and stores remote GPU snapshots.

### `src/storage/database.py`

SQLite storage layer.

Core tables:
- `gpu_snapshots`
- `system_snapshots`
- `log_events`
- `anomalies`
- `summaries`
- `bookmarks`

Optional-source tables:
- `prometheus_snapshots`
- `redfish_snapshots`
- `multinode_snapshots`

This file also contains aggregate query helpers used by `summarize`.

### `src/analysis/`

- `anomaly.py`
  - Threshold-based and z-score-based anomaly detection.
  - Handles GPU and system anomalies.

- `correlator.py`
  - Groups anomalies and log events into time-windowed incident clusters.

### `src/summarizer/`

- `llm_client.py`
  - OpenAI-compatible client pointed at the local vLLM endpoint.

- `prompt_builder.py`
  - Builds the structured prompt used for daily summaries.
  - Includes explicit data-quality guidance so the LLM does not overstate sparse telemetry.

- `report_generator.py`
  - Writes the final Markdown report to `reports/`.

### `scripts/`

- `run_collect.sh`
  - Wrapper for scheduled collection.

- `run_summarize.sh`
  - Wrapper for scheduled summarization.

- `install_cron.sh`
  - Installs cron entries for automated operation.

## Data We Collect and Why

### 1. Local GPU telemetry

Why:
- Core signal for GPU health and workload behavior.
- Needed to detect idle GPUs, memory pressure, thermal problems, ECC errors, and power anomalies.

Main metrics:
- utilization
- memory used / total
- temperature
- power draw
- ECC counts
- throttle reasons
- process holders
- NVLink counters

### 2. Local system telemetry

Why:
- GPU issues are often caused or explained by host-side bottlenecks.
- Useful for distinguishing GPU-side problems from CPU, RAM, disk, or network issues.

Main metrics:
- CPU %
- host memory %
- swap
- disk I/O
- network I/O
- load averages

### 3. Fabric Manager logs

Why:
- Important for NVSwitch / NVLink fabric health.
- Helps explain interconnect problems and GPU communication issues.

### 4. InfiniBand ACM logs

Why:
- Gives signal on cluster networking / RDMA-related issues.
- Useful once multi-node workloads are considered.

### 5. Prometheus metrics (planned / optional)

Why:
- Best source for cluster-wide and historical metrics.
- Lets us summarize beyond a single node.
- Important for future KPIs like utilization, capacity, and cluster health trends.

### 6. AlertManager alerts (planned / optional)

Why:
- Existing operational knowledge is already encoded in cluster alerts.
- Helps the LLM correlate “what the system already thinks is wrong” with raw metrics.

### 7. Redfish hardware telemetry (planned / optional)

Why:
- Needed for chassis-level visibility outside the OS.
- Useful for power, thermal, and hardware event correlation.

### 8. Multi-node SSH snapshots (planned / optional)

Why:
- Lets the product grow from node-level to cluster-level reporting.
- Important for summarizing fleet-wide GPU usage and identifying skew across nodes.

## Manual Testing Flow

Use this exact order when validating on the cluster.

### 1. Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

If a module is missing because of Python version mismatch, install with:

```bash
python3 -m pip install <package>
```

### 2. Check runtime availability

```bash
python3 -m src.cli probe
```

Expected:
- GPU should be OK
- system should be OK
- local logs should be OK
- LLM should be OK
- optional sources may show SKIP until access is configured

### 3. Run one collection cycle

```bash
python3 -m src.cli collect --no-remote
```

Use `--no-remote` until Prometheus / AlertManager / Redfish / SSH are configured.

### 4. Inspect the database

```bash
python3 -m src.cli status
```

### 5. Inspect local GPU data

```bash
python3 -m src.cli show --hours 1
```

### 6. Run anomaly detection

```bash
python3 -m src.cli analyze --hours 1
```

### 7. Inspect the summary prompt without spending LLM output

```bash
python3 -m src.cli summarize --hours 1 --dry-run
```

### 8. Generate a real report

```bash
python3 -m src.cli summarize --hours 1
```

Report output:
- `reports/report_YYYY-MM-DD_HHMMSS_gpu003.md`

## Known Testing Lessons

- A single collection run produces only one sample per GPU and one system snapshot.
- Sparse sampling can cause the LLM to over-interpret the data.
- The prompt was updated to explicitly warn the model when sample counts are low.
- High VRAM with 0% GPU utilization does **not** automatically mean a hung workload.
  - It may also mean:
    - a loaded inference server waiting for requests
    - a paused workload
    - a checkpoint boundary

## What We Still Need From the Cluster

These are the remaining access items needed to make the product fuller and more cluster-aware.

### Needed access

- **Prometheus endpoint URL**
  - Needed for cluster-wide metrics and PromQL-based summaries.

- **AlertManager endpoint URL**
  - Needed to pull currently firing alerts.

- **Redfish endpoint URL + credentials**
  - Needed for BMC-level hardware telemetry.

- **SSH access from gpu003 to other GPU nodes**
  - Needed for the multi-node collector.

- **Optional: kubectl / kubeconfig**
  - Not strictly required for the current report-first product, but useful if we later want Kubernetes object context.

### Once access is available

Update `config.yaml`:
- `prometheus.url`
- `alertmanager.url`
- `redfish.url`
- `redfish.username`
- `redfish.password`

Then run:

```bash
python3 -m src.cli probe
python3 -m src.cli collect
python3 -m src.cli summarize --hours 1 --dry-run
```

## End Goal

The end product should be:
- a report-first AIOps summarizer for GPU infrastructure
- able to run directly on the cluster
- able to ingest local node metrics, node logs, and eventually cluster-wide telemetry
- able to produce concise summaries, recommendations, and risk assessments
- conservative when data quality is low
- stronger and more diagnostic when Prometheus, AlertManager, Redfish, and multi-node access become available

## Agent Guidance

If you are an agent working on this repo:
- Prefer improving the **report quality** over adding UI.
- Do not reintroduce dashboards unless explicitly requested.
- Keep commits small and testable.
- Treat optional collectors as probe-based integrations that must fail gracefully.
- Be careful not to let the LLM make claims stronger than the data supports.
- If the user asks about cluster specifics, ask them to run commands on the cluster and paste the results back.
