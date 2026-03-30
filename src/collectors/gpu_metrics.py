"""
GPU metrics collector using pynvml (primary) with nvidia-smi fallback.

Collects: utilization, memory, temperature, power, ECC errors, NVLink stats,
clock speeds, throttle reasons, and running processes for all GPUs on the node.
"""

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

THROTTLE_REASONS = {
    0x0000000000000001: "gpu_idle",
    0x0000000000000002: "applications_clocks_setting",
    0x0000000000000004: "sw_power_cap",
    0x0000000000000008: "hw_slowdown",
    0x0000000000000010: "sync_boost",
    0x0000000000000020: "sw_thermal_slowdown",
    0x0000000000000040: "hw_thermal_slowdown",
    0x0000000000000080: "hw_power_brake_slowdown",
    0x0000000000000100: "display_clocks_setting",
}


@dataclass
class GpuProcessInfo:
    pid: int
    name: str
    used_gpu_memory_mib: int


@dataclass
class NvLinkStats:
    link_id: int
    tx_bytes: int
    rx_bytes: int
    crc_errors: int
    replay_errors: int
    recovery_errors: int


@dataclass
class GpuSnapshot:
    timestamp: str
    gpu_index: int
    uuid: str
    name: str
    # Utilization
    gpu_util_pct: float
    memory_util_pct: float
    # Memory
    memory_used_mib: int
    memory_total_mib: int
    memory_free_mib: int
    # Thermal & Power
    temperature_c: int
    power_draw_w: float
    power_limit_w: float
    # Clocks
    clock_sm_mhz: int
    clock_mem_mhz: int
    # ECC
    ecc_sbe_volatile: int
    ecc_dbe_volatile: int
    ecc_sbe_aggregate: int
    ecc_dbe_aggregate: int
    # Throttle
    throttle_reasons: list[str] = field(default_factory=list)
    # Processes
    processes: list[dict] = field(default_factory=list)
    # NVLink
    nvlink_stats: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _try_pynvml() -> bool:
    """Check if pynvml is available and NVML can be initialized."""
    try:
        import pynvml
        pynvml.nvmlInit()
        pynvml.nvmlShutdown()
        return True
    except Exception:
        return False


def collect_gpu_metrics_pynvml() -> list[GpuSnapshot]:
    """Collect GPU metrics using pynvml (NVML Python bindings)."""
    import pynvml

    pynvml.nvmlInit()
    try:
        device_count = pynvml.nvmlDeviceGetCount()
        ts = datetime.now(timezone.utc).isoformat()
        snapshots = []

        for i in range(device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)

            uuid = pynvml.nvmlDeviceGetUUID(handle)
            name = pynvml.nvmlDeviceGetName(handle)

            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            temp = pynvml.nvmlDeviceGetTemperature(
                handle, pynvml.NVML_TEMPERATURE_GPU
            )

            try:
                power_draw = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
            except pynvml.NVMLError:
                power_draw = 0.0

            try:
                power_limit = pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0
            except pynvml.NVMLError:
                power_limit = 0.0

            try:
                clock_sm = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
            except pynvml.NVMLError:
                clock_sm = 0

            try:
                clock_mem = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
            except pynvml.NVMLError:
                clock_mem = 0

            ecc_sbe_vol = ecc_dbe_vol = ecc_sbe_agg = ecc_dbe_agg = 0
            try:
                ecc_sbe_vol = pynvml.nvmlDeviceGetTotalEccErrors(
                    handle,
                    pynvml.NVML_SINGLE_BIT_ECC,
                    pynvml.NVML_VOLATILE_ECC,
                )
            except pynvml.NVMLError:
                pass
            try:
                ecc_dbe_vol = pynvml.nvmlDeviceGetTotalEccErrors(
                    handle,
                    pynvml.NVML_DOUBLE_BIT_ECC,
                    pynvml.NVML_VOLATILE_ECC,
                )
            except pynvml.NVMLError:
                pass
            try:
                ecc_sbe_agg = pynvml.nvmlDeviceGetTotalEccErrors(
                    handle,
                    pynvml.NVML_SINGLE_BIT_ECC,
                    pynvml.NVML_AGGREGATE_ECC,
                )
            except pynvml.NVMLError:
                pass
            try:
                ecc_dbe_agg = pynvml.nvmlDeviceGetTotalEccErrors(
                    handle,
                    pynvml.NVML_DOUBLE_BIT_ECC,
                    pynvml.NVML_AGGREGATE_ECC,
                )
            except pynvml.NVMLError:
                pass

            throttle_list = []
            try:
                throttle_bitmask = pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(
                    handle
                )
                for bit, reason in THROTTLE_REASONS.items():
                    if throttle_bitmask & bit:
                        throttle_list.append(reason)
            except pynvml.NVMLError:
                pass

            proc_list = []
            try:
                procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
                for p in procs:
                    pname = "unknown"
                    try:
                        pname = pynvml.nvmlSystemGetProcessName(p.pid)
                    except pynvml.NVMLError:
                        pass
                    proc_list.append({
                        "pid": p.pid,
                        "name": pname,
                        "used_gpu_memory_mib": (p.usedGpuMemory or 0) // (1024 * 1024),
                    })
            except pynvml.NVMLError:
                pass

            nvlink_list = []
            try:
                for link in range(18):
                    try:
                        tx = pynvml.nvmlDeviceGetNvLinkUtilizationCounter(
                            handle, link, 0
                        )
                        rx = pynvml.nvmlDeviceGetNvLinkUtilizationCounter(
                            handle, link, 1
                        )
                        replay = pynvml.nvmlDeviceGetNvLinkErrorCounter(
                            handle,
                            link,
                            pynvml.NVML_NVLINK_ERROR_DL_REPLAY,
                        )
                        recovery = pynvml.nvmlDeviceGetNvLinkErrorCounter(
                            handle,
                            link,
                            pynvml.NVML_NVLINK_ERROR_DL_RECOVERY,
                        )
                        crc = pynvml.nvmlDeviceGetNvLinkErrorCounter(
                            handle,
                            link,
                            pynvml.NVML_NVLINK_ERROR_DL_CRC_FLIT,
                        )
                        nvlink_list.append({
                            "link_id": link,
                            "tx_bytes": tx[0] if isinstance(tx, tuple) else tx,
                            "rx_bytes": rx[0] if isinstance(rx, tuple) else rx,
                            "crc_errors": crc,
                            "replay_errors": replay,
                            "recovery_errors": recovery,
                        })
                    except pynvml.NVMLError:
                        break
            except pynvml.NVMLError:
                pass

            snapshots.append(GpuSnapshot(
                timestamp=ts,
                gpu_index=i,
                uuid=uuid,
                name=name,
                gpu_util_pct=util.gpu,
                memory_util_pct=util.memory,
                memory_used_mib=mem_info.used // (1024 * 1024),
                memory_total_mib=mem_info.total // (1024 * 1024),
                memory_free_mib=mem_info.free // (1024 * 1024),
                temperature_c=temp,
                power_draw_w=power_draw,
                power_limit_w=power_limit,
                clock_sm_mhz=clock_sm,
                clock_mem_mhz=clock_mem,
                ecc_sbe_volatile=ecc_sbe_vol,
                ecc_dbe_volatile=ecc_dbe_vol,
                ecc_sbe_aggregate=ecc_sbe_agg,
                ecc_dbe_aggregate=ecc_dbe_agg,
                throttle_reasons=throttle_list,
                processes=proc_list,
                nvlink_stats=nvlink_list,
            ))

        return snapshots
    finally:
        pynvml.nvmlShutdown()


def collect_gpu_metrics_smi() -> list[GpuSnapshot]:
    """Fallback: collect GPU metrics by parsing nvidia-smi CSV output."""
    query_fields = ",".join([
        "index", "uuid", "name",
        "utilization.gpu", "utilization.memory",
        "memory.used", "memory.total", "memory.free",
        "temperature.gpu",
        "power.draw", "power.limit",
        "clocks.current.sm", "clocks.current.memory",
        "ecc.errors.corrected.volatile.total",
        "ecc.errors.uncorrected.volatile.total",
        "ecc.errors.corrected.aggregate.total",
        "ecc.errors.uncorrected.aggregate.total",
    ])

    result = subprocess.run(
        ["nvidia-smi", f"--query-gpu={query_fields}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        logger.error("nvidia-smi failed: %s", result.stderr)
        return []

    ts = datetime.now(timezone.utc).isoformat()
    snapshots = []

    for line in result.stdout.strip().split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 17:
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

        idx = safe_int(parts[0])

        proc_list = _get_processes_smi(idx)

        snapshots.append(GpuSnapshot(
            timestamp=ts,
            gpu_index=idx,
            uuid=parts[1],
            name=parts[2],
            gpu_util_pct=safe_float(parts[3]),
            memory_util_pct=safe_float(parts[4]),
            memory_used_mib=safe_int(parts[5]),
            memory_total_mib=safe_int(parts[6]),
            memory_free_mib=safe_int(parts[7]),
            temperature_c=safe_int(parts[8]),
            power_draw_w=safe_float(parts[9]),
            power_limit_w=safe_float(parts[10]),
            clock_sm_mhz=safe_int(parts[11]),
            clock_mem_mhz=safe_int(parts[12]),
            ecc_sbe_volatile=safe_int(parts[13]),
            ecc_dbe_volatile=safe_int(parts[14]),
            ecc_sbe_aggregate=safe_int(parts[15]),
            ecc_dbe_aggregate=safe_int(parts[16]),
            processes=proc_list,
        ))

    return snapshots


def _get_processes_smi(gpu_index: int) -> list[dict]:
    """Get running processes for a specific GPU via nvidia-smi."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                f"--id={gpu_index}",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        procs = []
        for line in result.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3 and parts[0]:
                procs.append({
                    "pid": int(parts[0]),
                    "name": parts[1],
                    "used_gpu_memory_mib": int(parts[2]) if parts[2] != "[N/A]" else 0,
                })
        return procs
    except Exception:
        return []


_use_pynvml: Optional[bool] = None


def collect_gpu_metrics() -> list[GpuSnapshot]:
    """Collect GPU metrics, auto-selecting pynvml or nvidia-smi fallback."""
    global _use_pynvml
    if _use_pynvml is None:
        _use_pynvml = _try_pynvml()
        logger.info("GPU collector backend: %s", "pynvml" if _use_pynvml else "nvidia-smi")

    if _use_pynvml:
        try:
            return collect_gpu_metrics_pynvml()
        except Exception as e:
            logger.warning("pynvml collection failed, falling back to nvidia-smi: %s", e)
            return collect_gpu_metrics_smi()
    else:
        return collect_gpu_metrics_smi()
