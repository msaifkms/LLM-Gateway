"""Usage rows and summaries against a real Postgres.

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
    model: str = "gpt-4.1-mini",
    status: str = "MISS",
    prompt: int = 1_000,
    completion: int = 500,
    cost: str = "0.00120000",
    list_cost: str = "0.00120000",
) -> UsageEvent:
    return UsageEvent(
        user_id=user_id,
        api_key_id=key_id,
        route="chat-default",
        provider="cache" if status == "HIT" else "openai-default",
        model=model,
        cache_status=status,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=Decimal(cost),
        list_cost_usd=Decimal(list_cost),
        priced=True,
    )


async def make_user(pool: asyncpg.Pool, name: str) -> tuple[int, int]:
    user_id = await admin.create_user(pool, name)
    issued = await admin.issue_key(pool, name)
    return user_id, issued.key_id


def test_the_usage_migration_creates_the_table(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> list[str]:
        rows = await pool.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'usage_events'"
        )
        return [r["column_name"] for r in rows]

    columns = with_pool(database_url, scenario)

    assert {
        "user_id",
        "api_key_id",
        "cost_usd",
        "list_cost_usd",
        "cache_status",
    } <= set(columns)


def test_a_summary_adds_up_totals_models_and_days(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        user_id, key_id = await make_user(pool, "alice")
        store = PostgresUsageStore(pool)
        await store.record(event(user_id, key_id))
        await store.record(
            event(user_id, key_id, status="HIT", cost="0", list_cost="0.0012")
        )
        await store.record(
            event(
                user_id,
                key_id,
                model="claude-haiku-5-5",
                status="COALESCED",
                prompt=2_000,
                completion=100,
                cost="0",
                list_cost="0.00025",
            )
        )
        return await store.summary(user_id, 7)

    summary = with_pool(database_url, scenario)

    totals = summary.totals
    assert totals.requests == 3
    assert totals.cache_served == 2  # the HIT and the COALESCED
    assert totals.prompt_tokens == 4_000
    assert totals.completion_tokens == 1_100
    assert totals.cost_usd == Decimal("0.0012")  # only the MISS cost money
    assert totals.saved_usd == Decimal(
        "0.00145"
    )  # what the two free ones would have cost
    assert [(m.model, m.requests) for m in summary.by_model] == [
        ("gpt-4.1-mini", 2),
        ("claude-haiku-5-5", 1),
    ]  # most expensive first
    assert len(summary.by_day) == 1
    assert summary.by_day[0].requests == 3


def test_a_user_only_sees_their_own_usage(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        alice, alice_key = await make_user(pool, "alice")
        bob, bob_key = await make_user(pool, "bob")
        store = PostgresUsageStore(pool)
        await store.record(event(alice, alice_key))
        await store.record(event(bob, bob_key, cost="5", list_cost="5"))
        return await store.summary(alice, 7), await store.summary(bob, 7)

    alice_summary, bob_summary = with_pool(database_url, scenario)

    assert alice_summary.totals.cost_usd == Decimal("0.0012")
    assert bob_summary.totals.cost_usd == Decimal(5)


def test_the_window_excludes_older_rows(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        user_id, key_id = await make_user(pool, "alice")
        store = PostgresUsageStore(pool)
        await store.record(event(user_id, key_id))
        await pool.execute(
            "INSERT INTO usage_events (user_id, api_key_id, created_at, route, provider,"
            " model, cache_status, prompt_tokens, completion_tokens, cost_usd,"
            " list_cost_usd, priced) VALUES ($1, $2, now() - interval '10 days', 'r', 'p',"
            " 'm', 'MISS', 1, 1, 1, 1, true)",
            user_id,
            key_id,
        )
        return await store.summary(user_id, 7), await store.summary(user_id, 30)

    week, month = with_pool(database_url, scenario)

    assert week.totals.requests == 1
    assert month.totals.requests == 2


def test_a_user_with_no_usage_gets_zeros(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        user_id, _ = await make_user(pool, "alice")
        return await PostgresUsageStore(pool).summary(user_id, 7)

    summary = with_pool(database_url, scenario)

    assert summary.totals.requests == 0
    assert summary.totals.cost_usd == 0
    assert summary.by_model == []
    assert summary.by_day == []


def test_the_database_rejects_nonsense_rows(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> None:
        user_id, key_id = await make_user(pool, "alice")
        store = PostgresUsageStore(pool)
        bad_status = event(user_id, key_id, status="WEIRD")
        with pytest.raises(asyncpg.CheckViolationError):
            await store.record(bad_status)
        with pytest.raises(asyncpg.CheckViolationError):
            await store.record(event(user_id, key_id, cost="-1"))

    with_pool(database_url, scenario)


def test_deleting_a_user_deletes_their_usage(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> int:
        user_id, key_id = await make_user(pool, "alice")
        await PostgresUsageStore(pool).record(event(user_id, key_id))
        await pool.execute("DELETE FROM users WHERE id = $1", user_id)
        return int(await pool.fetchval("SELECT count(*) FROM usage_events"))

    assert with_pool(database_url, scenario) == 0


def test_gateway_end_to_end_chat_then_usage(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    async def setup(pool: asyncpg.Pool) -> str:
        await admin.create_user(pool, "alice")
        return (await admin.issue_key(pool, "alice")).plaintext

    key = with_pool(database_url, setup)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delitem(app.dependency_overrides, authenticate)  # use real auth
    auth = {"Authorization": f"Bearer {key}"}
    body = {"model": "mock-1", "messages": [{"role": "user", "content": "hi"}]}

    with TestClient(app) as client:
        chat = client.post("/v1/chat/completions", json=body, headers=auth)
        usage = client.get("/v1/usage", headers=auth)
        anonymous = client.get("/v1/usage")

    assert chat.status_code == 200
    assert usage.status_code == 200
    report = usage.json()
    assert report["totals"]["requests"] == 1
    assert report["totals"]["prompt_tokens"] == chat.json()["usage"]["prompt_tokens"]
    assert len(report["by_model"]) == 1
    assert anonymous.status_code == 401
