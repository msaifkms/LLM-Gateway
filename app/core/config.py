"""Runtime settings read from environment variables."""

import os
from dataclasses import dataclass
from typing import Self

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_REDIS_TIMEOUT_S = 0.5


@dataclass(frozen=True)
class Settings:
    #: None means "no Redis": the gateway runs without a cache.
    redis_url: str | None
    #: Connect/read timeout for Redis, so a slow Redis can never slow requests.
    redis_timeout_s: float

    @classmethod
    def from_env(cls) -> Self:
        redis_url = os.getenv("REDIS_URL", DEFAULT_REDIS_URL).strip()
        timeout_s = float(os.getenv("REDIS_TIMEOUT_S", str(DEFAULT_REDIS_TIMEOUT_S)))
        return cls(redis_url=redis_url or None, redis_timeout_s=timeout_s)
