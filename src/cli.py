"""
CLI entry point for the GPU Log Summarizer.

Usage:
    python -m src.cli collect          # Run one collection cycle
    python -m src.cli show             # Show latest metrics
    python -m src.cli show --events    # Show recent log events
    python -m src.cli analyze          # Run anomaly detection + correlation
    python -m src.cli status           # Show database stats
    python -m src.cli summarize        # Generate LLM summary
    python -m src.cli probe            # Check which data sources are reachable
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gpu-log-summary")


def _load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    logger.warning("config.yaml not found, using defaults")
    return {}


def _get_db(config: dict):
    from src.storage.database import MetricsDB
    db_path = config.get("collection", {}).get("db_path", "data/metrics.db")
    return MetricsDB(db_path)


@click.group()
def cli():
    """GPU Cluster Log Summarizer - AIOps pipeline for H200 GPU monitoring."""
    pass


@cli.command()
@click.option("--gpu/--no-gpu", default=True, help="Collect local GPU metrics")
@click.option("--system/--no-system", default=True, help="Collect system metrics")
@click.option("--logs/--no-logs", default=True, help="Parse log files")
@click.option("--remote/--no-remote", default=True, help="Collect from remote sources")
def collect(gpu, system, logs, remote):
    """Run one data collection cycle from all available sources."""
    config = _load_config()
    db = _get_db(config)

    try:
        if gpu:
            from src.collectors.gpu_metrics import collect_gpu_metrics
            snapshots = collect_gpu_metrics()
            count = db.insert_gpu_snapshots(snapshots)
            click.echo(f"[GPU] Collected {count} snapshots ({len(snapshots)} GPUs)")

        if system:
            from src.collectors.system_metrics import collect_system_metrics
            sys_snap = collect_system_metrics()
            db.insert_system_snapshot(sys_snap)
            click.echo(f"[System] CPU: {sys_snap.cpu_percent}%, MEM: {sys_snap.memory_percent}%")

        if logs:
            from src.collectors.log_parser import (
                collect_all_log_events, ParseBookmark,
            )

            fm_bm = None
            ib_bm = None
            saved_fm = db.get_bookmark("fabricmanager")
            if saved_fm:
                fm_bm = ParseBookmark(
                    path=saved_fm["file_path"],
                    offset=saved_fm["file_offset"],
                    inode=saved_fm["file_inode"],
                )
            saved_ib = db.get_bookmark("infiniband")
            if saved_ib:
                ib_bm = ParseBookmark(
                    path=saved_ib["file_path"],
                    offset=saved_ib["file_offset"],
                    inode=saved_ib["file_inode"],
                )

            events, fm_bm, ib_bm = collect_all_log_events(fm_bm, ib_bm)
            if events:
                db.insert_log_events(events)
            click.echo(f"[Logs] Parsed {len(events)} new log events")

            db.save_bookmark("fabricmanager", fm_bm.path, fm_bm.offset, fm_bm.inode)
            db.save_bookmark("infiniband", ib_bm.path, ib_bm.offset, ib_bm.inode)

        if remote:
            from src.collectors.prometheus import collect_prometheus_metrics
            prom_results = collect_prometheus_metrics(config)
            if prom_results:
                db.insert_prometheus_snapshots(prom_results)
                click.echo(f"[Prometheus] Collected {len(prom_results)} metric queries")

            # S3-based Prometheus snapshots
            from src.collectors.s3_prometheus import (
                collect_s3_prometheus,
                probe as s3_prom_probe,
            )
            s3_config = config.get("prometheus_s3", {})
            if s3_config.get("enabled", False) and s3_config.get("bucket", ""):
                s3_bm = db.get_bookmark("prometheus_s3")
                last_key = s3_bm["file_path"] if s3_bm else ""
                s3_results, new_last_key = collect_s3_prometheus(config, last_key=last_key)
                if s3_results:
                    db.insert_prometheus_snapshots(s3_results)
                    click.echo(f"[Prometheus S3] Collected {len(s3_results)} metric results")
                if new_last_key and new_last_key != last_key:
                    db.save_bookmark("prometheus_s3", new_last_key, 0, 0)

            from src.collectors.alertmanager import collect_alerts
            alert_events = collect_alerts(config)
            if alert_events:
                db.insert_log_events(alert_events)
                click.echo(f"[AlertManager] {len(alert_events)} firing alerts")

            from src.collectors.redfish import collect_redfish_data
            rf_snap, rf_events = collect_redfish_data(config)
            if rf_snap:
                db.insert_redfish_snapshot(rf_snap)
                click.echo(f"[Redfish] {len(rf_snap.chassis_temps)} temps, "
                            f"{len(rf_snap.fan_readings)} fans, {len(rf_events)} SEL events")
            if rf_events:
                db.insert_log_events(rf_events)

            from src.collectors.multinode import collect_multinode_metrics
            mn_results = collect_multinode_metrics(config)
            if mn_results:
                total_gpus = sum(len(v) for v in mn_results.values())
                db.insert_multinode_snapshots(mn_results)
                click.echo(f"[Multi-node] {len(mn_results)} nodes, {total_gpus} GPUs")

        click.echo("Collection complete.")
    finally:
        db.close()


@cli.command()
@click.option("--events", is_flag=True, help="Show log events instead of GPU metrics")
@click.option("--hours", default=1, help="Lookback window in hours")
@click.option("--gpu-index", type=int, default=None, help="Filter by GPU index")
@click.option("--limit", default=20, help="Max rows to show")
def show(events, hours, gpu_index, limit):
    """Show recently collected data."""
    config = _load_config()
    db = _get_db(config)

    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=hours)).isoformat()
    end = now.isoformat()

    try:
        if events:
            rows = db.get_log_events(start=start, end=end, limit=limit)
            if not rows:
                click.echo("No log events found in the last %d hour(s)." % hours)
                return
            click.echo(f"\n{'='*80}")
            click.echo(f" Log Events (last {hours}h) - {len(rows)} events")
            click.echo(f"{'='*80}")
            for r in rows:
                click.echo(f"  [{r['level']:5s}] [{r['source']:15s}] {r['timestamp']}")
                click.echo(f"         {r['category']}: {r['message'][:120]}")
                click.echo()
        else:
            rows = db.get_gpu_snapshots(start=start, end=end, gpu_index=gpu_index, limit=limit)
            if not rows:
                click.echo("No GPU snapshots found. Run 'collect' first.")
                return

            from tabulate import tabulate
            table_data = []
            for r in rows:
                table_data.append([
                    r["gpu_index"],
                    r["timestamp"][:19],
                    f"{r['gpu_util_pct']:.0f}%",
                    f"{r['memory_used_mib']}/{r['memory_total_mib']} MiB",
                    f"{r['temperature_c']}C",
                    f"{r['power_draw_w']:.0f}/{r['power_limit_w']:.0f}W",
                    r["ecc_dbe_volatile"],
                    ", ".join(r["throttle_reasons"]) or "-",
                ])
            headers = ["GPU", "Timestamp", "Util", "Memory", "Temp", "Power", "DBE", "Throttle"]
            click.echo(f"\n GPU Snapshots (last {hours}h)")
            click.echo(tabulate(table_data, headers=headers, tablefmt="simple"))
    finally:
        db.close()


@cli.command()
@click.option("--hours", default=24, help="Lookback window in hours")
@click.option("--correlate/--no-correlate", default=True, help="Run event correlation")
def analyze(hours, correlate):
    """Run anomaly detection and event correlation on collected data."""
    from src.analysis.anomaly import run_anomaly_detection
    from src.analysis.correlator import correlate_events, format_clusters_for_llm

    config = _load_config()
    db = _get_db(config)

    try:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=hours)).isoformat()
        end = now.isoformat()

        gpu_snaps = db.get_gpu_snapshots(start=start, end=end, limit=10000)
        sys_snaps = db.get_system_snapshots(start=start, end=end, limit=10000)

        if not gpu_snaps and not sys_snaps:
            click.echo("No data found. Run 'collect' first.")
            return

        anomalies = run_anomaly_detection(gpu_snaps, sys_snaps, config)

        if anomalies:
            anomaly_dicts = [a.to_dict() for a in anomalies]
            count = db.insert_anomalies(anomaly_dicts)
            click.echo(f"\nDetected {count} anomalies:")
            for a in anomalies:
                icon = {"critical": "!!", "warning": "! ", "info": "  "}.get(a.severity, "  ")
                gpu_tag = f" [GPU {a.gpu_index}]" if a.gpu_index is not None else ""
                click.echo(f"  [{icon}] {a.severity.upper():8s}{gpu_tag} {a.description}")
        else:
            click.echo("\nNo anomalies detected — all metrics within normal ranges.")

        if correlate:
            log_events = db.get_log_events(start=start, end=end, limit=500)
            anomaly_dicts = [a.to_dict() for a in anomalies] if anomalies else []
            corr_config = config.get("analysis", {}).get("correlation", {})
            window = corr_config.get("time_window_seconds", 300)

            clusters = correlate_events(anomaly_dicts, log_events, window)
            if clusters:
                click.echo(f"\nCorrelated into {len(clusters)} incident cluster(s):")
                for c in clusters:
                    click.echo(f"  {c.summary}")
            else:
                click.echo("\nNo correlated incidents found.")
    finally:
        db.close()


@cli.command()
def status():
    """Show database statistics."""
    config = _load_config()
    db = _get_db(config)

    try:
        counts = db.get_row_counts()
        click.echo("\n Database Status")
        click.echo(f"  Path: {db.db_path}")
        click.echo(f"  {'─'*40}")
        for table, count in counts.items():
            click.echo(f"  {table:25s} {count:>8,d} rows")

        size_bytes = os.path.getsize(db.db_path)
        click.echo(f"  {'─'*40}")
        click.echo(f"  {'Total size':25s} {size_bytes / 1024:.1f} KB")
    finally:
        db.close()


@cli.command()
@click.option("--hours", default=24, help="Summary lookback window in hours")
@click.option("--dry-run", is_flag=True, help="Print the prompt without calling the LLM")
def summarize(hours, dry_run):
    """Generate an LLM-powered daily summary."""
    from src.summarizer.llm_client import LLMClient
    from src.summarizer.prompt_builder import (
        build_daily_summary_prompt, get_system_prompt,
    )
    from src.summarizer.report_generator import generate_report
    from src.analysis.anomaly import run_anomaly_detection
    from src.analysis.correlator import correlate_events

    config = _load_config()
    db = _get_db(config)

    try:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=hours)).isoformat()
        end = now.isoformat()

        click.echo(f"Gathering data for {hours}h window: {start[:19]} → {end[:19]} UTC")

        gpu_snaps = db.get_gpu_snapshots(start=start, end=end, limit=10000)
        gpu_stats = db.get_gpu_stats_summary(start, end)
        sys_snaps = db.get_system_snapshots(start=start, end=end, limit=10000)
        log_events = db.get_log_events(start=start, end=end, limit=200)
        prom_data = db.get_prometheus_snapshots(start=start, end=end)
        rf_data = db.get_redfish_snapshots(start=start, end=end, limit=5)
        mn_stats = db.get_multinode_stats_summary(start, end)

        anomaly_objs = run_anomaly_detection(gpu_snaps, sys_snaps, config)
        anomalies = [a.to_dict() for a in anomaly_objs]

        corr_config = config.get("analysis", {}).get("correlation", {})
        window = corr_config.get("time_window_seconds", 300)
        clusters = correlate_events(anomalies, log_events, window)

        prompt = build_daily_summary_prompt(
            gpu_stats=gpu_stats,
            log_events=log_events,
            anomalies=anomalies,
            system_snapshots=sys_snaps,
            period_start=start,
            period_end=end,
            incident_clusters=clusters,
            prometheus_data=prom_data,
            redfish_data=rf_data,
            multinode_stats=mn_stats,
        )

        if dry_run:
            click.echo(f"\n{'='*80}")
            click.echo(" DRY RUN — Prompt that would be sent to the LLM:")
            click.echo(f"{'='*80}")
            click.echo(f"\n[SYSTEM]\n{get_system_prompt()}\n")
            click.echo(f"[USER]\n{prompt}")
            click.echo(f"\nPrompt length: {len(prompt):,} chars")
            return

        llm_config = config.get("llm", {})
        client = LLMClient(
            base_url=llm_config.get("base_url", "http://localhost:30000/v1"),
            model=llm_config.get("model", "Qwen/Qwen3.5-397B-A17B"),
            max_tokens=llm_config.get("max_tokens", 4096),
            temperature=llm_config.get("temperature", 0.3),
        )

        click.echo("Sending to LLM... (this may take 30-120 seconds)")
        result = client.generate_summary(prompt, system_prompt=get_system_prompt())

        report_config = config.get("reports", {})
        report_path = generate_report(
            content=result["content"],
            period_start=start,
            period_end=end,
            output_dir=report_config.get("output_dir", "reports"),
            node_name=config.get("cluster", {}).get("node_name", "gpu003"),
        )

        db.insert_summary({
            "timestamp": now.isoformat(),
            "period_start": start,
            "period_end": end,
            "summary_type": "daily",
            "content": result["content"],
            "model_used": result.get("model"),
            "prompt_tokens": result.get("prompt_tokens"),
            "completion_tokens": result.get("completion_tokens"),
        })

        if anomalies:
            db.insert_anomalies(anomalies)

        click.echo(f"\nSummary generated: {report_path}")
        click.echo(f"Tokens: {result.get('prompt_tokens', '?')} prompt + "
                    f"{result.get('completion_tokens', '?')} completion")
        click.echo(f"Latency: {result.get('latency_seconds', '?')}s")
        click.echo(f"Anomalies: {len(anomalies)}, Incidents: {len(clusters)}")
    finally:
        db.close()


@cli.command()
def probe():
    """Check which data sources are currently reachable."""
    config = _load_config()

    def _status(ok, detail=""):
        return f"OK{' (' + detail + ')' if detail else ''}" if ok else f"SKIP ({detail})"

    # Local GPU
    try:
        from src.collectors.gpu_metrics import _try_pynvml
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=count", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        gpu_ok = result.returncode == 0
        gpu_count = len(result.stdout.strip().split("\n")) if gpu_ok else 0
        click.echo(f"  GPU (local nvidia-smi) .... {_status(gpu_ok, f'{gpu_count} GPUs')}")
    except Exception:
        click.echo(f"  GPU (local nvidia-smi) .... {_status(False, 'nvidia-smi not found')}")

    # System
    try:
        import psutil
        click.echo(f"  System (psutil) ........... {_status(True)}")
    except ImportError:
        click.echo(f"  System (psutil) ........... {_status(False, 'psutil not installed')}")

    # Logs
    log_sources = config.get("collection", {}).get("log_sources", [])
    for ls in log_sources:
        path = ls.get("path", "")
        readable = os.path.isfile(path) and os.access(path, os.R_OK)
        click.echo(f"  Logs ({ls.get('type', '?'):15s}) .. {_status(readable, path)}")

    # Prometheus (direct)
    from src.collectors.prometheus import probe as prom_probe
    prom_url = config.get("prometheus", {}).get("url", "")
    prom_ok = prom_probe(prom_url) if prom_url else False
    prom_detail = "url not configured" if not prom_url else (prom_url if prom_ok else "unreachable")
    click.echo(f"  Prometheus (direct) ....... {_status(prom_ok, prom_detail)}")

    # Prometheus (S3)
    from src.collectors.s3_prometheus import probe as s3_prom_probe
    s3_config = config.get("prometheus_s3", {})
    s3_bucket = s3_config.get("bucket", "")
    s3_ok = s3_prom_probe(config) if s3_bucket else False
    s3_detail = "bucket not configured" if not s3_bucket else (f"s3://{s3_bucket}" if s3_ok else "unreachable")
    click.echo(f"  Prometheus (S3) ........... {_status(s3_ok, s3_detail)}")

    # AlertManager
    from src.collectors.alertmanager import probe as am_probe
    am_url = config.get("alertmanager", {}).get("url", "")
    am_ok = am_probe(am_url) if am_url else False
    am_detail = "url not configured" if not am_url else (am_url if am_ok else "unreachable")
    click.echo(f"  AlertManager .............. {_status(am_ok, am_detail)}")

    # Redfish
    from src.collectors.redfish import probe as rf_probe
    rf_config = config.get("redfish", {})
    rf_url = rf_config.get("url", "")
    rf_ok = rf_probe(rf_url, rf_config.get("username", ""), rf_config.get("password", "")) if rf_url else False
    rf_detail = "url not configured" if not rf_url else (rf_url if rf_ok else "unreachable")
    click.echo(f"  Redfish ................... {_status(rf_ok, rf_detail)}")

    # Multi-node SSH
    from src.collectors.multinode import probe_node
    mn_nodes = config.get("multinode", {}).get("nodes", [])
    mn_timeout = config.get("multinode", {}).get("ssh_timeout", 5)
    reachable = []
    for node in mn_nodes:
        if probe_node(node, timeout=mn_timeout):
            reachable.append(node)
    mn_detail = f"{len(reachable)}/{len(mn_nodes)} nodes reachable"
    if reachable:
        mn_detail += f" ({', '.join(reachable)})"
    click.echo(f"  Multi-node SSH ............ {_status(bool(reachable), mn_detail)}")

    # LLM
    try:
        from src.summarizer.llm_client import LLMClient
        llm_config = config.get("llm", {})
        client = LLMClient(
            base_url=llm_config.get("base_url", "http://localhost:30000/v1"),
            model=llm_config.get("model", "Qwen/Qwen3.5-397B-A17B"),
        )
        llm_ok = client.health_check()
        click.echo(f"  LLM (vLLM) ................ {_status(llm_ok, client.model)}")
    except Exception as e:
        click.echo(f"  LLM (vLLM) ................ {_status(False, str(e))}")


if __name__ == "__main__":
    cli()
