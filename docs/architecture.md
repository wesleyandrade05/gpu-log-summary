# Architecture

## Purpose

This project is a report-first telemetry summarizer for GPU infrastructure. The
system is designed to run on a cluster node, collect local and optional remote
signals, normalize them into SQLite, detect suspicious conditions, and produce
a Markdown report through a local LLM endpoint.

The most important architectural point is that the product is not a dashboard.
The pipeline exists to produce a useful written operational summary that stays
grounded in the available evidence.

## What Exists Today

Implemented and working for the primary node path:
- local GPU telemetry collection
- local host telemetry collection
- Fabric Manager parsing
- InfiniBand ACM parsing
- SQLite persistence
- threshold anomaly detection
- z-score anomaly detection for sufficiently dense windows
- time-window incident correlation
- prompt construction for a local vLLM-served model
- Markdown report generation
- CLI and cron wrappers

Implemented but only active when access is available:
- direct Prometheus API collection
- S3-backed Prometheus snapshot ingestion
- AlertManager ingestion
- Redfish hardware telemetry
- multi-node SSH snapshots

## System Shape

```text
probe
  checks local dependencies and optional integrations

collect
  local GPU metrics
  local system metrics
  local logs
  optional remote collectors
        |
        v
SQLite
  raw snapshots
  normalized log events
  anomalies
  summaries
  bookmarks
        |
        +--> analyze
        |      threshold + z-score anomaly detection
        |      temporal incident clustering
        |
        +--> summarize
               aggregate query helpers
               prompt builder with data-quality constraints
               local vLLM request
               Markdown report output
```

## Core Runtime Flow

### 1. Configuration load

`src/cli.py` loads `config.yaml` through `_load_config()`. The configuration
controls:
- database path
- collection cadence
- local log paths
- anomaly thresholds
- correlation window
- LLM endpoint and model
- report output directory
- optional collector settings

The project defaults to `data/metrics.db` for SQLite and `reports/` for
generated Markdown output.

### 2. Probe before execution

The `probe` command is the operator-friendly reachability check. It verifies:
- local GPU access with `nvidia-smi`
- `psutil` availability
- readability of configured log paths
- Prometheus direct access
- S3 Prometheus bucket access
- AlertManager reachability
- Redfish reachability
- multi-node SSH reachability
- local vLLM health and model visibility

This is intentionally operational rather than academic. It tells the operator
what the node can actually see right now.

### 3. Collection phase

The `collect` command is the ingestion entrypoint. It has four major segments.

#### Local GPU collection

`src/collectors/gpu_metrics.py` collects one snapshot per GPU for the current
sampling moment. The collector prefers `pynvml`, with `nvidia-smi` as the
overall fallback strategy for environments where NVML bindings are unavailable.

The GPU snapshot contract includes:
- utilization
- memory usage
- temperature
- power draw and limit
- clocks
- ECC counters
- throttle reasons
- running compute processes
- NVLink counters and error counters

The collector returns structured `GpuSnapshot` objects. The CLI inserts them
into `gpu_snapshots` via `MetricsDB.insert_gpu_snapshots()`.

#### Local system collection

`src/collectors/system_metrics.py` uses `psutil` to capture:
- CPU percentage
- CPU count
- host memory totals and utilization
- swap totals and utilization
- disk I/O counters
- network I/O counters
- load averages

These become a single `system_snapshots` row per collection run.

#### Local log collection

`src/collectors/log_parser.py` parses:
- `/var/log/fabricmanager.log`
- `/var/log/ibacm.log`
- `/var/log/nvidia-dcgm/`

Fabric Manager and InfiniBand parsing are incremental. The system stores a
bookmark per source in the `bookmarks` table containing:
- source key
- file path
- byte offset
- inode

This gives the parser three important properties:
- new runs only read appended log lines
- inode changes reset parsing safely after rotation
- truncation resets the read offset instead of silently skipping content

The parser normalizes output into `LogEvent` rows with:
- timestamp
- source
- level
- category
- message
- raw line
- metadata JSON

DCGM directory parsing is currently opportunistic and mostly placeholder
because the directory is usually empty in the observed environment.

#### Optional remote collection

When `collect` runs with remote collection enabled, the CLI also attempts:
- direct Prometheus queries
- S3-backed Prometheus snapshot ingestion
- AlertManager ingestion
- Redfish collection
- multi-node SSH collection

The architectural intent here is explicit: optional integrations should skip
cleanly if the URL, bucket, credentials, or reachability are missing.

## Optional Integration Design

### Direct Prometheus

`src/collectors/prometheus.py` executes configured instant PromQL queries
against a reachable Prometheus endpoint and stores results in
`prometheus_snapshots`.

In the current cluster reality, direct Prometheus access from `gpu003` is not
expected to be the long-term path.

### S3-backed Prometheus

`src/collectors/s3_prometheus.py` is the preferred cluster-wide metric path
when direct Prometheus is unavailable. It:
- lists JSON snapshot objects from a bucket and prefix
- optionally filters them using the last processed S3 key
- parses multiple supported snapshot formats
- converts them into the same `PrometheusResult` shape used by the direct
  collector
- stores the newest processed key as the `prometheus_s3` bookmark

This is a strong architectural choice because it preserves the downstream
contract. Storage, prompt building, and summarization do not care whether the
cluster metrics arrived live from Prometheus or as snapshots from S3.

### AlertManager

`src/collectors/alertmanager.py` turns active alerts into log-like events so
they can join the same anomaly and incident narrative as the local logs.

### Redfish

`src/collectors/redfish.py` is the hardware telemetry path for chassis-level
visibility such as temperatures, fans, power supply state, and hardware event
log entries.

### Multi-node SSH

`src/collectors/multinode.py` is the low-friction cluster expansion path. It
collects remote `nvidia-smi` snapshots over SSH and stores them in
`multinode_snapshots`, giving the summarizer some fleet context without
requiring a larger observability platform.

## Storage Architecture

`src/storage/database.py` is the durable center of the project.

### Primary tables

- `gpu_snapshots`
  Raw per-GPU samples from the local node.

- `system_snapshots`
  Host-level snapshots for CPU, memory, swap, disk, network, and load.

- `log_events`
  Normalized notable events from Fabric Manager, InfiniBand, DCGM, and
  alert-like integrations.

- `anomalies`
  Derived findings produced by the analysis stage.

- `summaries`
  Persisted LLM outputs plus model and token metadata.

- `bookmarks`
  Incremental read state for log files and S3 object progression.

### Optional-source tables

- `prometheus_snapshots`
- `redfish_snapshots`
- `multinode_snapshots`

### Important storage design decisions

- SQLite uses WAL mode for operational simplicity and better write behavior on
  a single node.
- Nested fields such as processes, throttle reasons, NVLink counters, and
  external results are stored as JSON text rather than spread across many
  auxiliary tables.
- Summarization does not read raw GPU rows directly for the core report table.
  It uses aggregate helpers such as `get_gpu_stats_summary()` and
  `get_multinode_stats_summary()` to give the model denser, easier-to-interpret
  context.

## Analysis Architecture

### Threshold detection

`src/analysis/anomaly.py` checks individual snapshots against configured limits
for:
- GPU temperature
- GPU power draw percentage
- GPU memory pressure
- ECC errors
- throttle reasons
- NVLink CRC and replay errors
- system memory pressure
- system CPU saturation
- swap pressure

This produces structured `Anomaly` objects with:
- timestamp
- source
- severity
- metric name
- metric value
- threshold
- description
- optional GPU index

### Statistical detection

The same module can run z-score detection on numeric series when enough samples
exist. The practical behavior matters here:
- the series is grouped per GPU or per logical key
- fewer than 5 points means no z-score analysis for that series
- zero-variance series are ignored

That means statistical detection only becomes useful after the node has been
collecting at a meaningful cadence for long enough. Sparse windows rely mostly
on threshold logic and logs.

### Incident correlation

`src/analysis/correlator.py` merges anomalies and log events into a unified,
time-ordered event stream. Events within the configured correlation window are
grouped into `IncidentCluster` objects.

The resulting cluster summary captures:
- time range
- maximum severity
- participating sources
- participating GPU indices
- counts of anomalies and log events

This matters because the model is not asked to infer relationships from a flat
pile of facts alone. It receives already-clustered candidate incidents.

## Summarization Architecture

### Prompt construction

`src/summarizer/prompt_builder.py` is where report quality is protected most
directly.

The prompt includes:
- node and period header
- explicit data quality section
- aggregated per-GPU statistics
- system summary
- optional multi-node section
- optional Prometheus section
- optional Redfish section
- anomaly section
- log event section
- optional incident cluster section
- final task instruction

The critical product behavior is the data-quality safeguard. When sample counts
are low, the prompt explicitly instructs the model not to over-claim and not to
equate high VRAM plus low utilization with a stuck workload by default.

### LLM invocation

`src/summarizer/llm_client.py` uses the OpenAI-compatible API exposed by the
local vLLM service. The configured target is:
- base URL: `http://localhost:30000/v1`
- model: `Qwen/Qwen3.5-397B-A17B`

The interaction model is intentionally narrow: the application does all data
collection, aggregation, anomaly detection, and incident correlation in Python
first, then sends the model a fully prepared text context package.

### What happens before the LLM call

The `summarize` command in `src/cli.py` does the following before any model
request is made:
- loads the requested time window from SQLite
- fetches raw GPU snapshots and system snapshots
- fetches recent log events
- fetches optional Prometheus, Redfish, and multi-node data if present in the
  database
- recomputes anomalies for that time window
- correlates anomalies and log events into incident clusters
- builds a structured prompt string from those precomputed results

This means the model is not responsible for querying storage, finding logs,
running anomaly logic, or discovering correlations. Those are all deterministic
application-side steps.

### Exact message structure

The request sent to vLLM is a chat-completions request with exactly two
messages:
- one `system` message
- one `user` message

The payload shape is effectively:

```json
{
  "model": "Qwen/Qwen3.5-397B-A17B",
  "messages": [
    {
      "role": "system",
      "content": "<fixed AIOps/report-writing instructions>"
    },
    {
      "role": "user",
      "content": "<full structured telemetry prompt for the selected window>"
    }
  ],
  "max_tokens": 8192,
  "temperature": 0.3
}
```

In implementation terms, `LLMClient.generate_summary()` constructs:
- `messages[0]`
  the fixed system prompt from `get_system_prompt()`
- `messages[1]`
  the generated user prompt from `build_daily_summary_prompt()`

No conversation history is preserved across runs. Each summary request is a
fresh, self-contained one-shot call.

### What is in the system prompt

The system prompt provides behavioral instructions for the model. It tells the
model to:
- act as an AIOps engineer analyzing an NVIDIA H200 cluster node
- write a concise Markdown operations report
- prioritize critical findings first
- use exact values and timestamps
- give recommendations and a risk assessment
- respect sparse-data constraints and avoid overconfident diagnoses

This is a stable instruction layer. It defines the style, caution level, and
reporting expectations.

### What is in the user prompt

The user prompt is a single large structured text block assembled by
`build_daily_summary_prompt()`. It includes:
- cluster/node header and reporting period
- explicit data-quality section
- aggregated GPU metrics summary
- system metrics summary
- optional multi-node GPU section
- optional Prometheus section
- optional Redfish section
- anomaly section
- log-event section
- correlated-incident section when clusters exist
- final task instruction telling the model which report sections to produce

This is the evidence layer. It is where the actual facts for the reporting
window live.

### No tool calling

The current architecture does not use tool calling.

Specifically, the request does not include:
- `tools`
- `functions`
- tool choice settings
- response-format schemas

The model cannot query the database, fetch logs, call external APIs, or invoke
application functions during summarization. It only receives text and returns
text.

This is an intentional design choice:
- it keeps summarization deterministic on the application side
- it makes dry-run inspection easy
- it limits failure modes to prompt construction and model output quality rather
  than multi-step agent behavior

### Exact API used

The client calls the OpenAI-compatible chat completions endpoint via the Python
SDK:

```python
response = self.client.chat.completions.create(
    model=self.model,
    messages=messages,
    max_tokens=max_tokens or self.max_tokens,
    temperature=temperature if temperature is not None else self.temperature,
)
```

At the time of writing, those are the only parameters passed explicitly by the
application.

### Response handling

The client reads the first choice from the response and extracts:
- `message.content`
- fallback reasoning content if Qwen returns reasoning in a separate field
- token usage
- latency
- finish reason

The returned text is then:
- written to a Markdown report under `reports/`
- persisted to the `summaries` table with model and token metadata

### How to inspect the exact request

The architecture is intentionally inspectable. Running:

```bash
python3 -m src.cli summarize --hours 1 --dry-run
```

prints:
- the full system prompt
- the full user prompt
- the final prompt length

This is the easiest way to verify exactly what the model would receive for a
given window without making a live generation call.

The client returns:
- content
- model id
- prompt tokens
- completion tokens
- total tokens
- latency
- finish reason

### Report output

`src/summarizer/report_generator.py` writes a Markdown report under `reports/`
with:
- frontmatter metadata
- node name
- generation time
- summarized time window
- LLM-produced report body

The generated filename pattern is:
- `report_YYYY-MM-DD_HHMMSS_<node>.md`

## CLI Behavior by Command

### `probe`

Operational reachability check. No data is stored.

### `collect`

Stores raw telemetry and normalized event data. This is the ingestion phase.

### `show`

Reads recent GPU snapshots or log events from SQLite for quick operator
inspection.

### `analyze`

Runs anomaly detection and optional incident correlation from stored data.

### `status`

Reports row counts and database size.

### `summarize`

Builds aggregate context, recomputes anomalies and clusters for the selected
window, calls the LLM, writes the report, and stores the summary metadata.

One implementation detail is worth knowing: `summarize` recalculates anomalies
for the selected window and inserts them into the `anomalies` table again. In
practice, that means the anomalies table currently behaves more like a history
of analysis outputs than a deduplicated set of unique findings.

## How This Runs on the Cluster

The project is meant to be edited locally but executed remotely on `gpu003`.

Operational wrappers under `scripts/` provide the default cadence:
- `run_collect.sh`
  runs `python3 -m src.cli collect`
- `run_summarize.sh`
  runs `python3 -m src.cli summarize --hours 24`
- `install_cron.sh`
  installs:
  - collection every 5 minutes
  - summarization daily at `06:00 UTC`

That cadence lines up with the design:
- frequent raw collection
- less frequent report generation

## Known Constraints and Current Practical Limits

- The strongest, most reliable path today is still the single-node path on
  `gpu003`.
- Direct Prometheus is coded but expected to remain unreachable in the current
  environment.
- S3-backed Prometheus is the highest-value next integration once bucket access
  exists.
- AlertManager, Redfish, and multi-node SSH are intentionally optional and
  should never prevent local-node report generation.
- Sparse sampling can mislead the model unless the prompt is explicit about
  confidence limits, which is why prompt safety is a core architecture concern,
  not a cosmetic one.

## What This Architecture Is Optimized For

- fast deployment on a real cluster node
- conservative diagnosis under incomplete telemetry
- extensibility via optional collectors
- simple local persistence and inspection
- report quality over UI surface area

That makes the architecture a good fit for an early operational summarizer,
especially in an environment where observability access is uneven and the most
important outcome is a trustworthy written daily report.
