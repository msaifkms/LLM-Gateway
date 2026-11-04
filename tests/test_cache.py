import asyncio
import logging

import pytest
from fake_redis import FakeRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api.schemas import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    TokenUsage,
)
from app.core.cache import (
    KEY_PREFIX,
    ResponseCache,
    build_cache_key,
    is_cacheable,
    wants_no_store,
)
from app.core.routes import CacheMode, Route


def make_request(
    content: str = "Hi",
    *,
    model: str = "mock-1",
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=content)],
        temperature=temperature,
        max_tokens=max_tokens,
    )


def make_response(text: str = "cached answer") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-1",
        created=1,
        model="mock-1",
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content=text),
                finish_reason="stop",
            )
        ],
        usage=TokenUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7),
    )


# --- cache key -------------------------------------------------------------

# --- cache key -------------------------------------------------------------

USER = 1


def test_key_is_stable_and_well_formed() -> None:
    key = build_cache_key(make_request(), USER)

    assert key == build_cache_key(make_request(), USER)
    assert key.startswith(KEY_PREFIX)
    assert len(key.removeprefix(KEY_PREFIX)) == 64  # SHA-256 hex digest


@pytest.mark.parametrize(
    "other",
    [
        make_request("Different prompt"),
        make_request(model="openai-default"),
        make_request(temperature=0.5),
        make_request(max_tokens=100),
    ],
    ids=["messages", "model-alias", "temperature", "max-tokens"],
)
def test_anything_that_shapes_the_answer_changes_the_key(
    other: ChatCompletionRequest,
) -> None:
    assert build_cache_key(make_request(), USER) != build_cache_key(other, USER)


def test_different_users_never_share_a_key() -> None:
    same_request = make_request("What is my account balance?")

    assert build_cache_key(same_request, 1) != build_cache_key(same_request, 2)


def test_message_order_and_roles_matter() -> None:
    a = ChatCompletionRequest(
        model="m",
        messages=[
            ChatMessage(role="system", content="Be brief."),
            ChatMessage(role="user", content="Hi"),
        ],
    )
    b = ChatCompletionRequest(
        model="m",
        messages=[
            ChatMessage(role="user", content="Hi"),
            ChatMessage(role="system", content="Be brief."),
        ],
    )

    assert build_cache_key(a, USER) != build_cache_key(b, USER)


def test_key_handles_non_ascii_text() -> None:
    assert build_cache_key(make_request("héllo 你好"), USER) != build_cache_key(
        make_request("hello"), USER
    )


# --- what is safe to cache -------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "targets", "temperature", "expected"),
    [
        (CacheMode.AUTO, ("mock-1",), 0.0, True),
        (CacheMode.AUTO, ("openai-default",), 0.0, True),
        (CacheMode.AUTO, ("mock-1",), 1.0, False),
        (CacheMode.AUTO, ("mock-1",), 0.2, False),
        # Anthropic ignores temperature, so temperature 0 proves nothing.
        (CacheMode.AUTO, ("anthropic-default",), 0.0, False),
        (CacheMode.AUTO, ("anthropic-default", "openai-default"), 0.0, False),
        # Explicit opt-in still requires temperature 0.
        (CacheMode.TEMPERATURE_ZERO, ("anthropic-default",), 0.0, True),
        (CacheMode.TEMPERATURE_ZERO, ("anthropic-default",), 1.0, False),
        (CacheMode.NEVER, ("mock-1",), 0.0, False),
    ],
)
def test_is_cacheable(
    mode: CacheMode,
    targets: tuple[str, ...],
    temperature: float,
    expected: bool,
) -> None:
    route = Route(targets=targets, cache=mode)

    assert is_cacheable(make_request(temperature=temperature), route) is expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, False),
        ("", False),
        ("no-store", True),
        ("No-Store", True),
        ("max-age=0, no-store", True),
        (" no-cache , no-store ", True),
        ("no-cache", False),
        ("max-age=60", False),
        ("no-store-ish", False),
    ],
)
def test_wants_no_store(header: str | None, expected: bool) -> None:
    assert wants_no_store(header) is expected


# --- Redis-backed store ----------------------------------------------------


def make_cache(fake: FakeRedis, ttl_s: int = 3600) -> ResponseCache:
    return ResponseCache(fake, ttl_s=ttl_s)  # type: ignore[arg-type]


def test_miss_returns_none() -> None:
    cache = make_cache(FakeRedis())

    assert asyncio.run(cache.get("absent")) is None


def test_set_then_get_round_trips_and_applies_the_ttl() -> None:
    fake = FakeRedis()
    cache = make_cache(fake, ttl_s=120)

    async def scenario() -> ChatCompletionResponse | None:
        await cache.set("k", make_response())
        return await cache.get("k")

    assert asyncio.run(scenario()) == make_response()
    assert fake.ttls["k"] == 120


def test_corrupt_entry_is_treated_as_a_miss(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeRedis()
    fake.store["k"] = b"{not valid json"
    cache = make_cache(fake)

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(cache.get("k")) is None

    assert "unreadable" in caplog.text


def test_wrong_shape_entry_is_treated_as_a_miss() -> None:
    fake = FakeRedis()
    fake.store["k"] = b'{"unexpected": true}'

    assert asyncio.run(make_cache(fake).get("k")) is None


def test_redis_errors_are_swallowed() -> None:
    fake = FakeRedis()
    fake.fail_with = RedisConnectionError("down")
    cache = make_cache(fake)

    async def scenario() -> ChatCompletionResponse | None:
        await cache.set("k", make_response())  # must not raise
        return await cache.get("k")

    assert asyncio.run(scenario()) is None


def test_outage_is_logged_once_not_per_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeRedis()
    fake.fail_with = OSError("network down")
    cache = make_cache(fake)

    async def scenario() -> None:
        for _ in range(5):
            await cache.get("k")

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())

    assert caplog.text.count("unavailable") == 1


def test_cache_recovers_after_an_outage() -> None:
    fake = FakeRedis()
    cache = make_cache(fake)

    async def scenario() -> ChatCompletionResponse | None:
        fake.fail_with = RedisConnectionError("down")
        await cache.set("k", make_response())  # lost during the outage
        fake.fail_with = None
        await cache.set("k", make_response("after recovery"))
        return await cache.get("k")

    result = asyncio.run(scenario())

    assert result is not None
    assert result.choices[0].message.content == "after recovery"
