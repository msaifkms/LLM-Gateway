from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from app.api.chat import providers
from app.main import app

client = TestClient(app)


def test_chat_completion_success() -> None:
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-1",
            "messages": [
                {
                    "role": "user",
                    "content": "Hello!",
                }
            ],
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["object"] == "chat.completion"
    assert data["model"] == "mock-1"
    assert data["choices"][0]["message"]["content"] == "Hello from the Mock LLM!"


def test_unsupported_model() -> None:
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "unknown-model",
            "messages": [
                {
                    "role": "user",
                    "content": "Hello!",
                }
            ],
        },
    )

    assert response.status_code == 400


def test_invalid_request() -> None:
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-1",
            "messages": [],
        },
    )

    assert response.status_code == 422


def test_missing_provider_configuration() -> None:
    original = providers["openai-default"]

    try:
        mock = AsyncMock()
        mock.complete.side_effect = RuntimeError("Missing key")
        providers["openai-default"] = mock

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai-default",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 503

    finally:
        providers["openai-default"] = original


def test_provider_timeout() -> None:
    original = providers["openai-default"]

    try:
        mock = AsyncMock()
        mock.complete.side_effect = httpx.ReadTimeout("Simulated timeout")
        providers["openai-default"] = mock

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai-default",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 504

    finally:
        providers["openai-default"] = original
