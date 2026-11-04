from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI, Request, Response

from app.api.chat import router as chat_router
from app.api.usage import router as usage_router
from app.auth.store import PostgresKeyStore
from app.core.cache import ResponseCache
from app.core.config import Settings
from app.core.killswitch import KillSwitch, PostgresKillSwitchStore
from app.core.ratelimit import RateLimiter
from app.core.redis_client import check_redis, create_redis
from app.core.router import Router
from app.core.routes import DEFAULT_ROUTES
from app.core.singleflight import SingleFlight
from app.db.pool import check_db, create_db_pool
from app.providers.registry import build_providers
from app.usage.store import PostgresUsageStore


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

        db = (
            await create_db_pool(settings.database_url, max_size=settings.db_pool_max)
            if settings.database_url
            else None
        )
        app.state.db = db
        app.state.key_store = PostgresKeyStore(db) if db is not None else None
        app.state.usage_store = PostgresUsageStore(db) if db is not None else None
        app.state.kill_switch = (
            KillSwitch(PostgresKillSwitchStore(db)) if db is not None else None
        )

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

        app.state.rate_limiter = (
            RateLimiter(redis, fail_open=settings.rate_limit_fail_open)
            if redis is not None
            else None
        )

        try:
            # Both checks log a warning and never block startup.
            if db is not None:
                await check_db(db)
            if redis is not None:
                await check_redis(redis)
            yield
        finally:
            if redis is not None:
                await redis.aclose()
            if db is not None:
                await db.close()


app = FastAPI(
    title="LLM Gateway",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/healthz")
def health_check() -> dict[str, str]:
    """Liveness: the process is up. Checks no dependencies."""
    return {"status": "ok"}


@app.get("/readyz")
async def readiness(request: Request, response: Response) -> dict[str, str]:
    """Readiness: can this instance serve traffic?

    Postgres is required (no database, no way to authenticate). Redis is only
    reported: the gateway keeps working without it.
    """
    db: asyncpg.Pool | None = request.app.state.db
    redis = request.app.state.redis

    if db is None:
        postgres = "disabled"
    else:
        postgres = "ok" if await check_db(db) else "unavailable"

    if redis is None:
        redis_status = "disabled"
    else:
        redis_status = "ok" if await check_redis(redis) else "unavailable"

    ready = postgres == "ok"
    if not ready:
        response.status_code = 503
    return {
        "status": "ready" if ready else "not_ready",
        "postgres": postgres,
        "redis": redis_status,
    }


app.include_router(chat_router)
app.include_router(usage_router)
