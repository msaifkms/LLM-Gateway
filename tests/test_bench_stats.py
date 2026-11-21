"""The benchmark suite's statistics helpers (bench/stats.py)."""

from collections import Counter

import pytest

from bench.stats import (
    counter_total,
    latency_summary,
    median_of_runs,
    percentile,
    provider_calls,
    spread,
    zipf_sample,
)


def test_percentile_interpolates_between_ranks() -> None:
    values = [10.0, 20.0, 30.0, 40.0]

    assert percentile(values, 0) == 10
    assert percentile(values, 50) == 25
    assert percentile(values, 100) == 40
    assert percentile([7.0], 99) == 7
    assert percentile(list(range(1, 101)), 99) == pytest.approx(99.01)


@pytest.mark.parametrize(("values", "q"), [([], 50), ([1.0], -1), ([1.0], 101)])
def test_percentile_rejects_nonsense(values: list[float], q: float) -> None:
    with pytest.raises(ValueError):
        percentile(values, q)


def test_latency_summary_is_in_milliseconds() -> None:
    summary = latency_summary([0.001, 0.002, 0.003, 0.004])

    assert summary["n"] == 4
    assert summary["p50_ms"] == 2.5
    assert summary["max_ms"] == 4.0
    assert summary["mean_ms"] == 2.5


def test_median_and_spread_across_runs() -> None:
    runs = [{"p50_ms": 3.0}, {"p50_ms": 1.0}, {"p50_ms": 2.0}]

    assert median_of_runs(runs, "p50_ms") == 2.0
    assert spread(runs, "p50_ms") == (1.0, 3.0)


def test_zipf_is_skewed_and_reproducible() -> None:
    sample = zipf_sample(200, 5000, seed=1)
    counts = Counter(sample)

    assert sample == zipf_sample(200, 5000, seed=1)
    assert counts[0] > counts[10] > counts.get(150, 0)
    assert all(0 <= i < 200 for i in sample)
    with pytest.raises(ValueError):
        zipf_sample(0, 1)


METRICS = """\
# HELP gateway_provider_attempts_total Provider attempts.
# TYPE gateway_provider_attempts_total counter
gateway_provider_attempts_total{outcome="success",provider="mock-1"} 7.0
gateway_provider_attempts_total{outcome="ProviderServerError",provider="mock-down"} 5.0
gateway_provider_attempts_total{outcome="CircuitOpenError",provider="mock-down"} 95.0
gateway_provider_attempts_created{outcome="success",provider="mock-1"} 1.7e9
"""


def test_counters_are_summed_by_label() -> None:
    assert counter_total(METRICS, "gateway_provider_attempts") == 107
    assert (
        counter_total(METRICS, "gateway_provider_attempts", {"provider": "mock-down"})
        == 100
    )
    assert counter_total(METRICS, "missing") == 0


def test_provider_calls_exclude_circuit_open_rejections() -> None:
    assert provider_calls(METRICS, "mock-down") == 5
    assert provider_calls(METRICS, "mock-1") == 7
