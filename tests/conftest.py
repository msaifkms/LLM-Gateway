from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.auth.store import Principal
from app.main import app

TEST_PRINCIPAL = Principal(
    user_id=1,
    user_name="test-user",
    key_id=1,
    rate_limit_per_min=60,
    daily_token_limit=None,
    daily_usd_limit=None,
    cache_enabled=True,
)


@pytest.fixture(autouse=True)
def no_external_services_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch a real Redis or Postgres unless they opt in.

    An empty URL means "not configured".
    """
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")


@pytest.fixture(autouse=True)
def signed_in_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests are about something other than auth, so they skip it.

    Tests of authentication itself remove this override (see test_auth_endpoint).
    """
    monkeypatch.setitem(app.dependency_overrides, authenticate, lambda: TEST_PRINCIPAL)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client with the app lifespan running (shared HTTP client, router)."""
    with TestClient(app) as test_client:
        yield test_client
