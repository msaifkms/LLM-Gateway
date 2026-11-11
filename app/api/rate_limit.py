"""Rate limiting as a request dependency: runs after auth, before the cache."""

import math
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response

from app.api.auth import authenticate
from app.auth.store import Principal
from app.core import metrics
from app.core.ratelimit import RateLimiter, RateLimiterUnavailable


def get_rate_limiter(request: Request) -> RateLimiter | None:
    """None when Redis is not configured: the gateway then has no rate limits."""
    limiter: RateLimiter | None = request.app.state.rate_limiter
    return limiter


async def rate_limit(
    response: Response,
    principal: Annotated[Principal, Depends(authenticate)],
    limiter: Annotated[RateLimiter | None, Depends(get_rate_limiter)],
) -> Principal:
    if limiter is None:
        return principal

    try:
        result = await limiter.check(principal.user_id, principal.rate_limit_per_min)
    except RateLimiterUnavailable:
        metrics.RATE_LIMIT_EVENTS.labels("unavailable").inc()
        raise HTTPException(
            status_code=503, detail="Rate limiting is unavailable"
        ) from None

    if result is None:  # Redis is down and the limiter fails open
        metrics.RATE_LIMIT_EVENTS.labels("fail_open").inc()
        return principal

    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
    }
    if not result.allowed:
        metrics.RATE_LIMIT_EVENTS.labels("limited").inc()
        headers["Retry-After"] = str(max(1, math.ceil(result.retry_after_s)))
        raise HTTPException(
            status_code=429, detail="Rate limit exceeded", headers=headers
        )

    response.headers.update(headers)
    return principal
