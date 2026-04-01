"""
Redfish BMC collector — queries hardware telemetry from the Baseboard
Management Controller via the Redfish REST API.

Collects: chassis thermal sensors (fans, temperatures), power supply status,
and the system event log (SEL) for hardware-level incidents.
"""

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

from src.collectors.log_parser import LogEvent

logger = logging.getLogger(__name__)

requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)


@dataclass
class RedfishSnapshot:
    timestamp: str
    chassis_temps: list[dict] = field(default_factory=list)
    fan_readings: list[dict] = field(default_factory=list)
    power_supplies: list[dict] = field(default_factory=list)
    sel_entries: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def probe(url: str, username: str = "", password: str = "", timeout: int = 5) -> bool:
    """Check if Redfish endpoint is reachable."""
    if not url:
        return False
    try:
        auth = HTTPBasicAuth(username, password) if username else None
        resp = requests.get(
            f"{url.rstrip('/')}/redfish/v1/",
            auth=auth, verify=False, timeout=timeout,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _get(url: str, path: str, auth: Optional[HTTPBasicAuth], timeout: int = 10) -> Optional[dict]:
    """GET a Redfish resource path."""
    try:
        resp = requests.get(
            f"{url.rstrip('/')}{path}",
            auth=auth, verify=False, timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        logger.debug("Redfish GET %s failed: %s", path, e)
        return None


def collect_redfish_data(config: dict) -> tuple[Optional[RedfishSnapshot], list[LogEvent]]:
    """Collect thermal, power, and SEL data from the Redfish BMC."""
    rf_config = config.get("redfish", {})
    if not rf_config.get("enabled", False):
        return None, []

    url = rf_config.get("url", "")
    username = rf_config.get("username", "")
    password = rf_config.get("password", "")

    if not url:
        logger.debug("Redfish URL not configured, skipping")
        return None, []

    if not probe(url, username, password):
        logger.info("Redfish at %s not reachable, skipping", url)
        return None, []

    auth = HTTPBasicAuth(username, password) if username else None
    ts = datetime.now(timezone.utc).isoformat()

    snapshot = RedfishSnapshot(timestamp=ts)
    events = []

    # Thermal data (temperatures + fans)
    thermal = _get(url, "/redfish/v1/Chassis/1/Thermal", auth)
    if thermal:
        for t in thermal.get("Temperatures", []):
            snapshot.chassis_temps.append({
                "name": t.get("Name", ""),
                "reading_c": t.get("ReadingCelsius"),
                "upper_critical": t.get("UpperThresholdCritical"),
                "upper_warning": t.get("UpperThresholdNonCritical"),
                "status": t.get("Status", {}).get("Health", ""),
            })
        for f in thermal.get("Fans", []):
            snapshot.fan_readings.append({
                "name": f.get("Name", ""),
                "reading_rpm": f.get("Reading"),
                "status": f.get("Status", {}).get("Health", ""),
            })

    # Power data
    power = _get(url, "/redfish/v1/Chassis/1/Power", auth)
    if power:
        for ps in power.get("PowerSupplies", []):
            snapshot.power_supplies.append({
                "name": ps.get("Name", ""),
                "power_output_w": ps.get("PowerOutputWatts"),
                "line_input_voltage": ps.get("LineInputVoltage"),
                "status": ps.get("Status", {}).get("Health", ""),
            })

    # System Event Log
    sel = _get(url, "/redfish/v1/Managers/1/LogServices/SEL/Entries", auth)
    if sel:
        for entry in sel.get("Members", [])[:50]:
            severity = entry.get("Severity", "OK")
            if severity in ("OK", "Informational"):
                continue
            snapshot.sel_entries.append({
                "id": entry.get("Id", ""),
                "created": entry.get("Created", ""),
                "severity": severity,
                "message": entry.get("Message", ""),
                "sensor_type": entry.get("SensorType", ""),
            })
            level = "ERROR" if severity == "Critical" else "WARN"
            events.append(LogEvent(
                timestamp=entry.get("Created", ts),
                source="redfish",
                level=level,
                category=f"sel_{entry.get('SensorType', 'hardware')}",
                message=entry.get("Message", "Unknown hardware event"),
                raw_line=str(entry),
                metadata={"severity": severity, "sensor_type": entry.get("SensorType", "")},
            ))

    logger.info("Redfish: %d temps, %d fans, %d PSUs, %d SEL events",
                len(snapshot.chassis_temps), len(snapshot.fan_readings),
                len(snapshot.power_supplies), len(events))
    return snapshot, events
