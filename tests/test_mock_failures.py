import asyncio
import time

import pytest

from app.api.schemas import ChatCompletionRequest, ChatMessage
from app.providers.mock import FailureMode, MockProvider
from app.resilience.errors import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
)

REQUEST = ChatCompletionRequest(
    model="mock-1",
    messages=[ChatMessage(role="user", content="Hi")],
)


def call(provider: MockProvider) -> object:
    return asyncio.run(provider.complete(REQUEST))


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (FailureMode.SERVER_ERROR, ProviderServerError),
        (FailureMode.TIMEOUT, ProviderTimeoutError),
        (FailureMode.CONNECTION, ProviderConnectionError),
        (FailureMode.RATE_LIMIT, ProviderRateLimitError),
        (FailureMode.AUTH, ProviderAuthError),
        (FailureMode.BAD_REQUEST, ProviderBadRequestError),
        (FailureMode.MALFORMED, ProviderResponseError),
    ],
)
def test_each_failure_mode_raises_its_error(
    mode: FailureMode, expected: type[ProviderError]
) -> None:
    provider = MockProvider(failure_mode=mode)

    with pytest.raises(expected) as info:
        call(provider)

    assert info.value.provider == "mock"


def test_rate_limit_carries_retry_after() -> None:
    provider = MockProvider(failure_mode=FailureMode.RATE_LIMIT, retry_after_s=3.0)

    with pytest.raises(ProviderRateLimitError) as info:
        call(provider)

    assert info.value.retry_after == 3.0


def test_latency_is_applied() -> None:
    provider = MockProvider(latency_s=0.05)

    start = time.perf_counter()
    call(provider)

    assert time.perf_counter() - start >= 0.04


def test_failure_rate_zero_never_fails() -> None:
    provider = MockProvider(failure_mode=FailureMode.SERVER_ERROR, failure_rate=0.0)

    for _ in range(50):
        call(provider)

    assert provider.calls == 50


def test_partial_failure_rate_is_reproducible_with_seed() -> None:
    def count_failures() -> int:
        provider = MockProvider(
            failure_mode=FailureMode.SERVER_ERROR, failure_rate=0.5, seed=1
        )
        failures = 0
        for _ in range(200):
            try:
                call(provider)
            except ProviderServerError:
                failures += 1
        return failures

    first = count_failures()

    assert 70 <= first <= 130
    assert count_failures() == first


def test_calls_counter_counts_failures_too() -> None:
    provider = MockProvider(failure_mode=FailureMode.TIMEOUT)

    for _ in range(3):
        with pytest.raises(ProviderTimeoutError):
            call(provider)

    assert provider.calls == 3


def test_provider_can_recover_when_mode_is_flipped() -> None:
    provider = MockProvider(failure_mode=FailureMode.SERVER_ERROR)

    with pytest.raises(ProviderServerError):
        call(provider)

    provider.failure_mode = FailureMode.NONE

    assert call(provider) is not None
