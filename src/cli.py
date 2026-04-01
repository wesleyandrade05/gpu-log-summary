"""
CLI entry point for the GPU Log Summarizer.

Usage:
    python -m src.cli collect          # Run one collection cycle
    python -m src.cli show             # Show latest metrics
    python -m src.cli show --events    # Show recent log events
    python -m src.cli analyze          # Run anomaly detection + correlation
    python -m src.cli status           # Show database stats
    python -m src.cli summarize        # Generate LLM summary (Week 3)
    python -m src.cli dashboard        # Start web dashboard (Week 4)
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
@click.option("--gpu/--no-gpu", default=True, help="Collect GPU metrics")
@click.option("--system/--no-system", default=True, help="Collect system metrics")
@click.option("--logs/--no-logs", default=True, help="Parse log files")
def collect(gpu, system, logs):
    """Run one data collection cycle (GPU metrics, system metrics, log events)."""
    config = _load_config()
    db = _get_db(config)

    try:
        if gpu:
            from src.collectors.gpu_metrics import collect_gpu_metrics
            snapshots = collect_gpu_metrics()
            count = db.insert_gpu_snapshots(snapshots)
            click.echo(f"Collected {count} GPU snapshots ({len(snapshots)} GPUs)")

        if system:
            from src.collectors.system_metrics import collect_system_metrics
            sys_snap = collect_system_metrics()
            db.insert_system_snapshot(sys_snap)
            click.echo(f"Collected system metrics (CPU: {sys_snap.cpu_percent}%, "
                        f"MEM: {sys_snap.memory_percent}%)")

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
            click.echo(f"Parsed {len(events)} new log events")

            db.save_bookmark("fabricmanager", fm_bm.path, fm_bm.offset, fm_bm.inode)
            db.save_bookmark("infiniband", ib_bm.path, ib_bm.offset, ib_bm.inode)

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
@click.option("--host", default=None, help="Dashboard host")
@click.option("--port", default=None, type=int, help="Dashboard port")
def dashboard(host, port):
    """Start the web dashboard (requires Week 4 modules)."""
    try:
        from src.dashboard.app import create_app
    except ImportError:
        click.echo("Dashboard module not yet implemented. Coming in Week 4.")
        return

    config = _load_config()
    dash_config = config.get("dashboard", {})
    h = host or dash_config.get("host", "0.0.0.0")
    p = port or dash_config.get("port", 8050)

    app = create_app(config)
    click.echo(f"Starting dashboard at http://{h}:{p}")
    app.run(host=h, port=p, debug=False)


if __name__ == "__main__":
    cli()
