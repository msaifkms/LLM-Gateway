import httpx
from fastapi import APIRouter, HTTPException

from app.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from app.providers.base import LLMProvider
from app.providers.mock import MockProvider
from app.providers.openai import OpenAIProvider

router = APIRouter(prefix="/v1")

providers: dict[str, LLMProvider] = {
    "mock-1": MockProvider(),
    "openai-default": OpenAIProvider(),
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

    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Provider configuration unavailable",
        ) from None

    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="Upstream provider request failed",
        ) from None
