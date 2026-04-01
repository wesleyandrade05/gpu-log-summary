"""
Prompt engineering for GPU cluster daily summaries.

Builds structured prompts from collected metrics, anomalies, and log events
that guide the LLM toward producing actionable, well-organized reports.
"""

import json
import logging
from datetime import datetime
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
) -> str:
    """Build the full prompt for a daily summary.

    Assembles GPU stats, anomalies, log events, and system metrics into
    a structured context block that the LLM can reason over.
    """
    sections = []

    sections.append(_build_header(period_start, period_end))
    sections.append(_build_gpu_stats_section(gpu_stats))
    sections.append(_build_system_stats_section(system_snapshots))
    sections.append(_build_anomalies_section(anomalies))
    sections.append(_build_log_events_section(log_events))

    if incident_clusters:
        sections.append(_build_incidents_section(incident_clusters))

    sections.append(_build_task_instruction())

    prompt = "\n\n".join(s for s in sections if s)

    logger.info("Built prompt: %d chars, %d sections", len(prompt), len(sections))
    return prompt


def _build_header(start: str, end: str) -> str:
    return (
        f"## Cluster Telemetry Data\n"
        f"**Node:** gpu003 (8x NVIDIA H200, 143GB VRAM each, NVLink/NVSwitch interconnect)\n"
        f"**Period:** {start[:19]} to {end[:19]} UTC\n"
        f"**Data sources:** nvidia-smi/pynvml GPU metrics, Fabric Manager logs, "
        f"system metrics (psutil)"
    )


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


def _build_anomalies_section(anomalies: list[dict]) -> str:
    if not anomalies:
        return "## Detected Anomalies\nNo anomalies detected — all metrics within normal ranges."

    critical = [a for a in anomalies if a.get("severity") == "critical"]
    warnings = [a for a in anomalies if a.get("severity") == "warning"]

    lines = [f"## Detected Anomalies ({len(critical)} critical, {len(warnings)} warnings)\n"]

    if critical:
        lines.append("### Critical")
        for a in critical:
            gpu_tag = f" [GPU {a['gpu_index']}]" if a.get("gpu_index") is not None else ""
            lines.append(f"- **{a.get('metric_name', '?')}**{gpu_tag}: {a['description']}")

    if warnings:
        lines.append("\n### Warnings")
        for a in warnings[:20]:
            gpu_tag = f" [GPU {a['gpu_index']}]" if a.get("gpu_index") is not None else ""
            lines.append(f"- **{a.get('metric_name', '?')}**{gpu_tag}: {a['description']}")
        if len(warnings) > 20:
            lines.append(f"- ... and {len(warnings) - 20} more warnings")

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


def _build_incidents_section(clusters: list) -> str:
    """Format pre-correlated incident clusters."""
    from src.analysis.correlator import format_clusters_for_llm
    text = format_clusters_for_llm(clusters)
    return f"## Correlated Incidents\n\n{text}"


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
        "8. **Risk Assessment** — what might go wrong in the next 24 hours based on trends\n\n"
        "Use exact values and timestamps. Be concise but thorough."
    )


def get_system_prompt() -> str:
    """Return the system prompt for the LLM."""
    return SYSTEM_PROMPT
