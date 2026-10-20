from datetime import datetime, timedelta, timezone

import pytest

from logpipe.aggregate import MinuteAggregator, floor_minute
from logpipe.anomaly import AnomalyMonitor, ZScoreDetector, detect_series
from logpipe.models import LogRecord

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


def rec(offset_s: int, status: int = 200, path: str = "/a", latency: float = 50.0, ip="1.1.1.1"):
    return LogRecord(
        ts=T0 + timedelta(seconds=offset_s),
        ip=ip,
        method="GET",
        path=path,
        status=status,
        bytes_sent=100,
        latency_ms=latency,
        path_normalized=path,
    )


def test_minute_buckets_gap_fill_and_error_rate():
    agg = MinuteAggregator(retention_minutes=None)
    agg.extend([rec(0), rec(30, status=503), rec(59), rec(150, status=404), rec(240)])
    series = agg.series()
    assert [b.minute for b in series] == [T0 + timedelta(minutes=i) for i in range(5)]
    assert [b.requests for b in series] == [3, 0, 1, 0, 1]
    assert series[0].error_rate == pytest.approx(1 / 3)
    assert series[2].errors_4xx == 1
    assert series[1].p(95) is None
    assert agg.requests_per_minute()[0] == (T0, 3)
    assert agg.total == 5
    assert floor_minute(T0 + timedelta(seconds=59)) == T0


def test_top_counters_and_slowest():
    agg = MinuteAggregator()
    for i in range(30):
        agg.add(rec(i, path="/slow", latency=900 + i, ip=f"10.0.0.{i % 3}"))
    for i in range(10):
        agg.add(rec(i, path="/fast", latency=5))
    assert agg.top_endpoints(1) == [("/slow", 30)]
    assert agg.top_ips(1)[0][1] == 10
    slow = agg.slowest_endpoints(5, min_requests=20)
    assert [p for p, _, _ in slow] == ["/slow"]  # /fast excluded by min_requests
    assert slow[0][1] > 900


def test_retention_evicts_old_minutes():
    agg = MinuteAggregator(retention_minutes=10)
    agg.add(rec(0))
    agg.add(rec(20 * 60))
    assert T0 not in agg.buckets
    assert T0 + timedelta(minutes=20) in agg.buckets


def test_detector_needs_min_points_then_flags_spike():
    det = ZScoreDetector("request_rate", window=10, threshold=3.0, min_points=5, std_floor=1.0)
    points = [(T0 + timedelta(minutes=i), 50.0 + (i % 2)) for i in range(5)]
    assert all(det.observe(ts, v) is None for ts, v in points)
    alert = det.observe(T0 + timedelta(minutes=5), 200.0)
    assert alert is not None
    assert alert.metric == "request_rate" and alert.severity == "critical" and alert.z > 6
    assert "request rate 200/min" in alert.describe()


def test_anomalous_points_do_not_poison_baseline():
    det = ZScoreDetector("request_rate", window=10, threshold=3.0, min_points=3, std_floor=1.0)
    for i in range(5):
        det.observe(T0 + timedelta(minutes=i), 10.0)
    assert det.observe(T0 + timedelta(minutes=5), 100.0) is not None
    # Baseline still ~10, so the second spike minute is also flagged.
    assert det.observe(T0 + timedelta(minutes=6), 100.0) is not None
    assert det.baseline[0] == pytest.approx(10.0)


def test_std_floor_suppresses_noise_on_quiet_series():
    noisy = ZScoreDetector("error_rate", window=10, threshold=3.0, min_points=3, std_floor=0.0)
    quiet = ZScoreDetector("error_rate", window=10, threshold=3.0, min_points=3, std_floor=0.05)
    for i in range(5):
        noisy.observe(T0 + timedelta(minutes=i), 0.0)
        quiet.observe(T0 + timedelta(minutes=i), 0.0)
    # One 5xx in thirty requests on an otherwise clean series.
    assert noisy.observe(T0 + timedelta(minutes=5), 1 / 30) is not None
    assert quiet.observe(T0 + timedelta(minutes=5), 1 / 30) is None
    assert quiet.observe(T0 + timedelta(minutes=6), 0.4) is not None


def test_detector_validation_and_reset():
    with pytest.raises(ValueError):
        ZScoreDetector("request_rate", window=1)
    with pytest.raises(ValueError):
        ZScoreDetector("request_rate", window=5, min_points=6)
    det = ZScoreDetector("request_rate", window=5, min_points=2)
    det.observe(T0, 1.0)
    det.reset()
    assert det.baseline == (0.0, 0.0)


def test_detect_series_batch():
    points = [(T0 + timedelta(minutes=i), 40.0) for i in range(30)]
    points.append((T0 + timedelta(minutes=30), 400.0))
    alerts = detect_series(points, "request_rate", window=30, min_points=10, std_floor=5.0)
    assert len(alerts) == 1 and alerts[0].ts == T0 + timedelta(minutes=30)


def test_monitor_scores_each_minute_once():
    monitor = AnomalyMonitor(window=10, threshold=3.0, min_points=3)
    for i in range(5):
        assert monitor.observe_minute(T0 + timedelta(minutes=i), 50, 0.0) == []
    spike_minute = T0 + timedelta(minutes=5)
    alerts = monitor.observe_minute(spike_minute, 500, 0.5)
    assert {a.metric for a in alerts} == {"request_rate", "error_rate"}
    assert monitor.observe_minute(spike_minute, 500, 0.5) == []  # revisited minute is ignored
