from typing import Protocol

from app.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)


class LLMProvider(Protocol):
    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """Process a chat request and return a normalized response."""
        ...
