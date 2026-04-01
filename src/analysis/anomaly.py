"""
Anomaly detection for GPU cluster metrics.

Two detection strategies:
1. Threshold-based: fixed limits for temperature, power, memory, ECC errors
2. Statistical: z-score deviation from rolling mean for any numeric metric

Anomalies are classified by severity: "critical", "warning", "info".
"""

import json
import logging
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = {
    "gpu_temp_warn": 75,
    "gpu_temp_critical": 83,
    "gpu_power_warn_pct": 90,
    "gpu_memory_warn_pct": 95,
    "ecc_error_threshold": 1,
    "zscore_threshold": 3.0,
    "system_memory_warn_pct": 90,
    "system_cpu_warn_pct": 95,
    "nvlink_crc_error_threshold": 10,
    "nvlink_replay_error_threshold": 100,
}


@dataclass
class Anomaly:
    timestamp: str
    source: str          # "gpu", "system", "nvlink", "log"
    severity: str        # "critical", "warning", "info"
    metric_name: str
    metric_value: float
    threshold: float
    description: str
    gpu_index: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def detect_gpu_threshold_anomalies(
    snapshots: list[dict],
    thresholds: Optional[dict] = None,
) -> list[Anomaly]:
    """Check GPU snapshots against fixed thresholds."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    anomalies = []

    for snap in snapshots:
        gpu = snap["gpu_index"]
        ts = snap["timestamp"]

        temp = snap.get("temperature_c", 0)
        if temp >= t["gpu_temp_critical"]:
            anomalies.append(Anomaly(
                timestamp=ts, source="gpu", severity="critical",
                metric_name="temperature_c", metric_value=temp,
                threshold=t["gpu_temp_critical"],
                description=f"GPU {gpu} temperature CRITICAL at {temp}C "
                            f"(threshold: {t['gpu_temp_critical']}C) — "
                            f"hardware throttling imminent",
                gpu_index=gpu,
            ))
        elif temp >= t["gpu_temp_warn"]:
            anomalies.append(Anomaly(
                timestamp=ts, source="gpu", severity="warning",
                metric_name="temperature_c", metric_value=temp,
                threshold=t["gpu_temp_warn"],
                description=f"GPU {gpu} temperature elevated at {temp}C "
                            f"(warning threshold: {t['gpu_temp_warn']}C)",
                gpu_index=gpu,
            ))

        power = snap.get("power_draw_w", 0)
        power_limit = snap.get("power_limit_w", 700)
        if power_limit > 0:
            power_pct = (power / power_limit) * 100
            if power_pct >= t["gpu_power_warn_pct"]:
                anomalies.append(Anomaly(
                    timestamp=ts, source="gpu", severity="warning",
                    metric_name="power_draw_pct", metric_value=round(power_pct, 1),
                    threshold=t["gpu_power_warn_pct"],
                    description=f"GPU {gpu} power draw at {power:.0f}W "
                                f"({power_pct:.0f}% of {power_limit:.0f}W limit)",
                    gpu_index=gpu,
                ))

        mem_total = snap.get("memory_total_mib", 1)
        mem_used = snap.get("memory_used_mib", 0)
        if mem_total > 0:
            mem_pct = (mem_used / mem_total) * 100
            if mem_pct >= t["gpu_memory_warn_pct"]:
                anomalies.append(Anomaly(
                    timestamp=ts, source="gpu", severity="warning",
                    metric_name="memory_used_pct", metric_value=round(mem_pct, 1),
                    threshold=t["gpu_memory_warn_pct"],
                    description=f"GPU {gpu} memory at {mem_pct:.1f}% "
                                f"({mem_used} / {mem_total} MiB) — OOM risk",
                    gpu_index=gpu,
                ))

        dbe = snap.get("ecc_dbe_volatile", 0)
        if dbe >= t["ecc_error_threshold"]:
            anomalies.append(Anomaly(
                timestamp=ts, source="gpu", severity="critical",
                metric_name="ecc_dbe_volatile", metric_value=dbe,
                threshold=t["ecc_error_threshold"],
                description=f"GPU {gpu} has {dbe} uncorrectable (double-bit) ECC "
                            f"error(s) — data corruption possible, consider draining workloads",
                gpu_index=gpu,
            ))

        sbe = snap.get("ecc_sbe_volatile", 0)
        if sbe >= 10:
            anomalies.append(Anomaly(
                timestamp=ts, source="gpu", severity="warning",
                metric_name="ecc_sbe_volatile", metric_value=sbe,
                threshold=10,
                description=f"GPU {gpu} has {sbe} correctable (single-bit) ECC "
                            f"errors — possible hardware degradation trend",
                gpu_index=gpu,
            ))

        throttle = snap.get("throttle_reasons", [])
        if isinstance(throttle, str):
            throttle = json.loads(throttle)
        critical_throttles = {"hw_slowdown", "hw_thermal_slowdown", "hw_power_brake_slowdown"}
        active_critical = set(throttle) & critical_throttles
        if active_critical:
            anomalies.append(Anomaly(
                timestamp=ts, source="gpu", severity="warning",
                metric_name="throttle_reasons", metric_value=len(active_critical),
                threshold=0,
                description=f"GPU {gpu} is being hardware-throttled: "
                            f"{', '.join(active_critical)}",
                gpu_index=gpu,
            ))

        nvlink_stats = snap.get("nvlink_stats", [])
        if isinstance(nvlink_stats, str):
            nvlink_stats = json.loads(nvlink_stats)
        for link in nvlink_stats:
            crc = link.get("crc_errors", 0)
            if crc >= t["nvlink_crc_error_threshold"]:
                anomalies.append(Anomaly(
                    timestamp=ts, source="nvlink", severity="warning",
                    metric_name="nvlink_crc_errors",
                    metric_value=crc,
                    threshold=t["nvlink_crc_error_threshold"],
                    description=f"GPU {gpu} NVLink {link['link_id']} has {crc} CRC "
                                f"errors — fabric integrity concern",
                    gpu_index=gpu,
                ))
            replay = link.get("replay_errors", 0)
            if replay >= t["nvlink_replay_error_threshold"]:
                anomalies.append(Anomaly(
                    timestamp=ts, source="nvlink", severity="warning",
                    metric_name="nvlink_replay_errors",
                    metric_value=replay,
                    threshold=t["nvlink_replay_error_threshold"],
                    description=f"GPU {gpu} NVLink {link['link_id']} has {replay} "
                                f"replay errors — link degradation",
                    gpu_index=gpu,
                ))

    return anomalies


def detect_system_threshold_anomalies(
    snapshots: list[dict],
    thresholds: Optional[dict] = None,
) -> list[Anomaly]:
    """Check system snapshots against fixed thresholds."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    anomalies = []

    for snap in snapshots:
        ts = snap["timestamp"]

        mem_pct = snap.get("memory_percent", 0)
        if mem_pct >= t["system_memory_warn_pct"]:
            anomalies.append(Anomaly(
                timestamp=ts, source="system", severity="warning",
                metric_name="system_memory_pct", metric_value=mem_pct,
                threshold=t["system_memory_warn_pct"],
                description=f"System memory at {mem_pct:.1f}% — host OOM kill risk",
            ))

        cpu_pct = snap.get("cpu_percent", 0)
        if cpu_pct >= t["system_cpu_warn_pct"]:
            anomalies.append(Anomaly(
                timestamp=ts, source="system", severity="warning",
                metric_name="system_cpu_pct", metric_value=cpu_pct,
                threshold=t["system_cpu_warn_pct"],
                description=f"System CPU at {cpu_pct:.1f}% — may bottleneck GPU data feeding",
            ))

        swap_pct = snap.get("swap_percent", 0)
        if swap_pct >= 50:
            anomalies.append(Anomaly(
                timestamp=ts, source="system", severity="warning",
                metric_name="swap_percent", metric_value=swap_pct,
                threshold=50,
                description=f"Swap usage at {swap_pct:.1f}% — system under heavy "
                            f"memory pressure, performance degradation likely",
            ))

    return anomalies


def detect_zscore_anomalies(
    snapshots: list[dict],
    metric_name: str,
    z_threshold: float = 3.0,
    source: str = "gpu",
    gpu_index_key: str = "gpu_index",
) -> list[Anomaly]:
    """Statistical anomaly detection via z-score on a single metric.

    Groups by gpu_index (if present), computes mean/stddev over the window,
    and flags points that deviate by more than z_threshold standard deviations.
    """
    from collections import defaultdict

    groups: dict[Optional[int], list] = defaultdict(list)
    for snap in snapshots:
        key = snap.get(gpu_index_key)
        val = snap.get(metric_name)
        if val is not None:
            groups[key].append((snap["timestamp"], float(val)))

    anomalies = []
    for group_key, series in groups.items():
        if len(series) < 5:
            continue

        values = [v for _, v in series]
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        stddev = math.sqrt(variance) if variance > 0 else 0

        if stddev == 0:
            continue

        for ts, val in series:
            z = abs(val - mean) / stddev
            if z >= z_threshold:
                direction = "above" if val > mean else "below"
                anomalies.append(Anomaly(
                    timestamp=ts,
                    source=source,
                    severity="warning",
                    metric_name=f"{metric_name}_zscore",
                    metric_value=round(val, 2),
                    threshold=round(mean + z_threshold * stddev, 2),
                    description=f"{'GPU ' + str(group_key) + ' ' if group_key is not None else ''}"
                                f"{metric_name} = {val:.2f} is {z:.1f} stddevs "
                                f"{direction} mean ({mean:.2f}) — statistical anomaly",
                    gpu_index=group_key,
                ))

    return anomalies


def run_anomaly_detection(
    gpu_snapshots: list[dict],
    system_snapshots: list[dict],
    config: Optional[dict] = None,
) -> list[Anomaly]:
    """Run all anomaly detection strategies and return a unified, deduplicated list."""
    anomaly_config = (config or {}).get("analysis", {}).get("anomaly", {})
    z_threshold = anomaly_config.get("zscore_threshold", 3.0)

    all_anomalies = []

    all_anomalies.extend(detect_gpu_threshold_anomalies(gpu_snapshots, anomaly_config))
    all_anomalies.extend(detect_system_threshold_anomalies(system_snapshots, anomaly_config))

    for metric in ["temperature_c", "power_draw_w", "gpu_util_pct", "memory_util_pct"]:
        all_anomalies.extend(detect_zscore_anomalies(
            gpu_snapshots, metric, z_threshold=z_threshold, source="gpu",
        ))

    for metric in ["cpu_percent", "memory_percent"]:
        all_anomalies.extend(detect_zscore_anomalies(
            system_snapshots, metric, z_threshold=z_threshold,
            source="system", gpu_index_key="_none_",
        ))

    seen = set()
    deduped = []
    for a in all_anomalies:
        key = (a.timestamp, a.source, a.metric_name, a.gpu_index)
        if key not in seen:
            seen.add(key)
            deduped.append(a)

    deduped.sort(key=lambda a: (
        {"critical": 0, "warning": 1, "info": 2}.get(a.severity, 3),
        a.timestamp,
    ))

    logger.info("Anomaly detection found %d anomalies (%d after dedup)",
                len(all_anomalies), len(deduped))
    return deduped
