import os

import httpx
from pydantic import ValidationError

from app.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from app.providers.http import post_json
from app.resilience.errors import ProviderConfigError, ProviderResponseError

PROVIDER_NAME = "openai"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


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
            raise ProviderConfigError(
                "OPENAI_API_KEY is not configured", provider=PROVIDER_NAME
            )

        payload: dict[str, object] = {
            "model": self.model,
            "messages": [message.model_dump() for message in request.messages],
            "temperature": request.temperature,
        }

        if request.max_tokens is not None:
            payload["max_completion_tokens"] = request.max_tokens

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if self.client is not None:
            data = await post_json(
                self.client,
                provider=PROVIDER_NAME,
                url=OPENAI_URL,
                headers=headers,
                payload=payload,
            )
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                data = await post_json(
                    client,
                    provider=PROVIDER_NAME,
                    url=OPENAI_URL,
                    headers=headers,
                    payload=payload,
                )

        try:
            return ChatCompletionResponse.model_validate(data)
        except ValidationError as exc:
            raise ProviderResponseError(
                "openai returned an unexpected response shape",
                provider=PROVIDER_NAME,
            ) from exc
