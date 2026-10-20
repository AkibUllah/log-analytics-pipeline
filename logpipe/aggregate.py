"""In-memory per-minute aggregation used by follow mode and the alert engine."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from logpipe.models import LogRecord
from logpipe.percentiles import QuantileSketch


def floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


@dataclass
class MinuteBucket:
    minute: datetime
    requests: int = 0
    errors_5xx: int = 0
    errors_4xx: int = 0
    bytes_sent: int = 0
    latency: QuantileSketch = field(default_factory=lambda: QuantileSketch(compression=50))

    @property
    def error_rate(self) -> float:
        return self.errors_5xx / self.requests if self.requests else 0.0

    def p(self, percentile: float) -> float | None:
        return self.latency.percentile(percentile) if len(self.latency) else None


class MinuteAggregator:
    """Rolling per-minute buckets plus global top-N counters.

    ``retention_minutes`` bounds memory in follow mode; batch callers can set it
    to ``None`` to keep everything.
    """

    def __init__(self, retention_minutes: int | None = 24 * 60) -> None:
        self.retention = retention_minutes
        self.buckets: dict[datetime, MinuteBucket] = {}
        self.endpoints: Counter[str] = Counter()
        self.ips: Counter[str] = Counter()
        self.statuses: Counter[int] = Counter()
        self.endpoint_latency: dict[str, QuantileSketch] = defaultdict(
            lambda: QuantileSketch(compression=50)
        )
        self.total = 0

    def add(self, record: LogRecord) -> MinuteBucket:
        minute = floor_minute(record.ts)
        bucket = self.buckets.get(minute)
        if bucket is None:
            bucket = self.buckets[minute] = MinuteBucket(minute=minute)
            self._evict(minute)
        bucket.requests += 1
        bucket.bytes_sent += record.bytes_sent
        if 500 <= record.status < 600:
            bucket.errors_5xx += 1
        elif 400 <= record.status < 500:
            bucket.errors_4xx += 1
        if record.latency_ms is not None:
            bucket.latency.add(record.latency_ms)
            self.endpoint_latency[record.path_normalized].add(record.latency_ms)
        self.endpoints[record.path_normalized] += 1
        self.ips[record.ip] += 1
        self.statuses[record.status] += 1
        self.total += 1
        return bucket

    def extend(self, records: list[LogRecord]) -> None:
        for r in records:
            self.add(r)

    def _evict(self, newest: datetime) -> None:
        if self.retention is None:
            return
        cutoff = newest - timedelta(minutes=self.retention)
        for key in [k for k in self.buckets if k < cutoff]:
            del self.buckets[key]

    def series(self, fill_gaps: bool = True) -> list[MinuteBucket]:
        """Buckets in time order, optionally zero-filled for minutes with no traffic."""
        if not self.buckets:
            return []
        keys = sorted(self.buckets)
        if not fill_gaps:
            return [self.buckets[k] for k in keys]
        out: list[MinuteBucket] = []
        cursor = keys[0]
        while cursor <= keys[-1]:
            out.append(self.buckets.get(cursor) or MinuteBucket(minute=cursor))
            cursor += timedelta(minutes=1)
        return out

    def requests_per_minute(self) -> list[tuple[datetime, int]]:
        return [(b.minute, b.requests) for b in self.series()]

    def error_rate_series(self) -> list[tuple[datetime, float]]:
        return [(b.minute, b.error_rate) for b in self.series()]

    def top_endpoints(self, n: int = 10) -> list[tuple[str, int]]:
        return self.endpoints.most_common(n)

    def top_ips(self, n: int = 10) -> list[tuple[str, int]]:
        return self.ips.most_common(n)

    def slowest_endpoints(self, n: int = 10, percentile: float = 95, min_requests: int = 20):
        rows = [
            (path, sketch.percentile(percentile), len(sketch))
            for path, sketch in self.endpoint_latency.items()
            if len(sketch) >= min_requests
        ]
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows[:n]
