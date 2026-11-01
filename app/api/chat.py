import math
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from app.core.router import Router
from app.resilience.errors import ProviderError

router = APIRouter(prefix="/v1")


def get_router(request: Request) -> Router:
    """The Router is built once at startup (see lifespan in app/main.py)."""
    gateway_router: Router = request.app.state.router
    return gateway_router


@router.post(
    "/chat/completions",
    response_model=ChatCompletionResponse,
)
async def create_chat_completion(
    request: ChatCompletionRequest,
    response: Response,
    routing: Annotated[Router, Depends(get_router)],
) -> ChatCompletionResponse:
    if not routing.has_route(request.model):
        raise HTTPException(
            status_code=400,
            detail="Unsupported model",
        )

    try:
        result = await routing.complete(request)
    except ProviderError as exc:
        headers = None
        if exc.retry_after is not None:
            headers = {"Retry-After": str(math.ceil(exc.retry_after))}
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=headers,
        ) from None

    response.headers["X-Provider"] = result.provider
    return result.response
