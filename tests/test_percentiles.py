import random

import pytest

from logpipe.percentiles import QuantileSketch, exact_percentile, exact_percentiles


def test_exact_percentile_interpolates():
    data = [10, 20, 30, 40, 50]
    assert exact_percentile(data, 0) == 10
    assert exact_percentile(data, 50) == 30
    assert exact_percentile(data, 100) == 50
    assert exact_percentile(data, 90) == pytest.approx(46.0)
    assert exact_percentile([7], 99) == 7


def test_exact_percentile_validation():
    with pytest.raises(ValueError):
        exact_percentile([], 50)
    with pytest.raises(ValueError):
        exact_percentile([1], 101)
    assert exact_percentiles([1, 2, 3], (50, 100)) == {50: 2.0, 100: 3.0}


def test_sketch_is_close_to_exact_on_lognormal_latencies():
    rng = random.Random(1)
    values = [rng.lognormvariate(4.0, 0.7) for _ in range(50_000)]
    sketch = QuantileSketch(compression=100)
    sketch.extend(values)
    assert len(sketch) == 50_000
    for p in (50, 95, 99):
        exact = exact_percentile(values, p)
        approx = sketch.percentile(p)
        assert abs(approx - exact) / exact < 0.03, (p, exact, approx)
    assert sketch.quantile(0) == pytest.approx(min(values))
    assert sketch.quantile(1) == pytest.approx(max(values))


def test_sketch_bounded_memory_and_merge():
    a, b = QuantileSketch(compression=50), QuantileSketch(compression=50)
    a.extend(range(0, 10_000))
    b.extend(range(10_000, 20_000))
    a._compress()
    assert len(a._centroids) < 400
    merged = a.merge(b)
    assert len(merged) == 20_000
    assert merged.percentile(50) == pytest.approx(10_000, rel=0.02)


def test_sketch_validation():
    s = QuantileSketch()
    with pytest.raises(ValueError):
        s.quantile(0.5)
    with pytest.raises(ValueError):
        s.add(1.0, weight=0)
    with pytest.raises(ValueError):
        QuantileSketch(compression=1)
    s.add(42.0)
    assert s.quantile(0.9) == 42.0
