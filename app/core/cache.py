"""Exact-match response cache in Redis.

The cache is an optimization: every Redis failure is logged and treated as a
miss, so a Redis outage can never fail a request.
"""

import hashlib
import json
import logging

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.api.schemas import ChatCompletionRequest, ChatCompletionResponse
from app.core.routes import CacheMode, Route

logger = logging.getLogger(__name__)

#: Bump when the key layout or the stored format changes, so old entries are
#: ignored instead of misread.
KEY_VERSION = 2
KEY_PREFIX = f"llmgw:cache:v{KEY_VERSION}:"

#: Providers that ignore the request's temperature (Anthropic API version
#: 2026-10-21 removed it), so "temperature 0" does not make them deterministic.
TEMPERATURE_IGNORED_TARGETS = frozenset({"anthropic-default"})


def build_cache_key(request: ChatCompletionRequest, user_id: int) -> str:
    """SHA-256 of the canonical JSON of everything that shapes the answer.

    The user is part of the key: a cached answer is that user's data, so one
    user must never be served another user's entry.
    """
    canonical = json.dumps(
        {
            "user": user_id,
            "model": request.model,  # the route alias, not the upstream model
            "messages": [message.model_dump() for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{KEY_PREFIX}{digest}"


def is_cacheable(request: ChatCompletionRequest, route: Route) -> bool:
    """Only requests that should produce the same answer every time."""
    if route.cache is CacheMode.NEVER:
        return False
    if request.temperature != 0:
        return False
    if route.cache is CacheMode.TEMPERATURE_ZERO:
        return True
    return not any(target in TEMPERATURE_IGNORED_TARGETS for target in route.targets)


def wants_no_store(cache_control: str | None) -> bool:
    """True if the request's Cache-Control header contains the no-store directive."""
    if not cache_control:
        return False
    return any(
        directive.split("=", 1)[0].strip().lower() == "no-store"
        for directive in cache_control.split(",")
    )


class ResponseCache:
    def __init__(self, client: Redis, ttl_s: int) -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._healthy = True

    async def get(self, key: str) -> ChatCompletionResponse | None:
        try:
            raw = await self._client.get(key)
        except (RedisError, OSError):
            self._mark_unhealthy()
            return None
        self._mark_healthy()

        if raw is None:
            return None
        try:
            return ChatCompletionResponse.model_validate_json(raw)
        except ValidationError:
            logger.warning("Ignoring an unreadable cache entry")
            return None

    async def set(self, key: str, response: ChatCompletionResponse) -> None:
        try:
            await self._client.set(key, response.model_dump_json(), ex=self._ttl_s)
        except (RedisError, OSError):
            self._mark_unhealthy()
            return
        self._mark_healthy()

    def _mark_unhealthy(self) -> None:
        # Log once per outage, not once per request.
        if self._healthy:
            logger.warning("Redis cache unavailable; serving requests uncached")
            self._healthy = False

    def _mark_healthy(self) -> None:
        if not self._healthy:
            logger.info("Redis cache recovered")
            self._healthy = True
