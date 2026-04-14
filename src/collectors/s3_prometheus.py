"""
S3-based Prometheus snapshot collector.

Downloads Prometheus metric snapshots from an S3 bucket as an alternative
to direct Prometheus API access. The cluster admin deposits JSON files
(in Prometheus query-response format) into the bucket, and this collector
ingests them into the same pipeline.

The output is identical to what prometheus.py produces (list[PrometheusResult]),
so storage, prompt building, and summarization require zero changes.

Gracefully skips if the bucket is unreachable or unconfigured.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.collectors.prometheus import PrometheusResult

logger = logging.getLogger(__name__)

# Supported file extensions for snapshot files
SUPPORTED_EXTENSIONS = (".json",)


def _build_s3_client(config: dict):
    """Build a boto3 S3 client from the prometheus_s3 config block."""
    s3_config = config.get("prometheus_s3", {})
    kwargs = {}

    region = s3_config.get("region", "")
    if region:
        kwargs["region_name"] = region

    endpoint_url = s3_config.get("endpoint_url", "")
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url

    return boto3.client("s3", **kwargs)


def probe(config: dict, timeout: int = 5) -> bool:
    """Check if the S3 bucket is reachable.

    Performs a lightweight HeadBucket call to verify connectivity and
    permissions without listing or downloading objects.
    """
    s3_config = config.get("prometheus_s3", {})
    if not s3_config.get("enabled", False):
        return False

    bucket = s3_config.get("bucket", "")
    if not bucket:
        return False

    try:
        client = _build_s3_client(config)
        client.head_bucket(Bucket=bucket)
        return True
    except (BotoCoreError, ClientError) as e:
        logger.debug("S3 Prometheus probe failed for bucket '%s': %s", bucket, e)
        return False
    except Exception as e:
        logger.debug("S3 Prometheus probe unexpected error: %s", e)
        return False


def _list_snapshot_keys(
    client,
    bucket: str,
    prefix: str,
    after_key: str = "",
) -> list[dict]:
    """List all JSON snapshot objects in the bucket under the given prefix.

    Returns a list of dicts with 'Key', 'LastModified', and 'Size'.
    If after_key is provided, only returns objects whose key is
    lexicographically greater (for bookmark-based dedup).
    """
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    page_kwargs = {"Bucket": bucket, "Prefix": prefix}
    if after_key:
        page_kwargs["StartAfter"] = after_key

    try:
        for page in paginator.paginate(**page_kwargs):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if any(key.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                    keys.append({
                        "Key": key,
                        "LastModified": obj["LastModified"],
                        "Size": obj.get("Size", 0),
                    })
    except (BotoCoreError, ClientError) as e:
        logger.warning("S3: failed to list objects in s3://%s/%s: %s", bucket, prefix, e)

    return sorted(keys, key=lambda x: x["Key"])


def _parse_snapshot_file(raw_bytes: bytes, key: str) -> list[PrometheusResult]:
    """Parse a single snapshot JSON file into PrometheusResult objects.

    Supports two formats:
    1. Single Prometheus API response:
       {"status": "success", "data": {"resultType": "vector", "result": [...]}}
    2. Batch format — a list of named query results:
       [{"query_name": "...", "promql": "...", "data": {"result": [...]}}]

    Falls back to treating the entire file as a single result if it has
    'query_name' and 'results' at the top level.
    """
    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError as e:
        logger.warning("S3: skipping malformed JSON in %s: %s", key, e)
        return []

    ts = datetime.now(timezone.utc).isoformat()

    # Try to extract a timestamp from the key (best-effort)
    # e.g. "snapshots/2026-04-14T12:00:00Z.json" or "gpu_temp_20260414_120000.json"
    file_ts = _extract_timestamp_from_key(key)
    if file_ts:
        ts = file_ts

    results = []

    if isinstance(data, list):
        # Batch format: list of query results
        for item in data:
            if not isinstance(item, dict):
                continue
            qname = item.get("query_name", _derive_query_name(key))
            promql = item.get("promql", qname)
            raw_results = _extract_results(item)
            if raw_results is not None:
                results.append(PrometheusResult(
                    timestamp=item.get("timestamp", ts),
                    query_name=qname,
                    promql=promql,
                    results=raw_results,
                ))

    elif isinstance(data, dict):
        # Single file — could be Prometheus API response or our own format
        if "data" in data and isinstance(data["data"], dict):
            # Prometheus API response format
            raw_results = data["data"].get("result", [])
            qname = data.get("query_name", _derive_query_name(key))
            promql = data.get("promql", data.get("data", {}).get("query", qname))
            results.append(PrometheusResult(
                timestamp=data.get("timestamp", ts),
                query_name=qname,
                promql=promql,
                results=raw_results,
            ))
        elif "query_name" in data and "results" in data:
            # Direct PrometheusResult-like format
            results.append(PrometheusResult(
                timestamp=data.get("timestamp", ts),
                query_name=data["query_name"],
                promql=data.get("promql", data["query_name"]),
                results=data["results"],
            ))
        else:
            logger.warning(
                "S3: unrecognized format in %s (keys: %s), skipping",
                key, list(data.keys())[:5],
            )

    return results


def _extract_results(item: dict) -> Optional[list]:
    """Pull the results list out of various possible shapes."""
    if "data" in item and isinstance(item["data"], dict):
        return item["data"].get("result", [])
    if "results" in item:
        return item["results"]
    if "result" in item:
        return item["result"]
    return None


def _derive_query_name(key: str) -> str:
    """Derive a query name from the S3 object key.

    Examples:
        'snapshots/gpu_temperature/2026-04-14.json' → 'gpu_temperature'
        'gpu_temperature_20260414.json' → 'gpu_temperature_20260414'
    """
    import os
    basename = os.path.basename(key)
    name = os.path.splitext(basename)[0]
    # If the parent directory looks like a query name, use that instead
    parts = key.rstrip("/").split("/")
    if len(parts) >= 2:
        parent = parts[-2]
        # Heuristic: if parent looks like a metric name (lowercase, underscores)
        if parent and parent.replace("_", "").replace("-", "").isalnum():
            return parent
    return name


def _extract_timestamp_from_key(key: str) -> Optional[str]:
    """Best-effort timestamp extraction from an S3 key.

    Tries common patterns like ISO format or YYYYMMDD_HHMMSS in the filename.
    Returns None if no timestamp is found (caller will use current time).
    """
    import os
    import re

    basename = os.path.splitext(os.path.basename(key))[0]

    # Try ISO-ish: 2026-04-14T12:00:00Z or 2026-04-14T12:00:00
    iso_match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", basename)
    if iso_match:
        try:
            dt = datetime.fromisoformat(iso_match.group(1).replace("Z", "+00:00"))
            return dt.isoformat()
        except ValueError:
            pass

    # Try compact: 20260414_120000 or 20260414T120000
    compact_match = re.search(r"(\d{8})[_T](\d{6})", basename)
    if compact_match:
        try:
            dt = datetime.strptime(
                f"{compact_match.group(1)}_{compact_match.group(2)}",
                "%Y%m%d_%H%M%S",
            )
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    # Try date only: 2026-04-14
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", basename)
    if date_match:
        try:
            dt = datetime.strptime(date_match.group(1), "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    return None


def collect_s3_prometheus(
    config: dict,
    last_key: str = "",
) -> tuple[list[PrometheusResult], str]:
    """Download and parse Prometheus snapshots from S3.

    Args:
        config: Full application config dict.
        last_key: The last S3 key that was successfully processed (for dedup).
                  Objects with keys <= this value will be skipped.

    Returns:
        Tuple of (list of PrometheusResult, last_processed_key).
        The last_processed_key should be saved as a bookmark for the next run.
    """
    s3_config = config.get("prometheus_s3", {})
    if not s3_config.get("enabled", False):
        return [], last_key

    bucket = s3_config.get("bucket", "")
    if not bucket:
        logger.debug("S3 Prometheus: bucket not configured, skipping")
        return [], last_key

    prefix = s3_config.get("prefix", "")

    try:
        client = _build_s3_client(config)
    except Exception as e:
        logger.warning("S3 Prometheus: failed to create client: %s", e)
        return [], last_key

    # List new objects
    objects = _list_snapshot_keys(client, bucket, prefix, after_key=last_key)
    if not objects:
        logger.info("S3 Prometheus: no new snapshot files found")
        return [], last_key

    logger.info(
        "S3 Prometheus: found %d new snapshot file(s) in s3://%s/%s",
        len(objects), bucket, prefix,
    )

    all_results = []
    latest_key = last_key

    for obj in objects:
        key = obj["Key"]
        try:
            response = client.get_object(Bucket=bucket, Key=key)
            raw = response["Body"].read()
        except (BotoCoreError, ClientError) as e:
            logger.warning("S3 Prometheus: failed to download %s: %s", key, e)
            continue

        parsed = _parse_snapshot_file(raw, key)
        if parsed:
            all_results.extend(parsed)
            logger.debug("S3 Prometheus: parsed %d results from %s", len(parsed), key)
        else:
            logger.debug("S3 Prometheus: no results from %s", key)

        # Always advance the bookmark even if parsing yielded nothing,
        # so we don't re-download bad files on every run
        latest_key = key

    logger.info(
        "S3 Prometheus: collected %d metric results from %d files",
        len(all_results), len(objects),
    )
    return all_results, latest_key
