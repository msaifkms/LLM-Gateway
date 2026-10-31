import math

from fastapi import APIRouter, HTTPException

from app.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from app.providers.anthropic import AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.mock import MockProvider
from app.providers.openai import OpenAIProvider
from app.resilience.errors import ProviderError

router = APIRouter(prefix="/v1")

providers: dict[str, LLMProvider] = {
    "mock-1": MockProvider(),
    "openai-default": OpenAIProvider(),
    "anthropic-default": AnthropicProvider(),
}


@router.post(
    "/chat/completions",
    response_model=ChatCompletionResponse,
)
async def create_chat_completion(
    request: ChatCompletionRequest,
) -> ChatCompletionResponse:
    provider = providers.get(request.model)

    if provider is None:
        raise HTTPException(
            status_code=400,
            detail="Unsupported model",
        )

    try:
        return await provider.complete(request)

    except ProviderError as exc:
        headers = None
        if exc.retry_after is not None:
            headers = {"Retry-After": str(math.ceil(exc.retry_after))}
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=headers,
        ) from None
