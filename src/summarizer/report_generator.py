"""
Report generator — writes LLM summaries to Markdown files.

Creates timestamped report files in the reports/ directory with
consistent naming and a metadata header.
"""

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def generate_report(
    content: str,
    period_start: str,
    period_end: str,
    output_dir: str = "reports",
    node_name: str = "gpu003",
) -> str:
    """Write a summary to a Markdown report file.

    Returns the path to the generated report file.
    """
    os.makedirs(output_dir, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")
    filename = f"report_{date_str}_{time_str}_{node_name}.md"
    filepath = os.path.join(output_dir, filename)

    header = (
        f"---\n"
        f"node: {node_name}\n"
        f"generated: {now.isoformat()}\n"
        f"period_start: {period_start}\n"
        f"period_end: {period_end}\n"
        f"---\n\n"
        f"# GPU Cluster Daily Report — {node_name}\n"
        f"**Period:** {period_start[:19]} to {period_end[:19]} UTC  \n"
        f"**Generated:** {now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
        f"---\n\n"
    )

    with open(filepath, "w") as f:
        f.write(header)
        f.write(content)
        f.write("\n")

    logger.info("Report written to %s (%d bytes)", filepath, os.path.getsize(filepath))
    return filepath


def list_reports(output_dir: str = "reports") -> list[dict]:
    """List existing report files with metadata."""
    if not os.path.isdir(output_dir):
        return []

    reports = []
    for fname in sorted(os.listdir(output_dir), reverse=True):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(output_dir, fname)
        stat = os.stat(fpath)
        reports.append({
            "filename": fname,
            "path": fpath,
            "size_bytes": stat.st_size,
            "created": datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat(),
        })

    return reports
