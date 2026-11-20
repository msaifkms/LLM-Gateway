"""Eval runs against a real Postgres (and Redis, if TEST_REDIS_URL is set).

Skipped unless TEST_DATABASE_URL is set (the database name must end in _test).
"""

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal

import asyncpg
import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.auth import admin
from app.db.migrate import apply_migrations
from app.db.pool import create_db_pool
from app.evals.cli import build_parser, run_with_database
from app.evals.report import RunInfo
from app.evals.runner import CaseResult
from app.evals.store import PostgresEvalStore
from app.main import app, lifespan

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
REDIS_URL = os.getenv("TEST_REDIS_URL")

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
    async def runner() -> T:
        pool = await create_db_pool(url, max_size=3)
        try:
            await apply_migrations(pool)
            return await scenario(pool)
        finally:
            await pool.close()

    return asyncio.run(runner())


def info(dataset: str = "factual_qa", model: str = "chat-default") -> RunInfo:
    return RunInfo(
        dataset=dataset,
        model=model,
        dataset_sha256="a" * 64,
        judge_model="chat-judge",
        batch_id="batch-1",
        settings={"concurrency": 2, "no_cache": True},
    )


def result(case_id: str, passed: bool = True, **kw: object) -> CaseResult:
    fields: dict[str, object] = {
        "score": 1.0 if passed else 0.0,
        "tags": ["geo"],
        "latency_ms": 123.5,
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "cost_usd": Decimal("0.00001200"),
        "judge_tokens": 50,
        "judge_cost_usd": Decimal("0.00000500"),
        "cache_status": "BYPASS",
        "provider": "openai-default",
        "response_model": "gpt-4.1-mini-2025-04-14",
        "request_id": f"eval-r1-{case_id}",
        "output": "Paris",
        "reason": "" if passed else "got 'Lyon'",
        **kw,
    }
    return CaseResult(case_id=case_id, scorer="normalized", passed=passed, **fields)  # type: ignore[arg-type]


def test_the_migration_creates_the_eval_tables(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> set[str]:
        rows = await pool.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        return {row["tablename"] for row in rows}

    assert {"eval_runs", "eval_results"} <= with_pool(database_url, scenario)


def test_a_run_and_its_results_round_trip(database_url: str) -> None:
    results = [result("capital"), result("gold", passed=False, error=None)]

    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        store = PostgresEvalStore(pool)
        created = await store.create_run(info())
        finished = await store.finish_run(created.id, "completed", results)
        return (
            created,
            finished,
            await store.get_run(created.id),
            await store.get_results(created.id),
        )

    created, finished, loaded, stored = with_pool(database_url, scenario)

    assert created.id is not None and created.status == "running"
    assert created.started_at is not None and created.finished_at is None
    assert finished.status == "completed"
    assert finished.finished_at is not None
    assert loaded == finished
    assert loaded.settings == {"concurrency": 2, "no_cache": True}
    assert (loaded.judge_model, loaded.batch_id) == ("chat-judge", "batch-1")
    assert stored == results  # every field survives, in order


def test_a_run_cannot_be_finished_twice(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> int:
        store = PostgresEvalStore(pool)
        run = await store.create_run(info())
        await store.finish_run(run.id, "completed", [result("a")])
        with pytest.raises(ValueError, match="already finished"):
            await store.finish_run(run.id, "completed", [result("b")])
        # The failed second finish wrote nothing (one transaction).
        return int(await pool.fetchval("SELECT count(*) FROM eval_results"))

    assert with_pool(database_url, scenario) == 1


def test_the_database_rejects_nonsense(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> None:
        store = PostgresEvalStore(pool)
        run = await store.create_run(info())
        with pytest.raises(asyncpg.CheckViolationError):
            await store.finish_run(run.id, "completed", [result("a", score=1.5)])
        with pytest.raises(asyncpg.CheckViolationError):  # an error cannot pass
            await store.finish_run(run.id, "completed", [result("a", error="boom")])
        with pytest.raises(asyncpg.UniqueViolationError):
            await store.finish_run(run.id, "completed", [result("a"), result("a")])
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE eval_runs SET status = 'weird'")
        with pytest.raises(asyncpg.CheckViolationError):  # finished needs a time
            await pool.execute("UPDATE eval_runs SET status = 'completed'")

    with_pool(database_url, scenario)


def test_list_runs_counts_results_newest_first(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        store = PostgresEvalStore(pool)
        first = await store.create_run(info("factual_qa"))
        await store.finish_run(first.id, "completed", [result("a"), result("b", False)])
        second = await store.create_run(info("json_output"))
        await store.create_run(info("factual_qa"))  # still running, no results
        return (
            await store.list_runs(),
            await store.list_runs(dataset="factual_qa", limit=1),
            second.id,
        )

    everything, latest_factual, second_id = with_pool(database_url, scenario)

    assert [(r.info.id, r.cases, r.passed) for r in everything] == [
        (3, 0, 0),
        (second_id, 0, 0),
        (1, 2, 1),
    ]
    assert [(r.info.id, r.info.status) for r in latest_factual] == [(3, "running")]


def test_latest_scores_take_the_newest_completed_run_per_dataset_and_model(
    database_url: str,
) -> None:
    async def scenario(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        store = PostgresEvalStore(pool)
        old = await store.create_run(info("factual_qa", "m1"))
        await store.finish_run(old.id, "completed", [result("a", False)])
        new = await store.create_run(info("factual_qa", "m1"))
        await store.finish_run(
            new.id, "completed", [result("a"), result("b", False, score=0.5)]
        )
        failed = await store.create_run(info("factual_qa", "m1"))
        await store.finish_run(failed.id, "failed", [])
        other = await store.create_run(info("json_output", "m2"))
        await store.finish_run(other.id, "completed", [result("x")])
        await store.create_run(info("running_set", "m3"))
        stale = await store.create_run(info("stale_set", "m1"))
        await store.finish_run(stale.id, "completed", [result("x")])
        await pool.execute(
            "UPDATE eval_runs SET started_at = now() - interval '40 days', "
            "finished_at = now() - interval '40 days' WHERE id = $1",
            stale.id,
        )
        return await store.latest_scores()

    scores = with_pool(database_url, scenario)

    by_key = {(s.dataset, s.model): s for s in scores}
    assert set(by_key) == {("factual_qa", "m1"), ("json_output", "m2")}
    latest = by_key[("factual_qa", "m1")]
    assert latest.pass_rate == 0.5  # the newer run, not the failed or the old one
    assert latest.mean_score == 0.75
    assert by_key[("json_output", "m2")].pass_rate == 1.0


def test_deleting_a_run_deletes_its_results(database_url: str) -> None:
    async def scenario(pool: asyncpg.Pool) -> int:
        store = PostgresEvalStore(pool)
        run = await store.create_run(info())
        await store.finish_run(run.id, "completed", [result("a")])
        await pool.execute("DELETE FROM eval_runs WHERE id = $1", run.id)
        return int(await pool.fetchval("SELECT count(*) FROM eval_results"))

    assert with_pool(database_url, scenario) == 0


def test_an_eval_runs_through_the_real_gateway_and_is_billed_like_traffic(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real auth, rate limiting (with Redis), usage rows, stored results, /metrics."""

    async def setup(pool: asyncpg.Pool) -> str:
        await admin.create_user(pool, "evals", rate_limit_per_min=600)
        return (await admin.issue_key(pool, "evals")).plaintext

    key = with_pool(database_url, setup)
    monkeypatch.setenv("DATABASE_URL", database_url)
    if REDIS_URL:
        monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.delitem(app.dependency_overrides, authenticate)  # use real auth

    async def run(*argv: str) -> int:
        args = build_parser().parse_args(argv)
        async with lifespan(app):
            transport = httpx.ASGITransport(app=app)
            return await run_with_database(args, database_url, transport)

    common = ["--dataset", "mock_smoke", "--model", "mock-1", "--key", key]
    assert asyncio.run(run("run", *common, "--no-cache", "--min-pass-rate", "1")) == 0
    assert asyncio.run(run("run", *common, "--concurrency", "1")) == 0
    assert asyncio.run(run("compare", "1", "2", "--fail-on-regression")) == 0
    assert asyncio.run(run("report", "2")) == 0
    out = capsys.readouterr().out
    assert "No regressions." in out
    assert "Eval: mock_smoke on mock-1" in out

    bad_key = ["--dataset", "mock_smoke", "--model", "mock-1", "--key", "llmgw_wrong"]
    assert asyncio.run(run("run", *bad_key)) == 1
    assert "HTTP 401" in capsys.readouterr().err

    async def check(pool: asyncpg.Pool):  # type: ignore[no-untyped-def]
        runs = await pool.fetch("SELECT id, status FROM eval_runs ORDER BY id")
        results = await pool.fetchval("SELECT count(*) FROM eval_results")
        usage = await pool.fetch(
            "SELECT cache_status, count(*) AS n FROM usage_events "
            "GROUP BY cache_status ORDER BY cache_status"
        )
        return (
            [tuple(r) for r in runs],
            results,
            {r["cache_status"]: r["n"] for r in usage},
        )

    runs, results, usage = with_pool(database_url, check)

    assert runs == [(1, "completed"), (2, "completed"), (3, "failed")]
    assert results == 12
    # Every eval request was billed like real traffic: 12 usage rows.
    assert sum(usage.values()) == 12
    if REDIS_URL:
        assert usage["BYPASS"] == 6  # the no-cache run
        assert usage.get("MISS", 0) + usage.get("HIT", 0) == 6
    else:
        assert usage == {"BYPASS": 12}  # no Redis: nothing is cached

    with TestClient(app) as client:
        body = client.get("/metrics").text
    assert 'gateway_eval_pass_rate{dataset="mock_smoke",model="mock-1"} 1.0' in body


def test_runs_are_owned_and_scoped_to_their_user(database_url: str) -> None:
    from app.auth.keys import hash_api_key

    async def scenario(pool: asyncpg.Pool) -> dict[str, object]:
        await admin.create_user(pool, "alice")
        await admin.create_user(pool, "bob")
        alice_key = await admin.issue_key(pool, "alice")
        store = PostgresEvalStore(pool)
        alice = await store.owner_of_key(hash_api_key(alice_key.plaintext))
        bob = await pool.fetchval("SELECT id FROM users WHERE name = 'bob'")
        assert alice is not None

        alices = await store.create_run(replace_user(info(), alice))
        await store.finish_run(alices.id or 0, "completed", [result("a")])
        bobs = await store.create_run(replace_user(info("json_output"), bob))
        unowned = await store.create_run(info("mock_smoke"))

        await admin.revoke_key(pool, alice_key.key_id)
        return {
            "alice_runs": await store.list_runs_for_user(alice),
            "bob_runs": await store.list_runs_for_user(bob),
            "alice_reads_bobs": await store.get_run_for_user(alice, bobs.id or 0),
            "alice_reads_own": await store.get_run_for_user(alice, alices.id or 0),
            "nobody_reads_unowned": await store.get_run_for_user(
                alice, unowned.id or 0
            ),
            "admin_sees": len(await store.list_runs()),
            "revoked_key_owner": await store.owner_of_key(
                hash_api_key(alice_key.plaintext)
            ),
            "unknown_key_owner": await store.owner_of_key(hash_api_key("llmgw_x")),
        }

    out = with_pool(database_url, scenario)

    alice_runs = out["alice_runs"]
    assert isinstance(alice_runs, list)
    assert [(r.info.dataset, r.cases, r.passed) for r in alice_runs] == [
        ("factual_qa", 1, 1)
    ]
    assert isinstance(out["bob_runs"], list)
    assert [r.info.dataset for r in out["bob_runs"]] == ["json_output"]
    assert out["alice_reads_bobs"] is None
    assert out["alice_reads_own"] is not None
    assert out["nobody_reads_unowned"] is None
    assert out["admin_sees"] == 3  # the CLI still lists everything
    assert out["revoked_key_owner"] is None  # same rules as authentication
    assert out["unknown_key_owner"] is None


def replace_user(run: RunInfo, user_id: int) -> RunInfo:
    from dataclasses import replace

    return replace(run, user_id=user_id)
