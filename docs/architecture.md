# Architecture

## Purpose

A report-first telemetry summarizer for a GPU node. The pipeline collects
local and optional remote signals, stores them in SQLite, derives anomalies
and incident clusters, and produces a Markdown report through a local
LLM endpoint.

The product is deliberately not a dashboard. The deliverable is a written
operational summary that stays grounded in the available evidence.

## System shape

```text
collect ──▶ SQLite ──▶ analyze ──▶ summarize (LLM) ──▶ Markdown report
   ▲          │           │            │
   │          ▼           ▼            ▼
local +    bookmarks   anomalies   prompt builder
optional   raw rows    incidents   data-quality safeguards
sources
```

Five top-level CLI commands map cleanly onto this flow:

| Command     | Role                                                       |
|-------------|------------------------------------------------------------|
| `probe`     | reachability check for local + optional sources + LLM      |
| `collect`   | write fresh telemetry into SQLite                          |
| `show`      | inspect recent rows for an operator                        |
| `analyze`   | run anomaly detection + correlation, no LLM call           |
| `summarize` | build prompt, call LLM, write Markdown report              |

## Collection

`src/collectors/` contains the data sources. The local-node path is the
core deliverable; everything else is an optional enrichment.

**Local (always on):**
- `gpu_metrics.py` — per-GPU snapshot via `pynvml` (utilization, memory,
  temp, power, ECC, throttle reasons, NVLink counters, clocks, processes)
- `system_metrics.py` — host CPU, memory, swap, disk I/O, network, load
- `log_parser.py` — incremental parsing of Fabric Manager and InfiniBand
  ACM logs into `LogEvent` rows

**Optional (skip cleanly when unavailable):**
- direct Prometheus, S3-backed Prometheus snapshots, AlertManager,
  Redfish (BMC), multi-node `nvidia-smi` over SSH

Log parsing uses a `bookmarks` table (path + offset + inode) so cron-driven
collection only reads new lines, handles rotation by inode, and resets on
truncation.

## Storage

`src/storage/database.py` owns SQLite (WAL mode). Primary tables:

- `gpu_snapshots`, `system_snapshots` — raw per-cycle samples
- `log_events` — normalized notable events from all log-like sources
- `anomalies` — derived findings from the analysis stage
- `summaries` — persisted LLM outputs with token/latency metadata
- `bookmarks` — incremental read state for files and S3 keys
- `prometheus_snapshots`, `redfish_snapshots`, `multinode_snapshots` —
  optional sources

Nested fields (processes, throttle reasons, NVLink counters, external
results) are stored as JSON text rather than spread across many auxiliary
tables. The summarizer reads aggregates (`get_gpu_stats_summary()`,
`get_multinode_stats_summary()`) rather than raw rows so the model sees
denser, easier-to-interpret context.

## Analysis

`src/analysis/` runs two complementary detectors and a temporal
correlator:

- **Threshold detection** — `anomaly.py` checks each snapshot against
  configured limits (temperature, power, memory, ECC, throttle reasons,
  NVLink errors, system memory/CPU/swap).
- **Z-score detection** — same module, same `Anomaly` shape, but only on
  series with ≥5 points and non-zero variance. Sparse windows fall back
  to thresholds and logs.
- **Incident correlation** — `correlator.py` merges anomalies and log
  events into a unified time-ordered stream and groups events within the
  configured correlation window into `IncidentCluster` objects with
  severity, sources, and participating GPUs.

The model receives already-clustered candidate incidents rather than a flat
pile of facts.

## Summarization

`src/summarizer/prompt_builder.py` is where report quality is most directly
protected. The prompt has a fixed section structure:

1. Header (node, period, sources)
2. **Data quality** — explicit sample-density notice with confidence
   guidance
3. Aggregated GPU statistics
4. System metrics summary
5. Optional sections (multinode, Prometheus, Redfish)
6. Anomalies (capped, severity-sorted)
7. Log events (capped, level-sorted)
8. Correlated incidents (capped, severity-sorted)
9. Task instruction

The data-quality block instructs the model to avoid overconfident claims
when sample counts are low — particularly to *not* equate "high VRAM + low
util" with a stuck workload by default.

`llm_client.py` calls vLLM's OpenAI-compatible chat completions endpoint
with exactly two messages (system + user), no tool calling, no conversation
history. The system prompt is fixed instructions about tone, sparse-data
behavior, and output structure. The user prompt is the assembled evidence.
The application does all collection, aggregation, anomaly logic, and
correlation in Python first; the model receives a fully-prepared text
context package.

`report_generator.py` writes the response to
`reports/report_YYYY-MM-DD_HHMMSS_<node>.md` and persists summary metadata
in the `summaries` table.

## Why this shape

Several choices are deliberate and worth flagging:

- **Deterministic application-side analysis, narrow LLM contract.** Anomaly
  detection, correlation, and aggregation run in Python before any model
  call. This makes summarization auditable via `summarize --dry-run` and
  removes a large class of agent-style failure modes.
- **Optional sources fail closed.** Each remote collector probes first and
  skips cleanly when unconfigured or unreachable, so the local path always
  produces a report.
- **Conservative prompt over verbose prompt.** Section caps and the
  data-quality block exist because the most likely failure mode for an
  early telemetry pipeline is *false confidence*, not lack of facts.
- **Cron over daemon.** Collection runs from `cron` every 5 minutes, the
  summarizer every 12 hours, and a watchdog restarts vLLM when needed.
  No long-running Python process to babysit.

## Constraints to know

- The strongest path is single-node on `gpu003`. S3 Prometheus is the
  highest-value next source (collector already implemented).
- Sparse sampling will mislead the model unless the prompt is explicit
  about confidence — which is why the prompt structure is treated as a
  product surface, not a cosmetic detail.
- `summarize` recomputes anomalies for the selected window and writes
  them to `anomalies` again, so that table currently behaves more like a
  log of analysis runs than a deduplicated set. This is acceptable for now
  and called out in `docs/next-steps.md`.
