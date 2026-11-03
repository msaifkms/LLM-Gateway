"""Admin operations: create users, issue and revoke API keys.

These run from the command line (app/cli.py), never over HTTP, so there is no
public endpoint that can mint keys.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import asyncpg

from app.auth.keys import display_prefix, generate_api_key, hash_api_key


class AdminError(Exception):
    """A problem the admin should see as a plain message, not a traceback."""


@dataclass(frozen=True)
class IssuedKey:
    key_id: int
    plaintext: str  # shown once; only its hash is stored


async def create_user(
    pool: asyncpg.Pool,
    name: str,
    *,
    rate_limit_per_min: int = 60,
    daily_token_limit: int | None = None,
    daily_usd_limit: Decimal | None = None,
) -> int:
    try:
        user_id = await pool.fetchval(
            "INSERT INTO users (name, rate_limit_per_min, daily_token_limit, "
            "daily_usd_limit) VALUES ($1, $2, $3, $4) RETURNING id",
            name,
            rate_limit_per_min,
            daily_token_limit,
            daily_usd_limit,
        )
    except asyncpg.UniqueViolationError:
        raise AdminError(f"user {name!r} already exists") from None
    return int(user_id)


async def issue_key(
    pool: asyncpg.Pool,
    user_name: str,
    *,
    label: str = "",
    cache_enabled: bool = True,
) -> IssuedKey:
    user_id = await pool.fetchval("SELECT id FROM users WHERE name = $1", user_name)
    if user_id is None:
        raise AdminError(f"no user named {user_name!r}")

    plaintext = generate_api_key()
    key_id = await pool.fetchval(
        "INSERT INTO api_keys (user_id, label, key_prefix, key_hash, cache_enabled) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        user_id,
        label,
        display_prefix(plaintext),
        hash_api_key(plaintext),
        cache_enabled,
    )
    return IssuedKey(key_id=int(key_id), plaintext=plaintext)


async def revoke_key(pool: asyncpg.Pool, key_id: int) -> None:
    revoked = await pool.fetchval(
        "UPDATE api_keys SET revoked_at = now() "
        "WHERE id = $1 AND revoked_at IS NULL RETURNING id",
        key_id,
    )
    if revoked is None:
        raise AdminError(f"no active key with id {key_id}")


async def list_keys(pool: asyncpg.Pool, user_name: str | None = None) -> list[Any]:
    return list(
        await pool.fetch(
            "SELECT k.id, u.name AS user_name, k.key_prefix, k.label, "
            "k.cache_enabled, k.created_at, k.revoked_at "
            "FROM api_keys AS k JOIN users AS u ON u.id = k.user_id "
            "WHERE ($1::text IS NULL OR u.name = $1) "
            "ORDER BY k.id",
            user_name,
        )
    )
