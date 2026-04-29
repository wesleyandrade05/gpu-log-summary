# GPU Log Summarizer Agent Guide

`AGENTS.md` is the lightweight entrypoint for this repository. It gives agents
the project frame, the operating constraints, and the fastest path into the
deeper documentation.

## Project Overview

This project is a report-first AIOps summarizer for GPU infrastructure.

Today the pipeline is built around:
- collecting local GPU telemetry from `gpu003`
- collecting local system telemetry
- parsing Fabric Manager and InfiniBand logs
- storing structured data in SQLite
- detecting anomalies and correlating incidents
- sending structured context to a local vLLM endpoint
- generating Markdown reports with summary, risks, and recommendations

The current target model on the cluster is:
- endpoint: `http://localhost:30000/v1`
- model: `Qwen/Qwen3.5-397B-A17B`

## Product Direction

Important guardrails:
- We are not building a dashboard right now.
- The primary deliverable is a conservative, high-signal report.
- Optional collectors must fail gracefully when the cluster access is missing.
- The LLM must not make claims stronger than the data supports.

## Non-Negotiable Working Rules

- Do not run project code, install dependencies, create virtual environments, or
  execute tests in the local editing environment.
- All runtime validation happens on the cluster, primarily on `gpu003`.
- When validation is needed, give the user exact commands to run on the
  cluster and explain the expected result.
- Prefer improving report quality, operational clarity, and graceful handling
  of partial data over adding UI.

## Current Status

Working today:
- local GPU metrics
- local system metrics
- Fabric Manager log parsing
- InfiniBand log parsing
- SQLite persistence
- anomaly detection
- temporal incident correlation
- prompt building
- Markdown report generation
- CLI commands for `probe`, `collect`, `show`, `analyze`, `status`, `summarize`
- cron wrapper scripts

Implemented but dependent on cluster access:
- direct Prometheus collector
- S3-backed Prometheus snapshot collector
- AlertManager collector
- Redfish collector
- multi-node SSH collector

Known environment:
- primary node: `gpu003`
- GPUs: `8x NVIDIA H200`
- readable logs:
  - `/var/log/fabricmanager.log`
  - `/var/log/ibacm.log`
  - `/var/log/nvidia-dcgm/` is usually present but often empty

## Read Next

- [Architecture](docs/architecture.md)
  Deep end-to-end system design, runtime flow, storage model, and current
  implementation details.

- [Cluster Testing Guide](docs/testing-on-cluster.md)
  Exact validation flow for running and checking the pipeline on `gpu003`.

- [Operational Workflow](docs/how-it-works-in-practice.md)
  Practical explanation of how the pipeline behaves day to day on the cluster.

- [Next Steps](docs/next-steps.md)
  Prioritized follow-up work, access dependencies, and product hardening areas.

## Quick File Map

- `src/cli.py`
  Main operator entrypoint and the best top-level flow reference.

- `src/collectors/`
  Local collectors plus optional external integrations.

- `src/storage/database.py`
  SQLite schema, inserts, bookmarks, and aggregate helpers.

- `src/analysis/`
  Threshold detection, z-score detection, and temporal correlation.

- `src/summarizer/`
  vLLM client, prompt builder, and report writer.

- `scripts/`
  Cron-oriented wrappers for collection and summarization.

## Agent Notes

- If the user asks for cluster-specific diagnosis, ask them to run commands on
  the cluster and share the results.
- If a URL, bucket, credential, or remote dependency is missing, the expected
  behavior is to skip cleanly rather than fail the whole pipeline.
- Treat sparse telemetry as a first-class product constraint. The report should
  say when confidence is limited.
