import asyncio

import pytest
from fake_redis import FakeRedis
from fastapi.testclient import TestClient
from test_cache_endpoint import install, post
from test_router import REQUEST, make_router, run

from app.core import metrics
from app.core.routes import Route
from app.providers.mock import FailureMode, MockProvider
from app.resilience.errors import ProviderBadRequestError, ProviderServerError


def sample(name: str, labels: dict[str, str]) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def attempts(provider: str, outcome: str) -> float:
    return sample(
        "gateway_provider_attempts_total", {"provider": provider, "outcome": outcome}
    )


def test_successful_attempt_is_counted_and_timed() -> None:
    router, _ = make_router({"ma": MockProvider("x")}, Route(targets=("ma",)))
    before = attempts("ma", "success")
    count_before = sample(
        "gateway_provider_attempt_duration_seconds_count", {"provider": "ma"}
    )

    run(router)

    assert attempts("ma", "success") == before + 1
    count_after = sample(
        "gateway_provider_attempt_duration_seconds_count", {"provider": "ma"}
    )
    assert count_after == count_before + 1


def test_failure_then_fallback_is_counted() -> None:
    providers = {
        "fa": MockProvider(failure_mode=FailureMode.SERVER_ERROR),
        "fb": MockProvider("ok"),
    }
    router, _ = make_router(providers, Route(targets=("fa", "fb"), max_retries=0))
    failed_before = attempts("fa", "ProviderServerError")
    ok_before = attempts("fb", "success")
    fallback_before = sample("gateway_fallbacks_total", {"provider": "fa"})

    result = run(router)

    assert result.provider == "fb"
    assert attempts("fa", "ProviderServerError") == failed_before + 1
    assert attempts("fb", "success") == ok_before + 1
    assert sample("gateway_fallbacks_total", {"provider": "fa"}) == fallback_before + 1


def test_every_retry_is_counted_as_an_attempt() -> None:
    providers = {"ra": MockProvider(failure_mode=FailureMode.SERVER_ERROR)}
    router, _ = make_router(providers, Route(targets=("ra",), max_retries=2))
    before = attempts("ra", "ProviderServerError")

    with pytest.raises(ProviderServerError):
        run(router)

    assert attempts("ra", "ProviderServerError") == before + 3


def test_bad_request_is_counted_but_never_falls_back() -> None:
    providers = {
        "ba": MockProvider(failure_mode=FailureMode.BAD_REQUEST),
        "bb": MockProvider("never used"),
    }
    router, _ = make_router(providers, Route(targets=("ba", "bb"), max_retries=0))
    before = attempts("ba", "ProviderBadRequestError")
    fallback_before = sample("gateway_fallbacks_total", {"provider": "ba"})

    with pytest.raises(ProviderBadRequestError):
        run(router)

    assert attempts("ba", "ProviderBadRequestError") == before + 1
    assert sample("gateway_fallbacks_total", {"provider": "ba"}) == fallback_before


def test_breaker_states_cover_every_provider() -> None:
    providers = {
        "sa": MockProvider(failure_mode=FailureMode.SERVER_ERROR),
        "sb": MockProvider("fine"),
    }
    router, _ = make_router(
        providers, Route(targets=("sa",), max_retries=0), threshold=1
    )

    states = router.breaker_states()
    assert set(states) == {"sa", "sb"}
    assert all(state.name == "CLOSED" for state in states.values())

    with pytest.raises(ProviderServerError):
        asyncio.run(router.complete(REQUEST))

    after = router.breaker_states()
    assert after["sa"].name == "OPEN"
    assert after["sb"].name == "CLOSED"


def cache_count(result: str) -> float:
    return sample("gateway_cache_requests_total", {"result": result})


def test_cache_outcomes_are_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, fake=FakeRedis())
    miss, hit, bypass = cache_count("MISS"), cache_count("HIT"), cache_count("BYPASS")

    assert post(client).headers["x-cache"] == "MISS"
    assert post(client).headers["x-cache"] == "HIT"
    assert post(client, temperature=0.7).headers["x-cache"] == "BYPASS"

    assert cache_count("MISS") == miss + 1
    assert cache_count("HIT") == hit + 1
    assert cache_count("BYPASS") == bypass + 1
