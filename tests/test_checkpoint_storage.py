import os
from datetime import timedelta, timezone

import pytest

from logpipe.anomaly import Alert
from logpipe.checkpoint import Checkpoint, read_new_lines, start_offset
from logpipe.percentiles import exact_percentile
from logpipe.storage import Store


def test_read_new_lines_batches_and_resumes(tmp_path):
    log = tmp_path / "a.log"
    cp = Checkpoint.load(tmp_path / "cp.json")
    log.write_text("l1\nl2\nl3\n")
    batches = list(read_new_lines(log, cp, batch_size=2))
    assert batches == [["l1\n", "l2\n"], ["l3\n"]]
    assert cp.state_for(log).lines == 3
    assert (tmp_path / "cp.json").exists()

    with log.open("a") as fh:
        fh.write("l4\nl5")  # l5 has no newline yet
    assert list(read_new_lines(log, cp)) == [["l4\n"]]
    with log.open("a") as fh:
        fh.write("\n")
    assert list(read_new_lines(log, cp)) == [["l5\n"]]
    assert list(read_new_lines(log, cp)) == []


def test_checkpoint_roundtrip_and_rotation(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("x\n" * 10)
    cp = Checkpoint.load(tmp_path / "cp.json")
    list(read_new_lines(log, cp))
    reloaded = Checkpoint.load(tmp_path / "cp.json")
    assert reloaded.state_for(log).offset == 20

    # Truncation (logrotate copytruncate): file shrinks -> start over.
    log.write_text("y\n")
    assert start_offset(log, reloaded) == 0
    # Rotation: new inode -> start over.
    os.replace(log, tmp_path / "a.log.1")
    log.write_text("z\n" * 3)
    assert start_offset(log, reloaded) == 0
    assert list(read_new_lines(log, reloaded)) == [["z\n"] * 3]
    reloaded.reset(log)
    assert reloaded.state_for(log) is None
    assert start_offset(log, None) == 0


def test_store_counts_and_percentiles_match_exact(store, sample_records):
    assert store.count() == len(sample_records)
    latencies = [r.latency_ms for r in sample_records]
    p = store.latency_percentiles()
    assert p.samples == len(latencies)
    assert p.p50 == pytest.approx(exact_percentile(latencies, 50))
    assert p.p95 == pytest.approx(exact_percentile(latencies, 95))
    assert p.p99 == pytest.approx(exact_percentile(latencies, 99))
    expected_err = sum(r.status >= 500 for r in sample_records) / len(sample_records)
    assert store.error_rate() == pytest.approx(expected_err)


def test_store_per_minute_series_is_gap_free_and_windowed(store):
    first, last = store.time_range()
    series = store.requests_per_minute()
    assert len(series) == 180
    assert series[0][0] == first.replace(second=0, microsecond=0)
    assert sum(c for _, c, _ in series) == store.count()
    window = store.requests_per_minute(since=last - timedelta(minutes=30))
    assert 30 <= len(window) <= 31
    assert store.total_requests(since=last - timedelta(minutes=30)) == sum(c for _, c, _ in window)
    assert store.total_requests(until=first) == 0


def test_store_top_lists_and_breakdowns(store, sample_records):
    top = store.top_endpoints(3)
    assert len(top) == 3 and top[0][1] >= top[1][1] >= top[2][1]
    assert all(p.count("{") >= 0 for p, _, _ in top)
    assert dict(store.status_breakdown())["2xx"] > 0
    assert sum(c for _, c in store.device_breakdown()) == len(sample_records)
    slow = store.slowest_endpoints(5, min_requests=20)
    assert slow and slow[0][1] >= slow[-1][1]
    assert "/api/v1/payments/bkash/execute" in [p for p, _, _ in slow]
    assert store.top_ips(2)[0][1] >= store.top_ips(2)[1][1]
    assert store.bytes_sent() == sum(r.bytes_sent for r in sample_records)


def test_store_export_parquet(store, tmp_path):
    out = tmp_path / "requests.parquet"
    exported = store.export_parquet(out)
    assert exported == store.count()
    assert out.stat().st_size > 1000
    first, last = store.time_range()
    partial = store.export_parquet(tmp_path / "p.parquet", since=last - timedelta(minutes=10))
    assert 0 < partial < exported


def test_store_alerts_roundtrip(store):
    first, last = store.time_range()
    alert = Alert("request_rate", last, 300.0, 40.0, 2.0, 100.0, "critical")
    assert store.insert_alerts([alert]) == 1
    rows = store.recent_alerts()
    assert rows[0]["metric"] == "request_rate" and rows[0]["severity"] == "critical"
    assert store.recent_alerts(since=last + timedelta(minutes=1)) == []
    assert store.insert_alerts([]) == 0


def test_store_empty_and_file_backed(tmp_path):
    empty = Store(":memory:")
    assert empty.time_range() is None and empty.count() == 0
    assert empty.requests_per_minute() == []
    assert empty.latency_percentiles().samples == 0
    assert empty.error_rate() == 0.0
    empty.close()
    path = tmp_path / "t.duckdb"
    with Store(path) as s:
        assert s.insert([]) == 0
    with Store(path, read_only=True) as ro:
        assert ro.count() == 0
        assert ro.time_range() is None or ro.time_range()[0].tzinfo is timezone.utc
