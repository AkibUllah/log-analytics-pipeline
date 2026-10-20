"""Report model plus text and HTML (inline SVG) renderers."""

from __future__ import annotations

import html
from dataclasses import asdict, dataclass, field
from datetime import datetime

from logpipe.anomaly import Alert, detect_series
from logpipe.storage import Percentiles, Store


@dataclass
class Report:
    generated_at: datetime
    since: datetime | None
    until: datetime | None
    first_ts: datetime | None
    last_ts: datetime | None
    total_requests: int
    error_rate: float
    bytes_sent: int
    percentiles: Percentiles
    per_minute: list[tuple[datetime, int, int]]
    status_breakdown: list[tuple[str, int]]
    top_endpoints: list[tuple[str, int, float]]
    slowest_endpoints: list[tuple[str, float, int]]
    top_ips: list[tuple[str, int]]
    devices: list[tuple[str, int]]
    alerts: list[Alert] = field(default_factory=list)

    @property
    def duration_minutes(self) -> int:
        return len(self.per_minute)

    @property
    def avg_rpm(self) -> float:
        return self.total_requests / self.duration_minutes if self.duration_minutes else 0.0

    @property
    def peak_rpm(self) -> tuple[datetime, int] | None:
        if not self.per_minute:
            return None
        minute, count, _ = max(self.per_minute, key=lambda r: r[1])
        return minute, count

    def to_dict(self) -> dict:
        data = asdict(self)
        data["avg_rpm"] = round(self.avg_rpm, 2)
        data["peak_rpm"] = self.peak_rpm
        data["alerts"] = [asdict(a) for a in self.alerts]
        return data


def build_report(
    store: Store,
    since: datetime | None = None,
    until: datetime | None = None,
    top: int = 10,
    anomaly_window: int = 30,
    anomaly_threshold: float = 3.0,
    anomaly_min_points: int = 10,
) -> Report:
    per_minute = store.requests_per_minute(since, until)
    alerts = detect_series(
        [(m, float(c)) for m, c, _ in per_minute],
        "request_rate",
        window=anomaly_window,
        threshold=anomaly_threshold,
        min_points=anomaly_min_points,
        std_floor=5.0,
    ) + detect_series(
        [(m, (e / c) if c else 0.0) for m, c, e in per_minute],
        "error_rate",
        window=anomaly_window,
        threshold=anomaly_threshold,
        min_points=anomaly_min_points,
        std_floor=0.05,
    )
    alerts.sort(key=lambda a: a.ts)
    rng = store.time_range()
    return Report(
        generated_at=datetime.now().astimezone(),
        since=since,
        until=until,
        first_ts=rng[0] if rng else None,
        last_ts=rng[1] if rng else None,
        total_requests=store.total_requests(since, until),
        error_rate=store.error_rate(since, until),
        bytes_sent=store.bytes_sent(since, until),
        percentiles=store.latency_percentiles(since, until),
        per_minute=per_minute,
        status_breakdown=store.status_breakdown(since, until),
        top_endpoints=store.top_endpoints(top, since, until),
        slowest_endpoints=store.slowest_endpoints(top, 20, since, until),
        top_ips=store.top_ips(top, since, until),
        devices=store.device_breakdown(since, until),
        alerts=alerts,
    )


# -- text ---------------------------------------------------------------------


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f} ms"


def _table(headers: list[str], rows: list[list[str]], align_right: set[int]) -> list[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            parts.append(cell.rjust(widths[i]) if i in align_right else cell.ljust(widths[i]))
        return "  ".join(parts).rstrip()

    return [fmt(headers), "  ".join("-" * w for w in widths)] + [fmt(r) for r in rows]


def render_text(report: Report) -> str:
    lines: list[str] = []
    window = "all data"
    if report.since or report.until:
        lo = report.since.strftime("%Y-%m-%d %H:%M") if report.since else "start"
        hi = report.until.strftime("%Y-%m-%d %H:%M") if report.until else "latest"
        window = f"{lo} -> {hi} UTC"
    lines.append(f"logpipe report  ({window})")
    lines.append("=" * 72)
    if report.first_ts and report.last_ts:
        lines.append(
            f"Data range      {report.first_ts:%Y-%m-%d %H:%M} -> {report.last_ts:%Y-%m-%d %H:%M} UTC"
        )
    peak = report.peak_rpm
    lines.append(
        f"Requests        {report.total_requests:,}  ({report.avg_rpm:,.1f}/min avg"
        + (f", peak {peak[1]:,}/min at {peak[0]:%H:%M}" if peak else "")
        + ")"
    )
    lines.append(f"5xx error rate  {report.error_rate:.2%}")
    lines.append(f"Bytes sent      {_fmt_bytes(report.bytes_sent)}")
    p = report.percentiles
    lines.append(
        f"Latency         p50 {_ms(p.p50)}   p95 {_ms(p.p95)}   p99 {_ms(p.p99)}"
        f"   ({p.samples:,} samples)"
    )
    lines.append("")
    lines.append("Status classes")
    total = report.total_requests or 1
    lines += [
        "  " + line
        for line in _table(
            ["class", "requests", "share"],
            [[k, f"{v:,}", f"{v / total:.1%}"] for k, v in report.status_breakdown],
            {1, 2},
        )
    ]
    lines.append("")
    lines.append("Top endpoints")
    lines += [
        "  " + line
        for line in _table(
            ["path", "requests", "5xx"],
            [[path, f"{c:,}", f"{e:.1%}"] for path, c, e in report.top_endpoints],
            {1, 2},
        )
    ]
    lines.append("")
    lines.append("Slowest endpoints (p95, >= 20 requests)")
    lines += [
        "  " + line
        for line in _table(
            ["path", "p95", "requests"],
            [[path, _ms(p95), f"{c:,}"] for path, p95, c in report.slowest_endpoints],
            {1, 2},
        )
    ]
    lines.append("")
    lines.append("Top client IPs")
    lines += [
        "  " + line
        for line in _table(["ip", "requests"], [[ip, f"{c:,}"] for ip, c in report.top_ips], {1})
    ]
    lines.append("")
    lines.append("Devices")
    lines += [
        "  " + line
        for line in _table(["device", "requests"], [[d, f"{c:,}"] for d, c in report.devices], {1})
    ]
    lines.append("")
    lines.append(f"Anomalies ({len(report.alerts)})")
    if report.alerts:
        lines += ["  " + a.describe() for a in report.alerts]
    else:
        lines.append("  none detected")
    return "\n".join(lines)


# -- HTML / SVG -----------------------------------------------------------------


def _svg_line_chart(
    points: list[tuple[datetime, int, int]], width: int = 900, height: int = 220
) -> str:
    if not points:
        return "<p>No data</p>"
    pad_l, pad_r, pad_t, pad_b = 48, 16, 12, 28
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    max_y = max(max(c for _, c, _ in points), 1)
    n = len(points)

    def x(i: int) -> float:
        return pad_l + (plot_w * i / max(n - 1, 1))

    def y(v: float) -> float:
        return pad_t + plot_h - (plot_h * v / max_y)

    req_path = " ".join(
        f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(c):.1f}" for i, (_, c, _) in enumerate(points)
    )
    err_path = " ".join(
        f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(e):.1f}" for i, (_, _, e) in enumerate(points)
    )
    area = f"{req_path} L{x(n - 1):.1f},{y(0):.1f} L{x(0):.1f},{y(0):.1f} Z"
    gridlines = []
    for frac in (0, 0.25, 0.5, 0.75, 1):
        gy = y(max_y * frac)
        gridlines.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" class="grid"/>'
            f'<text x="{pad_l - 6}" y="{gy + 4:.1f}" class="lbl" text-anchor="end">{int(max_y * frac):,}</text>'
        )
    ticks = []
    step = max(n // 6, 1)
    for i in range(0, n, step):
        ticks.append(
            f'<text x="{x(i):.1f}" y="{height - 8}" class="lbl" text-anchor="middle">{points[i][0]:%H:%M}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Requests per minute">'
        + "".join(gridlines)
        + f'<path d="{area}" class="area"/>'
        + f'<path d="{req_path}" class="line-req"/>'
        + f'<path d="{err_path}" class="line-err"/>'
        + "".join(ticks)
        + "</svg>"
    )


def _svg_bars(
    rows: list[tuple[str, float]], fmt: str = "{:,.0f}", width: int = 900, bar_h: int = 22
) -> str:
    if not rows:
        return "<p>No data</p>"
    label_w = 320
    max_v = max(v for _, v in rows) or 1
    height = bar_h * len(rows) + 8
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img">']
    for i, (label, value) in enumerate(rows):
        yy = 4 + i * bar_h
        w = (width - label_w - 90) * value / max_v
        parts.append(
            f'<text x="{label_w - 8}" y="{yy + 15}" class="lbl" text-anchor="end">{html.escape(label[:48])}</text>'
            f'<rect x="{label_w}" y="{yy + 3}" width="{w:.1f}" height="{bar_h - 8}" class="bar"/>'
            f'<text x="{label_w + w + 6:.1f}" y="{yy + 15}" class="lbl">{fmt.format(value)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


_HTML_CSS = """
:root{--bg:#0f172a;--panel:#1e293b;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--err:#f87171;--border:#334155}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}
main{max-width:980px;margin:0 auto;padding:32px 16px}h1{margin:0 0 4px}h2{font-size:1.05rem;margin:0 0 12px;color:var(--muted);font-weight:600}
.muted{color:var(--muted)}.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:20px;margin:16px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.kpi .v{font-size:1.6rem;font-weight:700}.kpi .l{color:var(--muted);font-size:.8rem}
svg{width:100%;height:auto;display:block}.grid{stroke:var(--border);stroke-width:1}.lbl{fill:var(--muted);font-size:11px}
.area{fill:var(--accent);opacity:.12}.line-req{fill:none;stroke:var(--accent);stroke-width:1.6}.line-err{fill:none;stroke:var(--err);stroke-width:1.4}
.bar{fill:var(--accent);opacity:.85}table{width:100%;border-collapse:collapse;font-size:.9rem}td,th{padding:6px 4px;border-bottom:1px solid var(--border);text-align:left}
td.n,th.n{text-align:right}.alert{padding:8px 12px;border-left:3px solid var(--err);margin:6px 0;background:#2a1a1a;border-radius:4px;font-size:.9rem}
.legend span{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
"""


def render_html(report: Report, title: str = "Access log report") -> str:
    p = report.percentiles
    total = report.total_requests or 1
    peak = report.peak_rpm
    window = "all data"
    if report.since or report.until:
        lo = report.since.strftime("%Y-%m-%d %H:%M") if report.since else "start"
        hi = report.until.strftime("%Y-%m-%d %H:%M") if report.until else "latest"
        window = f"{lo} &rarr; {hi} UTC"

    def kpi(value: str, label: str) -> str:
        return f'<div class="kpi"><div class="v">{value}</div><div class="l">{label}</div></div>'

    status_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td class='n'>{v:,}</td><td class='n'>{v / total:.1%}</td></tr>"
        for k, v in report.status_breakdown
    )
    ip_rows = "".join(
        f"<tr><td>{html.escape(ip)}</td><td class='n'>{c:,}</td></tr>" for ip, c in report.top_ips
    )
    device_rows = "".join(
        f"<tr><td>{html.escape(d)}</td><td class='n'>{c:,}</td></tr>" for d, c in report.devices
    )
    alerts_html = (
        "".join(f'<div class="alert">{html.escape(a.describe())}</div>' for a in report.alerts)
        or '<p class="muted">No anomalies detected.</p>'
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{_HTML_CSS}</style></head>
<body><main>
<h1>{html.escape(title)}</h1>
<p class="muted">Window: {window} &middot; generated {report.generated_at:%Y-%m-%d %H:%M %Z}</p>
<section class="card kpis">
{kpi(f"{report.total_requests:,}", "requests")}
{kpi(f"{report.avg_rpm:,.1f}", "avg req/min")}
{kpi(f"{peak[1]:,}" if peak else "-", "peak req/min")}
{kpi(f"{report.error_rate:.2%}", "5xx error rate")}
{kpi(_ms(p.p50), "p50 latency")}
{kpi(_ms(p.p95), "p95 latency")}
{kpi(_ms(p.p99), "p99 latency")}
{kpi(_fmt_bytes(report.bytes_sent), "bytes sent")}
</section>
<section class="card"><h2>Requests per minute <span class="legend"><span style="background:#38bdf8"></span>requests <span style="background:#f87171"></span>5xx</span></h2>
{_svg_line_chart(report.per_minute)}</section>
<section class="card"><h2>Anomalies ({len(report.alerts)})</h2>{alerts_html}</section>
<section class="card"><h2>Top endpoints (requests)</h2>{_svg_bars([(path, float(c)) for path, c, _ in report.top_endpoints])}</section>
<section class="card"><h2>Slowest endpoints (p95 ms, &ge; 20 requests)</h2>{_svg_bars([(path, p95) for path, p95, _ in report.slowest_endpoints], fmt="{:,.0f} ms")}</section>
<section class="card"><h2>Status classes</h2><table><tr><th>class</th><th class="n">requests</th><th class="n">share</th></tr>{status_rows}</table></section>
<section class="card"><h2>Top client IPs</h2><table><tr><th>ip</th><th class="n">requests</th></tr>{ip_rows}</table></section>
<section class="card"><h2>Devices</h2><table><tr><th>device</th><th class="n">requests</th></tr>{device_rows}</table></section>
</main></body></html>
"""
