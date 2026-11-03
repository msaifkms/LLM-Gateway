"""Tests against a real Redis. Skipped unless TEST_REDIS_URL is set.

Use a dedicated database (the URL must end in /15): these tests flush it.

    docker run -d --name test-redis -p 6379:6379 redis:7-alpine
    TEST_REDIS_URL=redis://localhost:6379/15 python -m pytest tests/test_redis_integration.py -v
"""

import asyncio
import os
from collections.abc import Iterator

import pytest
import redis
from fastapi.testclient import TestClient

from app.api.schemas import (
    ChatChoice,
    ChatCompletionResponse,
    ChatMessage,
    TokenUsage,
)
from app.core.cache import ResponseCache
from app.core.redis_client import check_redis, create_redis
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


def make_response() -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-real",
        created=1,
        model="mock-1",
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content="stored for real"),
                finish_reason="stop",
            )
        ],
        usage=TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
    )


def test_real_redis_is_reachable(redis_url: str) -> None:
    async def scenario() -> bool:
        client = create_redis(redis_url, timeout_s=1.0)
        try:
            return await check_redis(client)
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) is True


def test_round_trip_and_ttl_with_real_redis(redis_url: str) -> None:
    async def scenario() -> tuple[ChatCompletionResponse | None, int]:
        client = create_redis(redis_url, timeout_s=1.0)
        try:
            cache = ResponseCache(client, ttl_s=100)
            await cache.set("llmgw:test:key", make_response())
            return await cache.get("llmgw:test:key"), await client.ttl("llmgw:test:key")
        finally:
            await client.aclose()

    stored, ttl = asyncio.run(scenario())

    assert stored == make_response()
    assert 0 < ttl <= 100


def test_entries_really_expire(redis_url: str) -> None:
    async def scenario() -> ChatCompletionResponse | None:
        client = create_redis(redis_url, timeout_s=1.0)
        try:
            cache = ResponseCache(client, ttl_s=1)
            await cache.set("llmgw:test:short", make_response())
            await asyncio.sleep(1.5)
            return await cache.get("llmgw:test:short")
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) is None


def test_gateway_end_to_end_miss_then_hit(
    monkeypatch: pytest.MonkeyPatch, redis_url: str
) -> None:
    monkeypatch.setenv("REDIS_URL", redis_url)
    body = {
        "model": "chat-test",
        "messages": [{"role": "user", "content": "Hello!"}],
        "temperature": 0,
    }

    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", json=body)
        second = client.post("/v1/chat/completions", json=body)
        forced = client.post(
            "/v1/chat/completions", json=body, headers={"Cache-Control": "no-store"}
        )

    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert second.json() == first.json()
    assert forced.headers["x-cache"] == "BYPASS"
