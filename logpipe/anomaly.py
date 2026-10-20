"""Z-score anomaly detection on per-minute request rate and error rate."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Metric = Literal["request_rate", "error_rate"]


@dataclass(frozen=True)
class Alert:
    metric: Metric
    ts: datetime
    value: float
    mean: float
    std: float
    z: float
    severity: Literal["warning", "critical"]

    def describe(self) -> str:
        if self.metric == "request_rate":
            return (
                f"[{self.severity}] request rate {self.value:.0f}/min at {self.ts:%Y-%m-%d %H:%M} "
                f"(baseline {self.mean:.1f} +/- {self.std:.1f}, z={self.z:.1f})"
            )
        return (
            f"[{self.severity}] 5xx error rate {self.value:.1%} at {self.ts:%Y-%m-%d %H:%M} "
            f"(baseline {self.mean:.2%} +/- {self.std:.2%}, z={self.z:.1f})"
        )


class ZScoreDetector:
    """Flag a point whose z-score against the trailing window exceeds ``threshold``.

    The trailing window excludes the point being scored, and anomalous points are
    not added to the baseline so a sustained incident keeps alerting instead of
    becoming the new normal. ``std_floor`` is a lower bound on the baseline spread:
    on quiet traffic one 5xx in thirty requests is a 3% error rate, which would be
    many standard deviations above a near-zero baseline without it. The defaults are
    5 requests/min for the rate detector and 5 percentage points for error rate.
    """

    def __init__(
        self,
        metric: Metric,
        window: int = 30,
        threshold: float = 3.0,
        min_points: int = 10,
        std_floor: float = 1.0,
        direction: Literal["up", "both"] = "up",
    ) -> None:
        if window < 2:
            raise ValueError("window must be >= 2")
        if min_points < 2 or min_points > window:
            raise ValueError("min_points must be between 2 and window")
        self.metric = metric
        self.threshold = threshold
        self.min_points = min_points
        self.std_floor = std_floor
        self.direction = direction
        self._window: deque[float] = deque(maxlen=window)

    @property
    def baseline(self) -> tuple[float, float]:
        n = len(self._window)
        if n == 0:
            return 0.0, 0.0
        mean = sum(self._window) / n
        var = sum((x - mean) ** 2 for x in self._window) / n
        return mean, math.sqrt(var)

    def observe(self, ts: datetime, value: float) -> Alert | None:
        alert: Alert | None = None
        if len(self._window) >= self.min_points:
            mean, std = self.baseline
            effective_std = max(std, self.std_floor, 1e-9)
            z = (value - mean) / effective_std
            score = z if self.direction == "up" else abs(z)
            if score >= self.threshold:
                severity = "critical" if score >= 2 * self.threshold else "warning"
                alert = Alert(
                    metric=self.metric,
                    ts=ts,
                    value=value,
                    mean=mean,
                    std=std,
                    z=z,
                    severity=severity,
                )
        if alert is None:
            self._window.append(value)
        return alert

    def reset(self) -> None:
        self._window.clear()


def detect_series(
    points: list[tuple[datetime, float]],
    metric: Metric,
    window: int = 30,
    threshold: float = 3.0,
    min_points: int = 10,
    std_floor: float = 1.0,
) -> list[Alert]:
    detector = ZScoreDetector(
        metric, window=window, threshold=threshold, min_points=min_points, std_floor=std_floor
    )
    alerts: list[Alert] = []
    for ts, value in points:
        alert = detector.observe(ts, value)
        if alert:
            alerts.append(alert)
    return alerts


class AnomalyMonitor:
    """Runs request-rate and error-rate detectors over a per-minute series."""

    def __init__(self, window: int = 30, threshold: float = 3.0, min_points: int = 10) -> None:
        self.rate = ZScoreDetector(
            "request_rate", window=window, threshold=threshold, min_points=min_points, std_floor=5.0
        )
        self.errors = ZScoreDetector(
            "error_rate", window=window, threshold=threshold, min_points=min_points, std_floor=0.05
        )
        self._last_minute: datetime | None = None

    def observe_minute(self, minute: datetime, requests: int, error_rate: float) -> list[Alert]:
        alerts = []
        if self._last_minute is not None and minute <= self._last_minute:
            return alerts  # already scored (follow mode re-visits the current minute)
        self._last_minute = minute
        for detector, value in ((self.rate, float(requests)), (self.errors, error_rate)):
            alert = detector.observe(minute, value)
            if alert:
                alerts.append(alert)
        return alerts
