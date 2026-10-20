"""The normalised record every parser produces."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class LogRecord:
    ts: datetime
    ip: str
    method: str
    path: str
    status: int
    bytes_sent: int
    protocol: str = "HTTP/1.1"
    query: str = ""
    referrer: str | None = None
    user_agent: str | None = None
    latency_ms: float | None = None
    host: str | None = None
    # Filled by enrichment
    path_normalized: str = ""
    status_class: str = ""
    device: str = ""
    browser: str = ""
    is_bot: bool = False
    extra: dict = field(default_factory=dict)

    def as_row(self) -> tuple:
        """Column order matches ``storage.COLUMNS``."""
        return (
            self.ts,
            self.ip,
            self.method,
            self.path,
            self.path_normalized,
            self.query,
            self.status,
            self.status_class,
            self.bytes_sent,
            self.protocol,
            self.referrer,
            self.user_agent,
            self.device,
            self.browser,
            self.is_bot,
            self.latency_ms,
            self.host,
        )
