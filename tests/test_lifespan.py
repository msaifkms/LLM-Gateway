import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.cache import ResponseCache
from app.core.router import Router
from app.main import app
from app.providers.anthropic import AnthropicProvider
from app.providers.openai import OpenAIProvider


def test_startup_builds_router_and_skips_redis_when_unconfigured(
    client: TestClient,
) -> None:
    assert isinstance(app.state.router, Router)
    assert app.state.redis is None
    assert app.state.cache is None


def test_gateway_starts_and_serves_when_redis_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Port 1 refuses connections immediately, like a Redis that is down.
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")

    with TestClient(app) as test_client:
        assert app.state.redis is not None
        assert isinstance(app.state.cache, ResponseCache)

        assert test_client.get("/healthz").status_code == 200

        # Temperature 0 makes the request cacheable, so the gateway really tries
        # Redis, fails, and must still answer.
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "mock-1",
                "messages": [{"role": "user", "content": "Hi"}],
                "temperature": 0,
            },
        )
        assert response.status_code == 200
        assert response.headers["x-cache"] == "MISS"


def test_adapters_share_one_pooled_http_client(client: TestClient) -> None:
    router: Router = app.state.router
    openai = router._providers["openai-default"]
    anthropic = router._providers["anthropic-default"]

    assert isinstance(openai, OpenAIProvider)
    assert isinstance(anthropic, AnthropicProvider)
    assert openai.client is anthropic.client
    assert isinstance(openai.client, httpx.AsyncClient)
    assert not openai.client.is_closed


def test_startup_skips_postgres_when_unconfigured(client: TestClient) -> None:
    assert app.state.db is None


def test_gateway_starts_when_postgres_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Port 1 refuses connections immediately, like a Postgres that is down.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/gateway")

    with TestClient(app) as test_client:
        assert app.state.db is not None  # the pool exists; it connects lazily
        assert test_client.get("/healthz").status_code == 200
        assert test_client.get("/readyz").status_code == 503
