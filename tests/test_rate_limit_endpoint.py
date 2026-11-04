import pytest
from fastapi.testclient import TestClient

from app.api.chat import get_router
from app.api.rate_limit import get_rate_limiter
from app.core.ratelimit import RateLimiterUnavailable, RateLimitResult
from app.core.router import Router
from app.core.routes import DEFAULT_ROUTES
from app.main import app
from app.providers.mock import MockProvider

BODY = {"model": "mock-1", "messages": [{"role": "user", "content": "Hello!"}]}


class FakeLimiter:
    """Returns a canned verdict and records who asked."""

    def __init__(
        self,
        result: RateLimitResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[int, int]] = []

    async def check(self, user_id: int, limit_per_min: int) -> RateLimitResult | None:
        self.calls.append((user_id, limit_per_min))
        if self.error is not None:
            raise self.error
        return self.result


def install(
    monkeypatch: pytest.MonkeyPatch, limiter: FakeLimiter | None
) -> MockProvider:
    provider = MockProvider("hello back")
    router = Router(
        DEFAULT_ROUTES,
        {
            "mock-1": provider,
            "openai-default": MockProvider("unused"),
            "anthropic-default": MockProvider("unused"),
        },
    )
    monkeypatch.setitem(app.dependency_overrides, get_router, lambda: router)
    monkeypatch.setitem(app.dependency_overrides, get_rate_limiter, lambda: limiter)
    return provider


def test_an_allowed_request_reports_the_remaining_budget(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = FakeLimiter(
        RateLimitResult(True, limit=60, remaining=41, retry_after_s=0)
    )
    provider = install(monkeypatch, limiter)

    response = client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 200
    assert response.headers["x-ratelimit-limit"] == "60"
    assert response.headers["x-ratelimit-remaining"] == "41"
    assert "retry-after" not in response.headers
    assert provider.calls == 1


def test_the_limiter_is_asked_about_the_authenticated_user_and_their_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = FakeLimiter(RateLimitResult(True, 60, 59, 0))
    install(monkeypatch, limiter)

    client.post("/v1/chat/completions", json=BODY)

    assert limiter.calls == [(1, 60)]  # TEST_PRINCIPAL: user 1, 60 per minute


def test_a_request_over_the_limit_is_429_and_never_reaches_a_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = FakeLimiter(
        RateLimitResult(False, limit=60, remaining=0, retry_after_s=11.2)
    )
    provider = install(monkeypatch, limiter)

    response = client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 429
    assert response.json() == {"detail": "Rate limit exceeded"}
    assert response.headers["retry-after"] == "12"  # rounded up, never short
    assert response.headers["x-ratelimit-limit"] == "60"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert provider.calls == 0


def test_retry_after_is_at_least_one_second(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = FakeLimiter(RateLimitResult(False, 60, 0, retry_after_s=0.2))
    install(monkeypatch, limiter)

    response = client.post("/v1/chat/completions", json=BODY)

    assert response.headers["retry-after"] == "1"


def test_when_redis_is_down_and_the_limiter_fails_open_the_request_is_served(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeLimiter(result=None))

    response = client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 200
    assert "x-ratelimit-limit" not in response.headers


def test_when_the_limiter_fails_closed_the_request_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = install(monkeypatch, FakeLimiter(error=RateLimiterUnavailable()))

    response = client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 503
    assert response.json() == {"detail": "Rate limiting is unavailable"}
    assert provider.calls == 0


def test_without_redis_there_are_no_rate_limits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = install(monkeypatch, None)

    response = client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 200
    assert "x-ratelimit-limit" not in response.headers
    assert provider.calls == 1


def test_health_checks_are_never_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = FakeLimiter(RateLimitResult(False, 60, 0, 5))
    install(monkeypatch, limiter)

    assert client.get("/healthz").status_code == 200
    assert limiter.calls == []
