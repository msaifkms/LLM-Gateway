import asyncio

import httpx

from app.api.schemas import (
    ChatCompletionRequest,
    ChatMessage,
)
from app.providers.openai import OpenAIProvider


def test_openai_provider() -> None:
    def mock_api(
        request: httpx.Request,
    ) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"

        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "gpt-4.1-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Docker packages applications.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    transport = httpx.MockTransport(mock_api)

    async def run_test() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            provider = OpenAIProvider(
                api_key="test-key",
                model="gpt-4.1-mini",
                client=client,
            )

            request = ChatCompletionRequest(
                model="openai-default",
                messages=[
                    ChatMessage(
                        role="user",
                        content="Explain Docker",
                    )
                ],
            )

            response = await provider.complete(request)

            assert (
                response.choices[0].message.content == "Docker packages applications."
            )
            assert response.usage.total_tokens == 15

    asyncio.run(run_test())
