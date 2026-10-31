import asyncio
import random
from collections.abc import Awaitable, Callable

import pytest

from app.api.schemas import ChatCompletionRequest, ChatMessage
from app.providers.mock import FailureMode, MockProvider
from app.resilience.errors import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
)
from app.resilience.retry import RetryPolicy, call_with_retry, compute_delay


class FakeClock:
    """A clock whose sleep() advances time instantly instead of waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class Flaky:
    """Fails `failures` times with `error`, then succeeds."""

    def __init__(self, failures: int, error: Exception) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "ok"


def run_retry(
    func: Callable[[], Awaitable[str]],
    policy: RetryPolicy,
    clock: FakeClock,
    seed: int = 0,
) -> str:
    return asyncio.run(
        call_with_retry(
            func,
            policy,
            sleep=clock.sleep,
            clock=clock,
            rng=random.Random(seed),
        )
    )


def server_error() -> ProviderServerError:
    return ProviderServerError("boom", provider="test")


def test_success_on_first_try_does_not_sleep() -> None:
    clock = FakeClock()
    func = Flaky(failures=0, error=server_error())

    assert run_retry(func, RetryPolicy(), clock) == "ok"
    assert func.calls == 1
    assert clock.sleeps == []


def test_retries_transient_error_then_succeeds() -> None:
    clock = FakeClock()
    func = Flaky(failures=2, error=server_error())

    assert run_retry(func, RetryPolicy(max_retries=2), clock) == "ok"
    assert func.calls == 3
    assert len(clock.sleeps) == 2


def test_gives_up_after_max_retries() -> None:
    clock = FakeClock()
    func = Flaky(failures=99, error=server_error())

    with pytest.raises(ProviderServerError):
        run_retry(func, RetryPolicy(max_retries=2), clock)

    assert func.calls == 3  # first attempt + 2 retries


def test_zero_retries_means_one_attempt() -> None:
    clock = FakeClock()
    func = Flaky(failures=99, error=server_error())

    with pytest.raises(ProviderServerError):
        run_retry(func, RetryPolicy(max_retries=0), clock)

    assert func.calls == 1


@pytest.mark.parametrize(
    "error",
    [
        ProviderBadRequestError("bad", provider="test"),
        ProviderAuthError("auth", provider="test"),
    ],
)
def test_non_retryable_errors_are_not_retried(error: ProviderError) -> None:
    clock = FakeClock()
    func = Flaky(failures=99, error=error)

    with pytest.raises(type(error)):
        run_retry(func, RetryPolicy(max_retries=5), clock)

    assert func.calls == 1
    assert clock.sleeps == []


def test_non_provider_errors_propagate_immediately() -> None:
    clock = FakeClock()
    func = Flaky(failures=99, error=ValueError("a real bug"))

    with pytest.raises(ValueError):
        run_retry(func, RetryPolicy(max_retries=5), clock)

    assert func.calls == 1


def test_retry_after_is_honored() -> None:
    clock = FakeClock()
    error = ProviderRateLimitError("slow down", provider="test", retry_after=3.0)
    func = Flaky(failures=1, error=error)

    assert run_retry(func, RetryPolicy(), clock) == "ok"
    assert clock.sleeps[0] >= 3.0


def test_retry_after_beyond_budget_fails_fast() -> None:
    clock = FakeClock()
    error = ProviderRateLimitError("wait", provider="test", retry_after=30.0)
    func = Flaky(failures=1, error=error)

    with pytest.raises(ProviderRateLimitError):
        run_retry(func, RetryPolicy(max_total_s=20.0), clock)

    assert func.calls == 1
    assert clock.sleeps == []


def test_total_budget_is_never_exceeded() -> None:
    clock = FakeClock()
    func = Flaky(failures=999, error=server_error())
    policy = RetryPolicy(
        max_retries=10, base_delay_s=4.0, max_delay_s=10.0, max_total_s=15.0
    )

    with pytest.raises(ProviderServerError):
        run_retry(func, policy, clock)

    assert clock.now <= 15.0


@pytest.mark.parametrize("attempt", range(8))
def test_delay_stays_within_exponential_cap(attempt: int) -> None:
    policy = RetryPolicy(base_delay_s=0.2, max_delay_s=5.0)
    cap = min(5.0, 0.2 * 2**attempt)
    rng = random.Random(1)

    for _ in range(200):
        assert 0 <= compute_delay(attempt, policy, None, rng) <= cap


def test_retry_after_sets_a_floor_on_the_delay() -> None:
    policy = RetryPolicy()

    assert compute_delay(0, policy, 9.0, random.Random(1)) >= 9.0


def test_invalid_policy_is_rejected() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=-1)

    with pytest.raises(ValueError):
        RetryPolicy(max_total_s=0)


def test_retry_wrapped_around_the_mock_provider() -> None:
    """The mock fails twice, then 'recovers'; retry hides the outage."""
    provider = MockProvider(failure_mode=FailureMode.SERVER_ERROR)
    request = ChatCompletionRequest(
        model="mock-1", messages=[ChatMessage(role="user", content="Hi")]
    )

    async def attempt() -> str:
        if provider.calls == 2:
            provider.failure_mode = FailureMode.NONE
        response = await provider.complete(request)
        return response.choices[0].message.content

    clock = FakeClock()

    assert run_retry(attempt, RetryPolicy(max_retries=2), clock) == (
        "Hello from the Mock LLM!"
    )
    assert provider.calls == 3
