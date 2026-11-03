"""DuckDB storage for enriched requests, analytical queries and Parquet export."""

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from logpipe.models import LogRecord
from logpipe.percentiles import QuantileSketch

COLUMNS = (
    "ts",
    "ip",
    "method",
    "path",
    "path_normalized",
    "query",
    "status",
    "status_class",
    "bytes_sent",
    "protocol",
    "referrer",
    "user_agent",
    "device",
    "browser",
    "is_bot",
    "latency_ms",
    "host",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    ts              TIMESTAMP NOT NULL,
    ip              VARCHAR,
    method          VARCHAR,
    path            VARCHAR,
    path_normalized VARCHAR,
    query           VARCHAR,
    status          SMALLINT,
    status_class    VARCHAR,
    bytes_sent      BIGINT,
    protocol        VARCHAR,
    referrer        VARCHAR,
    user_agent      VARCHAR,
    device          VARCHAR,
    browser         VARCHAR,
    is_bot          BOOLEAN,
    latency_ms      DOUBLE,
    host            VARCHAR
);
CREATE TABLE IF NOT EXISTS alerts (
    metric    VARCHAR,
    ts        TIMESTAMP,
    value     DOUBLE,
    mean      DOUBLE,
    std       DOUBLE,
    z         DOUBLE,
    severity  VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp
);
-- Hourly rollups written by ``logpipe compact`` (see ``logpipe/rollup.py``). Raw rows
-- older than the retention boundary are folded in here and then deleted, so every
-- request is counted in exactly one of ``requests`` or ``hourly``.
CREATE TABLE IF NOT EXISTS hourly (
    hour            TIMESTAMP PRIMARY KEY,
    requests        BIGINT NOT NULL,
    errors_5xx      BIGINT NOT NULL,
    bytes_sent      BIGINT NOT NULL,
    latency_samples BIGINT NOT NULL,
    latency_sketch  VARCHAR
);
CREATE TABLE IF NOT EXISTS hourly_counts (
    hour       TIMESTAMP NOT NULL,
    dim        VARCHAR NOT NULL,
    key        VARCHAR,
    requests   BIGINT NOT NULL,
    errors_5xx BIGINT NOT NULL
);
"""


_CSV_COLUMNS = (
    "{'ts': 'TIMESTAMP', 'ip': 'VARCHAR', 'method': 'VARCHAR', 'path': 'VARCHAR', "
    "'path_normalized': 'VARCHAR', 'query': 'VARCHAR', 'status': 'SMALLINT', "
    "'status_class': 'VARCHAR', 'bytes_sent': 'BIGINT', 'protocol': 'VARCHAR', "
    "'referrer': 'VARCHAR', 'user_agent': 'VARCHAR', 'device': 'VARCHAR', 'browser': 'VARCHAR', "
    "'is_bot': 'BOOLEAN', 'latency_ms': 'DOUBLE', 'host': 'VARCHAR'}"
)


def _naive_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Percentiles:
    p50: float | None
    p95: float | None
    p99: float | None
    samples: int
    # True when the window reaches into hourly rollups and the values come from merged
    # sketches rather than ``quantile_cont`` over raw rows.
    approximate: bool = False


class Store:
    """Thin wrapper over a DuckDB connection. Use ``":memory:"`` for tests.

    Read queries stitch raw ``requests`` rows together with the ``hourly`` rollups that
    ``logpipe compact`` leaves behind, so totals, breakdowns and percentiles cover the
    whole window whether or not part of it has been compacted. Rollups have hour
    granularity: a window that starts or ends inside a compacted hour includes that
    whole hour. Per-minute series, top IPs, slowest endpoints and Parquet export need
    individual requests and therefore only see raw rows.
    """

    def __init__(self, path: str | Path = ":memory:", read_only: bool = False) -> None:
        self.path = str(path)
        self.con = duckdb.connect(self.path, read_only=read_only)
        if not read_only:
            self.con.execute(_SCHEMA)
        # A database written before rollups existed has no hourly tables, and a
        # read-only connection cannot create them.
        self.has_rollups = (
            self.con.execute(
                "SELECT count(*) FROM duckdb_tables() "
                "WHERE table_name IN ('hourly', 'hourly_counts')"
            ).fetchone()[0]
            == 2
        )

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.close()

    # -- writes -----------------------------------------------------------

    def insert(self, records: list[LogRecord]) -> int:
        """Bulk-load a batch. Rows are staged to a temporary CSV and read with DuckDB's
        vectorised ``read_csv``; this is two orders of magnitude faster than
        ``executemany`` for tens of thousands of rows per batch."""
        if not records:
            return 0
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", newline="", encoding="utf-8", delete=False
        ) as tmp:
            writer = csv.writer(tmp, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
            for r in records:
                row = list(r.as_row())
                row[0] = _naive_utc(row[0]).isoformat(sep=" ", timespec="microseconds")
                writer.writerow(row)
            tmp_path = tmp.name
        try:
            self.con.execute(
                f"""
                INSERT INTO requests ({",".join(COLUMNS)})
                SELECT * FROM read_csv(?, header=false, delim=',', quote='"', escape='"',
                    nullstr='', columns={_CSV_COLUMNS}, timestampformat='%Y-%m-%d %H:%M:%S.%f')
                """,
                [tmp_path],
            )
        finally:
            os.unlink(tmp_path)
        return len(records)

    def insert_alerts(self, alerts) -> int:  # noqa: ANN001 - list[Alert]
        if not alerts:
            return 0
        self.con.executemany(
            "INSERT INTO alerts (metric, ts, value, mean, std, z, severity) VALUES (?,?,?,?,?,?,?)",
            [(a.metric, _naive_utc(a.ts), a.value, a.mean, a.std, a.z, a.severity) for a in alerts],
        )
        return len(alerts)

    # -- reads ------------------------------------------------------------

    def count(self) -> int:
        return int(self.con.execute("SELECT count(*) FROM requests").fetchone()[0])

    def time_range(self) -> tuple[datetime, datetime] | None:
        """First and last timestamp held, raw or rolled up. A rollup counts as covering
        its whole hour."""
        lo, hi = self.con.execute("SELECT min(ts), max(ts) FROM requests").fetchone()
        if self.has_rollups:
            first_hour, last_hour = self.con.execute(
                "SELECT min(hour), max(hour) FROM hourly"
            ).fetchone()
            if first_hour is not None:
                last_rolled = last_hour + timedelta(hours=1) - timedelta(microseconds=1)
                lo = first_hour if lo is None else min(lo, first_hour)
                hi = last_rolled if hi is None else max(hi, last_rolled)
        if lo is None:
            return None
        return lo.replace(tzinfo=timezone.utc), hi.replace(tzinfo=timezone.utc)

    def _window(self, since: datetime | None, until: datetime | None) -> tuple[str, list]:
        clauses, params = [], []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(_naive_utc(since))
        if until is not None:
            clauses.append("ts < ?")
            params.append(_naive_utc(until))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def _hour_window(self, since: datetime | None, until: datetime | None) -> tuple[str, list]:
        """``AND`` clauses selecting every rollup hour that overlaps ``[since, until)``."""
        clauses, params = [], []
        if since is not None:
            clauses.append(" AND hour > ?")
            params.append(_naive_utc(since) - timedelta(hours=1))
        if until is not None:
            clauses.append(" AND hour < ?")
            params.append(_naive_utc(until))
        return "".join(clauses), params

    def _stitch(
        self, raw_sql: str, raw_params: list, rollup_sql: str, since, until
    ) -> tuple[str, list]:
        """``raw_sql UNION ALL rollup_sql``; ``rollup_sql`` ends in a ``WHERE`` clause that
        the hour window is appended to. Without rollup tables only ``raw_sql`` is used."""
        if not self.has_rollups:
            return raw_sql, raw_params
        hw, hparams = self._hour_window(since, until)
        return f"{raw_sql} UNION ALL {rollup_sql}{hw}", raw_params + hparams

    def rollup_hours(self, since=None, until=None) -> int:
        """Number of compacted hours that overlap the window."""
        if not self.has_rollups:
            return 0
        hw, params = self._hour_window(since, until)
        return int(
            self.con.execute(f"SELECT count(*) FROM hourly WHERE TRUE{hw}", params).fetchone()[0]
        )

    def _totals(self, since, until) -> tuple[int, int, int]:
        """(requests, 5xx, bytes) across raw rows and rollups."""
        where, params = self._window(since, until)
        sql, args = self._stitch(
            "SELECT count(*) AS n, count(*) FILTER (WHERE status >= 500) AS e, "
            f"coalesce(sum(bytes_sent), 0) AS b FROM requests{where}",
            params,
            "SELECT requests, errors_5xx, bytes_sent FROM hourly WHERE TRUE",
            since,
            until,
        )
        n, e, b = self.con.execute(
            f"SELECT coalesce(sum(n), 0), coalesce(sum(e), 0), coalesce(sum(b), 0) FROM ({sql})",
            args,
        ).fetchone()
        return int(n), int(e), int(b)

    def _breakdown(self, dim: str, column: str, since, until) -> list[tuple[str, int, int]]:
        """(key, requests, 5xx) per value of a dimension, raw and rolled up."""
        where, params = self._window(since, until)
        sql, args = self._stitch(
            f"SELECT {column} AS key, count(*) AS n, count(*) FILTER (WHERE status >= 500) AS e "
            f"FROM requests{where} GROUP BY 1",
            params,
            f"SELECT key, requests, errors_5xx FROM hourly_counts WHERE dim = '{dim}'",
            since,
            until,
        )
        rows = self.con.execute(
            f"SELECT key, sum(n), sum(e) FROM ({sql}) GROUP BY 1", args
        ).fetchall()
        return [(k, int(n), int(e)) for k, n, e in rows]

    def total_requests(self, since=None, until=None) -> int:
        return self._totals(since, until)[0]

    def requests_per_minute(self, since=None, until=None) -> list[tuple[datetime, int, int]]:
        """(minute, requests, 5xx) per minute, gaps filled with zero."""
        where, params = self._window(since, until)
        rows = self.con.execute(
            f"""
            WITH bounds AS (
                SELECT date_trunc('minute', min(ts)) AS lo, date_trunc('minute', max(ts)) AS hi
                FROM requests{where}
            ),
            minutes AS (
                SELECT unnest(generate_series(lo, hi, INTERVAL 1 MINUTE)) AS minute FROM bounds
                WHERE lo IS NOT NULL
            ),
            counts AS (
                SELECT date_trunc('minute', ts) AS minute,
                       count(*) AS requests,
                       count(*) FILTER (WHERE status >= 500) AS errors
                FROM requests{where}
                GROUP BY 1
            )
            SELECT m.minute, coalesce(c.requests, 0), coalesce(c.errors, 0)
            FROM minutes m LEFT JOIN counts c USING (minute)
            ORDER BY m.minute
            """,
            params + params,
        ).fetchall()
        return [(m.replace(tzinfo=timezone.utc), int(r), int(e)) for m, r, e in rows]

    def requests_per_hour(self, since=None, until=None) -> list[tuple[datetime, int, int]]:
        """(hour, requests, 5xx) per hour across raw rows and rollups, gaps filled."""
        where, params = self._window(since, until)
        sql, args = self._stitch(
            "SELECT date_trunc('hour', ts) AS hour, count(*) AS n, "
            f"count(*) FILTER (WHERE status >= 500) AS e FROM requests{where} GROUP BY 1",
            params,
            "SELECT hour, requests, errors_5xx FROM hourly WHERE TRUE",
            since,
            until,
        )
        rows = self.con.execute(
            f"""
            WITH agg AS (SELECT hour, sum(n) AS n, sum(e) AS e FROM ({sql}) GROUP BY 1),
            bounds AS (SELECT min(hour) AS lo, max(hour) AS hi FROM agg),
            hours AS (
                SELECT unnest(generate_series(lo, hi, INTERVAL 1 HOUR)) AS hour FROM bounds
                WHERE lo IS NOT NULL
            )
            SELECT h.hour, coalesce(a.n, 0), coalesce(a.e, 0)
            FROM hours h LEFT JOIN agg a USING (hour)
            ORDER BY h.hour
            """,
            args,
        ).fetchall()
        return [(h.replace(tzinfo=timezone.utc), int(n), int(e)) for h, n, e in rows]

    def latency_percentiles(self, since=None, until=None) -> Percentiles:
        where, params = self._window(since, until)
        extra = " AND latency_ms IS NOT NULL" if where else " WHERE latency_ms IS NOT NULL"
        sketches = []
        if self.has_rollups:
            hw, hparams = self._hour_window(since, until)
            sketches = self.con.execute(
                f"SELECT latency_sketch FROM hourly WHERE latency_sketch IS NOT NULL{hw}", hparams
            ).fetchall()
        if sketches:
            # Percentiles do not average, so merge the hourly sketches and fold the raw
            # latencies of the window into the same sketch.
            sketch = QuantileSketch.from_json(sketches[0][0])
            for (text,) in sketches[1:]:
                sketch.merge(QuantileSketch.from_json(text))
            for value, weight in self.con.execute(
                f"SELECT latency_ms, count(*) FROM requests{where}{extra} GROUP BY 1", params
            ).fetchall():
                sketch.add(value, int(weight))
            return Percentiles(
                p50=sketch.quantile(0.50),
                p95=sketch.quantile(0.95),
                p99=sketch.quantile(0.99),
                samples=sketch.count,
                approximate=True,
            )
        row = self.con.execute(
            f"""
            SELECT quantile_cont(latency_ms, 0.50), quantile_cont(latency_ms, 0.95),
                   quantile_cont(latency_ms, 0.99), count(*)
            FROM requests{where}{extra}
            """,
            params,
        ).fetchone()
        return Percentiles(p50=row[0], p95=row[1], p99=row[2], samples=int(row[3]))

    def error_rate(self, since=None, until=None) -> float:
        total, errors, _ = self._totals(since, until)
        return errors / total if total else 0.0

    def status_breakdown(self, since=None, until=None) -> list[tuple[str, int]]:
        rows = self._breakdown("status_class", "status_class", since, until)
        return sorted(((str(k), n) for k, n, _ in rows), key=lambda r: r[0])

    def top_endpoints(self, n: int = 10, since=None, until=None) -> list[tuple[str, int, float]]:
        """(normalised path, requests, 5xx rate)."""
        rows = self._breakdown("path", "path_normalized", since, until)
        rows.sort(key=lambda r: (-r[1], r[0] is None, r[0] or ""))
        return [(p, c, e / c) for p, c, e in rows[:n]]

    def slowest_endpoints(
        self, n: int = 10, min_requests: int = 20, since=None, until=None
    ) -> list[tuple[str, float, int]]:
        """(normalised path, p95 latency ms, requests)."""
        where, params = self._window(since, until)
        extra = " AND latency_ms IS NOT NULL" if where else " WHERE latency_ms IS NOT NULL"
        rows = self.con.execute(
            f"""
            SELECT path_normalized, quantile_cont(latency_ms, 0.95) AS p95, count(*) AS c
            FROM requests{where}{extra}
            GROUP BY 1 HAVING count(*) >= ? ORDER BY p95 DESC LIMIT ?
            """,
            params + [min_requests, n],
        ).fetchall()
        return [(p, float(lat), int(c)) for p, lat, c in rows]

    def top_ips(self, n: int = 10, since=None, until=None) -> list[tuple[str, int]]:
        where, params = self._window(since, until)
        rows = self.con.execute(
            f"SELECT ip, count(*) AS c FROM requests{where} GROUP BY 1 ORDER BY c DESC, 1 LIMIT ?",
            params + [n],
        ).fetchall()
        return [(ip, int(c)) for ip, c in rows]

    def device_breakdown(self, since=None, until=None) -> list[tuple[str, int]]:
        rows = self._breakdown("device", "device", since, until)
        rows.sort(key=lambda r: (-r[1], r[0] is None, r[0] or ""))
        return [(d, c) for d, c, _ in rows]

    def bytes_sent(self, since=None, until=None) -> int:
        return self._totals(since, until)[2]

    def recent_alerts(self, since=None, limit: int = 50):
        where, params = self._window(since, None)
        rows = self.con.execute(
            f"SELECT metric, ts, value, mean, std, z, severity FROM alerts{where} "
            "ORDER BY ts DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [
            {
                "metric": m,
                "ts": ts.replace(tzinfo=timezone.utc).isoformat(),
                "value": v,
                "mean": mean,
                "std": std,
                "z": z,
                "severity": sev,
            }
            for m, ts, v, mean, std, z, sev in rows
        ]

    # -- export -----------------------------------------------------------

    def export_parquet(self, out_path: str | Path, since=None, until=None) -> int:
        where, params = self._window(since, until)
        out = str(Path(out_path))
        self.con.execute(
            f"COPY (SELECT * FROM requests{where} ORDER BY ts) TO '{out}' (FORMAT PARQUET)", params
        )
        return int(self.con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0])
