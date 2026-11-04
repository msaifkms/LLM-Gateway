import logging
from decimal import Decimal

import httpx
import pytest
from fake_redis import FakeRedis
from fastapi.testclient import TestClient

from app.api.chat import get_cache, get_router
from app.api.schemas import (
    ChatChoice,
    ChatCompletionResponse,
    ChatMessage,
    TokenUsage,
)
from app.api.usage import get_usage_store
from app.auth.store import Principal
from app.core.cache import ResponseCache
from app.core.router import Router
from app.core.routes import DEFAULT_ROUTES
from app.main import app
from app.providers.mock import FailureMode, MockProvider
from app.usage.pricing import compute_cost
from app.usage.recorder import build_event
from app.usage.store import UsageEvent, UsageSummary

PRINCIPAL = Principal(
    user_id=7,
    user_name="u",
    key_id=3,
    rate_limit_per_min=60,
    daily_token_limit=None,
    daily_usd_limit=None,
    cache_enabled=True,
)


def answer(
    model: str, prompt: int = 1_000, completion: int = 500
) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="x",
        created=1,
        model=model,
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content="hi"),
                finish_reason="stop",
            )
        ],
        usage=TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        ),
    )


# --- what gets charged, per cache status ---------------------------------------


@pytest.mark.parametrize("status", ["MISS", "BYPASS"])
def test_requests_that_called_a_provider_are_charged(status: str) -> None:
    event = build_event(
        PRINCIPAL, "chat-default", status, "openai-default", answer("gpt-4.1-mini")
    )

    assert event.cost_usd == Decimal("0.00120000")
    assert event.list_cost_usd == event.cost_usd


@pytest.mark.parametrize("status", ["HIT", "COALESCED"])
def test_requests_answered_without_a_provider_call_are_free(status: str) -> None:
    event = build_event(
        PRINCIPAL, "chat-default", status, "cache", answer("gpt-4.1-mini")
    )

    assert event.cost_usd == 0
    assert event.list_cost_usd == Decimal("0.00120000")  # what it would have cost
    assert (
        event.prompt_tokens == 1_000
    )  # the tokens the caller received are still logged


def test_an_event_identifies_who_asked_what_and_who_answered() -> None:
    event = build_event(
        PRINCIPAL, "chat-default", "MISS", "openai-default", answer("gpt-4.1-mini")
    )

    assert (event.user_id, event.api_key_id) == (7, 3)
    assert (event.route, event.provider, event.model) == (
        "chat-default",
        "openai-default",
        "gpt-4.1-mini",
    )
    assert event.priced is True


def test_an_unpriced_model_is_recorded_as_unpriced() -> None:
    event = build_event(PRINCIPAL, "r", "MISS", "p", answer("mystery-model"))

    assert event.cost_usd == 0
    assert event.priced is False


# --- recording through the endpoint ----------------------------------------------


class FakeUsageStore:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []
        self.fail_with: Exception | None = None

    async def record(self, event: UsageEvent) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.events.append(event)

    async def summary(self, user_id: int, days: int) -> UsageSummary:
        raise NotImplementedError


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: FakeUsageStore | None,
    fake_redis: FakeRedis | None = None,
    provider: MockProvider | None = None,
) -> MockProvider:
    mock = provider or MockProvider("hello back")
    router = Router(
        DEFAULT_ROUTES,
        {
            "mock-1": mock,
            "openai-default": MockProvider("unused"),
            "anthropic-default": MockProvider("unused"),
        },
    )
    cache = ResponseCache(fake_redis, ttl_s=60) if fake_redis is not None else None  # type: ignore[arg-type]
    monkeypatch.setitem(app.dependency_overrides, get_router, lambda: router)
    monkeypatch.setitem(app.dependency_overrides, get_cache, lambda: cache)
    monkeypatch.setitem(app.dependency_overrides, get_usage_store, lambda: store)
    return mock


def post(client: TestClient) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-1",
            "messages": [{"role": "user", "content": "Hello!"}],
            "temperature": 0,
        },
    )


def test_a_miss_then_a_hit_are_recorded_and_only_the_miss_could_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore()
    install(monkeypatch, store=store, fake_redis=FakeRedis())

    first = post(client)
    second = post(client)

    miss, hit = store.events
    body = first.json()
    expected = compute_cost(
        body["model"],
        body["usage"]["prompt_tokens"],
        body["usage"]["completion_tokens"],
    )
    assert second.headers["x-cache"] == "HIT"
    assert (miss.cache_status, miss.provider) == ("MISS", "mock-1")
    assert (hit.cache_status, hit.provider) == ("HIT", "cache")
    assert miss.cost_usd == expected.amount
    assert hit.cost_usd == 0
    assert hit.list_cost_usd == miss.list_cost_usd
    assert (miss.user_id, miss.api_key_id, miss.route) == (1, 1, "mock-1")
    assert miss.prompt_tokens == body["usage"]["prompt_tokens"]


def test_an_uncached_request_is_recorded_as_bypass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore()
    install(monkeypatch, store=store, fake_redis=None)

    post(client)

    assert [e.cache_status for e in store.events] == ["BYPASS"]


def test_a_failed_request_records_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore()
    install(
        monkeypatch,
        store=store,
        provider=MockProvider(failure_mode=FailureMode.BAD_REQUEST),
    )

    response = post(client)

    assert response.status_code == 400
    assert store.events == []


def test_a_bookkeeping_failure_never_fails_the_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUsageStore()
    store.fail_with = OSError("database went away")
    install(monkeypatch, store=store)

    with caplog.at_level(logging.ERROR):
        response = post(client)

    assert response.status_code == 200
    assert "Could not record usage" in caplog.text


def test_without_a_database_requests_still_work_and_nothing_is_recorded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, store=None)

    assert post(client).status_code == 200
