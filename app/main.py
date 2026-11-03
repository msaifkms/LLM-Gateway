from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.core.cache import ResponseCache
from app.core.config import Settings
from app.core.redis_client import check_redis, create_redis
from app.core.router import Router
from app.core.routes import DEFAULT_ROUTES
from app.core.singleflight import SingleFlight
from app.providers.registry import build_providers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create shared resources at startup and close them at shutdown."""
    settings = Settings.from_env()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    ) as http_client:
        app.state.router = Router(DEFAULT_ROUTES, build_providers(http_client))
        app.state.singleflight = SingleFlight()

        redis = (
            create_redis(settings.redis_url, settings.redis_timeout_s)
            if settings.redis_url
            else None
        )
        app.state.redis = redis
        app.state.cache = (
            ResponseCache(redis, ttl_s=settings.cache_ttl_s)
            if redis is not None
            else None
        )

        try:
            if redis is not None:
                await check_redis(redis)  # logs a warning; never blocks startup
            yield
        finally:
            if redis is not None:
                await redis.aclose()


app = FastAPI(
    title="LLM Gateway",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/healthz")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(chat_router)
