import math
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.api.killswitch import require_gateway_enabled
from app.api.quota import enforce_quota
from app.api.rate_limit import rate_limit
from app.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from app.api.usage import get_usage_store
from app.auth.store import Principal
from app.core import metrics
from app.core.cache import (
    ResponseCache,
    build_cache_key,
    is_cacheable,
    wants_no_store,
)
from app.core.router import Router, RouteResult
from app.core.singleflight import SingleFlight
from app.resilience.errors import ProviderError
from app.usage.recorder import build_event, record_usage
from app.usage.store import UsageStore

router = APIRouter(prefix="/v1")


def get_router(request: Request) -> Router:
    """The Router is built once at startup (see lifespan in app/main.py)."""
    gateway_router: Router = request.app.state.router
    return gateway_router


def get_cache(request: Request) -> ResponseCache | None:
    """None when Redis is not configured: the gateway then runs uncached."""
    cache: ResponseCache | None = request.app.state.cache
    return cache


def get_singleflight(request: Request) -> SingleFlight:
    flight: SingleFlight = request.app.state.singleflight
    return flight


@router.post(
    "/chat/completions",
    response_model=ChatCompletionResponse,
)
async def create_chat_completion(
    _enabled: Annotated[None, Depends(require_gateway_enabled)],
    request: ChatCompletionRequest,
    response: Response,
    principal: Annotated[Principal, Depends(rate_limit)],
    routing: Annotated[Router, Depends(get_router)],
    cache: Annotated[ResponseCache | None, Depends(get_cache)],
    flight: Annotated[SingleFlight, Depends(get_singleflight)],
    usage_store: Annotated[UsageStore | None, Depends(get_usage_store)],
    cache_control: Annotated[str | None, Header()] = None,
) -> ChatCompletionResponse:
    route = routing.get_route(request.model)
    if route is None:
        raise HTTPException(
            status_code=400,
            detail="Unsupported model",
        )

    cache_key: str | None = None
    if (
        cache is not None
        and principal.cache_enabled
        and not wants_no_store(cache_control)
        and is_cacheable(request, route)
    ):
        cache_key = build_cache_key(request, principal.user_id)
        cached = await cache.get(cache_key)
        if cached is not None:
            response.headers["X-Cache"] = "HIT"
            metrics.CACHE_REQUESTS.labels("HIT").inc()
            await record_usage(
                usage_store,
                build_event(principal, request.model, "HIT", "cache", cached),
            )
            return cached

    # A provider is about to be paid for (cache hits returned above, free).
    await enforce_quota(principal, usage_store)

    async def fetch() -> RouteResult:
        """Call the providers and, when cacheable, store the answer."""
        result = await routing.complete(request)
        if cache is not None and cache_key is not None:
            await cache.set(cache_key, result.response)
        return result

    try:
        if cache_key is not None:
            # Identical requests that miss at the same moment share one call.
            result, is_leader = await flight.run(cache_key, fetch)
        else:
            result, is_leader = await fetch(), True
    except ProviderError as exc:
        headers = None
        if exc.retry_after is not None:
            headers = {"Retry-After": str(math.ceil(exc.retry_after))}
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=headers,
        ) from None

    if cache_key is None:
        cache_status = "BYPASS"
    elif is_leader:
        cache_status = "MISS"
    else:
        cache_status = "COALESCED"

    response.headers["X-Cache"] = cache_status
    metrics.CACHE_REQUESTS.labels(cache_status).inc()
    response.headers["X-Provider"] = result.provider
    await record_usage(
        usage_store,
        build_event(
            principal, request.model, cache_status, result.provider, result.response
        ),
    )
    return result.response
