import random
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.chat import get_router
from app.core.router import Router
from app.core.routes import DEFAULT_ROUTES
from app.main import app
from app.providers.mock import MockProvider
from app.resilience.errors import (
    CircuitOpenError,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConfigError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
)

HELLO = {"role": "user", "content": "Hello!"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


def use_router(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]) -> None:
    """Swap in a fresh router (own breakers, instant sleeps, no real adapters)."""
    clock = FakeClock()
    providers = {
        "mock-1": MockProvider(),
        "openai-default": MockProvider(),
        "anthropic-default": MockProvider(),
        **overrides,
    }
    router = Router(
        DEFAULT_ROUTES,
        providers,
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(0),
    )
    monkeypatch.setitem(app.dependency_overrides, get_router, lambda: router)


def failing(error: Exception) -> AsyncMock:
    mock = AsyncMock()
    mock.complete.side_effect = error
    return mock


def test_chat_completion_success(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "mock-1", "messages": [HELLO]},
    )

    assert response.status_code == 200
    assert response.headers["x-provider"] == "mock-1"

    data = response.json()

    assert data["object"] == "chat.completion"
    assert data["model"] == "mock-1"
    assert data["choices"][0]["message"]["content"] == "Hello from the Mock LLM!"


def test_unsupported_model(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "unknown-model", "messages": [HELLO]},
    )

    assert response.status_code == 400


def test_invalid_request(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "mock-1", "messages": []},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ProviderTimeoutError("t", provider="openai"), 504),
        (ProviderConnectionError("c", provider="openai"), 502),
        (ProviderRateLimitError("r", provider="openai"), 503),
        (ProviderServerError("s", provider="openai"), 502),
        (ProviderAuthError("a", provider="openai"), 503),
        (ProviderConfigError("k", provider="openai"), 503),
        (ProviderResponseError("m", provider="openai"), 502),
        (ProviderBadRequestError("b", provider="openai"), 400),
        (CircuitOpenError("o", provider="openai"), 503),
    ],
)
def test_provider_errors_map_to_http_status(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: ProviderError,
    status: int,
) -> None:
    use_router(monkeypatch, {"openai-default": failing(error)})

    response = client.post(
        "/v1/chat/completions",
        json={"model": "openai-default", "messages": [HELLO]},
    )

    assert response.status_code == status
    assert response.json()["detail"] == error.detail


def test_rate_limit_sets_retry_after_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = ProviderRateLimitError("limited", provider="openai", retry_after=2.5)
    use_router(monkeypatch, {"openai-default": failing(error)})

    response = client.post(
        "/v1/chat/completions",
        json={"model": "openai-default", "messages": [HELLO]},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "3"


def test_chat_default_falls_back_to_second_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_router(
        monkeypatch,
        {
            "anthropic-default": failing(ProviderServerError("down", provider="a")),
            "openai-default": MockProvider("from openai"),
        },
    )

    response = client.post(
        "/v1/chat/completions",
        json={"model": "chat-default", "messages": [HELLO]},
    )

    assert response.status_code == 200
    assert response.headers["x-provider"] == "openai-default"
    assert response.json()["choices"][0]["message"]["content"] == "from openai"


def test_chat_default_returns_last_error_when_everything_is_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_router(
        monkeypatch,
        {
            "anthropic-default": failing(ProviderServerError("down", provider="a")),
            "openai-default": failing(ProviderTimeoutError("slow", provider="o")),
        },
    )

    response = client.post(
        "/v1/chat/completions",
        json={"model": "chat-default", "messages": [HELLO]},
    )

    assert response.status_code == 504
