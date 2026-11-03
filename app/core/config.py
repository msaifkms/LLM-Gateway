"""Runtime settings read from environment variables."""

import os
from dataclasses import dataclass
from typing import Self

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_REDIS_TIMEOUT_S = 0.5
DEFAULT_CACHE_TTL_S = 3600
DEFAULT_DB_POOL_MAX = 10


@dataclass(frozen=True)
class Settings:
    #: None means "no Redis": the gateway runs without a cache.
    redis_url: str | None
    #: Connect/read timeout for Redis, so a slow Redis can never slow requests.
    redis_timeout_s: float
    #: How long a cached response lives before Redis deletes it.
    cache_ttl_s: int = DEFAULT_CACHE_TTL_S
    #: PostgreSQL URL (users, API keys, usage). None means no database is configured.
    database_url: str | None = None
    #: Maximum simultaneous Postgres connections held by this gateway instance.
    db_pool_max: int = DEFAULT_DB_POOL_MAX

    @classmethod
    def from_env(cls) -> Self:
        redis_url = os.getenv("REDIS_URL", DEFAULT_REDIS_URL).strip()
        timeout_s = float(os.getenv("REDIS_TIMEOUT_S", str(DEFAULT_REDIS_TIMEOUT_S)))
        ttl_s = int(os.getenv("CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_S)))
        if ttl_s <= 0:
            raise ValueError("CACHE_TTL_SECONDS must be a positive integer")

        database_url = os.getenv("DATABASE_URL", "").strip()
        pool_max = int(os.getenv("DB_POOL_MAX", str(DEFAULT_DB_POOL_MAX)))
        if pool_max <= 0:
            raise ValueError("DB_POOL_MAX must be a positive integer")

        return cls(
            redis_url=redis_url or None,
            redis_timeout_s=timeout_s,
            cache_ttl_s=ttl_s,
            database_url=database_url or None,
            db_pool_max=pool_max,
        )
