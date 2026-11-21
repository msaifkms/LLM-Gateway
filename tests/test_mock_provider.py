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


def test_the_mock_can_report_a_model_and_token_counts() -> None:
    mock = MockProvider(
        model_name="gpt-4.1-mini", prompt_tokens=10, completion_tokens=4
    )
    request = ChatCompletionRequest(
        model="alias", messages=[ChatMessage(role="user", content="hi")]
    )

    response = asyncio.run(mock.complete(request))

    assert response.model == "gpt-4.1-mini"
    assert (response.usage.prompt_tokens, response.usage.total_tokens) == (10, 14)
