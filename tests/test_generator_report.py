from datetime import datetime, timedelta, timezone

from logpipe.enrich import enrich
from logpipe.generator import generate_lines
from logpipe.parsers import parse_json_line, parse_nginx_combined
from logpipe.report import build_report, render_html, render_text
from logpipe.storage import Store
from logpipe.timeutil import parse_duration, resolve_window
from tests.conftest import END


def test_generator_is_deterministic_ordered_and_parsable(sample_lines):
    again = list(generate_lines(3000, seed=7, end=END, duration_minutes=180))
    assert again == sample_lines
    assert len(sample_lines) == 3000
    records = [parse_nginx_combined(line) for line in sample_lines]
    assert all(records)
    stamps = [r.ts for r in records]
    assert stamps == sorted(stamps)
    assert stamps[-1] < END and stamps[0] >= END - timedelta(minutes=180)


def test_generator_json_format_and_incident():
    lines = list(generate_lines(2000, fmt="json", seed=3, end=END, duration_minutes=180))
    records = [enrich(parse_json_line(line)) for line in lines]
    assert all(records)
    errors = [r for r in records if r.status >= 500]
    assert len(errors) / len(records) > 0.02  # incident pushes 5xx well above the 0.4% base rate
    quiet = [
        enrich(parse_json_line(line))
        for line in generate_lines(
            2000, fmt="json", seed=3, end=END, duration_minutes=180, incident=False
        )
    ]
    assert sum(r.status >= 500 for r in quiet) / len(quiet) < 0.02


def test_report_detects_injected_incident_and_renders(store):
    report = build_report(store, top=5, anomaly_window=30, anomaly_min_points=10)
    assert report.total_requests == 3000
    assert report.duration_minutes == 180
    assert report.peak_rpm is not None
    metrics = {a.metric for a in report.alerts}
    assert "error_rate" in metrics and "request_rate" in metrics
    text = render_text(report)
    for heading in (
        "Status classes",
        "Top endpoints",
        "Slowest endpoints",
        "Top client IPs",
        "Anomalies",
    ):
        assert heading in text
    assert "5xx error rate" in text
    html = render_html(report, title="Test <report>")
    assert html.count("<svg") == 3
    assert "Test &lt;report&gt;" in html
    assert "/api/v1/products/{id}" in html


def test_report_window_and_dict(store):
    first, last = store.time_range()
    report = build_report(
        store, since=last - timedelta(minutes=30), until=last - timedelta(minutes=10)
    )
    assert 20 <= report.duration_minutes <= 21
    data = report.to_dict()
    assert data["total_requests"] == report.total_requests
    assert isinstance(data["alerts"], list)
    empty = build_report(Store(":memory:"))
    assert empty.total_requests == 0 and empty.peak_rpm is None and empty.avg_rpm == 0.0
    assert "none detected" in render_text(empty)
    assert "No data" in render_html(empty)


def test_duration_parsing_and_window_resolution():
    assert parse_duration("90s") == timedelta(seconds=90)
    assert parse_duration("15m") == timedelta(minutes=15)
    assert parse_duration("1.5h") == timedelta(hours=1, minutes=30)
    assert parse_duration("2d") == timedelta(days=2)
    assert parse_duration("1w") == timedelta(weeks=1)
    for bad in ("", "1x", "h", "ten minutes"):
        try:
            parse_duration(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    anchor = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    since, until = resolve_window("1h", "10m", anchor)
    assert until == anchor - timedelta(minutes=10)
    assert since == until - timedelta(hours=1)
    since, until = resolve_window("2026-10-20T06:00:00Z", None, anchor)
    assert since == datetime(2026, 10, 20, 6, tzinfo=timezone.utc) and until is None
    assert resolve_window(None, None, anchor) == (None, None)
