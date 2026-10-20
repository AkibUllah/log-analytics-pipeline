"""DuckDB storage for enriched requests, analytical queries and Parquet export."""

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from logpipe.models import LogRecord

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


class Store:
    """Thin wrapper over a DuckDB connection. Use ``":memory:"`` for tests."""

    def __init__(self, path: str | Path = ":memory:", read_only: bool = False) -> None:
        self.path = str(path)
        self.con = duckdb.connect(self.path, read_only=read_only)
        if not read_only:
            self.con.execute(_SCHEMA)

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
        row = self.con.execute("SELECT min(ts), max(ts) FROM requests").fetchone()
        if row is None or row[0] is None:
            return None
        return row[0].replace(tzinfo=timezone.utc), row[1].replace(tzinfo=timezone.utc)

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

    def total_requests(self, since=None, until=None) -> int:
        where, params = self._window(since, until)
        return int(self.con.execute(f"SELECT count(*) FROM requests{where}", params).fetchone()[0])

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

    def latency_percentiles(self, since=None, until=None) -> Percentiles:
        where, params = self._window(since, until)
        extra = " AND latency_ms IS NOT NULL" if where else " WHERE latency_ms IS NOT NULL"
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
        where, params = self._window(since, until)
        total, errors = self.con.execute(
            f"SELECT count(*), count(*) FILTER (WHERE status >= 500) FROM requests{where}", params
        ).fetchone()
        return errors / total if total else 0.0

    def status_breakdown(self, since=None, until=None) -> list[tuple[str, int]]:
        where, params = self._window(since, until)
        return [
            (str(k), int(v))
            for k, v in self.con.execute(
                f"SELECT status_class, count(*) FROM requests{where} GROUP BY 1 ORDER BY 1", params
            ).fetchall()
        ]

    def top_endpoints(self, n: int = 10, since=None, until=None) -> list[tuple[str, int, float]]:
        """(normalised path, requests, 5xx rate)."""
        where, params = self._window(since, until)
        rows = self.con.execute(
            f"""
            SELECT path_normalized, count(*) AS c,
                   count(*) FILTER (WHERE status >= 500)::DOUBLE / count(*) AS err
            FROM requests{where} GROUP BY 1 ORDER BY c DESC, 1 LIMIT ?
            """,
            params + [n],
        ).fetchall()
        return [(p, int(c), float(e)) for p, c, e in rows]

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
        where, params = self._window(since, until)
        rows = self.con.execute(
            f"SELECT device, count(*) AS c FROM requests{where} GROUP BY 1 ORDER BY c DESC", params
        ).fetchall()
        return [(d, int(c)) for d, c in rows]

    def bytes_sent(self, since=None, until=None) -> int:
        where, params = self._window(since, until)
        return int(
            self.con.execute(
                f"SELECT coalesce(sum(bytes_sent), 0) FROM requests{where}", params
            ).fetchone()[0]
        )

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
