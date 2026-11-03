"""Redis connection helpers. Redis is an optimization, never a dependency."""

import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def create_redis(url: str, timeout_s: float) -> Redis:
    """Build a client. This does not connect; the first command does."""
    return Redis.from_url(
        url,
        socket_connect_timeout=timeout_s,
        socket_timeout=timeout_s,
    )


async def check_redis(client: Redis) -> bool:
    """Ping Redis. Returns False (and logs) instead of raising."""
    try:
        await client.ping()
    except (RedisError, OSError):
        logger.warning("Redis is unreachable; caching is skipped until it recovers")
        return False
    return True
