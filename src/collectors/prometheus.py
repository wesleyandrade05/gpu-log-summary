"""
Prometheus collector — queries GPU and node metrics via PromQL.

Probes the endpoint first; silently skips if unreachable or unconfigured.
When the admin provides the Prometheus URL, update config.yaml and this
collector will automatically start ingesting cluster-wide metrics.
"""

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_QUERIES = [
    {"name": "gpu_temperature", "promql": "DCGM_FI_DEV_GPU_TEMP"},
    {"name": "gpu_utilization", "promql": "DCGM_FI_DEV_GPU_UTIL"},
    {"name": "gpu_memory_used", "promql": "DCGM_FI_DEV_FB_USED"},
    {"name": "gpu_power_draw", "promql": "DCGM_FI_DEV_POWER_USAGE"},
    {"name": "gpu_ecc_dbe", "promql": "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL"},
    {"name": "node_cpu_usage",
     "promql": '100 - (avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'},
    {"name": "node_memory_available", "promql": "node_memory_MemAvailable_bytes"},
    {"name": "node_disk_io_read", "promql": "rate(node_disk_read_bytes_total[5m])"},
    {"name": "node_network_receive", "promql": "rate(node_network_receive_bytes_total[5m])"},
]


@dataclass
class PrometheusResult:
    timestamp: str
    query_name: str
    promql: str
    results: list  # list of {metric: dict, value: [timestamp, value_str]}

    def to_dict(self) -> dict:
        return asdict(self)


def probe(url: str, timeout: int = 5) -> bool:
    """Check if Prometheus is reachable."""
    if not url:
        return False
    try:
        resp = requests.get(f"{url.rstrip('/')}/-/healthy", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def query_instant(url: str, promql: str, timeout: int = 10) -> Optional[list]:
    """Execute an instant PromQL query and return the result vector."""
    try:
        resp = requests.get(
            f"{url.rstrip('/')}/api/v1/query",
            params={"query": promql},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["result"]
        logger.warning("Prometheus query failed: %s", data.get("error", "unknown"))
        return None
    except Exception as e:
        logger.warning("Prometheus query error for '%s': %s", promql, e)
        return None


def query_range(
    url: str, promql: str, start: str, end: str,
    step: str = "5m", timeout: int = 15,
) -> Optional[list]:
    """Execute a range PromQL query."""
    try:
        resp = requests.get(
            f"{url.rstrip('/')}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["result"]
        return None
    except Exception as e:
        logger.warning("Prometheus range query error: %s", e)
        return None


def collect_prometheus_metrics(config: dict) -> list[PrometheusResult]:
    """Run all configured PromQL queries and return structured results."""
    prom_config = config.get("prometheus", {})
    if not prom_config.get("enabled", False):
        return []

    url = prom_config.get("url", "")
    if not url:
        logger.debug("Prometheus URL not configured, skipping")
        return []

    if not probe(url):
        logger.info("Prometheus at %s not reachable, skipping", url)
        return []

    queries = prom_config.get("queries", DEFAULT_QUERIES)
    ts = datetime.now(timezone.utc).isoformat()
    results = []

    for q in queries:
        name = q["name"]
        promql = q["promql"]
        data = query_instant(url, promql)
        if data is not None:
            results.append(PrometheusResult(
                timestamp=ts,
                query_name=name,
                promql=promql,
                results=data,
            ))

    logger.info("Prometheus: collected %d/%d queries", len(results), len(queries))
    return results
