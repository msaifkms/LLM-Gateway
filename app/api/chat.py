import httpx
from fastapi import APIRouter, HTTPException

from app.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from app.providers.anthropic import AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.mock import MockProvider
from app.providers.openai import OpenAIProvider

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

    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Provider configuration unavailable",
        ) from None

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid provider request",
        ) from None

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Upstream provider timed out",
        ) from None

    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code

        if status == 429:
            raise HTTPException(
                status_code=429,
                detail="Upstream provider rate limit exceeded",
            ) from None

        raise HTTPException(
            status_code=502,
            detail="Upstream provider rejected the request",
        ) from None

    except httpx.RequestError:
        raise HTTPException(
            status_code=502,
            detail="Upstream provider connection failed",
        ) from None
