import asyncio

import httpx

from app.api.schemas import (
    ChatCompletionRequest,
    ChatMessage,
)
from app.providers.anthropic import AnthropicProvider


def test_anthropic_provider() -> None:
    def mock_api(
        request: httpx.Request,
    ) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "test-key"

        import json

        payload = json.loads(request.content)

        assert payload["system"] == "Be concise."
        assert payload["messages"] == [{"role": "user", "content": "Explain Docker"}]
        assert payload["max_tokens"] == 100

        return httpx.Response(
            200,
            json={
                "id": "msg_test_123",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [
                    {
                        "type": "text",
                        "text": "Docker packages applications.",
                    }
                ],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                },
            },
        )

    async def run_test() -> None:
        transport = httpx.MockTransport(mock_api)

        async with httpx.AsyncClient(transport=transport) as client:
            provider = AnthropicProvider(
                api_key="test-key",
                model="claude-haiku-5-5",
                client=client,
            )

            request = ChatCompletionRequest(
                model="anthropic-default",
                messages=[
                    ChatMessage(
                        role="system",
                        content="Be concise.",
                    ),
                    ChatMessage(
                        role="user",
                        content="Explain Docker",
                    ),
                ],
                max_tokens=100,
            )

            response = await provider.complete(request)

            assert response.model == "claude-haiku-5-5"
            assert (
                response.choices[0].message.content == "Docker packages applications."
            )
            assert response.usage.prompt_tokens == 12
            assert response.usage.completion_tokens == 8
            assert response.usage.total_tokens == 20
            assert response.choices[0].finish_reason == "stop"

    asyncio.run(run_test())
