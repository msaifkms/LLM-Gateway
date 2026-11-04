"""PostgreSQL connection pool helpers.

Unlike Redis, Postgres is not optional once authentication exists: if it is down
the gateway cannot tell who is calling, so requests must fail rather than skip it.
The pool is created lazily (no connection at startup), so the gateway can start
before Postgres is ready and /readyz reports the real state.
"""

import logging

import asyncpg

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 3.0
QUERY_TIMEOUT_S = 5.0


async def create_db_pool(url: str, *, max_size: int) -> asyncpg.Pool:
    """Build a pool. min_size=0 means nothing connects until the first query."""
    return await asyncpg.create_pool(
        dsn=url,
        min_size=0,
        max_size=max_size,
        timeout=CONNECT_TIMEOUT_S,
        command_timeout=QUERY_TIMEOUT_S,
    )


async def check_db(pool: asyncpg.Pool) -> bool:
    """Run SELECT 1. Returns False (and logs) instead of raising."""
    try:
        await pool.fetchval("SELECT 1")
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
        logger.warning("Postgres is unreachable; authenticated requests will fail")
        return False
    return True
