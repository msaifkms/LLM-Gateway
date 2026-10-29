import os

import httpx

from app.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)


class OpenAIProvider:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.client = client

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        payload: dict[str, object] = {
            "model": self.model,
            "messages": [message.model_dump() for message in request.messages],
            "temperature": request.temperature,
        }

        if request.max_tokens is not None:
            payload["max_completion_tokens"] = request.max_tokens

        async def send(
            client: httpx.AsyncClient,
        ) -> httpx.Response:
            return await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if self.client is not None:
            response = await send(self.client)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await send(client)

        response.raise_for_status()

        return ChatCompletionResponse.model_validate(response.json())
