"""Router: priority fallback with retry and a circuit breaker per provider."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from app.api.schemas import ChatCompletionRequest, ChatCompletionResponse
from app.core import metrics
from app.core.routes import Route
from app.providers.base import LLMProvider
from app.resilience.breaker import BreakerConfig, BreakerState, CircuitBreaker
from app.resilience.errors import ProviderError, ProviderTimeoutError
from app.resilience.retry import RetryPolicy, call_with_retry


@dataclass(frozen=True)
class RouteResult:
    response: ChatCompletionResponse
    provider: str  # the target that answered
    failed_providers: tuple[str, ...]  # targets given up on before it


async def _sleep(delay: float) -> None:
    await asyncio.sleep(delay)


class Router:
    def __init__(
        self,
        routes: Mapping[str, Route],
        providers: Mapping[str, LLMProvider],
        *,
        breaker_config: BreakerConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = _sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        for route_name, route in routes.items():
            for target in route.targets:
                if target not in providers:
                    raise ValueError(
                        f"route {route_name!r} targets unknown provider {target!r}"
                    )

        self._routes = routes
        self._providers = providers  # kept by reference, looked up per call
        self._breaker_config = breaker_config
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._breakers: dict[str, CircuitBreaker] = {}

    def has_route(self, name: str) -> bool:
        return name in self._routes

    def get_route(self, name: str) -> Route | None:
        return self._routes.get(name)

    def breaker_state(self, provider: str) -> BreakerState:
        breaker = self._breakers.get(provider)
        return breaker.state if breaker else BreakerState.CLOSED

    def breaker_states(self) -> dict[str, BreakerState]:
        """State of every provider's breaker (closed if never used)."""
        return {name: self.breaker_state(name) for name in self._providers}

    def _breaker(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name, self._breaker_config, clock=self._clock
            )
        return self._breakers[name]

    async def complete(self, request: ChatCompletionRequest) -> RouteResult:
        route = self._routes[request.model]
        start = self._clock()
        last_error: ProviderError | None = None
        failed: list[str] = []

        for name in route.targets:
            remaining = route.deadline_s - (self._clock() - start)
            if remaining <= 0:
                break

            try:
                response = await self._try_target(name, route, request, remaining)
            except ProviderError as exc:
                failed.append(name)
                last_error = exc
                if not exc.fallback:
                    raise  # e.g. bad request: no other provider can fix it
                metrics.FALLBACKS.labels(name).inc()
                continue

            return RouteResult(
                response=response,
                provider=name,
                failed_providers=tuple(failed),
            )

        if last_error is None:
            raise ProviderTimeoutError(
                "route deadline exceeded before any attempt", provider="router"
            )
        raise last_error

    async def _try_target(
        self,
        name: str,
        route: Route,
        request: ChatCompletionRequest,
        remaining: float,
    ) -> ChatCompletionResponse:
        provider = self._providers[name]
        breaker = self._breaker(name)
        timeout_s = min(route.timeout_s, remaining)
        policy = RetryPolicy(max_retries=route.max_retries, max_total_s=remaining)

        async def attempt() -> ChatCompletionResponse:
            try:
                result = await breaker.call(
                    lambda: self._run_attempt(provider, name, request, timeout_s)
                )
            except ProviderError as exc:
                # Includes "circuit open" rejections, which never reach the provider.
                metrics.PROVIDER_ATTEMPTS.labels(name, type(exc).__name__).inc()
                raise
            except Exception:
                metrics.PROVIDER_ATTEMPTS.labels(name, "unexpected").inc()
                raise
            metrics.PROVIDER_ATTEMPTS.labels(name, "success").inc()
            return result

        return await call_with_retry(
            attempt,
            policy,
            sleep=self._sleep,
            clock=self._clock,
            rng=self._rng,
        )

    async def _run_attempt(
        self,
        provider: LLMProvider,
        name: str,
        request: ChatCompletionRequest,
        timeout_s: float,
    ) -> ChatCompletionResponse:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s):
                return await provider.complete(request)
        except TimeoutError as exc:
            raise ProviderTimeoutError(
                f"{name} exceeded the {timeout_s:.1f}s attempt timeout",
                provider=name,
            ) from exc
        finally:
            metrics.PROVIDER_DURATION.labels(name).observe(
                time.perf_counter() - started
            )
