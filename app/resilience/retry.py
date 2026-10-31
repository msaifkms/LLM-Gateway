"""Retry with exponential backoff and full jitter."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.resilience.errors import ProviderError


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2  # retries AFTER the first attempt
    base_delay_s: float = 0.2  # backoff cap for the first retry
    max_delay_s: float = 5.0  # backoff never exceeds this
    max_total_s: float = 20.0  # budget for the whole call, sleeps included

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.base_delay_s < 0 or self.max_delay_s < 0 or self.max_total_s <= 0:
            raise ValueError("delays must be >= 0 and max_total_s must be > 0")


async def _default_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def compute_delay(
    attempt: int,
    policy: RetryPolicy,
    retry_after: float | None,
    rng: random.Random,
) -> float:
    """Seconds to wait before retry number ``attempt + 1``.

    Full jitter: a random value between 0 and the exponential cap. If the
    provider sent Retry-After, we never wait less than that.
    """
    cap = min(policy.max_delay_s, policy.base_delay_s * (2**attempt))
    delay = rng.uniform(0, cap)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


async def call_with_retry[T](
    func: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] = _default_sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: random.Random | None = None,
) -> T:
    """Call ``func``, retrying retryable ProviderErrors with backoff.

    Raises the last error when: it is not retryable, retries are used up,
    or waiting would exceed the total time budget.
    """
    rng = rng or random.Random()
    start = clock()
    attempt = 0

    while True:
        try:
            return await func()
        except ProviderError as exc:
            if not exc.retryable or attempt >= policy.max_retries:
                raise

            delay = compute_delay(attempt, policy, exc.retry_after, rng)
            if clock() - start + delay > policy.max_total_s:
                raise

            await sleep(delay)
            attempt += 1
