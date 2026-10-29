import pytest
from pydantic import ValidationError

from app.api.schemas import ChatCompletionRequest


def test_valid_chat_request() -> None:
    request = ChatCompletionRequest(
        model="chat-default",
        messages=[{"role": "user", "content": "Hello!"}],
    )

    assert request.model == "chat-default"
    assert request.temperature == 1.0
    assert len(request.messages) == 1


def test_invalid_temperature() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest(
            model="chat-default",
            messages=[{"role": "user", "content": "Hello!"}],
            temperature=50,
        )


def test_empty_messages() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest(
            model="chat-default",
            messages=[],
        )
