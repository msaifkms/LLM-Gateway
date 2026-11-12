import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from test_quota_endpoint import BASE, FakeUsageStore, post
from test_quota_endpoint import install as install_quota
from test_rate_limit_endpoint import BODY, FakeLimiter
from test_rate_limit_endpoint import install as install_limiter

from app.api.killswitch import get_kill_switch
from app.api.metrics import router as metrics_router
from app.core import metrics
from app.core.cache import ResponseCache
from app.core.killswitch import KillSwitchUnavailable
from app.core.ratelimit import RateLimiterUnavailable, RateLimitResult
from app.main import app
from app.usage.recorder import record_usage
from app.usage.store import DailyUsage, UsageEvent


def sample(name: str, labels: dict[str, str] | None = None) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels or {}) or 0.0


class FakeSwitch:
    def __init__(self, on: bool = False, error: Exception | None = None) -> None:
        self.on = on
        self.error = error

    async def is_on(self) -> bool:
        if self.error is not None:
            raise self.error
        return self.on


def event(
    cache_status: str = "MISS",
    *,
    model: str = "metrics-test-model",
    priced: bool = True,
) -> UsageEvent:
    billed = cache_status in {"MISS", "BYPASS"}
    return UsageEvent(
        user_id=1,
        api_key_id=1,
        route="mock-1",
        provider="mock-1",
        model=model,
        cache_status=cache_status,
        prompt_tokens=100,
        completion_tokens=50,
        cost_usd=Decimal("0.25") if billed else Decimal(0),
        list_cost_usd=Decimal("0.25"),
        priced=priced,
    )


# Rate limiter


def rl(outcome: str) -> float:
    return sample("gateway_rate_limit_events_total", {"outcome": outcome})


def test_rate_limited_requests_are_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_limiter(
        monkeypatch,
        FakeLimiter(RateLimitResult(False, limit=60, remaining=0, retry_after_s=2)),
    )
    before = rl("limited")
    assert client.post("/v1/chat/completions", json=BODY).status_code == 429
    assert rl("limited") == before + 1


def test_limiter_errors_are_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_limiter(monkeypatch, FakeLimiter(error=RateLimiterUnavailable()))
    before = rl("unavailable")
    assert client.post("/v1/chat/completions", json=BODY).status_code == 503
    assert rl("unavailable") == before + 1


def test_fail_open_requests_are_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_limiter(monkeypatch, FakeLimiter(None))
    before = rl("fail_open")
    assert client.post("/v1/chat/completions", json=BODY).status_code == 200
    assert rl("fail_open") == before + 1


# Quotas


def quota(reason: str) -> float:
    return sample("gateway_quota_rejections_total", {"reason": reason})


def test_token_quota_rejections_are_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    principal = replace(BASE, daily_token_limit=500)
    install_quota(monkeypatch, principal, FakeUsageStore(DailyUsage(600, Decimal(0))))
    before = quota("tokens")
    assert post(client).status_code == 429
    assert quota("tokens") == before + 1


def test_usd_quota_rejections_are_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    principal = replace(BASE, daily_usd_limit=Decimal(1))
    install_quota(monkeypatch, principal, FakeUsageStore(DailyUsage(0, Decimal(2))))
    before = quota("usd")
    assert post(client).status_code == 429
    assert quota("usd") == before + 1


def test_unreadable_usage_is_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeUsageStore()
    store.error = OSError("database down")
    install_quota(monkeypatch, replace(BASE, daily_token_limit=500), store)
    before = quota("unavailable")
    assert post(client).status_code == 503
    assert quota("unavailable") == before + 1


# Kill switch


def blocks(reason: str) -> float:
    return sample("gateway_kill_switch_blocks_total", {"reason": reason})


def test_kill_switch_blocks_are_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        app.dependency_overrides, get_kill_switch, lambda: FakeSwitch(on=True)
    )
    before = blocks("disabled")
    assert client.post("/v1/chat/completions", json=BODY).status_code == 503
    assert blocks("disabled") == before + 1


def test_unknown_kill_switch_state_is_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch = FakeSwitch(error=KillSwitchUnavailable())
    monkeypatch.setitem(app.dependency_overrides, get_kill_switch, lambda: switch)
    before = blocks("unavailable")
    assert client.post("/v1/chat/completions", json=BODY).status_code == 503
    assert blocks("unavailable") == before + 1


def test_kill_switch_gauge_is_read_at_scrape_time() -> None:
    scrape_app = FastAPI()
    scrape_app.include_router(metrics_router)
    switch = FakeSwitch(on=True)
    scrape_app.state.kill_switch = switch
    client = TestClient(scrape_app)

    client.get("/metrics")
    assert sample("gateway_kill_switch_on") == 1

    switch.on = False
    client.get("/metrics")
    assert sample("gateway_kill_switch_on") == 0

    switch.error = KillSwitchUnavailable()
    assert client.get("/metrics").status_code == 200
    assert sample("gateway_kill_switch_on") == 0  # last known value kept


# Spend


def test_billed_usage_adds_tokens_and_cost() -> None:
    model = "metrics-test-billed"
    prompt = sample("gateway_tokens_total", {"model": model, "kind": "prompt"})
    completion = sample("gateway_tokens_total", {"model": model, "kind": "completion"})
    cost = sample("gateway_cost_usd_total", {"model": model})

    asyncio.run(record_usage(FakeUsageStore(), event("MISS", model=model)))

    assert (
        sample("gateway_tokens_total", {"model": model, "kind": "prompt"})
        == prompt + 100
    )
    assert (
        sample("gateway_tokens_total", {"model": model, "kind": "completion"})
        == completion + 50
    )
    assert sample("gateway_cost_usd_total", {"model": model}) == pytest.approx(
        cost + 0.25
    )


def test_cache_hits_add_savings_not_cost() -> None:
    model = "metrics-test-hit"
    saved = sample("gateway_cost_saved_usd_total", {"model": model})
    cost = sample("gateway_cost_usd_total", {"model": model})

    asyncio.run(record_usage(FakeUsageStore(), event("HIT", model=model)))

    assert sample("gateway_cost_saved_usd_total", {"model": model}) == pytest.approx(
        saved + 0.25
    )
    assert sample("gateway_cost_usd_total", {"model": model}) == cost


def test_unpriced_models_are_counted() -> None:
    model = "metrics-test-unpriced"
    before = sample("gateway_unpriced_requests_total", {"model": model})
    asyncio.run(record_usage(None, event("MISS", model=model, priced=False)))
    assert sample("gateway_unpriced_requests_total", {"model": model}) == before + 1


def test_usage_write_failures_are_counted_not_raised() -> None:
    class BrokenStore(FakeUsageStore):
        async def record(self, event: UsageEvent) -> None:
            raise OSError("database down")

    before = sample("gateway_usage_write_failures_total")
    asyncio.run(record_usage(BrokenStore(), event()))
    assert sample("gateway_usage_write_failures_total") == before + 1


# Redis


def test_a_cache_outage_is_counted_once() -> None:
    class BrokenRedis:
        async def get(self, key: str) -> None:
            raise RedisConnectionError("down")

    cache = ResponseCache(BrokenRedis(), ttl_s=60)  # type: ignore[arg-type]
    before = sample("gateway_redis_outages_total", {"component": "cache"})

    async def scenario() -> None:
        await cache.get("a")
        await cache.get("b")

    asyncio.run(scenario())
    assert sample("gateway_redis_outages_total", {"component": "cache"}) == before + 1
