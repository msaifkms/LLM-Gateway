"""GATEWAY_BENCH_MODE: extra Mock targets and routes for the benchmark suite."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.routes import BENCH_ROUTES, DEFAULT_ROUTES, build_routes
from app.main import app
from app.providers.registry import BENCH_PRICED_MODEL, BENCH_PRICED_TOKENS


def chat(client: TestClient, model: str, **extra: object):  # type: ignore[no-untyped-def]
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}], **extra},
    )


@pytest.fixture
def bench_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("GATEWAY_BENCH_MODE", "true")
    with TestClient(app) as test_client:
        yield test_client


def test_bench_routes_are_off_by_default(client: TestClient) -> None:
    aliases = [m["id"] for m in client.get("/v1/models").json()["data"]]

    assert sorted(aliases) == sorted(DEFAULT_ROUTES)
    assert chat(client, "bench-fallback").status_code == 400


def test_build_routes_only_adds_bench_routes_when_asked() -> None:
    assert build_routes() == DEFAULT_ROUTES
    assert build_routes(bench=True) == DEFAULT_ROUTES | BENCH_ROUTES
    assert not set(BENCH_ROUTES) & set(DEFAULT_ROUTES)


def test_bench_mode_lists_the_bench_routes(bench_client: TestClient) -> None:
    aliases = {m["id"] for m in bench_client.get("/v1/models").json()["data"]}

    assert aliases == set(DEFAULT_ROUTES) | set(BENCH_ROUTES)


def test_the_fallback_route_is_answered_by_the_healthy_mock(
    bench_client: TestClient,
) -> None:
    response = chat(bench_client, "bench-fallback")

    assert response.status_code == 200
    assert response.headers["x-provider"] == "mock-1"


def test_the_delay_route_waits_about_50_ms(bench_client: TestClient) -> None:
    import time

    started = time.perf_counter()
    response = chat(bench_client, "bench-delay-50")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert response.headers["x-provider"] == "mock-delay-50"
    assert elapsed >= 0.05


def test_the_priced_mock_reports_a_real_model_and_tokens(
    bench_client: TestClient,
) -> None:
    response = chat(bench_client, "bench-priced")

    body = response.json()
    assert body["model"] == BENCH_PRICED_MODEL
    assert (body["usage"]["prompt_tokens"], body["usage"]["completion_tokens"]) == (
        BENCH_PRICED_TOKENS
    )
    assert float(response.headers["x-cost-usd"]) > 0  # priced like the real model
