# Next Steps

## Intent

This document captures the highest-value follow-up work for the current
report-first summarizer. The priorities below assume we want better report
quality, broader telemetry coverage, and more reliable on-cluster operation
without turning the project into a UI-heavy monitoring product.

## Priority 1: Unlock Cluster-Wide Context

### 1. Complete S3 Prometheus integration

This is the most important missing operational dependency.

Needed from the cluster side:
- S3 bucket name
- prefix convention
- credentials or IAM access path

Why it matters:
- it is the cleanest path to cluster-wide metrics
- the collector is already implemented
- downstream storage and summarization are already wired for it
- it materially improves report quality without requiring a major redesign

Recommended follow-up after access arrives:
- validate `probe`
- validate `collect`
- inspect `prometheus_snapshots`
- inspect `summarize --dry-run`
- add at least one example configuration block to the docs

### 2. Wire AlertManager when available

Why it matters:
- it injects existing operational knowledge into the same narrative as raw
  telemetry
- it can improve prioritization in the generated report

Needed:
- reachable URL

### 3. Wire Redfish when available

Why it matters:
- it adds chassis-level thermal, fan, power, and hardware event context
- it helps separate OS-level symptoms from hardware-level causes

Needed:
- URL
- credentials

### 4. Enable multi-node SSH collection

Why it matters:
- it is the simplest path from node-level reporting to fleet-aware reporting
- it enables comparative statements across nodes before a full observability
  backend is in place

Needed:
- working SSH from `gpu003` to peer GPU nodes

## Priority 2: Improve Report Quality

### 1. Strengthen sparse-data handling even further

Current prompt safeguards are good, but this is a core product risk area.

Good follow-ups:
- add explicit confidence labels in the report prompt
- differentiate between "single snapshot", "short window", and "dense window"
- surface sample counts more prominently in the final report body

### 2. Add more derived context before the LLM call

Right now the model gets aggregates plus incidents, which is a strong start.
Additional precomputed context could improve precision:
- sustained idle-memory patterns by GPU
- repeated ECC trend summaries
- repeated throttle-event rollups
- log-event frequency summaries by category
- simple "top concerns" ranking before prompting

### 3. Improve prompt sections for optional sources

As more integrations come online, the prompt should stay structured rather than
becoming a dump of raw evidence. A good next step is source-specific summaries
that compress:
- cluster health
- hardware health
- cross-node skew
- active alert themes

## Priority 3: Improve Data and Analysis Reliability

### 1. Reduce anomaly duplication semantics

Today `summarize` recalculates anomalies and inserts them again. That is
acceptable for now, but over time it may make the `anomalies` table feel more
like a log of analysis runs than a clean derived dataset.

Potential improvements:
- add run identifiers
- add deduplication keys
- separate detected anomalies from persisted analysis executions

### 2. Expand automated testing beyond S3 Prometheus

Current automated coverage is strongest for the S3 collector. Good next targets:
- log parser bookmark behavior
- anomaly detection edge cases
- prompt builder sparse-data safeguards
- report generation output shape
- CLI smoke coverage with fixture data

### 3. Add fixture-driven integration tests

A practical medium-term improvement is a small corpus of:
- GPU snapshot fixtures
- system snapshot fixtures
- Fabric Manager log fixtures
- InfiniBand log fixtures
- expected anomaly and incident outputs

That would let us regression-test report inputs without requiring live cluster
conditions.

## Priority 4: Make On-Cluster Operations Smoother

### 1. Add a deployment and operations doc for the cluster user

The testing guide is strong for validation. A separate operator-focused guide
could cover:
- first-time setup
- config editing workflow
- cron installation
- where to inspect failures
- how to rotate or manage report and log retention

### 2. Add explicit environment diagnostics

Useful CLI follow-ups:
- show current config summary
- show last successful collection timestamp
- show bookmark state
- show last successful summary metadata

### 3. Add retention and maintenance utilities

Likely future needs:
- prune old reports
- vacuum or rotate SQLite data
- compact old anomalies or summaries

## Priority 5: Prepare for Broader Cluster Intelligence

These are valuable, but they come after the core telemetry access gaps.

Potential future directions:
- optional Kubernetes context once `kubectl` access exists
- comparative reporting across nodes
- longer-horizon trend summaries
- recurring incident detection
- recommendation templates based on collector-specific evidence

## What Not To Prioritize Right Now

These are intentionally lower priority given the current product goal:
- dashboards
- complex front-end work
- heavy visualization layers
- large platform rewrites before S3 Prometheus is online

The report-first path is still the right focus.

## Suggested Execution Order

If we want the biggest payoff with the least architectural churn, the next work
should happen in this order:

1. get S3 Prometheus access and validate it end to end
2. improve prompt/report behavior for confidence and sparse data
3. expand automated coverage around parsers, anomaly logic, and prompt building
4. bring in AlertManager and Redfish as they become available
5. enable multi-node SSH to widen the report scope

That sequence builds breadth without losing the reliability of the existing
single-node path.
