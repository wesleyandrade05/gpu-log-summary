"""
System-level metrics collector using psutil.

Collects CPU, memory, disk, and network stats to complement GPU telemetry.
"""

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class SystemSnapshot:
    timestamp: str
    cpu_percent: float
    cpu_count: int
    memory_total_mib: int
    memory_used_mib: int
    memory_percent: float
    swap_total_mib: int
    swap_used_mib: int
    swap_percent: float
    disk_read_bytes: int
    disk_write_bytes: int
    net_sent_bytes: int
    net_recv_bytes: int
    load_avg_1m: float
    load_avg_5m: float
    load_avg_15m: float

    def to_dict(self) -> dict:
        return asdict(self)


def collect_system_metrics() -> SystemSnapshot:
    """Collect a snapshot of system-level metrics."""
    import psutil

    ts = datetime.now(timezone.utc).isoformat()

    cpu_pct = psutil.cpu_percent(interval=1)
    cpu_count = psutil.cpu_count()

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    disk = psutil.disk_io_counters()
    net = psutil.net_io_counters()

    load = psutil.getloadavg()

    return SystemSnapshot(
        timestamp=ts,
        cpu_percent=cpu_pct,
        cpu_count=cpu_count,
        memory_total_mib=mem.total // (1024 * 1024),
        memory_used_mib=mem.used // (1024 * 1024),
        memory_percent=mem.percent,
        swap_total_mib=swap.total // (1024 * 1024),
        swap_used_mib=swap.used // (1024 * 1024),
        swap_percent=swap.percent,
        disk_read_bytes=disk.read_bytes if disk else 0,
        disk_write_bytes=disk.write_bytes if disk else 0,
        net_sent_bytes=net.bytes_sent if net else 0,
        net_recv_bytes=net.bytes_recv if net else 0,
        load_avg_1m=load[0],
        load_avg_5m=load[1],
        load_avg_15m=load[2],
    )
