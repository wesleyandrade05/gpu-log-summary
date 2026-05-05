# GPU Cluster Log Summarizer

Automated report-first AIOps pipeline for GPU infrastructure. The project
collects telemetry from a GPU cluster node, stores structured context in
SQLite, detects anomalies, correlates related events, and generates Markdown
reports through an on-cluster vLLM-served `Qwen/Qwen3.5-397B-A17B` model.

## Product Direction

- Primary deliverable: human-readable operational reports
- Current focus: local-node summarization on `gpu003`, with graceful expansion
  to optional cluster-wide sources
- Non-goal right now: dashboards

## Important Execution Model

This project is developed locally but executed and validated on the cluster.

- edit code locally or through remote IDE workflows
- run the pipeline on `gpu003`
- do not treat the local editing environment as the runtime environment

## Quick Start On The Cluster

Create the virtual environment and install dependencies:

```bash
cd ~/class_projects/wesley-gpu-monitor/gpu-log-summary
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install vllm==0.18.0
```

Start the vLLM inference server (loads in ~5-10 minutes):

```bash
bash scripts/start_vllm.sh
tail -f logs/vllm.log  # wait for "Application startup complete"
```

Install cron jobs (collection every 5 min, summary every 12h, vLLM watchdog every 10 min):

```bash
bash scripts/install_cron.sh
```

Check what is reachable:

```bash
.venv/bin/python -m src.cli probe
```

Run one local-only collection cycle:

```bash
.venv/bin/python -m src.cli collect --no-remote
```

Inspect recent data:

```bash
.venv/bin/python -m src.cli status
.venv/bin/python -m src.cli show --hours 1
.venv/bin/python -m src.cli show --events --hours 1
```

Inspect the summary prompt without spending LLM output:

```bash
.venv/bin/python -m src.cli summarize --hours 1 --dry-run
```

Generate a report:

```bash
.venv/bin/python -m src.cli summarize --hours 1
```

## CLI Commands

- `probe`
  Check which local and optional data sources are reachable.

- `collect`
  Run one collection cycle and store results in SQLite.

- `show`
  Inspect recent GPU metrics or log events from SQLite.

- `analyze`
  Run anomaly detection and event correlation without calling the LLM.

- `status`
  Show table counts and database size.

- `summarize`
  Build a prompt, call the local vLLM endpoint, and write a Markdown report.

## Architecture Summary

```text
GPU metrics + system metrics + logs + optional remote sources
                        |
                        v
                 SQLite persistence
                        |
                        v
       anomaly detection + incident correlation
                        |
                        v
         prompt building with data-quality safeguards
                        |
                        v
        local vLLM summary generation via OpenAI API
                        |
                        v
                 Markdown report output
```

## Project Structure

```text
src/
  cli.py                CLI entrypoint
  collectors/           Local and optional telemetry collectors
  storage/              SQLite schema and query helpers
  analysis/             Anomaly detection and temporal correlation
  summarizer/           Prompt builder, vLLM client, report writer
scripts/                Cron and execution wrappers
tests/                  Targeted automated tests
docs/                   Architecture, testing, operations, and next steps
```

## Documentation

- [AGENTS.md](AGENTS.md)
  Lightweight agent entrypoint and project overview.

- [docs/architecture.md](docs/architecture.md)
  Deep architecture, runtime flow, storage model, and implementation details.

- [docs/testing-on-cluster.md](docs/testing-on-cluster.md)
  Exact validation sequence for `gpu003`.

- [docs/how-it-works-in-practice.md](docs/how-it-works-in-practice.md)
  Practical operating model and debugging workflow.

- [docs/operations.md](docs/operations.md)
  Known failure modes, resolutions, vLLM version notes, and health checks.

- [docs/next-steps.md](docs/next-steps.md)
  Prioritized follow-up work and access dependencies.
