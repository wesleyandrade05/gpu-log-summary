"""
Tests for the S3-based Prometheus snapshot collector.

Uses moto to mock AWS S3 in-process — no real AWS credentials or
network access required. These tests verify the full path from
S3 listing/download through JSON parsing into PrometheusResult
objects and finally into SQLite storage.
"""

import json
import os
import tempfile
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from src.collectors.s3_prometheus import (
    collect_s3_prometheus,
    probe,
    _parse_snapshot_file,
    _derive_query_name,
    _extract_timestamp_from_key,
    _list_snapshot_keys,
    _build_s3_client,
)
from src.collectors.prometheus import PrometheusResult
from src.storage.database import MetricsDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BUCKET = "test-prometheus-snapshots"
REGION = "us-east-1"


def _base_config(bucket=BUCKET, prefix="", enabled=True):
    """Build a minimal config dict for tests."""
    return {
        "prometheus_s3": {
            "enabled": enabled,
            "bucket": bucket,
            "prefix": prefix,
            "region": REGION,
            "endpoint_url": "",
        }
    }


def _make_prom_api_response(query_name="gpu_temperature", promql="DCGM_FI_DEV_GPU_TEMP"):
    """Create a snapshot in Prometheus API response format."""
    return {
        "status": "success",
        "query_name": query_name,
        "promql": promql,
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"__name__": promql, "gpu": "0", "instance": "gpu003:9400"},
                    "value": [1713100800, "72"],
                },
                {
                    "metric": {"__name__": promql, "gpu": "1", "instance": "gpu003:9400"},
                    "value": [1713100800, "68"],
                },
            ],
        },
    }


def _make_batch_response():
    """Create a batch snapshot with multiple queries."""
    return [
        {
            "query_name": "gpu_temperature",
            "promql": "DCGM_FI_DEV_GPU_TEMP",
            "timestamp": "2026-04-14T12:00:00+00:00",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"gpu": "0"}, "value": [1713100800, "72"]},
                ],
            },
        },
        {
            "query_name": "gpu_utilization",
            "promql": "DCGM_FI_DEV_GPU_UTIL",
            "timestamp": "2026-04-14T12:00:00+00:00",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"gpu": "0"}, "value": [1713100800, "85.5"]},
                ],
            },
        },
    ]


def _make_direct_result_format():
    """Create a snapshot in our own PrometheusResult-like format."""
    return {
        "query_name": "gpu_power_draw",
        "promql": "DCGM_FI_DEV_POWER_USAGE",
        "timestamp": "2026-04-14T14:00:00+00:00",
        "results": [
            {"metric": {"gpu": "0"}, "value": [1713100800, "350.5"]},
            {"metric": {"gpu": "1"}, "value": [1713100800, "420.0"]},
        ],
    }


def _upload_json(s3_client, bucket, key, data):
    """Helper to upload a JSON object to mocked S3."""
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data).encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Probe tests
# ---------------------------------------------------------------------------

class TestProbe:
    @mock_aws
    def test_probe_ok(self):
        """Probe returns True when bucket exists and is accessible."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        config = _base_config()
        assert probe(config) is True

    @mock_aws
    def test_probe_no_bucket(self):
        """Probe returns False gracefully when bucket doesn't exist."""
        config = _base_config(bucket="nonexistent-bucket")
        assert probe(config) is False

    def test_probe_disabled(self):
        """Probe returns False when S3 Prometheus is disabled."""
        config = _base_config(enabled=False)
        assert probe(config) is False

    def test_probe_empty_bucket_name(self):
        """Probe returns False when bucket name is empty."""
        config = _base_config(bucket="")
        assert probe(config) is False

    def test_probe_no_config(self):
        """Probe returns False when prometheus_s3 section is missing."""
        assert probe({}) is False


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------

class TestParseSnapshotFile:
    def test_parse_prometheus_api_format(self):
        """Parse a single Prometheus API response format file."""
        data = _make_prom_api_response()
        raw = json.dumps(data).encode()
        results = _parse_snapshot_file(raw, "snapshots/gpu_temperature/2026-04-14T12:00:00Z.json")

        assert len(results) == 1
        r = results[0]
        assert isinstance(r, PrometheusResult)
        assert r.query_name == "gpu_temperature"
        assert r.promql == "DCGM_FI_DEV_GPU_TEMP"
        assert len(r.results) == 2
        assert r.results[0]["value"][1] == "72"

    def test_parse_batch_format(self):
        """Parse a batch file with multiple queries."""
        data = _make_batch_response()
        raw = json.dumps(data).encode()
        results = _parse_snapshot_file(raw, "batch_2026-04-14.json")

        assert len(results) == 2
        assert results[0].query_name == "gpu_temperature"
        assert results[1].query_name == "gpu_utilization"

    def test_parse_direct_result_format(self):
        """Parse our own PrometheusResult-like format."""
        data = _make_direct_result_format()
        raw = json.dumps(data).encode()
        results = _parse_snapshot_file(raw, "gpu_power_draw.json")

        assert len(results) == 1
        assert results[0].query_name == "gpu_power_draw"
        assert len(results[0].results) == 2

    def test_parse_malformed_json(self):
        """Malformed JSON is skipped with a warning, not a crash."""
        raw = b"this is { not valid json !!!"
        results = _parse_snapshot_file(raw, "bad_file.json")
        assert results == []

    def test_parse_unrecognized_format(self):
        """Unrecognized JSON structure returns empty list."""
        raw = json.dumps({"foo": "bar", "baz": 42}).encode()
        results = _parse_snapshot_file(raw, "unknown.json")
        assert results == []

    def test_parse_empty_results(self):
        """File with valid format but empty results still parses."""
        data = {
            "status": "success",
            "query_name": "gpu_temp",
            "promql": "DCGM_FI_DEV_GPU_TEMP",
            "data": {"resultType": "vector", "result": []},
        }
        raw = json.dumps(data).encode()
        results = _parse_snapshot_file(raw, "empty.json")
        assert len(results) == 1
        assert results[0].results == []


# ---------------------------------------------------------------------------
# Timestamp extraction tests
# ---------------------------------------------------------------------------

class TestTimestampExtraction:
    def test_iso_timestamp(self):
        ts = _extract_timestamp_from_key("snapshots/2026-04-14T12:00:00Z.json")
        assert ts is not None
        assert "2026-04-14" in ts

    def test_compact_timestamp(self):
        ts = _extract_timestamp_from_key("gpu_temp_20260414_120000.json")
        assert ts is not None
        assert "2026-04-14" in ts

    def test_date_only(self):
        ts = _extract_timestamp_from_key("daily/2026-04-14.json")
        assert ts is not None
        assert "2026-04-14" in ts

    def test_no_timestamp(self):
        ts = _extract_timestamp_from_key("gpu_temperature.json")
        assert ts is None


# ---------------------------------------------------------------------------
# Query name derivation tests
# ---------------------------------------------------------------------------

class TestDeriveQueryName:
    def test_from_parent_dir(self):
        name = _derive_query_name("snapshots/gpu_temperature/2026-04-14.json")
        assert name == "gpu_temperature"

    def test_from_filename(self):
        name = _derive_query_name("gpu_temperature.json")
        assert name == "gpu_temperature"


# ---------------------------------------------------------------------------
# Collection tests (full S3 mock)
# ---------------------------------------------------------------------------

class TestCollectS3Prometheus:
    @mock_aws
    def test_collect_single_file(self):
        """Single snapshot file is downloaded and parsed correctly."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        data = _make_prom_api_response()
        _upload_json(conn, BUCKET, "gpu_temperature_2026-04-14.json", data)

        config = _base_config()
        results, last_key = collect_s3_prometheus(config)

        assert len(results) == 1
        assert results[0].query_name == "gpu_temperature"
        assert len(results[0].results) == 2
        assert last_key == "gpu_temperature_2026-04-14.json"

    @mock_aws
    def test_collect_multiple_files(self):
        """Multiple snapshot files are all processed."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        _upload_json(conn, BUCKET, "snap_001.json", _make_prom_api_response("gpu_temp"))
        _upload_json(conn, BUCKET, "snap_002.json", _make_prom_api_response("gpu_util"))
        _upload_json(conn, BUCKET, "snap_003.json", _make_prom_api_response("gpu_mem"))

        config = _base_config()
        results, last_key = collect_s3_prometheus(config)

        assert len(results) == 3
        assert last_key == "snap_003.json"

    @mock_aws
    def test_collect_with_prefix(self):
        """Only files under the configured prefix are collected."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        # Under prefix
        _upload_json(conn, BUCKET, "gpu003/snap_001.json", _make_prom_api_response())
        _upload_json(conn, BUCKET, "gpu003/snap_002.json", _make_prom_api_response())
        # Not under prefix
        _upload_json(conn, BUCKET, "gpu004/snap_001.json", _make_prom_api_response())

        config = _base_config(prefix="gpu003/")
        results, last_key = collect_s3_prometheus(config)

        assert len(results) == 2
        assert last_key.startswith("gpu003/")

    @mock_aws
    def test_collect_bookmark_dedup(self):
        """Second collection run skips already-processed files."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        _upload_json(conn, BUCKET, "snap_001.json", _make_prom_api_response())
        _upload_json(conn, BUCKET, "snap_002.json", _make_prom_api_response())

        config = _base_config()

        # First run — gets both files
        results1, last_key1 = collect_s3_prometheus(config)
        assert len(results1) == 2
        assert last_key1 == "snap_002.json"

        # Second run with bookmark — gets nothing
        results2, last_key2 = collect_s3_prometheus(config, last_key=last_key1)
        assert len(results2) == 0
        assert last_key2 == last_key1

        # Add a new file
        _upload_json(conn, BUCKET, "snap_003.json", _make_prom_api_response())

        # Third run — gets only the new file
        results3, last_key3 = collect_s3_prometheus(config, last_key=last_key1)
        assert len(results3) == 1
        assert last_key3 == "snap_003.json"

    @mock_aws
    def test_collect_bad_json_skipped(self):
        """Malformed JSON files are skipped without crashing."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        # Bad file
        conn.put_object(Bucket=BUCKET, Key="bad.json", Body=b"not json at all!!!")
        # Good file
        _upload_json(conn, BUCKET, "good.json", _make_prom_api_response())

        config = _base_config()
        results, last_key = collect_s3_prometheus(config)

        # Only the good file produces results
        assert len(results) == 1
        # But the bookmark advances past both files
        assert last_key == "good.json"

    @mock_aws
    def test_collect_empty_bucket(self):
        """Empty bucket returns empty list cleanly."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        config = _base_config()
        results, last_key = collect_s3_prometheus(config)

        assert results == []
        assert last_key == ""

    def test_collect_disabled(self):
        """Disabled config returns empty list without touching S3."""
        config = _base_config(enabled=False)
        results, last_key = collect_s3_prometheus(config)
        assert results == []

    def test_collect_no_bucket(self):
        """Empty bucket name returns empty list without error."""
        config = _base_config(bucket="")
        results, last_key = collect_s3_prometheus(config)
        assert results == []

    @mock_aws
    def test_collect_batch_format_file(self):
        """Batch-format files (list of queries) are parsed correctly."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        _upload_json(conn, BUCKET, "batch_2026-04-14.json", _make_batch_response())

        config = _base_config()
        results, last_key = collect_s3_prometheus(config)

        assert len(results) == 2
        names = {r.query_name for r in results}
        assert names == {"gpu_temperature", "gpu_utilization"}

    @mock_aws
    def test_collect_ignores_non_json_files(self):
        """Non-JSON files (e.g. .txt, .csv) are ignored."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        conn.put_object(Bucket=BUCKET, Key="readme.txt", Body=b"just a readme")
        conn.put_object(Bucket=BUCKET, Key="data.csv", Body=b"col1,col2\n1,2")
        _upload_json(conn, BUCKET, "snap.json", _make_prom_api_response())

        config = _base_config()
        results, last_key = collect_s3_prometheus(config)

        assert len(results) == 1
        assert last_key == "snap.json"

    @mock_aws
    def test_collect_nested_directory_structure(self):
        """Files organized in query_name/timestamp.json structure work."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        _upload_json(
            conn, BUCKET,
            "gpu_temperature/2026-04-14T12:00:00Z.json",
            _make_prom_api_response("gpu_temperature"),
        )
        _upload_json(
            conn, BUCKET,
            "gpu_utilization/2026-04-14T12:00:00Z.json",
            _make_prom_api_response("gpu_utilization"),
        )

        config = _base_config()
        results, last_key = collect_s3_prometheus(config)

        assert len(results) == 2
        names = {r.query_name for r in results}
        assert names == {"gpu_temperature", "gpu_utilization"}


# ---------------------------------------------------------------------------
# End-to-end: S3 → collector → SQLite
# ---------------------------------------------------------------------------

class TestEndToEnd:
    @mock_aws
    def test_e2e_into_database(self):
        """Full pipeline: mock S3 → collector → insert into SQLite → query back."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        _upload_json(conn, BUCKET, "snap_001.json", _make_prom_api_response("gpu_temperature"))
        _upload_json(conn, BUCKET, "snap_002.json", _make_direct_result_format())

        config = _base_config()
        results, last_key = collect_s3_prometheus(config)

        assert len(results) == 2

        # Insert into a temporary database
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = MetricsDB(db_path)

            try:
                count = db.insert_prometheus_snapshots(results)
                assert count == 2

                # Query back
                rows = db.get_prometheus_snapshots()
                assert len(rows) == 2

                # Verify data integrity
                names = {r["query_name"] for r in rows}
                assert "gpu_temperature" in names
                assert "gpu_power_draw" in names

                # Verify results were stored as JSON and can be deserialized
                for row in rows:
                    assert isinstance(row["results"], list)
                    assert len(row["results"]) >= 1

                # Verify row counts
                counts = db.get_row_counts()
                assert counts["prometheus_snapshots"] == 2
            finally:
                db.close()

    @mock_aws
    def test_e2e_bookmark_persistence(self):
        """Bookmark is saved and used across collection runs via database."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        _upload_json(conn, BUCKET, "snap_001.json", _make_prom_api_response())
        _upload_json(conn, BUCKET, "snap_002.json", _make_prom_api_response())

        config = _base_config()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = MetricsDB(db_path)

            try:
                # First collection
                bm = db.get_bookmark("prometheus_s3")
                last_key = bm["file_path"] if bm else ""
                results, new_key = collect_s3_prometheus(config, last_key=last_key)
                assert len(results) == 2
                db.insert_prometheus_snapshots(results)
                db.save_bookmark("prometheus_s3", new_key, 0, 0)

                # Add another file
                _upload_json(conn, BUCKET, "snap_003.json", _make_prom_api_response())

                # Second collection using persisted bookmark
                bm = db.get_bookmark("prometheus_s3")
                last_key = bm["file_path"]
                assert last_key == "snap_002.json"

                results2, new_key2 = collect_s3_prometheus(config, last_key=last_key)
                assert len(results2) == 1
                db.insert_prometheus_snapshots(results2)
                db.save_bookmark("prometheus_s3", new_key2, 0, 0)

                # Total in DB
                counts = db.get_row_counts()
                assert counts["prometheus_snapshots"] == 3
            finally:
                db.close()


# ---------------------------------------------------------------------------
# S3 listing tests
# ---------------------------------------------------------------------------

class TestListSnapshotKeys:
    @mock_aws
    def test_list_with_start_after(self):
        """StartAfter correctly filters out earlier keys."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        _upload_json(conn, BUCKET, "a.json", {})
        _upload_json(conn, BUCKET, "b.json", {})
        _upload_json(conn, BUCKET, "c.json", {})

        keys = _list_snapshot_keys(conn, BUCKET, "", after_key="a.json")
        key_names = [k["Key"] for k in keys]

        assert "a.json" not in key_names
        assert "b.json" in key_names
        assert "c.json" in key_names

    @mock_aws
    def test_list_sorted(self):
        """Results are sorted by key."""
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(Bucket=BUCKET)

        _upload_json(conn, BUCKET, "c.json", {})
        _upload_json(conn, BUCKET, "a.json", {})
        _upload_json(conn, BUCKET, "b.json", {})

        keys = _list_snapshot_keys(conn, BUCKET, "")
        key_names = [k["Key"] for k in keys]

        assert key_names == ["a.json", "b.json", "c.json"]
