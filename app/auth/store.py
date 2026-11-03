"""Who is calling? Looking up an API key hash in Postgres."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import asyncpg


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: a user, the key they used, and their limits."""

    user_id: int
    user_name: str
    key_id: int
    rate_limit_per_min: int
    daily_token_limit: int | None
    daily_usd_limit: Decimal | None
    cache_enabled: bool


class KeyStore(Protocol):
    async def lookup(self, key_hash: str) -> Principal | None: ...


LOOKUP_SQL = """
SELECT u.id AS user_id,
       u.name AS user_name,
       u.rate_limit_per_min,
       u.daily_token_limit,
       u.daily_usd_limit,
       k.id AS key_id,
       k.cache_enabled
FROM api_keys AS k
JOIN users AS u ON u.id = k.user_id
WHERE k.key_hash = $1
  AND k.revoked_at IS NULL
  AND u.is_active
"""


class PostgresKeyStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def lookup(self, key_hash: str) -> Principal | None:
        """The caller for this key, or None if it is unknown, revoked or inactive."""
        row = await self._pool.fetchrow(LOOKUP_SQL, key_hash)
        if row is None:
            return None
        return Principal(
            user_id=row["user_id"],
            user_name=row["user_name"],
            key_id=row["key_id"],
            rate_limit_per_min=row["rate_limit_per_min"],
            daily_token_limit=row["daily_token_limit"],
            daily_usd_limit=row["daily_usd_limit"],
            cache_enabled=row["cache_enabled"],
        )
