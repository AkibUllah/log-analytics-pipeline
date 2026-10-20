"""Line parsers: nginx ``combined`` (with optional request_time) and JSON logs.

Both return ``LogRecord`` or ``None`` for lines that cannot be parsed; callers
count rejects rather than failing the whole batch.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone

from logpipe.models import LogRecord

# 203.0.113.9 - - [20/Oct/2026:10:15:32 +0600] "GET /api/v1/orders/42 HTTP/1.1" 200 1532
#   "https://shop.example.com/cart" "Mozilla/5.0 ..." 0.123
# The trailing request_time (seconds) and optional upstream time are nginx extensions
# commonly added to the combined format; both are optional here.
NGINX_COMBINED_RE = re.compile(
    r"^(?P<ip>\S+) (?P<ident>\S+) (?P<user>\S+) \[(?P<time>[^\]]+)\] "
    r'"(?P<request>[^"]*)" (?P<status>\d{3}) (?P<bytes>\d+|-)'
    r'(?: "(?P<referrer>[^"]*)" "(?P<ua>[^"]*)")?'
    r"(?: (?P<rt>\d+\.\d+|-))?"
    r"(?: (?P<urt>[\d.,\s-]+))?\s*$"
)
_REQUEST_RE = re.compile(r"^(?P<method>[A-Z]+) (?P<target>\S+)(?: (?P<proto>HTTP/[\d.]+))?$")
_NGINX_TIME_FMT = "%d/%b/%Y:%H:%M:%S %z"

JSON_TS_KEYS = ("ts", "time", "timestamp", "@timestamp", "time_iso8601")
JSON_LATENCY_KEYS = ("latency_ms", "duration_ms", "response_time_ms")
JSON_LATENCY_SECONDS_KEYS = ("request_time", "latency", "duration")


def _split_target(target: str) -> tuple[str, str]:
    path, _, query = target.partition("?")
    return path or "/", query


def _parse_nginx_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, _NGINX_TIME_FMT).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_nginx_combined(line: str) -> LogRecord | None:
    match = NGINX_COMBINED_RE.match(line.rstrip("\n"))
    if not match:
        return None
    ts = _parse_nginx_time(match["time"])
    if ts is None:
        return None
    req = _REQUEST_RE.match(match["request"])
    if req:
        method, target, proto = req["method"], req["target"], req["proto"] or "HTTP/1.0"
    else:  # malformed request line, e.g. "-" or binary garbage; keep the record
        method, target, proto = "UNKNOWN", "/", "HTTP/1.0"
    path, query = _split_target(target)
    referrer = match["referrer"]
    ua = match["ua"]
    rt = match["rt"]
    return LogRecord(
        ts=ts,
        ip=match["ip"],
        method=method,
        path=path,
        query=query,
        protocol=proto,
        status=int(match["status"]),
        bytes_sent=0 if match["bytes"] in (None, "-") else int(match["bytes"]),
        referrer=None if referrer in (None, "-", "") else referrer,
        user_agent=None if ua in (None, "-", "") else ua,
        latency_ms=None if rt in (None, "-") else round(float(rt) * 1000, 3),
    )


def _parse_json_ts(value) -> datetime | None:  # noqa: ANN001
    if isinstance(value, (int, float)):
        # Heuristic: milliseconds if clearly too large for seconds.
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return _parse_nginx_time(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def parse_json_line(line: str) -> LogRecord | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    ts = next((_parse_json_ts(obj[k]) for k in JSON_TS_KEYS if k in obj), None)
    if ts is None:
        return None

    target = obj.get("path") or obj.get("uri") or obj.get("request_uri") or "/"
    method = obj.get("method") or obj.get("request_method") or "GET"
    if "request" in obj and isinstance(obj["request"], str) and not obj.get("path"):
        req = _REQUEST_RE.match(obj["request"])
        if req:
            method, target = req["method"], req["target"]
    path, query = _split_target(str(target))
    if not query and obj.get("query"):
        query = str(obj["query"]).lstrip("?")

    latency_ms = next((float(obj[k]) for k in JSON_LATENCY_KEYS if obj.get(k) is not None), None)
    if latency_ms is None:
        secs = next((obj[k] for k in JSON_LATENCY_SECONDS_KEYS if obj.get(k) is not None), None)
        if secs is not None:
            try:
                latency_ms = round(float(secs) * 1000, 3)
            except (TypeError, ValueError):
                latency_ms = None

    try:
        status = int(obj.get("status") or obj.get("status_code") or 0)
        bytes_sent = int(
            obj.get("bytes") or obj.get("body_bytes_sent") or obj.get("bytes_sent") or 0
        )
    except (TypeError, ValueError):
        return None

    known = {
        *JSON_TS_KEYS, *JSON_LATENCY_KEYS, *JSON_LATENCY_SECONDS_KEYS,
        "path", "uri", "request_uri", "method", "request_method", "request", "query",
        "status", "status_code", "bytes", "body_bytes_sent", "bytes_sent",
        "ip", "remote_addr", "client_ip", "referrer", "referer", "http_referer",
        "user_agent", "http_user_agent", "ua", "protocol", "server_protocol", "host",
    }  # fmt: skip
    return LogRecord(
        ts=ts,
        ip=str(obj.get("ip") or obj.get("remote_addr") or obj.get("client_ip") or "-"),
        method=str(method).upper(),
        path=path,
        query=query,
        protocol=str(obj.get("protocol") or obj.get("server_protocol") or "HTTP/1.1"),
        status=status,
        bytes_sent=bytes_sent,
        referrer=obj.get("referrer") or obj.get("referer") or obj.get("http_referer") or None,
        user_agent=obj.get("user_agent") or obj.get("http_user_agent") or obj.get("ua") or None,
        latency_ms=latency_ms,
        host=obj.get("host"),
        extra={k: v for k, v in obj.items() if k not in known},
    )


Parser = Callable[[str], "LogRecord | None"]
PARSERS: dict[str, Parser] = {"nginx": parse_nginx_combined, "json": parse_json_line}


def detect_format(sample_lines: Iterable[str]) -> str:
    """Guess the log format from a few lines: ``json`` if they start with ``{``, else ``nginx``."""
    for line in sample_lines:
        stripped = line.strip()
        if not stripped:
            continue
        return "json" if stripped.startswith("{") else "nginx"
    return "nginx"


def parse_lines(lines: Iterable[str], fmt: str = "nginx") -> Iterator[LogRecord | None]:
    parser = PARSERS[fmt]
    for line in lines:
        if not line.strip():
            continue
        yield parser(line)
