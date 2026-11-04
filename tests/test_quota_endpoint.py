from dataclasses import replace
from decimal import Decimal

import asyncpg
import httpx
import pytest
from fake_redis import FakeRedis
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.api.chat import get_cache, get_router
from app.api.usage import get_usage_store
from app.auth.store import Principal
from app.core.cache import ResponseCache
from app.core.router import Router
from app.core.routes import DEFAULT_ROUTES
from app.main import app
from app.providers.mock import MockProvider
from app.usage.store import DailyUsage, UsageEvent, UsageSummary

BASE = Principal(
    user_id=5,
    user_name="limited",
    key_id=9,
    rate_limit_per_min=60,
    daily_token_limit=None,
    daily_usd_limit=None,
    cache_enabled=True,
)


class FakeUsageStore:
    def __init__(self, today: DailyUsage | None = None) -> None:
        self.today = today or DailyUsage(0, Decimal(0))
        self.error: Exception | None = None
        self.asked: list[int] = []
        self.events: list[UsageEvent] = []

    async def usage_today(self, user_id: int) -> DailyUsage:
        self.asked.append(user_id)
        if self.error is not None:
            raise self.error
        return self.today

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)

    async def summary(self, user_id: int, days: int) -> UsageSummary:
        raise NotImplementedError


def install(
    monkeypatch: pytest.MonkeyPatch,
    principal: Principal,
    store: FakeUsageStore | None,
    *,
    fake_redis: FakeRedis | None = None,
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
    cache = ResponseCache(fake_redis, ttl_s=60) if fake_redis is not None else None  # type: ignore[arg-type]
    monkeypatch.setitem(app.dependency_overrides, authenticate, lambda: principal)
    monkeypatch.setitem(app.dependency_overrides, get_router, lambda: router)
    monkeypatch.setitem(app.dependency_overrides, get_cache, lambda: cache)
    monkeypatch.setitem(app.dependency_overrides, get_usage_store, lambda: store)
    return provider


def post(client: TestClient) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-1",
            "messages": [{"role": "user", "content": "Hello!"}],
            "temperature": 0,
        },
    )


def test_a_user_under_their_quota_is_served(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore(DailyUsage(400, Decimal("0.5")))
    limited = replace(BASE, daily_token_limit=1_000, daily_usd_limit=Decimal(2))
    provider = install(monkeypatch, limited, store)

    assert post(client).status_code == 200
    assert provider.calls == 1
    assert store.asked == [5]  # asked about this user, and only this user


def test_a_user_who_used_their_tokens_is_refused_before_any_provider_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore(DailyUsage(1_000, Decimal(0)))
    provider = install(monkeypatch, replace(BASE, daily_token_limit=1_000), store)

    response = post(client)

    assert response.status_code == 429
    assert response.json() == {"detail": "Daily token quota exceeded"}
    assert 1 <= int(response.headers["retry-after"]) <= 86_400  # until 00:00 UTC
    assert provider.calls == 0
    assert store.events == []  # a refused request is not a usage row


def test_a_user_who_spent_their_budget_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore(DailyUsage(10, Decimal("2.00")))
    provider = install(
        monkeypatch, replace(BASE, daily_usd_limit=Decimal("2.00")), store
    )

    response = post(client)

    assert response.status_code == 429
    assert response.json() == {"detail": "Daily spending quota exceeded"}
    assert provider.calls == 0


def test_users_without_limits_cost_no_extra_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore(DailyUsage(10**9, Decimal(1000000)))
    install(monkeypatch, BASE, store)

    assert post(client).status_code == 200
    assert store.asked == []


def test_cached_answers_are_still_served_after_the_quota_is_used_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore(DailyUsage(0, Decimal(0)))
    limited = replace(BASE, daily_token_limit=1_000)
    provider = install(monkeypatch, limited, store, fake_redis=FakeRedis())

    first = post(client)  # a MISS while still under quota
    store.today = DailyUsage(5_000, Decimal(0))  # now over quota
    second = post(client)

    assert first.headers["x-cache"] == "MISS"
    assert second.status_code == 200
    assert second.headers["x-cache"] == "HIT"  # free, so never blocked
    assert provider.calls == 1


def test_a_new_request_is_refused_once_over_quota_even_with_a_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore(DailyUsage(5_000, Decimal(0)))
    provider = install(
        monkeypatch,
        replace(BASE, daily_token_limit=1_000),
        store,
        fake_redis=FakeRedis(),
    )

    assert post(client).status_code == 429  # a MISS would cost money
    assert provider.calls == 0


def test_if_usage_cannot_be_read_a_limited_user_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore()
    store.error = asyncpg.PostgresConnectionError("down")
    provider = install(monkeypatch, replace(BASE, daily_token_limit=1_000), store)

    response = post(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "Quota check unavailable"}
    assert provider.calls == 0


def test_without_a_usage_store_a_limited_user_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = install(monkeypatch, replace(BASE, daily_usd_limit=Decimal(1)), None)

    assert post(client).status_code == 503
    assert provider.calls == 0
