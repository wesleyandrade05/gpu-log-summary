# Next Steps

The follow-up work that would most improve the report-first summarizer,
ordered roughly by impact.

## Operational dependency to flag

The original target model (`Qwen/Qwen3.5-397B-A17B`) lives on a
FUSE-backed cluster mount that is intermittently unreachable. When that
mount hangs, vLLM workers enter uninterruptible kernel sleep and survive
`kill -9`. The project now serves `Qwen/Qwen3-235B-A22B-FP8` from the
local HuggingFace cache, which removes the FUSE dependency without
changing the pipeline. The shared mount remains a cluster-infrastructure
risk worth flagging in any final write-up. See `docs/operations.md` for
the diagnosis path and the (one-line) rollback if the mount becomes
reliable.

## Priority 1 — unlock cluster-wide context

The collectors are already implemented; they only need access.

- **S3 Prometheus** is the highest-value next source. It turns a
  single-node summarizer into one with real cluster context. Needs
  bucket name, prefix convention, and credentials/IAM path.
- **AlertManager** injects existing operational knowledge into the same
  narrative as raw telemetry. Needs a reachable URL.
- **Redfish (BMC)** adds chassis-level thermal, fan, power, and hardware
  event context — useful for separating OS-level symptoms from
  hardware-level causes. Needs a URL and credentials.
- **Multi-node SSH** is the simplest path from node-level to fleet-aware
  reporting. Needs SSH from `gpu003` to peer GPU nodes.

Validation pattern after enabling any of these: `probe`, `collect`,
`status`, then `summarize --dry-run` and confirm new context appears.

## Priority 2 — improve report quality

- Strengthen sparse-data handling further: explicit confidence labels,
  distinct prompt language for "single snapshot" vs "short window" vs
  "dense window", and surface sample counts in the report body.
- Add more derived context before the LLM call: sustained idle-memory
  patterns, ECC and throttle rollups, log-event frequency by category,
  and a simple "top concerns" ranking.
- Keep the prompt structured as more sources come online — source-specific
  summaries instead of a raw-evidence dump.

## Priority 3 — analysis and data hygiene

- Reduce anomaly duplication semantics. Today `summarize` recomputes
  anomalies and inserts them again, which is fine for now but makes the
  `anomalies` table feel like a log of analysis runs. Add run identifiers
  or deduplication keys.
- Expand automated coverage beyond the S3 collector: log parser bookmark
  behavior, anomaly-detection edge cases, prompt builder sparse-data
  safeguards, and a small fixture corpus to regression-test report inputs
  without live cluster conditions.

## Priority 4 — operational polish

- Retention/maintenance utilities: prune old reports, vacuum SQLite,
  compact old anomalies and summaries.
- Diagnostics CLI: show current config summary, last successful
  collection, bookmark state, last successful summary metadata.

## Explicitly not now

Dashboards, complex front-end work, large platform rewrites. The
report-first path is still the right focus.

## Suggested execution order

1. S3 Prometheus access and end-to-end validation
2. Prompt/report improvements for confidence and sparse data
3. Test coverage for parsers, anomaly logic, and prompt building
4. AlertManager and Redfish as access becomes available
5. Multi-node SSH to widen the report scope
