from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.api.chat import get_router
from app.api.killswitch import get_kill_switch
from app.api.usage import get_usage_store
from app.core.killswitch import KillSwitchUnavailable
from app.core.router import Router
from app.core.routes import DEFAULT_ROUTES
from app.main import app
from app.providers.mock import MockProvider
from app.usage.store import (
    UsageEvent,
    UsageSummary,
    UsageTotals,
)


class FakeSwitch:
    def __init__(self, on: bool = False, unavailable: bool = False) -> None:
        self.on = on
        self.unavailable = unavailable
        self.asked = 0

    async def is_on(self) -> bool:
        self.asked += 1
        if self.unavailable:
            raise KillSwitchUnavailable
        return self.on


def install(monkeypatch: pytest.MonkeyPatch, switch: FakeSwitch | None) -> MockProvider:
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
    monkeypatch.setitem(app.dependency_overrides, get_kill_switch, lambda: switch)
    return provider


def post(client: TestClient) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={"model": "mock-1", "messages": [{"role": "user", "content": "Hi"}]},
    )


def test_when_the_switch_is_off_requests_are_served(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = install(monkeypatch, FakeSwitch(on=False))

    assert post(client).status_code == 200
    assert provider.calls == 1


def test_when_the_switch_is_on_chat_is_refused_and_no_provider_is_called(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = install(monkeypatch, FakeSwitch(on=True))

    response = post(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily disabled"}
    assert response.headers["retry-after"] == "60"
    assert provider.calls == 0


def test_the_switch_is_checked_before_authentication(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch = FakeSwitch(on=True)
    install(monkeypatch, switch)
    monkeypatch.delitem(app.dependency_overrides, authenticate)  # real auth, no key

    # A paused gateway answers 503 without touching the database for a key lookup.
    assert post(client).status_code == 503


def test_if_the_flag_cannot_be_read_the_gateway_assumes_it_is_paused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = install(monkeypatch, FakeSwitch(unavailable=True))

    response = post(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable"}
    assert provider.calls == 0


def test_without_a_database_there_is_no_switch_and_requests_are_served(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = install(monkeypatch, None)

    assert post(client).status_code == 200
    assert provider.calls == 1


def test_health_checks_and_usage_reports_work_while_paused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeSwitch(on=True))

    class Store:
        async def record(self, event: UsageEvent) -> None:
            raise NotImplementedError

        async def summary(self, user_id: int, days: int) -> UsageSummary:
            zero = Decimal(0)
            return UsageSummary(UsageTotals(0, 0, 0, 0, zero, zero), [], [])

    monkeypatch.setitem(app.dependency_overrides, get_usage_store, lambda: Store())

    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/usage").status_code == 200
