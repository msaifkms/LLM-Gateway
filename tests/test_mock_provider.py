import asyncio

from app.api.schemas import ChatCompletionRequest, ChatMessage
from app.providers.base import LLMProvider
from app.providers.mock import MockProvider


def test_mock_provider_response() -> None:
    provider = MockProvider()

    request = ChatCompletionRequest(
        model="mock-1",
        messages=[
            ChatMessage(
                role="user",
                content="Hello!",
            )
        ],
    )

    response = asyncio.run(provider.complete(request))

    assert response.model == "mock-1"
    assert response.choices[0].message.role == "assistant"
    assert response.choices[0].message.content == "Hello from the Mock LLM!"
    assert response.object == "chat.completion"


def test_custom_mock_response() -> None:
    provider = MockProvider(response_text="This is a custom response.")

    request = ChatCompletionRequest(
        model="mock-1",
        messages=[
            ChatMessage(
                role="user",
                content="Test",
            )
        ],
    )

    response = asyncio.run(provider.complete(request))

    assert response.choices[0].message.content == "This is a custom response."


def test_mock_implements_provider_interface() -> None:
    provider: LLMProvider = MockProvider()

    assert callable(provider.complete)
