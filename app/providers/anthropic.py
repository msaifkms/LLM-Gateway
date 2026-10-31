import os
import time
from typing import Any

import httpx
from pydantic import ValidationError

from app.api.schemas import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    TokenUsage,
)
from app.providers.http import post_json
from app.resilience.errors import (
    ProviderBadRequestError,
    ProviderConfigError,
    ProviderResponseError,
)

PROVIDER_NAME = "anthropic"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2026-10-21"


def _to_response(data: dict[str, Any]) -> ChatCompletionResponse:
    """Normalize an Anthropic Messages reply. Raises KeyError/TypeError if malformed."""
    text = "".join(
        block["text"] for block in data["content"] if block["type"] == "text"
    )

    usage = data["usage"]
    input_tokens = usage["input_tokens"]
    output_tokens = usage["output_tokens"]

    finish_reason = "length" if data["stop_reason"] == "max_tokens" else "stop"

    return ChatCompletionResponse(
        id=data["id"],
        created=int(time.time()),
        model=data["model"],
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content=text),
                finish_reason=finish_reason,
            )
        ],
        usage=TokenUsage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


class AnthropicProvider:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
        self.client = client

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        if not self.api_key:
            raise ProviderConfigError(
                "ANTHROPIC_API_KEY is not configured", provider=PROVIDER_NAME
            )
        api_key = self.api_key

        # Anthropic uses a top-level system field.
        system_messages = [
            msg.content for msg in request.messages if msg.role == "system"
        ]
        conversation = [
            msg.model_dump() for msg in request.messages if msg.role != "system"
        ]

        if not conversation:
            raise ProviderBadRequestError(
                "request has only system messages",
                provider=PROVIDER_NAME,
                detail="At least one non-system message is required",
            )

        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": request.max_tokens or 256,
            "messages": conversation,
        }
        if system_messages:
            payload["system"] = "\n\n".join(system_messages)

        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        if self.client is not None:
            data = await post_json(
                self.client,
                provider=PROVIDER_NAME,
                url=ANTHROPIC_URL,
                headers=headers,
                payload=payload,
            )
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                data = await post_json(
                    client,
                    provider=PROVIDER_NAME,
                    url=ANTHROPIC_URL,
                    headers=headers,
                    payload=payload,
                )

        try:
            return _to_response(data)
        except (KeyError, TypeError, ValidationError) as exc:
            raise ProviderResponseError(
                "anthropic returned an unexpected response shape",
                provider=PROVIDER_NAME,
            ) from exc
