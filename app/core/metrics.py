"""Prometheus metrics for the gateway, in one dedicated registry."""

from collections.abc import Mapping

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
)

from app.resilience.breaker import BreakerState

CONTENT_TYPE = CONTENT_TYPE_LATEST

REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

HTTP_REQUESTS = Counter(
    "gateway_http_requests_total",
    "HTTP requests handled, by route template and status.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "gateway_http_request_duration_seconds",
    "End-to-end request latency in seconds.",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
CACHE_REQUESTS = Counter(
    "gateway_cache_requests_total",
    "Chat requests by cache outcome (HIT, MISS, COALESCED, BYPASS).",
    ["result"],
    registry=REGISTRY,
)
PROVIDER_ATTEMPTS = Counter(
    "gateway_provider_attempts_total",
    "Provider call attempts by outcome (success or the error class).",
    ["provider", "outcome"],
    registry=REGISTRY,
)
PROVIDER_DURATION = Histogram(
    "gateway_provider_attempt_duration_seconds",
    "Duration of attempts that actually reached the provider.",
    ["provider"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
FALLBACKS = Counter(
    "gateway_fallbacks_total",
    "Times a provider was given up on and the next one was tried.",
    ["provider"],
    registry=REGISTRY,
)
BREAKER_STATE = Gauge(
    "gateway_circuit_breaker_state",
    "Circuit breaker state per provider: 0 closed, 1 half-open, 2 open.",
    ["provider"],
    registry=REGISTRY,
)

_STATE_VALUES = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}


def state_value(state: BreakerState) -> int:
    return _STATE_VALUES.get(state.name.upper(), -1)


def observe_request(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_DURATION.labels(method, route).observe(seconds)


def update_breaker_states(states: Mapping[str, BreakerState]) -> None:
    for provider, state in states.items():
        BREAKER_STATE.labels(provider).set(state_value(state))


def render() -> bytes:
    return generate_latest(REGISTRY)
