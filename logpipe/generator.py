"""Synthetic but realistic access-log generator for demos and tests.

Traffic shape: a daily curve (quiet at night, busy in the evening) with
weekly-seasonality-free noise, a weighted endpoint mix modelled on an
e-commerce + payments API, per-endpoint latency distributions, a bot share,
and an optional incident: a 5xx burst with a latency spike, plus a traffic
flash-crowd. Both are large enough for the z-score detector to flag them.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# (path template, weight, base latency ms, latency sigma, 404 chance)
ENDPOINTS: list[tuple[str, int, float, float, float]] = [
    ("/", 120, 45, 0.4, 0.0),
    ("/api/v1/products", 220, 80, 0.5, 0.0),
    ("/api/v1/products/{id}", 260, 60, 0.5, 0.03),
    ("/api/v1/categories", 90, 35, 0.4, 0.0),
    ("/api/v1/search?q={q}", 110, 140, 0.6, 0.0),
    ("/api/v1/cart", 150, 70, 0.5, 0.0),
    ("/api/v1/cart/items/{id}", 60, 65, 0.5, 0.01),
    ("/api/v1/orders", 70, 180, 0.6, 0.0),
    ("/api/v1/orders/{id}", 80, 90, 0.5, 0.02),
    ("/api/v1/payments/bkash/create", 35, 420, 0.5, 0.0),
    ("/api/v1/payments/bkash/execute", 30, 650, 0.6, 0.0),
    ("/api/v1/payments/sslcommerz/ipn", 15, 120, 0.4, 0.0),
    ("/api/v1/auth/login", 55, 160, 0.4, 0.0),
    ("/api/v1/auth/refresh", 40, 40, 0.3, 0.0),
    ("/api/v1/users/{uuid}", 45, 55, 0.4, 0.01),
    ("/static/app.{hash}.js", 200, 8, 0.3, 0.0),
    ("/static/app.{hash}.css", 180, 6, 0.3, 0.0),
    ("/static/img/{id}.webp", 300, 12, 0.4, 0.02),
    ("/health", 60, 3, 0.2, 0.0),
    ("/wp-login.php", 6, 10, 0.3, 1.0),  # scanner noise
    ("/.env", 4, 8, 0.3, 1.0),
]
_ENDPOINT_WEIGHTS = [e[1] for e in ENDPOINTS]

USER_AGENTS: list[tuple[str, int]] = [
    ("Mozilla/5.0 (Linux; Android 13; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36", 30),
    ("Mozilla/5.0 (Linux; Android 12; Redmi Note 11) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36", 18),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1", 12),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", 22),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0", 5),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15", 4),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", 3),
    ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", 3),
    ("Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)", 1),
    ("curl/8.5.0", 1),
    ("python-requests/2.31.0", 1),
]  # fmt: skip
_UA_WEIGHTS = [u[1] for u in USER_AGENTS]

REFERRERS: list[tuple[str | None, int]] = [
    (None, 55),
    ("https://shop.example.com.bd/", 20),
    ("https://www.facebook.com/", 12),
    ("https://www.google.com/", 8),
    ("https://m.facebook.com/", 3),
    ("https://t.co/", 2),
]
_REF_WEIGHTS = [r[1] for r in REFERRERS]

# Illustrative client prefixes (Bangladeshi mobile operators / ISPs and a few foreign).
IP_PREFIXES: list[tuple[str, int]] = [
    ("103.108.14", 20), ("114.130.5", 15), ("27.147.20", 12), ("202.84.4", 10),
    ("103.86.19", 8), ("45.64.13", 6), ("103.148.7", 6), ("37.111.20", 5),
    ("66.249.66", 2), ("157.55.39", 1), ("185.220.101", 2), ("5.188.62", 1),
]  # fmt: skip
_IP_WEIGHTS = [p[1] for p in IP_PREFIXES]


@dataclass
class Incident:
    start_minute: int
    duration_minutes: int
    error_rate: float
    latency_multiplier: float


def _daily_curve(ts: datetime) -> float:
    """Relative traffic intensity across the day in Asia/Dhaka local time (UTC+6)."""
    local_hour = (ts.hour + 6) % 24 + ts.minute / 60
    # Trough around 04:00, peak around 21:00.
    return 0.35 + 0.65 * (0.5 + 0.5 * math.cos((local_hour - 21) / 24 * 2 * math.pi))


def _fill_path(rng: random.Random, template: str) -> str:
    path = template
    if "{id}" in path:
        path = path.replace("{id}", str(rng.randint(1, 5000)))
    if "{uuid}" in path:
        path = path.replace(
            "{uuid}",
            f"{rng.getrandbits(128):032x}"[:8] + "-1c2d-4e5f-8a9b-" + f"{rng.getrandbits(48):012x}",
        )
    if "{hash}" in path:
        path = path.replace("{hash}", f"{rng.getrandbits(32):08x}")
    if "{q}" in path:
        path = path.replace(
            "{q}", rng.choice(["saree", "panjabi", "eid+offer", "rice+cooker", "mobile", "shoes"])
        )
    return path


def _nginx_line(
    rng: random.Random,
    ts: datetime,
    ip: str,
    method: str,
    target: str,
    status: int,
    size: int,
    referrer: str | None,
    ua: str,
    latency_ms: float,
) -> str:
    stamp = ts.astimezone(timezone(timedelta(hours=6))).strftime("%d/%b/%Y:%H:%M:%S %z")
    ref = referrer or "-"
    return f'{ip} - - [{stamp}] "{method} {target} HTTP/1.1" {status} {size} "{ref}" "{ua}" {latency_ms / 1000:.3f}'


def _json_line(
    ts: datetime,
    ip: str,
    method: str,
    target: str,
    status: int,
    size: int,
    referrer: str | None,
    ua: str,
    latency_ms: float,
) -> str:
    path, _, query = target.partition("?")
    return json.dumps(
        {
            "time": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "remote_addr": ip,
            "method": method,
            "path": path,
            "query": query,
            "status": status,
            "body_bytes_sent": size,
            "http_referer": referrer,
            "http_user_agent": ua,
            "request_time": round(latency_ms / 1000, 3),
            "host": "shop.example.com.bd",
        },
        separators=(",", ":"),
    )


def generate_lines(
    lines: int = 50_000,
    *,
    fmt: str = "nginx",
    seed: int = 42,
    end: datetime | None = None,
    duration_minutes: int = 24 * 60,
    incident: bool = True,
) -> Iterator[str]:
    """Yield ``lines`` log lines in time order covering ``duration_minutes`` ending at ``end``."""
    if fmt not in ("nginx", "json"):
        raise ValueError("fmt must be 'nginx' or 'json'")
    rng = random.Random(seed)
    end = (end or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=duration_minutes)

    # Allocate lines to minutes following the daily curve, then add an incident/flash crowd.
    weights = [_daily_curve(start + timedelta(minutes=m)) for m in range(duration_minutes)]
    incidents: list[Incident] = []
    if incident and duration_minutes >= 120:
        err_start = int(duration_minutes * 0.62)
        incidents.append(Incident(err_start, 12, error_rate=0.35, latency_multiplier=4.0))
        spike_start = int(duration_minutes * 0.80)
        for m in range(spike_start, min(spike_start + 6, duration_minutes)):
            weights[m] *= 3.2  # flash crowd
    total_weight = sum(weights)
    per_minute = [int(lines * w / total_weight) for w in weights]
    remainder = lines - sum(per_minute)
    for i in range(remainder):  # distribute rounding leftovers
        per_minute[i % duration_minutes] += 1

    for minute_index, n in enumerate(per_minute):
        minute_start = start + timedelta(minutes=minute_index)
        active = next(
            (
                i
                for i in incidents
                if i.start_minute <= minute_index < i.start_minute + i.duration_minutes
            ),
            None,
        )
        offsets = sorted(rng.random() * 60 for _ in range(n))
        for offset in offsets:
            ts = minute_start + timedelta(seconds=offset)
            template, _, base_latency, sigma, not_found = rng.choices(ENDPOINTS, _ENDPOINT_WEIGHTS)[
                0
            ]
            target = _fill_path(rng, template)
            method = (
                "POST"
                if any(
                    k in template
                    for k in ("/create", "/execute", "/ipn", "/login", "/orders", "/cart")
                )
                and rng.random() < 0.6
                else "GET"
            )
            ua = rng.choices(USER_AGENTS, _UA_WEIGHTS)[0][0]
            prefix = rng.choices(IP_PREFIXES, _IP_WEIGHTS)[0][0]
            ip = f"{prefix}.{rng.randint(1, 254)}"
            referrer = rng.choices(REFERRERS, _REF_WEIGHTS)[0][0]

            latency = rng.lognormvariate(math.log(base_latency), sigma)
            roll = rng.random()
            if active and roll < active.error_rate:
                status = rng.choice([500, 502, 503, 503, 504])
                latency *= active.latency_multiplier
            elif roll < not_found:
                status = 404
            elif roll < not_found + 0.004:
                status = 500
            elif template.startswith("/api/v1/auth") and roll < 0.12:
                status = 401
            elif template.startswith("/api/v1/orders") and method == "POST" and roll < 0.08:
                status = 422
            elif template.startswith("/static") and roll < 0.30:
                status = 304
            else:
                status = 201 if method == "POST" and "orders" in template else 200
            size = (
                0
                if status == 304
                else int(
                    rng.lognormvariate(math.log(1800 if "/static" not in template else 24000), 0.8)
                )
            )
            latency_ms = round(min(latency, 60_000), 1)

            if fmt == "nginx":
                yield _nginx_line(
                    rng, ts, ip, method, target, status, size, referrer, ua, latency_ms
                )
            else:
                yield _json_line(ts, ip, method, target, status, size, referrer, ua, latency_ms)
