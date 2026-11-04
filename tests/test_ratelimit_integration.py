"""The real Lua script against a real Redis. Skipped unless TEST_REDIS_URL is set.

Use a dedicated database (the URL must end in /15): these tests flush it.

    docker run -d --name test-redis -p 6379:6379 redis:7-alpine
    TEST_REDIS_URL=redis://localhost:6379/15 python -m pytest tests/test_ratelimit_integration.py -v
"""

import asyncio
import os
from collections.abc import Iterator

import pytest
import redis
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.auth.store import Principal
from app.core.ratelimit import KEY_PREFIX, RateLimiter
from app.core.redis_client import create_redis
from app.main import app

REDIS_URL = os.getenv("TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL is not set")


@pytest.fixture
def redis_url() -> Iterator[str]:
    assert REDIS_URL is not None
    if not REDIS_URL.rstrip("/").endswith("/15"):
        pytest.fail("TEST_REDIS_URL must use database 15; these tests flush it")

    admin = redis.Redis.from_url(REDIS_URL)
    admin.flushdb()
    yield REDIS_URL
    admin.flushdb()
    admin.close()


def test_a_burst_up_to_the_limit_is_allowed_then_refused(redis_url: str) -> None:
    async def scenario() -> list:  # type: ignore[type-arg]
        client = create_redis(redis_url, timeout_s=1.0)
        try:
            limiter = RateLimiter(client)
            return [await limiter.check(1, 5) for _ in range(7)]
        finally:
            await client.aclose()

    results = asyncio.run(scenario())

    assert [r.allowed for r in results] == [True] * 5 + [False] * 2
    assert [r.remaining for r in results[:5]] == [4, 3, 2, 1, 0]
    assert results[5].retry_after_s == pytest.approx(12, abs=0.5)  # 1 token at 5/min
    assert all(r.limit == 5 for r in results)


def test_each_user_has_an_independent_bucket(redis_url: str) -> None:
    async def scenario() -> tuple[bool, bool]:
        client = create_redis(redis_url, timeout_s=1.0)
        try:
            limiter = RateLimiter(client)
            for _ in range(3):
                await limiter.check(1, 3)  # user 1 uses their whole budget
            alice = await limiter.check(1, 3)
            bob = await limiter.check(2, 3)
            assert alice is not None and bob is not None
            return alice.allowed, bob.allowed
        finally:
            await client.aclose()

    alice_allowed, bob_allowed = asyncio.run(scenario())

    assert alice_allowed is False
    assert bob_allowed is True


def test_tokens_refill_over_time(redis_url: str) -> None:
    async def scenario() -> tuple[bool, int]:
        client = create_redis(redis_url, timeout_s=1.0)
        try:
            limiter = RateLimiter(client)
            for _ in range(60):  # one at a time: drain the bucket quickly
                await limiter.check(1, 60)
            drained = await limiter.check(1, 60)
            assert drained is not None

            await asyncio.sleep(1.1)  # 60 per minute = 1 token per second

            refilled = [await limiter.check(1, 60) for _ in range(3)]
            return drained.allowed, sum(1 for r in refilled if r and r.allowed)
        finally:
            await client.aclose()

    drained_allowed, allowed_after_wait = asyncio.run(scenario())

    assert drained_allowed is False
    assert 1 <= allowed_after_wait <= 2  # about 1, with timing slack


def test_idle_buckets_expire_instead_of_piling_up(redis_url: str) -> None:
    async def scenario() -> int:
        client = create_redis(redis_url, timeout_s=1.0)
        try:
            await RateLimiter(client).check(1, 60)
            return await client.pttl(f"{KEY_PREFIX}1")
        finally:
            await client.aclose()

    ttl_ms = asyncio.run(scenario())

    assert 0 < ttl_ms <= 61_000  # full refill time (60 s) plus a second of slack


def test_a_hundred_simultaneous_requests_never_over_admit(redis_url: str) -> None:
    async def scenario() -> list[bool]:
        client = create_redis(redis_url, timeout_s=2.0)
        try:
            limiter = RateLimiter(client)
            results = await asyncio.gather(*[limiter.check(1, 10) for _ in range(100)])
            return [r.allowed for r in results if r is not None]
        finally:
            await client.aclose()

    allowed = asyncio.run(scenario())

    assert len(allowed) == 100
    # The bucket holds 10. The only slack is refill during the burst itself
    # (10 per minute is 1 token per 6 seconds), so at most one extra gets in.
    assert 10 <= sum(allowed) <= 11


def test_two_gateway_instances_share_one_budget(redis_url: str) -> None:
    async def scenario() -> int:
        first = create_redis(redis_url, timeout_s=2.0)
        second = create_redis(redis_url, timeout_s=2.0)
        try:
            a, b = RateLimiter(first), RateLimiter(second)
            results = await asyncio.gather(
                *[a.check(1, 10) for _ in range(50)],
                *[b.check(1, 10) for _ in range(50)],
            )
            return sum(1 for r in results if r is not None and r.allowed)
        finally:
            await first.aclose()
            await second.aclose()

    assert 10 <= asyncio.run(scenario()) <= 11  # per user, not per instance


def test_gateway_end_to_end_returns_429_after_the_limit(
    monkeypatch: pytest.MonkeyPatch, redis_url: str
) -> None:
    monkeypatch.setenv("REDIS_URL", redis_url)
    three_per_minute = Principal(
        user_id=42,
        user_name="limited",
        key_id=1,
        rate_limit_per_min=3,
        daily_token_limit=None,
        daily_usd_limit=None,
        cache_enabled=False,
    )
    monkeypatch.setitem(
        app.dependency_overrides, authenticate, lambda: three_per_minute
    )
    body = {"model": "chat-test", "messages": [{"role": "user", "content": "hi"}]}

    with TestClient(app) as client:
        responses = [client.post("/v1/chat/completions", json=body) for _ in range(5)]

    assert [r.status_code for r in responses] == [200, 200, 200, 429, 429]
    assert [r.headers["x-ratelimit-remaining"] for r in responses[:3]] == [
        "2",
        "1",
        "0",
    ]
    assert int(responses[3].headers["retry-after"]) >= 1
