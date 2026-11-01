import asyncio
import random
import time

import pytest

from app.api.schemas import ChatCompletionRequest, ChatMessage
from app.core.router import Router, RouteResult
from app.core.routes import Route
from app.providers.mock import FailureMode, MockProvider
from app.resilience.breaker import BreakerConfig, BreakerState
from app.resilience.errors import (
    ProviderBadRequestError,
    ProviderServerError,
    ProviderTimeoutError,
)

REQUEST = ChatCompletionRequest(
    model="r", messages=[ChatMessage(role="user", content="Hi")]
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


def make_router(
    providers: dict[str, MockProvider],
    route: Route,
    *,
    threshold: int = 5,
    cooldown: float = 1000.0,
) -> tuple[Router, FakeClock]:
    clock = FakeClock()
    router = Router(
        {"r": route},
        providers,
        breaker_config=BreakerConfig(
            failure_threshold=threshold, recovery_timeout_s=cooldown
        ),
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(0),
    )
    return router, clock


def run(router: Router) -> RouteResult:
    return asyncio.run(router.complete(REQUEST))


def test_primary_serves_when_healthy() -> None:
    a, b = MockProvider("from a"), MockProvider("from b")
    router, _ = make_router({"a": a, "b": b}, Route(targets=("a", "b")))

    result = run(router)

    assert result.provider == "a"
    assert result.failed_providers == ()
    assert result.response.choices[0].message.content == "from a"
    assert b.calls == 0


def test_falls_back_when_primary_keeps_failing() -> None:
    a = MockProvider("from a", failure_mode=FailureMode.SERVER_ERROR)
    b = MockProvider("from b")
    router, _ = make_router({"a": a, "b": b}, Route(targets=("a", "b")))

    result = run(router)

    assert result.provider == "b"
    assert result.failed_providers == ("a",)
    assert a.calls == 3  # first attempt + 2 retries
    assert b.calls == 1


def test_primary_fully_down_100_requests_all_succeed() -> None:
    """The spec's failover criterion: >= 99% success with the primary 100% down."""
    a = MockProvider(failure_mode=FailureMode.SERVER_ERROR)
    b = MockProvider("from b")
    router, _ = make_router({"a": a, "b": b}, Route(targets=("a", "b")), threshold=5)

    results = [run(router) for _ in range(100)]

    assert all(r.provider == "b" for r in results)
    assert a.calls == 5  # the breaker opened after 5 failures, then protected it
    assert b.calls == 100
    assert router.breaker_state("a") is BreakerState.OPEN
    assert router.breaker_state("b") is BreakerState.CLOSED


def test_non_retryable_error_falls_back_without_retrying() -> None:
    a = MockProvider(failure_mode=FailureMode.AUTH)
    b = MockProvider("from b")
    router, _ = make_router({"a": a, "b": b}, Route(targets=("a", "b")))

    result = run(router)

    assert result.provider == "b"
    assert a.calls == 1


def test_bad_request_neither_retries_nor_falls_back() -> None:
    a = MockProvider(failure_mode=FailureMode.BAD_REQUEST)
    b = MockProvider("from b")
    router, _ = make_router({"a": a, "b": b}, Route(targets=("a", "b")))

    with pytest.raises(ProviderBadRequestError):
        run(router)

    assert a.calls == 1
    assert b.calls == 0


def test_raises_the_last_error_when_every_target_fails() -> None:
    a = MockProvider(failure_mode=FailureMode.SERVER_ERROR)
    b = MockProvider(failure_mode=FailureMode.TIMEOUT)
    router, _ = make_router({"a": a, "b": b}, Route(targets=("a", "b")))

    with pytest.raises(ProviderTimeoutError):
        run(router)


def test_slow_provider_hits_the_attempt_timeout_and_falls_back() -> None:
    a = MockProvider("from a", latency_s=1.0)
    b = MockProvider("from b")
    route = Route(targets=("a", "b"), timeout_s=0.05, max_retries=0)
    router, _ = make_router({"a": a, "b": b}, route)

    start = time.perf_counter()
    result = run(router)

    assert result.provider == "b"
    assert a.calls == 1
    assert time.perf_counter() - start < 0.5


def test_breaker_recovers_and_primary_serves_again() -> None:
    a = MockProvider("from a", failure_mode=FailureMode.SERVER_ERROR)
    b = MockProvider("from b")
    router, clock = make_router(
        {"a": a, "b": b}, Route(targets=("a", "b")), threshold=3, cooldown=30.0
    )

    assert run(router).provider == "b"  # 3 failures open a's breaker
    assert router.breaker_state("a") is BreakerState.OPEN
    assert run(router).provider == "b"  # rejected instantly, no call to a
    assert a.calls == 3

    a.failure_mode = FailureMode.NONE  # the provider recovers
    clock.now += 30.0  # cooldown elapses

    result = run(router)

    assert result.provider == "a"  # the half-open probe succeeded
    assert router.breaker_state("a") is BreakerState.CLOSED
    assert a.calls == 4


def test_route_deadline_is_respected() -> None:
    a = MockProvider(failure_mode=FailureMode.SERVER_ERROR)
    b = MockProvider(failure_mode=FailureMode.SERVER_ERROR)
    route = Route(targets=("a", "b"), max_retries=5, deadline_s=1.0)
    router, clock = make_router({"a": a, "b": b}, route, threshold=100)

    with pytest.raises(ProviderServerError):
        run(router)

    assert clock.now <= 1.0


def test_has_route() -> None:
    router, _ = make_router({"a": MockProvider()}, Route(targets=("a",)))

    assert router.has_route("r") is True
    assert router.has_route("nope") is False


def test_unknown_provider_in_a_route_is_rejected_at_startup() -> None:
    with pytest.raises(ValueError):
        Router({"r": Route(targets=("ghost",))}, {"a": MockProvider()})


def test_route_validation() -> None:
    with pytest.raises(ValueError):
        Route(targets=())
    with pytest.raises(ValueError):
        Route(targets=("a",), timeout_s=0)
    with pytest.raises(ValueError):
        Route(targets=("a",), max_retries=-1)
