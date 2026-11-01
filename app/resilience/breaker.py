"""Per-provider circuit breaker: closed -> open -> half-open -> closed."""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from app.resilience.errors import CircuitOpenError, ProviderError


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerConfig:
    failure_threshold: int = 5  # consecutive failures that open the circuit
    recovery_timeout_s: float = 30.0  # cooldown before a probe is allowed
    half_open_max_calls: int = 1  # concurrent probes while half-open

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.recovery_timeout_s < 0:
            raise ValueError("recovery_timeout_s must be >= 0")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        config: BreakerConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.config = config or BreakerConfig()
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = 0

    # --- inspection -------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        self._refresh()
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def seconds_until_retry(self) -> float:
        """Remaining cooldown while open, else 0."""
        if self._state is not BreakerState.OPEN:
            return 0.0
        elapsed = self._clock() - self._opened_at
        return max(0.0, self.config.recovery_timeout_s - elapsed)

    # --- state machine ----------------------------------------------------

    def _refresh(self) -> None:
        """Open -> half-open once the cooldown has elapsed (lazy, no timer)."""
        if (
            self._state is BreakerState.OPEN
            and self._clock() - self._opened_at >= self.config.recovery_timeout_s
        ):
            self._state = BreakerState.HALF_OPEN
            self._half_open_in_flight = 0

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._half_open_in_flight = 0

    def _close(self) -> None:
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._half_open_in_flight = 0

    def allow_request(self) -> bool:
        self._refresh()

        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN:
            return False

        # Half-open: only a limited number of probes may pass.
        if self._half_open_in_flight < self.config.half_open_max_calls:
            self._half_open_in_flight += 1
            return True
        return False

    def record_success(self) -> None:
        if self._state is BreakerState.HALF_OPEN:
            self._close()
        elif self._state is BreakerState.CLOSED:
            self._consecutive_failures = 0
        # While open, a late result from a call that started earlier is ignored.

    def record_failure(self) -> None:
        if self._state is BreakerState.HALF_OPEN:
            self._open()  # the probe failed: back to open, cooldown restarts
        elif self._state is BreakerState.CLOSED:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.config.failure_threshold:
                self._open()

    def _release_probe(self) -> None:
        if self._state is BreakerState.HALF_OPEN and self._half_open_in_flight > 0:
            self._half_open_in_flight -= 1

    # --- convenience wrapper ---------------------------------------------

    async def call[T](self, func: Callable[[], Awaitable[T]]) -> T:
        """Run ``func`` through the breaker.

        Raises CircuitOpenError without calling ``func`` when the circuit
        is open (or a half-open probe is already in flight).
        """
        if not self.allow_request():
            raise CircuitOpenError(
                f"circuit open for {self.name}",
                provider=self.name,
                retry_after=self.seconds_until_retry(),
            )

        try:
            result = await func()
        except ProviderError as exc:
            if exc.fallback:
                self.record_failure()  # the provider looks unhealthy
            else:
                self.record_success()  # provider answered; the request was bad
            raise
        except BaseException:
            # Cancellation or a bug in our code says nothing about the
            # provider. Free the probe slot so we can't get stuck half-open.
            self._release_probe()
            raise

        self.record_success()
        return result
