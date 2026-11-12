import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.metrics import router as metrics_router
from app.core import metrics
from app.core.request_context import RequestContextMiddleware
from app.resilience.breaker import BreakerState


def sample(name: str, labels: dict[str, str] | None = None) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels or {}) or 0.0


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(metrics_router)

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    return app


class FakeRouter:
    def __init__(self, states: dict[str, BreakerState]) -> None:
        self._states = states

    def breaker_states(self) -> dict[str, BreakerState]:
        return self._states


def test_metrics_endpoint_serves_prometheus_text() -> None:
    response = TestClient(make_app()).get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE gateway_cache_requests_total counter" in response.text
    assert "# TYPE gateway_http_request_duration_seconds histogram" in response.text


def test_every_breaker_state_has_a_gauge_value() -> None:
    for state in BreakerState:
        assert metrics.state_value(state) in {0, 1, 2}


def test_closed_is_zero_and_open_is_two() -> None:
    assert metrics.state_value(BreakerState.CLOSED) == 0
    open_state = next(s for s in BreakerState if s.name.upper() == "OPEN")
    assert metrics.state_value(open_state) == 2


def test_scrape_reads_breaker_states_from_the_router() -> None:
    app = make_app()
    app.state.router = FakeRouter(
        {f"prov{i}": state for i, state in enumerate(BreakerState)}
    )
    TestClient(app).get("/metrics")
    for i, state in enumerate(BreakerState):
        value = sample("gateway_circuit_breaker_state", {"provider": f"prov{i}"})
        assert value == metrics.state_value(state)


def test_scrape_works_without_a_router() -> None:
    assert TestClient(make_app()).get("/metrics").status_code == 200


def test_http_metrics_use_the_route_template() -> None:
    labels = {"method": "GET", "route": "/items/{item_id}", "status": "200"}
    before = sample("gateway_http_requests_total", labels)
    client = TestClient(make_app())
    client.get("/items/1")
    client.get("/items/2")
    assert sample("gateway_http_requests_total", labels) == before + 2
    duration = {"method": "GET", "route": "/items/{item_id}"}
    assert sample("gateway_http_request_duration_seconds_count", duration) >= 2


def test_unmatched_paths_share_one_label() -> None:
    labels = {"method": "GET", "route": "unmatched", "status": "404"}
    before = sample("gateway_http_requests_total", labels)
    client = TestClient(make_app())
    client.get("/nope-1")
    client.get("/nope-2")
    assert sample("gateway_http_requests_total", labels) == before + 2


def test_metrics_endpoint_does_not_count_itself() -> None:
    labels = {"method": "GET", "route": "/metrics", "status": "200"}
    before = sample("gateway_http_requests_total", labels)
    TestClient(make_app()).get("/metrics")
    assert sample("gateway_http_requests_total", labels) == before


@pytest.mark.parametrize("result", ["HIT", "MISS", "COALESCED", "BYPASS"])
def test_cache_counter_accepts_every_outcome(result: str) -> None:
    before = sample("gateway_cache_requests_total", {"result": result})
    metrics.CACHE_REQUESTS.labels(result).inc()
    assert sample("gateway_cache_requests_total", {"result": result}) == before + 1
