import httpx
import pytest
from fake_redis import FakeRedis
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api.chat import get_cache, get_router
from app.core.cache import ResponseCache
from app.core.router import Router
from app.core.routes import DEFAULT_ROUTES, CacheMode, Route
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
