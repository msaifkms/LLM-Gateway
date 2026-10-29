from fastapi.testclient import TestClient

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
