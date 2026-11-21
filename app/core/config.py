"""Runtime settings read from environment variables."""

import os
from dataclasses import dataclass
from typing import Self
from urllib.parse import quote

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_REDIS_TIMEOUT_S = 0.5
DEFAULT_CACHE_TTL_S = 3600
DEFAULT_DB_POOL_MAX = 10

#: REDIS_URL values that mean "run without Redis", besides an empty value. ECS
#: drops environment variables whose value is empty, so a deployment needs a word.
REDIS_DISABLED_VALUES = frozenset({"none", "off", "disabled"})


def _parse_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be true or false")


def database_url_from_env() -> str | None:
    """DATABASE_URL, or a URL built from DB_HOST, DB_USER, DB_PASSWORD and friends.

    The parts form exists for deployments that inject the password on its own
    (for example from a secret store managed by the database service), so the
    full URL never has to be stored anywhere. DATABASE_URL wins if both are set.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url

    host = os.getenv("DB_HOST", "").strip()
    if not host:
        return None
    user = os.getenv("DB_USER", "").strip()
    name = os.getenv("DB_NAME", "").strip()
    if not user or not name:
        raise ValueError("DB_HOST is set, so DB_USER and DB_NAME must be set too")

    password = os.getenv("DB_PASSWORD", "")
    port = os.getenv("DB_PORT", "5432").strip() or "5432"
    if not port.isdigit():
        raise ValueError("DB_PORT must be a number")
    sslmode = os.getenv("DB_SSLMODE", "").strip()

    # Generated passwords can hold characters that are special in a URL.
    credentials = quote(user, safe="")
    if password:
        credentials += ":" + quote(password, safe="")
    url = f"postgresql://{credentials}@{host}:{port}/{quote(name, safe='')}"
    if sslmode:
        url += f"?sslmode={quote(sslmode, safe='')}"
    return url


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
    #: If Redis is down: True lets requests through unlimited, False refuses them.
    rate_limit_fail_open: bool = True
    #: Add the benchmark-only Mock targets and routes (never in production).
    bench_mode: bool = False

    @classmethod
    def from_env(cls) -> Self:
        redis_url = os.getenv("REDIS_URL", DEFAULT_REDIS_URL).strip()
        if redis_url.lower() in REDIS_DISABLED_VALUES:
            redis_url = ""
        timeout_s = float(os.getenv("REDIS_TIMEOUT_S", str(DEFAULT_REDIS_TIMEOUT_S)))
        ttl_s = int(os.getenv("CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_S)))
        if ttl_s <= 0:
            raise ValueError("CACHE_TTL_SECONDS must be a positive integer")

        database_url = database_url_from_env()
        pool_max = int(os.getenv("DB_POOL_MAX", str(DEFAULT_DB_POOL_MAX)))
        if pool_max <= 0:
            raise ValueError("DB_POOL_MAX must be a positive integer")
        fail_open = _parse_bool("RATE_LIMIT_FAIL_OPEN", default=True)
        bench_mode = _parse_bool("GATEWAY_BENCH_MODE", default=False)

        return cls(
            redis_url=redis_url or None,
            redis_timeout_s=timeout_s,
            cache_ttl_s=ttl_s,
            database_url=database_url or None,
            db_pool_max=pool_max,
            rate_limit_fail_open=fail_open,
            bench_mode=bench_mode,
        )
