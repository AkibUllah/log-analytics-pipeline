"""``tail -F``-style follow mode built on asyncio and the checkpoint reader."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from logpipe.aggregate import MinuteAggregator
from logpipe.anomaly import Alert, AnomalyMonitor
from logpipe.checkpoint import Checkpoint
from logpipe.models import LogRecord
from logpipe.pipeline import ingest_file
from logpipe.storage import Store

logger = logging.getLogger(__name__)


class Follower:
    """Poll a log file, ingest new lines, score each completed minute for anomalies.

    Only *completed* minutes (strictly before the newest record's minute) are
    scored, so a partially-filled current minute never looks like a traffic drop.
    """

    def __init__(
        self,
        path: Path,
        store: Store,
        checkpoint: Checkpoint | None,
        monitor: AnomalyMonitor,
        fmt: str = "auto",
        poll_interval: float = 1.0,
        on_alert: Callable[[Alert], None] | None = None,
        on_records: Callable[[list[LogRecord]], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.store = store
        self.checkpoint = checkpoint
        self.monitor = monitor
        self.fmt = fmt
        self.poll_interval = poll_interval
        self.on_alert = on_alert
        self.on_records = on_records
        self.aggregator = MinuteAggregator(retention_minutes=6 * 60)
        self.alerts: list[Alert] = []
        self.records_seen = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _handle_batch(self, records: list[LogRecord]) -> None:
        self.aggregator.extend(records)
        self.records_seen += len(records)
        if self.on_records:
            self.on_records(records)

    def _score_completed_minutes(self) -> list[Alert]:
        series = self.aggregator.series()
        if len(series) < 2:
            return []
        new_alerts: list[Alert] = []
        for bucket in series[:-1]:  # last bucket may still be filling
            for alert in self.monitor.observe_minute(
                bucket.minute, bucket.requests, bucket.error_rate
            ):
                new_alerts.append(alert)
        if new_alerts:
            self.alerts.extend(new_alerts)
            self.store.insert_alerts(new_alerts)
            for alert in new_alerts:
                logger.warning(alert.describe())
                if self.on_alert:
                    self.on_alert(alert)
        return new_alerts

    async def poll_once(self) -> int:
        if not self.path.exists():
            return 0
        result = await asyncio.to_thread(
            ingest_file,
            self.path,
            self.store,
            self.fmt,
            self.checkpoint,
            5000,
            self._handle_batch,
        )
        if result.parsed:
            self._score_completed_minutes()
        return result.parsed

    async def run(self, max_polls: int | None = None) -> None:
        polls = 0
        while not self._stop.is_set():
            await self.poll_once()
            polls += 1
            if max_polls is not None and polls >= max_polls:
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                continue
