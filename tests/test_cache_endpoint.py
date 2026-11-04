import asyncio
from typing import Annotated

import httpx
import pytest
from fake_redis import FakeRedis
from fastapi import Header
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api.auth import authenticate
from app.api.chat import get_cache, get_router, get_singleflight
from app.auth.store import Principal
from app.core.cache import ResponseCache
from app.core.router import Router
from app.core.routes import DEFAULT_ROUTES, CacheMode, Route
from app.core.singleflight import SingleFlight
from app.main import app
from app.providers.mock import FailureMode, MockProvider


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake: FakeRedis | None,
    providers: dict[str, MockProvider] | None = None,
    routes: dict[str, Route] | None = None,
    ttl_s: int = 3600,
) -> dict[str, MockProvider]:
    """Replace the app's router and cache with test doubles."""
    all_providers = {
        "mock-1": MockProvider("fresh answer"),
        "openai-default": MockProvider("from openai"),
        "anthropic-default": MockProvider("from anthropic"),
        **(providers or {}),
    }
    router = Router(routes or DEFAULT_ROUTES, all_providers)
    cache = ResponseCache(fake, ttl_s=ttl_s) if fake is not None else None  # type: ignore[arg-type]

    monkeypatch.setitem(app.dependency_overrides, get_router, lambda: router)
    monkeypatch.setitem(app.dependency_overrides, get_cache, lambda: cache)
    return all_providers


def post(
    client: TestClient,
    content: str = "Hello!",
    *,
    model: str = "mock-1",
    temperature: float = 0,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
        },
        headers=headers or {},
    )


def test_second_identical_request_is_a_hit_without_calling_the_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = install(monkeypatch, fake=FakeRedis())

    first = post(client)
    second = post(client)

    assert first.status_code == second.status_code == 200
    assert first.headers["x-cache"] == "MISS"
    assert first.headers["x-provider"] == "mock-1"
    assert second.headers["x-cache"] == "HIT"
    assert "x-provider" not in second.headers  # no provider was called
    assert second.json() == first.json()
    assert providers["mock-1"].calls == 1


def test_different_prompts_do_not_share_an_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = install(monkeypatch, fake=FakeRedis())

    post(client, "first prompt")
    other = post(client, "second prompt")

    assert other.headers["x-cache"] == "MISS"
    assert providers["mock-1"].calls == 2


def test_non_deterministic_requests_bypass_the_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRedis()
    providers = install(monkeypatch, fake=fake)

    first = post(client, temperature=1.0)
    second = post(client, temperature=1.0)

    assert first.headers["x-cache"] == second.headers["x-cache"] == "BYPASS"
    assert providers["mock-1"].calls == 2
    assert fake.gets == fake.sets == 0  # Redis was never touched


def test_no_store_skips_both_reading_and_writing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRedis()
    providers = install(monkeypatch, fake=fake)

    skipped = post(client, headers={"Cache-Control": "no-store"})
    normal = post(client)  # nothing was stored by the no-store request

    assert skipped.headers["x-cache"] == "BYPASS"
    assert normal.headers["x-cache"] == "MISS"
    assert providers["mock-1"].calls == 2


def test_no_store_ignores_an_existing_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = install(monkeypatch, fake=FakeRedis())

    post(client)  # stored
    forced = post(client, headers={"Cache-Control": "max-age=0, no-store"})

    assert forced.headers["x-cache"] == "BYPASS"
    assert providers["mock-1"].calls == 2


def test_without_redis_every_response_is_a_bypass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = install(monkeypatch, fake=None)

    first = post(client)
    second = post(client)

    assert first.headers["x-cache"] == second.headers["x-cache"] == "BYPASS"
    assert providers["mock-1"].calls == 2


def test_redis_failure_never_fails_the_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRedis()
    fake.fail_with = RedisConnectionError("down")
    providers = install(monkeypatch, fake=fake)

    first = post(client)
    second = post(client)

    assert first.status_code == second.status_code == 200
    assert first.headers["x-cache"] == "MISS"  # attempted, but nothing was stored
    assert providers["mock-1"].calls == 2


def test_errors_are_never_cached(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRedis()
    failing = MockProvider(failure_mode=FailureMode.BAD_REQUEST)
    install(monkeypatch, fake=fake, providers={"mock-1": failing})

    response = post(client)

    assert response.status_code == 400
    assert fake.store == {}


def test_entries_expire_with_the_configured_ttl(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRedis()
    install(monkeypatch, fake=fake, ttl_s=90)

    post(client)

    assert list(fake.ttls.values()) == [90]


def test_anthropic_routes_are_not_cached_unless_they_opt_in(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = install(monkeypatch, fake=FakeRedis())

    first = post(client, model="chat-default")
    second = post(client, model="chat-default")

    assert first.headers["x-cache"] == second.headers["x-cache"] == "BYPASS"
    assert providers["anthropic-default"].calls == 2


def test_opted_in_route_caches_temperature_zero_requests(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {
        "opt-in": Route(
            targets=("anthropic-default",), cache=CacheMode.TEMPERATURE_ZERO
        )
    }
    providers = install(monkeypatch, fake=FakeRedis(), routes=routes)

    first = post(client, model="opt-in")
    second = post(client, model="opt-in")

    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert providers["anthropic-default"].calls == 1


def test_never_route_is_not_cached(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {"private": Route(targets=("mock-1",), cache=CacheMode.NEVER)}
    providers = install(monkeypatch, fake=FakeRedis(), routes=routes)

    post(client, model="private")
    second = post(client, model="private")

    assert second.headers["x-cache"] == "BYPASS"
    assert providers["mock-1"].calls == 2


# --- single-flight: concurrent identical requests --------------------------


async def post_concurrently(
    count: int, *, model: str = "mock-1"
) -> list[httpx.Response]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return await asyncio.gather(
            *[
                http.post(
                    "/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Hello!"}],
                        "temperature": 0,
                    },
                )
                for _ in range(count)
            ]
        )


def install_for_concurrency(
    monkeypatch: pytest.MonkeyPatch, provider: MockProvider, fake: FakeRedis
) -> None:
    # ASGITransport does not run the lifespan, so supply every dependency.
    router = Router(
        DEFAULT_ROUTES,
        {
            "mock-1": provider,
            "openai-default": MockProvider("unused"),
            "anthropic-default": MockProvider("unused"),
        },
    )
    cache = ResponseCache(fake, ttl_s=3600)  # type: ignore[arg-type]
    flight = SingleFlight()
    monkeypatch.setitem(app.dependency_overrides, get_router, lambda: router)
    monkeypatch.setitem(app.dependency_overrides, get_cache, lambda: cache)
    monkeypatch.setitem(app.dependency_overrides, get_singleflight, lambda: flight)


def test_concurrent_identical_misses_make_one_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MockProvider("shared answer", latency_s=0.2)
    fake = FakeRedis()
    install_for_concurrency(monkeypatch, provider, fake)

    responses = asyncio.run(post_concurrently(8))

    assert [r.status_code for r in responses] == [200] * 8
    assert provider.calls == 1  # not 8
    assert sorted(r.headers["x-cache"] for r in responses) == ["COALESCED"] * 7 + [
        "MISS"
    ]
    assert len({r.json()["id"] for r in responses}) == 1  # same answer for everyone
    assert fake.sets == 1


def test_a_later_identical_request_is_a_plain_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MockProvider(latency_s=0.05)
    install_for_concurrency(monkeypatch, provider, FakeRedis())

    asyncio.run(post_concurrently(3))
    later = asyncio.run(post_concurrently(1))

    assert later[0].headers["x-cache"] == "HIT"
    assert provider.calls == 1


def test_concurrent_requests_share_a_failure_instead_of_stampeding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MockProvider(latency_s=0.1, failure_mode=FailureMode.BAD_REQUEST)
    install_for_concurrency(monkeypatch, provider, FakeRedis())

    responses = asyncio.run(post_concurrently(5))

    assert [r.status_code for r in responses] == [400] * 5
    assert provider.calls == 1


# --- cache privacy: entries belong to the user who made them ----------------


def act_as_users_from_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticate as whoever the X-Test-User header names (default user 1)."""

    def override(
        x_test_user: Annotated[int, Header()] = 1,
        x_test_cache: Annotated[str, Header()] = "on",
    ) -> Principal:
        return Principal(
            user_id=x_test_user,
            user_name=f"user-{x_test_user}",
            key_id=x_test_user,
            rate_limit_per_min=60,
            daily_token_limit=None,
            daily_usd_limit=None,
            cache_enabled=x_test_cache == "on",
        )

    monkeypatch.setitem(app.dependency_overrides, authenticate, override)


def test_users_never_see_each_others_cached_answers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = install(monkeypatch, fake=FakeRedis())
    act_as_users_from_header(monkeypatch)

    alice_first = post(client, headers={"X-Test-User": "1"})
    bob_first = post(client, headers={"X-Test-User": "2"})  # same request, other user
    alice_again = post(client, headers={"X-Test-User": "1"})
    bob_again = post(client, headers={"X-Test-User": "2"})

    assert alice_first.headers["x-cache"] == "MISS"
    assert bob_first.headers["x-cache"] == "MISS"  # not HIT: Alice's entry is hers
    assert alice_again.headers["x-cache"] == "HIT"
    assert bob_again.headers["x-cache"] == "HIT"
    assert providers["mock-1"].calls == 2  # one call per user, not one in total


def test_a_key_with_caching_disabled_never_reads_or_writes_the_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRedis()
    providers = install(monkeypatch, fake=fake)
    act_as_users_from_header(monkeypatch)

    first = post(client, headers={"X-Test-Cache": "off"})
    second = post(client, headers={"X-Test-Cache": "off"})

    assert first.headers["x-cache"] == second.headers["x-cache"] == "BYPASS"
    assert providers["mock-1"].calls == 2
    assert fake.sets == 0  # nothing was stored for this key


def test_concurrent_identical_requests_from_different_users_do_not_coalesce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MockProvider("shared answer", latency_s=0.2)
    install_for_concurrency(monkeypatch, provider, FakeRedis())
    act_as_users_from_header(monkeypatch)

    async def scenario() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http:
            return await asyncio.gather(
                *[
                    http.post(
                        "/v1/chat/completions",
                        json={
                            "model": "mock-1",
                            "messages": [{"role": "user", "content": "Hello!"}],
                            "temperature": 0,
                        },
                        headers={"X-Test-User": str(user)},
                    )
                    for user in (1, 1, 1, 2, 2)
                ]
            )

    responses = asyncio.run(scenario())

    assert provider.calls == 2  # one shared call per user, never across users
    outcomes = sorted(r.headers["x-cache"] for r in responses)
    assert outcomes == ["COALESCED"] * 3 + ["MISS"] * 2
