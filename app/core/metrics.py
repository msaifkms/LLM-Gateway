"""Prometheus metrics for the gateway, in one dedicated registry."""

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:  # a runtime import would be circular (evals use usage metrics)
    from app.evals.store import LatestScore

CONTENT_TYPE = CONTENT_TYPE_LATEST

REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

# Traffic, cache, providers and breakers.

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

# Protection, spend and dependency metrics.

RATE_LIMIT_EVENTS = Counter(
    "gateway_rate_limit_events_total",
    "Rate limiter outcomes other than a normal allow: limited, unavailable, fail_open.",
    ["outcome"],
    registry=REGISTRY,
)
QUOTA_REJECTIONS = Counter(
    "gateway_quota_rejections_total",
    "Requests refused by a daily quota (tokens, usd) or because usage was unreadable.",
    ["reason"],
    registry=REGISTRY,
)
KILL_SWITCH_BLOCKS = Counter(
    "gateway_kill_switch_blocks_total",
    "Chat requests refused by the kill switch: disabled, or unavailable (state unknown).",
    ["reason"],
    registry=REGISTRY,
)
KILL_SWITCH_ON = Gauge(
    "gateway_kill_switch_on",
    "1 while the kill switch is on, 0 while off (read at scrape time).",
    registry=REGISTRY,
)
TOKENS = Counter(
    "gateway_tokens_total",
    "Tokens billed by providers, by model and kind (prompt, completion).",
    ["model", "kind"],
    registry=REGISTRY,
)
COST_USD = Counter(
    "gateway_cost_usd_total",
    "Dollars billed by providers, by model.",
    ["model"],
    registry=REGISTRY,
)
SAVED_USD = Counter(
    "gateway_cost_saved_usd_total",
    "List price of answers served from the cache instead of a provider.",
    ["model"],
    registry=REGISTRY,
)
UNPRICED = Counter(
    "gateway_unpriced_requests_total",
    "Billed requests for a model with no price on file (recorded at cost 0).",
    ["model"],
    registry=REGISTRY,
)
USAGE_WRITE_FAILURES = Counter(
    "gateway_usage_write_failures_total",
    "Usage rows that could not be written to Postgres.",
    registry=REGISTRY,
)
REDIS_OUTAGES = Counter(
    "gateway_redis_outages_total",
    "Times a Redis-backed component switched to its degraded mode.",
    ["component"],
    registry=REGISTRY,
)

# Answer quality, from the latest stored eval runs (read at scrape time).

EVAL_PASS_RATE = Gauge(
    "gateway_eval_pass_rate",
    "Pass rate (0-1) of the latest completed eval run, by dataset and model.",
    ["dataset", "model"],
    registry=REGISTRY,
)
EVAL_MEAN_SCORE = Gauge(
    "gateway_eval_mean_score",
    "Mean score (0-1) of the latest completed eval run, by dataset and model.",
    ["dataset", "model"],
    registry=REGISTRY,
)
EVAL_LAST_RUN = Gauge(
    "gateway_eval_last_run_timestamp_seconds",
    "Unix time the latest completed eval run finished, by dataset and model.",
    ["dataset", "model"],
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


def observe_usage(
    *,
    model: str,
    billed: bool,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    list_cost_usd: float,
    priced: bool,
) -> None:
    if billed:
        TOKENS.labels(model, "prompt").inc(prompt_tokens)
        TOKENS.labels(model, "completion").inc(completion_tokens)
        COST_USD.labels(model).inc(cost_usd)
        if not priced:
            UNPRICED.labels(model).inc()
    else:
        SAVED_USD.labels(model).inc(list_cost_usd)


def update_eval_scores(scores: Iterable["LatestScore"]) -> None:
    """Replace every eval series, so a dataset that stopped running disappears."""
    for gauge in (EVAL_PASS_RATE, EVAL_MEAN_SCORE, EVAL_LAST_RUN):
        gauge.clear()
    for score in scores:
        labels = (score.dataset, score.model)
        EVAL_PASS_RATE.labels(*labels).set(score.pass_rate)
        EVAL_MEAN_SCORE.labels(*labels).set(score.mean_score)
        EVAL_LAST_RUN.labels(*labels).set(score.finished_at.timestamp())


def render() -> bytes:
    return generate_latest(REGISTRY)
