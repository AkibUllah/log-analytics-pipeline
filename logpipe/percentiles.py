"""Latency percentiles: exact on bounded windows, approximate (t-digest style) for streams.

``QuantileSketch`` keeps a bounded number of weighted centroids. Centroids near
the tails stay small, so p95/p99 remain accurate while the structure stays
O(max_centroids) in memory regardless of how many samples it has seen.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


def exact_percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolated percentile (``p`` in 0..100) over an unsorted sequence."""
    if not values:
        raise ValueError("cannot compute a percentile of an empty sequence")
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    frac = rank - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def exact_percentiles(values: Sequence[float], ps: Iterable[float]) -> dict[float, float]:
    return {p: exact_percentile(values, p) for p in ps}


class QuantileSketch:
    """Mergeable approximate quantile sketch (simplified t-digest, k0 size bound)."""

    def __init__(self, compression: float = 100.0) -> None:
        if compression < 10:
            raise ValueError("compression must be >= 10")
        self.compression = compression
        self._centroids: list[tuple[float, int]] = []  # (mean, weight), unsorted buffer
        self._unmerged = 0
        self.count = 0
        self.min = math.inf
        self.max = -math.inf

    # -- building ---------------------------------------------------------

    def add(self, value: float, weight: int = 1) -> None:
        if weight <= 0:
            raise ValueError("weight must be positive")
        self._centroids.append((float(value), weight))
        self._unmerged += 1
        self.count += weight
        self.min = min(self.min, value)
        self.max = max(self.max, value)
        if self._unmerged > self.compression * 4:
            self._compress()

    def extend(self, values: Iterable[float]) -> None:
        for v in values:
            self.add(v)

    def merge(self, other: QuantileSketch) -> QuantileSketch:
        for mean, weight in other._centroids:
            self.add(mean, weight)
        return self

    def _compress(self) -> None:
        if not self._centroids:
            return
        self._centroids.sort()
        merged: list[tuple[float, int]] = []
        total = self.count
        cur_mean, cur_w = self._centroids[0]
        seen = 0  # weight before current centroid
        for mean, w in self._centroids[1:]:
            q = (seen + cur_w + w / 2) / total
            # k0 scale function: allowed weight shrinks toward both tails.
            limit = max(1.0, 4 * total * q * (1 - q) / self.compression)
            if cur_w + w <= limit:
                cur_mean = (cur_mean * cur_w + mean * w) / (cur_w + w)
                cur_w += w
            else:
                merged.append((cur_mean, cur_w))
                seen += cur_w
                cur_mean, cur_w = mean, w
        merged.append((cur_mean, cur_w))
        self._centroids = merged
        self._unmerged = 0

    # -- querying ---------------------------------------------------------

    def quantile(self, q: float) -> float:
        """Estimate the value at quantile ``q`` in 0..1."""
        if not 0 <= q <= 1:
            raise ValueError("q must be between 0 and 1")
        if self.count == 0:
            raise ValueError("sketch is empty")
        self._compress()
        cents = self._centroids
        if len(cents) == 1:
            return cents[0][0]
        target = q * self.count
        cumulative = 0.0
        for i, (mean, w) in enumerate(cents):
            if cumulative + w >= target:
                # Interpolate within this centroid towards its neighbours.
                left = self.min if i == 0 else (cents[i - 1][0] + mean) / 2
                right = self.max if i == len(cents) - 1 else (mean + cents[i + 1][0]) / 2
                frac = (target - cumulative) / w if w else 0.0
                return left + (right - left) * frac
            cumulative += w
        return self.max

    def percentile(self, p: float) -> float:
        return self.quantile(p / 100)

    def __len__(self) -> int:
        return self.count
