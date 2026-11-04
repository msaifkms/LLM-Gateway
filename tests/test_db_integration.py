"""Tests against a real Postgres. Skipped unless TEST_DATABASE_URL is set.

The database name must end in "_test": these tests DROP the public schema.

    docker run -d --name test-postgres -p 5433:5432 \
      -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gateway_test postgres:16-alpine
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/gateway_test \
      python -m pytest tests/test_db_integration.py -v
"""

import asyncio
import os
from collections.abc import Iterator

import asyncpg
import pytest

from app.db.migrate import apply_migrations
from app.db.pool import check_db, create_db_pool

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


def test_real_postgres_is_reachable(database_url: str) -> None:
    async def scenario() -> bool:
        pool = await create_db_pool(database_url, max_size=2)
        try:
            return await check_db(pool)
        finally:
            await pool.close()

    assert asyncio.run(scenario()) is True


def test_migrations_create_tables_and_are_idempotent(database_url: str) -> None:
    async def scenario() -> tuple[list[str], list[str], set[str]]:
        pool = await create_db_pool(database_url, max_size=2)
        try:
            first = await apply_migrations(pool)
            second = await apply_migrations(pool)
            rows = await pool.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            return first, second, {row["tablename"] for row in rows}
        finally:
            await pool.close()

    first, second, tables = asyncio.run(scenario())

    assert first == [
        "001_users_and_api_keys",
        "002_usage_events",
        "003_gateway_flags",
    ]
    assert second == []  # already applied: nothing to do
    assert {"users", "api_keys", "usage_events", "gateway_flags"} <= tables


def test_two_instances_migrating_at_once_apply_each_file_once(
    database_url: str,
) -> None:
    async def scenario() -> list[list[str]]:
        pool = await create_db_pool(database_url, max_size=4)
        try:
            results = await asyncio.gather(
                apply_migrations(pool), apply_migrations(pool)
            )
            return list(results)
        finally:
            await pool.close()

    results = asyncio.run(scenario())

    assert sorted(results) == [
        [],
        ["001_users_and_api_keys", "002_usage_events", "003_gateway_flags"],
    ]


def test_constraints_protect_the_data(database_url: str) -> None:
    async def scenario() -> tuple[int, int]:
        pool = await create_db_pool(database_url, max_size=2)
        try:
            await apply_migrations(pool)
            user_id = await pool.fetchval(
                "INSERT INTO users (name) VALUES ('alice') RETURNING id"
            )
            insert_key = (
                "INSERT INTO api_keys (user_id, key_prefix, key_hash) "
                "VALUES ($1, 'llmgw_ab', $2)"
            )
            await pool.execute(insert_key, user_id, "hash-1")

            with pytest.raises(asyncpg.UniqueViolationError):
                await pool.execute(insert_key, user_id, "hash-1")  # same hash twice
            with pytest.raises(asyncpg.UniqueViolationError):
                await pool.execute("INSERT INTO users (name) VALUES ('alice')")
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(
                    "INSERT INTO users (name, rate_limit_per_min) VALUES ('bob', 0)"
                )

            keys_before = await pool.fetchval("SELECT count(*) FROM api_keys")
            await pool.execute("DELETE FROM users WHERE id = $1", user_id)
            keys_after = await pool.fetchval("SELECT count(*) FROM api_keys")
            return keys_before, keys_after
        finally:
            await pool.close()

    keys_before, keys_after = asyncio.run(scenario())

    assert keys_before == 1
    assert keys_after == 0  # deleting a user deletes their keys


def test_new_users_get_safe_defaults(database_url: str) -> None:
    async def scenario() -> asyncpg.Record:
        pool = await create_db_pool(database_url, max_size=2)
        try:
            await apply_migrations(pool)
            return await pool.fetchrow(
                "INSERT INTO users (name) VALUES ('carol') RETURNING *"
            )
        finally:
            await pool.close()

    user = asyncio.run(scenario())

    assert user["rate_limit_per_min"] == 60
    assert user["daily_token_limit"] is None
    assert user["daily_usd_limit"] is None
    assert user["is_active"] is True
