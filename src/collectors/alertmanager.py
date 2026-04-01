"""
AlertManager collector — pulls currently firing alerts.

Converts alerts into LogEvent-compatible format so they feed into the
anomaly detection and event correlation pipeline.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from src.collectors.log_parser import LogEvent

logger = logging.getLogger(__name__)


def probe(url: str, timeout: int = 5) -> bool:
    """Check if AlertManager is reachable."""
    if not url:
        return False
    try:
        resp = requests.get(f"{url.rstrip('/')}/api/v2/status", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def collect_alerts(config: dict) -> list[LogEvent]:
    """Fetch firing alerts from AlertManager and convert to LogEvents."""
    am_config = config.get("alertmanager", {})
    if not am_config.get("enabled", False):
        return []

    url = am_config.get("url", "")
    if not url:
        logger.debug("AlertManager URL not configured, skipping")
        return []

    if not probe(url):
        logger.info("AlertManager at %s not reachable, skipping", url)
        return []

    try:
        resp = requests.get(
            f"{url.rstrip('/')}/api/v2/alerts",
            params={"active": "true", "silenced": "false", "inhibited": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        alerts = resp.json()
    except Exception as e:
        logger.warning("Failed to fetch alerts from AlertManager: %s", e)
        return []

    events = []
    for alert in alerts:
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        status = alert.get("status", {})

        severity = labels.get("severity", "warning")
        if severity not in ("critical", "warning", "info"):
            severity = "warning"

        level = "ERROR" if severity == "critical" else "WARN"

        alert_name = labels.get("alertname", "unknown")
        instance = labels.get("instance", "")
        summary = annotations.get("summary", "")
        description = annotations.get("description", "")

        message = f"[{alert_name}] {summary or description}"
        if instance:
            message += f" (instance: {instance})"

        starts_at = alert.get("startsAt", datetime.now(timezone.utc).isoformat())

        events.append(LogEvent(
            timestamp=starts_at,
            source="alertmanager",
            level=level,
            category=f"alert_{alert_name}",
            message=message,
            raw_line=str(alert),
            metadata={
                "alertname": alert_name,
                "severity": severity,
                "instance": instance,
                "labels": labels,
                "annotations": annotations,
                "state": status.get("state", "active"),
            },
        ))

    logger.info("AlertManager: collected %d firing alerts", len(events))
    return events
