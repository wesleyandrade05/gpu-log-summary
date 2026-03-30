"""
SQLite storage layer for GPU metrics, system metrics, and log events.

Provides schema creation, insert helpers, and query functions for the
data pipeline. Uses JSON columns for nested data (processes, nvlink, metadata).
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gpu_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    gpu_index INTEGER NOT NULL,
    uuid TEXT NOT NULL,
    name TEXT NOT NULL,
    gpu_util_pct REAL,
    memory_util_pct REAL,
    memory_used_mib INTEGER,
    memory_total_mib INTEGER,
    memory_free_mib INTEGER,
    temperature_c INTEGER,
    power_draw_w REAL,
    power_limit_w REAL,
    clock_sm_mhz INTEGER,
    clock_mem_mhz INTEGER,
    ecc_sbe_volatile INTEGER DEFAULT 0,
    ecc_dbe_volatile INTEGER DEFAULT 0,
    ecc_sbe_aggregate INTEGER DEFAULT 0,
    ecc_dbe_aggregate INTEGER DEFAULT 0,
    throttle_reasons TEXT DEFAULT '[]',
    processes TEXT DEFAULT '[]',
    nvlink_stats TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_gpu_snap_ts ON gpu_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_gpu_snap_gpu ON gpu_snapshots(gpu_index);

CREATE TABLE IF NOT EXISTS system_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cpu_percent REAL,
    cpu_count INTEGER,
    memory_total_mib INTEGER,
    memory_used_mib INTEGER,
    memory_percent REAL,
    swap_total_mib INTEGER,
    swap_used_mib INTEGER,
    swap_percent REAL,
    disk_read_bytes INTEGER,
    disk_write_bytes INTEGER,
    net_sent_bytes INTEGER,
    net_recv_bytes INTEGER,
    load_avg_1m REAL,
    load_avg_5m REAL,
    load_avg_15m REAL
);

CREATE INDEX IF NOT EXISTS idx_sys_snap_ts ON system_snapshots(timestamp);

CREATE TABLE IF NOT EXISTS log_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    raw_line TEXT,
    metadata TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_log_ts ON log_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_log_level ON log_events(level);
CREATE INDEX IF NOT EXISTS idx_log_source ON log_events(source);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    severity TEXT NOT NULL,
    metric_name TEXT,
    metric_value REAL,
    threshold REAL,
    description TEXT NOT NULL,
    gpu_index INTEGER
);

CREATE INDEX IF NOT EXISTS idx_anomaly_ts ON anomalies(timestamp);
CREATE INDEX IF NOT EXISTS idx_anomaly_sev ON anomalies(severity);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    summary_type TEXT NOT NULL,
    content TEXT NOT NULL,
    model_used TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER
);

CREATE INDEX IF NOT EXISTS idx_summary_ts ON summaries(timestamp);

CREATE TABLE IF NOT EXISTS bookmarks (
    source TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    file_offset INTEGER DEFAULT 0,
    file_inode INTEGER DEFAULT 0
);
"""


class MetricsDB:
    def __init__(self, db_path: str = "data/metrics.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- GPU snapshots --

    def insert_gpu_snapshots(self, snapshots: list) -> int:
        """Insert a batch of GpuSnapshot objects. Returns count inserted."""
        rows = []
        for s in snapshots:
            d = s.to_dict() if hasattr(s, "to_dict") else s
            rows.append((
                d["timestamp"], d["gpu_index"], d["uuid"], d["name"],
                d["gpu_util_pct"], d["memory_util_pct"],
                d["memory_used_mib"], d["memory_total_mib"], d["memory_free_mib"],
                d["temperature_c"], d["power_draw_w"], d["power_limit_w"],
                d["clock_sm_mhz"], d["clock_mem_mhz"],
                d["ecc_sbe_volatile"], d["ecc_dbe_volatile"],
                d["ecc_sbe_aggregate"], d["ecc_dbe_aggregate"],
                json.dumps(d.get("throttle_reasons", [])),
                json.dumps(d.get("processes", [])),
                json.dumps(d.get("nvlink_stats", [])),
            ))
        self.conn.executemany(
            """INSERT INTO gpu_snapshots (
                timestamp, gpu_index, uuid, name,
                gpu_util_pct, memory_util_pct,
                memory_used_mib, memory_total_mib, memory_free_mib,
                temperature_c, power_draw_w, power_limit_w,
                clock_sm_mhz, clock_mem_mhz,
                ecc_sbe_volatile, ecc_dbe_volatile,
                ecc_sbe_aggregate, ecc_dbe_aggregate,
                throttle_reasons, processes, nvlink_stats
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def get_gpu_snapshots(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        gpu_index: Optional[int] = None,
        limit: int = 1000,
    ) -> list[dict]:
        query = "SELECT * FROM gpu_snapshots WHERE 1=1"
        params = []
        if start:
            query += " AND timestamp >= ?"
            params.append(start)
        if end:
            query += " AND timestamp <= ?"
            params.append(end)
        if gpu_index is not None:
            query += " AND gpu_index = ?"
            params.append(gpu_index)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["throttle_reasons"] = json.loads(d["throttle_reasons"])
            d["processes"] = json.loads(d["processes"])
            d["nvlink_stats"] = json.loads(d["nvlink_stats"])
            results.append(d)
        return results

    # -- System snapshots --

    def insert_system_snapshot(self, snapshot) -> None:
        d = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        self.conn.execute(
            """INSERT INTO system_snapshots (
                timestamp, cpu_percent, cpu_count,
                memory_total_mib, memory_used_mib, memory_percent,
                swap_total_mib, swap_used_mib, swap_percent,
                disk_read_bytes, disk_write_bytes,
                net_sent_bytes, net_recv_bytes,
                load_avg_1m, load_avg_5m, load_avg_15m
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d["timestamp"], d["cpu_percent"], d["cpu_count"],
                d["memory_total_mib"], d["memory_used_mib"], d["memory_percent"],
                d["swap_total_mib"], d["swap_used_mib"], d["swap_percent"],
                d["disk_read_bytes"], d["disk_write_bytes"],
                d["net_sent_bytes"], d["net_recv_bytes"],
                d["load_avg_1m"], d["load_avg_5m"], d["load_avg_15m"],
            ),
        )
        self.conn.commit()

    def get_system_snapshots(
        self, start: Optional[str] = None, end: Optional[str] = None, limit: int = 1000
    ) -> list[dict]:
        query = "SELECT * FROM system_snapshots WHERE 1=1"
        params = []
        if start:
            query += " AND timestamp >= ?"
            params.append(start)
        if end:
            query += " AND timestamp <= ?"
            params.append(end)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    # -- Log events --

    def insert_log_events(self, events: list) -> int:
        rows = []
        for e in events:
            d = e.to_dict() if hasattr(e, "to_dict") else e
            rows.append((
                d["timestamp"], d["source"], d["level"], d["category"],
                d["message"], d.get("raw_line", ""),
                json.dumps(d.get("metadata", {})),
            ))
        self.conn.executemany(
            """INSERT INTO log_events (
                timestamp, source, level, category, message, raw_line, metadata
            ) VALUES (?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def get_log_events(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        level: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        query = "SELECT * FROM log_events WHERE 1=1"
        params = []
        if start:
            query += " AND timestamp >= ?"
            params.append(start)
        if end:
            query += " AND timestamp <= ?"
            params.append(end)
        if level:
            query += " AND level = ?"
            params.append(level)
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["metadata"] = json.loads(d["metadata"])
            results.append(d)
        return results

    # -- Anomalies --

    def insert_anomalies(self, anomalies: list) -> int:
        rows = []
        for a in anomalies:
            d = a if isinstance(a, dict) else a.to_dict()
            rows.append((
                d["timestamp"], d["source"], d["severity"],
                d.get("metric_name"), d.get("metric_value"),
                d.get("threshold"), d["description"],
                d.get("gpu_index"),
            ))
        self.conn.executemany(
            """INSERT INTO anomalies (
                timestamp, source, severity, metric_name, metric_value,
                threshold, description, gpu_index
            ) VALUES (?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def get_anomalies(
        self, start: Optional[str] = None, end: Optional[str] = None, limit: int = 200
    ) -> list[dict]:
        query = "SELECT * FROM anomalies WHERE 1=1"
        params = []
        if start:
            query += " AND timestamp >= ?"
            params.append(start)
        if end:
            query += " AND timestamp <= ?"
            params.append(end)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    # -- Summaries --

    def insert_summary(self, summary: dict) -> int:
        cursor = self.conn.execute(
            """INSERT INTO summaries (
                timestamp, period_start, period_end, summary_type,
                content, model_used, prompt_tokens, completion_tokens
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                summary["timestamp"], summary["period_start"], summary["period_end"],
                summary["summary_type"], summary["content"],
                summary.get("model_used"), summary.get("prompt_tokens"),
                summary.get("completion_tokens"),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_latest_summary(self, summary_type: str = "daily") -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM summaries WHERE summary_type = ? ORDER BY timestamp DESC LIMIT 1",
            (summary_type,),
        ).fetchone()
        return dict(row) if row else None

    def get_summaries(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM summaries ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Bookmarks (for incremental log parsing) --

    def save_bookmark(self, source: str, path: str, offset: int, inode: int):
        self.conn.execute(
            """INSERT OR REPLACE INTO bookmarks (source, file_path, file_offset, file_inode)
               VALUES (?, ?, ?, ?)""",
            (source, path, offset, inode),
        )
        self.conn.commit()

    def get_bookmark(self, source: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM bookmarks WHERE source = ?", (source,)
        ).fetchone()
        return dict(row) if row else None

    # -- Aggregate queries for analysis --

    def get_gpu_stats_summary(self, start: str, end: str) -> list[dict]:
        """Get per-GPU aggregate stats for a time window."""
        rows = self.conn.execute(
            """SELECT
                gpu_index, name,
                COUNT(*) as sample_count,
                AVG(gpu_util_pct) as avg_gpu_util,
                MAX(gpu_util_pct) as max_gpu_util,
                AVG(memory_util_pct) as avg_mem_util,
                MAX(memory_used_mib) as max_mem_used,
                AVG(temperature_c) as avg_temp,
                MAX(temperature_c) as max_temp,
                AVG(power_draw_w) as avg_power,
                MAX(power_draw_w) as max_power,
                SUM(ecc_dbe_volatile) as total_dbe
            FROM gpu_snapshots
            WHERE timestamp >= ? AND timestamp <= ?
            GROUP BY gpu_index
            ORDER BY gpu_index""",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_row_counts(self) -> dict:
        """Quick summary of how much data is stored."""
        counts = {}
        for table in ["gpu_snapshots", "system_snapshots", "log_events", "anomalies", "summaries"]:
            row = self.conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            counts[table] = row["cnt"]
        return counts
