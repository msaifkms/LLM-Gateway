"""Admin commands and key lookup against a real Postgres.

Skipped unless TEST_DATABASE_URL is set (the database name must end in _test).
"""

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal

import asyncpg
import pytest

from app.auth import admin
from app.auth.keys import hash_api_key
from app.auth.store import PostgresKeyStore
from app.db.migrate import apply_migrations
from app.db.pool import create_db_pool

DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not set")


async def reset_schema(url: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()


@pytest.fixture
def database_url() -> Iterator[str]:
    assert DATABASE_URL is not None
    if not DATABASE_URL.rstrip("/").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must name a database ending in _test")

    asyncio.run(reset_schema(DATABASE_URL))
    yield DATABASE_URL
    asyncio.run(reset_schema(DATABASE_URL))


def with_pool[T](url: str, scenario: Callable[[asyncpg.Pool], Awaitable[T]]) -> T:
    """Run a scenario against a freshly migrated database."""

    async def runner() -> T:
        pool = await create_db_pool(url, max_size=3)
        try:
            await apply_migrations(pool)
            return await scenario(pool)
        finally:
            await pool.close()

    return asyncio.run(runner())


def test_create_user_stores_limits(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> asyncpg.Record:
        user_id = await admin.create_user(
            pool,
            "alice",
            rate_limit_per_min=30,
            daily_token_limit=50_000,
            daily_usd_limit=Decimal("2.50"),
        )
        return await pool.fetchrow("SELECT * FROM users WHERE id = $1", user_id)

    row = with_pool(database_url, scenario)

    assert row["name"] == "alice"
    assert row["rate_limit_per_min"] == 30
    assert row["daily_token_limit"] == 50_000
    assert row["daily_usd_limit"] == Decimal("2.500000")


def test_duplicate_user_names_are_rejected_cleanly(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> None:
        await admin.create_user(pool, "alice")
        with pytest.raises(admin.AdminError, match="already exists"):
            await admin.create_user(pool, "alice")

    with_pool(database_url, scenario)


def test_only_the_hash_of_a_key_is_stored(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> tuple[str, asyncpg.Record, int]:
        await admin.create_user(pool, "alice")
        issued = await admin.issue_key(pool, "alice", label="laptop")
        row = await pool.fetchrow("SELECT * FROM api_keys WHERE id = $1", issued.key_id)
        # search every text column of the table for the plaintext
        leaks = await pool.fetchval(
            "SELECT count(*) FROM api_keys AS k WHERE position($1 IN k::text) > 0",
            issued.plaintext,
        )
        return issued.plaintext, row, leaks

    plaintext, row, leaks = with_pool(database_url, scenario)

    assert plaintext.startswith("llmgw_")
    assert row["key_hash"] == hash_api_key(plaintext)
    assert row["key_prefix"] == plaintext[:10]
    assert row["label"] == "laptop"
    assert leaks == 0  # the plaintext appears nowhere in the table


def test_a_valid_key_resolves_to_its_user_and_limits(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        user_id = await admin.create_user(
            pool, "alice", rate_limit_per_min=30, daily_token_limit=1000
        )
        issued = await admin.issue_key(pool, "alice")
        found = await PostgresKeyStore(pool).lookup(hash_api_key(issued.plaintext))
        return user_id, issued, found

    user_id, issued, found = with_pool(database_url, scenario)

    assert found is not None
    assert found.user_id == user_id
    assert found.user_name == "alice"
    assert found.key_id == issued.key_id
    assert found.rate_limit_per_min == 30
    assert found.daily_token_limit == 1000
    assert found.daily_usd_limit is None
    assert found.cache_enabled is True


def test_an_unknown_key_resolves_to_nobody(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        await admin.create_user(pool, "alice")
        await admin.issue_key(pool, "alice")
        return await PostgresKeyStore(pool).lookup(hash_api_key("llmgw_not-a-real-key"))

    assert with_pool(database_url, scenario) is None


def test_a_revoked_key_stops_working(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        await admin.create_user(pool, "alice")
        issued = await admin.issue_key(pool, "alice")
        store = PostgresKeyStore(pool)
        digest = hash_api_key(issued.plaintext)

        before = await store.lookup(digest)
        await admin.revoke_key(pool, issued.key_id)
        after = await store.lookup(digest)

        with pytest.raises(admin.AdminError, match="no active key"):
            await admin.revoke_key(pool, issued.key_id)  # already revoked
        return before, after

    before, after = with_pool(database_url, scenario)

    assert before is not None
    assert after is None


def test_a_deactivated_user_cannot_use_their_keys(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        await admin.create_user(pool, "alice")
        issued = await admin.issue_key(pool, "alice")
        await pool.execute("UPDATE users SET is_active = FALSE WHERE name = 'alice'")
        return await PostgresKeyStore(pool).lookup(hash_api_key(issued.plaintext))

    assert with_pool(database_url, scenario) is None


def test_issuing_a_key_for_a_missing_user_fails_cleanly(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> None:
        with pytest.raises(admin.AdminError, match="no user named"):
            await admin.issue_key(pool, "ghost")

    with_pool(database_url, scenario)


def test_key_options_and_multiple_keys_per_user(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        await admin.create_user(pool, "alice")
        private = await admin.issue_key(pool, "alice", cache_enabled=False)
        normal = await admin.issue_key(pool, "alice")
        store = PostgresKeyStore(pool)
        return (
            await store.lookup(hash_api_key(private.plaintext)),
            await store.lookup(hash_api_key(normal.plaintext)),
            await admin.list_keys(pool, "alice"),
        )

    private, normal, listed = with_pool(database_url, scenario)

    assert private is not None and normal is not None
    assert private.cache_enabled is False
    assert normal.cache_enabled is True
    assert private.user_id == normal.user_id
    assert private.key_id != normal.key_id
    assert [row["key_prefix"].startswith("llmgw_") for row in listed] == [True, True]
