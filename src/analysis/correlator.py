"""
Temporal event correlator.

Groups anomalies and log events that occur within a configurable time window
into "incident clusters". Each cluster represents a potentially related set
of events that an LLM can analyze for root-cause relationships.

Example: NVLink CRC errors spike at 14:05, Fabric Manager logs a health mask
change at 14:06, and GPU throttling is detected at 14:07 — these three events
form a single correlated incident.
"""

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

logger = logging.getLogger(__name__)


@dataclass
class CorrelatedEvent:
    """A single event within an incident cluster."""
    timestamp: str
    source: str
    event_type: str       # "anomaly" or "log_event"
    severity: str
    description: str
    gpu_index: Optional[int] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class IncidentCluster:
    """A group of temporally correlated events."""
    cluster_id: int
    start_time: str
    end_time: str
    events: list[CorrelatedEvent] = field(default_factory=list)
    max_severity: str = "info"
    gpu_indices: list[int] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse ISO timestamp, tolerating various formats. Always returns UTC-aware."""
    dt = None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        try:
            dt = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _make_correlated_event(item: dict, event_type: str) -> Optional[CorrelatedEvent]:
    """Convert an anomaly dict or log_event dict into a CorrelatedEvent."""
    ts = item.get("timestamp", "")

    if event_type == "anomaly":
        return CorrelatedEvent(
            timestamp=ts,
            source=item.get("source", "unknown"),
            event_type="anomaly",
            severity=item.get("severity", "info"),
            description=item.get("description", ""),
            gpu_index=item.get("gpu_index"),
            metadata={
                "metric_name": item.get("metric_name"),
                "metric_value": item.get("metric_value"),
                "threshold": item.get("threshold"),
            },
        )
    else:
        return CorrelatedEvent(
            timestamp=ts,
            source=item.get("source", "unknown"),
            event_type="log_event",
            severity=_log_level_to_severity(item.get("level", "INFO")),
            description=item.get("message", ""),
            gpu_index=None,
            metadata={
                "category": item.get("category"),
                "log_metadata": item.get("metadata", {}),
            },
        )


def _log_level_to_severity(level: str) -> str:
    level = level.upper()
    if level in ("ERROR", "FATAL", "CRITICAL"):
        return "critical"
    elif level == "WARN" or level == "WARNING":
        return "warning"
    return "info"


def correlate_events(
    anomalies: list[dict],
    log_events: list[dict],
    time_window_seconds: int = 300,
) -> list[IncidentCluster]:
    """Group anomalies and log events into incident clusters.

    Events are sorted chronologically. A new cluster starts when a gap
    exceeding time_window_seconds is found between consecutive events.
    """
    all_events: list[CorrelatedEvent] = []

    for a in anomalies:
        ev = _make_correlated_event(a, "anomaly")
        if ev:
            all_events.append(ev)

    for le in log_events:
        ev = _make_correlated_event(le, "log_event")
        if ev:
            all_events.append(ev)

    timestamped = []
    for ev in all_events:
        dt = _parse_ts(ev.timestamp)
        if dt:
            timestamped.append((dt, ev))

    timestamped.sort(key=lambda x: x[0])

    if not timestamped:
        return []

    window = timedelta(seconds=time_window_seconds)
    clusters: list[IncidentCluster] = []
    cluster_id = 0

    current_events = [timestamped[0][1]]
    current_start = timestamped[0][0]
    current_end = timestamped[0][0]

    for dt, ev in timestamped[1:]:
        if dt - current_end <= window:
            current_events.append(ev)
            current_end = dt
        else:
            clusters.append(_build_cluster(cluster_id, current_start, current_end, current_events))
            cluster_id += 1
            current_events = [ev]
            current_start = dt
            current_end = dt

    clusters.append(_build_cluster(cluster_id, current_start, current_end, current_events))

    clusters.sort(key=lambda c: SEVERITY_RANK.get(c.max_severity, 9))

    logger.info("Correlated %d events into %d incident clusters",
                len(timestamped), len(clusters))
    return clusters


def _build_cluster(
    cluster_id: int,
    start: datetime,
    end: datetime,
    events: list[CorrelatedEvent],
) -> IncidentCluster:
    """Build an IncidentCluster from a list of events."""
    gpu_indices = sorted({e.gpu_index for e in events if e.gpu_index is not None})
    sources = sorted({e.source for e in events})
    max_sev = min(
        (e.severity for e in events),
        key=lambda s: SEVERITY_RANK.get(s, 9),
    )

    n_anomalies = sum(1 for e in events if e.event_type == "anomaly")
    n_logs = sum(1 for e in events if e.event_type == "log_event")
    gpu_str = f" on GPU(s) {gpu_indices}" if gpu_indices else ""
    duration = (end - start).total_seconds()

    summary = (
        f"[{max_sev.upper()}] {n_anomalies} anomalie(s) + {n_logs} log event(s) "
        f"from {', '.join(sources)}{gpu_str} "
        f"over {duration:.0f}s ({start.strftime('%H:%M:%S')}–{end.strftime('%H:%M:%S')})"
    )

    return IncidentCluster(
        cluster_id=cluster_id,
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        events=events,
        max_severity=max_sev,
        gpu_indices=gpu_indices,
        sources=sources,
        summary=summary,
    )


def format_clusters_for_llm(clusters: list[IncidentCluster]) -> str:
    """Format incident clusters into a structured text block for LLM consumption."""
    if not clusters:
        return "No correlated incidents detected in this period."

    lines = []
    for c in clusters:
        lines.append(f"### Incident #{c.cluster_id + 1} — {c.summary}")
        lines.append(f"Time window: {c.start_time} to {c.end_time}")
        lines.append("")

        for i, ev in enumerate(c.events, 1):
            gpu_tag = f" [GPU {ev.gpu_index}]" if ev.gpu_index is not None else ""
            lines.append(f"  {i}. [{ev.severity.upper()}] [{ev.source}]{gpu_tag} "
                         f"{ev.timestamp}")
            lines.append(f"     {ev.description}")
            if ev.metadata:
                filtered = {k: v for k, v in ev.metadata.items() if v is not None}
                if filtered:
                    lines.append(f"     Data: {filtered}")
            lines.append("")

        lines.append("")

    return "\n".join(lines)
