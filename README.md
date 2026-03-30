# GPU Cluster Log Summarizer

Automated AIOps pipeline that collects GPU metrics and logs from an NVIDIA H200
cluster node, detects anomalies, and generates LLM-powered daily summaries via
an on-cluster Qwen3.5-397B model served by vLLM.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run a one-shot metric collection
python -m src.cli collect

# View the latest collected metrics
python -m src.cli show

# Generate a daily summary
python -m src.cli summarize

# Start the web dashboard
python -m src.cli dashboard
```

## Architecture

```
Data Collection (every 5 min)
  nvidia-smi / pynvml  -->  GPU metrics
  Fabric Manager logs   -->  NVSwitch/NVLink events
  psutil               -->  System metrics (CPU, mem, disk, net)
        |
        v
  SQLite Storage (time-series metrics + parsed events)
        |
        v
  Analysis (anomaly detection + event correlation)
        |
        v
  LLM Summarization (Qwen3.5-397B via vLLM)
        |
        v
  Output (Markdown reports, web dashboard)
```

## Configuration

Edit `config.yaml` to adjust thresholds, collection intervals, LLM parameters,
and log source paths.

## Project Structure

```
src/
  collectors/       GPU metrics, system metrics, log parsers
  storage/          SQLite database layer
  analysis/         Anomaly detection, event correlation
  summarizer/       LLM client, prompt building, report generation
  dashboard/        Flask web UI
  cli.py            Command-line interface
scripts/            Cron job installation helpers
tests/              Unit tests
```
