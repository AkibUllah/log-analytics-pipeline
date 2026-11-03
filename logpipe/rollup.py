"""Hourly rollups and retention: fold old raw requests into ``hourly`` tables.

``compact`` takes every raw row older than an hour-aligned boundary, adds it to the
rollup for its hour (merging with a rollup that already exists, so late-arriving rows
are not lost), deletes the raw rows and optionally expires old rollups. It all runs in
one transaction, so a crash can never leave a request counted twice or not at all,
and running it again with nothing new to fold in changes nothing.

Every rollup column is mergeable: counts and byte totals add, and latency is kept as a
serialised ``QuantileSketch`` rather than as precomputed percentiles, because
percentiles of different hours cannot be averaged into a correct percentile.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from logpipe.percentiles import QuantileSketch
from logpipe.storage import Store, _naive_utc

logger = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
SKETCH_COMPRESSION = 100.0

# Dimensions kept per hour in ``hourly_counts`` (dimension name -> requests column).
ROLLUP_DIMENSIONS = {
    "status_class": "status_class",
    "device": "device",
    "path": "path_normalized",
}


@dataclass(frozen=True)
class CompactionResult:
    raw_before: datetime
    hourly_before: datetime | None
    rows_compacted: int
    hours_updated: int
    hours_expired: int


def floor_hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def compaction_bounds(
    anchor: datetime, keep_raw: timedelta, keep_hourly: timedelta | None = None
) -> tuple[datetime, datetime | None]:
    """Hour-aligned cut-offs for ``compact``: raw rows before the first are rolled up,
    rollups before the second are deleted. Aligning to the hour means a rollup never
    covers part of an hour whose remaining rows are still raw."""
    if keep_raw < timedelta(0):
        raise ValueError("keep_raw must not be negative")
    if keep_hourly is not None and keep_hourly < keep_raw:
        raise ValueError("hourly retention must be at least as long as raw retention")
    anchor = anchor.astimezone(timezone.utc)
    raw_before = floor_hour(anchor - keep_raw)
    hourly_before = floor_hour(anchor - keep_hourly) if keep_hourly is not None else None
    return raw_before, hourly_before


def compact(
    store: Store, raw_before: datetime, hourly_before: datetime | None = None
) -> CompactionResult:
    """Roll raw rows with ``ts < raw_before`` into hourly rollups, delete them and drop
    rollups with ``hour < hourly_before``. Both bounds must be hour-aligned."""
    for bound in (raw_before, hourly_before):
        if bound is not None and bound != floor_hour(bound):
            raise ValueError(f"compaction bound {bound.isoformat()} is not on an hour boundary")
    if hourly_before is not None and hourly_before > raw_before:
        raise ValueError("hourly_before must not be later than raw_before")
    raw_cut = _naive_utc(raw_before)
    hourly_cut = _naive_utc(hourly_before) if hourly_before is not None else None

    con = store.con
    con.execute("BEGIN TRANSACTION")
    try:
        hours = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT date_trunc('hour', ts) FROM requests WHERE ts < ? ORDER BY 1",
                [raw_cut],
            ).fetchall()
        ]
        rows = 0
        for hour in hours:
            rows += _fold_hour(store, hour)
        con.execute("DELETE FROM requests WHERE ts < ?", [raw_cut])
        expired = 0
        if hourly_cut is not None:
            (expired,) = con.execute(
                "SELECT count(*) FROM hourly WHERE hour < ?", [hourly_cut]
            ).fetchone()
            con.execute("DELETE FROM hourly WHERE hour < ?", [hourly_cut])
            con.execute("DELETE FROM hourly_counts WHERE hour < ?", [hourly_cut])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    # Deleted rows only give space back to the file once DuckDB checkpoints.
    con.execute("CHECKPOINT")
    logger.info(
        "compacted %d rows into %d hours before %s; expired %d hours",
        rows, len(hours), raw_before.isoformat(), expired,
    )  # fmt: skip
    return CompactionResult(
        raw_before=raw_before,
        hourly_before=hourly_before,
        rows_compacted=rows,
        hours_updated=len(hours),
        hours_expired=int(expired),
    )


def _fold_hour(store: Store, hour: datetime) -> int:
    """Merge the raw rows of one hour into its rollup. Returns the raw row count."""
    con = store.con
    window = [hour, hour + HOUR]
    requests, errors, bytes_sent, samples = con.execute(
        """
        SELECT count(*), count(*) FILTER (WHERE status >= 500), coalesce(sum(bytes_sent), 0),
               count(latency_ms)
        FROM requests WHERE ts >= ? AND ts < ?
        """,
        window,
    ).fetchone()
    raw_rows = int(requests)

    # Duplicate latencies are common (nginx logs request_time in whole milliseconds), so
    # feed the sketch value/weight pairs instead of one call per request.
    sketch = QuantileSketch(compression=SKETCH_COMPRESSION)
    for value, weight in con.execute(
        "SELECT latency_ms, count(*) FROM requests WHERE ts >= ? AND ts < ? "
        "AND latency_ms IS NOT NULL GROUP BY 1",
        window,
    ).fetchall():
        sketch.add(value, int(weight))

    existing = con.execute(
        "SELECT requests, errors_5xx, bytes_sent, latency_samples, latency_sketch "
        "FROM hourly WHERE hour = ?",
        [hour],
    ).fetchone()
    if existing is not None:
        requests += existing[0]
        errors += existing[1]
        bytes_sent += existing[2]
        samples += existing[3]
        if existing[4]:
            sketch.merge(QuantileSketch.from_json(existing[4]))

    counts: dict[tuple[str, str | None], list[int]] = defaultdict(lambda: [0, 0])
    for dim, column in ROLLUP_DIMENSIONS.items():
        for key, n, e in con.execute(
            f"SELECT {column}, count(*), count(*) FILTER (WHERE status >= 500) "
            "FROM requests WHERE ts >= ? AND ts < ? GROUP BY 1",
            window,
        ).fetchall():
            counts[(dim, key)][0] += int(n)
            counts[(dim, key)][1] += int(e)
    for dim, key, n, e in con.execute(
        "SELECT dim, key, requests, errors_5xx FROM hourly_counts WHERE hour = ?", [hour]
    ).fetchall():
        counts[(dim, key)][0] += int(n)
        counts[(dim, key)][1] += int(e)

    con.execute("DELETE FROM hourly WHERE hour = ?", [hour])
    con.execute(
        "INSERT INTO hourly VALUES (?, ?, ?, ?, ?, ?)",
        [hour, requests, errors, bytes_sent, samples, sketch.to_json() if sketch.count else None],
    )
    con.execute("DELETE FROM hourly_counts WHERE hour = ?", [hour])
    con.executemany(
        "INSERT INTO hourly_counts VALUES (?, ?, ?, ?, ?)",
        [(hour, dim, key, n, e) for (dim, key), (n, e) in counts.items()],
    )
    return raw_rows
