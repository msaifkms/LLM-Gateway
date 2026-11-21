"""Small, dependency-free statistics helpers for the benchmark suite."""

import math
import random
import statistics
from collections.abc import Iterable, Sequence

from prometheus_client.parser import text_string_to_metric_families


def percentile(values: Sequence[float], q: float) -> float:
    """The q-th percentile (0-100) with linear interpolation between ranks."""
    if not values:
        raise ValueError("percentile of no values")
    if not 0 <= q <= 100:
        raise ValueError("q must be between 0 and 100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * q / 100
    low = math.floor(rank)
    high = math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def latency_summary(seconds: Sequence[float]) -> dict[str, float]:
    """p50/p95/p99/mean/max in milliseconds, plus the sample count."""
    ms = [s * 1000 for s in seconds]
    return {
        "n": len(ms),
        "p50_ms": round(percentile(ms, 50), 3),
        "p95_ms": round(percentile(ms, 95), 3),
        "p99_ms": round(percentile(ms, 99), 3),
        "mean_ms": round(statistics.fmean(ms), 3),
        "max_ms": round(max(ms), 3),
    }


def median_of_runs(runs: Sequence[dict[str, float]], key: str) -> float:
    """The median of one statistic across repeated runs."""
    return statistics.median(run[key] for run in runs)


def spread(runs: Sequence[dict[str, float]], key: str) -> tuple[float, float]:
    """(min, max) of one statistic across runs, to show run-to-run variation."""
    values = [run[key] for run in runs]
    return min(values), max(values)


def zipf_sample(
    population: int, size: int, *, s: float = 1.1, seed: int = 0
) -> list[int]:
    """Indexes 0..population-1 drawn with Zipf popularity: a few are very common.

    A realistic shape for repeated prompts (the same few questions asked often,
    a long tail asked once). Seeded, so every run sends the same mix.
    """
    if population < 1 or size < 0:
        raise ValueError("population must be >= 1 and size >= 0")
    weights = [1 / (rank**s) for rank in range(1, population + 1)]
    rng = random.Random(seed)
    return rng.choices(range(population), weights=weights, k=size)


def counter_total(
    metrics_text: str, name: str, labels: dict[str, str] | None = None
) -> float:
    """Sum a Prometheus counter's samples whose labels include ``labels``.

    ``name`` is the metric name without the ``_total`` suffix, as the parser
    reports families.
    """
    wanted = labels or {}
    total = 0.0
    for family in text_string_to_metric_families(metrics_text):
        if family.name != name:
            continue
        for sample in family.samples:
            if not sample.name.endswith("_total"):
                continue
            if all(sample.labels.get(k) == v for k, v in wanted.items()):
                total += sample.value
    return total


def provider_calls(metrics_text: str, provider: str) -> float:
    """Attempts that actually reached a provider (circuit-open rejections excluded)."""
    everything = counter_total(
        metrics_text, "gateway_provider_attempts", {"provider": provider}
    )
    rejected = counter_total(
        metrics_text,
        "gateway_provider_attempts",
        {"provider": provider, "outcome": "CircuitOpenError"},
    )
    return everything - rejected


def mean(values: Iterable[float]) -> float:
    return statistics.fmean(list(values))
