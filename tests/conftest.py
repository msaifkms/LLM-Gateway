from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def no_external_services_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch a real Redis or Postgres unless they opt in.

    An empty URL means "not configured".
    """
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client with the app lifespan running (shared HTTP client, router)."""
    with TestClient(app) as test_client:
        yield test_client
