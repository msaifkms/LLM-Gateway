from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.chat import providers
from app.main import app
from app.resilience.errors import (
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

client = TestClient(app)

HELLO = {"role": "user", "content": "Hello!"}


def test_chat_completion_success() -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "mock-1", "messages": [HELLO]},
    )

    assert response.status_code == 200

    data = response.json()

    assert data["object"] == "chat.completion"
    assert data["model"] == "mock-1"
    assert data["choices"][0]["message"]["content"] == "Hello from the Mock LLM!"


def test_unsupported_model() -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "unknown-model", "messages": [HELLO]},
    )

    assert response.status_code == 400


def test_invalid_request() -> None:
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
    ],
)
def test_provider_errors_map_to_http_status(
    monkeypatch: pytest.MonkeyPatch, error: ProviderError, status: int
) -> None:
    mock = AsyncMock()
    mock.complete.side_effect = error
    monkeypatch.setitem(providers, "openai-default", mock)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "openai-default", "messages": [HELLO]},
    )

    assert response.status_code == status
    assert response.json()["detail"] == error.detail


def test_rate_limit_sets_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = AsyncMock()
    mock.complete.side_effect = ProviderRateLimitError(
        "limited", provider="openai", retry_after=2.5
    )
    monkeypatch.setitem(providers, "openai-default", mock)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "openai-default", "messages": [HELLO]},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "3"
