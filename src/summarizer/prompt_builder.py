"""
Prompt engineering for GPU cluster daily summaries.

Builds structured prompts from collected metrics, anomalies, and log events
that guide the LLM toward producing actionable, well-organized reports.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert AIOps engineer analyzing telemetry from an NVIDIA H200 GPU \
cluster node. You produce concise, actionable daily operations summaries for \
system administrators and ML engineers.

Your summaries must:
1. Start with an executive overview (1-2 sentences: overall health status)
2. Highlight critical issues first, then warnings, then informational observations
3. For each anomaly or incident, explain the likely root cause and impact
4. Correlate related events (e.g., temperature spike + throttling + power draw)
5. Provide specific, actionable recommendations
6. Use exact metric values and timestamps — never be vague
7. End with a brief forward-looking risk assessment

Data quality rules (critical):
- Respect the **Data quality** section in the user message. If samples per GPU are \
very low (e.g. 1–3), state clearly that conclusions are limited and do NOT claim \
definitive diagnoses such as "hung job" or "deadlock" — phrase them as hypotheses.
- High VRAM usage with low GPU utilization in a snapshot often reflects a loaded \
inference server (e.g. vLLM) between requests, not necessarily a stuck training job.
- Host CPU percentage may be misleading on sparse or single-point samples; prefer \
load averages and multiple samples when drawing conclusions.

Keep the full report complete: finish every section including Risk Assessment. \
Prefer shorter paragraphs over hitting output limits.

Format the output as a well-structured Markdown report with clear section headers.
Do NOT include any preamble like "Here is the summary" — start directly with the report.\
"""


def build_daily_summary_prompt(
    gpu_stats: list[dict],
    log_events: list[dict],
    anomalies: list[dict],
    system_snapshots: list[dict],
    period_start: str,
    period_end: str,
    incident_clusters: Optional[list] = None,
    prometheus_data: Optional[list[dict]] = None,
    redfish_data: Optional[list[dict]] = None,
    multinode_stats: Optional[list[dict]] = None,
) -> str:
    """Build the full prompt for a daily summary.

    Assembles GPU stats, anomalies, log events, system metrics, and optional
    Prometheus/Redfish/multi-node data into a structured context block.
    """
    sections = []

    data_sources = ["nvidia-smi/pynvml GPU metrics", "Fabric Manager logs", "system metrics (psutil)"]
    if prometheus_data:
        data_sources.append("Prometheus cluster metrics")
    if redfish_data:
        data_sources.append("Redfish BMC hardware telemetry")
    if multinode_stats:
        data_sources.append("multi-node GPU metrics via SSH")

    sections.append(_build_header(period_start, period_end, data_sources))
    sections.append(_build_data_quality_section(gpu_stats, system_snapshots))
    sections.append(_build_gpu_stats_section(gpu_stats))
    sections.append(_build_system_stats_section(system_snapshots))

    if multinode_stats:
        sections.append(_build_multinode_section(multinode_stats))

    if prometheus_data:
        sections.append(_build_prometheus_section(prometheus_data))

    if redfish_data:
        sections.append(_build_redfish_section(redfish_data))

    sections.append(_build_anomalies_section(anomalies))
    sections.append(_build_log_events_section(log_events))

    if incident_clusters:
        sections.append(_build_incidents_section(incident_clusters))

    sections.append(_build_task_instruction())

    prompt = "\n\n".join(s for s in sections if s)

    logger.info("Built prompt: %d chars, %d sections", len(prompt), len(sections))
    return prompt


def _build_header(start: str, end: str, data_sources: Optional[list[str]] = None) -> str:
    sources = ", ".join(data_sources) if data_sources else "nvidia-smi/pynvml, logs, psutil"
    return (
        f"## Cluster Telemetry Data\n"
        f"**Primary Node:** gpu003 (8x NVIDIA H200, 143GB VRAM each, NVLink/NVSwitch)\n"
        f"**Period:** {start[:19]} to {end[:19]} UTC\n"
        f"**Data sources:** {sources}"
    )


def _build_data_quality_section(
    gpu_stats: list[dict],
    system_snapshots: list[dict],
) -> str:
    """Tell the model how sparse the data is so it avoids over-confident diagnoses."""
    if not gpu_stats:
        return "## Data quality\nNo GPU aggregate rows; treat the window as data-sparse."

    counts = [int(g.get("sample_count") or 0) for g in gpu_stats]
    min_s = min(counts) if counts else 0
    max_s = max(counts) if counts else 0
    sys_n = len(system_snapshots)

    lines = [
        "## Data quality (read this first)\n",
        f"- **GPU metric samples in this window:** min {min_s}, max {max_s} per GPU "
        f"(from the `Samples` column below).",
        f"- **System snapshots in this window:** {sys_n}.",
    ]

    if min_s <= 2:
        lines.append(
            "- **Sparse data:** With only one or two samples per GPU, you MUST NOT "
            "state as fact that workloads are hung, deadlocked, or mis-scheduled. "
            "Offer **possible explanations** (including idle inference with a loaded "
            "model) and recommend collecting more frequent samples or checking "
            "`nvidia-smi` / process lists on the node."
        )
    elif min_s < 12:
        lines.append(
            "- **Moderate sampling:** Trends are somewhat visible; still avoid "
            "absolute claims without corroborating log or alert evidence."
        )
    else:
        lines.append(
            "- **Sampling:** Enough points for basic trend discussion; still "
            "correlate with logs and alerts when available."
        )

    return "\n".join(lines)


def _build_gpu_stats_section(gpu_stats: list[dict]) -> str:
    if not gpu_stats:
        return "## GPU Metrics Summary\nNo GPU data available for this period."

    lines = ["## GPU Metrics Summary (aggregated per GPU)\n"]
    lines.append("| GPU | Samples | Avg Util% | Max Util% | Avg Mem% | Max Mem MiB | "
                 "Avg Temp C | Max Temp C | Avg Power W | Max Power W | DBE Errors |")
    lines.append("|-----|---------|-----------|-----------|----------|-------------|"
                 "------------|------------|-------------|-------------|------------|")

    for g in gpu_stats:
        lines.append(
            f"| {g.get('gpu_index', '?')} "
            f"| {g.get('sample_count', 0)} "
            f"| {g.get('avg_gpu_util', 0):.1f} "
            f"| {g.get('max_gpu_util', 0):.1f} "
            f"| {g.get('avg_mem_util', 0):.1f} "
            f"| {g.get('max_mem_used', 0):,.0f} "
            f"| {g.get('avg_temp', 0):.1f} "
            f"| {g.get('max_temp', 0)} "
            f"| {g.get('avg_power', 0):.0f} "
            f"| {g.get('max_power', 0):.0f} "
            f"| {g.get('total_dbe', 0)} |"
        )

    return "\n".join(lines)


def _build_system_stats_section(snapshots: list[dict]) -> str:
    if not snapshots:
        return "## System Metrics\nNo system data available."

    latest = snapshots[0]

    cpu_values = [s.get("cpu_percent", 0) for s in snapshots]
    mem_values = [s.get("memory_percent", 0) for s in snapshots]
    avg_cpu = sum(cpu_values) / len(cpu_values) if cpu_values else 0
    max_cpu = max(cpu_values) if cpu_values else 0
    avg_mem = sum(mem_values) / len(mem_values) if mem_values else 0
    max_mem = max(mem_values) if mem_values else 0

    return (
        f"## System Metrics Summary\n"
        f"- **CPU:** avg {avg_cpu:.1f}%, max {max_cpu:.1f}% "
        f"({latest.get('cpu_count', '?')} cores)\n"
        f"- **Memory:** avg {avg_mem:.1f}%, max {max_mem:.1f}% "
        f"({latest.get('memory_total_mib', 0):,} MiB total)\n"
        f"- **Swap:** {latest.get('swap_percent', 0):.1f}% used\n"
        f"- **Load Average:** {latest.get('load_avg_1m', 0):.2f} / "
        f"{latest.get('load_avg_5m', 0):.2f} / {latest.get('load_avg_15m', 0):.2f}\n"
        f"- **Network I/O:** {latest.get('net_sent_bytes', 0) / 1e9:.2f} GB sent, "
        f"{latest.get('net_recv_bytes', 0) / 1e9:.2f} GB received (cumulative)"
    )


MAX_CRITICAL = 30
MAX_WARNINGS = 20


def _build_anomalies_section(anomalies: list[dict]) -> str:
    if not anomalies:
        return "## Detected Anomalies\nNo anomalies detected — all metrics within normal ranges."

    critical = [a for a in anomalies if a.get("severity") == "critical"]
    warnings = [a for a in anomalies if a.get("severity") == "warning"]

    lines = [f"## Detected Anomalies ({len(critical)} critical, {len(warnings)} warnings)\n"]

    if critical:
        lines.append("### Critical")
        for a in critical[:MAX_CRITICAL]:
            gpu_tag = f" [GPU {a['gpu_index']}]" if a.get("gpu_index") is not None else ""
            lines.append(f"- **{a.get('metric_name', '?')}**{gpu_tag}: {a['description']}")
        if len(critical) > MAX_CRITICAL:
            lines.append(f"- ... and {len(critical) - MAX_CRITICAL} more critical anomalies")

    if warnings:
        lines.append("\n### Warnings")
        for a in warnings[:MAX_WARNINGS]:
            gpu_tag = f" [GPU {a['gpu_index']}]" if a.get("gpu_index") is not None else ""
            lines.append(f"- **{a.get('metric_name', '?')}**{gpu_tag}: {a['description']}")
        if len(warnings) > MAX_WARNINGS:
            lines.append(f"- ... and {len(warnings) - MAX_WARNINGS} more warnings")

    return "\n".join(lines)


def _build_log_events_section(events: list[dict]) -> str:
    if not events:
        return "## Log Events\nNo notable log events in this period."

    errors = [e for e in events if e.get("level") in ("ERROR", "FATAL", "CRITICAL")]
    warns = [e for e in events if e.get("level") in ("WARN", "WARNING")]
    others = [e for e in events if e.get("level") not in
              ("ERROR", "FATAL", "CRITICAL", "WARN", "WARNING")]

    lines = [f"## Log Events ({len(errors)} errors, {len(warns)} warnings, "
             f"{len(others)} other)\n"]

    if errors:
        lines.append("### Errors")
        for e in errors[:15]:
            lines.append(f"- [{e['source']}] {e['timestamp']}: {e['message'][:200]}")

    if warns:
        lines.append("\n### Warnings")
        for e in warns[:15]:
            lines.append(f"- [{e['source']}] {e['timestamp']}: {e['message'][:200]}")

    return "\n".join(lines)


def _build_prometheus_section(prom_data: list[dict]) -> str:
    """Format Prometheus query results for the prompt."""
    if not prom_data:
        return ""

    lines = ["## Prometheus Cluster Metrics\n"]
    for item in prom_data:
        name = item.get("query_name", "unknown")
        results = item.get("results", [])
        if not results:
            continue
        lines.append(f"### {name} ({item.get('promql', '')})")
        for r in results[:10]:
            metric_labels = r.get("metric", {})
            value = r.get("value", [None, "?"])
            val_str = value[1] if isinstance(value, list) and len(value) > 1 else str(value)
            label_str = ", ".join(f"{k}={v}" for k, v in metric_labels.items()
                                  if k != "__name__")
            lines.append(f"- {label_str}: **{val_str}**")
        if len(results) > 10:
            lines.append(f"- ... and {len(results) - 10} more results")
        lines.append("")

    return "\n".join(lines)


def _build_redfish_section(rf_data: list[dict]) -> str:
    """Format Redfish BMC data for the prompt."""
    if not rf_data:
        return ""

    latest = rf_data[0]
    lines = ["## Redfish BMC Hardware Telemetry\n"]

    temps = latest.get("chassis_temps", [])
    if temps:
        lines.append("### Chassis Temperatures")
        for t in temps:
            status = t.get("status", "")
            reading = t.get("reading_c")
            if reading is not None:
                warn = f" (warning: {t['upper_warning']}C)" if t.get("upper_warning") else ""
                lines.append(f"- {t.get('name', '?')}: **{reading}C** [{status}]{warn}")

    fans = latest.get("fan_readings", [])
    if fans:
        lines.append("\n### Fan Status")
        for f in fans:
            lines.append(f"- {f.get('name', '?')}: {f.get('reading_rpm', '?')} RPM [{f.get('status', '')}]")

    psu = latest.get("power_supplies", [])
    if psu:
        lines.append("\n### Power Supplies")
        for p in psu:
            lines.append(f"- {p.get('name', '?')}: {p.get('power_output_w', '?')}W "
                         f"[{p.get('status', '')}]")

    sel = latest.get("sel_entries", [])
    if sel:
        lines.append(f"\n### System Event Log ({len(sel)} events)")
        for s in sel[:10]:
            lines.append(f"- [{s.get('severity', '?')}] {s.get('created', '?')}: "
                         f"{s.get('message', '')}")

    return "\n".join(lines)


def _build_multinode_section(mn_stats: list[dict]) -> str:
    """Format multi-node GPU stats for the prompt."""
    if not mn_stats:
        return ""

    lines = ["## Multi-Node GPU Overview (remote nodes via SSH)\n"]
    lines.append("| Node | GPU | Name | Samples | Avg Util% | Max Temp C | "
                 "Avg Power W | DBE Errors |")
    lines.append("|------|-----|------|---------|-----------|------------|"
                 "-------------|------------|")

    for g in mn_stats:
        lines.append(
            f"| {g.get('node', '?')} "
            f"| {g.get('gpu_index', '?')} "
            f"| {g.get('name', '?')[:20]} "
            f"| {g.get('sample_count', 0)} "
            f"| {g.get('avg_gpu_util', 0):.1f} "
            f"| {g.get('max_temp', 0)} "
            f"| {g.get('avg_power', 0):.0f} "
            f"| {g.get('total_dbe', 0)} |"
        )

    return "\n".join(lines)


MAX_CLUSTERS = 20


def _build_incidents_section(clusters: list) -> str:
    """Format pre-correlated incident clusters. Clusters are pre-sorted by severity."""
    from src.analysis.correlator import format_clusters_for_llm
    total = len(clusters)
    capped = clusters[:MAX_CLUSTERS]
    text = format_clusters_for_llm(capped)
    suffix = ""
    if total > MAX_CLUSTERS:
        suffix = f"\n\n_(Showing {MAX_CLUSTERS} of {total} incident clusters, highest severity first.)_"
    return f"## Correlated Incidents\n\n{text}{suffix}"


def _build_task_instruction() -> str:
    return (
        "---\n"
        "## Your Task\n\n"
        "Based on ALL the data above, produce a **Daily GPU Cluster Health Report** with:\n\n"
        "1. **Executive Summary** — 2-3 sentences on overall cluster health\n"
        "2. **Critical Issues** — any immediate action items (if none, say so)\n"
        "3. **Anomaly Analysis** — for each anomaly group, explain the likely root cause, "
        "correlate with other signals, and assess impact\n"
        "4. **GPU Utilization Overview** — are GPUs being used efficiently? "
        "Any idle or underutilized GPUs?\n"
        "5. **NVLink & Fabric Health** — any interconnect issues?\n"
        "6. **System Resources** — CPU, memory, or I/O bottlenecks?\n"
        "7. **Recommendations** — specific, prioritized actions\n"
        "8. **Risk Assessment** — what might go wrong in the next 24 hours based on trends "
        "(keep this section to a short paragraph; do not truncate mid-sentence)\n\n"
        "Use exact values and timestamps. If **Data quality** indicates sparse samples, "
        "say so explicitly and keep speculation clearly labeled as hypothesis."
    )


def get_system_prompt() -> str:
    """Return the system prompt for the LLM."""
    return SYSTEM_PROMPT
