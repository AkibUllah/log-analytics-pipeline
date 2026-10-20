"""Duration parsing shared by the CLI and the API (``--since 1h``, ``90m``, ``2d``)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr|d|w)\s*$", re.I)
_UNITS = {
    "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400, "w": 604800,
}  # fmt: skip


def parse_duration(text: str) -> timedelta:
    match = _DURATION_RE.match(text)
    if not match:
        raise ValueError(f"invalid duration {text!r}; use forms like 30s, 15m, 1h, 2d, 1w")
    value, unit = match.groups()
    return timedelta(seconds=float(value) * _UNITS[unit.lower()])


def parse_when(text: str) -> datetime:
    """Accept an ISO-8601 timestamp or a relative duration ('now' minus duration)."""
    try:
        return datetime.now(timezone.utc) - parse_duration(text)
    except ValueError:
        pass
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_window(
    since: str | None,
    until: str | None,
    anchor: datetime,
) -> tuple[datetime | None, datetime | None]:
    """Turn ``--since/--until`` into absolute UTC bounds relative to ``anchor``."""
    until_dt: datetime | None = None
    if until:
        try:
            until_dt = anchor - parse_duration(until)
        except ValueError:
            until_dt = parse_when(until)
    end = until_dt or anchor
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = end - parse_duration(since)
        except ValueError:
            since_dt = parse_when(since)
    return since_dt, until_dt
