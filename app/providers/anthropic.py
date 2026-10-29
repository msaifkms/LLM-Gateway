import os
import time

import httpx

from app.api.schemas import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    TokenUsage,
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
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")

        api_key = self.api_key

        # Anthropic uses a top-level system field.
        system_messages = [
            msg.content for msg in request.messages if msg.role == "system"
        ]

        conversation = [
            msg.model_dump() for msg in request.messages if msg.role != "system"
        ]

        if not conversation:
            raise ValueError("At least one conversation message required")

        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": request.max_tokens or 256,
            "messages": conversation,
        }

        if system_messages:
            payload["system"] = "\n\n".join(system_messages)

        async def send(
            client: httpx.AsyncClient,
        ) -> httpx.Response:
            return await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2026-10-21",
                    "content-type": "application/json",
                },
                json=payload,
            )

        if self.client is not None:
            response = await send(self.client)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await send(client)

        response.raise_for_status()
        data = response.json()

        text = "".join(
            block["text"] for block in data["content"] if block["type"] == "text"
        )

        usage = data["usage"]
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]

        if data["stop_reason"] == "max_tokens":
            finish_reason = "length"
        else:
            finish_reason = "stop"

        return ChatCompletionResponse(
            id=data["id"],
            created=int(time.time()),
            model=data["model"],
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant",
                        content=text,
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=TokenUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
