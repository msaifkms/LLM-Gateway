import asyncio

import pytest

from app.api.schemas import ChatCompletionRequest, ChatMessage
from app.providers.mock import FailureMode, MockProvider
from app.resilience.breaker import BreakerConfig, BreakerState, CircuitBreaker
from app.resilience.errors import (
    CircuitOpenError,
    ProviderBadRequestError,
    ProviderServerError,
)


class Clock:
    """Manual clock: time only moves when the test says so."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_breaker(
    threshold: int = 3, timeout: float = 10.0, probes: int = 1
) -> tuple[CircuitBreaker, Clock]:
    clock = Clock()
    config = BreakerConfig(
        failure_threshold=threshold,
        recovery_timeout_s=timeout,
        half_open_max_calls=probes,
    )
    return CircuitBreaker("test", config, clock=clock), clock


def trip(breaker: CircuitBreaker, failures: int = 3) -> None:
    for _ in range(failures):
        breaker.record_failure()


def test_starts_closed_and_allows_requests() -> None:
    breaker, _ = make_breaker()

    assert breaker.state is BreakerState.CLOSED
    assert breaker.allow_request() is True


def test_opens_only_after_n_consecutive_failures() -> None:
    breaker, _ = make_breaker(threshold=3)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED

    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert breaker.allow_request() is False


def test_success_resets_the_failure_count() -> None:
    breaker, _ = make_breaker(threshold=3)

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()

    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 2


def test_open_becomes_half_open_after_cooldown() -> None:
    breaker, clock = make_breaker(timeout=10.0)
    trip(breaker)

    clock.advance(9.9)
    assert breaker.state is BreakerState.OPEN

    clock.advance(0.1)
    assert breaker.state is BreakerState.HALF_OPEN


def test_half_open_allows_exactly_one_probe() -> None:
    breaker, clock = make_breaker(probes=1)
    trip(breaker)
    clock.advance(10.0)

    assert breaker.allow_request() is True
    assert breaker.allow_request() is False


def test_half_open_allows_configured_number_of_probes() -> None:
    breaker, clock = make_breaker(probes=2)
    trip(breaker)
    clock.advance(10.0)

    assert breaker.allow_request() is True
    assert breaker.allow_request() is True
    assert breaker.allow_request() is False


def test_successful_probe_closes_the_circuit() -> None:
    breaker, clock = make_breaker(threshold=3)
    trip(breaker)
    clock.advance(10.0)
    assert breaker.allow_request() is True

    breaker.record_success()

    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 0
    assert breaker.allow_request() is True

    # It needs a full N failures to open again.
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED


def test_failed_probe_reopens_and_restarts_the_cooldown() -> None:
    breaker, clock = make_breaker(timeout=10.0)
    trip(breaker)
    clock.advance(10.0)
    assert breaker.allow_request() is True

    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN

    clock.advance(9.0)
    assert breaker.state is BreakerState.OPEN  # cooldown restarted at the failure

    clock.advance(1.0)
    assert breaker.state is BreakerState.HALF_OPEN


def test_late_results_are_ignored_while_open() -> None:
    breaker, _ = make_breaker()
    trip(breaker)

    breaker.record_success()

    assert breaker.state is BreakerState.OPEN


def test_invalid_config_is_rejected() -> None:
    with pytest.raises(ValueError):
        BreakerConfig(failure_threshold=0)
    with pytest.raises(ValueError):
        BreakerConfig(recovery_timeout_s=-1)
    with pytest.raises(ValueError):
        BreakerConfig(half_open_max_calls=0)


class Counter:
    """An async callable that counts invocations and then fails or succeeds."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "ok"


def test_call_passes_success_through() -> None:
    breaker, _ = make_breaker()
    func = Counter()

    assert asyncio.run(breaker.call(func)) == "ok"
    assert func.calls == 1


def test_call_counts_provider_failures_and_then_rejects_fast() -> None:
    breaker, _ = make_breaker(threshold=2, timeout=10.0)
    failing = Counter(ProviderServerError("boom", provider="test"))

    for _ in range(2):
        with pytest.raises(ProviderServerError):
            asyncio.run(breaker.call(failing))

    with pytest.raises(CircuitOpenError) as info:
        asyncio.run(breaker.call(failing))

    assert failing.calls == 2  # the third call never reached the function
    assert info.value.retry_after == 10.0
    assert info.value.retryable is False
    assert info.value.fallback is True


def test_bad_requests_do_not_trip_the_breaker() -> None:
    breaker, _ = make_breaker(threshold=2)
    bad = Counter(ProviderBadRequestError("bad", provider="test"))

    for _ in range(10):
        with pytest.raises(ProviderBadRequestError):
            asyncio.run(breaker.call(bad))

    assert breaker.state is BreakerState.CLOSED


def test_unexpected_errors_propagate_and_free_the_probe_slot() -> None:
    breaker, clock = make_breaker(probes=1)
    trip(breaker)
    clock.advance(10.0)
    buggy = Counter(ValueError("bug in our code"))

    with pytest.raises(ValueError):
        asyncio.run(breaker.call(buggy))

    # Still half-open, and the slot is free again for another probe.
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow_request() is True


def test_breaker_with_mock_provider_full_cycle() -> None:
    """Fail until open, fail fast, then recover through a half-open probe."""
    breaker, clock = make_breaker(threshold=3, timeout=30.0)
    provider = MockProvider(failure_mode=FailureMode.SERVER_ERROR)
    request = ChatCompletionRequest(
        model="mock-1", messages=[ChatMessage(role="user", content="Hi")]
    )

    def attempt() -> object:
        return asyncio.run(breaker.call(lambda: provider.complete(request)))

    for _ in range(3):
        with pytest.raises(ProviderServerError):
            attempt()
    assert breaker.state is BreakerState.OPEN

    with pytest.raises(CircuitOpenError):
        attempt()
    assert provider.calls == 3  # rejected without touching the provider

    provider.failure_mode = FailureMode.NONE  # the provider recovers
    clock.advance(30.0)

    assert attempt() is not None  # the half-open probe succeeds
    assert breaker.state is BreakerState.CLOSED
    assert provider.calls == 4
