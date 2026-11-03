"""Runtime settings read from environment variables."""

import os
from dataclasses import dataclass
from typing import Self

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_REDIS_TIMEOUT_S = 0.5
DEFAULT_CACHE_TTL_S = 3600


@dataclass(frozen=True)
class Settings:
    #: None means "no Redis": the gateway runs without a cache.
    redis_url: str | None
    #: Connect/read timeout for Redis, so a slow Redis can never slow requests.
    redis_timeout_s: float
    #: How long a cached response lives before Redis deletes it.
    cache_ttl_s: int = DEFAULT_CACHE_TTL_S

    @classmethod
    def from_env(cls) -> Self:
        redis_url = os.getenv("REDIS_URL", DEFAULT_REDIS_URL).strip()
        timeout_s = float(os.getenv("REDIS_TIMEOUT_S", str(DEFAULT_REDIS_TIMEOUT_S)))
        ttl_s = int(os.getenv("CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_S)))
        if ttl_s <= 0:
            raise ValueError("CACHE_TTL_SECONDS must be a positive integer")
        return cls(
            redis_url=redis_url or None,
            redis_timeout_s=timeout_s,
            cache_ttl_s=ttl_s,
        )
