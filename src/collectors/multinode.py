"""
Multi-node GPU collector — runs nvidia-smi on remote GPU nodes via SSH.

Configured by a list of hostnames in config.yaml. Skips unreachable nodes
gracefully. Uses subprocess SSH with a short timeout to avoid blocking.
"""

import logging
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RemoteGpuSnapshot:
    timestamp: str
    node: str
    gpu_index: int
    uuid: str
    name: str
    gpu_util_pct: float
    memory_util_pct: float
    memory_used_mib: int
    memory_total_mib: int
    temperature_c: int
    power_draw_w: float
    power_limit_w: float
    ecc_dbe_volatile: int

    def to_dict(self) -> dict:
        return asdict(self)


def probe_node(hostname: str, timeout: int = 5) -> bool:
    """Check if a node is reachable via SSH."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=%d" % timeout, "-o", "BatchMode=yes",
             hostname, "hostname"],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        return result.returncode == 0
    except Exception:
        return False


def collect_node_gpu_metrics(
    hostname: str, timeout: int = 15,
) -> list[RemoteGpuSnapshot]:
    """Collect GPU metrics from a remote node via SSH + nvidia-smi."""
    query_fields = ",".join([
        "index", "uuid", "name",
        "utilization.gpu", "utilization.memory",
        "memory.used", "memory.total",
        "temperature.gpu",
        "power.draw", "power.limit",
        "ecc.errors.uncorrected.volatile.total",
    ])

    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             hostname,
             f"nvidia-smi --query-gpu={query_fields} --format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("SSH to %s timed out", hostname)
        return []

    if result.returncode != 0:
        logger.warning("nvidia-smi on %s failed: %s", hostname, result.stderr.strip())
        return []

    ts = datetime.now(timezone.utc).isoformat()
    snapshots = []

    for line in result.stdout.strip().split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 11:
            continue

        def safe_int(v, default=0):
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        def safe_float(v, default=0.0):
            try:
                return float(v)
            except (ValueError, TypeError):
                return default

        snapshots.append(RemoteGpuSnapshot(
            timestamp=ts,
            node=hostname,
            gpu_index=safe_int(parts[0]),
            uuid=parts[1],
            name=parts[2],
            gpu_util_pct=safe_float(parts[3]),
            memory_util_pct=safe_float(parts[4]),
            memory_used_mib=safe_int(parts[5]),
            memory_total_mib=safe_int(parts[6]),
            temperature_c=safe_int(parts[7]),
            power_draw_w=safe_float(parts[8]),
            power_limit_w=safe_float(parts[9]),
            ecc_dbe_volatile=safe_int(parts[10]),
        ))

    return snapshots


def collect_multinode_metrics(config: dict) -> dict[str, list[RemoteGpuSnapshot]]:
    """Collect GPU metrics from all configured remote nodes.

    Returns a dict mapping hostname -> list of RemoteGpuSnapshot.
    Skips unreachable nodes.
    """
    mn_config = config.get("multinode", {})
    if not mn_config.get("enabled", False):
        return {}

    nodes = mn_config.get("nodes", [])
    if not nodes:
        return {}

    ssh_timeout = mn_config.get("ssh_timeout", 5)
    results = {}
    reachable = 0

    for hostname in nodes:
        if not probe_node(hostname, timeout=ssh_timeout):
            logger.info("Node %s not reachable via SSH, skipping", hostname)
            continue

        reachable += 1
        snapshots = collect_node_gpu_metrics(hostname)
        if snapshots:
            results[hostname] = snapshots
            logger.info("Node %s: collected %d GPU snapshots", hostname, len(snapshots))

    logger.info("Multi-node: %d/%d nodes reachable, %d total GPU snapshots",
                reachable, len(nodes),
                sum(len(s) for s in results.values()))
    return results
