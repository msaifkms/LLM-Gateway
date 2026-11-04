"""Quotas, limits and the kill switch against a real Postgres.

Skipped unless TEST_DATABASE_URL is set (the database name must end in _test).
"""

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.auth import admin
from app.core.killswitch import KillSwitch, PostgresKillSwitchStore
from app.db.migrate import apply_migrations
from app.db.pool import create_db_pool
from app.main import app
from app.usage.store import PostgresUsageStore, UsageEvent

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


def event(
    user_id: int,
    key_id: int,
    *,
    status: str = "MISS",
    prompt: int = 600,
    completion: int = 400,
    cost: str = "0.5",
) -> UsageEvent:
    return UsageEvent(
        user_id=user_id,
        api_key_id=key_id,
        route="r",
        provider="p",
        model="gpt-4.1-mini",
        cache_status=status,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=Decimal(cost),
        list_cost_usd=Decimal(cost),
        priced=True,
    )


async def make_user(pool: asyncpg.Pool, name: str, **limits: object) -> tuple[int, int]:
    user_id = await admin.create_user(pool, name, **limits)  # type: ignore[arg-type]
    return user_id, (await admin.issue_key(pool, name)).key_id


# --- usage today -----------------------------------------------------------------


def test_usage_today_counts_only_calls_that_were_paid_for(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        user_id, key_id = await make_user(pool, "alice")
        store = PostgresUsageStore(pool)
        await store.record(event(user_id, key_id))  # MISS: 1,000 tokens, $0.5
        await store.record(
            event(
                user_id, key_id, status="BYPASS", prompt=100, completion=0, cost="0.1"
            )
        )
        await store.record(event(user_id, key_id, status="HIT", cost="0"))  # free
        await store.record(event(user_id, key_id, status="COALESCED", cost="0"))  # free
        return await store.usage_today(user_id)

    today = with_pool(database_url, scenario)

    assert today.billed_tokens == 1_100  # the HIT and COALESCED tokens are not counted
    assert today.cost_usd == Decimal("0.6")  # the MISS (0.5) plus the BYPASS (0.1)


def test_usage_today_ignores_yesterday_and_other_users(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        alice, alice_key = await make_user(pool, "alice")
        bob, bob_key = await make_user(pool, "bob")
        store = PostgresUsageStore(pool)
        await store.record(event(alice, alice_key))
        await store.record(event(bob, bob_key, prompt=9_000, completion=0, cost="9"))
        # One second before today's 00:00 UTC began: belongs to yesterday.
        await pool.execute(
            "INSERT INTO usage_events (user_id, api_key_id, created_at, route, provider,"
            " model, cache_status, prompt_tokens, completion_tokens, cost_usd,"
            " list_cost_usd, priced) VALUES ($1, $2,"
            " date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            "   - interval '1 second', 'r', 'p', 'm', 'MISS', 5000, 5000, 7, 7, true)",
            alice,
            alice_key,
        )
        return await store.usage_today(alice)

    today = with_pool(database_url, scenario)

    assert today.billed_tokens == 1_000
    assert today.cost_usd == Decimal("0.5")


def test_a_user_with_no_usage_today_has_zero(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        user_id, _ = await make_user(pool, "alice")
        return await PostgresUsageStore(pool).usage_today(user_id)

    today = with_pool(database_url, scenario)

    assert today.billed_tokens == 0
    assert today.cost_usd == 0


# --- changing limits ---------------------------------------------------------------


def test_set_limits_changes_and_clears_limits(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        await admin.create_user(
            pool, "alice", daily_token_limit=500, daily_usd_limit=Decimal(1)
        )
        await admin.set_limits(
            pool, "alice", rate_limit_per_min=5, daily_token_limit=None
        )
        return await pool.fetchrow(
            "SELECT rate_limit_per_min, daily_token_limit, daily_usd_limit "
            "FROM users WHERE name = 'alice'"
        )

    row = with_pool(database_url, scenario)

    assert row["rate_limit_per_min"] == 5
    assert row["daily_token_limit"] is None  # removed
    assert row["daily_usd_limit"] == Decimal(1)  # not mentioned: untouched


def test_set_limits_rejects_unknown_users_empty_changes_and_bad_columns(
    database_url: str,
) -> None:
    async def scenario(pool: asyncpg.Pool) -> None:
        await admin.create_user(pool, "alice")
        with pytest.raises(admin.AdminError, match="no user named"):
            await admin.set_limits(pool, "ghost", rate_limit_per_min=5)
        with pytest.raises(admin.AdminError, match="nothing to change"):
            await admin.set_limits(pool, "alice")
        with pytest.raises(ValueError, match="unknown limits"):
            await admin.set_limits(pool, "alice", is_active=False)
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.set_limits(pool, "alice", rate_limit_per_min=0)

    with_pool(database_url, scenario)


# --- kill switch -------------------------------------------------------------------


def test_the_kill_switch_defaults_to_off_and_can_be_flipped(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        flag = PostgresKillSwitchStore(pool)
        before = await flag.get()
        await flag.set(True, "provider incident")
        during = await flag.get()
        await flag.set(False)
        after = await flag.get()
        return before, during, after

    before, during, after = with_pool(database_url, scenario)

    assert before.enabled is False  # no row yet means off
    assert (during.enabled, during.reason) == (True, "provider incident")
    assert after.enabled is False


def test_a_kill_switch_reads_the_real_flag(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        flag = PostgresKillSwitchStore(pool)
        off = await KillSwitch(flag).is_on()
        await flag.set(True)
        on = await KillSwitch(flag).is_on()  # a fresh instance: no cached answer
        return off, on

    assert with_pool(database_url, scenario) == (False, True)


# --- the whole gateway ---------------------------------------------------------------


async def _plain_key(pool: asyncpg.Pool, user: str) -> str:
    return (await admin.issue_key(pool, user)).plaintext


def test_gateway_end_to_end_quota_and_kill_switch(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    async def setup(pool: asyncpg.Pool) -> tuple[str, str]:
        capped, capped_key = await make_user(pool, "capped", daily_token_limit=1_000)
        await PostgresUsageStore(pool).record(event(capped, capped_key))  # 1,000 used
        await admin.create_user(pool, "free")
        return await _plain_key(pool, "capped"), await _plain_key(pool, "free")

    capped_key, free_key = with_pool(database_url, setup)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delitem(app.dependency_overrides, authenticate)  # real auth
    body = {"model": "mock-1", "messages": [{"role": "user", "content": "hi"}]}

    def call(client: TestClient, key: str):  # type: ignore[no-untyped-def]
        return client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )

    with TestClient(app) as client:
        capped = call(client, capped_key)
        free = call(client, free_key)

    assert capped.status_code == 429
    assert capped.json() == {"detail": "Daily token quota exceeded"}
    assert free.status_code == 200  # someone else's quota does not matter

    async def pause() -> None:
        pool = await create_db_pool(database_url, max_size=2)
        try:
            await PostgresKillSwitchStore(pool).set(True, "test")
        finally:
            await pool.close()

    asyncio.run(pause())

    with TestClient(app) as client:  # a fresh gateway process reads the flag
        paused = call(client, free_key)

    assert paused.status_code == 503
    assert paused.json() == {"detail": "Service temporarily disabled"}
