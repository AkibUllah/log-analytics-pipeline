from datetime import datetime, timedelta, timezone

import pytest

from logpipe import rollup
from logpipe.models import LogRecord
from logpipe.percentiles import QuantileSketch, exact_percentile
from logpipe.rollup import compact, compaction_bounds, floor_hour
from logpipe.storage import Store
from tests.conftest import END


def _rec(ts, status=200, latency=10.0, path="/x", nbytes=100):
    return LogRecord(
        ts=ts, ip="10.0.0.1", method="GET", path=path, status=status, bytes_sent=nbytes,
        latency_ms=latency, path_normalized=path, status_class=f"{status // 100}xx",
        device="desktop", browser="Chrome",
    )  # fmt: skip


def _hourly(store):
    return store.con.execute("SELECT * FROM hourly ORDER BY hour").fetchall()


def _hourly_counts(store):
    return store.con.execute(
        "SELECT * FROM hourly_counts ORDER BY hour, dim, key NULLS FIRST"
    ).fetchall()


def test_compaction_bounds_are_hour_aligned_and_validated():
    anchor = datetime(2026, 10, 20, 12, 34, 56, tzinfo=timezone.utc)
    raw, hourly = compaction_bounds(anchor, timedelta(hours=2), timedelta(days=1))
    assert raw == datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    assert hourly == datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    assert compaction_bounds(anchor, timedelta(0))[1] is None
    with pytest.raises(ValueError):
        compaction_bounds(anchor, timedelta(days=2), timedelta(days=1))
    with pytest.raises(ValueError):
        compaction_bounds(anchor, timedelta(hours=-1))


def test_compact_preserves_totals_per_hour(store, sample_records):
    expected = store.con.execute(
        """
        SELECT date_trunc('hour', ts), count(*), count(*) FILTER (WHERE status >= 500),
               sum(bytes_sent), count(latency_ms)
        FROM requests WHERE ts < ? GROUP BY 1 ORDER BY 1
        """,
        [datetime(2026, 10, 20, 11)],
    ).fetchall()
    boundary = END - timedelta(hours=1)
    result = compact(store, boundary)

    assert result.hours_updated == len(expected) == 2
    assert result.rows_compacted == sum(row[1] for row in expected)
    assert [row[:5] for row in _hourly(store)] == expected
    assert store.count() == len(sample_records) - result.rows_compacted
    assert store.con.execute("SELECT min(ts) FROM requests").fetchone()[0] >= datetime(
        2026, 10, 20, 11
    )
    # Every dimension accounts for every rolled-up request.
    for dim in rollup.ROLLUP_DIMENSIONS:
        total = store.con.execute(
            "SELECT sum(requests) FROM hourly_counts WHERE dim = ?", [dim]
        ).fetchone()[0]
        assert total == result.rows_compacted


def test_hourly_sketch_percentiles_are_within_tolerance(store, sample_records):
    hour = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
    latencies = [r.latency_ms for r in sample_records if hour <= r.ts < hour + timedelta(hours=1)]
    compact(store, hour + timedelta(hours=1))
    (text,) = store.con.execute("SELECT latency_sketch FROM hourly").fetchone()
    sketch = QuantileSketch.from_json(text)
    assert sketch.count == len(latencies)
    assert sketch.min == min(latencies) and sketch.max == max(latencies)
    for p in (50, 95, 99):
        exact = exact_percentile(latencies, p)
        assert sketch.percentile(p) == pytest.approx(exact, rel=0.05), p


def test_compact_is_idempotent(store):
    boundary = END - timedelta(hours=1)
    first = compact(store, boundary)
    snapshot = (_hourly(store), _hourly_counts(store), store.count())
    second = compact(store, boundary)
    assert first.rows_compacted > 0
    assert (second.rows_compacted, second.hours_updated, second.hours_expired) == (0, 0, 0)
    assert (_hourly(store), _hourly_counts(store), store.count()) == snapshot


def test_compact_boundary_is_exclusive():
    store = Store(":memory:")
    boundary = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    store.insert(
        [
            _rec(boundary - timedelta(hours=1)),  # first instant of 09:00
            _rec(boundary - timedelta(microseconds=1), status=503, latency=900.0),
            _rec(boundary),  # first instant of 10:00 stays raw
            _rec(boundary + timedelta(minutes=59, seconds=59)),
        ]
    )
    result = compact(store, boundary)
    assert result.rows_compacted == 2 and result.hours_updated == 1
    (row,) = _hourly(store)
    assert row[0] == datetime(2026, 10, 20, 9)
    assert row[1:5] == (2, 1, 200, 2)
    assert QuantileSketch.from_json(row[5]).max == 900.0
    assert store.count() == 2
    store.close()


def test_late_rows_merge_into_an_existing_rollup():
    store = Store(":memory:")
    hour = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
    store.insert([_rec(hour + timedelta(minutes=m), latency=float(m)) for m in range(10)])
    compact(store, hour + timedelta(hours=1))
    # A rotated file ingested late brings more rows for the already compacted hour.
    store.insert([_rec(hour + timedelta(minutes=30), status=500, latency=1000.0, path="/y")])
    result = compact(store, hour + timedelta(hours=1))
    assert result.rows_compacted == 1
    (row,) = _hourly(store)
    assert row[1:5] == (11, 1, 1100, 11)
    sketch = QuantileSketch.from_json(row[5])
    assert sketch.count == 11 and sketch.max == 1000.0
    counts = {(d, k): (n, e) for _, d, k, n, e in _hourly_counts(store)}
    assert counts[("path", "/x")] == (10, 0)
    assert counts[("path", "/y")] == (1, 1)
    assert counts[("status_class", "5xx")] == (1, 1)
    assert counts[("device", "desktop")] == (11, 1)
    assert store.count() == 0
    store.close()


def test_hourly_retention_expires_old_rollups(store):
    compact(store, END - timedelta(hours=1))
    result = compact(store, END - timedelta(hours=1), hourly_before=END - timedelta(hours=2))
    assert result.hours_expired == 1
    assert [row[0] for row in _hourly(store)] == [datetime(2026, 10, 20, 10)]
    assert {row[0] for row in _hourly_counts(store)} == {datetime(2026, 10, 20, 10)}


def test_rows_without_latency_and_null_dimensions():
    store = Store(":memory:")
    hour = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
    rec = _rec(hour, latency=None)
    rec.device = ""  # stored as NULL
    store.insert([rec])
    compact(store, hour + timedelta(hours=1))
    (row,) = _hourly(store)
    assert row[1] == 1 and row[4] == 0 and row[5] is None
    assert ("device", None, 1) in [(d, k, n) for _, d, k, n, _ in _hourly_counts(store)]
    store.close()


def test_compact_validates_bounds(store):
    with pytest.raises(ValueError):
        compact(store, END - timedelta(minutes=30))
    with pytest.raises(ValueError):
        compact(store, END - timedelta(hours=2), hourly_before=END - timedelta(hours=1))


def test_compact_rolls_back_on_failure(store, monkeypatch):
    before = store.count()
    calls = []

    def failing_fold(s, hour):
        calls.append(hour)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return original(s, hour)

    original = rollup._fold_hour
    monkeypatch.setattr(rollup, "_fold_hour", failing_fold)
    with pytest.raises(RuntimeError):
        compact(store, END - timedelta(hours=1))
    assert store.count() == before
    assert _hourly(store) == [] and _hourly_counts(store) == []


def test_compact_file_backed_store(tmp_path, sample_records):
    path = tmp_path / "c.duckdb"
    with Store(path) as s:
        s.insert(sample_records)
        result = compact(s, floor_hour(END - timedelta(hours=1)))
    with Store(path, read_only=True) as ro:
        hourly_total = ro.con.execute("SELECT sum(requests) FROM hourly").fetchone()[0]
        assert hourly_total == result.rows_compacted
        assert ro.count() + hourly_total == len(sample_records)
