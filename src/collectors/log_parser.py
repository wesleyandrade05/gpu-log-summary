"""
Log parsers for NVIDIA Fabric Manager, DCGM, and InfiniBand ACM logs.

Each parser reads a log file, extracts structured events with timestamps,
severity, and categorized payloads. Supports incremental parsing via
a file-offset bookmark so the same lines aren't re-processed.
"""

import logging
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

FM_LINE_RE = re.compile(
    r"^\[(?P<timestamp>[A-Za-z]{3}\s+\d{1,2}\s+\d{4}\s+\d{2}:\d{2}:\d{2})\]\s+"
    r"\[(?P<level>\w+)\]\s+"
    r"\[tid\s+(?P<tid>\d+)\]\s+"
    r"(?P<message>.+)$"
)

FM_TIMESTAMP_FMT = "%b %d %Y %H:%M:%S"

IB_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+):\s+"
    r"(?P<message>.+)$"
)

FM_EVENT_PATTERNS = {
    "gpu_probe": re.compile(r"NVLink inband GPU probe request received.*GPU Id (\d+)"),
    "gpu_added": re.compile(r"added GPU with UUID (GPU-[\w-]+)"),
    "multicast_alloc": re.compile(r"multicast group (\d+) is allocated"),
    "team_setup_ok": re.compile(r"successfully setup multicast team.*request id (\d+)"),
    "team_setup_fail": re.compile(r"failed.*setup.*team", re.IGNORECASE),
    "fabric_error": re.compile(r"error|fail|fatal|critical", re.IGNORECASE),
    "nvswitch_health": re.compile(r"Fabric Health Mask:(\w+)"),
    "link_state_change": re.compile(r"Link Mask.*Enabled Link Mask:(\w+)"),
}


@dataclass
class LogEvent:
    timestamp: str
    source: str          # "fabricmanager", "dcgm", "infiniband"
    level: str           # INFO, WARN, ERROR, etc.
    category: str        # e.g. "gpu_probe", "multicast_alloc", "fabric_error"
    message: str
    raw_line: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParseBookmark:
    """Tracks file offset for incremental parsing."""
    path: str
    offset: int = 0
    inode: int = 0


def _read_new_lines(path: str, bookmark: Optional[ParseBookmark] = None) -> tuple[list[str], ParseBookmark]:
    """Read only new lines since the last bookmark. Handles log rotation via inode check."""
    if not os.path.isfile(path):
        logger.warning("Log file not found: %s", path)
        return [], bookmark or ParseBookmark(path=path)

    stat = os.stat(path)
    current_inode = stat.st_ino

    if bookmark is None:
        bookmark = ParseBookmark(path=path, offset=0, inode=current_inode)

    if bookmark.inode != current_inode:
        logger.info("Log file rotated (inode changed): %s", path)
        bookmark.offset = 0
        bookmark.inode = current_inode

    if stat.st_size < bookmark.offset:
        logger.info("Log file truncated, re-reading from start: %s", path)
        bookmark.offset = 0

    with open(path, "r", errors="replace") as f:
        f.seek(bookmark.offset)
        lines = f.readlines()
        bookmark.offset = f.tell()

    return lines, bookmark


def _classify_fm_event(message: str) -> tuple[str, dict]:
    """Classify a Fabric Manager log message into a category with metadata."""
    for category, pattern in FM_EVENT_PATTERNS.items():
        m = pattern.search(message)
        if m:
            return category, {"match": m.group(0), "groups": list(m.groups())}
    return "info", {}


def parse_fabricmanager_log(
    path: str = "/var/log/fabricmanager.log",
    bookmark: Optional[ParseBookmark] = None,
) -> tuple[list[LogEvent], ParseBookmark]:
    """Parse NVIDIA Fabric Manager log file for structured events."""
    lines, bookmark = _read_new_lines(path, bookmark)
    events = []

    for raw_line in lines:
        raw_line = raw_line.rstrip("\n")
        m = FM_LINE_RE.match(raw_line)
        if not m:
            continue

        try:
            ts = datetime.strptime(m.group("timestamp"), FM_TIMESTAMP_FMT)
            ts_str = ts.isoformat()
        except ValueError:
            ts_str = m.group("timestamp")

        level = m.group("level").upper()
        message = m.group("message")
        category, metadata = _classify_fm_event(message)
        metadata["tid"] = m.group("tid")

        if category == "info" and level == "INFO":
            continue

        events.append(LogEvent(
            timestamp=ts_str,
            source="fabricmanager",
            level=level,
            category=category,
            message=message,
            raw_line=raw_line,
            metadata=metadata,
        ))

    logger.info("Parsed %d events from %s (%d lines read)", len(events), path, len(lines))
    return events, bookmark


def parse_infiniband_log(
    path: str = "/var/log/ibacm.log",
    bookmark: Optional[ParseBookmark] = None,
) -> tuple[list[LogEvent], ParseBookmark]:
    """Parse InfiniBand ACM log file."""
    lines, bookmark = _read_new_lines(path, bookmark)
    events = []

    for raw_line in lines:
        raw_line = raw_line.rstrip("\n")
        m = IB_LINE_RE.match(raw_line)
        if not m:
            continue

        ts_str = m.group("timestamp")
        message = m.group("message")

        level = "INFO"
        category = "ib_info"
        if "ERROR" in message.upper():
            level = "ERROR"
            category = "ib_error"
        elif "WARN" in message.upper():
            level = "WARN"
            category = "ib_warning"

        if level == "INFO":
            continue

        events.append(LogEvent(
            timestamp=ts_str,
            source="infiniband",
            level=level,
            category=category,
            message=message,
            raw_line=raw_line,
        ))

    logger.info("Parsed %d events from %s (%d lines read)", len(events), path, len(lines))
    return events, bookmark


def parse_dcgm_logs(
    directory: str = "/var/log/nvidia-dcgm/",
    bookmark: Optional[ParseBookmark] = None,
) -> tuple[list[LogEvent], Optional[ParseBookmark]]:
    """Parse DCGM log directory. Currently a placeholder since the dir is empty."""
    events = []
    if not os.path.isdir(directory):
        return events, bookmark

    for fname in sorted(os.listdir(directory)):
        fpath = os.path.join(directory, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    level = "INFO"
                    category = "dcgm_info"
                    if "error" in line.lower() or "fail" in line.lower():
                        level = "ERROR"
                        category = "dcgm_error"
                    elif "warn" in line.lower():
                        level = "WARN"
                        category = "dcgm_warning"

                    if level == "INFO":
                        continue

                    events.append(LogEvent(
                        timestamp=datetime.now().isoformat(),
                        source="dcgm",
                        level=level,
                        category=category,
                        message=line,
                        raw_line=line,
                    ))
        except PermissionError:
            logger.warning("Permission denied reading %s", fpath)

    return events, bookmark


def collect_all_log_events(
    fm_bookmark: Optional[ParseBookmark] = None,
    ib_bookmark: Optional[ParseBookmark] = None,
) -> tuple[list[LogEvent], ParseBookmark, ParseBookmark]:
    """Collect log events from all sources. Returns events and updated bookmarks."""
    fm_events, fm_bookmark = parse_fabricmanager_log(bookmark=fm_bookmark)
    ib_events, ib_bookmark = parse_infiniband_log(bookmark=ib_bookmark)
    dcgm_events, _ = parse_dcgm_logs()

    all_events = fm_events + ib_events + dcgm_events
    all_events.sort(key=lambda e: e.timestamp)
    return all_events, fm_bookmark, ib_bookmark
